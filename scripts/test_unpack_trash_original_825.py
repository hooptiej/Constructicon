#!/usr/bin/env python3
"""Unpack and Tidy Up (#825): the opt-in "trash the original archive after a successful unpack".
Throwaway DB and storage (_testenv), synthetic archives, no server, no network.

Checks: an install whose unpack_jobs predates the column gets it at boot (default 0); off by default
(the archive stays on the card); on + the job ends done: the archive goes to the trash under the
job's own batch (trash row, file moved, off every card), members keep `extracted_from`, and ONE undo
brings back the archive (row and file) while removing the card and the members; on + partial: the
archive is kept and the message says why; on + failed: kept; a refused trash step leaves the unpack
done, keeps the archive and says why; a bad flag value is a clear 400; the web form (`trash_original=1`)
and the MCP param (`trash_original: true`) both reach the job; the dialog has the checkbox, unticked.

    python scripts/test_unpack_trash_original_825.py
"""

import io
import os
import re
import secrets
import sqlite3
import sys
import zipfile

import _testenv  # noqa: E402
TMP = _testenv.isolate("unpack-trash-825-")
os.environ["CAPTION_DISABLED"] = "1"
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
sys.path.insert(0, HERE)

try:
    import cairosvg  # noqa: F401
except Exception:  # no libcairo here (e.g. Windows): object types import it
    import types
    sys.modules["cairosvg"] = types.ModuleType("cairosvg")

from core import actor, cards, db, ingest, items, storage, unpack, users  # noqa: E402
from core.errors import AppError  # noqa: E402
_testenv.assert_isolated()

ingest.run_in_thread = lambda fn, *a: None
NOOP = lambda fn, *a: None  # noqa: E731
FAILS = []


def check(name, cond, extra=""):
    print(("PASS " if cond else "FAIL ") + name + (f"  [{str(extra)[:300]}]" if (extra and not cond) else ""))
    if not cond:
        FAILS.append(name)


def q(sql, *args):
    c = sqlite3.connect(os.environ["CONSTRUCTICON_DB_PATH"])
    c.row_factory = sqlite3.Row
    try:
        return [dict(r) for r in c.execute(sql, args)]
    finally:
        c.close()


def zip_bytes(entries):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        for name, data in entries:
            z.writestr(name, data)
    return buf.getvalue()


def upload(name, data):
    res = ingest.ingest_file(io.BytesIO(data), name, size=len(data), source="tester", run_background=NOOP)
    assert res.row is not None, res.error
    return res.row["slug"]


def refusal(fn, *a, **kw):
    try:
        fn(*a, **kw)
    except AppError as e:
        return e
    return None


def convert(slug, **kw):
    return unpack.run_job(unpack.start(slug, **kw)["id"], run_background=NOOP)


def on_card(card_id):
    return {r["post_slug"] for r in q("SELECT post_slug FROM project_items WHERE project_id = ?", card_id)}


db.init_db()
users.limiter.reset()

print("--- the column ---")
q("SELECT 1")  # warm
c = sqlite3.connect(os.environ["CONSTRUCTICON_DB_PATH"])
c.execute("ALTER TABLE unpack_jobs DROP COLUMN trash_original")
c.commit()
c.close()
check("fixture: an unpack_jobs table from before #825", "trash_original" not in {r["name"] for r in q("PRAGMA table_info(unpack_jobs)")})
db.init_db()
cols = {r["name"]: r for r in q("PRAGMA table_info(unpack_jobs)")}
check("boot adds trash_original to an existing unpack_jobs (NOT NULL, default 0)",
      "trash_original" in cols and cols["trash_original"]["notnull"] == 1 and cols["trash_original"]["dflt_value"] == "0", cols.get("trash_original"))

print("--- off by default ---")
a0 = upload("keep-me.zip", zip_bytes([("k/a.txt", b"keep a"), ("k/b.txt", b"keep b")]))
j0 = convert(a0)
check("a job started without the flag records it off", j0["status"] == "done" and j0["trash_original"] is False, j0)
check("... and the archive stays, on the new card", db.get_by_slug(a0) is not None and a0 in on_card(j0["result"]["card"]["id"]))
check("... with no trash_original note in the result", "trash_original" not in j0["result"])
cards.undo(j0["batch_id"])

