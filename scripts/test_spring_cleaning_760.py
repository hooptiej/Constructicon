#!/usr/bin/env python3
"""#760 + #556 "Spring Cleaning: Status Quo Vadis": the retired projects.status column goes, the
one-time v1 -> v2 card migration goes, and the dead card-link routes go.

Throwaway DBs only (scripts/_testenv.py), no server:

    python scripts/test_spring_cleaning_760.py

Covers:
  1. a fresh install: projects has no `status`, the #760 step is recorded, nothing is logged as
     dropped, and creating a card (core and web) works;
  2. an install shaped like before #760 (projects created with the old `status TEXT NOT NULL DEFAULT
     'active'` column, some cards holding other frozen words): the web boot drops the column, keeps
     every card, writes ONE change-log row with the count per word, and says so on stdout; an undo of
     a card edit and of a card delete logged BEFORE the drop (row images that name `status`) still
     works after it; creating a card works after it;
  3. a rerun on a finished DB changes nothing (schema and change log identical);
  4. a failure inside the step rolls it back, is logged loudly, and does not stop the app booting;
  5. the one-time card migration is gone (no module, no v2c_* migration);
  6. the retired routes (POST /api/project/{slug}/related, .../related/remove, GET
     /api/project/{slug}/links) answer 404, and the typed-link routes that replaced them still work.
Exits 1 if any check fails.
"""

import contextlib
import importlib.util
import io
import json
import os
import sqlite3
import sys
from pathlib import Path

import _testenv  # noqa: E402  (scripts/_testenv.py: temp DB + storage + exports, refuses otherwise)
TMP = _testenv.isolate("spring760-")
HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)
sys.path.insert(0, HERE)

try:
    import cairosvg  # noqa: F401
except Exception:  # silent-ok: no libcairo here (e.g. Windows); object types import it, nothing here renders SVG
    import types
    sys.modules["cairosvg"] = types.ModuleType("cairosvg")

from core import actor, cards, db  # noqa: E402
_testenv.assert_isolated()

FAILS = []
OP = "migration_drop_project_status_760"
BASE = "http://testhost.local"

# The projects table exactly as SCHEMA created it before #760 (only `status` differs from today's).
OLD_PROJECTS_DDL = """
CREATE TABLE projects (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    slug TEXT UNIQUE NOT NULL,
    title TEXT NOT NULL,
    description TEXT NOT NULL DEFAULT '',
    cover_slug TEXT,
    cover_project_id INTEGER REFERENCES projects(id),
    status TEXT NOT NULL DEFAULT 'active',
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL,
    tag_id INTEGER REFERENCES blog_tags(id),
    parent_id INTEGER REFERENCES projects(id)
)"""


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


def project_cols():
    return [r[1] for r in raw("PRAGMA table_info(projects)")]


def schema():
    return sorted(raw("SELECT type, name, sql FROM sqlite_master WHERE name NOT LIKE 'sqlite_%' ORDER BY type, name"))


def change_rows():
    return raw("SELECT id, form_body FROM audit_log WHERE op = ?", OP)


def details_of(form_body):
    d = json.loads(form_body or "{}")
    return d.get("details", d)


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


def make_old_install(path):
    """An empty DB file whose projects table has the pre-#760 DDL; init_db then builds the rest
    around it exactly as an upgrade would (CREATE TABLE IF NOT EXISTS skips projects)."""
    use(path)
    c = sqlite3.connect(path)
    c.execute(OLD_PROJECTS_DDL)
    c.commit()
    c.close()


# ---- 1. fresh install --------------------------------------------------------------------------
print("--- 1. a fresh install ---")
use(Path(TMP) / "fresh.db")
out, err = boot()
check("fresh: init_db ran cleanly", err is None, repr(err))
check("fresh: projects has no status column", "status" not in project_cols(), project_cols())
check("fresh: the #760 step is recorded", raw("SELECT 1 FROM schema_migrations WHERE name = ?", db.MIGRATION_760) != [])
check("fresh: no change-log row (nothing to drop)", change_rows() == [])
check("fresh: nothing said about #760", db.MIGRATION_760 not in out, out[-300:])
with actor.acting_as(actor.ACTOR_UI):
    res = cards.create("Fresh Card")
