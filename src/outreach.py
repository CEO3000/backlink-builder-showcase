"""Drafts and (optionally, with explicit opt-in) sends outreach emails asking
a site owner to consider linking to you -- the actual white-hat mechanism
behind a real backlink: a human on the other end chooses to add the link.

Safety defaults (see config/settings.yaml):
- auto_send is false by default. Everything sits in state/leads.json with
  status "drafted" until a human flips the switch or sends manually.
- Even with auto_send true, at most `max_outreach_sent_per_run` go out per
  run, with a minimum gap of `min_days_between_sends`.
- We only email addresses that are publicly published on the target's own
  site (a mailto: link found on the page or its /contact page) -- never
  guessed or purchased addresses.
"""
import json
import os
import re
import smtplib
import socket
import ssl
import time
import urllib.error
import urllib.request
from email.message import EmailMessage
from email.utils import formataddr

from src.site_profiler import USER_AGENT as BROWSER_USER_AGENT

MAILTO_RE = re.compile(r'mailto:([\w.+-]+@[\w-]+\.[\w.-]+)', re.IGNORECASE)
CONTACT_LINK_RE = re.compile(r'href="([^"]*contact[^"]*)"', re.IGNORECASE)
CONTACT_ANCHOR_RE = re.compile(r'<a\s[^>]*href="([^"]+)"[^>]*>(.*?)</a>', re.IGNORECASE | re.DOTALL)
CONTACT_TEXT_TERMS = ("contact us", "get in touch", "contact")

A_HREF_RE = re.compile(r'<a\s[^>]*href="([^"]+)"', re.IGNORECASE)

# Domains that are obviously never a useful "replace this dead link with my
# page" opportunity, even when they happen to 404 -- a broken link to a
# social profile is a stale account, not a resource gap.
SOCIAL_MEDIA_DOMAINS = {
    "facebook.com", "twitter.com", "x.com", "linkedin.com", "instagram.com",
    "youtube.com", "youtu.be", "pinterest.com", "tiktok.com", "reddit.com",
    "threads.net", "snapchat.com", "whatsapp.com", "t.me",
}

MAX_BROKEN_LINK_CHECKS = 15
BROKEN_LINK_CHECK_TIMEOUT = 5


def _fetch(url: str, timeout=10) -> str:
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 (backlink-outreach-bot; contact-info-lookup)"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read().decode("utf-8", errors="ignore")


def _find_contact_page_link(html: str, page_url: str) -> str | None:
    """Passive scan of already-fetched HTML for a link that looks like it
    leads to a contact page -- by href or by visible link text. Never
    fetches, follows, or interacts with it; just resolves the URL so a
    human can review it later."""
    from urllib.parse import urljoin
    for href, text in CONTACT_ANCHOR_RE.findall(html):
        visible_text = re.sub(r'<[^>]+>', ' ', text).lower()
        if "contact" in href.lower() or any(term in visible_text for term in CONTACT_TEXT_TERMS):
            return urljoin(page_url, href)
    return None


def find_public_contact_email(page_url: str) -> tuple[str | None, str | None, str | None]:
    """Looks for a publicly published mailto: link on the page itself, or on
    an obvious /contact page it links to. Returns (email, contact_page_url,
    html): email is None if nothing is found -- callers should not guess
    further. When no email is found, contact_page_url is set if the page has
    a link that looks like a contact page, so a human can follow up manually
    -- this only detects and records the URL, it never fills in or submits
    anything on that page. html is the lead's own page as fetched here, is
    returned so other passive, read-only checks (e.g. find_broken_link) can
    reuse it instead of fetching the page a second time."""
    try:
        html = _fetch(page_url)
    except Exception:
        return None, None, None

    m = MAILTO_RE.search(html)
    if m:
        return m.group(1), None, html

    contact_match = CONTACT_LINK_RE.search(html)
    if contact_match:
        contact_url = contact_match.group(1)
        if contact_url.startswith("/"):
            from urllib.parse import urljoin
            contact_url = urljoin(page_url, contact_url)
        if contact_url.startswith("http"):
            try:
                contact_html = _fetch(contact_url)
                m2 = MAILTO_RE.search(contact_html)
                if m2:
                    return m2.group(1), None, html
            except Exception:
                pass

    return None, _find_contact_page_link(html, page_url), html


