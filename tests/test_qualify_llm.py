import json
import urllib.error

import pytest

from src.backlinks import qualify, states as S
from src.backlinks.adapters.base import Adapter
from src.backlinks.llm import LLM, LLMError, classify, gemini_keys


# ------------------------------------------------------------------ page inspection
def html(body):
    return f"<html><head><title>Write for us</title></head><body>{body}</body></html>"


FORM = '<form action="/submit" method="post"><input name="name"><input type="email" name="email"><input type="url" name="site"><textarea name="msg"></textarea></form>'


def mech(body):
    return qualify.inspect_page(html(body), "https://example.org/write-for-us")["mechanism"]


def test_each_mechanism_is_read_from_the_page():
    assert mech(FORM) == "form_submission"
    assert mech(FORM + '<div class="g-recaptcha" data-sitekey="x"></div>') == "captcha_required"
    assert mech(FORM + '<script src="https://challenges.cloudflare.com/turnstile/v0/api.js"></script><div class="cf-turnstile"></div>') == "captcha_required"
    assert mech('<p>You must be logged in to submit a post.</p>') == "account_required"
    assert mech('<form><input type="password"><input name="u"><input name="x"></form>') == "account_required"
    assert mech('<p>Want to write for us? Pitch your idea by email: <a href="mailto:editor@example.org">editor@example.org</a></p>') == "email_only"
    assert mech('<p>Welcome to our site about gardening.</p>') == "no_submission_route_found"
    assert mech('<p>Automated submissions are not allowed.</p>' + FORM) == "automation_prohibited"


def test_search_boxes_are_not_mistaken_for_submission_forms():
    assert mech('<form role="search" action="/search"><input type="search"><input><input></form>') == "no_submission_route_found"


def test_page_policy_signals_are_recorded_as_evidence():
    i = qualify.inspect_page(html(FORM + "<p>Our editorial team will review every submission. Links are dofollow.</p>"), "https://x.org/")
    assert i["flags"]["editorial_approval"] and i["flags"]["mentions_dofollow"] and not i["flags"]["mentions_nofollow"]
    assert any("editor reviews" in e for e in i["evidence"]) and i["forms"][0]["action"] == "/submit"


# ------------------------------------------------------------------ qualification
class Ad(Adapter):
    name, domain, mechanism = "devto", "dev.to", "authorised_api"


class Scripted:
    def __init__(self, reply):
        self.reply, self.calls = reply, 0

    def generate(self, prompt):
        self.calls += 1
        if isinstance(self.reply, Exception):
            raise self.reply
        return self.reply, "m"


def candidate(store, domain="example.org"):
    job, _ = store.add_job(f"k-{domain}", "web", domain, f"https://{domain}/write", "tavily: q")
    return job


GOOD = json.dumps({"relevance": 8, "quality": 7, "topic": "wellbeing", "audience": "adults", "reason": "fits"})


def test_site_with_no_submission_route_is_rejected_without_spending_a_gemini_call(store):
    llm = Scripted(GOOD)
    job = candidate(store)
    status = qualify.qualify_candidate(store, job, qualify.inspect_page(html("<p>hello</p>"), "u"), {}, llm, ["anxiety"])
    assert status == S.REJECTED and llm.calls == 0 and "no way to submit" in job["qualification"]["rationale"]


def test_site_needing_email_or_captcha_is_unsupported_and_costs_no_gemini_call(store):
    for body, needle in ((FORM + '<div class="g-recaptcha"></div>', "CAPTCHA"), ('<a href="mailto:e@x.org">write for us by email</a>', "email")):
        job = candidate(store, domain=f"s{len(needle)}.org")
        llm = Scripted(GOOD)
        status = qualify.qualify_candidate(store, job, qualify.inspect_page(html(body), "u"), {}, llm, ["anxiety"])
        assert status == S.UNSUPPORTED_PUBLICATION_METHOD and needle in job["qualification"]["rationale"]
        assert llm.calls == 0 and job["qualification"]["relevance"] is None and "no AI call" in job["qualification"]["relevance_note"]


