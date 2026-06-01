import json
from pathlib import Path

CONFIG_DIR = Path.home() / ".token-sentinel"
CONFIG_FILE = CONFIG_DIR / "config.json"

_DEFAULT: dict = {
    "daily_budget_usd": 5.00,   # alert at 80% ($4.00) and 100% ($5.00)
    "exclude_tools": {
        "global": [],
        "per_project": {}
    }
}


def load() -> dict:
    if not CONFIG_FILE.exists():
        return {k: v for k, v in _DEFAULT.items()}
    try:
        data = json.loads(CONFIG_FILE.read_text())
        # backfill missing keys
        for k, v in _DEFAULT.items():
            data.setdefault(k, v)
        return data
    except Exception:
        return {k: v for k, v in _DEFAULT.items()}


def save(config: dict) -> None:
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    CONFIG_FILE.write_text(json.dumps(config, indent=2))


def get_exclusions(project_slug: str | None = None) -> set[str]:
    cfg = load()
    excl = cfg.get("exclude_tools", {})
    result: set[str] = set(excl.get("global", []))
    if project_slug:
        result.update(excl.get("per_project", {}).get(project_slug, []))
    return result


def add_exclusion(tool: str, project_slug: str | None = None) -> bool:
    """Returns True if added, False if already present."""
    cfg = load()
    excl = cfg.setdefault("exclude_tools", {"global": [], "per_project": {}})
    if project_slug:
        bucket = excl.setdefault("per_project", {}).setdefault(project_slug, [])
    else:
        bucket = excl.setdefault("global", [])
    if tool in bucket:
        return False
    bucket.append(tool)
    save(cfg)
    return True


def remove_exclusion(tool: str, project_slug: str | None = None) -> bool:
    """Returns True if removed, False if not found."""
    cfg = load()
    excl = cfg.get("exclude_tools", {})
    if project_slug:
        bucket = excl.get("per_project", {}).get(project_slug, [])
    else:
        bucket = excl.get("global", [])
    if tool not in bucket:
        return False
    bucket.remove(tool)
    save(cfg)
    return True


def list_exclusions() -> dict:
    cfg = load()
    return cfg.get("exclude_tools", {"global": [], "per_project": {}})
