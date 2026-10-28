"""Self-contained check for #555 ("Write-only tags"): one tag store, the tag tree.

  1. the tags_one_store_555 migration on a pre-#555 DB: free-text-only chips merged into the tree
     (an existing tag reused in any case), card-name chips left to the card, one change-log row
     (actor migration) with the counts, the chip cache rewritten and its triggers on; idempotent;
  2. every writer lands in the tree: the item Save (web + items.update), set_item_tags, bulk
     attach-tags, tags.attach, MCP item_tags / update, an upload's tags, upload-time automatch; the
     cached chips always equal the tree (even after a raw write to the column);
  3. the search tag filter (db.search, GET /api/search, MCP search) finds tree-only tags, sub-tags
     of a named tag, and a card's linked tag;
  4. chips never show the linked tag of a card the file is on (the card shows as a link);
  5. a card rename renames its linked tag in the same batch (undo restores both), with a warning when
     another tag already has the name;
  6. rename / merge / move / delete, each undoable, and every refusal (card tag, hobby tag, cycle,
     clash, sub-tags), through core, the web routes and the item chip's remove;
  7. the cleanup sweep lists "Ancient projects."-style tags (orphan, empty, near-duplicate), deletes
     only what is picked, refuses unsafe picks, and undoes;
  8. the MCP tool constructicon_tags (every action, refusals, undo);
  9. a viewer: hidden card tags stay out of the tree, the chips and the sweep; chip buttons greyed;
     writes refused (web 403, MCP forbidden).

Throwaway DB, no server:  python scripts/test_tags_555.py   -> ALL PASS / FAILED: ...
"""

import io
import json
import os
import re
import secrets
import sqlite3
import sys

import _testenv  # noqa: E402  (scripts/_testenv.py: temp DB + storage + exports, refuses otherwise)
TMP = _testenv.isolate("tags-555-")
os.environ.setdefault("CAPTION_DISABLED", "1")
ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")
sys.path.insert(0, ROOT)

try:
    import cairosvg  # noqa: F401
except Exception:  # silent-ok: no libcairo here (e.g. Windows): object types import it
    import types
    sys.modules["cairosvg"] = types.ModuleType("cairosvg")

from PIL import Image  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from core import actor, automatch, cards, db, hobbies, ingest, items, membership, paths, sharing, users  # noqa: E402
from core import tags  # noqa: E402
_testenv.assert_isolated()
from core.errors import AppError  # noqa: E402

ingest.run_in_thread = lambda fn, *a: None  # no background threads in a unit check

FAILS = []
BASE = "http://testhost.local"
SAME = {"Origin": BASE}


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


def execute(sql, *args):
    c = sqlite3.connect(os.environ["CONSTRUCTICON_DB_PATH"])
    try:
        c.execute(sql, args)
        c.commit()
    finally:
        c.close()


def mk(slug, tag_list=None):
    sf = f"{slug}.png"
    buf = io.BytesIO()
    Image.new("RGB", (8, 8), (20, 200, 10)).save(buf, "PNG")
    (paths.storage_dir() / sf).write_bytes(buf.getvalue())
    db.insert_upload(slug, f"{slug}.png", sf, "tester", media_type="image", tags=tag_list)
    return slug


def card(title, tag_name=None):
    tag = db._get_or_create_tag(tag_name or title)
    return db._create_project(title, tag_id=tag["id"], with_writeup=False)


def tree_names(slug):
    return sorted(t["name"] for t in db.list_tags_for_post(slug))


def chips(slug):
    return db.get_by_slug(slug)["tags"]


def tag_named(name):
    rows = q("SELECT * FROM blog_tags WHERE name = ?", name)
    return rows[0] if rows else None


def batch_ops(batch_id):
    return [(r["op"], r["actor"]) for r in q("SELECT op, actor FROM audit_log WHERE batch_id = ? AND op IS NOT NULL "
                                             "ORDER BY id", batch_id)]


def cache_ok():
    return db.check_chip_cache() == []


db.init_db()
users.limiter.reset()
ctx = actor.acting_as(actor.ACTOR_UI)
ctx.__enter__()

