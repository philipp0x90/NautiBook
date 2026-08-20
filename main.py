# main.py
from fastapi import FastAPI, HTTPException, Request, Form, Query
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from datetime import datetime
import asyncio
import aiosqlite
from contextlib import asynccontextmanager
from typing import Optional
from utils import get_sensor_data, get_position
from config import get_ikommunicate_url, get_ikommunicate_host, save_config, is_configured

DATABASE_URL = "logbook.db"
templates = Jinja2Templates(directory="templates")


def _datefr(value):
    """Convert YYYY-MM-DD (or ISO datetime) to DD/MM/YYYY for display."""
    if not value:
        return "—"
    s = str(value)
    # Only convert strings that start with YYYY-MM-DD
    if len(s) >= 10 and s[4] == "-" and s[7] == "-" and s[:4].isdigit() and s[5:7].isdigit() and s[8:10].isdigit():
        return f"{s[8:10]}/{s[5:7]}/{s[:4]}"
    return s


templates.env.filters["datefr"] = _datefr


async def init_db():
    async with aiosqlite.connect(DATABASE_URL) as db:
        await db.execute("PRAGMA foreign_keys = ON;")

        # ── Core tables ──────────────────────────────────────────────────

        await db.execute("""
            CREATE TABLE IF NOT EXISTS cruises (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT,
                departure TEXT,
                destination TEXT,
                start_time DATETIME,
                end_time DATETIME,
                loch_start REAL,
                loch_end REAL,
                created_at DATETIME DEFAULT CURRENT_TIMESTAMP
            )
        """)

        await db.execute("""
            CREATE TABLE IF NOT EXISTS routes (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT,
                start_time DATETIME,
                end_time DATETIME,
                departure_location TEXT,
                destination_location TEXT,
                notes TEXT,
                finished BOOLEAN,
                cruise_id INTEGER,
                motor_hours_start REAL,
                motor_hours_end REAL,
                created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
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
                route_id INTEGER,
                photo_path TEXT NOT NULL,
                comment TEXT,
                added_by TEXT,
                lat REAL,
                lon REAL,
                created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                cruise_id INTEGER,
                FOREIGN KEY (cruise_id) REFERENCES cruises(id),
                FOREIGN KEY (route_id) REFERENCES routes(id) ON DELETE CASCADE
            )
        """)

        # ── Ship tables ───────────────────────────────────────────────────

        await db.execute("""
            CREATE TABLE IF NOT EXISTS ship_info (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT,
                home_port TEXT,
                flag TEXT,
                mmsi TEXT,
                call_sign TEXT,
                registration TEXT,
                registry TEXT,
                issued_date TEXT,
                valid_until TEXT,
                loa REAL,
                hull_length REAL,
                waterline_length REAL,
                surface REAL,
                beam REAL,
                draft REAL,
                air_draft REAL,
                mast_height REAL,
                clearance_no_mast REAL,
                freeboard TEXT,
                displacement REAL,
                ballast REAL,
                sail_main REAL,
                sail_genoa REAL,
                sail_spinnaker REAL,
                sail_trinquette REAL,
                sail_portant REAL,
                tank_fuel REAL,
                tank_water REAL,
                engine_brand TEXT,
                engine_model TEXT,
                engine_serial TEXT,
                engine_power TEXT,
                engine_consumption REAL,
                engine_hours_initial REAL,
                engine_hours_date TEXT,
                insurance_company TEXT,
                insurance_policy TEXT,
                insurance_start TEXT,
                insurance_end TEXT,
                misc_notes TEXT
            )
        """)

        await db.execute("""
            CREATE TABLE IF NOT EXISTS todo_items (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                ship_id INTEGER NOT NULL DEFAULT 1,
                title TEXT,
                task TEXT,
                urgent BOOLEAN DEFAULT 0,
                status TEXT DEFAULT 'A faire',
                due_date TEXT,
                completed_at TEXT,
                tags TEXT,
                photo_path TEXT,
                created_at DATETIME DEFAULT CURRENT_TIMESTAMP
            )
        """)

        await db.execute("""
            CREATE TABLE IF NOT EXISTS expenses (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                ship_id INTEGER NOT NULL DEFAULT 1,
                date TEXT,
                designation TEXT,
                unit_type TEXT,
                unit_price REAL,
                paid REAL,
                balance REAL,
                expense_type TEXT,
                category TEXT,
                payment TEXT,
                supplier TEXT,
                created_at DATETIME DEFAULT CURRENT_TIMESTAMP
            )
        """)

        await db.execute("""
            CREATE TABLE IF NOT EXISTS contacts (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                ship_id INTEGER NOT NULL DEFAULT 1,
                company TEXT,
                contact_name TEXT,
                category TEXT,
                phone TEXT,
                email TEXT,
                website TEXT,
                address TEXT,
                notes TEXT,
                created_at DATETIME DEFAULT CURRENT_TIMESTAMP
            )
        """)

        await db.execute("""
            CREATE TABLE IF NOT EXISTS stopovers (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                route_id INTEGER,
                locality TEXT,
                name TEXT,
                type TEXT,
                cost REAL DEFAULT 0,
                cost_per_night REAL,
                notes TEXT,
                arrival_date TEXT,
                departure_date TEXT,
                created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (route_id) REFERENCES routes(id) ON DELETE CASCADE
            )
        """)

        await db.execute("""
            CREATE TABLE IF NOT EXISTS crew_members (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                first_name TEXT,
                last_name TEXT,
                age INTEGER,
                birth_place TEXT,
                birth_date TEXT,
                nationality TEXT,
                street TEXT,
                postal_code TEXT,
                city TEXT,
                id_type TEXT,
                id_number TEXT,
                phone TEXT,
                email TEXT,
                photo_path TEXT,
                created_at DATETIME DEFAULT CURRENT_TIMESTAMP
            )
        """)

        await db.execute("""
            CREATE TABLE IF NOT EXISTS cruise_crew (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                cruise_id INTEGER,
                crew_member_id INTEGER,
                role TEXT DEFAULT 'crew',
                embark_date TEXT,
                disembark_date TEXT,
                FOREIGN KEY (cruise_id) REFERENCES cruises(id) ON DELETE CASCADE,
                FOREIGN KEY (crew_member_id) REFERENCES crew_members(id) ON DELETE CASCADE
            )
        """)

        await db.execute("""
            CREATE TABLE IF NOT EXISTS track_points (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                route_id INTEGER,
                timestamp DATETIME NOT NULL,
                lat REAL NOT NULL,
                lon REAL NOT NULL,
                photo_path TEXT,
                FOREIGN KEY (route_id) REFERENCES routes(id) ON DELETE CASCADE
            )
        """)

        await db.commit()


TRACK_INTERVAL = 30  # seconds between automatic GPS recordings


async def track_recorder_loop():
    """Records GPS position from SignalK every TRACK_INTERVAL seconds into track_points."""
    while True:
        await asyncio.sleep(TRACK_INTERVAL)
        try:
            async with aiosqlite.connect(DATABASE_URL) as db:
                cursor = await db.execute(
                    "SELECT id FROM routes WHERE finished IS NOT 1 ORDER BY id DESC LIMIT 1"
                )
                row = await cursor.fetchone()
            if row is None:
                continue
            route_id = row[0]

            position = await asyncio.to_thread(get_position)
            if position is None:
                continue
            lat, lon = position

            async with aiosqlite.connect(DATABASE_URL) as db:
                now = datetime.now().strftime("%Y-%m-%dT%H:%M:%S")
                await db.execute(
                    "INSERT INTO track_points (route_id, timestamp, lat, lon) VALUES (?, ?, ?, ?)",
                    (route_id, now, lat, lon),
                )
                await db.commit()
            print(f"Track point: route {route_id}  {lat:.5f}, {lon:.5f}")
        except Exception as e:
            print(f"Track recorder error: {e}")


