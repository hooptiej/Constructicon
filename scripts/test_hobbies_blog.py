#!/usr/bin/env python3
"""Self-contained check for #541 phase D: hobbies, blog, card creation/edit, the explicit
stale-decision sweep and the private raw writers.

Throwaway SQLite DB (CONSTRUCTICON_DB_PATH set before core is imported) built by the real
`db.init_db()`, a throwaway storage dir, the real FastAPI app through TestClient and the MCP tool
functions called directly. Every write is undone and the WHOLE database (every table, every
non-BLOB column, rowids of the ordered tables) is compared with its state before the write.
Covers:
  - hobbies: create (new tag / reused tag / bad name / bad status / alias), web + MCP create,
    add/remove a card over the web and MCP (logged with the right actor), unmark;
  - convert project -> hobby (dry run writes nothing; the card, children, files, links, family,
    blog attachments, blank write-up, open question and hobby state come back exactly on undo),
    web + MCP;
  - convert hobby -> card: family / collection / project rules, nested members, into_hobby, loose
    objects, home overrides, inactive -> paused, refusals that write nothing, dry run, undo; web +
    MCP;
  - card creation (web create, from-selection, from-related, MCP create): tag + card + write-up
    (+ files) as one batch, undo leaves nothing behind; a reused tag survives the undo;
  - the card Save (title / description / dates / kind) as one batch, and its 400s;
  - blog: create, update, attach cards / files (unknown ones refused), delete, each undone; MCP;
  - decisions: list_open / GET /api/pending-decisions never write; stale questions are left out of
    reads; sweep_stale resolves exactly the stale ones (card questions untouched), logged and
    undoable, idempotent; MCP sweep; a resolved project-match question is one undoable batch;
  - scripts/check_layering.py passes.
No server needed:

    python scripts/test_hobbies_blog.py

Exits 1 if any check fails.
"""

import io
import json
import os
import sqlite3
import subprocess
import sys
import tempfile
from pathlib import Path

import _testenv  # noqa: E402  (scripts/_testenv.py: temp DB + storage + exports, refuses otherwise)
TMP = _testenv.isolate("hobbies-blog-")
os.environ.setdefault("CAPTION_DISABLED", "1")
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))

try:
    import cairosvg  # noqa: F401
except Exception:  # no libcairo here (e.g. Windows): object types import it
    import types
    sys.modules["cairosvg"] = types.ModuleType("cairosvg")

from PIL import Image  # noqa: E402

from core import actor, blog, cards, db, decisions, hobbies, ingest, membership, paths, revisions, storage  # noqa: E402
_testenv.assert_isolated()  # now as core actually resolved the paths
from core.errors import AppError  # noqa: E402

ingest.run_in_thread = lambda fn, *a: None  # no background threads in a unit check

FAILS = []
ROWID_TABLES = ("project_items", "project_hobbies", "family_members", "project_relations", "blog_entry_projects",
                "post_tags")


def check(name, cond, extra=""):
    print(("PASS " if cond else "FAIL ") + name + (f"  [{extra}]" if (extra and not cond) else ""))
    if not cond:
        FAILS.append(name)


def code_of(fn, *a, **kw):
    try:
        fn(*a, **kw)
    except AppError as e:
        return e.code
    return None


def conn():
    c = sqlite3.connect(os.environ["CONSTRUCTICON_DB_PATH"])
    c.row_factory = sqlite3.Row
    return c


def q(sql, *args):
    c = conn()
    try:
        return [dict(r) for r in c.execute(sql, args)]
    finally:
        c.close()


def snapshot():
    """Every table but the audit/change log, every non-BLOB column (+ rowid where order lives)."""
    c = conn()
    try:
        out = {}
        for (t,) in c.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' "
                              "AND name NOT IN ('audit_log') ORDER BY name"):
            cols = [r["name"] for r in c.execute(f"PRAGMA table_info({t})") if (r["type"] or "").upper() != "BLOB"]
            sel = ", ".join(cols)
            if t in ROWID_TABLES:
                sel = "rowid AS _rowid, " + sel
            out[t] = sorted(json.dumps(dict(r), sort_keys=True, default=str) for r in c.execute(f"SELECT {sel} FROM {t}"))
        return out
    finally:
        c.close()


def diff(a, b):
    return {t: (len(a[t]), len(b.get(t, []))) for t in a if a[t] != b.get(t)}


def audit_count():
    return q("SELECT COUNT(*) AS n FROM audit_log")[0]["n"]


def change_count():
    return q("SELECT COUNT(*) AS n FROM audit_log WHERE op IS NOT NULL")[0]["n"]


