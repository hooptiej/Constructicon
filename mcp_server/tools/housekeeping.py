"""The MCP tools of one area. Library-wide work: delete, trash, duplicates (#615), lookalikes (#759),
proposals (#354 / #344), backup.

#547 step 3: moved verbatim from mcp_server/server.py, in its order (the order of the
modules in mcp_server/tools/__init__.py is the tools/list order).
"""

import typing

from mcp_server.app import mcp
from mcp_server._common import _DESTRUCTIVE, _delete_card, _only_for
from mcp_server.listing import _paged

from core import backup, correlations, duplicates, errors, lookalikes, items, provenance_rules, blog, reset
from core.errors import InvalidInput


def _delete_target(args):
    """Which delete a call asks for (its role row in ACTION_ROLES)."""
    if args.get("everything"):
        return "everything"
    return next((k for k in ("slugs", "card", "blog_entry") if args.get(k) is not None), "slug")


@mcp.tool(selector=(_delete_target, ("slug", "slugs", "card", "blog_entry", "everything")),
          annotations=_DESTRUCTIVE)
def constructicon_delete(slug: str | None = None, slugs: list[str] | None = None, card: str | int | None = None,
                         blog_entry: str | None = None, everything: bool = False, confirm: str | None = None,
                         missing_ok: bool = False, dry_run: bool = False, batch_id: str | None = None) -> dict:
    """Delete ONE kind of thing per call (undoable unless said otherwise):
    slug="x": an item, with its card/tag/relation/revision/blog links and open questions. The file
      goes to the trash for 7 days; undo restores all of it (after that: trash_expired).
      -> {deleted: true, batch_id, expires_at}.
    slugs=[...]: several items in one batch; unknown slugs are not_found unless missing_ok=true
      (then skipped). -> {deleted: <count>, slugs, batch_id, expires_at}.
    card=<id or slug>: a card. Its blank write-up goes too (a written one stays, with a warning);
      links, memberships and blog attachments go; nested cards stand alone; files stay.
      -> {ok, dry_run, changes, warnings, batch_id, deleted, children_orphaned, items_detached, writeup}.
    blog_entry=<slug>: a blog entry (its cards and files stay). -> {deleted: true, slug, batch_id}.
    everything=true AND confirm="DELETE EVERYTHING", nothing else: WIPE THE WHOLE ARCHIVE (items,
      files, cards, tags, hobbies, blog, questions, trash). PERMANENT, NOT undoable; settings,
      users and the change log stay. Either one alone is refused. Back up first
      (constructicon_backup). -> {deleted, counts, files_removed, trash_removed, batch_id}.
    dry_run and batch_id work for every kind except everything.
    """
    targets = [k for k, v in (("slug", slug), ("slugs", slugs), ("card", card), ("blog_entry", blog_entry))
               if v is not None]
    if everything:
        if targets:
            raise InvalidInput(f"everything=true wipes the whole archive and takes no other target (got "
                               f"{', '.join(targets)}). Nothing was deleted.")
        if confirm != reset.CONFIRM_PHRASE:
            raise errors.AppError("confirm_required", "Wiping the whole archive needs BOTH everything=true AND "
                                  f"confirm=\"{reset.CONFIRM_PHRASE}\". Nothing was deleted.", status=400)
        _only_for("everything=true (it is permanent: no dry run, no batch to join)",
                  dry_run=(dry_run, False), batch_id=(batch_id, None), missing_ok=(missing_ok, False))
        return reset.delete_everything(confirm)
    if confirm is not None:
        raise InvalidInput(f"confirm is only for everything=true (the whole-archive wipe); "
                           f"{'without everything=true it wipes nothing' if confirm == reset.CONFIRM_PHRASE else 'it is not needed here'}. "
                           "Nothing was deleted.")
    if len(targets) != 1:
        raise InvalidInput("Pass exactly one of slug, slugs, card or blog_entry (or everything=true with "
                           f"confirm=\"{reset.CONFIRM_PHRASE}\"). Nothing was deleted.")
    target = targets[0]
    if target != "slugs":
        _only_for(f"{target}=", missing_ok=(missing_ok, False))
    extra = {"dry_run": True} if dry_run else {}
    if target == "slug":
        result = items.delete([slug], dry_run=dry_run, batch_id=batch_id)
        return {"deleted": True, "batch_id": result.batch_id, "expires_at": result.data["expires_at"], **extra}
    if target == "slugs":
        if isinstance(slugs, str):
            raise InvalidInput("slugs must be a list; for one item pass slug=.")
        result = items.delete(slugs, missing_ok=missing_ok, dry_run=dry_run, batch_id=batch_id)
        return {"deleted": result.data["deleted"], "slugs": result.data["slugs"], "batch_id": result.batch_id,
                "expires_at": result.data["expires_at"], **extra}
    if target == "card":
        return _delete_card(card, dry_run, batch_id)
    res = blog.delete(blog_entry, dry_run=dry_run, batch_id=batch_id)
    return {"deleted": True, "slug": res.data["deleted"], "batch_id": res.batch_id, **extra}


