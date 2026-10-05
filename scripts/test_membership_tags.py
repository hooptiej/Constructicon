#!/usr/bin/env python3
"""Self-contained check for #541 phase C: membership, tags, relations and the one delete-all.

Throwaway SQLite DB (CONSTRUCTICON_DB_PATH set before core is imported) built by the real
`db.init_db()`, a throwaway storage dir, the real FastAPI app through TestClient and the MCP tool
functions called directly. Covers:
  - every "put files on a card" path and the side-effect flags it passes (linked tag, free-text
    tag merge, auto-cover), each undone, with the actor recorded;
  - every remove path (membership only; tags and cover stay), each undone in place;
  - the card reshaping ops (copy/move/split) still have no tag/cover side effects;
  - tags: the item Save (tags + title + provenance = ONE change-log row), bulk attach-tags creating
    a tag, MCP attach/detach/create, undo removing a created tag, the in-use refusal, lookups
    never creating;
  - relations: add (sharing tags and cards both ways) and remove, each undone;
  - delete-all: the phrase, every table cleared or kept (and every table classified), storage and
    .trash emptied, one change-log row with counts.
No server needed:

    python scripts/test_membership_tags.py

Exits 1 if any check fails.
"""

import io
import json
import os
import sqlite3
import sys
import tempfile
import time
from pathlib import Path

TMP = tempfile.mkdtemp(prefix="membership-tags-")
os.environ["CONSTRUCTICON_DB_PATH"] = os.path.join(TMP, "test.db")
os.environ.setdefault("CAPTION_DISABLED", "1")
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))

try:
    import cairosvg  # noqa: F401
except Exception:  # no libcairo here (e.g. Windows): object types import it
    import types
    sys.modules["cairosvg"] = types.ModuleType("cairosvg")

from PIL import Image  # noqa: E402

from core import actor, cards, db, ingest, items, membership, reset, revisions, storage  # noqa: E402
from core import tags as tags_svc  # noqa: E402
from core.errors import AppError  # noqa: E402

storage.STORAGE_DIR = Path(TMP) / "storage"
storage.STORAGE_DIR.mkdir()
ingest.run_in_thread = lambda fn, *a: None  # no background threads in a unit check

FAILS = []


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


def q(sql, *args):
    c = sqlite3.connect(os.environ["CONSTRUCTICON_DB_PATH"])
    c.row_factory = sqlite3.Row
    try:
        return [dict(r) for r in c.execute(sql, args)]
    finally:
        c.close()


def mk(slug):
    """An image item with a real file and a thumbnail on disk."""
    sf = f"{slug}.png"
    buf = io.BytesIO()
    Image.new("RGB", (8, 8), (200, 10, 10)).save(buf, "PNG")
    (storage.STORAGE_DIR / sf).write_bytes(buf.getvalue())
    storage.thumb_path_for(slug).write_bytes(b"thumb-" + slug.encode())
    db.insert_upload(slug, f"{slug}.png", sf, "tester", media_type="image")
    return slug


def card(title, with_tag=True):
    tag = db.get_or_create_tag(title) if with_tag else None
    return db.create_project(title, tag_id=tag["id"] if tag else None, with_writeup=False)


def state(card_id, slugs):
    """Everything an add/remove can touch, for before/after comparisons."""
    p = db.get_project(card_id)
    return {
        "members": [r["post_slug"] for r in db.list_project_item_rows(card_id)],
        "rows": [(r["rowid"], r["post_slug"], r["sort_order"]) for r in db.list_project_item_rows(card_id)],
        "cover": (p["cover_slug"], p["cover_project_id"]),
        "post_tags": {s: sorted(r["tag_id"] for r in db.list_post_tag_rows(s)) for s in slugs},
        "free": {s: db.get_by_slug(s)["tags"] for s in slugs},
    }


def changes_of(batch_id):
    return q("SELECT op, actor, mutations FROM audit_log WHERE batch_id = ? AND op IS NOT NULL ORDER BY id", batch_id)