@asynccontextmanager
async def lifespan(app: FastAPI):
    await init_db()
    print("Database initialized")
    recorder = asyncio.create_task(track_recorder_loop())
    yield
    recorder.cancel()
    try:
        await recorder
    except asyncio.CancelledError:
        pass


app = FastAPI(lifespan=lifespan)


# ── Ship helpers ──────────────────────────────────────────────────────────────

def get_current_ship_id(request: Request) -> int:
    try:
        return int(request.cookies.get('ship_id', 1))
    except (ValueError, TypeError):
        return 1


async def _fetch_ship(db, ship_id: int):
    """Return the requested ship, falling back to the first ship if not found."""
    cursor = await db.execute("SELECT * FROM ship_info WHERE id = ?", (ship_id,))
    ship = await cursor.fetchone()
    if ship is None:
        cursor = await db.execute("SELECT * FROM ship_info ORDER BY id LIMIT 1")
        ship = await cursor.fetchone()
    return ship


# ── Home ──────────────────────────────────────────────────────────────────────

@app.get("/", response_class=HTMLResponse)
async def home(request: Request):
    if not is_configured():
        return RedirectResponse(url="/setup", status_code=302)
    return templates.TemplateResponse("home.html", {"request": request})


# ── Ship (Navire) ─────────────────────────────────────────────────────────────

@app.get("/ship", response_class=HTMLResponse)
async def ship_index(request: Request):
    return RedirectResponse(url="/ship/info", status_code=302)


@app.get("/ship/select", response_class=HTMLResponse)
async def ship_select(request: Request):
    async with aiosqlite.connect(DATABASE_URL) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute("SELECT * FROM ship_info ORDER BY id")
        ships = await cursor.fetchall()
    return templates.TemplateResponse(
        "ship/select.html",
        {
            "request": request,
            "active_section": "ship",
            "ships": [dict(s) for s in ships],
            "current_ship_id": get_current_ship_id(request),
        },
    )


@app.post("/ship/select/{ship_id}")
async def set_current_ship(ship_id: int):
    response = RedirectResponse(url="/ship/info", status_code=303)
    response.set_cookie(key="ship_id", value=str(ship_id), max_age=365 * 24 * 3600, httponly=True)
    return response


@app.get("/ship/new", response_class=HTMLResponse)
async def new_ship_form(request: Request):
    return templates.TemplateResponse(
        "ship/new.html",
        {"request": request, "active_section": "ship"},
    )


@app.post("/ship/new")
async def create_ship(name: str = Form(...)):
    async with aiosqlite.connect(DATABASE_URL) as db:
        cursor = await db.execute("INSERT INTO ship_info (name) VALUES (?)", (name,))
        ship_id = cursor.lastrowid
        await db.commit()
    response = RedirectResponse(url="/ship/info/edit", status_code=303)
    response.set_cookie(key="ship_id", value=str(ship_id), max_age=365 * 24 * 3600, httponly=True)
    return response


@app.get("/ship/info", response_class=HTMLResponse)
async def ship_info(request: Request):
    async with aiosqlite.connect(DATABASE_URL) as db:
        db.row_factory = aiosqlite.Row
        ship = await _fetch_ship(db, get_current_ship_id(request))
        ship = dict(ship) if ship else None
    return templates.TemplateResponse(
        "ship/info.html",
        {"request": request, "active_section": "ship", "ship": ship, "current_ship": ship},
    )


@app.get("/ship/info/edit", response_class=HTMLResponse)
async def edit_ship_info_form(request: Request):
    async with aiosqlite.connect(DATABASE_URL) as db:
        db.row_factory = aiosqlite.Row
        ship = await _fetch_ship(db, get_current_ship_id(request))
        ship = dict(ship) if ship else None
    return templates.TemplateResponse(
        "ship/info_edit.html",
        {"request": request, "active_section": "ship", "ship": ship, "current_ship": ship},
    )


