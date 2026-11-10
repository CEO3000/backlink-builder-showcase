"""Main entry point. Run on a schedule (see .github/workflows/backlinks.yml)
with your PC off. Loops over every site in config/sites/*.yaml and, per
site, does a small, rate-limited amount of work:

  1. Refresh that site's auto-detected profile (name/description/keywords)
     if it's missing or stale -- see src/site_profiler.py.
  2. Discover a few new link-opportunity leads relevant to those keywords.
  3. Look for a publicly published contact email on promising leads and
     draft a short, honest outreach note.
  4. Send any leads a human has marked "approved" in the dashboard (or, if
     require_approval is off, send fresh drafts directly) -- capped and
     paced.
  5. Note which high-authority profiles (Google Business Profile, etc.)
     still need their one-time manual signup.

Everything is logged to state/ (machine-readable, including the aggregated
state/dashboard.json the control panel reads) and logs/ (human-readable run
report per site) so progress is fully auditable.
"""
from datetime import datetime, timezone, timedelta
from urllib.parse import urlparse

from src.config import load_settings, load_sites, load_default_high_authority_targets, publishing_site_ids, ConfigError, LOGS_DIR
from src import state as st
from src import discovery
from src import tavily_discovery
from src import outreach
from src import site_profiler

# Alternate discovery engines by calendar day (UTC) rather than calling
# Firecrawl every run -- each engine then only has to cover roughly half the
# month's days out of its own free-tier quota, which is what lets both run
# with a higher per-run search-call cap (see MAX_SEARCH_CALLS_PER_RUN in
# discovery.py / tavily_discovery.py) without either running out early.
# Both share the same rotation cursor (state/sites/<id>/discovery_cursor.json)
# since they're drawing from the same (keyword, template) combo space.
def _discovery_provider_for_today():
    day = datetime.now(timezone.utc).toordinal()
    return (tavily_discovery, "Tavily") if day % 2 == 0 else (discovery, "Firecrawl")


