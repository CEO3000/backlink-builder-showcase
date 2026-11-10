import json

import pytest

from src.backlinks import states as S
from src.backlinks.store import Store, make_key


def test_every_state_has_a_defined_set_of_exits_and_terminals_have_none():
    assert set(S.TRANSITIONS) == S.ALL
    assert S.TERMINAL == {S.REJECTED, S.REMOVED, S.UNSUPPORTED_PUBLICATION_METHOD, S.INACCESSIBLE}
    for nxt in S.TRANSITIONS.values():
        assert nxt <= S.ALL


def test_the_happy_path_is_allowed_and_shortcuts_are_not():
    path = [S.DISCOVERED, S.QUALIFIED, S.CONTENT_READY, S.SUBMISSION_IN_PROGRESS, S.SUBMITTED, S.PUBLISHED, S.VERIFIED]
    for a, b in zip(path, path[1:]):
        S.check_transition(a, b)
    for a, b in ((S.DISCOVERED, S.VERIFIED), (S.CONTENT_READY, S.VERIFIED), (S.SUBMISSION_IN_PROGRESS, S.VERIFIED),
                 (S.PENDING_APPROVAL, S.VERIFIED), (S.SUBMITTED, S.VERIFIED), (S.REJECTED, S.QUALIFIED), (S.REMOVED, S.VERIFIED)):
        with pytest.raises(S.IllegalTransition):
            S.check_transition(a, b)


def test_only_a_published_or_previously_checked_job_can_become_verified():
    can_verify = {s for s, nxt in S.TRANSITIONS.items() if S.VERIFIED in nxt}
    assert can_verify == {S.PUBLISHED, S.VERIFICATION_FAILED, S.VERIFIED}


def test_unknown_status_is_refused():
    with pytest.raises(S.IllegalTransition):
        S.check_transition(S.DISCOVERED, "DONE")


def test_same_key_never_creates_a_second_job(store):
    key = make_key("devto", "dev.to", "screens-and-sleep")
    a, created_a = store.add_job(key, "devto", "dev.to", "https://dev.to/", "configured topic")
    b, created_b = store.add_job(key, "devto", "dev.to", "https://dev.to/", "again")
    assert created_a and not created_b and a is b and len(store.jobs) == 1


def test_key_ignores_case_and_www():
    assert make_key("web", "WWW.Example.com", "Https://X") == make_key("web", "example.com", "https://x")
    assert make_key("devto", "dev.to", "a") != make_key("devto", "dev.to", "b")


def test_transition_is_recorded_in_history_and_illegal_ones_leave_the_job_alone(store):
    job, _ = store.add_job("k1", "devto", "dev.to", "u", "src")
    store.transition(job, S.QUALIFIED, "ok")
    with pytest.raises(S.IllegalTransition):
        store.transition(job, S.VERIFIED, "cheating")
    assert job["status"] == S.QUALIFIED
    assert [h["to"] for h in job["history"]] == [S.DISCOVERED, S.QUALIFIED]


def test_verified_count_only_counts_verified_jobs_first_verified_today(store, clock):
    j1, _ = store.add_job("a", "devto", "dev.to", "u", "s")
    j2, _ = store.add_job("b", "devto", "dev.to", "u", "s")
    j3, _ = store.add_job("c", "devto", "dev.to", "u", "s")
    for j in (j1, j2, j3):
        j["status"] = S.PUBLISHED
    j1.update(status=S.VERIFIED, first_verified_at=clock().isoformat())
    j2.update(status=S.VERIFICATION_FAILED, first_verified_at=clock().isoformat())   # not live now: not counted
    assert store.verified_on() == 1
    j3.update(status=S.PUBLISHED)                                                    # submitted/published is not success
    assert store.verified_on() == 1


def test_a_live_job_blocks_its_domain_but_finished_ones_do_not(store):
    job, _ = store.add_job("a", "web", "example.com", "https://example.com/p", "s")
    assert store.domain_has_live_job("web", "www.example.com")
    store.transition(job, S.REJECTED, "no")
    assert not store.domain_has_live_job("web", "example.com")


def test_state_survives_a_restart(tmp_path, clock):
    s1 = Store(tmp_path / "x", clock=clock)
    job, _ = s1.add_job("a", "devto", "dev.to", "u", "s")
    s1.bump("generated")
    s1.add_usage("tavily", 3)
    path = s1.save_content(job["id"], json.dumps({"title": "t"}))
    s1.save()
    s2 = Store(tmp_path / "x", clock=clock)
    assert s2.jobs["a"]["status"] == S.DISCOVERED and s2.count("generated") == 1
    assert s2.used_this_month("tavily") == 3 and s2.used_today("tavily") == 3
    assert json.loads(s2.read_content(path))["title"] == "t"


def test_usage_resets_with_the_month(store, clock):
    store.add_usage("firecrawl", 5)
    clock.advance(days=10)  # 2026-11-06
    assert store.used_this_month("firecrawl") == 0 and store.used_today("firecrawl") == 0


def test_corrupt_state_file_does_not_crash(tmp_path, clock):
    (tmp_path / "bad").mkdir()
    (tmp_path / "bad" / "jobs.json").write_text("{not json")
    assert Store(tmp_path / "bad", clock=clock).jobs == {}
