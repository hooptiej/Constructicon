#!/usr/bin/env python3
"""#648 "Finders keepers": claiming a fresh install needs proof, and only one claim can win.

Throwaway DBs, storage and exports (scripts/_testenv.py), Starlette's TestClient, no server:

    python scripts/test_setup_race_648.py

Proves, on a fresh install (no users):
  * POST /api/auth/setup with no install token -> 403 setup_proof_required, nobody created;
    a wrong one (or a non-string) -> 403 setup_proof_wrong; repeats -> 429 too_many_attempts, even
    with the right token while backing off;
  * the right token -> the admin is created and signed in; /setup and POST setup are 404 after,
    with the right OR a wrong token (no token oracle once set up);
  * concurrent setups with the right token -> exactly one admin: five in-process HTTP requests at
    once, and six separate OS processes (like the web and MCP containers) calling
    users.create_first_admin on one DB file at the same moment;
  * the escape hatches: /admin with the install token still shows "Create the admin account",
    the token can create the admin over the API (POST /api/users, or setup with a Bearer header),
    and `python3 -m core.manage.reset_password --create-admin` works with no proof at all;
  * an install with no token configured: /setup explains the reset script, POST is 403
    setup_unavailable;
  * the audit log never holds the install token, a wrong token or a password.
Every credential is generated per run and none is printed. Exits 1 if any check fails.
"""

import contextlib
import io
import json
import os
import secrets
import sqlite3
import subprocess
import sys
import threading
import time
from pathlib import Path

import _testenv  # noqa: E402  (scripts/_testenv.py: temp DB + storage + exports, refuses otherwise)
TMP = _testenv.isolate("setup648-")
HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.join(HERE, "..")
sys.path.insert(0, ROOT)
sys.path.insert(0, HERE)

from starlette.testclient import TestClient  # noqa: E402

from core import db, install_config, install_token, users  # noqa: E402
_testenv.assert_isolated()
from web import app as webapp  # noqa: E402
from core.manage import reset_password  # noqa: E402  (#634: moved from scripts/)

FAILS = []
HOST = "testhost.local"
SAME = {"Origin": f"http://{HOST}"}
TOKEN = _testenv.TOKEN
FIELD = users.SETUP_PROOF_FIELD
WRONG = "wrong-" + secrets.token_urlsafe(40)
SECRETS = [TOKEN, WRONG]


def check(name, cond, extra=""):
    print(("PASS " if cond else "FAIL ") + name + (f"  [{extra}]" if (extra and not cond) else ""))
    if not cond:
        FAILS.append(name)


def pw():
    p = "s648-" + secrets.token_urlsafe(12)
    SECRETS.append(p)
    return p


def code(r):
    try:
        return (r.json().get("error") or {}).get("code")
    except ValueError:
        return None


def anon():
    return TestClient(webapp.app, base_url=f"http://{HOST}")


def setup(client, proof=None, username=None, password=None, extra_headers=None, omit=False):
    body = {"username": username or ("u_" + secrets.token_hex(3)), "password": password or pw()}
    if not omit:
        body[FIELD] = proof
    return client.post("/api/auth/setup", json=body, headers={**SAME, **(extra_headers or {})})


def fresh_db(name):
    """Point this process at a new empty DB (the app reads db.DB_PATH per connection)."""
    db.DB_PATH = Path(TMP) / name
    db.init_db()
    install_config.clear_cache()
    users.limiter.reset()


def audit_dump():
    conn = sqlite3.connect(db.DB_PATH)
    try:
        return "\n".join(" | ".join(str(c) for c in r) for r in conn.execute("SELECT * FROM audit_log"))
    finally:
        conn.close()


def no_secret_in_audit(label):
    dump = audit_dump()
    check(f"{label}: no install token, wrong token or password in audit_log",
          not any(s in dump for s in SECRETS))


# =============================================================================================
print("--- 1. fresh install: proof required ---")
fresh_db("main.db")
c = anon()
check("fresh install needs setup", users.setup_needed())
r = c.get("/setup")
check("GET /setup 200 with an install-token field and where to find it",
      r.status_code == 200 and f'name="{FIELD}"' in r.text and "secrets/constructicon_token" in r.text, r.status_code)

r = setup(c, omit=True)
check("no install token -> 403 setup_proof_required (shared shape)",
      r.status_code == 403 and code(r) == "setup_proof_required" and r.json().get("ok") is False
      and r.json().get("detail"), r.text[:200])
