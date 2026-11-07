#!/usr/bin/env python3
"""Where's My Crate At (#826): archive unpack jobs show in the processing drawer. Throwaway DB and
storage (_testenv), synthetic archives, no server, no network.

Checks: `unpack.drawer_jobs()` lists a queued job (waiting), a running one (phase, done / total, the
current member), a finished one with its card link and counts, a partial one naming each failed member
with its reason, and a failed one with its reason; a job that finished more than an hour ago drops out;
running jobs come first; a job on a sensitive archive is hidden from a viewer (but shown to its admin);
a deleted archive's job shows only to admins and its starter; `GET /api/processing` carries `unpack_jobs` and counts queued /
running jobs as in flight; the drawer template renders the jobs box.

    python scripts/test_unpack_drawer_826.py
"""

import io
import os
import re
import secrets
import sys
import time
import zipfile

import _testenv  # noqa: E402
TMP = _testenv.isolate("unpack-drawer-826-")
os.environ["CAPTION_DISABLED"] = "1"
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
sys.path.insert(0, HERE)

try:
    import cairosvg  # noqa: F401
except Exception:  # no libcairo here (e.g. Windows): object types import it
    import types
    sys.modules["cairosvg"] = types.ModuleType("cairosvg")

from core import actor, db, ingest, items, unpack, users  # noqa: E402
_testenv.assert_isolated()

ingest.run_in_thread = lambda fn, *a: None
NOOP = lambda fn, *a: None  # noqa: E731
FAILS = []


def check(name, cond, extra=""):
    print(("PASS " if cond else "FAIL ") + name + (f"  [{str(extra)[:300]}]" if (extra and not cond) else ""))
    if not cond:
        FAILS.append(name)


def zip_bytes(entries):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        for name, data in entries:
            z.writestr(name, data)
    return buf.getvalue()


def upload(name, data, sensitive=False):
    res = ingest.ingest_file(io.BytesIO(data), name, size=len(data), source="tester", run_background=NOOP,
                             sensitive=sensitive)
    assert res.row is not None, res.error
    return res.row["slug"]


def by_id(jobs, job_id):
    return next((j for j in jobs if j["id"] == job_id), None)


db.init_db()
users.limiter.reset()

# Users first: a viewer, and the admin who uploads the sensitive archive.
PW = {}
with actor.acting_as(actor.ACTOR_SCRIPT):
    for name, role in (("dr_viewer", "viewer"), ("dr_admin", "admin"), ("dr_editor", "editor")):
        PW[name] = "dr-" + secrets.token_urlsafe(16)
        users.create_user(name, PW[name], role)
VIEWER, ADMIN, EDITOR = "user:dr_viewer", "user:dr_admin", "user:dr_editor"

print("--- the jobs, one in each state ---")
done_slug = upload("done-crate.zip", zip_bytes([("d/one.txt", b"done one"), ("d/two.txt", b"done two")]))
j_done = unpack.run_job(unpack.start(done_slug)["id"], run_background=NOOP)
check("fixture: a finished job", j_done["status"] == "done", j_done.get("error"))

part_slug = upload("partial-crate.zip", zip_bytes([("p/good.txt", b"good"), ("p/bad.txt", b"bad")]))
real_ingest = ingest.ingest_file


def flaky(f, filename, **kw):
    if filename == "bad.txt":
        raise OSError("disk hiccup while storing bad.txt")
    return real_ingest(f, filename, **kw)


ingest.ingest_file = flaky
try:
    j_part = unpack.run_job(unpack.start(part_slug)["id"], run_background=NOOP)
finally:
    ingest.ingest_file = real_ingest
check("fixture: a partial job", j_part["status"] == "partial", j_part["status"])

fail_slug = upload("junk-crate.zip", zip_bytes([(".DS_Store", b"x"), ("__MACOSX/._a", b"y")]))
j_fail = unpack.run_job(unpack.start(fail_slug)["id"], run_background=NOOP)
check("fixture: a failed job", j_fail["status"] == "failed", j_fail["status"])

run_slug = upload("running-crate.zip", zip_bytes([("r/a.txt", b"run a")]))
j_run = unpack.start(run_slug)
db._update_unpack_job(j_run["id"], status="running", started_at=time.time(),
                      progress={"phase": "members", "done": 3, "total": 10, "current": "r/a.txt",
                                "message": "Unpacking 4 of 10: r/a.txt"})
queue_slug = upload("queued-crate.zip", zip_bytes([("q/a.txt", b"queued a")]))
j_queue = unpack.start(queue_slug)

old_slug = upload("old-crate.zip", zip_bytes([("o/a.txt", b"old a")]))
j_old = unpack.run_job(unpack.start(old_slug)["id"], run_background=NOOP)
db._update_unpack_job(j_old["id"], finished_at=time.time() - unpack.DRAWER_RECENT_SECONDS - 60)

with actor.acting_as(ADMIN):
    sens_slug = upload("secret-crate.zip", zip_bytes([("s/a.txt", b"secret a")]), sensitive=True)
    j_sens = unpack.run_job(unpack.start(sens_slug)["id"], run_background=NOOP)

gone_slug = upload("gone-crate.zip", zip_bytes([("g/a.txt", b"gone a")]))
j_gone = unpack.run_job(unpack.start(gone_slug)["id"], run_background=NOOP)
items.delete([gone_slug])
check("fixture: the deleted archive is gone", db.get_by_slug(gone_slug) is None)
with actor.acting_as(EDITOR):
    mine_slug = upload("editors-crate.zip", zip_bytes([("e/a.txt", b"editor a")]))
    j_mine = unpack.run_job(unpack.start(mine_slug)["id"], run_background=NOOP)