def _bare_domain(netloc: str) -> str:
    netloc = (netloc or "").lower()
    return netloc[4:] if netloc.startswith("www.") else netloc


def _is_link_broken(url: str, timeout: int = BROKEN_LINK_CHECK_TIMEOUT) -> bool:
    """Only a confident signal counts: 404/410, or a domain that no longer
    exists. A 403, timeout or 5xx is usually a site blocking automated checks
    (example-bot-blocker.com 403s bots but loads fine in a browser) -- and telling a
    real person their working link is dead is far worse than missing one."""
    req = urllib.request.Request(url, headers={"User-Agent": BROWSER_USER_AGENT})
    try:
        with urllib.request.urlopen(req, timeout=timeout):
            return False
    except urllib.error.HTTPError as e:
        return e.code in (404, 410)
    except urllib.error.URLError as e:
        return isinstance(e.reason, socket.gaierror)
    except Exception:
        return False


def find_broken_link(html: str, page_url: str, own_domain: str) -> str | None:
    """Passive enrichment for broken-link building: scans already-fetched
    page HTML for outbound links and checks (at most MAX_BROKEN_LINK_CHECKS
    of them) whether any are dead, so the lead can be pitched the client's
    own page as a natural replacement instead of just asked for space.
    Only ever issues read-only HEAD/GET requests -- the same thing any link
    checker, uptime monitor, or search engine crawler does constantly.
    Never raises: a failure here should never block a lead from getting a
    normal draft."""
    try:
        from urllib.parse import urljoin, urlparse

        page_domain = _bare_domain(urlparse(page_url).netloc)
        client_domain = _bare_domain(own_domain)

        candidates = []
        seen = set()
        for href in A_HREF_RE.findall(html):
            href = href.strip()
            if not href or href.startswith("#"):
                continue
            if href.lower().startswith(("mailto:", "tel:", "javascript:")):
                continue
            absolute = urljoin(page_url, href)
            parsed = urlparse(absolute)
            if parsed.scheme not in ("http", "https"):
                continue
            link_domain = _bare_domain(parsed.netloc)
            if not link_domain or link_domain == page_domain or link_domain == client_domain:
                continue
            if any(link_domain == d or link_domain.endswith("." + d) for d in SOCIAL_MEDIA_DOMAINS):
                continue
            if absolute in seen:
                continue
            seen.add(absolute)
            candidates.append(absolute)

        for url in candidates[:MAX_BROKEN_LINK_CHECKS]:
            if _is_link_broken(url):
                return url
        return None
    except Exception:
        return None


def draft_email(site: dict, lead: dict) -> tuple[str, str]:
    """Returns (subject, body) written by Gemini, or the plain template if Gemini can't.
    Records which in lead["drafted_by"]."""
    try:
        subject, body, lead["drafted_by"] = write_subject_body(outreach_prompt(site, lead))
        return subject, body
    except Exception as e:
        print(f"  [outreach] AI draft failed, using template: {e}")
    lead["drafted_by"] = "plain template"
    return _draft_template(site, lead)


def _clean_description(description: str) -> str:
    """Strips rhetorical/ad-copy questions ("Looking for X?") out of a
    site's own marketing description so it reads like a person describing
    their site, not pasted website copy."""
    if not description:
        return ""
    sentences = re.split(r'(?<=[.!?])\s+', description.strip())
    statements = [s for s in sentences if not s.strip().endswith('?')]
    return ' '.join(statements).strip()


# Closing line of every email that makes the second offer, and of venue emails. The owner chose this wording
# over the earlier "If you'd rather not hear from us again, just reply and let me know." opt-out.
OPT_OUT_LINE = "Thank you for your attention."

