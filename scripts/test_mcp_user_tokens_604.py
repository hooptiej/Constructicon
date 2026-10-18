#!/usr/bin/env python3
"""Per-user MCP tokens (#604 build step 5). Throwaway DB, no server.

    python scripts/test_mcp_user_tokens_604.py

  1. Account API (web, real sessions): a user makes named tokens; the secret is in the answer once,
     never in the DB (only its sha256) nor the audit log; list / rotate / revoke; duplicate and empty
     names, someone else's token (404), the install token (403 not_signed_in: no user);
  2. core.mcp_tokens.authenticate: a valid token -> that user; rotated, revoked, unknown, a disabled
     user and a deleted user are refused with a reason that says what to do;
  3. the MCP's bearer middleware: the install token still runs as `mcp`; a user token sets the
     request's identity; refused ones are 401 with the reason; a broken lookup is a 503 that says so;
  4. end to end through the real streamable-HTTP MCP app: a tool call with a user's token runs AS
     that user (a card it creates is theirs, the change log names them), the role table applies (a
     viewer's token can't write), and they see what they see (a private card they aren't on is
     not_found); the install token still acts as the admin.
Prints PASS/FAIL per check, then FAILED: <names> or ALL PASS.
"""

import asyncio
import json
import os
import re
import secrets
import sqlite3
import sys

import _testenv  # noqa: E402
TMP = _testenv.isolate("mcptok604-")
HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)
sys.path.insert(0, HERE)

from starlette.testclient import TestClient  # noqa: E402

from core import actor, cards, db, install_token, mcp_tokens, sharing, users  # noqa: E402
_testenv.assert_isolated()
from web import app as webapp  # noqa: E402

FAILS = []
HOST = "testhost.local"
BASE = f"http://{HOST}"
SAME = {"Origin": BASE}


def check(name, cond, extra=""):
    print(("PASS " if cond else "FAIL ") + name + (f"  [{extra}]" if (extra and not cond) else ""))
    if not cond:
        FAILS.append(name)


def new_client():
    return TestClient(webapp.app, base_url=BASE, follow_redirects=False)


def csrf_of(client):
    m = re.search(r'<meta name="csrf-token" content="([^"]+)"', client.get("/").text)
    return m.group(1) if m else ""


def err(r):
    try:
        b = r.json()
    except ValueError:
        return None
    return (b.get("error") or {}).get("code") if isinstance(b, dict) else None


db.init_db()
users.limiter.reset()
NAMES = {"A": ("mt_admin", "admin"), "E": ("mt_editor", "editor"), "V": ("mt_viewer", "viewer"),
         "E2": ("mt_editor2", "editor"), "GONE": ("mt_gone", "editor")}
PW = {}
with actor.acting_as(actor.ACTOR_SCRIPT):
    for k, (n, role) in NAMES.items():
        PW[k] = "mt-" + secrets.token_urlsafe(16)
        users.create_user(n, PW[k], role)
UID = {k: db.get_user(username=n)["id"] for k, (n, _r) in NAMES.items()}
C = {"token": _testenv.client(webapp.app, base_url=BASE, follow_redirects=False)}
for k, (n, _r) in NAMES.items():
    c = new_client()
    r = c.post("/api/auth/login", json={"username": n, "password": PW[k]}, headers=SAME)
    check(f"{k} signs in", r.status_code == 200, r.text[:200])
    C[k] = c
H = {k: ({**SAME, "X-CSRF-Token": csrf_of(c)} if k in NAMES else dict(SAME)) for k, c in C.items()}


def make(who, name):
    return C[who].post("/api/account/mcp-tokens", json={"name": name}, headers=H[who])


# ---- 1. Account API ---------------------------------------------------------------------------
print("--- 1. account API ---")
r = make("E", "windows claude")
check("E makes 'windows claude' (200, a ctu_ secret in the answer)", r.status_code == 200
      and r.json().get("token", "").startswith("ctu_"), r.text[:200])
E_TOK, E_ID = r.json()["token"], r.json()["id"]
r = make("E", "mac claude")
E_TOK2, E_ID2 = r.json()["token"], r.json()["id"]
check("a second named token (several per user)", r.status_code == 200 and E_ID2 != E_ID)
r = make("E", "Windows Claude")
check("a name you already use (any case): 409 token_name_taken", r.status_code == 409 and err(r) == "token_name_taken")
r = make("E", "   ")
check("an empty name: 400 bad_token_name", r.status_code == 400 and err(r) == "bad_token_name")
r = make("V", "viewer laptop")
check("a viewer may make one too (its MCP can only read)", r.status_code == 200)
V_TOK = r.json()["token"]
r = make("token", "script")
check("the install token has no user: 403 not_signed_in", r.status_code == 403 and err(r) == "not_signed_in", r.text[:200])
lst = C["E"].get("/api/account/mcp-tokens").json()["tokens"]
check("list: E's two tokens, names only, no secret or hash",
      sorted(t["name"] for t in lst) == ["mac claude", "windows claude"] and not any("token" in t or "hash" in json.dumps(t) for t in lst))
