#!/usr/bin/env python3
"""Auth step 1 (#467): users, passwords, sessions, CSRF, first-run setup. Throwaway DB, no server.

    python scripts/test_auth_step1.py

Covers: the scrypt hash format and verification; validation; first-run setup (only once, signs
in, fills the owner name); the session cookie's flags; sliding expiry and logout; sign-in failures
and the backoff; CSRF (required with a session cookie, not without); actor attribution in the
change log and request log; the last-admin rule; disabling and password changes ending sessions;
role_of() reading the signed-in user's role while nothing is enforced; generic undo refusing a
user change; the reset script; and that no password or hash ever reaches the audit log.
Every credential here is generated per run. Exits 1 if any check fails.
"""

import contextlib
import io
import os
import re
import secrets
import sqlite3
import sys

import _testenv  # noqa: E402  (scripts/_testenv.py: temp DB + storage + exports, refuses otherwise)
TMP = _testenv.isolate("auth1-")
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, ".."))
sys.path.insert(0, HERE)

from starlette.testclient import TestClient  # noqa: E402

from core import actor, cards, db, install_config, roles, users  # noqa: E402
from core.errors import AppError  # noqa: E402
_testenv.assert_isolated()
from web import app as webapp  # noqa: E402
import reset_password  # noqa: E402

FAILS = []
HOST = "testhost.local"
SAME = {"Origin": f"http://{HOST}"}


def check(name, cond, extra=""):
    print(("PASS " if cond else "FAIL ") + name + (f"  [{extra}]" if (extra and not cond) else ""))
    if not cond:
        FAILS.append(name)


def code_of(fn, *a, **k):
    try:
        fn(*a, **k)
    except AppError as e:
        return e.code
    return None


def pw():
    return "t-" + secrets.token_urlsafe(12)


def csrf_from(html):
    m = re.search(r'<meta name="csrf-token" content="([^"]+)"', html)
    return m.group(1) if m else None


db.init_db()
users.limiter.reset()
SECRETS = []  # every password used, to grep the audit log for at the end

# --- 1. hashing -----------------------------------------------------------------------------
p1 = pw()
SECRETS.append(p1)
h1, h2 = users.hash_password(p1), users.hash_password(p1)
parts = h1.split("$")
check("hash is scrypt$n$r$p$salt$hash", len(parts) == 6 and parts[:4] == ["scrypt", str(2 ** 15), "8", "1"], h1[:20])
check("a fresh salt every time", h1 != h2 and parts[4] != h2.split("$")[4])
check("verify: right password", users.verify_password(p1, h1))
check("verify: wrong password", not users.verify_password(p1 + "x", h1))
check("verify: NULL hash is never a match", not users.verify_password(p1, None))
check("verify: malformed hash is False", not users.verify_password(p1, "scrypt$abc"))
tampered = "$".join(parts[:5] + [parts[5][:-2] + ("AA" if parts[5][-2:] != "AA" else "BB")])
check("verify: tampered hash is False", not users.verify_password(p1, tampered))
legacy = "scrypt$16384$8$1$" + "$".join(users._b64(x) for x in (
    b"0123456789abcdef", users._scrypt(p1, b"0123456789abcdef", 2 ** 14, 8, 1, 32)))
check("verify: older cost params still verify (self-describing)", users.verify_password(p1, legacy))

# --- 2. validation --------------------------------------------------------------------------
check("password under 10 chars refused", code_of(users.validate_password, "short") == "bad_password")
check("10-char password accepted", users.validate_password("abcdefghij") == "abcdefghij")
check("bad username refused", code_of(users.validate_username, "a b") == "bad_username")
check("bad role refused", code_of(users.validate_role, "owner") == "bad_role")

