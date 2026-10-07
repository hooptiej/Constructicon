"""Who opened a sensitive item (#604 follow-up 7, #603).

Owner decision 2026-10-07: view logging is for SENSITIVE items only (a restricted type such as a key
or a certificate, or an item flagged "This is sensitive"). Record who opened, previewed or downloaded
one, through any door: the item page, the item API, the hotlink `/f/<slug>`, its thumbnail, and the
MCP (`get`, `download`, `view`). Ordinary reads stay unlogged.

One `item_access_log` row = {slug, actor, how, at}. `how` names the door (HOW_* below). The same
actor through the same door on the same item within FOLD_SECONDS is folded into the row already
there (the item page polls the item API, and a grid re-requests thumbnails), so the log reads as
"who looked, when, how", not as a request log.

Callers don't use this module directly: a door calls `policy.note_access(row, how)` after its policy
check passed, and that is a no-op for an ordinary item. Admins read the log on the item page
(`for_item`) and in Admin's restricted list (`summary`); `GET /api/image/{slug}/access-log` (admin).

Writing it is best effort: a failure to record (a locked database) is logged with context and the
read still succeeds. It never blocks the person looking.
"""

import logging
import time

from . import actor as actor_ctx, besteffort, db

log = logging.getLogger("constructicon.access_log")

HOW_PAGE = "page"            # /object/<slug>
HOW_API = "api"              # GET /api/image/<slug>
HOW_FILE = "file"            # /f/<slug> (the hotlink: the file itself)
HOW_THUMB = "thumb"          # /f/<slug>/thumb
HOW_MCP_GET = "mcp_get"      # constructicon_get
HOW_MCP_DOWNLOAD = "mcp_download"
HOW_MCP_VIEW = "mcp_view"    # constructicon_view (the picture as MCP image content)
HOWS = (HOW_PAGE, HOW_API, HOW_FILE, HOW_THUMB, HOW_MCP_GET, HOW_MCP_DOWNLOAD, HOW_MCP_VIEW)

FOLD_SECONDS = 300


def record(slug, how, actor=None, *, now=None):
    """Logs one access (folded into a recent identical one). Returns True when a row was written."""
    who = actor_ctx.resolve(actor)
    now = time.time() if now is None else now
    try:
        return db.insert_access_log(slug, who, how, now, fold_seconds=FOLD_SECONDS)
    except Exception as e:
        besteffort.warn(log, "access log: recording a sensitive item's access", e, slug=slug, how=how, actor=who)
        return False


def for_item(slug, limit=100):
    """The item's access rows, newest first: [{actor, how, at}]."""
    return db.list_access_log(slug, limit=limit)


def summary(slugs):
    """{slug: {"count", "last_at", "last_actor"}} for Admin's restricted list (one query)."""
    return db.access_log_summary(list(slugs))
