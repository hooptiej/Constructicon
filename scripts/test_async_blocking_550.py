#!/usr/bin/env python3
"""#550: blocking work must not run on the event loop.

Throwaway DB + storage + exports (scripts/_testenv.py), no external network:

    python scripts/test_async_blocking_550.py

  1. Static: no `async def` under web/ (routes, middleware, handlers) calls a known-blocking core
     module (db, items, ingest, site_export, users, ...), a subprocess, `open()` or `time.sleep`
     directly. The call has to be inside `run_in_threadpool(...)` / `to_thread` / `from_thread`, or the
     route has to be a plain `def` (FastAPI runs those in a worker thread). Also pins the routes that
     were fixed so a refactor back to `async def` is caught by name.
  2. Live: the real app on a loopback uvicorn server, with the slow call monkeypatched to sleep. A
     concurrent /healthz must answer fast while the slow request is still running, for export build,
     export publish, /api/content, /api/image/{slug}, /api/projects/{id}, blog PUT, install config and
     login (scrypt).
  3. The actor still reaches the writes made from the worker thread (audit_log.actor = "token", not
     "system"), for the routes that read their form/JSON body through from_request_thread.
  4. A second export build/publish while one runs is a 409 export_busy, not a second concurrent git
     push; the lock is released afterwards (also after a failure).

Exits 1 if any check fails.
"""
import ast
import json
import os
import sys
import threading
import time
import types
import urllib.error
import urllib.parse
import urllib.request

import _testenv  # noqa: E402  (scripts/_testenv.py: temp DB + storage + exports, refuses otherwise)
TMP = _testenv.isolate("async550-")
ROOT = os.path.abspath(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
sys.path.insert(0, ROOT)
sys.modules.setdefault("cairosvg", types.ModuleType("cairosvg"))  # no libcairo needed here

from core import db, ingest, install_config, items, paths, site_export, users  # noqa: E402
_testenv.assert_isolated()

FAILS = []


def check(name, cond, extra=""):
    print(("PASS " if cond else "FAIL ") + name + (f"  [{extra}]" if (extra and not cond) else ""))
    if not cond:
        FAILS.append(name)


# ---- 1. static check -----------------------------------------------------------------------
# Modules whose functions do synchronous DB / disk / subprocess / network work.
BLOCKING_MODULES = {
    "db", "items", "ingest", "site_export", "users", "blog", "cards", "hobbies", "membership",
    "install_config", "policy", "backup", "reset", "captions", "storage", "thumbnails", "ocr",
    "revisions", "tags_svc", "changes", "decisions", "card_rules", "similarity", "timeline",
    "subprocess", "shutil", "os", "requests", "urllib", "sqlite3",
}
BLOCKING_NAMES = {"open"}
OFFLOADERS = {"run_in_threadpool", "to_thread", "run_sync", "from_request_thread"}
# os.path.* and os.environ are pure; only flag os functions that touch the disk.
OS_PURE = {"path", "environ", "getenv", "sep", "fspath"}
# Functions of a blocking module that do no I/O (context managers, string/hmac work): reviewed.
PURE_CALLS = {("users", "actor_for"), ("users", "signed_in_as"), ("users", "check_csrf")}


def _root_and_attr(node):
    """('db', 'get_setting') for db.get_setting(...) / ('os', 'path') for os.path.join(...)."""
    func = node.func
    if isinstance(func, ast.Name):
        return None, func.id
    parts = []
    while isinstance(func, ast.Attribute):
        parts.append(func.attr)
        func = func.value
    if isinstance(func, ast.Name):
        return func.id, parts[-1]
    return None, None


def blocking_calls_in(fn_node):
    """Calls inside an async function body (not inside a nested def) that are not offloaded."""
    found = []

    def visit(node, offloaded):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)) and node is not fn_node:
            # a nested sync def / lambda: its body runs wherever it is called, which is the offloader
            # when it is handed to one; judge it only when it is not (then it is a real inline call).
            return
        if isinstance(node, ast.Call):
            root, attr = _root_and_attr(node)
            is_offload = attr in OFFLOADERS
            if not offloaded:
                if root in BLOCKING_MODULES and (root, attr) not in PURE_CALLS and not (root == "os" and attr in OS_PURE):
                    found.append((node.lineno, f"{root}.{attr}()"))
                elif root is None and attr in BLOCKING_NAMES:
                    found.append((node.lineno, f"{attr}()"))
                elif root == "time" and attr == "sleep":
                    found.append((node.lineno, "time.sleep()"))
            for child in ast.iter_child_nodes(node):
                visit(child, offloaded or is_offload)
            return
        for child in ast.iter_child_nodes(node):
            visit(child, offloaded)

    for stmt in fn_node.body:
        visit(stmt, False)
    return found


