"""The guest-post machine. A website is used only when it clearly accepts outside articles and a link back, through a form the
system can fill; everything else is never shown. No test touches the network or a real browser."""
import base64
import json
import pytest

from conftest import FakeWeb, link, page
from src.backlinks import guestposts as G, pipeline, states as S
from src.backlinks.llm import LLMError
from src.backlinks.store import make_key
from test_pipeline import sandbox  # noqa: F401 (autouse fixture)
from test_two_a_day import shipped

ACCEPTS = "We welcome guest posts from outside writers on writing and publishing books."
LINKS = "You may include one link to your own website in your author bio."
FORM = ('<form id="gp" action="/submit" method="post"><label for="n">Your name *</label><input id="n" name="your-name" required>'
        '<label for="e">Email *</label><input id="e" type="email" name="your-email" required>'
        '<label for="t">Article title</label><input id="t" name="post-title">'
        '<label for="a">Your article *</label><textarea id="a" name="your-message" required></textarea>'
        '<label for="b">Author bio</label><textarea id="b" name="author-bio"></textarea>'
        '<p style="display:none"><input name="hp-field" value=""></p>'
        '<label><input type="checkbox" name="agree" required> I agree to the terms</label>'
        '<label><input type="checkbox" name="newsletter"> Send me the newsletter</label>'
        '<button type="submit">Send</button></form>')
CAPTCHA = '<div class="g-recaptcha" data-sitekey="k"></div>'
EFF = {"priority_keywords": ["book", "writing"], "discovery_keywords": ["writing a book"], "competitor_terms": ["bookrival"]}
SETTINGS = {"do_not_contact": ["example-company"]}
SITE = {"id": "genbooks-uk", "name": "Genbooks", "url": "https://genbooks.uk/", "contact": {"name": "The Genbooks team", "email": "support@genbooks.example"},
        "priority_keywords": ["book", "writing"]}


def guide(form=FORM, text=ACCEPTS + " " + LINKS, captcha=False):
    return f"<html><head><title>Write for us - Writers Hub</title></head><body><h1>Write for us</h1><p>{text}</p>{form.replace('</form>', (CAPTCHA if captcha else '') + '</form>')}</body></html>"


class Judge:
    """Gemini as the checks would see it: an acceptance answer, a relevance answer, and articles."""

    def __init__(self, cfg, acceptance=None, relevance=8, quality=8, down=False):
        from test_pipeline import ArticleLLM
        self.articles, self.calls, self.down = ArticleLLM(cfg), [], down
        self.acceptance = acceptance or {"accepts_outside_articles": True, "accepts_quote": ACCEPTS, "links_allowed": True, "links_quote": LINKS,
                                         "paid": False, "min_words": None, "max_words": None, "topics": "writing a book", "rules": "No AI spam."}
        self.relevance, self.quality = relevance, quality

    def generate(self, prompt):
        self.calls.append(prompt[:40])
        if self.down:
            raise LLMError("quota", "daily limit", True)
        if prompt.startswith("You check whether a website accepts articles"):
            return json.dumps(self.acceptance), "m"
        if prompt.startswith("Judge this web page"):
            return json.dumps({"relevance": self.relevance, "quality": self.quality, "topic": "books", "audience": "writers", "reason": "fits"}), "m"
        return self.articles.generate(prompt)


@pytest.fixture
def cfg():
    c = shipped("backlinks-genbooks.yaml")
    c["discovery"] = {**c["discovery"], "inspect_per_run": 10, "firecrawl_scrapes_per_run": 0}
    c["guest_posts"] = {**c["guest_posts"], "submit": False}   # review mode unless a test says live(cfg): the shipped file is live
    return c


def candidate(store, domain="writershub.example.org", path="write-for-us"):
    url = f"https://{domain}/{path}"
    job, _ = store.add_job(make_key("web", domain, url), adapter="web", domain=domain, page_url=url, discovery_source="tavily: x", title="Write for us")
    return job


def checked(store, cfg, html, judge=None, domain="writershub.example.org", extra=None):
    job = candidate(store, domain)
    web = FakeWeb({job["page_url"]: html, **(extra or {})})
    result = G.check_candidate(store, job, cfg, EFF, SETTINGS, judge or Judge(cfg), "Tavily", web, [0])
    return result, job


# ------------------------------------------------------------------ the form
def test_a_typical_guest_post_form_is_mapped_and_the_traps_are_left_alone():
    m = G.map_form(guide(), "https://x.org/write-for-us")
    roles = {f["name"]: f["role"] for f in m["fields"]}
    assert m["ok"] and roles == {"your-name": "name", "your-email": "email", "post-title": "title", "your-message": "article", "author-bio": "bio", "agree": "consent"}
    assert "hp-field" not in roles, "a hidden honeypot field is never touched"
    assert "newsletter" not in roles, "a newsletter box is never ticked"
    assert m["form"] == {"id": "gp", "action": "/submit", "index": 0}


def test_a_form_with_a_required_field_the_system_cannot_fill_is_not_used():
    form = FORM.replace("</form>", '<label for="ph">Phone *</label><input id="ph" type="tel" name="phone" required></form>')
    m = G.map_form(guide(form), "https://x.org/p")
    assert not m["ok"] and any("cannot fill" in p for p in m["problems"])
    assert not G.map_form("<form><input name='q'><input name='x'><input name='y'></form>", "https://x.org")["ok"], "no text box for the article"


def test_a_confirm_email_box_is_filled_too():
    form = FORM.replace("</form>", '<label for="e2">Confirm email *</label><input id="e2" type="email" name="email-2" required></form>')
    assert G.map_form(guide(form), "https://x.org/p")["ok"]


# ------------------------------------------------------------------ the strict check
def test_a_website_that_clearly_accepts_outside_articles_and_links_is_qualified_with_its_evidence(store, cfg):
    result, job = checked(store, cfg, guide())
    assert result == "qualified" and job["status"] == S.QUALIFIED and job["adapter"] == "guest"
    g = job["guest"]
    assert g["accepts_quote"] == ACCEPTS and g["links_quote"] == LINKS and g["captcha"] is False and g["relevance"] == 8
    assert g["form_url"] == job["page_url"] and g["words"] == [600, 1200]


def test_a_captcha_form_is_qualified_as_needing_the_owners_click(store, cfg):
    result, job = checked(store, cfg, guide(captcha=True))
    assert result == "qualified" and job["guest"]["captcha"] is True


@pytest.mark.parametrize("change,reason", [
    ({"accepts_quote": "We publish guest posts from anyone at all."}, "accepts articles from outside authors"),   # an invented quote
    ({"accepts_outside_articles": False}, "accepts articles from outside authors"),
    ({"links_allowed": False}, "link to their own website"),
    ({"links_quote": "Links are welcome everywhere."}, "link to their own website"),   # invented
    ({"paid": True}, "charges for publishing"),
    ({"min_words": 3000}, "length limits"),
])
def test_without_clear_evidence_the_website_is_never_used(store, cfg, change, reason):
    judge = Judge(cfg)
    judge.acceptance = {**judge.acceptance, **change}
    result, job = checked(store, cfg, guide(), judge)
    assert result == "rejected" and reason in job["history"][-1]["note"]


def test_an_on_topic_website_that_does_not_accept_articles_is_never_shown(store, cfg):
    info = "<html><head><title>How to write a book</title></head><body><h1>Planning your book</h1><p>Tips on outlining.</p><p>" + "More about planning a book. " * 30 + "</p><script>var captcha=1</script></body></html>"
    result, job = checked(store, cfg, info)
    assert result == "rejected" and "no submission form" in job["history"][-1]["note"]


def test_an_email_website_is_used_only_when_the_check_names_an_address_shown_on_the_page(store, cfg):
    html = f"<html><body><h1>Write for us</h1><p>{ACCEPTS} {LINKS}</p><a href='mailto:editor@x.org'>Email the editor</a><p>" + "Pitch us a topic first. " * 30 + "</p></body></html>"
    result, job = checked(store, cfg, html)   # the check names no address
    assert result == "rejected" and "no clear email address" in job["history"][-1]["note"]


def test_a_weak_fit_is_not_used(store, cfg):
    result, job = checked(store, cfg, guide(), Judge(cfg, relevance=5))
    assert result == "rejected" and "fit" in job["history"][-1]["note"]


@pytest.mark.parametrize("domain", ["bookrival.example.com", "facebook.com", "medium.com", "example-company.org"])
def test_competitors_social_networks_open_platforms_and_the_do_not_contact_list_are_never_used(store, cfg, domain):
    result, job = checked(store, cfg, guide(), domain=domain)
    assert result == "rejected" and job["history"][-1]["note"].startswith("not used:")


def test_the_guidelines_page_linked_from_the_form_page_counts_as_evidence(store, cfg):
    html = guide(text="Send us your article.").replace("<h1>Write for us</h1>", '<h1>Write for us</h1><a href="/guest-post-guidelines">Read our guest post guidelines</a>')
    rules = f"<html><body><h1>Guidelines</h1><p>{ACCEPTS}</p><p>{LINKS}</p></body></html>"
    result, job = checked(store, cfg, html, extra={"https://writershub.example.org/guest-post-guidelines": rules})
    assert result == "qualified"


def test_a_website_is_considered_once_ever(store, cfg):
    checked(store, cfg, guide())
    other = candidate(store, path="contribute")
    assert G.check_candidate(store, other, cfg, EFF, SETTINGS, Judge(cfg), "Tavily", FakeWeb({other["page_url"]: guide()}), [0]) == "rejected"
    assert "already considered" in other["history"][-1]["note"]


def test_when_gemini_is_down_the_website_waits_for_the_next_run(store, cfg):
    result, job = checked(store, cfg, guide(), Judge(cfg, down=True))
    assert result == "llm_down" and job["status"] == S.DISCOVERED


def test_quotes_are_matched_word_for_word_but_ignore_spacing_and_curly_quotes():
    assert G.quote_found("We welcome guest posts", "Hello.  We   welcome guest posts from writers")
    assert G.quote_found("Don’t forget your bio link", "Don't forget your bio link here")
    assert not G.quote_found("short", "short text")
    assert not G.quote_found("We welcome everyone", "We welcome guest posts")


# ------------------------------------------------------------------ writing and sending
def qualified(store, cfg, captcha=False, domain="writershub.example.org"):
    result, job = checked(store, cfg, guide(captcha=captcha), domain=domain)
    assert result == "qualified"
    return job


class Sender:
    def __init__(self, outcome="sent"):
        self.calls, self.outcome = [], outcome

    def __call__(self, url, payload, shot):
        self.calls.append((url, payload))
        return {"outcome": self.outcome, "detail": "Thank you, your article was received."}


def test_review_mode_writes_the_articles_but_sends_nothing(store, cfg):
    job = qualified(store, cfg)
    sender, report = Sender(), []
    G.write_and_send(store, {**cfg, "guest_posts": {**cfg["guest_posts"], "submit": False}}, SITE, Judge(cfg), report, sender)
    assert job["status"] == S.CONTENT_READY and sender.calls == [] and any("NOT sent (review mode" in r for r in report)
    assert job["topic_key"] and job["title"] and job["anchor_text"]


