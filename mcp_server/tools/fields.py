"""The MCP tools of one area. retry_ocr (alias), set_agent_notes, provenance options, word lists, and the
retired single-field setters (aliases).

#547 step 3: moved verbatim from mcp_server/server.py, in its order (the order of the
modules in mcp_server/tools/__init__.py is the tools/list order).
"""

from mcp_server.app import mcp
from mcp_server.shapes import _to_public, _to_public_project
from mcp_server._common import _retry_ocr, _written
from mcp_server.listing import _list_brand_assets

from core import policy, card_rules, cards, db, items, provenance_options, hobbies, vocab as vocab_svc
from core.errors import InvalidInput, NotFound


@mcp.tool(alias_of=("constructicon_run_type_action", None))
def constructicon_retry_ocr(slug: str) -> dict | None:
    return _retry_ocr(slug)


def _agent_notes_target(args):
    """Which notes a set_agent_notes call writes (its role row in ACTION_ROLES): a card's or an item's."""
    return "card" if args.get("card") is not None else "slug"


@mcp.tool(selector=(_agent_notes_target, ("slug", "card")))
def constructicon_set_agent_notes(slug: str | None = None, notes: str | None = None,
                                  card: str | int | None = None, append: bool = False) -> dict | None:
    """Set (or, with null, clear) your private working notes on an item (slug=) or a card (card=, its
    id or slug); exactly one of them. E.g. "Fusion 360 screenshot, not user-facing", or story details
    the owner mentioned for a later write-up. Never shown in the app, the web API, search or exports.
    append=true ADDS notes under what is there as a dated block ("--- 2026-10-27 ---", Mountain Time,
    then the text) instead of replacing it: the safe way to add to notes you haven't read (empty
    notes with append is refused).
    Read them back: constructicon_get(slug, include=["agent_notes"]) for an item,
    constructicon_get_project(card) for a card. Undoable.
    -> the item (or card) plus agent_notes (the whole notes after); a card, and any append, also
    gets batch_id.
    """
    if (slug is None) == (card is None):
        raise InvalidInput("Pass exactly one of slug= (an item) or card= (a card).")
    if not isinstance(append, bool):
        raise InvalidInput("append must be true or false.")
    if card is not None:  # #745: the card twin, through the card service (not_found if missing)
        res = cards.set_agent_notes(card, notes, append=append)  # #763: the append is atomic in core
        return {**_to_public_project(res.data["card"]), "agent_notes": res.data["agent_notes"],
                "batch_id": res.batch_id}
    if append:  # #763: read-and-add inside one transaction, in the item service
        res = items.append_agent_notes(slug, notes)
    else:
        res = items.update(slug, agent_notes=notes)  # #541 phase D: through the item service (not_found if missing)
    row = db.get_by_slug(slug)
    # Never return the raw DB row: it carries the `embedding` BLOB, which
    # isn't JSON-serializable, so the tool call itself failed for every row
    # that had been through OCR (#211).
    out = {**_to_public(row), "agent_notes": row.get("agent_notes")}
    if append:  # #763: the append's batch, so it can be undone; the plain set's answer is unchanged (#745)
        out["batch_id"] = _written(res.batch_id)
    return out


@mcp.tool()
def constructicon_list_provenance_options(scope: str = "card", include_retired: bool = False) -> dict:
    """The provenance lists the owner edits in /admin. scope: "card" (for
    constructicon_update_project fields.provenance) or "file" (for constructicon_update
    fields.provenance). include_retired=true adds retired keys (not settable on anything new).
    -> {ok, scope, options: [{key, label, sort_order, retired}]}.
    """
    opts = provenance_options.list_options(scope, include_retired=include_retired)
    return {"ok": True, "scope": scope,
            "options": [{k: o[k] for k in ("key", "label", "sort_order", "retired")} for o in opts]}


@mcp.tool()
def constructicon_list_vocab(vocab: str = "brand_role") -> dict:
    """The word lists the owner edits in /admin. vocab: "brand_role" (the suggested roles
    for constructicon_update fields.brand_role; a custom role is still allowed) or "wallpaper_tag"
    (the tag names that put an item on the Wallpaper page, any case).
    -> {ok, vocab, terms: [{term, sort_order}]}.
    """
    return {"ok": True, "vocab": vocab,
            "terms": [{"term": t["term"], "sort_order": t["sort_order"]} for t in vocab_svc.list_terms(vocab)]}


@mcp.tool(alias_of=("constructicon_update", None))
def constructicon_set_provenance(slug: str, provenance: str | None = None) -> dict | None:
    row = items.update(slug, provenance=provenance).item
    return {**_to_public(row), "provenance": row.get("provenance")}