card = res.data["card"]
check("fresh: creating a card works, with a stage and no v1 status", res.ok and card.get("stage") == "in_progress"
      and "status" not in card and db.get_project(card["id"]) is not None, card)

# ---- 2. an install shaped like before #760 ---------------------------------------------------------
print("--- 2. an install from before #760: the column drops ---")
OLD = Path(TMP) / "old.db"
make_old_install(OLD)
out, err = boot()  # the first boot of new code on an old DB: drops at once (no cards yet)
check("old: first boot ran cleanly", err is None, repr(err))
check("old: ... and dropped the (empty) column", "status" not in project_cols(), project_cols())
rows = change_rows()
check("old: ... with one change-log row counting 0 cards", len(rows) == 1 and details_of(rows[0][1]).get("rows") == 0,
      rows)

# Now an old DB with cards in it: put the column back as the old DDL had it, as if those cards were
# made before the upgrade, and forget the step ran.
OLD2 = Path(TMP) / "old_with_cards.db"
make_old_install(OLD2)
c = sqlite3.connect(OLD2)
c.execute("CREATE TABLE schema_migrations (name TEXT PRIMARY KEY, applied_at REAL NOT NULL)")
c.execute("INSERT INTO schema_migrations (name, applied_at) VALUES (?, 0)", (db.MIGRATION_760,))
c.commit()
c.close()
out, err = boot()  # the step is "recorded" and the column is still there: it drops anyway (every boot)
check("old: a recorded step with the column still present drops it at the next boot",
      err is None and "status" not in project_cols(), (repr(err), project_cols()))
# Rebuild a realistic one: the old table, cards, edits, THEN the upgrade.
OLD3 = Path(TMP) / "old_real.db"
use(OLD3)
c = sqlite3.connect(OLD3)
c.execute(OLD_PROJECTS_DDL)
c.commit()
c.close()
# Boot with the #760 step switched off, so the column survives while we make cards on "old code".
real_step = db._drop_project_status_760
db._drop_project_status_760 = lambda: None
out, err = boot()
db._drop_project_status_760 = real_step
check("old: boot with the step held back leaves the column", err is None and "status" in project_cols(),
      (repr(err), project_cols()))
with actor.acting_as(actor.ACTOR_UI):
    a = cards.create("Mulberry Gearbox").data["card"]
    b = cards.create("Quillbadger Chassis").data["card"]
    d = cards.create("Doomed Card").data["card"]
c = sqlite3.connect(OLD3)
c.execute("UPDATE projects SET status = 'shelved' WHERE id = ?", (b["id"],))
c.commit()
c.close()
check("old: new cards took the column default ('active')",
      raw("SELECT status FROM projects WHERE id = ?", a["id"]) == [("active",)])
with actor.acting_as(actor.ACTOR_UI):
    edit = cards.update(b["id"], title="Quillbadger Chassis II")
    gone = cards.delete_card(d["id"])
edit_img = json.loads(raw("SELECT mutations FROM audit_log WHERE batch_id = ? AND op = 'update_card'",
                          edit.batch_id)[0][0])
gone_imgs = [json.loads(m) for (m,) in raw("SELECT mutations FROM audit_log WHERE batch_id = ?", gone.batch_id)]
named = any("status" in (m.get("before") or {}) for m in edit_img) or \
    any("status" in (m.get("before") or {}) for ms in gone_imgs for m in ms if m.get("table") == "projects")
