#!/usr/bin/env python3
"""#704 "Imagerepo exorcism, part 2": the dead columns and tables go, but never blind.

Throwaway DBs only (scripts/_testenv.py), no server:

    python scripts/test_drop_columns_704.py

capture_events.source / client / ticket_id / artifact_link / icon and the clients / client_domains
tables are dropped at web boot by db._drop_dead_columns_704 (init_db, migrate=True), which counts
first and KEEPS any column or table that holds real data, with a loud warning. Covers:
  1. a fresh install: none of them exist, the step is recorded, nothing is logged as dropped;
  2. an install shaped like prod (2026-10-17: client/ticket_id/icon empty, artifact_link always
     /f/<slug>, source only 'screenshot'/'external', three placeholder clients, no domains):
     everything drops, rows and search survive, the search triggers come back, integrity ok;
     an undo of an item edit and of a delete logged BEFORE the drop still works after it;
  3. real data in each guarded column / table in turn: that one is kept with the warning, the
     others drop, the app still starts and still writes items; a rerun warns again and drops it
     once its data is cleared;
  4. a rerun on a finished DB changes nothing (schema and change log identical);
  5. the web and MCP payloads no longer carry the fields; /api/clients is gone; old form fields
     are ignored;
  6. search still works (API and MCP), including re-indexing after a write.
Exits 1 if any check fails.
"""

import contextlib
import io
import json
import os
import sqlite3
import sys
from pathlib import Path

import _testenv  # noqa: E402  (scripts/_testenv.py: temp DB + storage + exports, refuses otherwise)
TMP = _testenv.isolate("drop704-")
HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)
sys.path.insert(0, HERE)

try:
    import cairosvg  # noqa: F401
except Exception:  # silent-ok: no libcairo here (e.g. Windows); object types import it, nothing here renders SVG
    import types
    sys.modules["cairosvg"] = types.ModuleType("cairosvg")

from core import actor, cards, db, items  # noqa: E402
_testenv.assert_isolated()

FAILS = []
DEAD_COLS = ("source", "client", "ticket_id", "artifact_link", "icon")
DEAD_TABLES = ("clients", "client_domains")
GONE_KEYS = ("source", "client", "ticket_id", "artifact_link")
HOST = "testhost.local"
BASE = f"http://{HOST}"


def check(name, cond, extra=""):
    print(("PASS " if cond else "FAIL ") + name + (f"  [{extra}]" if (extra and not cond) else ""))
    if not cond:
        FAILS.append(name)


def use(path):
    db.DB_PATH = Path(path)


def raw(sql, *args):
    c = sqlite3.connect(db.DB_PATH)
    try:
        return c.execute(sql, args).fetchall()
    finally:
        c.close()


def item_cols():
    return [r[1] for r in raw("PRAGMA table_info(capture_events)")]


def tables():
    return {r[0] for r in raw("SELECT name FROM sqlite_master WHERE type = 'table'")}


def schema():
    return sorted(raw("SELECT type, name, sql FROM sqlite_master WHERE name NOT LIKE 'sqlite_%' ORDER BY type, name"))


def change_rows(op="migration_drop_dead_columns_704"):
    return raw("SELECT id, form_body FROM audit_log WHERE op = ?", op)


def boot():
    """init_db as the web app runs it; returns (stdout text, exception or None)."""
    buf = io.StringIO()
    err = None
    with contextlib.redirect_stdout(buf):
        try:
            with actor.acting_as(actor.ACTOR_SYSTEM):
                db.init_db()
        except Exception as e:  # silent-ok: the test reports it as a failed check below
            err = e
    return buf.getvalue(), err