# --- 3. first-run setup ---------------------------------------------------------------------
client = TestClient(webapp.app, base_url=f"http://{HOST}")
anon = TestClient(webapp.app, base_url=f"http://{HOST}")  # never signs in (a script / anonymous page)
check("setup needed on an empty install", users.setup_needed())
check("GET /setup open while no users", client.get("/setup").status_code == 200)
r = client.get("/admin")
check("admin page shows 'Create the admin account' banner", "users-setup-banner" in r.text and "/setup" in r.text)
r = client.get("/")
check("anonymous page: 'Sign in' chip, no CSRF meta", "Sign in" in r.text and csrf_from(r.text) is None)
admin_name, admin_pw = "admin_" + secrets.token_hex(3), pw()
SECRETS.append(admin_pw)
r = client.post("/api/auth/setup", json={"username": admin_name, "password": "short"}, headers=SAME)
check("setup with a short password -> 400 bad_password", r.status_code == 400 and r.json()["error"]["code"] == "bad_password")
r = client.post("/api/auth/setup", json={"username": admin_name, "password": admin_pw, "display_name": "Test Owner"},
                headers=SAME)
check("setup creates the admin", r.status_code == 200 and r.json()["user"]["role"] == "admin", r.text[:200])
cookie = r.headers.get("set-cookie", "")
check("cookie: HttpOnly, SameSite=Lax, Path=/, 30-day Max-Age, no Secure on http",
      cookie.startswith(users.SESSION_COOKIE + "=") and "HttpOnly" in cookie and "SameSite=Lax" in cookie
      and "Path=/" in cookie and f"Max-Age={30 * 86400}" in cookie and "Secure" not in cookie, cookie)
check("setup fills install_config.owner_name", install_config.owner_name() == "Test Owner")
check("GET /setup is 404 once a user exists", client.get("/setup").status_code == 404)
r = anon.post("/api/auth/setup", json={"username": "second", "password": pw()}, headers=SAME)
check("POST setup again -> 404", r.status_code == 404)
check("setup banner gone from admin", "users-setup-banner" not in client.get("/admin").text)

# --- 4. signed in: chip, CSRF meta, me ------------------------------------------------------
r = client.get("/")
token = csrf_from(r.text)
check("signed-in page: name chip and CSRF meta + csrf.js", "Test Owner" in r.text and token and "/static/js/csrf.js" in r.text)
me = client.get("/api/auth/me").json()
check("/api/auth/me says signed in", me["signed_in"] and me["user"]["username"] == admin_name)
check("anonymous /api/auth/me is not signed in", anon.get("/api/auth/me").json()["signed_in"] is False)

# --- 5. CSRF --------------------------------------------------------------------------------
body = {"site_title": "Auth step 1 test"}
r = client.post("/api/install-config", json=body, headers=SAME)
check("cookie request without CSRF token -> 403 csrf_failed",
      r.status_code == 403 and r.json()["error"]["code"] == "csrf_failed", r.text[:200])
r = client.post("/api/install-config", json=body, headers={**SAME, "X-CSRF-Token": "wrong"})
check("cookie request with a wrong token -> 403", r.status_code == 403)
r = client.post("/api/install-config", json=body, headers={**SAME, "X-CSRF-Token": token})
check("cookie request with the token works", r.status_code == 200, r.text[:200])
r = anon.post("/api/install-config", json={"copyright_holder": "Anon"}, headers=SAME)
check("non-cookie client unaffected (no token needed)", r.status_code == 200, r.text[:200])

# --- 6. actor attribution -------------------------------------------------------------------
conn = sqlite3.connect(db.DB_PATH)
rows = conn.execute("SELECT actor, op FROM audit_log WHERE op = 'install_config_update' ORDER BY id").fetchall()
check("change log: signed-in edit is user:<name>", ("user:" + admin_name, "install_config_update") in rows, rows)
check("change log: anonymous edit stays owner-ui", ("owner-ui", "install_config_update") in rows, rows)
req = conn.execute("SELECT actor, status_code FROM audit_log WHERE op IS NULL AND path = '/api/install-config' "
                   "ORDER BY id").fetchall()
check("request log: signed-in rows are user:<name>, incl. the CSRF refusal",
      ("user:" + admin_name, 403) in req and ("user:" + admin_name, 200) in req, req)