# ---- 1. the migration on a pre-#555 DB --------------------------------------------------------------
print("--- 1. migration ---")
I1, I2, I3, I4, I5 = mk("i1"), mk("i2"), mk("i3"), mk("i4"), mk("i5")
L = card("Losi 8IGHT-E 2.0", tag_name="Losi 8IGHT")  # a drifted card: its tag kept the old title
membership.add_files(L["id"], [I1], **membership.UI_EFFECTS)
KERBAL = db._get_or_create_tag("Kerbal")
ANCIENT = db._get_or_create_tag("Ancient Projects")
db._attach_tags(I3, [ANCIENT["id"]])
db._attach_tags(I4, [KERBAL["id"]])
DOT = db._get_or_create_tag("Ancient projects.")  # the prod junk: a root tag with a trailing full stop
check("setup: the chip triggers exist on a migrated DB",
      len(q("SELECT name FROM sqlite_master WHERE type='trigger' AND name LIKE 'item_chips_%'")) == len(db._chip_triggers()))
# turn the clock back: no triggers, no migration record, and free text that disagrees with the tree
for (name,) in [(r["name"],) for r in q("SELECT name FROM sqlite_master WHERE type='trigger' AND name LIKE 'item_chips_%'")]:
    execute(f"DROP TRIGGER {name}")
execute("DELETE FROM schema_migrations WHERE name IN ('tags_one_store_555', 'search_tags_tree_555')")
execute("UPDATE capture_events SET tags = ? WHERE slug = ?", json.dumps(["Losi 8IGHT", "Losi 8IGHT-E 2.0", "rc"]), I1)
execute("UPDATE capture_events SET tags = ? WHERE slug = ?", json.dumps(["kerbal"]), I2)
execute("UPDATE capture_events SET tags = ? WHERE slug = ?", json.dumps(["Ancient Projects"]), I3)
execute("UPDATE capture_events SET tags = ? WHERE slug = ?", "[]", I4)
execute("UPDATE capture_events SET tags = ? WHERE slug = ?", "not json", I5)
tags_before = q("SELECT COUNT(*) AS n FROM blog_tags")[0]["n"]
ran = db.run_pending_migrations()
check("the migration ran (tags_one_store_555 + the search rebuild)", ran == ["tags_one_store_555", "search_tags_tree_555"], ran)
mig = q("SELECT * FROM audit_log WHERE op = 'migration_tags_one_store_555' ORDER BY id DESC LIMIT 1")[0]
d = json.loads(mig["form_body"])
check("one change-log row, actor migration", mig["actor"] == "migration" and mig["batch_id"], mig["actor"])
check("counts: 5 items, 3 disagreed, 2 changed, 2 links, 1 tag made, 1 card-name chip skipped, 1 unreadable",
      (d["items"], d["disagreeing"], d["items_changed"], d["links_added"], d["tags_created"], d["skipped_card_names"],
       d["unreadable"]) == (5, 3, 2, 2, 1, 1, 1), {k: d[k] for k in ("items", "disagreeing", "items_changed",
                                                                   "links_added", "tags_created", "skipped_card_names",
                                                                   "unreadable")})
check("the details name the card chips left to the card", sorted(d["card_name_chips"].get(I1, [])) ==
      ["Losi 8IGHT", "Losi 8IGHT-E 2.0"], d["card_name_chips"])
check("the change-log row is imaged (post_tags + the new tag)",
      {m["table"] for m in json.loads(mig["mutations"])} == {"post_tags", "blog_tags"})
check("i1: the free-only chip 'rc' is a tree tag now; no card-name tag was made",
      tree_names(I1) == ["Losi 8IGHT", "rc"] and tag_named("Losi 8IGHT-E 2.0") is None, tree_names(I1))
check("i2: 'kerbal' reused the existing 'Kerbal' (any case), no near-duplicate made",
      tree_names(I2) == ["Kerbal"] and tag_named("kerbal") is None)
check("tags created: exactly one", q("SELECT COUNT(*) AS n FROM blog_tags")[0]["n"] == tags_before + 1)
check("the chip triggers are on again", len(q("SELECT name FROM sqlite_master WHERE type='trigger' "
                                              "AND name LIKE 'item_chips_%'")) == len(db._chip_triggers()))
check("the cache was rewritten from the tree: i1 [rc] (card tag hidden), i2/i4 [Kerbal], i5 []",
      (chips(I1), chips(I2), chips(I4), chips(I5)) == (["rc"], ["Kerbal"], ["Kerbal"], []) and cache_ok(),
      (chips(I1), chips(I2), chips(I4), chips(I5)))
check("undo of the migration is refused like every migration row (the snapshot is the way back)",
      code_of(cards.undo, mig["batch_id"]) == "undo_refused")
