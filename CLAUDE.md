# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project

NautiBook is a web-based boat logbook (FastAPI + SQLite + server-rendered Jinja2) that records navigation data and pulls live sensor readings from a SignalK server on the boat's network (an iKommunicate gateway). It runs on a Raspberry Pi aboard the vessel (see `nautibook.service`).

**The UI is entirely in French.** Labels, headings, and — importantly — some *stored* values are French. Keep new user-facing strings in French.

## Commands

```bash
source .venv/bin/activate     # the venv is .venv (run.sh depends on this name)
pip install -r requirements.txt
./run.sh                      # uvicorn main:app --reload --host 0.0.0.0 --port 8000
```

`--reload` picks up Python and template edits without a restart. There are no tests, linters, or migration tooling.

## Domain model

The data hierarchy is the thing to understand first:

```
ship_info  (the ship_id cookie selects the "current" one)
  └── cruises  (croisière — a multi-leg voyage)
        └── routes  (a single leg: departure → destination, has finished flag)
              ├── logbook_lines  (manual log entries: wind, position, depth…)
              ├── stopovers      (escales — marina stays with cost/nights)
              └── track_points   (automatic GPS breadcrumbs)
trip_photos  (linked to a logbook line via trip_id, or a route/cruise)
```

**Strict single ownership, enforced in the schema.** Each level belongs to exactly one parent, all the way up: a route belongs to one cruise, so transitively to one ship. Every arrow above is a real foreign key with `ON DELETE CASCADE`, so this holds under deletion too — see the note on `connect()` below, which is what makes those cascades fire. Anything that lists cruises or routes must filter by the current ship; there is no "all ships" view.

`todo_items`, `expenses` and `contacts` also carry `ship_id`, but as a flat scope rather than part of this chain.

**Displayed numbers are not ids.** The `001` on a cruise and the `02` on a route are positions counted among their siblings — cruises within a ship, routes within a cruise — produced by the `CRUISE_NUMBER` / `ROUTE_NUMBER` SQL snippets, which the query aliases `AS number`. Ids are `AUTOINCREMENT` and never reused, so showing them meant a fresh cruise displayed `004` after the first three were deleted. Positions renumber instead: delete one and its successors shift down. Never show a raw `id` in the interface, and never treat `number` as stable — links, form actions and foreign keys all use `id`.

Several "current" pointers resolve implicitly rather than via a flag, and all are ship-scoped:
- **Current cruise** = that ship's cruise with the latest `COALESCE(start_time, created_at)`.
- **Current route** = highest-`id` route in the current cruise.
- `cruise_detail` takes the ship from the *cruise row*, not the cookie, so a direct link keeps prev/next navigation coherent.

## Architecture

- **`main.py`** (~1700 lines) — the whole app: `init_db()` schema creation, all routes grouped by section with `# ── Section ──` comment banners, and the map JSON API. Route order matters: literal paths like `/cruises/new` and `/cruises/list` are declared *before* `/cruises/{cruise_id}`.
- **`utils.py`** — SignalK HTTP client. Discovers the data endpoint from the configured host, then reads one path per field.
- **`config.py`** — reads/writes `config.yaml` (gitignored, holds only `ikommunicate_url`). `is_configured()` drives the first-run flow.
- **`templates/`** — Jinja2, all extending `base.html`, which holds the entire stylesheet inline and the nav bar.
- **`tables.sql`** — a translation of the original FileMaker schema (French table names, `ta_*`). It is *reference material only*, not the live schema and not kept in sync with `init_db()`.

There is no `static/` directory: Leaflet and other assets come from CDNs. The one `StaticFiles` mount is `IMG/`, served at `/IMG` — see below.

### Photos

`trip_photos.photo_path` and `todo_items.photo_path` hold a *string used verbatim as an `<img src>`* — either an external URL or a local `/IMG/<file>`. Nothing rewrites it, so old rows holding a bare URL keep working.

