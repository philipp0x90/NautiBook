# main.py
from fastapi import FastAPI, HTTPException, Request, Form
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from datetime import datetime
import aiosqlite
import requests
from contextlib import asynccontextmanager
from typing import Optional

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

# Templates setup
templates = Jinja2Templates(directory="templates")


# Database initialization
async def init_db():
    """Initialize database tables"""
    async with aiosqlite.connect(DATABASE_URL) as db:
        await db.execute("""
            CREATE TABLE IF NOT EXISTS logbook_entries (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp DATETIME NOT NULL,
                aws REAL,
                awa REAL,
                water_temp REAL,
                heading REAL,
                cog REAL,
                log REAL,
                trip REAL,
                depth REAL,
                position_lat REAL,
                position_lon REAL,
                stw REAL,
                sog REAL,
                tws REAL,
                twa REAL,
                sea_state TEXT,
                visibility TEXT,
                notes TEXT
            )
        """)

        await db.execute("""
            CREATE TABLE IF NOT EXISTS trip_photos (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                trip_id INTEGER,
                photo_path TEXT NOT NULL,
                comment TEXT,
                added_by TEXT,
                created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (trip_id) REFERENCES logbook_entries(id)
            )
        """)

        await db.commit()

def get_signalk_data(endpoint):
    resp = requests.get(f"{SIGNALK_URL}{endpoint}")
    if not resp:
        print("Error retrieving signalk data")
        return
    return resp.json()

# Lifespan context manager
@asynccontextmanager
async def lifespan(app: FastAPI):
    await init_db()
    print("Database initialized")
    yield


# Create FastAPI app
app = FastAPI(lifespan=lifespan)

# Mount static files (for CSS, images, etc.)
# app.mount("/static", StaticFiles(directory="static"), name="static")


# Routes
@app.get("/", response_class=HTMLResponse)
async def home(request: Request):
    """Home page - show recent logbook entries"""
    async with aiosqlite.connect(DATABASE_URL) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute("""
            SELECT * FROM logbook_entries 
            ORDER BY timestamp DESC 
            LIMIT 20
        """)
        entries = await cursor.fetchall()

    return templates.TemplateResponse(
        "home.html", {"request": request, "entries": entries}
    )


@app.get("/logbook/new_entry", response_class=HTMLResponse)
async def new_entry_form(request: Request):
    """Show form to create new logbook entry"""
    data = {}
    data["aws"] = get_signalk_data(endpoints["AWS"])["value"]
    data["awa"] = get_signalk_data(endpoints["AWA"])["value"]
    data["water_temp"] = get_signalk_data(endpoints["water_temp"])["value"]
    data["heading"] = get_signalk_data(endpoints["heading"])["value"]
    data["cog"] = get_signalk_data(endpoints["cog"])["value"]
    data["log"] = get_signalk_data(endpoints["log"])["value"]
    data["trip"] = get_signalk_data(endpoints["trip"])["value"]
    data["depth"] = get_signalk_data(endpoints["depth"])["value"]
    coord = get_signalk_data(endpoints["position"])["value"]
    data["lat"] = coord["latitude"]
    data["long"] = coord["longitude"]
    data["stw"] = get_signalk_data(endpoints["stw"])["value"] # Speed through water
    data["sog"] = get_signalk_data(endpoints["sog"])["value"] # Speed over ground
    # data["pressure"] = get_signalk_data(endpoints["pressure"])["value"] # Speed over ground
    # Test server doesn't have the proper sensors
    # data["tws"] = get_signalk_data(endpoints["tws"])["value"]
    # data["twa"] = get_signalk_data(endpoints["twa"])["value"]
    # Collect data from signalK and send the prefilled template.
    return templates.TemplateResponse("new_entry.html", {"request": request, "data": data})