# idempotence: run it again from scratch
execute("DELETE FROM schema_migrations WHERE name IN ('tags_one_store_555', 'search_tags_tree_555')")
links_before = q("SELECT COUNT(*) AS n FROM post_tags")[0]["n"]
db.run_pending_migrations()
d2 = json.loads(q("SELECT form_body FROM audit_log WHERE op = 'migration_tags_one_store_555' ORDER BY id DESC LIMIT 1")[0]["form_body"])
check("idempotent: a second run adds nothing", d2["links_added"] == 0 and d2["items_changed"] == 0 and d2["disagreeing"] == 0
      and q("SELECT COUNT(*) AS n FROM post_tags")[0]["n"] == links_before, d2)

# ---- 2. every writer lands in the tree ----------------------------------------------------------------
print("--- 2. writers ---")
from web import app as webapp  # noqa: E402
client = _testenv.client(webapp.app, base_url=BASE)
from mcp_server import server  # noqa: E402

W = mk("w1", tag_list=["uploaded-tag"])
check("an upload's tags go into the tree (op upload_tags)", tree_names(W) == ["uploaded-tag"] and chips(W) == ["uploaded-tag"]
      and q("SELECT COUNT(*) AS n FROM audit_log WHERE op = 'upload_tags'")[0]["n"] >= 1)
items.update(W, tags=["uploaded-tag", "saved"])
check("items.update(tags=): the tree", tree_names(W) == ["saved", "uploaded-tag"] and cache_ok())
r = client.post(f"/api/image/{W}", data={"tags": json.dumps(["saved", "web-typed"])}, headers=SAME)
check("the web item Save: the tree (full replace of the chips)", r.status_code == 200 and tree_names(W) == ["saved", "web-typed"],
      r.text[:200])
tags.set_item_tags(W, ["saved"])
check("tags.set_item_tags: full replace", tree_names(W) == ["saved"] and chips(W) == ["saved"])
r = client.post("/api/bulk/attach-tags", data={"slugs": [W, I4], "tag_names": ["bulk-555"]}, headers=SAME)
check("bulk attach-tags: the tree, both items", r.status_code == 200 and "bulk-555" in tree_names(W) and "bulk-555" in tree_names(I4))
tags.attach(W, ["attached-555"])
check("tags.attach: the tree, and the chip follows", "attached-555" in tree_names(W) and "attached-555" in chips(W))
with actor.acting_as(actor.ACTOR_MCP):
    server.constructicon_item_tags(W, ["mcp-555"])
    server.constructicon_update(W, tags=["saved", "mcp-update-555"])
check("MCP item_tags + update(tags=): the tree", tree_names(W) == ["mcp-update-555", "saved"], tree_names(W))
A1 = mk("kerbal-mun-landing")
res = automatch.apply_to_upload(A1, ["kerbal mun landing"])
check("upload-time automatch: through the tag service (op automatch_tags), in the tree",
      "Kerbal" in tree_names(A1) and "Kerbal" in res["tags"]
      and q("SELECT COUNT(*) AS n FROM audit_log WHERE op = 'automatch_tags'")[0]["n"] == 1)
execute("UPDATE capture_events SET tags = ? WHERE slug = ?", json.dumps(["bogus"]), W)
check("a raw write to the cache column is put back to the tree's answer", chips(W) == ["saved", "mcp-update-555"], chips(W))
check("the chip cache equals the tree for every item", cache_ok(), db.check_chip_cache())

# ---- 3. the search tag filter reads the tree --------------------------------------------------------
print("--- 3. search ---")
S = mk("s1")
tags.create("Child555", parent_name="Parent555")
tags.attach(S, ["tree-only-555"])
with db.transaction():
    with db.ImageLog("test_seed", None, "seed555") as log:
        tags.link(log, S, tag_named("Child555")["id"])
found = [r["slug"] for r in db.search(tags=["tree-only-555"])]
check("db.search(tags=) finds a tree-only tag", found == [S], found)
check("... in any case", [r["slug"] for r in db.search(tags=["TREE-ONLY-555"])] == [S])
check("... and an item under a named parent tag", [r["slug"] for r in db.search(tags=["Parent555"])] == [S])
check("... and a card's linked tag (no chip, still a tree tag)", I1 in [r["slug"] for r in db.search(tags=["Losi 8IGHT"])])
r = client.get("/api/search", params={"tags": "tree-only-555"})
check("GET /api/search?tags= too", r.status_code == 200 and [x["slug"] for x in r.json()] == [S], r.text[:200])
with actor.acting_as(actor.ACTOR_MCP):
    out = server.constructicon_search(tags=["tree-only-555"])