def live(cfg):
    return {**cfg, "guest_posts": {**cfg["guest_posts"], "submit": True}}


def test_a_form_without_captcha_is_filled_and_sent_with_the_owners_details(store, cfg):
    job = qualified(store, cfg)
    sender = Sender()
    G.write_and_send(store, live(cfg), SITE, Judge(cfg), [], sender)
    assert job["status"] == S.SUBMITTED and job["guest"]["how"] == "automatically" and job["guest"]["sent_on"] == store.today()
    url, payload = sender.calls[0]
    values = {f["role"]: f["value"] for f in payload["fields"]}
    assert url == job["guest"]["form_url"] and payload["back"].endswith(f"?sent={job['id']}")
    assert values["name"] == "Genbooks team" and values["email"] == "support@genbooks.example" and values["title"] == job["title"]
    assert "](" not in values["article"] and "https://genbooks.uk/" in values["article"], "plain text, the link written out"
    assert values["bio"].startswith("The Genbooks team runs Genbooks (https://genbooks.uk/)") and values["consent"] is True
    assert "newsletter" not in values


def test_a_captcha_form_becomes_a_click_link_that_carries_every_value(store, cfg):
    job = qualified(store, cfg, captcha=True)
    sender = Sender()
    G.write_and_send(store, live(cfg), SITE, Judge(cfg), [], sender)
    assert sender.calls == [] and job["status"] == S.PENDING_APPROVAL and job["guest"]["link_on"] == store.today()
    base, data = job["guest"]["link"].split("#bl=")
    payload = json.loads(base64.urlsafe_b64decode(data + "=" * (-len(data) % 4)).decode("utf-8"))
    assert base == job["guest"]["form_url"] and payload["form"]["id"] == "gp" and {f["role"] for f in payload["fields"]} >= {"name", "email", "article", "bio"}


def test_never_more_than_the_daily_number_and_each_on_a_different_website(store, cfg):
    jobs = [qualified(store, cfg, domain=f"site{i}.example.org") for i in range(4)]
    sender = Sender()
    G.write_and_send(store, live(cfg), SITE, Judge(cfg), [], sender)
    assert len(sender.calls) == 2 and sum(j["status"] == S.SUBMITTED for j in jobs) == 2
    G.write_and_send(store, live(cfg), SITE, Judge(cfg), [], sender)
    assert len(sender.calls) == 2, "not again the same day"
    assert len({u for u, _ in sender.calls}) == 2


@pytest.mark.parametrize("outcome,status", [("blocked", S.PENDING_APPROVAL), ("captcha", S.PENDING_APPROVAL), ("unclear", S.SUBMITTED),
                                            ("error", S.REJECTED), ("not_filled", S.REJECTED), ("unavailable", S.RETRY_REQUIRED)])
def test_every_sending_outcome_is_handled_and_nothing_is_ever_sent_twice(store, cfg, outcome, status):
    job = qualified(store, cfg)
    G.write_and_send(store, live(cfg), SITE, Judge(cfg), [], Sender(outcome))
    assert job["status"] == status
    if outcome == "unclear":
        assert job["guest"]["sent_on"], "it may have gone through: it counts as sent and is never resent"


# ------------------------------------------------------------------ the owner's click, publication and the dashboard
def test_the_owners_sent_report_and_a_published_address_are_applied_and_the_link_is_verified(store, cfg):
    job = qualified(store, cfg, captcha=True)
    G.write_and_send(store, live(cfg), SITE, Judge(cfg), [], Sender())
    G.status_path(store).write_text(json.dumps({job["id"]: {"status": "sent", "at": store.now_iso()}}), encoding="utf-8")
    G.apply_owner_reports(store, cfg, [])
    assert job["status"] == S.SUBMITTED and job["guest"]["how"] == "by you (CAPTCHA)"
    url = "https://writershub.example.org/blog/my-article"
    web = FakeWeb({url: page(link(job["target_url"], job["anchor_text"]), title=job["title"]), job["target_url"]: "ok"})
    G.status_path(store).write_text(json.dumps({job["id"]: {"status": "published", "published_url": url}}), encoding="utf-8")
    G.apply_owner_reports(store, cfg, [], web)
    assert job["status"] == S.VERIFIED and job["link_attributes"]["counts_for_seo"] is True


def test_publication_is_found_in_the_sitemap_and_unanswered_sends_expire(store, cfg, clock):
    job = qualified(store, cfg)
    G.write_and_send(store, live(cfg), SITE, Judge(cfg), [], Sender())
    slug = "-".join(w for w in job["title"].lower().replace(":", "").replace("?", "").split())
    url = f"https://writershub.example.org/blog/{slug}/"
    web = FakeWeb({"https://writershub.example.org/sitemap.xml": f"<urlset><url><loc>{url}</loc></url></urlset>",
                   url: page(link(job["target_url"], job["anchor_text"]), title=job["title"]), job["target_url"]: "ok"})
    clock.advance(days=3)
    G.track(store, cfg, [], web)
    assert job["status"] == S.VERIFIED and job["publication_url"] == url
    other = qualified(store, cfg, domain="quiet.example.org")
    clock.advance(days=1)
    G.write_and_send(store, live(cfg), SITE, Judge(cfg), [], Sender())
    clock.advance(days=61)
    G.track(store, cfg, [], FakeWeb({}))
    assert other["status"] == S.REJECTED and "not published within 60 days" in other["history"][-1]["note"]


def test_the_old_platforms_jobs_become_history_and_old_set_aside_pages_are_checked_again(store):
    old, _ = store.add_job("d1", "devto", "dev.to", "https://dev.to/", "configured topic")
    web = candidate(store, "old.example.org")
    store.transition(web, S.UNSUPPORTED_PUBLICATION_METHOD, "captcha required (old rules)")
    G.migrate(store, [])
    assert old["legacy"] is True and web["status"] == S.DISCOVERED and web["guest_requeued"]
    store.transition(web, S.REJECTED, "checked again")
    G.migrate(store, [])
    assert web["status"] == S.REJECTED, "once only"


def test_the_dashboard_items_show_the_evidence_and_the_link_only_while_waiting(store, cfg):
    job = qualified(store, cfg, captcha=True)
    G.write_and_send(store, live(cfg), SITE, Judge(cfg), [], Sender())
    (item,) = G.dashboard_items(store)
    assert item["status"] == "waiting" and item["link"].startswith(job["guest"]["form_url"] + "#bl=") and item["accepts_quote"] == ACCEPTS
    G.status_path(store).write_text(json.dumps({job["id"]: {"status": "sent"}}), encoding="utf-8")
    G.apply_owner_reports(store, cfg, [])
    (item,) = G.dashboard_items(store)
    assert item["status"] == "sent" and item["link"] is None


def test_the_daily_run_finds_checks_writes_and_reports_without_the_old_platforms(store, cfg):
    from test_pipeline import Provider, SETTINGS as RUN_SETTINGS
    job = candidate(store)
    web = FakeWeb({job["page_url"]: guide()})
    site = {**SITE, "restrict_to_uk": False, "discovery_keywords": ["writing a book"], "competitor_terms": []}
    result = pipeline.run(store=store, cfg=cfg, adapters={}, llm=Judge(cfg), fetch=web, engine="Firecrawl", provider=Provider(), site=site,
                          settings=RUN_SETTINGS, write_logs=False)
    assert job["status"] == S.CONTENT_READY, "review mode: checked and written, not sent"
    assert "QUALIFIED" in result["report"] and "NOT sent (review mode" in result["report"]


# ------------------------------------------------------------------ the browser side, with the data Python prepares
import shutil as _shutil
import subprocess as _subprocess
from pathlib import Path as _Path

_ROOT = _Path(__file__).resolve().parent.parent
_NODE = _shutil.which("node")
_JSDOM = (_ROOT / "dashboard" / "node_modules" / "jsdom").exists()
needs_browser_sim = pytest.mark.skipif(not (_NODE and _JSDOM), reason="node and jsdom are needed")


@needs_browser_sim
def test_the_data_python_prepares_fills_the_real_form_in_the_browser_code(store, cfg):
    job = qualified(store, cfg)
    G.write_and_send(store, cfg, SITE, Judge(cfg), [], Sender())          # review mode: the article is written
    payload = G.payload_for(store, job, cfg, SITE)
    done = _subprocess.run([_NODE, str(_ROOT / "extension" / "fill_cli.js")], input=json.dumps({"html": guide(), "payload": payload}),
                           capture_output=True, text=True, encoding="utf-8", timeout=60)
    assert done.returncode == 0, done.stderr
    out = json.loads(done.stdout)
    assert out["report"]["missing"] == [] and out["report"]["filled"] == len(payload["fields"])
    v = out["values"]
    assert v["your-name"] == "Genbooks team" and v["your-email"] == "support@genbooks.example" and v["post-title"] == job["title"]
    assert "https://genbooks.uk/" in v["your-message"] and v["author-bio"].startswith("The Genbooks team") and v["agree"] is True
    assert v["hp-field"] == "" and v["newsletter"] is False, "the honeypot and the newsletter box are left alone"


@needs_browser_sim
def test_the_extension_checks_pass():
    done = _subprocess.run([_NODE, str(_ROOT / "extension" / "test_extension.js")], capture_output=True, text=True, encoding="utf-8", timeout=120)
    assert done.returncode == 0, done.stdout[-1500:] + done.stderr[-500:]


# ------------------------------------------------------------------ the real headless browser against a local website
def _chromium_ready():
    try:
        from playwright.sync_api import sync_playwright
        with sync_playwright() as p:
            p.chromium.launch().close()
        return True
    except Exception:  # noqa: BLE001
        return False


def _local_site(form_html):
    import threading
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
    from urllib.parse import parse_qs
    got = {}

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def do_GET(self):
            body = form_html.encode("utf-8")
            self.send_response(200); self.send_header("Content-Type", "text/html; charset=utf-8"); self.end_headers(); self.wfile.write(body)

        def do_POST(self):
            n = int(self.headers.get("Content-Length") or 0)
            got.update({k: v[0] for k, v in parse_qs(self.rfile.read(n).decode("utf-8")).items()})
            self.send_response(200); self.send_header("Content-Type", "text/html; charset=utf-8"); self.end_headers()
            self.wfile.write(b"<html><body><p>Thank you, your article was received. We will review it.</p></body></html>")

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server, got


@pytest.mark.skipif(not _chromium_ready(), reason="Playwright's Chromium is not installed here")
def test_the_real_headless_browser_fills_and_sends_a_guest_post_form(store, cfg, tmp_path):
    from src.backlinks import browser
    job = qualified(store, cfg)
    G.write_and_send(store, cfg, SITE, Judge(cfg), [], Sender())          # review mode: the article is written
    payload = G.payload_for(store, job, cfg, SITE)
    server, got = _local_site(guide())
    try:
        browser._ready = True                                              # Chromium is already installed on this machine
        res = browser.submit(f"http://127.0.0.1:{server.server_port}/write-for-us", payload, tmp_path / "shot.png")
    finally:
        server.shutdown()
    assert res["outcome"] == "sent" and "Thank you" in res["detail"] and (tmp_path / "shot.png").exists()
    assert got["your-name"] == "Genbooks team" and got["your-email"] == "support@genbooks.example" and got["post-title"] == job["title"]
    assert "https://genbooks.uk/" in got["your-message"] and got["author-bio"].startswith("The Genbooks team") and got["agree"] == "on"
    assert "newsletter" not in got and got.get("hp-field", "") == "", "the website received no newsletter opt-in and an empty honeypot"


