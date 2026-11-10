/* Backlink helper: fills a guest-post form from a prepared payload. The same file runs in two places:
   - the Chrome extension (content script), when the owner opens a "Click to submit" link from the dashboard;
   - the automatic sender on GitHub (Playwright injects it), for forms without a CAPTCHA.
   It never touches a CAPTCHA: it only reads whether the CAPTCHA has been answered by the person.
   payload: { id, site, back, form: { id, action, index }, fields: [{ name, id, kind, value, role }] } */
"use strict";

var BL_CAPTCHA_WIDGETS = ".g-recaptcha, .h-captcha, .cf-turnstile, [data-sitekey], iframe[src*='recaptcha'], iframe[src*='hcaptcha'], iframe[src*='turnstile']";
var BL_CAPTCHA_TOKENS = "textarea[name='g-recaptcha-response'], textarea[name='h-captcha-response'], input[name='cf-turnstile-response'], textarea[name='cf-turnstile-response']";
var BL_OK_WORDS = /thank you|thanks for|has been (sent|received|submitted)|we('ll| will) (review|be in touch|get back|contact)|successfully|received your|submission (was|has been) received/i;
var BL_ERROR_SELECTOR = "[role=alert], .error, .errors, .invalid-feedback, .wpcf7-not-valid-tip, .wpforms-error, .gfield_error, .validation_error, .form-error, .nf-error-msg";

function blQuote(v) { return String(v).replace(/\\/g, "\\\\").replace(/"/g, '\\"'); }

function blVisible(el) {
  if (!el) return false;
  var cs = (el.ownerDocument.defaultView || window).getComputedStyle(el);
  if (cs.display === "none" || cs.visibility === "hidden") return false;
  for (var a = el.parentElement; a; a = a.parentElement) {
    var s = (a.ownerDocument.defaultView || window).getComputedStyle(a);
    if (s.display === "none" || s.visibility === "hidden") return false;
  }
  return true;
}

function blFindForm(p) {
  var f = p.form || {};
  if (f.id) { var byId = document.querySelector('form[id="' + blQuote(f.id) + '"]'); if (byId) return byId; }
  if (f.action) {
    var forms = document.querySelectorAll("form");
    for (var i = 0; i < forms.length; i++) { if ((forms[i].getAttribute("action") || "") === f.action) return forms[i]; }
  }
  if (typeof f.index === "number" && document.forms[f.index]) return document.forms[f.index];
  return null;
}

function blField(form, spec) {
  if (spec.name) { var n = form.querySelector('[name="' + blQuote(spec.name) + '"]'); if (n) return n; }
  if (spec.id) { var i = form.querySelector('[id="' + blQuote(spec.id) + '"]') || document.querySelector('[id="' + blQuote(spec.id) + '"]'); if (i) return i; }
  return null;
}

function blSetValue(el, value) {
  var view = el.ownerDocument.defaultView || window;
  var proto = el.tagName === "TEXTAREA" ? view.HTMLTextAreaElement.prototype : el.tagName === "SELECT" ? view.HTMLSelectElement.prototype : view.HTMLInputElement.prototype;
  var desc = Object.getOwnPropertyDescriptor(proto, "value");
  if (desc && desc.set) desc.set.call(el, value); else el.value = value;   // the native setter, so frameworks such as React notice
  ["input", "change", "blur"].forEach(function (t) { el.dispatchEvent(new view.Event(t, { bubbles: true })); });
}

/** The CAPTCHA parts that can belong to the prepared form: those inside it and those outside every form. One inside another form (for
    example the blog's comment box under the guidelines) has nothing to do with ours. Without a payload: every one on the page. */
function blOwn(p, selector) {
  var form = p ? blFindForm(p) : null;
  return Array.prototype.filter.call(document.querySelectorAll(selector), function (w) {
    // the pop-up a CAPTCHA opens for its picture puzzle sits outside every form and belongs to whichever CAPTCHA opened it
    if (form && w.tagName === "IFRAME" && /bframe|challenge/i.test((w.getAttribute("src") || "") + " " + (w.getAttribute("title") || ""))) return false;
    var host = w.closest ? w.closest("form") : null;
    return !form || !host || host === form;
  });
}

/** A CAPTCHA the person has to deal with: a visible widget. An invisible one (just a badge in the corner) has nothing to tick, and its
    answer only appears when the form is sent, so it must not make us wait. none | waiting | solved */
function blCaptchaState(p) {
  var tokens = blOwn(p, BL_CAPTCHA_TOKENS);
  for (var i = 0; i < tokens.length; i++) { if ((tokens[i].value || "").length > 10) return "solved"; }
  var widgets = blOwn(p, BL_CAPTCHA_WIDGETS);
  for (var j = 0; j < widgets.length; j++) {
    var w = widgets[j];
    if (w.getAttribute("data-size") === "invisible" || (w.closest && w.closest(".grecaptcha-badge"))) continue;
    if (blVisible(w) && w.offsetWidth > 40 && w.offsetHeight > 30) return "waiting";
  }
  return "none";
}

/** Any CAPTCHA technology that may protect the prepared form, visible or invisible. The automatic sender never sends such a form: that
    stays the person's click. When every CAPTCHA on the page sits inside other forms, the script and badge belong to them, not to ours. */
function blCaptchaPresent(p) {
  if (blOwn(p, BL_CAPTCHA_WIDGETS).length || blOwn(p, BL_CAPTCHA_TOKENS).length) return true;
  if (document.querySelector(BL_CAPTCHA_WIDGETS) || document.querySelector(BL_CAPTCHA_TOKENS)) return false;
  if (document.querySelector(".grecaptcha-badge")) return true;
  return Array.prototype.some.call(document.scripts, function (sc) { return /recaptcha|hcaptcha|turnstile/i.test(sc.src || ""); });
}

/** Human-check fields (for example "1 plus 4 equals?") are never answered by us: the labels of those still empty. */
function blHumanPending(p) {
  var form = blFindForm(p), pending = [];
  (p.wait_for || []).forEach(function (spec) {
    var el = form ? blField(form, spec) : null;
    if (el && blVisible(el) && !String(el.value || "").trim()) pending.push(spec.label || spec.name);
  });
  return pending;
}

function blFill(p) {
  var form = blFindForm(p), out = { filled: 0, missing: [], captcha: blCaptchaState(p), captcha_present: blCaptchaPresent(p), human: blHumanPending(p) };
  if (!form) { out.missing.push("the form"); return out; }
  (p.fields || []).forEach(function (spec) {
    var el = blField(form, spec);
    if (!el || (spec.kind !== "checkbox" && !blVisible(el))) {
      if (spec.required === false) return;   // an optional box that is not shown is left alone (it may be a trap for robots)
      out.missing.push(spec.role || spec.name || spec.id); return;
    }
    if (spec.kind === "checkbox") { if (!el.checked) el.click(); }
    else if (spec.kind === "select") {
      var ok = Array.prototype.some.call(el.options, function (o) { return o.value === spec.value; });
      if (!ok) { out.missing.push(spec.role || spec.name); return; }
      blSetValue(el, spec.value);
    } else blSetValue(el, spec.value);
    out.filled++;
  });
  return out;
}

/** Puts back any prepared value that has gone missing. Some websites reset their form a moment after the page has loaded (Contact Form 7 on
    example-publisher.org wiped it about 2 seconds after we filled it, 2026-11-08). Only an EMPTY box is refilled, so whatever the person
    typed is never overwritten. Returns how many boxes it refilled. */
function blRefill(p) {
  var form = blFindForm(p), n = 0;
  if (!form) return 0;
  (p.fields || []).forEach(function (spec) {
    var el = blField(form, spec);
    if (!el || (spec.kind !== "checkbox" && !blVisible(el))) return;
    if (spec.kind === "checkbox") { if (!el.checked) { el.click(); n++; } return; }
    if (spec.kind === "select") {
      var has = Array.prototype.some.call(el.options, function (o) { return o.value === spec.value; });
      if (has && el.value !== spec.value && el.selectedIndex <= 0) { blSetValue(el, spec.value); n++; }   // still on the first (default) choice
      return;
    }
    if (String(el.value || "") === "") { blSetValue(el, spec.value); n++; }
  });
  return n;
}

function blSubmitButton(form) {
  var c = form.querySelectorAll("button[type=submit], input[type=submit], button:not([type])");
  for (var i = 0; i < c.length; i++) { if (blVisible(c[i]) && !c[i].disabled) return c[i]; }
  if (form.id) { var outside = document.querySelector('button[form="' + blQuote(form.id) + '"], input[type=submit][form="' + blQuote(form.id) + '"]'); if (outside) return outside; }
  return null;
}

function blSubmit(p) {
  var form = blFindForm(p); if (!form) return false;
  var b = blSubmitButton(form);
  if (b) { b.click(); return true; }
  if (form.requestSubmit) { form.requestSubmit(); return true; }
  return false;
}

/** What the page shows after sending: { success, error, errors: [text], text, formGone }. */
function blResult(p) {
  var body = document.body ? (document.body.innerText || document.body.textContent || "") : "";
  var text = body.replace(/\s+/g, " ");
  var form = blFindForm(p), gone = !form || !blVisible(form);
  var errors = Array.prototype.filter.call(document.querySelectorAll(BL_ERROR_SELECTOR), function (e) { return blVisible(e) && (e.textContent || "").trim().length > 2; })
    .map(function (e) { return e.textContent.replace(/\s+/g, " ").trim().slice(0, 140); }).slice(0, 3);
  var ok = BL_OK_WORDS.test(text);
  return { success: ok || (gone && !errors.length), error: errors.length > 0 && !ok, errors: errors, text: text.slice(0, 400), formGone: gone };
}

if (typeof module !== "undefined" && module.exports) {
  module.exports = { blFill: blFill, blRefill: blRefill, blCaptchaPresent: blCaptchaPresent, blHumanPending: blHumanPending, blSubmit: blSubmit, blResult: blResult, blCaptchaState: blCaptchaState, blFindForm: blFindForm, blVisible: blVisible };
}
