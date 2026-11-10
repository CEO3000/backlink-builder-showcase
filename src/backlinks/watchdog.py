"""The watchdog for the publication lifecycle. It replaces "was an email sent?" with "is a backlink verified live?":
it rescues stuck jobs, retries transient failures a bounded number of times, re-checks published links, notices
removed ones, and reports the real daily result. Notifications go through the existing relay in .watchdog/
(watchdog_notify.py emails them), so the infrastructure is the same."""
import json
from datetime import datetime
from pathlib import Path

from src.backlinks import states as S
from src.backlinks.executor import schedule_retry, verify_job
from src.backlinks.verify import http_fetch

WATCHDOG_STATE = Path(".watchdog/agent_state.json")


def _due(job: dict, now: datetime) -> bool:
    nxt = job.get("next_action_at")
    return bool(nxt) and datetime.fromisoformat(nxt) <= now


def notify(store, run_id: str, message: str, path: Path | None = None) -> bool:
    """Queues one email-relayed notification, once per run_id. Returns True if it was newly queued."""
    path = Path(path) if path else WATCHDOG_STATE
    state = {"retried_run_ids": [], "last_diagnosed_run_id": None, "notifications": []}
    if path.exists():
        try:
            state.update(json.loads(path.read_text(encoding="utf-8")))
        except json.JSONDecodeError:
            pass
    if any(str(n.get("run_id")) == run_id for n in state["notifications"]):
        return False
    state["notifications"].append({"run_id": run_id, "message": message, "created_at": store.now_iso()})
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(state, indent=2) + "\n", encoding="utf-8")
    return True


def recover_stuck(store, adapters: dict, cfg: dict, fetch=http_fetch) -> list[str]:
    """A job left in SUBMISSION_IN_PROGRESS means a run died mid-submission. Look for the article on the platform first
    (so a retry cannot duplicate it), then either continue from what is there or schedule a retry."""
    notes = []
    for job in store.in_states(S.SUBMISSION_IN_PROGRESS):
        age = (store.now() - datetime.fromisoformat(job["updated_at"])).total_seconds() / 60
        if age < cfg["limits"]["stuck_after_minutes"]:
            continue
        if job.get("adapter") == "guest":   # the run stopped while the website's form was being sent: it may have gone through, so it is never sent twice
            job["guest"].update(sent_on=store.today(), how="automatically", outcome="unclear")
            store.transition(job, S.SUBMITTED, "the run stopped while sending; it may have gone through, so it is never sent again", submitted_at=store.now_iso())
            notes.append(f"{job['id']} stopped while sending; counted as sent (never sent twice)")
            continue
        adapter = adapters.get(job["adapter"])
        found = adapter.find_existing(job["title"]) if adapter else None
        if found and found.outcome == "published":
            store.transition(job, S.PUBLISHED, "recovered: the article was already live on the platform",
                             publication_url=found.url, submission_reference=found.reference)
            verify_job(store, job, cfg, fetch)
            notes.append(f"recovered {job['id']} (it had been published)")
        elif found:
            store.transition(job, S.PENDING_APPROVAL, "recovered: the article exists as a draft", submission_reference=found.reference, draft_url=found.url)
            notes.append(f"recovered {job['id']} (draft exists)")
        else:
            schedule_retry(store, job, cfg, "stuck", f"no result after {age:.0f} minutes and nothing found on the platform")
            notes.append(f"{job['id']} was stuck; retry scheduled")
    return notes


def release_retries(store, cfg: dict) -> list[dict]:
    """RETRY_REQUIRED jobs whose backoff has passed go back to the step they failed at. Returns jobs ready to submit."""
    ready = []
    for job in store.in_states(S.RETRY_REQUIRED):
        if not _due(job, store.now()):
            continue
        if job.get("content_path"):
            store.transition(job, S.CONTENT_READY, "backoff passed; retrying the submission")
            ready.append(job)
        elif job.get("adapter") != "web":
            store.transition(job, S.QUALIFIED, "backoff passed; content will be regenerated")
        else:
            store.transition(job, S.DISCOVERED, "backoff passed; the page will be inspected again")
        job["next_action_at"] = None
    return ready


def unblock(store, adapters: dict) -> list[str]:
    """BLOCKED_CREDENTIALS jobs go back to QUALIFIED once the adapter reports its credential is present again."""
    notes = []
    for job in store.in_states(S.BLOCKED_CREDENTIALS):
        adapter = adapters.get(job["adapter"])
        if adapter and adapter.available()[0] and not job.get("credential_retried_on") == store.today():
            store.transition(job, S.QUALIFIED, "the credential is present again", credential_retried_on=store.today())
            notes.append(f"{job['id']} unblocked")
    return notes


def check_pending(store, adapters: dict, cfg: dict, fetch=http_fetch) -> list[str]:
    """Drafts waiting for approval: if the owner has published one on the platform, pick it up and verify it."""
    notes = []
    for job in store.in_states(S.PENDING_APPROVAL):
        adapter = adapters.get(job["adapter"])
        found = adapter.find_existing(job["title"]) if adapter else None
        if found and found.outcome == "published" and found.url:
            store.transition(job, S.PUBLISHED, "approved: the article is now live on the platform", publication_url=found.url)
            store.bump("published")
            verify_job(store, job, cfg, fetch)
            notes.append(f"{job['id']} was published by its owner and checked")
    return notes


MAX_PUBLISH_ATTEMPTS = 3