def ops_of(batch_id):
    return [(r["op"], r["actor"]) for r in q("SELECT op, actor FROM audit_log WHERE batch_id = ? AND op IS NOT NULL "
                                             "ORDER BY id", batch_id)]


def mk(slug):
    sf = f"{slug}.png"
    buf = io.BytesIO()
    Image.new("RGB", (8, 8), (10, 200, 10)).save(buf, "PNG")
    (paths.storage_dir() / sf).write_bytes(buf.getvalue())
    db.insert_upload(slug, f"{slug}.png", sf, "tester", media_type="image")
    return slug


def undo_ok(name, batch_id, before):
    cards.undo(batch_id)
    after = snapshot()
    check(f"undo {name}: database exactly as before", after == before, diff(before, after))


db.init_db()
ctx = actor.acting_as(actor.ACTOR_UI)
ctx.__enter__()

from fastapi.testclient import TestClient  # noqa: E402
from web import app as webapp  # noqa: E402
from mcp_server import server  # noqa: E402

client = _testenv.client(webapp.app)

A, B, C, D, E, F, G = (mk(s) for s in ("ia", "ib", "ic", "id", "ie", "if", "ig"))
base = cards.create("Base Card", link_tag=True).data["card"]  # a card that exists throughout

# ---- 1. hobbies: create ------------------------------------------------------------------
before = snapshot()
res = hobbies.create("Rocketry")
h = res.data["hobby"]
check("create: new tag marked as an active hobby with a code",
      res.data["tag_created"] and h["status"] == "active" and h["group_code"] == "ROC", h)
check("create: one change-log row, actor owner-ui", ops_of(res.batch_id) == [("hobby_create", "owner-ui")], ops_of(res.batch_id))
undo_ok("hobby create (tag created)", res.batch_id, before)
check("create: tag gone after undo", q("SELECT COUNT(*) AS n FROM blog_tags WHERE name = 'Rocketry'")[0]["n"] == 0)

tag_only = cards.create("Gardening").data["card"]  # makes a root tag "Gardening"
before = snapshot()
res = hobbies.create("Gardening", "dormant")
check("create on an existing tag reuses it; 'dormant' -> inactive with a warning",
      not res.data["tag_created"] and res.data["hobby"]["id"] == tag_only["tag_id"]
      and res.data["hobby"]["status"] == "inactive" and res.warnings, res.to_dict())
undo_ok("hobby create (tag reused)", res.batch_id, before)
check("reused tag survives the undo", db.get_tag(tag_only["tag_id"]) is not None and not db.get_tag(tag_only["tag_id"])["is_hobby"])
check("create: empty name -> bad_request", code_of(hobbies.create, "  ") == "bad_request")
check("create: bad status -> bad_request, nothing written",
      code_of(hobbies.create, "Nope", "sleepy") == "bad_request" and snapshot() == before)

before = snapshot()
r = client.post("/api/hobbies", data={"name": "Web Hobby"})
check("web POST /api/hobbies: 200 + id/slug/batch_id", r.status_code == 200 and {"id", "slug", "batch_id"} <= set(r.json()), r.text)
check("web create logged as token", ops_of(r.json()["batch_id"]) == [("hobby_create", "token")])
undo_ok("web hobby create", r.json()["batch_id"], before)
r = client.post("/api/hobbies", data={"name": "   "})
check("web create, blank name: 400 + same message", r.status_code == 400 and r.json()["detail"] == "Hobby name can't be empty", r.text)
with actor.acting_as(actor.ACTOR_MCP):
    out = server.constructicon_create_hobby("Mcp Hobby", status="inactive")
check("MCP create_hobby: shape + inactive + batch", out.get("status") == "inactive" and out.get("group_code") and out.get("batch_id"), out)
check("MCP create logged as mcp", ops_of(out["batch_id"]) == [("hobby_create", "mcp")])
cards.undo(out["batch_id"])

