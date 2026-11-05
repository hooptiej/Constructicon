#!/usr/bin/env python3
"""Nothing in the Box: a 0-byte upload is refused, and the ones already stored can be listed (#830).
Throwaway DB + storage, no server.

    python scripts/test_empty_upload_830.py

In-process (scripts/_testenv.py). Covers:
  1. the core door: ingest.ingest_file refuses size 0 (error_kind "empty", the exact message), and also
     refuses when the DECLARED size is wrong but nothing was actually written;
  2. every front end that reaches it: the web upload (400, code empty_file, the message in both
     error.message and detail), the MCP upload and the MCP import (code empty_file), and an unpacked
     archive's empty member (skipped and listed with the same words; the rest of the archive still
     unpacks);
  3. nothing is kept: no capture_events row, no stray file in storage, for each refusal;
  4. a normal small file still succeeds on each path;
  5. the negative control: the storage layer alone (the old behaviour) would have kept the empty file;
  6. scripts/list_empty_files_830.py: lists exactly the items whose stored file is 0 bytes, lists a
     missing stored file separately, and changes nothing (read-only).
Exits 1 if any check fails.
"""

import contextlib
import io
import os
import sqlite3
import sys
import zipfile

import _testenv  # noqa: E402  (scripts/_testenv.py: temp DB + storage + exports, refuses otherwise)
TMP = _testenv.isolate("empty830-")
os.environ["CAPTION_DISABLED"] = "1"
IMPORT = os.path.join(TMP, "import-inbox") if isinstance(TMP, str) else str(TMP / "import-inbox")
os.makedirs(IMPORT, exist_ok=True)
os.environ["CONSTRUCTICON_IMPORT_DIR"] = IMPORT
HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)
sys.path.insert(0, HERE)

try:
    import cairosvg  # noqa: F401
except Exception:  # no libcairo here (e.g. Windows): object types import it
    import types
    sys.modules["cairosvg"] = types.ModuleType("cairosvg")

from starlette.testclient import TestClient  # noqa: E402

from core import actor, db, ingest, paths, storage, unpack  # noqa: E402
_testenv.assert_isolated()
from web import app as webapp  # noqa: E402

ingest.run_in_thread = lambda fn, *a: None
NOOP = lambda fn, *a: None  # noqa: E731  (no OCR threads in a test)
FAILS = []
HOST = "testhost.local"
BASE = f"http://{HOST}"
SAME = {"Origin": BASE}


def check(name, cond, extra=""):
    print(("PASS " if cond else "FAIL ") + name + (f"  [{str(extra)[:300]}]" if (extra and not cond) else ""))
    if not cond:
        FAILS.append(name)


def msg(name):
    return f"{name} is empty (0 bytes), so nothing was saved. Check the original file and upload it again."


def item_count():
    c = sqlite3.connect(os.environ["CONSTRUCTICON_DB_PATH"])
    try:
        return c.execute("SELECT COUNT(*) FROM capture_events").fetchone()[0]
    finally:
        c.close()


def storage_files():
    return sorted(p.name for p in paths.storage_dir().iterdir() if p.is_file())


def snapshot():
    return item_count(), storage_files()


db.init_db()
from mcp_server import server as mcp  # noqa: E402  (after the DB env is set)

TOKEN = _testenv.client(webapp.app, base_url=BASE, follow_redirects=False)

# ===== 1. the core door ===============================================================================
print("--- 1. ingest.ingest_file ---")
before = snapshot()
res = ingest.ingest_file(io.BytesIO(b""), "blank.txt", size=0, source="tester", run_background=NOOP)
check("size 0: refused with error_kind 'empty'", res.error_kind == "empty" and res.row is None and not res.duplicate, res)
check("size 0: the exact message", res.error == msg("blank.txt"), res.error)
check("size 0: nothing stored (no row, no file)", snapshot() == before, (before, snapshot()))

res = ingest.ingest_file(io.BytesIO(b""), "lied-about.txt", size=999, source="tester", run_background=NOOP)
check("a wrong declared size with no bytes written is refused too", res.error_kind == "empty" and res.error == msg("lied-about.txt"), res)
check("... and its partial file is removed", snapshot() == before, (before, snapshot()))