items.delete([mine_slug])

print("--- drawer_jobs, as a viewer ---")
vj = unpack.drawer_jobs(actor=VIEWER)
r = by_id(vj, j_run["id"])
check("a running job is listed with its phase, done / total and the current member",
      r is not None and r["status"] == "running" and not r["finished"] and (r["done"], r["total"]) == (3, 10)
      and r["current"] == "r/a.txt" and r["phase"] == "members", r)
check("... with its archive named and linked", r and r["archive"]["url"] == f"/object/{run_slug}"
      and r["archive"]["name"], r and r["archive"])
qj = by_id(vj, j_queue["id"])
check("a queued job is listed, waiting", qj is not None and qj["status"] == "queued" and qj["message"] == "Waiting to start.", qj)
d = by_id(vj, j_done["id"])
check("a finished job shows the card it made (a link) and its counts",
      d is not None and d["finished"] and d["card"] and d["card"]["url"].startswith("/project/")
      and d["counts"]["created"] == 2 and d["current"] is None, d)
p = by_id(vj, j_part["id"])
check("a partial job names each failed member with its reason",
      p is not None and p["status"] == "partial" and len(p["failed"]) == 1 and p["failed"][0]["path"] == "p/bad.txt"
      and "disk hiccup" in p["failed"][0]["reason"] and p["failed_more"] == 0, p)
f = by_id(vj, j_fail["id"])
check("a failed job says why", f is not None and f["status"] == "failed" and f["error"] and "Nothing to unpack" in f["error"], f)
check("a job that finished more than an hour ago is not listed", by_id(vj, j_old["id"]) is None)
check("running and queued jobs come first", [j["status"] for j in vj[:2]] == ["running", "queued"], [j["status"] for j in vj])
check("a sensitive archive's job is hidden from a viewer (nothing about the item leaks)", by_id(vj, j_sens["id"]) is None)
check("a deleted archive's job is hidden from a viewer", by_id(vj, j_gone["id"]) is None)
check("... and from a viewer for someone else's deleted archive too", by_id(vj, j_mine["id"]) is None)
mj = by_id(unpack.drawer_jobs(actor=EDITOR), j_mine["id"])
check("the person who started a job still sees it once its archive is deleted (no link)",
      mj is not None and mj["is_mine"] and mj["archive"]["url"] is None, mj)
check("is_mine is false for jobs someone else started", not any(j["is_mine"] for j in vj))

print("--- drawer_jobs, as the admin ---")
aj = unpack.drawer_jobs(actor=ADMIN)
s = by_id(aj, j_sens["id"])
check("the admin sees the sensitive archive's job, marked as theirs", s is not None and s["is_mine"], s)
g = by_id(aj, j_gone["id"])
check("the admin sees a deleted archive's job, saying the archive is deleted (no link)",
      g is not None and g["archive"]["url"] is None and "deleted" in g["archive"]["name"], g)

print("--- many failures are cut short, and say how many more ---")
many = {"failed": [{"path": f"m/{i}.txt", "reason": "boom"} for i in range(unpack.DRAWER_FAILED_SHOWN + 4)],
        "message": "Unpacked with failures.", "counts": {"created": 0, "linked": 0, "skipped": 0, "failed": 14}}
db._update_unpack_job(j_part["id"], result=many)
p2 = by_id(unpack.drawer_jobs(actor=VIEWER), j_part["id"])
check("at most DRAWER_FAILED_SHOWN failures are listed, with the rest counted",
      p2 and len(p2["failed"]) == unpack.DRAWER_FAILED_SHOWN and p2["failed_more"] == 4, p2 and (len(p2["failed"]), p2["failed_more"]))

print("--- web: GET /api/processing ---")
from starlette.testclient import TestClient  # noqa: E402
from web import app as webapp  # noqa: E402
BASE = "http://testhost.local"
SAME = {"Origin": BASE}
c = TestClient(webapp.app, base_url=BASE, follow_redirects=False)
c.post("/api/auth/login", json={"username": "dr_viewer", "password": PW["dr_viewer"]}, headers=SAME)
res = c.get("/api/processing")
body = res.json() if res.status_code == 200 else {}
ids = {j["id"] for j in body.get("unpack_jobs", [])}
check("/api/processing answers unpack_jobs for a viewer", res.status_code == 200 and {j_run["id"], j_queue["id"], j_done["id"]} <= ids,
      res.text[:300])
check("... without the sensitive or deleted archives' jobs", j_sens["id"] not in ids and j_gone["id"] not in ids)
check("... and the in-flight count includes the queued and running jobs",
      body.get("count", 0) >= 2 and body["count"] == sum(1 for i in body["items"] if i["overall"] == "processing") + 2,
      (body.get("count"), [i["overall"] for i in body.get("items", [])]))
page = c.get("/").text
check("the drawer template has the jobs box and renders unpack_jobs",
      'id="proc-jobs"' in page and "data.unpack_jobs" in page and "Open the card" in page)
anon = TestClient(webapp.app, base_url=BASE, follow_redirects=False)
check("an anonymous caller still gets nothing (401)", anon.get("/api/processing").status_code == 401)

print()
if FAILS:
    print(f"FAILED: {', '.join(FAILS)}")
    sys.exit(1)
print("ALL PASS")