@app.post("/ship/info/edit")
async def save_ship_info(
    request: Request,
    name: Optional[str] = Form(None),
    home_port: Optional[str] = Form(None),
    flag: Optional[str] = Form(None),
    mmsi: Optional[str] = Form(None),
    call_sign: Optional[str] = Form(None),
    registration: Optional[str] = Form(None),
    registry: Optional[str] = Form(None),
    issued_date: Optional[str] = Form(None),
    valid_until: Optional[str] = Form(None),
    loa: Optional[float] = Form(None),
    hull_length: Optional[float] = Form(None),
    waterline_length: Optional[float] = Form(None),
    surface: Optional[float] = Form(None),
    beam: Optional[float] = Form(None),
    draft: Optional[float] = Form(None),
    air_draft: Optional[float] = Form(None),
    mast_height: Optional[float] = Form(None),
    clearance_no_mast: Optional[float] = Form(None),
    freeboard: Optional[str] = Form(None),
    displacement: Optional[float] = Form(None),
    ballast: Optional[float] = Form(None),
    sail_main: Optional[float] = Form(None),
    sail_genoa: Optional[float] = Form(None),
    sail_spinnaker: Optional[float] = Form(None),
    sail_trinquette: Optional[float] = Form(None),
    sail_portant: Optional[float] = Form(None),
    tank_fuel: Optional[float] = Form(None),
    tank_water: Optional[float] = Form(None),
    engine_brand: Optional[str] = Form(None),
    engine_model: Optional[str] = Form(None),
    engine_serial: Optional[str] = Form(None),
    engine_power: Optional[str] = Form(None),
    engine_consumption: Optional[float] = Form(None),
    engine_hours_initial: Optional[float] = Form(None),
    engine_hours_date: Optional[str] = Form(None),
    insurance_company: Optional[str] = Form(None),
    insurance_policy: Optional[str] = Form(None),
    insurance_start: Optional[str] = Form(None),
    insurance_end: Optional[str] = Form(None),
    misc_notes: Optional[str] = Form(None),
):
    ship_id = get_current_ship_id(request)
    vals = (
        name or None, home_port or None, flag or None, mmsi or None, call_sign or None,
        registration or None, registry or None, issued_date or None, valid_until or None,
        loa, hull_length, waterline_length, surface, beam, draft, air_draft,
        mast_height, clearance_no_mast, freeboard or None, displacement, ballast,
        sail_main, sail_genoa, sail_spinnaker, sail_trinquette, sail_portant,
        tank_fuel, tank_water,
        engine_brand or None, engine_model or None, engine_serial or None,
        engine_power or None, engine_consumption, engine_hours_initial,
        engine_hours_date or None,
        insurance_company or None, insurance_policy or None,
        insurance_start or None, insurance_end or None,
        misc_notes or None,
    )
    async with aiosqlite.connect(DATABASE_URL) as db:
        cursor = await db.execute("SELECT id FROM ship_info WHERE id = ?", (ship_id,))
        existing = await cursor.fetchone()
        if existing:
            await db.execute(
                """UPDATE ship_info SET
                   name=?, home_port=?, flag=?, mmsi=?, call_sign=?, registration=?, registry=?,
                   issued_date=?, valid_until=?, loa=?, hull_length=?, waterline_length=?,
                   surface=?, beam=?, draft=?, air_draft=?, mast_height=?, clearance_no_mast=?,
                   freeboard=?, displacement=?, ballast=?, sail_main=?, sail_genoa=?,
                   sail_spinnaker=?, sail_trinquette=?, sail_portant=?, tank_fuel=?, tank_water=?,
                   engine_brand=?, engine_model=?, engine_serial=?, engine_power=?,
                   engine_consumption=?, engine_hours_initial=?, engine_hours_date=?,
                   insurance_company=?, insurance_policy=?, insurance_start=?, insurance_end=?,
                   misc_notes=?
                   WHERE id=?""",
                (*vals, ship_id),
            )
        else:
            cursor = await db.execute(
                """INSERT INTO ship_info (
                   name, home_port, flag, mmsi, call_sign, registration, registry,
                   issued_date, valid_until, loa, hull_length, waterline_length,
                   surface, beam, draft, air_draft, mast_height, clearance_no_mast,
                   freeboard, displacement, ballast, sail_main, sail_genoa,
                   sail_spinnaker, sail_trinquette, sail_portant, tank_fuel, tank_water,
                   engine_brand, engine_model, engine_serial, engine_power,
                   engine_consumption, engine_hours_initial, engine_hours_date,
                   insurance_company, insurance_policy, insurance_start, insurance_end,
                   misc_notes)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                vals,
            )
            new_id = cursor.lastrowid
            await db.commit()
            response = RedirectResponse(url="/ship/info", status_code=303)
            response.set_cookie(key="ship_id", value=str(new_id), max_age=365 * 24 * 3600, httponly=True)
            return response
        await db.commit()
    return RedirectResponse(url="/ship/info", status_code=303)


@app.get("/ship/expenses", response_class=HTMLResponse)
async def ship_expenses(request: Request):
    ship_id = get_current_ship_id(request)
    async with aiosqlite.connect(DATABASE_URL) as db:
        db.row_factory = aiosqlite.Row
        ship = await _fetch_ship(db, ship_id)
        cursor = await db.execute(
            "SELECT * FROM expenses WHERE ship_id = ? ORDER BY date DESC", (ship_id,)
        )
        entries = await cursor.fetchall()
    return templates.TemplateResponse(
        "ship/expenses.html",
        {
            "request": request, "active_section": "ship",
            "current_ship": dict(ship) if ship else None,
            "entries": [dict(e) for e in entries],
        },
    )


@app.get("/ship/expenses/new", response_class=HTMLResponse)
async def new_expense_form(request: Request):
    ship_id = get_current_ship_id(request)
    async with aiosqlite.connect(DATABASE_URL) as db:
        db.row_factory = aiosqlite.Row
        ship = await _fetch_ship(db, ship_id)
        cursor = await db.execute(
            "SELECT id, company, contact_name FROM contacts WHERE ship_id = ? ORDER BY company, contact_name",
            (ship_id,),
        )
        contacts = await cursor.fetchall()
    return templates.TemplateResponse(
        "ship/expenses_new.html",
        {
            "request": request, "active_section": "ship",
            "current_ship": dict(ship) if ship else None,
            "contacts": [dict(c) for c in contacts],
        },
    )


@app.post("/ship/expenses/new")
async def create_expense(
    request: Request,
    date: Optional[str] = Form(None),
    designation: Optional[str] = Form(None),
    unit_type: Optional[str] = Form(None),
    unit_price: Optional[float] = Form(None),
    paid: Optional[float] = Form(None),
    expense_type: Optional[str] = Form(None),
    category: Optional[str] = Form(None),
    payment: Optional[str] = Form(None),
    supplier: Optional[str] = Form(None),
):
    ship_id = get_current_ship_id(request)
    balance = None
    if unit_price is not None:
        if paid is None:
            paid = 0
        balance = unit_price - paid
    async with aiosqlite.connect(DATABASE_URL) as db:
        await db.execute(
            """INSERT INTO expenses (ship_id, date, designation, unit_type, unit_price, paid, balance, expense_type, category, payment, supplier)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (ship_id, date or None, designation or None, unit_type or None, unit_price, paid, balance,
             expense_type or None, category or None, payment or None, supplier or None),
        )
        await db.commit()
    return RedirectResponse(url="/ship/expenses", status_code=303)


@app.post("/ship/expenses/{entry_id}/delete")
async def delete_expense(entry_id: int):
    async with aiosqlite.connect(DATABASE_URL) as db:
        await db.execute("DELETE FROM expenses WHERE id = ?", (entry_id,))
        await db.commit()
    return RedirectResponse(url="/ship/expenses", status_code=303)


@app.get("/ship/todo", response_class=HTMLResponse)
async def ship_todo(request: Request):
    ship_id = get_current_ship_id(request)
    async with aiosqlite.connect(DATABASE_URL) as db:
        db.row_factory = aiosqlite.Row
        ship = await _fetch_ship(db, ship_id)
        cursor = await db.execute(
            """SELECT * FROM todo_items WHERE ship_id = ?
               ORDER BY
                 CASE WHEN status = 'Terminé' THEN 1 ELSE 0 END,
                 urgent DESC,
                 CASE WHEN due_date IS NULL THEN 1 ELSE 0 END,
                 due_date ASC,
                 created_at DESC""",
            (ship_id,),
        )
        items = await cursor.fetchall()
    return templates.TemplateResponse(
        "ship/todo.html",
        {
            "request": request, "active_section": "ship",
            "current_ship": dict(ship) if ship else None,
            "items": [dict(i) for i in items],
        },
    )


@app.get("/ship/todo/new", response_class=HTMLResponse)
async def new_todo_form(request: Request):
    async with aiosqlite.connect(DATABASE_URL) as db:
        db.row_factory = aiosqlite.Row
        ship = await _fetch_ship(db, get_current_ship_id(request))
    return templates.TemplateResponse(
        "ship/todo_new.html",
        {"request": request, "active_section": "ship", "current_ship": dict(ship) if ship else None},
    )


@app.post("/ship/todo/new")
async def create_todo_item(
    request: Request,
    title: str = Form(...),
    task: Optional[str] = Form(None),
    urgent: Optional[str] = Form(None),
    due_date: Optional[str] = Form(None),
    tags: Optional[str] = Form(None),
    photo_path: Optional[str] = Form(None),
):
    ship_id = get_current_ship_id(request)
    async with aiosqlite.connect(DATABASE_URL) as db:
        await db.execute(
            "INSERT INTO todo_items (ship_id, title, task, urgent, due_date, tags, photo_path) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (ship_id, title, task or None, 1 if urgent else 0, due_date or None, tags or None, photo_path or None),
        )
        await db.commit()
    return RedirectResponse(url="/ship/todo", status_code=303)


@app.get("/ship/todo/{item_id}/edit", response_class=HTMLResponse)
async def edit_todo_form(request: Request, item_id: int):
    async with aiosqlite.connect(DATABASE_URL) as db:
        db.row_factory = aiosqlite.Row
        ship = await _fetch_ship(db, get_current_ship_id(request))
        cursor = await db.execute("SELECT * FROM todo_items WHERE id = ?", (item_id,))
        item = await cursor.fetchone()
    if item is None:
        raise HTTPException(status_code=404, detail="Item not found")
    return templates.TemplateResponse(
        "ship/todo_edit.html",
        {
            "request": request, "active_section": "ship",
            "current_ship": dict(ship) if ship else None,
            "item": dict(item),
        },
    )


