"""The MCP tools of one area. Cards: list / create / update / get project, card_files (+ aliases).

#547 step 3: moved verbatim from mcp_server/server.py, in its order (the order of the
modules in mcp_server/tools/__init__.py is the tools/list order).
"""

import typing

from mcp_server.app import mcp
from mcp_server.shapes import TEXT_PREVIEW_CHARS, _to_public, _to_public_listed, _to_public_project, text_chars_arg
from mcp_server._common import _Collect, _fields_arg, _only_for

from core import policy, card_rules, cards, db, items, object_types, changes, membership, sharing
from core.errors import InvalidInput, NotFound


@mcp.tool()
def constructicon_list_projects(kind: str | None = None, activity: str | None = None,
                                stage: str | None = None) -> list[dict]:
    """All cards, most recently updated first. Filters: kind (project | thing | action | family |
    collection | event), activity (active | inactive), stage (in_progress | in_use | idea | paused |
    done | stopped). Each card has its status fields and labels, and needs_input (an open question).
    """
    return [_to_public_project(p) for p in  # #604 step 3: not the private cards this caller isn't on
            policy.filter_visible_cards(db.list_projects(kind=kind, activity=activity, stage=stage))]


@mcp.tool()
def constructicon_create_project(title: str, description: str = "", cover_slug: str | None = None, parent_id: int | None = None,
                                 kind: str | None = None, stage: str | None = None, stop_reason: str | None = None) -> dict:
    """Create a card, with a root tag of the same name linked to it and a blank write-up, as one
    batch. kind: project (default) | thing | action | family | collection | event. stage:
    in_progress (default) | in_use | idea | paused | done | stopped (stopped needs stop_reason
    failed | abandoned). parent_id makes it "part of" that card (a family/collection can't nest or
    be a parent: nest_group_kind). Errors: bad_status, not_found. Returns the card + batch_id.
    """
    # #541 phase D: core validates everything first; tag + card + write-up are ONE undoable batch.
    result = cards.create(title, description=description, cover_slug=cover_slug, parent=parent_id,
                          kind=kind, stage=stage, stop_reason=stop_reason)
    return {**_to_public_project(result.data["card"]), "batch_id": result.batch_id}


# #746: what constructicon_update_project(fields={...}) takes beyond its named parameters.
CARD_FIELDS = {
    "activity": (str, type(None)),
    "force_kind": (bool,),
    "whereabouts": (str, type(None)),
    "whereabouts_note": (str, type(None)),
    "provenance": (str, type(None)),
    "provenance_credit": (str, type(None)),
    "highlight": (bool,),
    "home": (str, int, type(None)),
    "synopsis": (str, type(None)),
    "flavor": (str, type(None)),
    "writeup_slug": (str,),
    "owner_words": (bool,),
    "cover_card": (str, int),  # #554: borrow a nested child's or a member's cover (cards.set_cover)
}


def _set_writeup(project, slug, owner_words, batch_id):
    """#541 phase D: the write-up item goes on the card (no side effects), the owner-words mark, and
    writeup_slug: the caller's transaction and batch. Returns the card row after."""
    row = policy.viewable_rows([slug])[0][0]  # #709: missing = not_found, not visible = forbidden (says why)
    if not object_types.can_be_writeup(row):
        label = object_types.get_object_type(row.get("media_type")).label
        raise ValueError(f"{label} items can't be a project write-up (their type declares no writeup_body_key)")
    membership.add_files(project["id"], [slug], batch_id=batch_id, **membership.NO_EFFECTS)
    if owner_words is not None:
        items.update(slug, type_metadata={"owner_words": bool(owner_words)}, batch_id=batch_id)
    return cards.update(project["id"], writeup_slug=slug, batch_id=batch_id)


