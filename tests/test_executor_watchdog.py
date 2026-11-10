import json
from datetime import timedelta

import pytest

from conftest import FakeWeb, link, page
from src.backlinks import executor, states as S, watchdog
from src.backlinks.adapters.base import Adapter, SubmissionResult

TARGET = "https://www.hypnotreatment.uk/treatments/insomnia"
PUB_URL = "https://dev.to/someone/post-1abc"
CONTENT = {"title": "Sleep and screens", "body_markdown": "b", "description": "d", "tags": ["sleep"]}


class FakeAdapter(Adapter):
    name, domain, mechanism, docs_url = "devto", "dev.to", "authorised_api", "https://docs.example"

    def __init__(self, results=(), existing=None, available=True):
        self.results, self.existing, self._available = list(results), existing, available
        self.submitted = []

    def available(self):
        return (self._available, "ok" if self._available else "no key")

    def submit(self, content, live):
        self.submitted.append((content, live))
        return self.results.pop(0)

    def find_existing(self, title):
        return self.existing


def published(url=PUB_URL):
    return SubmissionResult("published", url=url, reference="1")


def make_job(store, status=S.CONTENT_READY, key="k", **kw):
    job, _ = store.add_job(key, "devto", "dev.to", "https://dev.to/", "configured topic", target_url=TARGET,
                           expected_urls=[TARGET, "https://www.hypnotreatment.uk/"], title="Sleep and screens", content_path="content/k.json", retry_count=0)
    job.update(kw)
    path = ["QUALIFIED", "CONTENT_READY", "SUBMISSION_IN_PROGRESS", "PUBLISHED", "VERIFIED"]
    job["status"] = status
    return job


def live_web():
    return FakeWeb({PUB_URL: page(link(TARGET)), TARGET: "ok"})


def test_accepted_submission_with_a_live_link_becomes_verified(store, cfg):
    job = make_job(store)
    status = executor.submit_job(store, job, FakeAdapter([published()]), CONTENT, True, cfg, fetch=live_web())
    assert status == S.VERIFIED and job["publication_url"] == PUB_URL and job["first_verified_at"]
    assert job["link_attributes"] == {"rel": [], "follow": True, "anchor_text": "hypnotherapy for insomnia", "kind": "text_link",
                                      "indexable": True, "page_nofollow": False, "counts_for_seo": True}
    assert [h["to"] for h in job["history"]][-3:] == [S.SUBMISSION_IN_PROGRESS, S.PUBLISHED, S.VERIFIED]
    assert store.verified_on() == 1


def test_accepted_submission_without_a_link_on_the_page_is_never_counted(store, cfg):
    job = make_job(store)
    status = executor.submit_job(store, job, FakeAdapter([published()]), CONTENT, True, cfg, fetch=FakeWeb({PUB_URL: page()}))
    assert status == S.VERIFICATION_FAILED and store.verified_on() == 0
    assert job["verification"]["outcome"] == "link_absent" and job["next_action_at"]


def test_a_draft_waits_for_approval_and_is_not_published(store, cfg):
    job = make_job(store)
    adapter = FakeAdapter([SubmissionResult("draft", url="https://dev.to/me/draft", reference="9")])
    assert executor.submit_job(store, job, adapter, CONTENT, False, cfg, fetch=live_web()) == S.PENDING_APPROVAL
    assert adapter.submitted[0][1] is False and job["draft_url"] == "https://dev.to/me/draft" and not job["publication_url"]
    assert store.verified_on() == 0


def test_a_draft_the_owner_publishes_is_picked_up_and_verified(store, cfg):
    job = make_job(store, status=S.PENDING_APPROVAL, draft_url="https://dev.to/me/draft")
    adapter = FakeAdapter(existing=published())
    notes = watchdog.check_pending(store, {"devto": adapter}, cfg, fetch=live_web())
    assert job["status"] == S.VERIFIED and "published by its owner" in notes[0]


def test_a_draft_still_unpublished_stays_pending(store, cfg):
    job = make_job(store, status=S.PENDING_APPROVAL)
    watchdog.check_pending(store, {"devto": FakeAdapter(existing=SubmissionResult("draft", url="u"))}, cfg, fetch=live_web())
    assert job["status"] == S.PENDING_APPROVAL


def test_platform_rejection_and_validation_errors_are_final(store, cfg):
    job = make_job(store)
    r = SubmissionResult("rejected", message="Title is too short", error_category="validation")
    assert executor.submit_job(store, job, FakeAdapter([r]), CONTENT, True, cfg) == S.REJECTED
    assert job["last_error"] == "Title is too short" and job["error_category"] == "validation"