@app.post("/ship/todo/{item_id}/edit")
async def update_todo_item(
    item_id: int,
    title: Optional[str] = Form(None),
    task: Optional[str] = Form(None),
    urgent: Optional[str] = Form(None),
    status: Optional[str] = Form(None),
    due_date: Optional[str] = Form(None),
    completed_at: Optional[str] = Form(None),
    tags: Optional[str] = Form(None),
    photo_path: Optional[str] = Form(None),
):
    async with aiosqlite.connect(DATABASE_URL) as db:
        await db.execute(
            """UPDATE todo_items SET title=?, task=?, urgent=?, status=?, due_date=?, completed_at=?, tags=?, photo_path=?
               WHERE id=?""",
            (title or None, task or None, 1 if urgent else 0, status or 'A faire', due_date or None,
             completed_at or None, tags or None, photo_path or None, item_id),
        )
        await db.commit()
    return RedirectResponse(url="/ship/todo", status_code=303)


@app.post("/ship/todo/{item_id}/done")
async def mark_todo_done(item_id: int, next: Optional[str] = Form(None)):
    today = datetime.now().strftime("%Y-%m-%d")
    async with aiosqlite.connect(DATABASE_URL) as db:
        await db.execute(
            "UPDATE todo_items SET status='Terminé', completed_at=? WHERE id=? AND status != 'Terminé'",
            (today, item_id),
        )
        await db.commit()
    return RedirectResponse(url=next or "/ship/todo", status_code=303)


@app.post("/ship/todo/{item_id}/undo")
async def undo_todo_item(item_id: int, next: Optional[str] = Form(None)):
    async with aiosqlite.connect(DATABASE_URL) as db:
        await db.execute(
            "UPDATE todo_items SET status='A faire', completed_at=NULL WHERE id=?",
            (item_id,),
        )
        await db.commit()
    return RedirectResponse(url=next or "/ship/todo", status_code=303)


@app.post("/ship/todo/{item_id}/delete")
async def delete_todo_item(item_id: int):
    async with aiosqlite.connect(DATABASE_URL) as db:
        await db.execute("DELETE FROM todo_items WHERE id = ?", (item_id,))
        await db.commit()
    return RedirectResponse(url="/ship/todo", status_code=303)


@app.get("/ship/fuel", response_class=HTMLResponse)
async def ship_fuel(request: Request):
    async with aiosqlite.connect(DATABASE_URL) as db:
        db.row_factory = aiosqlite.Row
        ship = await _fetch_ship(db, get_current_ship_id(request))
    return templates.TemplateResponse(
        "ship/fuel.html",
        {"request": request, "active_section": "ship", "current_ship": dict(ship) if ship else None},
    )


@app.get("/ship/contacts", response_class=HTMLResponse)
async def ship_contacts(request: Request):
    ship_id = get_current_ship_id(request)
    async with aiosqlite.connect(DATABASE_URL) as db:
        db.row_factory = aiosqlite.Row
        ship = await _fetch_ship(db, ship_id)
        cursor = await db.execute(
            "SELECT * FROM contacts WHERE ship_id = ? ORDER BY company, contact_name", (ship_id,)
        )
        contacts = await cursor.fetchall()
    return templates.TemplateResponse(
        "ship/contacts.html",
        {
            "request": request, "active_section": "ship",
            "current_ship": dict(ship) if ship else None,
            "contacts": [dict(c) for c in contacts],
        },
    )


@app.get("/ship/contacts/new", response_class=HTMLResponse)
async def new_contact_form(request: Request):
    async with aiosqlite.connect(DATABASE_URL) as db:
        db.row_factory = aiosqlite.Row
        ship = await _fetch_ship(db, get_current_ship_id(request))
    return templates.TemplateResponse(
        "ship/contacts_new.html",
        {"request": request, "active_section": "ship", "current_ship": dict(ship) if ship else None},
    )