# ---- 2. hobbies: add / remove a card (web + MCP) -------------------------------------------
H = hobbies.create("Collecting").data["hobby"]
before = snapshot()
r = client.post(f"/api/hobby/{H['slug']}/add-project", data={"project_id": str(base["id"])})
row = q("SELECT batch_id, actor FROM audit_log WHERE op = 'add_to_hobby' ORDER BY id DESC LIMIT 1")
check("web add-project: 200, logged as token", r.status_code == 200 and row and row[0]["actor"] == "token", r.text)
check("web add-project: card in hobby", base["id"] in [p["id"] for p in db.list_projects_for_hobby(H["id"])])
undo_ok("web add-project", row[0]["batch_id"], before)
client.post(f"/api/hobby/{H['slug']}/add-project", data={"project_id": str(base["id"])})
before = snapshot()
r = client.post(f"/api/hobby/{H['slug']}/remove-project", data={"project_id": base["slug"]})
row = q("SELECT batch_id, actor FROM audit_log WHERE op = 'remove_from_hobby' ORDER BY id DESC LIMIT 1")
check("web remove-project: 200, logged as token, card out", r.status_code == 200 and row[0]["actor"] == "token"
      and base["id"] not in [p["id"] for p in db.list_projects_for_hobby(H["id"])])
undo_ok("web remove-project", row[0]["batch_id"], before)
before = snapshot()
with actor.acting_as(actor.ACTOR_MCP):
    server.constructicon_remove_from_hobby(base["slug"], H["slug"])
row = q("SELECT batch_id, actor FROM audit_log WHERE op = 'remove_from_hobby' ORDER BY id DESC LIMIT 1")
check("MCP remove_from_hobby logged as mcp", row[0]["actor"] == "mcp")
undo_ok("MCP remove_from_hobby", row[0]["batch_id"], before)
before = snapshot()
other = cards.create("Other Card").data["card"]
before = snapshot()
with actor.acting_as(actor.ACTOR_MCP):
    server.constructicon_add_project_to_hobby(other["slug"], H["slug"])
row = q("SELECT batch_id, actor FROM audit_log WHERE op = 'add_to_hobby' ORDER BY id DESC LIMIT 1")
check("MCP add_project_to_hobby logged as mcp", row[0]["actor"] == "mcp")
undo_ok("MCP add_project_to_hobby", row[0]["batch_id"], before)

# ---- 3. unmark ---------------------------------------------------------------------------
U = hobbies.create("Unmark Me").data["hobby"]
hobbies.add_card(U["id"], other["id"])
cards.set_home(base["id"], f"hobby:{U['slug']}")
before = snapshot()
res = hobbies.unmark(U["slug"])
t = db.get_tag(U["id"])
check("unmark: not a hobby, code/activity cleared, membership + home override gone",
      not t["is_hobby"] and t["hobby_status"] is None and t["group_code"] is None
      and db.list_projects_for_hobby(U["id"]) == [] and db.get_project(base["id"])["home_kind"] is None
      and res.data["cards_removed"] == 1 and res.data["homes_cleared"] == [base["slug"]], res.to_dict())
undo_ok("unmark", res.batch_id, before)
cards.set_home(base["id"], None)

# ---- 4. convert project -> hobby ----------------------------------------------------------
proj = cards.create("Drone Fleet").data["card"]           # tag + blank write-up
kid1 = cards.create("Drone One", parent=proj["id"]).data["card"]
kid2 = cards.create("Drone Two", parent=proj["id"]).data["card"]
membership.add_files(proj["id"], [A, B], **membership.UI_EFFECTS)
cards.link(proj["id"], base["id"], "related")
fam = cards.create("Fleet Family", kind="family").data["card"]
cards.add_to_family(fam["id"], proj["id"])
hobbies.add_card(H["id"], proj["id"])
entry = blog.create("Fleet post").data["entry"]
blog.set_projects(entry["id"], [(proj["id"], "the fleet")])
db.add_pending_decision("card_status", cards.card_decision_slug(proj["slug"]),
                        {"question": "Still flying?", "options": [{"key": "done", "label": "Done"}]})
before = snapshot()
n_before = change_count()
res = hobbies.convert_from_card(proj["slug"], dry_run=True)
check("convert->hobby dry run: plan, nothing written",
      res.dry_run and res.changes and snapshot() == before and change_count() == n_before, diff(before, snapshot()))
r = client.post(f"/api/project/{proj['slug']}/convert-to-hobby")
body = r.json()
hob = db.get_hobby(proj["tag_id"])
check("web convert->hobby: 200, summary, batch",
      r.status_code == 200 and body["summary"] == {"children_moved": 2, "items_moved": 2} and body["batch_id"], body)
check("convert->hobby: card gone, its tag is an active hobby holding the children (unnested), files tagged",
      db.get_project(proj["id"]) is None and hob and hob["hobby_status"] == "active"
      and sorted(p["id"] for p in db.list_projects_for_hobby(hob["id"])) == sorted([kid1["id"], kid2["id"]])
      and db.get_project(kid1["id"])["parent_id"] is None
      and all(proj["tag_id"] in [x["id"] for x in db.list_tags_for_post(s)] for s in (A, B)))
