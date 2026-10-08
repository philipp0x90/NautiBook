# main.py
from fastapi import FastAPI, HTTPException, Request, Form, Query, File, UploadFile
from fastapi.responses import HTMLResponse, RedirectResponse, StreamingResponse, Response, FileResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from datetime import datetime, date, timedelta
import asyncio
import os
import hashlib
import secrets
import time
import csv
import io
import json
import re
import unicodedata
import subprocess
import aiosqlite
import requests
from contextlib import asynccontextmanager
from pathlib import Path
from typing import List, Optional
from urllib.parse import quote
from utils import get_sensor_data, get_position
import import_filemaker
import passwords
from config import get_ikommunicate_url, get_ikommunicate_host, save_config, is_configured

# NAUTIBOOK_DB : une autre base que logbook.db, pour regarder une copie dans
# l'app sans toucher la vraie (essai d'un import, par exemple) :
#   NAUTIBOOK_DB=/tmp/copie.db uvicorn main:app --port 8001
DATABASE_URL = os.environ.get("NAUTIBOOK_DB", "logbook.db")
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


JOURS_FR = ["lundi", "mardi", "mercredi", "jeudi", "vendredi", "samedi", "dimanche"]


def _jourfr(value):
    """ISO date (or datetime) → French weekday name: 2026-08-26 → mercredi.
    Hardcoded rather than locale-driven: strftime('%A') would depend on the
    Raspberry Pi's locale being installed and set, which it need not be."""
    if not value:
        return "—"
    try:
        return JOURS_FR[date.fromisoformat(str(value)[:10]).weekday()]
    except ValueError:
        return "—"


def _age(value):
    """Birth date → age in whole years, as of today.

    `crew_members.age` exists in the schema (a FileMaker leftover) but nothing
    writes it, and nothing should: a stored age is wrong within the year. The
    month/day comparison is what makes this a real age rather than a subtraction
    of years — someone born in December is not yet a year older in January.
    """
    if not value:
        return "—"
    try:
        born = date.fromisoformat(str(value)[:10])
    except ValueError:
        return "—"
    today = date.today()
    years = today.year - born.year - ((today.month, today.day) < (born.month, born.day))
    return years if years >= 0 else "—"


def _deg(value):
    """Angles are whole degrees, but REAL columns hand them back as floats (47.0)."""
    if value is None or value == "":
        return "—"
    try:
        return f"{round(float(value))}°"
    except (TypeError, ValueError):
        return value


def _hpa(value):
    """Pressure is whole hPa, but REAL columns hand it back as a float (1030.0).
    Same reason `deg` exists; `unit('hPa')` would print the trailing .0."""
    if value is None or value == "":
        return "—"
    try:
        return f"{round(float(value))} hPa"
    except (TypeError, ValueError):
        return value


def _unit(value, suffix):
    """Append a unit to a measurement. Falsy values show an em-dash, so a
    recorded 0 reads as "no value" — same as before this filter existed."""
    if not value:
        return "—"
    return f"{value} {suffix}"


def _dmm(value, hemispheres, deg_width):
    """Decimal degrees → degrees and decimal minutes, the format used on
    charts and plotters: 43.2891 → 43° 17.346' N. Positions are stored as
    signed DD, so the hemisphere comes from the sign."""
    if value is None or value == "":
        return "—"
    try:
        dd = float(value)
    except (TypeError, ValueError):
        return value
    hemisphere = hemispheres[0] if dd >= 0 else hemispheres[1]
    degrees, minutes = divmod(abs(dd) * 60, 60)
    if round(minutes, 3) >= 60:  # 59.9996' rounds up into the next degree
        degrees, minutes = degrees + 1, 0.0
    return f"{int(degrees):0{deg_width}d}° {minutes:06.3f}' {hemisphere}"


def _lat(value):
    """Latitude in DMM, two degree digits: 43° 17.346' N"""
    return _dmm(value, "NS", 2)


def _lon(value):
    """Longitude in DMM, two degree digits like the latitude: 05° 24.000' E.
    The width is a minimum, so a longitude past 100° keeps its three digits."""
    return _dmm(value, "EW", 2)


def _dmm_parts(value, hemispheres):
    """Split stored decimal degrees into the three boxes of a position form:
    whole degrees, decimal minutes, hemisphere letter. Same split as _dmm,
    which renders it as one string for display. An absent position leaves the
    boxes empty rather than showing a spurious 0° 00.000'."""
    blank = {"deg": "", "min": "", "hem": hemispheres[0]}
    if value is None or value == "":
        return blank
    try:
        dd = float(value)
    except (TypeError, ValueError):
        return blank
    hemisphere = hemispheres[0] if dd >= 0 else hemispheres[1]
    degrees, minutes = divmod(abs(dd) * 60, 60)
    if round(minutes, 3) >= 60:  # 59.9996' rounds up into the next degree
        degrees, minutes = degrees + 1, 0.0
    return {"deg": int(degrees), "min": f"{minutes:.3f}", "hem": hemisphere}


def _lat_parts(value):
    return _dmm_parts(value, "NS")


def _lon_parts(value):
    return _dmm_parts(value, "EW")


def _dmm_to_dd(degrees, minutes, hemisphere):
    """The way back: the three form boxes → the signed decimal degrees the
    database stores. Empty boxes mean "no position", not zero, so both blank
    yields None. The sign comes from the hemisphere, so a negative typed into
    the degrees box is ignored rather than flipping it twice."""
    if degrees is None and minutes is None:
        return None
    dd = abs(degrees or 0) + abs(minutes or 0) / 60
    if (hemisphere or "").upper() in ("S", "W"):
        dd = -dd
    return round(dd, 6)


def _euros(value):
    """Montant en euros : 1100000 → « 1.100.000,00 € » ; vide → ''.

    Point entre les milliers, virgule décimale. Espace insécable avant
    l'euro, pour qu'un montant ne se coupe jamais en fin de ligne. Tout
    montant en euros de l'app passe par ce filtre, qui est seul à décider
    de ce format."""
    if value is None or value == "":
        return ""
    # Python sait mettre la virgule des milliers et le point décimal : on
    # échange les deux, en passant par un caractère intermédiaire.
    text = f"{float(value):,.2f}".replace(",", "\x00").replace(".", ",").replace("\x00", ".")
    return f"{text}\u00a0€"


templates.env.filters["datefr"] = _datefr
templates.env.filters["euros"] = _euros
templates.env.filters["jourfr"] = _jourfr
templates.env.filters["age"] = _age
templates.env.filters["deg"] = _deg
templates.env.filters["hpa"] = _hpa
templates.env.filters["unit"] = _unit
templates.env.filters["lat"] = _lat
templates.env.filters["lon"] = _lon
templates.env.filters["latdmm"] = _lat_parts
templates.env.filters["londmm"] = _lon_parts


@asynccontextmanager
async def connect():
    """Open the database with foreign keys enforced.

    SQLite defaults the pragma to OFF *per connection*, so a plain
    aiosqlite.connect() makes every ON DELETE CASCADE in the schema a no-op:
    deleting a cruise used to leave its routes, lines and track points behind
    as invisible orphans. Always go through this helper.
    """
    async with aiosqlite.connect(DATABASE_URL) as db:
        await db.execute("PRAGMA foreign_keys = ON;")
        yield db