@pytest.mark.skipif(not _chromium_ready(), reason="Playwright's Chromium is not installed here")
def test_the_real_browser_never_sends_a_form_that_shows_a_captcha(store, cfg, tmp_path):
    from src.backlinks import browser
    job = qualified(store, cfg)
    G.write_and_send(store, cfg, SITE, Judge(cfg), [], Sender())
    server, got = _local_site(guide(captcha=True))
    try:
        browser._ready = True
        res = browser.submit(f"http://127.0.0.1:{server.server_port}/write-for-us", G.payload_for(store, job, cfg, SITE), None)
    finally:
        server.shutdown()
    assert res["outcome"] == "captcha" and got == {}, "a CAPTCHA turns it into the owner's click; nothing reaches the website"


# ------------------------------------------------------------------ pitch (application) forms, name sub-fields, human checks
PITCH_FORM = ('<form id="wpforms-form-1" action="/guest-blogger/" method="post">'
              '<label>Name *</label><input type="text" name="wpforms[fields][0][first]" required><label>First</label>'
              '<input type="text" name="wpforms[fields][0][last]" required><label>Last</label>'
              '<label for="e1">Email *</label><input id="e1" type="email" name="wpforms[fields][1]" required>'
              '<label for="t1">What are some topics or interests you have that you would like to share with your readers? *</label><textarea id="t1" name="wpforms[fields][2]" required></textarea>'
              '<label for="b1">Tell us a little about your background related to your interests (education, experience, etc.) *</label><textarea id="b1" name="wpforms[fields][9]" required></textarea>'
              '<label for="l1">Provide any relevant links that will help us get to know you and your writing (previous blog posts or articles, professional websites) *</label><textarea id="l1" name="wpforms[fields][10]" required></textarea>'
              '<label for="h1">Humans Only Please! * 1 plus 4 equals?</label><input id="h1" type="text" name="wpforms[fields][11][a]" required>'
              '<input type="hidden" name="wpforms[recaptcha]" value=""><button type="submit">Submit</button></form>')


def test_an_application_form_is_recognised_with_name_parts_a_pitch_and_a_human_check_left_alone():
    m = G.map_form(guide(PITCH_FORM), "https://x.org/guest-blogger/")
    assert m["ok"] and m["kind"] == "pitch" and m["problems"] == []
    assert [(f["role"]) for f in m["fields"]] == ["first_name", "last_name", "email", "pitch", "bio", "links"]
    assert len(m["human"]) == 1 and m["human"][0]["name"] == "wpforms[fields][11][a]", "the human question is never answered by the system"
    assert all(f["name"] != "wpforms[fields][11][a]" for f in m["fields"])


def test_a_form_with_a_box_for_the_article_is_still_an_article_form():
    assert G.map_form(guide(), "https://x.org/p")["kind"] == "article"


def test_a_pitch_needs_no_gemini_and_offers_three_unused_ideas_to_the_editor(store, cfg):
    result, job = checked(store, cfg, guide(PITCH_FORM))
    assert result == "qualified" and job["guest"]["mapping"]["kind"] == "pitch"
    judge = Judge(cfg)
    G.write_and_send(store, live(cfg), SITE, judge, [], Sender())
    assert not any(c.startswith("Write an original") for c in judge.calls), "no article is written for an application"
    assert job["status"] == S.SUBMITTED or job["status"] == S.PENDING_APPROVAL
    payload = G.payload_for(store, job, cfg, SITE)
    values = {f["role"]: f["value"] for f in payload["fields"]}
    assert values["first_name"] == "The" and values["last_name"] == "Genbooks team" or values["first_name"]
    assert values["pitch"].count("\n- ") == 3 and "not published elsewhere" in values["pitch"] and values["links"] == "https://genbooks.uk/"
    assert payload["wait_for"][0]["name"] == "wpforms[fields][11][a]", "the extension must wait for the person to answer"
    assert "article" not in values


def test_a_human_check_is_described_to_the_person_in_plain_words():
    m = G.map_form(guide(PITCH_FORM), "https://x.org/guest-blogger/")
    assert m["human"][0]["label"] == "Humans Only Please! * 1 plus 4 equals?".replace("* ", "").strip() or "equals" in m["human"][0]["label"].lower()
    assert "wpforms" not in m["human"][0]["label"] and "field_" not in m["human"][0]["label"]


def test_the_pitch_offers_the_ideas_that_fit_the_website_best_and_makes_no_claim_about_its_readers(store, cfg):
    result, job = checked(store, cfg, guide(PITCH_FORM))
    job["guest"]["topics"] = "cover design and word count"
    text = G.pitch_text(cfg, job, store, "Jane Smith")
    ideas = [l[2:] for l in text.splitlines() if l.startswith("- ")]
    assert len(ideas) == 3 and "for your readers" in text and "developers" not in text
    first = max(cfg["topics"], key=lambda t: len({"cover", "design", "word", "count"} & set(__import__("re").findall(r"[a-z]{3,}", (t["title_hint"] + " " + t["angle"]).lower()))))
    assert first["title_hint"] in ideas, "the idea closest to what the website wants comes first in the offer"


def test_the_websites_own_name_is_used_in_a_pitch_not_the_page_title():
    assert G.site_name_of('<meta property="og:site_name" content="AllPsych">', "example-psych.org", "Guest Blogger - AllPsych") == "AllPsych"
    assert G.site_name_of("<html></html>", "writershub.example.org", "Write for us - Writers Hub") == "Writers Hub"
    assert G.site_name_of("<html></html>", "www.example.org", "Write for us") == "example.org"
    assert G.site_name_of("<html></html>", "example.org", "Guest Posting Guidelines | The Calm Blog") == "The Calm Blog"


# ------------------------------------------------------------------ Gemini's instructions, the search and the safety nets
class Recorder(Judge):
    """A Judge that keeps every full prompt, to read exactly what Gemini is told."""

    def __init__(self, cfg, **kw):
        super().__init__(cfg, **kw)
        self.prompts = []

    def generate(self, prompt):
        self.prompts.append(prompt)
        return super().generate(prompt)


def test_gemini_is_told_who_writes_for_whom_and_how_an_editor_wants_it(store, cfg):
    job = qualified(store, cfg)
    judge = Recorder(cfg)
    G.write_and_send(store, cfg, SITE, judge, [], Sender())
    prompt = next(p for p in judge.prompts if p.startswith("Write an original"))
    assert "for the readers of Writers Hub, in particular" in prompt and "Writers Hub (writershub.example.org)" in prompt
    assert "The author is Genbooks team." in prompt and cfg["guest_posts"]["brief"][:60] in prompt
    assert "whose editors want: writing a book" in prompt and "No AI spam." in prompt, "the website's own wishes and rules"
    for rule in ("natural, human voice", "Open with the reader's real situation", "Never mention guest posting, SEO", "delve", "sentence case"):
        assert rule in prompt, rule
    assert "developer" not in prompt.lower() and "dev.to" not in prompt.lower()
    assert job["status"] == S.CONTENT_READY


def test_the_old_platform_prompt_is_unchanged_without_a_brief(cfg):
    from src.backlinks import content as C
    prompt = C.article_prompt(cfg["topics"][0], cfg["facts"], "an anchor", None, cfg["brand_name"], cfg["claims_rule"])
    assert "About the writer" not in prompt and "natural, human voice" not in prompt


def test_new_topic_ideas_are_for_the_niche_readers_never_for_programmers(cfg):
    from src.backlinks import topics as T
    prompt = T.topics_prompt(cfg, ["An old title"], 6)
    assert "dev.to" not in prompt and "never programmers or software developers" in prompt
    assert "other websites about writing a book" in prompt and cfg["guest_posts"]["brief"][:60] in prompt


@pytest.mark.parametrize("name", ["backlinks.yaml", "backlinks-social-field.yaml", "backlinks-genbooks.yaml"])
def test_every_site_has_a_writing_brief_niche_search_words_and_no_programmer_topics(name):
    c = shipped(name)
    gp = c["guest_posts"]
    assert len(gp["brief"]) > 300 and len(gp["search_words"]) >= 8 and not any("UK" in w for w in gp["search_words"])
    assert not [t["key"] for t in c["topics"] if "develop" in t["audience"].lower() or "programm" in t["audience"].lower()]
    assert c["discovery"]["new_candidates_per_run"] >= 20 and c["discovery"]["inspect_per_run"] >= 20


def test_a_page_that_never_mentions_links_is_turned_down_without_asking_gemini(store, cfg):
    judge = Judge(cfg)
    no_bio = FORM.replace('<label for="b">Author bio</label><textarea id="b" name="author-bio"></textarea>', "")
    result, job = checked(store, cfg, guide(form=no_bio, text=ACCEPTS), judge)
    assert result == "rejected" and "link to their own website" in job["history"][-1]["note"] and judge.calls == []


def test_the_fit_is_asked_only_after_the_acceptance_check_passes(store, cfg):
    judge = Judge(cfg)
    judge.acceptance = {**judge.acceptance, "links_allowed": False}
    result, _ = checked(store, cfg, guide(), judge)
    assert result == "rejected" and len(judge.calls) == 1, "one Gemini request for a website that fails the acceptance check"
    judge2 = Judge(cfg)
    assert checked(store, cfg, guide(), judge2, domain="other.example.org")[0] == "qualified" and len(judge2.calls) == 2


def test_the_search_uses_the_niche_words_from_any_country(store, cfg):
    seen = {}

    class Provider:
        MAX_SEARCH_CALLS_PER_RUN = 3

        def discover(self, keywords, query_templates, max_results, restrict_to_uk=False, known_urls=None, start_index=0):
            seen.update(keywords=keywords, templates=query_templates, uk=restrict_to_uk, max_results=max_results)
            return [{"url": "https://mindblog.example.com/write-for-us", "title": "Write for us", "snippet": "We accept guest posts about writing",
                     "matched_query": '"write for us" writing'}], 1

    out = G.find(store, {**SITE, "restrict_to_uk": True}, {"discovery": {"query_templates": ["x"]}}, cfg, Provider(), EFF, set())
    assert seen["keywords"] == cfg["guest_posts"]["search_words"] and seen["templates"] == G.TEMPLATES and seen["uk"] is False
    assert seen["max_results"] == cfg["discovery"]["new_candidates_per_run"] and out["added"] == 1


def test_a_link_from_an_earlier_day_that_the_owner_sends_today_does_not_take_one_of_todays_places(store, cfg, clock):
    old = qualified(store, cfg, captcha=True, domain="old.example.org")
    G.write_and_send(store, live(cfg), SITE, Judge(cfg), [], Sender())
    assert old["status"] == S.PENDING_APPROVAL
    clock.advance(days=1)
    G.status_path(store).write_text(json.dumps({old["id"]: {"status": "sent", "at": store.now_iso()}}), encoding="utf-8")
    G.apply_owner_reports(store, cfg, [])
    assert old["status"] == S.SUBMITTED and G._sent_today(store) == 0
    jobs = [qualified(store, cfg, domain=f"new{i}.example.org") for i in range(3)]
    sender = Sender()
    G.write_and_send(store, live(cfg), SITE, Judge(cfg), [], sender)
    assert len(sender.calls) == 2 and sum(j["status"] == S.SUBMITTED for j in jobs) == 2