print("--- on, and the job ends done ---")
a1 = upload("tidy-me.zip", zip_bytes([("t/one.txt", b"tidy one"), ("t/two.txt", b"tidy two")]))
arch_file = storage.path_for(db.get_by_slug(a1)["stored_filename"])
j1 = convert(a1, trash_original=True)
r1 = j1["result"]
check("the job ends done with trash_original on", j1["status"] == "done" and j1["trash_original"] is True, j1.get("error"))
check("the archive is gone from the library", db.get_by_slug(a1) is None)
check("... into the trash under the job's own batch (one undo covers it)",
      q("SELECT 1 FROM trash WHERE batch_id = ?", j1["batch_id"]) and not arch_file.exists(),
      q("SELECT batch_id FROM trash"))
check("... and off the new card", a1 not in on_card(r1["card"]["id"]))
check("the result says so (trashed, how long, Undo brings it back)",
      r1.get("trash_original", {}).get("trashed") is True and "went to the trash" in j1["message"]
      and "Undo" in j1["message"], j1["message"])
made = [x["slug"] for x in r1["created"]]
check("members keep extracted_from pointing at the archive's slug",
      made and all((db.get_by_slug(s)["type_metadata"] or {}).get("extracted_from", {}).get("slug") == a1 for s in made))
u = cards.undo(j1["batch_id"])
check("ONE undo brings the archive back (row and file)", db.get_by_slug(a1) is not None and arch_file.exists(), u.data if u else None)
check("... and removes the card and the members it made",
      not q("SELECT 1 FROM projects WHERE id = ?", r1["card"]["id"]) and all(db.get_by_slug(s) is None for s in made))

print("--- on, but the job ends partial ---")
a2 = upload("partly.zip", zip_bytes([("p/good.txt", b"good"), ("p/bad.txt", b"bad")]))
real_ingest = ingest.ingest_file


def flaky(f, filename, **kw):
    if filename == "bad.txt":
        raise OSError("disk hiccup while storing bad.txt")
    return real_ingest(f, filename, **kw)


ingest.ingest_file = flaky
try:
    j2 = convert(a2, trash_original=True)
finally:
    ingest.ingest_file = real_ingest
check("fixture: partial", j2["status"] == "partial", j2["status"])
check("a partial job keeps the archive", db.get_by_slug(a2) is not None and not q("SELECT 1 FROM trash WHERE batch_id = ?", j2["batch_id"]))
check("... and says why it was kept", j2["result"].get("trash_original", {}).get("trashed") is False
      and "kept" in j2["message"] and "didn't finish cleanly" in j2["message"], j2["message"])
cards.undo(j2["batch_id"])

print("--- on, but the job fails ---")
a3 = upload("junk.zip", zip_bytes([(".DS_Store", b"x"), ("__MACOSX/._a", b"y")]))
j3 = convert(a3, trash_original=True)
check("a failed job keeps the archive", j3["status"] == "failed" and db.get_by_slug(a3) is not None, j3["status"])

print("--- the trash step is refused ---")
a4 = upload("stuck.zip", zip_bytes([("s/a.txt", b"stuck a")]))
real_delete = items.delete


def refusing_delete(slugs, **kw):
    raise AppError("forbidden", "Nothing was changed: this item is marked sensitive, and only its uploader or an "
                   "admin can change it.", status=403)


items.delete = refusing_delete
try:
    j4 = convert(a4, trash_original=True)
finally:
    items.delete = real_delete
check("a refused trash step leaves the unpack done", j4["status"] == "done", j4.get("error"))
check("... keeps the archive", db.get_by_slug(a4) is not None)
check("... and says it was NOT trashed and why",
      j4["result"]["trash_original"]["trashed"] is False and "NOT sent to the trash" in j4["message"]
      and "marked sensitive" in j4["message"], j4["message"])
cards.undo(j4["batch_id"])

print("--- the flag's values ---")
for v, want in ((True, True), ("1", True), ("on", True), ("yes", True), ("TRUE", True),
                (False, False), (None, False), ("", False), ("0", False), ("off", False)):
    check(f"flag({v!r}) = {want}", unpack.flag(v) is want)
e = refusal(unpack.start, a4, trash_original="maybe")
check("a value that is neither yes nor no is refused (400)",
      e is not None and e.status == 400 and "trash_original" in e.message, e and e.message)