def make_old_install(path, rows):
    """A current DB put back into the pre-#704 shape (old columns + their indexes, the client tables
    with the three rows ensure_special_clients seeded), with `rows` = [{slug, ...old column values}]
    inserted. The #704 step's record is removed, so the next init_db runs it as on a real upgrade."""
    use(path)
    with actor.acting_as(actor.ACTOR_SYSTEM):
        db.init_db()
    c = sqlite3.connect(path, isolation_level=None)
    c.execute("BEGIN IMMEDIATE")
    for col, ddl in (("source", "TEXT NOT NULL DEFAULT 'screenshot'"), ("client", "TEXT"), ("ticket_id", "TEXT"),
                     ("artifact_link", "TEXT"), ("icon", "TEXT")):
        c.execute(f"ALTER TABLE capture_events ADD COLUMN {col} {ddl}")
    c.execute("CREATE INDEX idx_capture_events_source ON capture_events(source)")
    c.execute("CREATE INDEX idx_capture_events_client ON capture_events(client)")
    c.execute("CREATE INDEX idx_capture_events_ticket ON capture_events(ticket_id)")
    c.execute("CREATE TABLE clients (name TEXT PRIMARY KEY, category TEXT NOT NULL, nickname TEXT)")
    c.execute("CREATE TABLE client_domains (client_name TEXT NOT NULL, domain TEXT NOT NULL, "
              "PRIMARY KEY (client_name, domain))")
    for name in ("Unknown", "Not Business", "Internal Infrastructure"):
        c.execute("INSERT INTO clients (name, category) VALUES (?, 'special')", (name,))
    c.execute("DELETE FROM schema_migrations WHERE name = ?", (db.MIGRATION_704,))
    c.execute("COMMIT")
    c.close()
    for r in rows:
        slug = r["slug"]
        db.insert_upload(slug, r.get("filename", f"{slug}.png"), None, "tester", media_type="link",
                         external_url=f"https://example.com/{slug}", content_description=r.get("title", slug))
        sets = {k: v for k, v in r.items() if k in DEAD_COLS}
        sets.setdefault("artifact_link", f"/f/{slug}")
        sets.setdefault("source", "screenshot")
        c = sqlite3.connect(path)
        c.execute(f"UPDATE capture_events SET {', '.join(k + ' = ?' for k in sets)} WHERE slug = ?",
                  [*sets.values(), slug])
        c.commit()
        c.close()


PROD_ROWS = [
    {"slug": "p1", "title": "Mulberry gearbox", "source": "screenshot"},
    {"slug": "p2", "title": "Quillbadger chassis", "source": "external", "client": "", "ticket_id": ""},
    {"slug": "p3", "title": "Absolute hotlink", "artifact_link": "http://constructicon.local/f/p3"},
    {"slug": "p4", "title": "Https hotlink", "artifact_link": "https://example.org/f/p4", "icon": ""},
    {"slug": "p5", "title": "No link at all", "artifact_link": None, "source": ""},
]

# ---- 1. fresh install ----------------------------------------------------------------------------
print("--- 1. a fresh install ---")
use(Path(TMP) / "fresh.db")
out, err = boot()
check("fresh: init_db ran cleanly", err is None, repr(err))
check("fresh: capture_events has none of the dead columns", not set(DEAD_COLS) & set(item_cols()), item_cols())
check("fresh: no clients / client_domains tables", not set(DEAD_TABLES) & tables())
check("fresh: the #704 step is recorded", raw("SELECT 1 FROM schema_migrations WHERE name = ?", db.MIGRATION_704) != [])
check("fresh: no change-log row (nothing was dropped or kept)", change_rows() == [])
check("fresh: nothing said about #704", db.MIGRATION_704 not in out, out[-300:])
rep = db.dead_data_report_704()
check("fresh: the report says nothing is present", not any(i["present"] for i in rep.values()), rep)

# ---- 2. an install shaped like prod --------------------------------------------------------------
print("--- 2. an install shaped like prod: everything drops ---")
PROD = Path(TMP) / "prod.db"
make_old_install(PROD, PROD_ROWS)
check("prod-shape: starts with every dead column and table", set(DEAD_COLS) <= set(item_cols())
      and set(DEAD_TABLES) <= tables(), item_cols())
rep = db.dead_data_report_704()
check("prod-shape: the report finds no real data anywhere", all(i["real"] == 0 for i in rep.values()), rep)
check("prod-shape: ... though the columns hold values (source, artifact_link) and clients has 3 rows",
      rep["source"]["rows"] == 4 and rep["artifact_link"]["rows"] == 4 and rep["clients"]["rows"] == 3, rep)
