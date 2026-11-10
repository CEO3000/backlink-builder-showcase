"""Finds real link opportunities (resource pages, "write for us" pages,
guest-post calls) relevant to the site's niche, using the Firecrawl Search
API (api.firecrawl.dev).

Free tier: 1,000 credits/month, no credit card, resets monthly. Search costs
2 credits per 10 results. Sign up at firecrawl.dev, copy the API key (starts
with fc-) and set it as FIRECRAWL_API_KEY (env var / GitHub secret).

This module only reads public search results. It never visits or scrapes a
target site until a human has reviewed the lead in state/leads.json.
"""
import os
import re
import time
import json
import urllib.error
import urllib.request
from urllib.parse import urlparse

SEARCH_ENDPOINT = "https://api.firecrawl.dev/v2/search"

# Pacing between consecutive Firecrawl search calls, regardless of whether
# the previous call succeeded or failed. Firecrawl's free tier caps at 13
# requests/minute (confirmed from a live 429 error body: "Consumed (req/min):
# 13, Remaining (req/min): 0"). With two sites run back-to-back in the same
# job (up to MAX_SEARCH_CALLS_PER_RUN * 2 = 16 calls total), 5s spacing
# spreads them over ~80s -- about 12 req/min, just under the 13/min ceiling.
# Do not "optimise" this back down without re-checking that limit -- a
# once-daily job does not care about an extra 30s of wall-clock time, but
# going below ~4.6s reliably trips the limiter again.
SEARCH_CALL_DELAY_SECONDS = 5

# Backoff schedule for retrying a single query after an HTTP 429. Two
# retries max -- if Firecrawl is still rate-limiting after ~20s of backoff,
# further retries within the same run aren't worth the wall-clock cost.
RATE_LIMIT_RETRY_BACKOFF = (5, 15)


class DiscoveryUnavailable(RuntimeError):
    pass


class RateLimitError(RuntimeError):
    """Firecrawl responded with HTTP 429 (too many requests, not an
    out-of-credits condition) -- the caller may retry after a delay.

    retry_after, when set, is the number of seconds Firecrawl's own error
    body asked us to wait (parsed from e.g. "...or please retry after 3s")
    -- the server's own instruction, when it gives one."""

    def __init__(self, message: str, retry_after: float | None = None):
        super().__init__(message)
        self.retry_after = retry_after


_RETRY_AFTER_RE = re.compile(r"retry after (\d+(?:\.\d+)?)\s*s", re.IGNORECASE)


def _parse_retry_after_seconds(detail: str) -> float | None:
    """Best-effort extraction of a "retry after Ns" hint from a Firecrawl
    429 error body. Returns None (never raises) if the hint is missing or
    the body doesn't match the expected shape -- callers fall back to the
    fixed backoff schedule in that case."""
    if not detail:
        return None
    try:
        match = _RETRY_AFTER_RE.search(detail)
        if match:
            return float(match.group(1))
    except Exception:
        pass
    return None


class CreditsExhaustedError(RuntimeError):
    """Firecrawl responded with an out-of-credits/quota error, distinct
    from a 429 rate limit -- retrying immediately won't help."""


def _api_key():
    key = os.environ.get("FIRECRAWL_API_KEY")
    if not key:
        raise DiscoveryUnavailable(
            "FIRECRAWL_API_KEY not set -- skipping discovery. Sign up free at "
            "firecrawl.dev (no credit card, 1,000 credits/month) and add the "
            "key as a GitHub secret. See src/discovery.py docstring."
        )
    return key


