import math
import requests
from config import get_ikommunicate_url, get_keel_offset

DATABASE_URL = "logbook.db"
VESSEL = "self"

# SignalK serves everything in SI units (m/s, radians, Kelvin, meters, Pascals).
# The logbook stores and displays knots, degrees, Celsius, nautical miles and
# hectopascals.
MS_TO_KNOTS = 1.9438444924406
METERS_PER_NM = 1852

endpoints = {
    "AWS": f"vessels/{VESSEL}/environment/wind/speedApparent",
    "AWA": f"vessels/{VESSEL}/environment/wind/angleApparent",
    # outside et non inside : le baromètre du journal est la pression ambiante,
    # celle des cartes météo — inside donne la température du compartiment.
    "pressure": f"vessels/{VESSEL}/environment/outside/pressure",
    "water_temp": f"vessels/{VESSEL}/environment/water/temperature",
    # Cap magnétique, celui que lit le compas de route — et non headingTrue,
    # que l'iKommunicate du bord ne publie pas de toute façon.
    "heading": f"vessels/{VESSEL}/navigation/headingMagnetic",
    "cog": f"vessels/{VESSEL}/navigation/courseOverGroundTrue",
    "log": f"vessels/{VESSEL}/navigation/log",
    "trip": f"vessels/{VESSEL}/navigation/trip/log",
    "depth": f"vessels/{VESSEL}/environment/depth/belowKeel",
    # Repli : tous les serveurs ne publient pas belowKeel. Voir _read_depth.
    "depth_transducer": f"vessels/{VESSEL}/environment/depth/belowTransducer",
    "position": f"vessels/{VESSEL}/navigation/position",
    "stw": f"vessels/{VESSEL}/navigation/speedThroughWater",
    "sog": f"vessels/{VESSEL}/navigation/speedOverGround",
    "tws": f"vessels/{VESSEL}/environment/wind/speedTrue",
    # angleTrueWater et non directionTrue : le journal veut l'angle du vent
    # *par rapport au bateau*, signé bâbord/tribord comme l'AWA, là où
    # directionTrue est un relèvement absolu 0..360 — et l'iKommunicate du bord
    # ne le publie pas.
    "twa": f"vessels/{VESSEL}/environment/wind/angleTrueWater",
}


def _round1(value):
    return round(value, 1)


def _knots(value):
    return round(value * MS_TO_KNOTS, 1)


def _nm(value):
    return round(value / METERS_PER_NM, 1)


def _celsius(value):
    return round(value - 273.15, 1)


def _hpa(value):
    """Pressure: whole hectopascals, what a barometer shows. The 100 is written
    out like the 273.15 above — the prefix *is* the constant, so naming it would
    only add an indirection."""
    return round(value / 100)


def _signed_deg(value):
    """Wind angle: whole degrees, signed (negative = port side), range -180..180."""
    return round((math.degrees(value) + 180) % 360 - 180)


def _bearing_deg(value):
    """Heading / course: whole degrees, normalised to 0..360."""
    return round(math.degrees(value) % 360)


def _get_signalk_http_url() -> str | None:
    base = get_ikommunicate_url()
    if not base:
        return None
    try:
        resp = requests.get(base, timeout=5)
        resp.raise_for_status()
        return resp.json()["endpoints"]["v1"]["signalk-http"]
    except Exception as e:
        print(f"iKommunicate discovery failed: {e}")
        return None


def get_data_at_endpoint(signalk_url: str, endpoint: str):
    try:
        resp = requests.get(f"{signalk_url}{endpoint}/", timeout=5)
        if not resp.ok:
            print(f"Error retrieving signalk data: {endpoint}")
            return None
        return resp.json()
    except Exception as e:
        print(f"SignalK request failed ({endpoint}): {e}")
        return None


def _read(signalk_url: str, key: str, convert=None):
    """Fetch one endpoint and return its value in the unit the logbook displays."""
    r = get_data_at_endpoint(signalk_url, endpoints[key])
    if r is None or r.get("value") is None:
        return None
    if convert is None:
        return r["value"]
    try:
        return convert(r["value"])
    except (TypeError, ValueError) as e:
        print(f"SignalK unit conversion failed ({key}): {e}")
        return None


def _read_depth(signalk_url: str):
    """Profondeur sous la quille en mètres, une décimale.

    belowKeel est ce que le journal veut : le dégagement sous la quille. À
    défaut, belowTransducer sert de repli — mais il est mesuré depuis le
    capteur, donc **plus grand** que le dégagement réel. get_keel_offset() le
    corrige ; non réglé, il vaut 0 et la valeur reportée surestime le fond.
    C'est le sens dangereux de l'erreur, d'où le réglage.
    """
    sous_quille = _read(signalk_url, "depth", _round1)
    if sous_quille is not None:
        return sous_quille

    sous_capteur = _read(signalk_url, "depth_transducer")
    if sous_capteur is None:
        return None
    return _round1(sous_capteur - get_keel_offset())