def last_batch(op):
    rows = q("SELECT batch_id FROM audit_log WHERE op = ? ORDER BY id DESC LIMIT 1", op)
    return rows[0]["batch_id"] if rows else None


def tag_names(slug):
    return sorted(t["name"] for t in db.list_tags_for_post(slug))


db.init_db()
ctx = actor.acting_as(actor.ACTOR_UI)
ctx.__enter__()

# ---- 1. add_files with every side effect (the upload / item page / bulk behaviour) ----------
A, B, C, D = mk("a1"), mk("b1"), mk("c1"), mk("d1")
P = card("Rover Build")
TAG = db.get_tag(P["tag_id"])
before = state(P["id"], [A, B])
res = membership.add_files(P["id"], [A, B], **membership.UI_EFFECTS)
after = state(P["id"], [A, B])
check("UI flags: both files on the card, in order", after["members"] == [A, B], after["members"])
check("UI flags: linked tag attached to both", all(TAG["id"] in after["post_tags"][s] for s in (A, B)))
check("UI flags: tag name merged into free-text tags", all(TAG["name"] in after["free"][s] for s in (A, B)))
check("UI flags: coverless card takes the FIRST file as cover", after["cover"] == (A, None), after["cover"])
rows = changes_of(res.batch_id)
check("add_files is ONE change-log row (op add_files, actor owner-ui)",
      [(r["op"], r["actor"]) for r in rows] == [("add_files", "owner-ui")], rows)
tables = sorted({m["table"] for m in json.loads(rows[0]["mutations"])})
check("add_files images project_items, post_tags, capture_events (free text) and projects (cover)",
      tables == ["capture_events", "post_tags", "project_items", "projects"], tables)
cards.undo(res.batch_id)
check("undo add_files reverts membership, linked tag, free-text tag and cover", state(P["id"], [A, B]) == before,
      state(P["id"], [A, B]))

# already a member: side effects still apply (as the old attach_to_project did)
plain = membership.add_files(P["id"], [A], **membership.NO_EFFECTS)
check("NO_EFFECTS: membership only", db.get_project(P["id"])["cover_slug"] is None and db.list_post_tag_rows(A) == [])
res = membership.add_files(P["id"], [A], **membership.UI_EFFECTS)
check("already a member: no second row, but tag/cover still applied",
      res.data["added"] == [] and res.data["already"] == [A] and db.get_project(P["id"])["cover_slug"] == A
      and TAG["id"] in [t["id"] for t in db.list_tags_for_post(A)])
cards.undo(res.batch_id)
cards.undo(plain.batch_id)
check("both undone: card empty again, no tag, no cover",
      db.list_project_item_rows(P["id"]) == [] and db.list_post_tag_rows(A) == []
      and db.get_project(P["id"])["cover_slug"] is None)

# a card that has a cover keeps it; a card with no linked tag adds no tag
Q = card("Desk Build", with_tag=False)
db.update_project(Q["id"], cover_slug=D)
res = membership.add_files(Q["id"], [C], **membership.UI_EFFECTS)
check("card with a cover keeps it; no linked tag -> no tag writes",
      db.get_project(Q["id"])["cover_slug"] == D and db.list_post_tag_rows(C) == [] and db.get_by_slug(C)["tags"] == [])
cards.undo(res.batch_id)
check("unknown file refuses before writing (not_found)", code_of(membership.add_files, P["id"], ["nope"],
                                                                  **membership.UI_EFFECTS) == "not_found"
      and db.list_project_item_rows(P["id"]) == [])
check("unknown card -> not_found", code_of(membership.add_files, "no-card", [A], **membership.UI_EFFECTS) == "not_found")
try:
    membership.add_files(P["id"], [A])
    check("flags are keyword-required", False)
except TypeError:
    check("flags are keyword-required", True)
res = membership.add_files(P["id"], [A, B], dry_run=True, **membership.UI_EFFECTS)
check("dry run writes nothing", db.list_project_item_rows(P["id"]) == [] and db.get_project(P["id"])["cover_slug"] is None
      and changes_of(res.batch_id) == [])

