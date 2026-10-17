#!/usr/bin/env python3
"""Private agent notes on CARDS, not just items (#745). Throwaway DB, no server, no network.

    python scripts/test_card_agent_notes_745.py

In-process (scripts/_testenv.py: temp DB + storage + exports), the MCP tool functions called directly and
the web app through a TestClient with the per-run install token. Covers:
  1. schema: projects.agent_notes is added to a DB that lacks it, and init_db is idempotent;
  2. set / clear / undo: constructicon_set_agent_notes(card=...) by slug and by id, get_project returns the
     notes, null clears, each call is ONE change-log row (op card_agent_notes) and one undo restores the
     previous notes; a no-op logs nothing; updated_at is left alone; a dry run writes nothing;
  3. exactly one target: neither or both of slug= / card= is refused (bad_request), an unknown card is
     not_found, non-text notes are refused; the item path (slug=, positional) is unchanged; update /
     update_project can't write agent notes (unknown_field) and never touch them;
  4. privacy, with a unique marker in the notes: never in the web API (card list, card, explain, Save,
     orphan-child, delete, undo answers), the project / home / hobby pages, the project zip, the static
     export, search (db, web, MCP), or MCP list payloads (list_projects, explain_card, list_hobbies,
     update_project / create answers, the change summaries of set and undo); only get_project and
     set_agent_notes carry it;
  5. the role table: set_agent_notes has one editor row per target (slug, card).
Exits 1 if any check fails.
"""

import io
import json
import os
import sqlite3
import sys
import zipfile

import _testenv  # noqa: E402  (scripts/_testenv.py: temp DB + storage + exports, refuses otherwise)
TMP = _testenv.isolate("cardnotes745-")
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, ".."))
sys.path.insert(0, HERE)

from core import actor, cards, db, hobbies, membership, paths, roles, site_export  # noqa: E402
_testenv.assert_isolated()
from core.errors import AppError  # noqa: E402

FAILS = []
MARK = "ZQX745PRIVATENOTE"   # a token no other text in the fixture carries
MARK2 = "ZQX745SECONDNOTE"


def check(name, cond, extra=""):
    print(("PASS " if cond else "FAIL ") + name + (f"  [{str(extra)[:600]}]" if (extra and not cond) else ""))
    if not cond:
        FAILS.append(name)


def q(sql, *args):
    c = sqlite3.connect(os.environ["CONSTRUCTICON_DB_PATH"])
    c.row_factory = sqlite3.Row
    try:
        return [dict(r) for r in c.execute(sql, args)]
    finally:
        c.close()


def leaks(payload, *marks):
    text = payload if isinstance(payload, str) else json.dumps(payload, default=str)
    return any(m in text for m in (marks or (MARK, MARK2)))


def err(out):
    return out.get("error", {}).get("code") if isinstance(out, dict) and out.get("ok") is False else None


# --- 1. schema ---------------------------------------------------------------------------
db.init_db()
cols = {r["name"] for r in q("PRAGMA table_info(projects)")}
check("init_db adds projects.agent_notes", "agent_notes" in cols, sorted(cols))
c = sqlite3.connect(os.environ["CONSTRUCTICON_DB_PATH"])
c.execute("ALTER TABLE projects DROP COLUMN agent_notes")
c.commit()
c.close()
check("(fixture) an old DB without the column", "agent_notes" not in {r["name"] for r in q("PRAGMA table_info(projects)")})
db.init_db()
db.init_db()
check("init_db re-adds it to an old DB and is idempotent (two more boots)",
      [r["name"] for r in q("PRAGMA table_info(projects)")].count("agent_notes") == 1)

# --- fixture -----------------------------------------------------------------------------
(paths.storage_dir() / "cn-file-1.txt").write_text("an ordinary file on the rover card\n")
db.insert_upload("cn-file-1", "rover-notes.txt", "cn-file-1.txt", "tester", media_type="document",
                 description="rover fixture file")
