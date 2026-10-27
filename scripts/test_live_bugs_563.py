#!/usr/bin/env python3
"""Self-contained check for the live-bug bundle (#563).

Throwaway SQLite DB (CONSTRUCTICON_DB_PATH is set before core is imported), real `db.init_db()`,
the real FastAPI app through TestClient and direct calls into the MCP tool functions. No server,
no network, no background work (the background runner is stubbed):

    python scripts/test_live_bugs_563.py

Exits 1 if any check fails.
"""
import asyncio
import os
import re
import sys
import tempfile
import types

import _testenv  # noqa: E402  (scripts/_testenv.py: temp DB + storage + exports, refuses otherwise)
TMP = _testenv.isolate("livebugs563-")
ROOT = os.path.abspath(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
sys.path.insert(0, ROOT)
sys.modules.setdefault("cairosvg", types.ModuleType("cairosvg"))  # no libcairo needed here

from core import captions, db, decisions, ingest  # noqa: E402
_testenv.assert_isolated()  # now as core actually resolved the paths

FAILS = []


def check(name, cond, extra=""):
    print(("PASS " if cond else "FAIL ") + name + (f"  [{extra}]" if (extra and not cond) else ""))
    if not cond:
        FAILS.append(name)


def make_item(slug, uploader="tester", **kw):
    db.insert_content(slug, uploader, "youtube", external_url="https://example.com/" + slug,
                      content_description=kw.pop("title", slug), **kw)
    return slug


db.init_db()

from fastapi.testclient import TestClient  # noqa: E402
from mcp_server import server  # noqa: E402
from web import app as webapp  # noqa: E402

client = _testenv.client(webapp.app)

# ---- 1. remove-from-project x (db.get_post never existed) ----------------------------------
check("no db.get_post call left in app.py", "db.get_post(" not in open(os.path.join(ROOT, "web", "app.py"), encoding="utf-8").read())
proj = db._create_project("Remove test")
make_item("rm1")
db._add_item_to_project(proj["id"], "rm1")
r = client.post(f"/api/projects/{proj['slug']}/remove-item", data={"slug": "rm1"})
check("remove-item 200", r.status_code == 200, f"{r.status_code} {r.text[:200]}")
check("item detached", all(i["slug"] != "rm1" for i in db.list_project_items(proj["id"])))
r = client.post(f"/api/projects/{proj['slug']}/remove-item", data={"slug": "nope"})
check("remove-item unknown slug 404", r.status_code == 404, r.status_code)

# ---- 2. constructicon_run_type_action is a registered MCP tool -----------------------------
names = {t.name for t in asyncio.run(server.mcp.list_tools())}
check("run_type_action registered", "constructicon_run_type_action" in names)
check("update/list_items/item_tags registered (#746: get_posts_for_tag / detach_tag folded into them)",
      {"constructicon_update", "constructicon_list_items", "constructicon_item_tags"} <= names)

# ---- 3. constructicon_update(type_metadata) merges + cleans --------------------------------
make_item("md1", type_metadata={"auto_caption": "a cat", "view_count": 7, "medium": "oil"})
out = server.constructicon_update("md1", type_metadata={"rotation": 90, "medium": "  acrylic  "})
tm = db.get_by_slug("md1")["type_metadata"]
check("existing keys survive", tm.get("auto_caption") == "a cat" and tm.get("view_count") == 7, tm)
check("new key added", tm.get("rotation") == 90, tm)
check("physical-piece key cleaned", tm.get("medium") == "acrylic", tm)
# #548: MCP refusals come back as the shared error shape, not a raised ValueError.
bad = server.constructicon_update("md1", type_metadata={"date_made": "june 2009"})
check("bad date_made refused", isinstance(bad, dict) and bad.get("ok") is False
      and bad["error"]["code"] == "bad_physical_piece", bad)
check("refused update changed nothing", "date_made" not in db.get_by_slug("md1")["type_metadata"])

# ---- 4. item cards: no "No project yet" badge ----------------------------------------------
cards_js = open(os.path.join(ROOT, "web", "static", "js", "cards.js"), encoding="utf-8").read()
check("cards.js has no 'No project yet' badge", "No project yet" not in cards_js and "client-badge" not in cards_js)
check("cards.js keeps the unfiled lamp", "Not filed into a project yet" in cards_js)

# ---- 5. user gallery is not capped at 1000 -------------------------------------------------
for i in range(1030):
    make_item(f"g{i}", uploader="bulk")
page = client.get("/gallery/user/bulk")
check("gallery renders", page.status_code == 200, page.status_code)
m = re.search(r"(\d+) uploads?<", page.text)
check("gallery count is the true total", bool(m) and m.group(1) == "1030", m.group(0) if m else "no count")
# #553: the gallery is the shared file grid, server-paged: the page embeds the first batch, and
# GET /api/items/grid?source=user:<label> serves every row (it used to embed the whole list).
seen, cursor = [], ""
for _ in range(20):
    body = client.get("/api/items/grid", params={"source": "user:bulk", "limit": "500", "cursor": cursor}).json()
    seen += [it["slug"] for it in body["items"]]
    cursor = body["next_cursor"]
    if not cursor:
        break
check("gallery lists every item, paged through the grid endpoint", len(seen) == len(set(seen)) == 1030 and body["total"] == 1030,
      (len(seen), body.get("total")))
check("gallery template uses the shared grid (ItemCards pager inside it)", '{% include "_item_grid.html" %}' in open(
    os.path.join(ROOT, "web", "templates", "user_gallery.html"), encoding="utf-8").read() and "IC.pager(" in open(
    os.path.join(ROOT, "web", "static", "js", "item-grid.js"), encoding="utf-8").read())

# ---- 6. retype runs the real background steps (stubbed runner records them) ----------------
calls = []
real_runner = ingest.run_in_thread
ingest.run_in_thread = lambda fn, *a: calls.append((fn, a))
try:
    make_item("rt1", title="retype me")
    did = db.add_pending_decision("retype", "rt1", {"options": [{"key": "image", "label": "Image"}, {"key": "youtube", "label": "Video link"}]})
    decisions.resolve(did, choice="image", actor="test")
    check("retype changed the type", db.get_by_slug("rt1")["media_type"] == "image", db.get_by_slug("rt1")["media_type"])
    fns = [fn for fn, _ in calls]
    # #592: captions are persisted into caption_queue by post_insert itself (no background callable)
    check("retype queues the caption (persisted, #592)", (db.peek_caption_queue() or {}).get("slug") == "rt1", str(db.peek_caption_queue()))
finally:
    ingest.run_in_thread = real_runner
src = open(os.path.join(ROOT, "core", "object_types", "_pe.py"), encoding="utf-8").read()
check("Reclassify no longer passes a no-op runner", "lambda f, *args: None" not in src and "ingest.run_in_thread" in src)
check("run_in_thread runs the function on a thread", (lambda ev: (ingest.run_in_thread(ev.set), ev.wait(5))[1])(__import__("threading").Event()))

# ---- 7. POST /api/projects/{id}: validate before the first write ---------------------------
parent = db._create_project("Parent card")
child = db._create_project("Child card")
before = db.get_project(child["id"])
r = client.post(f"/api/projects/{child['slug']}", data={"parent_id": str(parent["id"]), "writeup_slug": "does-not-exist"})
check("bad writeup_slug is 400", r.status_code == 400, f"{r.status_code} {r.text[:200]}")
after = db.get_project(child["id"])
check("...and the card was NOT nested", after["parent_id"] == before["parent_id"], after["parent_id"])
r = client.post(f"/api/projects/{child['slug']}", data={"parent_id": str(parent["id"]), "start_date": "not-a-date"})
check("bad start_date is 400 and nothing nested", r.status_code == 400 and db.get_project(child["id"])["parent_id"] == before["parent_id"],
      f"{r.status_code}")
r = client.post(f"/api/projects/{child['slug']}", data={"parent_id": str(parent["id"]), "stage": "bogus-stage"})
check("a core refusal part-way (bad stage) rolls the nest back",
      r.status_code in (409, 422) and db.get_project(child["id"])["parent_id"] == before["parent_id"],
      f"{r.status_code} {r.text[:200]}")
r = client.post(f"/api/projects/{child['slug']}", data={"parent_id": str(parent["id"]), "title": "Child renamed"})
check("a valid update still works", r.status_code == 200 and db.get_project(child["id"])["parent_id"] == parent["id"]
      and db.get_project(child["id"])["title"] == "Child renamed", f"{r.status_code} {r.text[:200]}")

# ---- 8. MCP tag lookups do not create tags -------------------------------------------------
def tag_count():
    c = db.get_conn()
    try:
        return c.execute("SELECT COUNT(*) FROM blog_tags").fetchone()[0]
    finally:
        c.close()


n = tag_count()
r = server.constructicon_list_items(which="tag", tag="no-such-tag-xyz")
check("list_items(which=tag) unknown tag errors", isinstance(r, dict) and r["error"]["code"] == "not_found"
      and "no-such-tag-xyz" in r["error"]["message"], r)
make_item("tg1")
r = server.constructicon_item_tags("tg1", ["no-such-tag-xyz"], action="detach")
check("item_tags(detach) unknown tag errors", isinstance(r, dict) and r.get("ok") is False and r["error"]["code"] == "not_found", r)
check("neither created a tag", tag_count() == n, f"{n} -> {tag_count()}")
server.constructicon_item_tags("tg1", ["real-tag"])
check("attach still creates", tag_count() == n + 1)
check("lookup finds the existing tag", isinstance(server.constructicon_list_items(which="tag", tag="real-tag"), list))
r = server.constructicon_item_tags("tg1", ["real-tag"], action="detach")
check("detach works on an existing tag", isinstance(r, dict) and r.get("ok") is not False and r.get("batch_id"), r)

# ---- 9. project delete confirm wording matches real undo -----------------------------------
tpl_path = os.path.join(ROOT, "web", "templates", "project_detail.html")
tpl = open(tpl_path, encoding="utf-8").read()
check("delete confirm no longer says 'can't be undone'", "This can't be undone" not in tpl.split("Delete project (#367)")[1].split("Orphan child projects")[0])
# #554 (batch 5): the delete now offers Undo in the bar right after, so the wording says that.
check("delete confirm mentions the undo", "You can undo it right after" in tpl and "showUndoBar(summary, body.batch_id" in tpl)
from jinja2 import Environment  # noqa: E402
Environment(extensions=[]).parse(tpl)  # syntax only
check("project_detail.html parses", True)
d = client.post(f"/api/projects/{proj['slug']}/delete")
check("delete returns a batch_id to undo with", d.status_code == 200 and bool(d.json().get("batch_id")), d.text[:200])

# ---- 10. the boot-time 'failure'->'documented' rewrite is gone -----------------------------
make_item("pv1")
c = db.get_conn()
c.execute("UPDATE capture_events SET provenance='failure' WHERE slug='pv1'")
c.commit()
c.close()
db.init_db()
check("init_db leaves an owner's 'failure' provenance alone", db.get_by_slug("pv1")["provenance"] == "failure", db.get_by_slug("pv1")["provenance"])

print()
print("FAILED: " + ", ".join(FAILS) if FAILS else "all checks passed")
sys.exit(1 if FAILS else 0)
