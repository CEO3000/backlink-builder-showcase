import json
from datetime import datetime, timezone

import pytest

from conftest import FakeWeb, link, one_a_day, page
from src import discovery, state as st
from src.backlinks import pipeline, states as S, watchdog
from src.backlinks.adapters.base import Adapter, SubmissionResult
from src.backlinks.verify import FetchResult
from test_content import OK_REVIEW, article


@pytest.fixture(autouse=True)
def sandbox(tmp_path, monkeypatch):
    """Nothing in these tests may touch the real repository state, logs or watchdog files."""
    monkeypatch.setattr(st, "STATE_DIR", tmp_path / "state")
    monkeypatch.setattr(pipeline, "STATE_DIR", tmp_path / "state")
    monkeypatch.setattr(pipeline, "LOGS_DIR", tmp_path / "logs")
    monkeypatch.setattr(watchdog, "WATCHDOG_STATE", tmp_path / "wd" / "agent_state.json")


class FakeDevTo(Adapter):
    name, domain, mechanism, docs_url = "devto", "dev.to", "authorised_api", "https://developers.forem.com/api/v1"

    def __init__(self, web, available=True):
        self.web, self._available, self.live_calls, self.n = web, available, [], 0
        self.articles = {}

    def available(self):
        return (self._available, "ok" if self._available else "the DEVTO_API_KEY secret is not set")

    def submit(self, content, live):
        self.n += 1
        url = f"https://dev.to/me/post-{self.n}"
        self.live_calls.append(live)
        self.articles[content["title"]] = (url, live)
        if live:  # the platform renders the article, with the link we asked for, on a public page
            self.web.pages[url] = page(link(self._target(content)), title=content["title"])
        return SubmissionResult("published" if live else "draft", url=url, reference=str(self.n))

    @staticmethod
    def _target(content):
        import re
        m = re.search(r"\]\((https://www\.hypnotreatment\.uk/[^)]*)\)", content["body_markdown"])
        return m.group(1)

    def find_existing(self, title):
        got = self.articles.get(title)
        return SubmissionResult("published" if got and got[1] else "draft", url=got[0]) if got else None


VOCAB = ("calm night light screen quiet habit breath slow gentle morning evening water walk rest focus mind body shoulder jaw hands music "
         "window blanket tea book stretch pause routine phone desk chair lamp garden street train coffee lunch email deadline meeting weekend "
         "holiday friend family neighbour dog cat river hill stone paper pencil kitchen bedroom sunset coat rain wind cloud field bridge ladder "
         "clock mirror candle pillow sheet curtain doorway staircase corridor market harbour meadow orchard puzzle ribbon basket lantern").split()


def unique_body(seed: str, anchor: str, url: str) -> str:
    """A valid article whose wording is different for every seed, so the originality check treats it as a fresh piece."""
    import random
    rng = random.Random(seed)
    sentence = lambda: " ".join(rng.choice(VOCAB) for _ in range(rng.randint(8, 15))).capitalize() + "."
    paras = []
    for i in range(14):
        text = " ".join(sentence() for _ in range(4))
        if i == 6:
            text += f" Some people also read about [{anchor}]({url}) when their sleep stays poor."
        if i in (0, 4, 8, 12):
            paras.append("## " + " ".join(rng.choice(VOCAB) for _ in range(3)).capitalize())
        paras.append(text)
    return "\n\n".join(paras)


class ArticleLLM:
    """Returns a valid article for each topic asked for, then an OK fact-check, like Gemini on a good day."""

    def __init__(self, cfg, fail=None):
        self.cfg, self.calls, self.fail, self.n = cfg, 0, fail, 0

    def generate(self, prompt):
        self.calls += 1
        if self.fail:
            raise self.fail
        if prompt.startswith("You are a strict fact-checker"):
            return OK_REVIEW, "m"
        if prompt.startswith("Suggest "):
            return "[]", "m"
        if prompt.startswith("You check whether a website accepts articles"):   # these fixture pages never say they accept outside articles
            return json.dumps({"accepts_outside_articles": False, "accepts_quote": "", "links_allowed": False, "links_quote": "", "paid": False}), "m"
        if prompt.startswith("Judge this web page"):
            return json.dumps({"relevance": 8, "quality": 7, "topic": "wellbeing", "audience": "adults", "reason": "fits"}), "m"
        topic = next(t for t in self.cfg["topics"] if t["title_hint"] in prompt)
        anchor = next(a for a in topic["anchors"] if f"[{a}]" in prompt)
        self.n += 1
        body = unique_body(f"{topic['key']}-{self.n}", anchor, topic["target_url"])
        return f"TITLE: {topic['title_hint'][:100]}\nDESCRIPTION: A practical guide written for this audience.\nBODY:\n{body}", "gemini-test"


