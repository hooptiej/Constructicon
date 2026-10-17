#!/usr/bin/env python3
"""#667 "Two processes walk into a schema": boot-time DDL must be safe when the web app and the MCP
server run it at the same moment.

Throwaway DBs only (scripts/_testenv.py), no server:

    python scripts/test_init_db_race_667.py

Production, 2026-10-12: constructicon-web and constructicon-mcp were recreated together after an
upgrade that added audit_log.client_ip. Both ran db.init_db() (the MCP with migrate=False, which still
runs the schema DDL), both saw the column missing, both ran ALTER TABLE ... ADD COLUMN, and the MCP
crashed with "duplicate column name: client_ip".

Each round here builds a current DB, then puts it back into an OLD shape: the newest columns dropped
from several tables (audit_log.client_ip and required_role among them), the search index's table,
view and triggers gone, project_hobbies and family_members gone (odd rounds: the production shape,
every step an add); even rounds ALSO rewind the two table rebuilds (trash.expires_at NOT NULL again,
project_relations untyped again, with rows in it). Then it starts several OS processes on that one DB
file, a mix of init_db() (the web app) and init_db(migrate=False) (the MCP), released within a few
milliseconds of each other. It checks:
  * every process finished init_db without an exception;
  * the schema afterwards is exactly a fresh DB's: the same tables, indexes, views and triggers, and
    every table has the same columns, each exactly once;
  * the one-time project_relations rebuild ran exactly once (one migration_v2c_4_links change-log
    row) and kept its rows;
  * PRAGMA integrity_check is ok.
The round repeats ROUNDS times. Exits 1 if any check fails.
"""

import json
import os
import sqlite3
import subprocess
import sys
import time
from pathlib import Path

import _testenv  # noqa: E402  (scripts/_testenv.py: temp DB + storage + exports, refuses otherwise)
TMP = _testenv.isolate("initrace667-")
HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.abspath(os.path.join(HERE, ".."))
sys.path.insert(0, ROOT)

from core import db  # noqa: E402
_testenv.assert_isolated()

ROUNDS = 8
P = 6  # processes per round: even = init_db() (web), odd = init_db(migrate=False) (MCP)
FAILS = []


def check(name, cond, extra=""):
    print(("PASS " if cond else "FAIL ") + name + (f"  [{extra}]" if (extra and not cond) else ""))
    if not cond:
        FAILS.append(name)


def make_current_db(path):
    saved = db.DB_PATH
    db.DB_PATH = Path(path)
    try:
        db.init_db()
    finally:
        db.DB_PATH = saved


def schema_of(path):
    """({(type, name)} for every schema object, {table: [column names in order]})."""
    conn = sqlite3.connect(path)
    try:
        objects = {(t, n) for t, n in conn.execute(
            "SELECT type, name FROM sqlite_master WHERE name NOT LIKE 'sqlite_%'")}
        cols = {}
        for (t,) in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%'"):
            cols[t] = [r[1] for r in conn.execute(f'PRAGMA table_info("{t}")')]
        return objects, cols
    finally:
        conn.close()


# The newest columns, dropped to fake a DB from before they were added (the #646 one first).
DROPPED_COLUMNS = [
    ("audit_log", "client_ip"), ("audit_log", "required_role"),
    ("capture_events", "sensitive_at"), ("capture_events", "sensitive_by"), ("capture_events", "sensitive"),
    ("projects", "writeup_lead"), ("projects", "flavor"), ("projects", "synopsis"),
    ("hobby_settings", "synopsis"), ("hobby_settings", "flavor"),
    ("blog_entries", "created_by_user_id"), ("blog_tags", "group_code"),
]
# #704: what every install had before #704 and a fresh DB no longer has (the web boot drops them).
OLD_ITEM_COLUMNS = (("source", "TEXT NOT NULL DEFAULT 'screenshot'"), ("client", "TEXT"), ("ticket_id", "TEXT"),
                    ("artifact_link", "TEXT"), ("icon", "TEXT"))


