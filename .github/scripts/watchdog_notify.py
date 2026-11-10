import json
import os
import smtplib
import ssl
from email.mime.text import MIMEText
from pathlib import Path

STATE_FILE = Path(".watchdog/agent_state.json")
NOTIFIED_FILE = Path(".watchdog/notified_run_ids.json")


def load_json(path, default):
    if path.exists():
        try:
            return json.loads(path.read_text())
        except json.JSONDecodeError:
            return default
    return default


def write_notified(notified_ids):
    NOTIFIED_FILE.parent.mkdir(parents=True, exist_ok=True)
    NOTIFIED_FILE.write_text(json.dumps({"notified_run_ids": sorted(notified_ids)}, indent=2) + "\n")


def main():
    state = load_json(STATE_FILE, {"notifications": []})
    notified = load_json(NOTIFIED_FILE, {"notified_run_ids": []})
    notified_ids = set(str(x) for x in notified.get("notified_run_ids", []))

    pending = [n for n in state.get("notifications", []) if str(n.get("run_id")) not in notified_ids]

    # Always (re)write the file so it exists even on a no-op run -- the workflow's
    # git-add step assumes this path is always present.
    if not pending:
        write_notified(notified_ids)
        print("No pending notifications.")
        return

    host = os.environ["SMTP_HOST"]
    port = int(os.environ.get("SMTP_PORT", "587"))
    user = os.environ["SMTP_USER"]
    password = os.environ["SMTP_PASS"]
    from_email = os.environ.get("SMTP_FROM", user)
    to_email = os.environ["NOTIFY_TO"]

    context = ssl.create_default_context()
    with smtplib.SMTP(host, port) as server:
        server.starttls(context=context)
        server.login(user, password)
        for note in pending:
            msg = MIMEText(note.get("message", ""))
            msg["Subject"] = "Backlink Builder Watchdog Alert"
            msg["From"] = from_email
            msg["To"] = to_email
            server.send_message(msg)
            notified_ids.add(str(note.get("run_id")))
            print(f"Emailed notification for run {note.get('run_id')}")

    write_notified(notified_ids)


if __name__ == "__main__":
    main()
