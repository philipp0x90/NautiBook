# GitHub Copilot / AI Agent Instructions — NautiBook

Purpose: help AI coding agents be productive quickly in this repository by summarizing architecture, developer workflows, conventions, and concrete examples.

- **Big picture**: This is a small FastAPI web app (`main.py`) that stores a simple boat logbook in a local SQLite database (`logbook.db`). Jinja2 templates live in `templates/`. The app is run with `uvicorn` (see `run.sh`). A larger database schema is provided in `tables.sql` but the running app only uses `logbook_entries` and `trip_photos` by default.

- **Key files**:
  - `main.py` — application entrypoint, routes, DB init (`init_db`) and lifespan context.
  - `templates/` — Jinja2 templates: `home.html`, `new_entry.html`, `entry_detail.html`, `add_photo.html`.
  - `run.sh` — convenience script to run `uvicorn main:app --reload --host 0.0.0.0 --port 8000`.
  - `requirements.txt` — dependencies (FastAPI, aiosqlite, uvicorn, Jinja2, python-multipart).
  - `tables.sql` — full translated schema from FileMaker; use this as source-of-truth for broader DB additions.

- **Runtime & workflows**:
  - Development: create/activate virtualenv (project used `.boatenv` in local context), then `pip install -r requirements.txt` and run `./run.sh` or `uvicorn main:app --reload --host 0.0.0.0 --port 8000`.
  - DB init: `init_db()` runs at app lifespan startup and creates the minimal tables used by the app. There is no migration framework — if you change schema, update `init_db()` and `tables.sql` together.
  - No automated tests are present; validate changes by running the dev server and exercising the web forms.

- **Data flows & integration points**:
  - Web forms → POST endpoints in `main.py` insert into SQLite via `aiosqlite`.
  - Template rendering uses `templates = Jinja2Templates(directory="templates")` and returns `TemplateResponse` with the request plus context variables.
  - Photos are stored as paths/URLs in `trip_photos.photo_path` (no file upload handling implemented — forms expect a path or URL).

- **Important conventions & patterns** (project-specific)
  - Use async DB access with `aiosqlite.connect()` in `async with` blocks and call `await db.commit()` after writes.
  - Responses often redirect after POST using `RedirectResponse(..., status_code=303)`; maintain that pattern for form handlers.
  - Template variables assume simple Jinja2 usage (e.g., `entry.timestamp.split('.')[0]` used in templates to trim fractional seconds).
  - Field names in forms map directly to DB columns. Example field names used by the app:
    - Create entry: `position_lat`, `position_lon`, `speed`, `wind_speed`, `notes` (POST to `/logbook/new`).
    - Add photo: `photo_path`, `comment`, `added_by` (POST to `/logbook/{entry_id}/add-photo`).

- **Editing guidance for contributors / agents**
  - When adding routes that modify data, follow existing patterns: async `aiosqlite` connection, `row_factory = aiosqlite.Row` for reads, commit on writes, and redirect after POST.
  - If you add new DB tables/columns, update both `init_db()` in `main.py` and `tables.sql` so the on-start script and schema file stay consistent.
  - If you add file uploads for photos, implement a static `static/` directory and mount it in `main.py` (there is a commented example `app.mount("/static", StaticFiles(...))`).

- **Quick examples**
  - Start dev server:
    - `pip install -r requirements.txt`
    - `./run.sh` or `uvicorn main:app --reload --host 0.0.0.0 --port 8000`
  - Create entry via `curl`:
    - `curl -X POST -F "position_lat=43.7" -F "position_lon=7.26" -F "speed=5.2" -F "notes=Nice sail" http://localhost:8000/logbook/new -v`
  - Add photo via `curl`:
    - `curl -X POST -F "photo_path=/photos/sunset.jpg" -F "comment=Nice sunset" -F "added_by=me" http://localhost:8000/logbook/1/add-photo -v`

- **Where to look for examples**: Refer to `templates/new_entry.html` and `templates/add_photo.html` for exact form field names, and `main.py` route handlers for the DB queries.

If any section is unclear or you'd like me to add additional guidance (contributor checklist, code style, or example PR templates), tell me which parts to expand and I will iterate.