def get_position() -> tuple[float, float] | None:
    """Return (lat, lon) from SignalK, or None if unavailable."""
    signalk_url = _get_signalk_http_url()
    if not signalk_url:
        return None
    r = get_data_at_endpoint(signalk_url, endpoints["position"])
    if not r:
        return None
    coord = r["value"]
    return coord["latitude"], coord["longitude"]


def get_awa() -> tuple[bool, float | None]:
    """Apparent wind angle alone, for the new-line window's allure proposal.
    Returns (server reached, AWA) — the first tells « SignalK injoignable »
    apart from « pas d'anémomètre ». Far cheaper than get_sensor_data."""
    signalk_url = _get_signalk_http_url()
    if not signalk_url:
        return False, None
    try:
        return True, _read(signalk_url, "AWA", _signed_deg)
    except Exception as e:
        print(f"SignalK AWA read error: {e}")
        return True, None


def get_sensor_data() -> dict:
    signalk_url = _get_signalk_http_url()
    if not signalk_url:
        return {}

    data = {}
    try:
        readings = {
            "aws": _read(signalk_url, "AWS", _knots),          # m/s  → nds
            "awa": _read(signalk_url, "AWA", _signed_deg),     # rad  → °
            "tws": _read(signalk_url, "tws", _knots),          # m/s  → nds
            "twa": _read(signalk_url, "twa", _signed_deg),     # rad  → °
            "pressure": _read(signalk_url, "pressure", _hpa),  # Pa   → hPa
            "water_temp": _read(signalk_url, "water_temp", _celsius),  # K → °C
            "heading": _read(signalk_url, "heading", _bearing_deg),  # rad → °
            "cog": _read(signalk_url, "cog", _bearing_deg),    # rad  → °
            "log": _read(signalk_url, "log", _nm),             # m    → NM
            "trip": _read(signalk_url, "trip", _nm),           # m    → NM
            "depth": _read_depth(signalk_url),                 # m    → m
            "stw": _read(signalk_url, "stw", _knots),          # m/s  → nds
            "sog": _read(signalk_url, "sog", _knots),          # m/s  → nds
        }
        data = {k: v for k, v in readings.items() if v is not None}

        r = get_data_at_endpoint(signalk_url, endpoints["position"])
        if r:
            coord = r["value"]
            data["lat"] = coord["latitude"]
            data["long"] = coord["longitude"]
    except Exception as e:
        print(f"SignalK data collection error: {e}")

    return data


# ── Noms des serveurs (mDNS) ──
# Les adresses récentes des Réglages montrent le nom des machines
# (« nautibook », « MacStudio-de-Benoit ») plutôt que leur IP : le réseau les
# annonce en .local (mDNS, Bonjour / avahi). La bibliothèque standard ne sait
# pas faire la recherche inverse IP → nom en mDNS, d'où cette requête à la
# main : une question PTR envoyée au groupe 224.0.0.251:5353 depuis un port
# quelconque, à laquelle la machine répond directement (« legacy unicast »).

def _dns_nom(paquet: bytes, i: int) -> tuple[str, int]:
    """Lit un nom DNS (avec pointeurs de compression) ; renvoie le nom et la
    position qui suit le nom à son emplacement d'origine."""
    etiquettes, fin = [], None
    for _ in range(64):
        n = paquet[i]
        if n == 0:
            i += 1
            break
        if n & 0xC0 == 0xC0:
            if fin is None:
                fin = i + 2
            i = ((n & 0x3F) << 8) | paquet[i + 1]
            continue
        etiquettes.append(paquet[i + 1:i + 1 + n].decode("utf-8", "replace"))
        i += 1 + n
    return ".".join(etiquettes), (fin if fin is not None else i)


def mdns_name(ip: str, timeout: float = 1.5) -> str | None:
    """Le nom .local qu'une machine du réseau annonce pour son IPv4, ou None."""
    import socket
    import struct
    try:
        inverse = ".".join(reversed(ip.split("."))) + ".in-addr.arpa"
        question = b"".join(bytes([len(e)]) + e.encode() for e in inverse.split(".")) + b"\0"
        paquet = struct.pack(">HHHHHH", 0, 0, 1, 0, 0, 0) + question + struct.pack(">HH", 12, 1)
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.settimeout(timeout)
            s.sendto(paquet, ("224.0.0.251", 5353))
            reponse, _ = s.recvfrom(4096)
        qd, an = struct.unpack(">HH", reponse[4:8])
        i = 12
        for _ in range(qd):
            _, i = _dns_nom(reponse, i)
            i += 4
        for _ in range(an):
            _, i = _dns_nom(reponse, i)
            type_, _, _, longueur = struct.unpack(">HHIH", reponse[i:i + 10])
            i += 10
            if type_ == 12:
                return _dns_nom(reponse, i)[0] or None
            i += longueur
    except Exception:
        return None
    return None


def resolve_host(name: str) -> str | None:
    """L'IPv4 d'un nom, si cette machine sait le résoudre (un nom .local
    demande mDNS côté système : libnss-mdns sur le Pi)."""
    import socket
    try:
        return socket.getaddrinfo(name, None, socket.AF_INET)[0][4][0]
    except Exception:
        return None
