from conftest import FakeWeb, link, page
from src.backlinks.verify import FetchResult, extract_links, verify_backlink

HOSTS = ["hypnotreatment.uk", "www.hypnotreatment.uk"]
TARGET = "https://www.hypnotreatment.uk/treatments/insomnia"
EXPECTED = [TARGET, "https://www.hypnotreatment.uk/"]
URL = "https://dev.to/someone/sleep-and-screens-1abc"


def verify(web, **kw):
    return verify_backlink(URL, EXPECTED, HOSTS, expected_text=kw.pop("expected_text", "Sleep and screens"), fetch=web, now_iso="t", **kw)


def test_live_link_is_verified_with_evidence():
    web = FakeWeb({URL: page(link(TARGET)), TARGET: "<html>ok</html>"})
    r = verify(web)
    assert r["outcome"] == "verified"
    assert r["matched_link"]["anchor"] == "hypnotherapy for insomnia" and r["matched_link"]["follow"] is True
    assert r["page_status"] == 200 and r["content_present"] is True
    assert r["destination"]["status"] == 200 and r["destination"]["final_host_is_ours"] is True


def test_absent_link_is_not_verified():
    r = verify(FakeWeb({URL: page()}))
    assert r["outcome"] == "link_absent" and "no link to our site" in r["reason"]


def test_nofollow_is_still_a_backlink_but_is_recorded():
    r = verify(FakeWeb({URL: page(link(TARGET, rel="nofollow ugc")), TARGET: "x"}))
    assert r["outcome"] == "verified"
    assert r["matched_link"]["follow"] is False and r["matched_link"]["rel"] == ["nofollow", "ugc"]


def test_followed_link_is_preferred_over_a_nofollow_one():
    web = FakeWeb({URL: page(link(TARGET, rel="nofollow"), link("https://www.hypnotreatment.uk/", "Hypnotreatment")), "https://www.hypnotreatment.uk/": "x"})
    r = verify(web)
    assert r["matched_link"]["follow"] is True and r["matched_link"]["anchor"] == "Hypnotreatment"


def test_host_and_trailing_slash_variants_match():
    for href in ("https://hypnotreatment.uk/treatments/insomnia/", "http://WWW.hypnotreatment.uk/treatments/insomnia?utm_source=devto"):
        assert verify(FakeWeb({URL: page(link(href)), href: "x"}))["outcome"] == "verified", href


def test_lookalike_domains_do_not_count():
    for href in ("https://hypnotreatment.uk.evil.com/treatments/insomnia", "https://nothypnotreatment.uk/treatments/insomnia",
                 "https://example.com/?u=https://www.hypnotreatment.uk/treatments/insomnia"):
        assert verify(FakeWeb({URL: page(link(href))}))["outcome"] == "link_absent", href


def test_plain_text_mention_is_not_a_link():
    r = verify(FakeWeb({URL: page("Visit hypnotreatment.uk for more.")}))
    assert r["outcome"] == "link_absent" and "plain text" in r["reason"]


def test_hidden_links_do_not_count():
    hidden = f'<div style="display: none">{link(TARGET)}</div>'
    r = verify(FakeWeb({URL: page(extra=hidden)}))
    assert r["outcome"] == "link_absent"
    r = verify(FakeWeb({URL: page(extra=f"<noscript>{link(TARGET)}</noscript>")}))
    assert r["outcome"] == "link_absent"


def test_link_to_an_unexpected_page_of_our_site_is_reported_not_counted():
    r = verify(FakeWeb({URL: page(link("https://www.hypnotreatment.uk/treatments/phobia"))}))
    assert r["outcome"] == "link_absent" and "not to an expected page" in r["reason"]
    assert r["site_links_found"][0]["resolved"].endswith("/treatments/phobia")


def test_page_gone_and_unreachable_are_told_apart():
    assert verify(FakeWeb({URL: FetchResult(404, URL, "", "HTTP 404")}))["outcome"] == "page_gone"
    assert verify(FakeWeb({URL: FetchResult(410, URL, "", "HTTP 410")}))["outcome"] == "page_gone"
    for bad in (FetchResult(503, URL, "", "HTTP 503"), FetchResult(None, URL, "", "TimeoutError"), FetchResult(403, URL, "", "HTTP 403")):
        assert verify(FakeWeb({URL: bad}))["outcome"] == "page_unreachable"


def test_page_that_lost_our_content_fails_even_if_a_link_remains():
    r = verify(FakeWeb({URL: page(link(TARGET), title="Totally different post about gardening")}), expected_text="Why your brain will not switch off at night")
    assert r["outcome"] == "link_absent" and r["content_present"] is False


def test_destination_redirect_is_recorded():
    web = FakeWeb({URL: page(link("https://hypnotreatment.uk/treatments/insomnia")),
                   "https://hypnotreatment.uk/treatments/insomnia": FetchResult(200, "https://www.hypnotreatment.uk/treatments/insomnia", "x")})
    d = verify(web)["destination"]
    assert d["redirected"] is True and d["final_host_is_ours"] is True


def test_unreadable_links_are_ignored_and_image_links_are_labelled():
    links = extract_links('<a href="mailto:a@b.c">m</a><a href="javascript:void(0)">j</a><a href="#top">t</a>'
                          '<a href="https://www.hypnotreatment.uk/"><img alt="logo"></a>', "https://x.test/")
    assert [(l["kind"], l["anchor"]) for l in links] == [("image_link", "[image: logo]")]


def test_relative_links_resolve_against_the_page():
    links = extract_links('<a href="/treatments/insomnia">x</a>', "https://www.hypnotreatment.uk/blog/post")
    assert links[0]["resolved"] == "https://www.hypnotreatment.uk/treatments/insomnia"
