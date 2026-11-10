import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from src.backlinks.store import Store  # noqa: E402
from src.backlinks.verify import FetchResult  # noqa: E402



class Clock:
    def __init__(self):
        self.t = datetime(2026, 10, 27, 9, 15, tzinfo=timezone.utc)

    def __call__(self):
        return self.t

    def advance(self, **kw):
        self.t += timedelta(**kw)


@pytest.fixture
def clock():
    return Clock()


@pytest.fixture
def store(tmp_path, clock):
    return Store(tmp_path / "bl", clock=clock)


@pytest.fixture
def cfg():
    """The real config file, so the tests also prove it is valid."""
    return yaml.safe_load((ROOT / "config" / "backlinks.yaml").read_text(encoding="utf-8"))


def page(*links, title="Sleep and screens", extra=""):
    anchors = "".join(links)
    return (f"<html><head><title>{title}</title></head><body><article><h1>{title}</h1>"
            f"<p>Some words about winding down at night. {anchors}</p>{extra}</article></body></html>")


def link(href, text="hypnotherapy for insomnia", rel=None):
    return f'<a href="{href}"' + (f' rel="{rel}"' if rel else "") + f">{text}</a>"


class FakeWeb:
    """A fake fetch(): maps url -> FetchResult (or a string for a plain 200 page)."""

    def __init__(self, pages=None):
        self.pages, self.calls = dict(pages or {}), []

    def __call__(self, url, timeout=20):
        self.calls.append(url)
        r = self.pages.get(url)
        if r is None:
            return FetchResult(404, url, "", "HTTP 404")
        if isinstance(r, str):
            return FetchResult(200, url, r)
        return r


def one_a_day(cfg):
    """The older tests were written for one article a day per site; the shipped configs now allow two."""
    return {**cfg, "limits": {**cfg["limits"], "max_publications_per_platform_per_day": 1}}
