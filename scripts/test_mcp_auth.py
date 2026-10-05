"""#561: MCP bearer-token middleware + token loading (pure unit test, no server/DB)."""
import asyncio
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from mcp_server.auth import BearerTokenMiddleware, TokenConfigError, load_token, wrap  # noqa: E402

TOKEN = "a" * 40
fails = []


def check(name, cond):
    print(("ok   " if cond else "FAIL ") + name)
    if not cond:
        fails.append(name)


async def inner(scope, receive, send):
    await send({"type": "http.response.start", "status": 200, "headers": []})
    await send({"type": "http.response.body", "body": b"inner"})


mw = BearerTokenMiddleware(inner, TOKEN)


def call(headers, path="/mcp", method="POST"):
    out = []

    async def send(m):
        out.append(m)
    asyncio.run(mw({"type": "http", "method": method, "path": path, "headers": headers}, None, send))
    return out


r = call([])
check("no header -> 401", r[0]["status"] == 401)
check("401 has WWW-Authenticate: Bearer", (b"www-authenticate", b"Bearer") in r[0]["headers"])
check("401 body is the shared error shape", b'"code": "unauthorized"' in r[1]["body"] and b'"ok": false' in r[1]["body"])
check("401 body never contains the token", TOKEN.encode() not in r[1]["body"])
check("wrong token -> 401", call([(b"authorization", b"Bearer " + b"b" * 40)])[0]["status"] == 401)
check("wrong scheme -> 401", call([(b"authorization", b"Basic " + TOKEN.encode())])[0]["status"] == 401)
check("right token -> inner app", call([(b"authorization", b"Bearer " + TOKEN.encode())])[1]["body"] == b"inner")
check("lowercase scheme ok", call([(b"authorization", b"bearer " + TOKEN.encode())])[0]["status"] == 200)
check("/healthz open", call([], "/healthz", "GET")[0]["status"] == 200)
check("POST /healthz not exempt", call([], "/healthz", "POST")[0]["status"] == 401)
check("wrap with no token is a no-op", wrap(inner, "") is inner)

check("unset -> ''", load_token({}) == "")
check("env token", load_token({"CONSTRUCTICON_MCP_TOKEN": TOKEN}) == TOKEN)
try:
    load_token({"CONSTRUCTICON_MCP_TOKEN": "short"})
    check("short token refused", False)
except TokenConfigError as e:
    check("short token refused (message omits it)", "short" not in str(e).replace("too short", ""))
with tempfile.NamedTemporaryFile("w", delete=False) as f:
    f.write(TOKEN + "\n")
check("file token (stripped)", load_token({"CONSTRUCTICON_MCP_TOKEN_FILE": f.name}) == TOKEN)
os.unlink(f.name)
try:
    load_token({"CONSTRUCTICON_MCP_TOKEN_FILE": f.name})
    check("missing file refused", False)
except TokenConfigError:
    check("missing file refused", True)

print("FAILED: %s" % fails if fails else "all passed")
sys.exit(1 if fails else 0)