async def init_db():
    async with connect() as db:

        # ── Core tables ──────────────────────────────────────────────────

        await db.execute("""
            CREATE TABLE IF NOT EXISTS cruises (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                ship_id INTEGER,
                name TEXT,
                departure TEXT,
                destination TEXT,
                start_time DATETIME,
                end_time DATETIME,
                loch_start REAL,
                loch_end REAL,
                created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (ship_id) REFERENCES ship_info(id) ON DELETE CASCADE
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
                beam REAL,
                draft REAL,
                air_draft REAL,
                mast_height REAL,
                clearance_no_mast REAL,
                freeboard REAL,
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
                engine_power REAL,
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
                description TEXT,
                document_path TEXT,
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

        # Carnet de voyage : des publications (un texte, des photos, ou les
        # deux), rangées par croisière. L'auteur est une fiche équipier et non
        # un compte : repasser quelqu'un Mousse supprime son compte, pas ce
        # qu'il a écrit (ON DELETE SET NULL, et l'auteur devient anonyme
        # seulement si sa fiche disparaît).
        await db.execute("""
            CREATE TABLE IF NOT EXISTS carnet_entries (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                ship_id INTEGER NOT NULL REFERENCES ship_info(id) ON DELETE CASCADE,
                cruise_id INTEGER REFERENCES cruises(id) ON DELETE CASCADE,
                crew_member_id INTEGER REFERENCES crew_members(id) ON DELETE SET NULL,
                text TEXT,
                created_at DATETIME,
                updated_at DATETIME
            )
        """)
        # Une photo d'une publication. lat / lon : la position de la prise de
        # vue lue dans la photo (geo_source « photo »), sinon celle du bateau
        # au moment de publier (« bateau »), sinon rien. taken_at : l'heure de
        # prise de vue, quand la photo la donne. place : le nom du lieu, cherché
        # après coup (_fill_carnet_places) — NULL pas encore cherché, '' rien trouvé.
        await db.execute("""
            CREATE TABLE IF NOT EXISTS carnet_photos (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                entry_id INTEGER NOT NULL REFERENCES carnet_entries(id) ON DELETE CASCADE,
                photo_path TEXT NOT NULL,
                lat REAL,
                lon REAL,
                geo_source TEXT,
                taken_at DATETIME,
                position INTEGER,
                place TEXT
            )
        """)

        # Comptes de connexion. Un compte appartient à une fiche équipier, une
        # fiche a au plus un compte ; un équipier sans compte est « Mousse »
        # (lecture seule), ce n'est pas un rang stocké. Le rang est en
        # français, comme le domaine — jamais affiché tel quel (RANK_LABELS).
        await db.execute("""
            CREATE TABLE IF NOT EXISTS users (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                crew_member_id INTEGER NOT NULL UNIQUE
                    REFERENCES crew_members(id) ON DELETE CASCADE,
                username TEXT NOT NULL UNIQUE COLLATE NOCASE,
                password_hash TEXT NOT NULL,
                rank TEXT NOT NULL CHECK (rank IN ('amiral', 'capitaine', 'matelot')),
                created_at DATETIME
            )
        """)
        # Un seul Amiral, garanti par la base et pas seulement par l'app : un
        # index unique *partiel*, qui ne porte que sur les lignes 'amiral'.
        await db.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS users_one_admiral ON users(rank) WHERE rank = 'amiral'"
        )
        # Sessions : le cookie porte un jeton aléatoire, la base n'en garde que
        # l'empreinte SHA-256 — une base copiée ne permet pas de se connecter.
        await db.execute("""
            CREATE TABLE IF NOT EXISTS sessions (
                token_hash TEXT PRIMARY KEY,
                user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                created_at DATETIME,
                expires_at DATETIME
            )
        """)
        # Secrets de l'app, hachés : pour l'instant le seul code de secours de
        # l'Amiral (« admiral_recovery »).
        await db.execute("""
            CREATE TABLE IF NOT EXISTS app_secrets (
                name TEXT PRIMARY KEY,
                value TEXT NOT NULL
            )
        """)

        # Documents du navire (papiers, manuels…), listés sur sa fiche. Le
        # fichier est dans IMG/Documents/ ; path est son URL, comme photo_path.
        await db.execute("""
            CREATE TABLE IF NOT EXISTS ship_documents (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                ship_id INTEGER NOT NULL REFERENCES ship_info(id) ON DELETE CASCADE,
                title TEXT,
                path TEXT NOT NULL,
                valid_until TEXT,
                created_at DATETIME
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
                street TEXT,
                postal_code TEXT,
                city TEXT,
                country TEXT,
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
                gender TEXT,
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

        await _migrate(db)
        await db.commit()


async def _migrate(db):
    """Bring an existing logbook.db up to the schema above.

    CREATE TABLE IF NOT EXISTS silently skips tables that already exist, so a
    new column never reaches a database created before it was added. Each step
    must be idempotent — this runs on every startup.
    """
    cursor = await db.execute("PRAGMA table_info(cruises)")
    if "ship_id" not in {row[1] for row in await cursor.fetchall()}:
        await db.execute(
            "ALTER TABLE cruises ADD COLUMN ship_id INTEGER "
            "REFERENCES ship_info(id) ON DELETE CASCADE"
        )
        # Cruises recorded before ships were linked belong to the first ship.
        await db.execute(
            "UPDATE cruises SET ship_id = (SELECT MIN(id) FROM ship_info) WHERE ship_id IS NULL"
        )
        print("Migration: cruises.ship_id added")

    cursor = await db.execute("PRAGMA table_info(crew_members)")
    if "gender" not in {row[1] for row in await cursor.fetchall()}:
        await db.execute("ALTER TABLE crew_members ADD COLUMN gender TEXT")
        print("Migration: crew_members.gender added")

    cursor = await db.execute("PRAGMA table_info(expenses)")
    if "description" not in {row[1] for row in await cursor.fetchall()}:
        await db.execute("ALTER TABLE expenses ADD COLUMN description TEXT")
        print("Migration: expenses.description added")

    cursor = await db.execute("PRAGMA table_info(expenses)")
    if "document_path" not in {row[1] for row in await cursor.fetchall()}:
        await db.execute("ALTER TABLE expenses ADD COLUMN document_path TEXT")
        print("Migration: expenses.document_path added")

    cursor = await db.execute("PRAGMA table_info(ship_documents)")
    if "valid_until" not in {row[1] for row in await cursor.fetchall()}:
        await db.execute("ALTER TABLE ship_documents ADD COLUMN valid_until TEXT")
        print("Migration: ship_documents.valid_until added")

    # « Surface » (longueur × bau) retirée de la fiche : une valeur qui se
    # recalcule, et que personne ne consultait. La colonne part avec ses
    # données, comme les tags de la To Do.
    cursor = await db.execute("PRAGMA table_info(ship_info)")
    if "surface" in {row[1] for row in await cursor.fetchall()}:
        await db.execute("ALTER TABLE ship_info DROP COLUMN surface")
        print("Migration: ship_info.surface dropped")

    # Le franc-bord (en mètres) et la puissance (en cv) deviennent des
    # nombres, l'unité étant affichée à côté comme pour les autres champs.
    # Dans une base ancienne leurs colonnes restent de type texte (SQLite ne
    # change pas le type d'une colonne) : on y range « 88 » plutôt que
    # « 88 cv », et l'affichage le relit comme un nombre. Une saisie qui n'en
    # est pas un est laissée telle quelle, et affichée telle quelle.
    for column, units in (("freeboard", r"m"), ("engine_power", r"cv|ch|hp")):
        cursor = await db.execute(f"SELECT id, {column} FROM ship_info WHERE {column} IS NOT NULL")
        for ship_id, value in await cursor.fetchall():
            if not isinstance(value, str):
                continue
            try:
                number = _import_amount(re.sub(rf"\s*({units})\.?\s*$", "", value.strip(), flags=re.I))
            except ValueError:
                continue
            if number is not None and str(number) != value:
                await db.execute(f"UPDATE ship_info SET {column} = ? WHERE id = ?", (number, ship_id))
                print(f"Migration: {column} « {value} » → {number}")

    await _move_crew_photos(db)
    await _migrate_gallery_to_carnet(db)
    await _rename_doc_folder(db)

    # L'adresse d'un contact, un seul texte libre, devient quatre champs comme
    # celle d'un équipier. Un texte libre ne se redécoupe pas sûrement : il
    # passe tel quel dans street, à reprendre à la main. La colonne address
    # reste en base (plus rien ne la lit), ce qui garde l'original.
    cursor = await db.execute("PRAGMA table_info(contacts)")
    columns = {row[1] for row in await cursor.fetchall()}
    if "street" not in columns:
        for col in ("street", "postal_code", "city", "country"):
            await db.execute(f"ALTER TABLE contacts ADD COLUMN {col} TEXT")
        if "address" in columns:
            await db.execute("UPDATE contacts SET street = address WHERE address IS NOT NULL")
        print("Migration: contacts street / postal_code / city / country added")

    # Les tags de la To Do ont été retirés de l'interface : la colonne suit.
    cursor = await db.execute("PRAGMA table_info(todo_items)")
    if "tags" in {row[1] for row in await cursor.fetchall()}:
        await db.execute("ALTER TABLE todo_items DROP COLUMN tags")
        print("Migration: todo_items.tags dropped")

    cursor = await db.execute("PRAGMA table_info(carnet_photos)")
    if "place" not in {row[1] for row in await cursor.fetchall()}:
        await db.execute("ALTER TABLE carnet_photos ADD COLUMN place TEXT")
        print("Migration: carnet_photos.place added")


TRACK_INTERVAL = 30  # seconds between automatic GPS recordings

# Automatic GPS recording is off. It appended a point every TRACK_INTERVAL to
# whichever route was open, which piled up tens of thousands of rows — and any
# route deleted along the way left its points behind. Flip to True to resume.
TRACK_RECORDING = False


async def track_recorder_loop():
    """Records GPS position from SignalK every TRACK_INTERVAL seconds into track_points."""
    while True:
        await asyncio.sleep(TRACK_INTERVAL)
        try:
            async with connect() as db:
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

            async with connect() as db:
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
    _start_place_lookup()
    if not TRACK_RECORDING:
        print("Automatic GPS recording disabled")
        yield
        return
    recorder = asyncio.create_task(track_recorder_loop())
    yield
    recorder.cancel()
    try:
        await recorder
    except asyncio.CancelledError:
        pass


app = FastAPI(lifespan=lifespan)


# ── Photo storage ─────────────────────────────────────────────────────────────

# Uploaded images live in IMG/ and are served under the same name, so the
# photo_path stored in the database ("/IMG/20260825-143002_coucher.jpg") is
# directly usable as an <img src>. Photos entered as an external URL still
# work: nothing rewrites photo_path, the upload just fills it in.
IMG_DIR = Path(__file__).parent / "IMG"
IMG_URL = "/IMG"
IMG_SUFFIXES = {".jpg", ".jpeg", ".png", ".gif", ".webp", ".heic", ".heif"}

IMG_DIR.mkdir(exist_ok=True)
# Pas de montage StaticFiles pour IMG/ : ses fichiers (factures, documents,
# photos des équipiers) passent par la route serve_img, plus bas, qui exige une
# connexion et le droit de la rubrique (IMG_FOLDER_SECTIONS).

# Icônes du site (onglet, écran d'accueil de l'iPad, Android). Contrairement à
# IMG/, le dossier est suivi par git : c'est du code, pas des données, et il
# doit arriver sur le Pi avec le reste. Les pages les déclarent dans base.html.
ICONS_DIR = Path(__file__).parent / "icons"
app.mount("/icons", StaticFiles(directory=ICONS_DIR), name="icons")


# Deux fichiers que les navigateurs vont chercher d'eux-mêmes à la racine,
# sans lire les <link> de la page : /favicon.ico (onglet, historique, favoris)
# et /apple-touch-icon.png (iPad, « Sur l'écran d'accueil »). Sans ces routes,
# ils tombent sur un 404 à chaque visite.
@app.get("/favicon.ico", include_in_schema=False)
async def favicon():
    return FileResponse(ICONS_DIR / "favicon.ico")


@app.get("/apple-touch-icon.png", include_in_schema=False)
@app.get("/apple-touch-icon-precomposed.png", include_in_schema=False)
async def apple_touch_icon():
    return FileResponse(ICONS_DIR / "apple-touch-icon.png")


# Factures et reçus des comptes : un sous-dossier d'IMG/, et non un dossier à
# part, pour que backup.sh (rsync de tout IMG/) et copy_db.sh (scp -r IMG/)
# les emportent sans changement, et que le montage /IMG les serve déjà.
# Comme les photos, rien ne les supprime : remplacer le document d'une dépense
# ou supprimer la dépense laisse le fichier, qu'une base restaurée peut encore
# désigner.
# « Invoices-Receipts » et non « Invoices&Receipts » : un & non protégé coupe
# une commande tapée dans un terminal (ls IMG/Invoices&Receipts lance « ls
# IMG/Invoices » en arrière-plan), et ce dossier se manipule à la main sur le
# Pi. L'ancien nom, factures, est repris au démarrage par _rename_doc_folder.
DOC_SUBDIR = "Invoices-Receipts"
OLD_DOC_SUBDIR = "factures"
DOC_SUFFIXES = IMG_SUFFIXES | {".pdf"}
# Plafond par fichier : un ticket photographié fait 3 à 5 Mo, un PDF scanné
# rarement plus de 10. Au-delà c'est une erreur, qui remplirait la carte SD.
DOC_MAX_BYTES = 20 * 1024 * 1024
(IMG_DIR / DOC_SUBDIR).mkdir(exist_ok=True)

# Photos des équipiers : leur propre sous-dossier d'IMG/, pour la même raison
# que les factures (sauvegarde et montage /IMG sans rien changer). Celles
# enregistrées avant sont déplacées au démarrage par _move_crew_photos.
CREW_SUBDIR = "SailingCrew"
(IMG_DIR / CREW_SUBDIR).mkdir(exist_ok=True)

# Photos du carnet de voyage, protégées par la rubrique « carnet ».
CARNET_SUBDIR = "Carnet"
(IMG_DIR / CARNET_SUBDIR).mkdir(exist_ok=True)

# Documents du navire, ajoutés depuis sa fiche (section « Documents »). PDF et
# images, comme les factures, mais un plafond plus haut : un manuel moteur en
# PDF dépasse volontiers les 20 Mo d'une facture.
SHIP_DOC_SUBDIR = "Documents"
SHIP_DOC_MAX_BYTES = 50 * 1024 * 1024
(IMG_DIR / SHIP_DOC_SUBDIR).mkdir(exist_ok=True)


async def _save_upload(upload: Optional[UploadFile], suffixes: set, subdir: str = "",
                       max_bytes: Optional[int] = None) -> Optional[str]:
    """Store an uploaded file under IMG/ (or one of its subfolders) and return
    its URL. Returns None when the form was submitted without choosing a file,
    so the caller can fall back to what it already had."""
    if upload is None or not upload.filename:
        return None
    suffix = Path(upload.filename).suffix.lower()
    if suffix not in suffixes:
        raise HTTPException(status_code=400, detail=f"Format de fichier non supporté : {suffix or 'inconnu'}")
    folder = IMG_DIR / subdir if subdir else IMG_DIR
    # Keep a readable name but drop anything that could escape IMG/ or need
    # URL-encoding, and stamp it so two "coucher.jpg" can coexist.
    stem = re.sub(r"[^A-Za-z0-9_-]+", "-", Path(upload.filename).stem).strip("-")[:40] or "fichier"
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    name = f"{stamp}_{stem}{suffix}"
    counter = 1
    while (folder / name).exists():
        name = f"{stamp}_{stem}-{counter}{suffix}"
        counter += 1
    # Un octet de plus que le plafond suffit à savoir qu'il est dépassé, sans
    # charger tout un fichier démesuré en mémoire.
    data = await upload.read(max_bytes + 1 if max_bytes else -1)
    if max_bytes and len(data) > max_bytes:
        raise HTTPException(status_code=413, detail=f"Fichier trop volumineux (plus de {max_bytes // (1024 * 1024)} Mo)")
    await asyncio.to_thread((folder / name).write_bytes, data)
    return f"{IMG_URL}/{subdir + '/' if subdir else ''}{name}"


async def _save_photo(upload: Optional[UploadFile]) -> Optional[str]:
    """Store an uploaded image in IMG/ and return the URL to use as photo_path."""
    return await _save_upload(upload, IMG_SUFFIXES)


async def _save_crew_photo(upload: Optional[UploadFile]) -> Optional[str]:
    """Photo d'un équipier, dans IMG/SailingCrew/."""
    return await _save_upload(upload, IMG_SUFFIXES, CREW_SUBDIR)


async def _rename_doc_folder(db):
    """IMG/factures/ devient IMG/Invoices-Receipts/, et le chemin des
    documents des dépenses suit.

    Étape de _migrate, rejouée à chaque démarrage. Fichier par fichier plutôt
    qu'un renommage du dossier : le nouveau existe déjà, créé vide au
    chargement du module. Tous les fichiers suivent — c'est le dossier qui
    change de nom —, même ceux qu'aucune dépense ne désigne. Les chemins sont
    réécrits ensuite ; un arrêt entre les deux se rattrape au tour suivant,
    puisque les fichiers déjà déplacés sont sautés et les chemins réécrits
    quoi qu'il arrive. L'ancien dossier, vidé, est retiré."""
    old = IMG_DIR / OLD_DOC_SUBDIR
    if old.is_dir():
        for f in old.iterdir():
            if f.name == ".DS_Store":
                f.unlink()   # métadonnées du Finder, recréées au besoin
            elif not (IMG_DIR / DOC_SUBDIR / f.name).exists():
                f.rename(IMG_DIR / DOC_SUBDIR / f.name)
                print(f"Migration: {OLD_DOC_SUBDIR}/{f.name} → {DOC_SUBDIR}/")
        try:
            old.rmdir()
        except OSError:
            pass   # un fichier homonyme n'a pas pu être déplacé : on le laisse
    old_prefix, new_prefix = f"{IMG_URL}/{OLD_DOC_SUBDIR}/", f"{IMG_URL}/{DOC_SUBDIR}/"
    await db.execute(
        "UPDATE expenses SET document_path = ? || substr(document_path, ?) WHERE document_path LIKE ?",
        (new_prefix, len(old_prefix) + 1, old_prefix + "%"),
    )


async def _migrate_gallery_to_carnet(db):
    """Les photos de l'ancienne galerie (trip_photos) deviennent des
    publications du carnet de voyage, une par photo, son commentaire pour
    texte. Rejouée à chaque démarrage : une photo déjà reprise (même chemin)
    est sautée. Les fichiers ne bougent pas, leur chemin non plus."""
    cursor = await db.execute(
        """SELECT p.photo_path, p.comment, p.lat, p.lon, p.created_at,
                  COALESCE(p.cruise_id, r.cruise_id), c.ship_id
           FROM trip_photos p
           LEFT JOIN routes r ON p.route_id = r.id
           LEFT JOIN cruises c ON c.id = COALESCE(p.cruise_id, r.cruise_id)
           WHERE p.photo_path NOT IN (SELECT photo_path FROM carnet_photos)"""
    )
    rows = await cursor.fetchall()
    if not rows:
        return
    cursor = await db.execute("SELECT MIN(id) FROM ship_info")
    first_ship = (await cursor.fetchone())[0]
    for path, comment, lat, lon, created, cruise_id, ship_id in rows:
        ship_id = ship_id or first_ship
        if ship_id is None:
            continue
        cursor = await db.execute(
            "INSERT INTO carnet_entries (ship_id, cruise_id, text, created_at) VALUES (?, ?, ?, ?)",
            (ship_id, cruise_id, comment, created),
        )
        await db.execute(
            "INSERT INTO carnet_photos (entry_id, photo_path, lat, lon, geo_source, position) "
            "VALUES (?, ?, ?, ?, ?, 0)",
            (cursor.lastrowid, path, lat, lon, "photo" if lat is not None else None),
        )
    print(f"Migration: {len(rows)} photo(s) de la galerie reprise(s) dans le carnet de voyage")


async def _move_crew_photos(db):
    """Range dans IMG/SailingCrew/ les photos d'équipiers restées à la racine
    d'IMG/, et met leur chemin à jour dans la base.

    Étape de _migrate, donc rejouée à chaque démarrage : une photo déjà rangée
    n'est plus concernée. Le fichier est déplacé avant que la base soit
    modifiée ; si l'app s'arrêtait entre les deux, le tour suivant trouve le
    fichier déjà à destination et ne fait que corriger le chemin. Une photo
    introuvable, ou une URL externe, est laissée telle quelle.

    Seules les photos qu'une fiche désigne sont déplacées : un ancien fichier
    qu'aucune ne désigne plus reste où il est, au cas où une sauvegarde
    ancienne de la base y renverrait encore."""
    cursor = await db.execute(
        "SELECT id, photo_path FROM crew_members WHERE photo_path LIKE ?", (IMG_URL + "/%",)
    )
    for crew_id, path in await cursor.fetchall():
        name = path[len(IMG_URL) + 1:]
        if "/" in name:
            continue   # déjà dans un sous-dossier
        src, dst = IMG_DIR / name, IMG_DIR / CREW_SUBDIR / name
        if src.exists() and not dst.exists():
            src.rename(dst)
        if dst.exists():
            await db.execute(
                "UPDATE crew_members SET photo_path = ? WHERE id = ?",
                (f"{IMG_URL}/{CREW_SUBDIR}/{name}", crew_id),
            )
            print(f"Migration: photo d'équipier rangée dans {CREW_SUBDIR}/ : {name}")


async def _save_ship_document(upload: Optional[UploadFile]) -> Optional[str]:
    """Document du navire (image ou PDF), dans IMG/Documents/."""
    return await _save_upload(upload, DOC_SUFFIXES, SHIP_DOC_SUBDIR, SHIP_DOC_MAX_BYTES)


async def _save_document(upload: Optional[UploadFile]) -> Optional[str]:
    """Facture ou reçu d'une dépense (image ou PDF), dans IMG/Invoices-Receipts/."""
    return await _save_upload(upload, DOC_SUFFIXES, DOC_SUBDIR, DOC_MAX_BYTES)


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


@app.middleware("http")
async def attach_ship_name(request: Request, call_next):
    """Expose the current ship's name to every template as request.state.ship_name.

    base.html shows it in the footer on every page, and only the /ship/*
    handlers pass `current_ship` in their context — threading it through the
    other thirty would be worse than one small query here.
    """
    request.state.ship_name = None
    if not request.url.path.startswith("/api/"):
        async with connect() as db:
            db.row_factory = aiosqlite.Row
            ship = await _fetch_ship(db, get_current_ship_id(request))
            if ship is not None:
                request.state.ship_name = ship["name"]
    return await call_next(request)


# ── Mise à jour en direct ─────────────────────────────────────────────────────

# Plusieurs appareils (iPad au cockpit, MacBook à la table à cartes) affichent le
# même serveur. Les pages sont rendues côté serveur, donc une saisie faite
# ailleurs n'apparaît pas tant qu'on ne recharge pas. Chaque page ouverte tient
# donc un flux /api/changes, sur lequel le serveur pousse un numéro de révision ;
# quand il change, le navigateur recharge.
#
# Un seul compteur global, sans granularité par écran : un bateau, une poignée de
# pages, et un rechargement est exactement ce dont une page rendue côté serveur a
# besoin. Ça suppose *un seul* processus uvicorn — c'est le cas ici (run.sh et
# nautibook.service lancent un worker unique) ; avec plusieurs workers il faudrait
# sortir le compteur du processus.
_revision = 0
_revision_changed = asyncio.Event()

# Sans trafic, une ligne de commentaire toutes les 20 s garde la connexion
# ouverte (et permet au navigateur de repérer un serveur tombé).
LIVE_PING = 20


def _bump_revision() -> None:
    """Signale aux autres appareils que la base a changé."""
    global _revision
    _revision += 1
    # set() puis clear() : un « pulse ». set() réveille tous les attendeurs
    # présents de façon synchrone, donc le clear() qui suit ne les rendort pas,
    # et l'événement repart à zéro pour le tour suivant.
    _revision_changed.set()
    _revision_changed.clear()


@app.middleware("http")
async def announce_changes(request: Request, call_next):
    """Incrémente la révision dès qu'une écriture a eu lieu.

    Toutes les mutations de l'app sont de simples posts de formulaire terminés
    par une redirection 303 (aucune écriture ne passe par fetch()), donc un POST
    redirigé est le signal « quelque chose a changé » — et il évite d'aller
    poser un crochet dans la trentaine de handlers.

    Le enregistreur GPS de fond n'appelle pas _bump_revision : un point toutes
    les 30 s rechargerait toutes les pages ouvertes en permanence.
    """
    response = await call_next(request)
    if request.method == "POST" and response.status_code == 303:
        _bump_revision()
    return response


# ── Comptes, rangs et droits ──────────────────────────────────────────────────
#
# Cinq rangs : Amiral (un seul), Capitaine, Matelot — qui ont un compte —, et
# Mousse, qui désigne simplement l'absence de compte : lecture seule, sans
# connexion. « Skipper » n'a rien à voir : c'est une fonction tenue sur une
# croisière (cruise_crew.role), pas un grade.
#
# Les accès suivent la grille arrêtée par l'utilisateur (grille-permissions.csv,
# 01/10/2026) : pour chaque rubrique de l'app et chaque rang, un niveau.
#   caché     : absent du menu, page refusée
#   voir      : lecture seule
#   modifier  : ajouter et corriger (comprend voir)
#   tout      : voir, modifier et supprimer
# Les rubriques « imports » et « rangs » sont tout ou rien.
#
# Une seule règle relie une adresse à un droit (required_permission) : la
# rubrique vient de SECTION_RULES, le niveau de la requête — ouvrir une page,
# c'est voir ; ouvrir un formulaire (…/new, …/edit) ou enregistrer, modifier ;
# …/delete, supprimer. Cette même règle décide du menu (voit), des pages
# refusées (le middleware), de la recherche, et des boutons que base.html
# masque à l'écran : changer un niveau ici change tout cela ensemble.

RANKS = ["amiral", "capitaine", "matelot"]
RANK_LABELS = {"amiral": "Amiral", "capitaine": "Capitaine", "matelot": "Matelot", None: "Mousse"}

LEVELS = {"caché": [], "voir": ["voir"], "modifier": ["voir", "modifier"],
          "tout": ["voir", "modifier", "supprimer"]}

# Rubrique → niveau, pour Mousse (None), Matelot, Capitaine, Amiral.
ACCESS_GRID = {
    #               Mousse    Matelot   Capitaine   Amiral
    "navire":     ("voir",   "voir",   "modifier", "tout"),   # Infos navire, documents
    "todo":       ("voir",   "voir",   "modifier", "tout"),
    "gasoil":     ("voir",   "voir",   "modifier", "tout"),
    "comptes":    ("caché",  "caché",  "voir",     "tout"),
    "contacts":   ("caché",  "caché",  "modifier", "tout"),   # Carnet d'adresses
    "equipiers":  ("voir",   "voir",   "modifier", "tout"),   # nom, contact
    "identite":   ("caché",  "voir",   "modifier", "tout"),   # identité, adresse des équipiers
    "croisieres": ("voir",   "voir",   "modifier", "tout"),   # croisières, routes, escales
    "journal":    ("voir",   "voir",   "modifier", "tout"),   # lignes du journal de bord
    "carnet":     ("voir",   "modifier", "modifier", "tout"),   # Carnet de voyage (ex-Galerie)
    "outils":     ("voir",   "voir",   "modifier", "tout"),   # météo, carte
    "parametres": ("caché",  "caché",  "voir",     "tout"),   # SignalK, sauvegarde
}
YES_NO_GRID = {
    #               Mousse  Matelot  Capitaine  Amiral
    "imports":    (False,  False,   False,     True),
    "rangs":      (False,  False,   False,     True),
}
_RANK_COLUMN = {None: 0, "matelot": 1, "capitaine": 2, "amiral": 3}

PERMISSIONS = {
    rank: {f"{section}.{level}" for section, cells in ACCESS_GRID.items()
           for level in LEVELS[cells[col]]}
          | {section for section, cells in YES_NO_GRID.items() if cells[col]}
    for rank, col in _RANK_COLUMN.items()
}

# Champs d'une fiche équipier rangés sous « identité, adresse » : ils suivent la
# rubrique identite, les autres la rubrique equipiers.
IDENTITY_FIELDS = ("birth_date", "birth_place", "nationality", "id_type", "id_number",
                   "street", "postal_code", "city")

# Adresse → rubrique. Première règle qui convient ; une adresse qu'aucune ne
# couvre est libre (accueil, recherche, connexion…). Des expressions simples,
# valables telles quelles en Python et en JavaScript (base.html les reprend).
SECTION_RULES = [
    (r"^/crew/\d+/become-admiral$", None),             # premier Amiral : libre
    (r"^/ship/(expenses|contacts)/import", "imports"),
    (r"^/cruises/import$", "imports"),
    (r"^/ship/(info|documents|new)(/|$)", "navire"),
    (r"^/ship/todo(/|$)", "todo"),
    (r"^/ship/fuel(/|$)", "gasoil"),
    (r"^/ship/expenses(/|$)", "comptes"),
    (r"^/ship/contacts(/|$)", "contacts"),
    (r"^/crew/\d+/(rank|reset-password|username)$", "rangs"),
    (r"^/crew/\d+/field/(" + "|".join(IDENTITY_FIELDS) + r")$", "identite"),
    # Le formulaire complet contient l'identité : il en suit la rubrique.
    (r"^/crew/(new|\d+/edit)$", "identite"),
    (r"^/crew(/|$)", "equipiers"),
    (r"^/routes/\d+/new-line$", "journal"),
    (r"^/logbook(/|$)", "journal"),
    (r"^/(cruises|routes|stopovers)(/|$)", "croisieres"),
    (r"^/api/(routes|cruises|all-cruises)/", "croisieres"),
    (r"^/(carnet|gallery)(/|$)", "carnet"),
    (r"^/tools(/|$)", "outils"),
    (r"^/settings(/|$)", "parametres"),
]
_SECTION_RULES = [(re.compile(rx), section) for rx, section in SECTION_RULES]
# Ouvrir ces pages, c'est déjà modifier : ce sont des formulaires de saisie.
FORM_PAGE_RE = re.compile(r"/(new|edit|new-line)$")
# POST qui n'écrivent rien : tester la connexion SignalK ne fait que lire.
READ_ONLY_POSTS = ("/settings/test-signalk",)


def required_permission(method: str, path: str) -> Optional[str]:
    """Le droit qu'exige cette requête, ou None si elle est libre."""
    for rx, section in _SECTION_RULES:
        if rx.match(path):
            break
    else:
        return None
    if section is None or section in YES_NO_GRID:
        return section
    if method == "POST" and path not in READ_ONLY_POSTS:
        return f"{section}.supprimer" if path.endswith("/delete") else f"{section}.modifier"
    return f"{section}.modifier" if FORM_PAGE_RE.search(path) else f"{section}.voir"


SESSION_COOKIE = "nb_session"
SESSION_DAYS = 30
# Écritures permises sans être connecté : se connecter, récupérer l'Amiral,
# le tout premier réglage du serveur SignalK, devenir le premier Amiral
# (become_admiral vérifie lui-même qu'aucun compte n'existe encore), et
# changer de navire.
OPEN_POST_PATHS = ("/login", "/recover", "/setup")
# Changer de navire ne pose qu'un cookie : un Mousse peut le faire.
OPEN_POST_PATTERN = re.compile(r"^/(crew/\d+/become-admiral|ship/select/\d+)$")
# Ni session ni contrôle pour les icônes et le flux en direct : publics, et le
# flux reste ouvert en permanence. Les fichiers d'IMG/, eux, sont contrôlés.
NO_SESSION_PREFIXES = ("/icons", "/favicon", "/apple-touch-icon", "/api/changes")


def can(user: Optional[dict], permission: str) -> bool:
    """L'utilisateur (None = Mousse) a-t-il ce droit ?"""
    return permission in PERMISSIONS.get(user["rank"] if user else None, set())


def _token_hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


async def _session_user(token: str) -> Optional[dict]:
    """L'utilisateur d'un jeton de session encore valide, ou None."""
    async with connect() as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute(
            """SELECT u.id, u.username, u.rank, u.crew_member_id, m.first_name, m.last_name
               FROM sessions s JOIN users u ON s.user_id = u.id
               JOIN crew_members m ON u.crew_member_id = m.id
               WHERE s.token_hash = ? AND s.expires_at > ?""",
            (_token_hash(token), datetime.now().strftime("%Y-%m-%d %H:%M:%S")),
        )
        row = await cursor.fetchone()
    return dict(row) if row else None


async def _start_session(db, user_id: int) -> str:
    """Nouvelle session pour cet utilisateur ; renvoie le jeton du cookie."""
    token = secrets.token_urlsafe(32)
    now = datetime.now()
    await db.execute(
        "INSERT INTO sessions (token_hash, user_id, created_at, expires_at) VALUES (?, ?, ?, ?)",
        (_token_hash(token), user_id, now.strftime("%Y-%m-%d %H:%M:%S"),
         (now + timedelta(days=SESSION_DAYS)).strftime("%Y-%m-%d %H:%M:%S")),
    )
    # Ménage au passage : les sessions expirées ne servent plus à rien.
    await db.execute("DELETE FROM sessions WHERE expires_at <= ?", (now.strftime("%Y-%m-%d %H:%M:%S"),))
    return token


def _set_session_cookie(response, token: str):
    # HttpOnly : invisible au JavaScript de la page. SameSite=Lax : le
    # navigateur ne l'envoie pas avec un formulaire posté depuis un autre site,
    # ce qui ferme la porte aux écritures déclenchées à distance. Pas de Secure :
    # l'app est servie en HTTP sur le wifi du bord (risque accepté).
    response.set_cookie(SESSION_COOKIE, token, max_age=SESSION_DAYS * 24 * 3600,
                        httponly=True, samesite="lax")


def _safe_next(path: Optional[str], default: str = "/") -> str:
    """Une page de l'app où revenir — jamais une adresse extérieure, que ce
    paramètre suffirait sinon à imposer (« //ailleurs.com »)."""
    if path and path.startswith("/") and not path.startswith("//"):
        return path
    return default


def _referer_path(request: Request) -> str:
    """La page d'où vient la requête, chemin et paramètres seulement."""
    ref = request.headers.get("referer") or ""
    m = re.match(r"^https?://[^/]+(/[^#]*)?", ref)
    return _safe_next(m.group(1) if m and m.group(1) else None)


@app.middleware("http")
async def require_login_to_write(request: Request, call_next):
    """Charge l'utilisateur connecté (request.state.user, None pour un Mousse)
    et fait respecter les droits : toute écriture exige d'être connecté, et une
    page réservée exige son droit.

    Toutes les écritures de l'app sont des POST de formulaire : vérifier la
    méthode ici couvre les quelque cent routes d'un coup, y compris celles
    qu'on ajoutera. Déclaré après announce_changes, donc exécuté avant lui :
    une écriture refusée n'est pas annoncée aux autres pages."""
    path = request.url.path
    request.state.user = None
    if path.startswith(NO_SESSION_PREFIXES):
        return await call_next(request)
    token = request.cookies.get(SESSION_COOKIE)
    if token:
        request.state.user = await _session_user(token)
    user = request.state.user

    if request.method == "POST" and user is None and not (
        path in OPEN_POST_PATHS or OPEN_POST_PATTERN.match(path)
    ):
        # Retour, après connexion, sur la page où l'on voulait enregistrer.
        # La saisie elle-même est perdue : elle n'est pas gardée en route.
        return RedirectResponse(
            url=f"/login?ecrire=1&next={quote(_referer_path(request))}", status_code=303
        )

    permission = required_permission(request.method, path)
    if permission and not can(user, permission):
        if user is None:
            return RedirectResponse(url=f"/login?next={quote(path)}", status_code=303)
        return templates.TemplateResponse(
            "auth/forbidden.html",
            {"request": request, "active_section": None, "rang": RANK_LABELS[user["rank"]]},
            status_code=403,
        )
    return await call_next(request)


# Fichiers d'IMG/ : il faut être connecté, et avoir le droit « voir » de la
# rubrique dont relève le dossier. Un fichier à la racine d'IMG/ (photos des
# tâches) demande seulement d'être connecté. Le Mousse, sans compte, ne voit
# que les dossiers d'IMG_OPEN_FOLDERS (photos du carnet et des équipiers) — ailleurs, les
# pages qu'il consulte montrent les initiales ou un cadenas (fichier_visible).
IMG_FOLDER_SECTIONS = {"Invoices-Receipts": "comptes", "Documents": "navire", "SailingCrew": "equipiers",
                       "Carnet": "carnet"}
# Dossiers visibles sans connexion : les photos du carnet de voyage et celles
# des équipiers, que le Mousse peut regarder (choix de l'utilisateur,
# 02/10/2026). Factures et documents du navire restent réservés aux comptes.
IMG_OPEN_FOLDERS = {"Carnet", "SailingCrew"}


def can_see_file(user: Optional[dict], url: Optional[str]) -> bool:
    """Ce fichier peut-il être montré à cet utilisateur ? Une adresse hors
    d'IMG/ (photo donnée par une URL externe) n'est pas concernée."""
    if not url or not url.startswith(IMG_URL + "/"):
        return True
    rel = url[len(IMG_URL) + 1:]
    folder = rel.split("/", 1)[0] if "/" in rel else None
    section = IMG_FOLDER_SECTIONS.get(folder)
    if user is None:
        # Le Mousse ne voit que les dossiers ouverts sans connexion, et
        # seulement si la grille lui donne « voir » sur leur rubrique.
        return folder in IMG_OPEN_FOLDERS and can(None, f"{section}.voir")
    return section is None or can(user, f"{section}.voir")


@app.get(IMG_URL + "/{file_path:path}", include_in_schema=False)
async def serve_img(request: Request, file_path: str):
    if not can_see_file(request.state.user, f"{IMG_URL}/{file_path}"):
        # Une page ouverte directement (un PDF dans un onglet) mène à la
        # connexion ; une image dans une page reçoit un simple refus.
        if request.state.user is None and "text/html" in request.headers.get("accept", ""):
            return RedirectResponse(url=f"/login?next={quote(request.url.path)}", status_code=303)
        return Response(status_code=403)
    # resolve() puis vérification du parent : « ../logbook.db » ne sort pas d'IMG/.
    full = (IMG_DIR / file_path).resolve()
    if IMG_DIR.resolve() not in full.parents or not full.is_file():
        raise HTTPException(status_code=404)
    # private : le navigateur peut garder l'image une heure, aucun cache
    # intermédiaire ne la partage.
    return FileResponse(full, headers={"Cache-Control": "private, max-age=3600"})


def _template_user(request) -> Optional[dict]:
    return getattr(request.state, "user", None)


# Pour les templates : qui est connecté, ses droits, et le menu qu'il voit.
templates.env.globals["utilisateur"] = _template_user
templates.env.globals["peut"] = lambda request, permission: can(_template_user(request), permission)
def _voit(request, href: str, method: str = "GET") -> bool:
    """Cette adresse est-elle permise à l'utilisateur ? Sert au menu."""
    permission = required_permission(method, href.split("?", 1)[0])
    return permission is None or can(_template_user(request), permission)


templates.env.globals["voit"] = _voit
templates.env.globals["fichier_visible"] = lambda request, url: can_see_file(_template_user(request), url)
# Pour base.html, qui masque à l'écran ce que l'utilisateur ne peut pas faire :
# les règles, et la liste de ses droits.
templates.env.globals["regles_acces"] = lambda request: json.dumps({
    "rules": SECTION_RULES,
    "yesNo": list(YES_NO_GRID),
    "readOnlyPosts": list(READ_ONLY_POSTS),
    "perms": sorted(PERMISSIONS.get((_template_user(request) or {}).get("rank"), set())),
})
templates.env.globals["rank_labels"] = RANK_LABELS


@app.get("/api/changes")
async def changes_stream(request: Request):
    """Flux Server-Sent Events poussant le numéro de révision courant."""
    async def events():
        # Le premier message donne au navigateur sa référence, pour qu'une page
        # chargée juste après une saisie ne se recharge pas aussitôt.
        sent = _revision
        yield f"data: {sent}\n\n"
        while True:
            try:
                await asyncio.wait_for(_revision_changed.wait(), timeout=LIVE_PING)
            except asyncio.TimeoutError:
                pass
            if await request.is_disconnected():
                break
            # On compare au dernier numéro envoyé plutôt que de se fier au seul
            # réveil : un bump tombé entre deux attentes serait sinon perdu.
            if _revision != sent:
                sent = _revision
                yield f"data: {sent}\n\n"
            else:
                yield ": ping\n\n"

    return StreamingResponse(
        events(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"},
    )


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


# Où revenir après « Changer de navire » : la page équivalente pour le nouveau
# navire. Une page qui montre *un* enregistrement (/ship/contacts/5,
# /routes/12…) ne se rouvre pas telle quelle — il appartient à l'ancien
# navire — mais retombe sur la liste de sa rubrique. None garde la page telle
# quelle : elle ne dépend pas du navire (équipiers, outils…) ou se recalcule
# pour lui (/search?q=… relance la recherche). Premier préfixe qui convient,
# donc les plus précis d'abord.
SHIP_SWITCH_TARGETS = [
    ("/ship/todo", "/ship/todo"),
    ("/ship/fuel", "/ship/fuel"),
    ("/ship/expenses", "/ship/expenses"),
    ("/ship/contacts", "/ship/contacts"),
    ("/cruises/list", "/cruises/list"),
    ("/cruises/stopovers", "/cruises/stopovers"),
    ("/cruises/new", "/cruises/new"),
    ("/cruises", "/cruises/current"),
    ("/stopovers", "/cruises/stopovers"),
    ("/routes", "/routes/current"),
    ("/logbook", "/routes/current"),
    ("/gallery", "/gallery"),
    ("/search", None),
    ("/crew", None),
    ("/tools", None),
    ("/settings", None),
]


def _page_for_ship(from_page: Optional[str]) -> str:
    """Page où atterrir après un changement de navire, depuis `from_page`."""
    # Un chemin de l'app seulement : « //ailleurs.com » ou une URL complète
    # feraient de ce paramètre une redirection vers n'importe quel site.
    if not from_page or not from_page.startswith("/") or from_page.startswith("//"):
        return "/ship/info"
    path = from_page.split("?", 1)[0]
    if path == "/":   # l'accueil ne dépend d'aucun navire
        return "/"
    for prefix, target in SHIP_SWITCH_TARGETS:
        if path == prefix or path.startswith(prefix + "/"):
            return target or from_page
    return "/ship/info"


@app.get("/ship/select", response_class=HTMLResponse)
async def ship_select(request: Request, from_page: Optional[str] = Query(None, alias="from")):
    async with connect() as db:
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
            "from_page": from_page,
        },
    )


@app.post("/ship/select/{ship_id}")
async def set_current_ship(ship_id: int, from_page: Optional[str] = Form(None)):
    response = RedirectResponse(url=_page_for_ship(from_page), status_code=303)
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
    async with connect() as db:
        cursor = await db.execute("INSERT INTO ship_info (name) VALUES (?)", (name,))
        ship_id = cursor.lastrowid
        await db.commit()
    response = RedirectResponse(url="/ship/info/edit", status_code=303)
    response.set_cookie(key="ship_id", value=str(ship_id), max_age=365 * 24 * 3600, httponly=True)
    return response


@app.get("/ship/info", response_class=HTMLResponse)
async def ship_info(request: Request):
    async with connect() as db:
        db.row_factory = aiosqlite.Row
        ship = await _fetch_ship(db, get_current_ship_id(request))
        ship = dict(ship) if ship else None
        documents = []
        if ship:
            cursor = await db.execute(
                "SELECT * FROM ship_documents WHERE ship_id = ? ORDER BY created_at DESC, id DESC",
                (ship["id"],),
            )
            documents = [dict(d) for d in await cursor.fetchall()]
    return templates.TemplateResponse(
        "ship/info.html",
        {"request": request, "active_section": "ship", "ship": ship, "current_ship": ship,
         "documents": documents, "doc_max_bytes": SHIP_DOC_MAX_BYTES, "ship_fields": SHIP_FIELDS,
         # Repères de la couleur de validité : expiré avant aujourd'hui,
         # « bientôt » dans les DOC_EXPIRY_WARNING_DAYS qui suivent. Des
         # dates ISO, que la template compare comme des chaînes.
         "today": date.today().isoformat(),
         "soon": (date.today() + timedelta(days=DOC_EXPIRY_WARNING_DAYS)).isoformat()},
    )


# Un document dont la validité expire dans ce délai s'affiche en orange.
DOC_EXPIRY_WARNING_DAYS = 30


@app.post("/ship/documents/new")
async def add_ship_document(
    request: Request,
    title: Optional[str] = Form(None),
    valid_until: Optional[str] = Form(None),
    document_file: Optional[UploadFile] = File(None),
):
    """Ajoute un document à la fiche du navire courant. Sans titre, celui du
    fichier (sans son extension) en tient lieu."""
    path = await _save_ship_document(document_file)
    if path:
        title = (title or "").strip() or Path(document_file.filename).stem
        async with connect() as db:
            ship = await _fetch_ship(db, get_current_ship_id(request))
            if ship is None:
                raise HTTPException(status_code=404, detail="Aucun navire")
            await db.execute(
                "INSERT INTO ship_documents (ship_id, title, path, valid_until, created_at) "
                "VALUES (?, ?, ?, ?, ?)",
                (ship[0], title, path, valid_until or None, datetime.now().strftime("%Y-%m-%d %H:%M:%S")),
            )
            await db.commit()
    return RedirectResponse(url="/ship/info", status_code=303)


@app.post("/ship/documents/{doc_id}/valid-until")
async def set_ship_document_validity(doc_id: int, value: Optional[str] = Form(None)):
    """Modification sur place de la date de validité, depuis la liste : un
    document renouvelé change de date sans être retiré ni rajouté. Vide, le
    document n'a plus d'échéance."""
    async with connect() as db:
        await db.execute("UPDATE ship_documents SET valid_until = ? WHERE id = ?", (value or None, doc_id))
        await db.commit()
    return RedirectResponse(url="/ship/info", status_code=303)


@app.post("/ship/documents/{doc_id}/delete")
async def delete_ship_document(doc_id: int):
    """Retire le document de la fiche, mais laisse le fichier dans
    IMG/Documents/ : rien n'efface un fichier d'IMG/, pour qu'une base
    restaurée retrouve les siens (voir backup.sh)."""
    async with connect() as db:
        await db.execute("DELETE FROM ship_documents WHERE id = ?", (doc_id,))
        await db.commit()
    return RedirectResponse(url="/ship/info", status_code=303)


@app.get("/ship/info/edit", response_class=HTMLResponse)
async def edit_ship_info_form(request: Request):
    async with connect() as db:
        db.row_factory = aiosqlite.Row
        ship = await _fetch_ship(db, get_current_ship_id(request))
        ship = dict(ship) if ship else None
    return templates.TemplateResponse(
        "ship/info_edit.html",
        {"request": request, "active_section": "ship", "ship": ship, "current_ship": ship},
    )


# Champs de la fiche navire modifiables sur place, et leur type. Le nom est
# interpolé dans l'UPDATE : il doit venir de ce dictionnaire, jamais de l'URL
# telle quelle (même parti que EDITABLE_CONTACT_FIELDS). Les colonnes REAL
# sont des « number », de même que freeboard (en mètres) et engine_power (en
# cv), textes à l'origine et convertis au démarrage par _migrate.
SHIP_FIELDS = {
    **{f: "text" for f in (
        "name", "home_port", "flag", "mmsi", "call_sign", "registration", "registry",
        "engine_brand", "engine_model", "engine_serial",
        "insurance_company", "insurance_policy",
    )},
    **{f: "number" for f in (
        "loa", "hull_length", "waterline_length", "beam", "draft", "air_draft", "freeboard",
        "mast_height", "clearance_no_mast", "displacement", "ballast",
        "sail_main", "sail_genoa", "sail_spinnaker", "sail_trinquette", "sail_portant",
        "tank_fuel", "tank_water", "engine_power", "engine_consumption", "engine_hours_initial",
    )},
    **{f: "date" for f in (
        "issued_date", "valid_until", "engine_hours_date", "insurance_start", "insurance_end",
    )},
    "misc_notes": "textarea",
}


@app.post("/ship/info/field/{field}")
async def update_ship_field(request: Request, field: str, value: Optional[str] = Form(None)):
    """Modification sur place d'un champ de la fiche du navire courant."""
    kind = SHIP_FIELDS.get(field)
    if kind is None:
        raise HTTPException(status_code=404, detail="Field not editable")
    value = (value or "").strip() or None
    if kind == "number" and value is not None:
        # « 12,5 » comme « 12.5 » ; une saisie qui n'est pas un nombre ne
        # remplace pas la valeur enregistrée.
        try:
            value = _import_amount(value)
        except ValueError:
            return RedirectResponse(url="/ship/info", status_code=303)
    async with connect() as db:
        ship = await _fetch_ship(db, get_current_ship_id(request))
        if ship is None:
            raise HTTPException(status_code=404, detail="Aucun navire")
        await db.execute(f"UPDATE ship_info SET {field} = ? WHERE id = ?", (value, ship[0]))
        await db.commit()
    return RedirectResponse(url="/ship/info", status_code=303)


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
    beam: Optional[float] = Form(None),
    draft: Optional[float] = Form(None),
    air_draft: Optional[float] = Form(None),
    mast_height: Optional[float] = Form(None),
    clearance_no_mast: Optional[float] = Form(None),
    freeboard: Optional[float] = Form(None),
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
    engine_power: Optional[float] = Form(None),
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
        loa, hull_length, waterline_length, beam, draft, air_draft,
        mast_height, clearance_no_mast, freeboard, displacement, ballast,
        sail_main, sail_genoa, sail_spinnaker, sail_trinquette, sail_portant,
        tank_fuel, tank_water,
        engine_brand or None, engine_model or None, engine_serial or None,
        engine_power, engine_consumption, engine_hours_initial,
        engine_hours_date or None,
        insurance_company or None, insurance_policy or None,
        insurance_start or None, insurance_end or None,
        misc_notes or None,
    )
    async with connect() as db:
        cursor = await db.execute("SELECT id FROM ship_info WHERE id = ?", (ship_id,))
        existing = await cursor.fetchone()
        if existing:
            await db.execute(
                """UPDATE ship_info SET
                   name=?, home_port=?, flag=?, mmsi=?, call_sign=?, registration=?, registry=?,
                   issued_date=?, valid_until=?, loa=?, hull_length=?, waterline_length=?,
                   beam=?, draft=?, air_draft=?, mast_height=?, clearance_no_mast=?,
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
                   beam, draft, air_draft, mast_height, clearance_no_mast,
                   freeboard, displacement, ballast, sail_main, sail_genoa,
                   sail_spinnaker, sail_trinquette, sail_portant, tank_fuel, tank_water,
                   engine_brand, engine_model, engine_serial, engine_power,
                   engine_consumption, engine_hours_initial, engine_hours_date,
                   insurance_company, insurance_policy, insurance_start, insurance_end,
                   misc_notes)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
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
async def ship_expenses(request: Request, tri: Optional[str] = None, ordre: Optional[str] = None):
    ship_id = get_current_ship_id(request)
    # Tri choisi par les flèches des titres de colonne. La colonne vient de
    # EXPENSE_SORTS et jamais de l'URL telle quelle : elle est interpolée dans
    # l'ORDER BY. Par défaut, les plus récentes d'abord.
    if tri not in EXPENSE_SORTS:
        tri, ordre = "date", "desc"
    ordre = "asc" if ordre == "asc" else "desc"
    column, is_text = EXPENSE_SORTS[tri]
    # Le texte se trie sans tenir compte des majuscules ni des accents (fold,
    # comme la recherche) ; les cases vides vont toujours en dernier, quel que
    # soit le sens ; à égalité, la date la plus récente d'abord.
    key = f"fold({column})" if is_text else column
    order_by = f"({column} IS NULL OR {column} = ''), {key} {ordre.upper()}, date DESC, id DESC"
    async with connect() as db:
        db.row_factory = aiosqlite.Row
        await db.create_function("fold", 1, _fold_sql, deterministic=True)
        ship = await _fetch_ship(db, ship_id)
        cursor = await db.execute(
            f"SELECT * FROM expenses WHERE ship_id = ? ORDER BY {order_by}", (ship_id,)
        )
        entries = await cursor.fetchall()
        # Nom du fournisseur → id de sa fiche. expenses.supplier retient un nom,
        # le même que celui que propose le formulaire (société, à défaut nom du
        # contact) ; un nom sans fiche dans le carnet reste sans lien.
        cursor = await db.execute(
            "SELECT id, COALESCE(company, contact_name) FROM contacts WHERE ship_id = ?", (ship_id,)
        )
        supplier_ids = {name: cid for cid, name in await cursor.fetchall() if name}
    return templates.TemplateResponse(
        "ship/expenses.html",
        {
            "request": request, "active_section": "ship",
            "current_ship": dict(ship) if ship else None,
            "entries": [dict(e) for e in entries],
            "supplier_ids": supplier_ids,
            "tri": tri, "ordre": ordre,
        },
    )


# Colonnes de la liste des comptes que l'on peut trier : nom dans l'URL →
# (colonne SQL, texte ?). Seules ces colonnes peuvent finir dans l'ORDER BY.
EXPENSE_SORTS = {
    "date": ("date", False),
    "montant": ("unit_price", False),
    "objet": ("designation", True),
    "type": ("expense_type", True),
    "fournisseur": ("supplier", True),
}


# Types de frais proposés dans le formulaire des comptes. Valeurs *stockées* :
# en renommer un laisse les lignes existantes sur l'ancien libellé, que le
# formulaire d'édition continue d'afficher (voir expenses_form.html). None
# marque le séparateur entre les types et « Autre ».
EXPENSE_TYPES = ["Equipement", "Entretien", "Stationnement", "Administratif", None, "Autre"]

# Valeur de l'option « Saisir manuellement » du fournisseur. Ce n'est pas un
# nom : la dépense est enregistrée sans fournisseur, puis le formulaire de
# contact s'ouvre et lui impute le contact créé (voir create_contact).
NEW_CONTACT = "__nouveau__"


def _after_expense_save(entry_id: int, supplier: Optional[str]) -> RedirectResponse:
    """Liste des comptes, ou création du contact si c'est ce qu'on a demandé."""
    if supplier == NEW_CONTACT:
        return RedirectResponse(url=f"/ship/contacts/new?expense_id={entry_id}", status_code=303)
    return RedirectResponse(url="/ship/expenses", status_code=303)


def _expense_balance(unit_price, paid):
    """Solde et montant payé, recalculés à chaque enregistrement.

    Sans montant il n'y a pas de solde ; avec un montant, un payé vide vaut 0.
    """
    if unit_price is None:
        return None, paid
    paid = paid or 0
    return unit_price - paid, paid


async def _expense_form(request: Request, entry: Optional[dict]):
    """Formulaire des comptes, en création (entry=None) comme en modification."""
    ship_id = get_current_ship_id(request)
    async with connect() as db:
        db.row_factory = aiosqlite.Row
        ship = await _fetch_ship(db, ship_id)
        cursor = await db.execute(
            "SELECT id, company, contact_name FROM contacts WHERE ship_id = ? ORDER BY company, contact_name",
            (ship_id,),
        )
        contacts = await cursor.fetchall()
    return templates.TemplateResponse(
        "ship/expenses_form.html",
        {
            "request": request, "active_section": "ship",
            "current_ship": dict(ship) if ship else None,
            "contacts": [dict(c) for c in contacts],
            "entry": entry,
            "expense_types": EXPENSE_TYPES,
            "new_contact": NEW_CONTACT,
            # Date du jour préremplie, en heure locale comme le reste du journal.
            "today": datetime.now().strftime("%Y-%m-%d"),
            "doc_accept": ",".join(sorted(DOC_SUFFIXES)),
            "doc_max_bytes": DOC_MAX_BYTES,
        },
    )


@app.get("/ship/expenses/new", response_class=HTMLResponse)
async def new_expense_form(request: Request):
    return await _expense_form(request, None)


@app.post("/ship/expenses/new")
async def create_expense(
    request: Request,
    date: Optional[str] = Form(None),
    designation: Optional[str] = Form(None),
    description: Optional[str] = Form(None),
    unit_price: Optional[float] = Form(None),
    paid: Optional[float] = Form(None),
    expense_type: Optional[str] = Form(None),
    payment: Optional[str] = Form(None),
    supplier: Optional[str] = Form(None),
    document_file: Optional[UploadFile] = File(None),
):
    ship_id = get_current_ship_id(request)
    balance, paid = _expense_balance(unit_price, paid)
    document = await _save_document(document_file)
    async with connect() as db:
        cursor = await db.execute(
            """INSERT INTO expenses (ship_id, date, designation, description, document_path, unit_price, paid,
                                     balance, expense_type, payment, supplier)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (ship_id, date or None, designation or None, description or None, document, unit_price, paid, balance,
             expense_type or None, payment or None,
             None if supplier == NEW_CONTACT else supplier or None),
        )
        entry_id = cursor.lastrowid
        await db.commit()
    return _after_expense_save(entry_id, supplier)


