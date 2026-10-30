"""Change log API (V2 cards 3.13).

Core card operations record every write as a row in `audit_log` (the existing
table, extended with op/actor/batch_id/mutations columns) carrying *row images*:
    {"table": "projects", "key": {"id": 12}, "before": {...} | None, "after": {...} | None}
`before` None = an insert, `after` None = a delete. Row images make a later undo
generic across tables. This module is the thin API; the SQL lives in core/db.py.

Piece 1 lays the infrastructure so every later setter records from day one; undo
itself arrives with the reorganizing tools (piece 6).
"""

import uuid

from . import db
# #560: the actor constants live in core/actor.py (one source); re-exported for older callers.
from .actor import ACTOR_MCP, ACTOR_MIGRATION, ACTOR_SCRIPT, ACTOR_SYSTEM, ACTOR_UI  # noqa: F401


def new_batch_id():
    """Groups the rows of one operation (or one bulk call)."""
    return uuid.uuid4().hex[:16]


def row_image(table, key, before, after):
    return {"table": table, "key": key, "before": before, "after": after}


def record(op, actor, mutations, batch_id=None, affected_slugs=None, conn=None, details=None):
    """Writes one change-log row. With `conn`, joins the caller's transaction
    (the caller commits); without, opens its own connection and commits.
    `actor` None = the current actor context (core/actor.py). `details`: an optional summary
    dict stored with the row (form_body), for records without row images. Returns the audit row id."""
    batch_id = batch_id or new_batch_id()
    if conn is not None:
        return db.insert_change_log(conn, op, actor, mutations, batch_id=batch_id, affected_slugs=affected_slugs,
                                    details=details)
    own = db.get_conn()
    try:
        row_id = db.insert_change_log(own, op, actor, mutations, batch_id=batch_id, affected_slugs=affected_slugs,
                                      details=details)
        own.commit()
        return row_id
    finally:
        own.close()


def via(row):
    """#778: the credit of a change-log row made with a personal MCP key, "<user>'s <token name>"
    (e.g. "Hooptie J's windows claude"), from the row's `via_token_id` and the token's CURRENT name, so a
    renamed key shows its new name; a revoked key reads "<user>'s claude". None for everything else:
    the web, the install token (actor `mcp`, admin-owned, credited as before by its actor) and the system."""
    token_id = row.get("via_token_id")
    who = row.get("actor") or ""
    if token_id is None or not who.startswith("user:"):
        return None
    from . import mcp_tokens, users  # lazy: both import this module's neighbours
    uid = users.user_id_for_actor(who)
    if uid is None:
        return f"{who[5:]}'s {mcp_tokens.REVOKED_CREDIT_NAME}"  # the user is gone; the name is all there is
    c = mcp_tokens.credit(uid, token_id)
    return c["text"] if c else None


def list_changes(card=None, batch_id=None, limit=50):
    """Newest-first change-log rows. `card` is a project slug. Each carries `via` (#778, see via())."""
    return [{**r, "via": via(r)} for r in db.list_change_log(card_slug=card, batch_id=batch_id, limit=limit)]