class Provider:
    def __init__(self, items=()):
        self.items, self.calls = list(items), 0

    def discover(self, **kw):
        self.calls += 1
        return list(self.items), 4


SITE = {"id": "hypnotreatment-uk", "url": "https://hypnotreatment.uk", "name": "Hypnotreatment", "restrict_to_uk": True,
        "priority_keywords": ["anxiety", "sleep"], "discovery_keywords": ["anxiety UK"]}
SETTINGS = {"discovery": {"query_templates": ['"write for us" {keyword}']}}


def go(store, cfg, adapter, llm, web, engine="Firecrawl", provider=None, **kw):
    return pipeline.run(store=store, cfg=cfg, adapters={"devto": adapter}, llm=llm, fetch=web, engine=engine, provider=provider or Provider(),
                        site=SITE, settings=SETTINGS, write_logs=False, **kw)


@pytest.fixture
def live(cfg):
    return one_a_day({**cfg, "live_publishing": True})


def test_end_to_end_live_publishing_counts_only_the_verified_link(store, live):
    web = FakeWeb()
    adapter, llm = FakeDevTo(web), ArticleLLM(live)
    result = go(store, live, adapter, llm, web)
    jobs = list(store.jobs.values())
    assert len(jobs) == 1 and jobs[0]["status"] == S.VERIFIED and store.verified_on() == 1
    j = jobs[0]
    assert j["publication_url"] == "https://dev.to/me/post-1" and j["link_attributes"]["follow"] is True
    assert j["verification"]["outcome"] == "verified" and j["verification"]["matched_link"]["resolved"].startswith("https://www.hypnotreatment.uk/")
    saved = json.loads(store.read_content(j["content_path"]))
    assert "Disclosure: I write on behalf of" in saved["body_markdown"] and saved["tags"]
    assert [h["to"] for h in j["history"]] == ["DISCOVERED", "QUALIFIED", "CONTENT_READY", "SUBMISSION_IN_PROGRESS", "PUBLISHED", "VERIFIED"]
    assert "1 verified live backlink(s) today, target 2 (target not reached)" in result["report"]


def test_the_report_says_the_target_was_missed_rather_than_inventing_a_second_link(store, live):
    web = FakeWeb()
    go(store, live, FakeDevTo(web), ArticleLLM(live), web)
    assert store.verified_on() == 1 and store.published_on("devto") == 1       # the per-platform cap, not a failure


def test_running_again_the_same_day_does_not_publish_or_duplicate(store, live):
    web = FakeWeb()
    adapter, llm = FakeDevTo(web), ArticleLLM(live)
    go(store, live, adapter, llm, web)
    go(store, live, adapter, llm, web)
    assert len(store.jobs) == 1 and adapter.n == 1


def test_the_next_day_publishes_a_different_topic_and_never_repeats_one(store, live, clock):
    web = FakeWeb()
    adapter, llm = FakeDevTo(web), ArticleLLM(live)
    for _ in range(3):
        go(store, live, adapter, llm, web)
        clock.advance(days=1)
    topics = [j["topic_key"] for j in store.jobs.values()]
    assert len(topics) == len(set(topics)) == 3 and store.in_states(S.VERIFIED).__len__() == 3
    anchors = [j["anchor_text"] for j in store.jobs.values()]
    assert len(anchors) == 3


def test_topics_run_out_gracefully(store, live, clock):
    live = {**live, "topic_generation": {"enabled": False}}
    web = FakeWeb()
    adapter, llm = FakeDevTo(web), ArticleLLM(live)
    for _ in range(len(live["topics"]) + 1):
        result = go(store, live, adapter, llm, web)
        clock.advance(days=1)
    assert len(store.jobs) == len(live["topics"]) and "No unused topics left" in result["report"]


def test_the_shipped_configs_publish_automatically(cfg):
    import yaml
    from pathlib import Path
    sf = yaml.safe_load((Path(__file__).resolve().parent.parent / "config" / "backlinks-social-field.yaml").read_text(encoding="utf-8"))
    assert cfg["live_publishing"] is True and sf["live_publishing"] is True


