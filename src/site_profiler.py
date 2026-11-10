"""Crawls a site's own pages (same domain only, robots.txt-respecting) and
derives its description and topic keywords automatically -- so adding a new
site to the system only requires a URL. Any `priority_keywords` given in the
site's config are always kept at the front of the resulting keyword list.

Results are cached in state/sites/<id>/profile.json and only refreshed every
`profiling.refresh_days` (config/settings.yaml) so a given site isn't
re-crawled on every scheduled run.
"""
import re
import time
import urllib.parse
import urllib.request
import urllib.robotparser
from collections import Counter
from datetime import datetime, timezone, timedelta

from bs4 import BeautifulSoup

from src import state as st

# A normal desktop-browser UA rather than a bot-identifying one. This is the
# site owner crawling their own property (not evasion of anyone else's
# robots.txt/ToS elsewhere) -- but some hosts front themselves with
# Cloudflare bot-protection that rejects any request whose User-Agent
# announces itself as a script/bot, self-crawl or not.
USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"

STOPWORDS = set("""
a about above after again against all am an and any are aren't as at be because been before being
below between both but by can't cannot could couldn't did didn't do does doesn't doing don't down
during each few for from further had hadn't has hasn't have haven't having he he'd he'll he's her
here here's hers herself him himself his how how's i i'd i'll i'm i've if in into is isn't it it's
its itself let's me more most mustn't my myself no nor not of off on once only or other ought our
ours ourselves out over own same shan't she she'd she'll she's should shouldn't so some such than
that that's the their theirs them themselves then there there's these they they'd they'll they're
they've this those through to too under until up very was wasn't we we'd we'll we're we've were
weren't what what's when when's where where's which while who who's whom why why's with won't
would wouldn't you you'd you'll you're you've your yours yourself yourselves also us our get one
new well may us like just can will shall might must read more click here page site home learn
find see today now back top menu skip contact us privacy policy terms cookie cookies email phone
call book booking free offer price cost account data guidelines browse login signup password
platform users user sign create join member membership app website web online
""".split())

# URL path fragments that indicate boilerplate/legal pages, not real topical
# content -- crawling these pollutes keyword extraction with generic
# platform words ("account", "guidelines") instead of subject matter.
BOILERPLATE_PATH_HINTS = (
    "/terms", "/privacy", "/cookie", "/legal", "/guidelines", "/login",
    "/signup", "/sign-up", "/register", "/faq", "/help", "/support",
)

WORD_RE = re.compile(r"[a-z][a-z'-]{2,}")

# Generic/filler single words that carry no topical meaning on their own --
# distinct from STOPWORDS above (which are filtered out of *all* candidate
# n-grams during tokenization). These are real, common enough words that
# they survive tokenization and can end up as standalone 1-gram "topic"
# keywords, which then get paired with query templates like
# '"write for us" {keyword}' and produce near-random, off-topic results
# (confirmed in production: "near", "next", "session", "life" all shipped
# as discovery keywords this way). Only applied to single-word candidates --
# a multi-word phrase containing one of these is left alone, since combining
# words already adds specificity a bare junk word lacks.
JUNK_KEYWORDS = set("""
near next session life book join find here more today now easy best top
new free home page click learn view see get with from your our make
""".split())


def _is_junk_keyword(keyword: str, extra_junk: set[str] = frozenset()) -> bool:
    if " " in keyword:
        return False
    return keyword in JUNK_KEYWORDS or keyword in extra_junk or len(keyword) < 4


def _site_self_terms(url: str) -> set[str]:
    """The site's own brand name, derived from its domain (e.g.
    "socialfield" from socialfield.uk). Like the priority-keyword word-roots
    excluded in effective_site(), this is a synonym for "this business,"
    not a topic -- but unlike a priority keyword, it's not curated, so it
    was slipping through as an auto-detected keyword (it's the single most
    repeated word on the site's own pages -- nav, footer, every heading)
    and getting searched verbatim, producing off-topic results."""
    host = urllib.parse.urlparse(url).netloc.lower()
    if host.startswith("www."):
        host = host[4:]
    return {part for part in re.split(r"[.\-]", host) if part}


def _fetch(url: str, timeout=10) -> str:
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read().decode("utf-8", errors="ignore")


def _allowed_by_robots(base_url: str, target_url: str, _cache={}) -> bool:
    domain = urllib.parse.urlparse(base_url).netloc
    if domain not in _cache:
        rp = urllib.robotparser.RobotFileParser()
        rp.set_url(urllib.parse.urljoin(base_url, "/robots.txt"))
        try:
            rp.read()
        except Exception:
            rp = None
        _cache[domain] = rp
    rp = _cache[domain]
    if rp is None:
        return True
    return rp.can_fetch(USER_AGENT, target_url)


