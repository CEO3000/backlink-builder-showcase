"""Finds and inspects candidate sites, on the existing alternating-day schedule (src/scheduler.py decides the engine):
  Tavily days     search for pages that may offer a way to publish (the existing tavily_discovery search)
  Firecrawl days  inspect the backlog of found pages; Firecrawl's scrape is used where a plain fetch cannot read the
                  page (JavaScript-rendered or blocked), and costs one credit each
Inspection also runs on Tavily days with plain, free fetches. Results are cached on the job, so a page or domain is
never searched or crawled twice."""
import json
import os
import urllib.error
import urllib.request

from src import discovery, state as st
from src.backlinks import states as S
from src.backlinks.store import make_key
from src.backlinks.verify import bare_host, http_fetch

FIRECRAWL_SCRAPE_URL = "https://api.firecrawl.dev/v2/scrape"


def firecrawl_scrape_html(url: str, timeout: int = 60) -> str | None:
    """HTML of a page via Firecrawl, or None. The v2 response shape is read defensively because it has not been
    exercised live from this repository yet."""
    key = os.environ.get("FIRECRAWL_API_KEY")
    if not key:
        return None
    req = urllib.request.Request(FIRECRAWL_SCRAPE_URL, method="POST", headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
                                 data=json.dumps({"url": url, "formats": ["rawHtml"]}).encode("utf-8"))
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = (json.loads(resp.read().decode("utf-8")) or {}).get("data") or {}
    except (urllib.error.URLError, TimeoutError, ValueError):
        return None
    return data.get("rawHtml") or data.get("html")


def search_for_candidates(store, site: dict, settings: dict, cfg: dict, provider, eff: dict, known_urls: set[str]) -> dict:
    """Tavily day: search, then record new pages as DISCOVERED jobs. Returns counts for the report."""
    quota = cfg["quotas"]["tavily_searches_per_month"]
    per_run = getattr(provider, "MAX_SEARCH_CALLS_PER_RUN", 16)
    daily = cfg["quotas"].get("tavily_searches_per_day")   # the owner's daily limit, kept however many times the workflow runs in a day
    allowed = per_run if daily is None else min(per_run, daily - store.used_today("tavily"))
    if allowed <= 0:
        return {"skipped": f"the daily limit of {daily} searches for this site is used up (it searches again tomorrow)"}
    if store.used_this_month("tavily") + allowed > quota:
        return {"skipped": f"our own monthly cap for this site ({quota} searches) would be exceeded"}
    left = tavily_remaining()
    if left is not None and left < allowed + RESERVE_CREDITS:
        return {"skipped": f"Tavily reports only {left} searches left this period; keeping a reserve of {RESERVE_CREDITS}"}
    cursor = st.load_discovery_cursor(site["id"])
    templates = settings["discovery"]["query_templates"]
    found, calls = provider.discover(keywords=eff["discovery_keywords"], query_templates=templates,
                                     max_results=cfg["discovery"]["new_candidates_per_run"],
                                     restrict_to_uk=site.get("restrict_to_uk", False), known_urls=known_urls, start_index=cursor,
                                     **({"max_calls": allowed} if allowed < per_run else {}))
    store.add_usage("tavily", calls)
    total = len(eff["discovery_keywords"]) * len(templates)
    st.save_discovery_cursor(site["id"], (cursor + calls) % total if total else 0)
    added = off_topic = 0
    for item in found:
        norm = discovery.normalize_url(item["url"])
        if norm in known_urls:
            continue
        if not discovery.is_link_list_style(item.get("matched_query")) and not discovery.is_relevant(item.get("title"), item.get("snippet"), site.get("priority_keywords")):
            off_topic += 1
            continue
        domain = bare_host(item["url"])
        if store.find_by_key(make_key("web", domain, norm)) or store.domain_has_live_job("web", domain):
            continue
        store.add_job(make_key("web", domain, norm), adapter="web", domain=domain, page_url=item["url"],
                      discovery_source=f"tavily: {item.get('matched_query', '')}", title=item.get("title"))
        known_urls.add(norm)
        added += 1
        store.bump("discovered")
    return {"searches": calls, "candidates": len(found), "added": added, "off_topic": off_topic}