def test_draft_mode_saves_private_drafts_and_stops_at_the_pending_limit(store, cfg, clock):
    cfg = one_a_day({**cfg, "live_publishing": False})                      # the owner can switch back to private drafts
    web = FakeWeb()
    adapter, llm = FakeDevTo(web), ArticleLLM(cfg)
    for _ in range(5):
        result = go(store, cfg, adapter, llm, web)
        clock.advance(days=1)
    assert adapter.live_calls == [False] * cfg["limits"]["max_pending_approval"]
    assert len(store.in_states(S.PENDING_APPROVAL)) == 3 and store.verified_on() == 0
    assert "drafts already await your approval" in result["report"]
    assert not any(url for url in (j.get("publication_url") for j in store.jobs.values()))


def test_owner_publishing_a_draft_is_picked_up_next_run_and_verified(store, cfg, clock):
    cfg = one_a_day({**cfg, "live_publishing": False})
    web = FakeWeb()
    adapter, llm = FakeDevTo(web), ArticleLLM(cfg)
    go(store, cfg, adapter, llm, web)
    job = next(iter(store.jobs.values()))
    url, _ = adapter.articles[job["title"]]
    adapter.articles[job["title"]] = (url, True)                                  # the owner pressed Publish
    web.pages[url] = page(link(FakeDevTo._target(json.loads(store.read_content(job["content_path"])))), title=job["title"])
    clock.advance(days=1)
    go(store, cfg, adapter, llm, web)
    assert job["status"] == S.VERIFIED and store.verified_on() == 1


def test_missing_credential_generates_nothing_and_tells_the_owner(store, live, tmp_path):
    web = FakeWeb()
    llm = ArticleLLM(live)
    result = go(store, live, FakeDevTo(web, available=False), llm, web)
    assert llm.calls == 0 and not store.jobs and "BLOCKED: devto cannot be used" in result["report"]
    notes = json.loads((tmp_path / "wd" / "agent_state.json").read_text())["notifications"]
    assert notes and "DEVTO_API_KEY" in notes[0]["message"]


def test_gemini_quota_failure_waits_without_using_up_retries_or_burning_topics(store, live, clock):
    from src.backlinks.llm import LLMError
    web = FakeWeb()
    adapter = FakeDevTo(web)
    result = go(store, live, adapter, ArticleLLM(live, fail=LLMError("quota", "out of quota", True)), web)
    job = next(iter(store.jobs.values()))
    assert len(store.jobs) == 1, "it stops for the day instead of failing every topic"
    assert job["status"] == S.RETRY_REQUIRED and job["error_category"] == "quota" and job["retry_count"] == 0
    assert adapter.n == 0 and store.verified_on() == 0 and "Stopping for today" in result["report"]
    clock.advance(hours=2)
    go(store, live, adapter, ArticleLLM(live), web)
    assert job["status"] == S.VERIFIED and job["retry_count"] == 0 and len(store.jobs) == 1


def test_a_refused_gemini_key_blocks_and_notifies_without_rejecting_the_topic(store, live, tmp_path):
    from src.backlinks.llm import LLMError
    web = FakeWeb()
    go(store, live, FakeDevTo(web), ArticleLLM(live, fail=LLMError("auth", "Gemini refused the key (HTTP 403)", False)), web)
    job = next(iter(store.jobs.values()))
    assert len(store.jobs) == 1 and job["status"] == S.BLOCKED_CREDENTIALS and "Gemini" in job["last_error"]
    notes = json.loads((tmp_path / "wd" / "agent_state.json").read_text())["notifications"]
    assert any("blocked" in n["message"] for n in notes)


def test_a_poor_draft_uses_the_jobs_bounded_retries(store, live, clock):
    class Poor(ArticleLLM):
        def generate(self, prompt):
            if prompt.startswith("Write an original"):
                self.calls += 1
                return "TITLE: Short\nDESCRIPTION: x\nBODY:\nNo.", "m"
            return super().generate(prompt)

    web = FakeWeb()
    go(store, live, FakeDevTo(web), Poor(live), web)
    assert store.jobs and all(j["status"] == S.RETRY_REQUIRED and j["retry_count"] == 1 and j["error_category"] == "quality" for j in store.jobs.values())


