"""Request-body size caps, per route (#649).

Without this, nothing bounded a request body: the audit logger buffered every mutating /api/*
body (a chunked one of any size), JSON routes read theirs whole, and uvicorn has no limit of its
own. The public sign-in routes did this before anyone signed in, so one anonymous client could
push the web process toward memory exhaustion.

How it works. `BodyLimitMiddleware` is pure ASGI and sits just inside the origin guard (web/app.py:
origin guard -> body limit -> actor -> session -> access -> audit -> csrf -> routes). It never reads
the body itself. It wraps `receive`, so whoever reads the body (the audit logger, Starlette's form
parser, a route) gets `BodyTooLarge` (an HTTPException, 413 `payload_too_large`) instead of bytes:
  * before the first byte is pulled, when the declared Content-Length is over the route's limit;
  * as soon as the bytes actually received pass the limit, so a chunked body (no Content-Length)
    or a lying one can't get round it. At most `limit` bytes are ever held.
A body nobody reads is never pulled off the socket (uvicorn applies flow control), so an anonymous
upload refused by AccessMiddleware (401) costs nothing either.

The audit logger (web/middleware.py) checks the declared length first and, when it is over, answers
413 itself without reading anything or running the route, and records the refusal with a
`{"_body": "not read: ..."}` marker. A chunked body that trips while the logger reads it gets the
same treatment. A route that trips it raises the exception, which web/app.py shapes.

Limits (read per request, so a test can set the variables after import):
  * auth: `/api/auth/*`, `/api/account/*`, `/api/users*`. Small JSON bodies (a username and a
    password of at most 1,024 characters); the sign-in routes are public. CONSTRUCTICON_MAX_AUTH_BODY_KB,
    default 64.
  * upload: `/api/upload` and `/api/image/<slug>/replace-file`, the only routes that take a file.
    The existing upload cap (CONSTRUCTICON_MAX_UPLOAD_MB, core/storage.MAX_BYTES) plus
    UPLOAD_OVERHEAD_BYTES for the multipart framing and the small form fields. They still stream
    (Starlette spools the file to disk); the per-file check in the routes stays as it was.
  * everything else: CONSTRUCTICON_MAX_BODY_KB, default 1024 (1 MB). Every other JSON/form body is
    a few KB (the largest real one on the owner's archive, a type_metadata bag, is about 6 KB);
    Starlette already caps each non-file multipart field at 1 MB.
An unset, empty, non-numeric or non-positive variable falls back to its default.
"""

import os
import re

from fastapi import HTTPException
from starlette.responses import JSONResponse

from core import errors, storage

DEFAULT_AUTH_BODY_KB = 64
DEFAULT_BODY_KB = 1024
UPLOAD_OVERHEAD_BYTES = 1024 * 1024

KIND_AUTH = "auth"
KIND_UPLOAD = "upload"
KIND_DEFAULT = "default"

SCOPE_KEY = "constructicon.body_limit"

_AUTH_PATH = re.compile(r"^/api/(auth|account)(/|$)|^/api/users(/|$)")
_UPLOAD_PATH = re.compile(r"^/api/upload$|^/api/image/[^/]+/replace-file$")


def _kb_env(name, default_kb):
    raw = (os.getenv(name) or "").strip()
    try:
        kb = int(raw) if raw else default_kb
    except ValueError:  # silent-ok: a garbage value falls back to the default (documented above)
        kb = default_kb
    return (kb if kb > 0 else default_kb) * 1024


def _normalize(path):
    """Collapse doubled slashes and drop a trailing one, so `/api/auth/login/` (answered with a
    307, but only after the audit logger has read the body) gets the same limit as the real path."""
    p = path or ""
    while "//" in p:
        p = p.replace("//", "/")
    return p.rstrip("/") or "/"


def limit_for(path):
    """(limit in bytes, kind) for a request path."""
    p = _normalize(path)
    if _AUTH_PATH.match(p):
        return _kb_env("CONSTRUCTICON_MAX_AUTH_BODY_KB", DEFAULT_AUTH_BODY_KB), KIND_AUTH
    if _UPLOAD_PATH.match(p):
        return storage.MAX_BYTES + UPLOAD_OVERHEAD_BYTES, KIND_UPLOAD
    return _kb_env("CONSTRUCTICON_MAX_BODY_KB", DEFAULT_BODY_KB), KIND_DEFAULT


def _human(n):
    if n >= 1024 * 1024:
        return f"{n / (1024 * 1024):.1f} MB".replace(".0 MB", " MB")
    if n >= 1024:
        return f"{n / 1024:.0f} KB"
    return f"{n} bytes"


class BodyTooLarge(HTTPException):
    """413 payload_too_large. An HTTPException so FastAPI's own body parsing re-raises it as is
    (it turns any other exception into a 400) and web/app.py's handler shapes it."""

    def __init__(self, limit, kind, declared=None):
        self.limit = limit
        self.kind = kind
        self.declared = declared
        if declared is not None:
            message = (f"This request's body is {_human(declared)}, over the {_human(limit)} limit for this "
                       "address. Nothing was read.")
        else:
            message = f"This request's body went over the {_human(limit)} limit for this address."
        self.message = message
        super().__init__(status_code=413, detail=message, headers={"Connection": "close"})

    @property
    def audit_marker(self):
        """What the request log stores instead of the body (#551 style: never the content)."""
        if self.declared is not None:
            return f"not read: {_human(self.declared)} declared, over the {_human(self.limit)} limit"
        return f"not read: over the {_human(self.limit)} limit (no Content-Length)"

    @property
    def audit_reason(self):
        return f"payload_too_large: {self.message}"

    def response(self):
        return JSONResponse(errors.http_body("payload_too_large", self.message,
                                             {"max_bytes": self.limit, "limit_kind": self.kind}),
                            status_code=413, headers={"Connection": "close"})


def declared_length(scope):
    """The request's Content-Length as an int, or None (absent or unusable; then bytes are
    counted as they arrive)."""
    for key, value in scope.get("headers", []):
        if key.lower() == b"content-length":
            try:
                n = int(value.decode("latin-1").strip())
            except ValueError:  # silent-ok: unusable = unknown size; the byte count still applies
                return None
            return n if n >= 0 else None
    return None


def declared_refusal(scope):
    """The BodyTooLarge for this request when its DECLARED length is over its limit, else None.
    Reads nothing."""
    info = scope.get(SCOPE_KEY)
    if info is None:
        return None
    limit, kind, declared = info
    if declared is not None and declared > limit:
        return BodyTooLarge(limit, kind, declared)
    return None


class BodyLimitMiddleware:
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        limit, kind = limit_for(scope.get("path", ""))
        declared = declared_length(scope)
        scope[SCOPE_KEY] = (limit, kind, declared)
        seen = 0

        async def limited_receive():
            nonlocal seen
            if declared is not None and declared > limit:
                raise BodyTooLarge(limit, kind, declared)
            if seen > limit:
                raise BodyTooLarge(limit, kind)
            message = await receive()
            if message.get("type") == "http.request":
                seen += len(message.get("body", b""))
                if seen > limit:
                    raise BodyTooLarge(limit, kind)
            return message

        await self.app(scope, limited_receive, send)
