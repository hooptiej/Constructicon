#!/usr/bin/env python3
"""Self-contained check for #559: the audit log never keeps a saved secret.

Throwaway DB, storage and exports (scripts/_testenv.py), Starlette's TestClient, no server:

    python scripts/test_audit_secrets_559.py

Proves: a settings POST's secret value never lands in audit_log (any spelling of the path, a
secret in the wrong field, an error reason that echoes it) nor in GET /api/audit-log; the
provenance and curator `key` fields are kept; every route that takes a secret has a redaction
rule; the migration scrubs seeded leaked rows, leaves other rows alone, and is idempotent.
Dummy values only, and no check ever prints one. Exits 1 if any check fails, like the other
scripts here, after naming every failed check.
"""

import json
import os
import re
import sqlite3
import sys

import _testenv  # noqa: E402  (scripts/_testenv.py: temp DB + storage + exports, refuses otherwise)
TMP = _testenv.isolate("auditsecrets559-")
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))

from core import db  # noqa: E402
_testenv.assert_isolated()  # now as core actually resolved the paths
from web import app as webapp  # noqa: E402
from web import request_guard  # noqa: E402

FAILS = []


def check(name, cond, extra=""):
    print(("PASS " if cond else "FAIL ") + name + (f"  [{extra}]" if (extra and not cond) else ""))
    if not cond:
        FAILS.append(name)


db.init_db()
HOST = "testhost.local:8000"
client = _testenv.client(webapp.app, base_url=f"http://{HOST}")
OK = {"origin": f"http://{HOST}"}
SECRET = "dummy-secret-559-do-not-use"
SECRET2 = "dummy-password-559-do-not-use"


def audit_dump():
    """Every audit_log row as one string, for 'the dummy appears nowhere' checks."""
    conn = sqlite3.connect(db.DB_PATH)
    try:
        return "\n".join(" | ".join(str(c) for c in r) for r in conn.execute("SELECT * FROM audit_log"))
    finally:
        conn.close()


def rows_for(path):
    conn = sqlite3.connect(db.DB_PATH)
    try:
        return conn.execute("SELECT form_body, error_detail, status_code FROM audit_log WHERE path = ? ORDER BY id", (path,)).fetchall()
    finally:
        conn.close()


# --- 1. a settings POST never stores its value ---
r = client.post("/api/settings", data={"key": "youtube_data_api_key", "value": SECRET}, headers=OK)
check("settings POST succeeds on the throwaway DB", r.status_code == 200, f"HTTP {r.status_code}: {r.text[:120]}")
body = json.loads(rows_for("/api/settings")[-1][0])
check("settings row keeps the setting's name", body.get("key") == "youtube_data_api_key", str(body))
check("settings row redacts the value", body.get("value") == "[REDACTED]", str({k: ("<kept>" if k == "value" else v) for k, v in body.items()}))

r = client.post("/api/settings/", data={"key": "youtube_data_api_key", "value": SECRET}, headers=OK, follow_redirects=False)
check("trailing-slash settings POST does not log the value", SECRET not in audit_dump(), f"HTTP {r.status_code}")

client.post("/api/settings", data={"key": SECRET2, "value": SECRET}, headers=OK)  # a secret pasted into the name box; refused 400
check("a secret in the `key` field of settings is not logged", SECRET2 not in audit_dump())

client.post("/api/settings", data={"key": "pages_publish_token", "value": SECRET, "extra": SECRET2}, headers=OK)
check("an unexpected extra field on settings is not logged", SECRET2 not in audit_dump())

r = client.post("/api/settings", json={"key": "youtube_data_api_key", "value": SECRET}, headers=OK)  # JSON body: refused 4xx, still audited
check("a JSON-body settings POST does not log the value", SECRET not in audit_dump(), f"HTTP {r.status_code}")

check("no dummy secret anywhere in audit_log after all settings requests", SECRET not in audit_dump() and SECRET2 not in audit_dump())

served = client.get("/api/audit-log?limit=5000")
check("GET /api/audit-log serves no dummy secret", served.status_code == 200 and SECRET not in served.text and SECRET2 not in served.text,
      f"HTTP {served.status_code}")