def test_failed_sends_never_make_it_write_article_after_article(store, cfg):
    jobs = [qualified(store, cfg, domain=f"refuses{i}.example.org") for i in range(6)]
    report = []
    G.write_and_send(store, live(cfg), SITE, Judge(cfg), report, Sender("error"))
    assert sum(1 for j in jobs if j.get("written_on") == store.today()) == 4 and any("enough articles were written today" in r for r in report)


def test_a_run_that_stopped_while_sending_never_sends_the_same_form_twice(store, cfg, clock):
    from src.backlinks import watchdog
    job = qualified(store, cfg)
    G.write_and_send(store, cfg, SITE, Judge(cfg), [], Sender())   # review mode: the article is ready
    store.transition(job, S.SUBMISSION_IN_PROGRESS, "sending")
    clock.advance(hours=2)
    watchdog.recover_stuck(store, {}, cfg)
    assert job["status"] == S.SUBMITTED and job["guest"]["outcome"] == "unclear"
    sender = Sender()
    G.write_and_send(store, live(cfg), SITE, Judge(cfg), [], sender)
    assert sender.calls == []


def test_pages_found_by_a_write_for_us_search_are_checked_before_old_leftovers(store, cfg):
    old = candidate(store, "oldlist.example.org", "useful-links")
    old["discovery_source"] = "tavily: \"useful links\" writing"
    new = candidate(store, "newblog.example.org")
    new["discovery_source"] = "tavily: \"write for us\" writing"
    web = FakeWeb({new["page_url"]: guide()})
    G.check_pass(store, {**cfg, "discovery": {**cfg["discovery"], "inspect_per_run": 1}}, EFF, SETTINGS, Judge(cfg), "Tavily", web, [])
    assert new["status"] == S.QUALIFIED and old["status"] == S.DISCOVERED


def test_the_daily_run_works_exactly_as_github_starts_it(monkeypatch):
    """pipeline.main() with nothing supplied by the test except the internet: the real config, the real store location,
    the real adapters (none), the real Gemini object and the real Tavily module (its network call faked). The 2026-11-01
    test run on GitHub failed here (KeyError: 'platforms') while every test that supplied its own adapters passed."""
    from conftest import FakeWeb
    from src import tavily_discovery
    from src.backlinks import browser, discover, llm as llm_mod
    from src import state as st
    configs = [{**c, "enabled": True, "guest_posts": {**c["guest_posts"], "submit": True}}
               for c in (shipped(n) for n in ("backlinks.yaml", "backlinks-social-field.yaml", "backlinks-genbooks.yaml"))]
    by_state = {c["state_dir"]: c for c in configs}
    url = "https://mindwell.example.com/write-for-us"
    web = FakeWeb({url: guide(captcha=True)})
    searches, made = [], []
    limits = []
    monkeypatch.setattr(tavily_discovery, "discover", lambda keywords, query_templates, max_results, restrict_to_uk=False, known_urls=None, start_index=0, max_calls=None: (
        limits.append(max_calls) or searches.append((keywords, restrict_to_uk)) or [{"url": url, "title": "Write for us", "snippet": "Guest posts about anxiety, wellbeing, sports, events, writing and books",
                                                        "matched_query": '"write for us" mental health'}], 1))
    monkeypatch.setattr(discover, "tavily_remaining", lambda: None)
    monkeypatch.setattr(browser, "submit", lambda *a, **k: pytest.fail("a CAPTCHA form must never be sent by the automatic browser"))

    class RealLLM(llm_mod.LLM):   # the real constructor with run()'s own arguments; only the answers are faked
        def __init__(self, store, *a, **k):
            super().__init__(store, *a, **k)
            made.append(k)
            self.judge = Judge(by_state[store.root.name])

        def generate(self, prompt):
            return self.judge.generate(prompt)

    monkeypatch.setattr(pipeline, "LLM", RealLLM)
    monkeypatch.setattr(pipeline, "load_configs", lambda: configs)
    real_settings = pipeline.load_settings
    monkeypatch.setattr(pipeline, "load_settings", lambda: {**real_settings(), "guest_rehearsal": ""})   # the one-off rehearsal has its own tests
    real_run = pipeline.run
    monkeypatch.setattr(pipeline, "run", lambda cfg: real_run(cfg=cfg, fetch=web))
    pipeline.main()
    dash = json.loads((st.STATE_DIR / "dashboard.json").read_text(encoding="utf-8"))
    assert dash["publishing_issues"] == [], dash["publishing_issues"]
    assert sorted(w for w, _ in searches) == sorted(c["guest_posts"]["search_words"] for c in configs) and not any(uk for _, uk in searches)
    assert len(made) == 3 and limits == [8, 8, 8], "the real run passes each site's daily limit to the search"
    for c in configs:
        items = dash["backlink_publishing"][c["site_id"]]["guest_posts"]
        assert len(items) == 1 and items[0]["domain"] == "mindwell.example.com" and items[0]["status"] == "waiting" and "#bl=" in items[0]["link"], c["site_id"]
        logs = list((pipeline.LOGS_DIR / c["log_dir"]).glob("run-*.md"))
        assert len(logs) == 1 and "ready for your click" in logs[0].read_text(encoding="utf-8"), c["site_id"]


# ------------------------------------------------------------------ the owner's daily limits
class Meter:
    """A search service that always uses as many searches as it is allowed (up to 5 per run), and records what it was allowed."""
    MAX_SEARCH_CALLS_PER_RUN = 16

    def __init__(self):
        self.allowed = []

    def discover(self, keywords, query_templates, max_results, restrict_to_uk=False, known_urls=None, start_index=0, max_calls=None):
        self.allowed.append(max_calls)
        return [], min(5, self.MAX_SEARCH_CALLS_PER_RUN if max_calls is None else max_calls)


def test_the_daily_search_limit_holds_however_many_times_the_workflow_runs_in_a_day(store, cfg, clock):
    assert cfg["quotas"]["tavily_searches_per_day"] == 8
    meter = Meter()
    outs = [G.find(store, SITE, {"discovery": {"query_templates": ["x"]}}, cfg, meter, EFF, set()) for _ in range(4)]
    assert store.used_today("tavily") == 8, "5 + 3, then nothing more today"
    assert meter.allowed == [8, 3] and "daily limit of 8" in outs[2]["skipped"] and "daily limit of 8" in outs[3]["skipped"]
    clock.advance(days=1)
    G.find(store, SITE, {"discovery": {"query_templates": ["x"]}}, cfg, meter, EFF, set())
    assert store.used_today("tavily") == 5 and meter.allowed[-1] == 8, "a new day starts with the full allowance again"


def test_the_daily_firecrawl_limit_holds_however_many_times_the_workflow_runs_in_a_day(store, cfg, monkeypatch):
    from src.backlinks import discover
    scraped = []
    monkeypatch.setattr(discover, "firecrawl_scrape_html", lambda url: scraped.append(url) or None)
    monkeypatch.setattr(discover, "firecrawl_remaining", lambda: None)
    c = {**cfg, "discovery": {**cfg["discovery"], "firecrawl_scrapes_per_run": 4}}
    assert c["quotas"]["firecrawl_scrapes_per_day"] == 4
    for i in range(6):
        candidate(store, f"unreadable{i}.example.org")
    web = FakeWeb({f"https://unreadable{i}.example.org/write-for-us": "<html></html>" for i in range(6)})
    for _ in range(3):   # the workflow runs three times today
        G.check_pass(store, c, EFF, SETTINGS, Judge(cfg), "Firecrawl", web, [])
    assert len(scraped) == 4 and store.used_today("firecrawl") == 4


def test_the_daily_limits_keep_all_three_sites_inside_the_free_monthly_allowance():
    configs = [shipped(n) for n in ("backlinks.yaml", "backlinks-social-field.yaml", "backlinks-genbooks.yaml")]
    assert sum(c["quotas"]["tavily_searches_per_day"] for c in configs) * 31 <= 900, "Tavily: 1,000 free credits a month, 100 kept in reserve"
    assert sum(c["quotas"]["firecrawl_scrapes_per_day"] for c in configs) * 31 <= 900, "Firecrawl: used every day (owner, 2026-11-08), 1,000 credits a month, 100 kept in reserve"
    assert all(c["discovery"]["new_candidates_per_run"] == 25 and c["discovery"]["inspect_per_run"] == 25 for c in configs)


# ------------------------------------------------------------------ the dress rehearsal
def _rehearsal_site():
    return {**SITE, "contact": {"name": "The Genbooks team", "email": "support@genbooks.example"}}


@pytest.mark.skipif(not _chromium_ready(), reason="Playwright's Chromium is not installed here")
def test_the_dress_rehearsal_writes_an_article_and_the_real_browser_sends_it_into_a_local_form(cfg, tmp_path):
    from src.backlinks.store import Store
    result = G.rehearse(cfg, _rehearsal_site(), Store(tmp_path / "real"), Judge(cfg), tmp_path / "logs")
    assert result["ok"], result["problems"]
    assert result["outcome"] == "sent" and result["words"] > 300 and result["title"]
    log = next((tmp_path / "logs").glob("rehearsal-*.md")).read_text(encoding="utf-8")
    assert "PASSED" in log and result["title"] in log and "Nothing was sent to any real website" in log and "your-message" in log


def test_a_rehearsal_that_cannot_write_or_send_says_so_and_never_raises(cfg, tmp_path):
    from src.backlinks.store import Store
    down = G.rehearse(cfg, _rehearsal_site(), Store(tmp_path / "a"), Judge(cfg, down=True), tmp_path / "logs")
    assert not down["ok"] and "article was not written" in down["problems"][0]
    refused = G.rehearse(cfg, _rehearsal_site(), Store(tmp_path / "b"), Judge(cfg), tmp_path / "logs",
                         sender=lambda url, payload, shot: {"outcome": "unavailable", "detail": "the browser could not be installed"})
    assert not refused["ok"] and "did not send the form: unavailable" in refused["problems"][0]


def test_the_daily_run_does_the_rehearsal_only_when_switched_on_and_shows_a_failure(monkeypatch):
    seen = []
    cfg = shipped("backlinks.yaml")
    monkeypatch.setattr(pipeline, "load_configs", lambda: [cfg])
    monkeypatch.setattr(pipeline, "_rehearsal", lambda c: seen.append(c["site_id"]) or [{"site_id": c["site_id"], "problems": ["the dress rehearsal failed: x"]}])
    monkeypatch.setattr(pipeline, "run", lambda cfg: None)
    monkeypatch.setattr(pipeline, "load_settings", lambda: {})
    pipeline.main()
    assert seen == []
    monkeypatch.setattr(pipeline, "load_settings", lambda: {"guest_rehearsal": "hypnotreatment-uk"})
    pipeline.main()
    from src import state as st
    assert seen == ["hypnotreatment-uk"]
    assert json.loads((st.STATE_DIR / "dashboard.json").read_text(encoding="utf-8"))["publishing_issues"][0]["problems"] == ["the dress rehearsal failed: x"]