@app.post("/ship/contacts/new")
async def create_contact(
    request: Request,
    company: Optional[str] = Form(None),
    contact_name: Optional[str] = Form(None),
    category: Optional[str] = Form(None),
    phone: Optional[str] = Form(None),
    email: Optional[str] = Form(None),
    website: Optional[str] = Form(None),
    address: Optional[str] = Form(None),
    notes: Optional[str] = Form(None),
):
    ship_id = get_current_ship_id(request)
    async with aiosqlite.connect(DATABASE_URL) as db:
        await db.execute(
            """INSERT INTO contacts (ship_id, company, contact_name, category, phone, email, website, address, notes)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (ship_id, company or None, contact_name or None, category or None, phone or None,
             email or None, website or None, address or None, notes or None),
        )
        await db.commit()
    return RedirectResponse(url="/ship/contacts", status_code=303)


@app.get("/ship/contacts/{contact_id}", response_class=HTMLResponse)
async def contact_detail(request: Request, contact_id: int):
    ship_id = get_current_ship_id(request)
    async with aiosqlite.connect(DATABASE_URL) as db:
        db.row_factory = aiosqlite.Row
        ship = await _fetch_ship(db, ship_id)
        cursor = await db.execute("SELECT * FROM contacts WHERE id = ?", (contact_id,))
        contact = await cursor.fetchone()
        if contact is None:
            raise HTTPException(status_code=404, detail="Contact not found")
        contact = dict(contact)
        name_filter = contact.get("company") or contact.get("contact_name") or ""
        cursor = await db.execute(
            "SELECT * FROM expenses WHERE ship_id = ? AND supplier = ? ORDER BY date DESC",
            (ship_id, name_filter),
        )
        purchases = await cursor.fetchall()
    return templates.TemplateResponse(
        "ship/contacts_detail.html",
        {
            "request": request, "active_section": "ship",
            "current_ship": dict(ship) if ship else None,
            "contact": contact,
            "purchases": [dict(p) for p in purchases],
        },
    )


# ── Cruises ───────────────────────────────────────────────────────────────────

@app.get("/cruises", response_class=HTMLResponse)
async def cruises_index(request: Request):
    return RedirectResponse(url="/cruises/current", status_code=302)


@app.get("/cruises/current", response_class=HTMLResponse)
async def current_cruise(request: Request):
    async with aiosqlite.connect(DATABASE_URL) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute(
            "SELECT * FROM cruises ORDER BY COALESCE(start_time, created_at) DESC LIMIT 1"
        )
        cruise = await cursor.fetchone()
        if cruise is None:
            return templates.TemplateResponse(
                "cruises/detail.html",
                {"request": request, "active_section": "cruises",
                 "cruise": None, "routes": [],
                 "prev_cruise_id": None, "next_cruise_id": None},
            )
        cruise_id = cruise["id"]
        cursor = await db.execute(
            """SELECT r.*,
                      (SELECT COUNT(*) FROM logbook_lines l WHERE l.route_id = r.id) AS line_count
               FROM routes r WHERE r.cruise_id = ?
               ORDER BY r.id ASC""",
            (cruise_id,),
        )
        routes = await cursor.fetchall()
        cursor = await db.execute(
            "SELECT id FROM cruises WHERE id < ? ORDER BY id DESC LIMIT 1", (cruise_id,)
        )
        prev_cruise = await cursor.fetchone()
        cursor = await db.execute(
            "SELECT id FROM cruises WHERE id > ? ORDER BY id ASC LIMIT 1", (cruise_id,)
        )
        next_cruise = await cursor.fetchone()
        cursor = await db.execute(
            """SELECT s.* FROM stopovers s
               JOIN routes r ON s.route_id = r.id
               WHERE r.cruise_id = ?
               ORDER BY COALESCE(s.arrival_date, '') ASC""",
            (cruise_id,),
        )
        stopovers = await cursor.fetchall()
    return templates.TemplateResponse(
        "cruises/detail.html",
        {
            "request": request,
            "active_section": "cruises",
            "is_current": True,
            "cruise": dict(cruise),
            "routes": [dict(r) for r in routes],
            "stopovers": [dict(s) for s in stopovers],
            "prev_cruise_id": prev_cruise["id"] if prev_cruise else None,
            "next_cruise_id": next_cruise["id"] if next_cruise else None,
        },
    )


@app.get("/cruises/list", response_class=HTMLResponse)
async def cruise_list(request: Request):
    async with aiosqlite.connect(DATABASE_URL) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute(
            "SELECT * FROM cruises ORDER BY id ASC"
        )
        cruises = await cursor.fetchall()
        current_cursor = await db.execute(
            "SELECT id FROM cruises ORDER BY COALESCE(start_time, created_at) DESC LIMIT 1"
        )
        latest = await current_cursor.fetchone()
    return templates.TemplateResponse(
        "cruises/list.html",
        {
            "request": request,
            "active_section": "cruises",
            "cruises": [dict(c) for c in cruises],
            "current_cruise_id": latest["id"] if latest else None,
        },
    )


@app.get("/cruises/stopovers", response_class=HTMLResponse)
async def all_stopovers(request: Request):
    async with aiosqlite.connect(DATABASE_URL) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute(
            """SELECT s.*,
                      CAST(
                          CASE WHEN s.arrival_date IS NOT NULL AND s.departure_date IS NOT NULL
                          THEN julianday(s.departure_date) - julianday(s.arrival_date)
                          ELSE NULL END AS INTEGER
                      ) AS nights,
                      r.departure_location, r.destination_location,
                      c.name AS cruise_name, c.id AS cruise_id
               FROM stopovers s
               LEFT JOIN routes r ON s.route_id = r.id
               LEFT JOIN cruises c ON r.cruise_id = c.id
               ORDER BY s.arrival_date DESC"""
        )
        stopovers = await cursor.fetchall()
    return templates.TemplateResponse(
        "cruises/stopovers.html",
        {
            "request": request,
            "active_section": "cruises",
            "stopovers": [dict(s) for s in stopovers],
        },
    )


@app.get("/cruises/new", response_class=HTMLResponse)
async def new_cruise_form(request: Request):
    return templates.TemplateResponse(
        "cruises/new.html",
        {"request": request, "active_section": "cruises"},
    )


@app.get("/cruises/{cruise_id}", response_class=HTMLResponse)
async def cruise_detail(request: Request, cruise_id: int):
    async with aiosqlite.connect(DATABASE_URL) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute("SELECT * FROM cruises WHERE id = ?", (cruise_id,))
        cruise = await cursor.fetchone()
        if cruise is None:
            raise HTTPException(status_code=404, detail="Cruise not found")
        cursor = await db.execute(
            """SELECT r.*,
                      (SELECT COUNT(*) FROM logbook_lines l WHERE l.route_id = r.id) AS line_count
               FROM routes r
               WHERE r.cruise_id = ?
               ORDER BY r.id ASC""",
            (cruise_id,),
        )
        routes = await cursor.fetchall()
        cursor = await db.execute(
            "SELECT id FROM cruises WHERE id < ? ORDER BY id DESC LIMIT 1", (cruise_id,)
        )
        prev_cruise = await cursor.fetchone()
        cursor = await db.execute(
            "SELECT id FROM cruises WHERE id > ? ORDER BY id ASC LIMIT 1", (cruise_id,)
        )
        next_cruise = await cursor.fetchone()
        cursor = await db.execute(
            """SELECT s.* FROM stopovers s
               JOIN routes r ON s.route_id = r.id
               WHERE r.cruise_id = ?
               ORDER BY COALESCE(s.arrival_date, '') ASC""",
            (cruise_id,),
        )
        stopovers = await cursor.fetchall()
    return templates.TemplateResponse(
        "cruises/detail.html",
        {
            "request": request,
            "active_section": "cruises",
            "cruise": dict(cruise),
            "routes": [dict(r) for r in routes],
            "stopovers": [dict(s) for s in stopovers],
            "prev_cruise_id": prev_cruise["id"] if prev_cruise else None,
            "next_cruise_id": next_cruise["id"] if next_cruise else None,
        },
    )


@app.post("/cruises/{cruise_id}/arrival")
async def cruise_arrival(cruise_id: int):
    now = datetime.now().strftime("%Y-%m-%dT%H:%M:%S")
    async with aiosqlite.connect(DATABASE_URL) as db:
        await db.execute(
            "UPDATE cruises SET end_time = ? WHERE id = ?",
            (now, cruise_id),
        )
        await db.commit()
    return RedirectResponse(url=f"/cruises/{cruise_id}", status_code=303)


@app.post("/cruises/{cruise_id}/set-end")
async def cruise_set_end(cruise_id: int, end_time: Optional[str] = Form(None)):
    async with aiosqlite.connect(DATABASE_URL) as db:
        await db.execute(
            "UPDATE cruises SET end_time = ? WHERE id = ?",
            (end_time or None, cruise_id),
        )
        await db.commit()
    return RedirectResponse(url=f"/cruises/{cruise_id}", status_code=303)


@app.post("/cruises/{cruise_id}/delete")
async def delete_cruise(cruise_id: int):
    async with aiosqlite.connect(DATABASE_URL) as db:
        await db.execute("DELETE FROM cruises WHERE id = ?", (cruise_id,))
        await db.commit()
    return RedirectResponse(url="/cruises/list", status_code=303)


@app.post("/cruises/new")
async def create_cruise(
    name: str = Form(...),
    departure: Optional[str] = Form(None),
    destination: Optional[str] = Form(None),
    start_time: Optional[str] = Form(None),
    end_time: Optional[str] = Form(None),
):
    async with aiosqlite.connect(DATABASE_URL) as db:
        await db.execute(
            "INSERT INTO cruises (name, departure, destination, start_time, end_time) VALUES (?, ?, ?, ?, ?)",
            (name, departure or None, destination or None, start_time or None, end_time or None),
        )
        await db.commit()
    return RedirectResponse(url="/cruises/list", status_code=303)


# ── Stopovers ────────────────────────────────────────────────────────────────

@app.get("/routes/{route_id}/stopovers/new", response_class=HTMLResponse)
async def new_stopover_form(request: Request, route_id: int):
    async with aiosqlite.connect(DATABASE_URL) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute(
            """SELECT r.*, c.name AS cruise_name
               FROM routes r LEFT JOIN cruises c ON r.cruise_id = c.id
               WHERE r.id = ?""",
            (route_id,),
        )
        route = await cursor.fetchone()
    if route is None:
        raise HTTPException(status_code=404, detail="Route not found")
    return templates.TemplateResponse(
        "routes/stopover_new.html",
        {"request": request, "active_section": "routes", "route": dict(route)},
    )


@app.post("/routes/{route_id}/stopovers/new")
async def create_stopover(
    route_id: int,
    locality: Optional[str] = Form(None),
    name: Optional[str] = Form(None),
    type: Optional[str] = Form(None),
    arrival_date: Optional[str] = Form(None),
    departure_date: Optional[str] = Form(None),
    cost_per_night: Optional[float] = Form(None),
    cost: Optional[float] = Form(None),
    notes: Optional[str] = Form(None),
):
    # Auto-calculate total if only nightly cost and dates are given
    if cost is None and cost_per_night is not None and arrival_date and departure_date:
        from datetime import date
        try:
            nights = (date.fromisoformat(departure_date) - date.fromisoformat(arrival_date)).days
            if nights > 0:
                cost = cost_per_night * nights
        except ValueError:
            pass
    async with aiosqlite.connect(DATABASE_URL) as db:
        await db.execute(
            """INSERT INTO stopovers (route_id, locality, name, type, arrival_date, departure_date, cost_per_night, cost, notes)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (route_id, locality or None, name or None, type or None,
             arrival_date or None, departure_date or None,
             cost_per_night, cost if cost is not None else 0, notes or None),
        )
        await db.commit()
    return RedirectResponse(url=f"/routes/{route_id}", status_code=303)