check("V's list doesn't include E's tokens", all(t["name"] == "viewer laptop" for t in C["V"].get("/api/account/mcp-tokens").json()["tokens"]))
conn = sqlite3.connect(db.DB_PATH)
dump = "\n".join(str(r) for r in conn.execute("SELECT * FROM mcp_tokens")) + "\n".join(
    str(r) for r in conn.execute("SELECT form_body, mutations, error_detail FROM audit_log"))
conn.close()
check("the secret is nowhere in the DB (mcp_tokens or the audit log), only its sha256",
      E_TOK not in dump and E_TOK2 not in dump and V_TOK not in dump and mcp_tokens.hash_token(E_TOK) in dump)
r = C["E2"].post(f"/api/account/mcp-tokens/{E_ID}/revoke", headers=H["E2"])
check("someone else's token: 404 (not yours)", r.status_code == 404)
r = C["E"].post(f"/api/account/mcp-tokens/{E_ID2}/rotate", headers=H["E"])
check("rotate: a new secret", r.status_code == 200 and r.json()["token"].startswith("ctu_") and r.json()["token"] != E_TOK2)
E_TOK2_NEW = r.json()["token"]

# ---- 2. authenticate ---------------------------------------------------------------------------
print("--- 2. authenticate ---")


def refused(tok):
    try:
        mcp_tokens.authenticate(tok)
    except mcp_tokens.TokenRefused as e:
        return str(e)
    return None


ident = mcp_tokens.authenticate(E_TOK)
check("a valid token -> E (actor user:mt_editor, role editor)", ident["actor"] == "user:mt_editor" and ident["role"] == "editor")
check("last_used_at is recorded", db.get_mcp_token(E_ID)["last_used_at"] is not None)
check("the rotated-away secret is refused, saying how to fix it", "rotated" in (refused(E_TOK2) or ""))
check("the new secret works", mcp_tokens.authenticate(E_TOK2_NEW)["token_id"] == E_ID2)
r = C["E"].post(f"/api/account/mcp-tokens/{E_ID2}/revoke", headers=H["E"])
check("revoke (200)", r.status_code == 200 and r.json()["revoked"]["id"] == E_ID2)
check("a revoked token is refused", refused(E_TOK2_NEW) is not None)
check("an unknown ctu_ token is refused", refused("ctu_" + secrets.token_urlsafe(32)) is not None)
r = make("E2", "e2 box")
E2_TOK = r.json()["token"]
with actor.acting_as(actor.ACTOR_SCRIPT):
    users.set_disabled(UID["E2"], True)
check("a disabled user's token is refused, saying an admin can enable them", "disabled" in (refused(E2_TOK) or ""))
with actor.acting_as(actor.ACTOR_SCRIPT):
    users.set_disabled(UID["E2"], False)
check("... and works again once enabled", mcp_tokens.authenticate(E2_TOK)["username"] == "mt_editor2")
GONE_TOK = make("GONE", "gone box").json()["token"]
with actor.acting_as(actor.ACTOR_SCRIPT):
    users.delete_user(UID["GONE"])
check("a deleted user's token is refused (and its row went with the user)",
      refused(GONE_TOK) is not None and db.list_mcp_tokens(UID["GONE"]) == [])

# ---- 3. the middleware ---------------------------------------------------------------------------
print("--- 3. middleware ---")
from mcp_server import auth as mcp_auth  # noqa: E402
INSTALL = install_token.load()
seen = []


async def inner(scope, receive, send):
    seen.append(mcp_auth.current_identity())
    await send({"type": "http.response.start", "status": 200, "headers": []})
    await send({"type": "http.response.body", "body": b"inner"})


def call(mw, token):
    out = []

    async def send(m):
        out.append(m)
    hdrs = [(b"authorization", b"Bearer " + token.encode())] if token else []
    asyncio.run(mw({"type": "http", "method": "POST", "path": "/mcp", "headers": hdrs}, None, send))
    return out