check("convert->hobby: open question resolved, link/family/blog rows gone",
      cards.open_card_decisions(proj["slug"]) == [] and db.list_project_link_rows(slug=proj["slug"]) == []
      and db.list_families_for_member(proj["id"]) == [] and db.list_entry_projects(entry["id"]) == [])
check("convert->hobby logged as token", {a for _, a in ops_of(body["batch_id"])} == {"token"})
undo_ok("convert project -> hobby", body["batch_id"], before)
check("convert->hobby undo: card, children, files, write-up, question back",
      db.get_project(proj["id"]) is not None and db.get_project(kid1["id"])["parent_id"] == proj["id"]
      and [r_["post_slug"] for r_ in db.list_project_item_rows(proj["id"])][1:] == [A, B]
      and len(cards.open_card_decisions(proj["slug"])) == 1)
notag = cards.create("No Tag Card", link_tag=False).data["card"]
membership.add_files(notag["id"], [C], **membership.NO_EFFECTS)
before = snapshot()
with actor.acting_as(actor.ACTOR_MCP):
    out = server.constructicon_convert_project_to_hobby(notag["slug"])
check("MCP convert->hobby (card had no tag): a tag is made, actor mcp",
      out["summary"]["items_moved"] == 1 and db.get_hobby(out["slug"]) is not None
      and {a for _, a in ops_of(out["batch_id"])} == {"mcp"}, out)
undo_ok("MCP convert project -> hobby (tag created)", out["batch_id"], before)

# ---- 5. convert hobby -> card ------------------------------------------------------------
GI = hobbies.create("GI Joe").data["hobby"]
m1 = cards.create("Joe Figures").data["card"]
m2 = cards.create("Joe Vehicles", kind="thing").data["card"]
m3 = cards.create("Joe Jeep", parent=m1["id"]).data["card"]   # nested under a member, and a member itself
for m in (m1, m2, m3):
    hobbies.add_card(GI["id"], m["id"])
membership.add_files(m1["id"], [D], **membership.NO_EFFECTS)
for s in (E, F):  # loose objects: tagged with the hobby, in none of its cards
    db._attach_tags(s, [GI["id"]])
homer = cards.create("Homer").data["card"]
cards.set_home(homer["id"], f"hobby:{GI['slug']}")
loose_before = [r_["slug"] for r_ in db.list_loose_hobby_objects(GI["id"])]
check("fixture: 2 loose objects", sorted(loose_before) == sorted([E, F]), loose_before)
before = snapshot()
n_before = change_count()
r = client.post(f"/api/hobby/{GI['slug']}/convert-to-card",
                data={"kind": "family", "into_hobby": H["slug"], "dry_run": "true"})
plan = r.json()
check("hobby->card dry run: 200, plan, nothing written",
      r.status_code == 200 and plan["dry_run"] and plan["loose_moved"] == 2 and plan["changes"]
      and snapshot() == before and change_count() == n_before, (r.text[:300], diff(before, snapshot())))
check("dry run plan: 2 members + 1 stays nested",
      sorted((m["slug"], m["how"]) for m in plan["members"]) == sorted(
          [(m1["slug"], "member"), (m2["slug"], "member"), (m3["slug"], "stays under its parent")]), plan["members"])
r = client.post(f"/api/hobby/{GI['slug']}/convert-to-card", data={"kind": "family", "into_hobby": H["slug"]})
res = r.json()
new = db.get_project(res["card"])
check("hobby->card: a family card titled after the hobby, reusing its tag, with a write-up, in Collecting",
      r.status_code == 200 and new["kind"] == "family" and new["title"] == "GI Joe" and new["tag_id"] == GI["id"]
      and new["writeup_slug"] and new["stage"] == "in_progress"
      and new["id"] in [p["id"] for p in db.list_projects_for_hobby(H["id"])], res)
check("hobby->card: top-level members are family members, the nested one stays nested",
      sorted(m["id"] for m in db.list_family_members(new["id"])) == sorted([m1["id"], m2["id"]])
      and db.get_project(m3["id"])["parent_id"] == m1["id"])
check("hobby->card: loose objects on the card, home override now the card, hobby unmarked, members out of it",
      {E, F} <= {r_["post_slug"] for r_ in db.list_project_item_rows(new["id"])}
      and (db.get_project(homer["id"])["home_kind"], db.get_project(homer["id"])["home_ref"]) == ("card", new["id"])
      and db.get_hobby(GI["id"]) is None and q("SELECT COUNT(*) AS n FROM project_hobbies WHERE hobby_tag_id = ?",
                                               GI["id"])[0]["n"] == 0)