check("request log: anonymous row owner-ui", ("owner-ui", 200) in req, req)
setup_row = conn.execute("SELECT actor FROM audit_log WHERE op = 'user_first_admin'").fetchone()
check("setup is logged (anonymous actor at setup time)", setup_row and setup_row[0] == "owner-ui")

# --- 7. role_of while nothing is enforced ----------------------------------------------------
check("roles.ENFORCE still False", roles.ENFORCE is False)
check("role_of(user:admin) = admin", roles.role_of("user:" + admin_name) == "admin")
check("role_of(owner-ui) = admin (unchanged)", roles.role_of("owner-ui") == "admin")

# --- 8. Admin > Users -----------------------------------------------------------------------
auth_h = {**SAME, "X-CSRF-Token": token}
ed_name, ed_pw = "editor_" + secrets.token_hex(3), pw()
SECRETS.append(ed_pw)
r = client.post("/api/users", json={"username": ed_name, "password": ed_pw, "role": "editor", "display_name": "Ed"},
                headers=auth_h)
check("admin adds an editor", r.status_code == 200 and r.json()["user"]["role"] == "editor", r.text[:200])
ed_id = r.json()["user"]["id"]
r = client.post("/api/users", json={"username": ed_name.upper(), "password": pw(), "role": "viewer"}, headers=auth_h)
check("username unique case-insensitively -> 409 username_taken", r.status_code == 409
      and r.json()["error"]["code"] == "username_taken")
check("role_of(user:editor) = editor", roles.role_of("user:" + ed_name) == "editor")
listing = client.get("/api/users").json()
check("listing has both users and no hash", len(listing["users"]) == 2 and "password_hash" not in str(listing)
      and "scrypt$" not in str(listing))
admin_id = [u for u in listing["users"] if u["username"] == admin_name][0]["id"]
r = client.post(f"/api/users/{admin_id}/role", json={"role": "editor"}, headers=auth_h)
check("demoting the last admin -> 409 last_admin", r.status_code == 409 and r.json()["error"]["code"] == "last_admin")
check("disabling the last admin -> 409", client.post(f"/api/users/{admin_id}/disable", headers=auth_h).status_code == 409)
check("deleting the last admin -> 409", client.post(f"/api/users/{admin_id}/delete", headers=auth_h).status_code == 409)
r = client.post(f"/api/users/{ed_id}/role", json={"role": "viewer"}, headers=auth_h)
check("change role editor -> viewer", r.status_code == 200 and r.json()["user"]["role"] == "viewer")

# the editor signs in (second browser), then is disabled: their session ends
edc = TestClient(webapp.app, base_url=f"http://{HOST}")
r = edc.post("/api/auth/login", json={"username": ed_name, "password": ed_pw}, headers=SAME)
check("second user signs in", r.status_code == 200)
check("their session exists", db.count_sessions(ed_id) == 1)
check("disable the user", client.post(f"/api/users/{ed_id}/disable", headers=auth_h).status_code == 200)
check("disable ended their session", db.count_sessions(ed_id) == 0 and not edc.get("/api/auth/me").json()["signed_in"])
r = edc.post("/api/auth/login", json={"username": ed_name, "password": ed_pw}, headers=SAME)
check("a disabled user can't sign in (401 invalid_login)", r.status_code == 401 and r.json()["error"]["code"] == "invalid_login")
users.limiter.reset()
check("enable again", client.post(f"/api/users/{ed_id}/enable", headers=auth_h).status_code == 200)
new_ed_pw = pw()
SECRETS.append(new_ed_pw)
r = client.post(f"/api/users/{ed_id}/password", json={"password": new_ed_pw}, headers=auth_h)
check("admin resets the password", r.status_code == 200)
check("old password no longer works", edc.post("/api/auth/login", json={"username": ed_name, "password": ed_pw},
                                               headers=SAME).status_code == 401)
