"""With live_publishing on, private drafts are published through the platform's API (no Publish button needed),
but only if the owner approved them or they pass today's quality rules."""
import io
import json
import urllib.error
from unittest import mock

import pytest

from conftest import FakeWeb, link, page
from src.backlinks import states as S, watchdog
from src.backlinks.adapters.base import Adapter, SubmissionResult
from test_content import article, raw, topic

PUB_URL = "https://dev.to/hypnotreatment/the-article-1abc"
TARGET = "https://www.hypnotreatment.uk/treatments/insomnia"
ADVERT = ("\n\nHypnotreatment is the hypnotherapy practice of Shahin Gurbanov, a qualified hypnotherapist in London. "
          "Shahin uses hypnotherapy with NLP and ideas from cognitive behavioural therapy (CBT). "
          "Sessions are available in person in London and online by video, starting with a free video consultation.")


class DraftAdapter(Adapter):
    name, domain, mechanism, docs_url = "devto", "dev.to", "authorised_api", "https://docs.example"

    def __init__(self, results):
        self.results, self.published = list(results), []

    def available(self):
        return True, "ok"

    def publish_draft(self, reference):
        self.published.append(reference)
        return self.results.pop(0)

    def find_existing(self, title):
        return None


def draft_job(store, cfg, extra="", **kw):
    t = topic(cfg)
    art = article(cfg, extra=extra)
    path = store.save_content("k", json.dumps({k: art[k] for k in ("title", "description", "body_markdown")} | {"tags": ["sleep"]}))
    job, _ = store.add_job("k", "devto", "dev.to", "https://dev.to/", "configured topic", topic_key=t["key"], target_url=t["target_url"],
                           expected_urls=[t["target_url"]], title=art["title"], anchor_text="hypnotherapy for insomnia", content_path=path,
                           submission_reference="42", draft_url="https://dev.to/me/draft-temp-slug-1", retry_count=0)
    job["status"] = S.PENDING_APPROVAL
    job.update(kw)
    return job


def live_web():
    return FakeWeb({PUB_URL: page(link(TARGET), title="Why your brain will not switch off at night"), TARGET: "ok"})


def test_a_good_draft_is_published_through_the_api_and_verified(store, cfg):
    cfg = {**cfg, "live_publishing": True}
    job = draft_job(store, cfg)
    job["title"] = "Why your brain will not switch off at night"
    adapter = DraftAdapter([SubmissionResult("published", url=PUB_URL, reference="42")])
    notes = watchdog.publish_pending(store, {"devto": adapter}, cfg, fetch=live_web())
    assert adapter.published == ["42"] and job["status"] == S.VERIFIED and job["publication_url"] == PUB_URL and store.verified_on() == 1
    assert "published through the API" in notes[0]


def test_nothing_is_published_while_live_publishing_is_off(store, cfg):
    job = draft_job(store, {**cfg, "live_publishing": False})
    adapter = DraftAdapter([])
    assert watchdog.publish_pending(store, {"devto": adapter}, {**cfg, "live_publishing": False}, fetch=live_web()) == []
    assert adapter.published == [] and job["status"] == S.PENDING_APPROVAL


def test_a_draft_that_fails_todays_rules_stays_a_draft_unless_the_owner_approved_it(store, cfg):
    cfg = {**cfg, "live_publishing": True}
    job = draft_job(store, cfg, extra=ADVERT)
    adapter = DraftAdapter([])
    notes = watchdog.publish_pending(store, {"devto": adapter}, cfg, fetch=live_web())
    assert adapter.published == [] and job["status"] == S.PENDING_APPROVAL
    assert "advert" in job["publish_skipped"] and "stays a draft" in notes[0]


def test_an_owner_approved_draft_is_published_even_if_it_is_promotional(store, cfg):
    cfg = {**cfg, "live_publishing": True}
    job = draft_job(store, cfg, extra=ADVERT, owner_approved=True)
    job["title"] = "Why your brain will not switch off at night"
    adapter = DraftAdapter([SubmissionResult("published", url=PUB_URL, reference="42")])
    watchdog.publish_pending(store, {"devto": adapter}, cfg, fetch=live_web())
    assert adapter.published == ["42"] and job["status"] == S.VERIFIED


def test_failures_are_recorded_and_bounded(store, cfg):
    cfg = {**cfg, "live_publishing": True}
    job = draft_job(store, cfg)
    bad = lambda: SubmissionResult("error", message="HTTP 503", error_category="server", retryable=True)
    adapter = DraftAdapter([bad(), bad(), bad(), bad()])
    for _ in range(5):
        watchdog.publish_pending(store, {"devto": adapter}, cfg, fetch=live_web())
    assert len(adapter.published) == watchdog.MAX_PUBLISH_ATTEMPTS and job["status"] == S.PENDING_APPROVAL
    assert job["error_category"] == "server" and job["publish_attempts"] == 3


def test_a_page_without_our_link_after_publishing_is_not_counted(store, cfg):
    cfg = {**cfg, "live_publishing": True}
    job = draft_job(store, cfg)
    adapter = DraftAdapter([SubmissionResult("published", url=PUB_URL, reference="42")])
    watchdog.publish_pending(store, {"devto": adapter}, cfg, fetch=FakeWeb({PUB_URL: page(title="Why your brain will not switch off at night")}))
    assert job["status"] == S.VERIFICATION_FAILED and store.verified_on() == 0


def test_a_draft_without_a_platform_reference_is_left_alone(store, cfg):
    cfg = {**cfg, "live_publishing": True}
    job = draft_job(store, cfg, submission_reference=None)
    adapter = DraftAdapter([])
    watchdog.publish_pending(store, {"devto": adapter}, cfg)
    assert adapter.published == [] and job["status"] == S.PENDING_APPROVAL


def test_the_full_watchdog_pass_includes_it(store, cfg):
    cfg = {**cfg, "live_publishing": True}
    job = draft_job(store, cfg)
    job["title"] = "Why your brain will not switch off at night"
    adapter = DraftAdapter([SubmissionResult("published", url=PUB_URL, reference="42")])
    adapter.find_existing = lambda title: SubmissionResult("draft", url="u")   # still a draft on the platform
    watchdog.run(store, {"devto": adapter}, cfg, fetch=live_web())
    assert job["status"] == S.VERIFIED
