# main.py
from fastapi import FastAPI, HTTPException, Request, Form, Query
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
                points_of_sail TEXT,
                visual_pos TEXT,
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
    """Home page - show cruises with nested routes and lines"""
    async with aiosqlite.connect(DATABASE_URL) as db:
        db.row_factory = aiosqlite.Row

        cursor = await db.execute(
            "SELECT * FROM cruises ORDER BY COALESCE(start_time, created_at) DESC"
        )
        cruises_rows = await cursor.fetchall()

        cursor = await db.execute(
            "SELECT * FROM routes ORDER BY COALESCE(start_time, created_at) ASC"
        )
        routes_rows = await cursor.fetchall()

        cursor = await db.execute(
            "SELECT * FROM logbook_lines ORDER BY timestamp ASC"
        )
        lines_rows = await cursor.fetchall()

    # Build hierarchy: cruise → routes → lines
    cruise_map = {}
    cruise_list = []
    for c in cruises_rows:
        cd = dict(c)
        cd["routes"] = []
        cruise_map[c["id"]] = cd
        cruise_list.append(cd)

    route_map = {}
    for r in routes_rows:
        rd = dict(r)
        rd["lines"] = []
        route_map[r["id"]] = rd
        if r["cruise_id"] and r["cruise_id"] in cruise_map:
            cruise_map[r["cruise_id"]]["routes"].append(rd)

    for ln in lines_rows:
        if ln["route_id"] and ln["route_id"] in route_map:
            route_map[ln["route_id"]]["lines"].append(dict(ln))

    current_cruise = cruise_list[0] if cruise_list else None
    past_cruises = cruise_list[1:]

    return templates.TemplateResponse(
        "home.html",
        {
            "request": request,
            "current_cruise": current_cruise,
            "past_cruises": past_cruises,
        },
    )


@app.get("/cruise/new", response_class=HTMLResponse)
async def new_cruise_form(request: Request):
    return templates.TemplateResponse("new_cruise.html", {"request": request})


@app.post("/cruise/new")
async def create_cruise(
    name: str = Form(...),
    start_time: Optional[str] = Form(None),
    end_time: Optional[str] = Form(None),
):
    async with aiosqlite.connect(DATABASE_URL) as db:
        await db.execute(
            "INSERT INTO cruises (name, start_time, end_time) VALUES (?, ?, ?)",
            (name, start_time or None, end_time or None),
        )
        await db.commit()
    return RedirectResponse(url="/", status_code=303)


@app.get("/route/new", response_class=HTMLResponse)
async def new_route_form(request: Request, cruise_id: int = Query(...)):
    async with aiosqlite.connect(DATABASE_URL) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute("SELECT * FROM cruises WHERE id = ?", (cruise_id,))
        cruise = await cursor.fetchone()
    if cruise is None:
        raise HTTPException(status_code=404, detail="Cruise not found")
    return templates.TemplateResponse(
        "new_route.html", {"request": request, "cruise": dict(cruise)}
    )


@app.post("/route/new")
async def create_route(
    cruise_id: int = Form(...),
    departure_location: Optional[str] = Form(None),
    destination_location: Optional[str] = Form(None),
    name: Optional[str] = Form(None),
    start_time: Optional[str] = Form(None),
    notes: Optional[str] = Form(None),
):
    async with aiosqlite.connect(DATABASE_URL) as db:
        await db.execute(
            """INSERT INTO routes (cruise_id, name, departure_location, destination_location, start_time, notes)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (cruise_id, name or None, departure_location or None, destination_location or None, start_time or None, notes or None),
        )
        await db.commit()
    return RedirectResponse(url="/", status_code=303)


@app.get("/logbook/new_line", response_class=HTMLResponse)
async def new_line_form(request: Request, route_id: int = Query(...)):
    """Show form to create new logbook line, always within a route"""
    async with aiosqlite.connect(DATABASE_URL) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute(
            """SELECT r.*, c.name AS cruise_name
               FROM routes r JOIN cruises c ON r.cruise_id = c.id
               WHERE r.id = ?""",
            (route_id,),
        )
        route = await cursor.fetchone()
    if route is None:
        raise HTTPException(status_code=404, detail="Route not found")
    data = get_sensor_data()
    return templates.TemplateResponse(
        "new_line.html", {"request": request, "data": data, "route": dict(route)}
    )


@app.post("/logbook/new_line")
async def create_line(
    request: Request,
    route_id: int = Form(...),
    position_lat: Optional[float] = Form(None),
    position_lon: Optional[float] = Form(None),
    aws: Optional[float] = Form(None),
    stw: Optional[float] = Form(None),
    sog: Optional[float] = Form(None),
    awa: Optional[float] = Form(None),
    sea_state: Optional[str] = Form(None),
    visibility: Optional[str] = Form(None),
    water_temp: Optional[float] = Form(None),
    heading: Optional[float] = Form(None),
    cog: Optional[float] = Form(None),
    sails: Optional[str] = Form(None),
    log: Optional[float] = Form(None),
    trip: Optional[float] = Form(None),
    depth: Optional[float] = Form(None),
    points_of_sail: Optional[str] = Form(None),
    visual_pos: Optional[str] = Form(None),
    notes: Optional[str] = Form(None),
):
    async with aiosqlite.connect(DATABASE_URL) as db:
        await db.execute(
            """INSERT INTO logbook_lines
               (timestamp, route_id, aws, awa, water_temp, heading, cog, log, trip, depth,
                position_lat, position_lon, stw, sog, tws, twa, pressure,
                sea_state, visibility, sails, points_of_sail, visual_pos, notes)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (datetime.utcnow(), route_id, aws, awa, water_temp, heading, cog, log, trip, depth,
             position_lat, position_lon, stw, sog, 0, 0, 0,
             sea_state, visibility, sails, points_of_sail, visual_pos, notes),
        )
        await db.commit()
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
