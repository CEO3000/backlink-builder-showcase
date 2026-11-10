"""Topics and articles are always new: no repeated topic, title, wording or exact link text, and a gentle start for a new account."""
import json

import pytest

from conftest import FakeWeb, one_a_day
from src.backlinks import content as C, pipeline, states as S, topics as T
from src.backlinks.llm import LLMError
from test_content import article, body, topic
from test_pipeline import ArticleLLM, FakeDevTo, Provider, SETTINGS, SITE, go, sandbox  # noqa: F401


TITLES = {1: "Planning a calm morning routine without extra gadgets", 2: "Handling a difficult conversation with steady breathing",
          3: "Choosing evening activities that help the mind unwind", 4: "Why small walks beat long gym sessions for busy people",
          5: "Keeping a simple worry diary that actually gets used", 6: "Building a quiet workspace on a tiny budget",
          7: "Learning to say no politely at work and at home"}


def good(i, **over):
    t = {"title_hint": TITLES[i], "audience": "people who want to learn more",
         "angle": "explains one practical thing readers can do this week", "target_url": "https://www.hypnotreatment.uk/treatments/anxiety",
         "anchors": [f"first link wording {i}", f"second link wording {i}", f"third link wording {i}"], "tags": ["Wellness", "Sleep"]}
    return t | over


@pytest.fixture
def gen_cfg(cfg):
    return {**cfg, "topic_generation": {"enabled": True, "when_unused_below": 4, "count": 3,
                                        "allowed_targets": [{"url": "https://www.hypnotreatment.uk/treatments/anxiety", "about": "anxiety"}]}}


class Ideas:
    def __init__(self, reply):
        self.reply, self.prompts = reply, []

    def generate(self, prompt):
        self.prompts.append(prompt)
        if isinstance(self.reply, Exception):
            raise self.reply
        return json.dumps(self.reply), "m"


def test_no_new_topics_are_asked_for_while_plenty_are_unused(store, gen_cfg):
    llm = Ideas([good(1)])
    out = T.with_generated_topics(store, gen_cfg, llm)
    assert llm.prompts == [] and out["topics"] == gen_cfg["topics"]


def test_when_few_are_unused_fresh_topics_are_added_and_remembered(store, gen_cfg):
    for t in gen_cfg["topics"][:-2]:
        store.add_job(f"k-{t['key']}", "devto", "dev.to", "u", "s", topic_key=t["key"])
    log = []
    out = T.with_generated_topics(store, gen_cfg, Ideas([good(1), good(2), good(3)]), log)
    assert len(out["topics"]) == len(gen_cfg["topics"]) + 3 and all(t.get("generated") for t in out["topics"][-3:])
    assert out["topics"][-1]["key"].startswith("gen-hypnotreatment-uk-") and "3 new topic(s) added" in log[0]
    again = T.with_generated_topics(store, gen_cfg, Ideas([]))                     # remembered across runs
    assert [t["key"] for t in again["topics"]] == [t["key"] for t in out["topics"]]


def test_repeats_and_bad_proposals_are_dropped_with_a_reason(store, gen_cfg):
    for t in gen_cfg["topics"][:-2]:
        store.add_job(f"k-{t['key']}", "devto", "dev.to", "u", "s", topic_key=t["key"])
    existing = gen_cfg["topics"][0]
    proposals = [good(1, title_hint=existing["title_hint"]),                                     # the same title
                 good(2, title_hint="Why your brain will not switch off at night, a different take"),  # closely rephrased
                 good(3, target_url="https://www.hypnotreatment.uk/treatments/depression"),     # a page not on the allowed list
                 good(4, anchors=existing["anchors"]),                                         # link texts already used
                 good(5, tags=[]), good(6, anchors=["only one"]), "not an object",
                 good(7)]                                                                      # the only good one
    log = []
    out = T.with_generated_topics(store, gen_cfg, Ideas(proposals), log)
    added = [t for t in out["topics"] if t.get("generated")]
    assert len(added) == 1 and added[0]["title_hint"] == TITLES[7]
    assert "7 dropped" in log[0]


def test_a_title_already_written_as_an_article_is_also_off_limits(store, gen_cfg):
    for t in gen_cfg["topics"][:-2]:
        store.add_job(f"k-{t['key']}", "devto", "dev.to", "u", "s", topic_key=t["key"])
    job, _ = store.add_job("written", "devto", "dev.to", "u", "s", title="Five calming evening habits for people who code", anchor_text="calming evening habits")
    llm = Ideas([good(1, title_hint="Five calming evening habits for developers who code", anchors=["calming evening habits", "x y", "z w"]), good(2)])
    out = T.with_generated_topics(store, gen_cfg, llm)
    assert [t["title_hint"] for t in out["topics"] if t.get("generated")] == [good(2)["title_hint"]]
    assert "Five calming evening habits for people who code" in llm.prompts[0], "the model is shown every past title"
    assert "never repeat, rephrase or lightly vary" in llm.prompts[0]