# ingest.attach_to_project (upload / automatch / decision path)
before = state(P["id"], [C])
r = ingest.attach_to_project(C, P["id"])
s = state(P["id"], [C])
check("ingest.attach_to_project = UI flags", s["members"] == [C] and TAG["id"] in s["post_tags"][C]
      and TAG["name"] in s["free"][C] and s["cover"] == (C, None))
check("attach_to_project ignores an unknown card, as before", ingest.attach_to_project(C, 999999) is None)
cards.undo(r.batch_id)
check("undo upload-style add", state(P["id"], [C]) == before)

# ---- 2. web adapters ---------------------------------------------------------------------
from fastapi.testclient import TestClient  # noqa: E402
from web import app as webapp  # noqa: E402

client = TestClient(webapp.app)

before = state(P["id"], [A])
r = client.post(f"/api/image/{A}/project", data={"project_id": str(P["id"])})
s = state(P["id"], [A])
check("web item page add: 200 + UI flags", r.status_code == 200 and s["members"] == [A] and s["cover"] == (A, None)
      and TAG["name"] in s["free"][A] and TAG["id"] in s["post_tags"][A], r.text[:200])
b = last_batch("add_files")
check("web add recorded as owner-ui", changes_of(b)[0]["actor"] == "owner-ui")
r = client.post(f"/api/changes/{b}/undo")
check("web undo of the add restores everything", r.status_code == 200 and state(P["id"], [A]) == before, r.text[:200])

before = state(P["id"], [A, B, C])
r = client.post("/api/bulk/add-to-project", data={"slugs": [A, "ghost", B, C], "project_id": str(P["id"])})
body = r.json()
s = state(P["id"], [A, B, C])
check("bulk add: count + batch_id, unknown slug skipped, UI flags",
      body.get("count") == 3 and body.get("batch_id") and s["members"] == [A, B, C] and s["cover"] == (A, None)
      and all(TAG["id"] in s["post_tags"][x] for x in (A, B, C)), body)
check("bulk add is one change-log row", len(changes_of(body["batch_id"])) == 1)
client.post(f"/api/changes/{body['batch_id']}/undo")
check("undo bulk add", state(P["id"], [A, B, C]) == before)
r = client.post("/api/bulk/add-to-project", data={"slugs": [A], "project_id": "999999"})
check("bulk add to a missing card: silent no-op with the count, as before",
      r.json() == {"count": 1} and db.list_project_item_rows(P["id"]) == [])

# remove paths: project page x, item page remove, MCP remove
membership.add_files(P["id"], [A, B, C], **membership.UI_EFFECTS)
before = state(P["id"], [A, B, C])
r = client.post(f"/api/projects/{P['id']}/remove-item", data={"slug": B})
s = state(P["id"], [A, B, C])
check("project page x: 200 + batch_id, membership only (tags + cover stay)",
      r.status_code == 200 and r.json().get("success") is True and r.json().get("batch_id")
      and s["members"] == [A, C] and s["post_tags"] == before["post_tags"] and s["free"] == before["free"]
      and s["cover"] == before["cover"], r.text[:200])
client.post(f"/api/changes/{r.json()['batch_id']}/undo")
check("undo of x restores the row in place (rowid + sort_order)", state(P["id"], [A, B, C]) == before)
r = client.post(f"/api/image/{A}/project/remove", data={"project_id": str(P["id"])})
s = state(P["id"], [A, B, C])
check("item page remove: membership only, even of the cover file",
      r.status_code == 200 and s["members"] == [B, C] and s["cover"] == (A, None) and s["post_tags"] == before["post_tags"])
cards.undo(last_batch("remove_files"))
check("undo item page remove", state(P["id"], [A, B, C]) == before)
r = client.post(f"/api/projects/{P['id']}/remove-item", data={"slug": "ghost"})
check("x on a missing object -> 404", r.status_code == 404)

