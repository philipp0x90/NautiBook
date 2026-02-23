# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project

NautiBook is a web-based boat logbook built with FastAPI and SQLite. It records navigation data (wind, position, speed, etc.) and integrates with a SignalK marine data server for sensor auto-population.

## Commands

```bash
# Setup
python3 -m venv .boatenv
source .boatenv/bin/activate
pip install -r requirements.txt

# Run dev server
./run.sh
# or directly:
uvicorn main:app --reload --host 0.0.0.0 --port 8000
```

There are no automated tests or linters configured.

## Architecture

All application logic lives in two files:

- **`main.py`** — FastAPI app, all HTTP routes, DB schema init, and form handlers
- **`utils.py`** — Fetches real-time sensor data from a SignalK server (demo.signalk.org) to pre-populate new entry forms

Templates are Jinja2 server-rendered HTML in `templates/`. The base layout is `base.html`.

Database is SQLite (`logbook.db`), accessed via `aiosqlite` with async/await throughout. The full intended schema is documented in `tables.sql`, but `init_db()` in `main.py` only creates the four currently active tables: `cruises`, `routes`, `logbook_lines`, `trip_photos`.

## Key Patterns

**Database reads** use `db.row_factory = aiosqlite.Row` to access columns by name. **Writes** use positional tuples.

**Form handlers** map HTML form fields directly to database columns via FastAPI `Form(None)` parameters (all optional). After a successful POST, handlers redirect with `303`.

**Timestamps** are stored as ISO datetime strings; templates strip microseconds with `.split('.')[0]`.

Foreign keys are enabled per-connection via `PRAGMA foreign_keys = ON`.

## Routes

| Method | Path | Purpose |
|--------|------|---------|
| GET | `/` | List 20 most recent log entries |
| GET/POST | `/logbook/new_line` | Create new log entry (form pre-populated from SignalK) |
| GET | `/logbook/{line_id}` | View entry details and associated photos |
| GET/POST | `/logbook/{line_id}/add-photo` | Add photo reference to an entry |