def strip_dashes(text: str) -> str:
    """The owner wants no em dashes or en dashes in any email: a dash between words becomes a comma
    and a number range keeps a plain hyphen. Applied to drafts and again right before sending."""
    # Only spaces and tabs are absorbed around a dash, so a dash at the end of a line never joins paragraphs.
    text = re.sub(r"(?<=\d)[ \t]*–[ \t]*(?=\d)", "-", text)
    text = re.sub(r"[ \t]*[—―–][ \t]*", ", ", text)
    text = re.sub(r",(?:[ \t]*,)+", ",", text)
    return re.sub(r",[ \t]+(?=\n)", ",", text)


# Free tier -- the key must live in a Google project with no billing attached, or it gets charged.
# Newest first. If the first stays busy the next is tried; each model has its own capacity and free limits.
GEMINI_MODELS = ("gemini-3.8-flash", "gemini-3.7-flash", "gemini-3.6-flash", "gemini-3.5-flash")
# The free plan's daily limit is per project AND per model ("GenerateRequestsPerDayPerProjectPerModel", 20 a day on
# the first two), so every model in this chain has its own allowance: the next is tried when one is used up.
GEMINI_RETRY_DELAYS = ((3, 8), (3, 8), (3, 8), (3, 8, 20))  # seconds to wait between tries, per model
GEMINI_BUSY_CODES = (429, 500, 502, 503, 504)
GEMINI_MIN_GAP_SECONDS = 7      # between requests: the free plan allows only about 10 a minute
GEMINI_MAX_QUOTA_WAIT = 90     # a 429 asking for a longer wait than this means the daily quota is used up
GEMINI_URL = "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"


def _second_offer_block(site: dict) -> str:
    offer = site.get("second_offer")
    return f"{offer}\n\n{OPT_OUT_LINE}\n\n" if offer else ""


def _additional_info_paragraph(site: dict) -> str:
    """The template's version of the site's `additional_info` news paragraph."""
    info = (site.get("additional_info") or "").strip()
    return f"In addition, {info[0].lower() + info[1:]}\n\n" if info else ""


def _draft_template(site: dict, lead: dict) -> tuple[str, str]:
    subject = f"Quick suggestion for {lead.get('title', 'your page')}"
    # First sentence only: the full description is context for the AI writer, too long to paste into a short email.
    description = re.split(r'(?<=[.!?])\s+', _clean_description(site.get("description")), maxsplit=1)[0]
    signoff = site.get("contact", {}).get("name") or "the team"
    body = (
        f"Hi,\n\n"
        f"I came across your page ({lead['url']}) while researching "
        f"{lead.get('matched_keyword') or ', '.join(site.get('keywords', [])[:2])}.\n\n"
        f"I wanted to mention {site['name']} ({site['url']}). {description}\n\n"
        f"{_additional_info_paragraph(site)}"
        f"It could be a useful addition if you're ever updating that page. "
        f"No worries either way, and thanks for putting the resource together.\n\n"
        f"{_second_offer_block(site)}"
        f"{site.get('closing') or 'Best,'}\n{signoff}"
    )
    return strip_dashes(subject), strip_dashes(body)


def draft_followup(site: dict, lead: dict) -> tuple[str, str]:
    """Returns (subject, body) for the single one-time follow-up sent to a
    lead who hasn't converted a couple weeks after the original outreach.
    Plain template, no AI call needed for a short polite nudge -- quotes
    the original draft underneath so the recipient has full context."""
    subject = f"Following up: {lead['draft_subject']}"
    body = (
        f"Hi,\n\n"
        f"Just wanted to follow up on the note below in case it got buried "
        f"in your inbox. No worries at all if it's not a fit right now.\n\n"
        f"---\n\n"
        f"{lead['draft_body']}"
    )
    return subject, body


def on_do_not_contact(lead: dict, terms: list[str], email: str | None = None) -> bool:
    text = f"{lead.get('url', '')} {lead.get('title') or ''} {email or lead.get('contact_email') or ''}".lower()
    return any(term.lower() in text for term in terms)


