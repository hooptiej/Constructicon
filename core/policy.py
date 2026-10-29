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
  * writes (#709): the service ops refuse an item the actor can't see (403 forbidden saying why)
    before they write: `viewable_rows(slugs)` for a set (items.delete, membership add/remove, tag
    merge) and for one item (items.get_item, tags' item lookup). set_sensitive / set_provenance keep
    their own `can_view` check and 404 (#603 / #553).

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

#604 step 3 (owner decisions 2026-10-07 + 2026-10-17): PRIVATE cards and hobbies. Cards are open
(every signed-in user sees them and their items) unless ticked private (projects.private,
hobby_settings.private). A private card / hobby is seen only by admins, its owner
(created_by_user_id; NULL = admin-owned, so admins only) and its members (share_members, viewer or
editor; the member must still hold an install role, viewer or above). Privacy is INHERITED DOWNWARD
along every grouping edge: a hobby -> its cards (project_hobbies), a card -> the cards nested in it
(projects.parent_id), a family / collection -> its members (family_members). A card is visible only
when the actor may see EVERY private node above it (and itself): a child can never be more open than
its parent, and a private child with its own list is closed to parent members not on it. An item is
hidden when ANY card holding it is hidden ("private wins", follow-up 1), or when it is tagged directly
with a hidden hobby (the hobby's loose objects). Unfiled items stay visible to every signed-in user.
  * `card_access(actor)` -> Access (what this actor can't see); `can_view_card`, `can_view_hobby`,
    `require_card_view`, `filter_visible_cards`, `filter_visible_hobbies`; `can_view` / `filter_visible`
    / `viewable_rows` / `sql_browse_clause` apply it to items.
  * Hotlinks are NOT changed (owner rule): `/f/<slug>` and its thumbnail stay public for an ordinary
    item even inside a private card (`require_file` checks only the sensitive rule).
  * `can_manage_sharing`: who may tick private / edit members (the card's or hobby's owner, or an admin).
  * `outsider_access()`: what someone with no access at all can't see (the static site export uses it:
    nothing under a private card or hobby is published).
"""

import contextlib
import contextvars
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


def _can_view_sensitive(item, who):
    """The #603 rule alone: an ordinary item -> yes; a sensitive one -> an admin or its uploader."""
    if item is None:
        return False
    if not is_restricted(item):
        return True
    if RESTRICTED_VIEW_ROLE is None:
        return True
    if roles.at_least(roles.role_of(who), RESTRICTED_VIEW_ROLE):
        return True
    return is_uploader(item, who)


# --- #604 step 3: private cards and hobbies -------------------------------------------------------

CARD, HOBBY = "card", "hobby"
SHARE_KINDS = (CARD, HOBBY)
MEMBER_ROLES = (roles.VIEWER, roles.EDITOR)


class Access:
    """What one actor can NOT see because of private cards and hobbies (#604 step 3). Empty (the
    `OPEN` instance) for an admin, or when nothing on the install is private."""

    __slots__ = ("hidden_cards", "hidden_hobbies", "_slugs")

    def __init__(self, hidden_cards=frozenset(), hidden_hobbies=frozenset()):
        self.hidden_cards = frozenset(hidden_cards)
        self.hidden_hobbies = frozenset(hidden_hobbies)
        self._slugs = None

    @property
    def empty(self):
        return not self.hidden_cards and not self.hidden_hobbies

    def card_hidden(self, card_id):
        return card_id is not None and int(card_id) in self.hidden_cards

    def hobby_hidden(self, tag_id):
        return tag_id is not None and int(tag_id) in self.hidden_hobbies

    def hidden_slugs(self):
        """Every item slug this actor can't see through privacy (on a hidden card, or tagged with a
        hidden hobby). Read once, then kept on this object."""
        if self._slugs is None:
            from core import db  # lazy: core.db imports this module
            self._slugs = frozenset(db.slugs_in_cards(self.hidden_cards) | db.slugs_tagged(self.hidden_hobbies))
        return self._slugs

    def item_hidden(self, item):
        if self.empty or item is None or not item.get("slug"):
            return False
        if self._slugs is not None:
            return item["slug"] in self._slugs
        from core import db  # lazy: core.db imports this module
        held = db.item_holders(item["slug"])
        return any(int(c) in self.hidden_cards for c in held["cards"]) or \
            any(int(t) in self.hidden_hobbies for t in held["tags"])


OPEN = Access()


def _has_share(snap, kind, target_id, owner, uid):
    """Owner or member of one private node (`uid` None = nobody signed in, or no role)."""
    if uid is None:
        return False
    if owner is not None and int(owner) == uid:
        return True
    return uid in snap["members"].get((kind, target_id), {})


def _has_edit_share(snap, kind, target_id, owner, uid):
    """Owner or EDITOR member of one private node (#604 follow-up 4: a card viewer sees only)."""
    if uid is None:
        return False
    if owner is not None and int(owner) == uid:
        return True
    return snap["members"].get((kind, target_id), {}).get(uid) == roles.EDITOR


def _access_from(snap, uid, share=_has_share):
    blocked_hobbies = {h for h, owner in snap["hobbies"].items() if not share(snap, HOBBY, h, owner, uid)}
    stack = [c for c, owner in snap["cards"].items() if not share(snap, CARD, c, owner, uid)]
    for h in blocked_hobbies:
        stack.extend(snap["hobby_cards"].get(h, []))
    hidden = set()
    while stack:  # inherit downward: nested parts and family / collection members
        c = stack.pop()
        if c in hidden:
            continue
        hidden.add(c)
        stack.extend(snap["children"].get(c, []))
        stack.extend(snap["family"].get(c, []))
    return Access(hidden, blocked_hobbies)


# (actor, Access) for the block inside access_scope(): one computation for a whole list.
_SCOPED = contextvars.ContextVar("constructicon_policy_access", default=None)


@contextlib.contextmanager
def access_scope(actor=None):
    """Work out `actor`'s Access once and reuse it for every check inside the block (filter a list,
    check a set of slugs). Yields the Access. Keep the block short: it doesn't see writes made inside."""
    who = actor_ctx.resolve(actor)
    cur = _SCOPED.get()
    if cur is not None and cur[0] == who:
        yield cur[1]
        return
    acc = _compute_access(who)
    token = _SCOPED.set((who, acc))
    try:
        yield acc
    finally:
        _SCOPED.reset(token)


def card_access(actor=None):
    """(#604 step 3) The Access for `actor` (default: the current actor context). Admins (signed-in
    admins, the install token, the MCP's install token, scripts, the system) see everything."""
    who = actor_ctx.resolve(actor)
    cur = _SCOPED.get()
    if cur is not None and cur[0] == who:
        return cur[1]
    return _compute_access(who)


def _compute_access(who):
    if roles.at_least(roles.role_of(who), roles.ADMIN):
        return OPEN
    from core import db  # lazy: core.db imports this module
    snap = db.privacy_snapshot()
    if snap is None:
        return OPEN
    return _access_from(snap, _viewer_user_id(who))


def outsider_access():
    """(#604 step 3) What someone with no access to any private card or hobby can't see: everything
    under a private node. The static site export publishes none of it."""
    from core import db  # lazy: core.db imports this module
    snap = db.privacy_snapshot()
    return OPEN if snap is None else _access_from(snap, None)


def edit_access(actor=None):
    """(#604 step 3, follow-ups 2 and 4) What `actor` may NOT change inside private territory: the
    cards / hobbies under a private node where they are neither owner nor EDITOR member (a viewer
    member only looks). Open cards are never in it (who may change an open card is unchanged: any
    editor; the owner-only rule for open cards is #604 step 4, not built). Admins: nothing."""
    who = actor_ctx.resolve(actor)
    have = roles.role_of(who)
    if roles.at_least(have, roles.ADMIN):
        return OPEN
    from core import db  # lazy: core.db imports this module
    snap = db.privacy_snapshot()
    if snap is None:
        return OPEN
    uid = _viewer_user_id(who) if roles.at_least(have, roles.EDITOR) else None
    return _access_from(snap, uid, share=_has_edit_share)


# The tables whose row images name an item (by slug), for check_write.
_ITEM_TABLES = {"capture_events": ("slug",), "post_tags": ("post_slug",),
                "capture_event_relations": ("slug_a", "slug_b"), "item_revisions": ("old_slug", "new_slug"),
                "blog_entry_items": ("post_slug",), "trash": ("slug",)}
_SHARE_ONLY = {"projects": {"private"}, "hobby_settings": {"private"}}


def check_write(mutations, actor=None):
    """(#604 step 3) The write guard on PRIVATE cards and hobbies, run by db.insert_change_log for
    every logged write (so every service op, the undo and any future door are covered). Inside
    private territory only an admin, the owner or an EDITOR member may change a card, its nesting,
    families, hobbies, links or files, or a file on it; a viewer member only looks (follow-up 4).
    Filing onto a private card is the file's own uploader or an editor of that card (follow-up 2):
    the same rule, since a project_items row for your own upload is allowed. Ticking Private /
    editing members is checked by core/sharing.py (owner or admin) and skipped here.
    Raises AppError 403 forbidden (the transaction rolls back: nothing is written)."""
    who = actor_ctx.resolve(actor)
    if not mutations or roles.at_least(roles.role_of(who), roles.ADMIN):
        return
    ed = edit_access(who)
    if ed.empty:
        return
    from core import db  # lazy: core.db imports this module
    uid = _viewer_user_id(who)
    items = {}

    def item_row(slug, image=None):
        if slug not in items:
            items[slug] = db.get_by_slug(slug) or image or {}
        return items[slug]

    def own(slug, image=None):
        row = item_row(slug, image)
        return uid is not None and row.get("uploaded_by_user_id") is not None and int(row["uploaded_by_user_id"]) == uid

    def refuse(kind, ref):
        name = None
        if kind == CARD:
            row = db.get_project(ref)
            if row and can_view_card(row, who):
                name = f"the private card '{row['title']}'"
        elif kind == HOBBY:
            row = db.get_hobby(ref)
            if row and can_view_hobby(row, who):
                name = f"the private {_hobby_word()} '{row['name']}'"
        what = name or f"a private card or {_hobby_word()} you can only look at"
        from core.errors import AppError
        raise AppError("forbidden",
                       f"Nothing was changed: this touches {what}. Members who can only look can't change it or "
                       f"the files on it (their own uploads aside); ask its owner to make you an editor, or an admin "
                       f"to do it.", status=403, details={"reason": "private_view_only", "kind": kind, "ref": ref})

    def item_blocked(slug, image=None):
        held = db.item_holders(slug)
        return (any(ed.card_hidden(c) for c in held["cards"]) or any(ed.hobby_hidden(t) for t in held["tags"])) \
            and not own(slug, image)

    for m in mutations:
        table, key = m.get("table"), m.get("key") or {}
        before, after = m.get("before") or {}, m.get("after") or {}
        changed = set(before) | set(after)
        if table in _SHARE_ONLY and m.get("before") is not None and m.get("after") is not None \
                and changed <= _SHARE_ONLY[table]:
            continue  # ticking Private: core/sharing.py's owner-or-admin check
        if table == "projects":
            for cid in {key.get("id"), before.get("parent_id"), after.get("parent_id")}:
                if cid is not None and ed.card_hidden(cid):
                    refuse(CARD, cid)
        elif table == "project_items":
            cid, slug = key.get("project_id"), key.get("post_slug")
            if ed.card_hidden(cid) and not own(slug):
                refuse(CARD, cid)
        elif table == "project_hobbies":
            if ed.card_hidden(key.get("project_id")):
                refuse(CARD, key.get("project_id"))
            if ed.hobby_hidden(key.get("hobby_tag_id")):
                refuse(HOBBY, key.get("hobby_tag_id"))
        elif table == "family_members":
            for cid in (key.get("family_id"), key.get("member_id")):
                if ed.card_hidden(cid):
                    refuse(CARD, cid)
        elif table == "blog_entry_projects":
            if ed.card_hidden(key.get("project_id")):
                refuse(CARD, key.get("project_id"))
        elif table == "project_relations":
            for s in (key.get("slug_a"), key.get("slug_b")):
                row = db.get_project(s) if s else None
                if row and ed.card_hidden(row["id"]):
                    refuse(CARD, row["id"])
        elif table in ("hobby_settings", "blog_tags"):
            tid = key.get("hobby_tag_id", key.get("id"))
            if ed.hobby_hidden(tid):
                refuse(HOBBY, tid)
        if table == "post_tags" and ed.hobby_hidden(key.get("tag_id")) and not own(key.get("post_slug")):
            refuse(HOBBY, key.get("tag_id"))
        for col in _ITEM_TABLES.get(table, ()):
            slug = key.get(col)
            if slug and item_blocked(slug, after or before):
                held = db.item_holders(slug)
                hid = next((c for c in held["cards"] if ed.card_hidden(c)), None)
                if hid is not None:
                    refuse(CARD, hid)
                refuse(HOBBY, next(t for t in held["tags"] if ed.hobby_hidden(t)))


def _card_id(card):
    return card.get("id") if isinstance(card, dict) else card


def can_view_card(card, actor=None, access=None):
    """May `actor` see this card (a row or an id)? Open cards: anyone signed in. Private (itself or
    above): admins, and those who are owner or member of EVERY private card / hobby above it."""
    if card is None:
        return False
    acc = access if access is not None else card_access(actor)
    return not acc.card_hidden(_card_id(card))


def can_view_hobby(hobby, actor=None, access=None):
    """May `actor` see this hobby (a blog_tags row or its id)?"""
    if hobby is None:
        return False
    acc = access if access is not None else card_access(actor)
    return not acc.hobby_hidden(_card_id(hobby))


def require_card_view(card, actor=None, message="not found"):
    """The card if `actor` may see it, else NotFound (404 `not_found`: the same answer as a missing
    card, so a private card's existence isn't leaked)."""
    if not can_view_card(card, actor):
        raise NotFound(message)
    return card


def filter_visible_cards(cards, actor=None, access=None):
    """The cards (rows with "id", or dicts with "card_id") `actor` may see, order kept."""
    acc = access if access is not None else card_access(actor)
    if acc.empty:
        return list(cards)
    return [c for c in cards if not acc.card_hidden(c.get("id", c.get("card_id")))]


def filter_visible_hobbies(hobbies, actor=None, access=None):
    """The hobbies (rows with "id") `actor` may see, order kept."""
    acc = access if access is not None else card_access(actor)
    if acc.empty:
        return list(hobbies)
    return [h for h in hobbies if not acc.hobby_hidden(h.get("id"))]


def hidden_tag_ids(actor=None, access=None):
    """(#604 step 3) Tags that would name something hidden from `actor`: a hidden private hobby, or the
    linked tag of a hidden card (unless a card they CAN see links it too). For the tag tree."""
    acc = access if access is not None else card_access(actor)
    if acc.empty:
        return set()
    from core import db  # lazy: core.db imports this module
    out = set(acc.hidden_hobbies)
    for tag_id, card_ids in db.card_tag_links().items():
        if all(acc.card_hidden(c) for c in card_ids):
            out.add(tag_id)
    return out


def filter_tag_tree(tree, actor=None):
    """(#604 step 3) db.list_tag_tree() without the tags (and their sub-tags) hidden_tag_ids names."""
    hidden = hidden_tag_ids(actor)
    if not hidden:
        return tree

    def walk(nodes):
        return [{**n, "children": walk(n.get("children") or [])} for n in nodes if n["id"] not in hidden]
    return walk(tree)


def share_state(kind, target):
    """(#604 step 3) Whether a card / hobby is private and why: {"private": bool (ticked on this one),
    "effective": bool (this one or anything above it), "inherited_from": [{kind, id, title, slug}]
    (the private cards / hobbies above it, nearest first)}. `target` is a card row or a hobby row."""
    from core import db  # lazy: core.db imports this module
    if kind == HOBBY:
        priv = bool(db.hobby_privacy(target["id"])["private"])
        return {"private": priv, "effective": priv, "inherited_from": []}
    inherited, seen, queue = [], {target["id"]}, [("card", target["id"])]
    while queue:
        k, i = queue.pop(0)
        if k != "card":
            continue
        up = db.card_parents(i)
        for h in up["hobbies"]:
            if ("hobby", h) in seen:
                continue
            seen.add(("hobby", h))
            if db.hobby_privacy(h)["private"]:
                row = db.get_hobby(h)
                inherited.append({"kind": HOBBY, "id": h, "title": row["name"] if row else None,
                                  "slug": row["slug"] if row else None})
        for c in up["cards"]:
            if c in seen:
                continue
            seen.add(c)
            row = db.get_project(c)
            if row and row.get("private"):
                inherited.append({"kind": CARD, "id": c, "title": row["title"], "slug": row["slug"]})
            queue.append(("card", c))
    priv = bool(target.get("private"))
    return {"private": priv, "effective": priv or bool(inherited), "inherited_from": inherited}


# Change-log row images name cards, hobbies and items by these keys (table -> [(column, what)]).
_CHANGE_KEYS = {
    "projects": [("id", CARD)],
    "project_items": [("project_id", CARD), ("post_slug", "item")],
    "project_hobbies": [("project_id", CARD), ("hobby_tag_id", HOBBY)],
    "family_members": [("family_id", CARD), ("member_id", CARD)],
    "blog_entry_projects": [("project_id", CARD)],
    "hobby_settings": [("hobby_tag_id", HOBBY)],
    "blog_tags": [("id", HOBBY)],
    "capture_events": [("slug", "item")],
    "post_tags": [("post_slug", "item"), ("tag_id", HOBBY)],
    "capture_event_relations": [("slug_a", "item"), ("slug_b", "item")],
    "item_revisions": [("old_slug", "item"), ("new_slug", "item")],
    "blog_entry_items": [("post_slug", "item")],
    "trash": [("slug", "item")],
}


def change_row_visible(row, actor=None, access=None):
    """(#604 steps 3 and 5) May `actor` read this change-log row (or undo it)? Its row images carry
    whole rows, so a row that touches a card / hobby hidden from them, or an item they can't see
    (sensitive, or under a private card), is not theirs to read. Admins read every row."""
    who = actor_ctx.resolve(actor)
    if roles.at_least(roles.role_of(who), roles.ADMIN):
        return True
    acc = access if access is not None else card_access(who)
    from core import db  # lazy: core.db imports this module
    images = {}
    slugs = set()
    for m in row.get("mutations") or []:
        table, key = m.get("table"), m.get("key") or {}
        if table == "share_members":
            k, tid = key.get("target_kind"), key.get("target_id")
            if (k == CARD and acc.card_hidden(tid)) or (k == HOBBY and acc.hobby_hidden(tid)):
                return False
            continue
        for col, what in _CHANGE_KEYS.get(table, ()):
            val = key.get(col)
            if val is None:
                continue
            if what == CARD and acc.card_hidden(val):
                return False
            if what == HOBBY and acc.hobby_hidden(val):
                return False
            if what == "item":
                slugs.add(val)
        if table == "capture_events":
            img = m.get("after") or m.get("before")
            if img and key.get("slug"):
                images[key["slug"]] = {**images.get(key["slug"], {}), **img}
    for slug in slugs:
        item = db.get_by_slug(slug) or images.get(slug)
        if item is not None and not can_view({**item, "slug": slug}, who):
            return False
    return True


def can_manage_sharing(owner_user_id, actor=None):
    """(#604 follow-ups 4 and 6) Ticking Private and editing the member list: an admin, or the card's /
    hobby's owner (created_by_user_id) while they hold the editor role. NULL owner = admin-owned."""
    who = actor_ctx.resolve(actor)
    have = roles.role_of(who)
    if roles.at_least(have, roles.ADMIN):
        return True
    if owner_user_id is None or not roles.at_least(have, roles.EDITOR):
        return False
    uid = _viewer_user_id(who)
    return uid is not None and uid == int(owner_user_id)


def can_view(item, actor=None, access=None):
    """May `actor` (default: the current actor context) see this item? THE one switch.
    Ordinary item: yes, unless a private card or hobby hides it (#604 step 3). Sensitive item
    (restricted type or flagged): an admin, or its uploader (and the same privacy rule)."""
    if item is None:
        return False
    who = actor_ctx.resolve(actor)
    if not _can_view_sensitive(item, who):
        return False
    acc = access if access is not None else card_access(who)
    return not acc.item_hidden(item)


def require_view(item, actor=None, message="not found"):
    """The item if `actor` may see it; else NotFound (404 `not_found`), the same answer a missing
    item gets. Callers keep their own "no such row" check first so that answer stays exactly as
    it was; this only adds the policy refusal."""
    if not can_view(item, actor):
        raise NotFound(message)
    return item


def viewable_item(slug, message="not found", actor=None):
    """The item row for `slug`, or NotFound (404 `not_found`) when there is no such row OR the actor
    may not see it. For every single-item web READ door (#467 step 2: the answer doesn't reveal that a
    hidden item exists). Since #730 the single-item WRITE doors use `writable_item` (403 saying why)."""
    from core import db  # lazy: core.db imports this module
    row = db.get_by_slug(slug)
    if row is None:
        raise NotFound(message)
    return require_view(row, actor, message)


def writable_item(slug, message="not found", actor=None):
    """(#730) The single-item WRITE door: the item row for `slug`, NotFound (404) when there is no such
    row, and when the actor may not see it the same 403 `forbidden` the bulk doors give since #709
    (`refusal`: "Nothing was changed: this item is marked sensitive, and only its uploader or an admin
    can change it.", details {refused, reason, reasons}). Owner rule: a failure says why. Reads keep
    `viewable_item` (404, so a page never confirms that a hidden item exists); a write names the slug
    itself, so echoing it back reveals nothing the caller didn't send, exactly as #709 settled.
    Not for `items.set_sensitive` / `set_provenance` (they keep their own 404, owner note on PR #727)."""
    return writable_items([(slug, message)], actor=actor)[0]


def writable_items(pairs, actor=None):
    """(#730) `writable_item` for a request that names two items (relate, supersedes): [(slug,
    missing_message)]. Every missing one is 404 with its own message first; then one `refusal` over
    the hidden ones ("1 of the 2 selected items ..."), so nothing is written. Returns the rows."""
    from core import db  # lazy: core.db imports this module
    who = actor_ctx.resolve(actor)
    rows = []
    for slug, message in pairs:
        row = db.get_by_slug(slug)
        if row is None:
            raise NotFound(message)
        rows.append(row)
    refused = [(r["slug"], _refusal_reason(r, who)) for r in rows if not can_view(r, who)]
    if refused:
        raise refusal(refused, len(rows))
    return rows


REFUSED_SENSITIVE = "sensitive"           # flagged "This is sensitive" (#603)
REFUSED_RESTRICTED_TYPE = "restricted_type"  # a restricted file type: keys, certificates (#443)
REFUSED_PRIVATE = "private"               # on a private card / hobby the actor isn't on (#604 step 3)
REFUSED_HIDDEN = "hidden"                 # none of those, but can_view still said no (a future rule)


def _refusal_reason(item, who=None):
    if not _can_view_sensitive(item, actor_ctx.resolve(who)):
        if is_flagged(item):
            return REFUSED_SENSITIVE
        if is_type_restricted(item):
            return REFUSED_RESTRICTED_TYPE
    if card_access(who).item_hidden(item):
        return REFUSED_PRIVATE
    return REFUSED_HIDDEN


def _hobby_word():
    """The install's word for a hobby, lower case, for a refusal message (#700)."""
    from core import install_config  # lazy: install_config imports core.db
    return install_config.hobby_label().one_lc


def refusal(refused, total):
    """(#709, owner decision 2026-10-17: "tell them no, and why") The 403 `forbidden` a write gets
    when it names items the actor may not see: says how many and why, nothing was changed.
    `refused` = [(slug, reason)], `total` = how many items the request named.
    details: {refused: [slugs], reason: sensitive | restricted_type | hidden | mixed,
    reasons: {slug: reason}}."""
    from core.errors import AppError
    n = len(refused)
    reasons = {slug: reason for slug, reason in refused}
    kinds = set(reasons.values())
    reason = kinds.pop() if len(kinds) == 1 else "mixed"
    if total == 1:
        which, verb, pron = "this item", "is", "it"
    elif n == 1:
        which, verb, pron = f"1 of the {total} selected items", "is", "it"
    else:
        which, verb, pron = f"{n} of the {total} selected items", "are", "them"
    if reason == REFUSED_SENSITIVE:
        why = f"{verb} marked sensitive, and only {'its' if n == 1 else 'their'} uploader or an admin can change {pron}"
    elif reason == REFUSED_RESTRICTED_TYPE:
        why = (f"{verb} {'a restricted file type' if n == 1 else 'restricted file types'} (keys, certificates), "
               f"which only an admin or {'its' if n == 1 else 'their'} uploader can change")
    elif reason == REFUSED_PRIVATE:
        why = (f"{verb} on a private card or {_hobby_word()} you aren't a member of; ask its owner or an admin "
               f"to add you, or to change {pron}")
    elif reason == REFUSED_HIDDEN:
        why = f"{verb} not visible to you, so you can't change {pron}"
    elif REFUSED_PRIVATE in set(reasons.values()):
        why = (f"{verb} marked sensitive, a restricted file type or on a private card you aren't a member of, "
               f"and only their owners, members or an admin can change {pron}")
    else:
        why = (f"{verb} marked sensitive or a restricted file type, and only their uploaders or an admin "
               f"can change {pron}")
    return AppError("forbidden", f"Nothing was changed: {which} {why}.", status=403,
                    details={"refused": [s for s, _ in refused], "reason": reason, "reasons": reasons})


def viewable_rows(slugs, *, missing_ok=False, actor=None):
    """(#709) The write-side door for a SET of items (bulk delete, add-to-card, tagging, and the
    single-item ops; the service ops call it before they write anything). Returns (rows, missing): the
    item rows in the order given (blanks and repeats dropped) when the actor may see EVERY one.
      * An unknown slug raises NotFound ("No item 'x'."), unless `missing_ok` (the bulk paths that
        always skipped unknown slugs): then it is left out and listed in `missing`.
      * Items that exist but the actor may not see refuse the WHOLE request with `refusal(...)`:
        403 `forbidden`, a message saying how many and why, the slugs in details. Nothing written.
        (Owner decision on #709: say no, and why. The caller named those slugs, so echoing them
        back reveals nothing they didn't send.)"""
    from core import db  # lazy: core.db imports this module
    wanted = [slugs] if isinstance(slugs, str) else list(slugs or [])
    wanted = list(dict.fromkeys((s or "").strip() for s in wanted if (s or "").strip()))
    who = actor_ctx.resolve(actor)
    rows, missing, refused = [], [], []
    with access_scope(who):
        for slug in wanted:
            row = db.get_by_slug(slug)
            if row is None:
                if not missing_ok:
                    raise NotFound(f"No item {slug!r}.")
                missing.append(slug)
            elif not can_view(row, who):
                refused.append((slug, _refusal_reason(row, who)))
            else:
                rows.append(row)
    if refused:
        raise refusal(refused, len(wanted))
    return rows, missing


def require_file(item, actor=None, message="not found"):
    """The public file doors (/f/<slug> and its thumbnail, #467 step 2: "public, except restricted
    and redacted items, which need an admin"). `require_view` (restricted -> admin), then a redacted
    item -> NotFound unless the actor holds REDACTED_FILE_ROLE. The caller keeps its own "no such
    row" 404 first and its own 410 for the admin who asks for a redacted file.
    #604 step 3, owner rule: a hotlink is the deliberate share, so an ORDINARY item's file stays public
    even inside a private card (only the sensitive rule applies here, not card privacy)."""
    who = actor_ctx.resolve(actor)
    hidden_only_by_privacy = _can_view_sensitive(item, who) and card_access(who).item_hidden(item)
    if not (can_view(item, who) or hidden_only_by_privacy):
        raise NotFound(message)
    if item.get("redacted") and not roles.at_least(roles.role_of(who), REDACTED_FILE_ROLE):
        raise NotFound(message)
    return item


def filter_visible(items, actor=None):
    """The items `actor` may see, order kept (restricted items only for an admin or their uploader;
    items under a private card or hobby only for its owner, members and admins, #604 step 3)."""
    who = actor_ctx.resolve(actor)
    items = list(items)
    with access_scope(who) as acc:
        if not acc.empty and len(items) > 3:
            acc.hidden_slugs()  # one read for the whole list instead of one per item
        return [i for i in items if can_view(i, who)]


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
    # #604 step 3: items on a card, or tagged with a hobby, this actor can't see (ids are ints).
    acc = card_access(who)
    if acc.hidden_cards:
        ids = ", ".join(str(int(i)) for i in sorted(acc.hidden_cards))
        out += f" AND {prefix}slug NOT IN (SELECT post_slug FROM project_items WHERE project_id IN ({ids}))"
    if acc.hidden_hobbies:
        ids = ", ".join(str(int(i)) for i in sorted(acc.hidden_hobbies))
        out += f" AND {prefix}slug NOT IN (SELECT post_slug FROM post_tags WHERE tag_id IN ({ids}))"
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


def can_unmark_sensitive(actor=None, item=None):
    """Clearing the #603 flag: an admin, or the item's own uploader (owner decision on #603,
    2026-10-07: "unmark is the uploader or an admin"). Marking is any editor: locking is the
    safe direction. Without an item only an admin qualifies."""
    who = actor_ctx.resolve(actor)
    if roles.at_least(roles.role_of(who), roles.ADMIN):
        return True
    return item is not None and is_uploader(item, who)