def test_auth_failure_blocks_instead_of_retrying(store, cfg):
    job = make_job(store)
    r = SubmissionResult("error", message="bad key", error_category="auth", http_status=401)
    assert executor.submit_job(store, job, FakeAdapter([r]), CONTENT, True, cfg) == S.BLOCKED_CREDENTIALS
    assert job["retry_count"] == 0


def test_transient_errors_retry_with_growing_backoff_then_stop(store, cfg, clock):
    job = make_job(store)
    err = lambda: SubmissionResult("error", message="busy", error_category="server", retryable=True)
    waits = []
    for attempt in range(cfg["limits"]["max_retries"]):
        assert executor.submit_job(store, job, FakeAdapter([err()]), CONTENT, True, cfg) == S.RETRY_REQUIRED
        waits.append((json_time(job["next_action_at"]) - clock()).total_seconds() / 3600)
        store.transition(job, S.CONTENT_READY, "retry")
    assert waits == [1, 6, 24]
    assert executor.submit_job(store, job, FakeAdapter([err()]), CONTENT, True, cfg) == S.REJECTED
    assert "retries exhausted" in job["history"][-1]["note"] and job["retry_count"] == cfg["limits"]["max_retries"] + 1


def json_time(s):
    from datetime import datetime
    return datetime.fromisoformat(s)


def test_a_job_with_a_target_outside_our_site_is_never_submitted(store, cfg):
    for bad in ("https://evil.example.com/page", "http://www.hypnotreatment.uk/treatments/insomnia", ""):
        job = make_job(store, key=f"k{bad}", target_url=bad)
        adapter = FakeAdapter([published()])
        assert executor.submit_job(store, job, adapter, CONTENT, True, cfg) == S.REJECTED
        assert adapter.submitted == []


def test_a_crash_between_submit_and_save_cannot_cause_a_duplicate(store, cfg):
    job = make_job(store)
    adapter = FakeAdapter([published("https://dev.to/should-not-be-created")], existing=published())
    assert executor.submit_job(store, job, adapter, CONTENT, True, cfg, fetch=live_web()) == S.VERIFIED
    assert adapter.submitted == [] and job["publication_url"] == PUB_URL


def test_in_progress_state_is_saved_before_the_network_call(store, cfg):
    job = make_job(store)
    seen = {}

    class Spy(FakeAdapter):
        def find_existing(self, title):
            seen["on_disk"] = json.loads((store.root / "jobs.json").read_text())[job["id"]]["status"]

    executor.submit_job(store, job, Spy([SubmissionResult("rejected", message="x")]), CONTENT, True, cfg)
    assert seen["on_disk"] == S.SUBMISSION_IN_PROGRESS


def test_stuck_job_is_recovered_when_the_article_exists(store, cfg, clock):
    job = make_job(store, status=S.SUBMISSION_IN_PROGRESS)
    clock.advance(minutes=45)
    notes = watchdog.recover_stuck(store, {"devto": FakeAdapter(existing=published())}, cfg, fetch=live_web())
    assert job["status"] == S.VERIFIED and "recovered" in notes[0]


def test_stuck_job_without_an_article_is_scheduled_for_retry(store, cfg, clock):
    job = make_job(store, status=S.SUBMISSION_IN_PROGRESS)
    clock.advance(minutes=45)
    watchdog.recover_stuck(store, {"devto": FakeAdapter()}, cfg)
    assert job["status"] == S.RETRY_REQUIRED and job["error_category"] == "stuck"


def test_a_fresh_in_progress_job_is_left_alone(store, cfg, clock):
    job = make_job(store, status=S.SUBMISSION_IN_PROGRESS)
    clock.advance(minutes=5)
    assert watchdog.recover_stuck(store, {"devto": FakeAdapter()}, cfg) == [] and job["status"] == S.SUBMISSION_IN_PROGRESS


def test_retries_are_released_only_after_their_backoff(store, cfg, clock):
    job = make_job(store)
    store.transition(job, S.SUBMISSION_IN_PROGRESS, "x")
    executor.schedule_retry(store, job, cfg, "server", "busy")
    assert watchdog.release_retries(store, cfg) == [] and job["status"] == S.RETRY_REQUIRED
    clock.advance(hours=1, minutes=1)
    assert watchdog.release_retries(store, cfg) == [job] and job["status"] == S.CONTENT_READY


def test_retry_without_content_goes_back_to_qualified_for_a_rewrite(store, cfg, clock):
    job = make_job(store, status=S.QUALIFIED, content_path=None)
    executor.schedule_retry(store, job, cfg, "quota", "out of quota")
    clock.advance(hours=2)
    assert watchdog.release_retries(store, cfg) == [] and job["status"] == S.QUALIFIED


def test_blocked_jobs_resume_once_the_credential_returns(store, cfg):
    job = make_job(store, status=S.BLOCKED_CREDENTIALS)
    assert watchdog.unblock(store, {"devto": FakeAdapter(available=False)}) == [] and job["status"] == S.BLOCKED_CREDENTIALS
    assert watchdog.unblock(store, {"devto": FakeAdapter(available=True)}) and job["status"] == S.QUALIFIED


