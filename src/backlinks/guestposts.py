"""Guest posts: websites in a site's niche that publish articles by outside authors and allow a link back.

One daily pass per site:
  find      search phrases that only match submission pages ("write for us", "submit a guest post", ...)
  check     every one of these must hold, or the website is never shown:
              - a real submission form (email-only, account-only and "no automated submissions" pages are dropped)
              - the page (or its guidelines page) clearly accepts outside articles AND allows a link to the author's site,
                each backed by a quote that our own code finds word for word in the page (a model's word alone never counts)
              - not paid, a good fit for the niche (relevance >= 7, quality >= 6), not a competitor, shop or social network
              - every required field of the form can be filled with confidence (hidden honeypot fields are never touched)
  write     a unique article that follows the website's own rules (word range, topics), with the normal checks
  send      no CAPTCHA: a headless browser fills and sends the form (src/backlinks/browser.py)
            CAPTCHA: a "Click to submit" link; the owner's Chrome extension fills and sends it after the owner ticks the CAPTCHA
  track     the owner's reports (state/guestpost_status.json) and the website's sitemap/feed, then the normal link verification
At most `articles_per_day` sends (or click links) per site per day; a website is used once, ever. `guest_posts.submit: false`
prepares everything without sending (review mode)."""
import base64
import html as htmllib
import json
import os
import re
from datetime import datetime, timedelta
from pathlib import Path
from urllib import robotparser
from urllib.parse import unquote, urljoin, urlparse

from bs4 import BeautifulSoup

from src import outreach, site_profiler, state as shared_state
from src.backlinks import content as C, discover, executor, qualify, states as S, verify
from src.backlinks.llm import LLMError
from src.backlinks.store import make_key
from src.outreach import SOCIAL_MEDIA_DOMAINS

# "submit an article" and "contribute an article" found mostly academic journals, so they are not used (2026-11-03)
TEMPLATES = ['"write for us" {keyword}', '"submit a guest post" {keyword}', '"guest post guidelines" {keyword}',
             '"become a contributor" {keyword}', '"accepting guest posts" {keyword}', '"guest post" "submission guidelines" {keyword}']
NOT_PUBLISHERS = frozenset("""amazon.com amazon.co.uk ebay.com ebay.co.uk etsy.com google.com bing.com yahoo.com wikipedia.org
gumtree.com alibaba.com aliexpress.com walmart.com tesco.com medium.com quora.com reddit.com pinterest.com issuu.com scribd.com
slideserve.com slideshare.net academia.edu researchgate.net ssrn.com nature.com springer.com springernature.com tandfonline.com
sciencedirect.com elsevier.com wiley.com sagepub.com frontiersin.org mdpi.com plos.org jmir.org apa.org merriam-webster.com
scientificarchives.com scientific-archives.net upwork.com fiverr.com semrush.com creately.com idealist.org adsy.com collaborator.pro substack.com sites.google.com
linkedin.com youtube.com""".split())
NOT_A_BLOG = re.compile(r"journal|scholar|academ|\.edu$|\.ac\.uk$|\.gov$|\.gov\.uk$|\.nhs\.uk$|\.nhs\.scot$", re.I)   # journals, universities, official bodies
MIN_RELEVANCE, MIN_QUALITY = 7, 6
DASHBOARD = "https://owner.github.io/backlink-dashboard/"
TRACK_DAYS, TRACK_EVERY_DAYS = 60, 7
LLM_DOWN = {"quota", "busy", "network", "truncated", "auth"}
GUIDE_LINK = re.compile(r"guideline|write for us|guest.?post|contribut|submi", re.I)
# A page that never mentions a link, a bio, a byline or the author's own website cannot say that links are allowed, so it is
# turned down without asking Gemini (the model's "yes" would need a quote that is not there).
LINK_WORDS = re.compile(r"\blinks?\b|backlink|hyperlink|\bbio\b|byline|\burl\b|your (own )?(website|site|blog)|author (box|profile|section)", re.I)
# The articles are written with AI, so a website that refuses or limits AI writing is never used, and neither is one that asks people
# who want backlinks (or SEO agencies) not to submit: sending to them would be dishonest (2026-11-07). Checked by our own code before
# Gemini is asked, and asked of Gemini as well.
_AI = r"(?:AI|A\.I\.|ChatGPT|GPT|LLMs?|artificial intelligence|machine)"
_AI_WORK = r"[- ]?(?:generated|written|assisted|created|produced|content|writing|articles?|posts?)"
AI_RULES = re.compile(r"\bno\s+(?:\w+\s+){0,2}" + _AI + _AI_WORK
                      + r"|\b(?:not accept\w*|don'?t accept|do not accept|won'?t accept|will not accept|reject\w*|declin\w*|discard\w*|refuse\w*|ignor\w*|delet\w*|prohibit\w*|not allowed|forbid\w*|banned)\b[^.!?]{0,40}"
                      + r"\b" + _AI + r"(?:[- ]?tools?|" + _AI_WORK + ")"
                      + r"|\b(?:not accept\w*|don'?t accept|do not accept|won'?t accept|will not accept|reject\w*|declin\w*|discard\w*|refuse\w*|ignor\w*|delet\w*|prohibit\w*|not allowed|forbid\w*|banned)\b[^.!?]{0,40}"
                      + r"\b(?:created|written|generated|produced)\s+(?:with|by|using)\s+" + _AI
                      + r"|\b" + _AI + r"[- ](?:generated|written|assisted|created|produced)\b[^.!?]{0,60}\b(?:not|never|won'?t|will not|reject\w*|declin\w*|prohibit\w*|forbid\w*|unacceptable|deleted)"
                      + r"|\b(?:created|written|generated|produced|made|drafted)\s+(?:with|by|using|through)\s+(?:an?\s+)?" + _AI + r"\b[^.!?]{0,50}\b(?:discard\w*|reject\w*|declin\w*|ignored|deleted|unacceptable|not (?:be )?(?:accepted|considered|published))"   # "copy created using LLMs will be discarded"
                      + r"|\bnot\s+(?:be\s+)?" + _AI + r"[- ](?:generated|written|created|produced|assisted)\b"   # "must not be AI-generated"
                      + r"|\b(?:not|never)\s+(?:be\s+)?(?:\w+\s+){0,2}(?:created|written|generated|produced|drafted|made)\s+(?:using|with|by|through)\s+(?:an?\s+)?" + _AI + r"\b"
                      + r"|\bwritten by (?:you|a human|humans?|people)\b[^.!?]{0,12}\bnot\b[^.!?]{0,12}" + _AI + r"\b"   # "written by you, not AI"
                      + r"|\b(?:you|authors?|writers?|contributors?|submitters?)\s+(?:did not|didn'?t|have not|haven'?t|must not|mustn'?t|should not|shouldn'?t|cannot|can'?t|may not)\s+(?:use|used)\s+" + _AI + r"\b"
                      + r"|\b(?:no|not)\s+" + _AI + r"\s+(?:was|were|has been|have been)\s+used\b"
                      + r"|\b" + _AI + r"[- ]free\b|\bwithout (?:the use of |using )?" + _AI + r"\b|\bfree (?:of|from) " + _AI + r"\b"
                      + r"|\bhuman[- ]written\b|\bwritten by (?:a )?humans?\b|\b100 ?% human\b|\bno more than \d+ ?% (?:of )?" + _AI
                      + r"|\bAI[- ]detect\w*|\b(?:GPTZero|Originality\.ai|ZeroGPT|Copyleaks)\b", re.I)
# Websites that only publish their own members (owner, 2026-11-07: The Self-Publishing Advice Center is "Members-Only" for the
# Alliance of Independent Authors; we are not members, so the pitch could only be wasted). Checked by our own code before Gemini, and Gemini is asked too.
MEMBERS_ONLY = re.compile(r"\bmembers?[- ]only\b"
                          r"|\bonly\b[^.!?]{0,40}\bmembers?\b[^.!?]{0,40}\b(?:eligible|can|may|are (?:allowed|welcome|able)|accepted|considered|invited)\b"
                          r"|\bmust (?:first )?(?:be|become|have been) (?:an? )?(?:\w+ ){0,3}members?\b"
                          r"|\b(?:open|available|reserved|limited|restricted) (?:only )?(?:to|for) (?:\w+ ){0,3}members\b(?! of the public)"
                          r"|\b(?:invite|accept|welcome|publish)s? (?:guest )?(?:blogs?|posts?|articles?|case studies|contributions?)(?: and case studies)? (?:only )?from (?:\w+ ){0,3}members\b(?! of the public)"
                          r"|\bmembership (?:is )?(?:required|needed|necessary)\b|\b(?:requires?|need) (?:an? )?(?:active |paid |current )?membership\b", re.I)
NO_LINK_BUILDERS = re.compile(r"\b(?:SEO|link[- ]?build\w*|backlinks?)\b[^.!?]{0,80}\b(?:do not|don'?t|not|never)\b[^.!?]{0,20}\b(?:submit|pitch|apply|contact|email|reach out)"
                              r"|\b(?:do not|don'?t|please don'?t)\b[^.!?]{0,20}\b(?:submit|pitch|apply|contact|email)\b[^.!?]{0,80}\b(?:SEO|link[- ]?build\w*|backlinks?)\b"
                              r"|\b(?:do not|don'?t|won'?t|will not|never)\b (?:accept|take|publish|work with)\b[^.!?]{0,60}\b(?:SEO|link[- ]?build\w*)"
                              r"|\bno (?:SEO|link[- ]?build\w*) (?:agencies|pitches|requests|submissions|posts)", re.I)


# ------------------------------------------------------------------ small helpers
def _norm(text: str) -> str:
    t = htmllib.unescape(text or "").lower()
    for a, b in (("’", "'"), ("‘", "'"), ("“", '"'), ("”", '"'), ("–", "-"), ("—", "-"), ("\xa0", " ")):
        t = t.replace(a, b)
    return re.sub(r"\s+", " ", t).strip()


def quote_found(quote: str, text: str) -> bool:
    """A model's evidence counts only when our own code finds the quote, word for word, in the page."""
    q = _norm(quote).strip(' ".\'')
    return len(q) >= 12 and q in _norm(text)


def page_text(html: str) -> str:
    soup = BeautifulSoup(html or "", "html.parser")
    for t in soup(["script", "style", "noscript", "svg"]):
        t.decompose()
    return re.sub(r"\s+", " ", soup.get_text(" ", strip=True))


def plain_text(markdown: str) -> str:
    """The article as plain text for a form's text box: links written out, no markdown symbols."""
    t = re.sub(r"\[([^\]]+)\]\((https?://[^)\s]+)\)", r"\1 (\2)", markdown)
    t = re.sub(r"^#{1,6}\s*", "", t, flags=re.M)
    t = re.sub(r"\*\*(.+?)\*\*", r"\1", t)
    t = re.sub(r"(?<![\w*])\*(?!\s)(.+?)(?<!\s)\*(?![\w*])", r"\1", t)
    t = re.sub(r"^\s*---+\s*$", "", t, flags=re.M)
    return re.sub(r"\n{3,}", "\n\n", t).strip()


