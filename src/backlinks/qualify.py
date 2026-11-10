"""Works out how a candidate site would actually accept published content, from evidence on the page, and whether
it is relevant enough to be worth it. A site is only ever QUALIFIED for publication when a confirmed adapter
exists for it; every other site is recorded accurately (needs email, CAPTCHA, an account, manual approval, ...)
and never attempted."""
import json
import re

from bs4 import BeautifulSoup

from src.backlinks import states as S
from src.backlinks.llm import LLM, LLMError

# A CAPTCHA only matters when it guards a form that submits something. The word alone (in a script, a cookie banner, a site-wide
# newsletter box) says nothing: the first version searched the whole page source for it and labelled information pages "CAPTCHA".
WIDGET_SELECTOR = ".g-recaptcha, .h-captcha, .cf-turnstile, [data-sitekey], iframe[src*='recaptcha'], iframe[src*='hcaptcha'], iframe[src*='turnstile']"
WIDGET_SCRIPT_RE = re.compile(r"recaptcha|hcaptcha|turnstile", re.I)
ACCOUNT_RE = re.compile(r"(log ?in|sign ?in|create an account|register|sign ?up)\W{0,20}(to|before)\W{0,10}(submit|post|publish|contribute)"
                        r"|you (must|need to) (be )?(logged in|registered|sign in|log in)(?!\W{0,3}to (post|leave|write|add|submit) (a |an )?(comment|reply|review))", re.I)   # not the comment box
AUTOMATION_FORBIDDEN_RE = re.compile(r"(no|not|without)\W{0,12}(automated|bot|scripted)\W{0,12}(submission|posting|access|tools?)"
                                     r"|automated (submissions?|posting|tools?) (are|is) (not allowed|prohibited|forbidden)", re.I)
APPROVAL_RE = re.compile(r"editorial (team|board|review)|we (will|may) (review|moderate)|subject to (approval|review)|manually (reviewed|approved)|moderat(ed|ion)", re.I)
NOFOLLOW_RE = re.compile(r"no-?follow", re.I)
DOFOLLOW_RE = re.compile(r"do-?follow", re.I)
NO_LINKS_RE = re.compile(r"(no|do not include|don'?t include|without)\W{0,10}(external |outbound |promotional )?links", re.I)
PITCH_WORDS = ("pitch", "submit", "contribut", "write for us", "guest post", "get in touch", "email us", "editor")


SITE_LOGIN_WORDS = re.compile(r"remember me|lost (your )?password|forgot (your )?password|reset password|keep me (signed|logged) in", re.I)
LOGIN_PLACES = ("header", "nav", "footer", "aside")
LOGIN_MARKERS = ("modal", "popup", "lightbox", "dialog", "login", "signin", "sign-in", "account", "header", "menu", "navbar", "offcanvas", "drawer", "overlay")


def _site_login(pw) -> bool:
    """A password box that belongs to the website's own sign-in (in the header, menu, footer or a pop-up, or a plain sign-in form with
    'Remember me' / 'Lost password'), not to the submission. Such a box is on every page of many blogs, so it says nothing about
    whether articles need an account (2026-11-07: three write-for-us pages were turned down for a menu login box)."""
    form = pw.find_parent("form")
    if form is not None and form.find("textarea") is None and SITE_LOGIN_WORDS.search(form.get_text(" ", strip=True)):
        return True
    for node in pw.parents:
        if getattr(node, "attrs", None) is None or node.name in ("body", "html"):
            break
        if node.name in ("main", "article") or node.get("role") == "main":
            return False
        tokens = " ".join([*(node.get("class") or []), node.get("id") or "", node.get("role") or ""]).lower()
        if node.name in LOGIN_PLACES or any(m in tokens for m in LOGIN_MARKERS):
            return True
    return False


def inspect_page(html: str, url: str) -> dict:
    """What the page says about how content is submitted. Everything recorded here is read off the page."""
    soup = BeautifulSoup(html, "html.parser")
    widget = bool(soup.select(WIDGET_SELECTOR)) or any(WIDGET_SCRIPT_RE.search(s.get("src") or "") for s in soup.find_all("script"))
    for t in soup(["script", "style", "noscript", "svg"]):
        t.decompose()
    text = re.sub(r"\s+", " ", soup.get_text(" ", strip=True))

    forms = []
    for f in soup.find_all("form"):
        if f.find("input", {"type": "search"}) or "search" in (f.get("role") or "").lower() or "search" in (f.get("action") or "").lower():
            continue
        fields = f.find_all(["textarea", "input", "select"])
        has_body = f.find("textarea") is not None or any((i.get("type") or "text") in ("url", "file") for i in f.find_all("input"))
        if has_body and len(fields) >= 3:
            forms.append({"action": f.get("action") or url, "method": (f.get("method") or "get").lower(), "fields": len(fields)})
    mailtos = [a["href"] for a in soup.find_all("a", href=True) if a["href"].lower().startswith("mailto:")]
    pitch_context = any(w in text.lower() for w in PITCH_WORDS)

    evidence, flags = [], {
        "captcha": bool(forms) and widget, "password_field": any(not _site_login(pw) for pw in soup.find_all("input", {"type": "password"})),
        "account_text": bool(ACCOUNT_RE.search(text)), "automation_forbidden": bool(AUTOMATION_FORBIDDEN_RE.search(text)),
        "editorial_approval": bool(APPROVAL_RE.search(text)), "mentions_nofollow": bool(NOFOLLOW_RE.search(text)),
        "mentions_dofollow": bool(DOFOLLOW_RE.search(text)), "says_no_links": bool(NO_LINKS_RE.search(text)),
    }
    if flags["automation_forbidden"]:
        mechanism = "automation_prohibited"; evidence.append("the page says automated submissions are not allowed")
    elif flags["account_text"] or (flags["password_field"] and pitch_context and not forms):
        mechanism = "account_required"; evidence.append("an account or login is needed to submit")
    elif forms and flags["captcha"]:
        mechanism = "captcha_required"; evidence.append(f"a form ({forms[0]['fields']} fields) protected by a CAPTCHA")
    elif forms:
        mechanism = "form_submission"; evidence.append(f"a submission form ({forms[0]['fields']} fields) posts to {forms[0]['action']}")
    elif mailtos and pitch_context:
        mechanism = "email_only"; evidence.append(f"submissions go by email ({mailtos[0][:60]})")
    else:
        mechanism = "no_submission_route_found"; evidence.append("no form, account flow or submission email was found on the page")
    if flags["editorial_approval"]:
        evidence.append("the page says an editor reviews or approves submissions")
    return {"mechanism": mechanism, "flags": flags, "forms": forms[:2], "evidence": evidence, "text_excerpt": text[:1500],
            "title": (soup.title.get_text(strip=True) if soup.title else "")[:200]}


