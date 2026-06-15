import yaml
from pathlib import Path

CONFIG_PATH = Path("config.yaml")


def load_config() -> dict:
    if not CONFIG_PATH.exists():
        return {}
    with open(CONFIG_PATH) as f:
        return yaml.safe_load(f) or {}


def save_config(data: dict):
    existing = load_config()
    existing.update(data)
    with open(CONFIG_PATH, "w") as f:
        yaml.dump(existing, f, default_flow_style=False, allow_unicode=True)


def _normalize_host(raw: str) -> str:
    raw = raw.strip()
    for prefix in ("https://", "http://"):
        if raw.startswith(prefix):
            raw = raw[len(prefix):]
            break
    if raw.endswith("/signalk"):
        raw = raw[: -len("/signalk")]
    return raw.rstrip("/")


def get_ikommunicate_host() -> str | None:
    raw = load_config().get("ikommunicate_url") or None
    if not raw:
        return None
    return _normalize_host(raw) or None


def get_ikommunicate_url() -> str | None:
    host = get_ikommunicate_host()
    if not host:
        return None
    return f"http://{host}/signalk"


def is_configured() -> bool:
    return bool(get_ikommunicate_host())
