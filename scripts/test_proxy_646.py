#!/usr/bin/env python3
"""Self-contained check for #646: trusted reverse proxies (web/proxy_trust.py).

Throwaway DB/storage/exports (scripts/_testenv.py), the real FastAPI app through TestClient, whose
`client=` argument plays the TCP peer. Run anywhere the requirements are installed:

    python scripts/test_proxy_646.py

The trust matrix:
  * no CONSTRUCTICON_TRUSTED_PROXIES: forged X-Forwarded-Proto / X-Forwarded-For change nothing
    (cookie not Secure, no HSTS, limiter key and audit IP = the TCP peer, the origin guard compares
    against http and refuses an https Origin with a hint about the setting);
  * a trusted peer saying https: cookie Secure, HSTS when CONSTRUCTICON_HSTS_SECONDS is set (and not
    when it isn't), limiter key and audit IP = the right-most untrusted X-Forwarded-For entry (never
    the spoofed left entries, never the proxy), the origin guard accepts the https origin and refuses
    the http one and other hosts, Starlette's absolute redirects use https, /login?next stays relative;
  * an untrusted peer forging the same headers while a proxy IS configured: nothing changes;
  * X-Forwarded-For chains (spoofed left entries, trusted hops on the right, all trusted, garbage,
    ports, IPv6), X-Forwarded-Proto values (http, junk, multi-value), bad setting entries;
  * a static scan: nothing under web/ or core/ reads the client address, the scheme or a forwarded
    header except web/proxy_trust.py.
Exits 1 if any check fails.
"""
import os
import re
import secrets
import sys
import types

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
import _testenv  # noqa: E402
TMP = _testenv.isolate("proxy646-")
os.environ["CAPTION_DISABLED"] = "1"
for _k in ("CONSTRUCTICON_HSTS_SECONDS", "CONSTRUCTICON_TRUSTED_PROXIES", "CONSTRUCTICON_ALLOWED_ORIGINS"):
    os.environ.pop(_k, None)
