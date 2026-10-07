"""Users, passwords and sessions (#467 step 1: users, login, first-run setup; enforced since step 2).

The service module for the `users` and `sessions` tables (schema in core/db.py). Every write goes
through here (scripts/check_layering.py); the web layer (web/auth.py, web/routes/auth.py) and the
reset script (scripts/reset_password.py) are thin adapters.

Users
  id, username (unique, case-insensitive, kept as typed), display_name, role (viewer | editor |
  admin), password_hash, created_at, last_login_at, disabled.
  * Passwords: stdlib `hashlib.scrypt` (no new dependency), a random 16-byte salt per hash, stored
    self-describing as "scrypt$n$r$p$salt$hash" (salt and hash urlsafe base64), so the cost can be
    raised later and old hashes still verify. Compared with `hmac.compare_digest`. Minimum length
    MIN_PASSWORD_LENGTH, no other complexity rules.
  * Ops: create_user, set_role, set_password, set_disabled, delete_user, create_first_admin. Each
    validates first, writes inside one db.transaction() and records ONE change-log row (actor from
    the context) whose `details` say what happened: username, role, "password changed". Never a
    password or a hash: the user rows are deliberately NOT imaged (db.IMAGE_TABLE_KEYS), so no hash
    can reach audit_log. That also keeps them out of the generic undo (an editor door, which would
    bypass the last-admin rule); the inverse op is the undo (enable, set the role back, reset the
    password).
  * The last enabled admin can't be demoted, disabled or deleted (409 `last_admin`).

Sessions
  A random 32-byte token in the `constructicon_session` cookie; the table stores only its sha256,
  plus a per-session CSRF token. Sliding expiry: SESSION_DAYS from the last use (the row is touched
  at most every SESSION_TOUCH_SECONDS). Logout deletes the row. A password change, a disable or a
  delete ends the user's other sessions.

Sign-in backoff (in memory, per process): after LOGIN_FREE_ATTEMPTS failures for an IP or a
username, each further attempt must wait 2**(extra failures) seconds (capped at
LOGIN_MAX_DELAY_SECONDS) after the last failure, else 429 `too_many_attempts` without checking the
password. Failures older than LOGIN_WINDOW_SECONDS are forgotten; a success clears that username's
and that IP's counts.

Current user: a ContextVar set per request by web/auth.py. Its actor is "user:<username>"
(core/actor.py); core/roles.role_of() reads the role through role_for_actor(). Anonymous requests
are actor `anonymous`, role public (step 2).
"""

import base64
import contextlib
import contextvars
import hashlib
import hmac
import logging
import re
import secrets
import threading
import time

from . import besteffort, changes, db, roles
from .errors import AppError, Conflict, InvalidInput, NotFound

log = logging.getLogger("constructicon.users")

ROLES = (roles.VIEWER, roles.EDITOR, roles.ADMIN)
USER_ACTOR_PREFIX = "user:"

MIN_PASSWORD_LENGTH = 10
MAX_PASSWORD_LENGTH = 1024
MAX_DISPLAY_NAME = 80
_USERNAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{1,31}$")

# scrypt cost: n=2**15, r=8, p=1 is ~95 ms and 32 MB on the NAS (2**14 was ~50 ms).
SCRYPT_N = 2 ** 15
SCRYPT_R = 8
SCRYPT_P = 1
SCRYPT_DKLEN = 32
SALT_BYTES = 16
_MAX_N = 2 ** 20  # refuse to verify a stored hash asking for absurd memory

SESSION_COOKIE = "constructicon_session"
SESSION_DAYS = 30
SESSION_SECONDS = SESSION_DAYS * 24 * 3600
SESSION_TOUCH_SECONDS = 600
TOKEN_BYTES = 32

LOGIN_FREE_ATTEMPTS = 5
LOGIN_WINDOW_SECONDS = 15 * 60
LOGIN_MAX_DELAY_SECONDS = 15 * 60

OP_CREATE = "user_create"
OP_ROLE = "user_set_role"
OP_PASSWORD = "user_set_password"
OP_DISABLE = "user_disable"
OP_ENABLE = "user_enable"
OP_DELETE = "user_delete"
OP_SETUP = "user_first_admin"


# --- passwords ------------------------------------------------------------------------------

