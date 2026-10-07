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
runs inside actor.acting_as("mcp"); core/roles.role_of("mcp") is admin). Per-user MCP tokens are
step 3: look the presented token up here, put its user on the ASGI scope, and have the tool
wrapper in server.py run as users.actor_for(user) instead of "mcp".
"""
import hmac
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from core import install_token  # noqa: E402  (stdlib only, no DB import)

MIN_TOKEN_LEN = install_token.MIN_TOKEN_LEN
HEALTH_PATH = "/healthz"
TokenConfigError = install_token.TokenConfigError


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
    def __init__(self, app, token):
        self.app = app
        self._token = token.encode() if token else b""  # empty = open mode (health check only)

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
        ok = scheme.lower() == b"bearer" and hmac.compare_digest(value.strip(), self._token)
        if not ok:
            return await self._send(send, *_json_response(
                401,
                {"ok": False, "error": {"code": "unauthorized",
                                        "message": "Missing or invalid MCP bearer token."}},
                [(b"www-authenticate", b"Bearer")],
            ))
        return await self.app(scope, receive, send)


def wrap(app, token):
    """Always wrap `app` (#574): with a token it enforces auth; without one it passes everything
    through but still serves GET /healthz, so the health check exists in both modes."""
    return BearerTokenMiddleware(app, token)
