# Constructicon Timeline Feature Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a "when" dimension to Constructicon — a sticky timeline rail on the gallery page (all projects) and on each project detail page (that project's own items), backed by a manual-override-capable effective-date model exposed through both the web UI and the MCP tool surface.

**Architecture:** New nullable date-override columns on `capture_events` and `projects`, resolved through a pure helper (override → derived → fallback) that both `web/app.py`'s page routes and `mcp_server/server.py`'s tools call. One reusable vanilla-JS `TimelineRail` component, instantiated once per page with different data, rendered into a new sticky column next to each page's existing (unchanged) grid.

**Tech Stack:** Python 3.14, FastAPI/Starlette + Jinja2 (`web/app.py`, `web/templates/`), raw SQLite via `core/db.py` (no ORM, no migration framework — idempotent `ALTER TABLE` in `init_db()`), vanilla JS (no build step, no bundler, no npm), FastMCP (`mcp_server/server.py`).

**Spec:** `docs/superpowers/specs/2026-09-11-constructicon-timeline-design.md`

## Global Constraints

- No change to `project_items.sort_order` or either existing grid's layout/behavior — the rail is additive.
- `capture_events.timestamp` is `NOT NULL` — an object's effective date is always resolvable without a fallback-of-last-resort.
- Override write paths (web UI, MCP tools) must converge on the same `core/db.py` setter functions — no duplicated override-vs-reset logic.
- No automated test runner exists in this repo (no `tests/`, no `pytest`/CI config). Pure-logic pieces get a standalone assert-based script under `scripts/`, mirroring existing scripts there. Anything visual/interactive (the rail's rendering, dock magnification, spacing morph) is verified live against the `constructicon-test` container per this repo's documented recipe (`CLAUDE.md`'s "Live-testing a branch against constructicon-test" section) — never invent a fake automated test for something this repo already treats as manual-only.
- No Artifact/mockup detour for visual tuning (owner's explicit instruction) — CSS constants (thumbnail size, magnification falloff, rail width) are written as reasonable starting values and tuned live on `constructicon-test`, not pre-validated against a mockup.
- Follow the repo's serialization rule: don't run this work's live-verification steps concurrently with another agent/session also touching `constructicon-test`.

---

## File Structure

**New files:**
- `core/timeline.py` — pure effective-date/span resolution logic (`resolve_item_date`, `resolve_project_span`). Split out of `db.py` rather than added there: this logic has no SQL in it, `db.py`'s own doc comment already treats "one clear responsibility per module" as the norm (`object_types.py`, `storage.py`, `ocr.py` all split out this way), and keeping it SQL-free makes it trivially testable without a real connection for the pure-math parts.
- `scripts/test_timeline_dates.py` — standalone assert-based test script (temp SQLite DB, real `core.db` calls) for the override/resolution logic end to end.
- `web/static/js/timeline-rail.js` — the shared `TimelineRail` component.
- `web/templates/_timeline_card_modal.html` — the new card-preview modal partial (included from both `home.html` and `project_detail.html`).

**Modified files:**
- `core/db.py` — new migration columns in `init_db()`; new setter functions `set_display_date_override`, `set_project_date_overrides`.
- `web/app.py` — `home_page()` gains timeline data for the gallery rail; `project_detail_page()` gains timeline data for the project rail; `_to_project_card`/`_to_content_public` get sibling functions (not modified in place — see Task 5) for the timeline-specific shape.
- `web/templates/home.html` — mount point + data for the gallery rail, inside `.home-widget-primary`.
- `web/templates/project_detail.html` — mount point + data for the project rail; new "Timeline date" start/end override fields near the existing title/status editing UI.
- `web/templates/object_detail.html` — new "Timeline date" override field near existing description/tags editing UI; extends the existing `.modal-backdrop`/lightbox pattern (lines 291-296, 1074-1087) is reused as-is for the object edit form, not modified.
- `mcp_server/server.py` — `_to_public`/`_to_public_project` (lines 29-49, 52-62) gain date fields; `constructicon_update`/`constructicon_update_project` (lines 166-190, 358-367) gain date params + `reset_*` flags.
- `web/static/style.css` — new `.timeline-rail`, `.timeline-entry`, `.timeline-bracket`, `.timeline-card-modal` rules.

---

## Phase 1 — Data model, resolution logic, MCP tool parity

Fully backend. No UI changes yet. Each task is independently testable via `scripts/test_timeline_dates.py` (logic) or a live `constructicon-test` MCP/HTTP check (MCP tools).

### Task 1: Migration columns + effective-date/span resolution logic

**Files:**
- Create: `core/timeline.py`
- Modify: `core/db.py:271-291` (add to the existing `ALTER TABLE` migration loops in `init_db()`)
- Create: `scripts/test_timeline_dates.py`

**Interfaces:**
- Produces: `core.timeline.resolve_item_date(row: dict) -> float`, `core.timeline.resolve_project_span(project: dict, items: list[dict]) -> tuple[float, float]` — both pure functions, no DB access. `items` is the same shape `core.db.list_project_items()` already returns (a list of `_row_to_dict()`'d `capture_events` rows).

- [ ] **Step 1: Write the failing test script**

Create `scripts/test_timeline_dates.py`:

```python
"""Standalone test for the Timeline feature's effective-date resolution
logic (no pytest in this repo — see CLAUDE.md). Uses a disposable SQLite
DB, never the real imagerepo.db. Run: python scripts/test_timeline_dates.py
"""
import os
import sys
import tempfile
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import core.db as db
from core.timeline import resolve_item_date, resolve_project_span

db.DB_PATH = tempfile.mktemp(suffix=".db")
db.init_db()


def make_item(slug, timestamp, content_date=None, display_date_override=None):
    db.insert_upload(slug, None, None, "test", media_type="image")
    if content_date is not None:
        db.get_conn().execute(
            "UPDATE capture_events SET content_date = ? WHERE slug = ?", (content_date, slug)
        )
    if display_date_override is not None:
        db.set_display_date_override(slug, display_date_override)
    return db.get_by_slug(slug)


def test_item_date_falls_back_to_timestamp():
    row = make_item("t1", timestamp=1000.0)
    assert resolve_item_date(row) == 1000.0, "no content_date/override -> timestamp"


def test_item_date_prefers_content_date_over_timestamp():
    row = make_item("t2", timestamp=1000.0, content_date=500.0)
    assert resolve_item_date(row) == 500.0, "content_date beats timestamp"


def test_item_date_prefers_override_over_everything():
    row = make_item("t3", timestamp=1000.0, content_date=500.0, display_date_override=200.0)
    assert resolve_item_date(row) == 200.0, "override beats content_date and timestamp"


def test_project_span_from_items():
    a = make_item("t4a", timestamp=100.0)
    b = make_item("t4b", timestamp=300.0)
    c = make_item("t4c", timestamp=200.0)
    project = db.create_project("Span Project")
    db.add_item_to_project(project["id"], "t4a")
    db.add_item_to_project(project["id"], "t4b")
    db.add_item_to_project(project["id"], "t4c")
    items = db.list_project_items(project["id"])
    start, end = resolve_project_span(project, items)
    assert start == 100.0, "start is earliest item date"
    assert end == 300.0, "end is latest item date"


def test_project_span_empty_falls_back_to_created_at():
    project = db.create_project("Empty Project")
    start, end = resolve_project_span(project, [])
    assert start == project["created_at"] == end, "empty project collapses to a point at created_at"


def test_project_span_respects_overrides():
    a = make_item("t6a", timestamp=100.0)
    project = db.create_project("Overridden Project")
    db.add_item_to_project(project["id"], "t6a")
    db.set_project_date_overrides(project["id"], start=50.0, end=999.0)
    project = db.get_project(project["id"])
    items = db.list_project_items(project["id"])
    start, end = resolve_project_span(project, items)
    assert (start, end) == (50.0, 999.0), "explicit overrides win over derived item dates"


if __name__ == "__main__":
    tests = [v for k, v in list(globals().items()) if k.startswith("test_")]
    failures = 0
    for t in tests:
        try:
            t()
            print(f"PASS {t.__name__}")
        except AssertionError as e:
            failures += 1
            print(f"FAIL {t.__name__}: {e}")
    os.remove(db.DB_PATH)
    if failures:
        print(f"\n{failures}/{len(tests)} failed")
        sys.exit(1)
    print(f"\nAll {len(tests)} passed")
```

- [ ] **Step 2: Run it to confirm it fails**

Run: `python scripts/test_timeline_dates.py`
Expected: `ModuleNotFoundError: No module named 'core.timeline'` (module doesn't exist yet) or `AttributeError` on `db.set_display_date_override`/`db.set_project_date_overrides` (not defined yet).

- [ ] **Step 3: Add the migration columns**

In `core/db.py`, extend the existing migration loop at line 271 (the same pattern already used for `content_date` etc. at line 282) — add a new tuple line right after it:

```python
    for column, ddl_type in (("display_date_override", "REAL"),):
        if column not in existing_columns:
            conn.execute(f"ALTER TABLE capture_events ADD COLUMN {column} {ddl_type}")
```

Then, after the `projects` table is created by `SCHEMA` (it already is, unconditionally, via `executescript(SCHEMA)` at the top of `init_db()`), add a matching check for `projects`:

```python
    existing_project_columns = {row["name"] for row in conn.execute("PRAGMA table_info(projects)")}
    for column, ddl_type in (("start_date_override", "REAL"), ("end_date_override", "REAL")):
        if column not in existing_project_columns:
            conn.execute(f"ALTER TABLE projects ADD COLUMN {column} {ddl_type}")
```

Place both blocks anywhere after `existing_columns` is first computed (line 270) and before `conn.commit()` (line 334).

- [ ] **Step 4: Add the setter functions to `core/db.py`**

Add near `update_project` (after line 1207):

```python
def set_display_date_override(slug, value):
    """value=None clears the override, reverting to the computed default
    (content_date, falling back to timestamp — see core/timeline.py)."""
    conn = get_conn()
    conn.execute("UPDATE capture_events SET display_date_override = ? WHERE slug = ?", (value, slug))
    conn.commit()
    conn.close()


def set_project_date_overrides(project_id, start=..., end=...):
    """start/end=None clears that override; the ... sentinel (default)
    means "leave this one alone" — same three-state convention as
    update_project's writeup_slug param, needed because a plain
    None-means-unchanged convention can't also express "clear it"."""
    existing = get_project(project_id)
    if existing is None:
        return None
    conn = get_conn()
    new_start = existing.get("start_date_override") if start is ... else start
    new_end = existing.get("end_date_override") if end is ... else end
    conn.execute(
        "UPDATE projects SET start_date_override = ?, end_date_override = ? WHERE id = ?",
        (new_start, new_end, existing["id"]),
    )
    conn.commit()
    conn.close()
    return get_project(existing["id"])
```

- [ ] **Step 5: Create `core/timeline.py`**

```python
"""Pure effective-date resolution for the Timeline feature — no DB access.
See docs/superpowers/specs/2026-09-11-constructicon-timeline-design.md.

Objects (capture_events rows) are a single point in time. Projects are a
span: they took time, they aren't moments.
"""


def resolve_item_date(row):
    """row is a capture_events dict (core.db.get_by_slug/_row_to_dict
    shape). timestamp is NOT NULL in the schema, so this always resolves."""
    if row.get("display_date_override") is not None:
        return row["display_date_override"]
    if row.get("content_date") is not None:
        return row["content_date"]
    return row["timestamp"]


def resolve_project_span(project, items):
    """project is a core.db projects dict. items is that project's
    capture_events rows (core.db.list_project_items shape) — may be empty.
    Returns (start, end); an empty or single-moment project collapses to
    start == end, deliberately, rather than a special-cased "no span" branch."""
    start_override = project.get("start_date_override")
    end_override = project.get("end_date_override")
    if start_override is not None and end_override is not None:
        return start_override, end_override

    item_dates = [resolve_item_date(item) for item in items]
    derived_start = min(item_dates) if item_dates else project["created_at"]
    derived_end = max(item_dates) if item_dates else project["created_at"]

    start = start_override if start_override is not None else derived_start
    end = end_override if end_override is not None else derived_end
    return start, end
```

- [ ] **Step 6: Run the test script to confirm it passes**

Run: `python scripts/test_timeline_dates.py`
Expected: `All 6 passed`

- [ ] **Step 7: Commit**

```bash
git add core/db.py core/timeline.py scripts/test_timeline_dates.py
git commit -m "Add timeline date-override columns and resolution logic"
```

---

### Task 2: MCP tool read-side — expose date fields

**Files:**
- Modify: `mcp_server/server.py:29-49` (`_to_public`), `mcp_server/server.py:52-62` (`_to_public_project`)

**Interfaces:**
- Consumes: `core.timeline.resolve_item_date`, `core.timeline.resolve_project_span` (Task 1). For a project, also needs its items — `db.list_project_items(project["id"])`.
- Produces: `_to_public(row)` now includes `timestamp`, `content_date`, `display_date_override`, `effective_date`. `_to_public_project(project)` now includes `created_at`, `start_date_override`, `end_date_override`, `effective_start`, `effective_end`.

- [ ] **Step 1: Update `_to_public`**

In `mcp_server/server.py`, add the import at the top of the file (alongside the existing `from . import object_types`-style local imports, or module-level if other `core` imports are module-level — match whatever's already there for `db`):

```python
from core.timeline import resolve_item_date, resolve_project_span
```

Then extend the dict literal in `_to_public` (line 29-49):

```python
def _to_public(row):
    spec = object_types.get_object_type(row.get("media_type"))
    return {
        "slug": row["slug"],
        "url": f"{BASE_URL}/f/{row['slug']}",
        "filename": row["filename"],
        "display_name": row.get("display_name") or row["filename"] or row.get("content_description") or row["slug"],
        "icon": row.get("icon") or spec.badge_icon,
        "media_type": row.get("media_type") or "image",
        "description": row["description"],
        "tags": row["tags"],
        "client": row["client"],
        "redacted": bool(row["redacted"]),
        "source": row["source"],
        "extracted_text": row["extracted_text"],
        "ocr_status": row["ocr_status"],
        "artifact_link": f"{BASE_URL}{row['artifact_link']}" if row["artifact_link"] else None,
        "timestamp": row["timestamp"],
        "content_date": row.get("content_date"),
        "display_date_override": row.get("display_date_override"),
        "effective_date": resolve_item_date(row),
    }
```

- [ ] **Step 2: Update `_to_public_project`**

This one needs the project's items to resolve a span, so its signature changes — update its one call site too (search `_to_public_project(` in the same file; each call site has the project dict already in scope, and either already has the items list nearby (`constructicon_get_project`) or needs `db.list_project_items(project["id"])` added):

```python
def _to_public_project(project):
    items = db.list_project_items(project["id"])
    effective_start, effective_end = resolve_project_span(project, items)
    return {
        "id": project["id"],
        "slug": project["slug"],
        "title": project["title"],
        "description": project["description"],
        "status": project["status"],
        "cover_slug": project.get("cover_slug"),
        "writeup_slug": project.get("writeup_slug"),
        "parent_id": project.get("parent_id"),
        "created_at": project["created_at"],
        "start_date_override": project.get("start_date_override"),
        "end_date_override": project.get("end_date_override"),
        "effective_start": effective_start,
        "effective_end": effective_end,
    }
```

- [ ] **Step 3: Verify live against `constructicon-test`**

Deploy this branch's `mcp_server/` to `constructicon-test-mcp` per `CLAUDE.md`'s tar-over-ssh recipe, restart, then from the TrueNAS box:

```bash
sudo docker exec constructicon-test-mcp python3 -c "
import core.db as db
db.init_db()
projects = db.list_projects()
print(projects[0] if projects else 'no projects to check')
"
```

Confirm no traceback (import of `core.timeline` resolves) and, separately, call `constructicon_get` / `constructicon_get_project` through an actual MCP session (or `constructicon_search`) and confirm the response JSON now includes `effective_date` / `effective_start`/`effective_end`.

- [ ] **Step 4: Commit**

```bash
git add mcp_server/server.py
git commit -m "Expose timeline date fields through constructicon_get/get_project"
```

---

### Task 3: MCP tool write-side — override + reset

**Files:**
- Modify: `mcp_server/server.py:166-190` (`constructicon_update`), `mcp_server/server.py:358-367` (`constructicon_update_project`)

**Interfaces:**
- Consumes: `db.set_display_date_override(slug, value)`, `db.set_project_date_overrides(project_id, start=..., end=...)` (Task 1).
- Produces: `constructicon_update(..., display_date: float | None = None, reset_display_date: bool = False)`, `constructicon_update_project(..., start_date: float | None = None, reset_start_date: bool = False, end_date: float | None = None, reset_end_date: bool = False)`.

- [ ] **Step 1: Update `constructicon_update`**

```python
@mcp.tool()
def constructicon_update(slug: str, description: str | None = None, tags: list[str] | None = None,
                   display_name: str | None = None, icon: str | None = None,
                   type_metadata: dict | None = None, display_date: float | None = None,
                   reset_display_date: bool = False) -> dict | None:
    """Update an object's metadata: description, tags, display name, icon, type-specific fields,
    and/or its timeline display date.

    Pass None for any field you don't want to change. type_metadata is replaced wholesale, not
    merged — read the object's current type_metadata first if you only want to change one key.
    (The web app's POST /api/image/{slug} merges instead, via db.update_content_metadata.)
    content_description (e.g. a YouTube video's title) isn't exposed through this tool yet —
    db.update_content_metadata / POST /api/image/{slug} can change it, this tool just doesn't
    take that parameter.

    display_date sets a manual override for this object's position on the Constructicon
    timeline (unix timestamp, e.g. what time.time() or a datetime's .timestamp() returns).
    reset_display_date=True clears the override, reverting to the computed default
    (content_date, falling back to the upload timestamp) — it wins over display_date if both
    are passed.

    Returns the updated object, or None if not found.
    """
    row = db.get_by_slug(slug)
    if row is None:
        return None
    if description is not None or tags is not None:
        row = db.update_tags(slug, description=description, tags=tags, client=None)
    if display_name is not None or icon is not None:
        row = db.rename_object(slug, display_name=display_name, icon=icon)
    if type_metadata is not None:
        db.set_type_metadata(slug, type_metadata)
        row = db.get_by_slug(slug)
    if reset_display_date:
        db.set_display_date_override(slug, None)
        row = db.get_by_slug(slug)
    elif display_date is not None:
        db.set_display_date_override(slug, display_date)
        row = db.get_by_slug(slug)
    return _to_public(row) if row else None
```

- [ ] **Step 2: Update `constructicon_update_project`**

```python
@mcp.tool()
def constructicon_update_project(project_id: str | int, title: str | None = None,
                                 description: str | None = None, cover_slug: str | None = None,
                                 status: str | None = None, start_date: float | None = None,
                                 reset_start_date: bool = False, end_date: float | None = None,
                                 reset_end_date: bool = False) -> dict | None:
    """Update a project's metadata, including its timeline span.

    start_date/end_date set manual overrides for this project's position on the Constructicon
    timeline (unix timestamps). reset_start_date/reset_end_date each clear that one override,
    reverting it to the computed default (earliest/latest item date, falling back to the
    project's created_at when it has no items) — a reset flag wins over its corresponding
    date param if both are passed.

    Returns the updated project, or None if not found.
    """
    project = db.update_project(project_id, title=title, description=description,
                                cover_slug=cover_slug, status=status)
    if project is None:
        return None
    if reset_start_date or reset_end_date or start_date is not None or end_date is not None:
        new_start = None if reset_start_date else (start_date if start_date is not None else ...)
        new_end = None if reset_end_date else (end_date if end_date is not None else ...)
        project = db.set_project_date_overrides(project_id, start=new_start, end=new_end)
    return _to_public_project(project) if project else None
```

- [ ] **Step 3: Verify live against `constructicon-test`**

Through an MCP session connected to `constructicon-test-mcp`, call `constructicon_update` with `display_date` set on a real test object, confirm `constructicon_get` reflects it in `display_date_override`/`effective_date`; call again with `reset_display_date=True`, confirm it reverts. Repeat for `constructicon_update_project` with both `start_date`/`end_date`.

- [ ] **Step 4: Commit**

```bash
git add mcp_server/server.py
git commit -m "Add timeline date override/reset params to constructicon_update tools"
```

---

## Phase 2 — Web UI override fields (no rail yet)

Straightforward CRUD, reusing the Phase 1 setters. Independent of the rail component — can land and be verified before any rail UI exists.

### Task 4: Object "Timeline date" override field

**Files:**
- Modify: `web/app.py` (find the existing `POST /api/image/{slug}` handler that calls `db.update_tags`/`db.rename_object` for the object edit form — add a sibling `display_date`/`reset_display_date` handling block using the same request-parsing pattern already there)
- Modify: `web/templates/object_detail.html` (add the field near the existing description/tags edit form)

**Interfaces:**
- Consumes: `db.set_display_date_override` (Task 1).

- [ ] **Step 1: Locate the existing edit handler and form**

Read `web/app.py`'s `POST /api/image/{slug}` route and `object_detail.html`'s existing description/tags `<form>`/fetch call to match their exact request shape (form-encoded vs JSON) and JS update pattern before adding to them — this repo's edit UI already has a working save flow; extend it rather than inventing a second one.

- [ ] **Step 2: Add the override field to the form and its save handler**

Add a date-time input + "Reset" button next to the existing edit fields in `object_detail.html`, following the same inline-JS-fetch pattern the description/tags field already uses (`fetch('/api/image/' + slug, { method: 'POST', ... })`). Wire "Reset" to send a distinct `reset_display_date=true` flag rather than an empty date string, so the backend can tell "clear it" apart from "field left blank, don't touch."

In `web/app.py`'s handler for that route, add:

```python
    if reset_display_date:
        db.set_display_date_override(slug, None)
    elif display_date:
        db.set_display_date_override(slug, float(display_date))
```

(placed alongside the existing `db.update_tags(...)`/`db.rename_object(...)` calls in that same handler, matching whatever request-parsing style — `Form(...)` params vs a parsed JSON body — the rest of the handler already uses).

- [ ] **Step 3: Verify live against `constructicon-test`**

Deploy, open a real object's detail page in a browser pointed at `constructicon-test`, set a timeline date, reload, confirm it persisted; click Reset, reload, confirm it's cleared. Also confirm via `sudo docker exec constructicon-test python3 -c "import core.db as db; print(db.get_by_slug('<slug>')['display_date_override'])"`.

- [ ] **Step 4: Commit**

```bash
git add web/app.py web/templates/object_detail.html
git commit -m "Add Timeline date override field to object edit UI"
```

---

### Task 5: Project "Timeline date" start/end override fields

**Files:**
- Modify: `web/app.py` (the existing project status/title edit handler(s) — same discovery step as Task 4)
- Modify: `web/templates/project_detail.html`

**Interfaces:**
- Consumes: `db.set_project_date_overrides` (Task 1).

- [ ] **Step 1: Locate the existing project edit handler(s)**

`project_detail.html:50-51` shows the existing status `<select>` (`#project-status-select`) with an immediate-effect JS handler (line 187-208) — find its corresponding `web/app.py` route and follow the same immediate-save-on-change pattern for the two new date fields, rather than a separate "Save" button.

- [ ] **Step 2: Add both fields + their save handlers**

Two date-time inputs ("Start", "End") + a "Reset" button each, next to the status selector. Each change handler POSTs to the project's update route with the corresponding `start_date`/`reset_start_date` or `end_date`/`reset_end_date` flag, mirroring Task 4's reset-vs-blank distinction.

In `web/app.py`'s project update handler:

```python
    if reset_start_date or reset_end_date or start_date or end_date:
        db.set_project_date_overrides(
            project_id,
            start=None if reset_start_date else (float(start_date) if start_date else ...),
            end=None if reset_end_date else (float(end_date) if end_date else ...),
        )
```

- [ ] **Step 3: Verify live against `constructicon-test`**

Same pattern as Task 4, on a real project page: set/reset start and end independently, confirm via `docker exec ... python3 -c "import core.db as db; print(db.get_project('<slug>'))"`.

- [ ] **Step 4: Commit**

```bash
git add web/app.py web/templates/project_detail.html
git commit -m "Add Timeline start/end override fields to project edit UI"
```

---

## Phase 3 — Shared rail component + gallery integration

### Task 6: `TimelineRail` component

**Files:**
- Create: `web/static/js/timeline-rail.js`

**Interfaces:**
- Produces: `class TimelineRail` — `new TimelineRail(containerEl, entries, { onOpen })`. `entries: Array<{id, thumbUrl, label, date, endDate?, isChild?}>` (`endDate` present only for span entries — gallery's projects; absent for project-detail's single-point items). `onOpen(entry)` is called on click.

- [ ] **Step 1: Write the component**

```javascript
// Shared timeline rail: dormant (small, evenly spaced) vs. interactive
// (dock-magnified, time-proportional spacing) states. Used on both the
// gallery page (project spans) and a project detail page (item points).
// See docs/superpowers/specs/2026-09-11-constructicon-timeline-design.md.

class TimelineRail {
  constructor(container, entries, options = {}) {
    this.container = container;
    this.entries = entries;
    this.onOpen = options.onOpen || function () {};
    this.mode = 'dormant';
    this._idleTimer = null;
    this._render();
    this._bindEvents();
  }

  _sorted() {
    return [...this.entries].sort((a, b) => a.date - b.date);
  }

  _render() {
    this.container.innerHTML = '';
    this.container.classList.add('timeline-rail');
    this._entries = this._sorted();
    this._entries.forEach((entry) => {
      const node = document.createElement('button');
      node.type = 'button';
      node.className = 'timeline-entry' + (entry.isChild ? ' timeline-entry-child' : '');
      node.dataset.id = entry.id;

      const thumb = document.createElement('img');
      thumb.className = 'timeline-thumb';
      thumb.loading = 'lazy';
      thumb.src = entry.thumbUrl || '';
      thumb.alt = entry.label || '';
      node.appendChild(thumb);

      if (entry.endDate !== undefined && entry.endDate !== entry.date) {
        const bracket = document.createElement('span');
        bracket.className = 'timeline-bracket';
        node.appendChild(bracket);
      }

      node.addEventListener('click', () => this.onOpen(entry));
      this.container.appendChild(node);
      entry._node = node;
    });
  }

  _bindEvents() {
    this.container.addEventListener('mousemove', (e) => this._onMouseMove(e));
    this.container.addEventListener('mouseleave', () => this._scheduleDormant());
  }

  _onEngage() {
    if (this.mode !== 'interactive') {
      this.mode = 'interactive';
      this.container.classList.add('timeline-rail-interactive');
    }
    this._applyProportionalSpacing();
    this._scheduleDormant();
  }

  _scheduleDormant() {
    clearTimeout(this._idleTimer);
    this._idleTimer = setTimeout(() => this._toDormant(), 1200);
  }

  _toDormant() {
    this.mode = 'dormant';
    this.container.classList.remove('timeline-rail-interactive');
    this._entries.forEach((entry) => {
      entry._node.style.transform = '';
      entry._node.style.marginTop = '';
    });
  }

  _applyProportionalSpacing() {
    if (this._entries.length === 0) return;
    const dates = this._entries.map((e) => e.date);
    const min = Math.min(...dates);
    const max = Math.max(...dates);
    const span = max - min || 1;
    const railHeight = this.container.clientHeight || this._entries.length * 48;
    let prevPx = 0;
    this._entries.forEach((entry, i) => {
      const px = ((entry.date - min) / span) * railHeight;
      const gap = i === 0 ? 0 : Math.max(px - prevPx, 8);
      entry._node.style.marginTop = `${gap}px`;
      prevPx = px;
    });
  }

  _onMouseMove(e) {
    this._onEngage();
    const rect = this.container.getBoundingClientRect();
    const cursorY = e.clientY - rect.top;
    this._entries.forEach((entry) => {
      const node = entry._node;
      const nodeRect = node.getBoundingClientRect();
      const nodeCenterY = nodeRect.top - rect.top + nodeRect.height / 2;
      const distance = Math.abs(cursorY - nodeCenterY);
      const scale = Math.max(1, 1.8 - distance / 80);
      node.style.transform = `scale(${scale.toFixed(2)})`;
    });
  }
}
```

- [ ] **Step 2: Add supporting CSS**

Add to `web/static/style.css`:

```css
.timeline-rail {
  position: sticky;
  top: 1rem;
  width: 64px;
  max-height: calc(100vh - 2rem);
  overflow-y: auto;
  display: flex;
  flex-direction: column;
  align-items: center;
  flex-shrink: 0;
}
.timeline-entry {
  all: unset;
  cursor: pointer;
  display: block;
  width: 40px;
  height: 40px;
  border-radius: 6px;
  overflow: hidden;
  transition: transform 120ms ease;
  transform-origin: center;
}
.timeline-entry-child {
  width: 32px;
  height: 32px;
  margin-left: 12px;
}
.timeline-thumb {
  width: 100%;
  height: 100%;
  object-fit: cover;
  display: block;
}
.timeline-bracket {
  display: block;
  width: 2px;
  height: 12px;
  margin: 2px auto 0;
  background: var(--accent, #888);
}
.timeline-rail-interactive .timeline-entry {
  transition: transform 80ms ease, margin-top 200ms ease;
}
```

(`--accent` — check `style.css` for whichever CSS custom property the rest of the app already uses for its accent color, and use that name instead if different.)

- [ ] **Step 3: Verify with a throwaway inline check**

This component has no page wiring yet (Tasks 7/9 add that) and this repo has no JS test runner, so verify it loads without syntax errors the simplest available way: reference it from a scratch `<script src="/static/js/timeline-rail.js"></script>` tag temporarily added to any already-working page, load that page on `constructicon-test`, and confirm via `read_console_messages`-equivalent (or just opening browser devtools) that `TimelineRail` is defined on `window` with no parse errors. Remove the temporary script tag before committing — Task 7 adds the real one.

- [ ] **Step 4: Commit**

```bash
git add web/static/js/timeline-rail.js web/static/style.css
git commit -m "Add shared TimelineRail component"
```

---

### Task 7: Card-preview modal partial

**Files:**
- Create: `web/templates/_timeline_card_modal.html`
- Modify: `web/static/style.css` (new `.timeline-card-modal` rules, extending the existing `.modal-backdrop` pattern)

**Interfaces:**
- Produces: an `{% include "_timeline_card_modal.html" %}`-able partial with a fixed DOM id contract: `#timeline-card-modal-backdrop`, `#timeline-card-modal-close`, and content slots `#timeline-card-cover`, `#timeline-card-title`, `#timeline-card-date`, `#timeline-card-desc`, `#timeline-card-open-link` (an `<a>` for the "open full page" second click). `TimelineRail`'s `onOpen` callback (Task 6) is what populates and shows this modal — the two are wired together in Tasks 9/10, not here.

- [ ] **Step 1: Write the partial**

Reuses the exact `.modal-backdrop` class from `object_detail.html:291` (so it inherits that existing backdrop/overlay styling) with new inner content instead of a bare `<img>`:

```html
<div class="modal-backdrop timeline-card-modal" id="timeline-card-modal-backdrop">
  <button type="button" class="lightbox-close" id="timeline-card-modal-close">
    <svg width="20" height="20" viewBox="0 0 24 24" fill="none"><path d="M6 6L18 18M6 18L18 6" stroke="#ECE8DA" stroke-width="2" stroke-linecap="round"/></svg>
  </button>
  <div class="timeline-card-modal-body">
    <img class="timeline-card-cover" id="timeline-card-cover" src="" alt="">
    <div class="timeline-card-title" id="timeline-card-title"></div>
    <div class="timeline-card-date" id="timeline-card-date"></div>
    <div class="timeline-card-desc" id="timeline-card-desc"></div>
    <a class="timeline-card-open-link" id="timeline-card-open-link" href="#">Open</a>
  </div>
</div>
<script>
(function () {
  const backdrop = document.getElementById('timeline-card-modal-backdrop');
  document.getElementById('timeline-card-modal-close').addEventListener('click', () => backdrop.classList.remove('active'));
  backdrop.addEventListener('click', (e) => { if (e.target === backdrop) backdrop.classList.remove('active'); });
  document.addEventListener('keydown', (e) => { if (e.key === 'Escape') backdrop.classList.remove('active'); });
  window.openTimelineCardModal = function (entry) {
    document.getElementById('timeline-card-cover').src = entry.thumbUrl || '';
    document.getElementById('timeline-card-title').textContent = entry.label || '';
    document.getElementById('timeline-card-date').textContent = entry.dateLabel || '';
    document.getElementById('timeline-card-desc').textContent = entry.description || '';
    document.getElementById('timeline-card-open-link').href = entry.openUrl || '#';
    backdrop.classList.add('active');
  };
})();
</script>
```

- [ ] **Step 2: Add supporting CSS**

```css
.timeline-card-modal .timeline-card-modal-body {
  background: var(--surface, #1a1a1a);
  border-radius: 8px;
  padding: 1.5rem;
  max-width: 420px;
  display: flex;
  flex-direction: column;
  gap: 0.5rem;
}
.timeline-card-cover {
  width: 100%;
  max-height: 240px;
  object-fit: cover;
  border-radius: 6px;
}
```

(`--surface` — same note as Task 6: match this repo's actual existing custom-property name.)

- [ ] **Step 3: Commit**

```bash
git add web/templates/_timeline_card_modal.html web/static/style.css
git commit -m "Add shared card-preview modal partial for timeline rails"
```

---

### Task 8: Gallery timeline data (backend)

**Files:**
- Modify: `web/app.py:633-709` (`home_page`)

**Interfaces:**
- Consumes: `core.timeline.resolve_project_span` (Task 1), the already-computed `all_projects` local (line 646 — deliberately the *unfiltered* list, not the top-level-only `projects` used for the grid at line 667, since the timeline rail shows all projects with a child indicator per the spec).
- Produces: a new `"timeline_projects"` key in `home.html`'s template context: `list[{id, slug, title, cover_url, effective_start, effective_end, is_child}]`.

- [ ] **Step 1: Add the timeline data to `home_page`**

In `web/app.py`, after `all_projects = db.list_projects()` (line 646), add a helper call and pass its result into the existing `TemplateResponse` context dict (line 696-709):

```python
def _to_timeline_project(project):
    items = db.list_project_items(project["id"])
    effective_start, effective_end = resolve_project_span(project, items)
    return {
        "id": project["id"],
        "slug": project["slug"],
        "title": project["title"],
        "cover_url": _project_cover_url(project.get("cover_slug")),
        "effective_start": effective_start,
        "effective_end": effective_end,
        "is_child": project.get("parent_id") is not None,
    }
```

Add the import `from core.timeline import resolve_project_span` near the existing `core.db` import at the top of `web/app.py`. Add `"timeline_projects": [_to_timeline_project(p) for p in all_projects],` to the context dict at line 696-709 — using `all_projects` (unfiltered), not the `projects` local that's already filtered to top-level-only for the grid.

- [ ] **Step 2: Verify live against `constructicon-test`**

```bash
sudo docker exec constructicon-test python3 -c "
import core.db as db
from core.timeline import resolve_project_span
db.init_db()
for p in db.list_projects()[:3]:
    items = db.list_project_items(p['id'])
    print(p['title'], resolve_project_span(p, items), p.get('parent_id'))
"
```

Confirm no traceback and sane-looking `(start, end)` pairs.

- [ ] **Step 3: Commit**

```bash
git add web/app.py
git commit -m "Compute gallery timeline data (all projects, with child flag)"
```

---

### Task 9: Gallery timeline UI wiring

**Files:**
- Modify: `web/templates/home.html:31-54` (mount point inside `.home-widget-primary`)

**Interfaces:**
- Consumes: `TimelineRail` (Task 6), `#timeline-card-modal-backdrop`/`window.openTimelineCardModal` (Task 7, included once via `{% include "_timeline_card_modal.html" %}`), `timeline_projects` (Task 8).

- [ ] **Step 1: Add the rail's markup and data**

Inside `.home-widget-primary` (around line 46, after the `.hazard-stripe` div and before `_category_tiles.html`'s include, so the rail sits beside the grid rather than above it — wrap both in a flex row):

```html
<div class="timeline-widget-row">
  <aside class="timeline-rail" id="gallery-timeline-rail"></aside>
  <div class="timeline-widget-main">
    {% include "_category_tiles.html" %}
    {% if projects %}
    <div class="projects-container">
      <div class="featured-project" id="featured-project"></div>
      <div class="project-grid" id="projects-grid"></div>
    </div>
    {% endif %}
  </div>
</div>
```

(This wraps the existing `_category_tiles.html` include and `.projects-container` block that are already at lines 48-54 — move them inside `.timeline-widget-main` rather than duplicating them.)

Add near the bottom of `home.html`, alongside its existing inline `<script>` block:

```html
<script src="/static/js/timeline-rail.js"></script>
{% include "_timeline_card_modal.html" %}
<script>
(function () {
  const timelineProjects = {{ timeline_projects | tojson }};
  const railEl = document.getElementById('gallery-timeline-rail');
  if (railEl && timelineProjects.length) {
    new TimelineRail(
      railEl,
      timelineProjects.map((p) => ({
        id: p.id,
        thumbUrl: p.cover_url,
        label: p.title,
        date: p.effective_start,
        endDate: p.effective_end,
        isChild: p.is_child,
      })),
      {
        onOpen: (entry) => {
          const p = timelineProjects.find((x) => x.id === entry.id);
          window.openTimelineCardModal({
            thumbUrl: p.cover_url,
            label: p.title,
            dateLabel: new Date(p.effective_start * 1000).toLocaleDateString(),
            openUrl: `/project/${p.slug}`,
          });
        },
      }
    );
  }
})();
</script>
```

- [ ] **Step 2: Add the layout CSS**

```css
.timeline-widget-row {
  display: flex;
  gap: 1rem;
  align-items: flex-start;
}
.timeline-widget-main {
  flex: 1;
  min-width: 0;
}
```

- [ ] **Step 3: Verify live against `constructicon-test`**

Deploy per the tar-over-ssh recipe, restart, open the gallery page in a browser pointed at `constructicon-test`, and confirm: the rail renders with real project cover thumbnails in date order; hovering triggers magnification and the spacing morph; clicking an entry opens the card-preview modal with the right title/date/cover; the modal's "Open" link goes to that project's real detail page; child projects show the indent.

- [ ] **Step 4: Commit**

```bash
git add web/templates/home.html web/static/style.css
git commit -m "Wire the gallery timeline rail into home.html"
```

---

## Phase 4 — Project detail integration

### Task 10: Project-detail timeline data (backend)

**Files:**
- Modify: `web/app.py:922-946` (`project_detail_page`)

**Interfaces:**
- Consumes: `core.timeline.resolve_item_date` (Task 1), the already-computed `items` local (line 927).
- Produces: a new `"timeline_items"` key in `project_detail.html`'s template context: `list[{slug, thumb_url, title, effective_date}]`.

- [ ] **Step 1: Add the timeline data to `project_detail_page`**

The route already builds `items = [_to_content_public(r, project_slug=slug) for r in db.list_project_items(project["id"])]` at line 927 — `_to_content_public`'s dicts (line 549-...) don't carry the raw date fields needed to resolve an effective date, so compute the timeline list separately from the *raw* rows before they're passed through `_to_content_public`:

```python
    raw_items = db.list_project_items(project["id"])
    items = [_to_content_public(r, project_slug=slug) for r in raw_items]
    timeline_items = [
        {
            "slug": r["slug"],
            "thumb_url": f"/f/{r['slug']}/thumb" if _has_thumbnail(r) and not r.get("redacted") else None,
            "title": r.get("content_description") or r.get("description") or r.get("filename") or r["slug"],
            "effective_date": resolve_item_date(r),
        }
        for r in raw_items
    ]
```

Add `"timeline_items": timeline_items,` to the `TemplateResponse` context dict (line 936-945). Add `from core.timeline import resolve_item_date` to the same import line added in Task 8.

- [ ] **Step 2: Verify live against `constructicon-test`**

```bash
sudo docker exec constructicon-test python3 -c "
import core.db as db
from core.timeline import resolve_item_date
db.init_db()
projects = db.list_projects()
p = next((p for p in projects if db.list_project_items(p['id'])), None)
if p:
    for r in db.list_project_items(p['id']):
        print(r['slug'], resolve_item_date(r))
else:
    print('no project with items to check')
"
```

- [ ] **Step 3: Commit**

```bash
git add web/app.py
git commit -m "Compute per-project timeline data for project_detail_page"
```

---

### Task 11: Project-detail timeline UI wiring

**Files:**
- Modify: `web/templates/project_detail.html` (mount point near `.project-item-grid`)

**Interfaces:**
- Consumes: `TimelineRail` (Task 6), `#timeline-card-modal-backdrop`/`window.openTimelineCardModal` (Task 7 — only include `_timeline_card_modal.html` once per page; if `home.html`'s include pattern makes it awkward to share across pages, that's fine, each page includes its own copy, they don't conflict since only one page renders at a time), `timeline_items` (Task 10).

- [ ] **Step 1: Locate `.project-item-grid` and wrap it the same way Task 9 wrapped `.projects-container`**

Read `project_detail.html` to find the exact markup around `.project-item-grid` (mentioned in the earlier codebase exploration but not yet read line-by-line in this plan — confirm its container element before editing) and wrap it in the same `timeline-widget-row` / `timeline-widget-main` pattern Task 9 used, with `<aside class="timeline-rail" id="project-timeline-rail"></aside>` as the sibling.

- [ ] **Step 2: Add the data + wiring script**

```html
<script src="/static/js/timeline-rail.js"></script>
{% include "_timeline_card_modal.html" %}
<script>
(function () {
  const timelineItems = {{ timeline_items | tojson }};
  const railEl = document.getElementById('project-timeline-rail');
  if (railEl && timelineItems.length) {
    new TimelineRail(
      railEl,
      timelineItems.map((it) => ({
        id: it.slug,
        thumbUrl: it.thumb_url,
        label: it.title,
        date: it.effective_date,
      })),
      {
        onOpen: (entry) => {
          const it = timelineItems.find((x) => x.slug === entry.id);
          window.openTimelineCardModal({
            thumbUrl: it.thumb_url,
            label: it.title,
            dateLabel: new Date(it.effective_date * 1000).toLocaleDateString(),
            openUrl: `/object/${it.slug}`,
          });
        },
      }
    );
  }
})();
</script>
```

Note: no `endDate` passed here — project-detail entries are single points, per the spec (`TimelineRail`'s bracket rendering already checks `entry.endDate !== undefined`, so omitting it is sufficient, no extra flag needed).

- [ ] **Step 3: Verify live against `constructicon-test`**

Same verification checklist as Task 9's Step 3, on a real project's detail page: rail renders items in date order, magnification/spacing-morph work, click → modal → "Open" goes to `/object/<slug>`.

- [ ] **Step 4: Commit**

```bash
git add web/templates/project_detail.html
git commit -m "Wire the project-detail timeline rail into project_detail.html"
```

---

### Task 12: End-to-end live verification + PR

**Files:** none (verification only)

- [ ] **Step 1: Full manual pass on `constructicon-test`**

Walk both pages together: gallery timeline shows all projects (including children, indented) in start-date order with correct spans; a project's own detail page timeline shows its items in date order; setting a manual override (object and project, via both the web UI fields from Tasks 4/5 and an MCP call) visibly moves that entry on the rail after reload; resetting moves it back.

- [ ] **Step 2: Re-run the Phase 1 logic script one more time as a regression check**

Run: `python scripts/test_timeline_dates.py`
Expected: `All 6 passed`

- [ ] **Step 3: Open the PR**

Per this repo's "no issue, no fix" workflow, this work is already tracked at hooptiej/Constructicon#263. Push the branch and open a PR referencing it (`Closes #263`), following the deploy-checklist/backup discipline in `CLAUDE.md` before this ever touches `constructicon-web` (production) rather than just `constructicon-test`.

---

## Self-Review Notes

- **Spec coverage:** data model (Task 1), MCP read/write parity (Tasks 2-3), web override fields (Tasks 4-5), shared rail component + both states (Task 6), card-preview-modal click flow (Task 7), gallery integration incl. child-project indicator (Tasks 8-9), project-detail integration (Tasks 10-11), no-`sort_order`-change constraint (respected throughout — grids untouched), testing approach split between the pure-logic script and live `constructicon-test` verification (every task) — all spec sections have a corresponding task.
- **Type consistency:** `TimelineRail` entry shape (`{id, thumbUrl, label, date, endDate?, isChild?}`) is defined once in Task 6 and used identically in Tasks 9 and 11. `resolve_item_date`/`resolve_project_span` signatures defined in Task 1 are used with matching arguments in Tasks 2, 3, 8, 10.
- **Placeholder scan:** no TBD/TODO; two tasks (5's exact handler location, 11's exact `.project-item-grid` container markup) explicitly say "read the file first to confirm" rather than guessing unverified surrounding markup — that's a real first step, not a deferred placeholder, since this plan's author didn't have those exact line ranges open at write time for every single touched file.
