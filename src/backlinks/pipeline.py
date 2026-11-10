"""One daily pass of the backlink publisher:  watchdog -> discover -> inspect/qualify -> write -> submit -> verify -> report.

Run with:  python -m src.backlinks.pipeline
It never sends email to site owners and never touches the SocialField venue workflow."""
import json
from datetime import datetime, timezone

import yaml

from src import discovery, site_profiler, state as st
from src.backlinks import content as C, discover, executor, guestposts, qualify, settings as settings_mod, states as S, topics as topics_mod, watchdog
from src.backlinks.adapters import build_adapters
from src.backlinks.llm import LLM, PATIENCE_SECONDS, WAIT_BUDGET_SECONDS
from src.backlinks.store import Store, make_key
from src.backlinks.verify import http_fetch
from src.config import CONFIG_DIR, LOGS_DIR, STATE_DIR, load_settings, load_sites


# Content failures that say nothing about the topic: Gemini is out of quota, busy, unreachable, or refused our key.
LLM_DOWN = {"quota", "busy", "network", "truncated", "auth"}


def load_configs() -> list[dict]:
    """One config per site: config/backlinks.yaml (Hypnotreatment) and config/backlinks-<site>.yaml for the others, with the
    owner's dashboard choices (config/publishing_settings.json: articles per day, live or drafts, on or off) laid over each."""
    files = sorted(CONFIG_DIR.glob("backlinks*.yaml"), key=lambda p: (p.name != "backlinks.yaml", p.name))  # the original first
    overrides = settings_mod.load_settings_file()
    out = []
    for p in files:
        cfg = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
        out.append(settings_mod.apply_settings(cfg, overrides.get(cfg.get("site_id"))) if isinstance(cfg, dict) else cfg)
    return out


def load_config() -> dict:
    return load_configs()[0]


def _used_topic_keys(store, adapter_name: str | None = None) -> set[str]:
    """Topics already written on ANY platform, and for the submit queue: nothing is ever written twice."""
    return {j["topic_key"] for j in store.jobs.values() if j.get("topic_key")}


def _history(store, adapter_name: str) -> tuple[list[str], list[str], list[str]]:
    """(earlier article bodies, anchors used so far, titles written so far) for the originality, anchor-variety and
    fresh-title checks."""
    bodies, anchors, titles = [], [], []
    for j in store.jobs.values():
        if not j.get("content_path"):   # every earlier article (any platform, and the submit queue), not only this platform's
            continue
        try:
            bodies.append(json.loads(store.read_content(j["content_path"]))["body_markdown"])
        except (OSError, ValueError, KeyError):
            pass
        if j.get("anchor_text"):
            anchors.append(j["anchor_text"])
        if j.get("title"):
            titles.append(j["title"])
    return bodies, anchors, titles


def _new_topic_job(store, adapter, topic: dict, cfg: dict) -> dict:
    key = make_key(adapter.name, adapter.domain, topic["key"])
    job, created = store.add_job(key, adapter=adapter.name, domain=adapter.domain, page_url=f"https://{adapter.domain}/",
                                 discovery_source="configured topic", topic_key=topic["key"], target_url=topic["target_url"],
                                 expected_urls=[topic["target_url"], f"https://{cfg['verification']['canonical_hosts'][-1]}/"])
    if created:
        store.bump("discovered")
        store.transition(job, S.QUALIFIED, f"{adapter.domain} offers an official publishing API ({adapter.docs_url})", mechanism=adapter.mechanism,
                         qualification={"decision": "qualified", "mechanism": adapter.mechanism, "evidence": [adapter.docs_url],
                                        "automated_submission_permitted": "yes: the platform's own authorised API",
                                        "external_links_allowed": True, "editorial_approval_required": False,
                                        "link_type_stated": "not stated; recorded from the live page by the verifier",
                                        "audience": topic["audience"], "relevance": "topic chosen for fit with the practice"})
        store.bump("qualified")
    return job


