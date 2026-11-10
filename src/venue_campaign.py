"""Email-marketing campaign to UK venues, London first (sports and activity
venues, bars and nightlife), inviting them to list their events on SocialField. Separate
from the backlink outreach in scheduler.py: its own sender (Zoho,
info@socialfield.example), state (state/venues/), log and daily cap, all set in
config/venue_campaign.yaml.

Venues come from Google's Places API. A venue is only emailed if its own
website publishes an address on its own domain and shows it's a registered
company: UK PECR allows cold marketing email to companies when the sender is
identified and an opt-out is offered (every email ends with one), but sole
traders and partnerships need consent first. The do_not_contact list in
settings.yaml applies here too. run() never raises, so a failure here can't
stop the workflow's later steps, including committing the backlink state.
"""
import json
import os
import re
import urllib.request
from datetime import datetime, timezone
from urllib.parse import urljoin, urlparse

import yaml

from src import outreach, site_profiler, state as st
from src.config import CONFIG_DIR, LOGS_DIR, STATE_DIR, load_settings, load_sites

PLACES_URL = "https://places.googleapis.com/v1/places:searchText"
# Requesting websiteUri bills every search at the Text Search Enterprise rate (1,000 free a month).
FIELD_MASK = "places.id,places.displayName,places.formattedAddress,places.websiteUri,places.primaryType"
VENUE_STATE_DIR = STATE_DIR / "venues"

# Wording UK company websites use for their legally required disclosures, or a company number.
_COMPANY_NUMBER = r"(?:[a-z]{2}\d{6}|\d{6,8})\b"
_COMPANY_RE = re.compile(
    r"registered in (?:england|wales|scotland|northern ireland)"
    r"|registered office"
    r"|(?:company|registration|reg)\.?\s*(?:registration\s*)?(?:no\.?|number)\s*[:#.\-]?\s*" + _COMPANY_NUMBER
    + r"|registered\s*(?:no\.?|number)\s*[:#.\-]?\s*" + _COMPANY_NUMBER
    + r"|\bco\.?\s*reg(?:istration|istered)?\.?\s*(?:no\.?|number)?\s*[:#.\-]?\s*" + _COMPANY_NUMBER,
    re.IGNORECASE,
)
# Only a registered company may call itself Ltd, Limited, LLP, PLC or CIC, so an ownership notice such as
# "(c) 2026 Escape Plan Ltd" or "operated by MAP Maison Limited" shows the venue is run by a company.
_COMPANY_NOTICE_RE = re.compile(
    r"(?i:©|&copy;|\(c\)|copyright|operated by|owned by|run by|managed by|trading as|a trading name of)"
    r"\W{0,12}(?:\d{4}\W{0,6})?[A-Z][\w&'’.\- ]{1,50}?\s(?:Ltd\.?|Limited|LLP|PLC|CIC)\b",
)


def looks_like_registered_company(page_text: str) -> bool:
    return bool(_COMPANY_RE.search(page_text or "") or _COMPANY_NOTICE_RE.search(page_text or ""))


def search_places(query: str, api_key: str) -> list[dict]:
    body = json.dumps({"textQuery": query, "pageSize": 20, "regionCode": "GB", "languageCode": "en"}).encode("utf-8")
    req = urllib.request.Request(PLACES_URL, data=body, method="POST", headers={
        "Content-Type": "application/json",
        "X-Goog-Api-Key": api_key,
        "X-Goog-FieldMask": FIELD_MASK,
    })
    with urllib.request.urlopen(req, timeout=20) as resp:
        return json.loads(resp.read().decode("utf-8")).get("places", [])


def _email_on_domain(email: str, domain: str) -> bool:
    email_domain = email.rsplit("@", 1)[-1].lower()
    return email_domain == domain or email_domain.endswith("." + domain) or domain.endswith("." + email_domain)