@mcp.tool()
def constructicon_update_project(project_id: str | int | None = None, title: str | None = None,
                                 description: str | None = None, cover_slug: str | None = None,
                                 status: str | None = None, start_date: float | None = None,
                                 reset_start_date: bool = False, end_date: float | None = None,
                                 reset_end_date: bool = False, kind: str | None = None,
                                 stage: str | None = None, stop_reason: str | None = None,
                                 card: str | int | None = None, fields: dict | None = None,
                                 dry_run: bool = False) -> dict:
    """Edit a card, every change in ONE batch, applied in order (kind, status, whereabouts,
    provenance, highlight, home, face text, write-up, then title and the rest).
    card (or project_id): its id or slug. Parameters left out (None) are left alone.
    kind: project | thing | action | family | collection | event. stage: in_progress | in_use
    (active) | idea | paused | done | stopped (inactive); stopped needs stop_reason failed |
    abandoned; an event can't be in_use or paused. status: the old v1 word (wip, complete,
    shelved ...), translated to a stage with a warning.
    start_date / end_date: timeline overrides (unix seconds); reset_start_date / reset_end_date clear.
    fields: more fields, key present = set it, absent = leave it; null or "" clears:
      activity (must agree with the stage), force_kind (bool: leave a family/collection that has
      members), whereabouts (have_it | partial | parted_out | sold | gifted | lost | abandoned |
      never_built; thing / project / collection only; anything but have_it greys the card's art;
      abandoned = the object was left behind (towed, impound), not stop_reason abandoned, which
      is walking away from the project), whereabouts_note, provenance (an active card key,
      constructicon_list_provenance_options(scope="card")), provenance_credit, highlight (bool),
      home ("card:<slug>" | "hobby:<slug>" | a card; null = automatic), synopsis (max 600: a few
      sentences saying what the card is; set it whenever you write a write-up), flavor (one italic
      line, max 140), writeup_slug (an item that can be a write-up; it joins the card),
      owner_words (bool: the write-up is the owner's own words), cover_card (a nested part or a
      family / collection member, id or slug: the card borrows its cover and follows it; bad_cover
      with details.reason when it can't: not_related, no_cover, cover_loop, hidden, ...).
    E.g. fields={"whereabouts": "have_it", "synopsis": "A 3D-printed rover."}.
    dry_run=true answers as the call would and writes nothing.
    Returns the card plus batch_id, changes and warnings (if any). Codes: bad_status, bad_kind,
    bad_whereabouts, bad_provenance, bad_home, bad_card_text, unknown_field.
    """
    if (project_id is None) == (card is None):
        raise InvalidInput("Pass the card as card= (or project_id=), exactly one of them.")
    ref = card if card is not None else project_id
    card_warnings = []
    project = db.get_project(ref)
    if project is None or not policy.can_view_card(project):  # #604 step 3: hidden = missing
        raise NotFound(f"No card {ref!r}.")
    f = _fields_arg(fields, CARD_FIELDS, "constructicon_update_project")
    if "force_kind" in f and not kind:
        raise InvalidInput("fields.force_kind only goes with kind=.")
    if status:
        legacy = card_rules.legacy_to_status(status)
        kind = kind or legacy["kind"]
        if not stage:
            stage, stop_reason = legacy["stage"], legacy["stop_reason"]
        card_warnings.extend(legacy["warnings"])
    new_start = None if reset_start_date else (start_date if start_date is not None else ...)
    new_end = None if reset_end_date else (end_date if end_date is not None else ...)
    cid = project["id"]
    take = _Collect()
    # #541 phase D: one transaction, one batch (one constructicon_undo reverses the whole call).
    # #746: a dry run is the same calls rolled back, so each step sees what the earlier ones did.
    batch_id = changes.new_batch_id()
    with db.transaction(dry_run=dry_run):
        if kind:
            take(cards.set_kind(cid, kind, force=f.get("force_kind", False), batch_id=batch_id))
        if stage or stop_reason or "activity" in f:
            if not (stage or stop_reason):  # activity alone: checked against the current stage
                now = db.get_project(cid)
                stage, stop_reason = now.get("stage"), now.get("stop_reason")
            take(cards.set_status(cid, stage, stop_reason, activity=f.get("activity"), batch_id=batch_id))
        if "whereabouts" in f or "whereabouts_note" in f:
            now = db.get_project(cid)
            take(cards.set_whereabouts(cid, f["whereabouts"] if "whereabouts" in f else now.get("whereabouts"),
                                       f["whereabouts_note"] if "whereabouts_note" in f else ...,
                                       batch_id=batch_id))
        if "provenance" in f or "provenance_credit" in f:
            now = db.get_project(cid)
            take(cards.set_provenance(cid, f["provenance"] if "provenance" in f else now.get("provenance"),
                                      f["provenance_credit"] if "provenance_credit" in f else ...,
                                      batch_id=batch_id))
        if "highlight" in f:
            take(cards.set_highlight(cid, f["highlight"], batch_id=batch_id))
        if "home" in f:
            take(cards.set_home(cid, f["home"], batch_id=batch_id))
        if "synopsis" in f or "flavor" in f:
            take(cards.set_text(cid, synopsis=f.get("synopsis", ...), flavor=f.get("flavor", ...), batch_id=batch_id))
        if "writeup_slug" in f:
            take(_set_writeup(project, f["writeup_slug"], f.get("owner_words"), batch_id))
        elif "owner_words" in f:
            current = db.get_project(cid).get("writeup_slug")
            if not current:
                raise InvalidInput("This card has no write-up yet: pass fields.writeup_slug too.")
            take(items.update(current, type_metadata={"owner_words": f["owner_words"]}, batch_id=batch_id))
        take(cards.update(cid, title=title, description=description, cover_slug=cover_slug,
                          start=new_start, end=new_end, batch_id=batch_id))
        if "cover_card" in f:
            if cover_slug:
                raise InvalidInput("Pass cover_slug or fields.cover_card, not both: a card has one cover.")
            take(cards.set_cover(cid, from_card=f["cover_card"], batch_id=batch_id))
        result = {**_to_public_project(db.get_project(cid)), "batch_id": None if dry_run else batch_id}
    collected = take.result()
    result["changes"] = collected["changes"]
    card_warnings.extend(collected["warnings"])
    if card_warnings:
        result["warnings"] = card_warnings
    if dry_run:
        result["dry_run"] = True
    return result