with actor.acting_as(actor.ACTOR_UI):
    cards.create("Rover")
    cards.create("Rover Wheel")
    cards.create("Spare Card")
    ROVER = next(p for p in db.list_projects() if p["title"] == "Rover")
    WHEEL = next(p for p in db.list_projects() if p["title"] == "Rover Wheel")
    SPARE = next(p for p in db.list_projects() if p["title"] == "Spare Card")
    membership.add_files(ROVER["id"], ["cn-file-1"], **membership.UI_EFFECTS)
    cards.nest(WHEEL["id"], ROVER["id"])
    HOBBY = hobbies.create("Robotics").data["hobby"]
    hobbies.add_card(HOBBY["id"], ROVER["id"])

from web import app as webapp  # noqa: E402
client = _testenv.client(webapp.app, base_url="http://testhost.local:8000")
from mcp_server import server as S  # noqa: E402  (after the DB env is set)


def notes_in_db(card_id):
    return q("SELECT agent_notes FROM projects WHERE id = ?", card_id)[0]["agent_notes"]


def card_rows(batch_id):
    return q("SELECT op FROM audit_log WHERE batch_id = ? AND op IS NOT NULL", batch_id)


# --- 2. set / clear / undo ---------------------------------------------------------------
with actor.acting_as(actor.ACTOR_MCP):
    before_updated = q("SELECT updated_at FROM projects WHERE id = ?", ROVER["id"])[0]["updated_at"]
    gp = S.constructicon_get_project(ROVER["slug"])
    check("get_project: agent_notes is null before any are set", "agent_notes" in gp and gp["agent_notes"] is None,
          gp.get("agent_notes"))

    r1 = S.constructicon_set_agent_notes(card=ROVER["slug"], notes=f"{MARK}: owner said the chassis was a cake tin")
    check("set_agent_notes(card=slug): ok, returns the card + agent_notes + batch_id",
          r1.get("slug") == ROVER["slug"] and r1.get("agent_notes", "").startswith(MARK) and r1.get("batch_id"), r1)
    check("the notes are stored on the card row", (notes_in_db(ROVER["id"]) or "").startswith(MARK))
    check("ONE change-log row, op card_agent_notes",
          [r["op"] for r in card_rows(r1["batch_id"])] == ["card_agent_notes"], card_rows(r1["batch_id"]))
    check("updated_at is left alone (a private note is not a public edit)",
          q("SELECT updated_at FROM projects WHERE id = ?", ROVER["id"])[0]["updated_at"] == before_updated)
    gp = S.constructicon_get_project(ROVER["slug"])
    check("get_project returns the card's notes", (gp.get("agent_notes") or "").startswith(MARK), gp.get("agent_notes"))
    check("get_project by id returns them too",
          (S.constructicon_get_project(ROVER["id"]).get("agent_notes") or "").startswith(MARK))
    check("another card's get_project: no notes",
          S.constructicon_get_project(SPARE["slug"]).get("agent_notes") is None)

    r2 = S.constructicon_set_agent_notes(card=ROVER["id"], notes=f"{MARK2}: second pass")
    check("set_agent_notes(card=<id>) works and replaces the notes",
          r2.get("agent_notes", "").startswith(MARK2) and (notes_in_db(ROVER["id"]) or "").startswith(MARK2), r2)
    r_same = S.constructicon_set_agent_notes(card=ROVER["slug"], notes=r2["agent_notes"])
    check("the same notes again: nothing logged (batch_id null)", r_same.get("batch_id") is None, r_same)

    u = S.constructicon_undo(r2["batch_id"])
    check("one undo restores the previous notes", err(u) is None and (notes_in_db(ROVER["id"]) or "").startswith(MARK), u)
    check("the undo answer carries no notes text", not leaks(u), u)

    r3 = S.constructicon_set_agent_notes(card=ROVER["slug"], notes=None)
    check("notes=null clears them", r3.get("agent_notes") is None and notes_in_db(ROVER["id"]) is None, r3)
    check("get_project after the clear: null", S.constructicon_get_project(ROVER["slug"]).get("agent_notes") is None)
    S.constructicon_undo(r3["batch_id"])
    check("undoing the clear brings them back", (notes_in_db(ROVER["id"]) or "").startswith(MARK))

    res = cards.set_agent_notes(SPARE["id"], f"{MARK} dry", dry_run=True)
    check("core dry run answers and writes nothing", res.dry_run and notes_in_db(SPARE["id"]) is None)