def _b64(raw):
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def _unb64(text):
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def _scrypt(password, salt, n, r, p, dklen):
    return hashlib.scrypt(password.encode("utf-8"), salt=salt, n=n, r=r, p=p, dklen=dklen,
                          maxmem=256 * 1024 * 1024)


def hash_password(password):
    """"scrypt$n$r$p$salt$hash" for `password` with a fresh random salt."""
    salt = secrets.token_bytes(SALT_BYTES)
    digest = _scrypt(password, salt, SCRYPT_N, SCRYPT_R, SCRYPT_P, SCRYPT_DKLEN)
    return f"scrypt${SCRYPT_N}${SCRYPT_R}${SCRYPT_P}${_b64(salt)}${_b64(digest)}"


def verify_password(password, stored):
    """True when `password` matches the stored hash string. A missing hash (NULL: no usable
    password) is False; a malformed one is False and logged (it means a damaged row)."""
    if not stored or not isinstance(password, str):
        return False
    try:
        scheme, n, r, p, salt, digest = stored.split("$")
        n, r, p = int(n), int(r), int(p)
        salt, digest = _unb64(salt), _unb64(digest)
        if scheme != "scrypt" or n < 2 or n > _MAX_N or n & (n - 1) or not (0 < r <= 32) or not (0 < p <= 16):
            raise ValueError("unsupported scrypt parameters")
    except ValueError as e:
        besteffort.warn(log, "users: a stored password hash is malformed", e)
        return False
    return hmac.compare_digest(_scrypt(password, salt, n, r, p, len(digest)), digest)


_dummy = {"hash": None}


def _burn_time(password):
    """Spend a real hash's time when there is no user to check, so a wrong username and a wrong
    password take the same time."""
    if _dummy["hash"] is None:
        _dummy["hash"] = hash_password(secrets.token_urlsafe(12))
    verify_password(password or "", _dummy["hash"])


# --- validation -----------------------------------------------------------------------------

def validate_username(username):
    u = (username or "").strip() if isinstance(username, str) else ""
    if not _USERNAME.match(u):
        raise InvalidInput("A username is 2 to 32 letters, digits, '.', '_' or '-', starting with a letter or "
                           "digit.", code="bad_username")
    return u


def validate_password(password):
    if not isinstance(password, str) or len(password) < MIN_PASSWORD_LENGTH:
        raise InvalidInput(f"A password needs at least {MIN_PASSWORD_LENGTH} characters.", code="bad_password")
    if len(password) > MAX_PASSWORD_LENGTH:
        raise InvalidInput(f"A password can be at most {MAX_PASSWORD_LENGTH} characters.", code="bad_password")
    return password


def validate_role(role):
    r = (role or "").strip().lower() if isinstance(role, str) else ""
    if r not in ROLES:
        raise InvalidInput(f"The role must be one of {', '.join(ROLES)}.", code="bad_role")
    return r


def validate_display_name(name):
    if name is None:
        return None
    if not isinstance(name, str):
        raise InvalidInput("The display name must be text.", code="bad_display_name")
    v = " ".join(name.split())
    if len(v) > MAX_DISPLAY_NAME:
        raise InvalidInput(f"The display name can be at most {MAX_DISPLAY_NAME} characters.",
                           code="bad_display_name")
    return v or None


# --- reads ----------------------------------------------------------------------------------

def public(user):
    """A user as pages and APIs see it (never the hash)."""
    if not user:
        return None
    return {"id": user["id"], "username": user["username"], "display_name": user.get("display_name"),
            "name": user.get("display_name") or user["username"], "role": user["role"],
            "created_at": user.get("created_at"), "last_login_at": user.get("last_login_at"),
            "disabled": bool(user.get("disabled"))}


def list_users():
    return [public(u) for u in db.list_users()]


def any_users():
    return db.count_users() > 0


def setup_needed():
    """True while the install has no user at all: the first-run setup (/setup) is open."""
    return not any_users()


def get(user):
    """A user by id (int or digit string) or username; NotFound otherwise."""
    found = None
    if isinstance(user, int) or (isinstance(user, str) and user.isdigit()):
        found = db.get_user(user_id=int(user))
    if found is None and isinstance(user, str):
        found = db.get_user(username=user.strip())
    if not found:
        raise NotFound(f"No user {user!r}.")
    return found


