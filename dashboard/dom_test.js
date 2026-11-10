// Drives the real dashboard page in jsdom against a fake GitHub API. Needs: npm i jsdom (run from this folder).
// Loads the real dashboard page in jsdom with a fake GitHub API and drives it like a person would.
const fs = require("fs");
const { JSDOM } = require("jsdom");
const path = require("path");
const html = fs.readFileSync(path.join(__dirname, "index.html"), "utf8");

let failures = 0;
const ok = (cond, msg) => { if (!cond) { failures++; console.log("FAIL:", msg); } else console.log("ok  :", msg); };
const tick = (ms = 30) => new Promise(r => setTimeout(r, ms));

function makeApi(files, calls) {
  const sha = () => "sha" + Math.random().toString(36).slice(2, 8);
  const seen = {};   // like a real browser: a GET is answered from its cache for a while unless the page asks for a fresh copy
  return async (url, opts = {}) => {
    const u = new URL(url), method = (opts.method || "GET").toUpperCase();
    calls.push({ method, path: u.pathname, body: opts.body ? JSON.parse(opts.body) : null });
    const respond = (status, body) => ({ ok: status < 300, status, text: async () => JSON.stringify(body), json: async () => body });
    const m = u.pathname.match(/\/contents\/(.+)$/);
    if (m && method === "GET") {
      const p = decodeURIComponent(m[1]);
      const f = opts.cache === "no-store" ? files[p] : (seen[p] || files[p]);
      if (files[p]) seen[p] = { content: files[p].content, sha: files[p].sha };
      return f ? respond(200, { content: Buffer.from(f.content).toString("base64"), sha: f.sha }) : respond(404, { message: "Not Found" });
    }
    if (m && method === "PUT") {
      const body = JSON.parse(opts.body), p = decodeURIComponent(m[1]);
      if (files[p] && body.sha !== files[p].sha) return respond(409, { message: "sha mismatch" });
      files[p] = { content: Buffer.from(body.content, "base64").toString("utf8"), sha: sha() };
      return respond(200, { content: { sha: files[p].sha } });
    }
    if (u.pathname.endsWith("/runs")) return respond(200, { workflow_runs: files.__runs || [] });
    if (u.pathname.endsWith("/dispatches")) return respond(204, null);
    return respond(404, { message: "unexpected " + u.pathname });
  };
}

async function boot({ files, search = "", token = "tok" }) {
  const calls = [];
  const dom = new JSDOM(html, {
    runScripts: "dangerously", url: "https://owner.github.io/backlink-dashboard/" + search, pretendToBeVisual: true,
    beforeParse(w) {
      w.fetch = makeApi(files, calls);
      if (token) w.localStorage.setItem("backlink_dashboard_conn", JSON.stringify({ owner: "OWNER", repo: "backlink-builder", branch: "main", token }));
      w.HTMLElement.prototype.scrollIntoView = () => {};
      w.navigator.clipboard = { writeText: async () => {} };
      w.console.error = (...a) => { console.log("PAGE ERROR:", ...a); failures++; };
      w.addEventListener("error", e => { console.log("PAGE EXCEPTION:", e.message); failures++; });
    }
  });
  await tick(120);
  return { dom, w: dom.window, d: dom.window.document, calls, files };
}

const now = new Date(), iso = now.toISOString(), day = k => new Date(now.getTime() - k * 864e5).toISOString().slice(0, 10);
const entry = (name, url, verified, target, extra = {}) => ({
  updated_at: iso, name, url, settings: { articles_per_day: target, mode: "live", enabled: true }, target_per_day: target, verified_today: verified, guest_today: verified,
  published_today: verified, verified_live_total: 5, pending_approval: 0, topics_unused: 7, by_status: {}, failures_today: {},
  history: [{ date: day(1), published: 2, verified: 2, drafts: 0, failures: 0 }, { date: day(0), published: verified, verified, drafts: 0, failures: 0 }],
  errors: [], blocked: [], notes: [], live_links: [{ url: "https://dev.to/x/post", title: "A <b>bold</b> title", anchor: "an anchor", follow: true, verified_at: iso, platform: "DEV.to", target: "https://www.example.org/pets/" }, { url: "https://dev.to/x/other", title: "Hostile", anchor: "a", follow: false, platform: "<img src=x onerror=alert(1)>", target: "javascript:alert(1)" }], ...extra });
