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


def get_keel_offset() -> float:
    """Distance en mètres entre le capteur de sonde et le bas de la quille.

    Sert uniquement quand SignalK ne publie pas `depth/belowKeel` et qu'il faut
    se rabattre sur `depth/belowTransducer`, mesuré depuis le capteur : le
    dégagement réel sous la quille est plus petit de cet écart. Non réglé, la
    valeur vaut 0 et la profondeur reportée **surestime** le fond disponible.
    """
    try:
        offset = float(load_config().get("keel_offset") or 0)
    except (TypeError, ValueError):
        return 0.0
    # Un écart négatif *augmenterait* la profondeur reportée, c'est-à-dire
    # dans le sens qui rassure à tort. Le capteur est au-dessus du bas de la
    # quille ou au même niveau, jamais en dessous : la valeur est donc positive.
    return max(offset, 0.0)


def is_configured() -> bool:
    return bool(get_ikommunicate_host())
