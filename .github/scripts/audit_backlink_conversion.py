"""Read-only check of whether sent outreach actually resulted in a real,
live backlink. This never touches Firecrawl or how outreach is sent/
discovered -- it just fetches each sent lead's page directly (same as
audit_links.py's existing broken-link audit) and looks for a link back to
the site's own domain.

For each site, every lead with status == 'sent' that is at least 7 days
old (giving recipients time to act) and hasn't been re-checked in the last
7 days gets its target page fetched and scanned for an <a href="..."> that
contains the site's domain. A match flips the lead to 'confirmed_backlink'.
One summary email goes out only when this run confirmed at least one new
backlink -- silent otherwise, same convention as the other audits.
"""
import json
import os
import smtplib
import ssl
import sys
import urllib.request
import urllib.error
from datetime import datetime, timezone, timedelta
from email.mime.text import MIMEText
from pathlib import Path
from urllib.parse import urlparse

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))
from src.config import load_sites
from src.state import load_leads, save_leads, now_iso

TIMEOUT_SECONDS = 10
UA = "Mozilla/5.0 (compatible; BacklinkAudit/1.0; +https://github.com/OWNER/backlink-builder)"
RECHECK_DAYS = 7


def fetch(url):
    """Best-effort GET. Returns the body text, or None on any failure --
    callers treat "couldn't check" as a non-crash, not an error."""
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT_SECONDS) as resp:
            return resp.read().decode("utf-8", errors="replace")
    except Exception:
        return None


def site_domain(site_url):
    netloc = urlparse(site_url).netloc or urlparse(site_url).path
    return netloc[4:] if netloc.startswith("www.") else netloc


def days_since(iso_str):
    dt = datetime.fromisoformat(iso_str)
    return (datetime.now(timezone.utc) - dt).total_seconds() / 86400


def page_links_to(html, domain):
    for match in html.split("<a "):
        href_start = match.find("href=")
        if href_start == -1:
            continue
        quote = match[href_start + 5:href_start + 6]
        if quote not in ("'", '"'):
            continue
        href_end = match.find(quote, href_start + 6)
        if href_end == -1:
            continue
        href = match[href_start + 6:href_end]
        if domain in href:
            return True
    return False


def check_site(site):
    site_id = site["id"]
    domain = site_domain(site["url"])
    leads = load_leads(site_id)
    newly_confirmed = []

    for lid, lead in leads.items():
        try:
            if lead.get("status") != "sent":
                continue

            sent_at = lead.get("sent_at")
            if not sent_at:
                continue
            try:
                if days_since(sent_at) < RECHECK_DAYS:
                    continue
            except ValueError:
                continue

            last_check = lead.get("last_conversion_check_at")
            if last_check:
                try:
                    if days_since(last_check) < RECHECK_DAYS:
                        continue
                except ValueError:
                    pass

            html = fetch(lead["url"])
            lead["last_conversion_check_at"] = now_iso()
            if html and page_links_to(html, domain):
                lead["status"] = "confirmed_backlink"
                lead["backlink_confirmed_at"] = now_iso()
                newly_confirmed.append((site_id, lead["url"]))
        except Exception as e:
            print(f"[audit_backlink_conversion] site {site_id} lead {lid} failed: {e}")

    save_leads(site_id, leads)
    return newly_confirmed


def send_report(confirmed):
    lines = ["Good news -- some of your outreach resulted in real, confirmed backlinks:", ""]
    for site_id, url in confirmed:
        lines.append(f"- [{site_id}] your outreach to {url} resulted in a real backlink, confirmed today.")
    body = "\n".join(lines)

    host = os.environ["SMTP_HOST"]
    port = int(os.environ.get("SMTP_PORT", "587"))
    user = os.environ["SMTP_USER"]
    password = os.environ["SMTP_PASS"]
    from_email = os.environ.get("SMTP_FROM", user)
    to_email = os.environ.get("NOTIFY_TO", "owner@example.com")

    msg = MIMEText(body)
    msg["Subject"] = f"Backlink Builder: {len(confirmed)} new backlink(s) confirmed"
    msg["From"] = from_email
    msg["To"] = to_email

    context = ssl.create_default_context()
    with smtplib.SMTP(host, port) as server:
        server.starttls(context=context)
        server.login(user, password)
        server.send_message(msg)


def main():
    sites = load_sites()
    confirmed = []
    for site in sites:
        try:
            confirmed.extend(check_site(site))
        except Exception as e:
            print(f"[audit_backlink_conversion] site {site.get('id')} failed: {e}")

    if not confirmed:
        print("Backlink conversion audit: no newly confirmed backlinks this run.")
        return

    send_report(confirmed)
    print(f"Backlink conversion audit: confirmed {len(confirmed)} new backlink(s).")


if __name__ == "__main__":
    main()
