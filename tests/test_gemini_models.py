"""The shared Gemini client (src/outreach.py): each model has its own free daily allowance, so an exhausted
model must hand over to the next at once, without waiting."""
import io
import json
import urllib.error
from unittest import mock

import pytest

from src import outreach

MODELS = outreach.GEMINI_MODELS


class Resp(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def ok(text="hello"):
    return Resp(json.dumps({"candidates": [{"finishReason": "STOP", "content": {"parts": [{"text": text}]}}]}).encode())


def daily_429(retry_delay="33s"):
    body = {"error": {"code": 429, "details": [{"@type": "type.googleapis.com/google.rpc.QuotaFailure", "violations": [
        {"quotaId": "GenerateRequestsPerDayPerProjectPerModel-FreeTier", "quotaValue": "20"}]},
        {"@type": "type.googleapis.com/google.rpc.RetryInfo", "retryDelay": retry_delay}]}}
    return urllib.error.HTTPError("u", 429, "x", {}, io.BytesIO(json.dumps(body).encode()))


def per_minute_429(retry_delay="12s"):
    body = {"error": {"code": 429, "details": [{"violations": [{"quotaId": "GenerateRequestsPerMinutePerProjectPerModel-FreeTier"}]},
                                              {"retryDelay": retry_delay}]}}
    return urllib.error.HTTPError("u", 429, "x", {}, io.BytesIO(json.dumps(body).encode()))


def run(plan):
    """plan: {model: [responses]} where an exception instance is raised and 'ok' returns a good reply."""
    calls, sleeps = [], []

    def fake(req, timeout=None):
        model = req.full_url.split("/models/")[1].split(":")[0]
        calls.append(model)
        r = plan[model].pop(0)
        if r == "ok":
            return ok()
        raise r

    outreach._last_gemini_call = 0.0
    with mock.patch("urllib.request.urlopen", side_effect=fake), mock.patch("time.sleep", side_effect=sleeps.append), \
         mock.patch("time.monotonic", side_effect=iter(range(0, 10_000, 100))):
        try:
            return outreach.gemini_generate("p", "k"), calls, [s for s in sleeps if s > 10]
        except Exception as e:  # noqa: BLE001
            return type(e).__name__, calls, [s for s in sleeps if s > 10]


def test_there_are_four_models_each_with_its_own_retry_delays():
    assert len(MODELS) >= 4 and len(outreach.GEMINI_RETRY_DELAYS) == len(MODELS)
    assert len(set(MODELS)) == len(MODELS)


def test_the_first_model_is_used_when_it_has_quota():
    result, calls, _ = run({MODELS[0]: ["ok"]})
    assert result == ("hello", MODELS[0]) and calls == [MODELS[0]]


def test_each_exhausted_model_hands_over_to_the_next_without_waiting():
    plan = {MODELS[0]: [daily_429()], MODELS[1]: [daily_429()], MODELS[2]: [daily_429()], MODELS[3]: ["ok"]}
    result, calls, long_waits = run(plan)
    assert result == ("hello", MODELS[3]) and calls == list(MODELS)
    assert long_waits == [], "a daily quota is never waited for, even when Google also names a short retry delay"


def test_a_per_minute_limit_is_still_waited_for_on_the_same_model():
    result, calls, long_waits = run({MODELS[0]: [per_minute_429("12s"), "ok"]})
    assert result == ("hello", MODELS[0]) and calls == [MODELS[0], MODELS[0]] and long_waits == [13.0]


def test_when_every_model_is_out_for_the_day_it_fails_fast_so_the_caller_can_fall_back():
    plan = {m: [daily_429()] for m in MODELS}
    result, calls, long_waits = run(plan)
    assert result == "HTTPError" and calls == list(MODELS) and long_waits == []


def test_a_model_that_no_longer_exists_is_skipped():
    gone = urllib.error.HTTPError("u", 404, "x", {}, io.BytesIO(b"{}"))
    result, calls, _ = run({MODELS[0]: [gone], MODELS[1]: ["ok"]})
    assert result == ("hello", MODELS[1]) and calls == [MODELS[0], MODELS[1]]


def test_the_email_writer_reports_which_model_wrote_it(monkeypatch):
    monkeypatch.setenv("GEMINI_API_KEY", "k")
    text = "SUBJECT: S\nBODY:\nHi,\n\nBody.\n\nBest,\nTeam"
    with mock.patch.object(outreach, "gemini_generate", return_value=(text, MODELS[2])):
        subject, body, writer = outreach.write_subject_body("p")
    assert writer == f"Gemini ({MODELS[2]})" and subject == "S"
    with mock.patch.object(outreach, "gemini_generate", return_value=(text, MODELS[0])):
        assert outreach.write_subject_body("p")[2] == "Gemini"
