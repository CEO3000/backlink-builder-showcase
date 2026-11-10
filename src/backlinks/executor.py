"""Submits prepared content through an adapter and independently verifies the result. A successful API answer is
recorded as exactly that; only verify_job(), which reads the live page, can make a job VERIFIED."""
from datetime import timedelta
from urllib.parse import urlparse

from src.backlinks import states as S
from src.backlinks.adapters.base import Adapter
from src.backlinks.verify import bare_host, counts_for_seo, verify_backlink, http_fetch


def schedule_retry(store, job: dict, cfg: dict, category: str, message: str, count: bool = True) -> str:
    """Bounded retry with backoff. After max_retries the job is REJECTED, never retried forever.
    count=False is for failures that are not the job's fault (our own AI quota ran out): it waits and tries again
    without using up the job's retries."""
    limits = cfg["limits"]
    if count:
        job["retry_count"] += 1
    job["last_error"], job["error_category"] = message[:300], category
    store.record_failure(category)
    if job["retry_count"] > limits["max_retries"]:
        store.transition(job, S.REJECTED, f"retries exhausted after {limits['max_retries']} attempts: {message[:120]}")
        return S.REJECTED
    delays = limits["retry_backoff_hours"]
    wait = delays[min(max(job["retry_count"] - 1, 0), len(delays) - 1)]
    store.transition(job, S.RETRY_REQUIRED, f"{category}: {message[:120]} (retry {job['retry_count']}/{limits['max_retries']} in {wait}h)",
                     next_action_at=(store.now() + timedelta(hours=wait)).isoformat())
    return S.RETRY_REQUIRED


def validate_before_submit(job: dict, cfg: dict) -> str | None:
    """A reason the job must not be submitted, or None."""
    hosts = {bare_host(h) for h in cfg["verification"]["canonical_hosts"]}
    target = job.get("target_url") or ""
    parsed = urlparse(target)
    if parsed.scheme != "https" or bare_host(target) not in hosts:
        return f"the target {target!r} is not an https page on our own site"
    if not job.get("content_path") or not job.get("title"):
        return "there is no saved content to submit"
    return None


def submit_job(store, job: dict, adapter: Adapter, content: dict, live: bool, cfg: dict, fetch=http_fetch) -> str:
    """CONTENT_READY -> ... Returns the new status."""
    problem = validate_before_submit(job, cfg)
    if problem:
        store.transition(job, S.REJECTED, problem, last_error=problem, error_category="validation")
        store.record_failure("validation")
        return S.REJECTED

    store.transition(job, S.SUBMISSION_IN_PROGRESS, "submitting")
    store.save()  # written before the network call: a crash now leaves a visible in-progress job, never a silent duplicate

    existing = adapter.find_existing(job["title"])  # a previous attempt may have got through before a crash
    result = existing or adapter.submit(content, live)
    store.bump("submission_attempts")
    job["submitted_at"] = store.now_iso()

    if result.outcome == "published":
        store.transition(job, S.PUBLISHED, "published" + (" (already existed)" if existing else ""),
                         publication_url=result.url, submission_reference=result.reference)
        store.bump("published")
        return verify_job(store, job, cfg, fetch)
    if result.outcome == "draft":
        store.transition(job, S.PENDING_APPROVAL, "saved as a private draft; publish it on the platform to go live",
                         submission_reference=result.reference, draft_url=result.url)
        store.bump("drafts_pending_approval")
        return S.PENDING_APPROVAL
    if result.outcome == "pending_approval":
        store.transition(job, S.PENDING_APPROVAL, "waiting for the platform's editors", submission_reference=result.reference)
        return S.PENDING_APPROVAL
    if result.outcome == "rejected":
        store.record_failure(result.error_category or "rejected")
        store.transition(job, S.REJECTED, f"rejected by the platform: {result.message}", last_error=result.message,
                         error_category=result.error_category or "rejected")
        return S.REJECTED
    # error
    if result.error_category == "auth":
        store.record_failure("auth")
        store.transition(job, S.BLOCKED_CREDENTIALS, f"the platform refused our credentials: {result.message}",
                         last_error=result.message, error_category="auth")
        return S.BLOCKED_CREDENTIALS
    if result.retryable:
        return schedule_retry(store, job, cfg, result.error_category or "unknown", result.message)
    store.record_failure(result.error_category or "unknown")
    store.transition(job, S.REJECTED, f"submission failed and cannot be retried: {result.message}", last_error=result.message,
                     error_category=result.error_category or "unknown")
    return S.REJECTED


def next_check(store, job: dict, cfg: dict) -> str:
    """When a live link is looked at again: after 1 day, then 7, then every 30 days for as long as it is live."""
    days = cfg["limits"]["verify_recheck_days"]
    rechecks_done = max(job.get("verification_checks", 1) - 1, 0)   # the first verification is not a re-check
    return (store.now() + timedelta(days=days[min(rechecks_done, len(days) - 1)])).isoformat()


def verify_job(store, job: dict, cfg: dict, fetch=http_fetch) -> str:
    """Reads the live page. This is the only place a job can become VERIFIED."""
    expected = job.get("expected_urls") or [job["target_url"]]
    result = verify_backlink(job["publication_url"], expected, cfg["verification"]["canonical_hosts"],
                             expected_text=job.get("title"), fetch=fetch, now_iso=store.now_iso())
    job["verification_checks"] = job.get("verification_checks", 0) + 1
    job["last_verified_at"] = result["checked_at"]
    was_verified = job.get("first_verified_at") is not None
    outcome = result["outcome"]

    if outcome == "verified":
        link = result["matched_link"]
        store.transition(job, S.VERIFIED, "the live page carries the link" if not was_verified else "re-checked: still live",
                         verification=result, link_attributes={"rel": link["rel"], "follow": link["follow"], "anchor_text": link["anchor"], "kind": link["kind"],
                                          "indexable": result["indexing"]["indexable"], "page_nofollow": result["indexing"]["page_nofollow"],
                                          "counts_for_seo": counts_for_seo(link["follow"], result["indexing"])},
                         first_verified_at=job.get("first_verified_at") or result["checked_at"], verification_failures=0,
                         next_action_at=next_check(store, job, cfg))
        if not was_verified:
            store.bump("verified")
        return S.VERIFIED

    job["verification"] = result
    job["verification_failures"] = job.get("verification_failures", 0) + 1
    store.record_failure(f"verify_{outcome}")
    limit = cfg["limits"]["verify_failures_before_inaccessible"]
    if outcome in ("page_gone", "link_absent") and was_verified:
        store.transition(job, S.REMOVED, f"it was verified before and is no longer live: {result['reason']}", next_action_at=None)
        store.bump("removed")
        return S.REMOVED
    if outcome == "page_gone" or (outcome == "page_unreachable" and job["verification_failures"] >= limit):
        store.transition(job, S.INACCESSIBLE, f"permanently inaccessible: {result['reason']}", next_action_at=None)
        return S.INACCESSIBLE
    retry_in = cfg["limits"]["retry_backoff_hours"][min(job["verification_failures"] - 1, len(cfg["limits"]["retry_backoff_hours"]) - 1)]
    giving_up = job["verification_failures"] >= limit
    if job["status"] != S.VERIFICATION_FAILED:
        store.transition(job, S.VERIFICATION_FAILED, result["reason"],
                         next_action_at=None if giving_up else (store.now() + timedelta(hours=retry_in)).isoformat())
    else:
        job["next_action_at"] = None if giving_up else (store.now() + timedelta(hours=retry_in)).isoformat()
    return S.VERIFICATION_FAILED