def test_a_failed_idea_request_does_not_stop_anything(store, gen_cfg):
    for t in gen_cfg["topics"][:-2]:
        store.add_job(f"k-{t['key']}", "devto", "dev.to", "u", "s", topic_key=t["key"])
    log = []
    out = T.with_generated_topics(store, gen_cfg, Ideas(LLMError("quota", "out of quota", True)), log)
    assert out["topics"] == gen_cfg["topics"] and "could not be generated" in log[0]
    assert T.with_generated_topics(store, gen_cfg, Ideas("not json at all"), []) ["topics"] == gen_cfg["topics"]


def test_generation_can_be_switched_off(store, cfg):
    llm = Ideas([good(1)])
    assert T.with_generated_topics(store, {**cfg, "topic_generation": {"enabled": False}}, llm) is not None and llm.prompts == []


# ------------------------------------------------------------------ the article itself must be new too
def checks(cfg, art, **kw):
    return C.local_checks(art, topic(cfg), "hypnotherapy for insomnia", cfg["banned_claims"], [], **kw)


def test_everyday_words_do_not_make_two_titles_repeats_but_shared_subject_words_do():
    a = "How to wind down with your family after a long day"
    assert C.title_overlap(a, "How to plan your family budget with a long view") < C.MAX_TITLE_OVERLAP, "only 'family' and 'long' are shared"
    assert C.title_overlap(a, "Winding down with the family after a long day at work") >= C.MAX_TITLE_OVERLAP
    assert C.title_overlap("Sleep tips for busy people", "Calm mornings without extra gadgets") == 0


def test_a_title_too_close_to_an_earlier_article_is_refused(cfg):
    art = article(cfg)
    assert any("too close to an earlier article" in p for p in checks(cfg, art, history_titles=["Why your brain will not switch off at night"]))
    assert not any("too close" in p for p in checks(cfg, art, history_titles=["Gardening tips for spring borders"]))


def test_the_same_exact_link_text_is_limited_to_two_articles(cfg):
    art = article(cfg)
    assert not any("already been used" in p for p in checks(cfg, art, used_anchors=["hypnotherapy for insomnia"]))
    assert any("already been used 2 times" in p for p in checks(cfg, art, used_anchors=["Hypnotherapy for insomnia", "hypnotherapy for insomnia"]))


def test_the_pipeline_passes_past_titles_so_a_repeat_is_rewritten(store, cfg):
    live = one_a_day({**cfg, "live_publishing": True, "topic_generation": {"enabled": False}})
    web = FakeWeb()
    store.add_job("old", "devto", "dev.to", "u", "s", title="Why Your Brain Will Not Switch Off at Night, and What Actually Helps")
    store.jobs["old"]["status"] = S.VERIFIED
    bodies, anchors, titles = pipeline._history(store, "devto")
    assert titles == []        # a job without saved content is not history ...
    store.jobs["old"]["content_path"] = store.save_content("old", json.dumps({"body_markdown": body()}))
    assert pipeline._history(store, "devto")[2] == ["Why Your Brain Will Not Switch Off at Night, and What Actually Helps"]


# ------------------------------------------------------------------ a new account starts gently
def test_the_ramp_holds_a_new_account_to_one_article_a_day_for_a_week(store):
    limits = {"max_publications_per_platform_per_day": 2, "ramp_days": 7}
    assert pipeline.daily_cap(store, "devto", limits) == 1, "nothing published yet: the very first day counts as day one"


def test_ramp_dates(store, clock):
    limits = {"max_publications_per_platform_per_day": 2, "ramp_days": 7}
    job, _ = store.add_job("a", "devto", "dev.to", "u", "s")
    job["submitted_at"] = clock().isoformat()
    for days, expected in ((0, 1), (3, 1), (6, 1), (7, 2), (20, 2)):
        clock.t = clock.t.replace(day=27) + __import__("datetime").timedelta(days=days)
        assert pipeline.daily_cap(store, "devto", limits) == expected, days
    assert pipeline.daily_cap(store, "devto", {"max_publications_per_platform_per_day": 2}) == 2, "no ramp configured"
    assert pipeline.daily_cap(store, "devto", {"max_publications_per_platform_per_day": 1, "ramp_days": 7}) == 1


def test_a_rejected_first_attempt_does_not_start_the_ramp_clock(store, clock):
    limits = {"max_publications_per_platform_per_day": 2, "ramp_days": 7}
    job, _ = store.add_job("a", "devto", "dev.to", "u", "s")
    job["submitted_at"] = clock().isoformat()
    job["status"] = S.REJECTED
    clock.advance(days=3)
    assert pipeline.daily_cap(store, "devto", limits) == 1, "nothing was really published, so the account is still on day one"


def test_with_the_ramp_a_new_site_publishes_one_a_day_then_two(store, cfg, clock):
    live = {**cfg, "live_publishing": True, "topic_generation": {"enabled": False}, "limits": {**cfg["limits"], "max_publications_per_platform_per_day": 2, "ramp_days": 3, "max_new_jobs_per_day": 4}}
    web = FakeWeb()
    adapter, llm = FakeDevTo(web), ArticleLLM(live)
    per_day = []
    for _ in range(5):
        before = len(adapter.articles)
        go(store, live, adapter, llm, web)
        per_day.append(len(adapter.articles) - before)
        clock.advance(days=1)
    assert per_day[:3] == [1, 1, 1] and per_day[3] == 2
