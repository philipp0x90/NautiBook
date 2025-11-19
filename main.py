# main.py
from fastapi import FastAPI, HTTPException, Request, Form
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from fastapi.staticfiles import StaticFiles
from datetime import datetime
import aiosqlite
from contextlib import asynccontextmanager
from typing import Optional

# Database configuration
DATABASE_URL = "logbook.db"

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
                position_lat REAL,
                position_lon REAL,
                speed REAL,
                wind_speed REAL,
                notes TEXT,
                created_at DATETIME DEFAULT CURRENT_TIMESTAMP
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


@app.get("/logbook/new", response_class=HTMLResponse)
async def new_entry_form(request: Request):
    """Show form to create new logbook entry"""
    return templates.TemplateResponse("new_entry.html", {"request": request})


@app.post("/logbook/new")
async def create_entry(
    request: Request,
    position_lat: Optional[float] = Form(None),
    position_lon: Optional[float] = Form(None),
    speed: Optional[float] = Form(None),
    wind_speed: Optional[float] = Form(None),
    notes: Optional[str] = Form(None),
):
    """Create a new logbook entry"""
    async with aiosqlite.connect(DATABASE_URL) as db:
        await db.execute(
            """
            INSERT INTO logbook_entries 
            (timestamp, position_lat, position_lon, speed, wind_speed, notes)
            VALUES (?, ?, ?, ?, ?, ?)
        """,
            (datetime.utcnow(), position_lat, position_lon, speed, wind_speed, notes),
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