def _quota_wait_seconds(e: urllib.error.HTTPError) -> float | None:
    """Seconds Google says to wait after a 429 (its retryDelay, e.g. "34s"); 30 if it gives none.
    None means the wait is too long to be a per-minute limit, so the quota is used up for now."""
    try:
        text = e.read().decode("utf-8", "replace")
    except Exception:
        text = ""
    e.body_text = text  # kept so callers can report which quota Google says was hit (the body can only be read once)
    if "PerDay" in text:
        return None  # a daily quota will not come back in a minute: move on to the next model at once
    match = re.search(r'"retryDelay"\s*:\s*"(\d+(?:\.\d+)?)s"', text)
    wait = float(match.group(1)) + 1 if match else 30.0
    return wait if wait <= GEMINI_MAX_QUOTA_WAIT else None


_last_gemini_call = 0.0


def _gemini_candidate(model: str, body: bytes, api_key: str, retry_delays: tuple, deadline: float | None = None) -> dict:
    """One model's reply, retried after short waits while it is busy (5xx) or slow, and after Google's own
    suggested wait on 429. Requests are spaced GEMINI_MIN_GAP_SECONDS apart so the per-minute limit is not hit
    in the first place. deadline (time.monotonic()): nothing waits or runs past it. Raises the last error."""
    global _last_gemini_call
    req = urllib.request.Request(
        GEMINI_URL.format(model=model), method="POST", data=body,
        headers={"Content-Type": "application/json", "x-goog-api-key": api_key},
    )
    for delay in (*retry_delays, None):
        gap = GEMINI_MIN_GAP_SECONDS - (time.monotonic() - _last_gemini_call)
        if gap > 0:
            time.sleep(gap)
        if deadline is not None and time.monotonic() >= deadline:
            raise TimeoutError("the time allowed for Gemini is used up")
        _last_gemini_call = time.monotonic()
        try:
            with urllib.request.urlopen(req, timeout=120 if deadline is None else max(1.0, min(120.0, deadline - time.monotonic()))) as resp:
                return json.loads(resp.read().decode("utf-8"))["candidates"][0]
        except urllib.error.HTTPError as e:
            if e.code not in GEMINI_BUSY_CODES or delay is None:
                raise
            why = f"HTTP {e.code}"
            if e.code == 429:
                delay = _quota_wait_seconds(e)
                if delay is None:
                    raise
        except (TimeoutError, urllib.error.URLError) as e:
            if delay is None:
                raise
            why = type(e).__name__
        if deadline is not None and time.monotonic() + delay >= deadline:
            raise TimeoutError(f"{model} busy ({why}) and the time allowed for Gemini is used up")
        print(f"  [outreach] {model} busy ({why}), retrying in {delay:g}s")
        time.sleep(delay)


def gemini_generate(prompt: str, api_key: str, retry_delays=GEMINI_RETRY_DELAYS, deadline: float | None = None) -> tuple[str, str]:
    """(text, model) from Gemini's free tier. Tries each model in GEMINI_MODELS in turn: the next one
    is used when the first stays busy, times out, or no longer exists. Raises unless a reply finished normally.
    deadline (time.monotonic()): the whole call, every model and retry included, never runs past it."""
    body = json.dumps({"contents": [{"parts": [{"text": prompt}]}]}).encode("utf-8")
    for i, (model, delays) in enumerate(zip(GEMINI_MODELS, retry_delays)):
        try:
            candidate = _gemini_candidate(model, body, api_key, delays, deadline)
        except (urllib.error.URLError, TimeoutError) as e:  # HTTPError is a URLError
            retryable = not isinstance(e, urllib.error.HTTPError) or e.code in GEMINI_BUSY_CODES + (404,)
            if i == len(GEMINI_MODELS) - 1 or not retryable or (deadline is not None and time.monotonic() >= deadline):
                raise
            print(f"  [outreach] {model} unavailable ({e}), trying {GEMINI_MODELS[i + 1]}")
            continue
        if candidate.get("finishReason") != "STOP":
            raise ValueError(f"Gemini stopped before finishing (finishReason={candidate.get('finishReason')!r})")
        text = "".join(p.get("text", "") for p in candidate["content"]["parts"] if not p.get("thought"))
        return text, model


