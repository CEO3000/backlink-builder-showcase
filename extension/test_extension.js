// Tests the shared form filler (fill.js) and the Chrome extension's flow (content.js) in a simulated browser (jsdom).
// Run: node extension/test_extension.js   (jsdom comes from dashboard/node_modules)
const fs = require("fs"), path = require("path");
const { JSDOM } = require(path.join(__dirname, "..", "dashboard", "node_modules", "jsdom"));
const FILL = fs.readFileSync(path.join(__dirname, "fill.js"), "utf8");
const CONTENT = fs.readFileSync(path.join(__dirname, "content.js"), "utf8");

let failures = 0;
const ok = (cond, msg) => { if (!cond) { failures++; console.log("FAIL:", msg); } else console.log("ok  :", msg); };
const wait = (ms) => new Promise(r => setTimeout(r, ms));

const FORM = (extra) => `<form id="gp" action="/submit"><input name="your-name"><input type="email" name="your-email"><input name="post-title">
  <textarea name="your-message"></textarea><textarea name="author-bio"></textarea>
  <select name="cat"><option value="">Choose</option><option value="writing">Writing</option></select>
  <input type="checkbox" name="agree"><p style="display:none"><input name="hidden-one"></p>${extra || ""}<button type="submit">Send</button></form>`;
const PAYLOAD = {
  id: "abc123", site: "Genbooks", back: "https://owner.github.io/backlink-dashboard/?sent=abc123", form: { id: "gp", action: "/submit", index: 0 },
  fields: [{ name: "your-name", kind: "text", role: "name", value: "Genbooks team" }, { name: "your-email", kind: "text", role: "email", value: "support@genbooks.example" },
    { name: "post-title", kind: "text", role: "title", value: "Write for one reader: café notes" }, { name: "your-message", kind: "textarea", role: "article", value: "Line one.\n\nLine two." },
    { name: "author-bio", kind: "textarea", role: "bio", value: "The Genbooks team runs Genbooks (https://genbooks.uk/)." }, { name: "cat", kind: "select", role: "category", value: "writing" },
    { name: "agree", kind: "checkbox", role: "consent", value: true }]
};
const link = (p) => "#bl=" + Buffer.from(JSON.stringify(p), "utf8").toString("base64url");

function inject(d, code) { const el = d.createElement("script"); el.textContent = code; d.head.appendChild(el); }   // a real script tag, as in Chrome

function boot(body, hash) {
  const dom = new JSDOM(`<!doctype html><html><body><h1>Write for us</h1>${body}</body></html>`, { url: "https://writers.example.org/write-for-us" + (hash || ""), runScripts: "dangerously", pretendToBeVisual: true });
  const w = dom.window, d = w.document, seen = { submits: 0, inputs: 0 };
  d.addEventListener("submit", (e) => { e.preventDefault(); seen.submits++; }, true);
  d.addEventListener("input", () => { seen.inputs++; }, true);
  for (const k of ["offsetWidth", "offsetHeight"]) Object.defineProperty(w.HTMLElement.prototype, k, { configurable: true, get() { return k === "offsetWidth" ? 304 : 78; } });   // jsdom has no layout: give every element a real-looking size
  inject(d, FILL);
  return { w, d, seen };
}

