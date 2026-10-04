#!/usr/bin/env python3
"""Self-contained check for the request guard (#558) and audit redaction (#559).

Uses a throwaway SQLite DB (CONSTRUCTICON_DB_PATH set before core is imported) and
Starlette's TestClient, so no real database or server is touched:

    python scripts/test_request_guard.py

Exits 1 if any check fails. Dummy values only; no real secrets.
"""

import json
import os
import sqlite3
import sys
import tempfile

TMP = tempfile.mkdtemp(prefix="reqguard-")
os.environ["CONSTRUCTICON_DB_PATH"] = os.path.join(TMP, "test.db")
os.environ.setdefault("CONSTRUCTICON_STORAGE_DIR", os.path.join(TMP, "storage"))
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))

from starlette.testclient import TestClient  # noqa: E402

from core import db  # noqa: E402
from web import app as webapp  # noqa: E402

FAILS = []


def check(name, cond, extra=""):
    print(("PASS " if cond else "FAIL ") + name + (f"  [{extra}]" if (extra and not cond) else ""))
    if not cond:
        FAILS.append(name)


db.init_db()
HOST = "testhost.local:8000"
client = TestClient(webapp.app, base_url=f"http://{HOST}")
DUMMY = "dummy-not-a-real-secret-123"


def audit_rows(path):
    conn = sqlite3.connect(db.DB_PATH)
    try:
        return [r[0] for r in conn.execute("SELECT form_body FROM audit_log WHERE path = ? ORDER BY id", (path,))]
    finally:
        conn.close()


def post(headers=None, **kw):
    return client.post("/api/settings", data={"key": "thingiverse_app_token", "value": DUMMY},
                       headers=headers or {}, **kw)


# --- Origin / Referer rules ---
check("evil Origin -> 403", post({"Origin": "http://evil.example"}).status_code == 403)
r = post({"Origin": "http://evil.example"})
check("403 body is cross_origin JSON", r.json().get("error", {}).get("code") == "cross_origin" and r.json().get("ok") is False)
check("Origin: null -> 403", post({"Origin": "null"}).status_code == 403)
check("matching Origin passes", post({"Origin": f"http://{HOST}"}).status_code == 200)
check("no Origin, no Referer passes", post().status_code == 200)
check("foreign Referer, no Origin -> 403", post({"Referer": "http://evil.example/x"}).status_code == 403)
check("matching Referer, no Origin passes", post({"Referer": f"http://{HOST}/admin"}).status_code == 200)
check("default-port Host/Origin forms are equivalent",
      client.post("/api/settings", data={"key": "thingiverse_app_token", "value": DUMMY},
                  headers={"Origin": "http://testhost.local", "Host": "testhost.local:80"}).status_code == 200)
check("same host different port -> 403", post({"Origin": "http://testhost.local:9999"}).status_code == 403)
check("GET with evil Origin unaffected", client.get("/api/settings", headers={"Origin": "http://evil.example"}).status_code == 200)
for m in ("put", "delete"):
    rr = getattr(client, m)("/api/blog-entries/nope", headers={"Origin": "http://evil.example"})
    check(f"{m.upper()} with evil Origin -> 403", rr.status_code == 403)
os.environ["CONSTRUCTICON_ALLOWED_ORIGINS"] = "http://friend.example, other.example:81"
check("env-allowed Origin passes", post({"Origin": "http://friend.example"}).status_code == 200)
check("env-allowed bare host:port passes", post({"Origin": "http://other.example:81"}).status_code == 200)
check("non-listed Origin still 403 with env set", post({"Origin": "http://evil.example"}).status_code == 403)
del os.environ["CONSTRUCTICON_ALLOWED_ORIGINS"]

# --- JSON routes need Content-Type: application/json ---
ok_origin = {"Origin": f"http://{HOST}"}
check("export/build text/plain -> 415",
      client.post("/api/export/build", content="{}", headers={**ok_origin, "Content-Type": "text/plain"}).status_code == 415)
check("export/build form-encoded -> 415",
      client.post("/api/export/build", data={"a": "b"}, headers=ok_origin).status_code == 415)
check("export/publish text/plain -> 415",
      client.post("/api/export/publish", content='{"target":"test"}', headers={**ok_origin, "Content-Type": "text/plain"}).status_code == 415)
