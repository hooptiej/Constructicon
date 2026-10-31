"""The MCP tools of one area. Hobbies (#360): list / create / update, convert, unmark, card_hobby (+
aliases).

#547 step 3: moved verbatim from mcp_server/server.py, in its order (the order of the
modules in mcp_server/tools/__init__.py is the tools/list order).
"""

import typing

from mcp_server.app import mcp
from mcp_server.shapes import _hobby_creator
from mcp_server._common import _Collect, _only_for, _written

from core import policy, cards, db, changes, hobbies
from core.errors import InvalidInput, NotFound


# --- Hobbies (#360) ---

@mcp.tool()
def constructicon_list_hobbies() -> list[dict]:
    """All hobbies, by name: {id, name, slug, status (active | inactive, set by hand), group_code,
    project_count, flags, cover_slug, cover_picked, cover_source (picked | highlighted_project |
    recent_project | null), cover_project, picked_cover_slug, picked_cover_unavailable, created_by_user,
    created_via ("<user>'s <token name>")}.
    flags are computed, never stored: inactive_with_active_work, active_untouched (~2 years)."""
    out = []
    for h in policy.filter_visible_hobbies(db.list_hobbies()):  # #604 step 3
        f = cards.hobby_fields(h)
        out.append({
            "id": f["id"],
            "name": f["name"],
            "slug": f["slug"],
            "status": f["status"],
            "group_code": f["group_code"],
            "project_count": h.get("project_count", 0),
            "flags": f["flags"],
            **cards.hobby_cover_fields(cards.resolve_hobby_cover(h)),
            **_hobby_creator(h["id"]),  # #604 / #778
        })
    return out


@mcp.tool()
def constructicon_create_hobby(name: str, status: str = "active") -> dict:
    """Create a hobby from a name (a top-level tag, reused if it exists, marked as a hobby).
    status: active (default) | inactive. Returns {id, name, slug, status, group_code, batch_id}."""
    result = hobbies.create(name, status)
    return {**result.data["hobby"], "batch_id": result.batch_id}


def _convert_card_to_hobby(project_slug, dry_run):
    project = db.get_project(project_slug)
    if project is None or not policy.can_view_card(project):  # #604 step 3: hidden = missing
        raise NotFound(f"No card {project_slug!r}.")
    result = hobbies.convert_from_card(project["id"], dry_run=dry_run)
    hobby = result.data["hobby"]
    return {
        "id": hobby["id"],
        "name": hobby["name"],
        "slug": hobby["slug"],
        "status": hobby["status"],
        "summary": {
            "children_moved": result.data["children_moved"],
            "items_moved": result.data["items_moved"],
        },
        "dry_run": result.dry_run,
        "batch_id": result.batch_id,
        "changes": result.changes,
        "warnings": result.warnings,
    }


@mcp.tool(selector="direction")
def constructicon_convert(direction: typing.Literal["card_to_hobby", "hobby_to_card"], card: str | int | None = None,
                          hobby: str | int | None = None, kind: str | None = None, title: str | None = None,
                          into_hobby: str | int | None = None, dry_run: bool = False) -> dict:
    """Turn a card into a hobby or a hobby into a card, as ONE undoable batch.
    "card_to_hobby" (card=): the card becomes a hobby tag; nested cards join the hobby, its files
      get the tag, the card row goes (links, families, blog attachments, a blank write-up cleared).
      -> {id, name, slug, status, summary: {children_moved, items_moved}, dry_run, batch_id,
      changes, warnings}.
    "hobby_to_card" (hobby=, kind = family | collection | project, title (default the hobby's
      name), into_hobby (a hobby it joins)): the card reuses the hobby's tag; stage follows the
      hobby (active -> in_progress, inactive -> paused); member cards become family members or
      nested parts; loose files go onto it; homes that named the hobby name the card; the hobby is
      unmarked. Refused whole (nothing written): bad_membership, nest_group_kind,
      nest_second_parent. -> {ok, dry_run, changes, warnings, batch_id, card, card_id, kind,
      title, into_hobby, hobby, members: [{slug, how}], loose_moved, homes_moved}.
    E.g. direction="hobby_to_card", hobby="gi-joe", kind="family", into_hobby="collecting".
    """
    if direction == "card_to_hobby":
        if card is None:
            raise InvalidInput('direction="card_to_hobby" needs card.')
        _only_for('direction="card_to_hobby"', hobby=(hobby, None), kind=(kind, None), title=(title, None),
                  into_hobby=(into_hobby, None))
        return _convert_card_to_hobby(card, dry_run)
    if direction == "hobby_to_card":
        if hobby is None or not kind:
            raise InvalidInput('direction="hobby_to_card" needs hobby and kind (family, collection or project).')
        _only_for('direction="hobby_to_card"', card=(card, None))
        return hobbies.convert_to_card(hobby, kind, title, into_hobby, dry_run=dry_run).to_dict()
    raise InvalidInput(f"Unknown direction {direction!r}: use card_to_hobby or hobby_to_card.")


