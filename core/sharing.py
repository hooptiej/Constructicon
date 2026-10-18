"""Private cards and hobbies, and who else may see them (#604 build step 3).

Owner decisions (#604, 2026-10-07 table + follow-ups, and 2026-10-17): "shared by default, private
with a check box on the card". Cards and hobbies are OPEN (every signed-in user sees them and their
items) unless ticked Private. A private card or hobby is seen by admins, its owner
(created_by_user_id, #604 step 1; NULL = admin-owned) and the members listed here, each a viewer or
an editor of it. Privacy is inherited downward (a hobby's cards, a card's nested parts, a family's
members), and a child can never be more open than its parent: core/policy.py decides all of that.

This module is the ONE writer (service layer, #541): validate first, one db.transaction(), every row
through db.ImageLog (row images, so the generic undo reverses it), a Result, dry_run, actor from the
context. Ops:

  state(kind, target)                    the sharing panel: private / inherited / owner / members
  set_private(kind, target, private)     tick or untick Private (projects.private / hobby_settings.private)
  add_member(kind, target, user, role)   add a member, or change a member's role (viewer | editor)
  remove_member(kind, target, user)      take a member off

`kind` is "card" or "hobby"; `target` a card id/slug or a hobby id/slug; `user` a user id or name.

Who may change it (follow-ups 4 and 6): the card's / hobby's OWNER (holding the editor role) or an
admin; everyone else gets 403 forbidden saying who can. A card the actor can't see is 404 not_found
(the same answer as a missing card). The install role is the ceiling (follow-up 8): a viewer can't be
made a card editor (400 role_ceiling, saying how to fix it). Members stay listed when Private is
unticked, so ticking it again restores the same list; they matter only while the card is private.

What a viewer member may NOT do (follow-ups 2 and 4) is enforced for every logged write by
policy.check_write (called from db.insert_change_log): inside private territory only an admin, the owner
or an editor member changes a card or the files on it; filing your own upload is allowed. Not here
(later #604 steps): the owner-only edit rule for OPEN cards (step 4), the heir when a user is disabled or
deleted (follow-up 5).
"""

import time

from . import actor as actor_ctx, cards, changes, db, policy, roles, users
from .cards import Result
from .errors import AppError, InvalidInput, NotFound

OP_PRIVATE = "share_private"
OP_MEMBER_ADD = "share_member_add"
OP_MEMBER_REMOVE = "share_member_remove"

CARD, HOBBY = policy.CARD, policy.HOBBY


def _kind(kind):
    k = (kind or "").strip().lower()
    if k not in policy.SHARE_KINDS:
        raise InvalidInput(f"Unknown kind {kind!r}: give 'card' or 'hobby'.", code="bad_kind")
    return k


def _target(kind, target):
    """(row, owner_user_id, display name). A hidden or missing target is 404 not_found."""
    if kind == CARD:
        row = cards.get_card(target)
        return row, row.get("created_by_user_id"), row["title"]
    row = cards.get_hobby(target)
    return row, db.hobby_privacy(row["id"])["created_by_user_id"], row["name"]


def _noun(kind):
    if kind == CARD:
        return "card"
    from . import install_config  # lazy: install_config imports core.db
    return install_config.hobby_label().one_lc


def _require_manage(kind, owner, name, actor):
    if not policy.can_manage_sharing(owner, actor):
        owner_txt = (users.owner_info(owner) or {}).get("name") if owner is not None else None
        who = f"its owner ({owner_txt}) or an admin" if owner_txt else "an admin (it has no owner on record)"
        raise AppError("forbidden", f"Nothing was changed: only {who} can change who sees the {_noun(kind)} "
                                    f"'{name}'.", status=403, details={"kind": kind})


def _user(user):
    try:
        return users.get(user)
    except NotFound:
        raise NotFound(f"No user {user!r}: pick someone from the user list (Admin > Users).") from None


def _member_public(m):
    return {"user_id": m["user_id"], "username": m.get("username"),
            "name": m.get("display_name") or m.get("username") or f"deleted user #{m['user_id']}",
            "role": m["role"], "user_role": m.get("user_role"), "disabled": bool(m.get("disabled")),
            "added_at": m.get("added_at"),
            # follow-up 8: a card role can only narrow the install role. Say when it does.
            "capped": bool(m["role"] == roles.EDITOR and m.get("user_role") == roles.VIEWER)}