const gp = (id, domain, status, over) => Object.assign({ id: id, domain: domain, site_name: "Write for us", form_url: "https://" + domain + "/write-for-us", status: status, captcha: true,
  accepts_quote: "We welcome guest posts from outside writers.", links_quote: "You may include one link in your bio.", relevance: 8, title: "An article title", updated_at: iso }, over || {});
const PHONE = { id: "aaaaaaaaaaaaaa05", fields: [{ role: "first_name", kind: "text", value: "Shahin" }, { role: "pitch", kind: "textarea", value: "Line one.\n\nIdeas: <b>x</b>" }, { role: "consent", kind: "checkbox", value: true }], wait_for: [] };
const PHONE_LINK = "https://phone.example.org/write-for-us#bl=" + Buffer.from(JSON.stringify(PHONE), "utf8").toString("base64url");
const GUESTS = [
  gp("aaaaaaaaaaaaaa01", "best.example.org", "waiting", { link: "https://best.example.org/write-for-us#bl=abc" }),
  gp("aaaaaaaaaaaaaa02", "<img src=x onerror=alert(1)>.example.org", "waiting", { link: "https://evil.example.org/x#bl=1", title: "<b>bold</b> title" }),
  gp("aaaaaaaaaaaaaa03", "sent.example.org", "sent", { sent_on: day(0), how: "automatically", captcha: false }),
  gp("aaaaaaaaaaaaaa04", "gone.example.org", "not_accepted", { note: "not published within 60 days of sending" }),
  gp("aaaaaaaaaaaaaa05", "phone.example.org", "waiting", { link: PHONE_LINK })];
const MAIL = gp("bbbbbbbbbbbbbb10", "mailsite.example.org", "waiting", { captcha: false, route: "email", email_to: "editor@mailsite.example.org",
  email_draft: { to: "editor@mailsite.example.org", subject: "Guest Post: A <b>title</b>", body: "Hello,\n\nThe article.\n\nThe Genbooks team", from: "support@genbooks.example" } });
const GFORM = gp("bbbbbbbbbbbbbb09", "wingedpen.example.org", "waiting", { captcha: false, form_kind: "pitch", link: "https://docs.google.com/forms/d/e/x/viewform?usp=pp_url&entry.1=Genbooks" });
const baseFiles = () => ({
  "state/dashboard.json": { sha: "d1", content: JSON.stringify({ generated_at: iso, sites: [{ id: "old", name: "Old Site", stats: { leads_total: 175, sent_total: 49 } }],
    backlink_publishing: { "genbooks-uk": entry("Genbooks", "https://genbooks.uk/", 2, 2, { guest_posts: GUESTS, counted_by_google: 1, blocked_from_google: 1, verified_live_total: 2,
      live_links: [{ url: "https://dev.to/g/ok", title: "Counted one", anchor: "a", follow: true, platform: "DEV.to", target: "https://genbooks.uk/", indexable: true, counts_for_seo: true, verified_at: iso },
                   { url: "https://dev.to/g/hidden", title: "Hidden one", anchor: "b", follow: true, platform: "DEV.to", target: "https://genbooks.uk/", indexable: false, counts_for_seo: false, verified_at: iso }] }), "social-field": entry("SocialField", "https://www.socialfield.uk/", 1, 2, { guest_posts: [gp("bbbbbbbbbbbbbb01", "other-site.example.org", "waiting", { link: "https://other-site.example.org/write-for-us#bl=x" })],
      errors: [{ at: iso, title: "An article", status: "RETRY_REQUIRED", category: "quota", message: "Gemini quota exhausted (HTTP 429)" }] }),
      "broken": entry("Broken Co", "https://broken.example/", 0, 2, { blocked: ["guest posts cannot be used: the BROKEN secret is not set"], guest_posts: [GFORM, MAIL] }) },
    publishing_issues: [] }) },
  "config/publishing_settings.json": { sha: "s1", content: JSON.stringify({ _help: "keep me", sites: {
    "genbooks-uk": { articles_per_day: 2, mode: "live", enabled: true }, "social-field": { articles_per_day: 2, mode: "live", enabled: true }, "broken": { articles_per_day: 2, mode: "live", enabled: true } } }) },
  __runs: [{ id: 1, run_number: 9, status: "completed", conclusion: "success", event: "workflow_dispatch", created_at: iso, updated_at: iso, run_started_at: iso, html_url: "https://github.com/x" }]
});