def test_verified_link_is_rechecked_on_schedule_and_a_vanished_one_is_marked_removed(store, cfg, clock):
    job = make_job(store)
    executor.submit_job(store, job, FakeAdapter([published()]), CONTENT, True, cfg, fetch=live_web())
    assert job["status"] == S.VERIFIED and job["next_action_at"]
    clock.advance(hours=1)
    assert watchdog.reverify(store, cfg, fetch=live_web()) == []                       # not due yet
    clock.advance(days=1)
    watchdog.reverify(store, cfg, fetch=live_web())
    assert job["status"] == S.VERIFIED and job["verification_checks"] == 2             # still live, re-checked
    clock.advance(days=8)
    watchdog.reverify(store, cfg, fetch=FakeWeb({PUB_URL: page()}))
    assert job["status"] == S.REMOVED and store.verified_on() == 0


def test_a_transient_outage_does_not_remove_a_verified_link(store, cfg, clock):
    job = make_job(store)
    executor.submit_job(store, job, FakeAdapter([published()]), CONTENT, True, cfg, fetch=live_web())
    clock.advance(days=2)
    from src.backlinks.verify import FetchResult
    watchdog.reverify(store, cfg, fetch=FakeWeb({PUB_URL: FetchResult(503, PUB_URL, "", "HTTP 503")}))
    assert job["status"] == S.VERIFICATION_FAILED
    clock.advance(hours=2)
    watchdog.reverify(store, cfg, fetch=live_web())
    assert job["status"] == S.VERIFIED


def test_a_published_page_that_never_loads_ends_inaccessible_after_bounded_attempts(store, cfg, clock):
    from src.backlinks.verify import FetchResult
    job = make_job(store)
    down = FakeWeb({PUB_URL: FetchResult(None, PUB_URL, "", "TimeoutError")})
    executor.submit_job(store, job, FakeAdapter([published()]), CONTENT, True, cfg, fetch=down)
    for _ in range(5):
        clock.advance(days=2)
        watchdog.reverify(store, cfg, fetch=down)
    assert job["status"] == S.INACCESSIBLE and job["verification_failures"] == cfg["limits"]["verify_failures_before_inaccessible"]


def test_a_404_on_a_never_verified_page_is_permanent(store, cfg):
    job = make_job(store)
    executor.submit_job(store, job, FakeAdapter([published()]), CONTENT, True, cfg, fetch=FakeWeb({}))
    assert job["status"] == S.INACCESSIBLE


def test_notifications_are_queued_once_and_reach_the_existing_relay(store, cfg, tmp_path):
    job = make_job(store)
    executor.submit_job(store, job, FakeAdapter([published()]), CONTENT, True, cfg, fetch=live_web())
    state_file = tmp_path / "agent_state.json"
    cfg = {**cfg, "notifications": {"verified": True}}   # off by default; this checks the opt-in still works
    assert watchdog.notify_events(store, cfg, state_file) == ["verified"]
    assert watchdog.notify_events(store, cfg, state_file) == []                        # never twice
    notes = json.loads(state_file.read_text())["notifications"]
    assert notes[0]["run_id"] == f"backlinks-verified-{job['id']}" and PUB_URL in notes[0]["message"] and "followed" in notes[0]["message"]
    assert {"run_id", "message", "created_at"} <= set(notes[0])


def test_a_verified_link_sends_no_email_unless_asked_but_problems_still_do(store, cfg, tmp_path):
    state_file = tmp_path / "agent_state.json"
    job = make_job(store)
    executor.submit_job(store, job, FakeAdapter([published()]), CONTENT, True, cfg, fetch=live_web())
    make_job(store, key="b", status=S.PENDING_APPROVAL, draft_url="https://dev.to/me/d")
    assert watchdog.notify_events(store, cfg, state_file) == ["draft"], "no 'verified live' email by default, a draft waiting for you still is sent"
    assert "verified live" not in state_file.read_text()


def test_blocked_and_draft_and_removed_notifications(store, cfg, tmp_path):
    state_file = tmp_path / "agent_state.json"
    make_job(store, key="a", status=S.BLOCKED_CREDENTIALS, last_error="bad key")
    make_job(store, key="b", status=S.PENDING_APPROVAL, draft_url="https://dev.to/me/d")
    make_job(store, key="c", status=S.REMOVED, publication_url=PUB_URL)
    assert sorted(watchdog.notify_events(store, cfg, state_file)) == ["blocked", "draft", "removed"]
    text = " ".join(n["message"] for n in json.loads(state_file.read_text())["notifications"])
    assert "needs your approval" in text and "no longer live" in text and "credential" in text
