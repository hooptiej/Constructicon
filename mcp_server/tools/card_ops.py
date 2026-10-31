"""The MCP tools of one area. Card shape and history: nest, family, sharing (#604), link (+ retype_links),
split / merge, explain, bulk_edit, resolve_decisions, undo, list_changes, sweep_stale_decisions.

#547 step 3: moved verbatim from mcp_server/server.py, in its order (the order of the
modules in mcp_server/tools/__init__.py is the tools/list order).
"""

import typing

from mcp_server.app import mcp
from mcp_server._common import _delete_card, _only_for

from core import cards, decisions, sharing
from core.errors import InvalidInput


@mcp.tool(selector="action")
def constructicon_nest(child: str | int, parent: str | int | None = None, replace: bool = False,
                       dry_run: bool = False, action: typing.Literal["nest", "unnest"] = "nest") -> dict:
    """Make `child` PART OF `parent` ("nest", default; it only makes sense inside it, e.g. a Lua
    script inside the truck it runs on), or take it out of its parent ("unnest"; no-op if none).
    Not family membership (constructicon_family). Refused: nest_self, nest_cycle, nest_group_kind
    (a family/collection can't nest or be a parent), nest_second_parent (already part of another
    card: replace=true moves it). -> {ok, dry_run, changes, warnings, batch_id}.
    """
    if action == "nest":
        if parent is None:
            raise InvalidInput('action="nest" needs parent.')
        return cards.nest(child, parent, replace=replace, dry_run=dry_run).to_dict()
    if action == "unnest":
        _only_for('action="unnest"', parent=(parent, None), replace=(replace, False))
        return cards.unnest(child, dry_run=dry_run).to_dict()
    raise InvalidInput(f"Unknown action {action!r}: use nest or unnest.")


@mcp.tool(alias_of=("constructicon_nest", "unnest"))
def constructicon_unnest(child: str | int, dry_run: bool = False) -> dict:
    return cards.unnest(child, dry_run=dry_run).to_dict()


@mcp.tool(selector="action")
def constructicon_family(family: str | int, member: str | int, action: typing.Literal["add", "remove"] = "add",
                         dry_run: bool = False) -> dict:
    """Put a card in a family or collection ("add", default; many-to-many, moves no files, twice is
    a no-op) or take it out ("remove"). `family` must be a family/collection card and `member` must
    not be one (bad_membership). -> {ok, dry_run, changes, warnings, batch_id}."""
    if action == "add":
        return cards.add_to_family(family, member, dry_run=dry_run).to_dict()
    if action == "remove":
        return cards.remove_from_family(family, member, dry_run=dry_run).to_dict()
    raise InvalidInput(f"Unknown action {action!r}: use add or remove.")


@mcp.tool(alias_of=("constructicon_family", "add"))
def constructicon_add_to_family(family: str | int, member: str | int, dry_run: bool = False) -> dict:
    return cards.add_to_family(family, member, dry_run=dry_run).to_dict()


@mcp.tool(alias_of=("constructicon_family", "remove"))
def constructicon_remove_from_family(family: str | int, member: str | int, dry_run: bool = False) -> dict:
    return cards.remove_from_family(family, member, dry_run=dry_run).to_dict()


# --- Private cards and hobbies: who else may see them ---

@mcp.tool(selector="action")
def constructicon_sharing(action: typing.Literal["get", "set_private", "add_member", "remove_member", "users"] = "get",
                          card: str | int | None = None, hobby: str | int | None = None,
                          private: bool | None = None, user: str | int | None = None,
                          role: typing.Literal["viewer", "editor"] | None = None, dry_run: bool = False) -> dict | list:
    """Private cards and hobbies. Cards and hobbies are open to everyone signed in unless ticked
    private; then only admins, the owner (its creator) and its members see it and everything under it
    (nested cards, family members, a hobby's cards and loose files). Give exactly one of card= or hobby=
    (id or slug), except for action="users".
    action: "get" (default) -> {private (ticked here), effective (here or above), inherited_from
    [{kind, id, title, slug}], owner, members [{user_id, username, name, role, capped}], can_manage};
    "set_private" (private=true|false); "add_member" (user=id or username, role=viewer|editor,
    default viewer; adding again changes the role); "remove_member" (user=); "users" -> the people
    you can add [{id, username, name, role}]. The writes return {ok, changes, warnings, batch_id,
    sharing} and only the owner or an admin may make them (forbidden otherwise). A viewer member only
    looks; an editor member may change the card and its files. The install role is the ceiling: a
    viewer can't be made an editor (role_ceiling). Example: constructicon_sharing(action="add_member",
    card="desk-build", user="sam", role="editor")."""
    if action == "users":
        _only_for('action="users"', card=(card, None), hobby=(hobby, None), private=(private, None),
                  user=(user, None), role=(role, None), dry_run=(dry_run, False))
        return sharing.user_choices()
    if (card is None) == (hobby is None):
        raise InvalidInput("Give exactly one of card= or hobby=.")
    kind, target = ("card", card) if card is not None else ("hobby", hobby)
    if action == "get":
        _only_for('action="get"', private=(private, None), user=(user, None), role=(role, None),
                  dry_run=(dry_run, False))
        return sharing.state(kind, target)
    if action == "set_private":
        _only_for('action="set_private"', user=(user, None), role=(role, None))
        if private is None:
            raise InvalidInput('action="set_private" needs private=true or private=false.')
        return sharing.set_private(kind, target, private, dry_run=dry_run).to_dict()
    _only_for(f'action="{action}"', private=(private, None))
    if user in (None, ""):
        raise InvalidInput(f'action="{action}" needs user= (a user id or username; action="users" lists them).')
    if action == "add_member":
        return sharing.add_member(kind, target, user, role or "viewer", dry_run=dry_run).to_dict()
    if action == "remove_member":
        _only_for('action="remove_member"', role=(role, None))
        return sharing.remove_member(kind, target, user, dry_run=dry_run).to_dict()
    raise InvalidInput(f"Unknown action {action!r}: use get, set_private, add_member, remove_member or users.")