def test_the_real_search_module_stops_at_the_calls_it_is_allowed(monkeypatch):
    from src import tavily_discovery as T
    asked = []
    monkeypatch.setattr(T, "SEARCH_CALL_DELAY_SECONDS", 0)
    monkeypatch.setattr(T, "_search_with_retry", lambda query, num=10: asked.append(query) or [{"url": f"https://site{len(asked)}-{i}.example.org/x", "title": "t", "snippet": "s"} for i in range(10)])
    found, calls = T.discover(["a", "b", "c"], ['"write for us" {keyword}', '"guest post" {keyword}'], max_results=500, max_calls=2)
    assert calls == 2 and len(asked) == 2 and len(found) == 20
    asked.clear()
    found, calls = T.discover(["a", "b", "c"], ['"write for us" {keyword}'], max_results=500)
    assert calls == 3, "without a limit it uses what it needs, up to its own cap"


# ------------------------------------------------------------------ forms that are never used (found by the owner on 2026-11-02)
COMMENT_BOX = ('<div id="respond"><h3>Leave a Reply</h3><form id="commentform" class="comment-form" action="https://writershub.example.org/wp-comments-post.php" method="post">'
               '<textarea name="comment" required></textarea><input name="author" required><input type="email" name="email" required>'
               '<div class="g-recaptcha" data-sitekey="k"></div><button type="submit">Post Comment</button></form></div>')
FOOTER_CONTACT = ('<div class="extra-footer-links"><div id="contact-us"><h3>Contact us</h3><form action="/contact" method="post"><input name="your-name" required>'
                  '<input type="email" name="your-email" required><textarea name="your-message" required></textarea><button>Send</button></form></div></div>')


def test_the_blogs_comment_box_and_the_footer_contact_form_are_never_used(store, cfg):
    judge = Judge(cfg)
    result, job = checked(store, cfg, guide(form="").replace("</body>", COMMENT_BOX + FOOTER_CONTACT + "</body>"), judge)
    assert result == "rejected" and "no submission form" in job["history"][-1]["note"] and judge.calls == []


def test_a_website_whose_form_is_an_outside_google_form_is_skipped_with_that_reason(store, cfg):
    link_to_form = "<p>Ready to submit? <a href='https://docs.google.com/forms/d/abc/viewform'>Fill out this form</a>, and we will be in touch.</p>"
    result, job = checked(store, cfg, guide(form=link_to_form).replace("</body>", COMMENT_BOX + FOOTER_CONTACT + "</body>"))
    assert result == "rejected" and "outside form (docs.google.com)" in job["history"][-1]["note"]


def test_a_captcha_in_the_comment_box_does_not_make_the_guest_form_need_a_click(store, cfg):
    result, job = checked(store, cfg, guide().replace("</body>", COMMENT_BOX + "</body>"))
    assert result == "qualified" and job["guest"]["captcha"] is False, "the CAPTCHA belongs to the comment box, not to the guest-post form"
    result, job = checked(store, cfg, guide(captcha=True), domain="own.example.org")
    assert result == "qualified" and job["guest"]["captcha"] is True, "a CAPTCHA inside the guest-post form itself still needs the owner"


FIELDS_OK = ('<label for="n">Full name *</label><input id="n" name="fullName" required><label for="e">Email *</label><input id="e" type="email" name="email" required>'
             '<label for="a">Your article *</label><textarea id="a" name="article" required></textarea><label for="w">Website</label><input id="w" type="url" name="website">')


def test_a_trap_field_hidden_by_the_pages_styles_is_never_filled_and_the_real_one_is():
    m = G.map_form(f'<html><body><form id="f" action="/x"><div class="hidden"><input name="website_url_check"></div>{FIELDS_OK}</form></body></html>', "https://x.example.org/write")
    roles = {f["name"]: f["role"] for f in m["fields"]}
    assert m["ok"] and "website_url_check" not in roles and roles["website"] == "website" and all(f["required"] in (True, False) for f in m["fields"])
    wrapped = G.map_form(f'<html><body><div aria-hidden="true" class="hidden"><form id="f" action="/x">{FIELDS_OK}</form></div></body></html>', "https://x.example.org/write")
    assert wrapped["ok"] and len(wrapped["fields"]) == 4, "the page's own wrappers outside the form do not hide its fields (the browser checks what is really shown)"


def test_a_required_box_drawn_by_the_pages_scripts_means_the_form_is_not_used():
    drawn = '<div><label>Topic Category *</label><select aria-hidden="true" required style="position:absolute;width:1px"><option value="a">A</option></select></div>'
    m = G.map_form(f'<html><body><form id="f" action="/x">{FIELDS_OK}{drawn}</form></body></html>', "https://x.example.org/write")
    assert not m["ok"] and any("Topic Category" in p for p in m["problems"])


def test_an_applications_title_box_gets_the_first_idea_and_a_bio_box_that_mentions_links_gets_the_bio(store, cfg):
    form = ('<form id="p" action="/apply"><label for="n">Full name *</label><input id="n" name="n" required><label for="e">Email *</label><input id="e" type="email" name="e" required>'
            '<label for="t">Proposed article title *</label><input id="t" name="t" required><label for="i">Your topic ideas *</label><textarea id="i" name="i" required></textarea>'
            '<label for="b">Author bio (include links to your published work) *</label><textarea id="b" name="b" required></textarea><button type="submit">Apply</button></form>')
    result, job = checked(store, cfg, guide(form=form))
    assert result == "qualified" and job["guest"]["mapping"]["kind"] == "pitch"
    sender = Sender()
    G.write_and_send(store, live(cfg), SITE, Judge(cfg), [], sender)
    values = {f["role"]: f["value"] for f in sender.calls[0][1]["fields"]}
    assert values["title"] and values["title"] in values["pitch"], "the proposed title is the first idea of the pitch"
    assert values["bio"].startswith("The Genbooks team runs Genbooks") and "links" not in values


def test_a_form_the_system_could_not_fill_is_reported_as_such_not_as_refused(store, cfg):
    job = qualified(store, cfg)
    report = []
    G.write_and_send(store, live(cfg), SITE, Judge(cfg), report, Sender("not_filled"))
    assert job["status"] == S.REJECTED and "could not fill the form" in job["history"][-1]["note"] and not any("refused" in r for r in report)


def test_an_i_have_read_the_guidelines_box_is_an_agreement_and_a_sponsored_links_box_is_left_empty():
    form = ('<form id="gform_5" action="/guest-blog-submissions/#gf_5"><label for="f">First *</label><input id="f" name="input_1.3" required>'
            '<label for="l">Last *</label><input id="l" name="input_1.6" required><label for="e">Email *</label><input id="e" type="email" name="input_2" required>'
            '<label for="s">Sponsored Backlink(s)</label><input id="s" name="input_21">'
            '<label for="t">Guest Post Topic Proposal</label><textarea id="t" name="input_6"></textarea>'
            '<input type="checkbox" id="c" name="input_7.1" required><label for="c">I have read the guest blogging guidelines*.</label></form>')
    m = G.map_form(f"<html><body>{form}</body></html>", "https://www.rtor.example.org/guest-blog-submissions/")
    roles = {f["name"]: f["role"] for f in m["fields"]}
    assert m["ok"] and roles["input_7.1"] == "consent" and "input_21" not in roles, "never a sponsored link"


def test_a_website_that_is_not_used_is_not_listed_as_an_error(store, cfg):
    job = qualified(store, cfg)
    store.transition(job, S.REJECTED, "not used after all: its only form is a footer contact form")
    entry = pipeline._site_entry(store, cfg, [])
    assert not any(e["title"] == job["title"] for e in entry["errors"]) and any(i["id"] == job["id"] and i["status"] == "not_accepted" for i in entry["guest_posts"])


# ------------------------------------------------------------------ guest posts by email, to the address a website's own guidelines give (owner, 2026-11-03)
EMAIL_PAGE = (f"<html><head><title>Write for us - Writers Hub</title></head><body><h1>Write for us</h1><p>{ACCEPTS} {LINKS}</p>"
              "<p>Send your finished article to <a href='mailto:editor@writershub.example.org'>editor@writershub.example.org</a> with the subject "
              "\"Guest Post: [Your Topic]\". For advertising, email <a href='mailto:ads@writershub.example.org'>ads@writershub.example.org</a>.</p>" + "<p>Guidelines. </p>" * 20 + "</body></html>")
BY_EMAIL = {"how_to_submit": "email", "submission_email": "editor@writershub.example.org", "pitch_first": False, "subject_line": "Guest Post: [Your Topic]", "needs_attachment": False}
MAILBOX_SITE = {**SITE, "sender": {"name": "Genbooks", "email": "support@genbooks.example", "smtp_host": "smtp.example.org", "smtp_port": 587, "password_env": "TEST_MAILBOX_PASS"}}


class Mailer:
    def __init__(self, fail=False):
        self.sent, self.fail = [], fail

    def __call__(self, site, to, subject, body, box):
        if self.fail:
            raise OSError("535 authentication failed")
        self.sent.append({"to": to, "subject": subject, "body": body, "box": box})


def by_email(cfg, **change):
    judge = Judge(cfg)
    judge.acceptance = {**judge.acceptance, **BY_EMAIL, **change}
    return judge


def test_a_website_that_asks_for_guest_posts_by_email_is_qualified_with_its_address(store, cfg):
    result, job = checked(store, cfg, EMAIL_PAGE, by_email(cfg))
    g = job["guest"]
    assert result == "qualified" and g["route"] == "email" and g["email_to"] == "editor@writershub.example.org" and g["captcha"] is False
    assert g["subject_line"] == "Guest Post: [Your Topic]" and g["mapping"]["kind"] == "article"


@pytest.mark.parametrize("change,reason", [
    ({"submission_email": "invented@elsewhere.example.org"}, "no clear email address"),   # not on the page
    ({"submission_email": "ads@writershub.example.org"}, "advertising, sales or press"),
    ({"attachment": "google_doc"}, "Google Doc link"),   # a Word file is attached instead (2026-11-07); a Google Doc link cannot be made
    ({"needs_attachment": True}, "another kind of file"),   # an older answer shape: some file the system cannot make
    ({"how_to_submit": "other"}, "no clear email address"),
])
def test_an_email_website_is_never_used_without_a_proper_address(store, cfg, change, reason):
    result, job = checked(store, cfg, EMAIL_PAGE, by_email(cfg, **change))
    assert result == "rejected" and reason in job["history"][-1]["note"]


def test_an_address_any_campaign_has_emailed_before_is_never_emailed_again(store, cfg, monkeypatch):
    from src import state as shared
    monkeypatch.setattr(shared, "contacted_emails", lambda: {"editor@writershub.example.org"})
    result, job = checked(store, cfg, EMAIL_PAGE, by_email(cfg))
    assert result == "rejected" and "emailed before" in job["history"][-1]["note"]