def _prepare_content(store, job: dict, topic: dict, cfg: dict, llm: LLM, adapter_name: str) -> bool:
    """Writes and checks the article. True if the job is now CONTENT_READY; otherwise the job has been moved on."""
    bodies, anchors, titles = _history(store, adapter_name)
    cfg = {**cfg, "article_venue": cfg.get("article_venue") or C.VENUES.get(adapter_name, "an online blog")}   # the writer is told where THIS article will appear
    try:
        article = C.build_article(topic, cfg, llm, anchors, bodies, titles)
    except C.ContentError as e:
        if e.category == "auth":      # our Gemini key was refused: the owner has to look, the topic is not at fault
            store.record_failure("auth")
            store.transition(job, S.BLOCKED_CREDENTIALS, f"the Gemini key was refused: {e}", last_error=str(e), error_category="auth")
        elif e.category in LLM_DOWN:  # our own AI quota or the service is down: wait, without using up the job's retries
            executor.schedule_retry(store, job, cfg, e.category, str(e), count=False)
        else:                         # the draft itself failed the checks: bounded retries
            executor.schedule_retry(store, job, cfg, e.category, str(e))
        return False
    path = store.save_content(job["id"], json.dumps({k: article[k] for k in ("title", "description", "tags", "body_markdown")}, indent=2, ensure_ascii=False))
    store.bump("generated")
    store.transition(job, S.CONTENT_READY, f"article written and passed the checks ({article['word_count']} words, {article['writer_model']})",
                     title=article["title"], anchor_text=article["anchor"], content_version=article["version"], content_path=path)
    return True


def _submit(store, job: dict, adapter, cfg: dict, fetch) -> str:
    content = json.loads(store.read_content(job["content_path"]))
    return executor.submit_job(store, job, adapter, content, cfg["live_publishing"], cfg, fetch)


def daily_cap(store, adapter_name: str, limits: dict) -> int:
    """Articles allowed per day on this platform. A new account starts gently: for the first `ramp_days` days after the
    first article it is held to one a day, then the full `max_publications_per_platform_per_day` applies. A sudden burst
    of posts from a brand-new account looks automated to the platform, so the ramp lowers the risk of the account being flagged."""
    cap, ramp = limits["max_publications_per_platform_per_day"], limits.get("ramp_days", 0)
    if ramp and cap > 1:
        firsts = [j["submitted_at"] for j in store.jobs.values()
                  if j["adapter"] == adapter_name and j.get("submitted_at") and j["status"] != S.REJECTED]
        if not firsts or (store.now().date() - datetime.fromisoformat(min(firsts)).date()).days < ramp:
            return 1   # nothing published yet counts as day one
    return cap


def publish_pass(store, cfg: dict, adapters: dict, llm: LLM, ready: list[dict], fetch, report: list[str]) -> None:
    limits, target = cfg["limits"], cfg["daily_target_verified"]
    usable = {}
    for name, adapter in adapters.items():
        ok, why = adapter.available()
        if ok:
            usable[name] = adapter
        else:
            report.append(f"- BLOCKED: {name} cannot be used: {why}")
            iso = store.now().isocalendar()
            watchdog.notify(store, f"backlinks-blocked-{cfg['site_id']}-{name}-{iso[0]}W{iso[1]}",
                            f"Backlink publishing for {cfg['site_id']} on {name} cannot run: {why}.")

    for job in ready:  # retries whose backoff has passed
        if job["adapter"] in usable:
            report.append(f"- Retried submission of '{job.get('title')}': now {_submit(store, job, usable[job['adapter']], cfg, fetch)}")

    attempts = 0
    progress = True
    while progress and store.verified_on() < target and attempts < limits["max_new_jobs_per_day"]:
        progress = False
        for name, adapter in usable.items():
            if store.published_on(name) >= daily_cap(store, name, limits):
                continue
            if not cfg["live_publishing"] and len(store.in_states(S.PENDING_APPROVAL)) >= limits["max_pending_approval"]:
                report.append(f"- Waiting: {limits['max_pending_approval']} drafts already await your approval.")
                continue
            # A job already waiting for its article (after a retry or an unblocked credential) goes first.
            job = next((j for j in store.in_states(S.QUALIFIED) if j["adapter"] == name and j.get("topic_key")), None)
            if job:
                topic = next(t for t in cfg["topics"] if t["key"] == job["topic_key"])
            else:
                topic = next((t for t in cfg["topics"] if t["key"] not in _used_topic_keys(store, name)), None)
                if topic is None:
                    report.append(f"- No unused topics left for {name}: add more to config/backlinks.yaml.")
                    continue
                job = _new_topic_job(store, adapter, topic, cfg)
            attempts += 1
            progress = True
            if _prepare_content(store, job, topic, cfg, llm, name):
                status = _submit(store, job, adapter, cfg, fetch)
                report.append(f"- {name}: '{job['title']}' -> {status}")
            else:
                report.append(f"- {name}: topic '{topic['key']}' not published: {job['status']} ({job['last_error']})")
                if job["error_category"] in LLM_DOWN:
                    report.append("- Stopping for today: Gemini is unavailable, so no further article can be written.")
                    store.save()
                    return
            store.save()


