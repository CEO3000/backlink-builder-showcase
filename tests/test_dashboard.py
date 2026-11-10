"""The dashboard (dashboard/index.html). Its pure logic runs under node; the configuration it generates for a new website must
work in the real Python system: load, validate, and publish two articles a day with no hand-written topics."""
import json
import random
import re
import shutil
import subprocess
from pathlib import Path

import pytest
import yaml

from conftest import FakeWeb
from src.backlinks import pipeline, settings as S_, states as S
from test_pipeline import ArticleLLM, FakeDevTo, Provider, SETTINGS, VOCAB, sandbox  # noqa: F401 (autouse fixture)

ROOT = Path(__file__).resolve().parent.parent
DASH = ROOT / "dashboard"
NODE = shutil.which("node")
needs_node = pytest.mark.skipif(NODE is None, reason="node is not installed")


def js(*calls):
    """[(function name, *args), ...] -> list of results, run in one node process against the dashboard's lib script."""
    payload = json.dumps([{"fn": c[0], "args": list(c[1:])} for c in calls])
    done = subprocess.run([NODE, str(DASH / "lib_harness.js")], input=payload, capture_output=True, text=True, encoding="utf-8", timeout=60)
    assert done.returncode == 0, done.stderr
    out = json.loads(done.stdout)
    for call, r in zip(calls, out):
        assert r["ok"], f"{call[0]} raised: {r.get('error')}"
    return [r["value"] for r in out]


def one(*call):
    return js(call)[0]


# ------------------------------------------------------------------ static safety of the page
def test_the_page_has_no_inline_handlers_no_external_code_and_the_basics():
    html = (DASH / "index.html").read_text(encoding="utf-8")
    assert not re.search(r"\son[a-z]+\s*=", html), "no inline event handlers (events are delegated)"
    assert "javascript:" not in html and "eval(" not in html and "document.write" not in html
    assert not re.search(r"<script[^>]+src=", html) and not re.search(r'<link[^>]+rel="stylesheet"', html), "one self-contained file, no CDN"
    assert '<html lang="en"' in html and 'name="viewport"' in html and "prefers-color-scheme: dark" in html and "prefers-reduced-motion" in html
    assert 'aria-live="polite"' in html and "role=\"log\"" in html and ":focus-visible" in html
    assert "email_outreach: false" in html, "a site the dashboard adds can never be cold-emailed"


# ------------------------------------------------------------------ pure helpers
@needs_node
def test_text_helpers():
    assert js(("slugify", "Pet Shop UK"), ("slugify", "https://www.Example.com/"), ("slugify", "Café Über"), ("slugify", "  --x--  "),
              ("esc", "<img src=x onerror=alert(1)> & \"q\" 'a'"), ("safeUrl", "javascript:alert(1)"), ("safeUrl", "https://dev.to/x"),
              ("hostOf", "https://WWW.Example.com/a"), ("bareHost", "https://www.example.com/a")) == [
        "pet-shop-uk", "example-com", "cafe-uber", "x",
        "&lt;img src=x onerror=alert(1)&gt; &amp; &quot;q&quot; &#39;a&#39;", "#", "https://dev.to/x",
        "www.example.com", "example.com"]


@needs_node
@pytest.mark.parametrize("text", ['He said "hi"', "a: b # not a comment", "{braces} and [brackets]", "two\nlines", "café ✓ ü", "'single'", "- dash", "yes", "null", "123"])
def test_every_string_survives_a_trip_through_yaml(text):
    assert yaml.safe_load("key: " + one("yq", text))["key"] == text


@needs_node
def test_pages_and_facts_parsing():
    pages = one("parsePages", "https://x.com/ | home\n\n/menu | the menu\nhttps://x.com/a|b | c\nhttps://x.com/plain", "https://x.com/")
    assert pages == [{"url": "https://x.com/", "about": "home"}, {"url": "https://x.com/menu", "about": "the menu"},
                     {"url": "https://x.com/a", "about": "b | c"}, {"url": "https://x.com/plain", "about": "a page of the site"}]
    assert one("parseFacts", "- one\n* two\n• three\n\n  four  ") == ["one", "two", "three", "four"]