real = ingest.ingest_file(io.BytesIO(b"hello\n"), "hello.txt", size=6, source="tester", run_background=NOOP)
check("a normal small file still succeeds", real.row is not None and not real.error, real)
after_real = snapshot()
check("... one row, one file", after_real[0] == before[0] + 1 and len(after_real[1]) == len(before[1]) + 1, after_real)

res = ingest.ingest_file(io.BytesIO(b""), "hello.txt", size=0, source="tester", run_background=NOOP)
check("an empty file named like an existing one is 'empty', not 'duplicate'", res.error_kind == "empty" and not res.duplicate, res)

# ===== 2a. the web upload ============================================================================
print("--- 2a. web upload ---")
before = snapshot()
r = TOKEN.post("/api/upload", files={"file": ("web-empty.txt", b"", "text/plain")}, headers=SAME)
body = r.json() if r.headers.get("content-type", "").startswith("application/json") else {}
check("web: 400", r.status_code == 400, (r.status_code, r.text[:300]))
check("web: error.code is empty_file", body.get("error", {}).get("code") == "empty_file", body)
check("web: error.message is the exact message", body.get("error", {}).get("message") == msg("web-empty.txt"), body)
check("web: detail (the page JS reads it) carries the same words", body.get("detail") == msg("web-empty.txt"), body)
check("web: nothing stored", snapshot() == before, (before, snapshot()))
r = TOKEN.post("/api/upload", files={"file": ("web-ok.txt", b"fine\n", "text/plain")}, headers=SAME)
check("web: a normal small file still succeeds", r.status_code == 200 and r.json().get("slug"), r.text[:300])

# ===== 2b. MCP upload and import =====================================================================
print("--- 2b. MCP upload / import ---")
import base64  # noqa: E402
before = snapshot()
out = mcp.constructicon_upload("mcp-empty.txt", "")
check("MCP upload: ok false, code empty_file", out.get("ok") is False and out["error"]["code"] == "empty_file", out)
check("MCP upload: the exact message", out["error"]["message"] == msg("mcp-empty.txt"), out)
check("MCP upload: nothing stored", snapshot() == before, (before, snapshot()))
out = mcp.constructicon_upload("mcp-ok.txt", base64.b64encode(b"fine\n").decode())
check("MCP upload: a normal small file still succeeds", out.get("slug") and out.get("duplicate") is False, out)

with open(os.path.join(IMPORT, "inbox-empty.txt"), "wb"):
    pass
with open(os.path.join(IMPORT, "inbox-ok.txt"), "wb") as f:
    f.write(b"fine too\n")
before = snapshot()
out = mcp.constructicon_import("inbox-empty.txt")
check("MCP import: ok false, code empty_file", out.get("ok") is False and out["error"]["code"] == "empty_file", out)
check("MCP import: the exact message", out["error"]["message"] == msg("inbox-empty.txt"), out)
check("MCP import: nothing stored, the inbox file is left alone",
      snapshot() == before and os.path.exists(os.path.join(IMPORT, "inbox-empty.txt")), (before, snapshot()))
out = mcp.constructicon_import("inbox-ok.txt")
check("MCP import: a normal small file still succeeds", out.get("slug") and out.get("duplicate") is False, out)

# ===== 2c. unpack members ============================================================================
print("--- 2c. unpacked archive members ---")


def zip_bytes(entries):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        for name, data in entries:
            z.writestr(name, data)
    return buf.getvalue()


ZIP = zip_bytes([("pack/readme.txt", b"the one real member\n"), ("pack/empty.txt", b"")])
arch = ingest.ingest_file(io.BytesIO(ZIP), "pack.zip", size=len(ZIP), source="tester", run_background=NOOP)
check("fixture: the archive itself is a normal upload", arch.row is not None, arch)
slug = arch.row["slug"]
p = unpack.preview(slug, "collection", None)
skipped = {s["path"]: s["reason"] for s in p["skipped"]}
check("preview: the empty member is skipped and listed with the upload's words",
      skipped.get("pack/empty.txt") == msg("empty.txt"), skipped)
