"""Item visibility policy (#557; #443 restricted items; groundwork for auth #467).

"Can this actor see this item?" is decided HERE and nowhere else. Every door that hands out an
item (or a list of items) asks this module:

  * direct doors (one item by slug): `/object/<slug>`, `GET /api/image/<slug>` and the other item
    JSON reads, `/f/<slug>` and its thumbnail, MCP `constructicon_get` / `download` /
    `get_related` / `list_revisions`  ->  `require_view(row)` (or `can_view(row)`);
  * lists that show what a card/hobby/entry holds: project and hobby pages, MCP
    `get_project`, related / similar items, blog entries  ->  `filter_visible(rows)`;
  * general browsing (search, the gallery, home lists, unfiled, tag pages, uploader pages)
    ->  `sql_browse_clause()` inside core/db.py's queries, plus `filter_visible` where a route
    has the rows in hand;
  * the static site export and the project zip (anything that leaves the install)
    ->  `filter_exportable(rows)`.

scripts/check_policy_doors.py fails if a known door stops calling this module.

What it decides since #467 step 2 ("restricted means locked, not just hidden", #467 2026-10-02):
  * `RESTRICTED_VIEW_ROLE = roles.ADMIN`: `can_view` says False for a restricted item (a type with
    `restricted=True`: keys and certificates, object_types.restricted_types()) unless the actor is
    an admin (a signed-in admin, the install token `token`, the MCP `mcp`, in-process scripts).
    Every direct door refuses it with 404 `not_found` (deliberately the same answer as "no such
    item", so the slug's existence isn't leaked) and every list drops it.
  * The public file doors also hide REDACTED items from non-admins (`require_file`, 404).
  * General browsing hides restricted items from everyone (`sql_browse_clause`, the fragment
    that was `db._not_restricted`), as it always has; an admin finds them in /admin's "Keys &
    certificates" list and on the cards they're attached to.
  * Exports never contain them, for anyone.
A request must pass BOTH checks: the route's role (web/roles.py) and this item policy.

#603 / #604 step 2 (owner decisions 2026-10-07): "sensitive" = a restricted TYPE or the per-item
"This is sensitive" flag (capture_events.sensitive, set through core/items.py). `is_restricted`
is that OR, so every door above locks a flagged item exactly like a key or a certificate. Who may
see a sensitive item: admins, plus the user who UPLOADED it (capture_events.uploaded_by_user_id,
#604 step 1). The uploader rule applies to both kinds (a flagged item and a restricted type) when
an uploader is known; NULL (token / MCP / script uploads, everything from before step 1) means
admin-only, as before. An admin REDACTION still overrides it for everyone: the file doors
(`require_file`) refuse a redacted item to anyone but an admin, the uploader included.
General browsing: a restricted TYPE stays out of browsing for everyone (unchanged); a FLAGGED item
is in browsing (search, gallery, home, unfiled, tags) for admins and its uploader only.
Opening a sensitive item is recorded (core/access_log.py, `note_access`).
"""

import re

from core import actor as actor_ctx, roles
from core.errors import NotFound

# #467: the switch. None = restricted items are visible to anyone who can reach the door
# (the pre-step-2 behaviour). roles.ADMIN (#467 step 2, ON) = only admins see them anywhere.
RESTRICTED_VIEW_ROLE = roles.ADMIN

# #467 step 2: the public file doors (/f/<slug>, /f/<slug>/thumb) answer a REDACTED item only to
# this role (an admin then gets the old 410 "file was redacted"); anyone else gets 404 not_found,
# the same answer as a missing item, so the slug's existence isn't leaked through a public door.
REDACTED_FILE_ROLE = roles.ADMIN


def is_type_restricted(item):
    """True when this row's TYPE is restricted (#443: keys, certificates)."""
    from core import object_types  # lazy: type modules import core.db
    return object_types.is_restricted(item)


def is_flagged(item):
    """True when the row carries the per-item "This is sensitive" flag (#603)."""
    return bool(item and item.get("sensitive"))


def is_restricted(item):
    """True when this row is sensitive: a restricted type (#443) OR flagged (#603)."""
    return is_flagged(item) or is_type_restricted(item)


def restriction_reason(item):
    """Why an item is locked, for Admin's list and the item page: None for an ordinary item, else
    {"kind": "flag", "by": <actor>, "at": <epoch>} or {"kind": "type", "type": <label>} (a flagged
    item of a restricted type reports the flag)."""
    if is_flagged(item):
        return {"kind": "flag", "by": item.get("sensitive_by"), "at": item.get("sensitive_at")}
    if is_type_restricted(item):
        from core import object_types  # lazy: type modules import core.db
        return {"kind": "type", "type": object_types.get_object_type(item.get("media_type")).label}
    return None


def _viewer_user_id(actor):
    """The users.id of a signed-in, enabled user actor (role viewer or above); None otherwise."""
    if not (isinstance(actor, str) and actor.startswith("user:")):
        return None
    if not roles.at_least(roles.role_of(actor), roles.VIEWER):
        return None  # disabled or deleted: no role, so no uploader exception either
    from core import users  # lazy: users imports core.db
    return users.user_id_for_actor(actor)