def _add_to_project(slug, project_id):
    policy.viewable_rows([slug])  # #709: missing = not_found, not visible = forbidden (says why)
    if not policy.can_view_card(db.get_project(project_id)):  # #604 step 3: hidden = missing
        raise NotFound(f"No card {project_id!r}.")
    membership.add_files(project_id, [slug], **membership.UI_EFFECTS)  # #541 phase C: same flags as the web
    return [_to_public_project(p) for p in db.list_projects_for_post(slug)]


def _remove_from_project(slug, project_id):
    policy.viewable_rows([slug])  # #709: missing = not_found, not visible = forbidden (says why)
    project = db.get_project(project_id)
    if project is None or not policy.can_view_card(project):  # #604 step 3: hidden = missing
        raise NotFound(f"No card {project_id!r}.")
    membership.remove_files(project["id"], [slug])  # membership only: tags and cover stay
    return [_to_public_project(p) for p in db.list_projects_for_post(slug)]


@mcp.tool(selector="action")
def constructicon_card_files(card: str | int, slugs: list[str],
                             action: typing.Literal["add", "remove", "move", "copy"] = "add",
                             from_card: str | int | None = None, missing_ok: bool = False,
                             dry_run: bool = False, batch_id: str | None = None) -> dict:
    """Put files (item slugs) on a card, take them off, or move / copy them between cards.
    "add" (default): like the web's add-to-card: the files get the card's linked tag (both tag
      stores) and a card with no cover takes the first. Unknown slugs: not_found, unless
      missing_ok=true (skipped). -> {ok, dry_run, changes, warnings, batch_id, card, slugs, added,
      already, skipped, tagged, cover, items: [the files, listed shape]}.
    "remove": membership only (tags and cover stay). -> {ok, ..., card, removed, items}.
    "move" / "copy": from from_card to card. A move drops from_card's linked tag and adds card's; a
      copy keeps the files on from_card and only adds card's tag. A card's own write-up can't move;
      bad_files if a file isn't on from_card. -> {ok, ..., tags_added, tags_removed}.
    E.g. constructicon_card_files("rover", ["a1b2", "c3d4"], action="move", from_card="misc").
    """
    if action in ("move", "copy"):
        if from_card is None:
            raise InvalidInput(f'action="{action}" needs from_card (the card the files are on now).')
        _only_for(f'action="{action}"', missing_ok=(missing_ok, False))
        op = cards.move_files if action == "move" else cards.copy_files
        return op(slugs, from_card, card, dry_run=dry_run, batch_id=batch_id).to_dict()
    if action not in ("add", "remove"):
        raise InvalidInput(f"Unknown action {action!r}: use add, remove, move or copy.")
    _only_for(f'action="{action}"', from_card=(from_card, None))
    if isinstance(slugs, str):
        raise InvalidInput("slugs must be a list of item slugs.")
    project = db.get_project(card)
    if project is None or not policy.can_view_card(project):  # #604 step 3: hidden = missing
        raise NotFound(f"No card {card!r}.")
    if not missing_ok:
        policy.viewable_rows(slugs)  # #709: unknown = not_found, hidden = forbidden (says why), before any write
    if action == "add":
        res = membership.add_files(project["id"], slugs, missing_ok=missing_ok, dry_run=dry_run, batch_id=batch_id,
                                   **membership.UI_EFFECTS)
        listed = res.data["slugs"]
    else:
        res = membership.remove_files(project["id"], slugs, dry_run=dry_run, batch_id=batch_id)
        listed = res.data["removed"]
    return {**res.to_dict(), "items": [_to_public_listed(r) for r in policy.filter_visible(
        [db.get_by_slug(s) for s in listed if db.get_by_slug(s)])]}


@mcp.tool(alias_of=("constructicon_card_files", "add"))
def constructicon_add_to_project(slug: str, project_id: str | int) -> list[dict]:
    return _add_to_project(slug, project_id)


@mcp.tool(alias_of=("constructicon_card_files", "remove"))
def constructicon_remove_from_project(slug: str, project_id: str | int) -> list[dict]:
    return _remove_from_project(slug, project_id)