check("preview: the real member is still to be ingested",
      [m["path"] for m in p["members"]] == ["pack/readme.txt"], [m["path"] for m in p["members"]])
before = snapshot()
job = unpack.start(slug, "collection", None)
job = unpack.run_job(job["id"], run_background=NOOP)
r = job["result"]
check("unpack: the real member is created, the empty one is skipped with the reason",
      [c["path"] for c in r["created"]] == ["pack/readme.txt"]
      and any(s["path"] == "pack/empty.txt" and s["reason"] == msg("empty.txt") for s in r["skipped"]),
      (r["created"], r["skipped"]))
# (the new card also gets a file-less write-up row, so rows can't be counted here; stored files can)
check("unpack: exactly one new stored file (the real member); the empty one left nothing on disk",
      len(storage_files()) == len(before[1]) + 1 and all((paths.storage_dir() / n).stat().st_size > 0 for n in storage_files()),
      (before, snapshot()))
check("unpack: no stored item is named empty.txt",
      not [x for x in sqlite3.connect(os.environ["CONSTRUCTICON_DB_PATH"]).execute(
          "SELECT slug FROM capture_events WHERE filename = 'empty.txt'")])

# ===== 5. negative control ===========================================================================
print("--- 5. negative control: what the old code did ---")
before = snapshot()
old_slug, old_stored, old_n = storage.save_stream("old-empty.zip", io.BytesIO(b""))
check("the storage layer alone keeps a 0-byte file (so the guard has to live in ingest)",
      old_n == 0 and (paths.storage_dir() / old_stored).exists() and (paths.storage_dir() / old_stored).stat().st_size == 0)
db.insert_upload(old_slug, "old-empty.zip", old_stored, "tester", media_type="archive", file_size=0)
check("... and an item row for it could be made (the pre-fix result)", item_count() == before[0] + 1)

# ===== 6. the listing ================================================================================
print("--- 6. scripts/list_empty_files_830.py (read-only) ---")
import list_empty_files_830 as lister  # noqa: E402

gone_slug = "gonefile830"
db.insert_upload(gone_slug, "vanished.txt", "gonefile830.txt", "tester", media_type="text", file_size=5)
db_path = os.environ["CONSTRUCTICON_DB_PATH"]
snap = (item_count(), storage_files(), os.path.getsize(db_path),
        {n: (paths.storage_dir() / n).stat().st_size for n in storage_files()})
conn = lister.open_ro(db_path)
found = lister.find_empty(conn, paths.storage_dir())
conn.close()
empty_slugs = {r["slug"] for r in found["empty"]}
check("lists the 0-byte item", empty_slugs == {old_slug}, empty_slugs)
check("a stored file that is missing is listed separately, not as empty",
      [r["slug"] for r in found["missing"]] == [gone_slug], found["missing"])
_c = sqlite3.connect(db_path)
_with_file = _c.execute("SELECT COUNT(*) FROM capture_events WHERE stored_filename IS NOT NULL AND stored_filename != ''").fetchone()[0]
_c.close()
check("it checked every item that holds a stored file", found["checked"] == _with_file, (found["checked"], _with_file))

buf = io.StringIO()
with contextlib.redirect_stdout(buf):
    rc = lister.main([db_path, "--storage", str(paths.storage_dir())])
out = buf.getvalue()
check("main: exit 0 and the report names the empty item and says nothing was changed",
      rc == 0 and f"/object/{old_slug}" in out and "Nothing was changed" in out and "1 item(s) have a 0-byte" in out, out)
check("main: a bad database path exits 2 with a plain message",
      lister.main([os.path.join(TMP, "no-such.db") if isinstance(TMP, str) else str(TMP / "no-such.db"),
                   "--storage", str(paths.storage_dir())]) == 2)
check("read-only: the database, the item count and every stored file are unchanged",
      snap == (item_count(), storage_files(), os.path.getsize(db_path),
               {n: (paths.storage_dir() / n).stat().st_size for n in storage_files()}))

print()
if FAILS:
    print(f"FAILED: {len(FAILS)} check(s): {FAILS}")
    sys.exit(1)
print("ALL PASS")
