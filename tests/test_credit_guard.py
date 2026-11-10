"""Tavily and Firecrawl credits: besides our own counters, the providers' own numbers stop a search or scrape that would
leave less than a 100-credit reserve of the monthly allowance. Response shapes below are the real ones seen on 2026-10-27."""
import io
import json
from unittest import mock

from conftest import FakeWeb
from src.backlinks import discover, states as S
from src.backlinks.verify import FetchResult

TAVILY_REAL = {"key": {"usage": 246, "limit": None, "search_usage": 246}, "account": {"current_plan": "Researcher", "plan_usage": 246, "plan_limit": 1000}}
FIRECRAWL_REAL = {"success": True, "data": {"remaining_credits": 1005, "plan_credits": 1000}}


class Resp(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def serve(payload):
    return mock.patch("urllib.request.urlopen", return_value=Resp(json.dumps(payload).encode()))


def test_tavily_remaining_reads_the_real_shape(monkeypatch):
    monkeypatch.setenv("TAVILY_API_KEY", "k")
    with serve(TAVILY_REAL):
        assert discover.tavily_remaining() == 754


def test_firecrawl_remaining_reads_the_real_shape(monkeypatch):
    monkeypatch.setenv("FIRECRAWL_API_KEY", "k")
    with serve(FIRECRAWL_REAL):
        assert discover.firecrawl_remaining() == 1005


def test_unreadable_answers_or_no_key_give_none_never_a_guess(monkeypatch):
    monkeypatch.setenv("TAVILY_API_KEY", "k"); monkeypatch.setenv("FIRECRAWL_API_KEY", "k")
    with serve({"unexpected": "shape"}):
        assert discover.tavily_remaining() is None and discover.firecrawl_remaining() is None
    with mock.patch("urllib.request.urlopen", side_effect=OSError("down")):
        assert discover.tavily_remaining() is None
    monkeypatch.delenv("TAVILY_API_KEY"); monkeypatch.delenv("FIRECRAWL_API_KEY")
    assert discover.tavily_remaining() is None and discover.firecrawl_remaining() is None


class Provider:
    MAX_SEARCH_CALLS_PER_RUN = 16

    def __init__(self):
        self.calls = 0

    def discover(self, **kw):
        self.calls += 1
        return [], 4


def search(store, cfg):
    from test_pipeline import SETTINGS, SITE
    return discover.search_for_candidates(store, SITE, SETTINGS, cfg, Provider(), {"discovery_keywords": ["anxiety UK"]}, set())


def test_a_search_is_skipped_when_tavily_says_credits_are_nearly_gone(store, cfg, monkeypatch, tmp_path):
    from src import state as st
    monkeypatch.setattr(st, "STATE_DIR", tmp_path)
    monkeypatch.setattr(discover, "tavily_remaining", lambda: 100)          # 16 + reserve 100 would not fit
    out = search(store, cfg)
    assert "skipped" in out and "reserve of 100" in out["skipped"] and store.used_this_month("tavily") == 0


def test_a_search_goes_ahead_with_plenty_left_or_when_tavily_cannot_be_asked(store, cfg, monkeypatch, tmp_path):
    from src import state as st
    monkeypatch.setattr(st, "STATE_DIR", tmp_path)
    for remaining in (754, None):
        monkeypatch.setattr(discover, "tavily_remaining", lambda r=remaining: r)
        out = search(store, cfg)
        assert "skipped" not in out and out["searches"] == 4


def test_scraping_stops_when_firecrawl_says_credits_are_nearly_gone(store, cfg, monkeypatch):
    web = FakeWeb({"https://x.org.uk/p": FetchResult(403, "u", "", "HTTP 403")})
    scraped = []
    monkeypatch.setattr(discover, "firecrawl_remaining", lambda: 60)
    budget = [4]
    html, how = discover.fetch_for_inspection(store, "https://x.org.uk/p", "Firecrawl", cfg, budget, fetch=web, scrape=lambda u: scraped.append(u) or "<html>x</html>")
    assert html is None and scraped == [] and budget == [0] and "reserve of 100" in how and store.used_this_month("firecrawl") == 0


def test_scraping_goes_ahead_with_credits_left(store, cfg, monkeypatch):
    web = FakeWeb({"https://x.org.uk/p": FetchResult(403, "u", "", "HTTP 403")})
    monkeypatch.setattr(discover, "firecrawl_remaining", lambda: 1005)
    html, how = discover.fetch_for_inspection(store, "https://x.org.uk/p", "Firecrawl", cfg, [4], fetch=web, scrape=lambda u: "<html>scraped</html>")
    assert html == "<html>scraped</html>" and how == "firecrawl scrape" and store.used_this_month("firecrawl") == 1