check("...and nobody was created", users.setup_needed())
r = setup(c, proof="")
check("empty install token -> 403 setup_proof_required", r.status_code == 403 and code(r) == "setup_proof_required")
r = setup(c, proof=WRONG)
check("wrong install token -> 403 setup_proof_wrong", r.status_code == 403 and code(r) == "setup_proof_wrong",
      r.text[:200])
r = setup(c, proof=12345)
check("non-string install token -> 403", r.status_code == 403 and code(r) in ("setup_proof_wrong", "setup_proof_required"))
check("still nobody after 4 refusals", users.setup_needed())

r = setup(c, proof=WRONG)
check("5th wrong token -> still 403 (5 free attempts, like sign-in)", r.status_code == 403, r.status_code)
r = setup(c, proof=WRONG)
check("6th wrong token right after -> 429 too_many_attempts with retry_after",
      r.status_code == 429 and code(r) == "too_many_attempts" and r.json()["error"]["details"]["retry_after"] >= 1,
      r.text[:200])
r = setup(c, proof=TOKEN)
check("even the RIGHT token is 429 while backing off (not checked)", r.status_code == 429 and users.setup_needed())
check("refusals logged with the code only", "setup_proof_wrong" in audit_dump() and "too_many_attempts" in audit_dump())
no_secret_in_audit("after refusals")
users.limiter.reset()  # the backoff window would expire on its own; don't sleep through it here

# =============================================================================================
print("--- 2. right proof, five setups at once over HTTP ---")
N = 5
barrier = threading.Barrier(N)
results = [None] * N
clients = [anon() for _ in range(N)]
names = [f"racer{i}_{secrets.token_hex(2)}" for i in range(N)]
passwords = [pw() for _ in range(N)]


def race(i):
    barrier.wait()
    results[i] = setup(clients[i], proof=TOKEN, username=names[i], password=passwords[i])


threads = [threading.Thread(target=race, args=(i,)) for i in range(N)]
for t in threads:
    t.start()
for t in threads:
    t.join()
statuses = sorted(r.status_code for r in results)
check("exactly one 200, the rest 404 not_found", statuses == [200] + [404] * (N - 1), statuses)
check("exactly one user, and it's an admin", db.count_users() == 1 and db.count_active_admins() == 1)
win = next(i for i, r in enumerate(results) if r.status_code == 200)
check("the winner's answer names the admin it created",
      results[win].json()["user"]["username"] == names[win] and results[win].json()["user"]["role"] == "admin")
check("the winner got a session cookie", (results[win].headers.get("set-cookie") or "").startswith(users.SESSION_COOKIE + "="))
me = clients[win].get("/api/auth/me").json()
check("the winner is signed in (/api/auth/me)", me["signed_in"] and me["user"]["username"] == names[win])
check("losers are not signed in", all(not clients[i].get("/api/auth/me").json()["signed_in"]
                                      for i in range(N) if i != win))
check("owner name filled once from the winner", install_config.owner_name() == names[win])

print("--- 3. afterwards: 404, no oracle ---")
check("GET /setup 404 once an admin exists", c.get("/setup").status_code == 404)
r = setup(anon(), proof=TOKEN)
check("POST setup with the right token -> 404", r.status_code == 404 and code(r) == "not_found")
r = setup(anon(), proof=WRONG)
check("POST setup with a wrong token -> the same 404 (no token oracle)", r.status_code == 404 and code(r) == "not_found")
check("still exactly one user", db.count_users() == 1)
no_secret_in_audit("after setup")
conn = sqlite3.connect(db.DB_PATH)
bodies = [row[0] for row in conn.execute("SELECT form_body FROM audit_log WHERE path = '/api/auth/setup'"
                                          " AND method = 'POST'")]
conn.close()
check("every setup request-log row keeps no body values (not even the field names)",
      len(bodies) >= 10 and all(FIELD not in (b or "") and "username" not in (b or "") for b in bodies),
      (len(bodies), bodies[:2]))

# =============================================================================================
print("--- 4. six processes claim one DB file at the same moment ---")
RACE_DB = Path(TMP) / "race.db"
saved = db.DB_PATH
db.DB_PATH = RACE_DB
db.init_db()
db.DB_PATH = saved
CHILD = r"""
import json, sys, time
sys.path.insert(0, sys.argv[3])
from core import actor, users
from core.errors import AppError
go, name = float(sys.argv[1]), sys.argv[2]
ready = time.time()
while time.time() < go:
    time.sleep(0.0005)
start = time.time()
with actor.acting_as(actor.ACTOR_SCRIPT):
    try:
        users.create_first_admin(name, "child-pw-" + name + "-0123456789")
        out = "won"
    except AppError as e:
        out = e.code
print(json.dumps({"name": name, "out": out, "ready": ready, "start": start, "end": time.time()}))
"""
P = 6
go = time.time() + 15
env = {**os.environ, "CONSTRUCTICON_DB_PATH": str(RACE_DB), "PYTHONDONTWRITEBYTECODE": "1"}
procs = [subprocess.Popen([sys.executable, "-c", CHILD, repr(go), f"proc{i}", os.path.abspath(ROOT)],
                          stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, env=env) for i in range(P)]
