"""SocialField's backlink publishing: the same pipeline with its own config, state, facts, wording and DEV account."""
import json
from pathlib import Path

import pytest
import yaml

from conftest import FakeWeb, one_a_day
from src.backlinks import content as C, pipeline, states as S
from src.backlinks.store import Store
from test_pipeline import ArticleLLM, FakeDevTo, Provider, SETTINGS, go, sandbox  # noqa: F401  (sandbox is an autouse fixture)

ROOT = Path(__file__).resolve().parent.parent
HOME = "https://www.socialfield.uk/"
SF_SITE = {"id": "social-field", "url": "https://socialfield.uk", "name": "SocialField", "restrict_to_uk": True,
           "priority_keywords": ["sports", "events"], "discovery_keywords": ["sports clubs"]}


@pytest.fixture
def sf():
    return one_a_day({**yaml.safe_load((ROOT / "config" / "backlinks-social-field.yaml").read_text(encoding="utf-8")), "live_publishing": True})


@pytest.fixture
def hyp(cfg):
    return one_a_day({**cfg, "live_publishing": True})


def test_every_site_config_is_found_and_they_are_independent(cfg):
    configs = pipeline.load_configs()
    assert [c["site_id"] for c in configs][0] == "hypnotreatment-uk" and {c["site_id"] for c in configs} == {"hypnotreatment-uk", "social-field", "genbooks-uk"}
    for field in ("state_dir", "log_dir"):
        assert len({c[field] for c in configs}) == len(configs), field
    assert not any(c.get("platforms") for c in configs), "DEV.to and WordPress.com were removed; guest posts use each website's own form"
    assert {c["guest_posts"]["author"] for c in configs} == {"Shahin Gurbanov", "SocialField team", "Genbooks team"}
    hosts = [set(c["verification"]["canonical_hosts"]) for c in configs]
    assert all(a.isdisjoint(b) for i, a in enumerate(hosts) for b in hosts[i + 1:])
    keys = [t["key"] for c in configs for t in c["topics"]]
    assert len(keys) == len(set(keys)), "topic keys must differ so job ids and notifications never collide"
    assert sum(c["quotas"]["tavily_searches_per_month"] for c in configs) <= 1000
    assert sum(c["quotas"]["firecrawl_scrapes_per_month"] for c in configs) <= 1000
    for c in configs:
        gen = c["topic_generation"]
        assert gen["enabled"] and gen["allowed_targets"] and c["limits"]["ramp_days"] == 0 and c["limits"]["max_publications_per_platform_per_day"] == 2
        hosts_of_site = c["verification"]["canonical_hosts"]
        assert all(any(h in t["url"] for h in hosts_of_site) for t in gen["allowed_targets"]), c["site_id"]


def test_socialfield_config_is_valid(sf):
    keys = [t["key"] for t in sf["topics"]]
    assert len(keys) == len(set(keys)) >= 6
    for t in sf["topics"]:
        assert t["target_url"].startswith("https://www.socialfield.uk/") and 1 <= len(t["tags"]) <= 4 and all(x.isalnum() for x in t["tags"]), t["key"]
        assert len(set(t["anchors"])) == len(t["anchors"]) >= 3 and t["title_hint"] and t["audience"] and t["angle"]
    assert sf["verification"]["canonical_hosts"][-1] == "www.socialfield.uk"
    text = " ".join(sf["facts"])
    assert "hypno" not in text.lower() and "medical" not in text.lower()
    assert "after each event" not in text, "payouts must not be given a timing the owner has said is wrong"
    assert not any(ch in text for ch in "–—")


def test_socialfield_wording_is_its_own_not_the_health_wording(sf):
    prompt = C.article_prompt(sf["topics"][0], sf["facts"], "an anchor", None, sf["brand_name"], sf["claims_rule"])
    assert "SocialField" in prompt and "Hypnotreatment" not in prompt and "medication" not in prompt and "doctor" not in prompt
    assert "legal, tax or insurance advice" in prompt
    review = C.review_prompt({"title": "T", "body_markdown": "B"}, sf["facts"], sf["brand_name"], sf["reviewer_role"], sf["review_checks"])
    assert review.startswith("You are a strict fact-checker") and "events publication" in review
    assert "medical" not in review.lower() and "Approved facts about SocialField" in review
    assert "hypnotherapy" not in review.lower()


def test_the_health_wording_is_unchanged_for_hypnotreatment(hyp):
    prompt = C.article_prompt(hyp["topics"][0], hyp["facts"], "an anchor", None, hyp["brand_name"], C.CLAIMS_RULE)
    assert "Hypnotreatment" in prompt and "change or stop medication" in prompt
    assert "health and wellbeing publication" in C.review_prompt({"title": "T", "body_markdown": "B"}, hyp["facts"])


def test_socialfield_end_to_end_with_its_own_disclosure_and_state(store, sf, tmp_path):
    web = FakeWeb()
    adapter = FakeDevTo(web)
    adapter._target = staticmethod(lambda content: __import__("re").search(r"\]\((https://www\.socialfield\.uk/[^)]*)\)", content["body_markdown"]).group(1))
    pipeline.run(store=store, cfg=sf, adapters={"devto": adapter}, llm=ArticleLLM(sf), fetch=web, engine="Firecrawl", provider=Provider(),
                 site=SF_SITE, settings=SETTINGS, write_logs=True)
    job = next(iter(store.jobs.values()))
    assert job["status"] == S.VERIFIED and job["topic_key"].startswith("sf-") and store.verified_on() == 1
    saved = json.loads(store.read_content(job["content_path"]))
    assert f"[SocialField]({HOME})" in saved["body_markdown"] and "not legal, tax or insurance advice" in saved["body_markdown"]
    assert "hypnotherapy" not in saved["body_markdown"].lower() and "medical advice" not in saved["body_markdown"]
    assert job["expected_urls"][-1] == HOME and job["verification"]["matched_link"]["resolved"].startswith("https://www.socialfield.uk/")
    reports = list((tmp_path / "logs" / "backlinks-social-field").glob("run-*.md"))
    assert len(reports) == 1 and "social-field" in reports[0].read_text(encoding="utf-8")
    dash = json.loads((tmp_path / "state" / "dashboard.json").read_text())["backlink_publishing"]
    assert set(dash) == {"social-field"} and dash["social-field"]["verified_today"] == 1