check("hobby->card logged as token in one batch", ops_of(res["batch_id"]) and
      {a for _, a in ops_of(res["batch_id"])} == {"token"}, ops_of(res["batch_id"]))
undo_ok("convert hobby -> card (family)", res["batch_id"], before)
check("hobby->card undo: hobby back with its code, members and home override",
      db.get_hobby(GI["id"])["group_code"] == GI["group_code"]
      and sorted(p["id"] for p in db.list_projects_for_hobby(GI["id"])) == sorted([m1["id"], m2["id"], m3["id"]])
      and db.get_project(homer["id"])["home_kind"] == "hobby")

# project kind: members nested; inactive hobby -> paused; MCP; title override
cards.set_hobby_activity(GI["id"], "inactive")
before = snapshot()
with actor.acting_as(actor.ACTOR_MCP):
    out = server.constructicon_convert_hobby_to_card(GI["slug"], "project", title="Joe Build")
new = db.get_project(out["card"])
check("MCP hobby->card kind=project: members nested, paused, title override, actor mcp",
      new["kind"] == "project" and new["title"] == "Joe Build" and new["stage"] == "paused"
      and db.get_project(m1["id"])["parent_id"] == new["id"] and db.get_project(m2["id"])["parent_id"] == new["id"]
      and db.get_project(m3["id"])["parent_id"] == m1["id"] and {a for _, a in ops_of(out["batch_id"])} == {"mcp"}, out)
undo_ok("MCP convert hobby -> card (project)", out["batch_id"], before)
# refusals write nothing
outside = cards.create("Outside Parent").data["card"]
cards.nest(m2["id"], outside["id"])
before = snapshot()
check("project kind: a member already part of an outside card -> nest_second_parent, nothing written",
      code_of(hobbies.convert_to_card, GI["slug"], "project") == "nest_second_parent" and snapshot() == before)
cards.unnest(m2["id"])
grp = cards.create("Joe Sets", kind="collection").data["card"]
hobbies.add_card(GI["id"], grp["id"])
before = snapshot()
check("family kind: a collection member -> bad_membership, nothing written",
      code_of(hobbies.convert_to_card, GI["slug"], "family") == "bad_membership" and snapshot() == before)
check("project kind: a collection member -> nest_group_kind",
      code_of(hobbies.convert_to_card, GI["slug"], "project") == "nest_group_kind")
check("bad kind -> bad_kind", code_of(hobbies.convert_to_card, GI["slug"], "thing") == "bad_kind")
check("into itself -> bad_hobby", code_of(hobbies.convert_to_card, GI["slug"], "family", None, GI["slug"]) == "bad_hobby")
check("unknown hobby -> not_found", code_of(hobbies.convert_to_card, "no-such-hobby", "family") == "not_found")
hobbies.remove_card(GI["id"], grp["id"])

# ---- 6. card creation: one batch, nothing left behind ----------------------------------------
before = snapshot()
r = client.post("/api/projects", data={"title": "Fresh Card"})
made = db.get_project(r.json()["slug"])
b = q("SELECT batch_id FROM audit_log WHERE op = 'create_card' ORDER BY id DESC LIMIT 1")[0]["batch_id"]
check("web create: tag + card + write-up in ONE batch",
      [o for o, _ in ops_of(b)] == ["tag_create", "create_card", "create_writeup"] and made["writeup_slug"]
      and made["writeup_slug"] in [x["post_slug"] for x in db.list_project_item_rows(made["id"])]
      and made["tag_id"] in [t_["id"] for t_ in db.list_tags_for_post(made["writeup_slug"])], ops_of(b))
undo_ok("web create card", b, before)
check("web create undo: card, tag and write-up all gone",
      db.get_project(made["id"]) is None and db.get_tag(made["tag_id"]) is None and db.get_by_slug(made["writeup_slug"]) is None)
before = snapshot()
r = client.post("/api/projects", data={"title": "Gardening"})  # the tag "Gardening" exists
b = q("SELECT batch_id FROM audit_log WHERE op = 'create_card' ORDER BY id DESC LIMIT 1")[0]["batch_id"]
check("create with an existing tag: the tag is reused (no tag_create row)",
      [o for o, _ in ops_of(b)] == ["create_card", "create_writeup"]
      and db.get_project(r.json()["slug"])["tag_id"] == tag_only["tag_id"], ops_of(b))
