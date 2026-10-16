#!/usr/bin/env python3
"""Sign-in lockout (#647): "lock the attacker, not the account". Throwaway DB, no server, no sleeping.

    python scripts/test_lockout_647.py

Drives `users.authenticate()` and `users.LoginLimiter` with fake `now` values. Covers: an attacker
address hammering the admin's username gets escalating 429s (the heavy per-IP curve, capped at 15
minutes); meanwhile the real admin signs in from a clean address with the right password; the
username's delay never exceeds its cap; an address spraying many usernames is throttled; a wrong
password from a clean address still counts; someone who can sign in as themselves can't wash their
address clean to guess another account freely; no 429 checks the password (no timing oracle);
the audit log gets one row per throttle episode, never one per attempt, and never a password;
ip=None can't bypass the throttle. Every credential is generated per run. Exits 1 if any check fails.
"""

import json
import os
import secrets
import sys

import _testenv  # noqa: E402  (scripts/_testenv.py: temp DB + storage + exports, refuses otherwise)
TMP = _testenv.isolate("lockout647-")
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, ".."))
sys.path.insert(0, HERE)

from core import actor, db, users  # noqa: E402
from core.errors import AppError  # noqa: E402
_testenv.assert_isolated()

FAILS = []
L = users.limiter
CAP_IP = users.LOGIN_MAX_DELAY_SECONDS
CAP_USER = users.LOGIN_USER_MAX_DELAY_SECONDS
FREE = users.LOGIN_FREE_ATTEMPTS


def check(name, cond, extra=""):
    print(("PASS " if cond else "FAIL ") + name + (f"  [{extra}]" if (extra and not cond) else ""))
    if not cond:
        FAILS.append(name)


def pw():
    return "t-" + secrets.token_urlsafe(12)


def attempt(username, password, ip, now):
    """(code or None, retry_after or None) of one sign-in."""
    try:
        users.authenticate(username, password, ip, now=now)
    except AppError as e:
        return e.code, (e.details or {}).get("retry_after")
    return None, None


def wrong():
    return "wrong-" + secrets.token_urlsafe(8)


def throttle_rows():
    conn = db.get_conn()
    try:
        rows = conn.execute("SELECT actor, form_body FROM audit_log WHERE op = ? ORDER BY id",
                            (users.OP_LOGIN_THROTTLED,)).fetchall()
        return [(r["actor"], json.loads(r["form_body"])) for r in rows]
    finally:
        conn.close()


db.init_db()
L.reset()
ADMIN, ADMIN_PW = "boss-" + secrets.token_hex(3), pw()
EVE, EVE_PW = "eve-" + secrets.token_hex(3), pw()
SECRETS = [ADMIN_PW, EVE_PW]
with actor.acting_as(actor.ACTOR_SCRIPT):
    users.create_user(ADMIN, ADMIN_PW, "admin")
    users.create_user(EVE, EVE_PW, "viewer")

ATTACKER, REAL, OTHER = "203.0.113.66", "198.51.100.7", "198.51.100.8"
T0 = 2_000_000.0