check("new password works", edc.post("/api/auth/login", json={"username": ed_name, "password": new_ed_pw},
                                     headers=SAME).status_code == 200)
users.limiter.reset()
undo_row = conn.execute("SELECT id FROM audit_log WHERE op = 'user_set_role' ORDER BY id DESC").fetchone()
check("generic undo refuses a user change (no row images)", code_of(cards.undo, undo_row[0]) == "undo_refused")

# --- 9. my password -------------------------------------------------------------------------
other = TestClient(webapp.app, base_url=f"http://{HOST}")
other.post("/api/auth/login", json={"username": admin_name, "password": admin_pw}, headers=SAME)
check("admin has two sessions", db.count_sessions(admin_id) == 2)
r = client.post("/api/account/password", json={"current_password": "nope-nope-nope", "new_password": pw()}, headers=auth_h)
check("wrong current password -> 401 wrong_password", r.status_code == 401 and r.json()["error"]["code"] == "wrong_password")
admin_pw2 = pw()
SECRETS.append(admin_pw2)
r = client.post("/api/account/password", json={"current_password": admin_pw, "new_password": admin_pw2}, headers=auth_h)
check("change my password", r.status_code == 200 and r.json()["sessions_ended"] == 1, r.text[:200])
check("this session kept, the other ended", client.get("/api/auth/me").json()["signed_in"]
      and not other.get("/api/auth/me").json()["signed_in"])
r = anon.post("/api/account/password", json={"current_password": "x", "new_password": pw()}, headers=SAME)
check("anonymous change-password -> 401 not_signed_in", r.status_code == 401 and r.json()["error"]["code"] == "not_signed_in")

# --- 10. logout / login / backoff -------------------------------------------------------------
old_cookie = client.cookies.get(users.SESSION_COOKIE)
r = client.post("/api/auth/logout", headers=auth_h)
check("logout ok and clears the cookie", r.status_code == 200 and "Max-Age=0" in r.headers.get("set-cookie", ""))
check("logout deleted the session server-side", users.resolve_session(old_cookie) is None)
replay = TestClient(webapp.app, base_url=f"http://{HOST}")
replay.cookies.set(users.SESSION_COOKIE, old_cookie)
check("the old cookie replayed is anonymous", replay.get("/api/auth/me").json()["signed_in"] is False)
r = replay.post("/api/install-config", json={"copyright_holder": "Replay"}, headers=SAME)
check("a dead cookie is treated as anonymous (no CSRF needed, actor owner-ui)", r.status_code == 200)
r = client.post("/api/auth/login", json={"username": admin_name, "password": "wrong-password-1"}, headers=SAME)
check("wrong password -> 401 invalid_login", r.status_code == 401 and r.json()["error"]["code"] == "invalid_login")
r = client.post("/api/auth/login", json={"username": "nobody-here", "password": "wrong-password-1"}, headers=SAME)
check("unknown user -> the same 401", r.status_code == 401 and r.json()["error"]["code"] == "invalid_login")
users.limiter.reset()
for i in range(users.LOGIN_FREE_ATTEMPTS):
    client.post("/api/auth/login", json={"username": admin_name, "password": f"wrong-password-{i}"}, headers=SAME)
r = client.post("/api/auth/login", json={"username": admin_name, "password": admin_pw2}, headers=SAME)
check("after repeated failures even the right password backs off (429 too_many_attempts)",
      r.status_code == 429 and r.json()["error"]["code"] == "too_many_attempts"
      and r.json()["error"]["details"]["retry_after"] >= 1, r.text[:200])
# timing, with an injected clock: per-username and per-IP keys
users.limiter.reset()
t0 = 1_000_000.0
for i in range(users.LOGIN_FREE_ATTEMPTS):
    code_of(users.authenticate, admin_name, "bad-password-x", "10.9.9.9", now=t0 + i)
last = t0 + users.LOGIN_FREE_ATTEMPTS - 1
check("backoff: blocked right after", code_of(users.authenticate, admin_name, admin_pw2, "10.9.9.8", now=last + 1) == "too_many_attempts")
check("backoff: per-IP too (another username, same IP)",
      code_of(users.authenticate, ed_name, new_ed_pw, "10.9.9.9", now=last + 1) == "too_many_attempts")