# from-selection / from-related: new card + UI flags, one add batch
r = client.post("/api/projects/from-selection", data={"slugs": [D, "ghost"], "title": "Sel Card"})
sel = db.get_project(r.json()["id"])
check("from-selection: files on with linked tag and cover", r.status_code == 200
      and [x["post_slug"] for x in db.list_project_item_rows(sel["id"])][-1] == D
      and sel["tag_id"] in [t["id"] for t in db.list_tags_for_post(D)])

# ---- 3. MCP adapters (actor mcp) ---------------------------------------------------------
from mcp_server import server  # noqa: E402

R = card("Mcp Build")
RT = db.get_tag(R["tag_id"])
before = state(R["id"], [C, D])
with actor.acting_as(actor.ACTOR_MCP):
    server.constructicon_add_to_project(C, R["id"])
s = state(R["id"], [C, D])
check("MCP add_to_project now = web flags (linked tag + free-text name + cover)",
      s["members"] == [C] and RT["id"] in s["post_tags"][C] and RT["name"] in s["free"][C] and s["cover"] == (C, None), s)
b = last_batch("add_files")
check("MCP add recorded as mcp", changes_of(b)[0]["actor"] == "mcp")
cards.undo(b)
check("undo MCP add", state(R["id"], [C, D]) == before)
with actor.acting_as(actor.ACTOR_MCP):
    out = server.constructicon_add_items_to_project(R["id"], [D, "ghost", C])
s = state(R["id"], [C, D])
check("MCP add_items_to_project: same flags, skips unknown, returns the added objects",
      [o["slug"] for o in out] == [D, C] and s["members"] == [D, C] and s["cover"] == (D, None)
      and RT["name"] in s["free"][C] and RT["id"] in s["post_tags"][D])
b = last_batch("add_files")
check("MCP bulk add is one mcp row", [(x["op"], x["actor"]) for x in changes_of(b)] == [("add_files", "mcp")])
with actor.acting_as(actor.ACTOR_MCP):
    server.constructicon_remove_from_project(D, R["id"])
check("MCP remove: membership only", [x["post_slug"] for x in db.list_project_item_rows(R["id"])] == [C]
      and RT["id"] in [t["id"] for t in db.list_tags_for_post(D)])
cards.undo(last_batch("remove_files"))
cards.undo(b)
check("undo MCP bulk add", state(R["id"], [C, D]) == before)
W = mk("w1")
db.update_content_metadata(W, type_metadata={"body": ""})
db.set_media_type(W, "document")
with actor.acting_as(actor.ACTOR_MCP):
    server.constructicon_set_project_writeup(R["id"], W)
check("MCP set_project_writeup: membership only (no tag, no cover)",
      W in [x["post_slug"] for x in db.list_project_item_rows(R["id"])] and db.list_post_tag_rows(W) == []
      and db.get_project(R["id"])["cover_slug"] is None)

# ---- 4. card reshaping ops: no side effects ---------------------------------------------
S = card("Src Card")
T = card("Dst Card")
membership.write(S["id"], [A, B], [], "test_seed", None)
before_t = state(T["id"], [A, B])
res = cards.copy_files([A], S["id"], T["id"])
s = state(T["id"], [A, B])
check("copy_files: membership only, no tag/cover, op copy_files",
      s["members"] == [A] and s["cover"] == (None, None) and s["post_tags"] == before_t["post_tags"]
      and s["free"] == before_t["free"] and [x["op"] for x in changes_of(res.batch_id)] == ["copy_files"])
cards.undo(res.batch_id)
res = cards.move_files([A, B], S["id"], T["id"])
check("move_files: rows moved, ops logged once per side",
      [x["post_slug"] for x in db.list_project_item_rows(T["id"])] == [A, B] and db.list_project_item_rows(S["id"]) == []
      and [x["op"] for x in changes_of(res.batch_id)] == ["move_files", "move_files"])
cards.undo(res.batch_id)
res = cards.split_card(S["id"], [{"title": "Split Part", "file_slugs": [B]}], dry_run=True)
check("split dry run plans the move and writes nothing", res.dry_run and changes_of(res.batch_id) == []
      and [x["post_slug"] for x in db.list_project_item_rows(S["id"])] == [A, B])