def state(kind, target, actor=None):
    """The sharing panel for one card / hobby: {kind, id, title, slug, private (ticked here),
    effective (here or above), inherited_from [{kind, id, title, slug}], owner {id, username, name} |
    None, members [...], can_manage}. 404 for a card the actor can't see."""
    kind = _kind(kind)
    row, owner, name = _target(kind, target)
    st = policy.share_state(kind, row if kind == CARD else {**row, "private": db.hobby_privacy(row["id"])["private"]})
    return {"kind": kind, "id": row["id"], "title": name, "slug": row["slug"],
            "private": st["private"], "effective": st["effective"], "inherited_from": st["inherited_from"],
            "owner": users.owner_info(owner),
            "members": [_member_public(m) for m in db.share_members(kind, row["id"])],
            "can_manage": policy.can_manage_sharing(owner, actor)}


def set_private(kind, target, private, *, dry_run=False, actor=None, batch_id=None):
    """Ticks (True) or unticks (False) Private on a card or hobby. ONE imaged change-log row (op
    share_private), undoable; a no-op writes nothing. data: the new state()."""
    kind = _kind(kind)
    row, owner, name = _target(kind, target)
    _require_manage(kind, owner, name, actor)
    on = 1 if private else 0
    batch_id = batch_id or changes.new_batch_id()
    warnings = []
    with db.transaction(dry_run=dry_run):
        with db.ImageLog(OP_PRIVATE, actor, batch_id, [row["slug"]]) as log:
            if kind == CARD:
                log.update("projects", {"id": row["id"]}, {"private": on})
            else:
                key = {"hobby_tag_id": row["id"]}
                if log.get("hobby_settings", key) is None:
                    if on:
                        log.insert("hobby_settings", key, {"private": 1})
                else:
                    log.update("hobby_settings", key, {"private": on})
        rows = db.get_change_rows(batch_id=batch_id)
        data = state(kind, row["id"], actor)
    if not on and data["effective"]:
        above = ", ".join(f"'{i['title']}'" for i in data["inherited_from"])
        warnings.append(f"'{name}' is still private: it inherits that from {above}. Untick it there to open it.")
    if on and not data["members"] and owner is None:
        warnings.append(f"'{name}' has no owner on record, so only admins see it until you add members.")
    return Result(True, cards._changes_from_log(rows), warnings, batch_id if rows else None, dry_run, {"sharing": data})


def add_member(kind, target, user, role=roles.VIEWER, *, dry_run=False, actor=None, batch_id=None):
    """Adds `user` as a viewer or editor of a card / hobby, or changes their role there. ONE imaged
    row (op share_member_add), undoable. Refused: an unknown role (400 bad_role), editor for an
    install viewer (400 role_ceiling), an unknown user (404). Adding the owner, or someone already in
    that role, writes nothing and says so. data: the new state()."""
    kind = _kind(kind)
    role = (role or "").strip().lower()
    if role not in policy.MEMBER_ROLES:
        raise InvalidInput(f"Unknown member role {role!r}: give 'viewer' or 'editor'.", code="bad_role")
    row, owner, name = _target(kind, target)
    _require_manage(kind, owner, name, actor)
    u = _user(user)
    uname = u.get("display_name") or u["username"]
    if role == roles.EDITOR and u["role"] == roles.VIEWER:
        raise InvalidInput(f"Nothing was changed: {uname} is a viewer on this install, so they can't be an editor "
                           f"of '{name}' (a {_noun(kind)} role can only narrow the install role). Add them as a "
                           f"viewer, or ask an admin to make them an editor first.", code="role_ceiling")
    warnings = []
    if owner is not None and int(owner) == u["id"]:
        return Result(True, [], [f"{uname} owns '{name}' and always sees it; nothing to add."], None, dry_run,
                      {"sharing": state(kind, row["id"], actor)})
    if u["role"] == roles.ADMIN:
        warnings.append(f"{uname} is an admin and sees every {_noun(kind)} anyway.")
    if u.get("disabled"):
        warnings.append(f"{uname}'s account is disabled: they see nothing until an admin enables it.")
    batch_id = batch_id or changes.new_batch_id()
    key = {"target_kind": kind, "target_id": row["id"], "user_id": u["id"]}
    with db.transaction(dry_run=dry_run):
        with db.ImageLog(OP_MEMBER_ADD, actor, batch_id, [row["slug"]]) as log:
            current = log.get("share_members", key)
            if current is None:
                log.insert("share_members", key, {"role": role, "added_at": time.time()})
            elif current["role"] != role:
                log.update("share_members", key, {"role": role})
            else:
                warnings.append(f"{uname} is already a {role} of '{name}'.")
        rows = db.get_change_rows(batch_id=batch_id)
        data = state(kind, row["id"], actor)
    if not data["effective"]:
        warnings.append(f"'{name}' is open, so everyone signed in sees it already; members matter once you tick Private.")
    return Result(True, cards._changes_from_log(rows), warnings, batch_id if rows else None, dry_run, {"sharing": data})