check("MCP constructicon_search(tags=) too", [x["slug"] for x in out] == [S], out)
check("a keyword search finds the tree tag's name", S in [r["slug"] for r in db.search(query="Child555")])

# ---- 4. chips never show a card the file is on ----------------------------------------------------
print("--- 4. card names are not chips ---")
C2 = mk("c2")
D = card("Desk Build 555")
res = membership.add_files(D["id"], [C2], **membership.UI_EFFECTS)
check("on the card: the linked tag is in the tree, not a chip", "Desk Build 555" in tree_names(C2) and chips(C2) == [])
check("add_files writes no capture_events row (no free-text stamp)",
      "capture_events" not in {m["table"] for r in q("SELECT mutations FROM audit_log WHERE batch_id = ?", res.batch_id)
                               for m in json.loads(r["mutations"])})
r = client.get(f"/api/image/{C2}/tags")
check("GET /api/image/{slug}/tags: no card tag", r.status_code == 200 and r.json() == [], r.text[:200])
page = client.get(f"/object/{C2}").text
tag_row = re.search(r'<dd id="item-tag-chips".*?</dd>', page, re.S).group(0)
check("the item page: no card chip in Tags, the card is a link in WHERE IT LIVES",
      "Desk Build 555" not in tag_row and f'href="/project/{D["slug"]}"' in page)
check("the chip remove refuses the card's own tag (take the file off the card instead)",
      code_of(tags.detach, C2, D["tag_id"]) == "tag_is_card_tag")

# ---- 5. a card rename renames its linked tag --------------------------------------------------------
print("--- 5. card rename ---")
res = cards.update(L["id"], title="Losi 8IGHT-E 2.1")
ops = q("SELECT mutations FROM audit_log WHERE batch_id = ?", res.batch_id)
check("the linked tag follows the title", db.get_tag(L["tag_id"])["name"] == "Losi 8IGHT-E 2.1")
check("in the same change-log row (projects + blog_tags)", len(ops) == 1 and
      {m["table"] for m in json.loads(ops[0]["mutations"])} == {"projects", "blog_tags"}, ops)
cards.undo(res.batch_id)
check("undo puts back both the title and the tag's (drifted) name",
      db.get_project(L["id"])["title"] == "Losi 8IGHT-E 2.0" and db.get_tag(L["tag_id"])["name"] == "Losi 8IGHT")
tags.create("Taken 555")
res = cards.update(L["id"], title="Taken 555")
check("a name another tag has: the card is renamed, the tag keeps its name, and a warning says why",
      db.get_project(L["id"])["title"] == "Taken 555" and db.get_tag(L["tag_id"])["name"] == "Losi 8IGHT"
      and res.warnings and "Taken 555" in res.warnings[0], res.warnings)
cards.undo(res.batch_id)
r = client.post(f"/api/projects/{D['id']}", data={"title": "Desk Build 556"}, headers=SAME)
check("the web card Save renames the tag too", r.status_code == 200 and db.get_tag(D["tag_id"])["name"] == "Desk Build 556",
      (r.status_code, r.text[:200], db.get_tag(D["tag_id"])["name"]))

# ---- 6. rename / merge / move / delete --------------------------------------------------------------
print("--- 6. tag management ---")
RC = tag_named("rc")
res = tags.rename(RC["id"], "RC cars")
check("rename: everywhere (chips follow)", "RC cars" in chips(I1) and db.get_tag(RC["id"])["name"] == "RC cars"
      and res.data["items"] == 1 and batch_ops(res.batch_id) == [("tag_rename", "owner-ui")])
cards.undo(res.batch_id)
check("rename undo", db.get_tag(RC["id"])["name"] == "rc" and "rc" in chips(I1))
check("rename to a name another tag has (any case): 409 tag_name_conflict", code_of(tags.rename, RC["id"], "KERBAL") == "tag_name_conflict")
check("rename a card's linked tag: tag_is_card_tag", code_of(tags.rename, D["tag_id"], "Something else") == "tag_is_card_tag")
check("rename a card's tag to the card's own title is allowed (mends drift)",
      tags.rename(L["tag_id"], "Losi 8IGHT-E 2.0").batch_id and db.get_tag(L["tag_id"])["name"] == "Losi 8IGHT-E 2.0")