def collect_async_defs(rel_dir):
    out = []
    for dirpath, _dirs, files in os.walk(os.path.join(ROOT, rel_dir)):
        if "__pycache__" in dirpath or os.sep + "static" in dirpath or os.sep + "templates" in dirpath:
            continue
        for fname in sorted(files):
            if not fname.endswith(".py"):
                continue
            path = os.path.join(dirpath, fname)
            tree = ast.parse(open(path, encoding="utf-8").read(), filename=path)
            for node in ast.walk(tree):
                if isinstance(node, ast.AsyncFunctionDef):
                    out.append((os.path.relpath(path, ROOT), node))
    return out


# Documented exceptions (reviewed): each is one small indexed write on the request path that must
# not be skipped when the client disconnects mid-request.
ALLOWED = {
    ("web/middleware.py", "dispatch"): "the audit-log row is written in `finally`; awaiting there would lose "
                                       "the row when the request is cancelled (#122)",
}

bad = []
for rel, node in collect_async_defs("web"):
    if (rel, node.name) in ALLOWED:
        continue
    for lineno, what in blocking_calls_in(node):
        bad.append(f"{rel}:{lineno} async def {node.name} calls {what} on the event loop")
check("no async def under web/ runs blocking work directly", not bad, "; ".join(bad))

# The checker has to be able to fail: a synthetic offender and a synthetic fixed version.
_src_bad = "async def f(request):\n    x = await request.form()\n    return db.get_setting('a')\n"
_src_ok = "async def f(request):\n    x = await request.form()\n    return await run_in_threadpool(db.get_setting, 'a')\n"
_src_lambda = ("async def f(request):\n    return await run_in_threadpool(lambda: ingest.ingest_file(1))\n")
_fn = lambda s: next(n for n in ast.walk(ast.parse(s)) if isinstance(n, ast.AsyncFunctionDef))  # noqa: E731
check("the checker flags a direct db call", len(blocking_calls_in(_fn(_src_bad))) == 1)
check("the checker accepts run_in_threadpool(fn, ...)", not blocking_calls_in(_fn(_src_ok)))
check("the checker accepts run_in_threadpool(lambda: ...)", not blocking_calls_in(_fn(_src_lambda)))

# The routes that were fixed stay plain `def`, by name (route function -> file).
MUST_BE_SYNC = {
    "web/routes/blog_export.py": ["api_export_build", "api_export_publish", "api_update_blog_entry",
                                  "api_set_blog_entry_projects", "api_set_blog_entry_items"],
    "web/routes/items.py": ["api_create_content", "api_update_image", "api_run_type_action"],
    "web/routes/cards.py": ["api_update_project"],
    "web/routes/admin.py": ["api_set_install_config"],
    "web/routes/auth.py": ["api_login", "api_setup", "api_change_my_password", "api_create_user",
                           "api_set_user_role", "api_reset_user_password"],
}
for rel, names in MUST_BE_SYNC.items():
    tree = ast.parse(open(os.path.join(ROOT, rel), encoding="utf-8").read())
    kinds = {n.name: type(n).__name__ for n in ast.walk(tree) if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))}
    for name in names:
        check(f"{rel}: {name} is a plain def", kinds.get(name) == "FunctionDef", kinds.get(name))

# ---- the app, live on loopback --------------------------------------------------------------
db.init_db()
from web import app as webapp  # noqa: E402
import uvicorn  # noqa: E402

import socket  # noqa: E402
_s = socket.socket()
_s.bind(("127.0.0.1", 0))
PORT = _s.getsockname()[1]
_s.close()
server = uvicorn.Server(uvicorn.Config(webapp.app, host="127.0.0.1", port=PORT, log_level="warning", lifespan="off"))
threading.Thread(target=server.run, daemon=True).start()
BASE = f"http://127.0.0.1:{PORT}"
for _ in range(100):
    if server.started:
        break
    time.sleep(0.1)
check("test server is up", server.started)

AUTH = _testenv.auth_headers()
SLOW = 1.5   # seconds the patched call sleeps
FAST = 0.5   # /healthz must answer inside this while the slow request is running


