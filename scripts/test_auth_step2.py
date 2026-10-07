#!/usr/bin/env python3
"""Auth step 2 (#467): enforcement on. Throwaway DB, no server.

    python scripts/test_auth_step2.py

The whole matrix, in-process, with real sessions and a per-run install token:
  1. a fresh install (no user): every page -> /setup, the API -> 401, /setup open, /f public,
     /healthz open, the install token still admin;
  2. first admin via /setup; editor and viewer created through the Users API; /setup then 404;
  3. anonymous: pages -> /login?next=..., API 401 (shared shape), /f public except restricted and
     redacted (404), mounts gated (/preview, /docs, /openapi.json), static open, `next` validated;
  4. viewer / editor / admin: pages, editor routes, admin routes, restricted and redacted items on
     every door (/f, /object, item API, item writes, the decision queue);
  5. the install token: admin with no CSRF, actor `token`; wrong token 401 invalid_token (even on
     a public door); other schemes and the old desktop-app header grant nothing; no token
     configured = every Bearer refused;
  6. role_of for every actor; the MCP (role admin, refuses to start with no token); the web
     refusing to start on a broken token config;
  7. the desktop uploader's API module against the app (no token / wrong / right);
  8. no token, password or session in the audit log.
Exits 1 if any check fails.
"""

import os
import re
import secrets
import sqlite3
import subprocess
import sys
import types

import _testenv  # noqa: E402  (scripts/_testenv.py: temp DB + storage + exports, refuses otherwise)
TMP = _testenv.isolate("auth2-")
HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)
sys.path.insert(0, HERE)

from starlette.testclient import TestClient  # noqa: E402

from core import actor, cards, db, install_token, items, membership, paths, policy, roles, users  # noqa: E402
_testenv.assert_isolated()
from web import app as webapp  # noqa: E402
from web.routes import auth as auth_routes  # noqa: E402

FAILS = []
HOST = "testhost.local"
BASE = f"http://{HOST}"
SAME = {"Origin": BASE}
TOKEN = _testenv.TOKEN


def check(name, cond, extra=""):
    print(("PASS " if cond else "FAIL ") + name + (f"  [{extra}]" if (extra and not cond) else ""))
    if not cond:
        FAILS.append(name)


def new_client(**headers):
    return TestClient(webapp.app, base_url=BASE, follow_redirects=False, headers=headers or None)


def err_code(r):
    try:
        body = r.json()
    except ValueError:
        return None
    return body.get("error", {}).get("code") if isinstance(body, dict) and body.get("ok") is False else None


def csrf_of(client):
    m = re.search(r'<meta name="csrf-token" content="([^"]+)"', client.get("/").text)
    return m.group(1) if m else ""


def pw():
    return "t2-" + secrets.token_urlsafe(14)


def make_item(name, media_type="document", body=b"auth step 2 fixture\n", ext="txt"):
    slug = "a2-" + name + "-" + secrets.token_hex(3)
    (paths.storage_dir() / f"{slug}.{ext}").write_bytes(body)
    db.insert_upload(slug, f"{name}.{ext}", f"{slug}.{ext}", "tester", media_type=media_type,
                     description=f"auth2 {name}")
    return slug


db.init_db()
users.limiter.reset()
SECRETS = []

# fixtures (in-process, as a script)
with actor.acting_as(actor.ACTOR_SCRIPT):
    PLAIN = make_item("plain")
    CERT = make_item("cert", media_type="certkey", body=b"-----BEGIN CERTIFICATE-----\nMIIB\n-----END CERTIFICATE-----\n",
                     ext="pem")
    RED = make_item("redacted")
    items.redact(RED)
    CARD = cards.create("Auth Two Card").data["card"]
    membership.add_files(CARD["id"], [PLAIN, CERT], **membership.UI_EFFECTS)
    CERT_Q = db.add_pending_decision("retype", CERT, {"question": "auth2 restricted question",
                                                      "options": [{"key": "document", "label": "Document"}]})
    PLAIN_Q = db.add_pending_decision("retype", PLAIN, {"question": "auth2 plain question",
                                                        "options": [{"key": "document", "label": "Document"}]})
(paths.current_export_dir() / "index.html").write_text("<html><body>auth2 preview</body></html>")

anon = new_client()
tok = new_client(Authorization=f"Bearer {TOKEN}")

