"""Bearer install-token auth for the MCP HTTP transport (#561; #467 step 2).

The MCP sidecar is a second back end with direct DB access, so anything that can reach port
8100 could otherwise do anything. This is a pure-ASGI middleware wrapped around the streamable
HTTP app. It is deliberately dependency-free and never logs or echoes the token.

The token is the install token, shared with the web app: core/install_token.py reads it
(CONSTRUCTICON_INSTALL_TOKEN(_FILE), or the older CONSTRUCTICON_MCP_TOKEN(_FILE)).

  * token set:    every request needs `Authorization: Bearer <token>` (hmac.compare_digest);
                  otherwise 401 + `WWW-Authenticate: Bearer` + the shared error shape.
                  Only `GET /healthz` is exempt (it reveals nothing).
  * token unset:  #467 step 2: with roles enforced (core/roles.ENFORCE, on) the server REFUSES TO
                  START (mcp_server/server.py main): an MCP with no token would be an open admin
                  door. The safer of the two options (refuse to start vs. refuse every call): a
                  crash-looping container is impossible to miss, a 401-everything server looks
                  healthy. `wrap()` still supports an open mode for tests and for ENFORCE off.
  * token < 32 chars, unreadable or empty file: refuse to start.

Identity: the install token = role admin (owner decision 2026-10-07), actor `mcp` (every tool
runs inside actor.acting_as("mcp"); core/roles.role_of("mcp") is admin).

#604 step 5: PER-USER tokens (core/mcp_tokens.py, made in the web app's Account page). A presented
token that isn't the install token and starts with `ctu_` is looked up by its sha256; when it is
valid the request runs as that user: IDENTITY (a ContextVar) holds {"actor": "user:<name>", ...} for
the whole request, and the tool wrapper in server.py runs each tool as that actor, so the role table
(ACTION_ROLES) and the item / card policy apply to them. Refused (revoked, rotated, a disabled or
deleted user) = 401 with the reason and what to do. The install token stays the admin key.
"""
import contextvars
import hmac
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from core import install_token  # noqa: E402  (stdlib only, no DB import)

MIN_TOKEN_LEN = install_token.MIN_TOKEN_LEN
HEALTH_PATH = "/healthz"
TokenConfigError = install_token.TokenConfigError

# #604 step 5: who this MCP request is (None = the install token, actor `mcp`). Set by the middleware
# for the length of one request; read by server.py's tool wrapper (current_identity()).
IDENTITY = contextvars.ContextVar("constructicon_mcp_identity", default=None)


def current_identity():
    """The per-user identity of the request being served ({"actor", "user_id", "username", "role",
    "token_id", "token_name"}), or None when it came with the install token (or outside a request)."""
    return IDENTITY.get()


def _lookup_user_token(presented):
    """core.mcp_tokens.authenticate, imported on first use (this module stays DB-free at import)."""
    from core import mcp_tokens  # lazy: imports the DB layer
    return mcp_tokens.authenticate(presented)


def load_token(env=None):
    """Return the configured install token ('' when unset). Raises TokenConfigError if
    unreadable or too short. Never includes the token in a message."""
    return install_token.load(env)


def _json_response(status, payload, extra_headers=()):
    body = json.dumps(payload).encode()
    headers = [(b"content-type", b"application/json"), (b"content-length", str(len(body)).encode())]
    headers.extend(extra_headers)
    return status, headers, body


class BearerTokenMiddleware:
    def __init__(self, app, token, user_lookup=None):
        self.app = app
        self._token = token.encode() if token else b""  # empty = open mode (health check only)
        self._lookup = user_lookup or _lookup_user_token  # #604 step 5: per-user tokens

    async def _send(self, send, status, headers, body):
        await send({"type": "http.response.start", "status": status, "headers": headers})
        await send({"type": "http.response.body", "body": body})

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":  # lifespan etc. pass straight through
            return await self.app(scope, receive, send)
        if scope.get("method") == "GET" and scope.get("path") == HEALTH_PATH:
            return await self._send(send, *_json_response(200, {"ok": True}))
        if not self._token:  # open mode: nothing else is guarded
            return await self.app(scope, receive, send)
        presented = b""
        for k, v in scope.get("headers", ()):
            if k == b"authorization":
                presented = v
                break
        scheme, _, value = presented.partition(b" ")
        value = value.strip()
        is_bearer = scheme.lower() == b"bearer"
        if is_bearer and hmac.compare_digest(value, self._token):
            return await self.app(scope, receive, send)  # the install token: admin, actor `mcp`
        message = "Missing or invalid MCP bearer token."
        if is_bearer and value.startswith(b"ctu_"):
            try:
                ident = await _to_thread(self._lookup, value.decode("utf-8", "replace"))
            except Exception as exc:  # noqa: BLE001 -- refused (TokenRefused) or the DB failed: say which
                if exc.__class__.__name__ == "TokenRefused":
                    message = str(exc)
                else:
                    print(f"constructicon-mcp: per-user token lookup failed: {exc.__class__.__name__}: {exc}",
                          flush=True)
                    return await self._send(send, *_json_response(
                        503, {"ok": False, "error": {"code": "unavailable",
                                                     "message": "Could not check this MCP token right now (the "
                                                                "database didn't answer). Try again; if it keeps "
                                                                "failing, check the MCP container's log."}}))
            else:
                reset = IDENTITY.set(ident)
                try:
                    return await self.app(scope, receive, send)
                finally:
                    IDENTITY.reset(reset)
        return await self._send(send, *_json_response(
            401,
            {"ok": False, "error": {"code": "unauthorized", "message": message}},
            [(b"www-authenticate", b"Bearer")],
        ))


async def _to_thread(fn, *args):
    """Run a blocking DB lookup off the event loop (anyio ships with the MCP SDK)."""
    import anyio.to_thread
    return await anyio.to_thread.run_sync(fn, *args)


def wrap(app, token, user_lookup=None):
    """Always wrap `app` (#574): with a token it enforces auth; without one it passes everything
    through but still serves GET /healthz, so the health check exists in both modes. With a token,
    per-user tokens (#604 step 5) are accepted too (`user_lookup` replaces the DB lookup in tests)."""
    return BearerTokenMiddleware(app, token, user_lookup)