# a change logged BEFORE the drop: an edit and a delete, both undone after it
with actor.acting_as(actor.ACTOR_UI):
    edit = items.update("p1", description="before the drop")
    gone = items.delete(["p5"])
pre_img = json.loads(raw("SELECT mutations FROM audit_log WHERE batch_id = ?", gone.batch_id)[0][0])
check("prod-shape: the pre-drop delete's row image names the old columns (so its undo must skip them)",
      any("client" in (m.get("before") or {}) and "source" in (m.get("before") or {}) for m in pre_img),
      [sorted(m.get("before") or {}) for m in pre_img][:2])
before_count = raw("SELECT COUNT(*) FROM capture_events")[0][0]
out, err = boot()
check("prod-shape: init_db ran cleanly (the app starts)", err is None, repr(err))
check("prod-shape: every dead column is gone", not set(DEAD_COLS) & set(item_cols()), item_cols())
check("prod-shape: both client tables are gone", not set(DEAD_TABLES) & tables())
check("prod-shape: the indexes on them are gone",
      not raw("SELECT name FROM sqlite_master WHERE name IN ('idx_capture_events_source', "
              "'idx_capture_events_client', 'idx_capture_events_ticket')"))
check("prod-shape: no rows lost", raw("SELECT COUNT(*) FROM capture_events")[0][0] == before_count)
check("prod-shape: the drop is logged", "dropped source, client, ticket_id, artifact_link, icon, clients, client_domains" in out,
      out[-400:])
check("prod-shape: no warning", "WARNING" not in out, out[-400:])
rows = change_rows()
details = json.loads(rows[0][1]) if rows else {}
check("prod-shape: ONE change-log row with what was dropped and the counts",
      len(rows) == 1 and details.get("details", details).get("kept") == {}
      and len(details.get("details", details).get("dropped", [])) == 7, rows)
prod_objects = {(t, n) for t, n in raw("SELECT type, name FROM sqlite_master WHERE name NOT LIKE 'sqlite_%'")}
use(Path(TMP) / "fresh.db")
ref_objects = {(t, n) for t, n in raw("SELECT type, name FROM sqlite_master WHERE name NOT LIKE 'sqlite_%'")}
ref_cols = item_cols()
use(PROD)
check("prod-shape: same tables, indexes, views and triggers as a fresh DB", prod_objects == ref_objects,
      f"missing {sorted(ref_objects - prod_objects)}, extra {sorted(prod_objects - ref_objects)}")
check("prod-shape: capture_events has a fresh DB's columns", sorted(item_cols()) == sorted(ref_cols))
check("prod-shape: integrity_check ok", raw("PRAGMA integrity_check") == [("ok",)])
idx = db.check_search_index()
check("prod-shape: the search index is in step with the items", not idx["missing"] and not idx["stale"], idx)
check("prod-shape: search finds an item by name", [r["slug"] for r in db.search("mulberry")] == ["p1"])
with actor.acting_as(actor.ACTOR_UI):
    items.update("p2", display_name="Renamed zanzibarite")
check("prod-shape: a write after the drop re-indexes (the triggers are back)",
      [r["slug"] for r in db.search("zanzibarite")] == ["p2"])
with actor.acting_as(actor.ACTOR_UI):
    try:
        cards.undo(edit.batch_id)
        undo_err = None
    except Exception as e:  # silent-ok: reported by the check below
        undo_err = e
check("prod-shape: undoing an edit logged before the drop works", undo_err is None
      and db.get_by_slug("p1")["description"] == "", repr(undo_err))
with actor.acting_as(actor.ACTOR_UI):
    try:
        cards.undo(gone.batch_id)
        undo_err = None
    except Exception as e:  # silent-ok: reported by the check below
        undo_err = e
check("prod-shape: undoing a delete logged before the drop brings the item back", undo_err is None
      and db.get_by_slug("p5") is not None, repr(undo_err))