def excluded(domain: str, title: str, eff: dict, settings: dict) -> str | None:
    host = domain.lower().removeprefix("www.")
    if host in SOCIAL_MEDIA_DOMAINS:
        return "a social network"
    if any(host == d or host.endswith("." + d) for d in NOT_PUBLISHERS):
        return "a shop, search engine, platform or academic publisher (not a blog that takes guest posts)"
    if NOT_A_BLOG.search(host):
        return "a journal, university or official body (not a blog that takes guest posts)"
    if site_profiler.looks_like_competitor(title or "", eff.get("priority_keywords") or [], eff.get("competitor_terms"), host):
        return "a competitor (never contacted)"
    blocked = [str(x).lower() for x in (settings.get("do_not_contact") or [])]
    if any(b and (b in host or b in (title or "").lower()) for b in blocked):
        return "on the do-not-contact list"
    return None


def robots_allows(url: str, fetch=verify.http_fetch) -> bool:
    parts = urlparse(url)
    got = fetch(f"{parts.scheme}://{parts.netloc}/robots.txt")
    if got.status != 200 or not got.text:
        return True   # no robots.txt (or unreadable): nothing forbids reading the page
    rp = robotparser.RobotFileParser()
    rp.parse(got.text.splitlines())
    return rp.can_fetch("BacklinkVerifier", url)


def site_name_of(html: str, domain: str, title: str = "") -> str:
    """The website's own name for a pitch: og:site_name, else the part of the page title that is not 'write for us' wording, else the domain."""
    soup = BeautifulSoup(html or "", "html.parser")
    meta = soup.find("meta", attrs={"property": "og:site_name"})
    if meta and (meta.get("content") or "").strip():
        return meta["content"].strip()[:60]
    parts = [p.strip() for p in re.split(r"\s[-|" + "\u2013\u2014" + r":]\s", title or "") if p.strip()]
    plain = [p for p in parts if not re.search(r"write for|guest (post|blog)|guest-post|contribut|submit|guidelines", p, re.I) and len(p) <= 40]
    return plain[-1] if plain else domain.removeprefix("www.")


def _base_domain(host: str) -> str:
    """example.org for www.example.org and app.example.org; example.co.uk for blog.example.co.uk."""
    parts = host.lower().split(":")[0].removeprefix("www.").split(".")
    if len(parts) >= 3 and len(parts[-1]) == 2 and parts[-2] in ("co", "org", "ac", "gov", "com", "net", "ltd", "plc", "me", "nhs"):
        return ".".join(parts[-3:])
    return ".".join(parts[-2:])


# share buttons, comment replies, category/tag/author lists, feeds, logins, shops and files are never the guidelines or the form
SKIP_LINK = re.compile(r"[?&](share|replytocom|utm_\w+|amp|nb)=|/(category|categories|tag|tags|author|feed|page/\d+|wp-login\.php|login|cart|shop|product)/"
                       r"|\.(jpe?g|png|gif|webp|svg|pdf|zip|docx?)$", re.I)
CONTACT_HINT = re.compile(r"(contact|submission|online) form|contact page|form on (our|the) contact|via (our|the) contact|use (our|the) contact", re.I)


def related_pages(html: str, url: str, limit: int = 2) -> list[str]:
    """The website's own guidelines or submission pages linked from this page, best first: links that say submit, pitch, form,
    guidelines or write for us, on the same website or one of its own subdomains (app.example.org). When the page says to use the
    contact form, the contact page counts too. Share buttons, category and tag lists are never followed (2026-11-07)."""
    base = _base_domain(urlparse(url).netloc)

    def same(u):
        p = urlparse(u)
        return p.netloc.lower().removeprefix("www."), p.path.rstrip("/")
    here = same(url.split("#")[0])
    want_contact = bool(CONTACT_HINT.search(page_text(html)))
    scored: dict[str, int] = {}
    for a in BeautifulSoup(html or "", "html.parser").find_all("a", href=True):
        href = urljoin(url, a["href"]).split("#")[0]
        p = urlparse(href)
        if p.scheme not in ("http", "https") or _base_domain(p.netloc) != base or same(href) == here or SKIP_LINK.search(href):
            continue
        label = a.get_text(" ", strip=True)
        score = (3 if re.search(r"submi|form\b|pitch|apply", label, re.I) else 0) + (2 if GUIDE_LINK.search(label) else 0) + (1 if GUIDE_LINK.search(p.path) else 0) \
            + (2 if want_contact and re.search(r"contact", label + " " + p.path, re.I) else 0)
        if score:
            scored[href] = max(score, scored.get(href, 0))
    return [h for h, _ in sorted(scored.items(), key=lambda kv: -kv[1])][:limit]


def related_page(html: str, url: str) -> str | None:
    """The best guidelines or submission page linked from this page, if any."""
    return (related_pages(html, url, 1) or [None])[0]


# The guest-post guidelines page itself (its title, main heading or address says so), as opposed to an article that mentions guest posts
GUIDELINES_PAGE = re.compile(r"write[ -]?for[ -]?(us|me)|guest[ -]?(post|blog|blogger|blogging|article|author)s?[ -]+(guidelines?|polic(y|ies)|submissions?|program)"
                             r"|(submission|contributor|writer)s?[ -]guidelines|submit[ -](a[ -]|an[ -]|your[ -])?(guest|post|article|story)|become[ -]a[ -](contributor|writer)"
                             r"|(write|submit|send)[ -](us[ -])?an?[ -]guest[ -](post|blog|article)|\bcontribute\b|writers?[ -]wanted|pitch[ -]us", re.I)


def is_guidelines_page(html: str, url: str) -> bool:
    soup = BeautifulSoup(html or "", "html.parser")
    h1 = soup.find("h1")
    words = " ".join([soup.title.get_text(" ", strip=True) if soup.title else "", h1.get_text(" ", strip=True) if h1 else "",
                      urlparse(url).path.replace("-", " ").replace("_", " ").replace("/", " ")])
    return bool(GUIDELINES_PAGE.search(words))


# ------------------------------------------------------------------ email addresses on a page
EMAIL_RE = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)*\.[A-Za-z]{2,}")
NOT_FOR_SUBMISSIONS = re.compile(r"^(ads?|advert\w*|sales|sponsor\w*|partner\w*|marketing|press|pr|privacy|legal|abuse|no-?reply|jobs|careers|billing|unsubscribe)$", re.I)
GUEST_WORDS = re.compile(r"guest[ -]?(post|blog|article|author|contribut)|write for us|contribut|submission guidelines|submit (an |your )?(article|post|story|pitch)|pitch", re.I)


def _cf_decode(hexstr: str) -> str:
    """Cloudflare's email protection: the first byte is the key, the rest is the address XOR that key."""
    try:
        key = int(hexstr[:2], 16)
        return "".join(chr(int(hexstr[i:i + 2], 16) ^ key) for i in range(2, len(hexstr), 2)).lower()
    except ValueError:
        return ""


INVITES = re.compile(r"submi|guest|pitch|contribut|write for|send (us |me )?(the |your |an? )?(finished |completed |full )?(article|post|piece|story|idea|draft)", re.I)
# Addresses written out to keep robots away: "jane [at] example [dot] com", "jane(at)example.com", "janeATexampleDOTcom", "jane at example dot com"
_AT, _DOT = r"\s*[\[\(\{<]\s*at\s*[\]\)\}>]\s*", r"(?:\s*[\[\(\{<]\s*dot\s*[\]\)\}>]\s*|\.)"   # a plain dot only touching both parts: never the full stop
SPELLED = [re.compile(r"([A-Za-z0-9._%+-]+)" + _AT + r"([A-Za-z0-9-]+(?:" + _DOT + r"[A-Za-z0-9-]+)+)", re.I),
           re.compile(r"\b([a-z0-9._%+-]+)AT([a-z0-9-]+(?:DOT[a-z0-9-]+)+)\b"),
           re.compile(r"\b([A-Za-z0-9._%+-]+)\s+at\s+([A-Za-z0-9-]+(?:\s+dot\s+[A-Za-z0-9-]+)*\s+dot\s+(?:com|org|net|co|uk|io|au|ca|info|me|us|in|blog|ie|nz))\b", re.I)]


def spelled_out(text: str) -> set[str]:
    """The addresses a page writes out in words instead of showing them."""
    found = set()
    for pattern in SPELLED:
        for m in pattern.finditer(text or ""):
            domain = re.sub(r"\s*(?:[\[\(\{<]\s*dot\s*[\]\)\}>]|\s+dot\s+|DOT)\s*", ".", m.group(2), flags=re.I if pattern is not SPELLED[1] else 0)
            domain = re.sub(r"\s*\.\s*", ".", domain)
            address = (m.group(1) + "@" + domain).lower().strip(".")
            if EMAIL_RE.fullmatch(address):
                found.add(address)
    return found


def _addresses_in(el) -> set[str]:
    """The addresses an element shows: in its text (also written out in words), its mailto links and behind Cloudflare's email protection."""
    found = {m.lower() for m in EMAIL_RE.findall(el.get_text(" "))} | spelled_out(el.get_text(" "))
    for a in ([el] if el.name == "a" else []) + el.find_all("a", href=True):
        href = a.get("href") or ""
        if href.lower().startswith("mailto:"):
            found.update(m.lower() for m in EMAIL_RE.findall(unquote(href[7:].split("?")[0])))
        if "/cdn-cgi/l/email-protection#" in href:
            found.add(_cf_decode(href.split("#", 1)[1]))
    for c in ([el] if el.has_attr("data-cfemail") else []) + el.select("[data-cfemail]"):
        found.add(_cf_decode(c.get("data-cfemail") or ""))
    return found


SUBMISSION_BOX = re.compile(r"guest|submi|editor|editorial|contribut|writ|pitch|blog|articles?|stories|content", re.I)   # guestblogger@, editor@, submissions@


def address_invited(html: str, email: str, guidelines: bool = False) -> bool:
    """True when the page offers the address for submissions. Either it sits next to wording that invites them ("Ready to submit a
    post? Email us at ...": the paragraph it is in, or the one just before), or it sits in the page's own content (never the header,
    menu, sidebar or footer) and its name says it is for submissions (guestblogger@, editor@) or the page is the website's guest-post
    guidelines (2026-11-07: "Please send the post to neo@..." on a guidelines page was turned down before). A footer contact never counts."""
    email = email.lower()
    soup = BeautifulSoup(html or "", "html.parser")
    for block in soup.find_all(["p", "li", "td", "dd", "blockquote", "h2", "h3", "h4", "h5", "h6"]):
        if email not in _addresses_in(block):
            continue
        before = block.find_previous_sibling(lambda t: getattr(t, "get_text", None) and t.get_text(strip=True))
        if INVITES.search(block.get_text(" ")) or (before is not None and INVITES.search(before.get_text(" "))):
            return True
    if not (guidelines or SUBMISSION_BOX.search(email.split("@")[0])):
        return False
    spots = [s.parent for s in soup.find_all(string=True) if s.parent is not None and email in ({m.lower() for m in EMAIL_RE.findall(str(s))} | spelled_out(str(s)))]
    spots += [a for a in soup.find_all("a", href=True) if email in _addresses_in(a)] + [c for c in soup.select("[data-cfemail]") if email in _addresses_in(c)]
    return any(spot.name not in ("script", "style", "title") and not _in_site_chrome(spot) for spot in spots)