check("... and no job was queued for it", not db.list_unpack_jobs(status="queued", archive_slug=a4))

print("--- web and MCP ---")
from starlette.testclient import TestClient  # noqa: E402
from web import app as webapp  # noqa: E402
BASE = "http://testhost.local"
SAME = {"Origin": BASE}
pw = "tr-" + secrets.token_urlsafe(16)
vpw = "tr-" + secrets.token_urlsafe(16)
with actor.acting_as(actor.ACTOR_SCRIPT):
    users.create_user("tr_editor", pw, "editor")
    users.create_user("tr_viewer", vpw, "viewer")
cl = TestClient(webapp.app, base_url=BASE, follow_redirects=False)
cl.post("/api/auth/login", json={"username": "tr_editor", "password": pw}, headers=SAME)
m = re.search(r'<meta name="csrf-token" content="([^"]+)"', cl.get("/").text)
H = {**SAME, "X-CSRF-Token": m.group(1) if m else ""}
aw = upload("web-tidy.zip", zip_bytes([("w/a.txt", b"web a")]))
rs = cl.post(f"/api/image/{aw}/action/unpack", data={"mode": "start", "trash_original": "1"}, headers=H)
check("the web form's trash_original=1 reaches the job", rs.status_code == 200 and rs.json()["job"]["trash_original"] is True
      and "original archive goes to the trash" in rs.json()["message"], rs.text[:300])
jw = unpack.run_job(rs.json()["job"]["id"], run_background=NOOP)
check("... and the editor's job trashes the archive when done", jw["status"] == "done" and db.get_by_slug(aw) is None, jw["message"])
pj = cl.get(f"/api/unpack/jobs/{jw['id']}")
check("the starter's poll still reads the finished job once the archive is in the trash",
      pj.status_code == 200 and pj.json()["job"]["result"]["trash_original"]["trashed"] is True, pj.text[:200])
vc = TestClient(webapp.app, base_url=BASE, follow_redirects=False)
vc.post("/api/auth/login", json={"username": "tr_viewer", "password": vpw}, headers=SAME)
check("... but nobody else can (404, nothing about a deleted item leaks)", vc.get(f"/api/unpack/jobs/{jw['id']}").status_code == 404)
rb = cl.post(f"/api/image/{aw}/action/unpack", data={"mode": "start"}, headers=H)
check("(the archive is in the trash now, so the action answers 404)", rb.status_code == 404, rb.status_code)
ru = cl.post(f"/api/changes/{jw['batch_id']}/undo", headers=H)
check("the editor's one undo over the web brings the archive back", ru.status_code == 200 and db.get_by_slug(aw) is not None, ru.text[:200])
rbad = cl.post(f"/api/image/{aw}/action/unpack", data={"mode": "start", "trash_original": "maybe"}, headers=H)
check("a bad value over the web is a 400 in the shared shape", rbad.status_code == 400
      and "trash_original" in rbad.json()["error"]["message"], rbad.text[:200])
page = cl.get(f"/object/{aw}").text
js = open(os.path.join(os.path.dirname(HERE), "web", "static", "js", "unpack-crate.js"), encoding="utf-8").read()
check("the dialog has the checkbox, unticked by default, and sends trash_original",
      "unpack-trash-original" in js and "trash_original: tidy.checked" in js and "checked: ''" not in js
      and "/static/js/unpack-crate.js" in page)

from mcp_server import server as mcp  # noqa: E402
am = upload("mcp-tidy.zip", zip_bytes([("m/a.txt", b"mcp a")]))
r = mcp.constructicon_run_type_action(slug=am, action="unpack", params={"mode": "start", "trash_original": True})
check("MCP start takes trash_original: true", r.get("ok") and r["job"]["trash_original"] is True, str(r)[:300])
jm = unpack.run_job(r["job"]["id"], run_background=NOOP)
check("... and the archive goes to the trash when done", jm["status"] == "done" and db.get_by_slug(am) is None, jm["message"])
r = mcp.constructicon_undo(audit_id=jm["batch_id"])
check("MCP undo of the batch brings it back", r.get("ok") is not False and db.get_by_slug(am) is not None, str(r)[:300])

print()
if FAILS:
    print(f"FAILED: {', '.join(FAILS)}")
    sys.exit(1)
print("ALL PASS")