@app.get("/stopovers/{stopover_id}/edit", response_class=HTMLResponse)
async def edit_stopover_form(request: Request, stopover_id: int):
    async with aiosqlite.connect(DATABASE_URL) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute(
            """SELECT s.*, r.departure_location, r.destination_location, c.name AS cruise_name
               FROM stopovers s
               LEFT JOIN routes r ON s.route_id = r.id
               LEFT JOIN cruises c ON r.cruise_id = c.id
               WHERE s.id = ?""",
            (stopover_id,),
        )
        stopover = await cursor.fetchone()
    if stopover is None:
        raise HTTPException(status_code=404, detail="Stopover not found")
    return templates.TemplateResponse(
        "routes/stopover_edit.html",
        {"request": request, "active_section": "routes", "stopover": dict(stopover)},
    )


@app.post("/stopovers/{stopover_id}/edit")
async def update_stopover(
    stopover_id: int,
    locality: Optional[str] = Form(None),
    name: Optional[str] = Form(None),
    type: Optional[str] = Form(None),
    arrival_date: Optional[str] = Form(None),
    departure_date: Optional[str] = Form(None),
    cost_per_night: Optional[float] = Form(None),
    cost: Optional[float] = Form(None),
    notes: Optional[str] = Form(None),
):
    if cost is None and cost_per_night is not None and arrival_date and departure_date:
        from datetime import date
        try:
            nights = (date.fromisoformat(departure_date) - date.fromisoformat(arrival_date)).days
            if nights > 0:
                cost = cost_per_night * nights
        except ValueError:
            pass
    async with aiosqlite.connect(DATABASE_URL) as db:
        cursor = await db.execute("SELECT route_id FROM stopovers WHERE id = ?", (stopover_id,))
        row = await cursor.fetchone()
        route_id = row[0] if row else None
        await db.execute(
            """UPDATE stopovers SET locality=?, name=?, type=?, arrival_date=?, departure_date=?,
               cost_per_night=?, cost=?, notes=? WHERE id=?""",
            (locality or None, name or None, type or None,
             arrival_date or None, departure_date or None,
             cost_per_night, cost if cost is not None else 0, notes or None, stopover_id),
        )
        await db.commit()
    if route_id:
        return RedirectResponse(url=f"/routes/{route_id}", status_code=303)
    return RedirectResponse(url="/cruises/stopovers", status_code=303)


@app.post("/stopovers/{stopover_id}/delete")
async def delete_stopover(stopover_id: int):
    async with aiosqlite.connect(DATABASE_URL) as db:
        cursor = await db.execute("SELECT route_id FROM stopovers WHERE id = ?", (stopover_id,))
        row = await cursor.fetchone()
        route_id = row[0] if row else None
        await db.execute("DELETE FROM stopovers WHERE id = ?", (stopover_id,))
        await db.commit()
    if route_id:
        return RedirectResponse(url=f"/routes/{route_id}", status_code=303)
    return RedirectResponse(url="/cruises/stopovers", status_code=303)


# ── Routes (logbook legs) ─────────────────────────────────────────────────────

@app.get("/routes/current", response_class=HTMLResponse)
async def current_route(request: Request):
    async with aiosqlite.connect(DATABASE_URL) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute(
            """SELECT r.id FROM routes r
               WHERE r.cruise_id = (
                   SELECT id FROM cruises ORDER BY COALESCE(start_time, created_at) DESC LIMIT 1
               )
               ORDER BY r.id DESC LIMIT 1"""
        )
        latest = await cursor.fetchone()
    if latest:
        return RedirectResponse(url=f"/routes/{latest['id']}", status_code=302)
    return RedirectResponse(url="/cruises/current", status_code=302)


@app.post("/routes/{route_id}/arrivee")
async def route_arrivee(
    route_id: int,
    destination_location: Optional[str] = Form(None),
    motor_hours_end: Optional[float] = Form(None),
):
    now = datetime.now().strftime("%Y-%m-%dT%H:%M:%S")
    async with aiosqlite.connect(DATABASE_URL) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute(
            "SELECT cruise_id, destination_location FROM routes WHERE id = ?", (route_id,)
        )
        route = await cursor.fetchone()
        dest = destination_location or None
        if route and route["destination_location"]:
            dest = route["destination_location"]
        await db.execute(
            "UPDATE routes SET end_time=?, finished=1, destination_location=COALESCE(?, destination_location), motor_hours_end=? WHERE id=?",
            (now, dest, motor_hours_end, route_id),
        )
        await db.commit()
    return RedirectResponse(url="/cruises/current", status_code=303)


@app.post("/routes/{route_id}/delete")
async def delete_route(route_id: int):
    async with aiosqlite.connect(DATABASE_URL) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute("SELECT cruise_id FROM routes WHERE id = ?", (route_id,))
        row = await cursor.fetchone()
        cruise_id = row["cruise_id"] if row else None
        await db.execute("DELETE FROM routes WHERE id = ?", (route_id,))
        await db.commit()
    if cruise_id:
        return RedirectResponse(url=f"/cruises/{cruise_id}", status_code=303)
    return RedirectResponse(url="/cruises/current", status_code=303)


@app.get("/routes", response_class=HTMLResponse)
async def routes_index(request: Request):
    async with aiosqlite.connect(DATABASE_URL) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute(
            "SELECT id FROM routes ORDER BY COALESCE(start_time, created_at) DESC LIMIT 1"
        )
        latest = await cursor.fetchone()
    if latest:
        return RedirectResponse(url=f"/routes/{latest['id']}", status_code=302)
    return templates.TemplateResponse(
        "routes/list.html",
        {"request": request, "active_section": "routes"},
    )


@app.get("/routes/new", response_class=HTMLResponse)
async def new_route_form(request: Request, cruise_id: int = Query(...)):
    async with aiosqlite.connect(DATABASE_URL) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute("SELECT * FROM cruises WHERE id = ?", (cruise_id,))
        cruise = await cursor.fetchone()
    if cruise is None:
        raise HTTPException(status_code=404, detail="Cruise not found")
    return templates.TemplateResponse(
        "new_route.html",
        {"request": request, "cruise": dict(cruise), "active_section": "routes"},
    )