def page_emails(html: str) -> set[str]:
    """Every email address a page shows: in its text (also written out in words), in mailto links and behind Cloudflare's email protection."""
    soup = BeautifulSoup(html or "", "html.parser")
    found = {m.lower() for m in EMAIL_RE.findall(soup.get_text(" "))} | spelled_out(soup.get_text(" "))
    for a in soup.find_all("a", href=True):
        href = a["href"]
        if href.lower().startswith("mailto:"):
            found.update(m.lower() for m in EMAIL_RE.findall(unquote(href[7:].split("?")[0])))
        if "/cdn-cgi/l/email-protection#" in href:
            found.add(_cf_decode(href.split("#", 1)[1]))
    for el in soup.select("[data-cfemail]"):
        found.add(_cf_decode(el.get("data-cfemail") or ""))
    return {e for e in found if e and EMAIL_RE.fullmatch(e) and not re.search(r"\.(png|jpe?g|gif|webp|svg)$", e)}


# ------------------------------------------------------------------ the form
ROLES = [  # (role, words in the field's label, name, id or placeholder), checked in this order
    ("email", r"e-?mail"), ("first_name", r"first.?name|given.?name|forename|\bfirst\b|\[first\]"),
    ("last_name", r"last.?name|surname|family.?name|\blast\b|\[last\]"),
    ("website", r"website|web site|\burl\b|your (blog|site)|link to your"), ("bio", r"\bbio\b|about (you|the author|yourself)|author.?bio"),
    ("title", r"title|headline|subject"), ("phone", r"phone|mobile|\btel\b"), ("company", r"company|organi[sz]ation|business"),
    ("category", r"categor|topic|niche"), ("newsletter", r"newsletter|subscribe|marketing|mailing list|updates"),
    ("consent", r"agree|consent|terms|privacy|gdpr|accept|have read|i understand"), ("name", r"name|author"),
    ("article", r"article|content|post|body|message|story|text|pitch|details|description|comment|submission"),
]
HUMAN_CHECK = re.compile(r"human|robot|spam|equals|\b\d+\s*(?:plus|minus|times|\+|x)\s*\d+|what is \d|\bquiz|which is (?:bigger|larger|smaller|greater)|how many players", re.I)   # a question for a person: Contact Form 7 "quiz" fields too
PITCH_ROLES = [("bio", r"background|about you|about yourself|experience|education|bio\b"), ("links", r"links?|portfolio|published|previous|samples?|websites?"),
               ("pitch", r"topics?|interests?|ideas?|pitch|propos|write about|why")]   # a box for "your bio, with links" is a bio
ARTICLE_WORDS = re.compile(r"(your|the|full|paste|submit|type|enter) (article|post|story|submission|draft|manuscript)|article (text|body|content)|^(article|post|story|submission|content|body|message)\b", re.I)   # strong signs of a box for the article itself
HONEYPOT = re.compile(r"honeypot|\bhp\b|-hp\b|_hp\b|hp-|gform_validation_container|wpforms-field-hp|ak_hp|cf7-hp|leave this (field )?(empty|blank)"
                      r"|(?:url|e-?mail|website|address|name|phone)[_-]?check\b", re.I)   # e.g. website_url_check: a trap for robots
HIDDEN_CLASSES = {"hidden", "d-none", "hide", "is-hidden", "invisible", "sr-only", "screen-reader-text", "visually-hidden"}   # hidden by the page's own styles
CHROME_WORDS = ("footer", "sidebar", "widget-area", "masthead", "navbar", "offcanvas", "off-canvas", "site-header")
CAPTCHA_PARTS = ('.g-recaptcha, .h-captcha, .cf-turnstile, [data-sitekey], iframe[src*="recaptcha"], iframe[src*="hcaptcha"], iframe[src*="turnstile"], '
                 'textarea[name="g-recaptcha-response"], textarea[name="h-captcha-response"], input[name="cf-turnstile-response"]')
# forms hosted elsewhere: the page links to them, the system cannot fill them (yet)
EXTERNAL_FORMS = re.compile(r"docs\.google\.com/forms|forms\.gle/|typeform\.com|jotform\.com|airtable\.com/shr|tally\.so|submittable\.com|wufoo\.com|formstack\.com|cognitoforms\.com|forms\.office\.com", re.I)


def _label(soup, el) -> str:
    bits = [el.get("name") or "", el.get("id") or "", el.get("placeholder") or "", el.get("aria-label") or ""]
    if el.get("id"):
        lab = soup.find("label", attrs={"for": el["id"]})
        if lab:
            bits.append(lab.get_text(" ", strip=True))
    wrap = el.find_parent("label")
    if wrap:
        bits.append(wrap.get_text(" ", strip=True))
    return " ".join(bits).lower()


def _hidden(el) -> bool:
    if (el.get("type") or "").lower() == "hidden" or el.get("aria-hidden") == "true" or el.has_attr("hidden"):
        return True
    inside_form = True
    for node in [el, *el.parents]:
        if getattr(node, "attrs", None) is None:
            continue
        style = (node.get("style") or "").replace(" ", "").lower()
        if "display:none" in style or "visibility:hidden" in style:
            return True
        marker = " ".join([*(node.get("class") or []), node.get("id") or "", node.get("name") or ""])
        if HONEYPOT.search(marker):
            return True
        if inside_form:   # the page's own "hidden" classes and attributes count only between the field and its form (not "hidden on phones" ones)
            classes = node.get("class") or []
            if (HIDDEN_CLASSES & set(classes) and not any(":" in c for c in classes)) or (node is not el and (node.has_attr("hidden") or node.get("aria-hidden") == "true")):
                return True
        if node.name == "form":
            inside_form = False
    return False


def _comment_form(f) -> bool:
    """The blog's comment box under a post ("Leave a Reply"): never a place for an article."""
    return (f.get("id") or "") == "commentform" or "comment-form" in " ".join(f.get("class") or []) or "wp-comments-post" in (f.get("action") or "") \
        or f.find("textarea", attrs={"name": "comment"}) is not None


def _in_site_chrome(f) -> bool:
    """A form in the footer, sidebar, header or menu is site-wide (contact, newsletter), not the page's own submission form."""
    for node in f.parents:
        if getattr(node, "attrs", None) is None or node.name in ("body", "html"):
            break
        if node.name in ("main", "article") or node.get("role") == "main":
            return False   # the page's main content
        if node.name in ("footer", "aside", "nav", "header"):
            return True
        tokens = [*(node.get("class") or []), node.get("id") or ""]
        if any(w in t.lower() for t in tokens for w in CHROME_WORDS) or {"widget", "menu", "header"} & {t.lower() for t in tokens}:
            return True
    return False


def form_needs_person(html: str, mapping: dict) -> bool:
    """Whether the chosen form needs the owner: a CAPTCHA inside it, one on the page that cannot be tied to another form, or a question
    for a person. A CAPTCHA inside another form (for example the comment box under the guidelines) has nothing to do with this one."""
    if mapping.get("human"):
        return True
    soup = BeautifulSoup(html or "", "html.parser")
    forms, idx = soup.find_all("form"), (mapping.get("form") or {}).get("index", -1)
    mine = forms[idx] if 0 <= idx < len(forms) else None
    if mine is not None and mine.select(CAPTCHA_PARTS):
        return True
    widgets = soup.select(CAPTCHA_PARTS)
    if widgets:
        return any(w.find_parent("form") is None for w in widgets)
    return bool(soup.select(".grecaptcha-badge")) or any(re.search(r"recaptcha|hcaptcha|turnstile", s.get("src") or "", re.I) for s in soup.find_all("script"))


def _required(soup, el) -> bool:
    if el.has_attr("required") or el.get("aria-required") == "true" or "required" in " ".join(el.get("class") or []).lower():
        return True
    if el.get("id"):
        lab = soup.find("label", attrs={"for": el["id"]})
        if lab and "*" in lab.get_text():
            return True
    return False


def map_form(html: str, url: str) -> dict:
    """The submission form and a role for each of its fields. ok=False when it cannot be filled with confidence."""
    soup = BeautifulSoup(html or "", "html.parser")
    forms = [f for f in soup.find_all("form") if not (f.find("input", {"type": "search"}) or "search" in (f.get("role") or "").lower()
                                                       or "search" in (f.get("action") or "").lower()) and not _comment_form(f) and not _in_site_chrome(f)]
    best = max(forms, key=lambda f: (f.find("textarea") is not None, len(f.find_all(["input", "textarea", "select"]))), default=None)
    if best is None or best.find("textarea") is None:
        outside = next((a["href"] for a in soup.find_all("a", href=True) if EXTERNAL_FORMS.search(a["href"])), None)
        if outside:
            return {"ok": False, "problem": f"submissions go through an outside form ({urlparse(outside).netloc}) that the system cannot fill yet"}
        return {"ok": False, "problem": "no submission form with a text box for the article (comment boxes and the site's footer or sidebar forms are never used)"}
    all_forms = soup.find_all("form")
    out = {"ok": True, "form": {"id": best.get("id") or "", "action": best.get("action") or "", "index": all_forms.index(best)},
           "fields": [], "problems": [], "human": [], "kind": "article"}
    used = set()
    area_text = [_label(soup, e) for e in best.find_all("textarea") if not _hidden(e)]
    if area_text and not any(ARTICLE_WORDS.search(t) for t in area_text) and any(re.search(r"topic|interest|idea|pitch|propos|background|about you|tell us", t) for t in area_text):
        out["kind"] = "pitch"   # an application: topics, background, links. The article follows if the editors say yes
    for el in best.find_all(["input", "textarea", "select"]):
        kind_attr = (el.get("type") or "text").lower()
        if el.name == "input" and kind_attr in ("submit", "button", "image", "reset", "hidden"):
            continue
        required, text = _required(soup, el), _label(soup, el)
        if _hidden(el) or not (el.get("name") or el.get("id")):   # a trap for robots, or a box drawn by the page's own scripts
            if required and kind_attr != "hidden":   # traps are never required: a required box the system leaves alone means it cannot send the form
                around = el.find_parent(["div", "p", "label", "fieldset"])
                what = " ".join((text.strip() or (around.get_text(" ", strip=True) if around else "") or el.name).split())[:50]
                out["problems"].append(f"a required field the system cannot fill ({what})")
            continue
        if "captcha" in text or (el.get("name") or "").endswith("-response"):
            continue
        if el.name == "input" and kind_attr in ("text", "number", "tel") and HUMAN_CHECK.search(text):
            shown = text.replace(el.get("name") or "~", " ").replace(el.get("id") or "~", " ")
            out["human"].append({"name": el.get("name") or "", "id": el.get("id") or "", "label": re.sub(r"\s+", " ", shown).strip(" *")[:80]})   # a question for a person: never answered
            continue
        kind = "textarea" if el.name == "textarea" else "select" if el.name == "select" else "checkbox" if kind_attr == "checkbox" else \
            "radio" if kind_attr == "radio" else "file" if kind_attr == "file" else "text"
        role = "email" if kind_attr == "email" else "website" if kind_attr == "url" else "phone" if kind_attr == "tel" else None
        if role is None:
            for r, pattern in ROLES:
                if re.search(pattern, text):
                    role = r
                    break
        if kind == "textarea" and role in ("first_name", "last_name"):
            role = None
        if kind == "textarea" and out["kind"] == "pitch":
            role = next((r for r, pattern in PITCH_ROLES if re.search(pattern, text)), None)
        elif kind == "textarea" and role not in ("bio", "article"):
            role = "article" if "article" not in used else "bio" if re.search(r"about|bio", text) else role
        if kind == "checkbox" and role not in ("consent", "newsletter"):
            role = None
        fillable = role in ("email", "first_name", "last_name", "website", "bio", "title", "company", "category", "consent", "name", "article", "pitch", "links")
        if kind in ("radio", "file") or not fillable or (role in used and role not in ("consent", "email")):   # a second email box is "confirm email"
            if required:
                out["problems"].append(f"a required field the system cannot fill ({text[:50] or kind})")
            continue
        options = [(o.get("value") if o.get("value") is not None else o.get_text(strip=True), o.get_text(" ", strip=True)) for o in el.find_all("option")] if kind == "select" else []
        out["fields"].append({"name": el.get("name") or "", "id": el.get("id") or "", "kind": kind, "role": role, "required": required,
                              "options": [o for o in options if o[0]][:40]})
        used.add(role)
    if out["kind"] == "pitch":
        if not used & {"pitch", "bio"}:
            out["problems"].append("no field for a pitch or background")
    elif "article" not in used:
        out["problems"].append("no field for the article")
    if "email" not in used:
        out["problems"].append("no field for a reply email")
    out["ok"] = not out["problems"]
    return out