@mcp.tool(alias_of=("constructicon_convert", "card_to_hobby"))
def constructicon_convert_project_to_hobby(project_slug: str, dry_run: bool = False) -> dict | None:
    return _convert_card_to_hobby(project_slug, dry_run)


@mcp.tool(alias_of=("constructicon_convert", "hobby_to_card"))
def constructicon_convert_hobby_to_card(hobby: str | int, kind: str, title: str | None = None,
                                        into_hobby: str | int | None = None, dry_run: bool = False) -> dict:
    return hobbies.convert_to_card(hobby, kind, title, into_hobby, dry_run=dry_run).to_dict()


@mcp.tool()
def constructicon_unmark_hobby(hobby: str | int, dry_run: bool = False) -> dict:
    """Stop treating a tag as a hobby: its status and group code go, its card memberships are
    removed, and cards whose home was it go back to automatic. The tag and its files stay.
    -> {ok, dry_run, changes, warnings, batch_id, hobby, cards_removed, homes_cleared}."""
    return hobbies.unmark(hobby, dry_run=dry_run).to_dict()


@mcp.tool(selector="action")
def constructicon_card_hobby(card: str | int, hobby: str | int, action: typing.Literal["add", "remove"] = "add",
                             dry_run: bool = False, batch_id: str | None = None) -> dict:
    """Put a card in a hobby ("add", default; a card can be in several; twice is a no-op) or take
    it out ("remove"). -> {ok, dry_run, changes, warnings, batch_id}."""
    if action == "add":
        return cards.add_to_hobby(card, hobby, dry_run=dry_run, batch_id=batch_id).to_dict()
    if action == "remove":
        return cards.remove_from_hobby(card, hobby, dry_run=dry_run, batch_id=batch_id).to_dict()
    raise InvalidInput(f"Unknown action {action!r}: use add or remove.")


def _hobby_or_404(hobby):
    row = db.get_hobby(hobby)
    if row is None or not policy.can_view_hobby(row):  # #604 step 3: hidden = missing
        raise NotFound(f"No hobby {hobby!r}.")
    return row


@mcp.tool()
def constructicon_update_hobby(hobby: str | int, status: str | None = None, group_code: str | None = None,
                               cover_slug: str | None = None, clear_cover: bool = False,
                               synopsis: str | None = None, flavor: str | None = None,
                               name: str | None = None, cover_card: str | int | None = None,
                               dry_run: bool = False) -> dict:
    """Edit a hobby, every change in ONE batch. hobby: id or slug. Left out (None) = left alone.
    name: rename it; its page address (slug) follows and the old address redirects.
    status: active | inactive (the old words dormant / abandoned are stored as inactive, with a
      warning). group_code: 2-4 letters/digits shown on cards, unique, stored upper case.
    cover_slug: an item with a picture as its cover; cover_card: one of its cards (id or slug) whose
      cover it borrows and follows; clear_cover=true goes back to the automatic cover (its
      highlighted project's, else its latest project's). Only one of the three.
    synopsis (max 600) / flavor (max 140): its card face text; "" clears.
    dry_run=true answers as the call would and writes nothing.
    -> {ok, id, name, slug, status, group_code, flags, synopsis, flavor, cover: {cover_slug,
    cover_picked, cover_source, ...}, changes, warnings, batch_id, dry_run}.
    Codes: bad_hobby_activity, bad_group_code, group_code_conflict, bad_cover (details.reason),
    bad_card_text, hobby_name_conflict.
    """
    if sum(1 for x in (cover_slug, cover_card) if x not in (None, "")) + bool(clear_cover) > 1:
        raise InvalidInput("Pass only one of cover_slug, cover_card or clear_cover=true.")
    hid = _hobby_or_404(hobby)["id"]
    take = _Collect()
    batch_id = changes.new_batch_id()
    with db.transaction(dry_run=dry_run):
        if cover_slug is not None or cover_card not in (None, "") or clear_cover:  # first: set_cover reports the batch so far
            take(hobbies.set_cover(hid, None if clear_cover else cover_slug,
                                   cover_card=None if clear_cover else cover_card, batch_id=batch_id))
        if name is not None:
            take(hobbies.rename(hid, name, batch_id=batch_id))
        if status is not None:
            take(cards.set_hobby_activity(hid, status, batch_id=batch_id))
        if group_code is not None:
            take(cards.set_group_code(hid, group_code, batch_id=batch_id))
        if synopsis is not None or flavor is not None:
            take(hobbies.set_text(hid, synopsis=synopsis if synopsis is not None else ...,
                                  flavor=flavor if flavor is not None else ..., batch_id=batch_id))
        row = db.get_hobby(hid)
        f = cards.hobby_fields(row)
        out = {"ok": True, "id": f["id"], "name": f["name"], "slug": f["slug"], "status": f["status"],
               "group_code": f["group_code"], "flags": f["flags"], **hobbies.text_fields(row),
               "cover": cards.hobby_cover_fields(cards.resolve_hobby_cover(row)),
               **take.result(), "batch_id": None if dry_run else _written(batch_id), "dry_run": dry_run}
    return out