def test_irrelevant_or_low_quality_sites_are_rejected_when_they_could_be_published_to(store):
    low = json.dumps({"relevance": 2, "quality": 7, "reason": "about cars"})
    job = candidate(store, "dev.to")
    assert qualify.qualify_candidate(store, job, qualify.inspect_page(html(FORM), "u"), {"devto": Ad()}, Scripted(low), ["x"]) == S.REJECTED
    farm = json.dumps({"relevance": 9, "quality": 1, "reason": "link farm"})
    job = candidate(store, "dev.to/other")
    job["domain"] = "dev.to"
    assert qualify.qualify_candidate(store, job, qualify.inspect_page(html(FORM), "u"), {"devto": Ad()}, Scripted(farm), ["x"]) == S.REJECTED


def test_a_confirmed_adapter_qualifies_the_site(store):
    job = candidate(store, "dev.to")
    llm = Scripted(GOOD)
    status = qualify.qualify_candidate(store, job, qualify.inspect_page(html(FORM), "u"), {"devto": Ad()}, llm, ["x"])
    assert status == S.QUALIFIED and job["adapter"] == "devto" and job["mechanism"] == "authorised_api"
    assert llm.calls == 1 and job["qualification"]["relevance"] == 8


def test_the_relevance_question_uses_the_sites_own_wording(store):
    seen = []

    class Spy(Scripted):
        def generate(self, prompt):
            seen.append(prompt)
            return super().generate(prompt)

    job = candidate(store, "dev.to")
    qualify.qualify_candidate(store, job, qualify.inspect_page(html(FORM), "u"), {"devto": Ad()}, Spy(GOOD), ["sports"],
                              site_label="a UK events marketplace", fit_criteria="fit with local sports events")
    assert "a UK events marketplace" in seen[0] and "fit with local sports events" in seen[0] and "hypnotherapy" not in seen[0]


def test_a_failed_relevance_call_does_not_block_or_invent_a_score(store):
    job = candidate(store, "dev.to")
    qualify.qualify_candidate(store, job, qualify.inspect_page(html(FORM), "u"), {"devto": Ad()}, Scripted(LLMError("quota", "q", True)), ["x"])
    assert job["status"] == S.QUALIFIED and job["qualification"]["relevance"] is None and "relevance_error" in job["qualification"]


def test_unknown_policy_is_recorded_as_not_stated_never_assumed_permitted(store):
    job = candidate(store)
    qualify.qualify_candidate(store, job, qualify.inspect_page(html(FORM), "u"), {}, None, ["x"])
    q = job["qualification"]
    assert q["automated_submission_permitted"] == "not stated" and q["link_type_stated"] == "not stated"


# ------------------------------------------------------------------ Gemini keys, failover and classification
def http(code):
    return urllib.error.HTTPError("u", code, "x", {}, None)


def test_extra_keys_come_first_so_the_primary_keys_quota_stays_for_the_emails(monkeypatch):
    for n in ("GEMINI_API_KEY", "GEMINI_API_KEY_2", "GEMINI_API_KEY_3"):
        monkeypatch.delenv(n, raising=False)
    monkeypatch.setenv("GEMINI_API_KEY", "a")
    assert gemini_keys() == ["a"], "with only the primary key it is used alone"
    monkeypatch.setenv("GEMINI_API_KEY_2", "b"); monkeypatch.setenv("GEMINI_API_KEY_3", "a")
    assert gemini_keys() == ["b", "a"], "duplicates are dropped and the extra key leads"


def test_a_quota_error_reports_which_quota_google_says_was_hit():
    import io
    from src.backlinks.llm import classify
    body = json.dumps({"error": {"code": 429, "details": [{"violations": [{"quotaId": "GenerateRequestsPerDayPerProjectPerModel-FreeTier", "quotaValue": "20"}]}]}})
    e = urllib.error.HTTPError("u", 429, "x", {}, io.BytesIO(b""))
    e.body_text = body
    err = classify(e)
    assert err.category == "quota" and "GenerateRequestsPerDayPerProjectPerModel-FreeTier" in str(err) and "limit 20" in str(err)
    assert "(HTTP 429)" in str(classify(urllib.error.HTTPError("u", 429, "x", {}, io.BytesIO(b""))))


