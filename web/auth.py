"""Sessions and CSRF on the web side (#467 step 1). The rules live in core/users.py.

Two pure-ASGI middlewares (no body buffering, so streaming uploads are untouched):

  * SessionMiddleware (web/app.py: inside the origin guard and ActorMiddleware, outside the audit
    logger: guard -> actor -> session -> audit -> csrf -> routes). For a
    request carrying the `constructicon_session` cookie it resolves the session (one indexed read;
    skipped for /static and /brand) and, when it is live, runs the request with
      - users.current_user() = that user (pages show their name; core/roles.role_of reads the role),
      - the actor "user:<username>" (core/actor.py), so change-log and request-log rows say who.
    No cookie, or a dead one, is exactly the pre-auth behaviour: actor owner-ui, no user.
    When the session's expiry slid (core/users.SESSION_TOUCH_SECONDS), the cookie is re-sent with a
    fresh Max-Age unless the response already sets it (login/logout).

  * CsrfMiddleware (innermost, inside the audit logger, so a refusal is in the request log). A
    state-changing request (POST/PUT/PATCH/DELETE) that is signed in, i.e. carries a cookie that
    resolved to a live session, must send that session's token in the `X-CSRF-Token` header,
    else 403 `csrf_failed`. Requests without a session cookie (scripts, the MCP, the desktop
    uploader, anonymous pages) are not affected. The origin guard (#558) still runs first for
    every mutating request. base.html injects the token for signed-in pages (a meta tag plus
    static/js/csrf.js, which wraps fetch and XMLHttpRequest); there are no plain HTML POST forms.

The cookie: HttpOnly, SameSite=Lax, Path=/, Max-Age 30 days (sliding), Secure only when the request
itself is HTTPS (HTTPS is deferred: plain HTTP on the LAN for now).
"""

import contextvars
from http.cookies import CookieError, SimpleCookie

from starlette.concurrency import run_in_threadpool
from starlette.responses import JSONResponse

from core import actor as actor_ctx, users

CSRF_HEADER = "x-csrf-token"
MUTATING_METHODS = {"POST", "PUT", "PATCH", "DELETE"}
_SKIP_PREFIXES = ("/static/", "/brand/")

_session = contextvars.ContextVar("constructicon_session", default=None)


def current_session():
    """This request's live session row (with csrf_token), or None."""
    return _session.get()


def csrf_token():
    """The CSRF token for the page being rendered ("" when not signed in). A Jinja global."""
    s = _session.get()
    return s["csrf_token"] if s else ""


def _headers(scope):
    return {k.decode("latin-1").lower(): v.decode("latin-1") for k, v in scope.get("headers", [])}


def cookie_token(cookie_header):
    """The session token from a Cookie header value, or None."""
    if not cookie_header:
        return None
    jar = SimpleCookie()
    try:
        jar.load(cookie_header)
    except CookieError:  # silent-ok: a malformed Cookie header just means "no session" (anonymous)
        return None
    morsel = jar.get(users.SESSION_COOKIE)
    return morsel.value if morsel else None


def is_https(scope_or_request):
    scope = getattr(scope_or_request, "scope", scope_or_request)
    return scope.get("scheme") == "https"


def cookie_header_value(token, *, secure, max_age=users.SESSION_SECONDS):
    parts = [f"{users.SESSION_COOKIE}={token}", "Path=/", f"Max-Age={int(max_age)}", "HttpOnly", "SameSite=Lax"]
    if secure:
        parts.append("Secure")
    return "; ".join(parts)


def set_session_cookie(response, token, request):
    response.headers.append("set-cookie", cookie_header_value(token, secure=is_https(request)))


def clear_session_cookie(response, request):
    response.headers.append("set-cookie", cookie_header_value("", secure=is_https(request), max_age=0))


def _user_view(sess):
    return {"id": sess["user_id"], "username": sess["username"], "display_name": sess["display_name"],
            "name": sess["display_name"] or sess["username"], "role": sess["role"]}


class SessionMiddleware:
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        sess = None
        if not scope.get("path", "").startswith(_SKIP_PREFIXES):
            token = cookie_token(_headers(scope).get("cookie"))
            if token:
                sess = await run_in_threadpool(users.resolve_session, token)
        if sess is None:
            # No live session: exactly the pre-auth request (ActorMiddleware's owner-ui, no user).
            await self.app(scope, receive, send)
            return
        user = _user_view(sess)
        refresh = sess.get("refreshed")

        async def send_wrapper(message):
            if refresh and message["type"] == "http.response.start":
                headers = list(message.get("headers", []))
                prefix = f"{users.SESSION_COOKIE}=".encode("latin-1")
                if not any(k.lower() == b"set-cookie" and v.startswith(prefix) for k, v in headers):
                    headers.append((b"set-cookie", cookie_header_value(token, secure=is_https(scope)).encode("latin-1")))
                    message = {**message, "headers": headers}
            await send(message)

        s_token = _session.set(sess)
        try:
            with actor_ctx.acting_as(users.actor_for(user)), users.signed_in_as(user):
                await self.app(scope, receive, send_wrapper)
        finally:
            _session.reset(s_token)


class CsrfMiddleware:
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] == "http" and scope["method"] in MUTATING_METHODS:
            sess = _session.get()
            if sess is not None and not users.check_csrf(sess, _headers(scope).get(CSRF_HEADER)):
                msg = "Missing or wrong CSRF token. Reload the page and try again."
                response = JSONResponse({"ok": False, "error": {"code": "csrf_failed", "message": msg}, "detail": msg},
                                        status_code=403)
                await response(scope, receive, send)
                return
        await self.app(scope, receive, send)
