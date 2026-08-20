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
ship_info ──(ship_id cookie selects the "current" ship)
cruises  (croisière — a multi-leg voyage)
  └── routes  (a single leg: departure → destination, has finished flag)
        ├── logbook_lines  (manual log entries: wind, position, depth…)
        ├── stopovers      (escales — marina stays with cost/nights)
        └── track_points   (automatic GPS breadcrumbs)
trip_photos  (linked to a logbook line via trip_id, or a route/cruise)
```

Ship-scoped tables (`todo_items`, `expenses`, `contacts`) carry a `ship_id` column; navigation tables do not — they hang off `cruise_id` / `route_id`.

Several "current" routes resolve implicitly rather than via a flag:
- **Current cruise** = the cruise with the latest `COALESCE(start_time, created_at)`.
- **Current route** = highest-`id` route in the current cruise.
- **Track recording target** = highest-`id` route where `finished IS NOT 1`.

## Architecture

- **`main.py`** (~1700 lines) — the whole app: `init_db()` schema creation, all routes grouped by section with `# ── Section ──` comment banners, and the map JSON API. Route order matters: literal paths like `/cruises/new` and `/cruises/list` are declared *before* `/cruises/{cruise_id}`.
- **`utils.py`** — SignalK HTTP client. Discovers the data endpoint from the configured host, then reads one path per field.
- **`config.py`** — reads/writes `config.yaml` (gitignored, holds only `ikommunicate_url`). `is_configured()` drives the first-run flow.
- **`templates/`** — Jinja2, all extending `base.html`, which holds the entire stylesheet inline and the nav bar.
- **`tables.sql`** — a translation of the original FileMaker schema (French table names, `ta_*`). It is *reference material only*, not the live schema and not kept in sync with `init_db()`.

There is no `static/` directory and no `StaticFiles` mount. Leaflet and other assets come from CDNs; photos are stored as path/URL strings (`photo_path`) with no upload handling.

### Unit conversion boundary

SignalK serves SI units (m/s, radians, Kelvin, meters); the logbook stores and displays knots, degrees, °C, and nautical miles. **All conversion happens in `utils.py`** via the small `_knots` / `_nm` / `_celsius` / `_signed_deg` / `_bearing_deg` helpers — nothing downstream converts. Wind angles are signed (−180..180, negative = port); headings and courses are normalised 0..360.

Angles are whole degrees and depth is one decimal. This is enforced in three places, so keep them consistent: the converters in `utils.py`, the explicit `round()` calls in `create_line()`, and the `deg` Jinja filter used for display.

### Background GPS tracker

`track_recorder_loop()` is started by the app `lifespan` and inserts a `track_points` row every `TRACK_INTERVAL` (30 s) for the newest unfinished route. `get_position()` is blocking `requests` code, so it's dispatched with `asyncio.to_thread`. The loop swallows and prints all exceptions — it must never take the app down.

### Map API

Three JSON endpoints feed Leaflet maps in the templates: `/api/routes/{id}/map-data` (→ `routes/detail.html`), `/api/cruises/{id}/map-data` (→ `cruises/detail.html`), `/api/all-cruises/map-data` (→ `tools/chart.html`). Each returns both `track_points` (auto GPS) and `logbook_points` (manual entries) so the front end can draw them differently, plus a colour picked round-robin from `ROUTE_COLORS` / `CRUISE_COLORS`.

## Conventions

**DB access** — no connection pool or shared handle: every handler opens its own `async with aiosqlite.connect(DATABASE_URL)`. Reads set `db.row_factory = aiosqlite.Row` and are converted with `dict(row)` before going into a template context; writes pass positional tuples and `await db.commit()`.

**Schema changes** — `init_db()` uses `CREATE TABLE IF NOT EXISTS` and runs on every startup, so adding a *table* just works. Adding a *column* to an existing table does not — the guard skips it, and an existing `logbook.db` keeps the old shape. Apply an `ALTER TABLE` by hand for those.

**Form handlers** — form field names match DB column names one-to-one. Every field is `Optional[...] = Form(None)`, and empty strings are normalised with `x or None` so blanks land as `NULL`. POSTs end in `RedirectResponse(..., status_code=303)`. Mutations are always plain form posts — there is no `fetch()`-based write anywhere; the only JavaScript is Leaflet and the arrival modal.

**Inline editing** — logbook-line columns are edited in place on the route page: wrap one field in its own form (never a shared one, or concurrent edits overwrite each other) that auto-submits via `onchange="this.form.submit()"`, and give it `class="inline-edit"` so it reads as plain text until hovered. All of them post to the single handler `update_line_field` at `POST /logbook/{line_id}/field/{field}`, with the form field always named `value`. **To make another column editable, add its name to `EDITABLE_LINE_FIELDS`** — the field name is interpolated into the `UPDATE`, so that set is what keeps the URL out of SQL. Currently `visual_pos` (log table) and `notes` (Journal panel). Saving reloads the page, so scroll position resets.

**Ship context** — `get_current_ship_id(request)` reads the `ship_id` cookie (defaulting to 1); `_fetch_ship(db, ship_id)` falls back to the first ship when that id is gone. Templates get the ship as `current_ship`, which `base.html` uses for the nav label.

**Template context** — pass `active_section` (`ship`, `cruises`, `routes`, `tools`, `gallery`, `settings`) so `base.html` highlights the right nav item.

**Timestamps** — stored as ISO strings in **local** time via `datetime.now()` (deliberately not UTC — a logbook records boat time). **Display filters** — custom Jinja filters registered at the top of `main.py`, all falling back to an em-dash for missing values: `datefr` (ISO → `DD/MM/YYYY`), `deg` (float → whole degrees with the `°` sign, e.g. `47.0` → `47°`), `unit(suffix)` (appends a unit, e.g. `12.4` → `12.4 kn`), and `lat` / `lon` (signed decimal degrees → DMM, `43.2891` → `43° 17.346' N`). Units belong in the *value* via these filters, not in table headers — see the log table in `routes/detail.html`. Note `unit` treats `0` as absent, matching the older `value or '—'` idiom it replaced.

Positions are **stored** as signed decimal degrees (that is what SignalK returns and what Leaflet and the `/api/*/map-data` endpoints consume) and only **displayed** as DMM. Don't convert at the storage or API layer. The `lat`/`lon` filters derive the hemisphere letter from the sign, so hemispheres must never be hardcoded in a template.

**French stored values** — `todo_items.status` holds `'A faire'` / `'Terminé'` and these literals appear in SQL `WHERE` / `ORDER BY` clauses (`main.py:632`, `main.py:1341`). Don't translate them without updating every query.

**Deletes** — most foreign keys declare `ON DELETE CASCADE`, but `PRAGMA foreign_keys = ON` is only set inside `init_db()`, not in per-handler connections. Cascades therefore do **not** fire during normal request handling; delete children explicitly, or enable the pragma on that connection.

## Stale documentation

`.github/copilot-instructions.md` describes an earlier version of the app (a `logbook_entries` table, `new_entry.html`, `POST /logbook/new`) — none of which exist. Ignore it; prefer this file. `README.md` is a stub. `todo.md` and `notes_papa.md` (French setup notes for a non-developer user) are the working notes.