# ---- 4. a rerun changes nothing ------------------------------------------------------------------
print("--- 4. a rerun changes nothing ---")
s0, n0 = schema(), raw("SELECT COUNT(*) FROM audit_log")[0][0]
out, err = boot()
check("rerun: clean", err is None, repr(err))
check("rerun: the schema is identical", schema() == s0)
check("rerun: no new change-log row", raw("SELECT COUNT(*) FROM audit_log")[0][0] == n0)
check("rerun: nothing said about #704", db.MIGRATION_704 not in out, out[-300:])
res = db._drop_dead_columns_704()
check("rerun (direct): nothing dropped, nothing kept", res["dropped"] == [] and res["kept"] == {}, res)

# ---- 3. real data in each guarded column / table -------------------------------------------------
print("--- 3. real data: that one is kept, loudly; the others drop ---")
REAL = {
    "client": ([{"slug": "r1", "client": "Treeline Insurance"}], None),
    "ticket_id": ([{"slug": "r1", "ticket_id": "T-1001"}], None),
    "icon": ([{"slug": "r1", "icon": "*"}], None),
    "source": ([{"slug": "r1", "source": "uploader-app"}], None),
    "artifact_link": ([{"slug": "r1", "artifact_link": "/f/someone-else"}], None),
    "clients": ([], "INSERT INTO clients (name, category, nickname) VALUES ('Treeline Insurance', 'hudu', 'TLI')"),
    "client_domains": ([], "INSERT INTO client_domains (client_name, domain) VALUES ('Unknown', 'example.com')"),
}
CLEAR = {
    "client": "UPDATE capture_events SET client = NULL",
    "ticket_id": "UPDATE capture_events SET ticket_id = ''",
    "icon": "UPDATE capture_events SET icon = NULL",
    "source": "UPDATE capture_events SET source = 'screenshot'",
    "artifact_link": "UPDATE capture_events SET artifact_link = '/f/' || slug",
    "clients": "DELETE FROM clients WHERE category = 'hudu'",
    "client_domains": "DELETE FROM client_domains",
}
for name, (rows, extra_sql) in REAL.items():
    path = Path(TMP) / f"real-{name}.db"
    make_old_install(path, rows + [{"slug": "ok1"}])
    if extra_sql:
        c = sqlite3.connect(path)
        c.execute(extra_sql)
        c.commit()
        c.close()
    out, err = boot()
    check(f"{name}: the app still starts", err is None, repr(err))
    present = set(item_cols()) | tables()
    check(f"{name}: KEPT", name in present, sorted(present))
    others = [n for n in DEAD_COLS + DEAD_TABLES if n != name]
    check(f"{name}: every other one dropped", not set(others) & present, sorted(set(others) & present))
    check(f"{name}: a warning names it, the row count and what to do",
          f"WARNING {db.MIGRATION_704}: KEPT" in out and name in out and "1 row(s) hold real data" in out
          and "restart the web app" in out, out[-600:])
    rows_logged = change_rows()
    det = json.loads(rows_logged[0][1]) if rows_logged else {}
    det = det.get("details", det)
    check(f"{name}: the change-log row records it as kept", len(rows_logged) == 1 and det.get("kept") == {name: 1},
          rows_logged)
    # the app writes items fine with the kept column still there
    try:
        db.insert_upload(f"new-{name}", "new.png", None, "tester", media_type="link",
                         external_url="https://example.com/new", content_description="after")
        wrote = db.get_by_slug(f"new-{name}") is not None
    except Exception as e:  # silent-ok: reported by the check below
        wrote = repr(e)
    check(f"{name}: a new item can still be written", wrote is True, wrote)
    check(f"{name}: search still works", [r["slug"] for r in db.search("after")] == [f"new-{name}"])
    # rerun: still kept, warned again, no new change-log row
    n0 = raw("SELECT COUNT(*) FROM audit_log")[0][0]
    out, err = boot()
    check(f"{name}: rerun keeps it and warns again, no new change-log row",
          err is None and name in (set(item_cols()) | tables()) and "KEPT" in out
          and raw("SELECT COUNT(*) FROM audit_log")[0][0] == n0, out[-300:])
    # cleared: the next boot drops it
    c = sqlite3.connect(path)
    c.execute(CLEAR[name])
    c.commit()
    c.close()
    out, err = boot()
    check(f"{name}: once its data is cleared, the next boot drops it",
          err is None and name not in (set(item_cols()) | tables()) and "WARNING" not in out, out[-300:])