# --- 3. exactly one target, errors, the item path ----------------------------------------
with actor.acting_as(actor.ACTOR_MCP):
    check("neither slug nor card: bad_request", err(S.constructicon_set_agent_notes(notes="x")) == "bad_request")
    check("both slug and card: bad_request",
          err(S.constructicon_set_agent_notes(slug="cn-file-1", card=ROVER["slug"], notes="x")) == "bad_request")
    check("unknown card: not_found", err(S.constructicon_set_agent_notes(card="no-such-card", notes="x")) == "not_found")
    check("unknown card id: not_found", err(S.constructicon_set_agent_notes(card=999999, notes="x")) == "not_found")
    check("non-text notes: bad_request", err(S.constructicon_set_agent_notes(card=SPARE["slug"], notes=5)) == "bad_request")
    try:
        cards.set_agent_notes("no-such-card", "x")
        code = None
    except AppError as e:
        code = e.code
    check("core: unknown card is not_found", code == "not_found", code)

    it = S.constructicon_set_agent_notes("cn-file-1", "item scratch")  # positional (slug, notes): unchanged
    check("item path unchanged: positional slug, returns the item + agent_notes, no batch_id",
          it.get("slug") == "cn-file-1" and it.get("agent_notes") == "item scratch" and "batch_id" not in it, it)
    check("item notes read back through get(include=[agent_notes])",
          S.constructicon_get("cn-file-1", include=["agent_notes"]).get("agent_notes") == "item scratch")
    check("the card's notes are untouched by the item call", (notes_in_db(ROVER["id"]) or "").startswith(MARK))

    up = S.constructicon_update_project(card=ROVER["slug"], fields={"agent_notes": "nope"})
    check("update_project refuses fields.agent_notes (unknown_field)", err(up) == "unknown_field", up)
    up = S.constructicon_update_project(card=ROVER["slug"], title="Rover II", description="desc")
    check("an ordinary update_project leaves the notes alone and doesn't return them",
          (notes_in_db(ROVER["id"]) or "").startswith(MARK) and not leaks(up), up)
    check("update refuses fields.agent_notes (unknown_field)",
          err(S.constructicon_update(slug="cn-file-1", fields={"agent_notes": "x"})) == "unknown_field")

# --- 4. privacy ---------------------------------------------------------------------------
# Put a marker on EVERY card, so any card-shaped payload that leaked the column would show it.
with actor.acting_as(actor.ACTOR_MCP):
    for c in db.list_projects():
        if c["id"] != ROVER["id"]:
            cards.set_agent_notes(c["id"], f"{MARK2} on {c['slug']}")
    ROVER = db.get_project(ROVER["id"])
    check("db readers hand out card rows without the column",
          "agent_notes" not in ROVER and all("agent_notes" not in c for c in db.list_projects())
          and all("agent_notes" not in c for c in db.list_child_projects(ROVER["id"]))
          and all("agent_notes" not in c for c in db.list_projects_for_post("cn-file-1"))
          and all("agent_notes" not in c for c in db.list_projects_for_hobby(HOBBY["id"])))

    # MCP list / other payloads
    check("MCP list_projects: no notes", not leaks(S.constructicon_list_projects()))
    check("MCP explain_card: no notes", not leaks(S.constructicon_explain_card(ROVER["slug"])))
    check("MCP list_hobbies: no notes", not leaks(S.constructicon_list_hobbies()))
    check("MCP search for the marker: nothing", S.constructicon_search(query=MARK) == []
          and S.constructicon_search(query=MARK2) == [])
    check("MCP search results carry no notes", not leaks(S.constructicon_search(query="rover")))
    check("MCP list_changes keeps the row images (admin change log, as for item notes)",
          any(r["op"] == "card_agent_notes" for r in S.constructicon_list_changes()))
    made = S.constructicon_create_project(title="Fresh Card")
    check("MCP create_project answer: no agent_notes key", "agent_notes" not in made, made)
    gp = S.constructicon_get_project(ROVER["slug"])
    check("get_project's children / members / links carry no notes",
          not leaks({k: v for k, v in gp.items() if k != "agent_notes"}), {k: v for k, v in gp.items() if k != "agent_notes"})

check("db.search for the marker: nothing", db.search(MARK) == [] and db.search(MARK2) == [])

