"""Finds real link opportunities (resource pages, "write for us" pages,
guest-post calls) relevant to the site's niche, using the Tavily Search API
(api.tavily.com). This is the sibling of discovery.py (Firecrawl) -- see
scheduler.py, which alternates between the two by calendar day so that
each engine's own monthly quota only has to cover every-other-day usage,
letting each one run with a per-run call cap roughly double what a
daily-only cap could sustain.

Free tier: 1,000 searches/month, no credit card, resets monthly (1 search =
1 unit -- unlike Firecrawl, there's no separate "credits per result" cost).
Sign up at tavily.com, copy the API key (starts with tvly-) and set it as
TAVILY_API_KEY (env var / GitHub secret).

This module only reads public search results. It never visits or scrapes a
target site until a human has reviewed the lead in state/leads.json.
"""
import os
import re
import time
import json
import urllib.error
import urllib.request

from src.discovery import normalize_url, _is_uk_relevant, DiscoveryUnavailable

SEARCH_ENDPOINT = "https://api.tavily.com/search"

# Unlike discovery.py's SEARCH_CALL_DELAY_SECONDS (measured from a live
# Firecrawl 429 error body), Tavily hasn't thrown a real rate-limit error at
# this codebase yet -- this is a conservative starting guess, not a
# confirmed ceiling. Revisit it (like Firecrawl's was, repeatedly) once a
# real 429 shows up in the logs.
SEARCH_CALL_DELAY_SECONDS = 2

RATE_LIMIT_RETRY_BACKOFF = (5, 15)


class RateLimitError(RuntimeError):
    """Tavily responded with HTTP 429 -- the caller may retry after a delay."""

    def __init__(self, message: str, retry_after: float | None = None):
        super().__init__(message)
        self.retry_after = retry_after


_RETRY_AFTER_RE = re.compile(r"retry after (\d+(?:\.\d+)?)\s*s", re.IGNORECASE)


def _parse_retry_after_seconds(detail: str) -> float | None:
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
    """Tavily responded with what looks like an out-of-quota error. The
    exact response shape for this is unconfirmed (no real incident has hit
    it yet) -- treated conservatively as any 402/403 response, same
    heuristic discovery.py uses for Firecrawl. Tighten this once a real
    exhausted-quota response has actually been seen and logged."""


def _api_key():
    key = os.environ.get("TAVILY_API_KEY")
    if not key:
        raise DiscoveryUnavailable(
            "TAVILY_API_KEY not set -- skipping discovery. Sign up free at "
            "tavily.com (no credit card, 1,000 searches/month) and add the "
            "key as a GitHub secret. See src/tavily_discovery.py docstring."
        )
    return key


def search(query: str, num: int = 10) -> list[dict]:
    key = _api_key()
    payload = {"query": query, "max_results": min(num, 10)}
    if '"' in query:
        # Only pages that really contain the quoted phrase ("write for us"), not articles that merely talk about the subject:
        # without this about 60% of the results were ordinary articles (2026-11-07). Same cost per search.
        payload["exact_match"] = True
    body = json.dumps(payload).encode("utf-8")
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
        if e.code in (402, 403):
            raise CreditsExhaustedError(f"HTTP {e.code} (Tavily credits/quota exhausted?): {detail[:200]}") from e
        raise
    results = []
    for item in data.get("results", []):
        results.append({
            "url": item.get("url"),
            "title": item.get("title"),
            "snippet": item.get("content"),
        })
    return results


def _search_with_retry(query: str, num: int = 10) -> list[dict]:
    """Same retry contract as discovery._search_with_retry: retries a 429
    with backoff, up to len(RATE_LIMIT_RETRY_BACKOFF) times, and always
    represents exactly one query against MAX_SEARCH_CALLS_PER_RUN no matter
    how many retries it took."""
    attempt = 0
    while True:
        try:
            return search(query, num=num)
        except RateLimitError as e:
            if attempt >= len(RATE_LIMIT_RETRY_BACKOFF):
                raise
            delay = RATE_LIMIT_RETRY_BACKOFF[attempt]
            if e.retry_after is not None:
                delay = max(delay, e.retry_after + 1)
            print(f"  [tavily_discovery] rate limited ({query!r}) -- retrying in {delay}s "
                  f"(attempt {attempt + 1}/{len(RATE_LIMIT_RETRY_BACKOFF)})...")
            time.sleep(delay)
            attempt += 1


# Firecrawl's MAX_SEARCH_CALLS_PER_RUN (in discovery.py) is 16, sized so that
# running every OTHER day (see scheduler.py's alternation) keeps the same
# monthly total as the old daily-at-8 pace. Tavily's free tier is the same
# order of magnitude (1,000 units/month, and cheaper per-call since there's
# no Firecrawl-style credit multiplier), so it gets the same starting cap
# for symmetry. Unlike Firecrawl's number, this hasn't been validated
# against real usage data yet -- treat it as a starting point to tune down
# if Tavily actually rate-limits or exhausts quota in practice.
MAX_SEARCH_CALLS_PER_RUN = 16


def discover(keywords: list[str], query_templates: list[str], max_results: int, restrict_to_uk: bool = False, known_urls: set[str] | None = None, start_index: int = 0,
             max_calls: int | None = None) -> tuple[list[dict], int]:
    """Same contract as discovery.discover() (Firecrawl) -- see that
    docstring. Shares the same rotation cursor (persisted by the caller)
    since both engines draw from the same (keyword, template) combo space."""
    known_urls = {normalize_url(u) for u in (known_urls or set())}
    call_limit = MAX_SEARCH_CALLS_PER_RUN if max_calls is None else min(MAX_SEARCH_CALLS_PER_RUN, max_calls)   # max_calls: what is left of the daily limit
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
        if calls_made >= call_limit:
            print(f"  [tavily_discovery] hit the search-call cap ({call_limit}) -- stopping early to protect the Tavily quota.")
            break
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
            print(f"  [tavily_discovery] query failed ({query!r}): {e}")
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

    print(f"  [tavily_discovery] funnel: {calls_made} call(s) -> {raw_total} raw result(s) -> "
          f"dropped {dropped_known} already-known, {dropped_dup} dup-in-run, {dropped_non_uk} non-UK -> "
          f"{len(found)} returned")
    for example in non_uk_examples:
        print(f"  [tavily_discovery] non-UK example rejected: {example}")

    return found, calls_made