# ---- 1. fresh install: no user yet --------------------------------------------------------------
print("--- 1. no user yet ---")
check("setup needed", users.setup_needed())
for path in ("/", f"/object/{PLAIN}", "/admin", "/login", "/preview/", "/docs"):
    r = anon.get(path)
    check(f"no user: GET {path} -> 302 /setup", r.status_code == 302 and r.headers["location"] == "/setup",
          f"{r.status_code} {r.headers.get('location')}")
r = anon.get("/api/projects")
check("no user: API -> 401 unauthorized (shared shape)", r.status_code == 401 and err_code(r) == "unauthorized"
      and r.json().get("detail"), r.text[:200])
check("no user: /setup 200", anon.get("/setup").status_code == 200)
check("no user: /healthz 200", anon.get("/healthz").status_code == 200)
check("no user: /f/<plain> 200", anon.get(f"/f/{PLAIN}").status_code == 200)
check("no user: the install token is admin (GET /api/settings 200)", tok.get("/api/settings").status_code == 200)

# ---- 2. first admin, then an editor and a viewer ------------------------------------------------
print("--- 2. users ---")
admin = new_client()
admin_name, admin_pw = "boss_" + secrets.token_hex(2), pw()
SECRETS.append(admin_pw)
r = admin.post("/api/auth/setup", json={"username": admin_name, "password": admin_pw}, headers=SAME)
check("setup creates the admin and signs in", r.status_code == 200 and r.json()["user"]["role"] == "admin", r.text[:200])
check("after setup: /setup 404", anon.get("/setup").status_code == 404)
check("after setup: POST /api/auth/setup 404",
      anon.post("/api/auth/setup", json={"username": "x2", "password": pw()}, headers=SAME).status_code == 404)
check("after setup: /login 200", anon.get("/login").status_code == 200)
A_CSRF = csrf_of(admin)
AH = {**SAME, "X-CSRF-Token": A_CSRF}
clients = {"anonymous": anon}
for role in ("viewer", "editor"):
    name, secret = f"{role}_{secrets.token_hex(2)}", pw()
    SECRETS.append(secret)
    r = admin.post("/api/users", json={"username": name, "password": secret, "role": role}, headers=AH)
    check(f"admin creates a {role}", r.status_code == 200, r.text[:200])
    c = new_client()
    r = c.post("/api/auth/login", json={"username": name, "password": secret}, headers=SAME)
    check(f"{role} signs in", r.status_code == 200, r.text[:200])
    clients[role] = c
clients["admin"] = admin
clients["token"] = tok
WH = {role: ({**SAME, "X-CSRF-Token": csrf_of(c)} if role in ("viewer", "editor", "admin") else dict(SAME))
      for role, c in clients.items()}

# ---- 3. anonymous ---------------------------------------------------------------------------------
print("--- 3. anonymous ---")
r = anon.get(f"/project/{CARD['slug']}?a=1&b=2")
check("anon page -> 302 /login?next=<path+query> (encoded)", r.status_code == 302
      and r.headers["location"] == f"/login?next=%2Fproject%2F{CARD['slug']}%3Fa%3D1%26b%3D2", r.headers.get("location"))
check("anon / -> 302 /login (no next for /)", anon.get("/").headers.get("location") == "/login")
r = anon.post(f"/api/image/{PLAIN}", data={"display_name": "x"}, headers=SAME)
check("anon write -> 401 unauthorized + WWW-Authenticate", r.status_code == 401 and err_code(r) == "unauthorized"
      and r.headers.get("www-authenticate") == "Bearer", r.text[:150])
check("anon unknown page -> 302 /login", anon.get("/no-such-page").status_code == 302)
check("anon unknown API -> 401 (no route map for strangers)", anon.get("/api/no-such").status_code == 401)
check("viewer unknown API -> 404", clients["viewer"].get("/api/no-such").status_code == 404)
check("anon /static/js/csrf.js 200", anon.get("/static/js/csrf.js").status_code == 200)
check("anon /api/auth/me 200 (public)", anon.get("/api/auth/me").status_code == 200)
check("anon /logout page 200 (public)", anon.get("/logout").status_code == 200)
for path, want in (("/preview/", 302), ("/docs", 302), ("/redoc", 302), ("/openapi.json", 401)):
    r = anon.get(path)
    check(f"anon mount/docs {path} -> {want}", r.status_code == want, r.status_code)
