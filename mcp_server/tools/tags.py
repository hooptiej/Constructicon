"""The MCP tools of one area. Tags (#555): list_tags, create_tag, tags (tree / get / sweep / rename / merge /
move / delete), item_tags (+ aliases).

#547 step 3: moved verbatim from mcp_server/server.py, in its order (the order of the
modules in mcp_server/tools/__init__.py is the tools/list order).
"""

import typing

from mcp_server.app import mcp
from mcp_server.shapes import TEXT_PREVIEW_CHARS, _to_public, text_chars_arg
from mcp_server._common import _DESTRUCTIVE, _only_for, _written
from mcp_server.listing import LIST_PAGE_MAX, _posts_for_tag

from core import policy, db, changes, tags as tags_svc
from core.errors import InvalidInput, NotFound


@mcp.tool()
def constructicon_list_tags() -> list[dict]:
    """The tag tree: root tags, each with nested `children`."""
    return policy.filter_tag_tree(db.list_tag_tree())  # #604 step 3: no private card / hobby names


@mcp.tool()
def constructicon_create_tag(name: str, parent_name: str | None = None) -> dict:
    """Create a tag (under parent_name, or at the root), or return the one already there."""
    # #541 phase C: a created tag (and parent) is imaged, so constructicon_undo removes it.
    return tags_svc.create(name, parent_name).data["tag"]


@mcp.tool(selector="action", annotations=_DESTRUCTIVE)
def constructicon_tags(action: typing.Literal["tree", "get", "rename", "merge", "move", "delete", "sweep",
                                              "sweep_apply"] = "tree",
                       tag: str | int | None = None, name: str | None = None, into: str | int | None = None,
                       parent: str | int | None = None, tag_ids: list[int] | None = None,
                       dry_run: bool = False) -> dict:
    """Manage the tag tree (the one tag store; an item's chips are derived from it). `tag` / `into` /
    `parent` take a tag id or its name. Every write is one batch (constructicon_undo reverses it).
    "tree": {tags: [{id, name, path, items, cards, hobby, children}]}.
    "get": tag= -> {tags: [every tag with that name (any case), each with path, items, children, cards, hobby]}.
    "rename": tag=, name= -> {tag, old_name, items, batch_id}. A card's linked tag follows the card's
      title (rename the card); a name another tag has is refused (merge instead).
    "merge": tag= (the one that goes), into= -> its items and sub-tags move to `into`, then it is deleted.
    "move": tag=, parent= (omit or "" = the top level) -> no cycles.
    "delete": tag= -> it comes off every item. A card's or grouping's tag, or one with sub-tags, is refused.
    "sweep": the cleanup list, read-only: {entries: [{id, path, kinds (orphan | empty | near_duplicate),
      deletable, why_not, suggestion, duplicates}]}. Nothing is deleted.
    "sweep_apply": tag_ids= the picked entries -> deletes exactly those (all still deletable, sub-tags
      picked too, else the whole call is refused), ONE batch.
    dry_run=true answers as the write would and writes nothing.
    E.g. constructicon_tags(action="merge", tag="Ancient projects.", into="Ancient Projects").
    """
    if action == "tree":
        _only_for('action="tree"', tag=(tag, None), name=(name, None), into=(into, None), parent=(parent, None),
                  tag_ids=(tag_ids, None), dry_run=(dry_run, False))
        return {"tags": tags_svc.tree()}
    if action == "sweep":
        _only_for('action="sweep"', tag=(tag, None), name=(name, None), into=(into, None), parent=(parent, None),
                  tag_ids=(tag_ids, None), dry_run=(dry_run, False))
        return {"entries": tags_svc.sweep(), "kinds": list(tags_svc.SWEEP_KINDS)}
    if action == "sweep_apply":
        _only_for('action="sweep_apply"', tag=(tag, None), name=(name, None), into=(into, None), parent=(parent, None))
        if not tag_ids:
            raise InvalidInput('action="sweep_apply" needs tag_ids (the ids of the sweep entries to delete).')
        return tags_svc.sweep_apply(tag_ids, dry_run=dry_run).to_dict()
    if tag is None or (isinstance(tag, str) and not tag.strip()):
        raise InvalidInput(f'action="{action}" needs tag (a tag id or name).')
    if action == "get":
        _only_for('action="get"', name=(name, None), into=(into, None), parent=(parent, None), tag_ids=(tag_ids, None),
                  dry_run=(dry_run, False))
        if isinstance(tag, int) or str(tag).strip().isdigit():
            row = tags_svc.get_tag(tag)
            return {"tags": [{**row, **tags_svc.usage(row["id"])}]}
        return {"tags": tags_svc.get_by_name(str(tag))}
    if action == "rename":
        _only_for('action="rename"', into=(into, None), parent=(parent, None), tag_ids=(tag_ids, None))
        if not (name or "").strip():
            raise InvalidInput('action="rename" needs name (the new name).')
        return tags_svc.rename(tag, name, dry_run=dry_run).to_dict()
    if action == "merge":
        _only_for('action="merge"', name=(name, None), parent=(parent, None), tag_ids=(tag_ids, None))
        if into is None or (isinstance(into, str) and not into.strip()):
            raise InvalidInput('action="merge" needs into (the tag to keep).')
        return tags_svc.merge(tag, into, dry_run=dry_run).to_dict()
    if action == "move":
        _only_for('action="move"', name=(name, None), into=(into, None), tag_ids=(tag_ids, None))
        return tags_svc.move(tag, parent if parent not in ("",) else None, dry_run=dry_run).to_dict()
    if action == "delete":
        _only_for('action="delete"', name=(name, None), into=(into, None), parent=(parent, None), tag_ids=(tag_ids, None))
        return tags_svc.delete(tag, dry_run=dry_run).to_dict()
    raise InvalidInput(f"Unknown action {action!r}: use tree, get, rename, merge, move, delete, sweep or sweep_apply.")


