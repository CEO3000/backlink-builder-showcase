import json
import os
import smtplib
import ssl
import socket
import urllib.request
import urllib.error
from datetime import datetime, timezone
from email.mime.text import MIMEText
from pathlib import Path

STATE_SITES_DIR = Path("state/sites")
TIMEOUT_SECONDS = 10
UA = "Mozilla/5.0 (compatible; BacklinkAudit/1.0; +https://github.com/OWNER/backlink-builder)"


def classify_url(url):
    for method in ("HEAD", "GET"):
        req = urllib.request.Request(url, method=method, headers={"User-Agent": UA})
        try:
            with urllib.request.urlopen(req, timeout=TIMEOUT_SECONDS) as resp:
                if resp.status < 400:
                    return "ok", None
                return "uncertain", f"HTTP {resp.status}"
        except urllib.error.HTTPError as e:
            if e.code == 404:
                return "broken", "HTTP 404"
            if e.code == 405 and method == "HEAD":
                continue
            return "uncertain", f"HTTP {e.code}"
        except (urllib.error.URLError, socket.timeout, TimeoutError) as e:
            reason = str(getattr(e, "reason", e))
            if isinstance(getattr(e, "reason", None), socket.gaierror) or "Name or service not known" in reason or "nodename nor servname" in reason:
                return "broken", "domain does not resolve"
            return "uncertain", f"connection issue: {reason}"
        except Exception as e:
            return "uncertain", f"unexpected error: {e}"
    return "uncertain", "HEAD and GET both failed unexpectedly"


def main():
    today_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    broken = []
    uncertain = []

    if not STATE_SITES_DIR.exists():
        print("No state/sites directory found -- nothing to audit.")
        return

    for site_dir in sorted(STATE_SITES_DIR.iterdir()):
        if not site_dir.is_dir():
            continue
        site_id = site_dir.name
        history_file = site_dir / "run_history.json"
        if not history_file.exists():
            continue
        history = json.loads(history_file.read_text())
        todays_sent = [e for e in history if e.get("event") == "sent" and e.get("at", "").startswith(today_str)]
        for entry in todays_sent:
            url = entry.get("url")
            lead_id = entry.get("lead_id")
            if not url:
                continue
            status, reason = classify_url(url)
            if status == "broken":
                broken.append((site_id, lead_id, url, reason))
            elif status == "uncertain":
                uncertain.append((site_id, lead_id, url, reason))

    if not broken and not uncertain:
        print("Link audit: today's outreach targets all look healthy.")
        return

    for site_id, lead_id, url, reason in broken:
        leads_file = STATE_SITES_DIR / site_id / "leads.json"
        if not leads_file.exists() or not lead_id:
            continue
        leads = json.loads(leads_file.read_text())
        if lead_id in leads:
            leads[lead_id]["status"] = "target_page_broken"
            leads[lead_id]["broken_reason"] = reason
            leads_file.write_text(json.dumps(leads, indent=2) + "\n")

    lines = [f"Link audit for today's outreach ({today_str}):\n"]
    if broken:
        lines.append(f"CONFIRMED BROKEN -- excluded from future outreach automatically ({len(broken)}):")
        for site_id, lead_id, url, reason in broken:
            lines.append(f"  - [{site_id}] {url} -- {reason}")
        lines.append("")
    if uncertain:
        lines.append(
            f"UNCERTAIN, NOT auto-excluded -- these returned an error that could just be the site "
            f"blocking automated checks rather than an actually dead page, so I left them alone "
            f"rather than guess wrong ({len(uncertain)}):"
        )
        for site_id, lead_id, url, reason in uncertain:
            lines.append(f"  - [{site_id}] {url} -- {reason}")
        lines.append("\nWorth a quick manual look if any of these keep showing up.")
    body = "\n".join(lines)

    host = os.environ["SMTP_HOST"]
    port = int(os.environ.get("SMTP_PORT", "587"))
    user = os.environ["SMTP_USER"]
    password = os.environ["SMTP_PASS"]
    from_email = os.environ.get("SMTP_FROM", user)
    to_email = os.environ.get("NOTIFY_TO", "owner@example.com")

    msg = MIMEText(body)
    msg["Subject"] = f"Backlink Builder: link audit found {len(broken)} broken, {len(uncertain)} uncertain"
    msg["From"] = from_email
    msg["To"] = to_email

    context = ssl.create_default_context()
    with smtplib.SMTP(host, port) as server:
        server.starttls(context=context)
        server.login(user, password)
        server.send_message(msg)

    print(f"Link audit: reported {len(broken)} broken, {len(uncertain)} uncertain.")


if __name__ == "__main__":
    main()