if not named:
    # The ops image only the columns they touch; write a row image that names `status` the way a
    # whole-row image would (the delete's before image), so the undo path is exercised for real.
    for (aid, m) in raw("SELECT id, mutations FROM audit_log WHERE batch_id = ?", gone.batch_id):
        ms = json.loads(m)
        for x in ms:
            if x.get("table") == "projects" and x.get("before"):
                x["before"]["status"] = "active"
        c = sqlite3.connect(OLD3)
        c.execute("UPDATE audit_log SET mutations = ? WHERE id = ?", (json.dumps(ms), aid))
        c.commit()
        c.close()
gone_imgs = [json.loads(m) for (m,) in raw("SELECT mutations FROM audit_log WHERE batch_id = ?", gone.batch_id)]
check("old: a row image logged before the drop names `status`",
      any("status" in (m.get("before") or {}) for ms in gone_imgs for m in ms if m.get("table") == "projects"),
      gone_imgs[:1])
cards_before = raw("SELECT id, slug, title, kind, stage FROM projects ORDER BY id")
words = dict(raw("SELECT status, COUNT(*) FROM projects GROUP BY status"))
out, err = boot()
check("old: the upgrade boot ran cleanly", err is None, repr(err))
check("old: projects.status is gone", "status" not in project_cols(), project_cols())
check("old: every card survived, unchanged", raw("SELECT id, slug, title, kind, stage FROM projects ORDER BY id")
      == cards_before)
check("old: the drop is announced with the count", "dropped projects.status" in out and "2 card(s)" in out, out[-400:])
rows = change_rows()
det = details_of(rows[-1][1]) if rows else {}
check("old: ONE change-log row, with the rows and the count per word",
      len(rows) == 1 and det.get("rows") == 2 and det.get("by_word") == words
      and det.get("dropped") == ["projects.status"], (rows, words))
check("old: the change-log row is the migration's (undo refuses it)",
      raw("SELECT actor FROM audit_log WHERE op = ?", OP) == [("migration",)])
check("old: integrity_check ok", raw("PRAGMA integrity_check") == [("ok",)])
check("old: the search triggers and view came back",
      raw("SELECT 1 FROM sqlite_master WHERE type = 'view' AND name = 'item_search_src'") != []
      and all(raw("SELECT 1 FROM sqlite_master WHERE type = 'trigger' AND name = ?", n) for n in db._search_sync_triggers()))
with actor.acting_as(actor.ACTOR_UI):
    try:
        u1 = cards.undo(gone.batch_id)
        err1 = None
    except Exception as e:  # silent-ok: reported as a failed check below
        u1, err1 = None, e
check("old: undoing the pre-drop card delete (image names `status`) works and brings the card back",
      err1 is None and db.get_project(d["id"]) is not None, repr(err1))
with actor.acting_as(actor.ACTOR_UI):
    try:
        cards.undo(edit.batch_id)
        err2 = None
    except Exception as e:  # silent-ok: reported as a failed check below
        err2 = e
check("old: undoing the pre-drop card edit works", err2 is None
      and db.get_project(b["id"])["title"] == "Quillbadger Chassis", repr(err2))
with actor.acting_as(actor.ACTOR_UI):
    res = cards.create("After The Drop")
check("old: creating a card after the drop works", res.ok and db.get_project(res.data["card"]["id"]) is not None)
try:
    with actor.acting_as(actor.ACTOR_UI):
        cards.undo(rows[-1][0])
    refused = False
except Exception as e:  # silent-ok: the refusal is what this check wants
    refused = getattr(e, "code", "") == "undo_refused"
check("old: the migration's own change-log row is refused by undo (snapshot is the way back)", refused)

# ---- 3. rerun ------------------------------------------------------------------------------------
print("--- 3. a rerun on a finished DB ---")
s0, n0 = schema(), raw("SELECT COUNT(*) FROM audit_log")[0][0]
out, err = boot()
check("rerun: ran cleanly and changed nothing", err is None and schema() == s0, repr(err))
check("rerun: no new change-log row", len(change_rows()) == 1)
check("rerun: nothing said about #760", db.MIGRATION_760 not in out, out[-300:])