GOOD = {"name": "Pet Shop UK", "url": "https://petshop.example/", "description": "an online pet supplies shop",
        "facts": "We sell pet food and toys online.\nDelivery is free over a stated amount shown at checkout.\nCustomers can return unopened items.",
        "pages": "https://petshop.example/ | the homepage", "articlesPerDay": "2", "mode": "live", "id": "", "author": "Jane Smith"}


@needs_node
def test_validation_accepts_a_good_form_and_fills_in_the_derived_values():
    r = one("validateNewSite", GOOD, ["other"], ["other.com"])
    assert r["errors"] == {} and r["value"]["id"] == "pet-shop-uk" and r["value"]["author"] == "Jane Smith"
    assert r["value"]["articlesPerDay"] == 2 and r["value"]["mode"] == "live" and len(r["value"]["facts"]) == 3


@needs_node
@pytest.mark.parametrize("change,field", [
    ({"name": ""}, "name"), ({"name": "x" * 61}, "name"), ({"url": "petshop"}, "url"), ({"url": "ftp://petshop.example"}, "url"), ({"url": "https://nodot"}, "url"),
    ({"description": "ab"}, "description"), ({"id": "Bad Id"}, "id"), ({"id": "taken"}, "id"), ({"facts": "one\ntwo"}, "facts"),
    ({"facts": "short\nshort\nshort"}, "facts"), ({"facts": "\n".join(["A complete sentence about us."] * 13)}, "facts"),
    ({"pages": ""}, "pages"), ({"pages": "https://evil.example/ | not ours"}, "pages"), ({"pages": "\n".join(["/a | x"] * 13)}, "pages"),
    ({"articlesPerDay": "0"}, "articlesPerDay"), ({"articlesPerDay": "11"}, "articlesPerDay"), ({"articlesPerDay": "2.5"}, "articlesPerDay"),
    ({"author": ""}, "author"), ({"author": "<script>"}, "author"), ({"url": "https://already.example"}, "url")])
def test_validation_rejects_each_kind_of_mistake_with_a_message_for_that_field(change, field):
    r = one("validateNewSite", {**GOOD, **change}, ["taken"], ["already.example"])
    assert field in r["errors"] and r["value"] is None and len(r["errors"][field]) > 10


@needs_node
def test_the_same_website_with_or_without_www_is_a_duplicate():
    r = one("validateNewSite", {**GOOD, "url": "https://www.petshop.example/x"}, [], ["petshop.example"])
    assert "already added" in r["errors"]["url"]


@needs_node
def test_merge_settings_changes_only_what_was_asked():
    doc = {"_help": "keep", "extra": 1, "sites": {"a": {"articles_per_day": 2, "mode": "live", "enabled": True, "x": "keep"}, "b": {"articles_per_day": 1}}}
    out = one("mergeSettings", doc, "a", {"articles_per_day": 5, "mode": "draft", "enabled": False})
    assert out["sites"]["a"] == {"articles_per_day": 5, "mode": "draft", "enabled": False, "x": "keep"}
    assert out["sites"]["b"] == {"articles_per_day": 1} and out["_help"] == "keep" and out["extra"] == 1
    assert one("mergeSettings", doc, "a", {"articles_per_day": 99})["sites"]["a"]["articles_per_day"] == 10
    assert one("mergeSettings", doc, "a", {"articles_per_day": 0})["sites"]["a"]["articles_per_day"] == 1
    assert one("mergeSettings", doc, "a", {"mode": "sideways", "enabled": "yes"})["sites"]["a"] == doc["sites"]["a"]
    assert one("mergeSettings", None, "new", {"articles_per_day": 2})["sites"]["new"] == {"articles_per_day": 2}
    assert one("mergeSettings", {"sites": "broken"}, "a", {"enabled": True})["sites"]["a"] == {"enabled": True}


@needs_node
def test_what_the_dashboard_saves_is_what_the_python_side_applies(cfg):
    saved = one("mergeSettings", {"sites": {}}, "hypnotreatment-uk", {"articles_per_day": 4, "mode": "draft", "enabled": False})
    out = S_.apply_settings(cfg, saved["sites"]["hypnotreatment-uk"])
    assert out["limits"]["max_publications_per_platform_per_day"] == 4 and out["live_publishing"] is False and out["enabled"] is False


NOW = {"__date__": "2026-10-28T12:00:00Z"}


