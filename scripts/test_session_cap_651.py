#!/usr/bin/env python3
"""#651: the absolute session cap. Throwaway DB, no server, fake clocks (no sleeping).

    python scripts/test_session_cap_651.py

A session ends users.SESSION_MAX_AGE_DAYS after its sign-in (sessions.created_at), however often it
is used; the sliding refresh never pushes expires_at past that. Covers: a session refreshed
continuously still dies at created + cap; the refresh clamps expires_at to the cap (shown with a
sliding window shorter than the cap, so the clamp is visible); a fresh sign-in gets a new full
window; rows written before the cap (expires_at reaching past it) obey it too and are clamped on
their next touch; past-cap and idle-expired rows are purged at the next sign-in; over HTTP a
past-cap cookie is refused exactly like an idle-expired one (page 302 to /login, API 401), and the
cookie's Max-Age follows the server-side expiry (sign-in and a clamped refresh).
Exits 1 if any check fails.
"""

import hashlib
import os
import re
import secrets
import sys
import time

import _testenv  # noqa: E402  (scripts/_testenv.py: temp DB + storage + exports, refuses otherwise)
TMP = _testenv.isolate("cap651-")
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, ".."))
sys.path.insert(0, HERE)

from starlette.testclient import TestClient  # noqa: E402

from core import actor, db, users  # noqa: E402
_testenv.assert_isolated()
from web import app as webapp  # noqa: E402

FAILS = []
HOST = "testhost.local"
BASE = f"http://{HOST}"
SAME = {"Origin": BASE}
DAY = 24 * 3600
CAP = users.SESSION_MAX_AGE_SECONDS


def check(name, cond, extra=""):
    print(("PASS " if cond else "FAIL ") + name + (f"  [{extra}]" if (extra and not cond) else ""))
    if not cond:
        FAILS.append(name)


def th(token):
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def max_age_of(set_cookie):
    m = re.search(r"Max-Age=(\d+)", set_cookie or "")
    return int(m.group(1)) if m else None


def session_cookie_header(r):
    for v in r.headers.get_list("set-cookie"):
        if v.startswith(f"{users.SESSION_COOKIE}="):
            return v
    return None


def new_client(token=None):
    c = TestClient(webapp.app, base_url=BASE, follow_redirects=False)
    if token:
        c.cookies.set(users.SESSION_COOKIE, token)
    return c


db.init_db()
users.limiter.reset()
check("the cap is 30 days (owner decision on #651)", users.SESSION_MAX_AGE_DAYS == 30 and CAP == 30 * DAY)

name, password = "cap_" + secrets.token_hex(3), "t-" + secrets.token_urlsafe(14)
with actor.acting_as(actor.ACTOR_SCRIPT):
    users.create_user(name, password, "admin")
user = db.get_user(username=name)

# --- 1. used every day, still dies at created + cap ------------------------------------------
print("--- 1. continuous use ---")
T0 = 1_800_000_000.0
s = users.start_session(user, now=T0)
check("sign-in: expires at created + cap", s["expires_at"] == T0 + CAP, s["expires_at"] - T0)
check("sign-in: cookie Max-Age = remaining life", s["max_age"] == CAP)
tok = s["token"]
over = []
t = T0
while t < T0 + CAP - DAY:
    t += 6 * 3600 + 1  # every six hours, always past SESSION_TOUCH_SECONDS
    r = users.resolve_session(tok, now=t)
    if r is None or r["expires_at"] > T0 + CAP or r["max_age"] != int(r["expires_at"] - t):
        over.append((t - T0) / DAY)
check("refreshed every 6 h for ~29 days: alive, expiry never past created + cap, max_age consistent",
      not over, over[:3])
check("still alive one second before the cap", users.resolve_session(tok, now=T0 + CAP - 1) is not None)
check("dead at the cap", users.resolve_session(tok, now=T0 + CAP) is None)
check("dead after the cap", users.resolve_session(tok, now=T0 + CAP + 3600) is None)
check("the row is unchanged by the refused read (expires_at still <= cap)",
      db.get_session(th(tok), T0) and db.get_session(th(tok), T0)["expires_at"] <= T0 + CAP)

# --- 2. the refresh clamps (sliding window shorter than the cap) -----------------------------
print("--- 2. the clamp ---")
saved = users.SESSION_SECONDS
users.SESSION_SECONDS = 7 * DAY
try:
    T1 = T0 + 100 * DAY
    s = users.start_session(user, now=T1)
    check("short window: sign-in expires at now + window", s["expires_at"] == T1 + 7 * DAY)
    r = users.resolve_session(s["token"], now=T1 + 5 * DAY)
    check("short window: refresh at +5 d slides to +12 d", r and r["refreshed"] and r["expires_at"] == T1 + 12 * DAY)
    check("short window: the row agrees", db.get_session(th(s["token"]), T1)["expires_at"] == T1 + 12 * DAY)
    t = T1 + 5 * DAY
    while t < T1 + 25 * DAY:
        t += 3 * DAY
        users.resolve_session(s["token"], now=t)
    r = users.resolve_session(s["token"], now=T1 + 27 * DAY)
    check("refresh at +27 d: clamped to created + cap (not +34 d)", r and r["expires_at"] == T1 + CAP,
          r and (r["expires_at"] - T1) / DAY)
    check("clamped: max_age is the remaining 3 days", r and r["max_age"] == 3 * DAY)
    r2 = users.resolve_session(s["token"], now=T1 + 28 * DAY)
    check("a later touch that can't move the expiry: refreshed False (no cookie re-send)",
          r2 and not r2["refreshed"] and r2["expires_at"] == T1 + CAP)
    check("short window: dead at the cap", users.resolve_session(s["token"], now=T1 + CAP) is None)
    # an idle session still dies by the sliding window, before the cap
    s = users.start_session(user, now=T1)
    check("idle: dead after the sliding window", users.resolve_session(s["token"], now=T1 + 7 * DAY + 1) is None)