@mcp.tool(alias_of=("constructicon_card_hobby", "add"))
def constructicon_add_project_to_hobby(project_slug: str, hobby_slug: str) -> dict | None:
    project = db.get_project(project_slug)
    if project is None or not policy.can_view_card(project):  # #604 step 3: hidden = missing
        raise NotFound(f"No card {project_slug!r}.")

    hobby = db.get_hobby(hobby_slug)
    if hobby is None or not policy.can_view_hobby(hobby):  # #604 step 3: hidden = missing
        raise NotFound(f"No hobby {hobby_slug!r}.")

    hobbies.add_card(hobby["id"], project["id"])  # logged + undoable (V2 cards 3.13, #541)

    # Return the updated hobby
    updated_hobby = db.get_hobby(hobby["id"])
    projects = policy.filter_visible_cards(db.list_projects_for_hobby(hobby["id"]))  # #604 step 3

    return {
        "id": updated_hobby["id"],
        "name": updated_hobby["name"],
        "slug": updated_hobby["slug"],
        "status": updated_hobby.get("hobby_status"),
        "project_count": len(projects),
    }


@mcp.tool(alias_of=("constructicon_update_hobby", None))
def constructicon_set_hobby_status(hobby_slug: str, status: str) -> dict | None:
    hobby = db.get_hobby(hobby_slug)
    if hobby is None or not policy.can_view_hobby(hobby):  # #604 step 3: hidden = missing
        raise NotFound(f"No hobby {hobby_slug!r}.")
    result = cards.set_hobby_activity(hobby["id"], status)
    updated = cards.hobby_fields(db.get_hobby(hobby["id"]))
    return {
        "id": updated["id"],
        "name": updated["name"],
        "slug": updated["slug"],
        "status": updated["status"],
        "group_code": updated["group_code"],
        "flags": updated["flags"],
        "warnings": result.warnings,
        "changes": result.changes,
        "batch_id": result.batch_id,
    }


@mcp.tool(alias_of=("constructicon_update_hobby", None))
def constructicon_set_hobby_code(hobby_slug: str, group_code: str) -> dict | None:
    hobby = db.get_hobby(hobby_slug)
    if hobby is None or not policy.can_view_hobby(hobby):  # #604 step 3: hidden = missing
        raise NotFound(f"No hobby {hobby_slug!r}.")
    result = cards.set_group_code(hobby["id"], group_code)
    updated = db.get_hobby(hobby["id"])
    return {"id": updated["id"], "name": updated["name"], "slug": updated["slug"],
            "group_code": updated["group_code"], "changes": result.changes, "batch_id": result.batch_id}


@mcp.tool(alias_of=("constructicon_update_hobby", None))
def constructicon_set_hobby_cover(hobby: str | int, cover_slug: str | None = None, clear: bool = False,
                                  dry_run: bool = False) -> dict:
    if clear and cover_slug:
        raise InvalidInput("Pass either cover_slug or clear=true, not both.")
    return hobbies.set_cover(hobby, None if clear else cover_slug, dry_run=dry_run).to_dict()
