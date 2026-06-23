import requests
from config import get_ikommunicate_url

DATABASE_URL = "logbook.db"
VESSEL = "self"

endpoints = {
    "AWS": f"vessels/{VESSEL}/environment/wind/speedApparent",
    "AWA": f"vessels/{VESSEL}/environment/wind/angleApparent",
    "water_temp": f"vessels/{VESSEL}/environment/water/temperature",
    "heading": f"vessels/{VESSEL}/navigation/headingTrue",
    "cog": f"vessels/{VESSEL}/navigation/courseOverGroundTrue",
    "log": f"vessels/{VESSEL}/navigation/log",
    "trip": f"vessels/{VESSEL}/navigation/trip/log",
    "depth": f"vessels/{VESSEL}/environment/depth/belowKeel",
    "position": f"vessels/{VESSEL}/navigation/position/",
    "stw": f"vessels/{VESSEL}/navigation/speedThroughWater",
    "sog": f"vessels/{VESSEL}/navigation/speedOverGround",
    "tws": f"vessels/{VESSEL}/environment/wind/speedTrue",
    "twa": f"vessels/{VESSEL}/environment/wind/directionTrue",
}


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
        resp = requests.get(f"{signalk_url}{endpoint}", timeout=5)
        if not resp.ok:
            print(f"Error retrieving signalk data: {endpoint}")
            return None
        return resp.json()
    except Exception as e:
        print(f"SignalK request failed ({endpoint}): {e}")
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
        r = get_data_at_endpoint(signalk_url, endpoints["AWS"])
        if r: data["aws"] = r["value"]
        r = get_data_at_endpoint(signalk_url, endpoints["AWA"])
        if r: data["awa"] = r["value"]
        r = get_data_at_endpoint(signalk_url, endpoints["water_temp"])
        if r: data["water_temp"] = r["value"]
        r = get_data_at_endpoint(signalk_url, endpoints["heading"])
        if r: data["heading"] = r["value"]
        r = get_data_at_endpoint(signalk_url, endpoints["cog"])
        if r: data["cog"] = r["value"]
        r = get_data_at_endpoint(signalk_url, endpoints["log"])
        if r: data["log"] = r["value"]
        r = get_data_at_endpoint(signalk_url, endpoints["trip"])
        if r: data["trip"] = r["value"]
        r = get_data_at_endpoint(signalk_url, endpoints["depth"])
        if r: data["depth"] = r["value"]
        r = get_data_at_endpoint(signalk_url, endpoints["position"])
        if r:
            coord = r["value"]
            data["lat"] = coord["latitude"]
            data["long"] = coord["longitude"]
        r = get_data_at_endpoint(signalk_url, endpoints["stw"])
        if r: data["stw"] = r["value"]
        r = get_data_at_endpoint(signalk_url, endpoints["sog"])
        if r: data["sog"] = r["value"]
    except Exception as e:
        print(f"SignalK data collection error: {e}")

    return data