def actor_for(user):
    return f"{USER_ACTOR_PREFIX}{user['username']}"


# --- writes ---------------------------------------------------------------------------------

def _result(batch_id, data, warnings=()):
    from .cards import Result  # lazy: cards is heavy and imports a lot
    return Result(True, [], list(warnings), batch_id, False, data)


def _record(op, details, batch_id, actor=None):
    changes.record(op, actor, [], batch_id=batch_id, conn=db.get_conn(), details=details)


def _check_not_last_admin(user, what):
    if user["role"] == roles.ADMIN and not user["disabled"] and db.count_active_admins(exclude_user_id=user["id"]) == 0:
        raise Conflict(f"{user['username']} is the last admin, so they can't be {what}. Make someone else an "
                       "admin first.", code="last_admin")


def create_user(username, password, role, display_name=None, *, actor=None, batch_id=None):
    """A new enabled user. 409 `username_taken` when the name exists (any case)."""
    username = validate_username(username)
    validate_password(password)
    role = validate_role(role)
    display_name = validate_display_name(display_name)
    pw_hash = hash_password(password)  # outside the write lock: ~0.1 s
    batch_id = batch_id or changes.new_batch_id()
    with db.transaction():
        if db.get_user(username=username):
            raise Conflict(f"There is already a user called {username!r}.", code="username_taken")
        user_id = db._insert_user(username, display_name, role, pw_hash, time.time())
        _record(OP_CREATE, {"user_id": user_id, "username": username, "display_name": display_name, "role": role},
                batch_id, actor)
    return _result(batch_id, {"user": public(db.get_user(user_id=user_id))})


def create_first_admin(username, password, display_name=None, *, actor=None):
    """First-run setup: the first admin, only while the users table is empty (404 `not_found`
    afterwards, the same answer /setup gives). Also fills install_config.owner_name when unset."""
    from . import install_config
    username = validate_username(username)
    validate_password(password)
    display_name = validate_display_name(display_name)
    pw_hash = hash_password(password)
    batch_id = changes.new_batch_id()
    with db.transaction():
        if db.count_users():
            raise NotFound("Setup is already done.", code="not_found")
        user_id = db._insert_user(username, display_name, roles.ADMIN, pw_hash, time.time())
        _record(OP_SETUP, {"user_id": user_id, "username": username, "display_name": display_name,
                           "role": roles.ADMIN}, batch_id, actor)
        if not install_config.owner_name():
            install_config.update({"owner_name": display_name or username}, actor=actor, batch_id=batch_id)
    install_config.clear_cache()
    return _result(batch_id, {"user": public(db.get_user(user_id=user_id))})


def set_role(user, role, *, actor=None):
    target = get(user)
    role = validate_role(role)
    if target["role"] == role:
        return _result(None, {"user": public(target)}, ["Nothing changed."])
    batch_id = changes.new_batch_id()
    with db.transaction():
        target = get(target["id"])
        if role != roles.ADMIN:
            _check_not_last_admin(target, "demoted")
        db._update_user(target["id"], role=role)
        _record(OP_ROLE, {"user_id": target["id"], "username": target["username"], "from_role": target["role"],
                          "to_role": role}, batch_id, actor)
    return _result(batch_id, {"user": public(get(target["id"]))})


def set_password(user, password, *, current_password=None, keep_token=None, actor=None):
    """A new password. With `current_password` (the "change my password" page) the old one must
    match first (401 `wrong_password`). Ends the user's sessions except `keep_token`'s (the
    caller's own, when they change their own password). Logged as "password changed", never
    the hash."""
    target = get(user)
    validate_password(password)
    if current_password is not None and not verify_password(current_password, db.get_user_password_hash(target["id"])):
        raise AppError("wrong_password", "The current password is wrong.", status=401)
    pw_hash = hash_password(password)
    batch_id = changes.new_batch_id()
    keep = _token_hash(keep_token) if keep_token else None
    with db.transaction():
        db._update_user(target["id"], password_hash=pw_hash)
        ended = db._delete_user_sessions(target["id"], keep_token_hash=keep)
        _record(OP_PASSWORD, {"user_id": target["id"], "username": target["username"], "password_changed": True,
                              "sessions_ended": ended}, batch_id, actor)
    return _result(batch_id, {"user": public(get(target["id"])), "sessions_ended": ended})


