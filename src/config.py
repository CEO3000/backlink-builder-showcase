from pathlib import Path
import yaml

ROOT = Path(__file__).resolve().parent.parent
CONFIG_DIR = ROOT / "config"
SITES_DIR = CONFIG_DIR / "sites"
STATE_DIR = ROOT / "state"
LOGS_DIR = ROOT / "logs"


class ConfigError(RuntimeError):
    pass


def _load_yaml(path: Path):
    with path.open("r", encoding="utf-8") as f:
        return yaml.safe_load(f)


def load_settings():
    return _load_yaml(CONFIG_DIR / "settings.yaml")


def load_default_high_authority_targets():
    path = CONFIG_DIR / "high_authority_targets.yaml"
    if not path.exists():
        return []
    return _load_yaml(path) or []


def load_sites():
    """One config dict per file in config/sites/ (except files starting with
    '_', which are examples/templates). Each dict gets an added 'id' field
    (the filename stem) used to namespace its state and logs."""
    if not SITES_DIR.exists():
        raise ConfigError(
            "config/sites/ not found. Add one YAML file per site, e.g. "
            "config/sites/my-business.yaml (see config/sites/_example.yaml)."
        )
    sites = []
    for path in sorted(SITES_DIR.glob("*.yaml")):
        if path.stem.startswith("_"):
            continue
        data = _load_yaml(path) or {}
        if "url" not in data:
            raise ConfigError(f"{path} is missing required field 'url'")
        data["id"] = path.stem
        sites.append(data)
    if not sites:
        raise ConfigError("No site configs found in config/sites/ (only templates).")
    return sites


def publishing_site_ids() -> set[str]:
    """Sites that have a backlink publishing config (config/backlinks*.yaml). They are never cold-emailed."""
    ids = set()
    for path in CONFIG_DIR.glob("backlinks*.yaml"):
        try:
            data = _load_yaml(path) or {}
        except Exception:  # noqa: BLE001 -- an unreadable file must not stop the email scheduler
            continue
        if isinstance(data, dict) and data.get("site_id"):
            ids.add(str(data["site_id"]))
    return ids