# --- Typed links (V2 cards 3.8) ---

@mcp.tool(selector="action")
def constructicon_link(a: str | int, b: str | int | None = None, type: str | None = None, note: str | None = None,
                       dry_run: bool = False, batch_id: str | None = None,
                       action: typing.Literal["add", "remove", "retype", "list"] = "add",
                       from_type: str | None = None, to_type: str | None = None) -> dict | list:
    """Typed links between cards, read "a <type> b". type: built_for | applies_to | used_in |
    inspired_by (directed) | related (symmetric).
    "add" (default; b, type, note): a typed link over a related pair UPGRADES it; related over a
      typed pair is refused. Errors: bad_link (unknown type, self-link, a family/collection as the
      source of built_for/applies_to/used_in), link_conflict.
    "remove" (b, type optional): only that type, or with no type every link between the pair
      (no-op with a warning when nothing matches).
    "retype" (b, from_type, to_type): swap the type in one step; the note carries over unless note
      is given.
    "list": every link on card `a`, both directions: [{slug, title, kind, stage, type, direction
      (out | in | both), label, note}].
    Writes -> {ok, dry_run, changes, warnings, batch_id}; batch_id joins an earlier call's batch.
    E.g. constructicon_link("lua-script", "rc-truck", "used_in").
    """
    if action == "list":
        _only_for('action="list"', b=(b, None), type=(type, None), note=(note, None), dry_run=(dry_run, False),
                  batch_id=(batch_id, None), from_type=(from_type, None), to_type=(to_type, None))
        return cards.list_links(a)
    if b is None:
        raise InvalidInput(f'action="{action}" needs b (the other card).')
    if action == "add":
        if not type:
            raise InvalidInput('action="add" needs type.')
        _only_for('action="add"', from_type=(from_type, None), to_type=(to_type, None))
        return cards.link(a, b, type, "" if note is None else note, dry_run=dry_run, batch_id=batch_id).to_dict()
    if action == "remove":
        _only_for('action="remove"', note=(note, None), from_type=(from_type, None), to_type=(to_type, None))
        return cards.unlink(a, b, type, dry_run=dry_run, batch_id=batch_id).to_dict()
    if action == "retype":
        if not (from_type and to_type):
            raise InvalidInput('action="retype" needs from_type and to_type.')
        _only_for('action="retype" (use from_type / to_type)', type=(type, None))
        return cards.retype_link(a, b, from_type, to_type, note=note, dry_run=dry_run, batch_id=batch_id).to_dict()
    raise InvalidInput(f"Unknown action {action!r}: use add, remove, retype or list.")


@mcp.tool(alias_of=("constructicon_link", "remove"))
def constructicon_unlink(a: str | int, b: str | int, type: str | None = None, dry_run: bool = False) -> dict:
    return cards.unlink(a, b, type, dry_run=dry_run).to_dict()


@mcp.tool(alias_of=("constructicon_link", "retype"))
def constructicon_retype_link(a: str | int, b: str | int, from_type: str, to_type: str,
                              note: str | None = None, dry_run: bool = False) -> dict:
    return cards.retype_link(a, b, from_type, to_type, note=note, dry_run=dry_run).to_dict()


@mcp.tool()
def constructicon_retype_links(mapping: list[dict], dry_run: bool = True, partial_ok: bool = False) -> dict:
    """Retype many v1 `related` links at once. DRY-RUN BY DEFAULT (dry_run=false writes).
    mapping: [{a, b, to_type, from_type="related"}], each read "a <to_type> b". All-or-nothing
    in one transaction unless partial_ok=true (valid ones applied, the rest reported). Candidates:
    constructicon_needs(view="card_decisions", need="untyped_link").
    -> {ok, dry_run, applied, changes, warnings, batch_id, items: [{a, b, to_type, ok, changes,
    error?}]}."""
    return cards.retype_links(mapping, dry_run=dry_run, partial_ok=partial_ok)


