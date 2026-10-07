"""Sign-in, first-run setup, sign-out, user management and "change my password" (#467 step 1).

Thin adapters over core/users.py. Bodies are JSON (#558: Content-Type must be application/json,
415 otherwise). Request-log redaction for every route that carries a password is in
web/request_guard.py (AUDIT_ROUTE_RULES / AUDIT_ROUTE_PATTERNS: no body values logged).

Step 2 enforces the labels below (core/roles.ENFORCE): the sign-in routes are public, "my
password" needs a session (viewer), the user routes are admin (a signed-in admin or the install
token). /login redirects to /setup while the install has no user; /setup is 404 once one exists.
"""

from fastapi import HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse

from core import install_config, roles, users
from core.errors import AppError, NotFound
from web import auth as web_auth
from web.common import templates
from web.roles import RoleRouter, requires

router = RoleRouter(default_role=roles.PUBLIC)  # login / setup / logout must work with no login


async def _json_body(request: Request):
    """The JSON object body, or 415 / 400 (the #558 JSON gate)."""
    ctype = request.headers.get("content-type", "").split(";")[0].strip().lower()
    if ctype != "application/json":
        raise HTTPException(status_code=415, detail="Content-Type must be application/json")
    try:
        body = await request.json()
    except ValueError:
        raise HTTPException(status_code=400, detail="Body must be JSON") from None
    if not isinstance(body, dict):
        raise HTTPException(status_code=400, detail="Body must be a JSON object")
    return body


def _safe_next(target):
    """A same-site path to go to after signing in ("/" otherwise). Refused: anything not starting
    with one "/", "//host" and "/\\host" (browsers treat "\\" as "/"), and any control character or
    whitespace anywhere (browsers strip tabs/newlines from URLs, so "/\\t/evil" would become
    "//evil"). Too long -> "/"."""
    t = target or ""
    if (not t.startswith("/") or len(t) > 2000 or "\\" in t
            or any(ord(c) < 0x21 or ord(c) == 0x7f for c in t)):
        return "/"
    if t.startswith("//"):
        return "/"
    return t


def _client_ip(request: Request):
    return request.client.host if request.client else None


def _signed_in_response(request, user, payload):
    sess = users.start_session(user)
    response = JSONResponse({"ok": True, "user": users.public(user), **payload})
    web_auth.set_session_cookie(response, sess["token"], request)
    return response


# --- pages ----------------------------------------------------------------------------------

@router.get("/login", response_class=HTMLResponse)
def login_page(request: Request, next: str = "/"):
    if users.setup_needed():  # #467 step 2: a fresh install has no one to sign in as yet
        return RedirectResponse("/setup", status_code=302)
    return templates.TemplateResponse(request, "login.html", {"next_url": _safe_next(next),
                                                              "setup_needed": users.setup_needed()})


@router.get("/setup", response_class=HTMLResponse)
def setup_page(request: Request):
    """First-run setup: only while the install has no user at all, else 404."""
    if not users.setup_needed():
        raise NotFound("Setup is already done.")
    return templates.TemplateResponse(request, "setup.html", {"owner_name_set": bool(install_config.owner_name())})


@router.get("/logout", response_class=HTMLResponse)
def logout_page(request: Request):
    return templates.TemplateResponse(request, "logout.html", {})


@router.get("/account/password", response_class=HTMLResponse, dependencies=requires(roles.VIEWER))
def change_password_page(request: Request):
    return templates.TemplateResponse(request, "change_password.html", {"min_length": users.MIN_PASSWORD_LENGTH})


# --- sign-in / setup / sign-out ---------------------------------------------------------------

@router.get("/api/auth/me")
def api_me():
    """{signed_in, user, setup_needed}: who this browser is signed in as (never a token)."""
    me = users.current_user()
    return {"ok": True, "signed_in": bool(me), "user": me, "setup_needed": users.setup_needed()}