def downgrade(path, full):
    """Put a current DB back into an old shape (see the module docstring), in one transaction.
    full=False is the 2026-10-12 production shape: only columns and whole objects are missing, so
    every step is an add. full=True also rewinds the two table REBUILDS (trash, project_relations)."""
    conn = sqlite3.connect(path, isolation_level=None)
    try:
        conn.execute("BEGIN IMMEDIATE")
        # the search index (#543): triggers, view, table
        for (name,) in conn.execute("SELECT name FROM sqlite_master WHERE type = 'trigger' "
                                    "AND name LIKE 'item_search_%'").fetchall():
            conn.execute(f"DROP TRIGGER {name}")
        conn.execute("DROP VIEW item_search_src")
        conn.execute("DROP TABLE item_search")
        conn.execute("DROP INDEX idx_capture_events_sensitive")
        for table, column in DROPPED_COLUMNS:
            conn.execute(f"ALTER TABLE {table} DROP COLUMN {column}")
        conn.execute("DROP TABLE project_hobbies")
        conn.execute("DROP TABLE family_members")
        # #704: the dead imagerepo columns, their indexes and the client tables (placeholder rows only)
        for column, ddl in OLD_ITEM_COLUMNS:
            conn.execute(f"ALTER TABLE capture_events ADD COLUMN {column} {ddl}")
        conn.execute("CREATE INDEX idx_capture_events_source ON capture_events(source)")
        conn.execute("CREATE INDEX idx_capture_events_client ON capture_events(client)")
        conn.execute("CREATE INDEX idx_capture_events_ticket ON capture_events(ticket_id)")
        conn.execute("CREATE TABLE clients (name TEXT PRIMARY KEY, category TEXT NOT NULL)")
        conn.execute("CREATE TABLE client_domains (client_name TEXT NOT NULL, domain TEXT NOT NULL, "
                     "PRIMARY KEY (client_name, domain))")
        conn.execute("INSERT INTO clients (name, category) VALUES ('Unknown', 'special'), "
                     "('Not Business', 'special'), ('Internal Infrastructure', 'special')")
        if not full:
            conn.execute("COMMIT")
            return
        # trash.expires_at NOT NULL (the first phase-B shape)
        conn.execute("DROP INDEX idx_trash_expires")
        conn.execute("ALTER TABLE trash RENAME TO trash_cur")
        conn.execute("""CREATE TABLE trash (
            batch_id TEXT NOT NULL, slug TEXT NOT NULL, stored_filename TEXT,
            has_original INTEGER NOT NULL DEFAULT 0, has_thumb INTEGER NOT NULL DEFAULT 0,
            dir TEXT NOT NULL, size_bytes INTEGER NOT NULL DEFAULT 0, title TEXT,
            reason TEXT NOT NULL DEFAULT 'delete', created_at REAL NOT NULL, expires_at REAL NOT NULL,
            purged_at REAL, embedding BLOB, PRIMARY KEY (batch_id, slug))""")
        conn.execute("DROP TABLE trash_cur")
        conn.execute("CREATE INDEX idx_trash_expires ON trash(purged_at, expires_at)")
        # project_relations before V2 cards 3.8 (no type/note), with two rows to carry over
        conn.execute("DROP TABLE project_relations")
        conn.execute("""CREATE TABLE project_relations (
            slug_a TEXT NOT NULL, slug_b TEXT NOT NULL, created_at REAL NOT NULL,
            PRIMARY KEY (slug_a, slug_b))""")
        conn.execute("INSERT INTO project_relations VALUES ('card-a', 'card-b', 1.0), ('card-b', 'card-a', 1.0)")
        conn.execute("COMMIT")
    finally:
        conn.close()