# --- 2. plain `key` fields are kept ---
client.post("/api/provenance-options/card", data={"key": "audit_559_opt", "label": "Audit 559 Opt"}, headers=OK)
prow = rows_for("/api/provenance-options/card")
check("provenance-options row keeps the option slug", prow and json.loads(prow[-1][0]).get("key") == "audit_559_opt", str(prow[-1:]))
r = client.post("/api/curator/queue/defer", data={"key": "some_queue_key"}, headers=OK)
crow = rows_for("/api/curator/queue/defer")
check("curator queue row keeps its `key`", crow and json.loads(crow[-1][0]).get("key") == "some_queue_key", f"HTTP {r.status_code} {crow[-1:]}")

# --- 3. the backstop: secret-looking names are still redacted, nested ones too ---
scrub = request_guard.redact_audit_body
check("backstop redacts a top-level secret-looking field",
      scrub("/api/anything", {"api_token": SECRET, "title": "t"}) == {"api_token": "[REDACTED]", "title": "t"})
check("backstop redacts a nested secret-looking field",
      scrub("/api/anything", {"cfg": {"password": SECRET, "name": "n"}, "rows": [{"token": SECRET}]})
      == {"cfg": {"password": "[REDACTED]", "name": "n"}, "rows": [{"token": "[REDACTED]"}]})
check("a rule applies to the trailing-slash spelling", request_guard.audit_route_rule("/api/settings/") == "name_only"
      and request_guard.audit_route_rule("/api/users/7/password/") == "none")
check("password routes log no values at all",
      scrub("/api/auth/login", {"username": "u", "password": SECRET}) == {"_body": request_guard.NOT_LOGGED_BODY})
check("error reasons on a secret route keep only the code",
      request_guard.redact_audit_error("/api/settings/", f"bad_request: Unknown setting key: {SECRET}") == "bad_request")

# --- 4. every route that accepts a secret has a rule ---
SECRETISH = ("key", "secret", "token", "password", "api", "auth")
missing = []
for route in webapp.app.routes:
    methods = getattr(route, "methods", None) or set()
    path = getattr(route, "path", "")
    dep = getattr(route, "dependant", None)
    if not path.startswith("/api/") or not (methods & {"POST", "PUT", "PATCH", "DELETE"}) or dep is None:
        continue
    plain = set()
    for prefix, fields in request_guard.AUDIT_PLAIN_FIELDS:
        if path.startswith(prefix):
            plain |= fields
    sample = re.sub(r"\{[^}]+\}", "x", path)
    for p in dep.body_params:
        name = p.alias or p.name
        if any(w in name.lower() for w in SECRETISH) and name not in plain and request_guard.audit_route_rule(sample) is None:
            missing.append(f"{sorted(methods)} {path} field {name!r}")
check("every Form/body field that looks secret is on a route with a rule or a plain-field entry", not missing, "; ".join(missing))
# JSON-body routes read request.json() so FastAPI can't list their fields: name the ones that take a password.
for sample in ("/api/auth/login", "/api/auth/setup", "/api/account/password", "/api/users", "/api/users/3/password"):
    check(f"password route {sample} has a no-body rule", request_guard.audit_route_rule(sample) == "none")
check("the settings route has a name_only rule", request_guard.audit_route_rule("/api/settings") == "name_only")

# --- 5. the migration's path lists stay in step with the guard's ---
check("migration lists the same name_only paths as the guard",
      db._AUDIT_SECRET_NAME_ONLY_559 == {p for p, r in request_guard.AUDIT_ROUTE_RULES.items() if r == "name_only"})
check("migration lists the same no-body paths as the guard",
      db._AUDIT_SECRET_NO_BODY_559 == {p for p, r in request_guard.AUDIT_ROUTE_RULES.items() if r == "none"}
      and db._AUDIT_SECRET_NO_BODY_RE_559.pattern == request_guard.AUDIT_ROUTE_PATTERNS[0][0].pattern)