outs = []
for p in procs:
    so, se = p.communicate(timeout=120)
    try:
        outs.append(json.loads(so.strip().splitlines()[-1]))
    except (ValueError, IndexError):
        outs.append({"out": "crashed", "err": se[-300:]})
won = [o for o in outs if o.get("out") == "won"]
lost = [o for o in outs if o.get("out") == "not_found"]
check("every process was ready before the start line", all(o.get("ready", go) < go for o in outs),
      [o.get("err") or round(o.get("ready", 0) - go, 2) for o in outs])
spread = max(o.get("start", 0) for o in outs) - min(o.get("start", 0) for o in outs) if outs else 99
check("they all started within 50 ms of each other (overlapping ~100 ms hashes)", spread < 0.05, f"{spread:.3f}s")
check("exactly one process won, the other five got 404 not_found", len(won) == 1 and len(lost) == P - 1,
      [o.get("out") for o in outs])
conn = sqlite3.connect(RACE_DB)
n_users, n_admins = conn.execute("SELECT COUNT(*), SUM(role = 'admin') FROM users").fetchone()
n_setup_rows = conn.execute("SELECT COUNT(*) FROM audit_log WHERE op = ?", (users.OP_SETUP,)).fetchone()[0]
conn.close()
check("the DB file holds exactly one user, an admin, and one setup change-log row",
      n_users == 1 and n_admins == 1 and n_setup_rows == 1, (n_users, n_admins, n_setup_rows))

# =============================================================================================
print("--- 5. escape hatches ---")
fresh_db("hatch_admin.db")
tokc = _testenv.client(webapp.app, base_url=f"http://{HOST}")
r = tokc.get("/admin")
check("/admin with the install token shows 'Create the admin account' -> /setup",
      r.status_code == 200 and "users-setup-banner" in r.text and 'href="/setup"' in r.text, r.status_code)
r = tokc.post("/api/users", json={"username": "tok_admin", "password": pw(), "role": "admin"}, headers=SAME)
check("the install token can create the admin over the API (Admin > Users)", r.status_code == 200 and db.count_users() == 1,
      r.text[:200])
check("...which closes /setup", anon().get("/setup").status_code == 404)

fresh_db("hatch_bearer.db")
r = setup(tokc, omit=True)
check("setup with an Authorization: Bearer install token needs no body proof", r.status_code == 200
      and r.json()["user"]["role"] == "admin", r.text[:200])

fresh_db("hatch_script.db")
buf = io.StringIO()
with contextlib.redirect_stdout(buf), contextlib.redirect_stderr(io.StringIO()):
    rc = reset_password.main(["--create-admin", "rescue_admin", "--generate"])
check("core.manage.reset_password --create-admin works on a fresh install (no proof: shell access is the proof)",
      rc == 0 and db.count_users() == 1 and db.get_user(username="rescue_admin")["role"] == "admin")
check("...and /setup is 404 after it", anon().get("/setup").status_code == 404)

# =============================================================================================
print("--- 6. an install with no install token ---")
fresh_db("no_token.db")
os.environ.pop("CONSTRUCTICON_INSTALL_TOKEN", None)
install_token.reset_cache()
try:
    check("no token configured", not install_token.enabled())
    r = anon().get("/setup")
    check("/setup explains core.manage.reset_password --create-admin and shows no form",
          r.status_code == 200 and "setup-unavailable" in r.text and "--create-admin" in r.text
          and "python3 -m core.manage.reset_password" in r.text and "scripts/reset_password" not in r.text
          and 'id="setup-form"' not in r.text, r.status_code)
    r = setup(anon(), proof=WRONG)
    check("POST setup -> 403 setup_unavailable", r.status_code == 403 and code(r) == "setup_unavailable", r.text[:200])
    check("nobody created", users.setup_needed())
finally:
    _testenv.use_token()
no_secret_in_audit("no-token install")

print()
if FAILS:
    print(f"FAILED: {', '.join(FAILS)}")
    sys.exit(1)
print("ALL PASS")
