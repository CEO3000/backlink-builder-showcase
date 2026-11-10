"""The dashboard's controls (articles per day, live or drafts, on or off), config validation, and the data the dashboard shows."""
import json
from pathlib import Path

import pytest
import yaml

from conftest import FakeWeb
from src.backlinks import pipeline, settings as S_, states as S
from src.backlinks.llm import LLMError
from test_pipeline import ArticleLLM, FakeDevTo, Provider, SETTINGS, sandbox  # noqa: F401 (autouse fixture)

ROOT = Path(__file__).resolve().parent.parent


def shipped(name="backlinks.yaml"):
    return yaml.safe_load((ROOT / "config" / name).read_text(encoding="utf-8"))


# ------------------------------------------------------------------ applying the owner's choices
def test_articles_per_day_sets_the_cap_the_target_and_room_for_a_rewrite(cfg):
    out = S_.apply_settings(cfg, {"articles_per_day": 3})
    assert out["limits"]["max_publications_per_platform_per_day"] == 3 and out["daily_target_verified"] == 3
    assert out["limits"]["max_new_jobs_per_day"] >= 5 and out["limits"]["ramp_days"] == 0
    assert cfg["limits"]["max_publications_per_platform_per_day"] == 2, "the original config is not modified"


@pytest.mark.parametrize("given,expected", [(0, 1), (-4, 1), (1, 1), (10, 10), (99, 10), (2.7, 2), (5, 5)])
def test_articles_per_day_is_kept_between_one_and_ten(cfg, given, expected):
    assert S_.apply_settings(cfg, {"articles_per_day": given})["limits"]["max_publications_per_platform_per_day"] == expected


@pytest.mark.parametrize("bad", ["2", None, True, [2], {"n": 2}])
def test_a_badly_typed_number_is_ignored_not_trusted(cfg, bad):
    assert S_.apply_settings(cfg, {"articles_per_day": bad})["limits"] == cfg["limits"]


def test_mode_and_enabled(cfg):
    assert S_.apply_settings(cfg, {"mode": "draft"})["live_publishing"] is False
    assert S_.apply_settings({**cfg, "live_publishing": False}, {"mode": "live"})["live_publishing"] is True
    assert S_.apply_settings(cfg, {"mode": "sideways"})["live_publishing"] == cfg["live_publishing"]
    assert S_.apply_settings(cfg, {"enabled": False})["enabled"] is False
    assert S_.apply_settings(cfg, {"enabled": "no"})["enabled"] == cfg["enabled"]


def test_nothing_to_apply_returns_the_config_unchanged(cfg):
    assert S_.apply_settings(cfg, None) is cfg and S_.apply_settings(cfg, "x") is cfg
    assert S_.apply_settings(cfg, {"unknown": 1, "colour": "red"}) == cfg


def test_a_missing_empty_or_broken_settings_file_changes_nothing(tmp_path):
    p = tmp_path / "s.json"
    assert S_.load_settings_file(p) == {}
    for text in ("", "not json", "[]", '{"sites": 5}', '{"sites": {"a": "not a dict", "b": {"enabled": false}}}'):
        p.write_text(text)
        assert S_.load_settings_file(p) in ({}, {"b": {"enabled": False}}), text
    p.write_text('{"sites": {"b": {"enabled": false}}}')
    assert S_.load_settings_file(p) == {"b": {"enabled": False}}


def test_the_shipped_settings_file_matches_the_three_sites_and_the_owners_two_a_day():
    sites = S_.load_settings_file()
    assert set(sites) == {"hypnotreatment-uk", "social-field", "genbooks-uk"}
    # 2 a day (now: guest-post submissions per website), all three active from 2026-11-02 after the test runs of 2026-11-01
    assert all(v == {"articles_per_day": 2, "mode": "live", "enabled": True} for v in sites.values())