def daily_429():
    import io
    e = urllib.error.HTTPError("u", 429, "x", {}, io.BytesIO(b""))
    e.body_text = json.dumps({"error": {"details": [{"violations": [{"quotaId": "GenerateRequestsPerDayPerProjectPerModel-FreeTier", "quotaValue": "20"}]}]}})
    return e


def test_classification():
    assert classify(http(429)).category == "quota" and classify(http(429)).retryable
    assert classify(http(429)).temporary is True, "a 429 with no per-day name may be a per-minute limit: worth waiting"
    assert classify(daily_429()).temporary is False, "a quota Google names as per-day will not recover in minutes"
    assert classify(http(401)).category == "auth" and not classify(http(403)).retryable and not classify(http(401)).temporary
    assert classify(http(503)).category == "busy" and classify(http(503)).temporary
    assert classify(TimeoutError("t")).category == "network" and classify(TimeoutError("t")).temporary
    assert classify(ValueError("Gemini stopped before finishing (finishReason='MAX_TOKENS')")).category == "truncated"
    assert classify(KeyError("x")).category == "unknown" and not classify(KeyError("x")).temporary


def make_llm(store, gen, keys=("k1",), **kw):
    sleeps = []
    llm = LLM(store, 100, generate=gen, keys=list(keys), sleep=sleeps.append, **kw)
    return llm, sleeps


def test_a_key_out_of_quota_for_the_day_is_skipped_for_the_rest_of_the_run(store):
    calls = []

    def gen(prompt, key):
        calls.append(key)
        if key == "k1":
            raise daily_429()
        return "text", "gemini-3.8-flash"

    llm, sleeps = make_llm(store, gen, keys=("k1", "k2"))
    assert llm.generate("p") == ("text", "gemini-3.8-flash") and calls == ["k1", "k2"]
    llm.generate("p")
    assert calls == ["k1", "k2", "k2"] and sleeps == [], "no waiting: the second key had capacity"


def test_a_busy_gemini_is_waited_out_and_tried_again(store):
    """The owner's observation: when Gemini is busy, another try a few minutes later works."""
    outcomes = [http(503), TimeoutError("read timed out"), ("text", "gemini-3.7-flash")]
    calls = []

    def gen(prompt, key):
        calls.append(key)
        r = outcomes.pop(0)
        if isinstance(r, Exception):
            raise r
        return r

    llm, sleeps = make_llm(store, gen)
    assert llm.generate("p") == ("text", "gemini-3.7-flash")
    assert sleeps == [60, 120] and len(calls) == 3 and store.used_today("gemini") == 3


def test_a_per_minute_limit_is_waited_out_and_does_not_poison_the_key(store):
    outcomes = [http(429), ("first", "m"), ("second", "m")]

    def gen(prompt, key):
        r = outcomes.pop(0)
        if isinstance(r, Exception):
            raise r
        return r

    llm, sleeps = make_llm(store, gen)
    assert llm.generate("1") == ("first", "m") and sleeps == [60]
    assert llm.generate("2") == ("second", "m"), "the key was not marked as used up"


def test_it_does_not_wait_forever(store):
    llm, sleeps = make_llm(store, lambda p, k: (_ for _ in ()).throw(http(503)))
    with pytest.raises(LLMError) as e:
        llm.generate("p")
    assert e.value.category == "busy" and sleeps == [60, 120]


def test_the_total_waiting_time_is_capped(store):
    llm, sleeps = make_llm(store, lambda p, k: (_ for _ in ()).throw(http(503)), wait_budget=100)
    with pytest.raises(LLMError):
        llm.generate("p")
    assert sleeps == [60], "the next wait (120s) would go over the 100s budget"


def test_a_daily_quota_is_not_waited_for(store):
    calls = []
    llm, sleeps = make_llm(store, lambda p, k: calls.append(k) or (_ for _ in ()).throw(daily_429()))
    with pytest.raises(LLMError) as e:
        llm.generate("p")
    assert e.value.category == "quota" and "PerDay" in str(e.value) and sleeps == [] and calls == ["k1"]
    with pytest.raises(LLMError) as e2:
        llm.generate("p")
    assert e2.value.category == "quota" and "PerDay" in str(e2.value), "the same Google message is reported again"
    assert calls == ["k1"], "later calls in the same run do not try the used-up key again"