# Venues usually publish their email on a contact-style page, and their company details on a legal page.
_CONTACT_PAGE_HINTS = ("contact", "get-in-touch", "get in touch", "enquir", "find-us", "find us", "private-hire", "private hire")
_COMPANY_PAGE_HINTS = ("terms", "privacy", "legal", "about", "condition", "cookie", "policy", "imprint", "booking", "faq")
COMPANY_PAGES_TO_READ = 4
# Plain-text addresses too, not just mailto: links. Addresses a site hides from bots (e.g. Cloudflare's
# email obfuscation) are deliberately left alone.
_EMAIL_RE = re.compile(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+")
_SKIP_LOCAL_PARTS = ("noreply", "no-reply", "donotreply", "privacy", "gdpr", "dpo", "data", "legal", "compliance",
                     "jobs", "careers", "recruit", "hr", "accounts", "invoice", "finance", "billing", "press")
_PREFERRED_LOCAL_PARTS = ("event", "hire", "info", "hello", "enquir", "contact", "book", "reserv")
PAGE_FETCH_TIMEOUT = 8


def _linked_pages(html: str, base_url: str, domain: str, hints: tuple, limit: int) -> list[str]:
    """Same-site pages linked from html whose address or link text contains a hint, best hint first."""
    ranked = {}
    for href, text in outreach.CONTACT_ANCHOR_RE.findall(html):
        url = urljoin(base_url, href.strip()).split("#")[0]
        if url.rstrip("/") == base_url.rstrip("/") or url in ranked:
            continue
        if outreach._bare_domain(urlparse(url).netloc) != domain:
            continue
        label = f"{href} {re.sub(r'<[^>]+>', ' ', text)}".lower()
        rank = next((i for i, hint in enumerate(hints) if hint in label), None)
        if rank is not None:
            ranked[url] = rank
    return sorted(ranked, key=ranked.get)[:limit]


def _emails_on(html: str, domain: str) -> list[str]:
    found = []
    for email in _EMAIL_RE.findall(html):
        email = email.lower().strip(".")
        if _email_on_domain(email, domain) and not email.split("@")[0].startswith(_SKIP_LOCAL_PARTS) and email not in found:
            found.append(email)
    return found


def _pick_email(emails: list[str]) -> str | None:
    def rank(email):
        local = email.split("@")[0]
        return next((i for i, part in enumerate(_PREFERRED_LOCAL_PARTS) if part in local), len(_PREFERRED_LOCAL_PARTS))
    return min(emails, key=rank) if emails else None


def _look_up_venue(website: str, domain: str) -> tuple[str | None, str]:
    """(email, text for the company check) from the homepage, up to two contact pages and up to
    COMPANY_PAGES_TO_READ legal/terms/about pages. Emails only come from the homepage and contact pages,
    never the privacy/terms pages."""
    home = outreach._fetch(website, timeout=PAGE_FETCH_TIMEOUT)
    emails, pages = _emails_on(home, domain), [home]
    for url in _linked_pages(home, website, domain, _CONTACT_PAGE_HINTS, 2):
        try:
            page = outreach._fetch(url, timeout=PAGE_FETCH_TIMEOUT)
        except Exception:
            continue
        emails += [e for e in _emails_on(page, domain) if e not in emails]
        pages.append(page)
    for url in _linked_pages(home, website, domain, _COMPANY_PAGE_HINTS, COMPANY_PAGES_TO_READ):
        try:
            pages.append(outreach._fetch(url, timeout=PAGE_FETCH_TIMEOUT))
        except Exception:
            pass
    return _pick_email(emails), " ".join(site_profiler._extract_page(p)["body"] for p in pages)


def _prompt(eff: dict, venue: dict) -> str:
    return (
        "Write a short, friendly, genuine email (under 120 words) from SocialField to a UK venue, "
        "inviting them to list their events on SocialField for free. It's an introduction, not a hard sell: "
        "no hype, no bullet points, no em dashes or en dashes, no subject-line filler like 'Hope this finds you well'. Only use the facts "
        "given below -- never invent anything about the venue, such as its events, menu, size or history.\n\n"
        f"Venue: {venue['name']}\n"
        f"Type: {venue['type']}\n"
        f"Address: {venue['address']}\n"
        f"Website: {venue['website']}\n\n"
        f"About SocialField ({eff['url']}): {eff['description']}\n\n"
        "Mention the one or two SocialField benefits that best fit this kind of venue. "
        f"End the email with this line as written: \"{outreach.OPT_OUT_LINE}\"\n"
        + (f"End with \"{eff['closing']}\" on its own line, then: " if eff.get("closing") else "Sign off with: ")
        + f"{eff['contact'].get('name') or eff['name']}\n\n"
        "Respond in exactly this format and nothing else -- no markdown, no commentary before or after it:\n"
        "SUBJECT: <subject line>\nBODY:\n<email body>"
    )


def _template(eff: dict, venue: dict) -> tuple[str, str]:
    first_sentence = re.split(r"(?<=[.!?])\s+", eff["description"], maxsplit=1)[0]
    subject = f"List {venue['name']}'s events on SocialField for free"
    body = (
        f"Hi {venue['name']} team,\n\n"
        f"I'm getting in touch from SocialField ({eff['url']}). {first_sentence}\n\n"
        "If you host events, you can list them on SocialField for free and sell tickets or memberships "
        "through it. We handle discovery, booking, payment and ticketing, and payouts arrive automatically."
        "\n\n"
        f"{outreach.OPT_OUT_LINE}\n\n"
        f"{eff['closing'] + chr(10) if eff.get('closing') else ''}{eff['contact'].get('name') or eff['name']}"
    )
    return outreach.strip_dashes(subject), outreach.strip_dashes(body)


def draft(eff: dict, venue: dict) -> tuple[str, str]:
    """Gemini, otherwise the template. Records which in venue["drafted_by"]."""
    try:
        subject, body, venue["drafted_by"] = outreach.write_subject_body(_prompt(eff, venue))
        return subject, body
    except Exception as e:
        print(f"  [venue_campaign] AI draft failed, using template: {e}")
    venue["drafted_by"] = "plain template"
    return _template(eff, venue)


# Google place types (humanized) the owner doesn't want: cafes and restaurants rarely run events.
_NOT_OUR_NICHE_WORDS = {"cafe", "coffee", "bakery", "tea", "restaurant", "sandwich", "dessert", "brunch", "breakfast",
                        "diner", "steak", "pizza", "takeaway", "food", "cream", "juice", "bagel", "donut"}


def _screen(venue: dict, known_domains: set, do_not_contact: list, competitor_terms: list, contacted: set) -> tuple[str, str | None, bool]:
    """Returns (status, email, fetched). status "ok" means it's safe to email; fetched says whether the site was loaded."""
    if _NOT_OUR_NICHE_WORDS & set(venue["type"].lower().split()):
        return "not_our_niche", None, False
    if not venue["website"]:
        return "no_website", None, False
    if venue["domain"] in known_domains:
        return "duplicate_domain", None, False
    if outreach.on_do_not_contact({"url": venue["website"], "title": venue["name"]}, do_not_contact):
        return "do_not_contact", None, False
    if site_profiler.looks_like_competitor(venue["name"], None, competitor_terms, domain=venue["domain"]):
        return "skipped_competitor", None, False
    try:
        email, page_text = _look_up_venue(venue["website"], venue["domain"])
    except Exception:
        return "site_unreachable", None, True
    if not email:
        return "no_email", None, True
    if outreach.on_do_not_contact({"url": venue["website"], "title": venue["name"]}, do_not_contact, email):
        return "do_not_contact", email, True
    if email in contacted:
        return "already_contacted", email, True
    if not looks_like_registered_company(page_text):
        return "not_confirmed_company", email, True
    return "ok", email, True


def _run(report: list) -> bool:
    """Returns True if it did real work (so the run deserves a log file)."""
    cfg = yaml.safe_load((CONFIG_DIR / "venue_campaign.yaml").read_text(encoding="utf-8")) or {}
    if not cfg.get("enabled"):
        report.append("- Disabled in config/venue_campaign.yaml.")
        return False
    api_key = os.environ.get("GOOGLE_PLACES_API_KEY")
    smtp = outreach.smtp_for(cfg["sender"])
    if not api_key or not smtp["password"]:
        report.append(f"- Skipped: the GOOGLE_PLACES_API_KEY and {cfg['sender']['password_env']} GitHub secrets must both be set.")
        return False

    site = next(s for s in load_sites() if s["id"] == cfg["site_id"])
    eff = site_profiler.effective_site(site, st.load_profile(site["id"]) or site_profiler._fallback_profile_from_config(site))
    do_not_contact = load_settings().get("do_not_contact") or []
    contacted = st.contacted_emails()

    venues = st._load_json(VENUE_STATE_DIR / "venues.json", {})
    history = st._load_json(VENUE_STATE_DIR / "run_history.json", [])
    cursor = st._load_json(VENUE_STATE_DIR / "cursor.json", {}).get("index", 0)

    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    sent_today = sum(1 for h in history if h.get("event") == "sent" and h.get("at", "").startswith(today))
    remaining = max(0, cfg["max_emails_per_day"] - sent_today)
    if remaining == 0:
        report.append(f"- Already sent {sent_today} venue email(s) today, the daily cap.")
        return False

    known_domains = {v["domain"] for v in venues.values() if v.get("domain") and v.get("status") != "send_failed"}
    combos = [(t, a) for a in cfg["areas"] for t in cfg["venue_types"]]
    searches = checked = sent = 0
    statuses = {}
    stop = False

    def process(pid: str, venue: dict, known: set, count_skips: bool):
        """Screens one venue, emails it if it passes, and records the outcome in `venues`."""
        nonlocal checked, sent, stop
        status, email, fetched = _screen(venue, known, do_not_contact, site.get("competitor_terms") or [], contacted)
        checked += fetched
        venue.update(status=status, contact_email=email)
        venues[pid] = venue
        if status != "ok":
            if count_skips:
                statuses[status] = statuses.get(status, 0) + 1
            return
        subject, body = draft(eff, venue)
        try:
            outreach.send_email({"contact": {}}, email, subject, body, smtp=smtp)
        except Exception as e:
            venue["status"] = "send_failed"
            report.append(f"- Sending to {email} failed, stopping this run: {e}")
            stop = True
            return
        venue.update(status="sent", subject=subject, body=body, sent_at=st.now_iso())
        contacted.add(email.lower())
        history.append({"event": "sent", "at": venue["sent_at"], "place_id": pid, "email": email})
        sent += 1
        report.append(f"- Emailed {venue['name']} ({email}) -- written by {venue.get('drafted_by', 'unknown')}.")

    # Venues skipped earlier only because no company details were found get one more look with the wider
    # check, before any new search is spent. Each is re-checked once (rechecked_at) so none is retried forever.
    rechecked = revived = 0
    pending = [(pid, v) for pid, v in venues.items() if v.get("status") == "not_confirmed_company" and not v.get("rechecked_at")]
    for pid, venue in pending[:cfg.get("max_rechecks_per_run", 15)]:
        if stop or sent >= remaining or checked >= cfg["max_venues_checked_per_run"]:
            break
        others = {v["domain"] for p, v in venues.items() if p != pid and v.get("domain") and v.get("status") != "send_failed"}
        venue["rechecked_at"] = st.now_iso()
        sent_before = sent
        process(pid, venue, others, count_skips=False)
        rechecked += 1
        revived += sent - sent_before
    if rechecked:
        report.append(f"- Re-checked {rechecked} earlier skipped venue(s) with the wider company check: {revived} now emailed.")

    for offset in range(len(combos)):
        if stop or sent >= remaining or searches >= cfg["max_searches_per_run"] or checked >= cfg["max_venues_checked_per_run"]:
            break
        vtype, area = combos[(cursor + offset) % len(combos)]
        query = f"{vtype} in {area}"
        searches += 1
        try:
            places = search_places(query, api_key)
        except Exception as e:
            report.append(f"- Search failed ({query!r}): {e}")
            continue
        report.append(f"- Searched {query!r}: {len(places)} place(s).")
        for place in places:
            if sent >= remaining or checked >= cfg["max_venues_checked_per_run"]:
                break
            pid = place.get("id")
            if not pid or (pid in venues and venues[pid].get("status") != "send_failed"):
                continue
            website = place.get("websiteUri") or ""
            venue = {
                "name": (place.get("displayName") or {}).get("text", ""),
                "address": place.get("formattedAddress", ""),
                "website": website,
                "domain": outreach._bare_domain(urlparse(website).netloc) if website else "",
                "type": (place.get("primaryType") or "venue").replace("_", " "),
                "query": query,
                "found_at": st.now_iso(),
            }
            process(pid, venue, known_domains, count_skips=True)
            if venue["domain"]:
                known_domains.add(venue["domain"])
            if stop:
                break

    st._save_json(VENUE_STATE_DIR / "venues.json", venues)
    st._save_json(VENUE_STATE_DIR / "run_history.json", history)
    st._save_json(VENUE_STATE_DIR / "cursor.json", {"index": (cursor + searches) % len(combos)})
    skipped = ", ".join(f"{n} {s.replace('_', ' ')}" for s, n in sorted(statuses.items())) or "none"
    report.append(f"- {searches} Google search(es), {checked} website(s) checked, {sent} email(s) sent. Skipped: {skipped}.")
    return True


def run():
    report = [f"# Venue campaign -- {st.now_iso()}", ""]
    try:
        worth_logging = _run(report)
    except Exception as e:
        report.append(f"- Stopped early: {type(e).__name__}: {e}")
        worth_logging = True
    print("\n".join(report))
    if worth_logging:
        log_dir = LOGS_DIR / "venues"
        log_dir.mkdir(parents=True, exist_ok=True)
        (log_dir / f"run-{datetime.now(timezone.utc).strftime('%Y%m%d-%H%M%S')}.md").write_text("\n".join(report), encoding="utf-8")


if __name__ == "__main__":
    run()