def _internal_links(base_url: str, html: str, limit: int) -> list[str]:
    soup = BeautifulSoup(html, "html.parser")
    domain = urllib.parse.urlparse(base_url).netloc
    links = []
    seen = set()
    for a in soup.find_all("a", href=True):
        href = urllib.parse.urljoin(base_url, a["href"])
        parsed = urllib.parse.urlparse(href)
        if parsed.scheme not in ("http", "https") or parsed.netloc != domain:
            continue
        if any(hint in parsed.path.lower() for hint in BOILERPLATE_PATH_HINTS):
            continue
        clean = parsed._replace(fragment="").geturl()
        if clean in seen:
            continue
        seen.add(clean)
        links.append(clean)
        if len(links) >= limit:
            break
    return links


def _extract_page(html: str) -> dict:
    soup = BeautifulSoup(html, "html.parser")
    for tag in soup(["script", "style", "noscript", "svg"]):
        tag.decompose()
    title = soup.title.get_text(strip=True) if soup.title else ""
    meta_desc = ""
    meta = soup.find("meta", attrs={"name": "description"})
    if meta and meta.get("content"):
        meta_desc = meta["content"].strip()
    headings = " ".join(h.get_text(" ", strip=True) for h in soup.find_all(["h1", "h2"]))
    body = soup.get_text(" ", strip=True)
    return {"title": title, "meta_description": meta_desc, "headings": headings, "body": body}


def _tokenize(text: str) -> list[str]:
    return [w for w in WORD_RE.findall(text.lower()) if w not in STOPWORDS]


def _ngrams(words: list[str], n: int) -> list[str]:
    return [" ".join(words[i:i + n]) for i in range(len(words) - n + 1)]


def _extract_keywords(pages: list[dict], priority_keywords: list[str], top_n: int, junk_terms: set[str] = frozenset()) -> list[str]:
    counter = Counter()
    for page in pages:
        # weight title/meta/headings higher than raw body text
        weighted = (
            (page["title"] + " ") * 6
            + (page["meta_description"] + " ") * 3
            + (page["headings"] + " ") * 2
            + page["body"]
        )
        words = _tokenize(weighted)
        counter.update(_ngrams(words, 1))
        counter.update(_ngrams(words, 2))
        counter.update(_ngrams(words, 3))

    ranked = [kw for kw, count in counter.most_common(top_n * 4) if count > 1]
    keywords = [k.lower() for k in priority_keywords]
    for kw in ranked:
        if _is_junk_keyword(kw, junk_terms):
            continue
        if kw not in keywords:
            keywords.append(kw)
        if len(keywords) >= top_n:
            break
    return keywords


def crawl_and_profile(url: str, priority_keywords: list[str] | None, max_pages: int) -> dict:
    priority_keywords = priority_keywords or []
    to_visit = [url]
    seen = set()
    pages = []

    while to_visit and len(pages) < max_pages:
        page_url = to_visit.pop(0)
        if page_url in seen:
            continue
        seen.add(page_url)
        if not _allowed_by_robots(url, page_url):
            continue
        try:
            html = _fetch(page_url)
        except Exception:
            continue
        pages.append(_extract_page(html))
        if page_url == url:
            to_visit.extend(l for l in _internal_links(url, html, limit=max_pages * 2) if l not in seen)
        time.sleep(1)  # be gentle -- this is someone else's server, even if it's your own site

    homepage = pages[0] if pages else {"title": "", "meta_description": "", "headings": "", "body": ""}
    description = homepage["meta_description"] or (homepage["body"][:300].strip())

    return {
        "name": homepage["title"] or url,
        "description": description,
        "keywords": _extract_keywords(pages, priority_keywords, top_n=20, junk_terms=_site_self_terms(url)),
        "pages_crawled": len(pages),
        "profiled_at": st.now_iso(),
    }


def _fallback_profile_from_config(site: dict) -> dict:
    """Used when crawl_and_profile() comes back with 0 pages (site down,
    Cloudflare bot-protection, timeout, ...) and there's no good cached
    profile to fall back on. Rather than caching an almost-empty profile
    (no description, keywords == bare priority_keywords) for refresh_days,
    derive keywords from the site's own configured `description` plus its
    `priority_keywords` -- both human-curated and always available, so a
    failed crawl doesn't starve discovery of usable topic words."""
    priority_keywords = site.get("priority_keywords") or []
    description = site.get("description") or ""
    pseudo_page = {"title": "", "meta_description": description, "headings": "", "body": ""}
    keywords = _extract_keywords(
        [pseudo_page], priority_keywords, top_n=20, junk_terms=_site_self_terms(site["url"])
    )
    return {
        "name": site.get("name") or site["url"],
        "description": description,
        "keywords": keywords,
        "pages_crawled": 0,
        "profiled_at": st.now_iso(),
    }