def test_platform_error_is_retried_without_a_duplicate(store, live, clock):
    web = FakeWeb()

    class Flaky(FakeDevTo):
        def submit(self, content, live_):
            if self.n == 0:
                self.n += 1
                return SubmissionResult("error", message="HTTP 503", error_category="server", retryable=True)
            return super().submit(content, live_)

    adapter = Flaky(web)
    go(store, live, adapter, ArticleLLM(live), web)
    first = next(iter(store.jobs.values()))
    assert first["status"] == S.RETRY_REQUIRED and first["retry_count"] == 1
    assert store.verified_on() == 1, "another topic was published the same day instead of waiting"
    clock.advance(hours=1, minutes=5)
    go(store, live, adapter, ArticleLLM(live), web)
    assert first["status"] == S.VERIFIED
    topics = [j["topic_key"] for j in store.jobs.values()]
    titles = list(adapter.articles)
    assert len(topics) == len(set(topics)) and len(titles) == len(set(titles)), "no topic or article was submitted twice"


def test_publication_that_the_verifier_cannot_see_is_not_counted(store, live):
    web = FakeWeb()

    class Liar(FakeDevTo):
        def submit(self, content, live_):          # claims success, but the live page carries no link to us
            self.n += 1
            url = f"https://dev.to/me/post-{self.n}"
            self.web.pages[url] = page(title=content["title"])
            return SubmissionResult("published", url=url, reference="1")

    go(store, live, Liar(web), ArticleLLM(live), web)
    job = next(iter(store.jobs.values()))
    assert job["status"] == S.VERIFICATION_FAILED and store.verified_on() == 0 and store.count("published") == 1


# ------------------------------------------------------------------ discovery and the alternating engines
ITEMS = [{"url": "https://wellness-blog.org.uk/write-for-us", "title": "Write for us anxiety", "snippet": "anxiety", "matched_query": '"write for us" anxiety UK'},
         {"url": "https://mailonly.org.uk/contribute", "title": "Contribute anxiety", "snippet": "anxiety", "matched_query": '"write for us" anxiety UK'},
         {"url": "https://empty.org.uk/page", "title": "Sleep resources", "snippet": "sleep", "matched_query": '"write for us" anxiety UK'}]
FORM = '<form action="/s" method="post"><input name="a"><input type="email" name="b"><input type="url" name="c"><textarea name="d"></textarea></form>'
PAGES = {"https://wellness-blog.org.uk/write-for-us": page(FORM + '<div class="g-recaptcha"></div>' + "x" * 600),
         "https://mailonly.org.uk/contribute": page('<p>Pitch us at <a href="mailto:ed@mailonly.org.uk">ed@mailonly.org.uk</a> to contribute</p>' + "x" * 600),
         "https://empty.org.uk/page": page("<p>Nothing here</p>" + "x" * 600)}


def test_tavily_day_searches_and_records_what_each_site_really_requires(store, live):
    web, provider = FakeWeb(PAGES), Provider(ITEMS)
    result = go(store, live, FakeDevTo(web, available=False), ArticleLLM(live), web, engine="Tavily", provider=provider)
    by_domain = {j["domain"]: j for j in store.jobs.values()}
    assert provider.calls == 1 and store.used_this_month("tavily") == 4
    # guest-post rules: a website is only used when it clearly accepts outside articles and links, through a form
    wellness, mail, empty = (by_domain[d] for d in ("wellness-blog.org.uk", "mailonly.org.uk", "empty.org.uk"))
    assert wellness["status"] == S.REJECTED and "no clear statement" in wellness["history"][-1]["note"]
    assert mail["status"] == S.REJECTED and "no clear statement" in mail["history"][-1]["note"], "an email page that never says links are allowed is not used"
    assert empty["status"] == S.REJECTED and all(j.get("guest_checked") for j in (wellness, mail, empty))
    assert store.verified_on() == 0 and "Opportunities discovered: 3" in result["report"]


def test_the_same_page_or_domain_is_never_discovered_twice(store, live):
    web, provider = FakeWeb(PAGES), Provider(ITEMS)
    go(store, live, FakeDevTo(web, available=False), ArticleLLM(live), web, engine="Tavily", provider=provider)
    go(store, live, FakeDevTo(web, available=False), ArticleLLM(live), web, engine="Tavily", provider=provider)
    assert len(store.jobs) == 3