def inspect_pass(store, cfg: dict, adapters: dict, llm: LLM, engine: str, topics: list[str], fetch, report: list[str]) -> None:
    budget = [cfg["discovery"]["firecrawl_scrapes_per_run"]]
    waiting = [j for j in store.in_states(S.DISCOVERED) if j["adapter"] == "web"][:cfg["discovery"]["inspect_per_run"]]
    for job in waiting:
        html, how = discover.fetch_for_inspection(store, job["page_url"], engine, cfg, budget, fetch=fetch)
        if html is None:
            executor.schedule_retry(store, job, cfg, "unreachable", how)
            continue
        qcfg = cfg.get("qualification", {})
        status = qualify.qualify_candidate(store, job, qualify.inspect_page(html, job["page_url"]), adapters,
                                           llm if store.used_today("gemini") < cfg["quotas"]["gemini_calls_per_day"] - 10 else None, topics,
                                           site_label=qcfg.get("site_label"), fit_criteria=qcfg.get("fit_criteria"))
        if status == S.QUALIFIED:
            store.bump("qualified")
        report.append(f"- {job['domain']}: {status} ({(job.get('qualification') or {}).get('rationale', '')[:110]}) [{how}]")


def render_report(store, cfg: dict, notes: list[str], lines: list[str], search: dict | None, engine: str, credits: dict | None = None) -> str:
    day = store.today()
    c = store.daily.get(day, {"counts": {}, "failures": {}})["counts"]
    failures = store.daily.get(day, {"failures": {}})["failures"]
    verified_today = store.verified_on()
    live = [j for j in store.in_states(S.VERIFIED) if not j.get("legacy")]
    attrs = [j.get("link_attributes") or {} for j in live]
    by_state = {}
    for j in store.jobs.values():
        by_state[j["status"]] = by_state.get(j["status"], 0) + 1
    out = [f"# Backlink publishing -- {cfg['site_id']} -- {store.now_iso()}", "",
           f"Result: {verified_today} verified live backlink(s) today, target {cfg['daily_target_verified']} "
           f"({'TARGET REACHED' if verified_today >= cfg['daily_target_verified'] else 'target not reached'}). "
           f"Mode: {'live publishing' if cfg['live_publishing'] else 'drafts only (waiting for your approval)'}.", "",
           "## Today", f"- Searches for new websites: {search or 'none'}; backup for pages a plain fetch cannot read: {engine}",
           f"- Opportunities discovered: {c.get('discovered', 0)}; qualified: {c.get('qualified', 0)}; articles written and checked: {c.get('generated', 0)}",
           f"- Submission attempts: {c.get('submission_attempts', 0)}; published: {c.get('published', 0)}; saved as drafts awaiting approval: {c.get('drafts_pending_approval', 0)}",
           f"- Verified live: {c.get('verified', 0)}; removed since: {c.get('removed', 0)}",
           f"- Failures: {failures or 'none'}", "", "## Live verified backlinks in total",
           f"- {len(live)} live ({sum(1 for a in attrs if a.get('follow'))} followed, {sum(1 for a in attrs if a and not a.get('follow'))} nofollow/sponsored/ugc)",
           f"- Google can count {sum(1 for a in attrs if a.get('counts_for_seo') is True)} of them (page indexable and links followed); "
           f"{sum(1 for a in attrs if a.get('counts_for_seo') is False)} are live but the platform marks the page noindex/nofollow; "
           f"{sum(1 for a in attrs if a.get('counts_for_seo') is None)} not checked yet",
           "", "## All jobs by status", f"- {dict(sorted(by_state.items()))}", "", "## What happened"] + (lines or ["- nothing to do"])
    if notes:
        out += ["", "## Watchdog"] + [f"- {n}" for n in notes]
    out += ["", "## Quotas (our own counts)"] + [f"- {k}: {v}" for k, v in discover.usage_snapshot(store, cfg).items()]
    if credits:
        out += ["", "## Credits as reported by the providers (raw, trimmed)"] + [f"- {k}: {str(v)[:220]}" for k, v in credits.items()]
    return "\n".join(out)