# --- 6. migration: scrubs seeded leaked rows, touches nothing else, idempotent ---
conn = sqlite3.connect(db.DB_PATH)
conn.execute("DELETE FROM audit_log")
seed = [
    # (path, form_body, error_detail)
    ("/api/settings", json.dumps({"key": "[REDACTED]", "value": SECRET}), None),                 # the leak in the issue
    ("/api/settings", json.dumps({"key": "[REDACTED]", "value": "short"}), None),
    ("/api/settings/", json.dumps({"key": "[REDACTED]", "value": SECRET}), None),                # trailing-slash spelling
    ("/api/settings", json.dumps({"key": "youtube_data_api_key", "value": SECRET, "x": SECRET2}), f"bad_request: Unknown setting key: {SECRET2}"),
    ("/api/settings", "not json " + SECRET, None),                                               # unreadable body
    ("/api/settings", json.dumps({"_unparsed": True, "content_type": "text/plain", "bytes": 9}), None),
    ("/api/settings", json.dumps({}), None),
    ("/api/auth/login", json.dumps({"username": "u", "password": SECRET}), None),                # should never exist, but scrub it
    ("/api/users/4/password", json.dumps({"password": SECRET}), "weak_password: " + SECRET2),
    ("/api/other", json.dumps({"value": "keep-me"}), "bad_request: keep this reason"),
    ("/api/provenance-options/card", json.dumps({"key": "keep_slug"}), None),
    ("/api/users/4/role", json.dumps({"role": "editor"}), None),
]
for path, body, err in seed:
    conn.execute("INSERT INTO audit_log (method, path, form_body, affected_slugs, status_code, error_detail, timestamp) "
                 "VALUES ('POST', ?, ?, '[]', 200, ?, 1)", (path, body, err))
conn.execute("DELETE FROM schema_migrations WHERE name = 'audit_scrub_secret_routes_559'")  # simulate a DB that hasn't run it
conn.commit()
before_ids = [r[0] for r in conn.execute("SELECT id FROM audit_log ORDER BY id")]
conn.close()
check("migration is registered", "audit_scrub_secret_routes_559" in [n for n, _ in db.MIGRATIONS])
ran = db.run_pending_migrations()
check("migration ran exactly once", ran == ["audit_scrub_secret_routes_559"], str(ran))
conn = sqlite3.connect(db.DB_PATH)
after1 = conn.execute("SELECT id, path, form_body, error_detail FROM audit_log ORDER BY id").fetchall()
conn.close()
dump1 = "\n".join(" | ".join(str(c) for c in r) for r in after1)
check("migration: no dummy secret left in any row", SECRET not in dump1 and SECRET2 not in dump1 and "short" not in dump1)
check("migration: row count unchanged", [r[0] for r in after1] == before_ids)
check("migration: settings value rewritten, a clean setting name kept",
      json.loads(after1[3][2]) == {"key": "youtube_data_api_key", "value": "[REDACTED]", "x": "[REDACTED]"}, "row 4 (values hidden)")
check("migration: an already-redacted name stays redacted", json.loads(after1[0][2]) == {"key": "[REDACTED]", "value": "[REDACTED]"})
check("migration: trailing-slash row scrubbed", json.loads(after1[2][2]) == {"key": "[REDACTED]", "value": "[REDACTED]"})
check("migration: unreadable body replaced whole", json.loads(after1[4][2]) == {"_body": "[REDACTED]"})
check("migration: unparsed marker and empty body left alone",
      json.loads(after1[5][2]) == {"_unparsed": True, "content_type": "text/plain", "bytes": 9} and json.loads(after1[6][2]) == {})
check("migration: password-route rows lose their body", json.loads(after1[7][2]) == {"_body": request_guard.NOT_LOGGED_BODY}
      and json.loads(after1[8][2]) == {"_body": request_guard.NOT_LOGGED_BODY})
check("migration: error reasons on those rows keep only the code", after1[3][3] == "bad_request" and after1[8][3] == "weak_password")
check("migration: other routes untouched (bodies and reasons)",
      json.loads(after1[9][2]) == {"value": "keep-me"} and after1[9][3] == "bad_request: keep this reason"
      and json.loads(after1[10][2]) == {"key": "keep_slug"} and json.loads(after1[11][2]) == {"role": "editor"})

# idempotent: force the migration to run again over the scrubbed rows; nothing may change
conn = sqlite3.connect(db.DB_PATH)
conn.execute("DELETE FROM schema_migrations WHERE name = 'audit_scrub_secret_routes_559'")
conn.commit()
conn.close()
check("migration re-ran", db.run_pending_migrations() == ["audit_scrub_secret_routes_559"])
conn = sqlite3.connect(db.DB_PATH)
after2 = conn.execute("SELECT id, path, form_body, error_detail FROM audit_log ORDER BY id").fetchall()
conn.close()
check("migration is idempotent (a second run changes nothing)", after1 == after2)
check("migration is a no-op once recorded", db.run_pending_migrations() == [])

print()
print("FAILED: " + ", ".join(FAILS) if FAILS else "all checks passed")
sys.exit(1 if FAILS else 0)
