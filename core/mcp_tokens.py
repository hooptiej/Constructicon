"""Per-user MCP tokens (#604 build step 5).

Owner decisions (#604, 2026-10-07 + 2026-10-17): each user can have SEVERAL MCP tokens, each with a
name ("windows claude", "mac claude") and each revocable on its own, from their Account page. The MCP
then acts as THAT user: actor `user:<username>`, their role (core/roles.role_of), and they see what
they see (core/policy.py: sensitive items, private cards). The install token stays the admin key for
scripts and ops (actor `mcp`).

Stored like sessions: the token is 32 random bytes (secrets.token_urlsafe), prefixed `ctu_` so it is
recognisable in a config file; only its sha256 is kept (`mcp_tokens.token_hash`). The token itself is
shown ONCE, in the answer to create / rotate, and is never logged, imaged or stored.

  create(name)          a new token for the signed-in user -> {token (once), id, name, created_at}
  rotate(token_id)      a new secret for one of your tokens (the old one stops working at once)
  revoke(token_id)      delete one of your tokens
  list_mine()           your tokens, without secrets
  authenticate(token)   the MCP's sign-in: (user, token row) or raises TokenRefused with a clear reason

Every write is ONE change-log row with a `details` summary (user, token id, name; never the secret),
like core/users.py: not imaged, so the generic undo refuses it (revoke / create again instead).
Only the signed-in user manages their own tokens; the install token and the MCP itself (no user) get
403 not_signed_in. A disabled or deleted user's tokens are refused at sign-in (and a deleted user's
are removed with them, db._delete_user); re-enabling the user brings them back.
"""

import hashlib
import secrets
import time

from . import actor as actor_ctx, changes, db, roles, users
from .errors import AppError, Conflict, InvalidInput, NotFound

PREFIX = "ctu_"
MAX_NAME = 60
MAX_PER_USER = 20
TOUCH_EVERY_SECONDS = 600  # last_used_at is written at most this often (like sessions)

OP_CREATE = "mcp_token_create"
OP_ROTATE = "mcp_token_rotate"
OP_REVOKE = "mcp_token_revoke"


class TokenRefused(Exception):
    """A presented MCP token that can't be used, with the reason to tell the client."""


def hash_token(token):
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def looks_like_user_token(value):
    return isinstance(value, str) and value.startswith(PREFIX)


def _new_secret():
    return PREFIX + secrets.token_urlsafe(32)


def _me(actor=None):
    """The signed-in user making the request, or 403 not_signed_in."""
    who = actor_ctx.resolve(actor)
    uid = users.user_id_for_actor(who)
    user = db.get_user(user_id=uid) if uid is not None else None
    if user is None:
        raise AppError("not_signed_in", "MCP tokens belong to a user: sign in to the web app as yourself to make "
                       "or manage one (the install token and the MCP itself have no user).", status=403)
    return user


def _name(name):
    n = " ".join((name or "").split())
    if not n:
        raise InvalidInput("Give the token a name, e.g. the machine or agent that will use it (\"windows claude\").",
                           code="bad_token_name")
    if len(n) > MAX_NAME:
        raise InvalidInput(f"A token name is at most {MAX_NAME} characters.", code="bad_token_name")
    return n


def _public(row):
    return {"id": row["id"], "name": row["name"], "created_at": row["created_at"],
            "rotated_at": row.get("rotated_at"), "last_used_at": row.get("last_used_at")}


def _own(token_id, user):
    try:
        tid = int(token_id)
    except (TypeError, ValueError):
        raise NotFound(f"No MCP token {token_id!r}.") from None
    row = db.get_mcp_token(tid)
    if row is None or row["user_id"] != user["id"]:
        raise NotFound(f"You have no MCP token {token_id!r} (it may have been revoked already).")
    return row


def _record(op, details, batch_id, actor):
    changes.record(op, actor, [], batch_id=batch_id, conn=db.get_conn(), details=details)


def list_mine(actor=None):
    """The signed-in user's tokens, newest first (no secrets)."""
    return [_public(r) for r in db.list_mcp_tokens(_me(actor)["id"])]