for path in ("/preview/", "/docs", "/openapi.json"):
    check(f"viewer {path} -> 200", clients["viewer"].get(path).status_code == 200)
# next validation
for bad in ("//evil.example", "/\\evil.example", "/\t/evil.example", "https://evil.example", "evil", "/ok\nx",
            "/" + "a" * 2100):
    check(f"_safe_next refuses {bad[:20]!r}", auth_routes._safe_next(bad) == "/")
check("_safe_next keeps a same-site path with a query", auth_routes._safe_next("/project/x?a=1&b=2") == "/project/x?a=1&b=2")
r = anon.get("/login?next=//evil.example")
check("login page renders a refused next as '/'", r.status_code == 200 and "evil.example" not in r.text)

# ---- 4. the role matrix ---------------------------------------------------------------------------
print("--- 4. role matrix ---")
ROLES = ("anonymous", "viewer", "editor", "admin", "token")
PAGE_ANON = 302


def expect(r, want):
    if want == PAGE_ANON:
        return r.status_code == 302 and r.headers["location"].startswith("/login")
    return r.status_code == want


# (label, method, path, kwargs-factory(role) or None, {role: status})
def fresh(name):
    with actor.acting_as(actor.ACTOR_SCRIPT):
        return make_item(name)


def fresh_card(title):
    with actor.acting_as(actor.ACTOR_SCRIPT):
        return cards.create(title + " " + secrets.token_hex(2)).data["card"]


V, E, A = "viewer", "editor", "admin"
MATRIX = [
    ("page /", "GET", lambda r: "/", None, {"anonymous": PAGE_ANON, V: 200, E: 200, A: 200, "token": 200}),
    ("page /object/<plain>", "GET", lambda r: f"/object/{PLAIN}", None,
     {"anonymous": PAGE_ANON, V: 200, E: 200, A: 200, "token": 200}),
    ("page /project/<card>", "GET", lambda r: f"/project/{CARD['slug']}", None,
     {"anonymous": PAGE_ANON, V: 200, E: 200, A: 200, "token": 200}),
    ("page /admin (admin)", "GET", lambda r: "/admin", None, {"anonymous": PAGE_ANON, V: 403, E: 403, A: 200, "token": 200}),
    ("GET /api/projects", "GET", lambda r: "/api/projects", None, {"anonymous": 401, V: 200, E: 200, A: 200, "token": 200}),
    ("GET /api/image/<plain>", "GET", lambda r: f"/api/image/{PLAIN}", None,
     {"anonymous": 401, V: 200, E: 200, A: 200, "token": 200}),
    ("edit POST /api/image/<slug>", "POST", lambda r: f"/api/image/{fresh('edit-' + r)}",
     lambda r: {"data": {"display_name": "edited by " + r}}, {"anonymous": 401, V: 403, E: 200, A: 200, "token": 200}),
    ("tag POST /api/bulk/attach-tags", "POST", lambda r: "/api/bulk/attach-tags",
     lambda r: {"data": {"slugs": [PLAIN], "tag_names": ["auth2-" + r]}}, {"anonymous": 401, V: 403, E: 200, A: 200, "token": 200}),
    ("upload POST /api/upload", "POST", lambda r: "/api/upload",
     lambda r: {"files": {"file": (f"auth2-up-{r}-{secrets.token_hex(2)}.txt", b"uploaded by " + r.encode(), "text/plain")}},
     {"anonymous": 401, V: 403, E: 200, A: 200, "token": 200}),
    ("delete-to-trash", "POST", lambda r: f"/api/image/{fresh('del-' + r)}/delete", None,
     {"anonymous": 401, V: 403, E: 200, A: 200, "token": 200}),
    ("redact", "POST", lambda r: f"/api/image/{fresh('red-' + r)}/redact", None,
     {"anonymous": 401, V: 403, E: 200, A: 200, "token": 200}),
    ("GET /api/settings (admin)", "GET", lambda r: "/api/settings", None, {"anonymous": 401, V: 403, E: 403, A: 200, "token": 200}),
    ("GET /api/users (admin)", "GET", lambda r: "/api/users", None, {"anonymous": 401, V: 403, E: 403, A: 200, "token": 200}),
    ("GET /api/audit-log (admin)", "GET", lambda r: "/api/audit-log", None, {"anonymous": 401, V: 403, E: 403, A: 200, "token": 200}),
    ("empty trash (admin)", "POST", lambda r: "/api/trash/empty", lambda r: {"data": {"confirm": "EMPTY TRASH"}},
     {"anonymous": 401, V: 403, E: 403, A: 200, "token": 200}),
    ("permanent redacted delete (admin)", "POST",
     lambda r: f"/api/image/{_redacted_fixture(r)}/delete-redacted-file", lambda r: {"data": {"confirm": "true"}},
     {"anonymous": 401, V: 403, E: 403, A: 200, "token": 200}),
    ("convert card -> hobby (admin)", "POST", lambda r: f"/api/project/{fresh_card('Conv ' + r)['slug']}/convert-to-hobby", None,
     {"anonymous": 401, V: 403, E: 403, A: 200, "token": 200}),
]