def http(method, path, *, data=None, json_body=None, headers=None):
    h = dict(AUTH)
    h.update(headers or {})
    body = None
    if json_body is not None:
        body = json.dumps(json_body).encode()
        h["Content-Type"] = "application/json"
    elif data is not None:
        body = urllib.parse.urlencode(data).encode()
        h["Content-Type"] = "application/x-www-form-urlencoded"
    req = urllib.request.Request(BASE + path, data=body, method=method, headers=h)
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return r.status, r.read()
    except urllib.error.HTTPError as e:
        return e.code, e.read()


def healthz_during(slow_call, label):
    """Start slow_call() in a thread; once it has begun, time /healthz. Returns (healthz_seconds,
    slow_seconds, slow_result)."""
    box = {}

    def run():
        t0 = time.monotonic()
        box["result"] = slow_call()
        box["took"] = time.monotonic() - t0

    t = threading.Thread(target=run)
    t.start()
    time.sleep(0.3)  # let the slow request reach its sleep
    t0 = time.monotonic()
    status, _ = http("GET", "/healthz")
    health = time.monotonic() - t0
    t.join(30)
    check(f"{label}: /healthz answers while it runs (200)", status == 200, status)
    check(f"{label}: /healthz took {health:.3f}s (< {FAST}s)", health < FAST, f"{health:.3f}s")
    check(f"{label}: the slow request really was slow ({box.get('took', 0):.2f}s >= {SLOW}s)",
          box.get("took", 0) >= SLOW, box.get("took"))
    return box.get("result")


def slow(fn, ret=None):
    def wrapper(*a, **k):
        time.sleep(SLOW)
        return ret(*a, **k) if callable(ret) else ret
    return wrapper


# Control: prove the harness can see a blocked loop. A deliberately blocking async route.
from fastapi import APIRouter  # noqa: E402
_ctl = APIRouter()


@_ctl.get("/__block_loop__")
async def _block_loop():
    time.sleep(SLOW)  # the bug this issue is about
    return {"ok": True}


webapp.app.include_router(_ctl)
box_t0 = []


def _control():
    return http("GET", "/__block_loop__")


box = {}
th = threading.Thread(target=lambda: box.setdefault("r", _control()))
th.start()
time.sleep(0.3)
t0 = time.monotonic()
http("GET", "/healthz")
blocked_for = time.monotonic() - t0
th.join(30)
check(f"control: an async route that sleeps DOES stall /healthz ({blocked_for:.2f}s)", blocked_for > 0.8, f"{blocked_for:.2f}s")

# ---- 2a. export build / publish -------------------------------------------------------------
site_export.build_site = slow(site_export.build_site, {"projects": 0, "warnings": []})
status, body = healthz_during(lambda: http("POST", "/api/export/build", json_body={}), "POST /api/export/build")
check("export build still answers 200 with its report", status == 200, (status, body[:200]))

# publish: needs a target, a token and a current build on disk
install_config.update({"publish_targets": {"test": {"repo": "owner/repo", "branch": "main"}}})
db.set_setting("pages_publish_token", "ghp_test_token_not_real")
cur = paths.current_export_dir()
cur.mkdir(parents=True, exist_ok=True)
(cur / "index.html").write_text("<html></html>")
site_export.publish_build = slow(site_export.publish_build, {"commit": "abc123", "files": ["index.html"]})
status, body = healthz_during(lambda: http("POST", "/api/export/publish", json_body={"target": "test"}),
                              "POST /api/export/publish")
check("export publish still answers 200 with its commit", status == 200 and b"abc123" in body, (status, body[:200]))

# ---- 2b. ingest, image update, project update, blog PUT, install config, login -------------
ingest.ingest_content = slow(ingest.ingest_content, lambda *a, **k: types.SimpleNamespace(
    error="patched: nothing was ingested", row=None, pending_decision_id=None))
status, body = healthz_during(lambda: http("POST", "/api/content", data={"external_url": "https://example.com/x"}),
                              "POST /api/content")
check("/api/content still returns the ingest error as a 400", status == 400 and b"patched" in body, (status, body[:200]))

db.insert_content("item-a", "tester", "youtube", external_url="https://example.com/a", content_description="A")
_real_update = items.update


def _slow_update(*a, **k):
    time.sleep(SLOW)
    return _real_update(*a, **k)


items.update = _slow_update
status, body = healthz_during(lambda: http("POST", "/api/image/item-a", data={"description": "slow save"}),
                              "POST /api/image/{slug}")
check("/api/image/{slug} still saves (200)", status == 200, (status, body[:200]))
items.update = _real_update
check("...and the description landed", (db.get_by_slug("item-a") or {}).get("description") == "slow save")

from core import cards  # noqa: E402
card = cards.create("Slow card").data["card"]
_real_card_update = cards.update