for sub in ("projects", "items"):
    check(f"blog-entries/{sub} text/plain -> 415",
          client.put(f"/api/blog-entries/x/{sub}", content="[]", headers={**ok_origin, "Content-Type": "text/plain"}).status_code in (404, 415))
check("export/publish application/json passes the type gate (400 = no token, not 415)",
      client.post("/api/export/publish", json={"target": "test"}, headers=ok_origin).status_code != 415)
check("export/build application/json passes the type gate",
      client.post("/api/export/build", json={}, headers=ok_origin).status_code != 415)

# --- delete-all needs the typed phrase ---
check("delete-all no body -> 400", client.post("/api/delete-all", headers=ok_origin).status_code == 400)
check("delete-all wrong phrase -> 400", client.post("/api/delete-all", data={"confirm": "yes"}, headers=ok_origin).status_code == 400)
check("delete-all evil Origin even with phrase -> 403",
      client.post("/api/delete-all", data={"confirm": "DELETE EVERYTHING"}, headers={"Origin": "http://evil.example"}).status_code == 403)
# success branch, safe: throwaway DB only
r = client.post("/api/delete-all", data={"confirm": "DELETE EVERYTHING"}, headers=ok_origin)
check("delete-all with phrase -> 200 on throwaway DB", r.status_code == 200 and "deleted" in r.json(), r.text[:100])
rows = audit_rows("/api/delete-all")
check("delete-all writes audit rows (400s and the success)", len(rows) >= 3, str(len(rows)))

# --- audit redaction ---
rows = audit_rows("/api/settings")
check("settings audit rows exist", len(rows) >= 3)
check("no dummy secret anywhere in settings audit rows", all(DUMMY not in b for b in rows))
parsed = [json.loads(b) for b in rows]
check("settings rows log name only, value redacted",
      all(p.get("key") == "thingiverse_app_token" and p.get("value") == "[REDACTED]" for p in parsed), str(parsed[:1]))

client.post("/api/projects", data={"title": "Guard Test"}, headers=ok_origin)
# provenance option add: `key` is a plain slug, must not be over-redacted
r = client.post("/api/provenance-options/card", data={"key": "guard_test_opt", "label": "Guard Test Opt"}, headers=ok_origin)
prow = audit_rows("/api/provenance-options/card")
check("provenance-options audit keeps the option slug", prow and json.loads(prow[-1]).get("key") == "guard_test_opt", str(prow[-1:] if prow else r.text[:80]))
check("heuristic backstop still redacts token-ish fields",
      webapp._scrub_secrets({"api_token": "x", "title": "t"}, "/api/anything") == {"api_token": "[REDACTED]", "title": "t"})

# --- migration: scrub old rows, idempotent, touches nothing else ---
conn = sqlite3.connect(db.DB_PATH)
conn.execute("DELETE FROM audit_log")
legacy = [("/api/settings", json.dumps({"key": "[REDACTED]", "value": DUMMY})),
          ("/api/settings", json.dumps({"key": "[REDACTED]", "value": "short"})),
          ("/api/settings", "not json " + DUMMY),
          ("/api/other", json.dumps({"value": "keep-me"}))]
for path, body in legacy:
    conn.execute("INSERT INTO audit_log (method, path, form_body, affected_slugs, status_code, timestamp) VALUES ('POST', ?, ?, '[]', 200, 1)", (path, body))
conn.commit()
conn.close()
db.init_db()
conn = sqlite3.connect(db.DB_PATH)
after1 = conn.execute("SELECT id, path, form_body FROM audit_log ORDER BY id").fetchall()
conn.close()
check("scrub: settings values rewritten", all(DUMMY not in b and "short" not in b for _, p, b in after1 if p == "/api/settings"))
check("scrub: key field of old rows untouched", json.loads(after1[0][2]).get("value") == "[REDACTED]" and json.loads(after1[0][2]).get("key") == "[REDACTED]")
check("scrub: other routes untouched", json.loads(after1[3][2]) == {"value": "keep-me"})
db.init_db()
conn = sqlite3.connect(db.DB_PATH)
after2 = conn.execute("SELECT id, path, form_body FROM audit_log ORDER BY id").fetchall()
conn.close()
check("scrub is idempotent (second init_db is a no-op)", after1 == after2)

print()
print("FAILED: " + ", ".join(FAILS) if FAILS else "all checks passed")
sys.exit(1 if FAILS else 0)