with actor.acting_as(actor.ACTOR_ANONYMOUS):
    # --- 1. the attacker hammers the admin's username: escalating 429s ---------------------------
    print("== attacker address hammering the admin")
    now = T0
    for i in range(FREE):
        code, _ = attempt(ADMIN, wrong(), ATTACKER, now)
        check(f"free attempt {i + 1} is a plain 401", code == "invalid_login", code)
        now += 1
    last_fail = now - 1
    code, retry = attempt(ADMIN, wrong(), ATTACKER, now)
    check("the next attempt from the attacker is 429 too_many_attempts", code == "too_many_attempts", code)
    code, _ = attempt(ADMIN, ADMIN_PW, ATTACKER, now)
    check("even the RIGHT password from the throttled attacker address is 429 (not checked)",
          code == "too_many_attempts", code)
    # Keep going, each time waiting out the delay exactly: the per-IP delay climbs to the cap.
    # 10 delays: counts 5..14 -> 2, 4, ... 512, then the 900 s cap. (Not waiting out the last one:
    # at the cap the delay equals the 15-minute window, so the key would expire as it ends.)
    delays = []
    for k in range(10):
        wait = L.retry_after([("ip", ATTACKER)], now=last_fail)
        delays.append(round(wait))
        if k == 9:
            break
        now = last_fail + wait + 0.01
        code, _ = attempt(ADMIN, wrong(), ATTACKER, now)
        if code == "too_many_attempts":  # the username delay (<= 5 s) can also apply; wait that out
            now += L.retry_after([("user", ADMIN.lower())], now=now) + 0.01
            code, _ = attempt(ADMIN, wrong(), ATTACKER, now)
        check_ok = code == "invalid_login"
        if not check_ok:
            check("a wrong attempt after waiting out the delay is checked (401)", False, code)
            break
        last_fail = now
    check("the attacker's delays escalate", all(b >= a for a, b in zip(delays, delays[1:])) and delays[-1] > delays[0],
          str(delays))
    check("the attacker's delay reaches minutes and stops at the 15-minute cap",
          max(delays) == CAP_IP and delays[0] == 2, str(delays))
    code, retry = attempt(ADMIN, wrong(), ATTACKER, last_fail + 1)
    check("429 carries retry_after near the cap", code == "too_many_attempts" and CAP_IP - 1 <= retry <= CAP_IP + 1,
          f"{code} {retry}")

    # --- 2. meanwhile the real admin, from a clean address, signs in -----------------------------
    print("== the real admin from a clean address")
    t = last_fail + 1
    check("the admin's username is itself backing off right now", L.retry_after([("user", ADMIN.lower())], now=t) > 0
          or L.has_failures([("user", ADMIN.lower())], now=t))
    code, _ = attempt(ADMIN, ADMIN_PW, REAL, t)
    check("the real admin signs in from a clean address during the attack", code is None, code)
    code, _ = attempt(ADMIN, ADMIN_PW, ATTACKER, t + 1)
    check("...while the attacker address is still refused, right password or not", code == "too_many_attempts", code)

    # --- 3. the username delay is bounded ---------------------------------------------------------
    print("== the username delay never exceeds its cap")
    L.reset()
    t = T0 + 10_000
    for n in range(200):  # failures spread over many addresses, each a fresh (clean) one
        attempt(ADMIN, wrong(), f"10.47.{n // 250}.{n % 250}", t)
        t += 0.5
    entry = L._fails.get(("user", ADMIN.lower()))
    check("200 failures from 200 addresses all counted against the username", entry and entry[0] == 200,
          str(entry))
    worst = max(L.retry_after([("user", ADMIN.lower())], now=t - 0.5 + dt / 10) for dt in range(0, 100))
    check(f"the username's wait is at most {CAP_USER} s", 0 < worst <= CAP_USER, str(worst))
    # An address that itself has a failure against the admin is held back by the username delay...
    dirty = "10.47.0.1"
    code, retry = attempt(ADMIN, ADMIN_PW, dirty, t)
    check("an address with recent failures waits out the username delay (429)", code == "too_many_attempts", code)
    check("...and that 429 says at most the cap", retry is not None and retry <= CAP_USER + 1, str(retry))
    code, _ = attempt(ADMIN, ADMIN_PW, dirty, t + CAP_USER + 0.01)
    check("...and after at most the cap it is checked again (right password signs in)", code is None, code)
    # ...but a clean address never waits on the username.
    L.reset()
    t = T0 + 20_000
    for n in range(30):
        attempt(ADMIN, wrong(), f"10.48.0.{n}", t)
    code, _ = attempt(ADMIN, ADMIN_PW, OTHER, t)
    check("a clean address ignores the username delay entirely", code is None, code)

    # --- 4. one address spraying many usernames ---------------------------------------------------
    print("== spraying usernames from one address")
    L.reset()
    SPRAY = "192.0.2.50"
    t = T0 + 30_000
    for n in range(FREE):
        code, _ = attempt(f"user{n}-" + secrets.token_hex(2), wrong(), SPRAY, t + n)
        check(f"spray attempt {n + 1} (new username) is a 401", code == "invalid_login", code)
    code, _ = attempt("never-tried-" + secrets.token_hex(2), wrong(), SPRAY, t + FREE)
    check("the sprayer is throttled on its address, whatever username it tries next", code == "too_many_attempts", code)
    code, _ = attempt(EVE, EVE_PW, SPRAY, t + FREE)
    check("...even with a real account's right password (the address is what's locked)", code == "too_many_attempts", code)
    code, _ = attempt(EVE, EVE_PW, OTHER, t + FREE)
    check("...while that account still signs in from elsewhere", code is None, code)

    # --- 5. a wrong password from a clean address still counts -----------------------------------
    print("== a clean address's wrong password counts")
    L.reset()
    t = T0 + 40_000
    code, _ = attempt(ADMIN, wrong(), REAL, t)
    check("wrong password from a clean address -> 401", code == "invalid_login", code)
    check("...counted against the address", L._fails.get(("ip", REAL), (0,))[0] == 1)
    check("...and against the username", L._fails.get(("user", ADMIN.lower()), (0,))[0] == 1)
    check("...so the address is no longer clean", L.has_failures([("ip", REAL)], now=t + 1))
    for i in range(FREE - 1):
        attempt(ADMIN, wrong(), REAL, t + 1 + i)
    code, _ = attempt(ADMIN, ADMIN_PW, REAL, t + FREE)
    check("five wrong from one clean address -> that address backs off like any other", code == "too_many_attempts", code)
    code, _ = attempt(ADMIN, ADMIN_PW, REAL, t + 3600)
    check("a success clears the address", code is None and not L.has_failures([("ip", REAL)], now=t + 3600), code)

    # --- 6. signing in as yourself doesn't wash the address clean for another account -----------
    print("== a user can't reset the username delay by signing in as themselves")
    L.reset()
    t = T0 + 50_000
    for n in range(FREE):  # the admin's username backs off (failures from elsewhere)
        attempt(ADMIN, wrong(), f"10.49.0.{n}", t)
    INSIDER = "10.50.0.9"
    code, _ = attempt(ADMIN, wrong(), INSIDER, t + 0.1)
    check("the insider's guess at the admin from a clean address is checked (401)", code == "invalid_login", code)
    code, _ = attempt(EVE, EVE_PW, INSIDER, t + 0.2)
    check("the insider signs in as themselves (clears their address)", code is None, code)
    code, _ = attempt(ADMIN, wrong(), INSIDER, t + 0.3)
    check("...but their next guess at the admin still waits on the username delay (429)",
          code == "too_many_attempts", code)

    # --- 7. no 429 checks the password (no timing oracle) -----------------------------------------
    print("== a 429 never looks at the password")
    calls = []
    real_verify, real_burn = users.verify_password, users._burn_time
    users.verify_password = lambda *a, **k: calls.append("verify") or real_verify(*a, **k)
    users._burn_time = lambda *a, **k: calls.append("burn") or real_burn(*a, **k)
    try:
        L.reset()
        t = T0 + 60_000
        for n in range(FREE):
            attempt(ADMIN, wrong(), ATTACKER, t + n)
        calls.clear()
        r1 = attempt(ADMIN, ADMIN_PW, ATTACKER, t + FREE)
        r2 = attempt(ADMIN, wrong(), ATTACKER, t + FREE)
        r3 = attempt("no-such-user", wrong(), ATTACKER, t + FREE)
        check("throttled address: right password, wrong password and unknown user answer identically",
              r1[0] == r2[0] == r3[0] == "too_many_attempts" and r1[1] == r2[1] == r3[1], f"{r1} {r2} {r3}")
        check("...and none of them hashed anything", calls == [], str(calls))
        # the username-delay 429 (a dirty address) too
        L.reset()
        for n in range(FREE):
            attempt(ADMIN, wrong(), f"10.51.0.{n}", t)
        attempt(ADMIN, wrong(), "10.51.1.1", t + 0.1)  # this address is now dirty for the admin
        calls.clear()
        r1 = attempt(ADMIN, ADMIN_PW, "10.51.1.1", t + 0.2)
        r2 = attempt(ADMIN, wrong(), "10.51.1.1", t + 0.2)
        check("username-delay 429: right and wrong password answer identically",
              r1[0] == r2[0] == "too_many_attempts" and r1[1] == r2[1], f"{r1} {r2}")
        check("...without hashing", calls == [], str(calls))
        # an unknown username is throttled exactly like a real one (no existence oracle)
        L.reset()
        ghost = "ghost-" + secrets.token_hex(3)
        for n in range(FREE):
            attempt(ghost, wrong(), f"10.52.0.{n}", t)
        attempt(ghost, wrong(), "10.52.1.1", t + 0.1)
        rg = attempt(ghost, wrong(), "10.52.1.1", t + 0.2)
        check("an unknown username backs off exactly like a real one", rg[0] == "too_many_attempts" and rg[1] == r1[1],
              f"{rg} {r1}")
    finally:
        users.verify_password, users._burn_time = real_verify, real_burn

    # --- 8. the audit log: one row per throttle episode ---------------------------------------------
    print("== audit log rows")
    L.reset()
    before = len(throttle_rows())
    t = T0 + 70_000
    for n in range(20):  # the attacker keeps going, waiting out each delay
        now = t
        code, _ = attempt(ADMIN, wrong(), ATTACKER, now)
        if code == "too_many_attempts":
            t += max(L.retry_after([("ip", ATTACKER)], now=t), L.retry_after([("user", ADMIN.lower())], now=t)) + 0.01
            continue
        t += 0.01
    rows = throttle_rows()[before:]
    kinds = sorted(r[1]["throttled"] for r in rows)
    check("20 attempts in one episode -> exactly one 'ip' row and one 'username' row", kinds == ["ip", "username"],
          str(kinds))
    ip_row = next((r for r in rows if r[1]["throttled"] == "ip"), (None, {}))
    user_row = next((r for r in rows if r[1]["throttled"] == "username"), (None, {}))
    check("the ip row names the address", ip_row[1].get("client_ip") == ATTACKER, str(ip_row))
    check("the username row names the attacked real account", user_row[1].get("username") == ADMIN
          and user_row[1].get("username_known") is True, str(user_row))
    check("the rows are by the anonymous requester", {r[0] for r in rows} == {actor.ACTOR_ANONYMOUS}, str(rows))
    # A new episode (the window passed) logs again, once.
    t += users.LOGIN_WINDOW_SECONDS + 1
    for n in range(FREE + 3):
        attempt(ADMIN, wrong(), ATTACKER, t + n)  # the last three are 429s: not counted, not logged
    rows2 = throttle_rows()[before + len(rows):]
    check("after the window, a new episode adds one more row per key", sorted(r[1]["throttled"] for r in rows2)
          == ["ip", "username"], str(rows2))
    # An unknown username's row doesn't store the name (it may be a password typed in the wrong box).
    before = len(throttle_rows())
    L.reset()
    typo = "Pa55-" + secrets.token_urlsafe(10)
    SECRETS.append(typo)
    for n in range(FREE):
        attempt(typo, wrong(), f"10.53.0.{n}", t + 99_999)
    rows3 = throttle_rows()[before:]
    check("an unknown username's throttle row keeps no name", rows3 and all(r[1].get("username") is None
          and r[1].get("username_known") is False for r in rows3), str(rows3))
    conn = db.get_conn()
    dump = "\n".join(r[0] for r in conn.execute("SELECT form_body FROM audit_log").fetchall())
    conn.close()
    check("no password (or unknown-name typo) anywhere in the audit log", not any(s in dump for s in SECRETS))

    # --- 9. ip=None doesn't bypass ---------------------------------------------------------------
    print("== no client address")
    L.reset()
    t = T0 + 90_000
    for n in range(FREE):
        code, _ = attempt(ADMIN, wrong(), None, t + n)
    code, _ = attempt(ADMIN, ADMIN_PW, None, t + FREE)
    check("ip=None failures throttle further ip=None attempts", code == "too_many_attempts", code)
    code, _ = attempt(EVE, wrong(), None, t + FREE)
    check("...whatever the username (one shared 'unknown' address key)", code == "too_many_attempts", code)
    check("...counted under the 'unknown' key", (("ip", users.LOGIN_UNKNOWN_IP) in L._fails))
    code, _ = attempt(ADMIN, ADMIN_PW, REAL, t + FREE)
    check("...while a real address can still sign in", code is None, code)

L.reset()
print()
print(f"{len(FAILS)} failure(s)" if FAILS else "ALL PASS")
sys.exit(1 if FAILS else 0)