def _slow_cards_update(*a, **k):
    time.sleep(SLOW)
    return _real_card_update(*a, **k)


cards.update = _slow_cards_update
status, body = healthz_during(
    lambda: http("POST", f"/api/projects/{card['id']}", data={"description": "slow card save"}),
    "POST /api/projects/{id}")
cards.update = _real_card_update
check("/api/projects/{id} still saves (200)", status == 200, (status, body[:200]))
check("...and the description landed", (db.get_project(card["id"]) or {}).get("description") == "slow card save")

from core import blog  # noqa: E402
entry = blog.create("Slow entry").data["entry"]
_real_set_items = blog.set_items


def _slow_set_items(*a, **k):
    time.sleep(SLOW)
    return _real_set_items(*a, **k)


blog.set_items = _slow_set_items
status, body = healthz_during(lambda: http("PUT", f"/api/blog-entries/{entry['slug']}/items", json_body=[]),
                              "PUT /api/blog-entries/{slug}/items")
blog.set_items = _real_set_items
check("blog items PUT still saves (200)", status == 200, (status, body[:200]))

_real_cfg = install_config.update


def _slow_cfg(*a, **k):
    time.sleep(SLOW)
    return _real_cfg(*a, **k)


install_config.update = _slow_cfg
status, body = healthz_during(lambda: http("POST", "/api/install-config", json_body={"owner_name": "Pat"}),
                              "POST /api/install-config")
install_config.update = _real_cfg
check("install config still saves (200)", status == 200, (status, body[:200]))

_real_auth = users.authenticate


def _slow_auth(*a, **k):
    time.sleep(SLOW)
    return _real_auth(*a, **k)


users.authenticate = _slow_auth
status, body = healthz_during(
    lambda: http("POST", "/api/auth/login", json_body={"username": "nobody", "password": "wrong"}),
    "POST /api/auth/login")
users.authenticate = _real_auth
check("login still answers its 401 invalid_login", status == 401 and b"invalid_login" in body, (status, body[:200]))

# ---- 3. the actor reaches writes made from the worker thread ------------------------------
def change_actors():
    c = db.get_conn()
    try:
        return [r["actor"] for r in c.execute("SELECT actor FROM audit_log WHERE op IS NOT NULL").fetchall()]
    finally:
        c.close()


_actors = change_actors()
# (the in-process setup calls above, cards.create etc., are legitimately "script"/"migration" rows)
check("change-log rows from the threaded routes carry the token actor",
      _actors.count("token") >= 3, sorted(set(_actors)))
check("no change-log row fell back to system or anonymous (the actor crossed into the worker thread)",
      "system" not in _actors and "anonymous" not in _actors, sorted(set(_actors)))

# ---- 4. one export job at a time -------------------------------------------------------------
gate = threading.Event()
entered = threading.Event()


def _gated_build(config, out_dir=None):
    entered.set()
    gate.wait(15)
    return {"projects": 0, "warnings": []}


site_export.build_site = _gated_build
first = {}
t1 = threading.Thread(target=lambda: first.setdefault("r", http("POST", "/api/export/build", json_body={})))
t1.start()
entered.wait(5)
s2, b2 = http("POST", "/api/export/build", json_body={})
check("a second build while one runs is 409", s2 == 409, (s2, b2[:200]))
check("...with the export_busy code and a message naming the running job",
      b"export_busy" in b2 and b"build is already running" in b2, b2[:300])
s3, b3 = http("POST", "/api/export/publish", json_body={"target": "test"})
check("a publish while a build runs is 409 export_busy", s3 == 409 and b"export_busy" in b3, (s3, b3[:200]))
gate.set()
t1.join(15)
check("the first build finishes (200)", first.get("r", (None,))[0] == 200, first.get("r"))


def _failing_build(config, out_dir=None):
    raise RuntimeError("patched build failure")


site_export.build_site = _failing_build
s4, b4 = http("POST", "/api/export/build", json_body={})
check("a failing build is a 500 carrying the real message", s4 == 500 and b"patched build failure" in b4, (s4, b4[:200]))
site_export.build_site = slow(site_export.build_site, {"projects": 0, "warnings": []})
site_export.build_site = lambda config, out_dir=None: {"projects": 0, "warnings": []}
s5, b5 = http("POST", "/api/export/build", json_body={})
check("the lock was released after the failure: the next build runs (200)", s5 == 200, (s5, b5[:200]))

server.should_exit = True
if FAILS:
    print(f"\n{len(FAILS)} check(s) FAILED:")
    for f in FAILS:
        print("  - " + f)
    sys.exit(1)
print("\nall checks passed")
