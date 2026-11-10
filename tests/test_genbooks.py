"""Genbooks (genbooks.uk): the third site, with facts taken only from its own pages."""
import json
import re
from pathlib import Path

import pytest
import yaml

from conftest import FakeWeb, link, one_a_day, page
from src.backlinks import content as C, pipeline, states as S
from src.backlinks.verify import verify_backlink
from test_pipeline import ArticleLLM, FakeDevTo, Provider, SETTINGS, sandbox  # noqa: F401 (autouse fixture)

ROOT = Path(__file__).resolve().parent.parent
GB_SITE = {"id": "genbooks-uk", "url": "https://genbooks.uk", "name": "Genbooks", "restrict_to_uk": False,
           "priority_keywords": ["book", "author"], "discovery_keywords": ["writing a book"]}


@pytest.fixture
def gb():
    return one_a_day({**yaml.safe_load((ROOT / "config" / "backlinks-genbooks.yaml").read_text(encoding="utf-8")), "live_publishing": True})


def test_the_site_config_keeps_every_kind_of_email_off():
    site = yaml.safe_load((ROOT / "config/sites/genbooks-uk.yaml").read_text(encoding="utf-8"))
    assert site["email_outreach"] is False and site["url"] in ("https://genbooks.uk", "https://genbooks.uk/") and site["restrict_to_uk"] is False


def test_topics_facts_and_targets_are_valid(gb):
    keys = [t["key"] for t in gb["topics"]]
    assert len(keys) == len(set(keys)) >= 8 and all(k.startswith("gb-") for k in keys)
    for t in gb["topics"]:
        assert t["target_url"] == "https://genbooks.uk/" and 1 <= len(t["tags"]) <= 4 and all(x.isalnum() for x in t["tags"]), t["key"]
        assert len(set(t["anchors"])) == len(t["anchors"]) >= 3
    all_anchors = [a.lower() for t in gb["topics"] for a in t["anchors"]]
    assert len(all_anchors) == len(set(all_anchors)), "no link text is used twice"
    facts = " ".join(gb["facts"])
    for invented in ("AI", "artificial", "£", "$", "days", "hours", "PDF", "EPUB", "Amazon", "Kindle"):
        assert invented not in facts, f"{invented!r} is not stated on the site, so it must not be a fact"
    assert gb["verification"]["canonical_hosts"][-1] == "genbooks.uk" and not any(ch in facts for ch in "–—")


def test_the_wording_is_its_own(gb):
    prompt = C.article_prompt(gb["topics"][0], gb["facts"], "an anchor", None, gb["brand_name"], gb["claims_rule"])
    assert "Genbooks" in prompt and "Hypnotreatment" not in prompt and "SocialField" not in prompt and "medication" not in prompt
    review = C.review_prompt({"title": "T", "body_markdown": "B"}, gb["facts"], gb["brand_name"], gb["reviewer_role"], gb["review_checks"])
    assert "writing and publishing publication" in review and "medical" not in review.lower() and "technology used" in review


def test_genbooks_end_to_end_with_its_own_disclosure_state_and_account(store, gb, tmp_path):
    web = FakeWeb()
    adapter = FakeDevTo(web)
    adapter._target = staticmethod(lambda content: re.search(r"\]\((https://genbooks\.uk/[^)]*)\)", content["body_markdown"]).group(1))
    pipeline.run(store=store, cfg=gb, adapters={"devto": adapter}, llm=ArticleLLM(gb), fetch=web, engine="Firecrawl", provider=Provider(),
                 site=GB_SITE, settings=SETTINGS, write_logs=True)
    job = next(iter(store.jobs.values()))
    assert job["status"] == S.VERIFIED and job["topic_key"].startswith("gb-") and store.verified_on() == 1
    saved = json.loads(store.read_content(job["content_path"]))
    assert "[Genbooks](https://genbooks.uk/)" in saved["body_markdown"] and "not legal or publishing advice" in saved["body_markdown"]
    assert "hypnotherapy" not in saved["body_markdown"].lower() and "medical advice" not in saved["body_markdown"]
    assert (tmp_path / "logs" / "backlinks-genbooks").exists()
    dash = json.loads((tmp_path / "state" / "dashboard.json").read_text())["backlink_publishing"]
    assert "genbooks-uk" in dash and dash["genbooks-uk"]["verified_today"] == 1


def test_the_bare_domain_homepage_link_verifies_with_or_without_www_or_a_slash(gb):
    hosts = gb["verification"]["canonical_hosts"]
    for href in ("https://genbooks.uk", "https://genbooks.uk/", "https://www.genbooks.uk/", "http://genbooks.uk/?utm_source=devto"):
        url = "https://dev.to/genbooks/post-1"
        web = FakeWeb({url: page(link(href, "request a book from Genbooks")), href: "ok"})
        assert verify_backlink(url, ["https://genbooks.uk/"], hosts, fetch=web, now_iso="t")["outcome"] == "verified", href
    web = FakeWeb({"https://dev.to/x": page(link("https://genbooks.uk.evil.com/"))})
    assert verify_backlink("https://dev.to/x", ["https://genbooks.uk/"], hosts, fetch=web, now_iso="t")["outcome"] == "link_absent"


def test_a_missing_genbooks_key_blocks_only_genbooks(store, gb, tmp_path):
    web = FakeWeb()
    pipeline.run(store=store, cfg=gb, adapters={"devto": FakeDevTo(web, available=False)}, llm=ArticleLLM(gb), fetch=web, engine="Firecrawl",
                 provider=Provider(), site=GB_SITE, settings=SETTINGS, write_logs=False)
    notes = json.loads((tmp_path / "wd" / "agent_state.json").read_text())["notifications"]
    assert notes[0]["run_id"].startswith("backlinks-blocked-genbooks-uk-devto-") and not store.jobs