# ── Import des comptes ────────────────────────────────────────────────────────
# Un tableur (CSV ou Excel .xlsx) devient une série de dépenses, en deux temps :
# lecture et aperçu d'abord, sans rien écrire, puis enregistrement sur
# confirmation. Déclaré avant /ship/expenses/{entry_id}, qui prendrait
# « import » pour un id.

PAYMENT_MODES = ["Carte", "Espèces", "Virement", "Chèque"]
templates.env.globals["payment_modes"] = PAYMENT_MODES

# Colonnes du fichier modèle, dans l'ordre de la fiche, et la colonne qu'elles
# remplissent. L'import reconnaît aussi les variantes de IMPORT_ALIASES : un
# tableur existant n'a pas à reprendre les titres exacts.
IMPORT_COLUMNS = [
    ("Date", "date"), ("Montant", "unit_price"), ("Objet", "designation"),
    ("Type de frais", "expense_type"), ("Fournisseur", "supplier"), ("Payé", "paid"),
    ("Paiement", "payment"), ("Description", "description"),
]
IMPORT_ALIASES = {
    "date": "date",
    "montant": "unit_price", "montant (€)": "unit_price", "montant €": "unit_price",
    "prix": "unit_price", "pu tvac": "unit_price", "total": "unit_price",
    "objet": "designation", "designation": "designation", "libelle": "designation",
    "type de frais": "expense_type", "type frais": "expense_type", "type": "expense_type",
    "fournisseur": "supplier",
    "paye": "paid", "paye (€)": "paid", "paye €": "paid",
    "paiement": "payment", "mode de paiement": "payment",
    "description": "description", "notes": "description", "remarques": "description",
}
IMPORT_MAX_BYTES = 5 * 1024 * 1024   # un tableur de comptes pèse quelques Ko


def _import_amount(value):
    """« 1 234,56 € », « 1.234,56 », 1234.56 → 1234.56 ; vide → None.
    Lève ValueError si ce n'est pas un nombre."""
    if value is None or value == "":
        return None
    if isinstance(value, (int, float)):
        return round(float(value), 2)
    text = re.sub(r"[\s  €]", "", str(value))
    if not text:
        return None
    # Des deux séparateurs, le dernier est la décimale : « 1.234,56 » comme
    # « 1,234.56 ». Un seul, quel qu'il soit, est la décimale.
    if "," in text and "." in text:
        thousands = "." if text.rfind(",") > text.rfind(".") else ","
        text = text.replace(thousands, "")
    text = text.replace(",", ".")
    return round(float(text), 2)


def _import_date(value):
    """Cellule date d'Excel, « 24/09/2026 », « 24/09/26 », « 2026-09-24 »… →
    « 2026-09-24 » ; vide → None. Lève ValueError sinon."""
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        return value.strftime("%Y-%m-%d")
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, (int, float)):
        # Numéro de série Excel, quand la cellule n'est pas typée date.
        return (date(1899, 12, 30) + timedelta(days=int(value))).isoformat()
    text = str(value).strip().split(" ")[0]
    for fmt in ("%d/%m/%Y", "%d/%m/%y", "%Y-%m-%d", "%d-%m-%Y", "%d.%m.%Y", "%d.%m.%y"):
        try:
            return datetime.strptime(text, fmt).strftime("%Y-%m-%d")
        except ValueError:
            pass
    raise ValueError(text)


def _read_sheet(filename: str, data: bytes) -> list:
    """Le fichier en liste de lignes (listes de cellules), titres compris."""
    # Le champ fichier n'impose aucun type (voir expenses_import.html) : c'est
    # ici qu'une photo ou un PDF choisi par erreur est refusé, clairement.
    suffix = Path(filename).suffix.lower()
    if suffix not in (".csv", ".txt", ".xlsx", ".xls"):
        raise ValueError(f"Format non reconnu ({suffix or 'sans extension'}) : "
                         "choisissez un fichier Excel (.xlsx) ou CSV.")
    if filename.lower().endswith(".xlsx"):
        try:
            # Importé ici et non en tête : si la bibliothèque manquait (pip
            # hors réseau sur le Pi), l'app démarre quand même, seul l'import
            # Excel est refusé.
            from openpyxl import load_workbook
        except ImportError:
            raise ValueError("L'import Excel n'est pas disponible ici : enregistrez le fichier en CSV.")
        sheet = load_workbook(io.BytesIO(data), read_only=True, data_only=True).worksheets[0]
        return [list(row) for row in sheet.iter_rows(values_only=True)]
    if filename.lower().endswith(".xls"):
        raise ValueError("L'ancien format .xls n'est pas lu : enregistrez le fichier en .xlsx ou en CSV.")
    # CSV : UTF-8 (avec ou sans BOM) d'abord, puis l'encodage Windows d'Excel.
    for encoding in ("utf-8-sig", "cp1252"):
        try:
            text = data.decode(encoding)
            break
        except UnicodeDecodeError:
            continue
    # Excel en français sépare par « ; », d'autres par « , » ou tabulation.
    first = text.splitlines()[0] if text else ""
    delimiter = max(";,\t", key=first.count)
    return [row for row in csv.reader(io.StringIO(text), delimiter=delimiter)]


def _cell(value):
    """Cellule nettoyée : texte sans espaces autour, vide → None."""
    if isinstance(value, str):
        value = value.strip()
    return None if value == "" else value


def _find_header(rows: list, aliases: dict, required: set, message: str):
    """La ligne de titres et, pour chaque colonne du fichier reconnue, la
    colonne de la base qu'elle remplit : (index de la ligne, {n° : colonne}).

    C'est la première des dix premières lignes qui porte l'un des titres
    `required` : un tableur a parfois un intitulé ou une ligne vide au-dessus.
    Sinon ValueError(message)."""
    for i, row in enumerate(rows[:10]):
        found = {}
        for j, cell in enumerate(row):
            key = _fold(str(cell or "")).strip()
            if key in aliases and aliases[key] not in found.values():
                found[j] = aliases[key]
        if required & set(found.values()):
            return i, found
    raise ValueError(message)


def _import_template(fmt: str, headers: list, widths: tuple, name: str, customize=None) -> Response:
    """Fichier modèle vide, en CSV ou en Excel : les titres de colonnes, rien
    d'autre — une ligne d'exemple risquerait d'être importée avec le reste.
    `customize(feuille)` ajoute au modèle Excel ses listes et formats."""
    if fmt == "csv":
        out = io.StringIO()
        csv.writer(out, delimiter=";").writerow(headers)
        # BOM : sans lui, Excel ouvre l'UTF-8 comme du Windows et « Payé »
        # devient « PayÃ© ».
        return Response(
            ("\ufeff" + out.getvalue()).encode("utf-8"), media_type="text/csv",
            headers={"Content-Disposition": f'attachment; filename="modele-{name}.csv"'},
        )
    if fmt == "xlsx":
        from openpyxl import Workbook
        from openpyxl.styles import Font
        wb = Workbook()
        ws = wb.active
        ws.title = name.capitalize()
        ws.append(headers)
        for cell in ws[1]:
            cell.font = Font(bold=True)
        for index, width in enumerate(widths):
            ws.column_dimensions[chr(ord("A") + index)].width = width
        ws.freeze_panes = "A2"
        if customize:
            customize(ws)
        out = io.BytesIO()
        wb.save(out)
        return Response(
            out.getvalue(),
            media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            headers={"Content-Disposition": f'attachment; filename="modele-{name}.xlsx"'},
        )
    raise HTTPException(status_code=404, detail="Format inconnu")


def _dropdown(ws, column: str, values: list):
    """Liste déroulante sur une colonne du modèle Excel, pour taper les
    valeurs que l'app connaît ; une autre reste possible (erreur non bloquante)."""
    from openpyxl.worksheet.datavalidation import DataValidation
    dv = DataValidation(type="list", formula1='"' + ",".join(values) + '"', showErrorMessage=False)
    dv.add(f"{column}2:{column}1000")
    ws.add_data_validation(dv)


async def _parse_expense_import(db, ship_id: int, filename: str, data: bytes) -> dict:
    """Lit le fichier et prépare l'aperçu : lignes valides, doublons ignorés,
    erreurs, et avertissements (valeur hors liste, contact à créer)."""
    rows = _read_sheet(filename, data)
    header_index, mapping = _find_header(
        rows, IMPORT_ALIASES, {"designation"},
        "Colonne « Objet » introuvable : la première ligne doit porter les titres "
        "du fichier modèle (Date, Montant, Objet…).",
    )

    cursor = await db.execute(
        "SELECT id, COALESCE(company, contact_name) FROM contacts WHERE ship_id = ?", (ship_id,)
    )
    contacts = {_fold(name): name for _, name in await cursor.fetchall() if name}
    cursor = await db.execute(
        "SELECT date, designation, unit_price FROM expenses WHERE ship_id = ?", (ship_id,)
    )
    existing = {
        (d, _fold(o or ""), round(m, 2) if m is not None else None)
        for d, o, m in await cursor.fetchall()
    }
    types = {_fold(t): t for t in EXPENSE_TYPES if t}
    payments = {_fold(m): m for m in PAYMENT_MODES}

    lines, new_suppliers = [], {}
    for number, row in enumerate(rows[header_index + 1:], start=header_index + 2):
        raw = {col: _cell(row[j]) if j < len(row) else None for j, col in mapping.items()}
        if all(v is None for v in raw.values()):
            continue   # ligne vide
        line = {"line": number, "errors": [], "notes": [], "status": "ok"}
        try:
            line["date"] = _import_date(raw.get("date"))
        except ValueError as exc:
            line["date"] = None
            line["errors"].append(f"date illisible « {exc} »")
        for col, label in (("unit_price", "montant"), ("paid", "payé")):
            try:
                line[col] = _import_amount(raw.get(col))
            except ValueError:
                line[col] = None
                line["errors"].append(f"{label} illisible « {raw.get(col)} »")
        line["designation"] = str(raw["designation"]) if raw.get("designation") is not None else None
        line["description"] = str(raw["description"]) if raw.get("description") is not None else None
        if not line["designation"]:
            line["errors"].append("objet manquant")
        if not line["date"] and not any("date" in e for e in line["errors"]):
            line["errors"].append("date manquante")

        # Type de frais et mode de paiement : une valeur de la liste est
        # ramenée à son orthographe exacte (« entretien » → « Entretien ») ;
        # une autre (« Refit », « Ricci ») est gardée telle quelle, et la fiche
        # sait l'afficher.
        for col, known, label in (("expense_type", types, "type"), ("payment", payments, "paiement")):
            value = raw.get(col)
            if value is None:
                line[col] = None
            elif _fold(str(value)) in known:
                line[col] = known[_fold(str(value))]
            else:
                line[col] = str(value)
                line["notes"].append(f"{label} « {value} » hors liste, gardé tel quel")

        supplier = raw.get("supplier")
        if supplier is None:
            line["supplier"] = None
        elif _fold(str(supplier)) in contacts:
            line["supplier"] = contacts[_fold(str(supplier))]
        else:
            # Le même fournisseur nouveau sur plusieurs lignes n'est créé
            # qu'une fois, sous l'orthographe de sa première apparition.
            line["supplier"] = new_suppliers.setdefault(_fold(str(supplier)), str(supplier))
            line["notes"].append(f"contact « {line['supplier']} » créé")

        if line["errors"]:
            line["status"] = "error"
        elif (line["date"], _fold(line["designation"]), line["unit_price"]) in existing:
            line["status"] = "duplicate"
        lines.append(line)

    return {
        "lines": lines,
        "valid": [l for l in lines if l["status"] == "ok"],
        "new_suppliers": sorted({
            l["supplier"] for l in lines
            if l["status"] == "ok" and l["supplier"] and _fold(l["supplier"]) not in contacts
        }),
    }


