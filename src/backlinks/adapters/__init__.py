"""Registry of publication adapters. Add a new route by writing an adapter that satisfies base.Adapter and
listing it here; discovery, content generation, verification and the watchdog need no changes."""
from src.backlinks.adapters.base import Adapter, SubmissionResult
# DEV.to and WordPress.com were removed on 2026-11-01 at the owner's request; guest posts go through each website's own form.
ADAPTER_CLASSES: dict = {}


def build_adapters(platform_cfg: dict) -> dict[str, Adapter]:
    """Adapters for the platforms enabled in config/backlinks.yaml."""
    adapters = {}
    for name, cls in ADAPTER_CLASSES.items():
        cfg = (platform_cfg or {}).get(name) or {}
        if cfg.get("enabled"):
            adapters[name] = cls.from_config(cfg)
    return adapters


def adapter_for_domain(adapters: dict, domain: str):
    domain = domain.lower().removeprefix("www.")
    return next((a for a in adapters.values() if a.domain == domain), None)