res = cards.split_card(S["id"], [{"title": "Split Part", "file_slugs": [B]}])
new = db.get_project(res.data["created"][0]["id"])
check("split: the file moves, no tag/cover side effects",
      B in [x["post_slug"] for x in db.list_project_item_rows(new["id"])]
      and B not in [x["post_slug"] for x in db.list_project_item_rows(S["id"])] and new["cover_slug"] is None)
cards.undo(res.batch_id)

# ---- 5. tags ---------------------------------------------------------------------------
E = mk("e1")
items.update(E, tags=["alpha"])
before_e = db.get_by_slug(E)
n_tags = q("SELECT COUNT(*) AS n FROM blog_tags")[0]["n"]
r = client.post(f"/api/image/{E}", data={"tags": json.dumps(["alpha", "brand-new-tag"]), "display_name": "Edited",
                                        "provenance": "found"})
row = db.get_by_slug(E)
b = last_batch("item_update")
check("item Save (tags + title + provenance): written", r.status_code == 200 and row["tags"] == ["alpha", "brand-new-tag"]
      and row["display_name"] == "Edited" and row["provenance"] == "found" and "brand-new-tag" in tag_names(E), r.text[:200])
check("item Save is ONE change-log row", [x["op"] for x in changes_of(b)] == ["item_update"])
check("the Save created + imaged the new tag",
      any(m["table"] == "blog_tags" and m["before"] is None for m in json.loads(changes_of(b)[0]["mutations"])))
cards.undo(b)
row = db.get_by_slug(E)
check("undo the Save: title, provenance, free-text tags, post_tags restored and the created tag removed",
      row["display_name"] == before_e["display_name"] and row["provenance"] == before_e["provenance"]
      and row["tags"] == ["alpha"] and tag_names(E) == ["alpha"]
      and q("SELECT COUNT(*) AS n FROM blog_tags")[0]["n"] == n_tags and tags_svc.find("brand-new-tag") is None)
items.update(E, tags=[])
check("removing a typed tag detaches it (previous-list semantics)", tag_names(E) == [])

# bulk attach-tags creating a tag
r = client.post("/api/bulk/attach-tags", data={"slugs": [C, D, "ghost"], "tag_names": ["bulk-made"]})
body = r.json()
check("bulk attach-tags: count + batch_id, tag created and attached both ways",
      body.get("count") == 2 and body.get("batch_id") and "bulk-made" in db.get_by_slug(C)["tags"]
      and "bulk-made" in tag_names(D), body)
client.post(f"/api/changes/{body['batch_id']}/undo")
check("undo bulk attach-tags: rows gone and the created tag removed",
      tags_svc.find("bulk-made") is None and "bulk-made" not in db.get_by_slug(C)["tags"] and "bulk-made" not in tag_names(D))

# MCP attach / detach / create, lookups never create
base_c = tag_names(C)
with actor.acting_as(actor.ACTOR_MCP):
    server.constructicon_attach_tags(C, ["mcp-made", "alpha"])
b = last_batch("tag_attach")
check("MCP attach_tags: post_tags only (free text untouched), creates missing, actor mcp",
      tag_names(C) == sorted(base_c + ["alpha", "mcp-made"]) and "mcp-made" not in db.get_by_slug(C)["tags"]
      and changes_of(b)[0]["actor"] == "mcp", (tag_names(C), db.get_by_slug(C)["tags"], changes_of(b)))
with actor.acting_as(actor.ACTOR_MCP):
    server.constructicon_attach_tags(D, ["mcp-made"])
check("undo refused while a later op uses the created tag",
      code_of(cards.undo, b) == "undo_conflict" and tags_svc.find("mcp-made") is not None)
cards.undo(last_batch("tag_attach"))
cards.undo(b)
check("undo MCP attach removes rows and the created tag", tag_names(C) == base_c and tags_svc.find("mcp-made") is None)
with actor.acting_as(actor.ACTOR_MCP):
    server.constructicon_attach_tags(C, ["alpha"])
    server.constructicon_detach_tag(C, "alpha")
