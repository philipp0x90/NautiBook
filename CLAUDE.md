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
trip_photos  (a route/cruise; trip_id is vestigial — see Photos)
```

**Strict single ownership, enforced in the schema.** Each level belongs to exactly one parent, all the way up: a route belongs to one cruise, so transitively to one ship. Every arrow above is a real foreign key with `ON DELETE CASCADE`, so this holds under deletion too — see the note on `connect()` below, which is what makes those cascades fire. Anything that lists cruises or routes must filter by the current ship; there is no "all ships" view.

`todo_items`, `expenses` and `contacts` also carry `ship_id`, but as a flat scope rather than part of this chain.

**`crew_members` is the one exception to ship scoping** — deliberately, since the same person may sail on more than one boat. It has no `ship_id`, and `/crew` is the only list in the app that ignores the ship cookie; don't "fix" that by adding one. Its `age` column is a FileMaker leftover that **nothing reads or writes**: the `age` Jinja filter computes it from `birth_date`, because a stored age is wrong within the year. `cruise_crew` (who embarked on which cruise, with dates and a role) is managed by the Équipage panel on the cruise page, not by `/crew` — `/crew` holds the people, `cruise_crew` their embarkations. Its `role` stores `'skipper'` or `'crew'`, **English**, matching the column's own `DEFAULT` and unlike `todo_items.status`; only the display is French. There is at most one skipper per cruise, enforced by `set_skipper` demoting the others rather than by the schema, and the first person aboard becomes skipper by default. Removing the skipper leaves the cruise without one until another is designated. `_cruise_crew(db, cruise_id)` returns both who is aboard and who could still be added. There is no `UNIQUE(cruise_id, crew_member_id)`, so duplicates are kept out in two places that both matter: the form only lists people not aboard, *and* `add_cruise_crew` re-checks before inserting — a direct POST would otherwise list the same person twice, once per role. The embarkation dates are edited in place through `EDITABLE_CREW_DATES`, the same whitelist-in-the-URL pattern as `EDITABLE_LINE_FIELDS`. The crew member's own fiche shows the other side of the join, their cruises across every ship.

**A route holds at most one stopover.** An escale is where a leg ends, so there is one per leg by construction; the plural is an artefact of the FileMaker translation. Nothing enforces it — `stopovers.route_id` has no `UNIQUE` index and `create_stopover` doesn't check — so treat a route's stopover as a single optional record (`LIMIT 1`, "+ Escale" only when there is none, edit it otherwise) rather than trusting the data. The existing screens still iterate, which is harmless on a one-per-route dataset.

**Displayed numbers are not ids.** The `001` on a cruise and the `02` on a route are positions counted among their siblings — cruises within a ship, routes within a cruise — produced by the `CRUISE_NUMBER` / `ROUTE_NUMBER` SQL snippets, which the query aliases `AS number`. Ids are `AUTOINCREMENT` and never reused, so showing them meant a fresh cruise displayed `004` after the first three were deleted. Positions renumber instead: delete one and its successors shift down. Never show a raw `id` in the interface, and never treat `number` as stable — links, form actions and foreign keys all use `id`.

Several "current" pointers resolve implicitly rather than via a flag, and all are ship-scoped:
- **Current cruise** = that ship's cruise with the latest `COALESCE(start_time, created_at)`. Ask `_current_cruise_id(db, ship_id)` rather than re-writing that `ORDER BY`; the query existed in two places before and was about to exist in three.
- **Current route** = highest-`id` route in the current cruise. The same "latest = highest id" rule picks the *previous* leg when a new route prefills its departure, and the line a new logbook entry inherits its conditions from.
- `cruise_detail` takes the ship from the *cruise row*, not the cookie, so a direct link keeps prev/next navigation coherent. It also computes `is_current` itself, because reaching the current cruise through a direct link or the prev/next arrows must show the same "Croisière en cours" label as `/cruises/current`.

**Derived, not stored.** Several columns the interface shows have no column behind them, and two that do are dead:
- `cruises.loch_start` / `loch_end` exist in the schema but **no screen writes them**. The cruise list falls back to the loch recorded on the cruise's logbook lines (`LOCH_FIRST` / `LOCH_LAST`, `MIN`/`MAX` of `logbook_lines.log` — a loch never runs backwards, so those are the first and last reading whatever order the rows were typed in). `distance` is the difference, `duration_days` a `julianday()` subtraction. Before this, the list read `c.duration_days` and `c.distance`, neither of which exists, so both columns showed an em-dash forever.
- Where a cruise starts and where it has got to come from `CRUISE_FROM` / `CRUISE_TO`, shared by the list and both cruise-page handlers. The fallback rules are deliberately asymmetric: at departure the declared `cruises.departure` wins over the first route (it is the plan, and the first route may not exist yet); at arrival the **last route wins** over `cruises.destination`, because that column is a goal and the route is a fact.

## Architecture

- **`main.py`** (~2150 lines) — the whole app: `init_db()` schema creation, all routes grouped by section with `# ── Section ──` comment banners, and the map JSON API. Route order matters: literal paths like `/cruises/new` and `/cruises/list` are declared *before* `/cruises/{cruise_id}`.
- **`utils.py`** — SignalK HTTP client. Discovers the data endpoint from the configured host, then reads one path per field.
- **`config.py`** — reads/writes `config.yaml` (gitignored, holds only `ikommunicate_url`). `is_configured()` drives the first-run flow.
- **`templates/`** — Jinja2, all extending `base.html`, which holds the entire stylesheet inline and the nav bar.
- **`tables.sql`** — a translation of the original FileMaker schema (French table names, `ta_*`). It is *reference material only*, not the live schema and not kept in sync with `init_db()`.