def set_disabled(user, disabled, *, actor=None):
    """Disable (ends every session; can't sign in) or enable a user."""
    target = get(user)
    disabled = bool(disabled)
    if bool(target["disabled"]) == disabled:
        return _result(None, {"user": public(target)}, ["Nothing changed."])
    batch_id = changes.new_batch_id()
    with db.transaction():
        target = get(target["id"])
        ended = 0
        if disabled:
            _check_not_last_admin(target, "disabled")
            ended = db._delete_user_sessions(target["id"])
        db._update_user(target["id"], disabled=1 if disabled else 0)
        _record(OP_DISABLE if disabled else OP_ENABLE,
                {"user_id": target["id"], "username": target["username"], "sessions_ended": ended}, batch_id, actor)
    return _result(batch_id, {"user": public(get(target["id"]))})


def delete_user(user, *, actor=None):
    """Removes a user and their sessions. Never the last admin. Their past change-log rows keep
    their actor ("user:<name>"). Not undoable (create the user again)."""
    target = get(user)
    batch_id = changes.new_batch_id()
    with db.transaction():
        target = get(target["id"])
        _check_not_last_admin(target, "deleted")
        db._delete_user(target["id"])
        _record(OP_DELETE, {"user_id": target["id"], "username": target["username"], "role": target["role"]},
                batch_id, actor)
    return _result(batch_id, {"deleted": public(target)})


# --- sign-in backoff --------------------------------------------------------------------------

class LoginLimiter:
    """Per-key failure counts with exponential backoff, in memory (one web process)."""

    MAX_KEYS = 10000

    def __init__(self):
        self._lock = threading.Lock()
        self._fails = {}  # key -> (count, last_failure_time)

    def _live(self, key, now):
        entry = self._fails.get(key)
        if entry and now - entry[1] > LOGIN_WINDOW_SECONDS:
            self._fails.pop(key, None)
            return None
        return entry

    def retry_after(self, keys, now=None):
        """Seconds to wait before another attempt may be checked (0 = go ahead)."""
        now = time.time() if now is None else now
        wait = 0.0
        with self._lock:
            for key in keys:
                entry = self._live(key, now)
                if entry and entry[0] >= LOGIN_FREE_ATTEMPTS:
                    delay = min(2 ** (entry[0] - LOGIN_FREE_ATTEMPTS + 1), LOGIN_MAX_DELAY_SECONDS)
                    wait = max(wait, entry[1] + delay - now)
        return max(0.0, wait)

    def failed(self, keys, now=None):
        now = time.time() if now is None else now
        with self._lock:
            if len(self._fails) > self.MAX_KEYS:
                for k in [k for k, v in self._fails.items() if now - v[1] > LOGIN_WINDOW_SECONDS]:
                    self._fails.pop(k, None)
            for key in keys:
                entry = self._live(key, now)
                self._fails[key] = ((entry[0] if entry else 0) + 1, now)

    def succeeded(self, keys):
        with self._lock:
            for key in keys:
                self._fails.pop(key, None)

    def reset(self):
        with self._lock:
            self._fails.clear()


limiter = LoginLimiter()


def authenticate(username, password, ip=None, *, now=None):
    """The enabled user these credentials belong to. 401 `invalid_login` (the same answer for an
    unknown user, a wrong password or a disabled account); 429 `too_many_attempts` while backing
    off (details.retry_after, seconds), without checking the password."""
    now = time.time() if now is None else now
    name = (username or "").strip() if isinstance(username, str) else ""
    keys = [("user", name.lower())] + ([("ip", ip)] if ip else [])
    wait = limiter.retry_after(keys, now)
    if wait > 0:
        raise AppError("too_many_attempts", f"Too many failed sign-ins. Try again in {int(wait) + 1} seconds.",
                       status=429, details={"retry_after": int(wait) + 1})
    creds = db.get_user_credentials(name) if name else None
    if creds is None:
        _burn_time(password)
        ok = False
    else:
        ok = verify_password(password if isinstance(password, str) else "", creds["password_hash"]) \
            and not creds["disabled"]
    if not ok:
        limiter.failed(keys, now)
        raise AppError("invalid_login", "Wrong username or password.", status=401)
    # A success clears the IP's count too: one person's typos must not keep everyone behind that
    # address (a NAT, or a Docker bridge where every client shows as the gateway) backing off.
    # Each account is still guarded by its own username counter.
    limiter.succeeded(keys)
    db._update_user(creds["id"], last_login_at=now)
    return db.get_user(user_id=creds["id"])