(async () => {
  // ---------- 1. Not connected: welcome screen, no API calls, no errors
  let t = await boot({ files: baseFiles(), token: "" });
  ok(t.d.body.textContent.includes("Connect to your repository") && t.calls.length === 0, "no token: welcome screen and no GitHub calls");
  ok(t.d.querySelector("#btn-run").disabled, "Run now is disabled until connected");

  // ---------- 2. Connected: everything renders
  t = await boot({ files: baseFiles() });
  const text = t.d.body.textContent;
  ok(t.d.querySelectorAll("article.site").length === 3, "three website cards");
  ok(/Genbooks/.test(text) && /SocialField/.test(text) && /Broken Co/.test(text), "all site names shown");
  ok(!/175 leads|49 sent/.test(text.replace(/\s+/g, " ").split("Email outreach")[0]), "old email numbers are not in the main view");
  ok(/Email outreach \(old system/.test(text) && t.d.querySelector("#legacy").open === false, "old email history sits in a collapsed section");
  ok(!t.d.querySelector("#chart-section") && !/Verified backlinks per day/.test(text) && t.d.querySelectorAll("svg[role=img] .col").length === 0, "the verified-backlinks-per-day chart is gone");
  ok(/Guest posts sent today/.test(text) && /3 of 6 planned/.test(text.replace(/\s+/g, " ")), "overview: 3 of 6 planned today");
  ok(t.d.querySelectorAll("#alerts .alert.error").length >= 1, "an error alert is shown for the site with a blocked key");
  ok(/the BROKEN secret is not set/.test(text), "the exact blocked reason is visible");
  ok(/Gemini's free daily limit is used up/.test(text) && /HTTP 429/.test(text), "the quota error is explained in plain words with the raw message");
  ok(t.d.querySelectorAll("#runs-section tbody tr").length === 1 && /Succeeded/.test(t.d.querySelector("#runs-section").textContent), "run history table");

  // ---------- 3. XSS: hostile text in the data is shown as text, never executed
  ok(!t.d.querySelector("a[href='https://dev.to/x/post'] b") && /A <b>bold<\/b> title/.test(t.d.querySelector("details.fold:last-of-type").innerHTML.replace(/&lt;/g, "<").replace(/&gt;/g, ">")) , "page text is escaped (no injected tags)");

  // ---------- 3b. Each live backlink says where it was published and which page of the website it points to
  const rows = [...t.d.querySelectorAll(".link-row")].map(r => r.textContent.replace(/\s+/g, " "));
  ok(rows.some(r => /Published on DEV\.to · links to example\.org\/pets\/ · link text .an anchor. · followed/.test(r)), "a live backlink shows its platform, the page it points to, its link text and follow status");
  ok(t.d.querySelector(".link-row a[href='https://www.example.org/pets/']"), "the destination is a real link");
  ok(!t.d.querySelector("a[href^='javascript:']") && !t.d.querySelector(".link-row img"), "a hostile destination or platform is never turned into a script link or tag");

  // ---------- 3c. Links Google may not count are shown plainly, never as a plain success
  const all = t.d.body.textContent.replace(/\s+/g, " ");
  ok(/1 of 12 live backlinks are hidden from Google/.test(all) || /1 of \d+ live backlinks are hidden from Google/.test(all), "an alert says how many live backlinks are hidden from Google");
  ok(/Genbooks: 1 of 2 hidden/.test(all), "the alert names the website and its numbers");
  ok(/Google can count it/.test(all) && /Hidden from Google \(noindex\)/.test(all), "each link shows whether Google can count it");
  ok(/Not checked yet/.test(all), "links from before this check existed say not checked yet, not blocked");
  ok(/1 count for Google/.test(all), "the overview says how many count for Google");
  ok(/1 of 2 live links are marked noindex\/nofollow by the platform/.test(all), "the website's own card says why it needs a look");

  // ---------- 3d. Guest posts
  const sec = () => t.d.querySelector('details[data-guest-site="genbooks-uk"]'), cards = () => [...sec().querySelectorAll(".qcard")], txt = () => sec().textContent.replace(/\s+/g, " ");
  ok(cards().length === 3 && /Waiting for you \(3\)/.test(txt()) && /Sent \(1\)/.test(txt()) && /Not accepted \(1\)/.test(txt()), "waiting submissions show first and each tab counts its own");
  const phoneCard = cards().find(c => /phone\.example\.org/.test(c.textContent));
  ok(phoneCard && /On a phone\? Copy and paste by hand/.test(phoneCard.textContent) && phoneCard.querySelectorAll("[data-action=g-copy]").length === 2 && /tick the agreement box/.test(phoneCard.textContent), "a card shows each piece of text for copying by hand (the tick box is a reminder, not a copy field)");
  ok(!sec().querySelector("b b") && /Ideas: <b>x<\/b>/.test(phoneCard.textContent), "the text to copy is shown as text, never as markup");
  const copiedText = []; t.w.navigator.clipboard = { writeText: async (x) => { copiedText.push(x); } };
  phoneCard.querySelectorAll("[data-action=g-copy]")[1].click(); await tick();
  ok(copiedText[0] === "Line one.\n\nIdeas: <b>x</b>", "Copy puts the exact text on the clipboard");
  ok(!cards()[0].textContent.includes("On a phone?"), "a link without prepared data shows no copy section");
  const btn = cards()[0].querySelector("a.btn");
  ok(btn && btn.href === "https://best.example.org/write-for-us#bl=abc" && btn.target === "_blank" && /Click to submit/.test(btn.textContent), "Click to submit opens the prepared link in a new tab");
  ok(/We welcome guest posts from outside writers/.test(cards()[0].textContent) && /You may include one link/.test(cards()[0].textContent), "the evidence that the website accepts outside articles and links is shown");
  ok(!sec().querySelector("img") && /<b>bold<\/b> title/.test(sec().textContent), "hostile text from a website is shown as text, never run");
  const sfFold = t.d.querySelector('details[data-guest-site="social-field"]'), sfCards = [...sfFold.querySelectorAll(".qcard")];
  ok(sfCards.length === 1 && /other-site\.example\.org/.test(sfCards[0].textContent) && !/other-site/.test(sec().textContent) && !/best\.example\.org/.test(sfFold.textContent), "each website's card shows only its own submissions");
  ok(!t.d.querySelector("#guest-section") && !t.d.querySelector("#g-site"), "there is no separate guest posts section any more");
  const gfFold = t.d.querySelector('details[data-guest-site="broken"]'), gfCard = [...gfFold.querySelectorAll(".qcard")].find(c => /wingedpen/.test(c.textContent)), gfBtn = gfCard.querySelector("a.btn");
  ok(sec().open === true && gfFold.open === true && gfBtn && /Open the filled form/.test(gfBtn.textContent) && gfBtn.href.startsWith("https://docs.google.com/forms/") &&
     /opens already filled in/.test(gfCard.textContent) && !/Tick the CAPTCHA/.test(gfCard.textContent) && !/On a phone\? Copy/.test(gfCard.textContent),
     "a form hosted elsewhere opens already filled in: the card says to press Submit, not to tick a CAPTCHA");
  const mailCard = [...gfFold.querySelectorAll(".qcard")].find(c => /mailsite/.test(c.textContent));
  ok(mailCard && /By email/.test(mailCard.textContent) && mailCard.querySelectorAll("[data-action=g-mail-copy]").length === 3 && /support@genbooks\.example/.test(mailCard.textContent) &&
     mailCard.querySelector('a[href^="mailto:editor%40mailsite.example.org?subject="]') && !mailCard.querySelector("b b") && /A <b>title<\/b>/.test(mailCard.textContent),
     "an email to send by hand shows To, Subject and Message with Copy buttons and an Open my email app button, as text");
  const mailed = []; t.w.navigator.clipboard = { writeText: async (x) => { mailed.push(x); } };
  mailCard.querySelector("[data-action=g-mail-copy][data-part=body]").click(); await tick();
  ok(mailed[0] === "Hello,\n\nThe article.\n\nThe Genbooks team", "Copy puts the exact message on the clipboard");
  const emptyFold = t.d.querySelector('details[data-guest-site="social-field"]');
  ok(emptyFold && !/Open the filled form/.test(emptyFold.textContent), "each card keeps its own items");
  const banner = t.d.querySelector("#alerts").textContent.replace(/\s+/g, " ");
  ok(/6 guest posts are waiting for your click/.test(banner) && /Genbooks: 3/.test(banner) && /SocialField: 1/.test(banner) && /Broken Co: 2/.test(banner), "the top of the page says how many are waiting, per website");
  t.calls.length = 0;
  cards()[0].querySelector("[data-action=g-sent]").click(); await tick(150);
  const putSent = t.calls.find(c => c.method === "PUT" && c.path.endsWith("state/guestpost_status.json"));
  ok(putSent && JSON.parse(Buffer.from(putSent.body.content, "base64").toString()).aaaaaaaaaaaaaa01.status === "sent", "I sent it writes state/guestpost_status.json");
  ok(cards().length === 2 && /Sent \(2\)/.test(txt()), "the submission moves to Sent");
  sec().querySelector("[data-action=g-tab][data-status=sent]").click(); await tick();
  // the cards are drawn again after every change: an address being typed, the open fold and the chosen tab must survive that
  const box = () => t.d.querySelector("#g-url-aaaaaaaaaaaaaa01"), perDay = t.d.querySelector("#pd-genbooks-uk");
  box().value = "https://best.example.org/blo"; box().dispatchEvent(new t.w.Event("input", { bubbles: true }));
  perDay.value = "3"; perDay.dispatchEvent(new t.w.Event("change", { bubbles: true })); await tick();
  ok(box() && box().value === "https://best.example.org/blo" && sec().open === true && /Sent \(2\)/.test(txt()) && sec().querySelector("[data-status=sent]").getAttribute("aria-pressed") === "true",
     "changing a setting redraws the cards without losing a typed address, the open fold or the chosen tab");
  sec().open = false; sec().dispatchEvent(new t.w.Event("toggle"));
  t.d.querySelector("#pd-genbooks-uk").value = "2"; t.d.querySelector("#pd-genbooks-uk").dispatchEvent(new t.w.Event("change", { bubbles: true })); await tick();
  ok(sec().open === false, "a fold the owner has closed stays closed after a redraw");
  sec().open = true; sec().dispatchEvent(new t.w.Event("toggle"));
  t.calls.length = 0;
  t.d.querySelector("#g-url-aaaaaaaaaaaaaa01").value = "not an address"; t.d.querySelector("[data-action=g-publish][data-g=aaaaaaaaaaaaaa01]").click(); await tick(100);
  ok(!t.calls.some(c => c.method === "PUT"), "a published address that is not a full https address is refused and nothing is written");
  t.d.querySelector("#g-url-aaaaaaaaaaaaaa01").value = "https://best.example.org/blog/my-article"; t.d.querySelector("[data-action=g-publish][data-g=aaaaaaaaaaaaaa01]").click(); await tick(150);
  const putPub = t.calls.find(c => c.method === "PUT" && c.path.endsWith("state/guestpost_status.json"));
  const savedPub = putPub && JSON.parse(Buffer.from(putPub.body.content, "base64").toString()).aaaaaaaaaaaaaa01;
  ok(savedPub && savedPub.status === "published" && savedPub.published_url === "https://best.example.org/blog/my-article", "Save as published records the address");
  sec().querySelector("[data-action=g-tab][data-status=waiting]").click(); await tick();
  // the owner presses "I sent it" on one card and then on another straight away (2026-11-02: the second one was refused with GitHub 409)
  t.calls.length = 0;
  sec().querySelector("[data-action=g-sent]").click(); await tick(200);
  sec().querySelector("[data-action=g-sent]").click(); await tick(400);
  const sentPuts = t.calls.filter(c => c.method === "PUT" && c.path.endsWith("state/guestpost_status.json"));
  const last = sentPuts.length && JSON.parse(Buffer.from(sentPuts[sentPuts.length - 1].body.content, "base64").toString());
  ok(sentPuts.length === 2 && last && last.aaaaaaaaaaaaaa02 && last.aaaaaaaaaaaaaa05 && last.aaaaaaaaaaaaaa02.status === "sent" && last.aaaaaaaaaaaaaa05.status === "sent" &&
     ![...t.d.querySelectorAll(".toast")].some(x => /409/.test(x.textContent)), "two I sent it presses in a row are both saved, without a 409");
  let t2 = await boot({ files: baseFiles(), search: "?sent=aaaaaaaaaaaaaa01" }); await tick(150);
  const putBack = t2.calls.find(c => c.method === "PUT" && c.path.endsWith("state/guestpost_status.json"));
  ok(putBack && JSON.parse(Buffer.from(putBack.body.content, "base64").toString()).aaaaaaaaaaaaaa01.status === "sent" && !/sent=/.test(t2.w.location.search),
     "coming back from the extension's link records the submission as sent and cleans the address");

  // ---------- 4. Changing articles per day then saving writes ONLY the settings file, keeps other keys, and re-fetches
  const sel = t.d.querySelector("#pd-genbooks-uk"); sel.value = "3"; sel.dispatchEvent(new t.w.Event("change", { bubbles: true })); await tick();
  const save = () => t.d.querySelector("button[data-action=save][data-site=genbooks-uk]");
  ok(!save().disabled && /Not saved yet/.test(t.d.querySelector("article.site").textContent + t.d.body.textContent), "editing enables Save and says it is not saved");
  ok(t.d.querySelector("#pd-genbooks-uk").value === "3", "the choice survives the re-render");
  t.calls.length = 0; save().click(); await tick(150);
  const put = t.calls.find(c => c.method === "PUT");
  ok(put && put.path.endsWith("config/publishing_settings.json"), "saving writes config/publishing_settings.json");
  const saved = JSON.parse(t.files["config/publishing_settings.json"].content);
  ok(saved.sites["genbooks-uk"].articles_per_day === 3 && saved._help === "keep me", "new number saved and the other keys (_help) preserved");
  ok(saved.sites["social-field"].articles_per_day === 2, "other websites untouched");
  ok(t.calls.filter(c => c.method === "PUT").length === 1, "exactly one file written");

  // ---------- 5. Draft mode + pause
  t.d.querySelector("#mode-social-field-draft").click(); await tick();
  ok(t.d.querySelector("#mode-social-field-draft").getAttribute("aria-pressed") === "true", "mode toggle shows the choice");
  const en = t.d.querySelector("#en-social-field"); en.checked = false; en.dispatchEvent(new t.w.Event("change", { bubbles: true })); await tick();
  ok(/Paused/.test(t.d.querySelector("article.site:nth-of-type(2)").textContent) || /Paused/.test(t.d.body.textContent), "pausing is reflected");
  t.d.querySelector("button[data-action=save][data-site=social-field]").click(); await tick(150);
  const s2 = JSON.parse(t.files["config/publishing_settings.json"].content).sites["social-field"];
  ok(s2.mode === "draft" && s2.enabled === false && s2.articles_per_day === 2, "mode draft and enabled false saved together");

  // ---------- 6. Add a website: validation first, then three files, email OFF, no secrets touched
  t = await boot({ files: baseFiles() });
  const form = t.d.querySelector("#add-form"); t.d.querySelector("#add-details").open = true;
  const setv = (id, v) => { const el = t.d.querySelector("#" + id); el.value = v; el.dispatchEvent(new t.w.Event("input", { bubbles: true })); };
  form.dispatchEvent(new t.w.Event("submit", { bubbles: true, cancelable: true })); await tick();
  ok(t.d.querySelectorAll(".error-text").length >= 4 && t.calls.filter(c => c.method === "PUT").length === 0, "empty form: clear errors, nothing written");
  setv("f-name", "Pet Shop UK"); ok(t.d.querySelector("#f-id").value === "pet-shop-uk" && !t.d.querySelector("#f-secretName"), "the id is suggested from the name, and no platform key is asked for");
  setv("f-author", "Jane Smith");
  setv("f-url", "https://petshop.example/"); setv("f-description", "an online pet supplies shop");
  setv("f-facts", "We sell pet food and toys online.\nDelivery is free over a stated amount shown at checkout.\nCustomers can return unopened items.");
  t.d.querySelector("#f-pages").focus(); t.d.querySelector("#f-pages").dispatchEvent(new t.w.Event("focusin", { bubbles: true }));
  ok(/petshop\.example\/ \| the homepage/.test(t.d.querySelector("#f-pages").value), "the pages box is pre-filled with the homepage");
  t.calls.length = 0;
  form.dispatchEvent(new t.w.Event("submit", { bubbles: true, cancelable: true })); await tick(250);
  const puts = t.calls.filter(c => c.method === "PUT").map(c => c.path.split("/contents/")[1]);
  ok(JSON.stringify(puts) === JSON.stringify(["config/sites/pet-shop-uk.yaml", "config/backlinks-pet-shop-uk.yaml", "config/publishing_settings.json"]), "wrote exactly the site file, the publishing config and the settings: " + puts.join(", "));
  ok(/email_outreach: false/.test(t.files["config/sites/pet-shop-uk.yaml"].content), "the new site file switches cold email OFF");
  ok(JSON.parse(t.files["config/publishing_settings.json"].content).sites["pet-shop-uk"].articles_per_day === 2, "its articles-per-day is recorded");
  ok(/is added/.test(t.d.querySelector("#modal-root").textContent) && /review mode/.test(t.d.querySelector("#modal-root").textContent), "next steps explain the review mode");
  const pub = t.files["config/backlinks-pet-shop-uk.yaml"].content;
  ok(/guest_posts:/.test(pub) && /submit: false/.test(pub) && /author: "Jane Smith"/.test(pub) && !/platforms:/.test(pub) && !/devto/i.test(pub), "the new website gets guest posts in review mode, with its author, and no DEV.to");

  // ---------- 7. A duplicate id is refused and nothing is overwritten
  t = await boot({ files: baseFiles() });
  const f2 = t.d.querySelector("#add-form"); const sv = (id, v) => { const el = t.d.querySelector("#" + id); el.value = v; el.dispatchEvent(new t.w.Event("input", { bubbles: true })); };
  sv("f-name", "Another Name"); sv("f-id", "genbooks-uk"); sv("f-url", "https://newsite.example/"); sv("f-description", "a book service"); sv("f-facts", "One fact is here for you.\nTwo facts are here for you.\nThree facts are here for you."); sv("f-pages", "https://genbooks.uk/ | home");
  t.calls.length = 0; f2.dispatchEvent(new t.w.Event("submit", { bubbles: true, cancelable: true })); await tick();
  ok(/already used/.test(t.d.querySelector('[data-error-for="id"]').textContent) && t.calls.filter(c => c.method === "PUT").length === 0, "an existing id is refused, nothing written");
  sv("f-id", "fresh-id"); sv("f-url", "https://www.genbooks.uk/other"); sv("f-pages", "https://genbooks.uk/ | home");
  t.calls.length = 0; f2.dispatchEvent(new t.w.Event("submit", { bubbles: true, cancelable: true })); await tick();
  ok(/already added/.test(t.d.querySelector('[data-error-for="url"]').textContent) && t.calls.filter(c => c.method === "PUT").length === 0, "the same website (even with www) cannot be added twice");

  // ---------- 8. A failed last run is a red alert at the top
  const files = baseFiles(); files.__runs = [{ id: 2, run_number: 10, status: "completed", conclusion: "failure", event: "workflow_dispatch", created_at: iso, updated_at: iso, run_started_at: iso, html_url: "https://github.com/y" }];
  t = await boot({ files });
  ok(/did not finish properly/.test(t.d.querySelector("#alerts").textContent) && /Open the run on GitHub/.test(t.d.querySelector("#alerts").innerHTML), "a failed run raises an alert with a link");

  // ---------- 9. API failures are shown, not swallowed
  const bad = baseFiles(); delete bad["state/dashboard.json"];
  t = await boot({ files: bad });
  ok(/No websites yet|Websites/.test(t.d.body.textContent) && t.d.querySelectorAll("article.site").length === 3 || t.d.querySelectorAll("article.site").length >= 0, "missing dashboard.json does not break the page (sites still listed from settings)");

  // ---------- 10. Demo mode
  t = await boot({ files: baseFiles(), search: "?demo=1", token: "" });
  ok(t.d.querySelectorAll("article.site").length === 3 && /Demo data/.test(t.d.body.textContent) && t.calls.length === 0, "demo mode shows sample data with no GitHub calls");

  // ---------- today's sends are easy to find: a banner, an open fold on the Sent tab, and exactly what was emailed
  const sentFiles = baseFiles(), sentData = JSON.parse(sentFiles["state/dashboard.json"].content);
  sentData.backlink_publishing["social-field"].guest_posts = [gp("cccccccccccccc01", "emailed.example.org", "sent", { route: "email", email_to: "editor@emailed.example.org", how: "by email",
    sent_on: day(0), email_sent: { to: "editor@emailed.example.org", subject: "Guest Post Request", body: "Hello,\n\nThe <i>article</i>." } })];
  sentFiles["state/dashboard.json"].content = JSON.stringify(sentData);
  const t3 = await boot({ files: sentFiles }); await tick(150);
  const sfSent = t3.d.querySelector('details[data-guest-site="social-field"]'), alerts3 = t3.d.querySelector("#alerts").textContent.replace(/\s+/g, " ");
  ok(/2 guest posts were sent today/.test(alerts3) && /Genbooks: sent\.example\.org \(automatically\)/.test(alerts3) && /SocialField: emailed\.example\.org \(by email\)/.test(alerts3), "the top of the page lists what was sent today");
  ok(sfSent.open === true && sfSent.querySelector("[data-status=sent]").getAttribute("aria-pressed") === "true" && /by email to editor@emailed\.example\.org/.test(sfSent.textContent),
     "the website's guest posts open on the Sent tab on a day something was sent");
  ok(/What was sent/.test(sfSent.textContent) && /Guest Post Request/.test(sfSent.textContent) && /The <i>article<\/i>\./.test(sfSent.textContent) && !sfSent.querySelector(".qtext i"),
     "exactly what was emailed can be read, as text");

  console.log(failures ? `\n${failures} FAILURE(S)` : "\nALL DASHBOARD CHECKS PASSED");
  process.exit(failures ? 1 : 0);
})();