@mcp.tool(alias_of=("constructicon_delete", "slugs"))
def constructicon_delete_multiple(slugs: list[str]) -> dict:
    result = items.delete(slugs, missing_ok=True)
    return {"deleted": result.data["deleted"], "batch_id": result.batch_id}


@mcp.tool(alias_of=("constructicon_delete", "everything"), annotations=_DESTRUCTIVE)
def constructicon_delete_all(confirm: str = "") -> dict:
    return reset.delete_everything(confirm)


def _empty_trash(confirm):
    r = items.empty_trash(confirm)
    return {**r, "message": f"Purged {r['purged']} deleted item(s) ({r['bytes']} bytes). "
                            f"{r['held_kept']} redacted file(s) are still held (not touched)."}


@mcp.tool(selector="action", annotations=_DESTRUCTIVE)
def constructicon_trash(action: typing.Literal["list", "empty"] = "list", confirm: str | None = None) -> dict:
    """The trash. Deleted items' files wait there 7 days (undo brings them back); redacted files
    are held until the owner decides.
    "list" (default): {count, bytes, oldest, next_expiry, days, items: [{slug, title, batch_id,
      reason, size_bytes, created_at, expires_at}], held: {count, bytes, items}}.
    "empty": PERMANENTLY purge every ordinary deleted file now; needs confirm="EMPTY TRASH".
      Redact holds are not touched; those deletes can no longer be undone.
      -> {purged, bytes, slugs, held_kept, message}. Error confirm_required.
    """
    if action == "list":
        _only_for('action="list"', confirm=(confirm, None))
        return items.trash_summary()
    if action == "empty":
        return _empty_trash(confirm or "")
    raise InvalidInput(f"Unknown action {action!r}: use list or empty.")


@mcp.tool(alias_of=("constructicon_trash", "list"))
def constructicon_list_trash() -> dict:
    return items.trash_summary()


@mcp.tool(alias_of=("constructicon_trash", "empty"))
def constructicon_empty_trash(confirm: str) -> dict:
    return _empty_trash(confirm)


@mcp.tool(selector="action", annotations=_DESTRUCTIVE)
def constructicon_duplicates(action: typing.Literal["plan", "apply"] = "plan", dry_run: bool = True,
                             expect_trash: list[str] | None = None) -> dict:
    """Byte-identical duplicate files: the same stored bytes under several items.
    "plan" (default, read-only): {summary, summary_text, groups: [{sha256, size_bytes, keep, why,
      trash: [{slug, title, filename, date, cards, ...}], inherit: {cards, tags, free_text_tags,
      fields, flags}, in_revision_chain}], skipped, unreadable}. The survivor is the copy with the
      earliest real date from before Constructicon's first upload (ties: more cards, more metadata,
      older upload).
    "apply": folds every group into its survivor (it inherits the copies' cards, tags, blank text
      fields, flags, related links, covers) and trashes the copies (7-day undo), ONE batch.
      DRY-RUN BY DEFAULT (dry_run=false writes). expect_trash = the slugs a dry run listed: if
      the fresh plan differs it refuses (plan_changed) and writes nothing. Admin only.
      -> {ok, dry_run, batch_id, changes, trashed, kept, expires_at, summary, groups}.
    """
    if action == "plan":
        _only_for('action="plan"', dry_run=(dry_run, True), expect_trash=(expect_trash, None))
        built = duplicates.plan()
        return {**built, "summary_text": duplicates.summary_text(built)}
    if action == "apply":
        res = duplicates.apply(dry_run=dry_run, expect_trash=expect_trash).to_dict()
        res["summary_text"] = duplicates.summary_text(res)
        return res
    raise InvalidInput(f"Unknown action {action!r}: use plan or apply.")


