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


def is_restricted(item):
    """True when this row's type is restricted (#443)."""
    from core import object_types  # lazy: type modules import core.db
    return object_types.is_restricted(item)


def can_view(item, actor=None):
    """May `actor` (default: the current actor context) see this item? THE one switch."""
    if item is None:
        return False
    if not is_restricted(item):
        return True
    if RESTRICTED_VIEW_ROLE is None:
        return True
    return roles.at_least(roles.role_of(actor_ctx.resolve(actor)), RESTRICTED_VIEW_ROLE)


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
    """General-browsing filter, as SQL (" AND <prefix>media_type NOT IN (...)", or "" when no type
    is restricted), for core/db.py's search/gallery/home/tag/unfiled queries, where filtering in
    Python would mean fetching every row. Restricted items are never in general browsing, for any
    role (an admin finds them in /admin's "Keys & certificates" list). Formerly db._not_restricted.
    Type keys are registry identifiers, validated before being inlined. `actor` is accepted so
    #467 can widen this per role without touching the callers."""
    from core import object_types  # lazy: type modules import core.db
    keys = object_types.restricted_types()
    if not keys:
        return ""
    assert all(re.fullmatch(r"[a-z0-9_]+", k) for k in keys), keys
    return f" AND {prefix}media_type NOT IN ({', '.join(repr(k) for k in keys)})"


def exportable(item):
    """May this item leave the install (static site export, project zip)? Never when restricted,
    whoever asks: an export is read by people and agents outside the install."""
    return item is not None and not is_restricted(item)


def filter_exportable(items):
    return [i for i in items if exportable(i)]