finally:
    users.SESSION_SECONDS = saved

# --- 3. a fresh sign-in gets a new full window -----------------------------------------------
print("--- 3. fresh sign-in ---")
T2 = T0 + CAP + 10
s = users.start_session(user, now=T2)
check("new sign-in after the cap: full window again", s["expires_at"] == T2 + CAP and s["max_age"] == CAP)
check("new sign-in: alive at +29 d", users.resolve_session(s["token"], now=T2 + 29 * DAY) is not None)

# --- 4. rows from before the cap -------------------------------------------------------------
print("--- 4. pre-cap rows ---")
T3 = T0 + 400 * DAY
legacy = secrets.token_urlsafe(32)
# Written by the old code: slid at T3 + 25 d, so expires_at = T3 + 55 d, well past created + cap.
db._insert_session(th(legacy), user["id"], "csrf-x", T3, T3 + 55 * DAY)
r = users.resolve_session(legacy, now=T3 + 27 * DAY)
check("pre-cap row: alive before the cap", r is not None)
check("pre-cap row: its next touch clamps expires_at down to created + cap, and re-sends the cookie",
      r and r["refreshed"] and r["expires_at"] == T3 + CAP and r["max_age"] == 3 * DAY)
legacy2 = secrets.token_urlsafe(32)
db._insert_session(th(legacy2), user["id"], "csrf-y", T3, T3 + 55 * DAY)
check("pre-cap row never touched again: refused at the cap although expires_at is later",
      users.resolve_session(legacy2, now=T3 + CAP + 1) is None)

# --- 5. purge ---------------------------------------------------------------------------------
print("--- 5. purge ---")
idle = secrets.token_urlsafe(32)
db._insert_session(th(idle), user["id"], "csrf-z", T3 + 40 * DAY, T3 + 41 * DAY)  # idle-expired at T3+50d
fresh = secrets.token_urlsafe(32)
db._insert_session(th(fresh), user["id"], "csrf-w", T3 + 45 * DAY, T3 + 75 * DAY)  # live at T3+50d
users.start_session(user, now=T3 + 50 * DAY)
left = {r[0] for r in db.get_conn().execute("SELECT token_hash FROM sessions").fetchall()}
check("sign-in purges the past-cap row (expires_at still in the future)", th(legacy2) not in left)
check("sign-in purges the idle-expired row", th(idle) not in left)
check("sign-in keeps a live row", th(fresh) in left)

# --- 6. over HTTP, with the real clock --------------------------------------------------------
print("--- 6. HTTP ---")
db.get_conn().execute("DELETE FROM sessions").connection.commit()
users.limiter.reset()
c = new_client()
r = c.post("/api/auth/login", json={"username": name, "password": password}, headers=SAME)
ma = max_age_of(session_cookie_header(r))
check("login: cookie Max-Age = the server-side expiry (30 days)", r.status_code == 200 and ma == CAP, ma)
row = db.get_conn().execute("SELECT created_at, expires_at FROM sessions").fetchone()
check("login: row expires at created + cap", row and row[1] == row[0] + CAP)

now = time.time()
past_cap = secrets.token_urlsafe(32)
db._insert_session(th(past_cap), user["id"], "csrf-a", now - 31 * DAY, now + 20 * DAY)  # pre-cap shape
idle_dead = secrets.token_urlsafe(32)
db._insert_session(th(idle_dead), user["id"], "csrf-b", now - 2 * DAY, now - 60)
answers = {}
for label, tk in (("past the cap", past_cap), ("idle-expired", idle_dead)):
    cl = new_client(tk)
    page = cl.get("/admin")
    api = cl.get("/api/projects")
    me = cl.get("/api/auth/me")
    answers[label] = (page.status_code, page.headers.get("location"), api.status_code,
                      (api.json().get("error") or {}).get("code"), me.json().get("user"))
    check(f"{label}: page -> 302 /login?next=/admin", page.status_code == 302
          and page.headers.get("location") == "/login?next=%2Fadmin", f"{page.status_code} {page.headers.get('location')}")
    check(f"{label}: API -> 401 unauthorized", api.status_code == 401 and answers[label][3] == "unauthorized")
check("past-cap and idle-expired answers are identical", answers["past the cap"] == answers["idle-expired"],
      answers)

clamp = secrets.token_urlsafe(32)
db._insert_session(th(clamp), user["id"], "csrf-c", now - 20 * DAY, now + 25 * DAY)
db.get_conn().execute("UPDATE sessions SET last_seen = ? WHERE token_hash = ?",
                      (now - 3600, th(clamp))).connection.commit()
r = new_client(clamp).get("/api/auth/me")
ma = max_age_of(session_cookie_header(r))
check("pre-cap live row over HTTP: signed in, cookie re-sent with Max-Age ~10 days (not 25)",
      r.status_code == 200 and (r.json().get("user") or {}).get("username") == name
      and ma is not None and abs(ma - 10 * DAY) < 120, ma)

print(f"{len(FAILS)} failure(s)" if FAILS else "ALL PASS")
if FAILS:
    print("FAILED: " + ", ".join(FAILS))
    sys.exit(1)