(async () => {
  // ---------- fill.js
  let t = boot(FORM());
  let r = t.w.blFill(PAYLOAD);
  const v = (n) => t.d.querySelector(`[name="${n}"]`);
  ok(r.missing.length === 0 && r.filled === 7 && r.captcha === "none", "every prepared field is filled and there is no CAPTCHA");
  ok(v("your-name").value === "Genbooks team" && v("post-title").value === "Write for one reader: café notes" && v("your-message").value === "Line one.\n\nLine two.", "the values are exact, accents and line breaks included");
  ok(v("cat").value === "writing" && v("agree").checked === true, "a category is chosen and the terms box ticked");
  ok(t.seen.inputs >= 5, "input events fire, so the website's own scripts notice the values");
  ok(v("hidden-one").value === "", "a hidden field is never touched");
  r = t.w.blFill({ ...PAYLOAD, fields: [{ name: "hidden-one", kind: "text", role: "name", value: "x" }, { name: "nope", kind: "text", role: "email", value: "x" }] });
  ok(r.missing.join() === "name,email" && v("hidden-one").value === "", "a hidden or missing field is reported, never filled");
  ok(t.w.blFill({ ...PAYLOAD, form: { id: "other" } }).missing[0] === "the form", "a form that is not on the page is reported");
  ok(t.w.blSubmit(PAYLOAD) === true && t.seen.submits === 1, "Send presses the form's own button");

  t = boot(FORM('<div class="grecaptcha-badge"><iframe src="https://www.google.com/recaptcha/api2/anchor"></iframe></div><div class="g-recaptcha" data-sitekey="k" data-size="invisible"></div><textarea name="g-recaptcha-response" style="display:none"></textarea>'));
  ok(t.w.blCaptchaState() === "none", "an invisible reCAPTCHA (just a badge) is not something to wait for, even with its empty answer box on the page");
  t = boot(FORM('<div class="g-recaptcha" data-sitekey="k"></div><textarea name="g-recaptcha-response" style="display:none"></textarea>'));
  ok(t.w.blCaptchaState() === "waiting", "a CAPTCHA that is not answered yet is seen as waiting");
  t.d.querySelector("[name='g-recaptcha-response']").value = "x".repeat(40);
  ok(t.w.blCaptchaState() === "solved", "once the person answers it, it is seen as solved");

  t = boot("<p>Thank you, your article has been received. We will review it.</p>");
  ok(t.w.blResult(PAYLOAD).success === true, "a confirmation message counts as sent");
  t = boot(FORM('<span class="wpcf7-not-valid-tip">The field is required.</span>'));
  r = t.w.blResult(PAYLOAD);
  ok(r.error === true && r.errors[0] === "The field is required." && r.success === false, "a validation error shown by the site is reported, not called a success");

  // ---------- content.js (the extension)
  t = boot(FORM('<div class="g-recaptcha" data-sitekey="k"></div><textarea name="g-recaptcha-response" style="display:none"></textarea>'));
  inject(t.d, CONTENT);
  await wait(50);
  ok(!t.d.querySelector("[role=status]") && v === v, "without our #bl= marker the extension does nothing at all");

  t = boot(FORM('<div class="g-recaptcha" data-sitekey="k"></div><textarea name="g-recaptcha-response" style="display:none"></textarea>'), link(PAYLOAD));
  inject(t.d, CONTENT);
  await wait(100);
  const bar = () => t.d.querySelector("[role=status]").textContent;
  ok(t.w.location.hash === "", "the prepared data is removed from the address straight away");
  ok(t.d.querySelector("[name='your-message']").value === "Line one.\n\nLine two." && t.d.querySelector("[name='your-name']").value === "Genbooks team", "opening the link fills the form");
  ok(/tick the CAPTCHA/.test(bar()) && t.seen.submits === 0, "it stops at the CAPTCHA and asks the person to tick it, sending nothing yet");
  await wait(1500);
  ok(t.seen.submits === 0, "it keeps waiting while the CAPTCHA is not answered (it never touches the CAPTCHA)");
  t.d.querySelector("[name='g-recaptcha-response']").value = "y".repeat(40);
  await wait(1600);
  ok(t.seen.submits === 1, "as soon as the person answers the CAPTCHA, the form is sent once");
  await wait(4300);
  ok(/Sent/.test(bar()) && t.d.querySelector("[role=status] a").href === PAYLOAD.back, "it says it was sent and offers the way back to the dashboard");

  t = boot(FORM(), link(PAYLOAD));
  inject(t.d, CONTENT);
  await wait(3700);
  ok(t.seen.submits === 1, "without a CAPTCHA the extension fills, checks the values stay, and sends by itself");

  // ---------- a website that resets its form a moment after loading (Contact Form 7 on example-publisher.org, 2026-11-08: "it writes and disappears")
  t = boot(FORM('<div class="g-recaptcha" data-sitekey="k"></div><textarea name="g-recaptcha-response" style="display:none"></textarea>'), link(PAYLOAD));
  inject(t.d, CONTENT);
  await wait(100);
  t.d.getElementById("gp").reset();   // what the website's own script does
  ok(t.d.querySelector("[name='your-message']").value === "" && t.d.querySelector("[name='agree']").checked === false, "(the website's reset really empties the form)");
  await wait(800);
  ok(t.d.querySelector("[name='your-message']").value === "Line one.\n\nLine two." && t.d.querySelector("[name='your-name']").value === "Genbooks team" && t.d.querySelector("[name='agree']").checked === true && t.d.querySelector("[name='cat']").value === "writing",
     "while it waits for the person, values the website wiped are put back");
  t.d.getElementById("gp").reset();
  await wait(60);
  ok(t.d.querySelector("[name='your-message']").value === "Line one.\n\nLine two." && t.d.querySelector("[name='agree']").checked === true, "a reset is undone within a fraction of a second, so nothing is seen to vanish");
  ok(/helper/.test(t.d.querySelector("[role=status]").textContent), "the banner names the helper (with its version in Chrome), so a screenshot shows which code ran");
  t.d.querySelector("[name='your-message']").value = "My own words";
  await wait(1200);
  ok(t.d.querySelector("[name='your-message']").value === "My own words", "what the person typed is never overwritten");
  t.d.querySelector("[name='g-recaptcha-response']").value = "y".repeat(40);
  await wait(1600);
  ok(t.seen.submits === 1, "and the form is still sent once the person has ticked the CAPTCHA");

  t = boot(FORM(), link(PAYLOAD));
  let atSend = null;
  t.d.addEventListener("submit", () => { atSend = { name: t.d.querySelector("[name='your-name']").value, msg: t.d.querySelector("[name='your-message']").value, agree: t.d.querySelector("[name='agree']").checked }; }, true);
  inject(t.d, CONTENT);
  await wait(400);
  t.d.getElementById("gp").reset();
  await wait(3600);
  ok(t.seen.submits === 1 && atSend && atSend.name === "Genbooks team" && atSend.msg === "Line one.\n\nLine two." && atSend.agree === true, "a form the website emptied is full again at the moment it is sent");
  t = boot(FORM());
  t.w.blFill(PAYLOAD);
  ok(t.w.blRefill(PAYLOAD) === 0, "refilling a form that is still full changes nothing");
  t.d.querySelector("[name='post-title']").value = "";
  ok(t.w.blRefill(PAYLOAD) === 1 && t.d.querySelector("[name='post-title']").value === "Write for one reader: café notes", "refilling puts back exactly the box that was emptied");

  t = boot(FORM(), link({ ...PAYLOAD, fields: [...PAYLOAD.fields, { name: "missing-field", kind: "text", role: "phone", value: "1" }] }));
  inject(t.d, CONTENT);
  await wait(1000);
  ok(t.seen.submits === 0 && /could not be filled/.test(t.d.querySelector("[role=status]").textContent), "if any field cannot be filled, nothing is sent and the person is told");

  // ---------- a human-check question is never answered by us: it waits for the person
  const QUIZ = '<label>Humans only! 1 plus 4 equals?</label><input name="quiz" required>';
  const QPAYLOAD = { ...PAYLOAD, wait_for: [{ name: "quiz", label: "1 plus 4 equals?" }] };
  t = boot(FORM(QUIZ), link(QPAYLOAD));
  inject(t.d, CONTENT);
  await wait(1500);
  ok(t.seen.submits === 0 && /answer the question/.test(t.d.querySelector("[role=status]").textContent) && t.d.querySelector("[name=quiz]").value === "", "it fills everything else but leaves the human question empty and asks the person");
  t.d.querySelector("[name=quiz]").value = "5";
  await wait(1700);
  ok(t.seen.submits === 1, "once the person has answered it, the form is sent");


  // ---------- a CAPTCHA that belongs to another form (the blog's comment box) is not ours (found by the owner on 2026-11-02)
  const COMMENTS = (inside) => '<form id="commentform"><textarea name="comment"></textarea>' + (inside || "") + '<button type="submit">Post Comment</button></form>';
  const RECAPTCHA = '<div class="g-recaptcha" data-sitekey="k"></div><textarea name="g-recaptcha-response" style="display:none"></textarea>';
  t = boot(FORM() + COMMENTS(RECAPTCHA) + '<script src="https://www.google.com/recaptcha/api.js"></script>');
  ok(t.w.blCaptchaState(PAYLOAD) === "none" && t.w.blCaptchaPresent(PAYLOAD) === false, "a CAPTCHA inside another form (the comment box) does not count for ours");
  ok(t.w.blCaptchaState() === "waiting", "without a payload every CAPTCHA on the page still counts (the careful reading)");
  t = boot(FORM() + COMMENTS(RECAPTCHA) + '<div><iframe title="recaptcha challenge expires in two minutes" src="https://www.google.com/recaptcha/api2/bframe?hl=en"></iframe></div>');
  ok(t.w.blCaptchaPresent(PAYLOAD) === false, "the comment box CAPTCHA's picture pop-up, outside every form, does not count for ours either");
  t = boot(FORM(RECAPTCHA) + COMMENTS());
  ok(t.w.blCaptchaState(PAYLOAD) === "waiting" && t.w.blCaptchaPresent(PAYLOAD) === true, "a CAPTCHA inside our own form counts");
  t = boot(FORM() + '<script src="https://www.google.com/recaptcha/api.js"></script>');
  ok(t.w.blCaptchaPresent(PAYLOAD) === true, "a CAPTCHA script with no widget anywhere may protect our form, so the automatic sender leaves it to the person");

  t = boot(FORM() + COMMENTS(RECAPTCHA), link(PAYLOAD));
  inject(t.d, CONTENT);
  await wait(3700);
  ok(t.seen.submits === 1 && !t.d.querySelector("#commentform .g-recaptcha").style.outline && t.d.querySelector("[name=comment]").value === "",
     "the extension sends a form without its own CAPTCHA, never points at the comment box's CAPTCHA and never touches the comment box");

  // ---------- trap fields: an optional box that is not shown is left alone
  t = boot(FORM('<div style="display:none"><input name="website_url_check"></div>'));
  r = t.w.blFill({ ...PAYLOAD, fields: [...PAYLOAD.fields, { name: "website_url_check", kind: "text", role: "website", value: "https://genbooks.uk/", required: false }] });
  ok(r.missing.length === 0 && t.d.querySelector("[name=website_url_check]").value === "", "an optional box that is not shown is left empty and does not stop the form");
  r = t.w.blFill({ ...PAYLOAD, fields: [{ name: "website_url_check", kind: "text", role: "website", value: "x", required: true }] });
  ok(r.missing.join() === "website" && t.d.querySelector("[name=website_url_check]").value === "", "a required box that is not shown is reported, never filled");

  console.log(failures ? `\n${failures} FAILURE(S)` : "\nALL EXTENSION CHECKS PASSED");
  process.exit(failures ? 1 : 0);
})();