# --- sessions -------------------------------------------------------------------------------

def _token_hash(token):
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def start_session(user, *, now=None):
    """A new session for `user` (a dict with id). Returns {token, csrf_token, expires_at}: the
    token goes in the cookie and is never stored or logged."""
    now = time.time() if now is None else now
    token = secrets.token_urlsafe(TOKEN_BYTES)
    csrf = secrets.token_urlsafe(TOKEN_BYTES)
    db._purge_expired_sessions(now)
    db._insert_session(_token_hash(token), user["id"], csrf, now, now + SESSION_SECONDS)
    return {"token": token, "csrf_token": csrf, "expires_at": now + SESSION_SECONDS}


def resolve_session(token, *, now=None):
    """The live session for a cookie token (with its user's username/display_name/role), or None.
    Slides the expiry when the session was last touched over SESSION_TOUCH_SECONDS ago; the result
    then carries refreshed=True (the cookie should be re-sent)."""
    if not token or not isinstance(token, str) or len(token) > 200:
        return None
    now = time.time() if now is None else now
    th = _token_hash(token)
    sess = db.get_session(th, now)
    if not sess:
        return None
    sess["refreshed"] = False
    if now - sess["last_seen"] > SESSION_TOUCH_SECONDS:
        db._touch_session(th, now, now + SESSION_SECONDS)
        sess["last_seen"], sess["expires_at"], sess["refreshed"] = now, now + SESSION_SECONDS, True
    return sess


def end_session(token):
    """Logout: deletes the session row. True when there was one."""
    if not token or not isinstance(token, str):
        return False
    return bool(db._delete_session(_token_hash(token)))


def check_csrf(session, presented):
    """True when the presented token matches the session's."""
    return bool(session and isinstance(presented, str) and presented
                and hmac.compare_digest(presented.encode("utf-8"), session["csrf_token"].encode("utf-8")))


# --- the current user (per request) ---------------------------------------------------------

_current = contextvars.ContextVar("constructicon_user", default=None)


def current_user():
    """The signed-in user for this request ({id, username, display_name, role, name}) or None."""
    return _current.get()


@contextlib.contextmanager
def signed_in_as(user):
    token = _current.set(user)
    try:
        yield user
    finally:
        _current.reset(token)


def user_id_for_actor(actor):
    """#604: the users.id behind a "user:<name>" actor, or None for every other actor (the install
    token, the MCP, scripts, the system, anonymous) and for a name with no user row. This is what
    the ownership columns record: NULL = admin-owned. A disabled user still has an id (they still
    owned what they made); whether they may SEE anything is roles.role_of's call."""
    if not (isinstance(actor, str) and actor.startswith(USER_ACTOR_PREFIX)):
        return None
    name = actor[len(USER_ACTOR_PREFIX):]
    cur = _current.get()
    if cur and cur["username"] == name:
        return cur["id"]
    u = db.get_user(username=name)
    return u["id"] if u else None


def owner_info(user_id):
    """{id, username, name} for an ownership column's value; None for NULL (admin-owned). A user
    deleted since shows as {"id", "username": None, "name": "deleted user #<id>"}."""
    if user_id is None:
        return None
    u = db.get_user(user_id=user_id)
    if not u:
        return {"id": user_id, "username": None, "name": f"deleted user #{user_id}"}
    return {"id": u["id"], "username": u["username"], "name": u.get("display_name") or u["username"]}


def role_for_actor(actor):
    """core/roles.role_of() for a "user:<name>" actor: that user's role (PUBLIC when the user is
    gone or disabled)."""
    name = actor[len(USER_ACTOR_PREFIX):]
    cur = _current.get()
    if cur and cur["username"] == name:
        return cur["role"]
    u = db.get_user(username=name)
    if not u or u["disabled"]:
        return roles.PUBLIC
    return u["role"]