def run(store=None, cfg=None, adapters=None, llm=None, fetch=http_fetch, engine: str | None = None, provider=None,
        site=None, settings=None, write_logs: bool = True) -> dict:
    cfg = cfg or load_config()
    if not cfg.get("enabled"):
        print(f"Backlink publishing for {cfg.get('site_id')} is paused (switched off in its config or on the dashboard).")
        if write_logs:
            _mark_paused(cfg)
        return {"enabled": False}
    store = store or Store(STATE_DIR / cfg.get("state_dir", "backlinks"))
    site = site or next(s for s in load_sites() if s["id"] == cfg["site_id"])
    settings = settings or load_settings()
    adapters = adapters if adapters is not None else build_adapters(cfg.get("platforms") or {})
    llm = llm or LLM(store, cfg["quotas"]["gemini_calls_per_day"], patience=cfg["quotas"].get("gemini_patience_seconds", PATIENCE_SECONDS),
                     wait_budget=cfg["quotas"].get("gemini_wait_budget_seconds", WAIT_BUDGET_SECONDS))
    searcher = provider if engine == "Tavily" else None   # tests pass their own provider
    if provider is None:
        from src import tavily_discovery
        from src.scheduler import _discovery_provider_for_today
        provider, _ = _discovery_provider_for_today()
        searcher = tavily_discovery   # guest-post searches use Tavily every day: a few searches a site a day, well inside the free allowance
        # The backup reader (Firecrawl) for pages a plain fetch cannot read works EVERY day, still within each site's daily limit of
        # `firecrawl_scrapes_per_day` (4) and the monthly cap (owner, 2026-11-08). Until then it worked on alternate days only, so on
        # the other days every page a plain fetch could not read was lost: 15 pages on 2026-11-08 alone.
        engine = "Firecrawl"
    eff = site_profiler.effective_site(site, st.load_profile(site["id"]) or site_profiler._fallback_profile_from_config(site))
    lines: list[str] = []
    guestposts.migrate(store, lines)
    if write_logs:   # the real daily run: can the website's own mailbox send? (a sign-in only, nothing is sent)
        lines += [x for x in [guestposts.mailbox_check(site)] if x]
    cfg = topics_mod.with_generated_topics(store, cfg, llm, lines)   # more topics from Gemini when few are left unused

    wd = watchdog.run(store, adapters, cfg, fetch)
    search = None
    if searcher is not None:
        known = {discovery.normalize_url(j["page_url"]) for j in store.jobs.values()}
        known |= {discovery.normalize_url(l["url"]) for l in st.load_leads(site["id"]).values()}
        try:
            search = guestposts.find(store, site, settings, cfg, searcher, eff, known)
        except discovery.DiscoveryUnavailable as e:
            search = {"skipped": str(e)}
    guestposts.check_pass(store, cfg, eff, settings, llm, engine, fetch, lines)
    publish_pass(store, cfg, adapters, llm, wd["ready"], fetch, lines)   # no publishing platform is configured any more: this does nothing
    try:   # one guest post problem must never stop the daily run (the report and dashboard still get written)
        guestposts.apply_owner_reports(store, cfg, lines, fetch)
        guestposts.write_and_send(store, cfg, site, llm, lines,
                                  shots_dir=(LOGS_DIR / cfg.get("log_dir", "backlinks") / "submissions") if write_logs else None)
        guestposts.track(store, cfg, lines, fetch)
    except Exception as e:  # noqa: BLE001
        lines.append(f"- Guest posts stopped with an error: {type(e).__name__}: {str(e)[:160]}")
    watchdog.notify_events(store, cfg)
    store.save()

    report = render_report(store, cfg, wd["notes"], lines, search, engine, discover.provider_credits() if write_logs else None)
    if write_logs:
        log_dir = LOGS_DIR / cfg.get("log_dir", "backlinks")
        log_dir.mkdir(parents=True, exist_ok=True)
        (log_dir / f"run-{datetime.now(timezone.utc).strftime('%Y%m%d-%H%M%S')}.md").write_text(report, encoding="utf-8")
        _update_dashboard(store, cfg, lines)
    print(report)
    return {"enabled": True, "verified_today": store.verified_on(), "report": report}