def _redacted_fixture(role):
    with actor.acting_as(actor.ACTOR_SCRIPT):
        slug = make_item("perm-" + role)
        items.redact(slug)
    return slug


TABLE = {}
for label, method, path_f, kw_f, want in MATRIX:
    for role in ROLES:
        c = clients[role]
        kw = kw_f(role) if kw_f else {}
        headers = WH[role] if method == "POST" else None
        r = c.request(method, path_f(role), headers=headers, **kw)
        ok = expect(r, want[role])
        TABLE.setdefault(label, {})[role] = r.status_code
        check(f"{role:9} {label} -> {want[role]}", ok, f"{r.status_code} {r.text[:160]}")
        if r.status_code in (401, 403) and method != "GET" or (want[role] in (401, 403)):
            if r.status_code in (401, 403):
                code = err_code(r)
                check(f"{role:9} {label}: shared error shape ({code})",
                      code in ("unauthorized", "forbidden") and "detail" in r.json(), r.text[:120])

print("--- 4b. restricted and redacted items ---")
for role in ROLES:
    c = clients[role]
    admin_like = role in ("admin", "token")
    want_cert = 200 if admin_like else 404
    r = c.get(f"/f/{CERT}")
    check(f"{role:9} /f/<restricted> -> {want_cert}", r.status_code == want_cert, r.status_code)
    TABLE.setdefault("/f/<restricted>", {})[role] = r.status_code
    want_red = 410 if admin_like else 404
    r = c.get(f"/f/{RED}")
    check(f"{role:9} /f/<redacted> -> {want_red}", r.status_code == want_red, r.status_code)
    TABLE.setdefault("/f/<redacted>", {})[role] = r.status_code
    r = c.get(f"/f/{RED}/thumb")
    check(f"{role:9} /f/<redacted>/thumb -> {want_red}", r.status_code == want_red, r.status_code)
    r = c.get(f"/f/{PLAIN}")
    check(f"{role:9} /f/<plain> -> 200", r.status_code == 200, r.status_code)
    TABLE.setdefault("/f/<plain>", {})[role] = r.status_code
    if role == "anonymous":
        continue
    r = c.get(f"/object/{CERT}")
    check(f"{role:9} /object/<restricted> -> {200 if admin_like else 404}", r.status_code == (200 if admin_like else 404))
    r = c.get(f"/api/image/{CERT}")
    check(f"{role:9} GET /api/image/<restricted> -> {200 if admin_like else 404}",
          r.status_code == (200 if admin_like else 404) and (admin_like or err_code(r) == "not_found"))
    page = c.get(f"/project/{CARD['slug']}").text
    check(f"{role:9} card page {'lists' if admin_like else 'hides'} the restricted item", (CERT in page) == admin_like)
    pending = c.get("/api/pending-decisions").text
    check(f"{role:9} decision queue {'has' if admin_like else 'hides'} the restricted item's question",
          (CERT in pending) == admin_like and PLAIN in pending)
    if role == "editor":
        r = c.post(f"/api/image/{CERT}", data={"display_name": "pwned"}, headers=WH[role])
        check("editor edit of a restricted item -> 404 (no leak)", r.status_code == 404 and err_code(r) == "not_found")
        r = c.post(f"/api/image/{CERT}/delete", headers=WH[role])
        check("editor delete of a restricted item -> 404", r.status_code == 404)
        r = c.post(f"/api/image/{PLAIN}/related", data={"related_slug": CERT}, headers=WH[role])
        check("editor relate to a restricted item -> 404", r.status_code == 404)
