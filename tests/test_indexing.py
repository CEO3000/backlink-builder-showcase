"""A link is only worth having for ranking when search engines may index the page and follow its links. The first real
DEV.to pages (accounts with author-trust 0) carried noindex + nofollow, although the link itself was plain and verified."""
from conftest import FakeWeb, link, page
from src.backlinks import executor, states as S
from src.backlinks.verify import FetchResult, counts_for_seo, page_indexing, verify_backlink
from test_executor_watchdog import CONTENT, PUB_URL, TARGET, FakeAdapter, make_job, published

NOINDEX = '<meta name="robots" content="max-snippet:-1, max-image-preview:large"><meta name="robots" content="noindex"><meta name="robots" content="nofollow">'


def test_a_normal_page_is_indexable_and_followed():
    assert page_indexing('<meta name="robots" content="max-snippet:-1, max-image-preview:large, max-video-preview:-1">') == \
        {"indexable": True, "page_nofollow": False, "directives": []}
    assert page_indexing("<p>no robots tag at all</p>")["indexable"] is True


def test_noindex_and_nofollow_are_read_from_meta_tags_in_any_case_and_from_googlebot_tags():
    assert page_indexing(NOINDEX) == {"indexable": False, "page_nofollow": True, "directives": ["nofollow", "noindex"]}
    assert page_indexing('<META NAME="ROBOTS" CONTENT="NONE">')["indexable"] is False
    only_index = page_indexing('<meta name="googlebot" content="noindex">')
    assert only_index["indexable"] is False and only_index["page_nofollow"] is False


def test_the_x_robots_tag_header_counts_too():
    got = page_indexing("<p>hi</p>", {"x-robots-tag": "googlebot: noindex, nofollow"})
    assert got["indexable"] is False and got["page_nofollow"] is True


def test_counts_for_seo_needs_an_indexable_page_a_followed_link_and_a_page_that_follows():
    ok, blocked = page_indexing("<p>x</p>"), page_indexing(NOINDEX)
    assert counts_for_seo(True, ok) is True
    assert counts_for_seo(False, ok) is False, "a nofollow link does not count"
    assert counts_for_seo(True, blocked) is False
    assert counts_for_seo(True, page_indexing('<meta name="robots" content="nofollow">')) is False
    assert counts_for_seo(True, None) is None, "never checked is not the same as blocked"


def _verify(html):
    web = FakeWeb({PUB_URL: html, TARGET: "ok"})
    return verify_backlink(PUB_URL, [TARGET], ["www.hypnotreatment.uk"], "Sleep and screens", web, "now")


def test_the_link_is_still_verified_when_the_page_is_noindex_but_the_evidence_says_so():
    result = _verify(NOINDEX + page(link(TARGET)))
    assert result["outcome"] == "verified", "the link is live, so the existing success definition is unchanged"
    assert result["indexing"]["indexable"] is False and result["indexing"]["page_nofollow"] is True


def test_the_executor_stores_whether_google_can_count_the_link(store, cfg):
    job = make_job(store)
    web = FakeWeb({PUB_URL: NOINDEX + page(link(TARGET)), TARGET: "ok"})
    assert executor.submit_job(store, job, FakeAdapter([published()]), CONTENT, True, cfg, fetch=web) == S.VERIFIED
    la = job["link_attributes"]
    assert la["follow"] is True and la["indexable"] is False and la["page_nofollow"] is True and la["counts_for_seo"] is False


def test_headers_from_the_fetch_are_used():
    web = FakeWeb({PUB_URL: FetchResult(200, PUB_URL, page(link(TARGET)), None, {"x-robots-tag": "noindex"}), TARGET: "ok"})
    assert verify_backlink(PUB_URL, [TARGET], ["www.hypnotreatment.uk"], "Sleep and screens", web, "now")["indexing"]["indexable"] is False