@app.post("/logbook/new_entry")
async def create_entry(
    request: Request,
    position_lat: Optional[float] = Form(None),
    position_lon: Optional[float] = Form(None),
    speed: Optional[float] = Form(None),
    aws: Optional[float] = Form(None),
    tws: Optional[float] = Form(None),
    twa: Optional[float] = Form(None),
    stw: Optional[float] = Form(None),
    sog: Optional[float] = Form(None),
    awa: Optional[float] = Form(None),
    sea_state: Optional[str] = Form(None),
    visibility: Optional[str] = Form(None),
    water_temp: Optional[float] = Form(None),
    # pressure: Optional[float] = Form(None),
    heading: Optional[float] = Form(None),
    cog: Optional[float] = Form(None),
    sails: Optional[str] = Form(None),
    log: Optional[float] = Form(None),
    trip: Optional[float] = Form(None),
    depth: Optional[float] = Form(None),
    notes: Optional[str] = Form(None),
):
    """Create a new logbook entry"""
    print(f"{position_lat=}, {position_lon=}, {speed=}, {aws=}, {log=}, {sog=}")
    return
    async with aiosqlite.connect(DATABASE_URL) as db:
        await db.execute(
            """
            INSERT INTO logbook_entries 
            (timestamp, aws, awa, water_temp, heading, cog, log, trip, depth, position_lat, position_lon, stw, sog, tws, twa, pressure, notes)
            VALUES (?, ?, ?, ?, ?, ?)
        """,
            (datetime.utcnow(), aws, awa, water_temp, heading, cog, log, trip, depth, position_lat, position_lon, stw, sog, tws, twa, pressure, notes),
        )
        await db.commit()

    # Redirect back to home page
    return RedirectResponse(url="/", status_code=303)


@app.get("/logbook/{entry_id}", response_class=HTMLResponse)
async def view_entry(request: Request, entry_id: int):
    """View a specific logbook entry with photos"""
    async with aiosqlite.connect(DATABASE_URL) as db:
        db.row_factory = aiosqlite.Row

        # Get entry
        cursor = await db.execute(
            "SELECT * FROM logbook_entries WHERE id = ?", (entry_id,)
        )
        entry = await cursor.fetchone()

        if entry is None:
            raise HTTPException(status_code=404, detail="Entry not found")

        # Get photos for this entry
        cursor = await db.execute(
            "SELECT * FROM trip_photos WHERE trip_id = ? ORDER BY created_at DESC",
            (entry_id,),
        )
        photos = await cursor.fetchall()

    return templates.TemplateResponse(
        "entry_detail.html", {"request": request, "entry": entry, "photos": photos}
    )


@app.get("/logbook/{entry_id}/add-photo", response_class=HTMLResponse)
async def add_photo_form(request: Request, entry_id: int):
    """Show form to add photo to a trip"""
    async with aiosqlite.connect(DATABASE_URL) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute(
            "SELECT * FROM logbook_entries WHERE id = ?", (entry_id,)
        )
        entry = await cursor.fetchone()

        if entry is None:
            raise HTTPException(status_code=404, detail="Entry not found")

    return templates.TemplateResponse(
        "add_photo.html", {"request": request, "entry": entry}
    )


@app.post("/logbook/{entry_id}/add-photo")
async def add_photo(
    request: Request,
    entry_id: int,
    photo_path: str = Form(...),
    comment: Optional[str] = Form(None),
    added_by: Optional[str] = Form(None),
):
    """Add a photo to a trip"""
    async with aiosqlite.connect(DATABASE_URL) as db:
        # Check if entry exists
        cursor = await db.execute(
            "SELECT id FROM logbook_entries WHERE id = ?", (entry_id,)
        )
        if await cursor.fetchone() is None:
            raise HTTPException(status_code=404, detail="Entry not found")

        # Insert photo
        await db.execute(
            """
            INSERT INTO trip_photos (trip_id, photo_path, comment, added_by)
            VALUES (?, ?, ?, ?)
        """,
            (entry_id, photo_path, comment, added_by),
        )
        await db.commit()

    # Redirect back to entry detail page
    return RedirectResponse(url=f"/logbook/{entry_id}", status_code=303)


# Run with: uvicorn main:app --reload --host 0.0.0.0 --port 8000