check("MCP detach_tag", tag_names(C) == base_c)
cards.undo(last_batch("tag_detach"))
check("undo detach", tag_names(C) == sorted(base_c + ["alpha"]))
with actor.acting_as(actor.ACTOR_MCP):
    t = server.constructicon_create_tag("Child Tag", parent_name="Parent Tag")
b = last_batch("tag_create")
check("MCP create_tag makes parent + child in one batch",
      t["parent_id"] == tags_svc.find("Parent Tag")["id"] and len(json.loads(changes_of(b)[0]["mutations"])) == 2)
with actor.acting_as(actor.ACTOR_MCP):
    again = server.constructicon_create_tag("Child Tag", parent_name="Parent Tag")
check("create_tag of an existing tag returns it, writes nothing", again["id"] == t["id"] and last_batch("tag_create") == b)
cards.undo(b)
check("undo create_tag removes both", tags_svc.find("Child Tag") is None and tags_svc.find("Parent Tag") is None)
n_tags = q("SELECT COUNT(*) AS n FROM blog_tags")[0]["n"]
with actor.acting_as(actor.ACTOR_MCP):
    out1 = server.constructicon_get_posts_for_tag("nothing-here")
    out2 = server.constructicon_detach_tag(C, "nothing-here")
check("lookups never create a tag", out1.get("ok") is False and out2.get("ok") is False
      and q("SELECT COUNT(*) AS n FROM blog_tags")[0]["n"] == n_tags)
with actor.acting_as(actor.ACTOR_MCP):
    server.constructicon_update(E, tags=["mcp-typed"], display_name="Via MCP")
b = last_batch("item_update")
check("MCP update with tags: one item_update row (actor mcp)",
      [(x["op"], x["actor"]) for x in changes_of(b)] == [("item_update", "mcp")] and tag_names(E) == ["mcp-typed"])
cards.undo(b)
check("undo MCP update", tag_names(E) == [] and tags_svc.find("mcp-typed") is None)

# ---- 6. relations ----------------------------------------------------------------------
X, Y = mk("x1"), mk("y1")
items.update(X, tags=["xtag"])
membership.add_files(R["id"], [Y], **membership.NO_EFFECTS)
snap = (tag_names(X), tag_names(Y), [p["id"] for p in db.list_projects_for_post(X)],
        [p["id"] for p in db.list_projects_for_post(Y)])
r = client.post(f"/api/image/{X}/related", data={"related_slug": Y})
check("add related: linked both ways and categorization shared",
      r.status_code == 200 and [o["slug"] for o in r.json()] == [Y] and tag_names(Y) == ["xtag"]
      and R["id"] in [p["id"] for p in db.list_projects_for_post(X)]
      and q("SELECT COUNT(*) AS n FROM capture_event_relations WHERE slug_a IN (?, ?)", X, Y)[0]["n"] == 2)
b = last_batch("item_relate")
tables = sorted({m["table"] for m in json.loads(changes_of(b)[0]["mutations"])})
check("relate images relations, post_tags and project_items", tables == ["capture_event_relations", "post_tags",
                                                                          "project_items"], tables)
cards.undo(b)
check("undo add related: link AND the shared tags/cards reverted",
      (tag_names(X), tag_names(Y), [p["id"] for p in db.list_projects_for_post(X)],
       [p["id"] for p in db.list_projects_for_post(Y)]) == snap and db.list_related(X) == [])
with actor.acting_as(actor.ACTOR_MCP):
    server.constructicon_add_related(X, Y)
    server.constructicon_remove_related(X, Y)
check("MCP remove related: link gone, shared tags stay (as before)", db.list_related(X) == [] and tag_names(Y) == ["xtag"])
cards.undo(last_batch("item_unrelate"))
check("undo remove related", [o["slug"] for o in db.list_related(X)] == [Y] and [o["slug"] for o in db.list_related(Y)] == [X])