@app.get("/ship/expenses/import", response_class=HTMLResponse)
async def expense_import_form(request: Request):
    async with connect() as db:
        db.row_factory = aiosqlite.Row
        ship = await _fetch_ship(db, get_current_ship_id(request))
    return templates.TemplateResponse(
        "ship/expenses_import.html",
        {"request": request, "active_section": "ship",
         "current_ship": dict(ship) if ship else None,
         "columns": [c for c, _ in IMPORT_COLUMNS], "preview": None, "error": None},
    )


@app.get("/ship/expenses/import/modele.{fmt}")
async def expense_import_template(fmt: str):
    def customize(ws):
        _dropdown(ws, "D", [t for t in EXPENSE_TYPES if t])
        _dropdown(ws, "G", PAYMENT_MODES)
        for row in range(2, 1001):
            ws[f"A{row}"].number_format = "DD/MM/YYYY"
            for column in ("B", "F"):
                ws[f"{column}{row}"].number_format = "#,##0.00"
    return _import_template(fmt, [c for c, _ in IMPORT_COLUMNS],
                            (12, 12, 32, 16, 24, 12, 14, 40), "comptes", customize)


@app.post("/ship/expenses/import", response_class=HTMLResponse)
async def expense_import_preview(request: Request, file: Optional[UploadFile] = File(None)):
    """Premier temps : lire et montrer, sans rien enregistrer."""
    ship_id = get_current_ship_id(request)
    preview, error = None, None
    async with connect() as db:
        db.row_factory = aiosqlite.Row
        ship = await _fetch_ship(db, ship_id)
        db.row_factory = None
        if file is None or not file.filename:
            error = "Choisissez d'abord un fichier."
        else:
            data = await file.read(IMPORT_MAX_BYTES + 1)
            if len(data) > IMPORT_MAX_BYTES:
                error = "Fichier trop volumineux pour un tableau de comptes (plus de 5 Mo)."
            else:
                try:
                    preview = await _parse_expense_import(db, ship_id, file.filename, data)
                except ValueError as exc:
                    error = str(exc)
                except Exception:
                    error = "Ce fichier n'a pas pu être lu comme un tableur CSV ou Excel."
    return templates.TemplateResponse(
        "ship/expenses_import.html",
        {"request": request, "active_section": "ship",
         "current_ship": dict(ship) if ship else None,
         "columns": [c for c, _ in IMPORT_COLUMNS], "preview": preview, "error": error,
         "filename": file.filename if file else None,
         # Les lignes valides repartent avec la confirmation, déjà lues et
         # nettoyées : le fichier n'est ni gardé sur le Pi ni renvoyé.
         "payload": json.dumps(preview["valid"], ensure_ascii=False) if preview else None},
    )