def test_load_configs_lays_the_settings_over_each_site(tmp_path, monkeypatch):
    (tmp_path / "backlinks.yaml").write_text(yaml.safe_dump({**shipped(), "site_id": "one"}))
    (tmp_path / "backlinks-two.yaml").write_text(yaml.safe_dump({**shipped(), "site_id": "two"}))
    (tmp_path / "publishing_settings.json").write_text(json.dumps({"sites": {"one": {"articles_per_day": 4}, "two": {"enabled": False, "mode": "draft"}}}))
    monkeypatch.setattr(pipeline, "CONFIG_DIR", tmp_path)
    monkeypatch.setattr(S_, "SETTINGS_PATH", tmp_path / "publishing_settings.json")
    one, two = pipeline.load_configs()
    assert one["limits"]["max_publications_per_platform_per_day"] == 4 and one["live_publishing"] is True
    assert two["enabled"] is False and two["live_publishing"] is False and two["limits"]["max_publications_per_platform_per_day"] == 2


@pytest.mark.parametrize("n", [1, 2, 3, 5])
def test_the_chosen_number_is_really_what_a_run_publishes(store, cfg, n):
    chosen = S_.apply_settings({**cfg, "topic_generation": {"enabled": False}}, {"articles_per_day": n, "mode": "live"})
    web = FakeWeb()
    adapter = FakeDevTo(web)
    result = pipeline.run(store=store, cfg=chosen, adapters={"devto": adapter}, llm=ArticleLLM(chosen), fetch=web, engine="Firecrawl", provider=Provider(),
                          site={"id": "x", "url": "https://x.test", "name": "X"}, settings=SETTINGS, write_logs=False)
    assert len(adapter.articles) == n and store.verified_on() == n
    assert f"{n} verified live backlink(s) today, target {n}" in result["report"]


def test_draft_mode_from_the_dashboard_saves_private_drafts_only(store, cfg):
    chosen = S_.apply_settings(cfg, {"articles_per_day": 2, "mode": "draft"})
    web = FakeWeb()
    adapter = FakeDevTo(web)
    pipeline.run(store=store, cfg=chosen, adapters={"devto": adapter}, llm=ArticleLLM(chosen), fetch=web, engine="Firecrawl", provider=Provider(),
                 site={"id": "x", "url": "https://x.test", "name": "X"}, settings=SETTINGS, write_logs=False)
    assert adapter.live_calls and not any(adapter.live_calls) and store.verified_on() == 0


def test_a_site_paused_from_the_dashboard_does_nothing_and_shows_as_paused(store, cfg, tmp_path):
    chosen = S_.apply_settings(cfg, {"enabled": False})
    llm = ArticleLLM(chosen)
    out = pipeline.run(store=store, cfg=chosen, adapters={"devto": FakeDevTo(FakeWeb())}, llm=llm, fetch=FakeWeb(), engine="Firecrawl", provider=Provider(),
                       site={"id": "x", "url": "https://x.test", "name": "X"}, settings=SETTINGS, write_logs=True)
    assert out == {"enabled": False} and llm.calls == 0 and not store.jobs
    entry = json.loads((tmp_path / "state" / "dashboard.json").read_text())["backlink_publishing"][chosen["site_id"]]
    assert entry["settings"]["enabled"] is False and entry["name"] == "Hypnotreatment"


# ------------------------------------------------------------------ validating a whole site config
@pytest.mark.parametrize("name", ["backlinks.yaml", "backlinks-social-field.yaml", "backlinks-genbooks.yaml"])
def test_the_shipped_configs_are_valid(name):
    assert S_.validate_config(shipped(name)) == []


def test_every_kind_of_config_problem_is_found_in_plain_words():
    good = shipped()
    assert S_.validate_config(None) and S_.validate_config({})
    for key in ("facts", "limits", "verification", "topic_generation", "site_id"):
        assert any(key in p for p in S_.validate_config({k: v for k, v in good.items() if k != key})), key
    cases = {
        "site_id": ("Bad Id!", "site_id must be"), "state_dir": ("../escape", "state_dir must be a simple folder"),
        "daily_target_verified": (0, "daily_target_verified"), "facts": (["too short", "x"], "at least three statements"),
        "topics": ("nope", "topics must be a list")}
    for key, (value, expect) in cases.items():
        assert any(expect in p for p in S_.validate_config({**good, key: value})), key
    assert any("canonical_hosts" in p for p in S_.validate_config({**good, "verification": {"canonical_hosts": []}}))
    assert any("api_key_env" in p for p in S_.validate_config({**good, "platforms": {"devto": {"enabled": True}}}))
    off_site = {**good, "topic_generation": {"enabled": True, "allowed_targets": [{"url": "https://evil.example.com/x", "about": "x"}]}}
    assert any("must be a page on the site" in p for p in S_.validate_config(off_site))
    nothing = {**good, "topics": [], "topic_generation": {"enabled": False}}
    assert any("nothing to write about" in p for p in S_.validate_config(nothing))
    assert any("quotas.gemini_calls_per_day" in p for p in S_.validate_config({**good, "quotas": {}}))