undo_ok("web create card (tag reused)", b, before)
check("reused tag survives", db.get_tag(tag_only["tag_id"]) is not None)
before = snapshot()
r = client.post("/api/projects", data={"title": "Bad Stage Card", "stage": "bogus"})
check("web create, bad stage: 422 bad_status and NO stray tag", r.status_code == 422 and snapshot() == before, r.text)
r = client.post("/api/projects", data={"title": "x", "parent_id": "999999"})
check("web create, missing parent: 400 as before", r.status_code == 400 and r.json()["detail"] == "Parent project not found")
before = snapshot()
r = client.post("/api/projects/from-selection", data={"slugs": [G, "ghost"], "title": "Sel Card"})
sel = db.get_project(r.json()["slug"])
b = q("SELECT batch_id FROM audit_log WHERE op = 'create_card' ORDER BY id DESC LIMIT 1")[0]["batch_id"]
check("from-selection: card + files in ONE batch, cover set",
      [o for o, _ in ops_of(b)] == ["tag_create", "create_card", "create_writeup", "add_files"] and sel["cover_slug"] == G,
      ops_of(b))
undo_ok("from-selection", b, before)
db._add_relation(A, G)
before = snapshot()
r = client.post("/api/projects/from-related", data={"slug": A, "title": "Rel Card"})
b = q("SELECT batch_id FROM audit_log WHERE op = 'create_card' ORDER BY id DESC LIMIT 1")[0]["batch_id"]
check("from-related: one batch with both files", r.status_code == 200 and "add_files" in [o for o, _ in ops_of(b)]
      and sorted(x["post_slug"] for x in db.list_project_item_rows(r.json()["id"]))[:2] != [], ops_of(b))
undo_ok("from-related", b, before)
before = snapshot()
with actor.acting_as(actor.ACTOR_MCP):
    out = server.constructicon_create_project("Mcp Card", description="via mcp", kind="thing")
check("MCP create_project: one batch, actor mcp",
      out.get("batch_id") and {a for _, a in ops_of(out["batch_id"])} == {"mcp"}
      and [o for o, _ in ops_of(out["batch_id"])] == ["tag_create", "create_card", "create_writeup"], out)
undo_ok("MCP create card", out["batch_id"], before)
check("MCP create: empty title -> bad_request", server.constructicon_create_project("  ")["error"]["code"] == "bad_request")

# ---- 7. the card Save ------------------------------------------------------------------------
before = snapshot()
r = client.post(f"/api/projects/{base['id']}", data={"title": "Base Renamed", "description": "new words",
                                                    "kind": "thing", "start_date": "2020-01-02T00:00"})
body = r.json()
check("card Save: title/description/kind/start date in ONE batch",
      r.status_code == 200 and body["title"] == "Base Renamed" and body["kind"] == "thing"
      and body["start_date_override"] is not None
      and sorted(o for o, _ in ops_of(body["batch_id"])) == ["set_kind", "update_card"], ops_of(body.get("batch_id")))
undo_ok("card Save", body["batch_id"], before)
check("card Save undo restores the TITLE too (the #541 complaint)", db.get_project(base["id"])["title"] == "Base Card")
r = client.post(f"/api/projects/{base['id']}", data={"writeup_slug": "no-such-doc"})
check("card Save, unknown write-up: 400 'writeup slug not found'", r.status_code == 400 and r.json()["detail"] == "writeup slug not found")
r = client.post(f"/api/projects/{base['id']}", data={"writeup_slug": A})
check("card Save, image as write-up: 400 with the type message", r.status_code == 400 and "can't be a project write-up" in r.json()["detail"])
r = client.post(f"/api/projects/{base['id']}", data={"cover_project_id": str(other["id"])})
check("card Save, cover from a non-child: 400 as before", r.status_code == 400 and r.json()["detail"] == "Project is not a child of this project")
before = snapshot()
with actor.acting_as(actor.ACTOR_MCP):
    out = server.constructicon_update_project(base["slug"], title="Mcp Title", reset_start_date=True)
check("MCP update_project: one batch as mcp", out["title"] == "Mcp Title" and ops_of(out["batch_id"]) == [("update_card", "mcp")], out)
undo_ok("MCP update_project", out["batch_id"], before)

# ---- 8. blog --------------------------------------------------------------------------------
before = snapshot()
r = client.post("/api/blog-entries", data={"title": "Trip Report", "subtitle": "day one", "content_date": "1700000000"})
ent = r.json()
b = q("SELECT batch_id FROM audit_log WHERE op = 'blog_entry_create' ORDER BY id DESC LIMIT 1")[0]["batch_id"]
check("blog create (web): 200, slug, logged", r.status_code == 200 and ent["slug"] == "trip-report"
      and ops_of(b) == [("blog_entry_create", "token")], r.text[:200])