def field_values(mapping: dict, article: dict | None, author: str, email: str, website: str, brand: str, bio_plain: str, niche_words: list[str],
                 pitch: str = "", title: str = "") -> list[dict]:
    """The payload fields: one value per mapped field (a field with nothing true to put in it is left out)."""
    body = plain_text(article["body_markdown"]) if article else ""   # without a bio field, the bio is already the article's last paragraph
    first, _, rest = author.partition(" ")
    values = {"name": author, "first_name": first, "last_name": rest or "Team", "email": email, "website": website, "title": (article or {}).get("title", "") or title,
              "article": body, "bio": bio_plain, "company": brand, "consent": True, "pitch": pitch, "links": website}
    out = []
    for f in mapping["fields"]:
        if f["role"] == "category":
            words = {w.lower() for w in niche_words + re.findall(r"[a-zA-Z]{4,}", (article or {}).get("title", ""))}
            good = next((v for v, label in f["options"] if any(w in label.lower() for w in words)), None)
            choice = good or (next((v for v, label in f["options"] if v and not re.search(r"select|choose|--", label, re.I)), None) if f["required"] else None)
            if choice is None:
                continue
            out.append({"name": f["name"], "id": f["id"], "kind": "select", "role": "category", "value": choice, "required": f["required"]})
        elif f["role"] in values and values[f["role"]] not in ("", None):
            out.append({"name": f["name"], "id": f["id"], "kind": f["kind"], "role": f["role"], "value": values[f["role"]], "required": f["required"]})
    return out


def click_link(page_url: str, payload: dict) -> str:
    """page address + #bl=<data>: the fragment never reaches the website's server; only the owner's extension reads it."""
    data = base64.urlsafe_b64encode(json.dumps(payload, ensure_ascii=False).encode("utf-8")).decode("ascii").rstrip("=")
    return page_url.split("#")[0] + "#bl=" + data


def find(store, site: dict, settings: dict, cfg: dict, provider, eff: dict, known: set[str]) -> dict:
    """Searches with submission-intent phrases only (never resource or link-list phrases), with the usual credit guards.
    The words are the site's guest_posts.search_words (its niche, without place names), and websites from any country count:
    a good website on the same subject gives as good a backlink wherever it is based. Gemini then judges the fit."""
    narrowed = {**settings, "discovery": {**settings["discovery"], "query_templates": TEMPLATES}}
    words = list((cfg.get("guest_posts") or {}).get("search_words") or [])
    if not words:
        return discover.search_for_candidates(store, site, narrowed, cfg, provider, eff, known)
    site2 = {**site, "restrict_to_uk": False, "priority_keywords": [*(site.get("priority_keywords") or []), *words]}
    return discover.search_for_candidates(store, site2, narrowed, cfg, provider, {**eff, "discovery_keywords": words}, known)


# A one-off re-check is only for pages turned down BEFORE the rule it corrects. Without these dates the 2026-11-03 re-check took every
# page turned down for "no form" since then and checked it a second time the next morning: about 10 of each site's 25 daily checks
# were spent re-reading the day before's pages (found 2026-11-07).
EMAIL_ROUTE_FROM = "2026-11-03T11:30"   # the email route went live
RULES_0711_FROM = "2026-11-08T00:00"    # the corrected address, login-box and Word-file rules run from the 2026-11-08 run
# Notes of guidelines pages turned down under rules that were too strict until 2026-11-07; each such page is checked once more
RECHECK_0711 = re.compile(r"is not offered for guest posts|needs an account or refuses automated|no submission form the system can fill and no email address"
                          r"|Word file or a Google Doc|no clear email address for guest posts on the page itself")


def migrate(store, report: list[str]) -> None:
    """Once per store: DEV.to/WordPress.com jobs become history (legacy), and the pages the old system set aside as
    'needs a person' are checked again under the guest-post rules (its CAPTCHA detector mislabelled many of them)."""
    moved = marked = emailed = corrected = 0
    again: set[str] = set()
    for job in store.jobs.values():
        last = (job.get("history") or [{}])[-1].get("note", "")
        turned_down_at = (job.get("history") or [{}])[-1].get("at", "")
        if job.get("adapter") == "web" and job["status"] == S.REJECTED and job.get("guest_checked") and not job.get("rules_0711_checked") \
                and turned_down_at < RULES_0711_FROM and RECHECK_0711.search(last) and job["domain"] not in again \
                and GUIDELINES_PAGE.search(" ".join([job.get("title") or "", urlparse(job["page_url"]).path.replace("-", " ").replace("/", " ")])):
            job["history"].append({"at": store.now_iso(), "from": job["status"], "to": S.DISCOVERED,
                                   "note": "checked again under the corrected rules: addresses written out or given in the guidelines, menu login boxes, Word files (2026-11-07)"})
            job.update(status=S.DISCOVERED, rules_0711_checked=True, next_action_at=None, retry_count=0)
            again.add(job["domain"])
            corrected += 1
        if job.get("adapter") == "web" and job["status"] == S.REJECTED and job.get("guest_checked") and not job.get("email_rules_checked") \
                and turned_down_at < EMAIL_ROUTE_FROM and re.search(r"email only|no submission form", last):
            job["history"].append({"at": store.now_iso(), "from": job["status"], "to": S.DISCOVERED,
                                   "note": "checked again: websites that ask for guest posts by email are now used (owner, 2026-11-03)"})
            job.update(status=S.DISCOVERED, email_rules_checked=True, next_action_at=None, retry_count=0)
            emailed += 1
        if job.get("adapter") in ("devto", "wordpresscom") and not job.get("legacy"):
            job["legacy"] = True
            marked += 1
        if job.get("adapter") == "web" and job["status"] == S.UNSUPPORTED_PUBLICATION_METHOD and not job.get("guest_requeued"):
            job["history"].append({"at": store.now_iso(), "from": job["status"], "to": S.DISCOVERED, "note": "checked again under the guest-post rules"})
            job.update(status=S.DISCOVERED, guest_requeued=True, next_action_at=None)
            moved += 1
    if marked or moved or emailed or corrected:
        store.save()
        report.append(f"- Guest posts: {marked} old platform job(s) kept as history, {moved + emailed + corrected} earlier page(s) queued to be checked again"
                      + (f" ({emailed} that ask for submissions by email)" if emailed else "")
                      + (f" ({corrected} guidelines page(s) turned down under the old, too strict rules)" if corrected else "") + ".")


# ------------------------------------------------------------------ the strict check
def acceptance_prompt(text: str, site_label: str, emails=()) -> str:
    shown = ("\nEmail addresses shown on the page: " + ", ".join(sorted(emails))) if emails else ""
    return ("You check whether a website accepts articles written by outside authors, as a place to publish for " + site_label + ".\n"
            "Read the page text below (it may include the website's guidelines) and answer ONLY with a JSON object:\n"
            '{"accepts_outside_articles": true or false, "accepts_quote": "the exact sentence from the text that shows it", '
            '"links_allowed": true or false, "links_quote": "the exact sentence from the text that says contributors may include a link to their own website, in the article or in the author bio", '
            '"paid": true or false, "paid_quote": "the exact sentence if the website charges for publishing, else empty", '
            '"min_words": number or null, "max_words": number or null, "topics": "what topics they want, short", "rules": "other rules for contributors, short", '
            '"how_to_submit": "form" or "email" or "other", "submission_email": "the exact email address the text says to send guest posts or pitches to (never a general contact or footer address), else empty", '
            '"pitch_first": true or false (true if they want a topic idea or pitch before the full article), '
            '"subject_line": "the exact email subject line they ask for, else empty", '
            '"attachment": "none" or "word" or "google_doc" or "other" (what they want instead of the article as text in the email: a Word file, '
            'a Google Doc link, another kind of file, or none), '
            '"ai_restricted": true or false (true if the text forbids, limits or discourages AI-generated or AI-assisted writing in any way, or says submissions are checked with AI detectors), '
            '"no_link_builders": true or false (true if the text asks SEO agencies, link builders or people who want backlinks not to submit), '
            '"members_only": true or false (true if only members, subscribers, customers or accredited people of the website or of an organisation may contribute: a membership, a paid account or a qualification is needed), '
            '"competitor": true or false (true if the website itself sells the same kind of service as ' + site_label + ', so it is a competitor rather than a publication)}\n'
            "Be strict. accepts_outside_articles is true only if the text clearly invites people outside the organisation to send articles or guest "
            "posts for publication. links_allowed is true only if the text clearly says a contributor may include a link to their own website; "
            "if it says nothing about links, it is false. Copy quotes word for word from the text.\n\nPage text:\n" + text[:7000] + shown)


