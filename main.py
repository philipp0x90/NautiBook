# main.py
from fastapi import FastAPI, HTTPException, Request, Form
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from datetime import datetime
import aiosqlite
from contextlib import asynccontextmanager
from typing import Optional
from utils import get_sensor_data

# Database configuration
DATABASE_URL = "logbook.db"


# Templates setup
templates = Jinja2Templates(directory="templates")


# Database initialization
async def init_db():
    """Initialize database tables"""
    async with aiosqlite.connect(DATABASE_URL) as db:
        # Enable foreign keys in SQLite (important!)
        await db.execute("PRAGMA foreign_keys = ON;")

        # Cruises table
        await db.execute("""
            CREATE TABLE IF NOT EXISTS cruises (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT,
                start_time DATETIME,
                end_time DATETIME,
                created_at DATETIME DEFAULT CURRENT_TIMESTAMP
            )
        """)

        # Routes table
        await db.execute("""
            CREATE TABLE IF NOT EXISTS routes (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT,
                start_time DATETIME,
                end_time DATETIME,
                departure_location TEXT,
                destination_location TEXT,
                notes TEXT,
                created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                finished BOOLEAN,
                cruise_id INTEGER,
                FOREIGN KEY(cruise_id) REFERENCES cruises(id) ON DELETE CASCADE
            )
        """)

        await db.execute("""
            CREATE TABLE IF NOT EXISTS logbook_lines (
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
                pressure REAL,
                sea_state TEXT,
                visibility TEXT,
                sails TEXT,
                notes TEXT,
                route_id INTEGER,
                FOREIGN KEY (route_id) REFERENCES routes(id) ON DELETE CASCADE
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
                cruise_id INTEGER,
                FOREIGN KEY (cruise_id) REFERENCES cruises(id)
            )
        """)

        await db.commit()

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
    """Home page - show recent logbook lines"""
    async with aiosqlite.connect(DATABASE_URL) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute("""
            SELECT * FROM logbook_lines 
            ORDER BY timestamp DESC 
            LIMIT 20
        """)
        lines = await cursor.fetchall()

    return templates.TemplateResponse(
        "home.html", {"request": request, "lines": lines}
    )


@app.get("/logbook/new_line", response_class=HTMLResponse)
async def new_line_form(request: Request):
    """Show form to create new logbook line"""
    data = get_sensor_data()
    return templates.TemplateResponse("new_line.html", {"request": request, "data": data})


@app.post("/logbook/new_line")
async def create_line(
    request: Request,
    position_lat: Optional[float] = Form(None),
    position_lon: Optional[float] = Form(None),
    speed: Optional[float] = Form(None),
    aws: Optional[float] = Form(None),
    tws: Optional[str] = Form(None),
    twa: Optional[str] = Form(None),
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
    """Create a new logbook line"""
    #TODO change SQL to add line to current route
    async with aiosqlite.connect(DATABASE_URL) as db:
        # Replaced tws, twa, pressure with 0 while using demo server, tws not available on demo server
        await db.execute(
            """
            INSERT INTO logbook_lines 
            (timestamp, aws, awa, water_temp, heading, cog, log, trip, depth, position_lat, position_lon, stw, sog, tws, twa, pressure, sea_state, visibility, sails, notes)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
            (datetime.utcnow(), aws, awa, water_temp, heading, cog, log, trip, depth, position_lat, position_lon, stw, sog, 0, 0, 0, sea_state, visibility, sails, notes),
        )
        await db.commit()

    # Redirect back to home page
    return RedirectResponse(url="/", status_code=303)


@app.get("/logbook/{line_id}", response_class=HTMLResponse)
async def view_line(request: Request, line_id: int):
    """View a specific logbook line with photos"""
    async with aiosqlite.connect(DATABASE_URL) as db:
        db.row_factory = aiosqlite.Row

        # Get line
        cursor = await db.execute(
            "SELECT * FROM logbook_lines WHERE id = ?", (line_id,)
        )
        line = await cursor.fetchone()

        if line is None:
            raise HTTPException(status_code=404, detail="Entry not found")

        # Get photos for this line
        cursor = await db.execute(
            "SELECT * FROM trip_photos WHERE trip_id = ? ORDER BY created_at DESC",
            (line_id,),
        )
        photos = await cursor.fetchall()

    return templates.TemplateResponse(
        "line_detail.html", {"request": request, "line": line, "photos": photos}
    )


@app.get("/logbook/{line_id}/add-photo", response_class=HTMLResponse)
async def add_photo_form(request: Request, line_id: int):
    """Show form to add photo to a trip"""
    async with aiosqlite.connect(DATABASE_URL) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute(
            "SELECT * FROM logbook_lines WHERE id = ?", (line_id,)
        )
        line = await cursor.fetchone()

        if line is None:
            raise HTTPException(status_code=404, detail="Entry not found")

    return templates.TemplateResponse(
        "add_photo.html", {"request": request, "line": line}
    )


@app.post("/logbook/{line_id}/add-photo")
async def add_photo(
    request: Request,
    line_id: int,
    photo_path: str = Form(...),
    comment: Optional[str] = Form(None),
    added_by: Optional[str] = Form(None),
):
    """Add a photo to a trip"""
    async with aiosqlite.connect(DATABASE_URL) as db:
        # Check if line exists
        cursor = await db.execute(
            "SELECT id FROM logbook_lines WHERE id = ?", (line_id,)
        )
        if await cursor.fetchone() is None:
            raise HTTPException(status_code=404, detail="Entry not found")

        # Insert photo
        await db.execute(
            """
            INSERT INTO trip_photos (trip_id, photo_path, comment, added_by)
            VALUES (?, ?, ?, ?)
        """,
            (line_id, photo_path, comment, added_by),
        )
        await db.commit()

    # Redirect back to line detail page
    return RedirectResponse(url=f"/logbook/{line_id}", status_code=303)


# Run with: uvicorn main:app --reload --host 0.0.0.0 --port 8000