def test_a_refused_key_is_skipped_but_a_busy_service_is_not_a_reason_to_switch_keys(store):
    calls = []

    def gen(prompt, key):
        calls.append(key)
        raise http(401) if key == "bad" else http(503)

    llm, _ = make_llm(store, gen, keys=("bad", "good"), patience=())
    with pytest.raises(LLMError) as e:
        llm.generate("p")
    assert e.value.category == "busy" and calls == ["bad", "good"]
    llm, _ = make_llm(store, gen, keys=("good",), patience=())
    with pytest.raises(LLMError):
        llm.generate("p")


def test_a_refused_key_never_waits(store):
    llm, sleeps = make_llm(store, lambda p, k: (_ for _ in ()).throw(http(401)))
    with pytest.raises(LLMError) as e:
        llm.generate("p")
    assert e.value.category == "auth" and sleeps == []


def test_no_key_raises_a_clear_error(store):
    with pytest.raises(LLMError) as e:
        LLM(store, 100, generate=lambda p, k: ("x", "m"), keys=[]).generate("p")
    assert e.value.category == "auth"


def test_our_own_daily_cap_stops_calls_and_counts_usage(store):
    llm = LLM(store, 2, generate=lambda p, k: ("x", "m"), keys=["a"], sleep=lambda s: None)
    llm.generate("1"); llm.generate("2")
    assert store.used_today("gemini") == 2
    with pytest.raises(LLMError) as e:
        llm.generate("3")
    assert e.value.category == "quota" and "daily cap" in str(e.value)


# ------------------------------------------------------------------ the owner's cap: at most 5 minutes lost to Gemini trouble per website per run
class Clock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t


def test_hanging_requests_count_towards_the_five_minutes_and_then_gemini_waits_for_the_next_run(store):
    clock, calls = Clock(), []

    def hangs(prompt, key):   # every request hangs for two minutes and then times out
        calls.append(prompt)
        clock.t += 120
        raise TimeoutError("read timed out")

    llm, sleeps = make_llm(store, hangs, clock=clock)
    with pytest.raises(LLMError):
        llm.generate("first")
    assert llm._lost <= 300 + 120 and sleeps == [60], "120s lost, a 60s pause, 120s lost again: the next pause would pass 5 minutes"
    with pytest.raises(LLMError) as e:
        llm.generate("second")
    assert e.value.category == "busy" and "5 minutes" in str(e.value) and calls == ["first", "first"], "no more requests once 5 minutes are lost"


def test_a_normal_reply_costs_nothing_and_only_the_slow_part_of_a_reply_counts(store):
    clock = Clock()

    def answers(prompt, key):
        clock.t += 40 if prompt == "quick" else 200   # a 200-second reply was mostly retries inside the request
        return "ok", "m"

    llm, _ = make_llm(store, answers, clock=clock)
    for _ in range(20):
        llm.generate("quick")
    assert llm._lost == 0, "twenty normal replies never touch the budget"
    llm.generate("slow")
    assert llm._lost == 200 - 90


def test_the_request_itself_is_given_a_deadline_inside_the_five_minutes(store):
    clock, seen = Clock(), []

    def with_deadline(prompt, key, deadline=None):
        seen.append(deadline - clock.t)
        return "ok", "m"

    llm, _ = make_llm(store, with_deadline, clock=clock)
    llm.generate("p")
    assert seen == [300 + 90], "a request can never run past what is left of the 5 minutes (plus the time a normal reply takes)"


def test_the_gemini_request_never_starts_after_its_deadline(monkeypatch):
    import time as _time
    from src import outreach
    monkeypatch.setattr(outreach.urllib.request, "urlopen", lambda *a, **k: pytest.fail("no request after the deadline"))
    with pytest.raises(TimeoutError):
        outreach.gemini_generate("p", "key", deadline=_time.monotonic() - 1)