mw = mcp_auth.wrap(inner, INSTALL)
seen.clear()
out = call(mw, INSTALL)
check("install token -> the app, identity None (actor mcp)", out[0]["status"] == 200 and seen == [None])
seen.clear()
out = call(mw, E_TOK)
check("E's token -> the app, identity user:mt_editor", out[0]["status"] == 200 and seen and seen[0]["actor"] == "user:mt_editor")
check("the identity doesn't leak past the request", mcp_auth.current_identity() is None)
out = call(mw, E_TOK2_NEW)
body = out[1]["body"].decode()
check("a revoked token -> 401 with the reason (never the token)", out[0]["status"] == 401 and "revoked" in body
      and E_TOK2_NEW not in body, body)
out = call(mw, "")
check("no header -> 401 as before", out[0]["status"] == 401)


def broken(_tok):
    raise sqlite3.OperationalError("database is locked")


out = call(mcp_auth.wrap(inner, INSTALL, user_lookup=broken), E_TOK)
check("a lookup that fails -> 503 that says the token couldn't be checked", out[0]["status"] == 503
      and b"Could not check" in out[1]["body"])

# ---- 4. end to end through the real MCP app --------------------------------------------------------
print("--- 4. end to end ---")
from mcp_server import server as mcp  # noqa: E402

# a private card E isn't on, made by the admin
with actor.acting_as("user:mt_admin"):
    secret_card = cards.create("Tokentest Secret Card").data["card"]
    sharing.set_private("card", secret_card["id"], True)

app = mcp_auth.wrap(mcp.mcp.streamable_http_app(host="testserver", stateless_http=True), INSTALL)


def rpc(client, token, name, arguments):
    body = {"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": name, "arguments": arguments}}
    r = client.post("/mcp", json=body, headers={"Authorization": f"Bearer {token}",
                                                "Accept": "application/json, text/event-stream",
                                                "MCP-Protocol-Version": "2025-06-18"})
    text = r.text
    if r.headers.get("content-type", "").startswith("text/event-stream"):
        datas = [line[5:].strip() for line in text.splitlines() if line.startswith("data:")]
        text = datas[-1] if datas else ""
    try:
        msg = json.loads(text)
    except ValueError:
        return r.status_code, None, text[:300]
    res = msg.get("result") or {}
    content = res.get("content") or [{}]
    payload = content[0].get("text", "")
    try:
        payload = json.loads(payload)
    except (ValueError, TypeError):
        pass
    return r.status_code, res.get("isError", False), payload


with TestClient(app, base_url="http://testserver") as mc:
    st, is_err, out = rpc(mc, E_TOK, "constructicon_create_project", {"title": "Tokentest Made By E"})
    check("E's token: create_project works", st == 200 and not is_err, f"{st} {is_err} {str(out)[:300]}")
    made = db.get_project("tokentest-made-by-e") or next((p for p in db.list_projects() if p["title"] == "Tokentest Made By E"), None)
    check("... the card is E's (created_by_user_id)", made is not None and made["created_by_user_id"] == UID["E"])
    rows = [r for r in db.list_change_log(limit=50) if r["op"] == "create_card"]
    check("... and the change log names user:mt_editor", rows and rows[0]["actor"] == "user:mt_editor", rows[:1])
    st, is_err, out = rpc(mc, V_TOK, "constructicon_create_project", {"title": "Tokentest Viewer Try"})
    check("V's token (a viewer): a write is refused (forbidden, says the role)", is_err and isinstance(out, dict)
          and out.get("error", {}).get("code") == "forbidden" and "editor" in out["error"]["message"], str(out)[:300])
    st, is_err, out = rpc(mc, V_TOK, "constructicon_list_projects", {})
    check("V's token: a read works", not is_err and isinstance(out, (list, dict)), str(out)[:200])
    st, is_err, out = rpc(mc, E_TOK, "constructicon_get_project", {"id_or_slug": secret_card["slug"]})
    check("E's token: the admin's private card is not_found", is_err and out.get("error", {}).get("code") == "not_found", str(out)[:200])
    st, is_err, out = rpc(mc, INSTALL, "constructicon_get_project", {"id_or_slug": secret_card["slug"]})
    check("the install token (admin) still sees it", not is_err and out.get("slug") == secret_card["slug"], str(out)[:200])
    st, is_err, out = rpc(mc, E_TOK2_NEW, "constructicon_version", {})
    check("a revoked token over HTTP: 401", st == 401, st)

print()
print(("FAILED: " + ", ".join(FAILS)) if FAILS else "ALL PASS")
sys.exit(1 if FAILS else 0)