@mcp.tool(selector="action", annotations=_DESTRUCTIVE)
def constructicon_lookalikes(action: typing.Literal["list", "confirm", "dismiss"] = "list", keep: str | None = None,
                             slugs: list[str] | None = None, dry_run: bool = True, limit: int | None = None,
                             cursor: str | None = None) -> dict:
    """Look-alike photos: the same picture under different bytes (resized, recompressed, re-exported),
    found by perceptual hash and never folded without a yes. Byte-identical copies are
    constructicon_duplicates' job; items in one revision chain are never look-alikes.
    "list" (default, read-only, paged: limit 1..500, default 20, cursor): {items: [{id, slugs, keep,
      why, members: [{slug, title, filename, width, height, file_size, date, date_is_real, uploaded,
      cards, copy_marker}], pairs: [{a, b, distance}], answered, new_members}], next_cursor, more,
      limit, summary}. `keep` is the suggested keeper (same rule as duplicates), `why` says what
      decided; `answered` = pairs already said "Not the same", `new_members` = what is being asked now.
    "confirm" and "dismiss" are DRY-RUN BY DEFAULT: they answer as the write would and write nothing;
      pass dry_run=false to fold / record.
    "confirm" (keep=<slug>, slugs=optional subset of the group): "same photo, keep this one". The kept
      photo inherits the others' cards, tags, blank text fields, flags, related links and covers; the
      others go to the trash (7-day undo), ONE batch. A member answered "Not the same" against the
      keeper is left out (left_out). -> {ok, dry_run, batch_id, kept, trashed, left_out, plan, ...}.
      Refusals: not_found, forbidden, not_in_group, lookalike_conflict, nothing_to_fold.
    "dismiss" (slugs=[2+ photos of one group]): "not the same", remembered per pair so it is never
      asked again. -> {ok, batch_id, pairs}. Refusal: not_lookalikes.
    Example: constructicon_lookalikes(action="confirm", keep="abc123") (a dry run), then again with
    dry_run=False to fold.
    """
    if action == "list":
        _only_for('action="list"', keep=(keep, None), slugs=(slugs, None), dry_run=(dry_run, True))
        built = lookalikes.plan()
        groups = built["groups"]
        page = _paged("lookalikes", lambda n, o: groups[o:o + n], limit, cursor, 0, shape=lambda rows: rows,
                      default=20)
        return {**page, "summary": built["summary"], "unreadable": built["unreadable"]}
    _only_for(f'action="{action}"', limit=(limit, None), cursor=(cursor, None))
    if action == "confirm":
        return lookalikes.confirm(keep, slugs, dry_run=dry_run).to_dict()
    if action == "dismiss":
        _only_for('action="dismiss"', keep=(keep, None))
        return lookalikes.dismiss(slugs, dry_run=dry_run).to_dict()
    raise InvalidInput(f"Unknown action {action!r}: use list, confirm or dismiss.")