check("rename: empty name refused", code_of(tags.rename, RC["id"], "  ") == "bad_tag")
H = hobbies.create("Gardening 555").data
HT = tag_named("Gardening 555")
res = tags.rename(HT["id"], "Gardens 555")
check("rename a hobby's tag goes through hobbies.rename (slug follows)", tag_named("Gardens 555") is not None
      and tag_named("Gardens 555")["slug"] == "gardens-555")
cards.undo(res.batch_id)

# merge
DUP = db._get_or_create_tag("kerbal-dup")
DUPKID = tags.create("Dup kid", parent_name="kerbal-dup").data["tag"]
M1, M2 = mk("m1"), mk("m2")
db._attach_tags(M1, [DUP["id"]])
db._attach_tags(M2, [DUP["id"], KERBAL["id"]])
res = tags.merge(DUP["id"], "Kerbal")
check("merge: items move to the kept tag, once each", res.data["items_moved"] == 1 and res.data["already_had"] == 1
      and tree_names(M1) == ["Kerbal"] and tree_names(M2) == ["Kerbal"])
check("merge: sub-tags move under it, the source is gone", db.get_tag(DUPKID["id"])["parent_id"] == KERBAL["id"]
      and db.get_tag(DUP["id"]) is None)
cards.undo(res.batch_id)
check("merge undo: the source, its links and its sub-tag are back", db.get_tag(DUP["id"]) is not None
      and tree_names(M1) == ["kerbal-dup"] and db.get_tag(DUPKID["id"])["parent_id"] == DUP["id"] and cache_ok())
check("merge into itself: refused", code_of(tags.merge, DUP["id"], DUP["id"]) == "bad_request")
check("merge into its own sub-tag: tag_cycle", code_of(tags.merge, DUP["id"], DUPKID["id"]) == "tag_cycle")
check("merge a card's tag away: tag_is_card_tag", code_of(tags.merge, D["tag_id"], KERBAL["id"]) == "tag_is_card_tag")
check("merge a hobby's tag away: tag_is_hobby", code_of(tags.merge, HT["id"], KERBAL["id"]) == "tag_is_hobby")
check("merge into a missing tag: not_found", code_of(tags.merge, DUP["id"], "No Such Tag 555") == "not_found")

# move
res = tags.move(DUPKID["id"], KERBAL["id"])
check("move: re-parented", db.get_tag(DUPKID["id"])["parent_id"] == KERBAL["id"])
cards.undo(res.batch_id)
check("move undo", db.get_tag(DUPKID["id"])["parent_id"] == DUP["id"])
check("move under its own sub-tag: tag_cycle", code_of(tags.move, DUP["id"], DUPKID["id"]) == "tag_cycle")
check("move under itself: tag_cycle", code_of(tags.move, DUP["id"], DUP["id"]) == "tag_cycle")
check("move a hobby's tag under another: tag_is_hobby", code_of(tags.move, HT["id"], KERBAL["id"]) == "tag_is_hobby")
res = tags.move(DUPKID["id"], None)
check("move to the top level", db.get_tag(DUPKID["id"])["parent_id"] is None)
cards.undo(res.batch_id)

# delete
check("delete a tag with sub-tags: tag_has_children", code_of(tags.delete, DUP["id"]) == "tag_has_children")
check("delete a card's tag: tag_is_card_tag", code_of(tags.delete, D["tag_id"]) == "tag_is_card_tag")
check("delete a hobby's tag: tag_is_hobby", code_of(tags.delete, HT["id"]) == "tag_is_hobby")
res = tags.delete(DUPKID["id"], dry_run=True)
check("delete dry run writes nothing", db.get_tag(DUPKID["id"]) is not None and batch_ops(res.batch_id) == [])
db._attach_tags(M1, [DUPKID["id"]])
res = tags.delete(DUPKID["id"])
check("delete: gone, and off its item", db.get_tag(DUPKID["id"]) is None and "Dup kid" not in tree_names(M1)
      and res.data["items"] == 1)
cards.undo(res.batch_id)
check("delete undo: back, on its item again", db.get_tag(DUPKID["id"]) is not None and "Dup kid" in tree_names(M1) and cache_ok())