@app.post("/routes/new")
async def create_route(
    cruise_id: int = Form(...),
    departure_location: Optional[str] = Form(None),
    destination_location: Optional[str] = Form(None),
    name: Optional[str] = Form(None),
    start_time: Optional[str] = Form(None),
    notes: Optional[str] = Form(None),
    motor_hours_start: Optional[float] = Form(None),
):
    async with aiosqlite.connect(DATABASE_URL) as db:
        await db.execute(
            """INSERT INTO routes (cruise_id, name, departure_location, destination_location, start_time, notes, motor_hours_start)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (cruise_id, name or None, departure_location or None, destination_location or None,
             start_time or None, notes or None, motor_hours_start),
        )
        await db.commit()
    return RedirectResponse(url="/cruises/current", status_code=303)


@app.get("/routes/{route_id}", response_class=HTMLResponse)
async def route_detail(request: Request, route_id: int):
    async with aiosqlite.connect(DATABASE_URL) as db:
        db.row_factory = aiosqlite.Row

        cursor = await db.execute(
            """SELECT r.*, c.name AS cruise_name
               FROM routes r LEFT JOIN cruises c ON r.cruise_id = c.id
               WHERE r.id = ?""",
            (route_id,),
        )
        route = await cursor.fetchone()
        if route is None:
            raise HTTPException(status_code=404, detail="Route not found")

        cursor = await db.execute(
            "SELECT * FROM logbook_lines WHERE route_id = ? ORDER BY timestamp ASC",
            (route_id,),
        )
        lines = await cursor.fetchall()

        cursor = await db.execute(
            "SELECT * FROM stopovers WHERE route_id = ? ORDER BY arrival_date ASC",
            (route_id,),
        )
        stopovers = await cursor.fetchall()

        prev_route = None
        next_route = None
        if route["cruise_id"]:
            cursor = await db.execute(
                "SELECT id FROM routes WHERE cruise_id = ? AND id < ? ORDER BY id DESC LIMIT 1",
                (route["cruise_id"], route_id),
            )
            prev_route = await cursor.fetchone()
            cursor = await db.execute(
                "SELECT id FROM routes WHERE cruise_id = ? AND id > ? ORDER BY id ASC LIMIT 1",
                (route["cruise_id"], route_id),
            )
            next_route = await cursor.fetchone()

        ship_id = get_current_ship_id(request)
        cursor = await db.execute(
            "SELECT * FROM todo_items WHERE ship_id = ? AND status != 'Terminé' ORDER BY urgent DESC, id ASC",
            (ship_id,),
        )
        todos = await cursor.fetchall()

    return templates.TemplateResponse(
        "routes/detail.html",
        {
            "request": request,
            "active_section": "routes",
            "route": dict(route),
            "lines": [dict(ln) for ln in lines],
            "stopovers": [dict(s) for s in stopovers],
            "prev_route": dict(prev_route) if prev_route else None,
            "next_route": dict(next_route) if next_route else None,
            "todos": [dict(t) for t in todos],
        },
    )


@app.get("/routes/{route_id}/new-line", response_class=HTMLResponse)
async def new_line_form(request: Request, route_id: int):
    async with aiosqlite.connect(DATABASE_URL) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute(
            """SELECT r.*, c.name AS cruise_name
               FROM routes r LEFT JOIN cruises c ON r.cruise_id = c.id
               WHERE r.id = ?""",
            (route_id,),
        )
        route = await cursor.fetchone()
    if route is None:
        raise HTTPException(status_code=404, detail="Route not found")
    data = get_sensor_data()
    return templates.TemplateResponse(
        "routes/new_line.html",
        {"request": request, "active_section": "routes", "data": data, "route": dict(route)},
    )


@app.post("/routes/{route_id}/new-line")
async def create_line(
    request: Request,
    route_id: int,
    position_lat: Optional[float] = Form(None),
    position_lon: Optional[float] = Form(None),
    aws: Optional[float] = Form(None),
    stw: Optional[float] = Form(None),
    sog: Optional[float] = Form(None),
    awa: Optional[float] = Form(None),
    tws: Optional[float] = Form(None),
    twa: Optional[float] = Form(None),
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
    pressure: Optional[float] = Form(None),
    visual_pos: Optional[str] = Form(None),
    notes: Optional[str] = Form(None),
):
    if depth is not None:
        depth = round(depth, 1)
    async with aiosqlite.connect(DATABASE_URL) as db:
        await db.execute(
            """INSERT INTO logbook_lines
               (timestamp, route_id, aws, awa, water_temp, heading, cog, log, trip, depth,
                position_lat, position_lon, stw, sog, tws, twa, pressure,
                sea_state, visibility, sails, points_of_sail, visual_pos, notes)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                datetime.now(), route_id, aws, awa, water_temp, heading, cog, log, trip, depth,
                position_lat, position_lon, stw, sog, tws, twa, pressure,
                sea_state, visibility, sails, points_of_sail, visual_pos, notes,
            ),
        )
        await db.commit()
    return RedirectResponse(url=f"/routes/{route_id}", status_code=303)


# ── Map API ───────────────────────────────────────────────────────────────────

ROUTE_COLORS  = ["#2b79c6", "#e74c3c", "#27ae60", "#8e44ad", "#e67e22", "#16a085", "#c0392b", "#2980b9"]
CRUISE_COLORS = ["#e74c3c", "#27ae60", "#8e44ad", "#e67e22", "#16a085", "#2b79c6", "#c0392b", "#f39c12"]


@app.get("/api/all-cruises/map-data")
async def all_cruises_map_data():
    from fastapi.responses import JSONResponse
    async with aiosqlite.connect(DATABASE_URL) as db:
        db.row_factory = aiosqlite.Row

        cursor = await db.execute(
            "SELECT id, name FROM cruises ORDER BY COALESCE(start_time, created_at) ASC"
        )
        cruises_meta = await cursor.fetchall()

        cruises = []
        for i, c in enumerate(cruises_meta):
            cursor = await db.execute(
                """SELECT tp.lat, tp.lon
                   FROM track_points tp
                   JOIN routes r ON tp.route_id = r.id
                   WHERE r.cruise_id = ?
                   ORDER BY tp.timestamp ASC""",
                (c["id"],),
            )
            track_pts = [{"lat": p["lat"], "lon": p["lon"]} for p in await cursor.fetchall()]

            cursor = await db.execute(
                """SELECT l.position_lat AS lat, l.position_lon AS lon
                   FROM logbook_lines l
                   JOIN routes r ON l.route_id = r.id
                   WHERE r.cruise_id = ? AND l.position_lat IS NOT NULL AND l.position_lon IS NOT NULL
                   ORDER BY l.timestamp ASC""",
                (c["id"],),
            )
            log_pts = [{"lat": p["lat"], "lon": p["lon"]} for p in await cursor.fetchall()]

            cruises.append({
                "id": c["id"],
                "name": c["name"] or f"Croisière #{c['id']}",
                "color": CRUISE_COLORS[i % len(CRUISE_COLORS)],
                "track_points": track_pts,
                "logbook_points": log_pts,
            })

    return JSONResponse({"cruises": cruises})