CHILD = r"""
import json, sys, time
sys.path.insert(0, sys.argv[4])
from core import actor, db
go, name, migrate = float(sys.argv[1]), sys.argv[2], sys.argv[3] == "1"
ready = time.time()
while time.time() < go:
    time.sleep(0.0005)
start = time.time()
try:
    with actor.acting_as(actor.ACTOR_SYSTEM):
        db.init_db(migrate=migrate)
    out = "ok"
except Exception as e:
    out = f"{type(e).__name__}: {e}"
print(json.dumps({"name": name, "out": out, "ready": ready, "start": start, "end": time.time()}))
"""

print("--- the helpers ---")
HELPER_DB = Path(TMP) / "helpers.db"
hc = sqlite3.connect(HELPER_DB)
hc.execute("CREATE TABLE t (a TEXT)")
if hasattr(db, "_add_column"):
    db._add_column(hc, "t", "b", "TEXT")
    try:
        db._add_column(hc, "t", "b", "TEXT")
        again = "ok"
    except sqlite3.OperationalError as e:
        again = str(e)
    check("adding a column that is already there counts as success (belt and braces)", again == "ok", again)
    try:
        db._add_column(hc, "no_such_table", "b", "TEXT")
        other = "no error"
    except sqlite3.OperationalError as e:
        other = str(e)
    check("...but any other ALTER error still raises", other.startswith("no such table"), other)
    db._exec_script(hc, "-- a comment; with a semicolon\nCREATE TABLE u (x TEXT); -- trailing\n"
                        "CREATE TRIGGER u_ai AFTER INSERT ON u BEGIN INSERT INTO t (a) VALUES (NEW.x); END;\n")
    hc.execute("INSERT INTO u VALUES ('hi')")
    check("_exec_script runs each statement (comments with ';' and trigger bodies intact)",
          hc.execute("SELECT a FROM t").fetchall() == [("hi",)])
else:
    check("db._add_column exists (the #667 helper)", False, "missing")
hc.close()

REF_DB = Path(TMP) / "reference.db"
make_current_db(REF_DB)
REF_OBJECTS, REF_COLS = schema_of(REF_DB)
check("the reference DB has audit_log.client_ip", "client_ip" in REF_COLS.get("audit_log", []))