PLATFORM_NAMES = {"guest": "Guest post"}   # how the dashboard names each route
# DEV.to and WordPress.com were removed on 2026-11-01: guestposts.migrate() marks their old jobs legacy (history, never checked or shown)
ERROR_STATUSES = (S.RETRY_REQUIRED, S.REJECTED, S.BLOCKED_CREDENTIALS, S.VERIFICATION_FAILED, S.INACCESSIBLE, S.REMOVED)
NOTE_MARKERS = ("BLOCKED", "Stopping for today", "Waiting:", "could not be generated", "No unused topics", "failed", "stays a draft")


def _site_entry(store, cfg: dict, lines: list[str]) -> dict:
    """Everything the dashboard shows for one site: this run's real result, the owner's settings, the last 30 days, and what went wrong."""
    platforms = list((cfg.get("platforms") or {}).keys())
    live = [j for j in store.in_states(S.VERIFIED) if not j.get("legacy")]
    used = {j["topic_key"] for j in store.jobs.values() if j.get("topic_key")}
    today = store.daily.get(store.today(), {"counts": {}, "failures": {}})
    history = []
    for d in sorted(store.daily)[-30:]:
        c, f = store.daily[d]["counts"], store.daily[d]["failures"]
        history.append({"date": d, "published": c.get("published", 0), "verified": c.get("verified", 0),
                        "drafts": c.get("drafts_pending_approval", 0), "failures": sum(f.values())})
    errors = []
    for j in sorted(store.jobs.values(), key=lambda x: x["updated_at"], reverse=True):
        if j["adapter"] == "web" or j["status"] not in ERROR_STATUSES or (j["adapter"] == "guest" and j["status"] == S.REJECTED):
            continue   # a website that is not used is listed under Guest posts, Not accepted: it is not an error
        errors.append({"at": j["updated_at"], "title": j.get("title") or j.get("topic_key") or j["domain"], "status": j["status"],
                       "category": j.get("error_category"), "message": (j.get("last_error") or (j["history"][-1]["note"] if j.get("history") else ""))[:300]})
        if len(errors) == 12:
            break
    return {
        "updated_at": store.now_iso(), "name": cfg.get("brand_name") or cfg["site_id"], "url": f"https://{cfg['verification']['canonical_hosts'][-1]}/",
        "settings": {"articles_per_day": cfg["limits"]["max_publications_per_platform_per_day"],
                     "mode": "live" if cfg["live_publishing"] else "draft", "enabled": bool(cfg.get("enabled"))},
        "target_per_day": cfg["daily_target_verified"], "verified_today": store.verified_on(),
        "published_today": today["counts"].get("published", 0) + today["counts"].get("drafts_pending_approval", 0),
        "verified_live_total": len(live), "guest_posts": guestposts.dashboard_items(store),
        "guest_today": sum(1 for j in store.jobs.values() if j.get("adapter") == "guest" and store.today() in ((j.get("guest") or {}).get("sent_on"), (j.get("guest") or {}).get("link_on"))),
        "counted_by_google": sum(1 for j in live if (j.get("link_attributes") or {}).get("counts_for_seo") is True),
        "blocked_from_google": sum(1 for j in live if (j.get("link_attributes") or {}).get("counts_for_seo") is False),
        "pending_approval": len(store.in_states(S.PENDING_APPROVAL)),
        "topics_unused": len([t for t in cfg["topics"] if t["key"] not in used]),
        "by_status": {s_: sum(1 for j in store.jobs.values() if j["status"] == s_) for s_ in sorted({j["status"] for j in store.jobs.values()})},
        "failures_today": today["failures"], "history": history, "errors": errors,
        "blocked": [l.split("BLOCKED:", 1)[1].strip() for l in lines if "BLOCKED:" in l],
        "notes": [l.lstrip("- ").strip() for l in lines if any(m in l for m in NOTE_MARKERS) and "BLOCKED:" not in l][:6],
        "live_links": [{"url": j["publication_url"], "title": j.get("title"), "anchor": (j.get("link_attributes") or {}).get("anchor_text"),
                        "follow": (j.get("link_attributes") or {}).get("follow"), "verified_at": j.get("first_verified_at"),
                        "platform": PLATFORM_NAMES.get(j["adapter"], j["adapter"]), "target": j.get("target_url"),
                        "indexable": (j.get("link_attributes") or {}).get("indexable"),
                        "counts_for_seo": (j.get("link_attributes") or {}).get("counts_for_seo")} for j in live],
    }