def search(query: str, num: int = 5) -> list[dict]:
    key = _api_key()
    body = json.dumps({
        "query": query,
        "limit": min(num, 10),
        "sources": ["web"],
    }).encode("utf-8")
    req = urllib.request.Request(
        SEARCH_ENDPOINT,
        data=body,
        method="POST",
        headers={
            "Authorization": f"Bearer {key}",
            "Content-Type": "application/json",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        detail = ""
        try:
            detail = e.read().decode("utf-8", errors="ignore")
        except Exception:
            pass
        if e.code == 429:
            retry_after = _parse_retry_after_seconds(detail)
            raise RateLimitError(
                f"HTTP 429 Too Many Requests (rate limited): {detail[:200]}",
                retry_after=retry_after,
            ) from e
        if e.code in (402, 403) and "credit" in detail.lower():
            raise CreditsExhaustedError(f"HTTP {e.code} (Firecrawl credits/quota exhausted): {detail[:200]}") from e
        raise
    results = []
    for item in data.get("data", {}).get("web", []):
        results.append({
            "url": item.get("url"),
            "title": item.get("title"),
            "snippet": item.get("description"),
        })
    return results


def _is_uk_domain(url: str) -> bool:
    try:
        host = urlparse(url).netloc.lower()
    except Exception:
        return False
    if host.startswith("www."):
        host = host[4:]
    return host.endswith(".uk")


_UK_SIGNAL_TERMS = (
    "united kingdom", "(uk)", "u.k.", " uk ", "britain", "british",
    "london", "manchester", "birmingham", "leeds", "glasgow", "edinburgh",
    "liverpool", "bristol", "sheffield", "cardiff", "belfast", "england",
    "scotland", "wales", "northern ireland", "nhs",
)


def _is_uk_relevant(url: str, title: str = "", snippet: str = "") -> bool:
    """True if the domain itself is UK (.uk/.co.uk/.org.uk -- the strict,
    reliable signal), OR the title/snippet text contains a clear UK-content
    signal (a UK city name, "UK"/"British"/"NHS", or a GBP sign) -- catches
    genuinely UK-relevant content hosted on a .com/.org domain, which the
    domain-only check was wrongly excluding."""
    if _is_uk_domain(url):
        return True
    text = f" {title or ''} {snippet or ''} ".lower()
    if "£" in text:
        return True
    return any(term in text for term in _UK_SIGNAL_TERMS)


def is_relevant(title: str, snippet: str, priority_keywords: list[str] | None) -> bool:
    """True if the result's title/snippet contains at least one of the
    site's human-curated priority_keywords as a case-insensitive substring.

    Deliberately a loose OR-match against the trusted, human-curated
    keyword list -- not the broader auto-detected discovery_keywords list,
    and not requiring more than one match. A stricter check here (multiple
    keywords, or matching against the noisier auto-detected list) risks
    recreating the "zero new leads" problem this system already hit once
    before; this only needs to catch results with no topical connection to
    the site at all (e.g. a hypnotherapy site's outreach landing on a
    web-hosting company's guest-post guidelines, or a sports/events
    platform emailing a university social-work library guide).

    A site with no priority_keywords configured is passed through
    unfiltered, since there is nothing trusted to check against.
    """
    if not priority_keywords:
        return True
    text = f"{title or ''} {snippet or ''}".lower()
    return any(kw.lower() in text for kw in priority_keywords)


_LINK_LIST_QUERY_TERMS = (
    "useful links", "helpful resources", "further reading",
    "recommended resources", "resource page", "resources", "suggest a resource",
)


def is_link_list_style(matched_query: str | None) -> bool:
    """True if matched_query was rendered from a link-list-style template --
    the ask is "add our link to your list" (e.g. a "Useful Links" page on an
    NHS trust or hospital site). Once such a candidate has passed UK
    verification, its own text isn't required to already mention the site's
    topic: a link-list page is a list of outbound links, not topic content,
    so there is nothing there TO match. That's unlike a content-contribution
    template ("write for us", "guest post"), where the ask is to contribute
    topical content and matching against is_relevant() genuinely matters."""
    text = (matched_query or "").lower()
    return any(term in text for term in _LINK_LIST_QUERY_TERMS)


def normalize_url(url: str) -> str:
    """Strips a single trailing slash so "…/write-for-us" and
    "…/write-for-us/" -- the same page, differently spelled -- dedup as one
    lead instead of two (both would otherwise get discovered, drafted, and
    independently emailed to the same contact)."""
    if url and url.endswith("/"):
        return url[:-1]
    return url


def _search_with_retry(query: str, num: int = 10) -> list[dict]:
    """Calls search(), retrying up to len(RATE_LIMIT_RETRY_BACKOFF) times
    (with exponential backoff) if Firecrawl responds with a 429. A 429 means
    the request was rejected before being processed -- no credits spent --
    so retrying it here costs no extra credit budget. Whatever happens
    inside this function (one attempt or several), it represents exactly
    one query against MAX_SEARCH_CALLS_PER_RUN to the caller: the retries
    are invisible to the per-run call count, so a retried-then-successful
    query is never double-counted."""
    attempt = 0
    while True:
        try:
            return search(query, num=num)
        except RateLimitError as e:
            if attempt >= len(RATE_LIMIT_RETRY_BACKOFF):
                raise
            delay = RATE_LIMIT_RETRY_BACKOFF[attempt]
            if e.retry_after is not None:
                # Honour the server's own hint when it gives one, with a
                # small safety margin -- but never retry sooner than our
                # own backoff schedule would.
                delay = max(delay, e.retry_after + 1)
            print(f"  [discovery] rate limited ({query!r}) -- retrying in {delay}s "
                  f"(attempt {attempt + 1}/{len(RATE_LIMIT_RETRY_BACKOFF)})...")
            time.sleep(delay)
            attempt += 1


MAX_SEARCH_CALLS_PER_RUN = 16  # Hard ceiling regardless of how many new leads have been found so
# far. Firecrawl bills a flat 2 credits per call (up to 10 results). Doubled from 8 on 2026-09-23
# when scheduler.py started alternating Firecrawl/Tavily by calendar day instead of calling
# Firecrawl every run -- Firecrawl now only runs on ~half the days in a month, so doubling the
# per-run cap keeps the same monthly total as before: roughly 2 sites * 16 calls * 2 credits *
# (days/2 runs per month) ~= what 2 sites * 8 calls * 2 credits * 1 run/day used to cost, comfortably
# inside the free tier's 1,000/month budget. Before the alternation, this was restored from 3 to 7
# on 2026-08-26 after the Firecrawl billing cycle reset, then bumped to 8 shortly after.


def discover(keywords: list[str], query_templates: list[str], max_results: int, restrict_to_uk: bool = False, known_urls: set[str] | None = None, start_index: int = 0) -> tuple[list[dict], int]:
    """Runs (keyword, template) combinations starting at start_index (wrapping
    around the full combination list via modulo) until max_results new items
    are gathered or MAX_SEARCH_CALLS_PER_RUN actual search calls have been made,
    whichever comes first. Rotating the starting point across runs (persisted by
    the caller) means a small per-run call budget still eventually explores every
    combination over time, instead of always retrying the same first few combos
    that get exhausted quickly. known_urls (already-known lead URLs for this
    site) is checked here so results are deduped as they're collected -- keeps
    API usage flat over time regardless of lead history size. If restrict_to_uk
    is True, only results on a .uk/.co.uk/.org.uk domain are kept. Returns
    (found, calls_made) -- the caller should persist
    (start_index + calls_made) % total_combos as the next run's start_index.
    """
    known_urls = {normalize_url(u) for u in (known_urls or set())}
    found = []
    seen_this_call = set()
    calls_made = 0
    raw_total = 0
    dropped_known = 0
    dropped_dup = 0
    dropped_non_uk = 0
    non_uk_examples = []

    combos = [(k, t) for k in keywords for t in query_templates]
    total = len(combos)
    if total == 0:
        return found, 0

    for offset in range(total):
        if len(found) >= max_results:
            break
        if calls_made >= MAX_SEARCH_CALLS_PER_RUN:
            print(f"  [discovery] hit the per-run search-call cap ({MAX_SEARCH_CALLS_PER_RUN}) -- stopping early to protect the Firecrawl quota.")
            break
        # Paced unconditionally, before every call after the first -- never
        # skipped by a `continue` on failure. Consecutive failed calls with
        # no pacing between them is exactly what was tripping Firecrawl's
        # rate limiter (see git history / incident notes).
        if calls_made > 0:
            time.sleep(SEARCH_CALL_DELAY_SECONDS)
        keyword, template = combos[(start_index + offset) % total]
        query = template.format(keyword=keyword)
        try:
            results = _search_with_retry(query, num=10)
            calls_made += 1
        except DiscoveryUnavailable:
            raise
        except Exception as e:
            # A single bad query shouldn't kill the whole run.
            print(f"  [discovery] query failed ({query!r}): {e}")
            calls_made += 1
            continue
        raw_total += len(results)
        for r in results:
            url = r.get("url") or ""
            if not url:
                continue
            norm_url = normalize_url(url)
            if norm_url in known_urls:
                dropped_known += 1
                continue
            if norm_url in seen_this_call:
                dropped_dup += 1
                continue
            if restrict_to_uk and not _is_uk_relevant(url, r.get("title"), r.get("snippet")):
                dropped_non_uk += 1
                if len(non_uk_examples) < 3:
                    non_uk_examples.append(f'"{r.get("title") or ""}" ({url})')
                continue
            r["matched_query"] = query
            r["matched_keyword"] = keyword
            found.append(r)
            seen_this_call.add(norm_url)
            if len(found) >= max_results:
                break

    print(f"  [discovery] funnel: {calls_made} call(s) -> {raw_total} raw result(s) -> "
          f"dropped {dropped_known} already-known, {dropped_dup} dup-in-run, {dropped_non_uk} non-UK -> "
          f"{len(found)} returned")
    for example in non_uk_examples:
        print(f"  [discovery] non-UK example rejected: {example}")

    return found, calls_made