undo_ok("blog create", b, before)
ent = blog.create("Trip Report").data["entry"]
step = snapshot()
r = client.post(f"/api/blog-entries/{ent['slug']}", data={"title": "Trip Report 2", "cover_slug": A})
b = q("SELECT batch_id FROM audit_log WHERE op = 'blog_entry_update' ORDER BY id DESC LIMIT 1")[0]["batch_id"]
check("blog update (web): title + cover", r.status_code == 200 and db.get_blog_entry(ent["id"])["title"] == "Trip Report 2"
      and db.get_blog_entry(ent["id"])["cover_slug"] == A)
undo_ok("blog update", b, step)
r = client.post(f"/api/blog-entries/{ent['slug']}", data={"content_date": "soon"})
check("blog update, bad date: 400 same message", r.status_code == 400 and r.json()["detail"] == "Invalid content_date format")
step = snapshot()
r = client.put(f"/api/blog-entries/{ent['slug']}/projects", json=[{"project_id": base["id"], "note": "n1"},
                                                                    {"project_id": other["slug"], "note": ""}])
b = q("SELECT batch_id FROM audit_log WHERE op = 'blog_entry_set_projects' ORDER BY id DESC LIMIT 1")[0]["batch_id"]
check("blog attach cards (web): ordered, slug accepted", r.status_code == 200
      and [p["id"] for p in db.list_entry_projects(ent["id"])] == [base["id"], other["id"]])
undo_ok("blog set_projects", b, step)
r = client.put(f"/api/blog-entries/{ent['slug']}/projects", json=[{"project_id": 999999}])
check("blog attach unknown card: 404, nothing written", r.status_code == 404 and snapshot() == step, r.text)
r = client.put(f"/api/blog-entries/{ent['slug']}/items", json=[{"slug": B, "note": "x"}, {"slug": A}])
b = q("SELECT batch_id FROM audit_log WHERE op = 'blog_entry_set_items' ORDER BY id DESC LIMIT 1")[0]["batch_id"]
check("blog attach files (web)", r.status_code == 200 and [i["slug"] for i in db.list_entry_items(ent["id"])] == [B, A])
undo_ok("blog set_items", b, step)
r = client.put(f"/api/blog-entries/{ent['slug']}/items", json=[{"slug": "ghost"}])
check("blog attach unknown file: 404", r.status_code == 404)
blog.set_items(ent["id"], [(A, "")])
blog.set_projects(ent["id"], [(base["id"], "")])
r_ = blog.set_items(ent["id"], [(B, "first"), (A, "")])
check("re-setting the list keeps the existing row and reorders", [i["slug"] for i in db.list_entry_items(ent["id"])] == [B, A])
step = snapshot()
r = client.delete(f"/api/blog-entries/{ent['slug']}")
check("blog delete (web): entry and its rows gone, batch returned",
      r.status_code == 200 and r.json()["batch_id"] and db.get_blog_entry(ent["id"]) is None
      and q("SELECT COUNT(*) AS n FROM blog_entry_items WHERE entry_id = ?", ent["id"])[0]["n"] == 0)
undo_ok("blog delete", r.json()["batch_id"], step)
with actor.acting_as(actor.ACTOR_MCP):
    out = server.constructicon_create_blog_entry("Mcp Post", body="hi")
    server.constructicon_set_blog_entry_items(out["slug"], [{"slug": A, "note": "n"}])
    server.constructicon_update_blog_entry(out["slug"], clear_cover_slug=True, status="published")
    gone = server.constructicon_delete_blog_entry(out["slug"])
    missing = server.constructicon_delete_blog_entry(out["slug"])
mcp_ops = [r_["op"] for r_ in q("SELECT op FROM audit_log WHERE actor = 'mcp' AND op LIKE 'blog_entry_%' ORDER BY id")]
check("MCP blog create/set_items/update/delete all logged as mcp",
      mcp_ops[-4:] == ["blog_entry_create", "blog_entry_set_items", "blog_entry_update", "blog_entry_delete"]
      and gone is True and missing["error"]["code"] == "not_found", mcp_ops)

