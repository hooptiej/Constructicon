#!/usr/bin/env python3
"""Self-contained check for the per-route request-body caps (#649, web/body_limit.py).

Throwaway DB via _testenv, the real FastAPI app through TestClient, plus direct ASGI calls with a
hand-made `receive` for the chunked cases (so the test can count exactly how many bytes the app
pulled). No server, no network:

    python scripts/test_body_cap_649.py

Limits for this run: upload cap 2 MB (so the upload routes allow 3 MB with the framing allowance),
general JSON/form routes 256 KB, auth routes the default 64 KB.

Covers: an anonymous sign-in body declared over the limit is 413 payload_too_large without the
route running, and the request log records it without the body; just under the limit signs in; a
chunked body with no Content-Length is cut off once it passes the limit (sign-in, and an upload
whose multipart parse trips it inside FastAPI); an upload above the general cap but under the
upload cap succeeds; above the upload cap is 413; a password over MAX_PASSWORD_LENGTH is refused
without hashing; every route that takes a file is classified as an upload route (and nothing
else is); bad env values fall back to the defaults.
"""
import asyncio
import inspect
import json
import os
import sqlite3
import sys
import types

import _testenv  # noqa: E402  (scripts/_testenv.py: temp DB + storage + exports, refuses otherwise)
TMP = _testenv.isolate("bodycap649-")
os.environ["CONSTRUCTICON_MAX_UPLOAD_MB"] = "2"
os.environ["CONSTRUCTICON_MAX_BODY_KB"] = "256"
os.environ.pop("CONSTRUCTICON_MAX_AUTH_BODY_KB", None)
os.environ["CAPTION_DISABLED"] = "1"
ROOT = os.path.abspath(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
sys.path.insert(0, ROOT)
sys.modules.setdefault("cairosvg", types.ModuleType("cairosvg"))  # no libcairo needed here

from fastapi import UploadFile  # noqa: E402
from fastapi.routing import APIRoute  # noqa: E402
from starlette.testclient import TestClient  # noqa: E402

from core import db, storage, users  # noqa: E402
_testenv.assert_isolated()
from web import app as webapp, body_limit  # noqa: E402
from web.routes import auth as auth_routes  # noqa: E402

FAILS = []


def check(name, cond, extra=""):
    print(("PASS " if cond else "FAIL ") + name + (f"  [{extra}]" if (extra and not cond) else ""))
    if not cond:
        FAILS.append(name)


db.init_db()
PW = "correct horse battery"
users.create_first_admin("alice", PW, "Alice")

AUTH_LIMIT = 64 * 1024
GENERAL_LIMIT = 256 * 1024
UPLOAD_LIMIT = storage.MAX_BYTES + body_limit.UPLOAD_OVERHEAD_BYTES

# Count calls into the login route's body reader: the route looks `_json_body` up at call time.
_real_json_body = auth_routes._json_body
JSON_BODY_CALLS = []


def _counting_json_body(request):
    JSON_BODY_CALLS.append(request.url.path)
    return _real_json_body(request)


auth_routes._json_body = _counting_json_body

# Count password hashes (scrypt) so the too-long check can prove it skipped hashing.
_real_scrypt = users._scrypt
SCRYPT_CALLS = []


def _counting_scrypt(*a, **kw):
    SCRYPT_CALLS.append(1)
    return _real_scrypt(*a, **kw)


users._scrypt = _counting_scrypt


def last_audit(path):
    c = sqlite3.connect(db.DB_PATH)
    c.row_factory = sqlite3.Row
    row = c.execute("SELECT * FROM audit_log WHERE path = ? ORDER BY id DESC LIMIT 1", (path,)).fetchone()
    c.close()
    return dict(row) if row else None


def err_code(r):
    try:
        return (r.json().get("error") or {}).get("code")
    except ValueError:
        return None


def login_body(size, password=PW):
    """A JSON sign-in body of exactly `size` bytes (padded with an ignored field)."""
    base = {"username": "alice", "password": password, "pad": ""}
    raw = json.dumps(base)
    base["pad"] = "x" * (size - len(raw))
    out = json.dumps(base).encode()
    assert len(out) == size, (len(out), size)
    return out


# --- 0. the limits themselves -------------------------------------------------------------------

check("auth routes get the 64 KB default", body_limit.limit_for("/api/auth/login") == (AUTH_LIMIT, "auth"),
      body_limit.limit_for("/api/auth/login"))
check("a trailing slash or doubled slash doesn't dodge the auth limit",
      body_limit.limit_for("/api/auth/login/")[1] == "auth" and body_limit.limit_for("//api//auth/setup")[1] == "auth")
check("account and user admin routes are auth-sized too",
      all(body_limit.limit_for(p)[1] == "auth" for p in ("/api/account/password", "/api/users", "/api/users/3/password")))
check("upload routes get the upload cap plus the framing allowance",
      body_limit.limit_for("/api/upload") == (UPLOAD_LIMIT, "upload")
      and body_limit.limit_for("/api/image/abc123/replace-file") == (UPLOAD_LIMIT, "upload"))
check("everything else gets the general limit (256 KB here)",
      body_limit.limit_for("/api/install-config") == (GENERAL_LIMIT, "default")
      and body_limit.limit_for("/api/image/abc123") == (GENERAL_LIMIT, "default"))
check("lookalike paths are not upload routes",
      body_limit.limit_for("/api/uploader")[1] == "default"
      and body_limit.limit_for("/api/image/a/b/replace-file")[1] == "default")

saved = os.environ.get("CONSTRUCTICON_MAX_BODY_KB")
for bad in ("", "garbage", "0", "-5"):
    os.environ["CONSTRUCTICON_MAX_BODY_KB"] = bad
    check(f"CONSTRUCTICON_MAX_BODY_KB={bad!r} falls back to the 1 MB default",
          body_limit.limit_for("/api/x")[0] == body_limit.DEFAULT_BODY_KB * 1024)
os.environ["CONSTRUCTICON_MAX_BODY_KB"] = saved

# Every APIRoute that takes a file must be an upload route, and every upload route must take one.
file_routes, upload_routes = set(), set()
for route in webapp.app.routes:
    if not isinstance(route, APIRoute):
        continue
    takes_file = any("UploadFile" in str(param.annotation) or param.annotation is UploadFile
                     for param in inspect.signature(route.endpoint).parameters.values())
    sample = route.path.replace("{slug}", "abc123")
    for seg in ("{project_id}", "{user_id}", "{id}", "{hobby_id}", "{entry_id}"):
        sample = sample.replace(seg, "1")
    if takes_file:
        file_routes.add(route.path)
    if body_limit.limit_for(sample)[1] == "upload":
        upload_routes.add(route.path)
check("the routes that take a file are exactly the upload-limit routes",
      file_routes == upload_routes and file_routes == {"/api/upload", "/api/image/{slug}/replace-file"},
      f"file={sorted(file_routes)} upload={sorted(upload_routes)}")


# --- 1. anonymous sign-in, declared over the limit ----------------------------------------------

anon = TestClient(webapp.app)
JSON_BODY_CALLS.clear()
big = login_body(2 * 1024 * 1024)
r = anon.post("/api/auth/login", content=big, headers={"Content-Type": "application/json"})
check("a 2 MB anonymous sign-in body is 413", r.status_code == 413, (r.status_code, r.text[:200]))
check("... with code payload_too_large in the shared shape",
      err_code(r) == "payload_too_large" and r.json().get("ok") is False and r.json().get("detail"), r.text[:200])
check("... and details.max_bytes = the auth limit", (r.json().get("error") or {}).get("details", {}).get("max_bytes") == AUTH_LIMIT,
      r.text[:300])
check("... and the sign-in route never ran", JSON_BODY_CALLS == [], JSON_BODY_CALLS)
row = last_audit("/api/auth/login")
check("the request log has the 413", row is not None and row["status_code"] == 413, row)
check("... with the code only (sign-in errors keep no text) and no body",
      row is not None and row["error_detail"] == "payload_too_large" and "xxxx" not in (row["form_body"] or ""),
      row and (row["error_detail"], row["form_body"]))

r = anon.post("/api/auth/login", content=login_body(AUTH_LIMIT + 1), headers={"Content-Type": "application/json"})
check("one byte over the limit is 413 too", r.status_code == 413 and err_code(r) == "payload_too_large",
      (r.status_code, r.text[:200]))


# --- 2. just under the limit signs in -----------------------------------------------------------

JSON_BODY_CALLS.clear()
ok_client = TestClient(webapp.app)
r = ok_client.post("/api/auth/login", content=login_body(AUTH_LIMIT), headers={"Content-Type": "application/json"})
check("a sign-in body of exactly the limit (64 KB) signs in", r.status_code == 200 and r.json().get("ok") is True,
      (r.status_code, r.text[:200]))
check("... and the route ran", JSON_BODY_CALLS == ["/api/auth/login"], JSON_BODY_CALLS)
r = TestClient(webapp.app).post("/api/auth/login", content=json.dumps({"username": "alice", "password": PW}).encode(),
                                headers={"Content-Type": "application/json"})
check("a normal sign-in (fresh browser) still works", r.status_code == 200, (r.status_code, r.text[:200]))


# --- 3. a password over MAX_PASSWORD_LENGTH is refused without hashing ---------------------------

SCRYPT_CALLS.clear()
long_pw = "p" * (users.MAX_PASSWORD_LENGTH + 1)
r = TestClient(webapp.app).post("/api/auth/login", json={"username": "alice", "password": long_pw})
check("an over-long password is the usual 401 invalid_login", r.status_code == 401 and err_code(r) == "invalid_login",
      (r.status_code, r.text[:200]))
check("... and nothing was hashed", SCRYPT_CALLS == [], len(SCRYPT_CALLS))
SCRYPT_CALLS.clear()
r = TestClient(webapp.app).post("/api/auth/login", json={"username": "nobody", "password": long_pw})
check("... the same for an unknown user (no burn-time hash either)", r.status_code == 401 and SCRYPT_CALLS == [],
      (r.status_code, len(SCRYPT_CALLS)))
SCRYPT_CALLS.clear()
r = TestClient(webapp.app).post("/api/auth/login", json={"username": "alice", "password": "p" * users.MAX_PASSWORD_LENGTH})
check("a wrong password at exactly the max length is still checked (hashed)", r.status_code == 401 and len(SCRYPT_CALLS) >= 1,
      (r.status_code, len(SCRYPT_CALLS)))
users.limiter.reset()


# --- 4. chunked bodies (no Content-Length) are cut off -------------------------------------------

def drive(path, headers, chunk, total, method="POST"):
    """Call the ASGI app with a body of `total` bytes in `chunk`-sized pieces and no
    Content-Length. Returns (status, response body, bytes the app pulled)."""
    pulled = 0
    sent = []
    remaining = [total]

    async def receive():
        nonlocal pulled
        if remaining[0] <= 0:
            return {"type": "http.request", "body": b"", "more_body": False}
        piece = chunk[: remaining[0]]
        remaining[0] -= len(piece)
        pulled += len(piece)
        return {"type": "http.request", "body": piece, "more_body": remaining[0] > 0}

    async def send(message):
        sent.append(message)

    scope = {"type": "http", "asgi": {"version": "3.0"}, "http_version": "1.1", "method": method,
             "scheme": "http", "path": path, "raw_path": path.encode(), "query_string": b"", "root_path": "",
             "headers": [(k.lower().encode("latin-1"), v.encode("latin-1")) for k, v in headers],
             "client": ("127.0.0.1", 40000), "server": ("testserver", 80)}
    asyncio.run(webapp.app(scope, receive, send))
    status = next((m["status"] for m in sent if m["type"] == "http.response.start"), None)
    body = b"".join(m.get("body", b"") for m in sent if m["type"] == "http.response.body")
    return status, body, pulled


CHUNK = b"x" * 16384
JSON_BODY_CALLS.clear()
status, body, pulled = drive("/api/auth/login", [("host", "testserver"), ("content-type", "application/json")],
                             CHUNK, 10 * 1024 * 1024)
check("a chunked 10 MB anonymous sign-in body is 413", status == 413, (status, body[:200]))
check("... payload_too_large", b"payload_too_large" in body, body[:200])
check("... cut off at the limit (at most limit + one chunk pulled, not 10 MB)", pulled <= AUTH_LIMIT + len(CHUNK), pulled)
check("... and the route never ran", JSON_BODY_CALLS == [], JSON_BODY_CALLS)
row = last_audit("/api/auth/login")
check("... and the request log has it", row is not None and row["status_code"] == 413, row and row["status_code"])

# A chunked JSON body to a general (token) route: same, at the general limit.
status, body, pulled = drive("/api/install-config", [("host", "testserver"), ("content-type", "application/json"),
                                                      ("authorization", f"Bearer {_testenv.TOKEN}")],
                             CHUNK, 5 * 1024 * 1024)
check("a chunked 5 MB JSON body to a general route is 413, cut off at its limit",
      status == 413 and pulled <= GENERAL_LIMIT + len(CHUNK), (status, pulled, body[:200]))
row = last_audit("/api/install-config")
check("... logged with a 'not read' marker, never the body",
      row is not None and row["status_code"] == 413 and "not read" in (row["form_body"] or "")
      and "xxxx" not in (row["form_body"] or ""), row and (row["status_code"], row["form_body"]))

# A chunked multipart upload over the upload limit: the audit logger skips it (unknown size), so the
# multipart parser inside FastAPI trips it; FastAPI must pass the 413 through, not turn it into a 400.
boundary = "----bodycap649"
head = (f"--{boundary}\r\nContent-Disposition: form-data; name=\"file\"; filename=\"big.bin\"\r\n"
        "Content-Type: application/octet-stream\r\n\r\n").encode()


def multipart_drive(file_bytes_total):
    pulled = 0
    sent = []
    state = {"left": file_bytes_total, "head_done": False, "tail_done": False}
    tail = f"\r\n--{boundary}--\r\n".encode()

    async def receive():
        nonlocal pulled
        if not state["head_done"]:
            state["head_done"] = True
            pulled += len(head)
            return {"type": "http.request", "body": head, "more_body": True}
        if state["left"] > 0:
            piece = CHUNK[: state["left"]]
            state["left"] -= len(piece)
            pulled += len(piece)
            return {"type": "http.request", "body": piece, "more_body": True}
        if not state["tail_done"]:
            state["tail_done"] = True
            pulled += len(tail)
            return {"type": "http.request", "body": tail, "more_body": False}
        return {"type": "http.disconnect"}

    async def send(message):
        sent.append(message)

    scope = {"type": "http", "asgi": {"version": "3.0"}, "http_version": "1.1", "method": "POST",
             "scheme": "http", "path": "/api/upload", "raw_path": b"/api/upload", "query_string": b"",
             "root_path": "", "client": ("127.0.0.1", 40001), "server": ("testserver", 80),
             "headers": [(b"host", b"testserver"), (b"authorization", f"Bearer {_testenv.TOKEN}".encode()),
                         (b"content-type", f"multipart/form-data; boundary={boundary}".encode())]}
    asyncio.run(webapp.app(scope, receive, send))
    status = next((m["status"] for m in sent if m["type"] == "http.response.start"), None)
    body = b"".join(m.get("body", b"") for m in sent if m["type"] == "http.response.body")
    return status, body, pulled


status, body, pulled = multipart_drive(8 * 1024 * 1024)
check("a chunked 8 MB multipart upload (limit 3 MB) is 413, not 400", status == 413, (status, body[:300]))
check("... payload_too_large", b"payload_too_large" in body, body[:200])
check("... cut off at the upload limit", pulled <= UPLOAD_LIMIT + len(CHUNK) + len(head), pulled)
row = last_audit("/api/upload")
check("... and the request log says 413 too (not the parser's 400)",
      row is not None and row["status_code"] == 413 and "payload_too_large" in (row["error_detail"] or ""),
      row and (row["status_code"], row["error_detail"]))


# --- 5. uploads with a Content-Length ------------------------------------------------------------

tok = _testenv.client(webapp.app)
# 1 MB of text: over the 256 KB general limit, under the 2 MB upload cap.
mid_txt = ("line of text for the body cap test\n" * 30000).encode()[: 1024 * 1024]
r = tok.post("/api/upload", files={"file": ("mid-649.txt", mid_txt, "text/plain")})
check("an upload over the general limit but under the upload cap succeeds",
      r.status_code == 200 and r.json().get("slug"), (r.status_code, r.text[:300]))

too_big = ("y" * 80 + "\n").encode() * (4 * 1024 * 1024 // 81)  # ~4 MB > 3 MB upload limit
r = tok.post("/api/upload", files={"file": ("big-649.txt", too_big, "text/plain")})
check("an upload over the upload limit is 413 payload_too_large", r.status_code == 413 and err_code(r) == "payload_too_large",
      (r.status_code, r.text[:300]))
row = last_audit("/api/upload")
check("... and the request log records the 413 with a 'not read' marker",
      row is not None and row["status_code"] == 413 and "not read" in (row["form_body"] or ""),
      row and (row["status_code"], row["form_body"]))

# An anonymous upload is refused (401) before its body is read, whatever its size.
status, body, pulled = drive("/api/upload", [("host", "testserver"),
                                             ("content-type", f"multipart/form-data; boundary={boundary}")],
                             CHUNK, 5 * 1024 * 1024)
check("an anonymous upload is 401 with nothing read", status == 401 and pulled == 0, (status, pulled))

# A general route just under its limit still works (the install token, a JSON body).
pad = {"owner_name": "Test Owner", "_pad": "z" * (GENERAL_LIMIT - 200)}
r = tok.post("/api/install-config", content=json.dumps(pad).encode(), headers={"Content-Type": "application/json"})
check("a general route under its limit reaches the route (not 413)", r.status_code != 413, (r.status_code, r.text[:200]))
r = tok.post("/api/install-config", content=json.dumps({"x": "z" * (GENERAL_LIMIT + 10)}).encode(),
             headers={"Content-Type": "application/json"})
check("a general route over its limit is 413", r.status_code == 413 and err_code(r) == "payload_too_large",
      (r.status_code, r.text[:200]))

print("\n%s" % ("ALL PASS" if not FAILS else "FAILED: " + ", ".join(FAILS)))
sys.exit(1 if FAILS else 0)