def parse_acceptance(raw: str) -> dict:
    t = raw.strip()
    if t.startswith("```"):
        t = re.sub(r"^```(?:json)?|```$", "", t, flags=re.I).strip()
    d = json.loads(t[t.find("{"): t.rfind("}") + 1])

    def num(v):
        try:
            return int(v) if v not in (None, "", "null") else None
        except (TypeError, ValueError):
            return None
    email = str(d.get("submission_email") or "").strip().lower()
    if email and "@" not in email:   # the model may copy an address the way the page writes it out ("jane [at] example [dot] com")
        email = next(iter(spelled_out(email)), email)
    attachment = str(d.get("attachment") or "").strip().lower().replace(" ", "_")
    if attachment not in ("none", "word", "google_doc", "other"):
        attachment = "other" if d.get("needs_attachment") is True or attachment in ("true", "yes") else "none"
    return {"accepts": bool(d.get("accepts_outside_articles")), "accepts_quote": str(d.get("accepts_quote") or "")[:300],
            "links": bool(d.get("links_allowed")), "links_quote": str(d.get("links_quote") or "")[:300], "paid": bool(d.get("paid")),
            "paid_quote": str(d.get("paid_quote") or "")[:300], "min_words": num(d.get("min_words")), "max_words": num(d.get("max_words")),
            "topics": str(d.get("topics") or "")[:200], "rules": str(d.get("rules") or "")[:400],
            "how": str(d.get("how_to_submit") or "").lower(), "email": email,
            "pitch_first": bool(d.get("pitch_first")), "subject": str(d.get("subject_line") or "")[:150], "attachment": attachment,
            "ai": d.get("ai_restricted") is True, "no_link_builders": d.get("no_link_builders") is True, "members_only": d.get("members_only") is True,
            "competitor": bool(d.get("competitor"))}


def word_range(acc: dict) -> tuple[int, int] | None:
    """The length to write, inside the website's limits; None when its limits cannot be met."""
    lo = max(500, acc["min_words"] or 600)
    hi = acc["max_words"] or max(lo + 600, 1200)
    hi = min(hi, 2200)
    if hi < 500 or lo > 2000:
        return None
    return lo, max(hi, lo + 150)


def _reject(store, job: dict, reason: str) -> str:
    store.transition(job, S.REJECTED, reason, guest_checked=True)
    return "rejected"


def check_candidate(store, job: dict, cfg: dict, eff: dict, settings: dict, llm, engine: str, fetch, budget: list) -> str:
    """One discovered page through every check. Returns qualified | rejected | unreadable | llm_down."""
    recheck = bool(job.get("rules_0711_checked"))   # turned down under the old, too strict rules: its own earlier no does not count against it
    if any(j is not job and j["domain"] == job["domain"] and (j.get("adapter") == "guest" or j.get("guest_checked"))
           and not (recheck and j["status"] == S.REJECTED) for j in store.jobs.values()):
        return _reject(store, job, "this website was already considered (a website is used once, ever)")
    why = excluded(job["domain"], job.get("title") or "", eff, settings)   # before opening the page: no request is spent on it
    if why:
        return _reject(store, job, f"not used: {why}")
    html, how = discover.fetch_for_inspection(store, job["page_url"], engine, cfg, budget, fetch=fetch)
    if html is None:
        executor.schedule_retry(store, job, cfg, "unreachable", how)
        return "unreadable"
    first = qualify.inspect_page(html, job["page_url"])
    why = excluded(job["domain"], first["title"] or job.get("title") or "", eff, settings)
    if why:
        return _reject(store, job, f"not used: {why}")
    if not robots_allows(job["page_url"], fetch):
        return _reject(store, job, "the website's robots.txt asks automated tools not to read this page")
    pages = [(job["page_url"], html, first)]
    for other in related_pages(html, job["page_url"]):   # the guidelines and the form are often on separate pages
        got = fetch(other)
        if got.status == 200 and got.text:
            pages.append((other, got.text, qualify.inspect_page(got.text, other)))
    if any(p[2]["mechanism"] in ("automation_prohibited", "account_required") for p in pages):
        return _reject(store, job, "the website needs an account or refuses automated submissions")
    form_page = next(((u, h, i, m) for u, h, i in pages for m in [map_form(h, u)] if i["mechanism"] in ("form_submission", "captcha_required") and m["ok"]), None)
    text = " ".join(page_text(h) for _, h, _ in pages)
    emails = set().union(*(page_emails(h) for _, h, _ in pages)) if form_page is None else set()
    if form_page is None and not (emails and GUEST_WORDS.search(text)):
        problems = [p for u, h, i in pages for p in map_form(h, u).get("problems", []) or [map_form(h, u).get("problem")] if p]
        return _reject(store, job, "no submission form the system can fill and no email address for submissions" + (f" ({problems[0]})" if problems else ""))
    form_url, form_html, form_info, mapping = form_page or (job["page_url"], html, first, None)   # no form: the email route
    if not LINK_WORDS.search(text):
        return _reject(store, job, "no clear statement that contributors may link to their own website (the page never mentions links or an author bio)")
    plain = text.replace("’", "'").replace("‘", "'").replace("–", "-").replace("‑", "-")   # curly apostrophes and special hyphens must not hide a rule
    rule = AI_RULES.search(plain)
    if rule:
        return _reject(store, job, f"the website does not accept AI-written articles (\"{rule.group(0).strip()[:90]}\"); the articles are written with AI, so it is never used")
    rule = NO_LINK_BUILDERS.search(plain)
    if rule:
        return _reject(store, job, f"the website asks people who want backlinks not to submit (\"{rule.group(0).strip()[:90]}\")")
    rule = MEMBERS_ONLY.search(plain)
    if rule:
        return _reject(store, job, f"the website only publishes its own members, and we are not members (\"{rule.group(0).strip()[:90]}\")")
    qcfg = cfg.get("qualification") or {}
    label = qcfg.get("site_label", "a UK hypnotherapy practice")
    acc = _ask(store, job, cfg, llm, lambda: parse_acceptance(llm.generate(acceptance_prompt(text, label, emails))[0]))
    if isinstance(acc, str):
        return acc
    if acc["competitor"]:   # a second guard beside the competitor words: competitors are never contacted, by form or by email
        return _reject(store, job, "not used: a competitor (it sells the same kind of service; competitors are never contacted)")
    if acc["paid"]:
        return _reject(store, job, "the website charges for publishing (paid links are never used)")
    if acc["ai"]:
        return _reject(store, job, "the website does not accept AI-written articles; the articles are written with AI, so it is never used")
    if acc["no_link_builders"]:
        return _reject(store, job, "the website asks people who want backlinks not to submit")
    if acc["members_only"]:
        return _reject(store, job, "the website only publishes its own members, and we are not members")
    if not (acc["accepts"] and quote_found(acc["accepts_quote"], text)):
        return _reject(store, job, "no clear statement that the website accepts articles from outside authors")
    if not (acc["links"] and quote_found(acc["links_quote"], text)):
        return _reject(store, job, "no clear statement that contributors may link to their own website")
    words = word_range(acc)
    if words is None:
        return _reject(store, job, f"the website's length limits ({acc['min_words']} to {acc['max_words']} words) do not suit an article")
    to = acc["email"]
    if mapping is None:   # the email route: only an address the page itself shows and asks for guest posts to go to
        if acc["how"] != "email" or to not in emails:
            return _reject(store, job, "no clear email address for guest posts on the page itself")
        if not any(address_invited(h, to, is_guidelines_page(h, u)) for u, h, _ in pages):   # our own code confirms it, beside the model's answer
            return _reject(store, job, f"the address {to} is not offered for guest posts on the page (only an address next to an invitation to submit, "
                                       "or in the guidelines themselves, is used)")
        if NOT_FOR_SUBMISSIONS.match(to.split("@")[0]):
            return _reject(store, job, f"the address {to} is for advertising, sales or press, not for guest posts")
        if acc["attachment"] in ("google_doc", "other"):
            return _reject(store, job, "the website wants a Google Doc link or another kind of file, which the system cannot send yet")
        if to in shared_state.contacted_emails() or any(j is not job and (j.get("guest") or {}).get("email_to") == to for s_ in [store] for j in s_.jobs.values()):
            return _reject(store, job, f"{to} has been emailed before (an address is contacted once, ever)")
    # the fit is judged only for a website that has passed everything above, so most pages cost one Gemini request, not two
    rel = _ask(store, job, cfg, llm, lambda: qualify.parse_relevance(llm.generate(qualify.relevance_prompt(
        first, eff.get("discovery_keywords") or [], label, qcfg.get("fit_criteria", "fit with hypnotherapy, mental wellbeing, anxiety, stress, sleep, behavioural change")))[0]))
    if isinstance(rel, str):
        return rel
    if rel["relevance"] < MIN_RELEVANCE or rel["quality"] < MIN_QUALITY:
        return _reject(store, job, f"not a good enough fit (relevance {rel['relevance']:.0f}, quality {rel['quality']:.0f}: {rel['reason']})")
    by_email = mapping is None
    if by_email:
        mapping = {"ok": True, "form": {}, "fields": [], "problems": [], "human": [], "kind": "pitch" if acc["pitch_first"] else "article"}
    guest = {"form_url": form_url, "site_name": site_name_of(form_html, job["domain"], first["title"]), "captcha": False if by_email else form_needs_person(form_html, mapping),
             "mapping": mapping, "accepts_quote": acc["accepts_quote"], "links_quote": acc["links_quote"], "topics": acc["topics"],
             "rules": acc["rules"], "words": list(words), "relevance": rel["relevance"], "quality": rel["quality"], "reason": rel["reason"],
             "checked_at": store.now_iso(), "route": "email" if by_email else "form"}
    if by_email:
        guest.update(email_to=to, subject_line=acc["subject"], attach_word=acc["attachment"] == "word")
    store.transition(job, S.QUALIFIED, f"accepts outside articles and links; {'submissions by email to ' + to if by_email else 'form mapped'}",
                     adapter="guest", guest=guest, guest_checked=True)
    store.bump("qualified")
    return "qualified"


def _ask(store, job: dict, cfg: dict, llm, call):
    """One Gemini question for the check. The parsed answer, or the check's result when there is none."""
    try:
        return call()
    except LLMError as e:
        if e.category in LLM_DOWN:
            return "llm_down"
        executor.schedule_retry(store, job, cfg, e.category, str(e))
        return "unreadable"
    except (ValueError, KeyError, TypeError):
        return _reject(store, job, "the acceptance check gave no usable answer, so the website is not used")


class _GeminiDown:
    """Stands in for Gemini after it has failed once in a pass: it answers at once with "busy", so no more requests (or waiting) are spent on it."""

    def generate(self, prompt):
        raise LLMError("busy", "Gemini was unavailable earlier in this pass", True, temporary=True)


GEMINI_DOWN = _GeminiDown()