There is no `static/` directory: Leaflet and other assets come from CDNs. The one `StaticFiles` mount is `IMG/`, served at `/IMG` — see below.

### Photos

`trip_photos.photo_path` and `todo_items.photo_path` hold a *string used verbatim as an `<img src>`* — either an external URL or a local `/IMG/<file>`. Nothing rewrites it, so old rows holding a bare URL keep working.

Uploads land in `IMG/` (created at import time by `_save_photo`'s module block, git-ignored except `.gitkeep`) and are served by the `/IMG` mount, which is why the stored path doubles as the URL. `_save_photo(upload)` is the single entry point: it whitelists the suffix against `IMG_SUFFIXES`, slugifies the original stem, prefixes a `YYYYMMDD-HHMMSS` stamp, and suffixes `-1`, `-2`… on collision. It returns `None` when the form was submitted with no file, so every handler reads `await _save_photo(photo_file) or photo_path or None` — a chosen file wins, the text field is the fallback. Forms that accept a file need `enctype="multipart/form-data"`, and the field is always named `photo_file` alongside the text `photo_path`.

Replacing a to-do photo leaves the previous file in `IMG/`; nothing prunes orphans.

**Photos are attached from the to-do and crew forms only.** `_save_photo` has three callers: the two to-do handlers and `create_crew` / `update_crew` (which pass it through `COALESCE(?, photo_path)`, so submitting the form without choosing a file keeps the photo already stored). Attaching photos to a logbook line is gone: `line_detail.html`, `add_photo.html`, `GET /logbook/{id}` and both `add-photo` routes were removed — that screen was unreachable from anywhere anyway, was the last one in English, and displayed two columns (`speed`, `wind_speed`) absent from the schema. Photos are meant to be added from the gallery instead, but **`/gallery/add` does not exist**: the gallery's own `+ Photo` button (`templates/gallery/index.html:32`) 404s. The deleted `add_photo.html` is the natural starting point for it — recover it from git rather than writing a new form.

`trip_photos.trip_id` therefore points at nothing new. It also has **no foreign key** to `logbook_lines` (unlike `route_id`), so no cascade fires for it: `delete_line` clears matching rows by hand, the one deliberate exception to the "never delete children by hand" rule below.

### Unit conversion boundary

SignalK serves SI units (m/s, radians, Kelvin, meters); the logbook stores and displays knots, degrees, °C, and nautical miles. **All conversion happens in `utils.py`** via the small `_knots` / `_nm` / `_celsius` / `_signed_deg` / `_bearing_deg` helpers — nothing downstream converts. Wind angles are signed (−180..180, negative = port); headings and courses are normalised 0..360.

Angles are whole degrees and depth is one decimal. This is enforced in **four** places, so keep them consistent: the converters in `utils.py`, the explicit `round()` calls in `create_line()`, the identical ones in `update_line()`, and the `deg` Jinja filter used for display.

**Positions cross a second boundary, in the forms.** The new-line and edit-line forms take degrees, decimal minutes and a hemisphere in three boxes (`lat_deg` / `lat_min` / `lat_hem`, same for `lon_*`) — the format on charts and plotters. The column still holds signed decimal degrees, and only two functions convert: `_dmm_parts` splits a stored value into the three boxes (reachable from templates as the `latdmm` / `londmm` filters), `_dmm_to_dd` recomposes. Two rules live in `_dmm_to_dd` and nowhere else: **the hemisphere carries the sign**, so a minus typed into the degrees box is dropped rather than flipping the value twice, and both boxes empty means `None`, not zero. Minutes are shown to three decimals like the display filters, so re-saving an untouched form can move a position by ~30 cm.

### Background GPS tracker

**Currently disabled** — `TRACK_RECORDING = False`, so `lifespan` never starts the task. It had appended a `track_points` row every `TRACK_INTERVAL` (30 s) to the newest unfinished route, which accumulated ~19 000 rows, most of them orphaned by deletions that never cascaded. Flip the flag to resume.

The loop itself still works: `get_position()` is blocking `requests` code, so it's dispatched with `asyncio.to_thread`, and all exceptions are swallowed and printed — it must never take the app down. Note it picks its target route across all ships, since a background task has no cookie; that is only safe because one boat runs one instance.

### Map API

Three JSON endpoints feed Leaflet maps in the templates: `/api/routes/{id}/map-data` (→ `routes/detail.html`), `/api/cruises/{id}/map-data` (→ `cruises/detail.html`), `/api/all-cruises/map-data` (→ `tools/chart.html`). Each returns both `track_points` (auto GPS) and `logbook_points` (manual entries) so the front end can draw them differently, plus a colour picked round-robin from `ROUTE_COLORS` / `CRUISE_COLORS`.

## Conventions

**DB access** — no connection pool or shared handle: every handler opens its own `async with connect() as db`. Reads set `db.row_factory = aiosqlite.Row` and are converted with `dict(row)` before going into a template context; writes pass positional tuples and `await db.commit()`.

**Schema changes** — `init_db()` uses `CREATE TABLE IF NOT EXISTS` and runs on every startup, so adding a *table* just works. Adding a *column* does not: the guard skips the existing table and a live `logbook.db` keeps the old shape. Put those in `_migrate()`, which runs at the end of `init_db()` — guard each step (e.g. check `PRAGMA table_info`) because it re-runs on every startup. Update the `CREATE TABLE` too, so fresh databases skip the migration path.

**Form handlers** — form field names match DB column names one-to-one, *except* the position boxes (see the unit boundary above). Every field is `Optional[...] = Form(None)`, and empty strings are normalised with `x or None` so blanks land as `NULL`. POSTs end in `RedirectResponse(..., status_code=303)`. Mutations are always plain form posts — there is no `fetch()`-based write anywhere; the JavaScript is Leaflet, the modals, and two `onsubmit` confirmations.

Editing a logbook line duplicates `create_line`'s whole signature in `update_line` deliberately — same fields, same rounding — so **a new column has to be added to both**. Two details in `update_line`: the hour comes from an `<input type="datetime-local">`, which submits a `T` separator where stored rows hold a space, so it is normalised back before the `UPDATE` rather than leaving two shapes in the column; and `timestamp` goes through `COALESCE(?, timestamp)` so an emptied box cannot null out the one field the log table sorts on.

**Prefilled forms** — a form that opens with values already in it reads them from the previous row, never from a default:
- A new route inherits the previous leg's arrival place and engine-hour reading (`new_route_form`).
- A new logbook line inherits `sea_state`, `visibility` and `sails` from the route's latest line — nothing measures those three, and they change slowly. That is also why saving asks for confirmation, via `confirmerConditions()` in `new_line.html`: they are the only values on the form nobody typed for *this* line.
- Every such select carries a blank `<option value="">—</option>` first. Without it the browser preselects the first real option and the first line of a route silently records `Calme` / `Bonne`.
- Creating a route redirects to `/routes/{id}?nouvelle=1`; that flag does nothing but open the modal offering a first logbook line.

**Inline editing** — logbook-line columns are edited in place on the route page: wrap one field in its own form (never a shared one, or concurrent edits overwrite each other) that auto-submits via `onchange="this.form.submit()"`, and give it `class="inline-edit"` so it reads as plain text until hovered. All of them post to `update_line_field` at `POST /logbook/{line_id}/field/{field}`, with the form field always named `value`. `POST /logbook/note` is the one variant, taking `line_id` from the form body because the Journal panel picks the line in a dropdown; both share `_set_line_field`. **To make another column editable, add its name to `EDITABLE_LINE_FIELDS`** (`main.py:2073`) — the field name is interpolated into the `UPDATE`, so that set is what keeps the URL out of SQL. Currently `visual_pos` (log table) and `notes` (Journal panel). Saving reloads the page, so scroll position resets.

The cruise name uses the same one-field-form idiom on `cruises/detail.html`, but posts to its own `POST /cruises/{id}/set-name` — a dedicated route like `set-end`, not a whitelisted field name.

**Clickable rows** — `rowLink(event, url)` in `base.html` is the whole mechanism: put it in the row's `onclick` and give the row `cursor:pointer`. It ignores clicks that landed on anything interactive, which is what lets the log table's inline `visual_pos` field keep working inside a clickable row. A logbook line spans **two `<tr>`**, so the pair is wrapped in its own `<tbody class="log-line">`: that is what makes the whole line light up on hover, which a `:hover` on one `<tr>` cannot do. Both rows carry the same `onclick`, pointing at the line's edit form.

**A Jinja comment cannot go inside a tag.** Several templates loop over a list literal spelled out in the `{% for %}` itself (`ship/info.html`, `crew/detail.html`). A `{# … #}` between two entries of that list is not a comment — the tag is one expression, and the result is `TemplateSyntaxError: unexpected char '#'`, i.e. a 500 on that page only. Put the comment above the `{% for %}`. This has bitten twice; there is no linter to catch it, so a page whose template changed is worth loading once.

**Ship context** — `get_current_ship_id(request)` reads the `ship_id` cookie (defaulting to 1); `_fetch_ship(db, ship_id)` falls back to the first ship when that id is gone. Templates get the ship as `current_ship`, which `base.html` uses for the nav label.

**Template context** — pass `active_section` (`ship`, `cruises`, `crew`, `routes`, `tools`, `gallery`, `settings`) so `base.html` highlights the right nav item. Each of the six tabs maps to one section, `crew` being Équipiers — its own tab between Navire and Croisières, since crew belong to neither (`crew_members` has no `ship_id`). Six full-size tabs need ~960 px, which is why the compact-nav media query breaks at 980 px rather than the 780 it used when there were five.

**One form template for new *and* edit** — `crew/form.html` is the pattern to copy for anything with more than a handful of fields: it takes `member`, `None` meaning creation, and derives its action, title and button label from that. Compare `create_line` / `update_line`, whose twelve duplicated fields have to be kept in step by hand. The handlers still duplicate their `Form(None)` signatures, which is the house style — the saving is in the markup.

**Timestamps** — stored as ISO strings in **local** time via `datetime.now()` (deliberately not UTC — a logbook records boat time). **Display filters** — custom Jinja filters registered at the top of `main.py`, all falling back to an em-dash for missing values: `datefr` (ISO → `DD/MM/YYYY`), `jourfr` (ISO → French weekday, `2026-08-26` → `mercredi`), `age` (birth date → whole years as of today), `deg` (float → whole degrees with the `°` sign, e.g. `47.0` → `47°`), `unit(suffix)` (appends a unit, e.g. `12.4` → `12.4 kn`), `lat` / `lon` (signed decimal degrees → DMM, `43.2891` → `43° 17.346' N`), and `latdmm` / `londmm` (the same split as a dict, for form boxes). Units belong in the *value* via these filters, not in table headers — see the log table in `routes/detail.html`.

Two traps in that list. `unit` treats `0` as absent, matching the older `value or '—'` idiom it replaced — and so does any `{{ x or '—' }}`, which is why the cruise list tests `is not none` instead: a same-day cruise really does last 0 days, and a boat that did not move really did cover 0 NM. And `jourfr` hardcodes the French weekday names rather than calling `strftime('%A')`, which would depend on a French locale being installed *and* selected on the Pi — otherwise the day comes out in English.

Positions are **stored** as signed decimal degrees (that is what SignalK returns and what Leaflet and the `/api/*/map-data` endpoints consume) and only **displayed** or **entered** as DMM. Don't convert at the storage or API layer. The `lat`/`lon`/`latdmm`/`londmm` filters derive the hemisphere letter from the sign, so hemispheres must never be hardcoded in a template. `_lon` pads degrees to two digits like `_lat`, not three — the width is a minimum, so a longitude past 100° keeps its third digit.

**French stored values** — `todo_items.status` holds `'A faire'` / `'Terminé'` and these literals appear in SQL `WHERE` / `ORDER BY` clauses (`main.py:832`, `main.py:933`, `main.py:1676` among others). Don't translate them without updating every query.

**Deletes** — the schema's `ON DELETE CASCADE` clauses do the work; never delete children by hand. This depends entirely on `PRAGMA foreign_keys = ON`, which SQLite defaults to *off per connection*: a bare `aiosqlite.connect()` makes every cascade a silent no-op. That was the case for a long time and it orphaned ~19 000 rows. **Always open the database with the `connect()` helper**, never `aiosqlite.connect()` directly.

The single exception is `delete_line`, which deletes `trip_photos` rows by `trip_id` itself — that column has no foreign key, so there is no cascade to rely on. Where a cascade *does* exist, deleting by hand is still wrong.

## Stale documentation

`.github/copilot-instructions.md` describes an earlier version of the app (a `logbook_entries` table, `new_entry.html`, `POST /logbook/new`) — none of which exist. Ignore it; prefer this file. `README.md` is a stub. `todo.md` and `notes_papa.md` (French setup notes for a non-developer user) are the working notes.