def entry(**kw):
    return {"updated_at": "2026-10-28T09:20:00Z", "target_per_day": 2, "guest_today": 2, "topics_unused": 9, "errors": [], "blocked": [], "notes": [], **kw}


@needs_node
def test_site_status_levels_always_have_a_label_and_reasons():
    s = lambda e, st=None, issues=None: one("siteStatus", e, st or {"enabled": True}, issues or [], NOW)
    assert s(entry())["level"] == "ok" and s(entry())["label"] == "Healthy"
    assert s(None)["level"] == "new"
    assert s(entry(), {"enabled": False})["level"] == "paused"
    assert s(entry(guest_today=1))["level"] == "warn" and "1 of 2" in s(entry(guest_today=1))["reasons"][0]
    assert s(entry(topics_unused=1))["level"] == "warn"
    assert s(entry(notes=["Stopping for today: Gemini is unavailable"]))["level"] == "warn"
    assert s(entry(blocked=["the key is not set"]))["level"] == "error"
    assert s(entry(errors=[{"status": "BLOCKED_CREDENTIALS", "title": "A", "category": "auth", "message": "x"}]))["level"] == "error"
    assert s(entry(errors=[{"status": "RETRY_REQUIRED", "title": "A", "category": "quota", "message": "x"}]))["level"] == "ok", "a queued retry is not an error"
    assert s(entry(), issues=[{"problems": ["missing facts"]}])["level"] == "error"
    assert s(entry(updated_at="2026-10-25T09:00:00Z"))["level"] == "warn", "stale data is flagged"
    assert s(entry(updated_at="2026-10-27T09:00:00Z", guest_today=0))["level"] == "ok", "a site that has not run today yet is not blamed for today's count"


@needs_node
def test_every_error_category_is_explained_in_plain_words():
    for cat in ("auth", "quota", "busy", "network", "truncated", "rate_limited", "server", "validation", "quality", "factcheck", "stuck",
                "verify_link_absent", "verify_page_gone", "verify_page_unreachable"):
        e = one("explainError", cat, "RETRY_REQUIRED", "raw message")
        assert e["headline"] != "Something went wrong." and e["action"] and e["message"] == "raw message", cat
    assert "gone" in one("explainError", None, "REMOVED", "")["headline"]
    assert "blocked" in one("explainError", None, "BLOCKED_CREDENTIALS", "")["headline"].lower()
    assert one("explainError", "mystery", "X", "m")["headline"] == "Something went wrong."


@needs_node
def test_gemini_estimate_and_page_helpers():   # the per-day chart and its helpers were removed 2026-11-01
    assert one("estimateGemini", {"a": {"articles_per_day": 2}, "b": {"articles_per_day": 3, "enabled": False}, "c": {"articles_per_day": 4}}) == {"articles": 6, "requests": 28, "limit": 80}
    assert one("seriesSlot", ["b", "a", "c"], "a") == "var(--s1)" and one("seriesSlot", ["b", "a", "c"], "c") == "var(--s3)"
    assert one("seriesSlot", list("abcdefghi"), "i") == "var(--s-other)" and one("seriesSlot", ["a"], "zzz") == "var(--s-other)"
    assert [one("timeAgo", {"__date__": "2026-10-28T11:59:40Z"}, NOW), one("timeAgo", {"__date__": "2026-10-28T11:15:00Z"}, NOW),
            one("timeAgo", {"__date__": "2026-10-28T06:00:00Z"}, NOW), one("timeAgo", {"__date__": "2026-10-25T12:00:00Z"}, NOW)] == ["just now", "45 min ago", "6 h ago", "3 d ago"]


# ------------------------------------------------------------------ the generated configuration really works
TRICKY = {"name": "Joe's \"Best\" Café {Ltd}", "url": "https://www.joes-cafe.co.uk", "description": "a café & bakery: \"fresh\" daily {really}",
          "facts": "We bake bread: fresh every morning.\nThe café serves coffee, tea & \"cakes\" - all day.\nTables can be booked online for {groups} of any size.",
          "pages": "/menu | the menu\nhttps://www.joes-cafe.co.uk/ | the homepage", "articlesPerDay": "3", "mode": "draft", "id": "joes", "author": "Joe O'Brien"}
