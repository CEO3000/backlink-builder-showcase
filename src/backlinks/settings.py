"""Per-site controls the owner can change from the dashboard without editing code or YAML.

The dashboard writes config/publishing_settings.json:
    {"sites": {"genbooks-uk": {"articles_per_day": 2, "mode": "live", "enabled": true}}}
and these values are laid over the site's config/backlinks*.yaml at the start of every run. A missing, empty or
malformed file changes nothing, and unknown keys or sites are ignored, so a bad edit can never stop the system.

validate_config() is the safety check for a whole site config (the dashboard also creates new ones): a config with a
problem is skipped with a clear message instead of crashing the run or publishing something half-configured."""
import json
import re
from pathlib import Path
from urllib.parse import urlparse

from src.config import CONFIG_DIR

SETTINGS_PATH = CONFIG_DIR / "publishing_settings.json"
MIN_ARTICLES_PER_DAY, MAX_ARTICLES_PER_DAY = 1, 10
MODES = ("live", "draft")


def load_settings_file(path: Path | None = None) -> dict:
    """{site_id: override}. {} if the file is missing, empty or not valid JSON."""
    try:
        data = json.loads(Path(path or SETTINGS_PATH).read_text(encoding="utf-8"))
        sites = data.get("sites") if isinstance(data, dict) else None
        return {k: v for k, v in sites.items() if isinstance(v, dict)} if isinstance(sites, dict) else {}
    except (OSError, ValueError):
        return {}


def apply_settings(cfg: dict, override: dict | None) -> dict:
    """A copy of cfg with the owner's choices applied: articles_per_day, mode (live|draft) and enabled."""
    if not isinstance(override, dict):
        return cfg
    out = {**cfg, "limits": dict(cfg.get("limits", {}))}
    n = override.get("articles_per_day")
    if isinstance(n, (int, float)) and not isinstance(n, bool):
        n = max(MIN_ARTICLES_PER_DAY, min(MAX_ARTICLES_PER_DAY, int(n)))
        out["limits"]["max_publications_per_platform_per_day"] = n
        out["limits"]["max_new_jobs_per_day"] = max(out["limits"].get("max_new_jobs_per_day", 0), n + 2)  # room for a rewrite or retry
        out["limits"]["ramp_days"] = 0
        out["daily_target_verified"] = n
    if override.get("mode") in MODES:
        out["live_publishing"] = override["mode"] == "live"
    if isinstance(override.get("enabled"), bool):
        out["enabled"] = override["enabled"]
    return out


_SLUG = re.compile(r"^[a-z0-9][a-z0-9-]{0,60}$")
_FOLDER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,60}$")
_REQUIRED = ("site_id", "brand_name", "state_dir", "log_dir", "daily_target_verified", "live_publishing", "limits", "quotas",
             "discovery", "verification", "facts", "topics", "topic_generation")
_LIMITS = ("max_new_jobs_per_day", "max_publications_per_platform_per_day", "max_retries", "retry_backoff_hours", "stuck_after_minutes",
           "verify_recheck_days", "verify_failures_before_inaccessible")
_QUOTAS = ("tavily_searches_per_month", "firecrawl_scrapes_per_month", "gemini_calls_per_day")
_DISCOVERY = ("new_candidates_per_run", "inspect_per_run", "firecrawl_scrapes_per_run")


def validate_config(cfg: dict) -> list[str]:
    """Everything wrong with a site's publishing config, in plain words (empty list = fine)."""
    if not isinstance(cfg, dict):
        return ["the config file is empty or not a mapping"]
    problems = [f"missing '{k}'" for k in _REQUIRED if k not in cfg]
    if problems:
        return problems
    if not isinstance(cfg["site_id"], str) or not _SLUG.match(cfg["site_id"]):
        problems.append("site_id must be lowercase letters, digits and dashes")
    for k in ("state_dir", "log_dir"):
        if not isinstance(cfg[k], str) or not _FOLDER.match(cfg[k]):
            problems.append(f"{k} must be a simple folder name")
    problems += [f"limits.{k} is missing" for k in _LIMITS if k not in cfg["limits"]]
    problems += [f"quotas.{k} is missing" for k in _QUOTAS if k not in cfg["quotas"]]
    problems += [f"discovery.{k} is missing" for k in _DISCOVERY if k not in cfg["discovery"]]
    hosts = (cfg["verification"] or {}).get("canonical_hosts")
    if not isinstance(hosts, list) or not hosts or not all(isinstance(h, str) and h for h in hosts):
        problems.append("verification.canonical_hosts must list the site's address(es)")
        hosts = []
    if not isinstance(cfg["daily_target_verified"], int) or cfg["daily_target_verified"] < 1:
        problems.append("daily_target_verified must be a whole number of 1 or more")
    for name, p in (cfg.get("platforms") or {}).items():
        if (p or {}).get("enabled") and not (p or {}).get("api_key_env"):
            problems.append(f"platform {name} is enabled but has no api_key_env")
        if name == "wordpresscom" and (p or {}).get("enabled") and "." not in str((p or {}).get("site") or ""):
            problems.append("platform wordpresscom is enabled but has no blog address (site: mysite.wordpress.com)")
    facts = cfg["facts"]
    if not isinstance(facts, list) or len([f for f in facts if isinstance(f, str) and len(f.strip()) >= 15]) < 3:
        problems.append("facts needs at least three statements about the business, each a full sentence (they are all the articles may say about it)")
    if not isinstance(cfg["topics"], list):
        problems.append("topics must be a list (it may be empty when topic_generation is on)")
    gen = cfg["topic_generation"] or {}
    targets = gen.get("allowed_targets") or []
    if gen.get("enabled"):
        if not targets:
            problems.append("topic_generation needs allowed_targets: the pages an article may link to")
        for t in targets:
            host = urlparse(str((t or {}).get("url", ""))).netloc.lower().removeprefix("www.")
            if not (t or {}).get("about") or host not in {h.lower().removeprefix("www.") for h in hosts}:
                problems.append(f"allowed target {(t or {}).get('url')!r} must be a page on the site, with an 'about' description")
    elif not cfg["topics"]:
        problems.append("there are no topics and topic_generation is off, so there is nothing to write about")
    for t in cfg["topics"] if isinstance(cfg["topics"], list) else []:
        if not all(k in (t or {}) for k in ("key", "title_hint", "audience", "angle", "target_url", "anchors", "tags")):
            problems.append(f"topic {(t or {}).get('key')!r} is missing fields")
    return problems