# Web: every card read and the write answers that used to dump a whole row.
pages = {
    "GET /api/projects": client.get("/api/projects"),
    "GET /api/cards/{slug}": client.get(f"/api/cards/{ROVER['slug']}"),
    "GET /api/projects/{id}/explain": client.get(f"/api/projects/{ROVER['id']}/explain"),
    "GET /api/search": client.get("/api/search", params={"q": MARK}),
    "GET /api/search (rover)": client.get("/api/search", params={"q": "rover"}),
    "GET /api/hobbies": client.get("/api/hobbies"),
    "GET /project/{slug}": client.get(f"/project/{ROVER['slug']}"),
    "GET /project/{child}": client.get(f"/project/{WHEEL['slug']}"),
    "GET /": client.get("/"),
    "GET /hobbies": client.get("/hobbies"),
    "GET /object/{slug}": client.get("/object/cn-file-1"),
    "GET /api/image/{slug}": client.get("/api/image/cn-file-1"),
    "GET /api/project/{slug}/links": client.get(f"/api/project/{ROVER['slug']}/links"),
}
for name, r in pages.items():
    check(f"web {name}: {r.status_code}, no notes", r.status_code < 400 and not leaks(r.text), r.status_code)
for name, r in [("POST /api/projects/{id} (Save)", client.post(f"/api/projects/{ROVER['id']}", data={"title": "Rover III"})),
                ("POST /api/projects/{id}/orphan-child", client.post(f"/api/projects/{ROVER['id']}/orphan-child",
                                                                     data={"child_id": str(WHEEL["id"])}))]:
    check(f"web {name}: {r.status_code}, answer carries no notes", r.status_code < 400 and not leaks(r.text), r.text[:300])
check("Save left the notes alone", (notes_in_db(ROVER["id"]) or "").startswith(MARK))
z = client.get(f"/api/projects/{ROVER['id']}/export.zip")
blob = b""
if z.status_code == 200:
    with zipfile.ZipFile(io.BytesIO(z.content)) as zf:
        blob = b"".join(zf.read(n) for n in zf.namelist())
check("project zip: built, no notes in any file", z.status_code == 200 and MARK.encode() not in blob
      and MARK2.encode() not in blob, z.status_code)

# Static export of every card.
out = os.path.join(TMP, "site-out")
with actor.acting_as(actor.ACTOR_SCRIPT):
    site_export.build_site({"project_slugs": [c["slug"] for c in db.list_projects()]}, out_dir=out)
found = []
for root, _dirs, files in os.walk(out):
    for f in files:
        data = open(os.path.join(root, f), "rb").read()
        if MARK.encode() in data or MARK2.encode() in data:
            found.append(f)
check("static export: no notes in any file", os.path.isdir(out) and not found, found)

# Change summaries on the web: undo of a notes change, and a card delete (whole-row images).
with actor.acting_as(actor.ACTOR_MCP):
    rr = S.constructicon_set_agent_notes(card=SPARE["slug"], notes=f"{MARK} spare v2")
r = client.post(f"/api/changes/{rr['batch_id']}/undo")
check("web undo of a notes change: ok, answer carries no notes", r.status_code == 200 and not leaks(r.text), r.text[:300])
r = client.post(f"/api/projects/{SPARE['id']}/delete")
check("web card delete: ok, its change summary carries no notes", r.status_code == 200 and not leaks(r.text), r.text[:300])
batch = r.json().get("batch_id")
with actor.acting_as(actor.ACTOR_MCP):
    u = S.constructicon_undo(batch)
check("undoing the delete restores the card WITH its notes (the image keeps them)",
      err(u) is None and (notes_in_db(SPARE["id"]) or "").startswith(MARK2), u)

# --- 5. roles -----------------------------------------------------------------------------
check("ACTION_ROLES: set_agent_notes has an editor row per target",
      S.ACTION_ROLES.get(("constructicon_set_agent_notes", "slug")) == roles.EDITOR
      and S.ACTION_ROLES.get(("constructicon_set_agent_notes", "card")) == roles.EDITOR
      and ("constructicon_set_agent_notes", None) not in S.ACTION_ROLES)
check("the selector reads the target from the call",
      S._agent_notes_target({"card": "x", "slug": None}) == "card" and S._agent_notes_target({"slug": "a"}) == "slug")

print()
print("FAILED: " + ", ".join(FAILS) if FAILS else "ALL PASS")
sys.exit(1 if FAILS else 0)