Uploads land in `IMG/` (created at import time by `_save_photo`'s module block, git-ignored except `.gitkeep`) and are served by the `/IMG` mount, which is why the stored path doubles as the URL. `_save_photo(upload)` is the single entry point: it whitelists the suffix against `IMG_SUFFIXES`, slugifies the original stem, prefixes a `YYYYMMDD-HHMMSS` stamp, and suffixes `-1`, `-2`… on collision. It returns `None` when the form was submitted with no file, so every handler reads `await _save_photo(photo_file) or photo_path or None` — a chosen file wins, the text field is the fallback. Forms that accept a file need `enctype="multipart/form-data"`, and the field is always named `photo_file` alongside the text `photo_path`.

Replacing a to-do photo leaves the previous file in `IMG/`; nothing prunes orphans.

### Unit conversion boundary

SignalK serves SI units (m/s, radians, Kelvin, meters); the logbook stores and displays knots, degrees, °C, and nautical miles. **All conversion happens in `utils.py`** via the small `_knots` / `_nm` / `_celsius` / `_signed_deg` / `_bearing_deg` helpers — nothing downstream converts. Wind angles are signed (−180..180, negative = port); headings and courses are normalised 0..360.

Angles are whole degrees and depth is one decimal. This is enforced in three places, so keep them consistent: the converters in `utils.py`, the explicit `round()` calls in `create_line()`, and the `deg` Jinja filter used for display.

### Background GPS tracker

**Currently disabled** — `TRACK_RECORDING = False`, so `lifespan` never starts the task. It had appended a `track_points` row every `TRACK_INTERVAL` (30 s) to the newest unfinished route, which accumulated ~19 000 rows, most of them orphaned by deletions that never cascaded. Flip the flag to resume.

The loop itself still works: `get_position()` is blocking `requests` code, so it's dispatched with `asyncio.to_thread`, and all exceptions are swallowed and printed — it must never take the app down. Note it picks its target route across all ships, since a background task has no cookie; that is only safe because one boat runs one instance.

### Map API

Three JSON endpoints feed Leaflet maps in the templates: `/api/routes/{id}/map-data` (→ `routes/detail.html`), `/api/cruises/{id}/map-data` (→ `cruises/detail.html`), `/api/all-cruises/map-data` (→ `tools/chart.html`). Each returns both `track_points` (auto GPS) and `logbook_points` (manual entries) so the front end can draw them differently, plus a colour picked round-robin from `ROUTE_COLORS` / `CRUISE_COLORS`.

## Conventions

**DB access** — no connection pool or shared handle: every handler opens its own `async with connect() as db`. Reads set `db.row_factory = aiosqlite.Row` and are converted with `dict(row)` before going into a template context; writes pass positional tuples and `await db.commit()`.

**Schema changes** — `init_db()` uses `CREATE TABLE IF NOT EXISTS` and runs on every startup, so adding a *table* just works. Adding a *column* does not: the guard skips the existing table and a live `logbook.db` keeps the old shape. Put those in `_migrate()`, which runs at the end of `init_db()` — guard each step (e.g. check `PRAGMA table_info`) because it re-runs on every startup. Update the `CREATE TABLE` too, so fresh databases skip the migration path.

**Form handlers** — form field names match DB column names one-to-one. Every field is `Optional[...] = Form(None)`, and empty strings are normalised with `x or None` so blanks land as `NULL`. POSTs end in `RedirectResponse(..., status_code=303)`. Mutations are always plain form posts — there is no `fetch()`-based write anywhere; the only JavaScript is Leaflet and the arrival modal.

**Inline editing** — logbook-line columns are edited in place on the route page: wrap one field in its own form (never a shared one, or concurrent edits overwrite each other) that auto-submits via `onchange="this.form.submit()"`, and give it `class="inline-edit"` so it reads as plain text until hovered. All of them post to `update_line_field` at `POST /logbook/{line_id}/field/{field}`, with the form field always named `value`. `POST /logbook/note` is the one variant, taking `line_id` from the form body because the Journal panel picks the line in a dropdown; both share `_set_line_field`. **To make another column editable, add its name to `EDITABLE_LINE_FIELDS`** — the field name is interpolated into the `UPDATE`, so that set is what keeps the URL out of SQL. Currently `visual_pos` (log table) and `notes` (Journal panel). Saving reloads the page, so scroll position resets.

**Ship context** — `get_current_ship_id(request)` reads the `ship_id` cookie (defaulting to 1); `_fetch_ship(db, ship_id)` falls back to the first ship when that id is gone. Templates get the ship as `current_ship`, which `base.html` uses for the nav label.

**Template context** — pass `active_section` (`ship`, `cruises`, `routes`, `tools`, `gallery`, `settings`) so `base.html` highlights the right nav item.

**Timestamps** — stored as ISO strings in **local** time via `datetime.now()` (deliberately not UTC — a logbook records boat time). **Display filters** — custom Jinja filters registered at the top of `main.py`, all falling back to an em-dash for missing values: `datefr` (ISO → `DD/MM/YYYY`), `deg` (float → whole degrees with the `°` sign, e.g. `47.0` → `47°`), `unit(suffix)` (appends a unit, e.g. `12.4` → `12.4 kn`), and `lat` / `lon` (signed decimal degrees → DMM, `43.2891` → `43° 17.346' N`). Units belong in the *value* via these filters, not in table headers — see the log table in `routes/detail.html`. Note `unit` treats `0` as absent, matching the older `value or '—'` idiom it replaced.

Positions are **stored** as signed decimal degrees (that is what SignalK returns and what Leaflet and the `/api/*/map-data` endpoints consume) and only **displayed** as DMM. Don't convert at the storage or API layer. The `lat`/`lon` filters derive the hemisphere letter from the sign, so hemispheres must never be hardcoded in a template.

**French stored values** — `todo_items.status` holds `'A faire'` / `'Terminé'` and these literals appear in SQL `WHERE` / `ORDER BY` clauses (`main.py:632`, `main.py:1341`). Don't translate them without updating every query.

**Deletes** — the schema's `ON DELETE CASCADE` clauses do the work; never delete children by hand. This depends entirely on `PRAGMA foreign_keys = ON`, which SQLite defaults to *off per connection*: a bare `aiosqlite.connect()` makes every cascade a silent no-op. That was the case for a long time and it orphaned ~19 000 rows. **Always open the database with the `connect()` helper**, never `aiosqlite.connect()` directly.

## Stale documentation

`.github/copilot-instructions.md` describes an earlier version of the app (a `logbook_entries` table, `new_entry.html`, `POST /logbook/new`) — none of which exist. Ignore it; prefer this file. `README.md` is a stub. `todo.md` and `notes_papa.md` (French setup notes for a non-developer user) are the working notes.