def test_a_link_to_the_other_brand_does_not_verify(store, sf):
    """The verifier is told each site's own hosts, so a Hypnotreatment link on a SocialField article is not a SocialField backlink."""
    from conftest import link, page
    from src.backlinks.verify import verify_backlink
    url = "https://dev.to/me/post-1"
    web = FakeWeb({url: page(link("https://www.hypnotreatment.uk/treatments/insomnia"))})
    r = verify_backlink(url, ["https://www.socialfield.uk/events"], sf["verification"]["canonical_hosts"], fetch=web, now_iso="t")
    assert r["outcome"] == "link_absent"


def test_two_sites_run_side_by_side_without_sharing_anything(tmp_path, clock, hyp, sf):
    from src import state as st
    web = FakeWeb()
    stores = {}
    for cfg, site, target_host in ((hyp, {**SF_SITE, "id": "hypnotreatment-uk", "name": "Hypnotreatment"}, "hypnotreatment.uk"), (sf, SF_SITE, "socialfield.uk")):
        s = Store(tmp_path / "state" / cfg["state_dir"], clock=clock)
        adapter = FakeDevTo(web)
        adapter._target = staticmethod(lambda content, h=target_host: __import__("re").search(rf"\]\((https://www\.{h}/[^)]*)\)", content["body_markdown"]).group(1))
        adapter.n = 100 if cfg is sf else 0  # different article addresses on the same platform
        pipeline.run(store=s, cfg=cfg, adapters={"devto": adapter}, llm=ArticleLLM(cfg), fetch=web, engine="Firecrawl", provider=Provider(),
                     site=site, settings=SETTINGS, write_logs=True)
        stores[cfg["site_id"]] = s
    assert stores["hypnotreatment-uk"].verified_on() == 1 and stores["social-field"].verified_on() == 1
    assert not set(stores["hypnotreatment-uk"].jobs) & set(stores["social-field"].jobs)
    dash = json.loads((tmp_path / "state" / "dashboard.json").read_text())["backlink_publishing"]
    assert set(dash) == {"hypnotreatment-uk", "social-field"} and all(v["verified_today"] == 1 for v in dash.values())
    assert (tmp_path / "state" / "backlinks" / "jobs.json").exists() and (tmp_path / "state" / "backlinks-social-field" / "jobs.json").exists()


def test_main_runs_every_enabled_site_and_one_failure_does_not_stop_the_rest(monkeypatch):
    seen = []
    monkeypatch.setattr(pipeline, "load_configs", lambda: [{"site_id": "a"}, {"site_id": "b"}, {"site_id": "c"}])

    def fake_run(cfg=None, **kw):
        seen.append(cfg["site_id"])
        if cfg["site_id"] == "b":
            raise RuntimeError("boom")

    monkeypatch.setattr(pipeline, "run", fake_run)
    monkeypatch.setattr(pipeline.settings_mod, "validate_config", lambda cfg: [])
    pipeline.main()
    assert sorted(seen) == ["a", "b", "c"], "every site runs (the starting site rotates daily, so the order changes)"
    issues = json.loads((pipeline.STATE_DIR / "dashboard.json").read_text())["publishing_issues"]
    assert [i["site_id"] for i in issues] == ["b"] and "boom" in issues[0]["problems"][0], "the failure is shown on the dashboard"


def test_a_disabled_site_config_is_skipped(store):
    assert pipeline.run(store=store, cfg={"enabled": False, "site_id": "x"}) == {"enabled": False}


def test_missing_socialfield_key_blocks_with_a_site_specific_notification(store, sf, tmp_path):
    web = FakeWeb()
    go(store, sf, FakeDevTo(web, available=False), ArticleLLM(sf), web)
    notes = json.loads((tmp_path / "wd" / "agent_state.json").read_text())["notifications"]
    assert notes[0]["run_id"].startswith("backlinks-blocked-social-field-devto-") and "social-field" in notes[0]["message"]


def test_the_verified_notification_names_the_right_site(store, sf, tmp_path):
    from src.backlinks import watchdog
    sf = {**sf, "notifications": {"verified": True}}   # the email is opt-in now; this checks its wording
    web = FakeWeb()
    adapter = FakeDevTo(web)
    adapter._target = staticmethod(lambda content: __import__("re").search(r"\]\((https://www\.socialfield\.uk/[^)]*)\)", content["body_markdown"]).group(1))
    pipeline.run(store=store, cfg=sf, adapters={"devto": adapter}, llm=ArticleLLM(sf), fetch=web, engine="Firecrawl", provider=Provider(),
                 site=SF_SITE, settings=SETTINGS, write_logs=False)
    notes = json.loads((tmp_path / "wd" / "agent_state.json").read_text())["notifications"]
    assert any("backlink to www.socialfield.uk is verified live" in n["message"] for n in notes)