def relevance_prompt(inspection: dict, site_topics: list[str], site_label: str = "a UK hypnotherapy practice",
                     fit_criteria: str = "fit with hypnotherapy, mental wellbeing, anxiety, stress, sleep, behavioural change") -> str:
    return (
        f"Judge this web page as a possible place to publish an article that links to {site_label} "
        f"(topics: {', '.join(site_topics)}).\n\nPage title: {inspection['title']}\nPage text (excerpt): {inspection['text_excerpt']}\n\n"
        f"Respond with ONLY a JSON object: {{\"relevance\": 0-10 ({fit_criteria}), "
        "\"quality\": 0-10 (looks like a genuine, editorially sound site, not a link farm), "
        "\"topic\": \"what the site is about\", \"audience\": \"who reads it\", \"reason\": \"one short sentence\"}"
    )


def parse_relevance(text: str) -> dict:
    text = text.strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?|```$", "", text, flags=re.I).strip()
    data = json.loads(text)
    return {"relevance": float(data["relevance"]), "quality": float(data.get("quality", 0)),
            "topic": str(data.get("topic", ""))[:120], "audience": str(data.get("audience", ""))[:120],
            "reason": str(data.get("reason", ""))[:200]}


def qualify_candidate(store, job: dict, inspection: dict, adapters: dict, llm: LLM | None, site_topics: list[str],
                      min_relevance: float = 5, min_quality: float = 4, site_label: str | None = None, fit_criteria: str | None = None) -> str:
    """Moves a DISCOVERED job to its true state. Returns the new status. Gemini is asked to judge relevance only for a
    page that could actually be published to (a confirmed adapter exists), so no quota is spent on pages that cannot be."""
    from src.backlinks.adapters import adapter_for_domain
    q = {"mechanism": inspection["mechanism"], "evidence": inspection["evidence"], "flags": inspection["flags"],
         "page_title": inspection["title"], "automated_submission_permitted": (False if inspection["flags"]["automation_forbidden"] else "not stated"),
         "external_links_allowed": (False if inspection["flags"]["says_no_links"] else "not stated"),
         "link_type_stated": ("dofollow" if inspection["flags"]["mentions_dofollow"] else "nofollow" if inspection["flags"]["mentions_nofollow"] else "not stated"),
         "editorial_approval_required": inspection["flags"]["editorial_approval"], "relevance": None, "quality": None}

    if inspection["mechanism"] == "no_submission_route_found":
        q["decision"], q["rationale"] = "rejected", "no way to submit content was found on the page"
        store.transition(job, S.REJECTED, q["rationale"], qualification=q, mechanism=inspection["mechanism"])
        return S.REJECTED

    adapter = adapter_for_domain(adapters, job["domain"])
    if llm is not None and adapter:
        kwargs = {k: v for k, v in (("site_label", site_label), ("fit_criteria", fit_criteria)) if v}
        try:
            q.update(parse_relevance(llm.generate(relevance_prompt(inspection, site_topics, **kwargs))[0]))
        except (LLMError, ValueError, KeyError) as e:
            q["relevance_error"] = f"{type(e).__name__}: {e}"
    elif not adapter:
        q["relevance_note"] = "judged by the discovery keyword filter only; no AI call is spent on a page that cannot be published to"
    if q["relevance"] is not None and (q["relevance"] < min_relevance or q["quality"] < min_quality):
        q["decision"], q["rationale"] = "rejected", f"not relevant or not good enough (relevance {q['relevance']:g}/10, quality {q['quality']:g}/10): {q.get('reason', '')}"
        store.transition(job, S.REJECTED, q["rationale"], qualification=q, mechanism=inspection["mechanism"])
        return S.REJECTED

    if adapter:
        q["decision"], q["rationale"] = "qualified", f"a confirmed adapter exists for {adapter.domain}"
        store.transition(job, S.QUALIFIED, q["rationale"], qualification=q, mechanism=adapter.mechanism, adapter=adapter.name)
        return S.QUALIFIED
    needs = {"form_submission": "a form-specific adapter, after confirming the site allows automated submission",
             "email_only": "a human to send the email (this system no longer sends cold emails)",
             "captcha_required": "a person to solve the CAPTCHA, which is never bypassed",
             "account_required": "an account created by a person (fake accounts are never created)",
             "automation_prohibited": "nothing: the site prohibits automation"}[inspection["mechanism"]]
    q["decision"], q["rationale"] = "unsupported", f"{inspection['mechanism'].replace('_', ' ')}; it would need {needs}"
    store.transition(job, S.UNSUPPORTED_PUBLICATION_METHOD, q["rationale"], qualification=q, mechanism=inspection["mechanism"])
    return S.UNSUPPORTED_PUBLICATION_METHOD