@router.post("/api/auth/login")
async def api_login(request: Request):
    """JSON {username, password}. Sets the session cookie; 401 invalid_login, 429
    too_many_attempts (details.retry_after). Never logs the body."""
    body = await _json_body(request)
    user = users.authenticate(body.get("username"), body.get("password"), _client_ip(request))
    old = web_auth.cookie_token(request.headers.get("cookie"))
    if old:
        users.end_session(old)  # signing in again (maybe as someone else) replaces the old session
    return _signed_in_response(request, user, {})


@router.post("/api/auth/setup")
async def api_setup(request: Request):
    """First-run: JSON {username, password, display_name?} creates the first admin and signs
    them in. 404 once any user exists."""
    body = await _json_body(request)
    result = users.create_first_admin(body.get("username"), body.get("password"), body.get("display_name"))
    user = users.get(result.data["user"]["id"])
    return _signed_in_response(request, user, {"batch_id": result.batch_id})


@router.post("/api/auth/logout")
def api_logout(request: Request):
    """Ends this browser's session (server-side) and clears the cookie. Fine when signed out."""
    token = web_auth.cookie_token(request.headers.get("cookie"))
    ended = users.end_session(token) if token else False
    response = JSONResponse({"ok": True, "ended": ended})
    web_auth.clear_session_cookie(response, request)
    return response


# --- my password ----------------------------------------------------------------------------

@router.post("/api/account/password", dependencies=requires(roles.VIEWER))
async def api_change_my_password(request: Request):
    """JSON {current_password, new_password} for the signed-in user. Keeps this session, ends
    the user's others. 401 not_signed_in / wrong_password."""
    me = users.current_user()
    if not me:
        raise AppError("not_signed_in", "Sign in first.", status=401)
    body = await _json_body(request)
    token = web_auth.cookie_token(request.headers.get("cookie"))
    result = users.set_password(me["id"], body.get("new_password"), current_password=body.get("current_password") or "",
                                keep_token=token)
    return {"ok": True, "sessions_ended": result.data["sessions_ended"]}


# --- Admin > Users --------------------------------------------------------------------------

@router.get("/api/users", dependencies=requires(roles.ADMIN))
def api_list_users():
    me = users.current_user()
    return {"ok": True, "users": users.list_users(), "me": me["id"] if me else None,
            "roles": list(users.ROLES), "min_password_length": users.MIN_PASSWORD_LENGTH}


@router.post("/api/users", dependencies=requires(roles.ADMIN))
async def api_create_user(request: Request):
    """JSON {username, password, role, display_name?}. 409 username_taken."""
    body = await _json_body(request)
    return users.create_user(body.get("username"), body.get("password"), body.get("role"),
                             body.get("display_name")).to_dict()


@router.post("/api/users/{user_id}/role", dependencies=requires(roles.ADMIN))
async def api_set_user_role(user_id: int, request: Request):
    """JSON {role}. 409 last_admin when it would leave no enabled admin."""
    body = await _json_body(request)
    return users.set_role(user_id, body.get("role")).to_dict()


@router.post("/api/users/{user_id}/password", dependencies=requires(roles.ADMIN))
async def api_reset_user_password(user_id: int, request: Request):
    """JSON {password}: an admin's reset. Ends that user's sessions."""
    body = await _json_body(request)
    keep = None
    me = users.current_user()
    if me and me["id"] == user_id:
        keep = web_auth.cookie_token(request.headers.get("cookie"))
    result = users.set_password(user_id, body.get("password"), keep_token=keep)
    return {"ok": True, "user": result.data["user"], "sessions_ended": result.data["sessions_ended"],
            "batch_id": result.batch_id}


@router.post("/api/users/{user_id}/disable", dependencies=requires(roles.ADMIN))
def api_disable_user(user_id: int):
    return users.set_disabled(user_id, True).to_dict()


@router.post("/api/users/{user_id}/enable", dependencies=requires(roles.ADMIN))
def api_enable_user(user_id: int):
    return users.set_disabled(user_id, False).to_dict()


@router.post("/api/users/{user_id}/delete", dependencies=requires(roles.ADMIN))
def api_delete_user(user_id: int):
    """Never the last admin (409 last_admin)."""
    return users.delete_user(user_id).to_dict()