@mcp.tool(selector="action")
def constructicon_proposals(which: typing.Literal["provenance", "correlation"],
                            action: typing.Literal["list", "confirm", "dismiss"] = "list",
                            slugs: list[str] | None = None, pairs: list[list[str]] | None = None,
                            rule: str | None = None, dry_run: bool = True, limit: int | None = None,
                            cursor: str | None = None) -> dict:
    """The Curator's proposals that wait for a yes: nothing is written until confirmed.
    which="provenance": file provenance by gross rule. The first (and only) rule, text_heavy_reference:
      a PDF with 2,000+ characters of text, or a PDF / Word file whose name says manual, guide, datasheet,
      handbook, instructions, reference or spec, proposed as provenance "reference". A file whose
      provenance is already set is never proposed.
    which="correlation": card pairs that look related (text similarity of their files, shared tags,
      overlapping dates, shared files; score 0..1, suggested at 0.45+ with alike text plus at least one
      other signal), never ones already linked,
      nested, sharing a parent or a family, or a family / collection.
    "list" (default, read-only, paged: limit 1..500, default 20, cursor): provenance -> {items: [{slug,
      title, filename, media_type, chars, reason, evidence}], rule, blocked, summary}; correlation ->
      {items: [{a, b, a_title, b_title, score, parts, evidence, why}], summary}.
    "confirm" and "dismiss" are DRY-RUN BY DEFAULT; pass dry_run=false to write. One call = one batch,
      one constructicon_undo(batch_id) reverses it.
    "confirm": provenance writes the rule's provenance on `slugs` (default: every current proposal);
      correlation links `pairs` ([[a, b], ...], card slugs; default: every current suggestion) as
      "related". -> {ok, dry_run, batch_id, applied | linked, skipped}. Refusals: forbidden, not_found,
      not_proposed, nothing_to_confirm, rule_blocked, bad_rule.
    "dismiss" ("not this one" / "not related", remembered so it is never proposed again): provenance
      `slugs`, correlation `pairs`. -> {ok, batch_id, slugs | pairs}. Refusal: not_proposed.
    Example: constructicon_proposals(which="provenance") then
    constructicon_proposals(which="provenance", action="confirm", slugs=["abc"], dry_run=False).
    """
    if which not in ("provenance", "correlation"):
        raise InvalidInput(f"Unknown which {which!r}: use provenance or correlation.")
    if which == "correlation":
        _only_for('which="correlation"', slugs=(slugs, None), rule=(rule, None))
    else:
        _only_for('which="provenance"', pairs=(pairs, None))
    if action == "list":
        _only_for('action="list"', slugs=(slugs, None), pairs=(pairs, None), dry_run=(dry_run, True))
        if which == "provenance":
            built = provenance_rules.plan(rule=rule)
            rp = built["rules"][0] if rule else None
            props = rp["proposals"] if rp else [p for r in built["rules"] for p in r["proposals"]]
            page = _paged(f"proposals:provenance:{rule or 'all'}", lambda n, o: props[o:o + n], limit, cursor, 0,
                          shape=lambda rows: rows, default=20)
            return {**page, "summary": built["summary"],
                    "rules": [{**r["rule"], "blocked": r["blocked"], "summary": r["summary"]} for r in built["rules"]]}
        built = correlations.plan()
        found = built["pairs"]
        page = _paged("proposals:correlation", lambda n, o: found[o:o + n], limit, cursor, 0,
                      shape=lambda rows: rows, default=20)
        return {**page, "summary": built["summary"]}
    _only_for(f'action="{action}"', limit=(limit, None), cursor=(cursor, None))
    if which == "provenance":
        if action == "confirm":
            return provenance_rules.confirm(slugs, rule=rule, dry_run=dry_run).to_dict()
        if action == "dismiss":
            return provenance_rules.dismiss(slugs, rule=rule, dry_run=dry_run).to_dict()
    else:
        if action == "confirm":
            return correlations.confirm(pairs, dry_run=dry_run).to_dict()
        if action == "dismiss":
            return correlations.dismiss(pairs, dry_run=dry_run).to_dict()
    raise InvalidInput(f"Unknown action {action!r}: use list, confirm or dismiss.")


@mcp.tool()
def constructicon_backup() -> dict:
    """Back up the whole database and storage to a timestamped zip:
    {filename, path, size, created_at, integrity}."""
    return backup.create_backup()