# ---- 7. delete-all ---------------------------------------------------------------------
classified = set(reset.CLEARED_TABLES) | set(reset.KEPT_TABLES)
unclassified = [t for t in db.list_tables() if t not in classified]
check("every table is classified as cleared or kept by delete-all", unclassified == [], unclassified)
# make every cleared table non-empty
entry = db.create_blog_entry("An entry")
db.set_entry_items(entry["id"], [(A, "")])
db.set_entry_projects(entry["id"], [(P["id"], "")])
db.add_pending_decision("project_match", A, {"candidate_project_ids": [P["id"]]})
db.set_curator_state("decision:1", "dismiss")
db.enqueue_caption(A)
revisions.mark_superseded(B, C)
cards.link(P["id"], Q["id"], "related")
fam = db.create_project("Fam", kind="family", with_writeup=False)
cards.add_to_family(fam["id"], P["id"])
hob = db.get_or_create_tag("A Hobby")
db.mark_tag_as_hobby(hob["id"])
cards.add_to_hobby(P["id"], hob["id"])
items.delete([W])  # a trash row and a file in .trash
db.set_setting("some_key", "kept")
empty = [t for t in reset.CLEARED_TABLES if q(f"SELECT COUNT(*) AS n FROM {t}")[0]["n"] == 0]
check("delete-all fixture: every cleared table has rows", empty == [], empty)
check("storage and .trash hold files", any(storage.STORAGE_DIR.glob("*.png")) and any(items.trash_dir().rglob("*.png")))
r = client.post("/api/delete-all", data={"confirm": "yes"})
check("web delete-all wrong phrase -> 400 confirm_required, nothing cleared",
      r.status_code == 400 and r.json()["error"]["code"] == "confirm_required"
      and q("SELECT COUNT(*) AS n FROM capture_events")[0]["n"] > 0, r.text[:200])
with actor.acting_as(actor.ACTOR_MCP):
    out = server.constructicon_delete_all()
check("MCP delete-all without the phrase refuses", out.get("ok") is False and out["error"]["code"] == "confirm_required")
n_items = q("SELECT COUNT(*) AS n FROM capture_events")[0]["n"]
kept_before = {t: q(f"SELECT COUNT(*) AS n FROM {t}")[0]["n"] for t in reset.KEPT_TABLES}
with actor.acting_as(actor.ACTOR_MCP):
    out = server.constructicon_delete_all(confirm="DELETE EVERYTHING")
left = {t: q(f"SELECT COUNT(*) AS n FROM {t}")[0]["n"] for t in reset.CLEARED_TABLES}
check("delete-all clears every cleared table", all(v == 0 for v in left.values()), left)
check("delete-all reports the item count", out.get("deleted") == n_items, out)
files_left = [p for p in storage.STORAGE_DIR.rglob("*") if p.is_file()]
check("storage files and thumbnails removed, .trash gone", files_left == [] and not items.trash_dir().exists(), files_left)
kept_after = {t: q(f"SELECT COUNT(*) AS n FROM {t}")[0]["n"] for t in reset.KEPT_TABLES}
check("kept tables keep their rows (audit_log grows by the reset's record)",
      all(kept_after[t] == kept_before[t] for t in reset.KEPT_TABLES if t != "audit_log")
      and kept_after["audit_log"] == kept_before["audit_log"] + 1, (kept_before, kept_after))
rec = q("SELECT op, actor, form_body, mutations FROM audit_log WHERE op = 'delete_all'")
check("one change-log row: op delete_all, actor mcp, counts recorded",
      len(rec) == 1 and rec[0]["actor"] == "mcp" and json.loads(rec[0]["form_body"])["counts"]["capture_events"] == n_items,
      rec)
check("the reset is not undoable", code_of(cards.undo, out["batch_id"]) == "undo_refused")

ctx.__exit__(None, None, None)
print()
print("FAILED: %d" % len(FAILS) if FAILS else "all checks passed")
sys.exit(1 if FAILS else 0)