def is_uploader(item, actor=None):
    """Is `actor` the signed-in user who uploaded this item (#604 step 1)?"""
    owner = item.get("uploaded_by_user_id") if item else None
    if owner is None:
        return False
    uid = _viewer_user_id(actor_ctx.resolve(actor))
    return uid is not None and uid == owner


def can_view(item, actor=None):
    """May `actor` (default: the current actor context) see this item? THE one switch.
    Ordinary item: yes. Sensitive item (restricted type or flagged): an admin, or its uploader."""
    if item is None:
        return False
    if not is_restricted(item):
        return True
    if RESTRICTED_VIEW_ROLE is None:
        return True
    who = actor_ctx.resolve(actor)
    if roles.at_least(roles.role_of(who), RESTRICTED_VIEW_ROLE):
        return True
    return is_uploader(item, who)


def require_view(item, actor=None, message="not found"):
    """The item if `actor` may see it; else NotFound (404 `not_found`), the same answer a missing
    item gets. Callers keep their own "no such row" check first so that answer stays exactly as
    it was; this only adds the policy refusal."""
    if not can_view(item, actor):
        raise NotFound(message)
    return item


def viewable_item(slug, message="not found", actor=None):
    """The item row for `slug`, or NotFound (404 `not_found`) when there is no such row OR the actor
    may not see it. For every single-item web door, reads AND writes (#467 step 2: an editor who
    somehow holds a restricted item's slug can't edit, redact, delete or relate it either, and the
    answer doesn't reveal that it exists)."""
    from core import db  # lazy: core.db imports this module
    row = db.get_by_slug(slug)
    if row is None:
        raise NotFound(message)
    return require_view(row, actor, message)


def require_file(item, actor=None, message="not found"):
    """The public file doors (/f/<slug> and its thumbnail, #467 step 2: "public, except restricted
    and redacted items, which need an admin"). `require_view` (restricted -> admin), then a redacted
    item -> NotFound unless the actor holds REDACTED_FILE_ROLE. The caller keeps its own "no such
    row" 404 first and its own 410 for the admin who asks for a redacted file."""
    require_view(item, actor, message)
    if item.get("redacted") and not roles.at_least(roles.role_of(actor_ctx.resolve(actor)), REDACTED_FILE_ROLE):
        raise NotFound(message)
    return item


def filter_visible(items, actor=None):
    """The items `actor` may see, order kept (restricted items only for an admin)."""
    return [i for i in items if can_view(i, actor)]


def sql_browse_clause(prefix="", actor=None):
    """General-browsing filter, as SQL, for core/db.py's search/gallery/home/tag/unfiled queries,
    where filtering in Python would mean fetching every row. Formerly db._not_restricted.
      * Restricted TYPES are never in general browsing, for any role (an admin finds them in
        /admin's restricted list): " AND <prefix>media_type NOT IN (...)".
      * FLAGGED items (#603) are in browsing only for an admin and for their uploader:
        " AND (COALESCE(<prefix>sensitive, 0) = 0 OR <prefix>uploaded_by_user_id = <id>)", or
        nothing at all for an admin. Since OCR text and the embedding are only ever searched
        through these queries, a flagged item's text never surfaces for anyone else.
    `actor` defaults to the current actor context. Type keys are registry identifiers, validated
    before being inlined; the user id is an int from the users table."""
    from core import object_types  # lazy: type modules import core.db
    out = ""
    keys = object_types.restricted_types()
    if keys:
        assert all(re.fullmatch(r"[a-z0-9_]+", k) for k in keys), keys
        out += f" AND {prefix}media_type NOT IN ({', '.join(repr(k) for k in keys)})"
    who = actor_ctx.resolve(actor)
    if RESTRICTED_VIEW_ROLE is not None and not roles.at_least(roles.role_of(who), RESTRICTED_VIEW_ROLE):
        uid = _viewer_user_id(who)
        if uid is None:
            out += f" AND COALESCE({prefix}sensitive, 0) = 0"
        else:
            out += f" AND (COALESCE({prefix}sensitive, 0) = 0 OR {prefix}uploaded_by_user_id = {int(uid)})"
    return out


def exportable(item):
    """May this item leave the install (static site export, project zip, an agent's caption work)?
    Never when sensitive (a restricted type or flagged), whoever asks: an export is read by people
    and agents outside the install."""
    return item is not None and not is_restricted(item)


def filter_exportable(items):
    return [i for i in items if exportable(i)]


def note_access(item, how, actor=None):
    """Record that `actor` opened / previewed / downloaded a SENSITIVE item through door `how`
    (#604 follow-up 7). A no-op for an ordinary item. Call it after the door's policy check passed."""
    if item is not None and is_restricted(item):
        from core import access_log  # lazy: access_log imports core.db
        access_log.record(item["slug"], how, actor)


def can_unmark_sensitive(actor=None):
    """Clearing the #603 flag is admin-only (marking is any editor: locking is the safe direction)."""
    return roles.at_least(roles.role_of(actor_ctx.resolve(actor)), roles.ADMIN)
