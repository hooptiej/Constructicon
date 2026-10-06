"""Bearer install-token auth for the MCP HTTP transport (#561 short-term fix, part of #467).

The MCP sidecar is a second back end with direct DB access, so anything that can reach port
8100 could otherwise do anything. This is a pure-ASGI middleware wrapped around the streamable
HTTP app. It is deliberately dependency-free and never logs or echoes the token.

Token source (first hit wins):
  CONSTRUCTICON_MCP_TOKEN        the token itself
  CONSTRUCTICON_MCP_TOKEN_FILE   path to a file whose (stripped) contents are the token

  * token set:    every request needs `Authorization: Bearer <token>` (hmac.compare_digest);
                  otherwise 401 + `WWW-Authenticate: Bearer` + the shared error shape.
                  Only `GET /healthz` is exempt (it reveals nothing).
  * token unset:  open, as before (fresh installs, dev); main() logs a loud warning.
                  `GET /healthz` still answers {"ok": true} (#574): the wrapper is mounted in
                  both modes, so a deploy check or monitor can rely on it.
  * token < 32 chars: refuse to start.

Identity: one install token = one identity, and the actor stays the literal "mcp".
# TODO(#467): map tokens -> users here (look the presented token up, put the user on the ASGI
# scope, and have the tool wrapper in server.py set the actor from it instead of "mcp").
"""
import hmac
import json
import os

MIN_TOKEN_LEN = 32
HEALTH_PATH = "/healthz"


class TokenConfigError(RuntimeError):
    pass


def load_token(env=None):
    """Return the configured token ('' when unset). Raises TokenConfigError if unreadable or
    too short. Never includes the token in a message."""
    env = os.environ if env is None else env
    token = (env.get("CONSTRUCTICON_MCP_TOKEN") or "").strip()
    path = (env.get("CONSTRUCTICON_MCP_TOKEN_FILE") or "").strip()
    if not token and path:
        try:
            with open(path, "r", encoding="utf-8") as f:
                token = f.read().strip()
        except OSError as exc:
            raise TokenConfigError(
                f"CONSTRUCTICON_MCP_TOKEN_FILE is set to {path!r} but it can't be read ({exc.strerror or exc})"
            )
        if not token:
            raise TokenConfigError(f"CONSTRUCTICON_MCP_TOKEN_FILE {path!r} is empty")
    if token and len(token) < MIN_TOKEN_LEN:
        raise TokenConfigError(
            f"the MCP token is too short (need at least {MIN_TOKEN_LEN} characters). "
            "Generate one with: python scripts/mcp_token.py generate <file>"
        )
    return token


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
