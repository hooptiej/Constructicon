"""Sessions, the install token, the sign-in gate and CSRF on the web side (#467 steps 1 and 2).
The rules live in core/users.py, core/install_token.py and core/roles.py.

Three pure-ASGI middlewares (no body buffering, so streaming uploads are untouched). Order
(web/app.py): origin guard -> actor (anonymous) -> session -> access -> audit -> csrf -> routes.

  * SessionMiddleware (web/app.py: inside the origin guard and ActorMiddleware, outside the audit
    logger: guard -> actor -> session -> audit -> csrf -> routes). For a
    request carrying the `constructicon_session` cookie it resolves the session (one indexed read;
    skipped for /static and /brand) and, when it is live, runs the request with
      - users.current_user() = that user (pages show their name; core/roles.role_of reads the role),
      - the actor "user:<username>" (core/actor.py), so change-log and request-log rows say who.
    No cookie, or a dead one: actor `anonymous` (web/middleware.py), no user.
    When the session's expiry slid (core/users.SESSION_TOUCH_SECONDS), the cookie is re-sent with a
    fresh Max-Age unless the response already sets it (login/logout).

  * AccessMiddleware (#467 step 2), right inside SessionMiddleware, before anything reads a body:
      - `Authorization: Bearer <install token>` (core/install_token.py): the request runs as actor
        `token`, role admin, with no session (so no CSRF: a browser can't send this header
        cross-site without a CORS preflight the app never grants). A Bearer header with a WRONG
        token is refused, 401 `invalid_token`, on every path (a misconfigured script fails loudly
        instead of silently browsing as anonymous). Other Authorization schemes are ignored.
      - No trust for loopback or LAN addresses: no session and no token = anonymous, whatever the
        client IP. The old `X-Constructicon-Client: desktop-app` header grants nothing (it only
        picks the upload's Source label).
      - The gate: the request's route or mount label (web/roles.required_role_for) against the
        actor's role (core/roles.role_of). Public passes (/f hotlinks, /healthz, /login, /setup,
        /logout, /api/auth/*, /static, /brand). An anonymous request for anything else: a page
        (GET/HEAD outside /api/) is redirected (302) to /setup while the install has no user, else
        to /login?next=<path>; anything else gets 401 `unauthorized` (shared error shape). A
        signed-in actor below a MOUNT's label gets 403 (routes refuse in web/roles.require_role).
        This is what gates /preview and the OpenAPI docs, which can't carry a dependency.

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
import logging
from http.cookies import CookieError, SimpleCookie
from urllib.parse import urlencode

from starlette.concurrency import run_in_threadpool
from starlette.responses import JSONResponse, RedirectResponse

from core import actor as actor_ctx, besteffort, errors, install_token, roles, users
from web import roles as web_roles

log = logging.getLogger("constructicon.auth")

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


# --- the install token and the sign-in gate (#467 step 2) -----------------------------------

def _error_response(status, code, message, headers=None):
    return JSONResponse(errors.http_body(code, message), status_code=status, headers=headers)


def is_page_request(scope):
    """A browser navigation we can answer with a redirect: GET/HEAD outside /api/ (and not the
    OpenAPI JSON). Everything else gets a status code."""
    path = scope.get("path", "")
    return (scope.get("method") in ("GET", "HEAD") and not path.startswith("/api/")
            and path != "/openapi.json")


def login_redirect_target(scope):
    """/login?next=<this path and query>. The path comes from the server's own parse of the request
    and /login validates `next` again (web/routes/auth.py _safe_next), so it can't send anyone off
    site."""
    target = scope.get("path", "/") or "/"
    qs = scope.get("query_string", b"")
    if qs:
        target += "?" + qs.decode("latin-1")
    if target == "/":
        return "/login"
    return "/login?" + urlencode({"next": target})


class AccessMiddleware:
    def __init__(self, app, routes):
        self.app = app
        self._routes = routes  # callable -> the app's routes (read per request: routers are added later)

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http" or scope.get("path", "").startswith(_SKIP_PREFIXES):
            await self.app(scope, receive, send)
            return
        presented = install_token.bearer_value(_headers(scope).get("authorization"))
        if presented is None:
            await self._gate(scope, receive, send)
            return
        if not install_token.matches(presented):
            client = scope.get("client") or (None, None)
            besteffort.warn(log, "install token refused", "wrong bearer token",
                            path=scope.get("path"), client=client[0])
            response = _error_response(401, "invalid_token", "The install token is wrong.",
                                       headers={"WWW-Authenticate": 'Bearer error="invalid_token"'})
            await response(scope, receive, send)
            return
        s_token = _session.set(None)  # a token request is never a session (so no CSRF)
        try:
            with actor_ctx.acting_as(actor_ctx.ACTOR_TOKEN), users.signed_in_as(None):
                await self._gate(scope, receive, send)
        finally:
            _session.reset(s_token)

    async def _gate(self, scope, receive, send):
        if not roles.ENFORCE:
            await self.app(scope, receive, send)
            return
        need = web_roles.required_role_for(self._routes(), scope)
        have = roles.role_of(actor_ctx.current_actor())
        if roles.at_least(have, need):
            await self.app(scope, receive, send)
            return
        if have != roles.PUBLIC:
            response = _error_response(403, "forbidden", f"This needs the {need} role.")
        elif is_page_request(scope):
            setup = await run_in_threadpool(users.setup_needed)
            response = RedirectResponse("/setup" if setup else login_redirect_target(scope), status_code=302)
        else:
            response = _error_response(401, "unauthorized", "Sign in first, or send the install token "
                                       "(Authorization: Bearer ...).", headers={"WWW-Authenticate": "Bearer"})
        await response(scope, receive, send)