def _draft_problems(store, job: dict, cfg: dict) -> list[str]:
    """The current article-quality rules applied to a saved draft (the disclosure we append is left out)."""
    from src.backlinks import content as C
    topic = next((t for t in cfg["topics"] if t["key"] == job.get("topic_key")), None)
    if topic is None or not job.get("content_path"):
        return ["the draft's topic or saved content cannot be found"]
    saved = json.loads(store.read_content(job["content_path"]))
    body = saved["body_markdown"].split("\n\n---\n\n")[0]
    article = {**saved, "body_markdown": body}
    return C.local_checks(article, topic, job.get("anchor_text") or "", cfg.get("banned_claims", []), [], cfg.get("brand_name"), cfg.get("facts"))


def publish_pending(store, adapters: dict, cfg: dict, fetch=http_fetch) -> list[str]:
    """With live_publishing on, private drafts are made public through the platform's API, so nobody has to press Publish.
    A draft is published if the owner approved it (owner_approved) or it passes today's quality rules; otherwise it stays a
    draft. At most MAX_PUBLISH_ATTEMPTS tries per draft."""
    notes = []
    if not cfg.get("live_publishing"):
        return notes
    for job in store.in_states(S.PENDING_APPROVAL):
        adapter = adapters.get(job["adapter"])
        if adapter is None or not job.get("submission_reference") or job.get("publish_attempts", 0) >= MAX_PUBLISH_ATTEMPTS:
            continue
        if not job.get("owner_approved"):
            problems = _draft_problems(store, job, cfg)
            if problems:
                job["publish_skipped"] = "; ".join(problems)[:300]
                notes.append(f"{job['id']} stays a draft: {problems[0][:100]}")
                continue
        job["publish_attempts"] = job.get("publish_attempts", 0) + 1
        result = adapter.publish_draft(job["submission_reference"])
        if result.outcome == "published" and result.url:
            store.transition(job, S.PUBLISHED, "the draft was published through the API", publication_url=result.url)
            store.bump("published")
            verify_job(store, job, cfg, fetch)
            notes.append(f"{job['id']} published through the API and checked: {job['status']}")
        else:
            job["last_error"], job["error_category"] = result.message[:300], result.error_category or "unknown"
            store.record_failure(job["error_category"])
            notes.append(f"{job['id']}: publishing the draft failed ({result.error_category}: {result.message[:80]})")
    return notes


def reverify(store, cfg: dict, fetch=http_fetch) -> list[str]:
    """Published links never verified, failed checks whose retry is due, and verified links due a re-check."""
    notes = []
    for job in list(store.jobs.values()):
        if not job.get("publication_url") or job.get("legacy"):   # removed platforms' old links are no longer checked
            continue
        status = job["status"]
        if status == S.PUBLISHED or (status in (S.VERIFICATION_FAILED, S.VERIFIED) and _due(job, store.now())):
            before = status
            after = verify_job(store, job, cfg, fetch)
            if after != before or after != S.VERIFIED:
                notes.append(f"{job['id']}: {before} -> {after}")
    return notes


def run(store, adapters: dict, cfg: dict, fetch=http_fetch) -> dict:
    """One full pass. Returns {"ready": jobs to submit, "notes": [...]}."""
    notes = []
    notes += recover_stuck(store, adapters, cfg, fetch)
    notes += unblock(store, adapters)
    notes += check_pending(store, adapters, cfg, fetch)
    notes += publish_pending(store, adapters, cfg, fetch)
    notes += reverify(store, cfg, fetch)
    ready = release_retries(store, cfg)
    return {"ready": ready, "notes": notes}


def notify_events(store, cfg: dict, state_path: Path | None = None) -> list[str]:
    """Queues the notifications worth an email: a drafted article waiting for approval, a link removed, or a credential
    problem that needs the owner. Each is queued once. A link being verified is emailed only when the config asks for it
    (notifications.verified: true): the dashboard already shows every verified link, and the daily emails were noise."""
    queued = []
    mail_verified = bool((cfg.get("notifications") or {}).get("verified", False))
    host = cfg["verification"]["canonical_hosts"][-1]
    iso = store.now().isocalendar()
    for job in store.jobs.values():
        link = job.get("publication_url") or job.get("draft_url")
        if job["status"] == S.VERIFIED:
            if mail_verified and notify(store, f"backlinks-verified-{job['id']}",
                                        f"A backlink to {host} is verified live: {job['publication_url']} (anchor: "
                                        f"{(job.get('link_attributes') or {}).get('anchor_text')}, "
                                        f"{'followed' if (job.get('link_attributes') or {}).get('follow') else 'nofollow/sponsored/ugc'})", state_path):
                queued.append("verified")
        elif job["status"] == S.PENDING_APPROVAL and job.get("draft_url") and notify(
                store, f"backlinks-draft-{job['id']}",
                f"A new article is saved as a private draft and needs your approval before it goes public: \"{job.get('title')}\". "
                f"Open {job['draft_url']}, read it, and press Publish on {job['domain']}. The system will then verify the link by itself. "
                f"(Or set live_publishing: true in config/backlinks.yaml to publish automatically.)", state_path):
            queued.append("draft")
        elif job["status"] == S.REMOVED and notify(store, f"backlinks-removed-{job['id']}",
                                                   f"A backlink that was verified is no longer live: {link}", state_path):
            queued.append("removed")
        elif job["status"] == S.BLOCKED_CREDENTIALS and notify(
                store, f"backlinks-blocked-{cfg['site_id']}-{job['adapter']}-{iso[0]}W{iso[1]}",
                f"Backlink publishing for {cfg['site_id']} on {job['adapter']} is blocked: {job.get('last_error')}. Please check the credential (GitHub secret).", state_path):
            queued.append("blocked")
    return queued