SUBDOMAIN = {**GOOD, "name": "Example Blog", "url": "https://blog.example.com/", "pages": "https://blog.example.com/ | the blog", "id": "", "description": "a technology blog"}
FORMS = {"plain": GOOD, "tricky": TRICKY, "subdomain": SUBDOMAIN}


def generated(form):
    value = one("validateNewSite", form, [], [])
    assert value["errors"] == {}, value["errors"]
    v = value["value"]
    site_yaml, pub_yaml = js(("buildSiteYaml", v), ("buildPublishingYaml", v))
    return v, yaml.safe_load(site_yaml), yaml.safe_load(pub_yaml), site_yaml


@needs_node
@pytest.mark.parametrize("name", list(FORMS))
def test_the_generated_site_file_switches_email_off(name):
    v, site, pub, text = generated(FORMS[name])
    assert site["email_outreach"] is False and site["url"] == v["url"] and site["name"] == v["name"]
    assert pub["site_id"] in {"pet-shop-uk", "joes", "example-blog"}


@needs_node
@pytest.mark.parametrize("name", list(FORMS))
def test_the_generated_publishing_config_is_valid_and_faithful(name):
    v, _, cfg, _ = generated(FORMS[name])
    assert S_.validate_config(cfg) == []
    assert cfg["facts"] == v["facts"] and cfg["brand_name"] == v["name"] and cfg["site_id"] == v["id"] and cfg["topics"] == []
    assert "platforms" not in cfg and cfg["daily_target_verified"] == v["articlesPerDay"]
    gp = cfg["guest_posts"]
    assert gp["submit"] is False and gp["author"] == v["author"] and gp["bio"].startswith(v["author"] + " runs [" + v["name"] + "]({home})"), "review mode first, the author's bio links home"
    assert cfg["limits"]["max_publications_per_platform_per_day"] == v["articlesPerDay"] and cfg["limits"]["max_new_jobs_per_day"] == v["articlesPerDay"] + 2
    assert cfg["limits"]["ramp_days"] == 0 and cfg["live_publishing"] is (v["mode"] == "live")
    assert cfg["state_dir"] == cfg["log_dir"] == f"backlinks-{v['id']}"
    assert [t["url"] for t in cfg["topic_generation"]["allowed_targets"]] == [p["url"] for p in v["pages"]]
    entered = re.sub(r"^https?://", "", v["url"]).split("/")[0].lower()
    assert cfg["verification"]["canonical_hosts"][-1] == entered or entered.startswith("www.") is False
    assert entered in cfg["verification"]["canonical_hosts"]
    assert sum(c["quotas"]["tavily_searches_per_month"] for c in [cfg]) <= 300


@needs_node
@pytest.mark.parametrize("name", list(FORMS))
def test_the_text_templates_survive_python_formatting_even_with_braces_and_quotes(name):
    v, _, cfg, _ = generated(FORMS[name])
    disclosure = cfg["disclosure"].format(home="https://home.test/")
    assert f"[{v['name']}](https://home.test/)" in disclosure and v["description"] in disclosure and disclosure.startswith("\n\n---\n\n*Disclosure:")
    assert "{brand}" in cfg["review_checks"] and v["name"] in cfg["review_checks"].format(brand=v["name"]) is False or cfg["review_checks"].format(brand=v["name"])
    assert v["name"] in cfg["claims_rule"] and "Never promise" in cfg["claims_rule"]


@needs_node
def test_a_subdomain_gets_no_invented_www_variant_and_a_www_site_gets_both():
    assert generated(SUBDOMAIN)[2]["verification"]["canonical_hosts"] == ["blog.example.com"]
    assert generated(TRICKY)[2]["verification"]["canonical_hosts"] == ["joes-cafe.co.uk", "www.joes-cafe.co.uk"]
    assert generated(GOOD)[2]["verification"]["canonical_hosts"] == ["www.petshop.example", "petshop.example"]


def ideas_for(cfg, n, seed):
    rng = random.Random(seed)
    targets = [t["url"] for t in cfg["topic_generation"]["allowed_targets"]]
    out = []
    for i in range(n):
        w = rng.sample(VOCAB, 5)
        out.append({"title_hint": " ".join(w).capitalize() + " explained simply", "audience": "readers who want practical help",
                    "angle": f"shows one clear way to use {w[0]} and {w[1]} in daily life", "target_url": targets[i % len(targets)],
                    "anchors": [" ".join(rng.sample(VOCAB, 3)) for _ in range(3)], "tags": ["guides", "habits"]})
    return out


