"""Persistence for backlink jobs: one JSON file per concern under state/backlinks/, committed to the repo by the
existing workflow step like every other state file (no new database). A job is one attempt to get one backlink
published on one site, carrying everything needed to audit it later."""
import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path

from src.config import STATE_DIR
from src.backlinks import states as S


def _load(path: Path, default):
    if not path.exists():
        return default
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return default


def _save(path: Path, data) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False, sort_keys=True), encoding="utf-8")


def make_key(adapter: str, domain: str, subject: str) -> str:
    """The unique key that stops the same thing being processed twice: the route, the site and what is being
    published there (a topic for an article, the page address for a found opportunity)."""
    raw = f"{adapter}|{domain.lower().removeprefix('www.')}|{subject.strip().lower()}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]


class Store:
    def __init__(self, root: Path | None = None, clock=None):
        self.root = Path(root) if root else STATE_DIR / "backlinks"
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self.jobs: dict = _load(self.root / "jobs.json", {})
        self.daily: dict = _load(self.root / "daily.json", {})
        self.usage: dict = _load(self.root / "usage.json", {})

    # -- time -------------------------------------------------------------------------------------------
    def now(self) -> datetime:
        return self._clock()

    def now_iso(self) -> str:
        return self.now().isoformat()

    def today(self) -> str:
        return self.now().strftime("%Y-%m-%d")

    # -- persistence ------------------------------------------------------------------------------------
    def save(self) -> None:
        _save(self.root / "jobs.json", self.jobs)
        _save(self.root / "daily.json", self.daily)
        _save(self.root / "usage.json", self.usage)

    def save_content(self, job_id: str, text: str) -> str:
        """Keeps the exact content that was submitted (a JSON document), for audit. Returns its path relative to state/backlinks/."""
        path = self.root / "content" / f"{job_id}.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
        return f"content/{job_id}.json"

    def read_content(self, relative: str) -> str:
        return (self.root / relative).read_text(encoding="utf-8")

    # -- jobs -------------------------------------------------------------------------------------------
    def find_by_key(self, key: str) -> dict | None:
        return next((j for j in self.jobs.values() if j["key"] == key), None)

    def add_job(self, key: str, adapter: str, domain: str, page_url: str, discovery_source: str, **fields) -> tuple[dict, bool]:
        """(job, created). A second add with the same key returns the existing job untouched, so nothing is
        ever processed twice."""
        existing = self.find_by_key(key)
        if existing:
            return existing, False
        job_id = key
        now = self.now_iso()
        job = {
            "id": job_id, "key": key, "adapter": adapter, "domain": domain, "page_url": page_url,
            "discovery_source": discovery_source, "status": S.DISCOVERED, "created_at": now, "updated_at": now,
            "mechanism": None, "qualification": None, "content_version": None, "content_path": None,
            "title": None, "target_url": None, "anchor_text": None, "submitted_at": None, "publication_url": None,
            "submission_reference": None, "verification": None, "link_attributes": None, "retry_count": 0,
            "last_error": None, "error_category": None, "next_action_at": None, "last_verified_at": None,
            "first_verified_at": None, "history": [{"at": now, "from": None, "to": S.DISCOVERED, "note": "created"}],
        }
        job.update(fields)
        self.jobs[job_id] = job
        return job, True

    def transition(self, job: dict, new_status: str, note: str = "", **updates) -> dict:
        S.check_transition(job["status"], new_status)
        now = self.now_iso()
        job["history"].append({"at": now, "from": job["status"], "to": new_status, "note": note})
        job["status"] = new_status
        job["updated_at"] = now
        job.update(updates)
        return job

    def in_states(self, *statuses: str) -> list[dict]:
        return [j for j in self.jobs.values() if j["status"] in statuses]

    def domain_has_live_job(self, adapter: str, domain: str) -> bool:
        """True if there is a job on this route and site that is not finished with."""
        domain = domain.lower().removeprefix("www.")
        return any(j["adapter"] == adapter and j["domain"] == domain and j["status"] not in S.DEAD
                   for j in self.jobs.values())

    # -- counters ---------------------------------------------------------------------------------------
    def _day(self, day: str | None = None) -> dict:
        return self.daily.setdefault(day or self.today(), {"counts": {}, "failures": {}})

    def bump(self, counter: str, n: int = 1) -> None:
        counts = self._day()["counts"]
        counts[counter] = counts.get(counter, 0) + n

    def count(self, counter: str, day: str | None = None) -> int:
        return self._day(day)["counts"].get(counter, 0)

    def record_failure(self, category: str) -> None:
        failures = self._day()["failures"]
        failures[category] = failures.get(category, 0) + 1

    def verified_on(self, day: str | None = None) -> int:
        """Backlinks first verified on `day` that are still live: the only number that measures success."""
        day = day or self.today()
        return sum(1 for j in self.jobs.values()
                   if j["status"] == S.VERIFIED and (j.get("first_verified_at") or "").startswith(day))

    def published_on(self, adapter: str, day: str | None = None) -> int:
        day = day or self.today()
        return sum(1 for j in self.jobs.values()
                   if j["adapter"] == adapter and (j.get("submitted_at") or "").startswith(day)
                   and j["status"] in (S.SUBMITTED, S.PUBLISHED, S.VERIFIED, S.VERIFICATION_FAILED, S.PENDING_APPROVAL))

    # -- provider usage (Tavily / Firecrawl / Gemini), counted by us because the providers do not all report it
    def add_usage(self, engine: str, n: int = 1) -> None:
        month = self.now().strftime("%Y-%m")
        bucket = self.usage.setdefault(month, {})
        bucket[engine] = bucket.get(engine, 0) + n
        day_bucket = self.usage.setdefault("days", {}).setdefault(self.today(), {})
        day_bucket[engine] = day_bucket.get(engine, 0) + n

    def used_this_month(self, engine: str) -> int:
        return self.usage.get(self.now().strftime("%Y-%m"), {}).get(engine, 0)

    def used_today(self, engine: str) -> int:
        return self.usage.get("days", {}).get(self.today(), {}).get(engine, 0)