check("backoff: allowed once the delay passed", code_of(users.authenticate, admin_name, admin_pw2, "10.9.9.8",
                                                       now=last + 3) is None)
check("backoff: a good sign-in from the IP clears the IP's count",
      code_of(users.authenticate, ed_name, new_ed_pw, "10.9.9.9", now=last + 3) is None
      and users.limiter.retry_after([("ip", "10.9.9.9")], now=last + 3) == 0)
users.limiter.reset()
r = client.post("/api/auth/login", json={"username": admin_name.upper(), "password": admin_pw2}, headers=SAME)
check("sign in again (username any case)", r.status_code == 200)

# --- 11. sliding expiry -----------------------------------------------------------------------
tok = client.cookies.get(users.SESSION_COOKIE)
s = users.resolve_session(tok)
later = s["last_seen"] + users.SESSION_TOUCH_SECONDS + 5
s2 = users.resolve_session(tok, now=later)
check("sliding expiry: touched after the interval", s2 and s2["refreshed"] and s2["expires_at"] == later + users.SESSION_SECONDS)
check("expired session is dead", users.resolve_session(tok, now=s2["expires_at"] + 1) is None)

# --- 12. reset script -----------------------------------------------------------------------
with actor.acting_as(actor.ACTOR_SCRIPT):
    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        rc = reset_password.main([ed_name, "--generate"])
    m = re.search(r"store it now\): (\S+)", out.getvalue())
    check("reset script: generated password works", rc == 0 and m and users.verify_password(
        m.group(1), db.get_user_password_hash(ed_id)))
    if m:
        SECRETS.append(m.group(1))
    out = io.StringIO()
    rescue = "rescue_" + secrets.token_hex(3)
    with contextlib.redirect_stdout(out):
        rc = reset_password.main(["--create-admin", rescue, "--generate"])
    u = db.get_user(username=rescue)
    check("reset script: --create-admin makes an admin", rc == 0 and u and u["role"] == "admin")
    m = re.search(r"store it now\): (\S+)", out.getvalue())
    if m:
        SECRETS.append(m.group(1))
    pw_rows = conn.execute("SELECT actor FROM audit_log WHERE op = 'user_set_password' ORDER BY id DESC LIMIT 1").fetchone()
    check("reset script logs as actor script", pw_rows and pw_rows[0] == "script")
check("deleting a non-last admin works", client.post(f"/api/users/{u['id']}/delete", headers={
    **SAME, "X-CSRF-Token": csrf_from(client.get("/").text)}).status_code == 200)

# --- 13. no secrets in the audit log ----------------------------------------------------------
dump = "\n".join(str(r) for r in conn.execute("SELECT * FROM audit_log"))
check("audit log holds no password", not any(s_ in dump for s_ in SECRETS))
check("audit log holds no hash", "scrypt$" not in dump and "password_hash" not in dump)
check("audit log holds no session or CSRF token", tok not in dump and old_cookie not in dump and token not in dump)
login_bodies = conn.execute("SELECT form_body FROM audit_log WHERE path IN ('/api/auth/login', '/api/auth/setup', "
                            "'/api/account/password', '/api/users') OR path LIKE '/api/users/%/password'").fetchall()
check("password routes log no body values", login_bodies and all("not logged" in b[0] or b[0] == "{}" for b in login_bodies),
      login_bodies[:3])
pw_log = conn.execute("SELECT form_body FROM audit_log WHERE op = 'user_set_password'").fetchall()
check("password change logged as 'password_changed', no hash", pw_log and all('"password_changed": true' in b[0]
                                                                            and "scrypt" not in b[0] for b in pw_log))
conn.close()

print()
print(f"{len(FAILS)} failure(s)" if FAILS else "ALL PASS")
sys.exit(1 if FAILS else 0)
