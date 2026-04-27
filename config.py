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


def get_ikommunicate_url() -> str | None:
    return load_config().get("ikommunicate_url") or None


def is_configured() -> bool:
    return bool(get_ikommunicate_url())
