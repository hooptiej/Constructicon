#!/usr/bin/env python3
"""Self-contained check for #812: one "too big" refusal, whichever guard catches the file.

web/body_limit.py lets the upload routes take CONSTRUCTICON_MAX_UPLOAD_MB plus
UPLOAD_OVERHEAD_BYTES (1 MB) of multipart framing. A file inside that extra megabyte passes the body
cap and is refused later by the per-file check (ingest's early size check, or storage.save_stream
partway through the write). That second refusal used to be HTTP 400 `bad_request`; it is now the same
413 `payload_too_large`, with the same `details` keys, as the body cap's.

Throwaway DB + storage via _testenv, the real FastAPI app through TestClient. No server, no network:

    python scripts/test_upload_limit_812.py

Upload cap for this run: 2 MB (so the body cap is 3 MB).

Covers: a file over the body cap is 413 payload_too_large with details {max_bytes, limit_kind};
a file over the upload cap but inside the framing allowance gets the same code, status, body keys and
details keys; the same through save_stream (the early size check bypassed); max_bytes is the per-file
cap; nothing is left behind in storage and no item row is made; a file under the cap still uploads.
"""
import os
import sqlite3
import sys
import types

import _testenv  # noqa: E402  (scripts/_testenv.py: temp DB + storage + exports, refuses otherwise)
TMP = _testenv.isolate("uploadlimit812-")
os.environ["CONSTRUCTICON_MAX_UPLOAD_MB"] = "2"
os.environ["CAPTION_DISABLED"] = "1"
ROOT = os.path.abspath(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
sys.path.insert(0, ROOT)
sys.modules.setdefault("cairosvg", types.ModuleType("cairosvg"))  # no libcairo needed here

from core import db, ingest, paths, storage  # noqa: E402
_testenv.assert_isolated()
from web import app as webapp, body_limit  # noqa: E402

FAILS = []


def check(name, cond, extra=""):
    print(("PASS " if cond else "FAIL ") + name + (f"  [{extra}]" if (extra and not cond) else ""))
    if not cond:
        FAILS.append(name)


def body_of(r):
    try:
        return r.json()
    except ValueError:
        return {}


def error_of(r):
    return body_of(r).get("error") or {}


def stored_files():
    d = paths.storage_dir()
    return sorted(p.name for p in d.iterdir() if p.is_file()) if d.is_dir() else []


def item_count():
    c = sqlite3.connect(db.DB_PATH)
    try:
        return c.execute("SELECT COUNT(*) FROM capture_events").fetchone()[0]
    finally:
        c.close()


db.init_db()
tok = _testenv.client(webapp.app)

CAP = storage.MAX_BYTES
BODY_CAP = CAP + body_limit.UPLOAD_OVERHEAD_BYTES
check("the upload cap for this run is 2 MB", CAP == 2 * 1024 * 1024, CAP)

LINE = ("z" * 80 + "\n").encode()
over_body = LINE * ((CAP + body_limit.UPLOAD_OVERHEAD_BYTES + 1024 * 1024) // len(LINE))  # ~4 MB: past the body cap
in_window = LINE * ((CAP + 512 * 1024) // len(LINE))                                    # ~2.5 MB: over the cap, inside the allowance
under_cap = LINE * ((CAP // 2) // len(LINE))                                            # ~1 MB: fine
check("test files sit where intended",
      len(over_body) > BODY_CAP and CAP < len(in_window) < BODY_CAP and len(under_cap) < CAP,
      (len(over_body), len(in_window), len(under_cap)))

# --- 1. the body cap's refusal (the reference shape) ---------------------------------------------
before_files = stored_files()
r_body = tok.post("/api/upload", files={"file": ("over-body-812.txt", over_body, "text/plain")})
check("over the body cap: 413 payload_too_large", r_body.status_code == 413 and error_of(r_body).get("code") == "payload_too_large",
      (r_body.status_code, r_body.text[:300]))
body_details = error_of(r_body).get("details") or {}
check("... with details {max_bytes, limit_kind: upload}",
      set(body_details) == {"max_bytes", "limit_kind"} and body_details.get("limit_kind") == "upload", body_details)

# --- 2. inside the framing allowance: refused by the per-file check (early size check) -----------
r_early = tok.post("/api/upload", files={"file": ("in-window-812.txt", in_window, "text/plain")})
check("over the upload cap, inside the framing allowance: 413 (was 400)", r_early.status_code == 413, (r_early.status_code, r_early.text[:300]))
check("... code payload_too_large", error_of(r_early).get("code") == "payload_too_large", r_early.text[:300])
check("... ok:false and a detail string, as every error", body_of(r_early).get("ok") is False and isinstance(body_of(r_early).get("detail"), str),
      r_early.text[:300])
check("... the same top-level keys as the body cap's", set(body_of(r_early)) == set(body_of(r_body)), (set(body_of(r_early)), set(body_of(r_body))))
check("... the same error keys as the body cap's", set(error_of(r_early)) == set(error_of(r_body)), (set(error_of(r_early)), set(error_of(r_body))))
early_details = error_of(r_early).get("details") or {}
check("... the same details keys, limit_kind upload", set(early_details) == set(body_details) and early_details.get("limit_kind") == "upload",
      early_details)
check("... max_bytes is the per-file upload cap", early_details.get("max_bytes") == CAP, early_details)
check("... the message says what happened (the limit in MB)", "2MB" in (error_of(r_early).get("message") or ""), error_of(r_early))

# --- 3. the same file, but through save_stream (early size check bypassed) -------------------------
_real_ingest_file = ingest.ingest_file


def _understated_size(fileobj, filename, size=0, **kw):
    """The route measured the upload; pretend it didn't, so the cap is only caught while writing."""
    return _real_ingest_file(fileobj, filename, size=1, **kw)


ingest.ingest_file = _understated_size
try:
    r_stream = tok.post("/api/upload", files={"file": ("in-window-stream-812.txt", in_window, "text/plain")})
finally:
    ingest.ingest_file = _real_ingest_file
check("through save_stream: 413 payload_too_large", r_stream.status_code == 413 and error_of(r_stream).get("code") == "payload_too_large",
      (r_stream.status_code, r_stream.text[:300]))
stream_details = error_of(r_stream).get("details") or {}
check("... the same details keys and values as the early check",
      stream_details == early_details, (stream_details, early_details))

# --- 4. nothing left behind ----------------------------------------------------------------------
check("no file was left in storage by any refused upload", stored_files() == before_files, stored_files())
check("no item row was made by any refused upload", item_count() == 0, item_count())

# --- 5. a file under the cap still uploads --------------------------------------------------------
r_ok = tok.post("/api/upload", files={"file": ("under-cap-812.txt", under_cap, "text/plain")})
check("a file under the cap uploads (200)", r_ok.status_code == 200 and body_of(r_ok).get("slug"), (r_ok.status_code, r_ok.text[:300]))

print("\n%s" % ("ALL PASS" if not FAILS else "FAILED: " + ", ".join(FAILS)))
sys.exit(1 if FAILS else 0)