ROOT = os.path.abspath(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
sys.path.insert(0, ROOT)
try:
    import cairosvg  # noqa: F401
except Exception:  # no libcairo on this machine: nothing here draws an SVG
    sys.modules["cairosvg"] = types.ModuleType("cairosvg")

from core import actor, db, users  # noqa: E402
_testenv.assert_isolated()

FAILS = []


def check(name, cond, extra=""):
    print(("PASS " if cond else "FAIL ") + name + (f"  [{extra}]" if (extra and not cond) else ""))
    if not cond:
        FAILS.append(name)


db.init_db()
from fastapi.testclient import TestClient  # noqa: E402
from web import app as webapp, proxy_trust  # noqa: E402

HOST = "app.example.com"
PROXY = "10.20.0.5"         # the reverse proxy's address as the app sees it
OUTSIDER = "192.168.7.50"   # a client that reaches the app directly
REAL = "203.0.113.7"        # the real client, as the proxy reports it
SPOOF = "6.6.6.6"           # what a client writes at the left of X-Forwarded-For
FORGED = {"X-Forwarded-Proto": "https", "X-Forwarded-For": f"{SPOOF}, {REAL}"}

ADMIN, ADMIN_PW = "proxyadmin", "pw-" + secrets.token_urlsafe(12)
with actor.acting_as(actor.ACTOR_SCRIPT):
    users.create_first_admin(ADMIN, ADMIN_PW)


def peer(addr, token=False):
    kw = dict(base_url=f"http://{HOST}", client=(addr, 40000), raise_server_exceptions=False,
              follow_redirects=False)
    return _testenv.client(webapp.app, **kw) if token else TestClient(webapp.app, **kw)


def set_env(name, value):
    if value is None:
        os.environ.pop(name, None)
    else:
        os.environ[name] = value


def login(c, headers, password=ADMIN_PW):
    c.cookies.clear()  # a live session cookie would make this POST need the CSRF header
    return c.post("/api/auth/login", json={"username": ADMIN, "password": password}, headers=headers)


def session_cookie(r):
    return [v for v in r.headers.get_list("set-cookie") if v.startswith(users.SESSION_COOKIE + "=")]


def last_audit_ip(path="/api/auth/login"):
    conn = db.get_conn()
    try:
        row = conn.execute("SELECT client_ip FROM audit_log WHERE path = ? ORDER BY id DESC LIMIT 1",
                           (path,)).fetchone()
        return row["client_ip"] if row else "<no row>"
    finally:
        conn.close()


def ip_keys():
    return sorted(k[1] for k in users.limiter._fails if k[0] == "ip")


def failed_login_ip(c, headers):
    """One wrong-password sign-in; returns (status, the limiter's ip keys, the audit row's ip)."""
    users.limiter.reset()
    r = login(c, headers, password="wrong-" + secrets.token_urlsafe(6))
    keys = ip_keys()
    users.limiter.reset()
    return r.status_code, keys, last_audit_ip()


# --- the helpers on their own ---------------------------------------------------------------
print("== helpers")
nets = proxy_trust._parse("10.20.0.5, 172.30.0.0/24, ::1, not-an-ip, 300.1.1.1, *")
check("bad entries are skipped, good ones kept", [str(n) for n in nets] == ["10.20.0.5/32", "172.30.0.0/24", "::1/128"],
      str(nets))
check("empty setting trusts nothing", proxy_trust._parse("") == () and proxy_trust._parse(" , ") == ())
check("is_trusted: inside a CIDR", proxy_trust.is_trusted("172.30.0.77", nets))
check("is_trusted: outside", not proxy_trust.is_trusted("172.30.1.1", nets))
check("is_trusted: IPv4-mapped IPv6", proxy_trust.is_trusted("::ffff:10.20.0.5", nets))
check("is_trusted: not an address", not proxy_trust.is_trusted("testclient", nets))
check("is_trusted: no networks", not proxy_trust.is_trusted("10.20.0.5", ()))
cf = proxy_trust.client_from_forwarded_for
check("XFF: right-most untrusted wins over spoofed left entries", cf([f"{SPOOF}, 1.1.1.1, {REAL}"], nets) == REAL)
check("XFF: trusted hops on the right are skipped", cf([f"{SPOOF}, {REAL}, 172.30.0.9, 10.20.0.5"], nets) == REAL)
check("XFF: several header lines are one list", cf([SPOOF, f"{REAL}, 172.30.0.9"], nets) == REAL)
check("XFF: all trusted -> the left-most", cf(["172.30.0.3, 10.20.0.5"], nets) == "172.30.0.3")
check("XFF: garbage stops the walk (no made-up key)", cf([f"{REAL}, garbage"], nets) is None)
check("XFF: garbage left of the client is never reached", cf([f"garbage, {REAL}"], nets) == REAL)
check("XFF: ports and brackets are stripped", cf(["203.0.113.7:5555"], nets) == REAL
      and cf(["[2001:db8::1]:443"], nets) == "2001:db8::1")
check("XFF: empty", cf([""], nets) is None)

# --- the ASGI rewrite on its own --------------------------------------------------------------
print("== middleware")
seen = {}


async def _probe(scope, receive, send):
    seen["scheme"], seen["client"] = scope["scheme"], scope.get("client")


def rewrite(peer_addr, headers, scheme="http", typ="http"):
    import asyncio
    seen.clear()
    scope = {"type": typ, "scheme": scheme, "client": (peer_addr, 1),
             "headers": [(k.lower().encode(), v.encode()) for k, v in headers]}
    asyncio.run(proxy_trust.ProxyHeadersMiddleware(_probe)(scope, None, None))
    return seen["scheme"], seen["client"][0]


set_env(proxy_trust.ENV, PROXY)
check("trusted: https + client", rewrite(PROXY, FORGED.items()) == ("https", REAL))
check("trusted: X-Forwarded-Proto http keeps http", rewrite(PROXY, [("X-Forwarded-Proto", "http")]) == ("http", PROXY))
check("trusted: junk proto ignored", rewrite(PROXY, [("X-Forwarded-Proto", "javascript")]) == ("http", PROXY))
check("trusted: multi-value proto, right-most wins", rewrite(PROXY, [("X-Forwarded-Proto", "http, https")])[0] == "https")
check("trusted: websocket gets wss", rewrite(PROXY, [("X-Forwarded-Proto", "https")], scheme="ws", typ="websocket")[0] == "wss")
check("untrusted: nothing changes", rewrite(OUTSIDER, FORGED.items()) == ("http", OUTSIDER))
check("trusted: a https connection is not downgraded by a missing header", rewrite(PROXY, [], scheme="https") == ("https", PROXY))
set_env(proxy_trust.ENV, None)
check("no setting: nothing changes, even from loopback", rewrite("127.0.0.1", FORGED.items()) == ("http", "127.0.0.1"))

# --- the app: no setting (the home install) -----------------------------------------------------
print("== no CONSTRUCTICON_TRUSTED_PROXIES (home default)")
set_env(proxy_trust.ENV, None)
set_env("CONSTRUCTICON_HSTS_SECONDS", "31536000")
c = peer(PROXY)
r = login(c, {"Origin": f"http://{HOST}", **FORGED})
check("sign-in works", r.status_code == 200, r.text[:200])
ck = session_cookie(r)
check("cookie has no Secure flag despite X-Forwarded-Proto: https", ck and "Secure" not in ck[0], str(ck))
check("no HSTS despite X-Forwarded-Proto: https", "strict-transport-security" not in r.headers)
status, keys, audit_ip = failed_login_ip(c, {"Origin": f"http://{HOST}", **FORGED})
check("failed sign-in is 401", status == 401, str(status))
check("limiter key = the TCP peer, not X-Forwarded-For", keys == [PROXY], str(keys))
check("audit client_ip = the TCP peer", audit_ip == PROXY, str(audit_ip))
r = login(c, {"Origin": f"https://{HOST}", **FORGED})
check("origin guard compares against http: an https Origin is refused", r.status_code == 403
      and r.json()["error"]["code"] == "cross_origin", r.text[:200])
check("...with a hint about CONSTRUCTICON_TRUSTED_PROXIES", "CONSTRUCTICON_TRUSTED_PROXIES" in r.json()["error"]["message"])
r = login(c, {"Origin": "http://evil.example"})
check("a cross-site Origin gets no proxy hint", r.status_code == 403
      and "TRUSTED_PROXIES" not in r.json()["error"]["message"])
r = login(c, {"Referer": f"http://{HOST}/login"})
check("same-origin Referer (http) still passes", r.status_code == 200, r.text[:200])

# --- the app: a trusted proxy ----------------------------------------------------------------
print("== CONSTRUCTICON_TRUSTED_PROXIES set, request through the proxy")
set_env(proxy_trust.ENV, f"{PROXY}, 172.30.0.0/24")
via = peer(PROXY)
r = login(via, {"Origin": f"https://{HOST}", **FORGED})
check("sign-in over https via the proxy works", r.status_code == 200, r.text[:200])
ck = session_cookie(r)
check("cookie is Secure", ck and "Secure" in ck[0].split("; "), str(ck))
check("cookie keeps HttpOnly, SameSite=Lax", ck and "HttpOnly" in ck[0] and "SameSite=Lax" in ck[0])
check("HSTS sent (CONSTRUCTICON_HSTS_SECONDS set)", r.headers.get("strict-transport-security") == "max-age=31536000",
      str(r.headers.get("strict-transport-security")))
set_env("CONSTRUCTICON_HSTS_SECONDS", None)
r = login(via, {"Origin": f"https://{HOST}", **FORGED})
check("no HSTS when CONSTRUCTICON_HSTS_SECONDS is unset", r.status_code == 200
      and "strict-transport-security" not in r.headers)
set_env("CONSTRUCTICON_HSTS_SECONDS", "31536000")
status, keys, audit_ip = failed_login_ip(via, {"Origin": f"https://{HOST}", **FORGED})
check("limiter key = the real client (right-most untrusted), not the spoof or the proxy", keys == [REAL], str(keys))
check("audit client_ip = the real client", audit_ip == REAL, str(audit_ip))
status, keys, audit_ip = failed_login_ip(via, {"Origin": f"https://{HOST}", "X-Forwarded-Proto": "https",
                                               "X-Forwarded-For": f"{SPOOF}, {REAL}, 172.30.0.9"})
check("a second trusted hop is skipped", keys == [REAL] and audit_ip == REAL, f"{keys} {audit_ip}")
# The per-IP bucket is per client now: five failures from one client don't block another.
users.limiter.reset()
for i in range(6):
    login(via, {"Origin": f"https://{HOST}", "X-Forwarded-Proto": "https", "X-Forwarded-For": "198.51.100.1"},
          password=f"wrong-{i}-" + secrets.token_urlsafe(4))
blocked = [k for k in users.limiter._fails if k == ("ip", "198.51.100.1")]
check("failures are counted against the client behind the proxy", bool(blocked))
check("...and not against the proxy's own address", ("ip", PROXY) not in users.limiter._fails)
users.limiter.reset()
r = login(via, {"Origin": f"http://{HOST}", **FORGED})
check("origin guard: an http Origin is refused on the https site", r.status_code == 403, r.text[:200])
r = login(via, {"Origin": "https://evil.example", **FORGED})
check("origin guard: another https host is refused", r.status_code == 403, r.text[:200])
r = login(via, {"Referer": f"https://{HOST}/login", **FORGED})
check("origin guard: same-origin https Referer passes", r.status_code == 200, r.text[:200])
tok = peer(PROXY, token=True)
r = tok.get("/api/settings/", headers=FORGED)
check("Starlette's absolute redirect uses https (redirect_slashes)", r.status_code in (307, 308)
      and r.headers.get("location", "").startswith(f"https://{HOST}/"), f"{r.status_code} {r.headers.get('location')}")
anon = peer(PROXY)
r = anon.get("/admin", headers=FORGED)
check("anonymous page -> /login?next= (relative, so it keeps https)", r.status_code == 302
      and r.headers.get("location", "").startswith("/login"), f"{r.status_code} {r.headers.get('location')}")

print("== CONSTRUCTICON_TRUSTED_PROXIES set, an outsider forging the headers")
out = peer(OUTSIDER)
r = login(out, {"Origin": f"http://{HOST}", **FORGED})
check("sign-in works (plain http)", r.status_code == 200, r.text[:200])
ck = session_cookie(r)
check("cookie not Secure", ck and "Secure" not in ck[0], str(ck))
check("no HSTS", "strict-transport-security" not in r.headers)
status, keys, audit_ip = failed_login_ip(out, {"Origin": f"http://{HOST}", **FORGED})
check("limiter key = the outsider's own address", keys == [OUTSIDER], str(keys))
check("audit client_ip = the outsider's own address", audit_ip == OUTSIDER, str(audit_ip))
r = login(out, {"Origin": f"https://{HOST}", **FORGED})
check("an https Origin from the outsider is refused (the request was http)", r.status_code == 403)
r = _testenv.client(webapp.app, base_url=f"http://{HOST}", client=(OUTSIDER, 1), follow_redirects=False,
                    raise_server_exceptions=False).get("/api/settings/", headers=FORGED)
check("outsider's redirect stays http", r.headers.get("location", "").startswith(f"http://{HOST}/"),
      str(r.headers.get("location")))

print("== a real HTTPS connection (no proxy) is unchanged")
set_env(proxy_trust.ENV, None)
direct = TestClient(webapp.app, base_url=f"https://{HOST}", client=(OUTSIDER, 1), raise_server_exceptions=False)
r = login(direct, {"Origin": f"https://{HOST}"})
check("direct https: sign-in, Secure cookie, HSTS", r.status_code == 200 and "Secure" in session_cookie(r)[0]
      and "strict-transport-security" in r.headers, r.text[:200])
set_env("CONSTRUCTICON_HSTS_SECONDS", None)

# --- nobody else reads the address, the scheme or a forwarded header ------------------------------
print("== static scan")
PATTERNS = [re.compile(p, re.I) for p in (r"request\.client\b", r"scope(\.get\(|\[)\s*[\"']client[\"']",
                                          r"scope(\.get\(|\[)\s*[\"']scheme[\"']", r"[\"']x-forwarded",
                                          r"[\"']forwarded[\"']", r"\.url\.scheme",
                                          r"forwarded_allow_ips|proxy_headers")]
offenders = []
for top in ("web", "core"):
    for dirpath, _, files in os.walk(os.path.join(ROOT, top)):
        for f in files:
            if not f.endswith(".py"):
                continue
            path = os.path.join(dirpath, f)
            rel = os.path.relpath(path, ROOT).replace("\\", "/")
            if rel == "web/proxy_trust.py":
                continue
            with open(path, encoding="utf-8") as fh:
                for n, line in enumerate(fh, 1):
                    code = line.split("#", 1)[0]
                    if any(p.search(code) for p in PATTERNS):
                        offenders.append(f"{rel}:{n}: {line.strip()[:100]}")
check("only web/proxy_trust.py reads client / scheme / X-Forwarded-*", not offenders, "; ".join(offenders[:5]))

print()
if FAILS:
    print(f"FAILED: {', '.join(FAILS)}")
    sys.exit(1)
print("ALL PASS")