@mcp.tool(alias_of=("constructicon_card_files", "add"))
def constructicon_add_items_to_project(project_id: str | int, slugs: list[str]) -> list[dict]:
    project = db.get_project(project_id)
    if project is None or not policy.can_view_card(project):  # #604 step 3: hidden = missing
        raise NotFound(f"No card {project_id!r}.")
    result = membership.add_files(project["id"], slugs, missing_ok=True, **membership.UI_EFFECTS)
    return [_to_public_listed(db.get_by_slug(slug)) for slug in slugs if slug in result.data["slugs"]]


@mcp.tool(alias_of=("constructicon_update_project", None))
def constructicon_set_project_writeup(project_id: str | int, slug: str, owner_words: bool | None = None) -> dict | None:
    project = db.get_project(project_id)
    if project is None or not policy.can_view_card(project):  # #604 step 3: hidden = missing
        raise NotFound(f"No card {project_id!r}.")
    # #541 phase D: membership (no side effects, as before) + the owner-words mark + writeup_slug
    # are one transaction and one batch.
    batch_id = changes.new_batch_id()
    with db.transaction():
        updated = _set_writeup(project, slug, owner_words, batch_id).data["card"]
    return {**_to_public_project(updated), "batch_id": batch_id}


@mcp.tool()
def constructicon_get_project(id_or_slug: str | int, text_chars: int = TEXT_PREVIEW_CHARS,
                              full_writeup: bool = False) -> dict | None:
    """One card in full: its fields and live status (kind, activity, stage, stop_reason + labels,
    needs_input), items (with tags; text previews), cover, write-up (a preview like the items: its
    text and type_metadata.body cut to text_chars (at least 300), with body_chars and body_truncated; full_writeup=true
    sends it whole, or read it with constructicon_get(writeup_slug)), open_decisions, families /
    members / children, links (both directions), its face
    text (synopsis, flavor, writeup_lead, face_text, face_text_source) and agent_notes (your private
    notes, from constructicon_set_agent_notes(card=...); null when none). For a read-before-change
    view with level, needs and recent changes: constructicon_explain_card.
    """
    chars = text_chars_arg(text_chars)
    project = db.get_project(id_or_slug)
    if project is None or not policy.can_view_card(project):  # #604 step 3: hidden = missing
        raise NotFound(f"No card {id_or_slug!r}.")

    # Get the project items (#557: the item policy decides what the card shows)
    items = policy.filter_visible(db.list_project_items(project["id"]))
    items_public = []
    for item in items:
        item_dict = _to_public_listed(item, text_chars=chars)
        # Add tags for each item
        tags = db.list_tags_for_post(item["slug"])
        item_dict["tags"] = [t["name"] for t in tags]
        items_public.append(item_dict)

    # Get the cover object if it exists
    cover = None
    if project.get("cover_slug"):
        cover_row = db.get_by_slug(project["cover_slug"])
        if cover_row and policy.can_view(cover_row):  # #557
            cover = _to_public_listed(cover_row, text_chars=chars)

    # Get the writeup object if it exists
    writeup = None
    if project.get("writeup_slug"):
        writeup_row = db.get_by_slug(project["writeup_slug"])
        if writeup_row and policy.can_view(writeup_row):  # #557
            body = (writeup_row.get("type_metadata") or {}).get("body")
            body_chars = len(body) if isinstance(body, str) else 0
            if full_writeup:
                writeup = {**_to_public(writeup_row), "body_chars": body_chars, "body_truncated": False}
            else:  # #722: a preview, like every other item here
                writeup = _to_public_listed(writeup_row, text_chars=chars)
                writeup["body_chars"] = body_chars
                writeup["body_truncated"] = "body" in writeup.get("type_metadata_cut_keys", [])

    return {
        **_to_public_project(project),
        "items": items_public,
        "cover": cover,
        "writeup": writeup,
        "open_decisions": [cards.decision_summary(d) for d in cards.open_card_decisions(project["slug"])],
        # V2 cards 3.6/3.7: families this card is in, members (if it is a family or
        # collection), and the cards nested under it ("part of").
        **cards.family_fields(project),
        # V2 cards 3.8: typed links, both directions, with labels.
        "links": cards.list_links(project["slug"]),
        "children": [{"id": c["id"], "slug": c["slug"], "title": c["title"]}
                     for c in policy.filter_visible_cards(db.list_child_projects(project["id"]))],
        # #604 step 3: private or open, inherited from where, owner, members (constructicon_sharing).
        "sharing": sharing.state("card", project["id"]),
        # #596: the face text (synopsis, flavor, the cached write-up lead, what the face shows).
        **cards.text_fields(project),
        # #745: the card's private agent notes. Only this read (and set_agent_notes) returns them.
        "agent_notes": db.get_card_agent_notes(project["id"]),
    }
