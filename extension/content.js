/* Backlink helper: runs on every page, but does nothing unless the address carries our "#bl=" marker, which only the
   owner's dashboard puts on its "Click to submit" links. Then it fills the prepared form, waits for the owner to tick the
   CAPTCHA (it never touches the CAPTCHA itself), sends the form, and shows the result with a link back to the dashboard. */
(function () {
  "use strict";
  if (location.hash.indexOf("#bl=") !== 0) return;
  var p;
  try {
    var b64 = location.hash.slice(4).replace(/-/g, "+").replace(/_/g, "/");
    while (b64.length % 4) b64 += "=";
    p = JSON.parse(decodeURIComponent(escape(atob(b64))));
  } catch (e) { return; }
  try { history.replaceState(null, "", location.pathname + location.search); } catch (e) { /* keep going */ }

  var bar = document.createElement("div");
  bar.setAttribute("role", "status");
  bar.style.cssText = "position:fixed;left:12px;right:12px;top:12px;z-index:2147483647;padding:12px 16px;border-radius:10px;font:600 15px/1.4 system-ui,sans-serif;" +
    "box-shadow:0 6px 24px rgba(0,0,0,.25);background:#1f6feb;color:#fff;display:flex;gap:12px;align-items:center;flex-wrap:wrap";
  var msg = document.createElement("span"); bar.appendChild(msg);
  var ver = document.createElement("span");   // which version of this helper is running (so a screenshot shows whether the latest one was loaded)
  try { ver.textContent = "helper v" + chrome.runtime.getManifest().version; } catch (e) { ver.textContent = "helper"; }
  ver.style.cssText = "margin-left:auto;font:500 11px system-ui,sans-serif;opacity:.75"; bar.appendChild(ver);
  document.documentElement.appendChild(bar);
  function say(text, kind, linkText, href) {
    msg.textContent = text;
    bar.style.background = kind === "error" ? "#b42318" : kind === "warn" ? "#b54708" : kind === "ok" ? "#067647" : "#1f6feb";
    var old = bar.querySelector("a"); if (old) old.remove();
    if (href) { var a = document.createElement("a"); a.textContent = linkText; a.href = href; a.style.cssText = "color:#fff;text-decoration:underline"; bar.appendChild(a); }
  }

  say("Backlink helper: filling the form for " + p.site + "...");
  var started = Date.now(), guard = null, filledAt = 0;
  // Some websites reset their form a moment after the page has loaded (example-publisher.org, 2026-11-08: the values vanished about 2 seconds
  // after they were filled). While we wait, any box that has been emptied is filled again; what the person types is never overwritten.
  function keepFilled() {
    if (guard) return;
    filledAt = Date.now();
    document.addEventListener("reset", function (e) {   // the website resets its form: put everything back at once, so nothing is seen to vanish
      if (e.target === blFindForm(p) && guard) { setTimeout(function () { blRefill(p); }, 0); setTimeout(function () { blRefill(p); }, 150); }
    }, true);
    guard = setInterval(function () { if (Date.now() - filledAt > 120000) { clearInterval(guard); return; } blRefill(p); }, 250);
  }
  (function waitForForm() {
    if (!blFindForm(p)) {
      if (Date.now() - started < 15000) return setTimeout(waitForForm, 500);
      return say("Backlink helper: the form was not found on this page, so nothing was sent. Please tell Claude.", "error");
    }
    var r = blFill(p);
    if (r.missing.length) {
      return say("Backlink helper: some fields could not be filled (" + r.missing.join(", ") + "), so nothing was sent. Please tell Claude.", "error");
    }
    keepFilled();
    if (r.captcha === "none" && !r.human.length) { say("Form filled. Checking that it stays filled, then sending..."); setTimeout(send, 3200); return; }
    var target = blOwn(p, BL_CAPTCHA_WIDGETS).filter(function (w) { return blVisible(w); })[0] || null;   // never another form's CAPTCHA
    var form = blFindForm(p);
    (p.wait_for || []).forEach(function (spec) {   // the person answers the human-check question themselves
      var el = form && blField(form, spec);
      if (el) { el.style.outline = "4px solid #f79009"; el.style.outlineOffset = "3px"; if (!target) target = el; }
    });
    if (target) {
      if (target.scrollIntoView) target.scrollIntoView({ block: "center" });
      target.style.outline = "4px solid #f79009"; target.style.outlineOffset = "4px";
    }
    var asks = [];
    if (r.human.length) asks.push("answer the question (" + r.human.join(", ").slice(0, 60) + ")");
    if (r.captcha !== "none") asks.push("tick the CAPTCHA");
    say("Form filled. Please " + asks.join(" and ") + ": the form sends by itself as soon as you do.", "warn");
    (function poll() {
      if (blCaptchaState(p) !== "waiting" && blHumanPending(p).length === 0) { say("Thank you. Sending..."); setTimeout(send, 600); return; }
      setTimeout(poll, 700);
    })();
  })();

  function send() {
    if (guard) clearInterval(guard);
    blRefill(p);   // whatever the website emptied in the meantime goes back in just before the send
    if (!blSubmit(p)) return say("Backlink helper: the Send button was not found. Please press it yourself.", "error");
    setTimeout(function () {
      var res = blResult(p);
      if (res.error) return say("The site reported a problem: " + res.errors.join(" / "), "error");
      say(res.success ? "Sent. The site confirmed it." : "Sent. The site did not show a confirmation yet.", "ok", "Back to the dashboard (records it as sent)", p.back);
    }, 4000);
  }
})();
