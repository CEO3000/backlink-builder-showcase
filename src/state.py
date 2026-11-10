import json
import hashlib
from datetime import datetime, timezone
from src.config import STATE_DIR


def now_iso():
    return datetime.now(timezone.utc).isoformat()


def lead_id(url: str) -> str:
    return hashlib.sha256(url.strip().lower().encode("utf-8")).hexdigest()[:16]


def _site_dir(site_id: str):
    d = STATE_DIR / "sites" / site_id
    d.mkdir(parents=True, exist_ok=True)
    return d


def _load_json(path, default):
    if not path.exists():
        return default
    with path.open("r", encoding="utf-8") as f:
        return json.load(f)


def _save_json(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)


def load_leads(site_id: str) -> dict:
    return _load_json(_site_dir(site_id) / "leads.json", {})


def save_leads(site_id: str, leads: dict):
    _save_json(_site_dir(site_id) / "leads.json", leads)


def load_run_history(site_id: str) -> list:
    return _load_json(_site_dir(site_id) / "run_history.json", [])


def save_run_history(site_id: str, history: list):
    _save_json(_site_dir(site_id) / "run_history.json", history)


CONTACTED_LEAD_STATUSES = {"drafted", "approved", "sent", "confirmed_backlink"}


def contacted_emails() -> set[str]:
    """Every address either site's backlink outreach or the venue campaign has emailed, or queued an email to."""
    emails = set()
    for path in (STATE_DIR / "sites").glob("*/leads.json"):
        for lead in _load_json(path, {}).values():
            if lead.get("status") in CONTACTED_LEAD_STATUSES and lead.get("contact_email"):
                emails.add(lead["contact_email"].lower())
    for venue in _load_json(STATE_DIR / "venues" / "venues.json", {}).values():
        if venue.get("status") == "sent" and venue.get("contact_email"):
            emails.add(venue["contact_email"].lower())
    return emails


def load_profile(site_id: str):
    return _load_json(_site_dir(site_id) / "profile.json", None)


def save_profile(site_id: str, profile: dict):
    _save_json(_site_dir(site_id) / "profile.json", profile)


def load_ha_status(site_id: str) -> dict:
    return _load_json(_site_dir(site_id) / "high_authority_status.json", {})


def save_ha_status(site_id: str, status: dict):
    _save_json(_site_dir(site_id) / "high_authority_status.json", status)


def last_send_at(history: list):
    sends = [h["at"] for h in history if h.get("event") == "sent"]
    return max(sends) if sends else None


def save_dashboard(data: dict):
    _save_json(STATE_DIR / "dashboard.json", data)


def load_discovery_cursor(site_id: str) -> int:
    return _load_json(_site_dir(site_id) / "discovery_cursor.json", {}).get("index", 0)


def save_discovery_cursor(site_id: str, index: int) -> None:
    _save_json(_site_dir(site_id) / "discovery_cursor.json", {"index": index})