@mcp.tool(alias_of=("constructicon_link", "list"))
def constructicon_list_links(card: str | int) -> list[dict] | dict:
    return cards.list_links(card)


@mcp.tool(alias_of=("constructicon_needs", "card_decisions"))
def constructicon_list_needs_decision(kind: str | None = None, need: str | None = None,
                                      hobby: str | None = None, limit: int | None = None,
                                      card: str | int | None = None) -> list[dict]:
    return cards.list_needs_decision(kind=kind, need=need, hobby=hobby, limit=limit, card=card)


# --- V2 cards piece 6: the reorganizing toolkit (spec 7.1 / 7.3) -------------------
# Thin wrappers over core/cards.py; no rule lives here. Single-card tools apply by
# default; bulk tools (bulk_edit, resolve_decisions, retype_links) are DRY-RUN by default.

@mcp.tool()
def constructicon_split_card(source: str | int, parts: list[dict], keep_in_source: bool = False,
                             dry_run: bool = False, batch_id: str | None = None) -> dict:
    """Carve files (and description) out of one card into new cards, in one batch.
    parts: [{title, kind="project", relation="sibling" | "child", stage, stop_reason, provenance,
    provenance_credit, whereabouts, whereabouts_note, description, move_description, file_slugs,
    link_to_source: {type, note?} | null, hobbies: "inherit" | [slugs], families: "inherit" |
    [slugs], highlight}]. A child is nested under the source; a sibling copies the source's hobbies
    and families by default. Each part is a full card with a blank write-up. Files are MOVED out of
    the source unless keep_in_source=true; the source's write-up can't be split out. Pass the
    returned batch_id to later calls (constructicon_link, constructicon_card_hobby) to keep a
    reorganization in one undo.
    -> {ok, dry_run, changes, warnings, batch_id, created: [{id, slug, title, kind, relation,
    files}]}."""
    return cards.split_card(source, parts, keep_in_source=keep_in_source, dry_run=dry_run,
                            batch_id=batch_id).to_dict()


@mcp.tool(alias_of=("constructicon_delete", "card"))
def constructicon_delete_project(card: str | int, dry_run: bool = False, batch_id: str | None = None) -> dict:
    return _delete_card(card, dry_run, batch_id)


@mcp.tool()
def constructicon_merge_cards(keep: str | int, absorb: list[str | int], dry_run: bool = False,
                              batch_id: str | None = None) -> dict:
    """Fold cards into `keep` (the reverse of split), in one batch. Files, hobbies and families are
    unioned; children re-parented; links and blog attachments re-pointed (duplicates dropped; typed
    beats related). keep's cover and write-up win; an absorbed blank write-up goes, a real one stays
    as a file. Undo restores the absorbed cards with their ids. Refused: nest_cycle, bad_merge (a
    family/collection with a non-group card). -> {ok, dry_run, changes, warnings, batch_id, keep,
    absorbed}."""
    return cards.merge_cards(keep, absorb, dry_run=dry_run, batch_id=batch_id).to_dict()


@mcp.tool(alias_of=("constructicon_card_files", "move"))
def constructicon_move_files(slugs: list[str], from_card: str | int, to_card: str | int, dry_run: bool = False,
                             batch_id: str | None = None) -> dict:
    return cards.move_files(slugs, from_card, to_card, dry_run=dry_run, batch_id=batch_id).to_dict()


@mcp.tool(alias_of=("constructicon_card_files", "copy"))
def constructicon_copy_files(slugs: list[str], from_card: str | int, to_card: str | int, dry_run: bool = False,
                             batch_id: str | None = None) -> dict:
    return cards.copy_files(slugs, from_card, to_card, dry_run=dry_run, batch_id=batch_id).to_dict()


@mcp.tool(alias_of=("constructicon_card_hobby", "add"))
def constructicon_add_to_hobby(card: str | int, hobby: str | int, dry_run: bool = False,
                               batch_id: str | None = None) -> dict:
    return cards.add_to_hobby(card, hobby, dry_run=dry_run, batch_id=batch_id).to_dict()


@mcp.tool(alias_of=("constructicon_card_hobby", "remove"))
def constructicon_remove_from_hobby(card: str | int, hobby: str | int, dry_run: bool = False,
                                    batch_id: str | None = None) -> dict:
    return cards.remove_from_hobby(card, hobby, dry_run=dry_run, batch_id=batch_id).to_dict()


