# main.py
from fastapi import FastAPI, HTTPException, Request, Form, Query
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from datetime import datetime
import aiosqlite
from contextlib import asynccontextmanager
from typing import Optional
from utils import get_sensor_data

DATABASE_URL = "logbook.db"
templates = Jinja2Templates(directory="templates")


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
                photo_path TEXT NOT NULL,
                comment TEXT,
                added_by TEXT,
                created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                cruise_id INTEGER,
                FOREIGN KEY (cruise_id) REFERENCES cruises(id)
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
                task TEXT NOT NULL,
                urgent BOOLEAN DEFAULT 0,
                status TEXT DEFAULT 'A faire',
                due_date TEXT,
                completed_at TEXT,
                tags TEXT,
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

        # ── Migrations ────────────────────────────────────────────────────
        for stmt in [
            "ALTER TABLE todo_items ADD COLUMN tags TEXT",
            "ALTER TABLE todo_items ADD COLUMN ship_id INTEGER NOT NULL DEFAULT 1",
            "ALTER TABLE expenses ADD COLUMN ship_id INTEGER NOT NULL DEFAULT 1",
            "ALTER TABLE contacts ADD COLUMN ship_id INTEGER NOT NULL DEFAULT 1",
            "ALTER TABLE bosco_entries RENAME TO expenses",
        ]:
            try:
                await db.execute(stmt)
            except Exception:
                pass

        await db.commit()


@asynccontextmanager
async def lifespan(app: FastAPI):
    await init_db()
    print("Database initialized")
    yield


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
    task: str = Form(...),
    urgent: Optional[str] = Form(None),
    due_date: Optional[str] = Form(None),
    tags: Optional[str] = Form(None),
):
    ship_id = get_current_ship_id(request)
    async with aiosqlite.connect(DATABASE_URL) as db:
        await db.execute(
            "INSERT INTO todo_items (ship_id, task, urgent, due_date, tags) VALUES (?, ?, ?, ?, ?)",
            (ship_id, task, 1 if urgent else 0, due_date or None, tags or None),
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
    task: str = Form(...),
    urgent: Optional[str] = Form(None),
    status: Optional[str] = Form(None),
    due_date: Optional[str] = Form(None),
    completed_at: Optional[str] = Form(None),
    tags: Optional[str] = Form(None),
):
    async with aiosqlite.connect(DATABASE_URL) as db:
        await db.execute(
            """UPDATE todo_items SET task=?, urgent=?, status=?, due_date=?, completed_at=?, tags=?
               WHERE id=?""",
            (task, 1 if urgent else 0, status or 'A faire', due_date or None,
             completed_at or None, tags or None, item_id),
        )
        await db.commit()
    return RedirectResponse(url="/ship/todo", status_code=303)


@app.post("/ship/todo/{item_id}/done")
async def mark_todo_done(item_id: int):
    today = datetime.utcnow().strftime("%Y-%m-%d")
    async with aiosqlite.connect(DATABASE_URL) as db:
        await db.execute(
            "UPDATE todo_items SET status='Terminé', completed_at=? WHERE id=? AND status != 'Terminé'",
            (today, item_id),
        )
        await db.commit()
    return RedirectResponse(url="/ship/todo", status_code=303)


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
        routes = []
        if cruise:
            cursor = await db.execute(
                """SELECT r.*,
                          (SELECT COUNT(*) FROM logbook_lines l WHERE l.route_id = r.id) AS line_count
                   FROM routes r
                   WHERE r.cruise_id = ?
                   ORDER BY COALESCE(r.start_time, r.created_at) ASC""",
                (cruise["id"],),
            )
            routes = await cursor.fetchall()
    return templates.TemplateResponse(
        "cruises/current.html",
        {
            "request": request,
            "active_section": "cruises",
            "cruise": dict(cruise) if cruise else None,
            "routes": [dict(r) for r in routes],
        },
    )


@app.get("/cruises/list", response_class=HTMLResponse)
async def cruise_list(request: Request):
    async with aiosqlite.connect(DATABASE_URL) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute(
            "SELECT * FROM cruises ORDER BY COALESCE(start_time, created_at) DESC"
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
                      ) AS nights
               FROM stopovers s
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


# ── Routes (logbook legs) ─────────────────────────────────────────────────────

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
):
    async with aiosqlite.connect(DATABASE_URL) as db:
        await db.execute(
            """INSERT INTO routes (cruise_id, name, departure_location, destination_location, start_time, notes)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (cruise_id, name or None, departure_location or None, destination_location or None,
             start_time or None, notes or None),
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
    async with aiosqlite.connect(DATABASE_URL) as db:
        await db.execute(
            """INSERT INTO logbook_lines
               (timestamp, route_id, aws, awa, water_temp, heading, cog, log, trip, depth,
                position_lat, position_lon, stw, sog, tws, twa, pressure,
                sea_state, visibility, sails, points_of_sail, visual_pos, notes)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                datetime.utcnow(), route_id, aws, awa, water_temp, heading, cog, log, trip, depth,
                position_lat, position_lon, stw, sog, tws, twa, pressure,
                sea_state, visibility, sails, points_of_sail, visual_pos, notes,
            ),
        )
        await db.commit()
    return RedirectResponse(url=f"/routes/{route_id}", status_code=303)


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


# Run with: uvicorn main:app --reload --host 0.0.0.0 --port 8000
