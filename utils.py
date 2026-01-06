import requests

# Database configuration
DATABASE_URL = "logbook.db"
iKommunicate_URL = "https://demo.signalk.org/signalk"
# Connect to iKommunicate to make sure we have the right endpoint
SIGNALK_URL  = requests.get(iKommunicate_URL).json()["endpoints"]["v1"]["signalk-http"]
# TODO: get this dynamically
VESSEL = "self"

endpoints = {
    "AWS": f"vessels/{VESSEL}/environment/wind/speedApparent",
    "AWA": f"vessels/{VESSEL}/environment/wind/angleApparent",
    "water_temp": f"vessels/{VESSEL}/environment/water/temperature",
    "heading": f"vessels/{VESSEL}/navigation/headingTrue",
    "cog": f"vessels/{VESSEL}/navigation/courseOverGroundTrue",
    "log": f"vessels/{VESSEL}/navigation/log",
    "trip": f"vessels/{VESSEL}/navigation/trip/log",
    "depth": f"vessels/{VESSEL}/environment/depth/belowKeel", # ALT: belowSurface
    "position": f"vessels/{VESSEL}/navigation/position/",
    "stw": f"vessels/{VESSEL}/navigation/speedThroughWater",
    "sog": f"vessels/{VESSEL}/navigation/speedOverGround",
    "tws": f"vessels/{VESSEL}/environment/wind/speedTrue",
    "twa": f"vessels/{VESSEL}/environment/wind/directionTrue",
    # "pressure": f"vessels/{VESSEL}/environment/outside/pressure"
}

def get_data_at_endpoint(endpoint):
    resp = requests.get(f"{SIGNALK_URL}{endpoint}")
    if not resp:
        print("Error retrieving signalk data")
        return
    return resp.json()

def get_sensor_data():
    # data["pressure"] = get_signalk_data(endpoints["pressure"])["value"] # Speed over ground
    # Test server doesn't have the proper sensors
    # data["tws"] = get_signalk_data(endpoints["tws"])["value"]
    # data["twa"] = get_signalk_data(endpoints["twa"])["value"]
    data = {}
    data["aws"] = get_data_at_endpoint(endpoints["AWS"])["value"]
    data["awa"] = get_data_at_endpoint(endpoints["AWA"])["value"]
    data["water_temp"] = get_data_at_endpoint(endpoints["water_temp"])["value"]
    data["heading"] = get_data_at_endpoint(endpoints["heading"])["value"]
    data["cog"] = get_data_at_endpoint(endpoints["cog"])["value"]
    data["log"] = get_data_at_endpoint(endpoints["log"])["value"]
    data["trip"] = get_data_at_endpoint(endpoints["trip"])["value"]
    data["depth"] = get_data_at_endpoint(endpoints["depth"])["value"]
    coord = get_data_at_endpoint(endpoints["position"])["value"]
    data["lat"] = coord["latitude"]
    data["long"] = coord["longitude"]
    data["stw"] = get_data_at_endpoint(endpoints["stw"])["value"] # Speed through water
    data["sog"] = get_data_at_endpoint(endpoints["sog"])["value"] # Speed over ground

    return data