def create(name, *, actor=None):
    """A new token for the signed-in user. Returns {token, id, name, created_at, batch_id}: `token` is
    the secret, shown only here. 409 token_name_taken (a name you already use), too_many_tokens."""
    user = _me(actor)
    name = _name(name)
    batch_id = changes.new_batch_id()
    token = _new_secret()
    with db.transaction():
        mine = db.list_mcp_tokens(user["id"])
        if any(t["name"].lower() == name.lower() for t in mine):
            raise Conflict(f"You already have a token called {name!r}: rotate it, or pick another name.",
                           code="token_name_taken")
        if len(mine) >= MAX_PER_USER:
            raise Conflict(f"You already have {MAX_PER_USER} MCP tokens: revoke one you no longer use first.",
                           code="too_many_tokens")
        tid = db._insert_mcp_token(user["id"], name, hash_token(token), time.time())
        _record(OP_CREATE, {"user_id": user["id"], "username": user["username"], "token_id": tid, "name": name},
                batch_id, actor)
    return {**_public(db.get_mcp_token(tid)), "token": token, "batch_id": batch_id}


def rotate(token_id, *, actor=None):
    """A new secret for one of your tokens; the old secret stops working now. Returns {token, id,
    name, ...} with the new secret, shown only here."""
    user = _me(actor)
    batch_id = changes.new_batch_id()
    token = _new_secret()
    with db.transaction():
        row = _own(token_id, user)
        db._rehash_mcp_token(row["id"], hash_token(token), time.time())
        _record(OP_ROTATE, {"user_id": user["id"], "username": user["username"], "token_id": row["id"],
                            "name": row["name"]}, batch_id, actor)
    return {**_public(db.get_mcp_token(row["id"])), "token": token, "batch_id": batch_id}


def revoke(token_id, *, actor=None):
    """Deletes one of your tokens; an MCP client still using it gets 401 from its next call."""
    user = _me(actor)
    batch_id = changes.new_batch_id()
    with db.transaction():
        row = _own(token_id, user)
        db._delete_mcp_token(row["id"])
        _record(OP_REVOKE, {"user_id": user["id"], "username": user["username"], "token_id": row["id"],
                            "name": row["name"]}, batch_id, actor)
    return {"ok": True, "revoked": _public(row), "batch_id": batch_id}


def authenticate(presented, *, now=None):
    """The MCP's sign-in by a per-user token. Returns {"actor": "user:<name>", "user_id", "username",
    "role", "token_id", "token_name"}; raises TokenRefused with what to do when the token is unknown
    (revoked, rotated or mistyped), its user is gone, disabled, or holds no role."""
    if not looks_like_user_token(presented):
        raise TokenRefused("Missing or invalid MCP bearer token.")
    row = db.find_mcp_token(hash_token(presented))
    if row is None:
        raise TokenRefused("This MCP token is not valid: it was revoked, rotated or mistyped. Make or rotate one in "
                           "the web app under Account > MCP tokens, and update this client's config.")
    if row.get("username") is None:
        raise TokenRefused("This MCP token's user no longer exists. Ask an admin for an account, then make a new token.")
    if row.get("disabled"):
        raise TokenRefused(f"This MCP token belongs to {row['username']}, whose account is disabled. An admin can "
                           "enable it again (Admin > Users).")
    if not roles.at_least(row.get("role") or roles.PUBLIC, roles.VIEWER):
        raise TokenRefused(f"{row['username']} has no role on this install, so their MCP token can't be used.")
    now = time.time() if now is None else now
    if not row.get("last_used_at") or now - row["last_used_at"] >= TOUCH_EVERY_SECONDS:
        try:
            db._touch_mcp_token(row["id"], now)
        except Exception as exc:  # noqa: BLE001 -- a locked DB must not refuse a valid token
            from . import besteffort
            import logging
            besteffort.warn(logging.getLogger("constructicon.mcp_tokens"), "could not record an MCP token's last use",
                            exc, token_id=row["id"])
    return {"actor": f"{users.USER_ACTOR_PREFIX}{row['username']}", "user_id": row["user_id"],
            "username": row["username"], "role": row["role"], "token_id": row["id"], "token_name": row["name"]}