# ---- 4. a failing step does not stop the app ------------------------------------------------------
print("--- 4. a failure rolls back and is loud ---")
FAILDB = Path(TMP) / "fail.db"
make_old_install(FAILDB)
db._drop_project_status_760 = lambda: None  # build the rest of the schema first, column kept
out, err = boot()
db._drop_project_status_760 = real_step
check("fail: setup boot ran cleanly with the column kept", err is None and "status" in project_cols(), repr(err))
real_alter = db._ensure_chip_triggers


def boom(conn):
    raise RuntimeError("simulated failure")


db._ensure_chip_triggers = boom  # called inside the step after the ALTER: the whole step must roll back
buf = io.StringIO()
with contextlib.redirect_stdout(buf):
    res = db._run_drop_project_status_760()
db._ensure_chip_triggers = real_alter
check("fail: the wrapper returns None and says what happened",
      res is None and "ERROR drop_project_status_760" in buf.getvalue() and "NOT dropped" in buf.getvalue(),
      buf.getvalue()[-300:])
check("fail: nothing was dropped (rolled back)", "status" in project_cols(), project_cols())
check("fail: no change-log row", change_rows() == [])
out, err = boot()
check("fail: the next boot finishes the job", err is None and "status" not in project_cols(), repr(err))

# ---- 5. the one-time card migration is gone --------------------------------------------------------
print("--- 5. the v1 -> v2 card migration is gone ---")
check("no core.card_migration module", importlib.util.find_spec("core.card_migration") is None)
names = [n for n, _fn in db.MIGRATIONS]
check("no v2c_* migration in db.MIGRATIONS", not any(n.startswith("v2c") for n in names), names)
check("legacy_to_status still translates a v1 word a caller sends",
      __import__("core.card_rules", fromlist=["x"]).legacy_to_status("shelved")["stage"] == "paused")

# ---- 6. the retired routes ------------------------------------------------------------------------
print("--- 6. retired routes 404, typed links still work ---")
use(OLD3)
from web import app as webapp  # noqa: E402

admin = _testenv.client(webapp.app, base_url=BASE)
pa, pb = db.get_project(a["id"]), db.get_project(b["id"])
r = admin.post(f"/api/project/{pa['slug']}/related", data={"related_slug": pb["slug"]})
check("POST /api/project/{slug}/related is 404", r.status_code == 404, (r.status_code, r.text[:200]))
r = admin.post(f"/api/project/{pa['slug']}/related/remove", data={"related_slug": pb["slug"]})
check("POST /api/project/{slug}/related/remove is 404", r.status_code == 404, (r.status_code, r.text[:200]))
r = admin.get(f"/api/project/{pa['slug']}/links")
check("GET /api/project/{slug}/links is 404", r.status_code == 404, (r.status_code, r.text[:200]))
check("... and none of them is in the route table",
      not {"/api/project/{slug}/related", "/api/project/{slug}/related/remove", "/api/project/{slug}/links"}
      & {getattr(rt, "path", None) for rt in webapp.app.routes})
r = admin.post("/api/links", data={"a": pa["slug"], "b": pb["slug"], "type": "related"})
check("POST /api/links still links two cards", r.status_code == 200 and any(
    l.get("type") == "related" for l in r.json().get("links", [])), (r.status_code, r.text[:300]))
r = admin.post("/api/links/remove", data={"a": pa["slug"], "b": pb["slug"], "type": "related"})
check("POST /api/links/remove still unlinks them", r.status_code == 200 and not any(
    l.get("type") == "related" for l in r.json().get("links", [])), (r.status_code, r.text[:300]))
r = admin.post("/api/projects", data={"title": "Web Card"})
check("POST /api/projects creates a card (no status column to write)", r.status_code == 200, (r.status_code, r.text[:300]))
r = admin.get(f"/project/{pa['slug']}")
check("the card page renders", r.status_code == 200, r.status_code)

print("\n%s" % ("ALL PASS" if not FAILS else "FAILED: " + ", ".join(FAILS)))
sys.exit(1 if FAILS else 0)