check("the restricted item is untouched", db.get_by_slug(CERT)["display_name"] != "pwned")

# ---- 5. the install token -------------------------------------------------------------------------
print("--- 5. install token ---")
r = tok.post("/api/install-config", json={"site_title": "Auth2"}, headers=SAME)
check("token write needs no CSRF token", r.status_code == 200, r.text[:150])
conn = sqlite3.connect(db.DB_PATH)
row = conn.execute("SELECT actor FROM audit_log WHERE op = 'install_config_update' ORDER BY id DESC").fetchone()
check("token write attributed to actor 'token'", row and row[0] == "token", row)
for label, headers in (("wrong token", {"Authorization": "Bearer " + "x" * 64}),
                       ("lower-case scheme, wrong", {"Authorization": "bearer nope"})):
    c = new_client(**headers)
    r = c.get("/api/settings")
    check(f"{label} -> 401 invalid_token", r.status_code == 401 and err_code(r) == "invalid_token", r.text[:120])
    r = c.get(f"/f/{PLAIN}")
    check(f"{label} on a public door -> 401 too", r.status_code == 401)
c = new_client(Authorization=f"bearer {TOKEN}")
check("scheme is case-insensitive", c.get("/api/settings").status_code == 200)
c = new_client(Authorization="Basic dXNlcjpwYXNz")
r = c.get("/api/settings")
check("Basic auth grants nothing (anonymous 401 unauthorized)", r.status_code == 401 and err_code(r) == "unauthorized")
c = new_client(**{"X-Constructicon-Client": "desktop-app"})
r = c.post("/api/upload", files={"file": ("x.txt", b"x", "text/plain")}, headers=SAME)
check("old X-Constructicon-Client header grants nothing (401)", r.status_code == 401)
c = new_client(**{"X-Forwarded-For": "127.0.0.1", "X-Real-IP": "127.0.0.1"})
check("a loopback-looking request is still anonymous (no IP trust)", c.get("/api/projects").status_code == 401)
check("TestClient's own client address is loopback-ish and still refused", anon.get("/api/projects").status_code == 401)
vt = new_client(Authorization=f"Bearer {TOKEN}")
vt.cookies.update(clients["viewer"].cookies)
r = vt.post("/api/install-config", json={"site_title": "Auth2b"}, headers=SAME)
check("token + a viewer's cookie: the token wins (admin, no CSRF)", r.status_code == 200, r.text[:150])
saved = os.environ.pop("CONSTRUCTICON_INSTALL_TOKEN")
install_token.reset_cache()
try:
    r = tok.get("/api/settings")
    check("no token configured: every Bearer is refused (401 invalid_token)", r.status_code == 401
          and err_code(r) == "invalid_token")
finally:
    os.environ["CONSTRUCTICON_INSTALL_TOKEN"] = saved
    install_token.reset_cache()
check("token restored", tok.get("/api/settings").status_code == 200)

# ---- 6. role_of, the MCP, startup -----------------------------------------------------------------
print("--- 6. role_of / MCP / startup ---")
for a in ("token", "mcp", "script", "system", "migration", "owner-ui"):
    check(f"role_of({a}) = admin", roles.role_of(a) == roles.ADMIN)
for a in ("anonymous", "somebody", None, "user:ghost"):
    check(f"role_of({a}) = public", roles.role_of(a) == roles.PUBLIC)
check("ENFORCE on, RESTRICTED_VIEW_ROLE admin", roles.ENFORCE is True and policy.RESTRICTED_VIEW_ROLE == roles.ADMIN)
from mcp_server import server as mcp  # noqa: E402  (after the DB env is set)
check("MCP (actor mcp, admin) gets the restricted item", mcp.constructicon_get(CERT).get("slug") == CERT)
env = {k: v for k, v in os.environ.items() if not k.startswith("CONSTRUCTICON_") or k in (
    "CONSTRUCTICON_DB_PATH", "CONSTRUCTICON_STORAGE_DIR", "CONSTRUCTICON_EXPORTS_DIR")}
env["PYTHONPATH"] = ROOT
try:
    p = subprocess.run([sys.executable, "-m", "mcp_server.server"], cwd=ROOT, env=env, capture_output=True, text=True,
                       timeout=300)
    check("MCP with no token refuses to start (roles enforced)", p.returncode != 0 and "refusing to start" in p.stderr,
          (p.returncode, p.stderr[-300:]))
