"""The publishing pipeline must not interfere with the SocialField venue emails or the existing email workflow."""
import re
from pathlib import Path

import yaml

import pytest

from src import outreach, scheduler, site_profiler, state as st

ROOT = Path(__file__).resolve().parent.parent


def sources(folder):
    return {p.name: p.read_text(encoding="utf-8") for p in (ROOT / folder).rglob("*.py")}


def test_the_pipeline_sends_email_from_exactly_one_place():
    """Since 2026-11-03 (owner's decision) the publisher may email a guest post to the address a website's own guidelines give, from that
    website's own mailbox. That is the only email it can send: one function, through outreach.send_email, never a raw SMTP connection."""
    for name, text in sources("src/backlinks").items():
        assert "smtplib" not in text and "EmailMessage" not in text, name
        if name != "guestposts.py":
            assert "send_email" not in text, name
    text = sources("src/backlinks")["guestposts.py"]
    assert text.count("send_email") == 1 and "(mailer or outreach.send_email)(site, g[\"email_to\"]" in text
    assert "SMTP_USER" not in text and "gmail" not in text.lower(), "never the shared Gmail account: only the website's own mailbox"


def test_the_venue_and_email_code_do_not_depend_on_the_pipeline():
    for rel in ("src/venue_campaign.py", "src/outreach.py", "src/discovery.py", "src/tavily_discovery.py"):
        assert "backlinks" not in (ROOT / rel).read_text(encoding="utf-8"), rel


def test_the_venue_campaign_still_loads_its_own_config_and_site():
    import src.venue_campaign as vc
    cfg = yaml.safe_load((ROOT / "config" / "venue_campaign.yaml").read_text(encoding="utf-8"))
    assert cfg["enabled"] is True and cfg["site_id"] == "social-field" and cfg["sender"]["email"] == "info@socialfield.example"
    assert callable(vc.run) and vc.looks_like_registered_company("Company No. 4021421")


def test_backlink_emails_are_off_for_both_sites_but_the_venue_campaign_is_on():
    hyp = yaml.safe_load((ROOT / "config/sites/hypnotreatment-uk.yaml").read_text(encoding="utf-8"))
    soc = yaml.safe_load((ROOT / "config/sites/social-field.yaml").read_text(encoding="utf-8"))
    venue = yaml.safe_load((ROOT / "config/venue_campaign.yaml").read_text(encoding="utf-8"))
    gen = yaml.safe_load((ROOT / "config/sites/genbooks-uk.yaml").read_text(encoding="utf-8"))
    assert hyp["email_outreach"] is False and soc["email_outreach"] is False and gen["email_outreach"] is False
    assert venue["enabled"] is True, "the venue emails are a separate campaign and must stay on"


def _run(site, monkeypatch, tmp_path):
    monkeypatch.setattr(st, "STATE_DIR", tmp_path / "state")
    monkeypatch.setattr(scheduler, "LOGS_DIR", tmp_path / "logs")
    monkeypatch.setattr(site_profiler, "get_or_refresh_profile", lambda s, settings: site_profiler._fallback_profile_from_config(s))
    calls = {"search": 0, "send": 0}
    monkeypatch.setattr(scheduler.tavily_discovery, "discover", lambda **kw: calls.__setitem__("search", calls["search"] + 1) or ([], 0))
    monkeypatch.setattr(scheduler.discovery, "discover", lambda **kw: calls.__setitem__("search", calls["search"] + 1) or ([], 0))
    monkeypatch.setattr(outreach, "send_email", lambda *a, **k: calls.__setitem__("send", calls["send"] + 1))
    settings = yaml.safe_load((ROOT / "config/settings.yaml").read_text(encoding="utf-8"))
    return scheduler.run_for_site(site, settings), calls


def test_hypnotreatment_is_skipped_by_the_email_scheduler(monkeypatch, tmp_path):
    site = yaml.safe_load((ROOT / "config/sites/hypnotreatment-uk.yaml").read_text(encoding="utf-8")) | {"id": "hypnotreatment-uk"}
    out, calls = _run(site, monkeypatch, tmp_path)
    assert calls == {"search": 0, "send": 0} and out["id"] == "hypnotreatment-uk" and "stats" in out and "high_authority_pending" in out
    log = next((tmp_path / "logs" / "sites" / "hypnotreatment-uk").glob("run-*.md")).read_text(encoding="utf-8")
    assert "Email outreach is switched off" in log


def test_socialfield_backlink_emails_are_off_too(monkeypatch, tmp_path):
    site = yaml.safe_load((ROOT / "config/sites/social-field.yaml").read_text(encoding="utf-8")) | {"id": "social-field"}
    out, calls = _run(site, monkeypatch, tmp_path)
    assert calls == {"search": 0, "send": 0} and out["id"] == "social-field"
    log = next((tmp_path / "logs" / "sites" / "social-field").glob("run-*.md")).read_text(encoding="utf-8")
    assert "Email outreach is switched off" in log


def test_a_site_without_the_switch_and_without_a_publishing_config_still_gets_the_normal_email_workflow(monkeypatch, tmp_path):
    """The email scheduler itself is unchanged for a site that is not part of the publishing system."""
    site = yaml.safe_load((ROOT / "config/sites/social-field.yaml").read_text(encoding="utf-8")) | {"id": "an-email-only-site"}
    site.pop("email_outreach")
    out, calls = _run(site, monkeypatch, tmp_path)
    assert calls["search"] == 1
    log = next((tmp_path / "logs" / "sites" / "an-email-only-site").glob("run-*.md")).read_text(encoding="utf-8")
    assert "## Discovery" in log and "## Sending" in log and "## Follow-ups" in log and "switched off" not in log


@pytest.mark.parametrize("site_id", ["hypnotreatment-uk", "social-field", "genbooks-uk"])
def test_a_site_that_has_a_publishing_config_is_never_cold_emailed_even_if_its_site_file_forgot_the_switch(site_id, monkeypatch, tmp_path):
    """The dashboard's old 'Add a site' form wrote a site file without email_outreach: false. A publishing config now wins."""
    site = {"id": site_id, "url": "https://example.test/", "contact": {"name": "", "email": "", "reply_to": ""}}   # no email_outreach key at all
    out, calls = _run(site, monkeypatch, tmp_path)
    assert calls == {"search": 0, "send": 0}
    log = next((tmp_path / "logs" / "sites" / site_id).glob("run-*.md")).read_text(encoding="utf-8")
    assert "Email outreach is switched off" in log