def remove_member(kind, target, user, *, dry_run=False, actor=None, batch_id=None):
    """Takes `user` off a card's / hobby's member list. ONE imaged row (op share_member_remove),
    undoable. 404 when they aren't a member. data: the new state()."""
    kind = _kind(kind)
    row, owner, name = _target(kind, target)
    _require_manage(kind, owner, name, actor)
    u = _user(user)
    key = {"target_kind": kind, "target_id": row["id"], "user_id": u["id"]}
    batch_id = batch_id or changes.new_batch_id()
    with db.transaction(dry_run=dry_run):
        with db.ImageLog(OP_MEMBER_REMOVE, actor, batch_id, [row["slug"]]) as log:
            if not log.delete("share_members", key):
                raise NotFound(f"{u.get('display_name') or u['username']} is not a member of '{name}'.")
        rows = db.get_change_rows(batch_id=batch_id)
        data = state(kind, row["id"], actor)
    return Result(True, cards._changes_from_log(rows), [], batch_id, dry_run, {"sharing": data})


def user_choices(actor=None):
    """The people a card owner can add (enabled users, not the actor): [{id, username, name, role}].
    Editor and above only (the routes / tool say so); no hashes, no sign-in times."""
    me = users.user_id_for_actor(actor_ctx.resolve(actor))
    return [{"id": u["id"], "username": u["username"], "name": u.get("display_name") or u["username"],
             "role": u["role"]}
            for u in db.list_users() if not u.get("disabled") and u["id"] != me]


# --- the change log: undo and listing (#604 step 3) -----------------------------------------------

_SHARE_TABLE_FIELDS = {"projects": "private", "hobby_settings": "private"}


def check_undo_allowed(rows, actor=None):
    """Called by the generic undo before it writes anything. Undoing a sharing change (Private
    ticked / unticked, a member added / removed) needs the same right as making it (the target's
    owner or an admin); undoing anything that touches a card, hobby or item the actor can't see is
    refused too. 403 forbidden, saying why; nothing written."""
    who = actor_ctx.resolve(actor)
    if roles.at_least(roles.role_of(who), roles.ADMIN):
        return
    for r in rows:
        if not policy.change_row_visible(r, who):
            raise AppError("forbidden", f"Nothing was undone: change {r['id']} ({r['op']}) touches a private card, "
                                        f"{_noun(HOBBY)} or item you can't see.", status=403,
                           details={"audit_id": r["id"]})
        for m in r.get("mutations") or []:
            t, key = m.get("table"), m.get("key") or {}
            target = None
            if t == "share_members":
                target = (key.get("target_kind"), key.get("target_id"))
            elif t in _SHARE_TABLE_FIELDS and _SHARE_TABLE_FIELDS[t] in ((m.get("before") or {}) | (m.get("after") or {})):
                target = (CARD, key.get("id")) if t == "projects" else (HOBBY, key.get("hobby_tag_id"))
            if target is None or target[1] is None:
                continue
            kind, tid = target
            owner = (db.get_project(tid) or {}).get("created_by_user_id") if kind == CARD \
                else db.hobby_privacy(tid)["created_by_user_id"]
            if not policy.can_manage_sharing(owner, who):
                raise AppError("forbidden", f"Nothing was undone: change {r['id']} changes who sees a {_noun(kind)}, "
                                            f"which only its owner or an admin can do.", status=403,
                               details={"audit_id": r["id"]})


def visible_change_rows(rows, actor=None):
    """The change-log rows `actor` may read: all of them for an admin; otherwise not the ones that
    touch a card, hobby or item hidden from them (a row image carries the whole row)."""
    who = actor_ctx.resolve(actor)
    if roles.at_least(roles.role_of(who), roles.ADMIN):
        return list(rows)
    acc = policy.card_access(who)
    return [r for r in rows if policy.change_row_visible(r, who, access=acc)]