# ---- 9. decisions: reads never write; the explicit sweep -------------------------------------
gone_item = mk("gone1")
d_obj = db.add_pending_decision("retype", gone_item, {"options": [{"key": "image"}]})
c = conn()
c.execute("DELETE FROM capture_events WHERE slug = ?", (gone_item,))  # the object vanished (fixture)
c.commit()
c.close()
lonely = cards.create("Lonely").data["card"]
d_pm = db.add_pending_decision("project_match", B, {"candidate_project_ids": [lonely["id"], 999999]})
d_live_pm = db.add_pending_decision("project_match", C, {"candidate_project_ids": [lonely["id"], base["id"]]})
d_card = db.add_pending_decision("card_kind", cards.card_decision_slug(base["slug"]),
                                 {"question": "Kind?", "options": [{"key": "thing", "label": "Thing"}]})
d_dead_card = db.add_pending_decision("card_kind", "card:no-such-card", {"question": "?", "options": []})
d_sup = db.add_pending_decision(revisions.KIND_ITEM_SUPERSEDES, D, {"candidate_slugs": ["ghost-a"], "options": []})
open_before = [r_["id"] for r_ in q("SELECT id FROM pending_decisions WHERE resolved_at IS NULL ORDER BY id")]
n_audit, n_change = audit_count(), change_count()
listed = [e["id"] for e in decisions.list_open()]
for _ in range(5):
    r = client.get("/api/pending-decisions")
check("list_open + 5x GET /api/pending-decisions: no audit/change rows, no resolutions",
      audit_count() == n_audit and change_count() == n_change
      and [r_["id"] for r_ in q("SELECT id FROM pending_decisions WHERE resolved_at IS NULL ORDER BY id")] == open_before,
      (audit_count() - n_audit, change_count() - n_change))
check("reads leave the stale questions out (same as the old resolve-on-read)",
      sorted(listed) == sorted(i for i in open_before if i not in (d_obj, d_pm, d_dead_card, d_sup))
      and r.json()["count"] == len(listed), (listed, open_before))
check("count_open = what list_open shows", decisions.count_open() == len(listed))
check("stale reasons follow today's rules",
      [decisions.stale_reason(db.get_pending_decision(i)) for i in (d_obj, d_pm, d_dead_card, d_sup, d_card, d_live_pm)]
      == ["object deleted", "fewer than two candidates remain", "card deleted", "no candidates left", None, None])
before = snapshot()
res = decisions.sweep_stale(dry_run=True)
check("sweep dry run: reports 4, writes nothing", res.data["count"] == 4 and snapshot() == before and change_count() == n_change)
res = decisions.sweep_stale()
still_open = [r_["id"] for r_ in q("SELECT id FROM pending_decisions WHERE resolved_at IS NULL ORDER BY id")]
check("sweep resolves exactly the 4 stale ones; card + live questions untouched",
      sorted(x["id"] for x in res.data["resolved"]) == sorted([d_obj, d_pm, d_dead_card, d_sup])
      and d_card in still_open and d_live_pm in still_open, res.data)
check("sweep: one change-log row (sweep_stale_decisions, owner-ui here)", ops_of(res.batch_id) == [("sweep_stale_decisions", "owner-ui")])
check("sweep stored the stale reason", json.loads(q("SELECT payload FROM pending_decisions WHERE id = ?", d_pm)[0]["payload"])
      ["resolution"] == {"stale": "fewer than two candidates remain"})
res2 = decisions.sweep_stale()
check("second sweep: nothing to do, no change-log row", res2.data["count"] == 0 and ops_of(res2.batch_id) == [])
undo_ok("sweep", res.batch_id, before)
with actor.acting_as(actor.ACTOR_MCP):
    out = server.constructicon_sweep_stale_decisions()
check("MCP sweep: actor mcp", out["count"] == 4 and ops_of(out["batch_id"]) == [("sweep_stale_decisions", "mcp")], out)
# a resolved project_match question is one undoable batch
before = snapshot()
r = client.post(f"/api/pending-decisions/{d_live_pm}/resolve", data={"project_ids": [str(base["id"])]})
b = r.json().get("batch_id")
check("resolve project_match: add_files + resolve_decision in ONE batch",
      r.status_code == 200 and [o for o, _ in ops_of(b)] == ["add_files", "resolve_decision"], (r.text, ops_of(b)))
undo_ok("resolve project_match", b, before)

# ---- 10. layering ---------------------------------------------------------------------------
p = subprocess.run([sys.executable, os.path.join(os.path.dirname(os.path.abspath(__file__)), "check_layering.py")],
                   capture_output=True, text=True)
check("scripts/check_layering.py passes", p.returncode == 0, p.stdout + p.stderr)

ctx.__exit__(None, None, None)
print()
print("FAILED: %d" % len(FAILS) if FAILS else "all checks passed")
sys.exit(1 if FAILS else 0)