# web routes (admin token) and the item chip
r = client.post(f"/api/tags/{RC['id']}/rename", data={"name": "RC 555"}, headers=SAME)
check("web rename: 200 + batch_id", r.status_code == 200 and r.json().get("batch_id") and r.json()["items"] == 1, r.text[:200])
client.post(f"/api/changes/{r.json()['batch_id']}/undo", headers=SAME)
r = client.post(f"/api/tags/{RC['id']}/rename", data={"name": "Kerbal"}, headers=SAME)
check("web rename refusal: 409 in the shared shape, saying what to do", r.status_code == 409
      and r.json()["error"]["code"] == "tag_name_conflict" and "merge" in r.json()["error"]["message"], r.text[:200])
r = client.post(f"/api/tags/{D['tag_id']}/delete", headers=SAME)
check("web delete of a card's tag: 409 tag_is_card_tag", r.status_code == 409 and r.json()["error"]["code"] == "tag_is_card_tag")
r = client.post(f"/api/tags/{DUP['id']}/merge", data={"into": "Kerbal"}, headers=SAME)
check("web merge: 200", r.status_code == 200 and r.json()["into"]["name"] == "Kerbal", r.text[:200])
client.post(f"/api/changes/{r.json()['batch_id']}/undo", headers=SAME)
r = client.post(f"/api/tags/{DUPKID['id']}/move", data={"parent": ""}, headers=SAME)
check("web move to the top: 200", r.status_code == 200 and db.get_tag(DUPKID["id"])["parent_id"] is None, r.text[:200])
client.post(f"/api/changes/{r.json()['batch_id']}/undo", headers=SAME)
r = client.post(f"/api/image/{I1}/tags/{RC['id']}/remove", headers=SAME)
check("the item chip's x: off this item only, answers the chips left", r.status_code == 200 and "rc" not in tree_names(I1)
      and r.json()["batch_id"] and r.json()["chips"] == [], r.text[:200])
client.post(f"/api/changes/{r.json()['batch_id']}/undo", headers=SAME)
check("... undone", "rc" in tree_names(I1))
r = client.post(f"/api/image/nope/tags/{RC['id']}/remove", headers=SAME)
check("the chip's x on a missing item: 404", r.status_code == 404)
page = client.get(f"/object/{I1}").text
check("the item page: chips carry rename + remove buttons for an editor",
      'data-act="rename"' in page and 'data-act="remove"' in page and "tag-chips.js" in page
      and 'data-can-edit="1"' in page)

# ---- 7. the cleanup sweep ------------------------------------------------------------------------
print("--- 7. sweep ---")
EP = tags.create("Empty kid 555", parent_name="Empty parent 555").data["tag"]
EPP = tag_named("Empty parent 555")
entries = {e["id"]: e for e in tags.sweep()}
check("'Ancient projects.' is listed: orphan + near-duplicate of 'Ancient Projects', deletable",
      DOT["id"] in entries and entries[DOT["id"]]["kinds"] == ["orphan", "near_duplicate"] and entries[DOT["id"]]["deletable"]
      and entries[DOT["id"]]["duplicates"][0]["name"] == "Ancient Projects", entries.get(DOT["id"]))
check("'Ancient Projects' (on an item) is listed as a near-duplicate, NOT deletable (merge instead)",
      ANCIENT["id"] in entries and not entries[ANCIENT["id"]]["deletable"] and "merge" in entries[ANCIENT["id"]]["why_not"])
check("an empty branch: the parent is 'empty', its leaf 'orphan'",
      entries[EPP["id"]]["kinds"] == ["empty"] and entries[EP["id"]]["kinds"] == ["orphan"])
check("a card's tag and a hobby's tag are never offered for deletion",
      all(not e["deletable"] for e in entries.values() if e["cards"] or e["hobby"]))
check("the sweep wrote nothing", q("SELECT COUNT(*) AS n FROM audit_log WHERE op = 'tag_sweep_delete'")[0]["n"] == 0)
n_tags = q("SELECT COUNT(*) AS n FROM blog_tags")[0]["n"]
check("apply the parent without its sub-tag: refused, nothing deleted",
      code_of(tags.sweep_apply, [EPP["id"]]) == "sweep_refused" and q("SELECT COUNT(*) AS n FROM blog_tags")[0]["n"] == n_tags)
check("apply a non-deletable pick: refused (whole call)", code_of(tags.sweep_apply, [DOT["id"], ANCIENT["id"]]) == "sweep_refused"
      and tag_named("Ancient projects.") is not None)