def get_or_refresh_profile(site: dict, settings: dict) -> dict:
    """Returns a cached profile, re-crawling only if missing or stale.

    A crawl that comes back with pages_crawled == 0 (site down, network
    hiccup, robots.txt block, timeout -- crawl_and_profile swallows
    per-page fetch errors and just returns what it managed to get) is
    never treated as a successful refresh: it isn't cached as "fresh" for
    refresh_days, and it never overwrites a previously good (pages_crawled
    > 0) cached profile. Otherwise a single transient failure gets stamped
    with today's date and silently starves discovery of real keywords for
    the full refresh_days window, since nothing prompts a retry before
    then.

    When there's no good cached profile to fall back on either, a failed
    crawl still isn't a total loss: _fallback_profile_from_config() derives
    keywords from the site's own configured description/priority_keywords
    instead of caching an almost-empty profile.
    """
    site_id = site["id"]
    cached = st.load_profile(site_id)
    refresh_days = settings.get("profiling", {}).get("refresh_days", 30)

    if cached and cached["pages_crawled"] > 0:
        age = datetime.now(timezone.utc) - datetime.fromisoformat(cached["profiled_at"])
        if age < timedelta(days=refresh_days):
            return cached

    max_pages = settings.get("profiling", {}).get("max_pages_per_crawl", 10)
    profile = crawl_and_profile(site["url"], site.get("priority_keywords"), max_pages)
    if profile["pages_crawled"] == 0:
        if cached and cached["pages_crawled"] > 0:
            return cached
        profile = _fallback_profile_from_config(site)
    st.save_profile(site_id, profile)
    return profile


def _word_roots(phrases: list[str]) -> set[str]:
    """Crude stemming via 5-char prefixes, e.g. "hypnotherapy",
    "hypnotherapist", and "hypnosis" all collapse to "hypno"."""
    return {w[:5] for phrase in phrases for w in phrase.lower().split() if len(w) >= 5}


def looks_like_competitor(text: str, priority_keywords: list[str], competitor_terms: list[str] | None = None, domain: str = "") -> bool:
    """Outreach should never go to competitors, only to blogs/resource pages/
    places people advertise their own sites -- this is a hard rule, not a
    preference. Two detection modes:

    - `competitor_terms`, if given, is an explicit list of words/phrases
      that mark a competing business model (e.g. for an event-discovery
      platform: "ticket", "eventbrite" -- competitors are ticket-selling
      platforms, not "anything about events", so a generic keyword-overlap
      check would false-positive on every legitimate events blog). Any
      exact match in the lead text flags it -- and so does a match against
      `domain` (the lead's own hostname, e.g. "example-competitor.co.uk"): a
      competing business often says so plainly in its own domain name even
      when the specific page it published (e.g. a blog post about insomnia
      tips) reads like genuine third-party content. Only single-word terms
      are checked against the domain, since a hostname has no spaces for a
      multi-word phrase to match.
    - Otherwise, fall back to checking whether the lead shares 2+ word-roots
      with the site's own priority keywords (e.g. "hypnotherapist" +
      "london") -- works when the priority keywords are themselves
      service-identity words, so any page saturated with them is probably
      another business offering the same service in the same place.
    """
    text_lower = (text or "").lower()
    domain_lower = (domain or "").lower()
    if competitor_terms:
        if text_lower and any(term.lower() in text_lower for term in competitor_terms):
            return True
        if domain_lower and any(term.lower() in domain_lower for term in competitor_terms if " " not in term):
            return True
        return False
    if not text_lower or not priority_keywords:
        return False
    roots = _word_roots(priority_keywords)
    text_words = {w[:5] for w in re.findall(r"[a-z]+", text_lower) if len(w) >= 5}
    return len(roots & text_words) >= 2


def effective_site(site: dict, profile: dict) -> dict:
    """Merges the (minimal) user config with the auto-discovered profile
    into the shape outreach.py / discovery.py expect.

    `keywords` (priority keywords first) is for display/drafting -- it
    describes what the business IS, which is exactly what makes it a bad
    fit for search queries: "write for us" + "professional hypnotherapist
    in london" mostly finds *other* hypnotherapists, since that's the most
    semantically similar thing to that phrase. `discovery_keywords` is the
    auto-detected topic words only (anxiety, insomnia, ...), which pair
    much better with "write for us"/"resource page" style queries because
    they describe subject matter rather than "a business like this one."
    """
    priority = site.get("priority_keywords") or []
    # These are synonyms for "a business like this one," not topics --
    # exactly what makes a bad search query pairing with "write for us".
    priority_roots = _word_roots(priority)
    topic_keywords = [
        k for k in profile["keywords"]
        if k not in priority
        and not any(w[:5] in priority_roots for w in k.split() if len(w) >= 5)
    ]
    return {
        "id": site["id"],
        "url": site["url"],
        "name": site.get("name") or profile["name"],
        # email_description lets email copy change without shifting the keywords _fallback_profile_from_config() derives from description.
        "description": site.get("email_description") or site.get("description") or profile["description"],
        "keywords": priority + topic_keywords,
        "discovery_keywords": site.get("discovery_keywords") or topic_keywords or priority,
        "contact": site.get("contact") or {},
        "second_offer": site.get("second_offer"),
        "additional_info": site.get("additional_info"),
        "closing": site.get("closing"),
    }