for rnd in range(1, ROUNDS + 1):
    full = rnd % 2 == 0  # odd rounds: the production shape (adds only); even: the rebuilds too
    print(f"--- round {rnd} of {ROUNDS}: {P} processes on one old-shape DB "
          f"({'columns, objects and table rebuilds' if full else 'missing columns and objects only'}) ---")
    path = Path(TMP) / f"race{rnd}.db"
    make_current_db(path)
    downgrade(path, full)
    old_objects, old_cols = schema_of(path)
    check(f"r{rnd}: the DB starts in the old shape (no client_ip, no search index"
          + (", untyped links)" if full else ")"),
          "client_ip" not in old_cols["audit_log"] and ("table", "item_search") not in old_objects
          and "client" in old_cols["capture_events"] and ("table", "clients") in old_objects
          and ("type" not in old_cols["project_relations"]) == full)
    go = time.time() + 12  # every child imports core before this moment, then they all start at once
    env = {**os.environ, "CONSTRUCTICON_DB_PATH": str(path), "PYTHONDONTWRITEBYTECODE": "1"}
    procs = [subprocess.Popen([sys.executable, "-c", CHILD, repr(go), f"p{i}", "1" if i % 2 == 0 else "0", ROOT],
                              stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, env=env)
             for i in range(P)]
    outs = []
    for p in procs:
        so, se = p.communicate(timeout=180)
        try:
            outs.append({**json.loads(so.strip().splitlines()[-1]), "rc": p.returncode,
                         "fallback": "already added by another process" in so})
        except (ValueError, IndexError):
            outs.append({"out": "crashed: " + (se.strip().splitlines() or ["?"])[-1], "rc": p.returncode})
    check(f"r{rnd}: every process was ready before the start line", all(o.get("ready", go) < go for o in outs),
          [o.get("out") for o in outs if "ready" not in o])
    starts = [o["start"] for o in outs if "start" in o]
    spread = (max(starts) - min(starts)) if len(starts) == P else 99
    check(f"r{rnd}: they all started within 50 ms of each other", spread < 0.05, f"{spread:.3f}s")
    bad = [f"{o.get('name', '?')}: {o['out']}" for o in outs if o["out"] != "ok" or o["rc"] != 0]
    check(f"r{rnd}: all {P} init_db calls (web and MCP mixed) finished cleanly", not bad, "; ".join(bad))
    check(f"r{rnd}: the write lock did it (no process needed the duplicate-column fallback)",
          not any(o.get("fallback") for o in outs), [o.get("name") for o in outs if o.get("fallback")])
    objects, cols = schema_of(path)
    check(f"r{rnd}: same tables, indexes, views and triggers as a fresh DB", objects == REF_OBJECTS,
          f"missing {sorted(REF_OBJECTS - objects)}, extra {sorted(objects - REF_OBJECTS)}")
    dup = {t: [c for c in set(cs) if cs.count(c) > 1] for t, cs in cols.items()}
    dup = {t: d for t, d in dup.items() if d}
    diff = {t for t in set(REF_COLS) | set(cols) if sorted(REF_COLS.get(t, [])) != sorted(cols.get(t, []))}
    check(f"r{rnd}: every table has a fresh DB's columns, each exactly once", not dup and not diff,
          f"dup {dup}, differ {sorted(diff)}")
    check(f"r{rnd}: audit_log.client_ip exists exactly once", cols["audit_log"].count("client_ip") == 1)
    conn = sqlite3.connect(path)
    try:
        rebuilds = conn.execute("SELECT COUNT(*) FROM audit_log WHERE op = 'migration_v2c_4_links'").fetchone()[0]
        links = conn.execute("SELECT slug_a, slug_b, type FROM project_relations ORDER BY slug_a").fetchall()
        notnull = {r[1]: r[3] for r in conn.execute("PRAGMA table_info(trash)")}["expires_at"]
        integrity = conn.execute("PRAGMA integrity_check").fetchall()
    finally:
        conn.close()
    if full:
        check(f"r{rnd}: the project_relations rebuild ran exactly once and kept both rows",
              rebuilds == 1 and links == [("card-a", "card-b", "related"), ("card-b", "card-a", "related")],
              f"{rebuilds} rebuild rows, {links}")
    else:
        check(f"r{rnd}: no project_relations rebuild on a DB that didn't need one", rebuilds == 0, rebuilds)
    check(f"r{rnd}: trash.expires_at allows NULL again", notnull == 0)
    check(f"r{rnd}: PRAGMA integrity_check is ok", integrity == [("ok",)], integrity[:3])

print("--- a long write elsewhere (a big data migration in web): the MCP's boot waits, it doesn't fail ---")
path = Path(TMP) / "held.db"
make_current_db(path)
holder = sqlite3.connect(path, isolation_level=None)
holder.execute("BEGIN IMMEDIATE")
holder.execute("INSERT INTO app_settings (key, value) VALUES ('held_667', 'x')")
env = {**os.environ, "CONSTRUCTICON_DB_PATH": str(path), "PYTHONDONTWRITEBYTECODE": "1"}
child = subprocess.Popen([sys.executable, "-c", CHILD, repr(time.time()), "mcp", "0", ROOT],
                         stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, env=env)
time.sleep(4)
still_waiting = child.poll() is None
holder.execute("COMMIT")
holder.close()
so, se = child.communicate(timeout=120)
try:
    res = json.loads(so.strip().splitlines()[-1])["out"]
except (ValueError, IndexError):
    res = "crashed: " + (se.strip().splitlines() or ["?"])[-1]
check("init_db(migrate=False) was still waiting while another process held the write lock", still_waiting)
check("...then finished cleanly once it was released", res == "ok", res)
check("...and logged how long it waited", "waited" in so and "write lock" in so, so[-300:])

if FAILS:
    print(f"FAILED: {', '.join(FAILS)}")
    sys.exit(1)
print("ALL PASS")