def test_addresses_are_found_in_text_links_and_behind_cloudflares_protection():
    cf = "".join(f"{b:02x}" for b in [0x42] + [ord(c) ^ 0x42 for c in "guest@site.example.org"])
    html = (f"<p>Write to hello@site.example.org</p><a href='mailto:Editor@Site.example.org?subject=Guest%20post'>editor</a>"
            f"<a href='/cdn-cgi/l/email-protection#{cf}'>[email protected]</a><img src='logo@2x.png'>")
    assert G.page_emails(html) == {"hello@site.example.org", "editor@site.example.org", "guest@site.example.org"}


def test_the_article_is_emailed_from_the_websites_own_mailbox_with_their_subject_and_the_bio(store, cfg, monkeypatch):
    monkeypatch.setenv("TEST_MAILBOX_PASS", "secret")
    result, job = checked(store, cfg, EMAIL_PAGE, by_email(cfg))
    mailer = Mailer()
    G.write_and_send(store, live(cfg), MAILBOX_SITE, by_email(cfg), [], None, None, mailer)
    m = mailer.sent[0]
    assert job["status"] == S.SUBMITTED and job["guest"]["how"] == "by email" and job["guest"]["sent_on"] == store.today()
    assert m["to"] == "editor@writershub.example.org" and m["subject"] == f"Guest Post: {job['title']}" and m["box"]["from_email"] == "support@genbooks.example"
    assert job["title"] in m["body"] and "](" not in m["body"] and "About the author:" in m["body"] and "The Genbooks team runs Genbooks (https://genbooks.uk/)" in m["body"]
    assert m["body"].count("The Genbooks team runs") == 1, "the bio once, not also at the end of the article"
    assert G._sent_today(store) == 1, "an email counts towards the day's two"


def test_a_pitch_first_website_gets_ideas_and_no_article_is_written(store, cfg, monkeypatch):
    monkeypatch.setenv("TEST_MAILBOX_PASS", "secret")
    judge = by_email(cfg, pitch_first=True, subject_line="")
    result, job = checked(store, cfg, EMAIL_PAGE, judge)
    judge.calls.clear()
    mailer = Mailer()
    G.write_and_send(store, live(cfg), MAILBOX_SITE, judge, [], None, None, mailer)
    m = mailer.sent[0]
    assert job["status"] == S.SUBMITTED and judge.calls == [] and m["subject"] == "Guest post idea for Writers Hub"
    assert "Ideas I could write for your readers:" in m["body"] and m["body"].count("\n- ") == 3 and "About me:" in m["body"]


def test_without_a_mailbox_the_email_waits_on_the_dashboard_for_the_owner(store, cfg):
    result, job = checked(store, cfg, EMAIL_PAGE, by_email(cfg))
    G.write_and_send(store, live(cfg), SITE, by_email(cfg), [], None, None, Mailer())
    draft = job["guest"]["email_draft"]
    assert job["status"] == S.PENDING_APPROVAL and draft["to"] == "editor@writershub.example.org" and job["title"] in draft["body"] and draft["from"] == "support@genbooks.example"
    item = next(i for i in G.dashboard_items(store) if i["id"] == job["id"])
    assert item["status"] == "waiting" and item["route"] == "email" and item["email_draft"]["subject"] == f"Guest Post: {job['title']}"


def test_a_mailbox_that_refuses_leaves_the_email_for_the_owner_instead(store, cfg, monkeypatch):
    monkeypatch.setenv("TEST_MAILBOX_PASS", "secret")
    result, job = checked(store, cfg, EMAIL_PAGE, by_email(cfg))
    G.write_and_send(store, live(cfg), MAILBOX_SITE, by_email(cfg), [], None, None, Mailer(fail=True))
    assert job["status"] == S.PENDING_APPROVAL and "refused" in job["guest"]["detail"] and job["guest"]["email_draft"]


def test_the_mailbox_password_comes_from_the_secret_or_from_all_secrets(monkeypatch):
    monkeypatch.delenv("TEST_MAILBOX_PASS", raising=False)
    monkeypatch.setenv("ALL_SECRETS", '{"TEST_MAILBOX_PASS": "from-all-secrets"}')
    assert G.mailbox(MAILBOX_SITE)["password"] == "from-all-secrets"
    monkeypatch.setenv("ALL_SECRETS", "{}")
    assert G.mailbox(MAILBOX_SITE) is None and G.mailbox(SITE) is None


def test_websites_turned_down_only_for_wanting_email_are_checked_again_once(store):
    job = candidate(store, "emailsite.example.org")
    store.transition(job, S.REJECTED, "no submission form: articles go by email only (email-only websites are skipped)", guest_checked=True)
    other = candidate(store, "paid.example.org")
    store.transition(other, S.REJECTED, "the website charges for publishing (paid links are never used)", guest_checked=True)
    job["history"][-1]["at"] = other["history"][-1]["at"] = "2026-11-02T09:30:00+00:00"   # turned down before the email route existed
    report = []
    G.migrate(store, report)
    assert job["status"] == S.DISCOVERED and job["email_rules_checked"] and other["status"] == S.REJECTED and "by email" in report[0]
    store.transition(job, S.REJECTED, "no submission form the system can fill and no email address for submissions", guest_checked=True)
    job["history"][-1]["at"] = "2026-11-08T09:30:00+00:00"   # turned down again under today's rules
    G.migrate(store, [])
    assert job["status"] == S.REJECTED, "only once"


def test_a_page_turned_down_after_the_email_route_existed_is_not_checked_a_second_time(store):
    """Found 2026-11-07: every page turned down for "no form" was re-checked the next morning, using about 10 of the 25 daily checks."""
    job = candidate(store, "noform.example.org", "ten-tips")
    job["title"] = "Ten tips for stress"   # an ordinary article, not a guidelines page
    store.transition(job, S.REJECTED, "no submission form the system can fill and no email address for submissions", guest_checked=True)
    job["history"][-1]["at"] = "2026-11-06T09:30:00+00:00"
    G.migrate(store, [])
    assert job["status"] == S.REJECTED


def test_journals_and_platforms_are_turned_down_without_being_opened_or_using_a_place(store, cfg):
    for domain in ("journals.example-psychology-journal.org", "www.nature.com", "dept.example.ac.uk"):
        candidate(store, domain)
    real = candidate(store, "writershub.example.org")
    web = FakeWeb({real["page_url"]: guide()})
    report = []
    G.check_pass(store, {**cfg, "discovery": {**cfg["discovery"], "inspect_per_run": 1}}, EFF, SETTINGS, Judge(cfg), "Tavily", web, report)
    assert real["status"] == S.QUALIFIED and "3 page(s) turned down without opening them" in report[0]
    assert all("journal" not in u and "nature" not in u and "ac.uk" not in u for u in web.calls), "never opened"


@pytest.mark.parametrize("page", ["form", "email"])
def test_a_website_that_sells_the_same_service_is_never_contacted_by_form_or_email(store, cfg, page):
    judge = by_email(cfg, competitor=True) if page == "email" else Judge(cfg)
    if page == "form":
        judge.acceptance = {**judge.acceptance, "competitor": True}
    result, job = checked(store, cfg, EMAIL_PAGE if page == "email" else guide(), judge)
    assert result == "rejected" and "a competitor" in job["history"][-1]["note"]
    assert "the same kind of service as a UK hypnotherapy practice" in G.acceptance_prompt("text", "a UK hypnotherapy practice")


def test_only_an_address_next_to_an_invitation_to_submit_is_used_never_a_footer_contact(store, cfg):
    footer_only = (f"<html><body><h1>Write for us</h1><p>{ACCEPTS} {LINKS}</p><p>Please use the form on our contact page to pitch your guest post.</p>"
                   "<footer><p>Mobile: 0123 456 789</p><p>Email: info@writershub.example.org</p></footer>" + "<p>More. </p>" * 20 + "</body></html>")
    result, job = checked(store, cfg, footer_only, by_email(cfg, submission_email="info@writershub.example.org"))
    assert result == "rejected" and "not offered for guest posts" in job["history"][-1]["note"]


def test_an_address_just_after_ready_to_submit_counts_even_behind_cloudflares_protection():
    cf = "".join(f"{b:02x}" for b in [0x37] + [ord(c) ^ 0x37 for c in "alkesh@site.example.org"])
    html = (f"<div><p>Have a question?</p><p>Email us at <a href='mailto:hello@site.example.org'>hello@site.example.org</a></p>"
            f"<p>Ready to submit a post?</p><p>Please drop us an email at <a href='/cdn-cgi/l/email-protection#{cf}'>[email protected]</a></p>"
            f"<footer><p>Email: owner@site.example.org</p></footer></div>")
    assert G.address_invited(html, "alkesh@site.example.org") is True
    assert G.address_invited(html, "owner@site.example.org") is False and G.address_invited(html, "hello@site.example.org") is False


def test_what_was_emailed_is_kept_and_shown_on_the_dashboard(store, cfg, monkeypatch):
    monkeypatch.setenv("TEST_MAILBOX_PASS", "secret")
    result, job = checked(store, cfg, EMAIL_PAGE, by_email(cfg))
    mailer = Mailer()
    G.write_and_send(store, live(cfg), MAILBOX_SITE, by_email(cfg), [], None, None, mailer)
    kept = job["guest"]["email_sent"]
    assert kept["to"] == mailer.sent[0]["to"] and kept["subject"] == mailer.sent[0]["subject"] and kept["body"] == mailer.sent[0]["body"]
    item = next(i for i in G.dashboard_items(store) if i["id"] == job["id"])
    assert item["status"] == "sent" and item["email_to"] == "editor@writershub.example.org" and item["email_sent"]["body"] == kept["body"]


def test_a_contact_form_7_quiz_question_is_a_question_for_the_person_not_an_unfillable_field():
    """Found 2026-11-04: Example Publisher ("which is bigger, 2 or 6?") and example-sport.org ("how many players...") were turned down for it."""
    def form(label, name):
        return ('<form id="cf7" action="/contact/#wpcf7-f1" method="post"><label for="n">Your Name (required)</label><input id="n" name="your-name" required>'
                '<label for="e">Your Email (required)</label><input id="e" type="email" name="your-email" required><label for="s">Subject</label><input id="s" name="your-subject">'
                f'<label for="m">Your Message</label><textarea id="m" name="your-message"></textarea><label for="q">{label}</label><input id="q" type="text" name="{name}" required></form>')
    for label, name in (("which is bigger, 2 or 6?", "quiz-267"), ("How many players can play at once on a 5-a-side team?", "QuizQuestions")):
        m = G.map_form(f"<html><body><main>{form(label, name)}</main></body></html>", "https://x.example.org/contact/")
        assert m["ok"] and [h["name"] for h in m["human"]] == [name] and name not in {f["name"] for f in m["fields"]}, label