@mcp.tool(alias_of=("constructicon_update", None))
def constructicon_set_content_date(slug: str, date: str | None = None) -> dict | None:
    items.get_item(slug)
    row = items.update(slug, content_date=items.parse_date(date)).item  # naive ISO -> Mountain Time
    return {**_to_public(row), "content_date": row.get("content_date")}


@mcp.tool(alias_of=("constructicon_update", None))
def constructicon_set_highlight(slug: str, on: bool = False) -> dict | None:
    row = items.update(slug, highlight=on).item  # the old default: no `on` UNmarks
    return {**_to_public(row), "highlight": row.get("highlight")}


@mcp.tool(alias_of=("constructicon_update", None))
def constructicon_set_brand_asset(slug: str, is_brand: bool = False, brand_role: str | None = None) -> dict | None:
    row = items.update(slug, is_brand_asset=is_brand, brand_role=brand_role).item  # old default: clears both
    return {**_to_public(row), "is_brand_asset": bool(row.get("is_brand_asset")), "brand_role": row.get("brand_role")}


@mcp.tool(alias_of=("constructicon_list_items", "brand_assets"))
def constructicon_list_brand_assets() -> list[dict]:
    return _list_brand_assets()


@mcp.tool(alias_of=("constructicon_update_project", None))
def constructicon_set_project_status(id_or_slug: str, status: str) -> dict | None:
    if not policy.can_view_card(db.get_project(id_or_slug)):  # #604 step 3: hidden = missing
        raise NotFound(f"No card {id_or_slug!r}.")
    legacy = card_rules.legacy_to_status(status)
    warnings = list(legacy["warnings"])
    if legacy["kind"]:
        warnings.extend(cards.set_kind(id_or_slug, legacy["kind"]).warnings)
    warnings.extend(cards.set_status(id_or_slug, legacy["stage"], legacy["stop_reason"]).warnings)
    return {**_to_public_project(db.get_project(id_or_slug)), "warnings": warnings}


@mcp.tool(alias_of=("constructicon_update_project", None))
def constructicon_set_status(card: str | int, stage: str, stop_reason: str | None = None,
                             activity: str | None = None, dry_run: bool = False) -> dict:
    return cards.set_status(card, stage, stop_reason, activity=activity, dry_run=dry_run).to_dict()


@mcp.tool(alias_of=("constructicon_update_project", None))
def constructicon_set_kind(card: str | int, kind: str, force: bool = False, dry_run: bool = False) -> dict:
    return cards.set_kind(card, kind, force=force, dry_run=dry_run).to_dict()


@mcp.tool(alias_of=("constructicon_update_project", None))
def constructicon_set_whereabouts(card: str | int, whereabouts: str | None = None, note: str | None = None,
                                  clear_note: bool = False, dry_run: bool = False) -> dict:
    return cards.set_whereabouts(card, whereabouts, "" if clear_note else (note if note is not None else ...),
                                 dry_run=dry_run).to_dict()


@mcp.tool(alias_of=("constructicon_update_project", None))
def constructicon_set_card_text(card: str | int | None = None, hobby: str | int | None = None,
                                synopsis: str | None = None, flavor: str | None = None,
                                clear_synopsis: bool = False, clear_flavor: bool = False,
                                dry_run: bool = False) -> dict:
    if (card is None) == (hobby is None):
        raise InvalidInput("Pass exactly one of card or hobby.")
    syn = "" if clear_synopsis else (synopsis if synopsis is not None else ...)
    fla = "" if clear_flavor else (flavor if flavor is not None else ...)
    if card is not None:
        return cards.set_text(card, synopsis=syn, flavor=fla, dry_run=dry_run).to_dict()
    return hobbies.set_text(hobby, synopsis=syn, flavor=fla, dry_run=dry_run).to_dict()


@mcp.tool(alias_of=("constructicon_update_project", None))
def constructicon_set_card_provenance(card: str | int, provenance: str | None = None, credit: str | None = None,
                                      clear_credit: bool = False, dry_run: bool = False) -> dict:
    return cards.set_provenance(card, provenance, "" if clear_credit else (credit if credit is not None else ...),
                                dry_run=dry_run).to_dict()


@mcp.tool(alias_of=("constructicon_update_project", None))
def constructicon_set_card_highlight(card: str | int, on: bool = False, dry_run: bool = False) -> dict:
    return cards.set_highlight(card, on, dry_run=dry_run).to_dict()  # the old default: no `on` UNmarks