def check_pass(store, cfg: dict, eff: dict, settings: dict, llm, engine: str, fetch, report: list[str]) -> None:
    per_run, daily = cfg["discovery"]["firecrawl_scrapes_per_run"], (cfg.get("quotas") or {}).get("firecrawl_scrapes_per_day")
    budget = [per_run if daily is None else max(0, min(per_run, daily - store.used_today("firecrawl")))]   # the daily limit holds however often the workflow runs
    phrases = [t.split('"')[1] for t in TEMPLATES]   # pages found by a "write for us" style search go first: they are the likely ones
    pending = [j for j in store.in_states(S.DISCOVERED) if j["adapter"] == "web"]
    skipped = 0
    for job in pending:   # journals, platforms, shops, social networks and competitors are turned down without opening them
        why = excluded(job["domain"], job.get("title") or "", eff, settings)
        if why:
            _reject(store, job, f"not used: {why}")
            skipped += 1
    if skipped:
        report.append(f"- {skipped} page(s) turned down without opening them (journals, platforms, shops, social networks or competitors).")
    waiting = sorted((j for j in pending if j["status"] == S.DISCOVERED),
                     key=lambda j: not any(ph in (j.get("discovery_source") or "") for ph in phrases))[:cfg["discovery"]["inspect_per_run"]]
    counts, down = {}, False
    for job in waiting:
        result = check_candidate(store, job, cfg, eff, settings, GEMINI_DOWN if down else llm, engine, fetch, budget)
        counts[result] = counts.get(result, 0) + 1
        if result == "llm_down" and not down:
            # Gemini is out of reach for now: the websites that need it wait for the next run, but the rest of the pass carries on with the
            # system's own checks (form, address, link wording, robots, competitors), so the pile of unchecked pages does not keep growing.
            down = True
            report.append("- Guest posts: Gemini is unavailable, so the websites that need it are checked at the next run; the others are still checked now.")
        if result == "rejected":
            report.append(f"- {job['domain']}: not used ({job['history'][-1]['note'][:140]})")
        elif result == "qualified":
            g = job["guest"]
            how = f"by email to {g['email_to']}" if g.get("route") == "email" else "CAPTCHA, needs your click" if g["captcha"] else "no CAPTCHA, sent automatically"
            report.append(f"- {job['domain']}: QUALIFIED ({how}; relevance {g['relevance']:.0f})")
    store.save()
    if waiting:
        report.append(f"- Guest posts checked: {len(waiting)} website(s): {counts}")


# ------------------------------------------------------------------ writing and sending
def _bio(cfg: dict) -> str:
    gp, home = cfg.get("guest_posts") or {}, cfg["verification"]["canonical_hosts"][-1]
    return (gp.get("bio") or "").replace("{home}", f"https://{home}/").replace("{brand}", cfg["brand_name"])


def pick_topic(store, cfg: dict, guest: dict) -> dict | None:
    from src.backlinks import pipeline
    used = pipeline._used_topic_keys(store)
    unused = [t for t in cfg["topics"] if t["key"] not in used]
    wanted = set(re.findall(r"[a-z]{4,}", (guest.get("topics", "") + " " + guest.get("site_name", "")).lower()))
    return max(unused, key=lambda t: len(wanted & set(re.findall(r"[a-z]{4,}", f"{t['title_hint']} {t['angle']} {t['audience']}".lower()))), default=None)


def pitch_ideas(cfg: dict, job: dict, store) -> list[dict]:
    """Three unused article ideas, the ones that fit what the website wants best first."""
    from src.backlinks import pipeline
    used = pipeline._used_topic_keys(store)
    wanted = set(re.findall(r"[a-z]{4,}", (job["guest"].get("topics", "") + " " + job["guest"].get("site_name", "")).lower()))
    unused = [t for t in cfg["topics"] if t["key"] not in used]
    return sorted(unused, key=lambda t: -len(wanted & set(re.findall(r"[a-z]{4,}", f"{t['title_hint']} {t['angle']}".lower()))))[:3]


def pitch_text(cfg: dict, job: dict, store, author: str) -> str:
    """The pitch for an application form: three unused article ideas, from the site's own topics. No model call."""
    ideas = pitch_ideas(cfg, job, store)
    lo, hi = job["guest"].get("words") or [600, 1200]
    lines = [f"Hello,", "", f"I would like to contribute articles to {job['guest'].get('site_name') or job['domain']}. Ideas I can write for your readers:"]
    lines += [f"- {t['title_hint']}" for t in ideas]
    lines += ["", f"Each would be an original, practical guide for your readers, about {lo} to {hi} words, written for your website and not published "
                  "elsewhere. I am happy to change the topic, angle or length to suit you.", "", "Thank you,", author]
    return "\n".join(lines)


def prepare_pitch(store, job: dict, cfg: dict) -> str:
    store.transition(job, S.CONTENT_READY, "application pitch prepared (no article is written until the editors say yes)",
                     title=f"Pitch to {job['guest'].get('site_name') or job['domain']}", written_on=store.today(), expected_urls=[f"https://{cfg['verification']['canonical_hosts'][-1]}/"])
    return "written"


def write_article(store, job: dict, cfg: dict, llm) -> str:
    """QUALIFIED -> CONTENT_READY. Returns written | no_topic | llm_down | failed."""
    from src.backlinks import pipeline
    g = job["guest"]
    topic = pick_topic(store, cfg, g)
    if topic is None:
        return "no_topic"
    has_bio = any(f["role"] == "bio" for f in g["mapping"]["fields"]) or g.get("route") == "email"   # an email carries the bio itself
    home = cfg["verification"]["canonical_hosts"][-1]
    bio = _bio(cfg).replace("{", "{{").replace("}", "}}")   # the home address is already in; braces in a name stay literal through format()
    gp = cfg.get("guest_posts") or {}
    host = f"{g['site_name']} ({job['domain']})"
    brief = " ".join(x for x in (f"The author is {gp['author']}." if gp.get("author") else "", (gp.get("brief") or "").strip(),
                                 f"The article will be published on {host}" + (f", whose editors want: {g['topics']}." if g.get("topics") else ".")) if x)
    cfg2 = {**cfg, "article_venue": f"{host}, a website that publishes articles by outside contributors", "article_brief": brief,
            "article_words": tuple(g["words"]), "article_rules": " ".join(x for x in (g.get("rules", ""), ("Topics they want: " + g["topics"]) if g.get("topics") else "") if x)[:500],
            "disclosure": "" if has_bio else "\n\n---\n\n*About the author: " + bio + "*"}
    topic_for_site = {**topic, "audience": f"the readers of {g['site_name']}, in particular {topic['audience']}"}
    bodies, anchors, titles = pipeline._history(store, "guest")
    try:
        article = C.build_article(topic_for_site, cfg2, llm, anchors, bodies, titles)
    except C.ContentError as e:
        executor.schedule_retry(store, job, cfg, e.category, str(e), count=e.category not in LLM_DOWN)
        return "llm_down" if e.category in LLM_DOWN else "failed"
    path = store.save_content(job["id"], json.dumps({k: article[k] for k in ("title", "description", "tags", "body_markdown")}, indent=2, ensure_ascii=False))
    store.bump("generated")
    store.transition(job, S.CONTENT_READY, f"article written for {job['domain']} ({article['word_count']} words)", topic_key=topic["key"],
                     title=article["title"], anchor_text=article["anchor"], target_url=topic["target_url"], content_path=path,
                     expected_urls=[topic["target_url"], f"https://{home}/"], written_on=store.today())
    return "written"


def payload_for(store, job: dict, cfg: dict, site: dict) -> dict:
    g = job["guest"]
    article = json.loads(store.read_content(job["content_path"])) if job.get("content_path") else None
    gp, contact = cfg.get("guest_posts") or {}, site.get("contact") or {}
    bio_plain = plain_text(_bio(cfg))
    pitch = g["mapping"].get("kind") == "pitch"
    ideas = pitch_ideas(cfg, job, store) if pitch else []   # an application's "proposed title" box gets the first idea
    fields = field_values(g["mapping"], article, gp.get("author") or contact.get("name") or cfg["brand_name"], contact.get("email", ""),
                          f"https://{cfg['verification']['canonical_hosts'][-1]}/", cfg["brand_name"], bio_plain,
                          list(site.get("priority_keywords") or []),
                          pitch_text(cfg, job, store, gp.get("author") or contact.get("name") or cfg["brand_name"]) if pitch else "",
                          title=ideas[0]["title_hint"] if ideas else "")
    return {"id": job["id"], "site": cfg["brand_name"], "back": f"{DASHBOARD}?sent={job['id']}", "form": g["mapping"]["form"], "fields": fields,
            "wait_for": g["mapping"].get("human", [])}


def _sent_today(store) -> int:
    """Today's submissions made by the system: sent automatically today, or a click link made today. A link from an earlier day
    that the owner sends today does not take one of today's places."""
    today = store.today()
    return sum(1 for j in store.jobs.values() if j.get("adapter") == "guest" and (
        (j.get("guest") or {}).get("link_on") == today or ((j.get("guest") or {}).get("sent_on") == today and (j.get("guest") or {}).get("how") in ("automatically", "by email"))))


def write_and_send(store, cfg: dict, site: dict, llm, report: list[str], sender=None, shots_dir: Path | None = None, mailer=None) -> None:
    gp = cfg.get("guest_posts") or {}
    per_day = int(cfg["limits"]["max_publications_per_platform_per_day"])
    live = bool(gp.get("submit", False))
    ready = sorted((j for j in store.jobs.values() if j.get("adapter") == "guest" and j["status"] in (S.QUALIFIED, S.CONTENT_READY)),
                   key=lambda j: (j["status"] != S.CONTENT_READY, -(j["guest"].get("relevance") or 0)))
    written_today = sum(1 for j in store.jobs.values() if j.get("adapter") == "guest" and j.get("written_on") == store.today())
    for job in ready:
        if (_sent_today(store) if live else written_today) >= per_day:
            break
        if job["status"] == S.QUALIFIED and written_today >= per_day + 2:
            report.append("- Guest posts: enough articles were written today; the rest wait for tomorrow (this keeps Gemini use low).")
            break
        if job["status"] == S.QUALIFIED:
            result = prepare_pitch(store, job, cfg) if job["guest"]["mapping"].get("kind") == "pitch" else write_article(store, job, cfg, llm)
            store.save()
            if result == "llm_down":
                report.append("- Guest posts: Gemini is unavailable, so no more articles today.")
                break
            if result == "no_topic":
                report.append("- Guest posts: no unused topics left; new ones are generated when Gemini is available.")
                break
            if result != "written":
                report.append(f"- {job['domain']}: the article did not pass the checks; it is retried later.")
                continue
            written_today += 1
            report.append(f"- {job['domain']}: {'pitch prepared' if job['guest']['mapping'].get('kind') == 'pitch' else 'article written'} ('{job['title']}').")
        if not live:
            report.append(f"- {job['domain']}: ready, NOT sent (review mode: guest_posts.submit is false).")
            continue
        send(store, job, cfg, site, report, sender, shots_dir, mailer)
        store.save()