def test_a_site_with_no_hand_written_topics_is_valid_when_the_generator_is_on():
    cfg = {**shipped(), "topics": []}
    assert S_.validate_config(cfg) == []


# ------------------------------------------------------------------ what the dashboard is shown
def run_entry(store, cfg, adapter, llm, tmp_path, web=None):
    pipeline.run(store=store, cfg=cfg, adapters={"devto": adapter}, llm=llm, fetch=web or FakeWeb(), engine="Firecrawl", provider=Provider(),
                 site={"id": "x", "url": "https://x.test", "name": "X"}, settings=SETTINGS, write_logs=True)
    return json.loads((tmp_path / "state" / "dashboard.json").read_text())["backlink_publishing"][cfg["site_id"]]


def test_the_dashboard_entry_shows_settings_progress_history_and_topics(store, cfg, tmp_path):
    chosen = {**S_.apply_settings(cfg, {"articles_per_day": 2}), "topic_generation": {"enabled": False}}
    web = FakeWeb()
    e = run_entry(store, chosen, FakeDevTo(web), ArticleLLM(chosen), tmp_path, web)
    assert e["name"] == "Hypnotreatment" and e["url"] == "https://www.hypnotreatment.uk/"
    assert e["settings"] == {"articles_per_day": 2, "mode": "live", "enabled": True}
    assert e["target_per_day"] == 2 and e["verified_today"] == 2 and e["published_today"] == 2 and e["verified_live_total"] == 2
    assert e["topics_unused"] == len(chosen["topics"]) - 2 and e["errors"] == [] and e["blocked"] == []
    assert e["history"] == [{"date": store.today(), "published": 2, "verified": 2, "drafts": 0, "failures": 0}]
    assert len(e["live_links"]) == 2 and all(l["follow"] and l["anchor"] and l["url"].startswith("https://dev.to/") for l in e["live_links"])
    assert all(l["platform"] and l["target"].startswith("https://") and "hypnotreatment.uk" in l["target"] for l in e["live_links"])   # where it was published and which page it points to


def test_a_missing_key_is_shown_as_blocked(store, cfg, tmp_path):
    e = run_entry(store, cfg, FakeDevTo(FakeWeb(), available=False), ArticleLLM(cfg), tmp_path)
    assert len(e["blocked"]) == 1 and "DEVTO_API_KEY" in e["blocked"][0] and e["verified_today"] == 0


def test_gemini_trouble_shows_up_as_an_error_with_its_category_and_message(store, cfg, tmp_path):
    chosen = {**cfg, "topic_generation": {"enabled": False}}
    e = run_entry(store, chosen, FakeDevTo(FakeWeb()), ArticleLLM(chosen, fail=LLMError("quota", "Gemini quota exhausted (HTTP 429): PerDay", True)), tmp_path)
    assert e["errors"] and e["errors"][0]["status"] == S.RETRY_REQUIRED and e["errors"][0]["category"] == "quota"
    assert "Gemini quota exhausted" in e["errors"][0]["message"] and e["errors"][0]["title"]
    assert e["failures_today"].get("quota") == 1 and any("Stopping for today" in n for n in e["notes"])


def test_routine_candidate_page_failures_are_not_reported_as_errors(store, cfg, tmp_path):
    job, _ = store.add_job("k", "web", "example.org", "https://example.org/", "tavily: q")
    store.transition(job, S.REJECTED, "no way to submit content was found on the page", last_error="x", error_category="unreachable")
    chosen = {**cfg, "topic_generation": {"enabled": False}}
    web = FakeWeb()
    assert run_entry(store, chosen, FakeDevTo(web), ArticleLLM(chosen), tmp_path, web)["errors"] == []