# artifact_link's rule in detail
derived = db._artifact_link_is_derived
check("artifact_link: /f/<slug>, http://host/f/<slug> and https://host/f/<slug> are derived",
      derived("ab_c-1", "/f/ab_c-1") and derived("ab_c-1", "http://constructicon.local/f/ab_c-1")
      and derived("ab_c-1", "https://example.org:8443/f/ab_c-1"))
check("artifact_link: another slug, a deeper path, a query or another scheme is real data",
      not derived("ab", "/f/abc") and not derived("ab", "https://x/f/ab/thumb") and not derived("ab", "/f/ab?x=1")
      and not derived("ab", "ftp://x/f/ab") and not derived("ab", "https://x/g/f/ab"))

# ---- 5. payloads -----------------------------------------------------------------------------------
print("--- 5. the web and MCP payloads ---")
use(PROD)
from web import app as webapp  # noqa: E402
from mcp_server import server as mcp  # noqa: E402  (after the DB env is set)

admin = _testenv.client(webapp.app, base_url=BASE)


def no_dead_keys(obj):
    if isinstance(obj, dict):
        return not (set(obj) & set(GONE_KEYS)) and all(no_dead_keys(v) for v in obj.values())
    if isinstance(obj, list):
        return all(no_dead_keys(v) for v in obj)
    return True


r = admin.get("/api/image/p1")
check("GET /api/image/<slug>: 200, no dead keys", r.status_code == 200 and no_dead_keys(r.json()), r.text[:300])
check("GET /api/image/<slug>: type_icon is the type's badge", bool(r.json().get("type_icon")), r.json().get("type_icon"))
r = admin.get("/api/search", params={"query": "quillbadger"})
check("GET /api/search: finds it, no dead keys", r.status_code == 200 and [x["slug"] for x in r.json()] == ["p2"]
      and no_dead_keys(r.json()), r.text[:300])
r = admin.get("/api/gallery")
check("GET /api/gallery: 200, no dead keys", r.status_code == 200 and no_dead_keys(r.json()), r.text[:300])
r = admin.get("/api/home/files")
check("GET /api/home/files: 200, no dead keys", r.status_code == 200 and no_dead_keys(r.json()), r.text[:300])
r = admin.get("/api/clients")
check("GET /api/clients is gone (404)", r.status_code == 404, r.status_code)
r = admin.post("/api/image/p1", data={"description": "via form", "client": "X", "icon": "*"},
               headers={"Origin": BASE})
check("POST /api/image/<slug> ignores old client/icon fields", r.status_code == 200
      and db.get_by_slug("p1")["description"] == "via form" and no_dead_keys(r.json()), r.text[:300])
with actor.acting_as(actor.ACTOR_MCP):
    got = mcp.constructicon_get("p1")
    found = mcp.constructicon_search(query="mulberry")
check("MCP get: no dead keys", no_dead_keys(got), sorted(got) if isinstance(got, dict) else got)
check("MCP search: finds it, no dead keys", [x["slug"] for x in found] == ["p1"] and no_dead_keys(found), found)
with actor.acting_as(actor.ACTOR_MCP):
    try:
        mcp.constructicon_update("p1", icon="*")
        upd = "accepted"
    except TypeError:
        upd = "refused"
check("MCP update no longer takes icon", upd == "refused", upd)
for field in ("client", "icon"):
    try:
        with actor.acting_as(actor.ACTOR_UI):
            items.update("p1", **{field: "x"})
        res = "accepted"
    except Exception as e:  # silent-ok: reported by the check below
        res = getattr(e, "code", repr(e))
    check(f"items.update refuses {field!r} (unknown_field)", res == "unknown_field", res)

print("\n%s" % ("ALL PASS" if not FAILS else "FAILED: " + ", ".join(FAILS)))
sys.exit(1 if FAILS else 0)