def gemini_text(prompt: str, api_key: str) -> str:
    return gemini_generate(prompt, api_key)[0]


def _parse_subject_body(text: str) -> tuple[str, str]:
    subject_line, _, rest = text.partition("SUBJECT:")
    subject, _, body = rest.partition("BODY:")
    subject, body = subject.strip(), body.strip()
    if not subject or not body:
        # Not in the SUBJECT:/BODY: format -- raise so the caller falls back
        # instead of silently sending a blank email.
        raise ValueError(f"Response didn't match the expected format: {text!r}")
    return strip_dashes(subject), strip_dashes(body)


def write_subject_body(prompt: str) -> tuple[str, str, str]:
    """(subject, body, writer) from Gemini, the only writer (the owner's choice). Raises if it
    can't, so the caller uses its plain template."""
    gemini_key = os.environ.get("GEMINI_API_KEY")
    if not gemini_key:
        raise RuntimeError("no GEMINI_API_KEY set")
    text, model = gemini_generate(prompt, gemini_key)
    # Reports say plain "Gemini" for the usual model and name the model only when the backup wrote it.
    return (*_parse_subject_body(text), "Gemini" if model == GEMINI_MODELS[0] else f"Gemini ({model})")


def outreach_prompt(site: dict, lead: dict) -> str:
    broken_link = lead.get("broken_link_found")
    offer = site.get("second_offer")
    extra = (
        "After the link request, add one short, separate sentence making this second offer, "
        f"adapted naturally to their page: \"{offer}\" Then add this line as written: \"{OPT_OUT_LINE}\"\n\n"
    ) if offer else ""
    info = (site.get("additional_info") or "").strip()
    news = (
        "After the main request, add a short, separate paragraph that starts with \"In addition,\" and tells them "
        f"this news, using only these facts: \"{info}\" Keep the safety point (screening before the consultation, "
        "safety questions again during it) and say \"we believe\" for the first-of-its-kind claim. Finish the email "
        "by saying it is well worth a look, and that we would be glad to be included if it fits their page. "
        "Whenever you name our services, list NLP, CBT and MBL together.\n\n"
    ) if info else ""
    words = 200 if info else 120
    closing = site.get("closing")
    signoff_rule = (f"End with \"{closing}\" on its own line, then my name above" if closing
                    else "Sign off with my name above") + " -- no placeholder brackets like [Your Name].\n\n"
    if broken_link:
        # Broken-link building: hand the recipient something useful (a fix
        # for a dead link on their own page) instead of just asking for
        # space -- converts better because it isn't purely a request.
        prompt = (
            f"Write a short, genuine, non-spammy outreach email (under {words} words). "
            "I was reading their page and noticed a link on it that's now dead/broken. "
            "Mention the specific broken link as something I happened to notice while "
            "reading, as a friendly heads-up, then suggest my own page as a possible "
            "replacement or reference for that spot, since it's genuinely relevant. "
            "Lead with the broken-link heads-up, not with a pitch -- the value to them "
            "is the fix, my page is just an easy option if they want one. "
            "No hype, no bullet points, no em dashes or en dashes, no subject-line filler like "
            "'Hope this finds you well'. Be honest and not pushy. Only reference "
            "facts given below -- never invent other details about their page or "
            "business.\n\n"
            f"My site: {site['name']} -- {site['url']}\n"
            f"Description: {(site.get('description') or '').strip()}\n"
            f"My name: {site.get('contact', {}).get('name') or site['name']}\n\n"
            f"Their page: {lead['url']}\n"
            f"Their page title: {lead.get('title', '')}\n"
            f"The broken link I found on their page: {broken_link}\n\n"
            f"{news}{extra}{signoff_rule}"
            "Respond in exactly this format and nothing else -- no markdown, no "
            "commentary before or after it:\nSUBJECT: <subject line>\nBODY:\n<email body>"
        )
    else:
        prompt = (
            f"Write a short, genuine, non-spammy outreach email (under {words} words) "
            "asking the recipient to consider linking to my site from their page. "
            "No hype, no bullet points, no em dashes or en dashes, no subject-line filler like 'Hope this "
            "finds you well'. Be specific about why it's relevant to THEIR page, "
            "but only using the facts given below -- never invent details about "
            "their page, business, or content beyond what's stated here. If the "
            "snippet is too thin to say something specific, keep the relevance "
            "point general instead of making something up.\n\n"
            f"My site: {site['name']} -- {site['url']}\n"
            f"Description: {(site.get('description') or '').strip()}\n"
            f"My name: {site.get('contact', {}).get('name') or site['name']}\n\n"
            f"Their page: {lead['url']}\n"
            f"Their page title: {lead.get('title', '')}\n"
            f"Their page snippet: {lead.get('snippet', '')}\n"
            f"Matched search query (context on why this page is relevant): {lead.get('matched_query', '')}\n\n"
            f"{news}{extra}{signoff_rule}"
            "Respond in exactly this format and nothing else -- no markdown, no "
            "commentary before or after it:\nSUBJECT: <subject line>\nBODY:\n<email body>"
        )
    return prompt


