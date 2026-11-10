import json
import os
import smtplib
import ssl
import subprocess
import time
from datetime import datetime, timedelta, timezone
from email.mime.text import MIMEText
from pathlib import Path

STATE_FILE = Path(".watchdog/schedule_kickoff.json")
REPO = "OWNER/backlink-builder"
WORKFLOW = "backlinks.yml"
SCHEDULE_HOUR, SCHEDULE_MINUTE = 9, 13
GRACE_MINUTES = 30
MAX_RETRIES = 4
RETRY_SLEEP_SECONDS = 280


def load_state():
    if STATE_FILE.exists():
        try:
            return json.loads(STATE_FILE.read_text())
        except json.JSONDecodeError:
            pass
    return {
        "started_dates": [],
        "escalated_dates": [],
        "failure_retried_dates": [],
        "failure_escalated_dates": [],
    }


def save_state(state):
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    STATE_FILE.write_text(json.dumps(state, indent=2) + "\n")


def todays_runs(today_str):
    out = subprocess.run(
        ["gh", "run", "list", "--repo", REPO, "--workflow", WORKFLOW,
         "--json", "databaseId,conclusion,status,createdAt", "--limit", "10"],
        capture_output=True, text=True, check=True,
    )
    runs = json.loads(out.stdout)
    todays = [r for r in runs if r["createdAt"].startswith(today_str)]
    todays.sort(key=lambda r: r["createdAt"], reverse=True)
    return todays


def trigger_new_run():
    subprocess.run(["gh", "workflow", "run", WORKFLOW, "--repo", REPO], check=True)


def rerun(run_id):
    subprocess.run(["gh", "run", "rerun", str(run_id), "--repo", REPO], check=True)


def send_email(subject, body):
    host = os.environ["SMTP_HOST"]
    port = int(os.environ.get("SMTP_PORT", "587"))
    user = os.environ["SMTP_USER"]
    password = os.environ["SMTP_PASS"]
    from_email = os.environ.get("SMTP_FROM", user)
    to_email = os.environ["NOTIFY_TO"]
    msg = MIMEText(body)
    msg["Subject"] = subject
    msg["From"] = from_email
    msg["To"] = to_email
    context = ssl.create_default_context()
    with smtplib.SMTP(host, port) as server:
        server.starttls(context=context)
        server.login(user, password)
        server.send_message(msg)


def handle_missed_schedule(state, today_str, now):
    deadline = now.replace(hour=SCHEDULE_HOUR, minute=SCHEDULE_MINUTE, second=0, microsecond=0) + timedelta(minutes=GRACE_MINUTES)
    if now < deadline:
        return

    already_started_email = today_str in state.get("started_dates", [])
    found = False
    for attempt in range(1, MAX_RETRIES + 1):
        if todays_runs(today_str):
            found = True
            break
        print(f"No run found for today yet -- triggering (attempt {attempt}/{MAX_RETRIES}).")
        trigger_new_run()
        if not already_started_email:
            send_email(
                "Backlink Builder Watchdog: started today's overdue run",
                "Today's scheduled outreach run didn't start on its own within 30 minutes of its "
                "09:13 UTC time (a known GitHub reliability gap, not a bug in the code), so it was "
                "started automatically just now. No action needed.",
            )
            state.setdefault("started_dates", []).append(today_str)
            already_started_email = True
            save_state(state)
        if attempt < MAX_RETRIES:
            time.sleep(RETRY_SLEEP_SECONDS)
            if todays_runs(today_str):
                found = True
                break

    if not found and today_str not in state.get("escalated_dates", []):
        send_email(
            "Backlink Builder Watchdog: could not start today's run after 4 tries",
            f"I tried {MAX_RETRIES} times over about 20 minutes to start today's overdue outreach "
            "run automatically, but it still isn't showing up. I'll try again fresh tomorrow. "
            "If this keeps happening, please check github.com/OWNER/backlink-builder/actions "
            "or ask Claude to investigate.",
        )
        state.setdefault("escalated_dates", []).append(today_str)
    save_state(state)


def handle_run_health(state, today_str, runs):
    latest = runs[0]
    if latest.get("status") != "completed":
        print("Latest run today is still in progress -- nothing to do yet.")
        return
    if latest.get("conclusion") == "success":
        print("Latest run today succeeded -- nothing to do.")
        return
    if latest.get("conclusion") != "failure":
        print(f"Latest run today has conclusion {latest.get('conclusion')!r} -- leaving alone.")
        return

    run_id = latest["databaseId"]
    if today_str not in state.get("failure_retried_dates", []):
        print(f"Today's run (id {run_id}) failed -- retrying once.")
        rerun(run_id)
        state.setdefault("failure_retried_dates", []).append(today_str)
        save_state(state)
        return

    if today_str not in state.get("failure_escalated_dates", []):
        send_email(
            "Backlink Builder Watchdog: today's run failed twice",
            f"Today's outreach run (id {run_id}) failed, was retried automatically, and failed "
            f"again. Automatic code diagnosis/fixing has been turned off to save on usage, so this "
            f"needs a human look -- please check "
            f"github.com/OWNER/backlink-builder/actions/runs/{run_id} or ask Claude to look into it.",
        )
        state.setdefault("failure_escalated_dates", []).append(today_str)
        save_state(state)


def main():
    now = datetime.now(timezone.utc)
    today_str = now.strftime("%Y-%m-%d")
    state = load_state()

    runs = todays_runs(today_str)
    if not runs:
        handle_missed_schedule(state, today_str, now)
        return

    handle_run_health(state, today_str, runs)


if __name__ == "__main__":
    main()