except subprocess.TimeoutExpired:
    check("MCP with no token refuses to start (roles enforced)", False, "it started (timed out)")
os.environ["CONSTRUCTICON_INSTALL_TOKEN"] = "too-short"
install_token.reset_cache()
try:
    try:
        webapp._startup_as_system()
        refused = False
    except RuntimeError as e:
        refused = "refusing to start" in str(e) and "too-short" not in str(e)
    check("web refuses to start on a broken token config (and doesn't echo it)", refused)
finally:
    os.environ["CONSTRUCTICON_INSTALL_TOKEN"] = saved
    install_token.reset_cache()

# ---- 7. the desktop uploader's API module ---------------------------------------------------------
class _Resp:
    """Just enough of a requests.Response for the uploader's api module."""

    def __init__(self, r):
        self.status_code, self._r = r.status_code, r
        self.ok = 200 <= r.status_code < 400

    def json(self):
        return self._r.json()


def uploader_checks():
    try:
        import requests  # noqa: F401
    except ImportError:  # the module only needs the name; its post() is replaced below
        stub = types.ModuleType("requests")
        stub.RequestException = type("RequestException", (Exception,), {})
        sys.modules["requests"] = stub
    sys.path.insert(0, os.path.join(ROOT, "desktop_app"))
    from constructicon_uploader import api as up_api
    raw = new_client()

    def fake_post(url, data=None, files=None, headers=None, timeout=None, allow_redirects=True):
        assert url.startswith(BASE)
        return _Resp(raw.post(url[len(BASE):], data=data, files=files, headers={**(headers or {}), **SAME}))

    up_api.requests.post = fake_post
    upfile = os.path.join(TMP, "auth2-uploader.txt")
    with open(upfile, "w") as f:
        f.write("from the desktop uploader\n")
    try:
        up_api.upload_file(BASE, upfile)
        check("uploader without a token -> AuthError", False, "no error")
    except up_api.AuthError as e:
        check("uploader without a token -> AuthError 'set the install token'", "Set Install Token" in str(e), str(e))
    try:
        up_api.upload_file(BASE, upfile, token="wrong-" + "x" * 40)
        check("uploader with a wrong token -> AuthError", False, "no error")
    except up_api.AuthError as e:
        check("uploader with a wrong token -> AuthError (refused)", "refused" in str(e), str(e))
    res = up_api.upload_file(BASE, upfile, token=TOKEN)
    up_row = db.get_by_slug(res["slug"])
    check("uploader with the token uploads, labelled automated",
          up_row and up_row["tech"] == db.source_automated_upload(), up_row and up_row["tech"])
    check("uploader headers: Bearer + client identity", up_api.request_headers("abc") ==
          {"X-Constructicon-Client": "desktop-app", "Authorization": "Bearer abc"})


print("--- 7. desktop uploader ---")
if os.path.isdir(os.path.join(ROOT, "desktop_app", "constructicon_uploader")):
    uploader_checks()
else:
    # The app image doesn't carry desktop_app/ (only core/web/mcp_server/scripts are mounted), so a
    # run inside the container skips this part; a run from a checkout covers it.
    print("SKIP desktop uploader checks: desktop_app/ is not in this checkout")

# ---- 8. no secrets in the audit log ---------------------------------------------------------------
print("--- 8. audit log ---")
dump = "\n".join(str(r) for r in conn.execute("SELECT * FROM audit_log"))
check("audit log holds no install token", TOKEN not in dump)
check("audit log holds no Authorization header", "Bearer" not in dump and "authorization" not in dump.lower())
check("audit log holds no password", not any(s_ in dump for s_ in SECRETS))
actors = {r[0] for r in conn.execute("SELECT DISTINCT actor FROM audit_log WHERE op IS NULL")}
check("request log has token and user actors", "token" in actors and any(a.startswith("user:") for a in actors), actors)
conn.close()

print()
print("matrix (status per role):")
print(f"{'':40s} " + " ".join(f"{r:>9s}" for r in ROLES))
for label, row_ in TABLE.items():
    print(f"{label:40s} " + " ".join(f"{row_.get(r, ''):>9}" for r in ROLES))
print()
print(f"{len(FAILS)} failure(s)" if FAILS else "ALL PASS")
sys.exit(1 if FAILS else 0)