def fetch_for_inspection(store, url: str, engine: str, cfg: dict, scrape_budget: list, fetch=http_fetch, scrape=None) -> tuple[str | None, str]:
    """(html, how). A plain fetch first (free). On Firecrawl days, a page the plain fetch could not read is
    scraped through Firecrawl, within the per-run and monthly credit budgets."""
    page = fetch(url)
    if page.status == 200 and len(page.text) > 500:
        return page.text, "direct fetch"
    if engine == "Firecrawl" and scrape_budget[0] > 0 and store.used_this_month("firecrawl") < cfg["quotas"]["firecrawl_scrapes_per_month"]:
        left = firecrawl_remaining()
        if left is not None and left < RESERVE_CREDITS:
            scrape_budget[0] = 0   # Firecrawl says its credits are nearly gone: no more scrapes this run
            return None, f"unreadable (HTTP {page.status}); Firecrawl reports {left} credits left, keeping a reserve of {RESERVE_CREDITS}"
        scrape_budget[0] -= 1
        store.add_usage("firecrawl")
        html = (scrape or firecrawl_scrape_html)(url)
        if html:
            return html, "firecrawl scrape"
    return None, f"unreadable (HTTP {page.status}{', ' + page.error if page.error else ''})"


def usage_snapshot(store, cfg: dict) -> dict:
    q = cfg["quotas"]
    return {"tavily": f"{store.used_this_month('tavily')}/{q['tavily_searches_per_month']} searches this month (our own count)",
            "firecrawl": f"{store.used_this_month('firecrawl')}/{q['firecrawl_scrapes_per_month']} scrapes this month (our own count)",
            "gemini": f"{store.used_today('gemini')}/{q['gemini_calls_per_day']} calls today (our own count)"}


RESERVE_CREDITS = 100   # never spend the last 100 credits of a provider's monthly allowance


def _get_json(url: str, key: str, timeout: int = 15) -> dict | None:
    try:
        req = urllib.request.Request(url, headers={"Authorization": f"Bearer {key}"})
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except Exception:  # noqa: BLE001 -- a failed look-up falls back to our own counters
        return None


def tavily_remaining() -> int | None:
    """Searches Tavily says are left this period, or None. Live shape: {"account": {"plan_usage": 246, "plan_limit": 1000}}."""
    key = os.environ.get("TAVILY_API_KEY")
    data = _get_json("https://api.tavily.com/usage", key) if key else None
    try:
        return int(data["account"]["plan_limit"]) - int(data["account"]["plan_usage"])
    except (TypeError, KeyError, ValueError):
        return None


def firecrawl_remaining() -> int | None:
    """Credits Firecrawl says are left, or None. Live shape: {"data": {"remaining_credits": 1005}}."""
    key = os.environ.get("FIRECRAWL_API_KEY")
    data = _get_json("https://api.firecrawl.dev/v1/team/credit-usage", key) if key else None
    try:
        return int(data["data"]["remaining_credits"])
    except (TypeError, KeyError, ValueError):
        return None


def provider_credits() -> dict:
    """Remaining credits where the provider reports them. Both endpoints exist (they answer 401 without a key); the
    response shapes have not been exercised live from here, so any failure is reported as unavailable, never guessed."""
    out = {}
    for name, url, env in (("tavily", "https://api.tavily.com/usage", "TAVILY_API_KEY"),
                           ("firecrawl", "https://api.firecrawl.dev/v1/team/credit-usage", "FIRECRAWL_API_KEY")):
        key = os.environ.get(env)
        if not key:
            out[name] = "unavailable (no key)"
            continue
        try:
            req = urllib.request.Request(url, headers={"Authorization": f"Bearer {key}"})
            with urllib.request.urlopen(req, timeout=15) as resp:
                out[name] = json.loads(resp.read().decode("utf-8"))
        except Exception as e:
            out[name] = f"unavailable ({type(e).__name__})"
    return out