def send(store, job: dict, cfg: dict, site: dict, report: list[str], sender=None, shots_dir: Path | None = None, mailer=None) -> str:
    g = job["guest"]
    if g.get("route") == "email":
        return send_by_email(store, job, cfg, site, report, mailer)
    payload = payload_for(store, job, cfg, site)
    store.transition(job, S.SUBMISSION_IN_PROGRESS, "sending")
    if g["captcha"] or g["mapping"].get("human"):
        return _make_link(store, job, payload, report, "the form has a CAPTCHA or a question for a person")
    if sender is None:
        from src.backlinks import browser
        sender = browser.submit
    res = sender(g["form_url"], payload, (shots_dir / f"{job['id']}.png") if shots_dir else None)
    g.update(outcome=res["outcome"], detail=res.get("detail", "")[:300], screenshot=res.get("screenshot"))
    if res["outcome"] in ("sent", "unclear"):
        g.update(sent_on=store.today(), how="automatically")
        store.transition(job, S.SUBMITTED, f"sent automatically ({res['outcome']}: {g['detail'][:120]})", submitted_at=store.now_iso())
        report.append(f"- {job['domain']}: SENT automatically ({res['outcome']}).")
        return "sent"
    if res["outcome"] in ("captcha", "blocked"):
        return _make_link(store, job, payload, report, f"the automatic browser could not send it ({res['outcome']}: {g['detail'][:80]})")
    if res["outcome"] == "unavailable":
        executor.schedule_retry(store, job, cfg, "network", res.get("detail", ""), count=False)
        report.append(f"- {job['domain']}: the browser is not available ({res.get('detail', '')[:80]}); retried later.")
        return "retry"
    why = "the system could not fill the form" if res["outcome"] == "not_filled" else "the form refused the automatic submission"
    store.transition(job, S.REJECTED, f"{why}: {g['detail'][:160]}")
    report.append(f"- {job['domain']}: {why} ({g['detail'][:100]}).")
    return "refused"


def secret(name: str) -> str | None:
    """A GitHub secret: from its own variable, or from ALL_SECRETS (the whole set, which the publish step receives)."""
    if os.environ.get(name):
        return os.environ[name]
    try:
        return json.loads(os.environ.get("ALL_SECRETS") or "{}").get(name)
    except ValueError:
        return None


def mailbox(site: dict) -> dict | None:
    """The website's own mailbox (its `sender` block and Zoho password), or None when none is set up."""
    s = site.get("sender") or {}
    password = secret(s["password_env"]) if s.get("password_env") else None
    if not (s.get("email") and s.get("smtp_host") and password):
        return None
    return {"host": s["smtp_host"], "port": int(s.get("smtp_port") or 587), "user": s["email"], "password": password, "from_email": s["email"], "from_name": s.get("name")}


DOCX_TYPE = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"


def _docx_text(par, text: str) -> None:
    """One paragraph's text with **bold** words and [links](https://...) kept as real, clickable links."""
    from docx.oxml.shared import OxmlElement, qn
    for i, chunk in enumerate(re.split(r"(\[[^\]]+\]\(https?://[^)\s]+\))", text)):
        link = re.fullmatch(r"\[([^\]]+)\]\((https?://[^)\s]+)\)", chunk)
        if link:
            rid = par.part.relate_to(link.group(2), "http://schemas.openxmlformats.org/officeDocument/2006/relationships/hyperlink", is_external=True)
            h, r, rpr = OxmlElement("w:hyperlink"), OxmlElement("w:r"), OxmlElement("w:rPr")
            h.set(qn("r:id"), rid)
            for tag, val in (("w:color", "0563C1"), ("w:u", "single")):
                e = OxmlElement(tag)
                e.set(qn("w:val"), val)
                rpr.append(e)
            t = OxmlElement("w:t")
            t.text = link.group(1)
            t.set(qn("xml:space"), "preserve")
            r.append(rpr)
            r.append(t)
            h.append(r)
            par._p.append(h)
            continue
        for j, part in enumerate(re.split(r"\*\*(.+?)\*\*", chunk)):
            if part:
                par.add_run(part).bold = j % 2 == 1


def article_docx(article: dict, bio: str) -> bytes:
    """The article as a Word file, for websites whose guidelines ask for one: the title, headings, lists and links as written, then the bio."""
    import io
    from docx import Document
    doc = Document()
    doc.add_heading(article["title"], level=1)
    for block in re.split(r"\n\s*\n", article["body_markdown"].strip()):
        lines = [l.strip() for l in block.strip().splitlines() if l.strip()]
        if not lines:
            continue
        if lines[0].startswith("#"):
            doc.add_heading(re.sub(r"^#+\s*", "", lines[0]), level=2)
            lines = lines[1:]
            if not lines:
                continue
        if all(re.match(r"([-*]|\d+\.)\s+", l) for l in lines):
            for l in lines:
                _docx_text(doc.add_paragraph(style="List Number" if re.match(r"\d+\.", l) else "List Bullet"), re.sub(r"^([-*]|\d+\.)\s+", "", l))
        else:
            _docx_text(doc.add_paragraph(), " ".join(lines))
    doc.add_heading("About the author", level=2)
    _docx_text(doc.add_paragraph(), bio)
    buf = io.BytesIO()
    doc.save(buf)
    return buf.getvalue()


def attachments_for(store, job: dict, cfg: dict) -> list[tuple[str, bytes, str]]:
    """The Word file a website's guidelines ask for (only with a full article; a pitch needs none)."""
    g = job["guest"]
    if not g.get("attach_word") or g["mapping"].get("kind") == "pitch" or not job.get("content_path"):
        return []
    article = json.loads(store.read_content(job["content_path"]))
    name = (re.sub(r"[^A-Za-z0-9]+", "-", article["title"]).strip("-")[:60] or "article") + ".docx"
    return [(name, article_docx(article, _bio(cfg)), DOCX_TYPE)]


def mailbox_check(site: dict, login=None) -> str | None:
    """One report line: whether the website's own mailbox can send guest-post emails. It only signs in; nothing is sent."""
    s = site.get("sender") or {}
    if not s.get("email"):
        return None
    box = mailbox(site)
    if box is None:
        return f"- Mailbox {s['email']}: no password yet (GitHub secret {s.get('password_env')}), so guest-post emails wait on the dashboard for you to send."
    try:
        (login or outreach.check_login)(box)
    except Exception as e:  # noqa: BLE001 -- reported, never fatal
        return f"- Mailbox {s['email']}: could not sign in ({type(e).__name__}: {str(e)[:120]}), so guest-post emails wait on the dashboard for you to send."
    return f"- Mailbox {s['email']}: signed in fine; guest-post emails go out automatically."


def email_for(store, job: dict, cfg: dict, site: dict, attached: str = "") -> tuple[str, str]:
    """(subject, body): what the website's guidelines ask for, a pitch or the full article, with the author's bio. With `attached`
    (the Word file's name) the article travels in that file and the email only introduces it."""
    g, gp = job["guest"], cfg.get("guest_posts") or {}
    author = gp.get("author") or (site.get("contact") or {}).get("name") or cfg["brand_name"]
    home, name, bio = f"https://{cfg['verification']['canonical_hosts'][-1]}/", g.get("site_name") or job["domain"], plain_text(_bio(cfg))
    if g["mapping"].get("kind") == "pitch":
        ideas = pitch_ideas(cfg, job, store)
        lo, hi = g.get("words") or [600, 1200]
        title, default = (ideas[0]["title_hint"] if ideas else ""), f"Guest post idea for {name}"
        lines = ["Hello,", "", f"I would like to write a guest post for {name}. Ideas I could write for your readers:", *[f"- {t['title_hint']}" for t in ideas], "",
                 f"Each would be original, practical and written only for {name}, about {lo} to {hi} words. I am happy to change the topic, angle or length to suit you.",
                 "", f"About me: {bio}", "", "Thank you for considering it,", author, home]
    else:
        article = json.loads(store.read_content(job["content_path"]))
        title, default = article["title"], f"Guest post submission: {article['title']}"
        text = [f"As your guidelines ask, the article is attached as a Word file ({attached})."] if attached else [plain_text(article["body_markdown"])]
        lines = ["Hello,", "", f"Following your guidelines, I would like to submit an original article for {name}. It was written for your readers and has not "
                 "been published anywhere else.", "", f"Title: {title}", "", *text, "", "About the author:", bio, "",
                 "Thank you for considering it. I am happy to make any changes you need.", "", author, home]
    subject = re.sub(r"\[[^\]]*\]|<[^>]*>|\{[^}]*\}", title or name, g.get("subject_line") or default).strip()   # e.g. "Guest Post Pitch: [Your Topic]"
    return subject[:150], "\n".join(lines)


def send_by_email(store, job: dict, cfg: dict, site: dict, report: list[str], mailer=None) -> str:
    """Sends to the address the website's own guidelines give, from the website's own mailbox. Without one (or if the mailbox refuses),
    the email waits on the dashboard for the owner to send by hand."""
    g = job["guest"]
    subject, body = email_for(store, job, cfg, site)   # the article in the email itself: what the owner sends by hand when it has to wait
    files = attachments_for(store, job, cfg)
    store.transition(job, S.SUBMISSION_IN_PROGRESS, "sending by email")
    draft = {"to": g["email_to"], "subject": subject, "body": body, "from": ((site.get("sender") or {}).get("email") or (site.get("contact") or {}).get("email", ""))}
    box = mailbox(site)
    problem = None if box else f"no mailbox is set up for sending from {cfg['brand_name']}"
    if box:
        if files:
            subject, body = email_for(store, job, cfg, site, attached=files[0][0])
        try:
            (mailer or outreach.send_email)(site, g["email_to"], subject, body, box, **({"attachments": files} if files else {}))
        except Exception as e:  # noqa: BLE001 -- the owner sends it instead
            problem = f"the mailbox refused to send ({type(e).__name__}: {str(e)[:120]})"
    if problem:
        if files:
            problem += "; the website asks for the article as a Word file: paste it into Word and attach it"
        g.update(email_draft=draft, link_on=store.today(), detail=problem)
        store.transition(job, S.PENDING_APPROVAL, f"waiting for the owner to send the email to {g['email_to']}: {problem}")
        report.append(f"- {job['domain']}: email ready for you to send to {g['email_to']} ({problem}).")
        return "draft"
    g.update(sent_on=store.today(), how="by email", detail=f"to {g['email_to']}: {subject}",
             email_sent={"to": g["email_to"], "subject": subject, "body": body, "at": store.now_iso(), **({"attached": files[0][0]} if files else {})})
    store.transition(job, S.SUBMITTED, f"sent by email to {g['email_to']}", submitted_at=store.now_iso())
    report.append(f"- {job['domain']}: SENT by email to {g['email_to']} ('{subject}').")
    return "sent"


def _make_link(store, job: dict, payload: dict, report: list[str], why: str) -> str:
    g = job["guest"]
    g.update(link=click_link(g["form_url"], payload), link_on=store.today())
    store.transition(job, S.PENDING_APPROVAL, f"waiting for the owner's click: {why}")
    report.append(f"- {job['domain']}: ready for your click ({why}).")
    return "link"


# ------------------------------------------------------------------ the owner's reports and publication
def status_path(store) -> Path:
    return store.root.parent / "guestpost_status.json"


