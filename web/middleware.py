"""Request middleware: the actor context (#560) and audit logging (#547; moved verbatim from
web/app.py). Added to the app in web/app.py, inside the request guard (web/request_guard.py),
which stays outermost: guard -> ActorMiddleware -> AuditLoggingMiddleware -> routes."""

import json

from fastapi import Request
from starlette.middleware.base import BaseHTTPMiddleware

from core import actor as actor_ctx, db
from web import request_guard


# --- Actor context (#560) ---

def request_actor(scope):
    """Who is making this request. Today every HTTP request is the owner's UI; #467 (auth)
    replaces this with the logged-in user. The one place that decides it."""
    return actor_ctx.ACTOR_UI


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
    except ValueError:
        size = None
    if size is None:
        return f"not logged: {content_type}, unknown size" if content_type == "multipart/form-data" else None
    if size > AUDIT_BODY_MAX_BYTES:
        return f"not logged: {content_type}, {size / (1024 * 1024):.1f} MB"
    return None


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
            try:
                body_bytes = await request.body()
                # Try to parse as form data — FastAPI routes use Form(...) parameters
                if body_bytes:
                    try:
                        # Starlette's FormData is a multi-dict — the bulk
                        # routes send `slugs=a&slugs=b&...` as repeated
                        # fields (`slugs: list[str] = Form(...)`), and a
                        # plain dict() would keep only the last value
                        # (#214). Keep every value: a repeated key becomes
                        # a list, a single one stays a scalar.
                        form = await request.form()
                        form_data = {}
                        for key in form.keys():
                            values = form.getlist(key)
                            form_data[key] = values if len(values) > 1 else values[0]
                    except Exception:
                        # If form parsing fails, try JSON (some endpoints might use JSON)
                        try:
                            form_data = json.loads(body_bytes)
                        except Exception:
                            # If both fail, leave form_data empty — don't break the request
                            pass
            except Exception:
                # If anything goes wrong reading the body, just proceed without
                # logging the request body — don't let an audit logging error
                # break the actual request
                pass

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
                    except Exception:
                        pass
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