@app.post("/ship/expenses/import/confirm")
async def expense_import_confirm(request: Request, payload: str = Form(...)):
    """Second temps : enregistrer les lignes confirmées."""
    ship_id = get_current_ship_id(request)
    lines = json.loads(payload)
    async with connect() as db:
        # Revérifiés ici : entre l'aperçu et la confirmation, une autre
        # saisie a pu créer le contact ou la dépense.
        cursor = await db.execute(
            "SELECT COALESCE(company, contact_name) FROM contacts WHERE ship_id = ?", (ship_id,)
        )
        known = {_fold(n) for (n,) in await cursor.fetchall() if n}
        cursor = await db.execute(
            "SELECT date, designation, unit_price FROM expenses WHERE ship_id = ?", (ship_id,)
        )
        existing = {(d, _fold(o or ""), round(m, 2) if m is not None else None)
                    for d, o, m in await cursor.fetchall()}
        for line in lines:
            key = (line.get("date"), _fold(line.get("designation") or ""), line.get("unit_price"))
            if key in existing or not line.get("designation"):
                continue
            existing.add(key)
            supplier = line.get("supplier")
            if supplier and _fold(supplier) not in known:
                await db.execute(
                    "INSERT INTO contacts (ship_id, company) VALUES (?, ?)", (ship_id, supplier)
                )
                known.add(_fold(supplier))
            balance, paid = _expense_balance(line.get("unit_price"), line.get("paid"))
            await db.execute(
                """INSERT INTO expenses (ship_id, date, designation, description, unit_price, paid, balance,
                                         expense_type, payment, supplier)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (ship_id, line.get("date"), line.get("designation"), line.get("description"),
                 line.get("unit_price"), paid, balance, line.get("expense_type"),
                 line.get("payment"), supplier),
            )
        await db.commit()
    return RedirectResponse(url="/ship/expenses", status_code=303)


@app.get("/ship/expenses/{entry_id}", response_class=HTMLResponse)
async def expense_detail(request: Request, entry_id: int):
    async with connect() as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute("SELECT * FROM expenses WHERE id = ?", (entry_id,))
        entry = await cursor.fetchone()
    if entry is None:
        raise HTTPException(status_code=404, detail="Expense not found")
    return await _expense_form(request, dict(entry))


# unit_type et category restent en base mais ne sont plus saisis : l'UPDATE ne
# les touche pas, si bien qu'une ligne ancienne garde ce qu'elle avait.
@app.post("/ship/expenses/{entry_id}/edit")
async def update_expense(
    entry_id: int,
    date: Optional[str] = Form(None),
    designation: Optional[str] = Form(None),
    description: Optional[str] = Form(None),
    unit_price: Optional[float] = Form(None),
    paid: Optional[float] = Form(None),
    expense_type: Optional[str] = Form(None),
    payment: Optional[str] = Form(None),
    supplier: Optional[str] = Form(None),
    document_file: Optional[UploadFile] = File(None),
):
    balance, paid = _expense_balance(unit_price, paid)
    document = await _save_document(document_file)
    async with connect() as db:
        # COALESCE : enregistrer la fiche sans choisir de fichier garde le
        # document déjà là, comme la photo d'un équipier.
        await db.execute(
            """UPDATE expenses SET date = ?, designation = ?, description = ?,
                   document_path = COALESCE(?, document_path), unit_price = ?, paid = ?,
                   balance = ?, expense_type = ?, payment = ?, supplier = ?
               WHERE id = ?""",
            (date or None, designation or None, description or None, document, unit_price, paid, balance,
             expense_type or None, payment or None,
             None if supplier == NEW_CONTACT else supplier or None, entry_id),
        )
        await db.commit()
    return _after_expense_save(entry_id, supplier)


@app.post("/ship/expenses/{entry_id}/delete")
async def delete_expense(entry_id: int):
    async with connect() as db:
        await db.execute("DELETE FROM expenses WHERE id = ?", (entry_id,))
        await db.commit()
    return RedirectResponse(url="/ship/expenses", status_code=303)


@app.get("/ship/todo", response_class=HTMLResponse)
async def ship_todo(request: Request):
    ship_id = get_current_ship_id(request)
    async with connect() as db:
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
async def new_todo_form(request: Request, next: Optional[str] = None):
    async with connect() as db:
        db.row_factory = aiosqlite.Row
        ship = await _fetch_ship(db, get_current_ship_id(request))
    return templates.TemplateResponse(
        "ship/todo_new.html",
        {
            "request": request,
            "active_section": "ship",
            "current_ship": dict(ship) if ship else None,
            # Où renvoie « Annuler » : la page d'où l'on vient (une route),
            # /ship/todo par défaut.
            "next": next,
        },
    )


@app.post("/ship/todo/new")
async def create_todo_item(
    request: Request,
    title: str = Form(...),
    task: Optional[str] = Form(None),
    urgent: Optional[str] = Form(None),
    due_date: Optional[str] = Form(None),
    photo_path: Optional[str] = Form(None),
    photo_file: Optional[UploadFile] = File(None),
    next: Optional[str] = Form(None),
):
    ship_id = get_current_ship_id(request)
    photo = await _save_photo(photo_file) or photo_path or None
    async with connect() as db:
        await db.execute(
            "INSERT INTO todo_items (ship_id, title, task, urgent, due_date, photo_path) VALUES (?, ?, ?, ?, ?, ?)",
            (ship_id, title, task or None, 1 if urgent else 0, due_date or None, photo),
        )
        await db.commit()
    # Retour à la page appelante (une route) quand le formulaire en portait une.
    return RedirectResponse(url=next or "/ship/todo", status_code=303)


@app.get("/ship/todo/{item_id}/edit", response_class=HTMLResponse)
async def edit_todo_form(request: Request, item_id: int, next: Optional[str] = None):
    async with connect() as db:
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
            # Idem : « Annuler » revient à la page appelante.
            "next": next,
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
    photo_path: Optional[str] = Form(None),
    photo_file: Optional[UploadFile] = File(None),
    next: Optional[str] = Form(None),
):
    # A newly chosen file wins over the text field, which still holds the old path.
    photo = await _save_photo(photo_file) or photo_path or None
    status = status or 'A faire'
    # Le statut commande la date de réalisation, et l'emporte sur la case du
    # formulaire : « À faire » l'efface — une tâche rouverte n'a pas de date de
    # réalisation — et « En cours » la met toujours à aujourd'hui. Seul
    # « Terminé » laisse la date saisie à la main.
    if status == 'A faire':
        completed_at = None
    elif status == 'En cours':
        completed_at = datetime.now().strftime("%Y-%m-%d")
    async with connect() as db:
        await db.execute(
            """UPDATE todo_items SET title=?, task=?, urgent=?, status=?, due_date=?, completed_at=?, photo_path=?
               WHERE id=?""",
            (title or None, task or None, 1 if urgent else 0, status, due_date or None,
             completed_at or None, photo, item_id),
        )
        await db.commit()
    # Idem : la route d'où l'on vient, /ship/todo sinon.
    return RedirectResponse(url=next or "/ship/todo", status_code=303)


@app.post("/ship/todo/{item_id}/done")
async def mark_todo_done(item_id: int, next: Optional[str] = Form(None)):
    today = datetime.now().strftime("%Y-%m-%d")
    async with connect() as db:
        await db.execute(
            "UPDATE todo_items SET status='Terminé', completed_at=? WHERE id=? AND status != 'Terminé'",
            (today, item_id),
        )
        await db.commit()
    return RedirectResponse(url=next or "/ship/todo", status_code=303)


@app.post("/ship/todo/{item_id}/undo")
async def undo_todo_item(item_id: int, next: Optional[str] = Form(None)):
    async with connect() as db:
        await db.execute(
            "UPDATE todo_items SET status='A faire', completed_at=NULL WHERE id=?",
            (item_id,),
        )
        await db.commit()
    return RedirectResponse(url=next or "/ship/todo", status_code=303)


@app.post("/ship/todo/{item_id}/delete")
async def delete_todo_item(item_id: int):
    async with connect() as db:
        await db.execute("DELETE FROM todo_items WHERE id = ?", (item_id,))
        await db.commit()
    return RedirectResponse(url="/ship/todo", status_code=303)


@app.get("/ship/fuel", response_class=HTMLResponse)
async def ship_fuel(request: Request):
    async with connect() as db:
        db.row_factory = aiosqlite.Row
        ship = await _fetch_ship(db, get_current_ship_id(request))
    return templates.TemplateResponse(
        "ship/fuel.html",
        {"request": request, "active_section": "ship", "current_ship": dict(ship) if ship else None},
    )


@app.get("/ship/contacts", response_class=HTMLResponse)
async def ship_contacts(request: Request):
    ship_id = get_current_ship_id(request)
    async with connect() as db:
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


async def _contact_places(db, ship_id: int) -> dict:
    """Localités et pays déjà saisis dans le carnet du navire, pour les listes
    déroulantes des fiches contact. Une valeur nouvelle, tapée via « Autre… »,
    y figure dès qu'elle est enregistrée."""
    places = {}
    for col, key in (("city", "cities"), ("country", "countries")):
        cursor = await db.execute(
            f"SELECT DISTINCT {col} FROM contacts WHERE ship_id = ? AND {col} IS NOT NULL "
            f"ORDER BY {col} COLLATE NOCASE",
            (ship_id,),
        )
        places[key] = [row[0] for row in await cursor.fetchall()]
    return places


@app.get("/ship/contacts/new", response_class=HTMLResponse)
async def new_contact_form(request: Request, expense_id: Optional[int] = None):
    """expense_id : on vient d'une dépense, qui recevra ce contact pour fournisseur."""
    ship_id = get_current_ship_id(request)
    async with connect() as db:
        db.row_factory = aiosqlite.Row
        ship = await _fetch_ship(db, ship_id)
        places = await _contact_places(db, ship_id)
    return templates.TemplateResponse(
        "ship/contacts_new.html",
        {"request": request, "active_section": "ship", "current_ship": dict(ship) if ship else None,
         "expense_id": expense_id, **places},
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
    street: Optional[str] = Form(None),
    postal_code: Optional[str] = Form(None),
    city: Optional[str] = Form(None),
    country: Optional[str] = Form(None),
    notes: Optional[str] = Form(None),
    expense_id: Optional[int] = Form(None),
):
    ship_id = get_current_ship_id(request)
    async with connect() as db:
        await db.execute(
            """INSERT INTO contacts (ship_id, company, contact_name, category, phone, email, website,
                                     street, postal_code, city, country, notes)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (ship_id, company or None, contact_name or None, category or None, phone or None,
             email or None, website or None, street or None, postal_code or None, city or None,
             country or None, notes or None),
        )
        if expense_id is not None:
            # expenses.supplier est un nom, pas un id : le même que celui que
            # le formulaire des comptes propose pour ce contact, et que
            # contact_detail recherche pour son historique des achats.
            await db.execute(
                "UPDATE expenses SET supplier = ? WHERE id = ? AND ship_id = ?",
                (company or contact_name or None, expense_id, ship_id),
            )
        await db.commit()
    if expense_id is not None:
        return RedirectResponse(url=f"/ship/expenses/{expense_id}", status_code=303)
    return RedirectResponse(url="/ship/contacts", status_code=303)


# ── Import du carnet d'adresses ──────────────────────────────────────────────
# Même principe que l'import des comptes (aperçu, puis confirmation), avec une
# règle de plus : un contact déjà au carnet n'est pas dupliqué, il est
# complété — seuls ses champs vides reçoivent la valeur du fichier, un champ
# rempli n'est jamais écrasé. Déclaré avant /ship/contacts/{contact_id}.

CONTACT_IMPORT_COLUMNS = [
    ("Société", "company"), ("Contact", "contact_name"), ("Catégorie", "category"),
    ("Téléphone", "phone"), ("Email", "email"), ("Site web", "website"),
    ("Rue et numéro", "street"), ("Code postal", "postal_code"), ("Localité", "city"),
    ("Pays", "country"), ("Notes", "notes"),
]
CONTACT_FIELD_LABELS = {col: label.lower() for label, col in CONTACT_IMPORT_COLUMNS}
CONTACT_IMPORT_ALIASES = {
    "societe": "company", "entreprise": "company", "raison sociale": "company", "company": "company",
    "contact": "contact_name", "nom du contact": "contact_name", "nom": "contact_name",
    "personne de contact": "contact_name",
    "categorie": "category",
    "telephone": "phone", "tel": "phone", "tel.": "phone", "gsm": "phone", "mobile": "phone",
    "portable": "phone", "phone": "phone",
    "email": "email", "e-mail": "email", "mail": "email", "courriel": "email",
    "site web": "website", "site": "website", "web": "website", "site internet": "website",
    "rue et numero": "street", "rue": "street", "adresse": "street",
    "code postal": "postal_code", "cp": "postal_code",
    "localite": "city", "ville": "city", "commune": "city",
    "pays": "country",
    "notes": "notes", "remarques": "notes", "commentaire": "notes", "commentaires": "notes",
}
CONTACT_DATA_FIELDS = [col for _, col in CONTACT_IMPORT_COLUMNS if col not in ("company", "contact_name")]


def _contact_key(company, contact_name):
    """Ce qui identifie un contact : sa société, à défaut le nom du contact,
    replié — la même règle que expenses.supplier, qui retient ce nom-là."""
    return _fold(company or contact_name or "").strip()


def _import_text(value):
    """Cellule en texte : un code postal ou un téléphone lu comme nombre par
    Excel (1000.0) redevient « 1000 »."""
    if value is None:
        return None
    if isinstance(value, float) and value.is_integer():
        value = int(value)
    text = str(value).strip()
    return text or None


async def _parse_contact_import(db, ship_id: int, filename: str, data: bytes) -> dict:
    rows = _read_sheet(filename, data)
    header_index, mapping = _find_header(
        rows, CONTACT_IMPORT_ALIASES, {"company", "contact_name"},
        "Colonne « Société » ou « Contact » introuvable : la première ligne doit porter les "
        "titres du fichier modèle (Société, Contact, Catégorie…).",
    )
    db.row_factory = aiosqlite.Row
    cursor = await db.execute("SELECT * FROM contacts WHERE ship_id = ?", (ship_id,))
    existing = {}
    for r in await cursor.fetchall():
        existing.setdefault(_contact_key(r["company"], r["contact_name"]), dict(r))
    db.row_factory = None
    categories = {_fold(c): c for c in CONTACT_CATEGORIES}

    # Une entrée par contact : deux lignes de la même société dans le fichier
    # se fondent en une, la première valeur de chaque champ l'emportant.
    entries, order, errors = {}, [], []
    for number, row in enumerate(rows[header_index + 1:], start=header_index + 2):
        raw = {col: _import_text(_cell(row[j])) if j < len(row) else None for j, col in mapping.items()}
        if all(v is None for v in raw.values()):
            continue
        key = _contact_key(raw.get("company"), raw.get("contact_name"))
        if not key:
            errors.append({"line": number, "status": "error", "errors": ["ni société ni contact"],
                           "values": raw, "notes": [], "fills": [], "conflicts": []})
            continue
        cat = raw.get("category")
        if cat and _fold(cat) in categories:
            raw["category"] = categories[_fold(cat)]
        if key not in entries:
            entries[key] = {"lines": [number], "values": raw}
            order.append(key)
        else:
            entries[key]["lines"].append(number)
            for col, value in raw.items():
                if value is not None and entries[key]["values"].get(col) is None:
                    entries[key]["values"][col] = value

    lines = []
    for key in order:
        entry = entries[key]
        values = entry["values"]
        line = {"line": ", ".join(map(str, entry["lines"])), "values": values,
                "errors": [], "notes": [], "fills": [], "conflicts": []}
        if len(entry["lines"]) > 1:
            line["notes"].append(f"{len(entry['lines'])} lignes du fichier réunies")
        current = existing.get(key)
        if current is None:
            line["status"] = "new"
        else:
            line["id"] = current["id"]
            # Seuls les champs vides au carnet reçoivent la valeur du fichier ;
            # une valeur différente sur un champ rempli est signalée, pas écrite.
            for col in CONTACT_DATA_FIELDS + ["company", "contact_name"]:
                value = values.get(col)
                if value is None:
                    continue
                stored = current.get(col)
                if stored in (None, ""):
                    line["fills"].append(col)
                elif _fold(str(stored)).strip() != _fold(value).strip():
                    line["conflicts"].append(f"{CONTACT_FIELD_LABELS[col]} : « {stored} » conservé")
            line["status"] = "update" if line["fills"] else "same"
            line["stored"] = {k: current.get(k) for k in ("company", "contact_name")}
        lines.append(line)
    lines += errors
    lines.sort(key=lambda l: int(str(l["line"]).split(",")[0]))
    return {"lines": lines,
            "actions": [l for l in lines if l["status"] in ("new", "update")]}


@app.get("/ship/contacts/import", response_class=HTMLResponse)
async def contact_import_form(request: Request):
    async with connect() as db:
        db.row_factory = aiosqlite.Row
        ship = await _fetch_ship(db, get_current_ship_id(request))
    return templates.TemplateResponse(
        "ship/contacts_import.html",
        {"request": request, "active_section": "ship", "current_ship": dict(ship) if ship else None,
         "columns": [c for c, _ in CONTACT_IMPORT_COLUMNS], "labels": CONTACT_FIELD_LABELS,
         "preview": None, "error": None},
    )


@app.get("/ship/contacts/import/modele.{fmt}")
async def contact_import_template(fmt: str):
    def customize(ws):
        _dropdown(ws, "C", CONTACT_CATEGORIES)
        # Texte et non nombre : sans quoi Excel mange le zéro de tête d'un
        # code postal (01000) et le + d'un téléphone.
        for row in range(2, 1001):
            for column in ("D", "H"):
                ws[f"{column}{row}"].number_format = "@"
    return _import_template(fmt, [c for c, _ in CONTACT_IMPORT_COLUMNS],
                            (28, 22, 20, 18, 28, 26, 28, 12, 18, 14, 40), "contacts", customize)


@app.post("/ship/contacts/import", response_class=HTMLResponse)
async def contact_import_preview(request: Request, file: Optional[UploadFile] = File(None)):
    """Premier temps : lire et montrer, sans rien enregistrer."""
    ship_id = get_current_ship_id(request)
    preview, error = None, None
    async with connect() as db:
        db.row_factory = aiosqlite.Row
        ship = await _fetch_ship(db, ship_id)
        if file is None or not file.filename:
            error = "Choisissez d'abord un fichier."
        else:
            data = await file.read(IMPORT_MAX_BYTES + 1)
            if len(data) > IMPORT_MAX_BYTES:
                error = "Fichier trop volumineux pour un carnet d'adresses (plus de 5 Mo)."
            else:
                try:
                    preview = await _parse_contact_import(db, ship_id, file.filename, data)
                except ValueError as exc:
                    error = str(exc)
                except Exception:
                    error = "Ce fichier n'a pas pu être lu comme un tableur CSV ou Excel."
    return templates.TemplateResponse(
        "ship/contacts_import.html",
        {"request": request, "active_section": "ship", "current_ship": dict(ship) if ship else None,
         "columns": [c for c, _ in CONTACT_IMPORT_COLUMNS], "labels": CONTACT_FIELD_LABELS,
         "preview": preview, "error": error, "filename": file.filename if file else None,
         "payload": json.dumps(
             [{"id": l.get("id"), "values": l["values"]} for l in preview["actions"]],
             ensure_ascii=False) if preview else None},
    )


@app.post("/ship/contacts/import/confirm")
async def contact_import_confirm(request: Request, payload: str = Form(...)):
    """Second temps : créer les nouveaux contacts, compléter les autres."""
    ship_id = get_current_ship_id(request)
    fields = ["company", "contact_name"] + CONTACT_DATA_FIELDS
    async with connect() as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute("SELECT id, company, contact_name FROM contacts WHERE ship_id = ?", (ship_id,))
        known = {}
        for r in await cursor.fetchall():
            known.setdefault(_contact_key(r["company"], r["contact_name"]), r["id"])
        for action in json.loads(payload):
            values = {f: action["values"].get(f) for f in fields}
            key = _contact_key(values["company"], values["contact_name"])
            if not key:
                continue
            # Revérifié ici : le contact a pu être créé entre l'aperçu et la
            # confirmation, auquel cas on le complète au lieu de le doubler.
            contact_id = known.get(key)
            if contact_id is None:
                cols = [f for f in fields if values[f] is not None]
                cursor = await db.execute(
                    f"INSERT INTO contacts (ship_id, {', '.join(cols)}) VALUES (?{', ?' * len(cols)})",
                    (ship_id, *[values[c] for c in cols]),
                )
                known[key] = cursor.lastrowid
            else:
                # Champ par champ, et seulement s'il est vide *au moment de
                # l'écriture* : rien de ce qui est au carnet n'est écrasé.
                for f in fields:
                    if values[f] is not None:
                        await db.execute(
                            f"UPDATE contacts SET {f} = ? WHERE id = ? AND ship_id = ? "
                            f"AND ({f} IS NULL OR {f} = '')",
                            (values[f], contact_id, ship_id),
                        )
        await db.commit()
    return RedirectResponse(url="/ship/contacts", status_code=303)


@app.get("/ship/contacts/{contact_id}", response_class=HTMLResponse)
async def contact_detail(request: Request, contact_id: int):
    ship_id = get_current_ship_id(request)
    async with connect() as db:
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
        places = await _contact_places(db, ship_id)
    return templates.TemplateResponse(
        "ship/contacts_detail.html",
        {
            "request": request, "active_section": "ship",
            "current_ship": dict(ship) if ship else None,
            "contact": contact,
            "purchases": [dict(p) for p in purchases],
            **places,
        },
    )


# Catégories proposées pour un contact, à la création comme sur la fiche.
# Valeurs *stockées*, comme EXPENSE_TYPES.
CONTACT_CATEGORIES = [
    "Gréement / Voiles", "Moteur", "Électronique", "Accastillage", "Chantier naval",
    "Port / Capitainerie", "Assurance", "Carburant", "Autre",
]
templates.env.globals["contact_categories"] = CONTACT_CATEGORIES

# Champs de la fiche contact modifiables sur place. Le nom est interpolé dans
# l'UPDATE : il doit venir de cet ensemble, jamais directement de l'URL — même
# parti que EDITABLE_LINE_FIELDS.
EDITABLE_CONTACT_FIELDS = {
    "company", "contact_name", "category", "phone", "email", "website",
    "street", "postal_code", "city", "country", "notes",
}


@app.post("/ship/contacts/{contact_id}/field/{field}")
async def update_contact_field(contact_id: int, field: str, value: Optional[str] = Form(None)):
    """Modification sur place d'un champ de la fiche contact."""
    if field not in EDITABLE_CONTACT_FIELDS:
        raise HTTPException(status_code=404, detail="Field not editable")
    value = (value or "").strip() or None
    async with connect() as db:
        cursor = await db.execute(
            "SELECT ship_id, company, contact_name FROM contacts WHERE id = ?", (contact_id,)
        )
        row = await cursor.fetchone()
        if row is None:
            raise HTTPException(status_code=404, detail="Contact not found")
        ship_id, company, contact_name = row
        old_name = company or contact_name
        await db.execute(f"UPDATE contacts SET {field} = ? WHERE id = ?", (value, contact_id))
        # expenses.supplier retient le *nom* du contact (société, à défaut
        # nom du contact), pas son id. Renommer sans reporter le nom sur ses
        # dépenses viderait son historique des achats.
        if field in ("company", "contact_name"):
            if field == "company":
                company = value
            else:
                contact_name = value
            new_name = company or contact_name
            if old_name and new_name and new_name != old_name:
                await db.execute(
                    "UPDATE expenses SET supplier = ? WHERE ship_id = ? AND supplier = ?",
                    (new_name, ship_id, old_name),
                )
        await db.commit()
    return RedirectResponse(url=f"/ship/contacts/{contact_id}", status_code=303)


# ── Crew (équipiers) ──────────────────────────────────────────────────────────

# Deliberately *not* ship-scoped: crew_members carries no ship_id, because the
# same person may sail on more than one boat. This is the one list in the app
# that ignores the ship cookie. Which cruise someone embarked on lives in
# cruise_crew — no screen for that yet.
#
# `age` is never written: _age computes it from birth_date at display time.

# Dial codes offered next to the phone number. Belgium first — the boat's own
# flag, so the usual case — then the cruising grounds by country name. The stored
# column stays a single string ("+32 470 12 34 56"): _phone_parts splits it back
# into the two boxes, _join_phone puts it together. Add a country here and old
# rows keep working; remove one and numbers using it stop being recognised, so
# they fall back to the plain-number box.
DIAL_CODES = [
    ("+32", "Belgique"),
    ("+49", "Allemagne"),
    ("+34", "Espagne"),
    ("+33", "France"),
    ("+30", "Grèce"),
    ("+385", "Croatie"),
    ("+353", "Irlande"),
    ("+39", "Italie"),
    ("+352", "Luxembourg"),
    ("+356", "Malte"),
    ("+212", "Maroc"),
    ("+377", "Monaco"),
    ("+31", "Pays-Bas"),
    ("+351", "Portugal"),
    ("+44", "Royaume-Uni"),
    ("+41", "Suisse"),
    ("+216", "Tunisie"),
    ("+90", "Turquie"),
    ("+1", "USA / Canada"),
]


def _phone_parts(value):
    """Stored phone → the two form boxes, as a dict.

    Longest code first: `+33` is a prefix of `+351`, so a shortest-first match
    would read a Portuguese number as French and leave a stray `1` in the number.
    An unrecognised prefix is left whole in the number box rather than guessed at.
    """
    if not value:
        return {"code": "", "number": ""}
    s = str(value).strip()
    for code, _ in sorted(DIAL_CODES, key=lambda c: -len(c[0])):
        if s.startswith(code):
            return {"code": code, "number": s[len(code):].strip()}
    return {"code": "", "number": s}


def _join_phone(code, number):
    """The two boxes → the stored string. A lone country code is not a phone
    number, so it stores nothing; a number without its code still does.

    A Belgian number is regrouped `xxx xx xx xx`, so the column holds one shape
    rather than whatever spacing was typed. Only for exactly nine digits — the
    standard national number once the leading 0 is dropped. Eight digits is a
    Brussels landline (`2 512 34 56`), ten means the 0 was typed anyway; both
    are left as entered rather than mangled into the wrong grouping.
    """
    code, number = (code or "").strip(), (number or "").strip()
    if not number:
        return None
    if code == "+32":
        digits = "".join(c for c in number if c.isdigit())
        if len(digits) == 9:
            number = f"{digits[:3]} {digits[3:5]} {digits[5:7]} {digits[7:]}"
    return f"{code} {number}" if code else number


GENDERS = ["Femme", "Homme", "Non-binaire"]
# Valeurs *stockées*, comme GENDERS : les changer demande de reprendre les fiches.
ID_TYPES = ["Carte d'identité", "Passeport"]


def _equipier(gender, det=None):
    """« équipier » accordé au genre, avec son déterminant : `gender | equipier('cet')`.

    Le déterminant est demandé explicitement plutôt que collé devant le mot par
    la template, parce que c'est lui qui s'accorde aussi (cet/cette, un/une).
    Sans argument, le nom seul. `'le'` s'élide en « l' » aux trois genres, la
    voyelle initiale ne laissant pas le choix.

    « Non-binaire » prend l'écriture inclusive avec point médian (« cette·te »
    n'existant pas, c'est la forme la plus lisible sans forme neutre établie en
    français). Genre non renseigné : masculin, comme le reste de l'app (l'onglet
    « Équipiers », le bouton « + Nouvel équipier »).

    Le point médian « · » (U+00B7) et non un point ordinaire : c'est celui que
    les lecteurs d'écran savent ignorer.
    """
    # Index dans les triplets ci-dessous : masculin, féminin, inclusif.
    i = {"Femme": 1, "Non-binaire": 2}.get(gender, 0)
    noun = ("équipier", "équipière", "équipier·ère")[i]
    if det is None:
        return noun
    if det == "le":
        return f"l'{noun}"
    determiners = {
        "un": ("un", "une", "un·e"),
        "cet": ("cet", "cette", "cet·te"),
    }
    return f"{determiners[det][i]} {noun}"


templates.env.filters["phoneparts"] = _phone_parts
templates.env.filters["equipier"] = _equipier
# Même raison que dial_codes : liste de référence fixe, exposée en global plutôt
# que passée dans le contexte de chaque handler qui affiche le formulaire.
templates.env.globals["genders"] = GENDERS
templates.env.globals["id_types"] = ID_TYPES
# Exposed as a global rather than threaded through every handler's context: it is
# a fixed reference list, and the next form that needs it gets it for free.
templates.env.globals["dial_codes"] = DIAL_CODES


@app.get("/crew", response_class=HTMLResponse)
async def crew_list(request: Request):
    async with connect() as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute(
            "SELECT * FROM crew_members ORDER BY last_name COLLATE NOCASE, first_name COLLATE NOCASE"
        )
        crew = await cursor.fetchall()
    return templates.TemplateResponse(
        "crew/list.html",
        {"request": request, "active_section": "crew", "crew": [dict(m) for m in crew]},
    )


@app.get("/crew/new", response_class=HTMLResponse)
async def new_crew_form(request: Request):
    return templates.TemplateResponse(
        "crew/form.html",
        {"request": request, "active_section": "crew", "member": None},
    )


@app.post("/crew/new")
async def create_crew(
    request: Request,
    first_name: Optional[str] = Form(None),
    last_name: Optional[str] = Form(None),
    gender: Optional[str] = Form(None),
    birth_date: Optional[str] = Form(None),
    birth_place: Optional[str] = Form(None),
    nationality: Optional[str] = Form(None),
    street: Optional[str] = Form(None),
    postal_code: Optional[str] = Form(None),
    city: Optional[str] = Form(None),
    id_type: Optional[str] = Form(None),
    id_number: Optional[str] = Form(None),
    phone_code: Optional[str] = Form(None),
    phone: Optional[str] = Form(None),
    email: Optional[str] = Form(None),
    photo_file: Optional[UploadFile] = File(None),
    photo_path: Optional[str] = Form(None),
):
    phone = _join_phone(phone_code, phone)
    photo = await _save_crew_photo(photo_file) or photo_path or None
    async with connect() as db:
        await db.execute(
            """INSERT INTO crew_members
               (first_name, last_name, gender, birth_date, birth_place, nationality,
                street, postal_code, city, id_type, id_number, phone, email, photo_path)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (first_name or None, last_name or None, gender or None, birth_date or None,
             birth_place or None, nationality or None, street or None, postal_code or None,
             city or None, id_type or None, id_number or None, phone or None, email or None,
             photo),
        )
        await db.commit()
    return RedirectResponse(url="/crew", status_code=303)


@app.get("/crew/{crew_id}", response_class=HTMLResponse)
async def crew_detail(request: Request, crew_id: int):
    async with connect() as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute("SELECT * FROM crew_members WHERE id = ?", (crew_id,))
        member = await cursor.fetchone()
        # The other side of cruise_crew: not this cruise's crew, but this
        # person's cruises. Across every ship, since crew are not ship-scoped.
        cursor = await db.execute(
            # LEFT JOIN sur le navire : c'est justement parce que la personne
            # peut embarquer sur plusieurs bateaux que la colonne est utile, et
            # cruises.ship_id peut être nul sur une croisière d'avant le
            # rattachement aux navires.
            f"""SELECT cc.role, cc.embark_date, cc.disembark_date,
                       c.id AS cruise_id, c.name AS cruise_name,
                       c.start_time, c.end_time, {CRUISE_NUMBER} AS number,
                       s.name AS ship_name
                FROM cruise_crew cc
                JOIN cruises c ON cc.cruise_id = c.id
                LEFT JOIN ship_info s ON c.ship_id = s.id
                WHERE cc.crew_member_id = ?
                ORDER BY COALESCE(c.start_time, c.created_at) DESC""",
            (crew_id,),
        )
        cruises = await cursor.fetchall()
        # Précédent / Suivant dans l'ordre de la liste /crew — même ORDER BY,
        # sinon les boutons sauteraient d'une fiche à l'autre dans un ordre que
        # l'écran ne montre nulle part. Ids triés en Python plutôt qu'en SQL :
        # un nom nul ou deux homonymes rendent un « WHERE nom > ? » faux.
        cursor = await db.execute(
            "SELECT id FROM crew_members ORDER BY last_name COLLATE NOCASE, first_name COLLATE NOCASE"
        )
        ids = [r[0] for r in await cursor.fetchall()]
        # Son compte éventuel (sans compte : Mousse), et s'il existe déjà un
        # compte quelque part — sinon la fiche propose « Devenir Amiral ».
        cursor = await db.execute(
            "SELECT id, username, rank FROM users WHERE crew_member_id = ?", (crew_id,))
        account = await cursor.fetchone()
        cursor = await db.execute("SELECT COUNT(*) FROM users")
        has_users = (await cursor.fetchone())[0] > 0
    if member is None:
        raise HTTPException(status_code=404, detail="Crew member not found")
    i = ids.index(crew_id)
    return templates.TemplateResponse(
        "crew/detail.html",
        {
            "request": request,
            "active_section": "crew",
            "member": dict(member),
            "account": dict(account) if account else None,
            "has_users": has_users,
            "ranks": RANKS,
            "erreur": request.query_params.get("erreur"),
            "info": request.query_params.get("info"),
            "cruises": [dict(c) for c in cruises],
            "crew_fields": CREW_FIELDS, "crew_choices": CREW_CHOICES,
            "prev_id": ids[i - 1] if i > 0 else None,
            "next_id": ids[i + 1] if i + 1 < len(ids) else None,
        },
    )


@app.get("/crew/{crew_id}/edit", response_class=HTMLResponse)
async def edit_crew_form(request: Request, crew_id: int):
    async with connect() as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute("SELECT * FROM crew_members WHERE id = ?", (crew_id,))
        member = await cursor.fetchone()
    if member is None:
        raise HTTPException(status_code=404, detail="Crew member not found")
    return templates.TemplateResponse(
        "crew/form.html",
        {"request": request, "active_section": "crew", "member": dict(member)},
    )


@app.post("/crew/{crew_id}/edit")
async def update_crew(
    request: Request,
    crew_id: int,
    first_name: Optional[str] = Form(None),
    last_name: Optional[str] = Form(None),
    gender: Optional[str] = Form(None),
    birth_date: Optional[str] = Form(None),
    birth_place: Optional[str] = Form(None),
    nationality: Optional[str] = Form(None),
    street: Optional[str] = Form(None),
    postal_code: Optional[str] = Form(None),
    city: Optional[str] = Form(None),
    id_type: Optional[str] = Form(None),
    id_number: Optional[str] = Form(None),
    phone_code: Optional[str] = Form(None),
    phone: Optional[str] = Form(None),
    email: Optional[str] = Form(None),
    photo_file: Optional[UploadFile] = File(None),
    photo_path: Optional[str] = Form(None),
):
    # Same field list as create_crew — a new column has to be added to both.
    phone = _join_phone(phone_code, phone)
    photo = await _save_crew_photo(photo_file) or photo_path or None
    async with connect() as db:
        await db.execute(
            """UPDATE crew_members SET
                   first_name = ?, last_name = ?, gender = ?, birth_date = ?, birth_place = ?,
                   nationality = ?, street = ?, postal_code = ?, city = ?,
                   id_type = ?, id_number = ?, phone = ?, email = ?,
                   -- COALESCE : soumettre le formulaire sans toucher à la photo
                   -- ne doit pas effacer celle déjà enregistrée.
                   photo_path = COALESCE(?, photo_path)
               WHERE id = ?""",
            (first_name or None, last_name or None, gender or None, birth_date or None,
             birth_place or None, nationality or None, street or None, postal_code or None,
             city or None, id_type or None, id_number or None, phone or None, email or None,
             photo, crew_id),
        )
        await db.commit()
    return RedirectResponse(url=f"/crew/{crew_id}", status_code=303)


# Champs de la fiche équipier modifiables sur place, et leur type. Le nom est
# interpolé dans l'UPDATE : il doit venir de ce dictionnaire, jamais de l'URL
# telle quelle. Le téléphone et la photo ont leurs propres routes, plus bas :
# l'un se recompose de deux cases, l'autre est un fichier.
CREW_FIELDS = {
    **{f: "text" for f in (
        "first_name", "last_name", "birth_place", "nationality",
        "street", "postal_code", "city", "id_number", "email",
    )},
    "birth_date": "date",
    "gender": "select",
    "id_type": "select",
}
CREW_CHOICES = {"gender": GENDERS, "id_type": ID_TYPES}


@app.post("/crew/{crew_id}/field/{field}")
async def update_crew_field(crew_id: int, field: str, value: Optional[str] = Form(None)):
    """Modification sur place d'un champ de la fiche équipier."""
    if field not in CREW_FIELDS:
        raise HTTPException(status_code=404, detail="Field not editable")
    value = (value or "").strip() or None
    async with connect() as db:
        await db.execute(f"UPDATE crew_members SET {field} = ? WHERE id = ?", (value, crew_id))
        await db.commit()
    return RedirectResponse(url=f"/crew/{crew_id}", status_code=303)


@app.post("/crew/{crew_id}/phone")
async def update_crew_phone(crew_id: int, phone_code: Optional[str] = Form(None),
                            phone: Optional[str] = Form(None)):
    """Le téléphone, en deux cases (indicatif, numéro) comme dans le formulaire,
    recomposées par _join_phone."""
    async with connect() as db:
        await db.execute("UPDATE crew_members SET phone = ? WHERE id = ?",
                         (_join_phone(phone_code, phone), crew_id))
        await db.commit()
    return RedirectResponse(url=f"/crew/{crew_id}", status_code=303)


@app.post("/crew/{crew_id}/photo")
async def update_crew_photo(crew_id: int, photo_file: Optional[UploadFile] = File(None)):
    """Nouvelle photo, choisie d'un clic sur celle de la fiche. L'ancienne reste
    dans IMG/SailingCrew/, comme toute photo remplacée."""
    photo = await _save_crew_photo(photo_file)
    if photo:
        async with connect() as db:
            await db.execute("UPDATE crew_members SET photo_path = ? WHERE id = ?", (photo, crew_id))
            await db.commit()
    return RedirectResponse(url=f"/crew/{crew_id}", status_code=303)


@app.post("/crew/{crew_id}/delete")
async def delete_crew(crew_id: int):
    async with connect() as db:
        # Supprimer la fiche de l'Amiral emporterait son compte (cascade) et
        # laisserait l'app sans Amiral : on nomme d'abord quelqu'un d'autre.
        cursor = await db.execute(
            "SELECT 1 FROM users WHERE crew_member_id = ? AND rank = 'amiral'", (crew_id,))
        if await cursor.fetchone():
            error = "C'est la fiche de l'Amiral : nommez d'abord un autre Amiral pour pouvoir la supprimer."
            return RedirectResponse(url=f"/crew/{crew_id}?erreur={quote(error)}#acces", status_code=303)
        await db.execute("DELETE FROM crew_members WHERE id = ?", (crew_id,))
        await db.commit()
    return RedirectResponse(url="/crew", status_code=303)


# ── Crew aboard a cruise (équipage) ───────────────────────────────────────────

# cruise_crew.role holds 'skipper' or 'crew' — English, matching the column's
# own DEFAULT, and translated only for display. There is at most one skipper per
# cruise, enforced by demoting the others in set_skipper rather than by the
# schema.


async def _cruise_crew(db, cruise_id: int):
    """Who is aboard this cruise, and who could still be added.

    Both cruise-page handlers need this, so it lives here rather than in each.
    The skipper sorts first. `available` excludes people already aboard, which
    is what keeps duplicates out — cruise_crew has no UNIQUE(cruise_id,
    crew_member_id), so nothing else would.
    """
    cursor = await db.execute(
        """SELECT cc.id, cc.role, cc.embark_date, cc.disembark_date,
                  CAST(julianday(substr(cc.disembark_date, 1, 10))
                       - julianday(substr(cc.embark_date, 1, 10)) AS INTEGER) AS nights,
                  m.id AS member_id, m.first_name, m.last_name, m.gender, m.photo_path
           FROM cruise_crew cc
           JOIN crew_members m ON cc.crew_member_id = m.id
           WHERE cc.cruise_id = ?
           ORDER BY CASE WHEN cc.role = 'skipper' THEN 0 ELSE 1 END,
                    m.last_name COLLATE NOCASE, m.first_name COLLATE NOCASE""",
        (cruise_id,),
    )
    aboard = await cursor.fetchall()
    cursor = await db.execute(
        """SELECT id, first_name, last_name FROM crew_members
           WHERE id NOT IN (SELECT crew_member_id FROM cruise_crew WHERE cruise_id = ?)
           ORDER BY last_name COLLATE NOCASE, first_name COLLATE NOCASE""",
        (cruise_id,),
    )
    available = await cursor.fetchall()
    return [dict(r) for r in aboard], [dict(r) for r in available]


@app.post("/cruises/{cruise_id}/crew")
async def add_cruise_crew(
    cruise_id: int,
    crew_member_id: Optional[int] = Form(None),
    embark_date: Optional[str] = Form(None),
    disembark_date: Optional[str] = Form(None),
):
    if crew_member_id is None:
        return RedirectResponse(url=f"/cruises/{cruise_id}", status_code=303)
    async with connect() as db:
        # Someone is either aboard or not. The form only offers people who are
        # not, but the check belongs here too: without a UNIQUE(cruise_id,
        # crew_member_id) nothing else stops a second row, and a duplicate shows
        # up as the same person listed twice, once per role.
        cursor = await db.execute(
            "SELECT 1 FROM cruise_crew WHERE cruise_id = ? AND crew_member_id = ?",
            (cruise_id, crew_member_id),
        )
        if await cursor.fetchone():
            return RedirectResponse(url=f"/cruises/{cruise_id}", status_code=303)
        # First aboard is the skipper by default — a boat does not sail without
        # one, and it saves designating them by hand in the common case.
        cursor = await db.execute(
            "SELECT COUNT(*) FROM cruise_crew WHERE cruise_id = ?", (cruise_id,)
        )
        role = "crew" if (await cursor.fetchone())[0] else "skipper"
        await db.execute(
            """INSERT INTO cruise_crew (cruise_id, crew_member_id, role, embark_date, disembark_date)
               VALUES (?, ?, ?, ?, ?)""",
            (cruise_id, crew_member_id, role, embark_date or None, disembark_date or None),
        )
        await db.commit()
    return RedirectResponse(url=f"/cruises/{cruise_id}", status_code=303)


@app.post("/cruises/{cruise_id}/crew/{assignment_id}/skipper")
async def set_skipper(cruise_id: int, assignment_id: int):
    async with connect() as db:
        # One skipper per cruise: demote first, promote second, same transaction.
        await db.execute(
            "UPDATE cruise_crew SET role = 'crew' WHERE cruise_id = ?", (cruise_id,)
        )
        await db.execute(
            "UPDATE cruise_crew SET role = 'skipper' WHERE id = ? AND cruise_id = ?",
            (assignment_id, cruise_id),
        )
        await db.commit()
    return RedirectResponse(url=f"/cruises/{cruise_id}", status_code=303)


# Dates an embarkation may edit in place. Like EDITABLE_LINE_FIELDS, the name is
# interpolated into the UPDATE, so this set is what keeps the URL out of SQL.
EDITABLE_CREW_DATES = {"embark_date", "disembark_date"}


@app.post("/cruises/{cruise_id}/crew/{assignment_id}/date/{field}")
async def set_cruise_crew_date(
    cruise_id: int,
    assignment_id: int,
    field: str,
    value: Optional[str] = Form(None),
):
    """Inline edit of one embarkation date from the cruise page's crew panel."""
    if field not in EDITABLE_CREW_DATES:
        raise HTTPException(status_code=404, detail="Field not editable")
    async with connect() as db:
        await db.execute(
            f"UPDATE cruise_crew SET {field} = ? WHERE id = ? AND cruise_id = ?",
            (value or None, assignment_id, cruise_id),
        )
        await db.commit()
    return RedirectResponse(url=f"/cruises/{cruise_id}", status_code=303)


@app.post("/cruises/{cruise_id}/crew/{assignment_id}/delete")
async def remove_cruise_crew(cruise_id: int, assignment_id: int):
    """Takes someone off this cruise. Their crew_members fiche is untouched."""
    async with connect() as db:
        await db.execute(
            "DELETE FROM cruise_crew WHERE id = ? AND cruise_id = ?",
            (assignment_id, cruise_id),
        )
        await db.commit()
    return RedirectResponse(url=f"/cruises/{cruise_id}", status_code=303)


# ── Cruises ───────────────────────────────────────────────────────────────────

# What the interface shows as "001" is a position, not the primary key. Ids come
# from AUTOINCREMENT and are never reused, so after deleting every cruise the
# next one would display 004. Counting predecessors instead keeps the sequence
# at 1..N with no gaps: delete one and those after it shift down. Ids stay
# untouched underneath, so links and foreign keys still resolve.
# Each expects its table aliased as c (cruises) or r (routes).
#
# Cruises are counted in order of their start date (CRUISE_ORDER: start date,
# else creation date, then id to break ties), not of creation: a cruise
# imported after the fact (FileMaker) takes its place in time, and the ones
# after it shift up. The list, and the prev/next arrows of the cruise page
# (_cruise_neighbours), follow the same order.
CRUISE_KEY = "COALESCE({a}.start_time, {a}.created_at)"
CRUISE_ORDER = "COALESCE(c.start_time, c.created_at), c.id"
CRUISE_NUMBER = ("(SELECT COUNT(*) FROM cruises c2 WHERE c2.ship_id IS c.ship_id"
                 f" AND ({CRUISE_KEY.format(a='c2')}, c2.id) <= ({CRUISE_KEY.format(a='c')}, c.id))")
ROUTE_NUMBER = ("(SELECT COUNT(*) FROM routes r2"
                " WHERE r2.cruise_id IS r.cruise_id AND r2.id <= r.id)")

# Where a cruise starts and where it ends. At both ends the place typed on the
# cruise (departure / destination, editable in place on the cruise page) wins;
# without one, the first route's departure and the last route's arrival stand
# in. The arrival used to prefer the last route — the destination being a
# goal, not a fact — but once editable, a typed arrival that a route silently
# overrode would look like an edit that did not save.
CRUISE_FROM = ("COALESCE(c.departure, (SELECT r4.departure_location FROM routes r4"
               " WHERE r4.cruise_id = c.id ORDER BY r4.id ASC LIMIT 1))")
CRUISE_TO = ("COALESCE(c.destination, (SELECT COALESCE(r5.destination_location, r5.departure_location)"
             " FROM routes r5 WHERE r5.cruise_id = c.id ORDER BY r5.id DESC LIMIT 1))")

# Loch read on the cruise's logbook lines, for the cruise list. Also expect the
# cruises table aliased as c.
LOCH_FIRST = ("(SELECT MIN(l.log) FROM logbook_lines l"
              " JOIN routes r3 ON l.route_id = r3.id WHERE r3.cruise_id = c.id)")
LOCH_LAST = ("(SELECT MAX(l.log) FROM logbook_lines l"
             " JOIN routes r3 ON l.route_id = r3.id WHERE r3.cruise_id = c.id)")


async def _cruise_neighbours(db, ship_id: int, cruise_id: int):
    """Ids de la croisière précédente et de la suivante du navire, dans
    l'ordre des dates de départ (le même que CRUISE_NUMBER) ; None aux bouts."""
    key = f"({CRUISE_KEY.format(a='c')}, c.id)"
    here = f"(SELECT {CRUISE_KEY.format(a='x')}, x.id FROM cruises x WHERE x.id = ?)"
    found = []
    for comparison, direction in (("<", "DESC"), (">", "ASC")):
        cursor = await db.execute(
            f"SELECT c.id FROM cruises c WHERE c.ship_id = ? AND {key} {comparison} {here} "
            f"ORDER BY {CRUISE_KEY.format(a='c')} {direction}, c.id {direction} LIMIT 1",
            (ship_id, cruise_id))
        row = await cursor.fetchone()
        found.append(row[0] if row else None)
    return found[0], found[1]


async def _current_cruise_id(db, ship_id: int) -> Optional[int]:
    """La croisière en cours du navire, ou None. En cours = pas encore de date
    de fin, et au moins un équipier pas encore débarqué. Une date (fin ou
    débarquement) d'aujourd'hui ou d'avant compte comme passée, une date à
    venir non, une date vide non plus. Si plusieurs croisières remplissent ces
    conditions, la dernière commencée. Résolue à la volée — aucun drapeau dans
    le schéma — donc tout écran qui en a besoin la demande ici.
    Là où il faut une croisière quoi qu'il arrive (le carnet), voir
    _default_cruise_id."""
    today = date.today().isoformat()
    cursor = await db.execute(
        "SELECT c.id FROM cruises c WHERE c.ship_id = ? "
        "AND (COALESCE(c.end_time, '') = '' OR substr(c.end_time, 1, 10) > ?) "
        "AND EXISTS (SELECT 1 FROM cruise_crew cc WHERE cc.cruise_id = c.id "
        "    AND (COALESCE(cc.disembark_date, '') = '' OR substr(cc.disembark_date, 1, 10) > ?)) "
        "ORDER BY COALESCE(c.start_time, c.created_at) DESC LIMIT 1",
        (ship_id, today, today),
    )
    row = await cursor.fetchone()
    return row[0] if row else None


async def _default_cruise_id(db, ship_id: int) -> Optional[int]:
    """La croisière à proposer par défaut : celle en cours, sinon la dernière
    commencée. Pour le carnet : une croisière finie la veille reste celle où
    l'on écrit le lendemain, à la marina."""
    current = await _current_cruise_id(db, ship_id)
    if current is not None:
        return current
    cursor = await db.execute(
        "SELECT id FROM cruises WHERE ship_id = ? "
        "ORDER BY COALESCE(start_time, created_at) DESC LIMIT 1",
        (ship_id,),
    )
    row = await cursor.fetchone()
    return row[0] if row else None

@app.get("/cruises", response_class=HTMLResponse)
async def cruises_index(request: Request):
    return RedirectResponse(url="/cruises/current", status_code=302)


@app.get("/cruises/current", response_class=HTMLResponse)
async def current_cruise(request: Request):
    ship_id = get_current_ship_id(request)
    async with connect() as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute(
            f"SELECT c.*, {CRUISE_NUMBER} AS number,"
            f" {CRUISE_FROM} AS from_location, {CRUISE_TO} AS to_location"
            " FROM cruises c WHERE c.id = ?",
            (await _current_cruise_id(db, ship_id),),
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
            f"""SELECT r.*, {ROUTE_NUMBER} AS number,
                      (SELECT COUNT(*) FROM logbook_lines l WHERE l.route_id = r.id) AS line_count
               FROM routes r WHERE r.cruise_id = ?
               ORDER BY r.id ASC""",
            (cruise_id,),
        )
        routes = await cursor.fetchall()
        prev_cruise_id, next_cruise_id = await _cruise_neighbours(db, ship_id, cruise_id)
        cursor = await db.execute(
            """SELECT s.* FROM stopovers s
               JOIN routes r ON s.route_id = r.id
               WHERE r.cruise_id = ?
               ORDER BY COALESCE(s.arrival_date, '') ASC""",
            (cruise_id,),
        )
        stopovers = await cursor.fetchall()
        aboard, available_crew = await _cruise_crew(db, cruise_id)
    return templates.TemplateResponse(
        "cruises/detail.html",
        {
            "request": request,
            "active_section": "cruises",
            "is_current": True,
            "cruise": dict(cruise),
            "routes": [dict(r) for r in routes],
            "stopovers": [dict(s) for s in stopovers],
            "aboard": aboard,
            "available_crew": available_crew,
            "prev_cruise_id": prev_cruise_id,
            "next_cruise_id": next_cruise_id,
        },
    )


# ── Import d'une croisière FileMaker ──
# La page « Importer croisière (csv) », au pied de la liste des croisières :
# les trois exports FileMaker d'une croisière, un nom, et import_filemaker
# fait le reste, sur le navire courant. Déclarée avant /cruises/{cruise_id}.

async def _cruise_import_page(request, **context):
    async with connect() as db:
        db.row_factory = aiosqlite.Row
        ship = await _fetch_ship(db, get_current_ship_id(request))
    return templates.TemplateResponse("cruises/import.html", {
        "request": request, "active_section": "cruises", "current_ship": dict(ship) if ship else None,
        "colonnes": import_filemaker.COLONNES, **context})


@app.get("/cruises/import", response_class=HTMLResponse)
async def cruise_import_form(request: Request):
    return await _cruise_import_page(request)


@app.post("/cruises/import", response_class=HTMLResponse)
async def cruise_import(request: Request, nom: Optional[str] = Form(None),
                        fichiers: List[UploadFile] = File(default=[])):
    """Importe d'un coup : rien à confirmer après coup, l'import étant une
    seule transaction qui refuse une croisière déjà présente. Le compte rendu
    dit ce qui est entré et ce qui a été corrigé en route."""
    lus = {}
    try:
        for f in fichiers:
            if not (f and f.filename):
                continue
            data = await f.read(IMPORT_MAX_BYTES + 1)
            if len(data) > IMPORT_MAX_BYTES:
                raise import_filemaker.ImportErreur(f"« {f.filename} » est trop volumineux.")
            try:
                contenu = data.decode("utf-8")
            except UnicodeDecodeError:
                raise import_filemaker.ImportErreur(
                    f"« {f.filename} » n'est pas en Unicode (UTF-8) : refaites l'export en choisissant ce jeu de caractères.")
            lus[f.filename] = import_filemaker.lire_texte(contenu)
        if not lus:
            raise import_filemaker.ImportErreur("Choisissez les trois fichiers exportés de FileMaker.")
        exports = import_filemaker.reconnaitre(lus)
        resultat = await asyncio.to_thread(import_filemaker.importer, DATABASE_URL, nom,
                                           get_current_ship_id(request), exports)
    except import_filemaker.ImportErreur as e:
        return await _cruise_import_page(request, erreur=str(e), nom=nom)
    return await _cruise_import_page(request, resultat=resultat)


@app.get("/cruises/list", response_class=HTMLResponse)
async def cruise_list(request: Request):
    ship_id = get_current_ship_id(request)
    async with connect() as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute(
            f"""SELECT c.*, {CRUISE_NUMBER} AS number,
                       {CRUISE_FROM} AS from_location,
                       {CRUISE_TO} AS to_location,
                       CAST(julianday(c.end_time) - julianday(c.start_time)
                            AS INTEGER) AS duration_days,
                       -- Loch au départ et à l'arrivée. Les colonnes
                       -- cruises.loch_start / loch_end existent mais aucun
                       -- écran ne les remplit : à défaut, on lit le loch des
                       -- lignes de journal de la croisière. Un loch ne recule
                       -- pas, donc MIN et MAX sont bien le premier et le
                       -- dernier relevé, et une ligne saisie hors ordre
                       -- chronologique ne fausse rien.
                       COALESCE(c.loch_start, {LOCH_FIRST}) AS loch_from,
                       COALESCE(c.loch_end, {LOCH_LAST}) AS loch_to,
                       -- Distance = ce que le loch a compté. Arrondi parce que
                       -- la soustraction de deux flottants sort
                       -- 12.299999999999955 pour 512.3 - 500.0.
                       ROUND(COALESCE(c.loch_end, {LOCH_LAST})
                             - COALESCE(c.loch_start, {LOCH_FIRST}), 1) AS distance
                FROM cruises c WHERE c.ship_id = ? ORDER BY {CRUISE_ORDER}""",
            (ship_id,),
        )
        cruises = await cursor.fetchall()
        current_id = await _current_cruise_id(db, ship_id)
    return templates.TemplateResponse(
        "cruises/list.html",
        {
            "request": request,
            "active_section": "cruises",
            "cruises": [dict(c) for c in cruises],
            "current_cruise_id": current_id,
        },
    )


@app.get("/cruises/stopovers", response_class=HTMLResponse)
async def all_stopovers(request: Request):
    ship_id = get_current_ship_id(request)
    async with connect() as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute(
            f"""SELECT s.*,
                      CAST(
                          CASE WHEN s.arrival_date IS NOT NULL AND s.departure_date IS NOT NULL
                          THEN julianday(s.departure_date) - julianday(s.arrival_date)
                          ELSE NULL END AS INTEGER
                      ) AS nights,
                      r.departure_location, r.destination_location,
                      {ROUTE_NUMBER} AS route_number,
                      c.name AS cruise_name, c.id AS cruise_id
               FROM stopovers s
               JOIN routes r ON s.route_id = r.id
               JOIN cruises c ON r.cruise_id = c.id
               WHERE c.ship_id = ?
               -- Chronologique. COALESCE plutôt que arrival_date seul : une
               -- date NULL trie avant tout en SQLite, donc les escales sans
               -- date d'arrivée ouvriraient la liste ; elles la ferment.
               ORDER BY COALESCE(s.arrival_date, '9999') ASC, s.id ASC""",
            (ship_id,),
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
    async with connect() as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute(
            f"SELECT c.*, {CRUISE_NUMBER} AS number,"
            f" {CRUISE_FROM} AS from_location, {CRUISE_TO} AS to_location"
            " FROM cruises c WHERE c.id = ?",
            (cruise_id,),
        )
        cruise = await cursor.fetchone()
        if cruise is None:
            raise HTTPException(status_code=404, detail="Cruise not found")
        # Navigate within this cruise's own ship, so a direct link stays coherent
        # even when another ship is selected.
        ship_id = cruise["ship_id"]
        cursor = await db.execute(
            f"""SELECT r.*, {ROUTE_NUMBER} AS number,
                      (SELECT COUNT(*) FROM logbook_lines l WHERE l.route_id = r.id) AS line_count
               FROM routes r
               WHERE r.cruise_id = ?
               ORDER BY r.id ASC""",
            (cruise_id,),
        )
        routes = await cursor.fetchall()
        prev_cruise_id, next_cruise_id = await _cruise_neighbours(db, ship_id, cruise_id)
        cursor = await db.execute(
            """SELECT s.* FROM stopovers s
               JOIN routes r ON s.route_id = r.id
               WHERE r.cruise_id = ?
               ORDER BY COALESCE(s.arrival_date, '') ASC""",
            (cruise_id,),
        )
        stopovers = await cursor.fetchall()
        # Arriver ici par un lien direct ou par les flèches n'enlève rien au
        # fait que ce soit la croisière en cours : la mention doit s'afficher
        # comme sur /cruises/current.
        is_current = await _current_cruise_id(db, ship_id) == cruise_id
        aboard, available_crew = await _cruise_crew(db, cruise_id)
    return templates.TemplateResponse(
        "cruises/detail.html",
        {
            "request": request,
            "active_section": "cruises",
            "is_current": is_current,
            "cruise": dict(cruise),
            "routes": [dict(r) for r in routes],
            "stopovers": [dict(s) for s in stopovers],
            "aboard": aboard,
            "available_crew": available_crew,
            "prev_cruise_id": prev_cruise_id,
            "next_cruise_id": next_cruise_id,
        },
    )


@app.post("/cruises/{cruise_id}/arrival")
async def cruise_arrival(cruise_id: int):
    now = datetime.now().strftime("%Y-%m-%dT%H:%M:%S")
    async with connect() as db:
        await db.execute(
            "UPDATE cruises SET end_time = ? WHERE id = ?",
            (now, cruise_id),
        )
        await db.commit()
    return RedirectResponse(url=f"/cruises/{cruise_id}", status_code=303)


# Champs de l'en-tête d'une croisière modifiables sur place, avec leur type.
# Le nom du champ est interpolé dans l'UPDATE : cette liste est ce qui tient
# l'adresse hors du SQL, comme EDITABLE_LINE_FIELDS.
CRUISE_FIELDS = {"start_time": "date", "end_time": "date", "departure": "text", "destination": "text"}


@app.post("/cruises/{cruise_id}/field/{field}")
async def cruise_set_field(cruise_id: int, field: str, value: Optional[str] = Form(None)):
    """Départ, arrivée (dates et lieux) modifiés sur place depuis la page de la
    croisière. Une case vidée efface la valeur."""
    if field not in CRUISE_FIELDS:
        raise HTTPException(status_code=404, detail="Champ non modifiable")
    value = (value or "").strip() or None
    if value and CRUISE_FIELDS[field] == "date" and not re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
        raise HTTPException(status_code=400, detail="Date invalide")
    async with connect() as db:
        await db.execute(f"UPDATE cruises SET {field} = ? WHERE id = ?", (value, cruise_id))
        await db.commit()
    return RedirectResponse(url=f"/cruises/{cruise_id}", status_code=303)


@app.post("/cruises/{cruise_id}/set-name")
async def cruise_set_name(cruise_id: int, name: Optional[str] = Form(None)):
    """Rename a cruise from its own page, edited in place like a logbook line."""
    async with connect() as db:
        await db.execute(
            "UPDATE cruises SET name = ? WHERE id = ?",
            (name or None, cruise_id),
        )
        await db.commit()
    return RedirectResponse(url=f"/cruises/{cruise_id}", status_code=303)


@app.post("/cruises/{cruise_id}/delete")
async def delete_cruise(cruise_id: int):
    async with connect() as db:
        await db.execute("DELETE FROM cruises WHERE id = ?", (cruise_id,))
        await db.commit()
    return RedirectResponse(url="/cruises/list", status_code=303)


@app.post("/cruises/new")
async def create_cruise(
    request: Request,
    name: str = Form(...),
    departure: Optional[str] = Form(None),
    destination: Optional[str] = Form(None),
    start_time: Optional[str] = Form(None),
    end_time: Optional[str] = Form(None),
):
    async with connect() as db:
        await db.execute(
            "INSERT INTO cruises (ship_id, name, departure, destination, start_time, end_time)"
            " VALUES (?, ?, ?, ?, ?, ?)",
            (get_current_ship_id(request), name, departure or None, destination or None,
             start_time or None, end_time or None),
        )
        await db.commit()
    return RedirectResponse(url="/cruises/list", status_code=303)


# ── Stopovers ────────────────────────────────────────────────────────────────

@app.get("/routes/{route_id}/stopovers/new", response_class=HTMLResponse)
async def new_stopover_form(request: Request, route_id: int):
    async with connect() as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute(
            f"""SELECT r.*, {ROUTE_NUMBER} AS number, c.name AS cruise_name
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
    async with connect() as db:
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
    async with connect() as db:
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
    async with connect() as db:
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
    async with connect() as db:
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
    ship_id = get_current_ship_id(request)
    async with connect() as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute(
            "SELECT id FROM routes WHERE cruise_id = ? ORDER BY id DESC LIMIT 1",
            (await _current_cruise_id(db, ship_id),),
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
    async with connect() as db:
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


# Départ / arrivée d'une route, modifiables sur place depuis sa page, avec leur
# type ; même rôle que CRUISE_FIELDS (le nom du champ va dans l'UPDATE).
ROUTE_FIELDS = {"start_time": "datetime", "end_time": "datetime",
                "departure_location": "text", "destination_location": "text",
                "notes": "text"}   # le journal de la route (FileMaker), panneau Journal


@app.post("/routes/{route_id}/field/{field}")
async def route_set_field(route_id: int, field: str, value: Optional[str] = Form(None)):
    """Une case vidée efface la valeur. L'heure arrive d'un datetime-local
    (« 2026-09-18T07:53 »), la forme que les routes stockent déjà."""
    if field not in ROUTE_FIELDS:
        raise HTTPException(status_code=404, detail="Champ non modifiable")
    value = (value or "").strip() or None
    if value and ROUTE_FIELDS[field] == "datetime" and not re.fullmatch(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}(:\d{2})?", value):
        raise HTTPException(status_code=400, detail="Date invalide")
    async with connect() as db:
        await db.execute(f"UPDATE routes SET {field} = ? WHERE id = ?", (value, route_id))
        await db.commit()
    return RedirectResponse(url=f"/routes/{route_id}" + ("#journal" if field == "notes" else ""), status_code=303)


@app.post("/routes/{route_id}/delete")
async def delete_route(route_id: int):
    async with connect() as db:
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
    ship_id = get_current_ship_id(request)
    async with connect() as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute(
            """SELECT r.id FROM routes r
               JOIN cruises c ON r.cruise_id = c.id
               WHERE c.ship_id = ?
               ORDER BY COALESCE(r.start_time, r.created_at) DESC LIMIT 1""",
            (ship_id,),
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
    async with connect() as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute("SELECT * FROM cruises WHERE id = ?", (cruise_id,))
        cruise = await cursor.fetchone()
        # A leg starts where the previous one ended, with the same engine hours
        # on the clock. Highest id = the cruise's latest route, as everywhere
        # else. Either column may be NULL when that route has no arrival yet;
        # the form then simply opens blank.
        cursor = await db.execute(
            """SELECT destination_location, motor_hours_end
               FROM routes WHERE cruise_id = ? ORDER BY id DESC LIMIT 1""",
            (cruise_id,),
        )
        previous = await cursor.fetchone()
    if cruise is None:
        raise HTTPException(status_code=404, detail="Cruise not found")
    return templates.TemplateResponse(
        "new_route.html",
        {
            "request": request,
            "cruise": dict(cruise),
            "previous": dict(previous) if previous else None,
            "active_section": "routes",
        },
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
    async with connect() as db:
        cursor = await db.execute(
            """INSERT INTO routes (cruise_id, name, departure_location, destination_location, start_time, notes, motor_hours_start)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (cruise_id, name or None, departure_location or None, destination_location or None,
             start_time or None, notes or None, motor_hours_start),
        )
        route_id = cursor.lastrowid
        await db.commit()
    # Land on the fresh route rather than on the cruise: it is empty, and the
    # flag makes it offer a first logbook line straight away.
    return RedirectResponse(url=f"/routes/{route_id}?nouvelle=1", status_code=303)


@app.get("/routes/{route_id}", response_class=HTMLResponse)
async def route_detail(request: Request, route_id: int, nouvelle: int = 0):
    """The `nouvelle` flag is set by create_route and only opens the modal that
    offers a first logbook line; it changes nothing else on the page."""
    async with connect() as db:
        db.row_factory = aiosqlite.Row

        cursor = await db.execute(
            f"""SELECT r.*, {ROUTE_NUMBER} AS number, c.name AS cruise_name
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
            "ask_new_line": bool(nouvelle),
        },
    )


@app.get("/routes/{route_id}/new-line", response_class=HTMLResponse)
async def new_line_form(request: Request, route_id: int):
    async with connect() as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute(
            f"""SELECT r.*, {ROUTE_NUMBER} AS number, c.name AS cruise_name
               FROM routes r LEFT JOIN cruises c ON r.cruise_id = c.id
               WHERE r.id = ?""",
            (route_id,),
        )
        route = await cursor.fetchone()
        # Sea state, visibility and sail plan are eyeballed, not measured:
        # SignalK has nothing to say about them, so the form inherits them from
        # the route's latest line. They change slowly, hence the confirmation
        # on save.
        cursor = await db.execute(
            """SELECT sea_state, visibility, sails FROM logbook_lines
               WHERE route_id = ? ORDER BY timestamp DESC, id DESC LIMIT 1""",
            (route_id,),
        )
        previous = await cursor.fetchone()
    if route is None:
        raise HTTPException(status_code=404, detail="Route not found")
    data = get_sensor_data()
    return templates.TemplateResponse(
        "routes/new_line.html",
        {
            "request": request,
            "active_section": "routes",
            "data": data,
            "route": dict(route),
            "previous": dict(previous) if previous else None,
        },
    )


@app.post("/routes/{route_id}/new-line")
async def create_line(
    request: Request,
    route_id: int,
    # The form asks for degrees, decimal minutes and a hemisphere; the column
    # keeps signed decimal degrees. _dmm_to_dd is the only place that converts.
    lat_deg: Optional[float] = Form(None),
    lat_min: Optional[float] = Form(None),
    lat_hem: Optional[str] = Form(None),
    lon_deg: Optional[float] = Form(None),
    lon_min: Optional[float] = Form(None),
    lon_hem: Optional[str] = Form(None),
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
    # Angles are logged as whole degrees (position lat/lon keep full precision).
    awa = round(awa) if awa is not None else None
    twa = round(twa) if twa is not None else None
    heading = round(heading) if heading is not None else None
    cog = round(cog) if cog is not None else None
    # Pressure too: whole hPa, like a barometer. The field is step="any", so a
    # value pasted with decimals is normalised here rather than refused.
    pressure = round(pressure) if pressure is not None else None
    position_lat = _dmm_to_dd(lat_deg, lat_min, lat_hem)
    position_lon = _dmm_to_dd(lon_deg, lon_min, lon_hem)
    async with connect() as db:
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
async def all_cruises_map_data(request: Request):
    from fastapi.responses import JSONResponse
    ship_id = get_current_ship_id(request)
    async with connect() as db:
        db.row_factory = aiosqlite.Row

        cursor = await db.execute(
            "SELECT id, name FROM cruises WHERE ship_id = ? "
            "ORDER BY COALESCE(start_time, created_at) ASC",
            (ship_id,),
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
    async with connect() as db:
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
    async with connect() as db:
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
    async with connect() as db:
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

# ── Carnet de voyage ──────────────────────────────────────────────────────────
#
# L'ancienne galerie. Des publications — un texte, des photos, ou les deux —
# rangées par croisière, l'auteur étant l'équipier connecté. La page s'ouvre
# sur la croisière en cours ; un menu en choisit une autre ou « Voir tout ».
# Chacun corrige ses propres publications ; supprimer reste à l'Amiral (grille
# des droits, rubrique « carnet »).
#
# Les photos arrivent déjà réduites par le navigateur, qui a lu avant cela leur
# position et leur heure de prise de vue (EXIF) — la réduction les efface — et
# les envoie dans des champs à part. Sans position dans la photo, celle du
# bateau (SignalK) au moment de publier.

CARNET_MAX_PHOTOS = 20


@app.get("/gallery", include_in_schema=False)
async def gallery_redirect():
    return RedirectResponse(url="/carnet", status_code=301)


async def _boat_position() -> Optional[tuple]:
    """Position du bateau (SignalK), ou None. Trois secondes au plus : hors
    couverture ou serveur éteint, la publication ne doit pas attendre."""
    try:
        return await asyncio.wait_for(asyncio.to_thread(get_position), timeout=3)
    except Exception:
        return None


# ── Lieu des photos ──
# Le nom du lieu d'une photo vient de Nominatim (OpenStreetMap), à partir de sa
# position. Jamais pendant la publication : à bord, Internet manque souvent, et
# publier ne doit pas attendre. Une tâche de fond cherche les photos dont place
# est NULL, après chaque publication et au démarrage ; hors ligne, elles restent
# NULL et seront reprises à la fois suivante. Rien trouvé (en mer) : '', pour ne
# pas redemander. Nominatim demande une requête par seconde au plus, et un
# User-Agent qui identifie l'app.
NOMINATIM_URL = "https://nominatim.openstreetmap.org/reverse"
NOMINATIM_AGENT = "NautiBook/1.0 (carnet de bord)"
PLACE_KEYS = ("village", "town", "city", "hamlet", "island", "municipality", "county", "state")
_place_cache: dict = {}
_place_task: Optional[asyncio.Task] = None


def _place_name(lat: float, lon: float) -> Optional[str]:
    """Nom du lieu le plus proche ; '' si Nominatim n'en connaît pas, None
    s'il n'a pas répondu (hors ligne)."""
    key = (round(lat, 3), round(lon, 3))      # ~100 m : les photos d'un même endroit
    if key in _place_cache:
        return _place_cache[key]
    try:
        resp = requests.get(NOMINATIM_URL, timeout=5, headers={"User-Agent": NOMINATIM_AGENT},
                            params={"format": "jsonv2", "lat": lat, "lon": lon, "zoom": 14,
                                    "accept-language": "fr"})
        resp.raise_for_status()
        data = resp.json()
    except Exception:
        return None
    # « Unable to geocode » : rien à cet endroit (la pleine mer). Toute autre
    # réponse sans adresse — un refus passager, une limite atteinte — se retente.
    if data.get("error") == "Unable to geocode":
        name = ""
    elif not data.get("address"):
        return None
    else:
        address = data["address"]
        name = next((address[k] for k in PLACE_KEYS if address.get(k)), "")
    _place_cache[key] = name
    return name


async def _fill_carnet_places():
    try:
        while True:
            async with connect() as db:
                cursor = await db.execute(
                    "SELECT id, lat, lon FROM carnet_photos WHERE place IS NULL AND lat IS NOT NULL ORDER BY id")
                rows = await cursor.fetchall()
            if not rows:
                return
            for photo_id, lat, lon in rows:
                cached = (round(lat, 3), round(lon, 3)) in _place_cache
                name = await asyncio.to_thread(_place_name, lat, lon)
                if name is None:
                    return                            # hors ligne : à la prochaine fois
                async with connect() as db:
                    await db.execute("UPDATE carnet_photos SET place = ? WHERE id = ?", (name, photo_id))
                    await db.commit()
                if not cached:
                    await asyncio.sleep(1.5)
    except Exception as e:
        print(f"Lieu des photos : {e}")


def _start_place_lookup():
    """Lance la recherche des lieux, sauf si elle tourne déjà."""
    global _place_task
    if _place_task is None or _place_task.done():
        _place_task = asyncio.create_task(_fill_carnet_places())


def _float_or_none(value):
    try:
        return float(value) if value not in (None, "") else None
    except ValueError:
        return None


async def _store_carnet_photos(db, entry_id: int, files: List[UploadFile], lats, lons, takens,
                               start: int = 0) -> int:
    """Enregistre les photos d'une publication. Renvoie le nombre gardé."""
    files = [f for f in files if f and f.filename][:CARNET_MAX_PHOTOS]
    if not files:
        return 0
    boat = None
    kept = 0
    for i, upload in enumerate(files):
        path = await _save_upload(upload, IMG_SUFFIXES, CARNET_SUBDIR, DOC_MAX_BYTES)
        if not path:
            continue
        lat = _float_or_none(lats[i] if i < len(lats) else None)
        lon = _float_or_none(lons[i] if i < len(lons) else None)
        source = "photo" if lat is not None and lon is not None else None
        if source is None:
            if boat is None:
                boat = await _boat_position() or ()
            if boat:
                lat, lon, source = boat[0], boat[1], "bateau"
        taken = (takens[i] if i < len(takens) else None) or None
        await db.execute(
            "INSERT INTO carnet_photos (entry_id, photo_path, lat, lon, geo_source, taken_at, position) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (entry_id, path, lat if source else None, lon if source else None, source, taken, start + i),
        )
        kept += 1
    return kept


@app.get("/carnet", response_class=HTMLResponse)
async def carnet(request: Request, croisiere: Optional[str] = None, edit: Optional[int] = None):
    ship_id = get_current_ship_id(request)
    async with connect() as db:
        db.row_factory = aiosqlite.Row
        ship = await _fetch_ship(db, ship_id)
        current = await _current_cruise_id(db, ship_id)
        default = await _default_cruise_id(db, ship_id)
        cursor = await db.execute(
            "SELECT id, name, start_time FROM cruises WHERE ship_id = ? "
            "ORDER BY COALESCE(start_time, created_at) DESC", (ship_id,))
        cruises = [dict(c) for c in await cursor.fetchall()]
        # « tout », un id de croisière du navire, ou par défaut celle en cours
        # (à défaut, la dernière).
        voir_tout = croisiere == "tout"
        selected = None if voir_tout else (
            int(croisiere) if croisiere and croisiere.isdigit()
            and any(c["id"] == int(croisiere) for c in cruises) else default)
        where, args = "e.ship_id = ?", [ship_id]
        if not voir_tout:
            where += " AND e.cruise_id IS ?"
            args.append(selected)
        cursor = await db.execute(
            f"""SELECT e.*, c.name AS cruise_name, m.first_name, m.last_name, m.photo_path AS author_photo
                FROM carnet_entries e
                LEFT JOIN cruises c ON e.cruise_id = c.id
                LEFT JOIN crew_members m ON e.crew_member_id = m.id
                WHERE {where} ORDER BY e.created_at DESC, e.id DESC""", args)
        entries = [dict(e) for e in await cursor.fetchall()]
        if entries:
            ids = [e["id"] for e in entries]
            cursor = await db.execute(
                f"SELECT * FROM carnet_photos WHERE entry_id IN ({','.join('?' * len(ids))}) "
                "ORDER BY entry_id, position, id", ids)
            photos = {}
            for p in await cursor.fetchall():
                photos.setdefault(p["entry_id"], []).append(dict(p))
            for e in entries:
                e["photos"] = photos.get(e["id"], [])
    # Regroupées par jour, dans l'ordre de la liste.
    days = []
    for e in entries:
        day = (e["created_at"] or "")[:10]
        if not days or days[-1]["day"] != day:
            days.append({"day": day, "entries": []})
        days[-1]["entries"].append(e)
    user = _template_user(request)
    return templates.TemplateResponse(
        "carnet/index.html",
        {"request": request, "active_section": "gallery",
         "current_ship": dict(ship) if ship else None,
         "cruises": cruises, "current_cruise": current, "default_cruise": default, "selected": selected, "voir_tout": voir_tout,
         "days": days, "editing": edit,
         "my_crew_id": user["crew_member_id"] if user else None,
         "max_photos": CARNET_MAX_PHOTOS, "max_bytes": DOC_MAX_BYTES},
    )


@app.post("/carnet/new")
async def carnet_new(request: Request, text: Optional[str] = Form(None), cruise_id: Optional[str] = Form(None),
                     photo_files: List[UploadFile] = File(default=[]),
                     photo_lat: List[str] = Form(default=[]), photo_lon: List[str] = Form(default=[]),
                     photo_taken: List[str] = Form(default=[]), back: Optional[str] = Form(None)):
    ship_id = get_current_ship_id(request)
    user = _template_user(request)
    text = (text or "").strip() or None
    has_photo = any(f and f.filename for f in photo_files)
    if not text and not has_photo:
        return RedirectResponse(url=_safe_next(back, "/carnet"), status_code=303)
    async with connect() as db:
        db.row_factory = aiosqlite.Row
        # La croisière doit être une du navire ; à défaut, celle en cours, ou la dernière.
        cid = int(cruise_id) if cruise_id and cruise_id.isdigit() else None
        if cid is not None:
            cursor = await db.execute("SELECT 1 FROM cruises WHERE id = ? AND ship_id = ?", (cid, ship_id))
            if not await cursor.fetchone():
                cid = None
        if cid is None:
            cid = await _default_cruise_id(db, ship_id)
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        cursor = await db.execute(
            "INSERT INTO carnet_entries (ship_id, cruise_id, crew_member_id, text, created_at) VALUES (?, ?, ?, ?, ?)",
            (ship_id, cid, user["crew_member_id"] if user else None, text, now),
        )
        entry_id = cursor.lastrowid
        kept = await _store_carnet_photos(db, entry_id, photo_files, photo_lat, photo_lon, photo_taken)
        if not text and not kept:
            await db.execute("DELETE FROM carnet_entries WHERE id = ?", (entry_id,))
        await db.commit()
    _start_place_lookup()
    return RedirectResponse(url=_safe_next(back, "/carnet") + f"#publication-{entry_id}", status_code=303)


async def _own_entry(db, request, entry_id: int):
    """La publication, si l'utilisateur peut la corriger : la sienne, ou
    n'importe laquelle pour l'Amiral."""
    cursor = await db.execute("SELECT id, crew_member_id FROM carnet_entries WHERE id = ?", (entry_id,))
    entry = await cursor.fetchone()
    if entry is None:
        raise HTTPException(status_code=404, detail="Publication introuvable")
    user = _template_user(request)
    if not (user and (user["rank"] == "amiral" or user["crew_member_id"] == entry[1])):
        raise HTTPException(status_code=403, detail="Seul son auteur peut corriger une publication")
    return entry


@app.post("/carnet/{entry_id}/edit")
async def carnet_edit(request: Request, entry_id: int, text: Optional[str] = Form(None),
                      photo_files: List[UploadFile] = File(default=[]),
                      photo_lat: List[str] = Form(default=[]), photo_lon: List[str] = Form(default=[]),
                      photo_taken: List[str] = Form(default=[]), cruise_id: Optional[str] = Form(None),
                      back: Optional[str] = Form(None)):
    """Correction par son auteur : le texte, la croisière, et des photos en
    plus. Retirer une photo passe par carnet_photo_remove."""
    async with connect() as db:
        await _own_entry(db, request, entry_id)
        # La croisière doit être une du navire de la publication ; sinon elle
        # ne change pas.
        cursor = await db.execute(
            "SELECT c.id FROM cruises c JOIN carnet_entries e ON e.ship_id = c.ship_id "
            "WHERE e.id = ? AND c.id = ?",
            (entry_id, int(cruise_id) if cruise_id and cruise_id.isdigit() else None))
        row = await cursor.fetchone()
        cid = row[0] if row else None
        await db.execute("UPDATE carnet_entries SET text = ?, cruise_id = COALESCE(?, cruise_id), updated_at = ? "
                         "WHERE id = ?",
                         ((text or "").strip() or None, cid, datetime.now().strftime("%Y-%m-%d %H:%M:%S"), entry_id))
        cursor = await db.execute("SELECT COALESCE(MAX(position), -1) + 1 FROM carnet_photos WHERE entry_id = ?", (entry_id,))
        start = (await cursor.fetchone())[0]
        await _store_carnet_photos(db, entry_id, photo_files, photo_lat, photo_lon, photo_taken, start)
        await db.commit()
    _start_place_lookup()
    # Le retour est l'adresse de la page en correction : sans edit=, sinon la
    # publication se rouvrirait en correction une fois enregistrée.
    back = re.sub(r"[?&]$", "", re.sub(r"([?&])edit=\d+&?", r"\1", _safe_next(back, "/carnet")))
    # Changée de croisière, la publication n'est plus dans le carnet affiché :
    # on suit celle-ci, sauf depuis « Voir tout », qui la montre de toute façon.
    if cid is not None and "croisiere=tout" not in back:
        back = f"/carnet?croisiere={cid}"
    return RedirectResponse(url=back + f"#publication-{entry_id}", status_code=303)


@app.post("/carnet/{entry_id}/delete")
async def carnet_delete(entry_id: int, back: Optional[str] = Form(None)):
    """Suppression d'une publication (Amiral). Ses photos partent de la base
    avec elle ; les fichiers restent dans IMG/Carnet/, comme partout."""
    async with connect() as db:
        await db.execute("DELETE FROM carnet_entries WHERE id = ?", (entry_id,))
        await db.commit()
    return RedirectResponse(url=_safe_next(back, "/carnet"), status_code=303)


@app.post("/carnet/photos/{photo_id}/remove")
async def carnet_photo_remove(request: Request, photo_id: int, back: Optional[str] = Form(None)):
    """Retire une photo d'une publication ; le fichier reste. Son auteur le
    peut pour les siennes, qui a carnet.supprimer (l'Amiral) pour toutes.
    D'où « remove » et non « delete » : un …/delete exigerait carnet.supprimer
    de tous (required_permission), et c'est ici que l'auteur est vérifié."""
    async with connect() as db:
        cursor = await db.execute(
            "SELECT p.entry_id, e.crew_member_id FROM carnet_photos p "
            "JOIN carnet_entries e ON e.id = p.entry_id WHERE p.id = ?", (photo_id,))
        row = await cursor.fetchone()
        if row:
            user = _template_user(request)
            if not (can(user, "carnet.supprimer") or (user and user["crew_member_id"] == row[1])):
                raise HTTPException(status_code=403, detail="Seul son auteur peut retirer cette photo")
            await db.execute("DELETE FROM carnet_photos WHERE id = ?", (photo_id,))
            await db.commit()
    anchor = f"#publication-{row[0]}" if row else ""
    return RedirectResponse(url=_safe_next(back, "/carnet") + anchor, status_code=303)


@app.get("/logbook/{line_id}/edit", response_class=HTMLResponse)
async def edit_line_form(request: Request, line_id: int):
    """Full edit form for one logbook line, reached from the pencil on the route page."""
    async with connect() as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute("SELECT * FROM logbook_lines WHERE id = ?", (line_id,))
        line = await cursor.fetchone()
    if line is None:
        raise HTTPException(status_code=404, detail="Entry not found")
    return templates.TemplateResponse(
        "routes/edit_line.html",
        {"request": request, "active_section": "routes", "line": dict(line)},
    )


@app.post("/logbook/{line_id}/edit")
async def update_line(
    request: Request,
    line_id: int,
    timestamp: Optional[str] = Form(None),
    # Same DMM boxes as the creation form; see _dmm_to_dd.
    lat_deg: Optional[float] = Form(None),
    lat_min: Optional[float] = Form(None),
    lat_hem: Optional[str] = Form(None),
    lon_deg: Optional[float] = Form(None),
    lon_min: Optional[float] = Form(None),
    lon_hem: Optional[str] = Form(None),
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
    # Same rounding as create_line: whole degrees, whole hPa, lat/lon keep full
    # precision.
    awa = round(awa) if awa is not None else None
    twa = round(twa) if twa is not None else None
    heading = round(heading) if heading is not None else None
    cog = round(cog) if cog is not None else None
    pressure = round(pressure) if pressure is not None else None
    # <input type="datetime-local"> submits "YYYY-MM-DDTHH:MM"; rows written by
    # create_line hold str(datetime.now()), so normalise to that shape rather
    # than leaving two formats in the column.
    if timestamp:
        timestamp = timestamp.replace("T", " ")
        if len(timestamp) == 16:
            timestamp += ":00"
    position_lat = _dmm_to_dd(lat_deg, lat_min, lat_hem)
    position_lon = _dmm_to_dd(lon_deg, lon_min, lon_hem)
    async with connect() as db:
        cursor = await db.execute("SELECT route_id FROM logbook_lines WHERE id = ?", (line_id,))
        row = await cursor.fetchone()
        if row is None:
            raise HTTPException(status_code=404, detail="Entry not found")
        route_id = row[0]
        await db.execute(
            """UPDATE logbook_lines SET
                   timestamp = COALESCE(?, timestamp),
                   aws = ?, awa = ?, water_temp = ?, heading = ?, cog = ?, log = ?,
                   trip = ?, depth = ?, position_lat = ?, position_lon = ?, stw = ?,
                   sog = ?, tws = ?, twa = ?, pressure = ?, sea_state = ?,
                   visibility = ?, sails = ?, points_of_sail = ?, visual_pos = ?,
                   notes = ?
               WHERE id = ?""",
            (
                timestamp or None,
                aws, awa, water_temp, heading, cog, log, trip, depth,
                position_lat, position_lon, stw, sog, tws, twa, pressure,
                sea_state or None, visibility or None, sails or None,
                points_of_sail or None, visual_pos or None, notes or None,
                line_id,
            ),
        )
        await db.commit()
    dest = f"/routes/{route_id}" if route_id else f"/logbook/{line_id}"
    return RedirectResponse(url=dest, status_code=303)


@app.post("/logbook/{line_id}/delete")
async def delete_line(line_id: int):
    async with connect() as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute("SELECT route_id FROM logbook_lines WHERE id = ?", (line_id,))
        row = await cursor.fetchone()
        route_id = row["route_id"] if row else None
        # trip_photos.trip_id has no foreign key to logbook_lines, so no cascade
        # fires here — the attached photo rows have to go by hand, or they join
        # the orphans. The files in IMG/ stay, as everywhere else in the app.
        await db.execute("DELETE FROM trip_photos WHERE trip_id = ?", (line_id,))
        await db.execute("DELETE FROM logbook_lines WHERE id = ?", (line_id,))
        await db.commit()
    dest = f"/routes/{route_id}" if route_id else "/cruises/current"
    return RedirectResponse(url=dest, status_code=303)


# Columns the route page may edit in place. The field name is interpolated
# into SQL, so it must come from this set and never straight from the URL.
EDITABLE_LINE_FIELDS = {"visual_pos", "notes"}


async def _set_line_field(line_id: int, field: str, value: Optional[str]):
    """Update one whitelisted column; returns where to redirect afterwards."""
    if field not in EDITABLE_LINE_FIELDS:
        raise HTTPException(status_code=404, detail="Field not editable")
    async with connect() as db:
        cursor = await db.execute("SELECT route_id FROM logbook_lines WHERE id = ?", (line_id,))
        row = await cursor.fetchone()
        if row is None:
            raise HTTPException(status_code=404, detail="Entry not found")
        route_id = row[0]
        # Blancs autour retirés : une note faite d'un retour à la ligne
        # n'est pas une note, et s'afficherait vide dans le Journal.
        await db.execute(
            f"UPDATE logbook_lines SET {field} = ? WHERE id = ?",
            ((value or "").strip() or None, line_id),
        )
        await db.commit()
    return f"/routes/{route_id}" if route_id else f"/logbook/{line_id}"


@app.post("/logbook/{line_id}/field/{field}")
async def update_line_field(line_id: int, field: str, value: Optional[str] = Form(None)):
    """Inline edit of one logbook-line column from the route page. A note
    comes back to the Journal section rather than the top of the page."""
    url = await _set_line_field(line_id, field, value)
    return RedirectResponse(url=url + ("#journal" if field == "notes" else ""), status_code=303)


@app.post("/logbook/note")
async def add_line_note(line_id: int = Form(...), value: Optional[str] = Form(None)):
    """Journal: annotate a line picked from the dropdown of unannotated ones."""
    return RedirectResponse(url=await _set_line_field(line_id, "notes", value) + "#journal", status_code=303)


# ── Recherche ─────────────────────────────────────────────────────────────────

def _fold(text: str) -> str:
    """Minuscules sans accents : « Équipement » → « equipement »."""
    return "".join(
        c for c in unicodedata.normalize("NFKD", text) if not unicodedata.combining(c)
    ).lower()


def _fold_sql(value):
    """_fold exposé à SQLite, qui ne sait ignorer ni la casse des lettres
    accentuées (LIKE ne le fait que pour l'ASCII) ni les accents."""
    return _fold(value) if isinstance(value, str) else value


def _excerpt(text: str, needle: str, width: int = 60):
    """Extrait autour de la première occurrence, en trois morceaux (avant,
    trouvé, après) que la template affiche échappés, le trouvé surligné.

    La recherche se fait sur le texte replié, mais le découpage sur l'original :
    replier un caractère peut en changer la longueur, d'où la table des
    positions."""
    folded, origin = "", []
    for i, c in enumerate(text):
        f = _fold(c)
        folded += f
        origin.extend([i] * len(f))
    k = folded.find(needle)
    if k < 0:
        return None
    start = origin[k]
    end = origin[k + len(needle) - 1] + 1
    lo, hi = max(0, start - width), min(len(text), end + width)
    return {
        "before": ("…" if lo > 0 else "") + text[lo:start],
        "match": text[start:end],
        "after": text[end:hi] + ("…" if hi < len(text) else ""),
    }


# Une section par type de fiche : la requête (dont les colonnes cherchées),
# le lien vers la fiche, et de quoi l'intituler. `fields` liste les colonnes
# fouillées, dans l'ordre où l'extrait les considère ; `alias` est la table qui
# les porte quand la requête en joint plusieurs, et qui ont des colonnes
# homonymes (name, notes). Tout est limité au navire
# courant, sauf les équipiers, qui n'appartiennent à aucun (voir /crew).
SEARCH_SECTIONS = [
    {
        "title": "Croisières",
        "section": "croisieres",
        "alias": "c",
        "fields": ["name", "departure", "destination"],
        "sql": "SELECT c.id, c.name, c.departure, c.destination, c.start_time AS date "
               "FROM cruises c WHERE c.ship_id = :ship AND ({where}) "
               "ORDER BY COALESCE(c.start_time, c.created_at) DESC",
        "url": lambda r: f"/cruises/{r['id']}",
        "label": lambda r: r["name"] or "Croisière",
    },
    {
        "title": "Routes",
        "section": "croisieres",
        "alias": "r",
        "fields": ["name", "departure_location", "destination_location", "notes"],
        "sql": "SELECT r.id, r.name, r.departure_location, r.destination_location, r.notes, "
               "r.start_time AS date, c.name AS cruise_name "
               "FROM routes r JOIN cruises c ON r.cruise_id = c.id "
               "WHERE c.ship_id = :ship AND ({where}) ORDER BY r.id DESC",
        "url": lambda r: f"/routes/{r['id']}",
        "label": lambda r: " → ".join(x for x in (r["departure_location"], r["destination_location"]) if x)
                           or r["name"] or "Route",
        "context": lambda r: r["cruise_name"],
    },
    {
        "title": "Journal de bord",
        "section": "journal",
        "alias": "l",
        "fields": ["notes", "visual_pos", "sails"],
        "sql": "SELECT l.id, l.notes, l.visual_pos, l.sails, l.timestamp AS date, "
               "r.departure_location, r.destination_location "
               "FROM logbook_lines l JOIN routes r ON l.route_id = r.id JOIN cruises c ON r.cruise_id = c.id "
               "WHERE c.ship_id = :ship AND ({where}) ORDER BY l.timestamp DESC",
        "url": lambda r: f"/logbook/{r['id']}/edit",
        "label": lambda r: " → ".join(x for x in (r["departure_location"], r["destination_location"]) if x)
                           or "Ligne de journal",
    },
    {
        "title": "Escales",
        "section": "croisieres",
        "alias": "s",
        "fields": ["name", "locality", "notes"],
        "sql": "SELECT s.id, s.route_id, s.name, s.locality, s.notes, s.arrival_date AS date "
               "FROM stopovers s JOIN routes r ON s.route_id = r.id JOIN cruises c ON r.cruise_id = c.id "
               "WHERE c.ship_id = :ship AND ({where}) ORDER BY s.arrival_date DESC",
        "url": lambda r: f"/routes/{r['route_id']}",
        "label": lambda r: r["name"] or r["locality"] or "Escale",
        "context": lambda r: r["locality"] if r["name"] else None,
    },
    {
        "title": "Comptes",
        "section": "comptes",
        "fields": ["designation", "supplier", "expense_type", "description"],
        # Le montant se cherche aussi, sous la forme où il s'affiche (250,00).
        "amount": "unit_price",
        "sql": "SELECT id, designation, supplier, expense_type, description, date, unit_price "
               "FROM expenses WHERE ship_id = :ship AND ({where}) ORDER BY date DESC",
        "url": lambda r: f"/ship/expenses/{r['id']}",
        "label": lambda r: r["designation"] or "Dépense",
        "context": lambda r: _euros(r["unit_price"]) or None,
    },
    {
        "title": "Contacts",
        "section": "contacts",
        "fields": ["company", "contact_name", "category", "city", "country", "street",
                   "email", "phone", "notes"],
        "sql": "SELECT id, company, contact_name, category, city, country, street, email, phone, notes "
               "FROM contacts WHERE ship_id = :ship AND ({where}) "
               "ORDER BY company COLLATE NOCASE, contact_name COLLATE NOCASE",
        "url": lambda r: f"/ship/contacts/{r['id']}",
        "label": lambda r: r["company"] or r["contact_name"] or "Contact",
        "context": lambda r: r["contact_name"] if r["company"] else None,
    },
    {
        "title": "Équipiers",
        "section": "equipiers",
        "fields": ["first_name", "last_name", "city", "nationality", "email", "phone"],
        "sql": "SELECT id, first_name, last_name, city, nationality, email, phone "
               "FROM crew_members WHERE ({where}) "
               "ORDER BY last_name COLLATE NOCASE, first_name COLLATE NOCASE",
        "url": lambda r: f"/crew/{r['id']}",
        "label": lambda r: " ".join(x for x in (r["first_name"], (r["last_name"] or "").upper()) if x)
                           or "Équipier",
    },
    {
        "title": "Documents du navire",
        "section": "navire",
        "fields": ["title"],
        "sql": "SELECT id, title, path, created_at AS date "
               "FROM ship_documents WHERE ship_id = :ship AND ({where}) ORDER BY created_at DESC",
        # Le résultat ouvre le document lui-même : c'est ce qu'on cherchait.
        "url": lambda r: r["path"],
        "label": lambda r: r["title"] or "Document",
    },
    {
        "title": "Carnet de voyage",
        "section": "carnet",
        "alias": "e",
        "fields": ["text"],
        "sql": "SELECT e.id, e.text, e.cruise_id, e.created_at AS date, c.name AS cruise_name "
               "FROM carnet_entries e LEFT JOIN cruises c ON e.cruise_id = c.id "
               "WHERE e.ship_id = :ship AND ({where}) ORDER BY e.created_at DESC",
        # Ouvre le carnet de la croisière de la publication, sur elle.
        "url": lambda r: f"/carnet?croisiere={r['cruise_id'] or 'tout'}#publication-{r['id']}",
        "label": lambda r: (r["text"] or "")[:60] + ("…" if len(r["text"] or "") > 60 else ""),
        "context": lambda r: r["cruise_name"],
    },
    {
        "title": "To Do",
        "section": "todo",
        "fields": ["title", "task"],
        "sql": "SELECT id, title, task, status, due_date AS date "
               "FROM todo_items WHERE ship_id = :ship AND ({where}) ORDER BY id DESC",
        "url": lambda r: f"/ship/todo/{r['id']}/edit",
        "label": lambda r: r["title"] or "Tâche",
        "context": lambda r: r["status"],
    },
]

SEARCH_LIMIT = 50  # résultats par section au plus


@app.get("/search", response_class=HTMLResponse)
async def search(request: Request, q: Optional[str] = None):
    query = (q or "").strip()
    needle = _fold(query)
    ship_id = get_current_ship_id(request)
    sections, total = [], 0
    async with connect() as db:
        db.row_factory = aiosqlite.Row
        ship = await _fetch_ship(db, ship_id)
        # Deux lettres au moins : une seule ramènerait presque toute la base.
        if len(needle) >= 2:
            await db.create_function("fold", 1, _fold_sql, deterministic=True)
            # % et _ sont des jokers pour LIKE : on les neutralise pour qu'ils
            # soient cherchés tels quels.
            escaped = needle.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
            pattern = f"%{escaped}%"
            # Pour les montants : « 250 € », « 250.00 », « 250,00 » ou
            # « 1.234,56 » (tel qu'affiché) doivent tous trouver leur montant.
            # On retire l'euro et les espaces ; si point et virgule sont là
            # tous deux, le point sépare les milliers et s'en va ; seul, il est
            # la décimale. Et seulement s'il reste des chiffres.
            amount = re.sub(r"[\s€]", "", escaped)
            if "," in amount and "." in amount:
                amount = amount.replace(".", "")
            amount = amount.replace(".", ",")
            amount = f"%{amount}%" if re.search(r"\d", amount) else None
            user = _template_user(request)
            for sec in SEARCH_SECTIONS:
                # Une rubrique que le rang ne voit pas n'est pas fouillée.
                if not can(user, f"{sec['section']}.voir"):
                    continue
                fields = sec["fields"]
                if sec["section"] == "equipiers" and not can(user, "identite.voir"):
                    # Ni nationalité ni localité : elles relèvent de l'identité.
                    fields = [f for f in fields if f not in IDENTITY_FIELDS]
                sec = {**sec, "fields": fields}
                alias = sec.get("alias")
                where = " OR ".join(
                    f"fold({alias + '.' if alias else ''}{f}) LIKE :pattern ESCAPE '\\'"
                    for f in sec["fields"]
                )
                if sec.get("amount") and amount:
                    where += (f" OR replace(printf('%.2f', {sec['amount']}), '.', ',') "
                              f"LIKE :amount ESCAPE '\\'")
                cursor = await db.execute(
                    sec["sql"].format(where=where) + f" LIMIT {SEARCH_LIMIT + 1}",
                    {"ship": ship_id, "pattern": pattern, "amount": amount},
                )
                rows = [dict(r) for r in await cursor.fetchall()]
                if not rows:
                    continue
                results = []
                for r in rows[:SEARCH_LIMIT]:
                    label = sec["label"](r)
                    context = sec.get("context", lambda _: None)(r)
                    # L'extrait vient du premier champ trouvé qui ne figure pas
                    # déjà dans l'intitulé ou le contexte : inutile de répéter
                    # « Venise » sous « Novigrad → Venise ».
                    shown = f"{label} {context or ''}"
                    excerpt = None
                    for f in sec["fields"]:
                        if isinstance(r.get(f), str) and r[f] not in shown:
                            excerpt = _excerpt(r[f], needle)
                            if excerpt:
                                break
                    results.append({
                        "url": sec["url"](r),
                        "label": label,
                        "context": context,
                        "date": r.get("date"),
                        "excerpt": excerpt,
                    })
                total += len(results)
                sections.append({"title": sec["title"], "results": results,
                                 "more": len(rows) > SEARCH_LIMIT})
    return templates.TemplateResponse(
        "search.html",
        {
            "request": request, "active_section": None,
            "current_ship": dict(ship) if ship else None,
            "q": query, "sections": sections, "total": total,
            "too_short": 0 < len(needle) < 2,
        },
    )


# ── Connexion ─────────────────────────────────────────────────────────────────

USERNAME_RE = re.compile(r"^[A-Za-z0-9._-]{3,30}$")
# Anti-force brute : au-delà de LOGIN_MAX_FAILURES échecs en LOGIN_WINDOW
# secondes pour un même identifiant, on attend. En mémoire : un redémarrage
# remet les compteurs à zéro, ce qui suffit sur un bateau.
LOGIN_MAX_FAILURES, LOGIN_WINDOW = 5, 15 * 60
_login_failures: dict = {}


def _too_many_failures(key: str) -> Optional[int]:
    """Minutes d'attente restantes si cet identifiant est bloqué, sinon None."""
    now = time.time()
    recent = [t for t in _login_failures.get(key, []) if now - t < LOGIN_WINDOW]
    _login_failures[key] = recent
    if len(recent) >= LOGIN_MAX_FAILURES:
        return max(1, round((LOGIN_WINDOW - (now - recent[0])) / 60))
    return None


def _password_problem(password: str, confirm: Optional[str]) -> Optional[str]:
    if len(password or "") < passwords.MIN_PASSWORD_LENGTH:
        return f"Le mot de passe doit compter au moins {passwords.MIN_PASSWORD_LENGTH} caractères."
    if confirm is not None and password != confirm:
        return "Les deux mots de passe ne correspondent pas."
    return None


async def _hash(password: str) -> str:
    # scrypt prend quelques dizaines de millisecondes : hors de la boucle
    # asynchrone, pour ne pas figer les autres requêtes pendant ce temps.
    return await asyncio.to_thread(passwords.hash_password, password)


async def _check(password: str, stored: str) -> bool:
    return await asyncio.to_thread(passwords.check_password, password, stored)


async def _new_recovery_code(db) -> str:
    """Nouveau code de secours de l'Amiral ; l'ancien cesse de valoir."""
    code = passwords.new_recovery_code()
    stored = await _hash(passwords.normalize_recovery_code(code))
    await db.execute(
        "INSERT INTO app_secrets (name, value) VALUES ('admiral_recovery', ?) "
        "ON CONFLICT(name) DO UPDATE SET value = excluded.value",
        (stored,),
    )
    return code


def _auth_page(request, template: str, **context):
    return templates.TemplateResponse(template, {"request": request, "active_section": None, **context})


@app.get("/login", response_class=HTMLResponse)
async def login_form(request: Request, next: Optional[str] = None, ecrire: Optional[str] = None):
    if _template_user(request):
        return RedirectResponse(url=_safe_next(next), status_code=303)
    return _auth_page(request, "auth/login.html", next=_safe_next(next), ecrire=bool(ecrire), error=None)


@app.post("/login", response_class=HTMLResponse)
async def login(request: Request, username: str = Form(""), password: str = Form(""),
                next: Optional[str] = Form(None)):
    key = username.strip().lower()
    wait = _too_many_failures(key)
    if wait:
        return _auth_page(request, "auth/login.html", next=_safe_next(next), ecrire=False, username=username,
                          error=f"Trop d'essais pour cet identifiant : réessayez dans {wait} min.")
    async with connect() as db:
        cursor = await db.execute("SELECT id, password_hash FROM users WHERE username = ?", (username.strip(),))
        row = await cursor.fetchone()
        # Même vérification, même durée, que l'identifiant existe ou non.
        ok = await _check(password, row[1] if row else passwords.DUMMY_HASH)
        if not (row and ok):
            _login_failures.setdefault(key, []).append(time.time())
            return _auth_page(request, "auth/login.html", next=_safe_next(next), ecrire=False, username=username,
                              error="Identifiant ou mot de passe incorrect.")
        _login_failures.pop(key, None)
        token = await _start_session(db, row[0])
        await db.commit()
    response = RedirectResponse(url=_safe_next(next), status_code=303)
    _set_session_cookie(response, token)
    return response


@app.post("/logout")
async def logout(request: Request):
    token = request.cookies.get(SESSION_COOKIE)
    if token:
        async with connect() as db:
            await db.execute("DELETE FROM sessions WHERE token_hash = ?", (_token_hash(token),))
            await db.commit()
    response = RedirectResponse(url="/", status_code=303)
    response.delete_cookie(SESSION_COOKIE)
    return response


@app.get("/account", response_class=HTMLResponse)
async def account(request: Request, ok: Optional[str] = None):
    if not _template_user(request):
        return RedirectResponse(url="/login?next=/account", status_code=303)
    return _auth_page(request, "auth/account.html", error=None, ok=bool(ok))


@app.post("/account/password", response_class=HTMLResponse)
async def change_own_password(request: Request, current: str = Form(""), new: str = Form(""),
                              confirm: str = Form("")):
    user = _template_user(request)
    async with connect() as db:
        cursor = await db.execute("SELECT password_hash FROM users WHERE id = ?", (user["id"],))
        stored = (await cursor.fetchone())[0]
        if not await _check(current, stored):
            return _auth_page(request, "auth/account.html", ok=False, error="Le mot de passe actuel est incorrect.")
        problem = _password_problem(new, confirm)
        if problem:
            return _auth_page(request, "auth/account.html", ok=False, error=problem)
        await db.execute("UPDATE users SET password_hash = ? WHERE id = ?", (await _hash(new), user["id"]))
        # Les autres appareils connectés avec l'ancien mot de passe sont
        # déconnectés ; celui-ci garde sa session.
        token = request.cookies.get(SESSION_COOKIE) or ""
        await db.execute("DELETE FROM sessions WHERE user_id = ? AND token_hash != ?",
                         (user["id"], _token_hash(token)))
        await db.commit()
    return RedirectResponse(url="/account?ok=1", status_code=303)


@app.get("/recover", response_class=HTMLResponse)
async def recover_form(request: Request):
    return _auth_page(request, "auth/recover.html", error=None)


@app.post("/recover", response_class=HTMLResponse)
async def recover(request: Request, code: str = Form(""), new: str = Form(""), confirm: str = Form("")):
    """Mot de passe de l'Amiral oublié : le code de secours en donne un nouveau.
    Le code utilisé ne vaut plus ; un nouveau est montré, à noter à sa place."""
    wait = _too_many_failures("__recover__")
    if wait:
        return _auth_page(request, "auth/recover.html", error=f"Trop d'essais : réessayez dans {wait} min.")
    async with connect() as db:
        cursor = await db.execute("SELECT value FROM app_secrets WHERE name = 'admiral_recovery'")
        row = await cursor.fetchone()
        cursor = await db.execute(
            "SELECT u.id, u.crew_member_id FROM users u WHERE u.rank = 'amiral'")
        admiral = await cursor.fetchone()
        if not (row and admiral and await _check(passwords.normalize_recovery_code(code), row[0])):
            _login_failures.setdefault("__recover__", []).append(time.time())
            return _auth_page(request, "auth/recover.html", error="Code de secours incorrect.")
        problem = _password_problem(new, confirm)
        if problem:
            return _auth_page(request, "auth/recover.html", error=problem)
        await db.execute("UPDATE users SET password_hash = ? WHERE id = ?", (await _hash(new), admiral[0]))
        await db.execute("DELETE FROM sessions WHERE user_id = ?", (admiral[0],))
        new_code = await _new_recovery_code(db)
        token = await _start_session(db, admiral[0])
        await db.commit()
    _login_failures.pop("__recover__", None)
    response = _auth_page(request, "auth/recovery_code.html", code=new_code,
                          back=f"/crew/{admiral[1]}", premier=False)
    _set_session_cookie(response, token)
    return response


@app.post("/crew/{crew_id}/become-admiral", response_class=HTMLResponse)
async def become_admiral(request: Request, crew_id: int, username: str = Form(""),
                         password: str = Form(""), confirm: str = Form("")):
    """Premier compte de l'app : il devient l'Amiral. Possible seulement tant
    qu'aucun compte n'existe — après, plus personne ne peut s'en servir."""
    username = username.strip()
    async with connect() as db:
        cursor = await db.execute("SELECT COUNT(*) FROM users")
        if (await cursor.fetchone())[0]:
            raise HTTPException(status_code=403, detail="Un Amiral existe déjà")
        error = _username_problem(username) or _password_problem(password, confirm)
        if error:
            return RedirectResponse(url=f"/crew/{crew_id}?erreur={quote(error)}#acces", status_code=303)
        cursor = await db.execute(
            "INSERT INTO users (crew_member_id, username, password_hash, rank, created_at) "
            "VALUES (?, ?, ?, 'amiral', ?)",
            (crew_id, username, await _hash(password), datetime.now().strftime("%Y-%m-%d %H:%M:%S")),
        )
        user_id = cursor.lastrowid
        code = await _new_recovery_code(db)
        token = await _start_session(db, user_id)
        await db.commit()
    response = _auth_page(request, "auth/recovery_code.html", code=code, back=f"/crew/{crew_id}", premier=True)
    _set_session_cookie(response, token)
    return response


def _username_problem(username: str) -> Optional[str]:
    if not USERNAME_RE.match(username or ""):
        return "L'identifiant doit faire de 3 à 30 caractères : lettres, chiffres, point, tiret ou souligné."
    return None


def _require(request, permission: str):
    if not can(_template_user(request), permission):
        raise HTTPException(status_code=403, detail="Droit insuffisant")


@app.post("/crew/{crew_id}/rank")
async def set_crew_rank(request: Request, crew_id: int, rank: str = Form(...),
                        username: Optional[str] = Form(None), password: Optional[str] = Form(None)):
    """Changement de rang par l'Amiral, depuis la fiche.

    Mousse → autre rang : crée le compte (identifiant et mot de passe initial).
    → Mousse : supprime le compte, et ses sessions avec lui.
    → Amiral : transmission ; l'Amiral en place redevient Capitaine, dans la
    même transaction, si bien qu'il y a toujours exactement un Amiral. L'Amiral
    ne peut donc pas être rétrogradé directement : on en nomme un autre."""
    _require(request, "rangs")
    back = f"/crew/{crew_id}"
    if rank not in RANKS + ["mousse"]:
        raise HTTPException(status_code=400, detail="Rang inconnu")
    async with connect() as db:
        cursor = await db.execute("SELECT id, rank FROM users WHERE crew_member_id = ?", (crew_id,))
        account = await cursor.fetchone()
        if account and account[1] == "amiral" and rank != "amiral":
            error = "Pour changer le rang de l'Amiral, nommez d'abord un autre Amiral."
            return RedirectResponse(url=f"{back}?erreur={quote(error)}#acces", status_code=303)
        if rank == "mousse":
            if account:
                await db.execute("DELETE FROM users WHERE id = ?", (account[0],))
        else:
            if account is None:
                username = (username or "").strip()
                error = _username_problem(username) or _password_problem(password or "", None)
                if not error:
                    cursor = await db.execute("SELECT 1 FROM users WHERE username = ?", (username,))
                    if await cursor.fetchone():
                        error = f"L'identifiant « {username} » est déjà pris."
                if error:
                    return RedirectResponse(url=f"{back}?erreur={quote(error)}#acces", status_code=303)
                # Créé d'abord comme Matelot, pour qu'un Amiral nommé ainsi
                # passe par la transmission ci-dessous comme les autres.
                cursor = await db.execute(
                    "INSERT INTO users (crew_member_id, username, password_hash, rank, created_at) "
                    "VALUES (?, ?, ?, 'matelot', ?)",
                    (crew_id, username, await _hash(password), datetime.now().strftime("%Y-%m-%d %H:%M:%S")),
                )
                account = (cursor.lastrowid, "matelot")
            if rank == "amiral" and account[1] != "amiral":
                # L'ordre compte : l'index unique refuserait un second Amiral,
                # même un instant.
                await db.execute("UPDATE users SET rank = 'capitaine' WHERE rank = 'amiral'")
                await db.execute("UPDATE users SET rank = 'amiral' WHERE id = ?", (account[0],))
            elif rank != "amiral":
                await db.execute("UPDATE users SET rank = ? WHERE id = ?", (rank, account[0]))
        await db.commit()
    return RedirectResponse(url=f"{back}#acces", status_code=303)


@app.post("/crew/{crew_id}/reset-password")
async def reset_crew_password(request: Request, crew_id: int, password: str = Form("")):
    """L'Amiral redonne un mot de passe à un équipier qui a oublié le sien ;
    ses sessions ouvertes sont fermées."""
    _require(request, "rangs")
    back = f"/crew/{crew_id}"
    error = _password_problem(password, None)
    if error:
        return RedirectResponse(url=f"{back}?erreur={quote(error)}#acces", status_code=303)
    async with connect() as db:
        cursor = await db.execute("SELECT id FROM users WHERE crew_member_id = ?", (crew_id,))
        account = await cursor.fetchone()
        if account:
            await db.execute("UPDATE users SET password_hash = ? WHERE id = ?", (await _hash(password), account[0]))
            await db.execute("DELETE FROM sessions WHERE user_id = ?", (account[0],))
            await db.commit()
    return RedirectResponse(url=f"{back}?info={quote('Mot de passe réinitialisé.')}#acces", status_code=303)


@app.post("/crew/{crew_id}/username")
async def change_crew_username(request: Request, crew_id: int, username: str = Form("")):
    """L'Amiral, et lui seul, change l'identifiant d'un compte — le sien
    compris. Le mot de passe et les sessions ouvertes restent. Le rang est
    vérifié en plus du droit « rangs » : ce droit-ci ne suit pas la grille."""
    _require(request, "rangs")
    user = _template_user(request)
    if not user or user["rank"] != "amiral":
        raise HTTPException(status_code=403, detail="Réservé à l'Amiral")
    back = f"/crew/{crew_id}"
    username = username.strip()
    async with connect() as db:
        cursor = await db.execute("SELECT id, username FROM users WHERE crew_member_id = ?", (crew_id,))
        account = await cursor.fetchone()
        if account is None:
            return RedirectResponse(url=f"{back}#acces", status_code=303)
        error = _username_problem(username)
        if not error:
            # COLLATE NOCASE : « Paul » est pris par « paul », sauf par soi-même,
            # ce qui permet de ne changer que la casse.
            cursor = await db.execute("SELECT 1 FROM users WHERE username = ? AND id != ?", (username, account[0]))
            if await cursor.fetchone():
                error = f"L'identifiant « {username} » est déjà pris."
        if error:
            return RedirectResponse(url=f"{back}?erreur={quote(error)}#acces", status_code=303)
        await db.execute("UPDATE users SET username = ? WHERE id = ?", (username, account[0]))
        await db.commit()
    return RedirectResponse(url=f"{back}?info={quote(f'Identifiant changé : « {username} ».')}#acces", status_code=303)


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
async def settings_form(
    request: Request,
    sauvegarde: str = "",
    message_sauvegarde: str = "",
    signalk: str = "",
    message_signalk: str = "",
):
    """Les quatre paramètres viennent de settings_backup et settings_test_signalk.

    Les comptes rendus voyagent dans l'URL plutôt que dans un état côté serveur :
    ils s'affichent une fois et disparaissent au rechargement, ce qu'on attend
    d'un message ponctuel. Deux paires distinctes parce que les deux messages
    n'apparaissent pas au même endroit de la page.
    """
    async with connect() as db:
        db.row_factory = aiosqlite.Row
        ship = await _fetch_ship(db, get_current_ship_id(request))
    return templates.TemplateResponse(
        "settings.html",
        {
            "request": request,
            "active_section": "settings",
            "current_ship": dict(ship) if ship else None,
            "ikommunicate_host": get_ikommunicate_host() or "",
            # Toute autre valeur que 'ok' ou 'erreur' n'affiche rien.
            "sauvegarde": sauvegarde if sauvegarde in ("ok", "erreur") else "",
            "sauvegarde_message": message_sauvegarde,
            "signalk": signalk if signalk in ("ok", "erreur") else "",
            "signalk_message": message_signalk,
        },
    )


@app.post("/settings")
async def settings_save(
    request: Request,
    ikommunicate_url: Optional[str] = Form(None),
):
    save_config({"ikommunicate_url": (ikommunicate_url or "").strip()})
    return RedirectResponse(url="/settings", status_code=303)


# Le script vit à côté de main.py. Il sait se repérer seul, mais on le désigne
# par son chemin absolu : le dossier courant d'uvicorn n'est pas garanti.
BACKUP_SCRIPT = Path(__file__).resolve().parent / "backup.sh"


def _retour_settings(cle: str, etat: str, message: str) -> RedirectResponse:
    """Renvoie sur /settings avec l'état et le message en clair dans l'URL.

    `cle` vaut 'sauvegarde' ou 'signalk' : chaque compte rendu a son
    emplacement dans la page, d'où deux paires de paramètres.
    """
    return RedirectResponse(
        # Tronqué : le message finit dans une URL, et seul le résumé compte.
        url=f"/settings?{cle}={etat}&message_{cle}={quote(message[:300])}",
        status_code=303,
    )


@app.post("/settings/backup")
async def settings_backup():
    """Lance backup.sh depuis le bouton du pied de page des paramètres.

    Rien n'est dupliqué ici : le script reste la seule définition de ce qu'est
    une sauvegarde (destination, vérification, élagage), et ce handler ne fait
    que le lancer et rapporter ce qu'il a dit. Il tourne aussi bien à la main
    dans un terminal.
    """
    if not BACKUP_SCRIPT.exists():
        return _retour_settings("sauvegarde", "erreur", "backup.sh est introuvable à côté de main.py.")

    # subprocess.run bloque, et le script copie la base puis synchronise les
    # photos — quelques secondes, davantage au premier lancement. Même raison
    # que get_position() dans le traqueur : on le sort de la boucle d'événements.
    def _lancer():
        return subprocess.run(
            [str(BACKUP_SCRIPT)],
            cwd=str(BACKUP_SCRIPT.parent),
            capture_output=True,
            text=True,
            timeout=300,
        )

    try:
        resultat = await asyncio.to_thread(_lancer)
    except subprocess.TimeoutExpired:
        return _retour_settings("sauvegarde", "erreur", "La sauvegarde a dépassé 5 minutes et a été interrompue.")
    # Un bouton ne doit jamais renvoyer une page d'erreur 500 : même un script
    # non exécutable ou absent du disque se raconte dans le pied de page.
    except Exception as exc:
        return _retour_settings("sauvegarde", "erreur", f"Lancement impossible : {exc}")

    lignes = [ligne.strip() for ligne in resultat.stdout.splitlines() if ligne.strip()]

    if resultat.returncode == 0:
        # Les lignes « base : » et « photos : » sont le résumé utile ; le reste
        # (en-tête, élagage) n'apprend rien à qui vient de cliquer.
        resume = " · ".join(l for l in lignes if l.startswith(("base", "photos")))
        return _retour_settings("sauvegarde", "ok", resume or "Sauvegarde effectuée.")

    # Le script annonce ses échecs sur la sortie standard, préfixés ERREUR.
    echec = next((l for l in lignes if l.startswith("ERREUR")), "")
    return _retour_settings(
        "sauvegarde",
        "erreur",
        echec or resultat.stderr.strip() or f"Échec sans message (code {resultat.returncode}).",
    )


# Étiquettes françaises des mesures, pour nommer celles que le serveur ne
# publie pas. Les clés sont celles de get_sensor_data ; lat et long en sont
# absentes, la position étant rapportée à part.
SIGNALK_LABELS = {
    "aws": "vent apparent",
    "awa": "angle du vent apparent",
    "tws": "vent réel",
    "twa": "angle du vent réel",
    "pressure": "pression atmosphérique",
    "water_temp": "température de l'eau",
    "heading": "cap",
    "cog": "route fond",
    "log": "loch",
    "trip": "loch journalier",
    "depth": "sonde",
    "stw": "vitesse surface",
    "sog": "vitesse fond",
}


@app.post("/settings/test-signalk")
async def settings_test_signalk(ikommunicate_url: Optional[str] = Form(None)):
    """Interroge SignalK et rapporte le résultat dans la page.

    Le bouton partage le formulaire du champ d'adresse via `formaction`, donc
    la valeur tapée arrive ici et est enregistrée avant d'être testée :
    personne ne veut tester une adresse et découvrir ensuite qu'il fallait
    aussi l'enregistrer.

    Ce test existe parce que le badge « configuré » ne vérifie que la présence
    d'une chaîne, jamais la connexion — une adresse fausse s'affichait donc
    comme correcte, et les échecs ne partaient que dans le terminal du serveur.
    """
    # Enregistré tel quel, comme settings_save : une case vidée et un champ
    # absent arrivent tous deux à None avec Form(None), donc impossible de les
    # distinguer — et « Tester » sur une case effacée doit bien effacer.
    save_config({"ikommunicate_url": (ikommunicate_url or "").strip()})

    host = get_ikommunicate_host()
    if not host:
        return _retour_settings("signalk", "erreur", "Aucune adresse enregistrée.")

    # requests bloquant, comme get_position() dans le traqueur. Un hôte muet
    # coûte les 5 s du timeout de découverte, pas la somme des endpoints :
    # get_sensor_data() renonce d'emblée si la découverte échoue.
    data = await asyncio.to_thread(get_sensor_data)

    if not data:
        return _retour_settings(
            "signalk",
            "erreur",
            f"Aucune réponse de {host}. Vérifiez l'adresse et le port — "
            "SignalK écoute sur 3000 par défaut, et l'app interroge le 80 "
            "si aucun port n'est indiqué.",
        )

    mesures = [k for k in data if k not in ("lat", "long")]
    detail = f"{len(mesures)} mesure(s)"
    if data.get("lat") is not None and data.get("long") is not None:
        detail += f", position {data['lat']:.4f} / {data['long']:.4f}"
    absents = [nom for cle, nom in SIGNALK_LABELS.items() if cle not in data]
    if absents:
        detail += f". Non publié par le serveur : {', '.join(absents)}"
    return _retour_settings("signalk", "ok", f"Connecté à {host} — {detail}.")


# Run with: uvicorn main:app --reload --host 0.0.0.0 --port 8000
