import math
import requests
from config import get_ikommunicate_url

DATABASE_URL = "logbook.db"
VESSEL = "self"

# SignalK serves everything in SI units (m/s, radians, Kelvin, meters).
# The logbook stores and displays knots, degrees, Celsius and nautical miles.
MS_TO_KNOTS = 1.9438444924406
METERS_PER_NM = 1852

endpoints = {
    "AWS": f"vessels/{VESSEL}/environment/wind/speedApparent",
    "AWA": f"vessels/{VESSEL}/environment/wind/angleApparent",
    "water_temp": f"vessels/{VESSEL}/environment/water/temperature",
    "heading": f"vessels/{VESSEL}/navigation/headingTrue",
    "cog": f"vessels/{VESSEL}/navigation/courseOverGroundTrue",
    "log": f"vessels/{VESSEL}/navigation/log",
    "trip": f"vessels/{VESSEL}/navigation/trip/log",
    "depth": f"vessels/{VESSEL}/environment/depth/belowKeel",
    "position": f"vessels/{VESSEL}/navigation/position",
    "stw": f"vessels/{VESSEL}/navigation/speedThroughWater",
    "sog": f"vessels/{VESSEL}/navigation/speedOverGround",
    "tws": f"vessels/{VESSEL}/environment/wind/speedTrue",
    "twa": f"vessels/{VESSEL}/environment/wind/directionTrue",
}


def _round1(value):
    return round(value, 1)


def _knots(value):
    return round(value * MS_TO_KNOTS, 1)


def _nm(value):
    return round(value / METERS_PER_NM, 1)


def _celsius(value):
    return round(value - 273.15, 1)


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


def get_sensor_data() -> dict:
    signalk_url = _get_signalk_http_url()
    if not signalk_url:
        return {}

    data = {}
    try:
        readings = {
            "aws": _read(signalk_url, "AWS", _knots),          # m/s  → nds
            "awa": _read(signalk_url, "AWA", _signed_deg),     # rad  → °
            "water_temp": _read(signalk_url, "water_temp", _celsius),  # K → °C
            "heading": _read(signalk_url, "heading", _bearing_deg),    # rad → °
            "cog": _read(signalk_url, "cog", _bearing_deg),    # rad  → °
            "log": _read(signalk_url, "log", _nm),             # m    → NM
            "trip": _read(signalk_url, "trip", _nm),           # m    → NM
            "depth": _read(signalk_url, "depth", _round1),     # m    → m
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