def test_when_gemini_is_down_the_pass_carries_on_with_the_system_s_own_checks(store, cfg):
    """2026-11-05/06: the first Gemini failure stopped the whole pass, so the unchecked pile grew (Genbooks reached 55 pages)."""
    needs_gemini = candidate(store, "needs-gemini.example.org")   # passes every code check, so only Gemini can judge it
    needs_gemini["discovery_source"] = 'tavily: "write for us" writing'
    no_links = candidate(store, "nolinks.example.org")           # the page never mentions links or a bio: turned down by the code alone
    no_form = candidate(store, "noform.example.org")             # no form and no address: turned down by the code alone
    after = candidate(store, "also-needs-gemini.example.org")    # needs Gemini too: waits, without another request
    web = FakeWeb({needs_gemini["page_url"]: guide(), after["page_url"]: guide(),
                   no_links["page_url"]: guide(form=FORM.replace('<label for="b">Author bio</label><textarea id="b" name="author-bio"></textarea>', ""), text=ACCEPTS),
                   no_form["page_url"]: "<html><body><h1>Write for us</h1><p>" + ACCEPTS + " " + LINKS + "</p>" + "<p>More guidelines. </p>" * 30 + "</body></html>"})
    judge, report = Judge(cfg, down=True), []
    G.check_pass(store, cfg, EFF, SETTINGS, judge, "Tavily", web, report)
    assert needs_gemini["status"] == S.DISCOVERED and after["status"] == S.DISCOVERED, "the websites that need Gemini wait for the next run"
    assert no_links["status"] == S.REJECTED and no_form["status"] == S.REJECTED, "the others are still checked, by the system's own code"
    assert len(judge.calls) == 1, "Gemini was asked once; after its failure no more requests were made"
    assert sum("Gemini is unavailable" in r for r in report) == 1


# ------------------------------------------------------------------ the corrections of 2026-11-07 (the owner's "why does it not find 6 a day" research)
def test_the_search_asks_tavily_for_the_exact_phrase(monkeypatch):
    """Without exact_match about 60% of Tavily's results were ordinary articles that merely mention the subject."""
    from src import tavily_discovery as T
    sent = []

    class Answer:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def read(self):
            return b'{"results": []}'
    monkeypatch.setattr(T, "_api_key", lambda: "k")
    monkeypatch.setattr(T.urllib.request, "urlopen", lambda req, timeout=15: sent.append(json.loads(req.data)) or Answer())
    T.search('"write for us" mental health')
    T.search("plain words")
    assert sent[0]["exact_match"] is True and sent[0]["query"] == '"write for us" mental health'
    assert "exact_match" not in sent[1], "only a query with a quoted phrase asks for it"


def test_addresses_written_out_in_words_are_found():
    html = ("<p>If you would like to submit a guest post, please e-mail me (jessicaATexample-workshopDOTorg) with the idea.</p>"
            "<p>You can contact Stacey at info [at] example-blog.org . You can also submit your article there.</p><p>jane(at)example(dot)co(dot)uk</p>")
    assert {"jessica@example-workshop.org", "info@example-blog.org", "jane@example.co.uk"} <= G.page_emails(html)
    assert G.spelled_out("Meet us at the park. The event is at noon.") == set()
    assert G.parse_acceptance(json.dumps({"submission_email": "info [at] example-blog [dot] org"}))["email"] == "info@example-blog.org"


GUIDELINES_EMAIL = ("<html><head><title>Guest Post Guidelines - Writers Write</title></head><body><header><a href='mailto:office@ww.example.org'>office@ww.example.org</a></header>"
                    "<main><h1>Guest Post Guidelines</h1><p>{accepts} {links}</p><p>By submitting a guest post, you agree to these terms.</p>"
                    "<p>Please send the post to <a href='mailto:neo@ww.example.org'>neo@ww.example.org</a> and give us two weeks to respond.</p>"
                    "<div><p>Questions? Feel free to email us at <a href='mailto:guestblogger@ww.example.org'>guestblogger@ww.example.org</a></p></div></main>"
                    "<footer><p>Contact: <a href='mailto:hello@ww.example.org'>hello@ww.example.org</a></p></footer></body></html>").format(accepts=ACCEPTS, links=LINKS)


def test_an_address_in_the_guidelines_themselves_or_named_for_submissions_counts_but_the_header_and_footer_never_do():
    """Turned down before: 'Please send the post to neo@...' on example-writers.org, 'email us at guestblogger@...' on example-kids.org."""
    url = "https://ww.example.org/guest-post-guidelines"
    assert G.is_guidelines_page(GUIDELINES_EMAIL, url)
    assert G.address_invited(GUIDELINES_EMAIL, "neo@ww.example.org", True)
    assert G.address_invited(GUIDELINES_EMAIL, "guestblogger@ww.example.org"), "its name says it is for guest posts"
    assert not G.address_invited(GUIDELINES_EMAIL, "office@ww.example.org", True), "an address in the header is never used"
    assert not G.address_invited(GUIDELINES_EMAIL, "hello@ww.example.org", True), "a footer contact is never used"
    article = "<html><head><title>Ten tips for writers</title></head><body><main><h1>Ten tips</h1><p>Say hello: <a href='mailto:jo@ww.example.org'>jo@ww.example.org</a></p></main></body></html>"
    assert not G.is_guidelines_page(article, "https://ww.example.org/ten-tips") and not G.address_invited(article, "jo@ww.example.org", False)


def test_a_guidelines_page_that_asks_for_posts_by_email_at_its_own_address_is_qualified(store, cfg):
    judge = by_email(cfg, submission_email="neo@ww.example.org", subject_line="")
    result, job = checked(store, cfg, GUIDELINES_EMAIL, judge, domain="ww.example.org")
    assert result == "qualified" and job["guest"]["email_to"] == "neo@ww.example.org"


@pytest.mark.parametrize("rule", [
    "Human-Written: No more than 10% AI-generated content.",
    "We do not accept AI-written articles.",
    "All submissions are checked with an AI detector.",
    "If you want to generate backlinks to your website or if you are an agent doing link building for a client, please do not submit a pitch.",
    "We do not accept guest posts from SEO agencies.",
])
def test_a_website_that_refuses_ai_writing_or_link_builders_is_never_used_and_gemini_is_not_asked(store, cfg, rule):
    """The articles are written with AI: sending them where AI writing or link building is refused would be dishonest."""
    judge = Judge(cfg)
    result, job = checked(store, cfg, guide(text=f"{ACCEPTS} {LINKS} {rule}"), judge)
    assert result == "rejected" and job["status"] == S.REJECTED and judge.calls == []
    assert "AI-written" in job["history"][-1]["note"] or "want backlinks" in job["history"][-1]["note"]


@pytest.mark.parametrize("answer,reason", [({"ai_restricted": True}, "AI-written"), ({"no_link_builders": True}, "want backlinks")])
def test_when_gemini_reads_such_a_rule_the_website_is_not_used_either(store, cfg, answer, reason):
    judge = Judge(cfg)
    judge.acceptance = {**judge.acceptance, **answer}
    result, job = checked(store, cfg, guide(), judge)
    assert result == "rejected" and reason in job["history"][-1]["note"]


def test_ordinary_link_rules_are_not_mistaken_for_a_refusal():
    for ok in ("We only accept one do-follow backlink per high-quality, relevant post.", "Do not include more than one backlink.",
               "What are we NOT looking for? Requests for backlinks. Guest contributors can link to their website in their bio.", "Write about AI tools for authors."):
        assert not G.AI_RULES.search(ok) and not G.NO_LINK_BUILDERS.search(ok), ok


def test_a_menu_login_box_does_not_mean_an_account_is_needed_but_saying_so_does():
    from src.backlinks import qualify
    menu_login = ("<html><body><header><nav><a href='/write-for-us'>Write for us</a></nav><form class='login-form'><input name='log'><input type='password' name='pwd'>"
                  "<label><input type='checkbox' name='rememberme'> Remember me</label><a href='/lost'>Lost Password?</a></form></header>"
                  "<main><h1>Write for us</h1><p>Pitch us by email: <a href='mailto:editor@x.example.org'>editor@x.example.org</a></p></main>"
                  "<div class='modal' id='signin'><form><input name='u'><input type='password' name='p'></form></div></body></html>")
    assert qualify.inspect_page(menu_login, "https://x.example.org/write-for-us")["mechanism"] == "email_only"
    comment_note = ("<html><body><h1>Write for us</h1><p>Pitch us by email: <a href='mailto:editor@x.example.org'>editor@x.example.org</a></p>"
                    "<p>You must be logged in to post a comment.</p></body></html>")
    assert qualify.inspect_page(comment_note, "https://x.example.org/write-for-us")["mechanism"] == "email_only", "the comment box's note is not about articles"
    assert qualify.inspect_page("<html><body><h1>Contribute</h1><p>You must be logged in to submit an article.</p></body></html>",
                                "https://x.example.org/c")["mechanism"] == "account_required"


def test_the_submission_page_is_followed_but_share_buttons_and_category_lists_never_are():
    html = ("<html><body><h1>Guest post guidelines</h1><p>Send your draft title and bio via the contact form.</p>"
            "<a href='/guest-post-guidelines/?share=twitter'>Share on Twitter</a><a href='/category/guest-posts/'>Guest posts</a>"
            "<a href='https://app.mindful.example.org/submit'>online submission portal</a><a href='/contact/'>Contact</a>"
            "<a href='https://other.example.net/write-for-us'>Write for us elsewhere</a></body></html>")
    assert G.related_pages(html, "https://www.mindful.example.org/guest-post-guidelines") == ["https://app.mindful.example.org/submit", "https://www.mindful.example.org/contact/"]


class AttachMailer(Mailer):
    def __call__(self, site, to, subject, body, box, attachments=None):
        super().__call__(site, to, subject, body, box)
        self.sent[-1]["attachments"] = attachments or []


def test_a_website_that_asks_for_a_word_file_gets_the_article_attached(store, cfg, monkeypatch):
    """Five websites were turned down in a week only because they wanted a Word file."""
    import io
    from docx import Document
    monkeypatch.setenv("TEST_MAILBOX_PASS", "secret")
    judge = by_email(cfg, attachment="word")
    result, job = checked(store, cfg, EMAIL_PAGE, judge)
    assert result == "qualified" and job["guest"]["attach_word"] is True
    mailer = AttachMailer()
    G.write_and_send(store, live(cfg), MAILBOX_SITE, judge, [], None, None, mailer)
    m = mailer.sent[0]
    (name, data, kind), = m["attachments"]
    assert name.endswith(".docx") and kind == G.DOCX_TYPE and "attached as a Word file" in m["body"]
    assert G.plain_text(json.loads(store.read_content(job["content_path"]))["body_markdown"])[:200] not in m["body"], "the article travels in the file"
    article = json.loads(store.read_content(job["content_path"]))
    doc = Document(io.BytesIO(data))
    text = "\n".join(p.text for p in doc.paragraphs)
    assert doc.paragraphs[0].text == article["title"] and "About the author" in text and "The Genbooks team runs Genbooks" in text
    links = [r.target_ref for r in doc.part.rels.values() if r.is_external]
    assert any("genbooks.uk" in u for u in links), "the link in the article stays a real, clickable link"
    assert job["status"] == S.SUBMITTED and job["guest"]["email_sent"]["attached"] == name


def test_without_a_mailbox_a_word_file_website_waits_with_the_article_in_the_email_and_a_note(store, cfg):
    judge = by_email(cfg, attachment="word")
    result, job = checked(store, cfg, EMAIL_PAGE, judge)
    G.write_and_send(store, live(cfg), SITE, judge, [], None, None, AttachMailer())
    g = job["guest"]
    assert job["status"] == S.PENDING_APPROVAL and job["title"] in g["email_draft"]["body"] and "attach it" in g["detail"]