@mcp.tool(alias_of=("constructicon_update_project", None))
def constructicon_set_home(card: str | int, target: str | int | None = None, dry_run: bool = False) -> dict:
    return cards.set_home(card, target, dry_run=dry_run).to_dict()


@mcp.tool()
def constructicon_explain_card(card: str | int) -> dict:
    """Everything about a card, to read BEFORE proposing a change (read-only): identity, status (+
    labels, `provisional`), whereabouts, provenance, highlight, hobbies, families, members, parent,
    children, links, `home` (+ source, dangling_override) and `home_chain`, `files` (counts, date
    range), `level` (the 0-5 pips, each with a reason), `caption_check` (the Curator's caption
    check: passed, excused, reason, excused_files = [{type, label, count, reason}] for files of a
    type that isn't captioned, e.g. Word/PowerPoint/spreadsheets), open_decisions, needs, warnings,
    suggestions (provenance, typed links) and recent_changes (last 5, with batch ids)."""
    return cards.explain_card(card)


@mcp.tool()
def constructicon_bulk_edit(op: str, items: list[dict], dry_run: bool = True, partial_ok: bool = False) -> dict:
    """Run ONE setter over many cards. DRY-RUN BY DEFAULT (dry_run=false writes).
    op: set_status, set_kind, set_whereabouts, set_card_provenance, set_home, set_card_highlight,
    add_to_hobby, remove_from_hobby, add_to_family, remove_from_family, nest, unnest, link, unlink,
    retype_link, set_hobby_activity (these are the core operation names).
    items: [{card, args}]: e.g. set_status {card: "x", args: {stage: "done"}}; add_to_hobby {card:
    "x", args: {hobby: "3d-printing"}}; add_to_family {card: <member>, args: {family: <family>}};
    nest {card: <child>, args: {parent}}; link {card: a, args: {b, type, note?}};
    set_hobby_activity {card: <hobby>, args: {value: "inactive"}}. Unknown args are refused.
    In order, one transaction; all-or-nothing unless partial_ok=true.
    -> {ok, dry_run, op, applied, changes, warnings, batch_id, items: [{card, ok, applied, changes,
    warnings, error?}]}."""
    return cards.bulk(op, items, dry_run=dry_run, partial_ok=partial_ok)


@mcp.tool()
def constructicon_resolve_decisions(items: list, accept_suggested: bool = False, dry_run: bool = True,
                                    partial_ok: bool = False) -> dict:
    """Answer many card questions (card_status, card_built_for, card_kind, card_family_members) at
    once. DRY-RUN BY DEFAULT (dry_run=false writes).
    items: [{decision_id, choice}] (or choices: [...] for multi-answer); with
    accept_suggested=true just decision ids, each answered with its OWN suggestion (none:
    skipped). Find them with constructicon_needs(view="card_decisions"). All-or-nothing unless
    partial_ok=true. -> {ok, dry_run, batch_id, applied, would_apply, skipped, failed, changes,
    warnings, items: [{decision_id, card_slug, choice, status, changes, reason?, error?}]}."""
    return cards.resolve_decisions(items, accept_suggested=accept_suggested, dry_run=dry_run,
                                   partial_ok=partial_ok)


@mcp.tool()
def constructicon_undo(audit_id: str | int, force: bool = False, dry_run: bool = False) -> dict:
    """Reverse a change-log row, or a whole batch (audit_id = a row id or a batch_id), newest
    first, in one transaction. An undo is logged, so it can be undone too. Refused: undo_conflict
    (changed since; the message says what; force=true overrides), undo_refused (a migration, an
    already-undone entry, no row images), trash_expired. -> {ok, dry_run, changes, warnings,
    batch_id, undone, undone_ops}."""
    return cards.undo(audit_id, force=force, dry_run=dry_run).to_dict()


@mcp.tool()
def constructicon_list_changes(card: str | int | None = None, batch_id: str | None = None, limit: int = 50) -> dict:
    """The change log, newest first: {changes: [{id, op, actor, batch_id, timestamp, affected_slugs,
    undone_by, mutations: [{table, key, before, after}]}], withheld, withheld_note}. Filter by card or
    batch_id. A non-admin token gets only rows touching nothing hidden from them; `withheld` counts
    the rest of the newest `limit` rows (withheld_note says why)."""
    return cards.list_changes(card=card, batch_id=batch_id, limit=limit)


@mcp.tool()
def constructicon_sweep_stale_decisions(dry_run: bool = False) -> dict:
    """Resolve every open question nothing can answer any more (its file or card is gone, a
    card-match question has under 2 candidates left, a "does this replace ...?" question has no
    candidate). The web app also runs this hourly; listing never resolves anything.
    -> {ok, dry_run, changes, warnings, batch_id, resolved: [{id, kind, post_slug, reason}], count,
    by_kind}."""
    return decisions.sweep_stale(dry_run=dry_run).to_dict()