@app.get("/api/cruises/{cruise_id}/map-data")
async def cruise_map_data(cruise_id: int):
    from fastapi.responses import JSONResponse
    async with aiosqlite.connect(DATABASE_URL) as db:
        db.row_factory = aiosqlite.Row

        cursor = await db.execute(
            "SELECT id FROM cruises WHERE id = ?", (cruise_id,)
        )
        if await cursor.fetchone() is None:
            raise HTTPException(status_code=404, detail="Cruise not found")

        cursor = await db.execute(
            "SELECT id, departure_location, destination_location FROM routes WHERE cruise_id = ? ORDER BY id ASC",
            (cruise_id,),
        )
        routes_meta = await cursor.fetchall()

        routes = []
        for i, r in enumerate(routes_meta):
            cursor = await db.execute(
                "SELECT lat, lon, timestamp FROM track_points WHERE route_id = ? ORDER BY timestamp ASC",
                (r["id"],),
            )
            track_pts = [{"lat": p["lat"], "lon": p["lon"]} for p in await cursor.fetchall()]

            cursor = await db.execute(
                """SELECT position_lat AS lat, position_lon AS lon, timestamp
                   FROM logbook_lines
                   WHERE route_id = ? AND position_lat IS NOT NULL AND position_lon IS NOT NULL
                   ORDER BY timestamp ASC""",
                (r["id"],),
            )
            log_pts = [{"lat": p["lat"], "lon": p["lon"]} for p in await cursor.fetchall()]

            name = f"{r['departure_location'] or '?'} → {r['destination_location'] or '?'}"
            routes.append({
                "id": r["id"],
                "name": name,
                "color": ROUTE_COLORS[i % len(ROUTE_COLORS)],
                "track_points": track_pts,
                "logbook_points": log_pts,
            })

    return JSONResponse({"routes": routes})


@app.get("/api/routes/{route_id}/map-data")
async def route_map_data(route_id: int):
    from fastapi.responses import JSONResponse
    async with aiosqlite.connect(DATABASE_URL) as db:
        db.row_factory = aiosqlite.Row

        cursor = await db.execute(
            "SELECT lat, lon, timestamp FROM track_points WHERE route_id = ? ORDER BY timestamp ASC",
            (route_id,),
        )
        track_points = [{"lat": r["lat"], "lon": r["lon"], "timestamp": r["timestamp"]} for r in await cursor.fetchall()]

        cursor = await db.execute(
            """SELECT position_lat AS lat, position_lon AS lon, timestamp
               FROM logbook_lines
               WHERE route_id = ? AND position_lat IS NOT NULL AND position_lon IS NOT NULL
               ORDER BY timestamp ASC""",
            (route_id,),
        )
        logbook_points = [{"lat": r["lat"], "lon": r["lon"], "timestamp": r["timestamp"]} for r in await cursor.fetchall()]

    return JSONResponse({"track_points": track_points, "logbook_points": logbook_points})


# ── Tools ─────────────────────────────────────────────────────────────────────

@app.get("/tools", response_class=HTMLResponse)
async def tools_index(request: Request):
    return RedirectResponse(url="/tools/weather", status_code=302)


@app.get("/tools/weather", response_class=HTMLResponse)
async def tools_weather(request: Request):
    async with aiosqlite.connect(DATABASE_URL) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute(
            """SELECT position_lat, position_lon FROM logbook_lines
               WHERE position_lat IS NOT NULL AND position_lon IS NOT NULL
               ORDER BY timestamp DESC LIMIT 1"""
        )
        last_pos = await cursor.fetchone()
    lat = last_pos["position_lat"] if last_pos else 43.3
    lon = last_pos["position_lon"] if last_pos else 5.4
    return templates.TemplateResponse(
        "tools/weather.html",
        {"request": request, "active_section": "tools", "lat": lat, "lon": lon},
    )


@app.get("/tools/chart", response_class=HTMLResponse)
async def tools_chart(request: Request):
    return templates.TemplateResponse(
        "tools/chart.html",
        {"request": request, "active_section": "tools"},
    )


# ── Gallery ───────────────────────────────────────────────────────────────────

@app.get("/gallery", response_class=HTMLResponse)
async def gallery(request: Request):
    async with aiosqlite.connect(DATABASE_URL) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute("SELECT * FROM trip_photos ORDER BY created_at DESC")
        photos = await cursor.fetchall()
    return templates.TemplateResponse(
        "gallery/index.html",
        {"request": request, "active_section": "gallery", "photos": [dict(p) for p in photos]},
    )


@app.get("/logbook/{line_id}", response_class=HTMLResponse)
async def view_line(request: Request, line_id: int):
    async with aiosqlite.connect(DATABASE_URL) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute("SELECT * FROM logbook_lines WHERE id = ?", (line_id,))
        line = await cursor.fetchone()
        if line is None:
            raise HTTPException(status_code=404, detail="Entry not found")
        cursor = await db.execute(
            "SELECT * FROM trip_photos WHERE trip_id = ? ORDER BY created_at DESC", (line_id,)
        )
        photos = await cursor.fetchall()
    return templates.TemplateResponse(
        "line_detail.html",
        {"request": request, "line": line, "photos": photos, "active_section": "routes"},
    )


@app.get("/logbook/{line_id}/add-photo", response_class=HTMLResponse)
async def add_photo_form(request: Request, line_id: int):
    async with aiosqlite.connect(DATABASE_URL) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute("SELECT * FROM logbook_lines WHERE id = ?", (line_id,))
        line = await cursor.fetchone()
        if line is None:
            raise HTTPException(status_code=404, detail="Entry not found")
    return templates.TemplateResponse(
        "add_photo.html",
        {"request": request, "line": line, "active_section": "routes"},
    )


@app.post("/logbook/{line_id}/add-photo")
async def add_photo(
    request: Request,
    line_id: int,
    photo_path: str = Form(...),
    comment: Optional[str] = Form(None),
    added_by: Optional[str] = Form(None),
):
    async with aiosqlite.connect(DATABASE_URL) as db:
        cursor = await db.execute("SELECT id FROM logbook_lines WHERE id = ?", (line_id,))
        if await cursor.fetchone() is None:
            raise HTTPException(status_code=404, detail="Entry not found")
        await db.execute(
            "INSERT INTO trip_photos (trip_id, photo_path, comment, added_by) VALUES (?, ?, ?, ?)",
            (line_id, photo_path, comment, added_by),
        )
        await db.commit()
    return RedirectResponse(url=f"/logbook/{line_id}", status_code=303)


# ── Setup (first run) ─────────────────────────────────────────────────────────

@app.get("/setup", response_class=HTMLResponse)
async def setup_form(request: Request):
    return templates.TemplateResponse("setup.html", {"request": request})


@app.post("/setup")
async def setup_save(ikommunicate_url: str = Form(...)):
    save_config({"ikommunicate_url": ikommunicate_url.strip()})
    return RedirectResponse(url="/", status_code=303)


# ── Settings ──────────────────────────────────────────────────────────────────

@app.get("/settings", response_class=HTMLResponse)
async def settings_form(request: Request):
    async with aiosqlite.connect(DATABASE_URL) as db:
        db.row_factory = aiosqlite.Row
        ship = await _fetch_ship(db, get_current_ship_id(request))
    return templates.TemplateResponse(
        "settings.html",
        {
            "request": request,
            "active_section": "settings",
            "current_ship": dict(ship) if ship else None,
            "ikommunicate_host": get_ikommunicate_host() or "",
        },
    )


@app.post("/settings")
async def settings_save(
    request: Request,
    ikommunicate_url: Optional[str] = Form(None),
):
    save_config({"ikommunicate_url": (ikommunicate_url or "").strip()})
    return RedirectResponse(url="/settings", status_code=303)


# Run with: uvicorn main:app --reload --host 0.0.0.0 --port 8000