def test_genbooks_now_has_its_own_mailbox_on_zohos_international_servers():
    site = next(s for s in pipeline.load_sites() if s["id"] == "genbooks-uk")
    assert site["sender"]["email"] == "support@genbooks.example" and site["sender"]["smtp_host"] == "smtp.zoho.com" and site["sender"]["password_env"] == "GENBOOKS_SMTP_PASS"
    assert site["email_outreach"] is False, "a mailbox is for guest posts only: the old cold outreach stays off"


def test_the_mailbox_check_signs_in_without_sending_and_says_what_happens(monkeypatch):
    monkeypatch.setenv("ALL_SECRETS", "{}")
    monkeypatch.delenv("TEST_MAILBOX_PASS", raising=False)
    assert "no password yet" in G.mailbox_check(MAILBOX_SITE) and G.mailbox_check(SITE) is None
    monkeypatch.setenv("TEST_MAILBOX_PASS", "secret")
    tried = []
    assert "signed in fine" in G.mailbox_check(MAILBOX_SITE, login=tried.append) and tried[0]["user"] == "support@genbooks.example"

    def refuse(box):
        raise OSError("535 Authentication Failed")
    assert "could not sign in" in G.mailbox_check(MAILBOX_SITE, login=refuse)


def test_guidelines_pages_turned_down_under_the_old_rules_are_checked_once_more(store):
    def rejected(domain, path, note, title="Write for us"):
        url = f"https://{domain}/{path}"
        job, _ = store.add_job(make_key("web", domain, url), adapter="web", domain=domain, page_url=url, discovery_source="tavily: x", title=title)
        store.transition(job, S.REJECTED, note, guest_checked=True)
        job["history"][-1]["at"] = "2026-11-06T09:30:00+00:00"   # under the old rules: after the email route, before the corrections
        return job
    old_address = rejected("a.example.org", "guest-post-guidelines", "the address neo@a.example.org is not offered for guest posts on the page (only ...)")
    menu_login = rejected("b.example.org", "write-for-us", "the website needs an account or refuses automated submissions")
    article = rejected("c.example.org", "ten-tips", "no submission form the system can fill and no email address for submissions", title="Ten tips for stress")
    weak = rejected("d.example.org", "write-for-us", "not a good enough fit (relevance 5, quality 5: off topic)")
    report = []
    G.migrate(store, report)
    assert old_address["status"] == S.DISCOVERED and menu_login["status"] == S.DISCOVERED
    assert article["status"] == S.REJECTED and weak["status"] == S.REJECTED, "only guidelines pages, and only for the corrected reasons"
    G.migrate(store, report)
    assert sum("too strict" in r for r in report) == 1, "once only"
    later = rejected("e.example.org", "write-for-us", "the website needs an account or refuses automated submissions")
    later["history"][-1]["at"] = "2026-11-08T09:30:00+00:00"   # turned down under the corrected rules: no second check
    G.migrate(store, [])
    assert later["status"] == S.REJECTED


def test_a_rechecked_website_is_not_blocked_by_its_own_earlier_no(store, cfg):
    url = "https://writershub.example.org/write-for-us"
    job, _ = store.add_job(make_key("web", "writershub.example.org", url), adapter="web", domain="writershub.example.org", page_url=url, discovery_source="tavily: x", title="Write for us")
    store.transition(job, S.REJECTED, "the website needs an account or refuses automated submissions", guest_checked=True)
    job["history"][-1]["at"] = "2026-11-06T09:30:00+00:00"
    G.migrate(store, [])
    assert job["status"] == S.DISCOVERED
    web = FakeWeb({url: guide()})
    assert G.check_candidate(store, job, cfg, EFF, SETTINGS, Judge(cfg), "Tavily", web, [0]) == "qualified"


# ------------------------------------------------------------------ members-only websites (owner, 2026-11-07)
@pytest.mark.parametrize("rule", [
    "Write For Us: ALLi Guest Blogger Guidelines (Members-Only)",
    "Only ALLi members are eligible to write guest posts.",
    "We invite guest blogs and case studies from members of the Alliance of Independent Authors.",
    "Contributors must be paid members.",
    "Membership is required to contribute.",
])
def test_a_website_that_only_publishes_its_own_members_is_never_used_and_gemini_is_not_asked(store, cfg, rule):
    """2026-11-07: a pitch went to The Self-Publishing Advice Center, whose guidelines are "Members-Only" for the Alliance of Independent Authors."""
    judge = Judge(cfg)
    result, job = checked(store, cfg, guide(text=f"{ACCEPTS} {LINKS} {rule}"), judge)
    assert result == "rejected" and judge.calls == [] and "only publishes its own members" in job["history"][-1]["note"]


def test_when_gemini_reads_a_members_only_rule_the_website_is_not_used_either(store, cfg):
    judge = Judge(cfg)
    judge.acceptance = {**judge.acceptance, "members_only": True}
    result, job = checked(store, cfg, guide(), judge)
    assert result == "rejected" and "only publishes its own members" in job["history"][-1]["note"]


def test_the_word_member_in_ordinary_sentences_is_not_a_membership_rule():
    for ok in ("We welcome guest posts from members of the public.", "Our team members write most posts.", "Members of the press can email us.",
               "Open to everyone, members or not.", "Please read our membership terms if you subscribe to the newsletter."):
        assert not G.MEMBERS_ONLY.search(ok), ok


def test_the_backup_reader_works_every_day_not_only_on_alternate_days(store, cfg, monkeypatch):
    """Owner, 2026-11-08: on the days the alternating rule chose Tavily, pages a plain fetch could not read were lost (15 on that day)."""
    from src import scheduler
    from test_pipeline import SETTINGS as RUN_SETTINGS
    seen = []
    monkeypatch.setattr(G, "find", lambda *a, **k: {})
    monkeypatch.setattr(G, "check_pass", lambda store, cfg, eff, settings, llm, engine, fetch, report: seen.append(engine))
    site = {**SITE, "restrict_to_uk": False, "discovery_keywords": ["writing a book"], "competitor_terms": []}
    for day_rule in ("Tavily", "Firecrawl"):   # both kinds of day
        monkeypatch.setattr(scheduler, "_discovery_provider_for_today", lambda name=day_rule: (object(), name))
        pipeline.run(store=store, cfg=cfg, adapters={}, llm=Judge(cfg), fetch=FakeWeb(), site=site, settings=RUN_SETTINGS, write_logs=False)
    assert seen == ["Firecrawl", "Firecrawl"]


# ------------------------------------------------------------------ the AI-writing rule, widened (found by the by-hand pass of 2026-11-08)
@pytest.mark.parametrize("rule", [
    "Your article must not be AI-generated.",
    "Submissions must not be created using AI.",
    "All posts must be written by you, not AI.",
    "You must confirm you did not use AI to write the post.",
    "Please state that no AI was used.",
    "We are an AI-free publication.",
    "Posts written without AI only.",
    "We don’t accept AI-generated content.",   # a curly apostrophe must not hide the rule
])
def test_the_wider_ai_wording_is_caught_before_gemini_is_asked(store, cfg, rule):
    """The by-hand pass found four websites that got past the code check with these sentences."""
    judge = Judge(cfg)
    result, job = checked(store, cfg, guide(text=f"{ACCEPTS} {LINKS} {rule}"), judge)
    assert result == "rejected" and judge.calls == [] and "AI-written" in job["history"][-1]["note"]


def test_sentences_about_ai_that_are_not_a_ban_are_not_refused():
    for ok in ("Write about AI tools for authors.", "We use AI to translate our newsletter.", "We welcome guest posts on artificial intelligence.",
               "Free of charge for contributors.", "Articles must be original and not published elsewhere.", "Written by you, with your own examples."):
        assert not G.AI_RULES.search(ok), ok


# ------------------------------------------------------------------ a website that wipes its form right after we fill it (Example Publisher, 2026-11-08)
@pytest.mark.skipif(not _chromium_ready(), reason="Playwright's Chromium is not installed here")
def test_the_real_browser_refills_a_form_the_website_wipes_and_sends_it_full(store, cfg, tmp_path):
    """Contact Form 7 emptied the form a moment after it was filled: "it writes and disappears". The automatic sender waits until the
    values stay put (and refills what was wiped) before it sends, so the website receives the whole article."""
    from src.backlinks import browser
    job = qualified(store, cfg)
    G.write_and_send(store, cfg, SITE, Judge(cfg), [], Sender())
    payload = G.payload_for(store, job, cfg, SITE)
    wiper = ("<script>document.getElementById('gp').addEventListener('input', function once() { this.removeEventListener('input', once);"
             " setTimeout(function () { document.getElementById('gp').reset(); }, 0); });</script>")
    server, got = _local_site(guide().replace("</body>", wiper + "</body>"))
    try:
        browser._ready = True
        res = browser.submit(f"http://127.0.0.1:{server.server_port}/write-for-us", payload, None)
    finally:
        server.shutdown()
    assert res["outcome"] == "sent", res
    assert got["your-name"] == "Genbooks team" and got["your-email"] == "support@genbooks.example" and got["post-title"] == job["title"]
    assert "https://genbooks.uk/" in got["your-message"] and got["agree"] == "on"


def test_a_pitch_is_never_matched_to_an_unrelated_article_of_the_website(store, cfg, clock):
    """2026-11-10: the pitch "Pitch to The Self-Publishing Advice Center" matched an unrelated article on that website (it shares the
    words self, publishing and advice), which was then reported as published without our link (VERIFICATION_FAILED)."""
    judge = by_email(cfg, pitch_first=True, subject_line="")
    result, job = checked(store, cfg, EMAIL_PAGE, judge, domain="selfpublishingadvice.example.org")
    job["guest"]["site_name"] = "The Self-Publishing Advice Center"
    job["title"] = "Pitch to The Self-Publishing Advice Center"
    store.transition(job, S.CONTENT_READY, "pitch prepared")
    store.transition(job, S.SUBMISSION_IN_PROGRESS, "sending")
    store.transition(job, S.SUBMITTED, "sent", submitted_at=store.now_iso())
    url = "https://selfpublishingadvice.example.org/self-publishing-back-catalogue-hybrid-authors-advice/"
    web = FakeWeb({"https://selfpublishingadvice.example.org/sitemap.xml": f"<urlset><url><loc>{url}</loc></url></urlset>", url: page("<p>Unrelated advice.</p>", title="Back catalogue")})
    clock.advance(days=3)
    G.track(store, cfg, [], web)
    assert job["status"] == S.SUBMITTED, "a pitch is not looked for in the sitemap"
    assert G.find_published(job, web) is None, "and the website's own name never counts as a match"


@pytest.mark.parametrize("rule", [
    "It should go without saying that any pitches or copy created using LLMs will be discarded.",
    "Anything written by AI will be rejected.",
    "We will discard any article generated with ChatGPT.",
])
def test_the_ai_ban_is_caught_when_the_verdict_comes_after_or_uses_other_verbs(store, cfg, rule):
    """The Londoner (2026-11-10): "any pitches or copy created using LLMs will be discarded" got past the earlier wording."""
    judge = Judge(cfg)
    result, job = checked(store, cfg, guide(text=f"{ACCEPTS} {LINKS} {rule}"), judge)
    assert result == "rejected" and judge.calls == [] and "AI-written" in job["history"][-1]["note"]