def apply_owner_reports(store, cfg: dict, report: list[str], fetch=verify.http_fetch) -> None:
    try:
        reports = json.loads(status_path(store).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return
    for job in [j for j in store.jobs.values() if j.get("adapter") == "guest"]:
        r = reports.get(job["id"]) or {}
        if r.get("status") == "sent" and job["status"] == S.PENDING_APPROVAL:
            job["guest"].update(sent_on=str(r.get("at", store.now_iso()))[:10], how="by you (CAPTCHA)")
            store.transition(job, S.SUBMITTED, "sent by the owner after the CAPTCHA", submitted_at=r.get("at") or store.now_iso())
            report.append(f"- {job['domain']}: you sent it.")
        url = str(r.get("published_url") or "")
        if r.get("status") == "published" and url.startswith("https://") and job["status"] in (S.SUBMITTED, S.PENDING_APPROVAL):
            _published(store, job, cfg, url, "you reported it published", fetch, report)


def _published(store, job, cfg, url, note, fetch, report) -> None:
    store.transition(job, S.PUBLISHED, f"{note}: {url}", publication_url=url)
    status = executor.verify_job(store, job, cfg, fetch)
    report.append(f"- {job['domain']}: published at {url} -> {status}.")


def find_published(job: dict, fetch=verify.http_fetch) -> str | None:
    """Looks for the article in the website's sitemap and feed, by the words of its title. The website's own name and the words every
    pitch title carries never count: "Pitch to The Self-Publishing Advice Center" matched an unrelated article on that website
    (found 2026-11-10: it was reported as a published page without our link)."""
    own = set(re.findall(r"[a-z0-9]+", ((job.get("guest") or {}).get("site_name") or "").lower() + " " + job["domain"].lower().replace(".", " ")))
    words = [w for w in re.findall(r"[a-z0-9]+", (job.get("title") or "").lower()) if len(w) >= 4 and w not in own and w not in ("pitch", "query", "guest", "idea", "ideas")]
    if len(words) < 3:
        return None
    base = f"https://{job['domain']}"
    urls, queue = [], [f"{base}/sitemap.xml", f"{base}/sitemap_index.xml", f"{base}/post-sitemap.xml", f"{base}/feed", f"{base}/rss.xml"]
    seen_children = 0
    while queue:
        got = fetch(queue.pop(0))
        if got.status != 200 or not got.text:
            continue
        found = re.findall(r"<(?:loc|link)>\s*([^<\s]+)\s*</(?:loc|link)>", got.text)
        for u in found:
            if u.endswith(".xml") and "post" in u and seen_children < 3:
                queue.append(u)
                seen_children += 1
            else:
                urls.append(u)
    for u in urls:
        path = urlparse(u).path.lower()
        if sum(1 for w in words if w in path) >= max(3, int(len(words) * 0.6)):
            return u
    return None


def track(store, cfg: dict, report: list[str], fetch=verify.http_fetch) -> None:
    now = store.now()
    for job in [j for j in store.in_states(S.SUBMITTED) if j.get("adapter") == "guest"]:
        sent = datetime.fromisoformat(job.get("submitted_at") or store.now_iso())
        if now - sent > timedelta(days=TRACK_DAYS):
            store.transition(job, S.REJECTED, f"not published within {TRACK_DAYS} days of sending")
            continue
        if (job["guest"].get("mapping") or {}).get("kind") == "pitch" and not job.get("content_path"):
            continue   # a pitch has no article title of its own to look for: only the address the owner reports counts
        last = job["guest"].get("tracked_at")
        if now - sent < timedelta(days=2) or (last and now - datetime.fromisoformat(last) < timedelta(days=TRACK_EVERY_DAYS)):
            continue
        job["guest"]["tracked_at"] = store.now_iso()
        url = find_published(job, fetch)
        if url:
            _published(store, job, cfg, url, "found in the website's sitemap", fetch, report)
    store.save()


# ------------------------------------------------------------------ the dashboard
LABELS = {S.QUALIFIED: "ready", S.CONTENT_READY: "ready", S.SUBMISSION_IN_PROGRESS: "ready", S.PENDING_APPROVAL: "waiting",
          S.SUBMITTED: "sent", S.PUBLISHED: "published", S.VERIFIED: "published", S.VERIFICATION_FAILED: "published",
          S.REJECTED: "not_accepted", S.REMOVED: "not_accepted", S.RETRY_REQUIRED: "ready"}


def dashboard_items(store, limit: int = 40) -> list[dict]:
    jobs = sorted((j for j in store.jobs.values() if j.get("adapter") == "guest"), key=lambda j: j["updated_at"], reverse=True)[:limit]
    out = []
    for j in jobs:
        g, la = j.get("guest") or {}, j.get("link_attributes") or {}
        out.append({"id": j["id"], "domain": j["domain"], "site_name": g.get("site_name"), "form_url": g.get("form_url"), "status": LABELS.get(j["status"], "ready"),
                    "captcha": g.get("captcha"), "route": g.get("route", "form"), "email_to": g.get("email_to"),
                    "email_draft": g.get("email_draft") if j["status"] == S.PENDING_APPROVAL else None, "email_sent": g.get("email_sent"), "form_kind": (g.get("mapping") or {}).get("kind"), "human_check": bool((g.get("mapping") or {}).get("human")), "accepts_quote": g.get("accepts_quote"), "links_quote": g.get("links_quote"),
                    "relevance": g.get("relevance"), "reason": g.get("reason"), "title": j.get("title"), "link": g.get("link") if j["status"] == S.PENDING_APPROVAL else None,
                    "sent_on": g.get("sent_on"), "how": g.get("how"), "detail": g.get("detail"), "note": j["history"][-1]["note"] if j.get("history") else "",
                    "publication_url": j.get("publication_url"), "counts_for_seo": la.get("counts_for_seo"), "updated_at": j["updated_at"]})
    return out


# ------------------------------------------------------------------ the dress rehearsal
REHEARSAL_FORM = ('<html><head><title>Write for us - Rehearsal Magazine</title></head><body><h1>Write for us</h1>'
                  '<p>This is a copy of a typical guest-post form on this machine. Nothing here is a real website.</p>'
                  '<form id="gp" action="/submit" method="post"><label for="n">Your name *</label><input id="n" name="your-name" required>'
                  '<label for="e">Email *</label><input id="e" type="email" name="your-email" required>'
                  '<label for="t">Article title</label><input id="t" name="post-title">'
                  '<label for="a">Your article *</label><textarea id="a" name="your-message" required></textarea>'
                  '<label for="b">Author bio</label><textarea id="b" name="author-bio"></textarea>'
                  '<p style="display:none"><input name="hp-field" value=""></p>'
                  '<label><input type="checkbox" name="agree" required> I agree to the terms</label>'
                  '<label><input type="checkbox" name="newsletter"> Send me the newsletter</label>'
                  '<button type="submit">Send</button></form></body></html>')


def rehearse(cfg: dict, site: dict, real_store, llm, log_dir: Path, sender=None) -> dict:
    """A full dress rehearsal that touches no real website: the real Gemini writes one article with this site's instructions, the
    normal checks run, then the real browser fills a copy of a typical form served from this machine and sends it there.
    Writes the article and the verdict to log_dir. Returns {ok, problems, title, words, outcome}."""
    import tempfile
    import threading
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
    from urllib.parse import parse_qs
    from src.backlinks import browser
    from src.backlinks.store import Store

    got: dict = {}

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def do_GET(self):
            self.send_response(200); self.send_header("Content-Type", "text/html; charset=utf-8"); self.end_headers()
            self.wfile.write(REHEARSAL_FORM.encode("utf-8"))

        def do_POST(self):
            n = int(self.headers.get("Content-Length") or 0)
            got.update({k: v[0] for k, v in parse_qs(self.rfile.read(n).decode("utf-8")).items()})
            self.send_response(200); self.send_header("Content-Type", "text/html; charset=utf-8"); self.end_headers()
            self.wfile.write(b"<html><body><p>Thank you, your article was received. We will review it.</p></body></html>")

    out = {"ok": False, "problems": [], "title": "", "words": 0, "outcome": ""}
    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        url = f"http://127.0.0.1:{server.server_address[1]}/write-for-us"
        with tempfile.TemporaryDirectory() as tmp:
            store = Store(Path(tmp))
            mapping = map_form(REHEARSAL_FORM, url)
            if not mapping["ok"]:
                out["problems"].append(f"the rehearsal form could not be mapped: {mapping.get('problems') or mapping.get('problem')}")
                return out
            job, _ = store.add_job(make_key("web", "rehearsal.invalid", url), adapter="web", domain="rehearsal.invalid", page_url=url,
                                   discovery_source="rehearsal", title="Write for us")
            guest = {"form_url": url, "site_name": "Rehearsal Magazine", "captcha": False, "mapping": mapping, "accepts_quote": "", "links_quote": "",
                     "topics": "", "rules": "", "words": [700, 1100], "relevance": 9, "quality": 9, "reason": "rehearsal", "checked_at": store.now_iso()}
            store.transition(job, S.QUALIFIED, "rehearsal", adapter="guest", guest=guest, guest_checked=True)
            result = write_article(store, job, cfg, llm)
            if result != "written":
                out["problems"].append(f"the article was not written ({result}): {job.get('last_error') or 'see the run log'}")
                return out
            article = json.loads(store.read_content(job["content_path"]))
            out.update(title=article["title"], words=len(re.findall(r"[A-Za-z0-9']+", article["body_markdown"])))
            payload = payload_for(store, job, cfg, site)
            sent = (sender or browser.submit)(url, payload, log_dir / "rehearsal-form.png")
            out["outcome"] = sent["outcome"]
            if sent["outcome"] != "sent":
                out["problems"].append(f"the browser did not send the form: {sent['outcome']} ({sent.get('detail', '')[:160]})")
            else:
                arrived, written = (re.sub(r"\s+", " ", t).strip() for t in (got.get("your-message", ""), plain_text(article["body_markdown"])))   # a browser sends line breaks as CRLF
                if arrived != written:
                    first = next((i for i, (x, y) in enumerate(zip(arrived, written)) if x != y), min(len(arrived), len(written)))
                    out["problems"].append(f"the article that arrived in the form is not the article that was written (differs at character {first}: "
                                           f"arrived {arrived[first:first + 40]!r}, written {written[first:first + 40]!r})")
                empty = [k for k in ("your-name", "your-email", "post-title", "author-bio") if not got.get(k)]
                if empty:
                    out["problems"].append(f"a field arrived empty: {empty}")
                if got.get("hp-field"):
                    out["problems"].append("the hidden trap field was filled")
                if got.get("newsletter"):
                    out["problems"].append("the newsletter box was ticked")
                if not got.get("agree"):
                    out["problems"].append("the agreement box was not ticked")
            out["ok"] = not out["problems"]
            log_dir.mkdir(parents=True, exist_ok=True)
            stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
            (log_dir / f"rehearsal-{stamp}.md").write_text(
                f"# Dress rehearsal for {cfg['brand_name']}: {'PASSED' if out['ok'] else 'FAILED'}\n\n"
                f"Nothing was sent to any real website. The article below was written by Gemini with this site's instructions, the normal checks "
                f"passed, and the real browser sent it into a copy of a typical form on the GitHub machine.\n\n"
                f"- Browser outcome: {out['outcome']}\n- Problems: {out['problems'] or 'none'}\n- Words: {out['words']}\n"
                f"- Fields that arrived: {sorted(got)}\n\n---\n\n"
                f"# {article['title']}\n\n{article['body_markdown']}\n", encoding="utf-8")
    except Exception as e:  # noqa: BLE001 -- a rehearsal problem is reported, never raised
        out["problems"].append(f"{type(e).__name__}: {str(e)[:200]}")
        out["ok"] = False
    finally:
        server.shutdown()
    return out