def run_for_site(site: dict, settings: dict) -> dict:
    site_id = site["id"]
    report_lines = [f"# Backlink run -- {site_id} -- {st.now_iso()}", ""]

    profile = site_profiler.get_or_refresh_profile(site, settings)
    eff = site_profiler.effective_site(site, profile)
    report_lines.append(f"Profile: \"{eff['name']}\" -- {profile['pages_crawled']} page(s) crawled, "
                         f"{len(eff['keywords'])} keyword(s) known.")

    pacing = settings["pacing"]
    safety = settings["safety"]
    do_not_contact = settings.get("do_not_contact") or []

    leads = st.load_leads(site_id)
    history = st.load_run_history(site_id)

    if site.get("email_outreach") is False or site_id in publishing_site_ids():
        # Backlinks for this site come from the publishing pipeline (src/backlinks), which never emails site owners.
        report_lines += ["", "Email outreach is switched off for this site (email_outreach: false in its config). "
                             "Backlinks are published and verified by src/backlinks; see logs/backlinks/."]
        ha_status = st.load_ha_status(site_id)
        pending_ha = [t for t in (site.get("high_authority_targets") or load_default_high_authority_targets())
                      if not ha_status.get(t["id"], {}).get("done")]
        site_log_dir = LOGS_DIR / "sites" / site_id
        site_log_dir.mkdir(parents=True, exist_ok=True)
        (site_log_dir / f"run-{datetime.now(timezone.utc).strftime('%Y%m%d-%H%M%S')}.md").write_text("\n".join(report_lines), encoding="utf-8")
        print("\n".join(report_lines))
        return {
            "id": site_id, "url": site["url"], "name": eff["name"], "last_run_at": st.now_iso(),
            "stats": {"leads_total": len(leads), "drafted_pending": 0, "approved_pending": 0,
                      "sent_total": sum(1 for l in leads.values() if l["status"] == "sent"),
                      "no_contact_found": sum(1 for l in leads.values() if l["status"] == "no_contact_found")},
            "high_authority_pending": pending_ha,
        }

    # 1. Discovery
    report_lines.append("")
    report_lines.append("## Discovery")
    provider, provider_name = _discovery_provider_for_today()
    report_lines.append(f"Engine: {provider_name} (alternates by day).")
    known_urls = {discovery.normalize_url(l["url"]) for l in leads.values()}
    try:
        cursor = st.load_discovery_cursor(site_id)
        found, calls_made = provider.discover(
            keywords=eff["discovery_keywords"],
            query_templates=settings["discovery"]["query_templates"],
            max_results=pacing["max_leads_discovered_per_run"],
            restrict_to_uk=site.get("restrict_to_uk", False),
            known_urls=known_urls,
            start_index=cursor,
        )
        total_combos = len(eff["discovery_keywords"]) * len(settings["discovery"]["query_templates"])
        next_cursor = (cursor + calls_made) % total_combos if total_combos else 0
        st.save_discovery_cursor(site_id, next_cursor)
        new_count = 0
        already_known_count = 0
        failed_relevance_count = 0
        link_list_count = 0
        content_style_count = 0
        capped_count = 0
        relevance_rejected_titles = []
        for idx, item in enumerate(found):
            if new_count >= pacing["max_leads_discovered_per_run"]:
                capped_count = len(found) - idx
                break
            norm_url = discovery.normalize_url(item["url"])
            if norm_url in known_urls:
                already_known_count += 1
                continue
            if discovery.is_link_list_style(item.get("matched_query")):
                # UK verification already happened inside discover() -- a
                # link-list page's own text has no reason to mention the
                # site's topic, so the priority-keyword check doesn't apply.
                link_list_count += 1
            else:
                content_style_count += 1
                if not discovery.is_relevant(item.get("title"), item.get("snippet"), site.get("priority_keywords")):
                    failed_relevance_count += 1
                    if len(relevance_rejected_titles) < 3:
                        relevance_rejected_titles.append(item.get("title") or item.get("url") or "")
                    report_lines.append(f"- Skipped {item['url']} -- title/snippet doesn't match any priority keyword, likely off-topic.")
                    continue
            lid = st.lead_id(item["url"])
            leads[lid] = {
                "url": item["url"],
                "title": item.get("title"),
                "snippet": item.get("snippet"),
                "matched_query": item.get("matched_query"),
                "matched_keyword": item.get("matched_keyword"),
                "discovered_at": st.now_iso(),
                "status": "new",
            }
            known_urls.add(norm_url)
            new_count += 1
        report_lines.append(f"- Found {new_count} new lead(s).")
        funnel_line = (f"- Funnel: {len(found)} candidate(s) -> {already_known_count} already known, "
                       f"{link_list_count} link-list (UK-verified, auto-passed), "
                       f"{content_style_count} content-style checked ({failed_relevance_count} failed relevance) -> "
                       f"{new_count} new lead(s).")
        if capped_count:
            funnel_line = funnel_line[:-1] + f", {capped_count} capped by max_leads_discovered_per_run."
        report_lines.append(funnel_line)
        if relevance_rejected_titles:
            titles_str = " | ".join(f'"{t}"' for t in relevance_rejected_titles)
            report_lines.append(f"- Relevance rejected: {titles_str}")
    except discovery.DiscoveryUnavailable as e:
        report_lines.append(f"- Skipped: {e}")

    # 2. Drafting
    report_lines.append("")
    report_lines.append("## Drafting")
    drafted = 0
    already_contacted = st.contacted_emails()
    for lid, lead in leads.items():
        if drafted >= pacing["max_outreach_drafted_per_run"]:
            break
        if lead["status"] != "new":
            continue
        if outreach.on_do_not_contact(lead, do_not_contact):
            lead["status"] = "do_not_contact"
            report_lines.append(f"- Skipped {lead['url']} -- on the do-not-contact list.")
            continue
        lead_text = f"{lead.get('title', '')} {lead.get('snippet', '')}"
        lead_domain = urlparse(lead["url"]).netloc
        if site_profiler.looks_like_competitor(lead_text, site.get("priority_keywords"), site.get("competitor_terms"), domain=lead_domain):
            lead["status"] = "skipped_competitor"
            report_lines.append(f"- Skipped {lead['url']} -- looks like a competing business, not a genuine third-party site.")
            continue
        email, contact_page_url, page_html = outreach.find_public_contact_email(lead["url"])
        if not email:
            if contact_page_url:
                lead["status"] = "no_email_has_contact_page"
                lead["contact_page_url"] = contact_page_url
                report_lines.append(f"- No email found for {lead['url']}, but a contact page exists at {contact_page_url} -- left for manual follow-up.")
            else:
                lead["status"] = "no_contact_found"
                report_lines.append(f"- No public contact email found for {lead['url']} -- left for manual outreach.")
            continue
        if outreach.on_do_not_contact(lead, do_not_contact, email):
            lead["status"] = "do_not_contact"
            report_lines.append(f"- Skipped {lead['url']} -- {email} is on the do-not-contact list.")
            continue
        if email.lower() in already_contacted:
            lead["status"] = "skipped_already_contacted"
            report_lines.append(f"- Skipped {lead['url']} -- {email} has already been emailed by one of our campaigns.")
            continue
        if page_html:
            broken_link = outreach.find_broken_link(page_html, lead["url"], urlparse(eff["url"]).netloc)
            if broken_link:
                lead["broken_link_found"] = broken_link
                report_lines.append(f"- Found a dead outbound link on {lead['url']} ({broken_link}) -- pitching our page as a replacement.")
        subject, body = outreach.draft_email(eff, lead)
        lead.update({
            "status": "drafted",
            "contact_email": email,
            "draft_subject": subject,
            "draft_body": body,
            "drafted_at": st.now_iso(),
        })
        drafted += 1
        already_contacted.add(email.lower())
        report_lines.append(f"- Drafted outreach to {email} re: {lead['url']} (awaiting approval in dashboard) -- written by {lead.get('drafted_by', 'unknown')}")
    report_lines.append(f"- {drafted} draft(s) created this run.")

    # 3. Sending
    report_lines.append("")
    report_lines.append("## Sending")
    smtp = outreach.smtp_for(site.get("sender"))
    sending_paused = bool(smtp and not smtp["password"])
    if not eff["contact"].get("email"):
        report_lines.append("- No contact.email set in this site's config yet -- sending is disabled until it is.")
    elif sending_paused:
        report_lines.append(f"- Sending paused: the {site['sender']['password_env']} GitHub secret isn't set, "
                            f"so nothing can go out from {site['sender']['email']}.")
    else:
        sendable_status = "approved" if safety.get("require_approval", True) else "drafted"
        if safety.get("require_approval", True):
            pending = sum(1 for l in leads.values() if l["status"] == "drafted")
            report_lines.append(f"- require_approval is on. {pending} draft(s) waiting for approval in the dashboard.")

        today_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        already_sent_today = sum(
            1 for h in history if h.get("event") == "sent" and h.get("at", "").startswith(today_str)
        )
        daily_cap = pacing["max_outreach_sent_per_run"]
        remaining_today = max(0, daily_cap - already_sent_today)

        last_sent = st.last_send_at(history)
        can_send = True
        if last_sent:
            gap = datetime.now(timezone.utc) - datetime.fromisoformat(last_sent)
            if gap < timedelta(days=pacing["min_days_between_sends"]):
                can_send = False
                report_lines.append(
                    f"- Skipping sends: only {gap} since last send "
                    f"(minimum {pacing['min_days_between_sends']} days)."
                )
        if remaining_today <= 0:
            can_send = False
            report_lines.append(
                f"- Skipping sends: already sent {already_sent_today} today, at the daily cap of "
                f"{daily_cap} -- this guards against sending twice if the workflow runs more than "
                f"once in the same day (e.g. a late GitHub schedule trigger plus a manual/watchdog run)."
            )
        if can_send:
            sent = 0
            for lid, lead in leads.items():
                if sent >= remaining_today:
                    break
                if lead["status"] != sendable_status:
                    continue
                if outreach.on_do_not_contact(lead, do_not_contact):
                    lead["status"] = "do_not_contact"
                    report_lines.append(f"- Not sending to {lead.get('contact_email')} -- on the do-not-contact list.")
                    continue
                try:
                    outreach.send_email(eff, lead["contact_email"], lead["draft_subject"], lead["draft_body"], smtp=smtp)
                    lead["status"] = "sent"
                    lead["sent_at"] = st.now_iso()
                    history.append({"event": "sent", "at": lead["sent_at"], "lead_id": lid, "url": lead["url"]})
                    sent += 1
                    report_lines.append(f"- Sent outreach to {lead['contact_email']} re: {lead['url']}")
                except Exception as e:
                    report_lines.append(f"- Failed to send to {lead.get('contact_email')}: {e}")
            report_lines.append(f"- {sent} email(s) sent this run.")

    # 4. Follow-ups
    report_lines.append("")
    report_lines.append("## Follow-ups")
    followups_sent = 0
    for lid, lead in leads.items():
        if sending_paused or followups_sent >= pacing["max_followups_sent_per_run"]:
            break
        if lead["status"] != "sent":
            continue
        if lead.get("followup_sent_at"):
            continue
        if outreach.on_do_not_contact(lead, do_not_contact):
            lead["status"] = "do_not_contact"
            report_lines.append(f"- No follow-up to {lead.get('contact_email')} -- on the do-not-contact list.")
            continue
        sent_at = lead.get("sent_at")
        if not sent_at:
            continue
        try:
            age = datetime.now(timezone.utc) - datetime.fromisoformat(sent_at)
        except ValueError:
            continue
        if age < timedelta(days=14):
            continue
        try:
            subject, body = outreach.draft_followup(eff, lead)
            outreach.send_email(eff, lead["contact_email"], subject, body, smtp=smtp)
            lead["followup_sent_at"] = st.now_iso()
            followups_sent += 1
            report_lines.append(f"- Sent follow-up to {lead['contact_email']} re: {lead['url']}")
        except Exception as e:
            report_lines.append(f"- Failed to send follow-up to {lead.get('contact_email')}: {e}")
    report_lines.append(f"- {followups_sent} follow-up(s) sent this run.")

    # 5. High-authority profile status
    ha_targets = site.get("high_authority_targets") or load_default_high_authority_targets()
    ha_status = st.load_ha_status(site_id)
    pending_ha = [t for t in ha_targets if not ha_status.get(t["id"], {}).get("done")]
    report_lines.append("")
    report_lines.append("## High-authority profiles still needing one-time signup")
    if pending_ha:
        for t in pending_ha[:2]:
            report_lines.append(f"- [ ] {t['name']}: {t['signup_url']}")
        if len(pending_ha) > 2:
            report_lines.append(f"- ...and {len(pending_ha) - 2} more (see dashboard)")
    else:
        report_lines.append("- None left -- all marked done.")

    st.save_leads(site_id, leads)
    st.save_run_history(site_id, history)

    LOGS_DIR.mkdir(parents=True, exist_ok=True)
    site_log_dir = LOGS_DIR / "sites" / site_id
    site_log_dir.mkdir(parents=True, exist_ok=True)
    report_path = site_log_dir / f"run-{datetime.now(timezone.utc).strftime('%Y%m%d-%H%M%S')}.md"
    report_path.write_text("\n".join(report_lines), encoding="utf-8")
    print("\n".join(report_lines))
    print()

    return {
        "id": site_id,
        "url": site["url"],
        "name": eff["name"],
        "last_run_at": st.now_iso(),
        "stats": {
            "leads_total": len(leads),
            "drafted_pending": sum(1 for l in leads.values() if l["status"] == "drafted"),
            "approved_pending": sum(1 for l in leads.values() if l["status"] == "approved"),
            "sent_total": sum(1 for l in leads.values() if l["status"] == "sent"),
            "no_contact_found": sum(1 for l in leads.values() if l["status"] == "no_contact_found"),
        },
        "high_authority_pending": pending_ha,
    }


def run():
    settings = load_settings()
    try:
        sites = load_sites()
    except ConfigError as e:
        print(str(e))
        return

    dashboard_sites = []
    for site in sites:
        try:
            dashboard_sites.append(run_for_site(site, settings))
        except Exception as e:
            print(f"[scheduler] site {site.get('id')} failed: {e}")

    st.save_dashboard({"generated_at": st.now_iso(), "sites": dashboard_sites})


if __name__ == "__main__":
    run()