def smtp_for(sender: dict | None) -> dict | None:
    """SMTP settings for a config `sender` block (email, smtp_host, smtp_port, password_env); None means use SMTP_* env vars."""
    if not sender:
        return None
    return {"host": sender["smtp_host"], "port": int(sender["smtp_port"]), "user": sender["email"],
            "password": os.environ.get(sender["password_env"]), "from_email": sender["email"],
            "from_name": sender.get("name")}


def check_login(smtp: dict) -> None:
    """Signs in to the mailbox and out again, sending nothing: raises if the password or server is wrong."""
    with smtplib.SMTP(smtp["host"], int(smtp["port"]), timeout=30) as server:
        server.starttls(context=ssl.create_default_context())
        server.login(smtp["user"], smtp["password"])


def send_email(site: dict, to_email: str, subject: str, body: str, smtp: dict | None = None, attachments=None):
    """Sends via the SMTP_* env vars, or via `smtp` (host/port/user/password/from_email) when given. `attachments`: (file name, bytes,
    media type) for each file, e.g. the Word file a website's guest-post guidelines ask for."""
    if smtp is None:
        user = os.environ.get("SMTP_USER")
        smtp = {
            "host": os.environ.get("SMTP_HOST"),
            "port": int(os.environ.get("SMTP_PORT", "587")),
            "user": user,
            "password": os.environ.get("SMTP_PASS"),
            "from_email": os.environ.get("SMTP_FROM", user),
        }
    host, port, user, password, from_email = (smtp[k] for k in ("host", "port", "user", "password", "from_email"))

    if not all([host, user, password, from_email]):
        raise RuntimeError(
            "SMTP_HOST / SMTP_USER / SMTP_PASS not set -- cannot send. "
            "Set these as GitHub Actions secrets to enable sending."
        )

    msg = EmailMessage()
    msg["Subject"] = strip_dashes(subject)
    msg["From"] = formataddr((smtp["from_name"], from_email)) if smtp.get("from_name") else from_email
    msg["To"] = to_email
    msg["Reply-To"] = site["contact"].get("reply_to", from_email)
    msg.set_content(strip_dashes(body))
    for name, data, kind in attachments or []:
        main, _, sub = kind.partition("/")
        msg.add_attachment(data, maintype=main, subtype=sub, filename=name)

    context = ssl.create_default_context()
    with smtplib.SMTP(host, port) as server:
        server.starttls(context=context)
        server.login(user, password)
        server.send_message(msg)