check("apply a tag that isn't on the list: refused", code_of(tags.sweep_apply, [KERBAL["id"]]) == "sweep_refused")
res = tags.sweep_apply([DOT["id"]])
check("apply exactly 'Ancient projects.': only it is deleted", tag_named("Ancient projects.") is None
      and q("SELECT COUNT(*) AS n FROM blog_tags")[0]["n"] == n_tags - 1 and tag_named("Ancient Projects") is not None
      and batch_ops(res.batch_id) == [("tag_sweep_delete", "owner-ui")])
cards.undo(res.batch_id)
check("sweep apply undo brings it back", tag_named("Ancient projects.") is not None)
r = client.get("/api/tags/sweep")
check("web GET /api/tags/sweep lists it", r.status_code == 200 and DOT["id"] in [e["id"] for e in r.json()["entries"]])
r = client.post("/api/tags/sweep/apply", data={"tag_ids": [str(EPP["id"]), str(EP["id"])]}, headers=SAME)
check("web apply of a whole empty branch: both deleted, one batch", r.status_code == 200 and tag_named("Empty parent 555") is None
      and tag_named("Empty kid 555") is None and len(r.json()["deleted"]) == 2, r.text[:300])
client.post(f"/api/changes/{r.json()['batch_id']}/undo", headers=SAME)
check("... and undone (both back, the kid under the parent)", tag_named("Empty kid 555")["parent_id"] == EPP["id"])
page = client.get("/admin").text
check("Admin > Tags panel is on the admin page", 'id="admin-tags-section"' in page and "admin-tags.js" in page)
r = client.get("/api/tags/tree")
flat = []


def walk(nodes):
    for n in nodes:
        flat.append(n)
        walk(n["children"])


walk(r.json())
check("GET /api/tags/tree: the tree with counts, cards and hobbies", r.status_code == 200
      and any(n["name"] == "Kerbal" and n["items"] >= 3 for n in flat)
      and any(n["cards"] and n["cards"][0]["id"] == D["id"] for n in flat) and any(n["hobby"] for n in flat))

# ---- 8. MCP constructicon_tags --------------------------------------------------------------------
print("--- 8. MCP ---")
with actor.acting_as(actor.ACTOR_MCP):
    t = server.constructicon_tags()
    g = server.constructicon_tags(action="get", tag="kerbal")
    rn = server.constructicon_tags(action="rename", tag="rc", name="RC mcp")
    bad = server.constructicon_tags(action="rename", tag="rc mcp", name="Kerbal")
    extra = server.constructicon_tags(action="rename", tag="RC mcp", name="x", tag_ids=[1])
    und = server.constructicon_undo(rn["batch_id"])
    mg = server.constructicon_tags(action="merge", tag="kerbal-dup", into="Kerbal", dry_run=True)
    mv = server.constructicon_tags(action="move", tag="Dup kid")
    dl = server.constructicon_tags(action="delete", tag=D["tag_id"])
    sw = server.constructicon_tags(action="sweep")
    sa = server.constructicon_tags(action="sweep_apply", tag_ids=[DOT["id"]])
    amb = server.constructicon_tags(action="get", tag="nothing-called-this")
check("MCP tree", any(n["name"] == "Kerbal" for n in t["tags"]))
check("MCP get by name (any case): path and usage", g["tags"][0]["name"] == "Kerbal" and g["tags"][0]["items"] >= 3)
check("MCP rename: batch_id, undone by constructicon_undo", rn.get("batch_id") and und.get("ok") is not False
      and tag_named("rc") is not None)
check("MCP rename refusal: {ok: false, error: tag_name_conflict}", bad.get("ok") is False and bad["error"]["code"] == "tag_name_conflict", bad)
check("MCP: a parameter that doesn't apply is refused", extra.get("ok") is False and extra["error"]["code"] == "bad_request", extra)
check("MCP merge dry run: answers, writes nothing", mg.get("dry_run") and mg["items_moved"] == 1 and tag_named("kerbal-dup") is not None)
check("MCP move to the top level", mv.get("batch_id") and tag_named("Dup kid")["parent_id"] is None)
check("MCP delete of a card's tag: refused, says why", dl.get("ok") is False and dl["error"]["code"] == "tag_is_card_tag"
      and "card" in dl["error"]["message"])