@mcp.tool(alias_of=("constructicon_list_items", "tag"))
def constructicon_get_posts_for_tag(tag_name: str, text_chars: int = TEXT_PREVIEW_CHARS) -> list[dict]:
    # #722: still a bare list (its old shape), but only the first LIST_PAGE_MAX items, read in SQL;
    # list_items(which="tag") pages through the rest.
    return _posts_for_tag(tag_name, text_chars_arg(text_chars), limit=LIST_PAGE_MAX)["items"]


def _detach_tag(slug, tag_name):
    policy.viewable_rows([slug])  # #709: missing = not_found, not visible = forbidden (says why)
    tag = tags_svc.find_any(tag_name)  # #563: a lookup must not create the tag
    if tag is None:
        raise NotFound(f"No such tag: {tag_name!r}")
    tags_svc.detach(slug, tag["id"])
    return _to_public(db.get_by_slug(slug))


@mcp.tool(selector="action")
def constructicon_item_tags(slug: str, tag_names: list[str],
                            action: typing.Literal["attach", "detach"] = "attach") -> dict:
    """Add tags to an item ("attach", default; missing root tags are created) or take them off
    ("detach"; an unknown tag is not_found and nothing is changed). One batch either way. This is
    the tag tree (an item's chips are derived from it); constructicon_update(tags=[...]) replaces the chips.
    -> the item + batch_id.
    """
    if isinstance(tag_names, str) or not isinstance(tag_names, list):
        raise InvalidInput("tag_names must be a list of tag names.")
    if action == "attach":
        batch_id = tags_svc.attach(slug, tag_names).batch_id  # #541 phase C: one batch; created tags imaged too
    elif action == "detach":
        policy.viewable_rows([slug])  # #709: missing = not_found, not visible = forbidden (says why)
        found = []
        for name in dict.fromkeys(tag_names):
            tag = tags_svc.find_any(name)  # #563: a lookup must not create the tag
            if tag is None:
                raise NotFound(f"No such tag: {name!r}. Nothing was changed.")
            found.append(tag["id"])
        batch_id = changes.new_batch_id()
        with db.transaction():
            for tag_id in found:
                tags_svc.detach(slug, tag_id, batch_id=batch_id)
    else:
        raise InvalidInput(f"Unknown action {action!r}: use attach or detach.")
    return {**_to_public(db.get_by_slug(slug)), "batch_id": _written(batch_id)}


@mcp.tool(alias_of=("constructicon_item_tags", "attach"))
def constructicon_attach_tags(slug: str, tag_names: list[str]) -> dict | None:
    tags_svc.attach(slug, tag_names)  # #541 phase C: one batch; tags it creates are imaged too
    return _to_public(db.get_by_slug(slug))


@mcp.tool(alias_of=("constructicon_item_tags", "detach"))
def constructicon_detach_tag(slug: str, tag_name: str) -> dict | None:
    return _detach_tag(slug, tag_name)