class NewSiteLLM(ArticleLLM):
    """Gemini for a site that has no hand-written topics: it proposes topics when asked, and writes an article for any of them."""

    def __init__(self, cfg):
        super().__init__(cfg)
        self.requests, self.known = 0, []

    def generate(self, prompt):
        if prompt.startswith("Suggest "):
            self.requests += 1
            proposals = ideas_for(self.cfg, 6, self.requests)
            self.cfg = {**self.cfg, "topics": [*self.cfg["topics"], *({**p, "key": re.sub(r"\W+", "-", p["title_hint"].lower())} for p in proposals)]}
            return json.dumps(proposals), "m"
        return super().generate(prompt)


@needs_node
@needs_node
@pytest.mark.parametrize("name", ["plain", "tricky", "subdomain"])
def test_a_brand_new_site_from_the_dashboard_finds_checks_and_writes_guest_posts_in_review_mode(name, store, clock):
    from src.backlinks import guestposts as G
    from src.backlinks.store import make_key
    v, _, cfg, _ = generated({**FORMS[name], "mode": "live", "articlesPerDay": "2"})
    assert S_.validate_config(cfg) == [] and cfg["topics"] == []
    acc = "We welcome guest posts from outside writers on any topic our readers enjoy."
    lnk = "You may include one link to your own website in your author bio."
    form = ('<form id="f"><label for="n">Name *</label><input id="n" name="name" required><label for="e">Email *</label><input id="e" type="email" name="email" required>'
            '<label for="m">Your article *</label><textarea id="m" name="message" required></textarea><button>Send</button></form>')
    pages = {}
    for i in range(3):
        url = f"https://blog{i}.example.org/write-for-us"
        store.add_job(make_key("web", f"blog{i}.example.org", url), adapter="web", domain=f"blog{i}.example.org", page_url=url, discovery_source="tavily: x", title="Write for us")
        pages[url] = f"<html><head><title>Write for us</title></head><body><h1>Write for us</h1><p>{acc} {lnk}</p>{form}</body></html>"

    class Judge(NewSiteLLM):
        def generate(self, prompt):
            if prompt.startswith("You check whether a website accepts articles"):
                return json.dumps({"accepts_outside_articles": True, "accepts_quote": acc, "links_allowed": True, "links_quote": lnk, "paid": False}), "m"
            if prompt.startswith("Judge this web page"):
                return json.dumps({"relevance": 8, "quality": 8, "topic": "t", "audience": "a", "reason": "fits"}), "m"
            return super().generate(prompt)

    llm = Judge(cfg)
    site = {"id": v["id"], "url": v["url"], "name": v["name"], "restrict_to_uk": False, "priority_keywords": ["x"], "discovery_keywords": ["x"],
            "contact": {"name": v["author"], "email": "hello@example.org"}}
    pipeline.run(store=store, cfg=cfg, adapters={}, llm=llm, fetch=FakeWeb(pages), engine="Firecrawl", provider=Provider(), site=site, settings=SETTINGS, write_logs=False)
    guests = [j for j in store.jobs.values() if j.get("adapter") == "guest"]
    assert len(guests) == 3 and sum(j["status"] == S.CONTENT_READY for j in guests) == 2, "all three qualify; two articles a day are written"
    assert not any(j["status"] in (S.SUBMITTED, S.PENDING_APPROVAL) for j in guests), "review mode: nothing is sent"
    saved = json.loads(store.read_content(next(j for j in guests if j.get("content_path"))["content_path"]))
    assert v["name"] in saved["body_markdown"] and "About the author" in saved["body_markdown"], "with no bio field, the bio (brand intact) ends the article"


@needs_node
@pytest.mark.skipif(not (DASH / "node_modules" / "jsdom").exists(), reason="jsdom is not installed (run `npm i jsdom` in dashboard/)")
def test_the_page_works_end_to_end_in_a_dom_against_a_fake_github():
    done = subprocess.run([NODE, str(DASH / "dom_test.js")], capture_output=True, text=True, encoding="utf-8", timeout=120, cwd=DASH)
    assert done.returncode == 0 and "ALL DASHBOARD CHECKS PASSED" in done.stdout, done.stdout[-1500:] + done.stderr[-500:]