check("MCP sweep lists, sweep_apply deletes the pick", DOT["id"] in [e["id"] for e in sw["entries"]]
      and sa.get("batch_id") and tag_named("Ancient projects.") is None)
check("MCP get of a missing tag: not_found", amb.get("ok") is False and amb["error"]["code"] == "not_found")
with actor.acting_as(actor.ACTOR_MCP):
    server.constructicon_undo(sa["batch_id"])
    server.constructicon_undo(mv["batch_id"])
check("MCP undo of sweep_apply + move", tag_named("Ancient projects.") is not None and tag_named("Dup kid")["parent_id"] == DUP["id"])

# ---- 9. a viewer ------------------------------------------------------------------------------------
print("--- 9. viewer ---")
SECRET = card("Secret Card 555")
with actor.acting_as(actor.ACTOR_SCRIPT):
    sharing.set_private("card", SECRET["id"], True)
OPEN = mk("open-item")
db._attach_tags(OPEN, [SECRET["tag_id"], KERBAL["id"]])  # an open item that wears the private card's tag
pw = "tv-" + secrets.token_urlsafe(16)
with actor.acting_as(actor.ACTOR_SCRIPT):
    VIEWER = users.create_user("tags_viewer", pw, "viewer")
vc = TestClient(webapp.app, base_url=BASE, follow_redirects=False)
check("viewer signs in", vc.post("/api/auth/login", json={"username": "tags_viewer", "password": pw}, headers=SAME).status_code == 200)
vt = vc.get("/api/tags/tree").json()
flat = []
walk(vt)
check("viewer: the private card's tag is not in the tree", all(n["name"] != "Secret Card 555" for n in flat)
      and any(n["name"] == "Kerbal" for n in flat))
vpage = vc.get(f"/object/{OPEN}").text
vrow = re.search(r'<dd id="item-tag-chips".*?</dd>', vpage, re.S).group(0)
check("viewer: no chip names the private card; Kerbal shows", "Secret Card 555" not in vrow and "Kerbal" in vrow)
check("viewer: chip buttons shown greyed, saying the role", 'data-can-edit="0"' in vrow and "is-locked" in vrow
      and 'aria-disabled="true"' in vrow and "editor role" in vrow)
check("viewer: GET /api/image/{slug}/tags leaves it out", [c["name"] for c in vc.get(f"/api/image/{OPEN}/tags").json()] == ["Kerbal"])
csrf = re.search(r'<meta name="csrf-token" content="([^"]+)"', vpage).group(1)
r = vc.post(f"/api/tags/{KERBAL['id']}/rename", data={"name": "Nope"}, headers={**SAME, "X-CSRF-Token": csrf})
check("viewer: rename refused 403", r.status_code == 403 and r.json()["error"]["code"] == "forbidden", r.text[:200])
r = vc.get("/api/tags/sweep")
check("viewer: the sweep needs the editor role (403)", r.status_code == 403)
check("admin: the private card's tag IS in the tree", any(n["name"] == "Secret Card 555" for n in
                                                         [x for x in tags.tree()] + [c for x in tags.tree() for c in x["children"]]))
from mcp_server import auth as mcp_auth  # noqa: E402
tok = mcp_auth.IDENTITY.set({"actor": "user:tags_viewer", "user_id": VIEWER["id"] if isinstance(VIEWER, dict) else None})
try:
    vt_mcp = server.constructicon_tags()
    vr_mcp = server.constructicon_tags(action="rename", tag="Kerbal", name="Nope")
    vs_mcp = server.constructicon_tags(action="get", tag="Secret Card 555")
finally:
    mcp_auth.IDENTITY.reset(tok)
flat = []
walk(vt_mcp["tags"])
check("viewer over MCP: tree without the private card's tag", all(n["name"] != "Secret Card 555" for n in flat))
check("viewer over MCP: rename refused (forbidden)", vr_mcp.get("ok") is False and vr_mcp["error"]["code"] == "forbidden", vr_mcp)
check("viewer over MCP: the hidden tag reads as not_found", vs_mcp.get("ok") is False and vs_mcp["error"]["code"] == "not_found", vs_mcp)
check("the chip cache still equals the tree at the end", cache_ok(), db.check_chip_cache())

ctx.__exit__(None, None, None)
print()
print("FAILED: " + ", ".join(FAILS) if FAILS else "ALL PASS")
sys.exit(1 if FAILS else 0)