def _write_dashboard(site_id: str, entry: dict) -> None:
    path = STATE_DIR / "dashboard.json"
    data = st._load_json(path, {})
    section = data.get("backlink_publishing")
    if not isinstance(section, dict) or "updated_at" in section:  # first run, or the older single-site shape
        section = {}
    section[site_id] = entry
    data["backlink_publishing"] = section
    st._save_json(path, data)


def _update_dashboard(store, cfg: dict, lines: list[str] | None = None) -> None:
    """Adds each site's real result to state/dashboard.json under backlink_publishing[site_id], next to the per-site
    email stats the scheduler writes."""
    _write_dashboard(cfg["site_id"], _site_entry(store, cfg, lines or []))


def _mark_paused(cfg: dict) -> None:
    """A switched-off site stays on the dashboard, marked paused, with its last known numbers."""
    from datetime import datetime, timezone
    prior = (st._load_json(STATE_DIR / "dashboard.json", {}).get("backlink_publishing") or {}).get(cfg["site_id"], {})
    entry = {**prior, "updated_at": datetime.now(timezone.utc).isoformat(), "name": cfg.get("brand_name") or cfg["site_id"]}
    entry["settings"] = {**(prior.get("settings") or {}), "enabled": False,
                         "articles_per_day": cfg.get("limits", {}).get("max_publications_per_platform_per_day", 0),
                         "mode": "live" if cfg.get("live_publishing") else "draft"}
    _write_dashboard(cfg["site_id"], entry)


def _write_issues(issues: list[dict]) -> None:
    from datetime import datetime, timezone
    path = STATE_DIR / "dashboard.json"
    data = st._load_json(path, {})
    now = datetime.now(timezone.utc).isoformat()
    data["publishing_issues"] = [{"at": now, **i} for i in issues]
    st._save_json(path, data)


def rotated(configs: list, today=None) -> list:
    """The sites in today's order. The shared Gemini allowance is used in this order, so the starting site moves on by
    one each day: whichever site goes last (and may find the allowance spent) is not always the same one."""
    from datetime import date
    if not configs:
        return configs
    k = (today or date.today()).toordinal() % len(configs)
    return configs[k:] + configs[:k]


def _rehearsal(cfg: dict) -> list[dict]:
    """The dress rehearsal (guestposts.rehearse) for one site. Returns the problems to show on the dashboard (none when it passed)."""
    store = Store(STATE_DIR / cfg.get("state_dir", "backlinks"))
    site = next(s for s in load_sites() if s["id"] == cfg["site_id"])
    llm = LLM(store, cfg["quotas"]["gemini_calls_per_day"], patience=cfg["quotas"].get("gemini_patience_seconds", PATIENCE_SECONDS),
              wait_budget=cfg["quotas"].get("gemini_wait_budget_seconds", WAIT_BUDGET_SECONDS))
    result = guestposts.rehearse(cfg, site, store, llm, LOGS_DIR / cfg.get("log_dir", "backlinks"))
    store.save()
    print(f"[backlinks] dress rehearsal for {cfg['site_id']}: {'PASSED' if result['ok'] else 'FAILED'} {result}")
    return [] if result["ok"] else [{"site_id": cfg["site_id"], "problems": ["the dress rehearsal failed: " + "; ".join(result["problems"])]}]


def main() -> None:
    """Every site with a config/backlinks*.yaml, one after another. A site whose config has a problem is skipped with a
    clear message (shown on the dashboard), and one site failing never stops the others."""
    configs = rotated(load_configs())
    issues = []
    wanted = (load_settings().get("guest_rehearsal") or "")
    for cfg in [c for c in configs if c.get("site_id") == wanted]:   # a one-off check, switched on in config/settings.yaml: guest_rehearsal: <site id>
        issues += _rehearsal(cfg)
    for cfg in configs:
        problems = settings_mod.validate_config(cfg)
        site = cfg.get("site_id") if isinstance(cfg, dict) else "?"
        if problems:
            print(f"[backlinks] {site} skipped, its config has problems: " + "; ".join(problems))
            issues.append({"site_id": site, "problems": problems})
            continue
        try:
            run(cfg=cfg)
        except Exception as e:  # noqa: BLE001 -- report and carry on with the next site
            print(f"[backlinks] {site} failed: {type(e).__name__}: {e}")
            issues.append({"site_id": site, "problems": [f"the run stopped with an error: {type(e).__name__}: {e}"]})
    _write_issues(issues)


if __name__ == "__main__":
    main()
