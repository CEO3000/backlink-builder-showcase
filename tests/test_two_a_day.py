"""The owner's standing rule: each site publishes two articles every day, from the shipped settings (no test overrides)."""
import json
import random
import re
from pathlib import Path

import pytest
import yaml

from conftest import FakeWeb
from src.backlinks import pipeline, states as S
from src.backlinks.store import Store
from test_pipeline import ArticleLLM, FakeDevTo, Provider, SETTINGS, VOCAB, sandbox  # noqa: F401 (autouse fixture)

ROOT = Path(__file__).resolve().parent.parent
SITES = {
    "backlinks.yaml": ("hypnotreatment-uk", "hypnotreatment.uk"),
    "backlinks-social-field.yaml": ("social-field", "socialfield.uk"),
    "backlinks-genbooks.yaml": ("genbooks-uk", "genbooks.uk"),
}


def shipped(name):
    return yaml.safe_load((ROOT / "config" / name).read_text(encoding="utf-8"))


def adapter_for(web, host):
    a = FakeDevTo(web)
    a._target = staticmethod(lambda content, h=host: re.search(rf"\]\((https://(?:www\.)?{re.escape(h)}/[^)]*)\)", content["body_markdown"]).group(1))
    return a


def run(store, cfg, adapter, llm, web):
    site = {"id": cfg["site_id"], "url": "https://x.test", "name": cfg["brand_name"], "restrict_to_uk": False, "priority_keywords": ["x"], "discovery_keywords": ["x"]}
    return pipeline.run(store=store, cfg=cfg, adapters={"devto": adapter}, llm=llm, fetch=web, engine="Firecrawl", provider=Provider(),
                        site=site, settings=SETTINGS, write_logs=False)


@pytest.mark.parametrize("name", list(SITES))
def test_the_shipped_settings_ask_for_two_a_day_from_the_start(name):
    cfg = shipped(name)
    assert cfg["live_publishing"] is True and cfg["daily_target_verified"] == 2
    assert cfg["limits"]["max_publications_per_platform_per_day"] == 2 and cfg["limits"]["ramp_days"] == 0
    assert cfg["limits"]["max_new_jobs_per_day"] >= 3, "room for a rewrite or a retry on top of the two"


@pytest.mark.parametrize("name", list(SITES))
def test_one_run_publishes_and_verifies_two_articles_and_reports_the_target_reached(name, store):
    cfg = shipped(name)
    web = FakeWeb()
    llm = ArticleLLM(cfg)
    result = run(store, cfg, adapter_for(web, SITES[name][1]), llm, web)
    jobs = [j for j in store.jobs.values() if j.get("topic_key")]
    assert len(jobs) == 2 and all(j["status"] == S.VERIFIED for j in jobs) and store.verified_on() == 2
    assert len({j["topic_key"] for j in jobs}) == 2 and len({j["title"] for j in jobs}) == 2
    assert "2 verified live backlink(s) today, target 2 (TARGET REACHED)" in result["report"]
    assert llm.calls <= 6, "two articles use about two Gemini calls each (write and fact-check)"


@pytest.mark.parametrize("name", list(SITES))
def test_a_third_article_is_never_published_the_same_day(name, store):
    cfg = shipped(name)
    web = FakeWeb()
    adapter, llm = adapter_for(web, SITES[name][1]), ArticleLLM(cfg)
    run(store, cfg, adapter, llm, web)
    run(store, cfg, adapter, llm, web)
    assert len([j for j in store.jobs.values() if j.get("topic_key")]) == 2 and adapter.n == 2


@pytest.mark.parametrize("name", list(SITES))
def test_every_day_brings_two_more_and_never_a_repeat(name, store, clock):
    cfg = {**shipped(name), "topic_generation": {"enabled": False}}      # the hand-written topics only: 8 topics = 4 days
    web = FakeWeb()
    adapter, llm = adapter_for(web, SITES[name][1]), ArticleLLM(cfg)
    per_day = []
    for _ in range(4):
        before = len(adapter.articles)
        run(store, cfg, adapter, llm, web)
        per_day.append(len(adapter.articles) - before)
        clock.advance(days=1)
    assert per_day == [2, 2, 2, 2]
    topics = [j["topic_key"] for j in store.jobs.values()]
    titles = [j["title"] for j in store.jobs.values()]
    assert len(topics) == len(set(topics)) == 8 and len(titles) == len(set(titles))


def ideas(n, seed):
    """n different topic proposals made of different random words, like varied real ideas: no repeated title words, no reused link text."""
    rng = random.Random(seed)
    out = []
    for _ in range(n):
        title_words = rng.sample(VOCAB, 5)
        anchors = [" ".join(rng.sample(VOCAB, 3)) for _ in range(3)]
        out.append({"title_hint": " ".join(title_words).capitalize() + " explained simply", "audience": "readers who want practical help",
                    "angle": f"shows one clear way to use {title_words[0]} and {title_words[1]} in daily life",
                    "target_url": "https://www.hypnotreatment.uk/treatments/anxiety", "anchors": anchors, "tags": ["wellness", "habits"]})
    return out


class ArticleAndIdeasLLM(ArticleLLM):
    """Writes articles as usual and answers the 'suggest new topics' request with fresh proposals."""

    def __init__(self, cfg):
        super().__init__(cfg)
        self.requests = 0

    def generate(self, prompt):
        if prompt.startswith("Suggest "):
            self.requests += 1
            proposals = ideas(6, self.requests)
            # the fake must also be able to write an article for each topic it has just proposed
            self.cfg = {**self.cfg, "topics": [*self.cfg["topics"], *({**p, "key": re.sub(r"\W+", "-", p["title_hint"].lower())} for p in proposals)]}
            return json.dumps(proposals), "m"
        return super().generate(prompt)


def test_the_topics_never_run_out_two_a_day_for_two_weeks(store, clock):
    cfg = shipped("backlinks.yaml")                                     # topic generation ON, as shipped
    web = FakeWeb()
    adapter, llm = adapter_for(web, "hypnotreatment.uk"), ArticleAndIdeasLLM(cfg)
    for _ in range(14):
        run(store, cfg, adapter, llm, web)
        clock.advance(days=1)
    jobs = [j for j in store.jobs.values() if j.get("topic_key")]
    assert len(jobs) == 28 and all(j["status"] == S.VERIFIED for j in jobs), "two verified articles on each of 14 days"
    assert len({j["topic_key"] for j in jobs}) == 28 and len({j["title"] for j in jobs}) == 28
    assert sum(1 for j in jobs if j["topic_key"].startswith("gen-")) >= 28 - len(cfg["topics"]) and llm.requests >= 3
    anchors = [j["anchor_text"].lower() for j in jobs]
    assert max(anchors.count(a) for a in set(anchors)) <= 2, "no exact link text is used more than twice"
    saved = json.loads((store.root / "topics.json").read_text(encoding="utf-8"))
    assert len(saved) >= 28 - len(cfg["topics"]) and all(t["generated"] for t in saved)


def test_if_the_idea_request_keeps_failing_the_hand_written_topics_are_still_used_and_the_shortfall_is_reported(store, clock):
    cfg = shipped("backlinks.yaml")
    web = FakeWeb()
    adapter, llm = adapter_for(web, "hypnotreatment.uk"), ArticleLLM(cfg)      # answers "[]" to every idea request
    reports = [run(store, cfg, adapter, llm, web)["report"] for _ in range(len(cfg["topics"]) // 2 + 1) if not clock.advance(days=1)]
    assert len(adapter.articles) == len(cfg["topics"]), "all the hand-written topics are used, no more, no repeats"
    assert "No unused topics left" in reports[-1]
