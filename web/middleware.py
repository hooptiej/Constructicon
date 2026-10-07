"""Request middleware: the actor context (#560) and audit logging (#547; moved verbatim from
web/app.py). Added to the app in web/app.py, inside the request guard (web/request_guard.py),
which stays outermost: guard -> ActorMiddleware -> AuditLoggingMiddleware -> routes."""

import json
import logging

from fastapi import Request
from starlette.middleware.base import BaseHTTPMiddleware

from core import actor as actor_ctx, besteffort, db
from web import request_guard

log = logging.getLogger("constructicon.middleware")


# --- Actor context (#560) ---

def request_actor(scope):
    """Who is making a request before web/auth.py looks at it: `anonymous` (#467 step 2; role
    public). Inside this middleware, SessionMiddleware overrides it with "user:<username>" for a
    live session and AccessMiddleware with `token` for the install token. The client's address is
    never trusted (no loopback/LAN exception)."""
    return actor_ctx.ACTOR_ANONYMOUS


class ActorMiddleware:
    """Pure ASGI: sets the actor ContextVar for the whole request, so the audit logger, the
    route, run_in_threadpool, BackgroundTasks (all of which copy the context) and core's
    change log all see it."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        with actor_ctx.acting_as(request_actor(scope)):
            await self.app(scope, receive, send)


# --- Audit logging middleware ---

def _scrub_secrets(form_data, path=""):
    """#559: redaction is route-driven (see web/request_guard.py): routes that
    carry secrets log no values, and the field-name heuristic is only a backstop.
    Returns a new dict without mutating the original."""
    return request_guard.redact_audit_body(path, form_data)


# #438: request bodies above this aren't read into memory for the audit log —
# request.body() buffers the whole thing, which for a big /api/upload undid
# the streaming-to-disk from #433. 25 MB is the old upload cap, so every
# request that was audited with its fields before still is.
AUDIT_BODY_MAX_BYTES = 25 * 1024 * 1024


def _audit_body_skip_reason(request):
    """A short marker string if this request's body is too big (or of unknown
    size for a multipart upload) to buffer for the audit log, else None."""
    content_type = request.headers.get("content-type", "").split(";")[0].strip() or "unknown type"
    length = request.headers.get("content-length")
    try:
        size = int(length) if length is not None else None
    except ValueError:  # silent-ok: a bad Content-Length header just means "size unknown", handled below
        size = None
    if size is None:
        return f"not logged: {content_type}, unknown size" if content_type == "multipart/form-data" else None
    if size > AUDIT_BODY_MAX_BYTES:
        return f"not logged: {content_type}, {size / (1024 * 1024):.1f} MB"
    return None


def _unparsed_marker(content_type, size):
    """What the audit log records for a body that couldn't be parsed (#551): that something was
    sent, its type and size, and never any of its content."""
    try:
        size = int(size) if size is not None else None
    except (TypeError, ValueError):  # silent-ok: an unusable size is recorded as null
        size = None
    return {"_unparsed": True, "content_type": content_type or "unknown", "bytes": size}


async def _parse_audit_body(request, content_type, body_bytes):
    """The request's fields as a dict for the audit log, or the `_unparsed` marker. Never raises.

    A form body (urlencoded / multipart) is read as form data. Starlette's FormData is a
    multi-dict: the bulk routes send `slugs=a&slugs=b&...` as repeated fields, and a plain
    dict() would keep only the last value (#214), so a repeated key becomes a list and a
    single one stays a scalar. Anything else is tried as a JSON object (#551: a JSON body
    used to come back from request.form() as an empty form and was logged as {})."""
    try:
        if content_type in ("application/x-www-form-urlencoded", "multipart/form-data"):
            form = await request.form()
            data = {}
            for key in form.keys():
                values = form.getlist(key)
                data[key] = values if len(values) > 1 else values[0]
            return data
        data = json.loads(body_bytes)
        if isinstance(data, dict):
            return data
        reason = "the JSON body is not an object"
    except Exception as e:
        reason = repr(e)
    besteffort.warn(log, "audit: parsing the request body", reason,
                    method=request.method, path=request.url.path,
                    content_type=content_type or None, bytes=len(body_bytes))
    return _unparsed_marker(content_type, len(body_bytes))


# #583: an error response's body is read for the audit reason only when it is a small, already
# complete JSON body (the shared error shape or a plain HTTPException detail). A response with no
# known length (a stream) or a non-JSON type (a file) is never touched (#438).
ERROR_BODY_MAX_BYTES = 16 * 1024
ERROR_DETAIL_MAX_CHARS = 500


def reason_from_error_body(data):
    """The audit reason for a parsed JSON error body: "<code>: <message>" for the shared error
    shape (#548), else the plain `detail` string; None when it has neither."""
    if not isinstance(data, dict):
        return None
    err = data.get("error")
    if isinstance(err, dict) and err.get("message"):
        code = err.get("code")
        return f"{code}: {err['message']}" if code else str(err["message"])
    detail = data.get("detail")
    if isinstance(detail, str) and detail:
        return detail
    return None


async def _error_reason(response):
    """Reads the reason out of a >= 400 response without ever buffering a stream or a file, and
    leaves the response intact for the client. Never raises; None when there is nothing to record."""
    if response.status_code < 400:
        return None
    ctype = response.headers.get("content-type", "").split(";")[0].strip().lower()
    length = response.headers.get("content-length")
    iterator = getattr(response, "body_iterator", None)
    if ctype != "application/json" or iterator is None:
        return None
    try:
        size = int(length)
    except (TypeError, ValueError):  # silent-ok: no usable Content-Length means a stream: left alone (#438)
        return None
    if size > ERROR_BODY_MAX_BYTES:
        return None
    chunks = []
    async for chunk in iterator:
        chunks.append(chunk)

    async def replay():
        for c in chunks:
            yield c

    response.body_iterator = replay()
    try:
        data = json.loads(b"".join(c if isinstance(c, bytes) else c.encode() for c in chunks))
    except ValueError as e:
        besteffort.warn(log, "audit: reading an error response's reason", e)
        return None
    return reason_from_error_body(data)


class AuditLoggingMiddleware(BaseHTTPMiddleware):
    """Middleware to capture mutating /api/* requests (POST/PUT/DELETE) into
    the audit_log table. Reads the form body, scrubs secrets, logs the request
    with status and any error detail, then passes it through to the handler."""

    async def dispatch(self, request: Request, call_next):
        # Only audit mutating /api/* requests
        is_mutating = request.method in {"POST", "PUT", "PATCH", "DELETE"}
        is_api = request.url.path.startswith("/api/")
        should_audit = is_mutating and is_api

        form_data = {}
        skip_reason = _audit_body_skip_reason(request) if should_audit else None
        if skip_reason:
            form_data = {"_body": skip_reason}
        elif should_audit and request.method in {"POST", "PUT", "PATCH"}:
            # Read the request body so we can log it. Starlette automatically caches
            # the body after the first read, so the handler can read it again.
            # #551: a body we can't make sense of is recorded as a marker
            # ({"_unparsed": true, content_type, bytes}), never as {} (which would read as
            # "nothing was sent") and never as the raw body (secrets). The request itself
            # always goes through: an audit problem must not break the real request.
            content_type = request.headers.get("content-type", "").split(";")[0].strip().lower()
            try:
                body_bytes = await request.body()
            except Exception as e:
                besteffort.warn(log, "audit: reading the request body", e,
                                method=request.method, path=request.url.path)
                form_data = _unparsed_marker(content_type, request.headers.get("content-length"))
            else:
                if body_bytes:
                    form_data = await _parse_audit_body(request, content_type, body_bytes)

        # Call the actual route handler. A route can fail two ways: a caught
        # HTTPException/RequestValidationError, which Starlette's own
        # exception middleware (inside call_next) already turns into a
        # normal Response before it gets back here — no raise, just a 4xx/5xx
        # response, handled by the `else` branch below — or a truly unhandled
        # exception, which propagates out of call_next itself. The whole
        # point of #122 was making *that* second case debuggable after the
        # fact, so the audit row must still be written even though we
        # re-raise: do it in `finally`, not after a bare `try/except ...
        # raise` (which would skip the insert on every unhandled exception —
        # exactly the scenario this feature exists for).
        response = None
        status_code = 500
        error_detail = None
        # #557: touch request.state now so scope["state"] exists before the route runs; the route's
        # require_role dependency (web/roles.py) writes the route's role label into it.
        state = request.state
        try:
            response = await call_next(request)
            status_code = response.status_code
            if should_audit:
                # #583: a refusal the app made on purpose is a normal response, so say why.
                reason = await _error_reason(response)
                if reason:
                    error_detail = request_guard.redact_audit_error(request.url.path, reason)[:ERROR_DETAIL_MAX_CHARS]
        except Exception as e:
            error_detail = str(e)
            raise
        finally:
            if should_audit:
                scrubbed_form = _scrub_secrets(form_data, request.url.path)
                affected_slugs = []
                # Try to extract affected slugs from the path (e.g., /api/image/{slug})
                if "/image/" in request.url.path:
                    parts = request.url.path.split("/")
                    if len(parts) > 3 and parts[1] == "api" and parts[2] == "image":
                        slug = parts[3]
                        affected_slugs = [slug]
                # Also check for slugs in form data if present. Repeated
                # form fields arrive as a list (see above); a single slug
                # arrives as a bare string, which is the slug itself — not
                # JSON to be parsed (#214). Only a string that actually
                # looks like a JSON array gets decoded (a JSON-body client).
                if "slugs" in form_data:
                    try:
                        slugs = form_data["slugs"]
                        if isinstance(slugs, str):
                            stripped = slugs.strip()
                            if stripped.startswith("["):
                                slugs = json.loads(stripped)
                            else:
                                slugs = [slugs]
                        if isinstance(slugs, list):
                            affected_slugs.extend(
                                s for s in slugs if isinstance(s, str) and s
                            )
                    except Exception as e:
                        besteffort.warn(log, "audit: extracting affected slugs", e, path=request.url.path)
                # Deduplicate
                affected_slugs = list(set(affected_slugs))

                db.insert_audit_log(
                    method=request.method,
                    path=request.url.path,
                    form_body=scrubbed_form,
                    status_code=status_code,
                    error_detail=error_detail,
                    affected_slugs=affected_slugs,
                    actor=actor_ctx.current_actor(),  # #560
                    required_role=getattr(state, "required_role", None),  # #557
                )

        return response