def test_firecrawl_day_does_not_search_and_scrapes_only_what_a_plain_fetch_cannot_read(store, live, monkeypatch):
    web = FakeWeb({"https://js-site.org.uk/write": FetchResult(200, "https://js-site.org.uk/write", "<html></html>")})
    store.add_job("k1", "web", "js-site.org.uk", "https://js-site.org.uk/write", "tavily: q")
    store.add_job("k2", "web", "plain.org.uk", "https://plain.org.uk/p", "tavily: q")
    web.pages["https://plain.org.uk/p"] = PAGES["https://empty.org.uk/page"]
    scraped = []
    from src.backlinks import discover
    monkeypatch.setattr(discover, "firecrawl_scrape_html", lambda url: scraped.append(url) or page(FORM + "x" * 600))
    provider = Provider(ITEMS)
    go(store, live, FakeDevTo(web, available=False), ArticleLLM(live), web, engine="Firecrawl", provider=provider)
    assert provider.calls == 0                                       # no search on a Firecrawl day
    assert scraped == ["https://js-site.org.uk/write"] and store.used_this_month("firecrawl") == 1
    assert store.jobs["k1"]["status"] == S.REJECTED and store.jobs["k1"]["guest_checked"], "read through Firecrawl, then judged by the guest-post rules"


def test_firecrawl_budget_is_respected_and_unreadable_pages_retry_later(store, live, monkeypatch):
    for i in range(6):
        store.add_job(f"k{i}", "web", f"blocked{i}.org.uk", f"https://blocked{i}.org.uk/", "tavily: q")
    web = FakeWeb({f"https://blocked{i}.org.uk/": FetchResult(403, "u", "", "HTTP 403") for i in range(6)})
    from src.backlinks import discover
    monkeypatch.setattr(discover, "firecrawl_scrape_html", lambda url: None)
    go(store, live, FakeDevTo(web, available=False), ArticleLLM(live), web, engine="Firecrawl")
    assert store.used_this_month("firecrawl") == live["discovery"]["firecrawl_scrapes_per_run"]
    assert all(j["status"] == S.RETRY_REQUIRED and j["error_category"] == "unreachable" for j in store.jobs.values())


def test_monthly_quota_blocks_searching(store, live):
    store.usage.setdefault(store.now().strftime("%Y-%m"), {})["tavily"] = live["quotas"]["tavily_searches_per_month"] - 5   # used on earlier days
    provider = Provider(ITEMS)
    result = go(store, live, FakeDevTo(FakeWeb(), available=False), ArticleLLM(live), FakeWeb(PAGES), engine="Tavily", provider=provider)
    assert provider.calls == 0 and "monthly cap" in result["report"]


def test_gemini_daily_cap_is_counted_and_enforced(store, live):
    from src.backlinks.llm import LLM
    for _ in range(live["quotas"]["gemini_calls_per_day"]):
        store.add_usage("gemini")
    web = FakeWeb()
    llm = LLM(store, live["quotas"]["gemini_calls_per_day"], generate=lambda p, k: (_ for _ in ()).throw(AssertionError("must not call")), keys=["k"])
    go(store, live, FakeDevTo(web), llm, web)
    job = next(iter(store.jobs.values()))
    assert job["status"] == S.RETRY_REQUIRED and job["error_category"] == "quota"


def test_engine_alternation_is_unchanged(monkeypatch):
    from src import scheduler, tavily_discovery

    class FakeDT:
        day = 0

        @classmethod
        def now(cls, tz=None):
            return datetime.fromordinal(cls.day).replace(tzinfo=timezone.utc)

    monkeypatch.setattr(scheduler, "datetime", FakeDT)
    seen = []
    for day in range(739000, 739006):
        FakeDT.day = day
        module, name = scheduler._discovery_provider_for_today()
        seen.append(name)
        assert (module is tavily_discovery) == (name == "Tavily") and (module is discovery) == (name == "Firecrawl")
    assert seen == ["Tavily", "Firecrawl"] * 3 or seen == ["Firecrawl", "Tavily"] * 3


def test_report_and_dashboard_reflect_verified_results_only(store, live, tmp_path):
    web = FakeWeb()
    pipeline.run(store=store, cfg=live, adapters={"devto": FakeDevTo(web)}, llm=ArticleLLM(live), fetch=web, engine="Firecrawl", provider=Provider(),
                 site=SITE, settings=SETTINGS, write_logs=True)
    dash = json.loads((tmp_path / "state" / "dashboard.json").read_text())["backlink_publishing"]["hypnotreatment-uk"]
    assert dash["verified_today"] == 1 and dash["verified_live_total"] == 1 and dash["live_links"][0]["follow"] is True
    reports = list((tmp_path / "logs" / "backlinks").glob("run-*.md"))
    assert len(reports) == 1 and "Verified live: 1" in reports[0].read_text(encoding="utf-8")
    assert dash["target_per_day"] == 2
