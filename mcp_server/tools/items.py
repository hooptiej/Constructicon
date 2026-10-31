"""The MCP tools of one area. One item's fields: update, the caption tools (view, set_caption,
caption_review), undated, redact (+ aliases).

#547 step 3: moved verbatim from mcp_server/server.py, in its order (the order of the
modules in mcp_server/tools/__init__.py is the tools/list order).
"""

import typing

from mcp.server.mcpserver import Image as McpImage

from mcp_server.app import mcp
from mcp_server.shapes import TEXT_PREVIEW_CHARS, _ELLIPSIS, _to_public
from mcp_server._common import _DESTRUCTIVE, _fields_arg, _needs_caption, _only_for, _written
from mcp_server.listing import _paged

from core import access_log, policy, captions, thumbnails, db, item_dates, item_title, items, object_types
from core import changes, parse_limits, transcripts
from core.errors import InvalidInput, NotFound


# #746: what constructicon_update(fields={...}) takes beyond its named parameters.
ITEM_FIELDS = {
    "provenance": (str, type(None)),
    "content_date": (str, int, float, type(None)),
    "highlight": (bool,),
    "is_brand_asset": (bool,),
    "brand_role": (str, type(None)),
    "sensitive": (bool,),
    "transcript_vtt": (str,),  # #789: a YouTube video's subtitles (WebVTT / SRT text) -> transcript + cue times
}


def _transcript_from_vtt(slug, vtt):
    """#789: the type_metadata a subtitle file gives a video item (refused for any other type)."""
    row = items.get_item(slug)
    spec = object_types.get_object_type(row.get("media_type"))
    if getattr(spec, "text_fetch_fn", None) is None:
        raise InvalidInput(f"fields.transcript_vtt is for a YouTube video item; {slug!r} is {spec.label}. "
                           "Nothing was changed.", code="bad_transcript")
    meta = transcripts.fields_from_vtt(vtt)
    meta["transcript_note"] = "from a subtitle file handed in through the MCP"
    return meta


@mcp.tool()
def constructicon_update(slug: str, description: str | None = None, tags: list[str] | None = None,
                   display_name: str | None = None,
                   type_metadata: dict | None = None, display_date: float | None = None,
                   reset_display_date: bool = False, content_description: str | None = None,
                   fields: dict | None = None, dry_run: bool = False, short_title: str | None = None) -> dict:
    """Edit an item, every change in ONE batch. Parameters left out (None) are left alone.
    description: the uploader's note. content_description: the item's long caption/description ("" clears).
    display_name: its title override. short_title: its short name, a few words ("" clears); the title
    is display_name, else short_title, else the filename. tags: the item's full tag list (replaces its chips in the tag tree).
    type_metadata: MERGED by top-level key; a key set to "" is cleared.
    display_date: timeline position override (unix seconds); reset_display_date=true clears it.
    fields: more fields, key present = set it, absent = leave it:
      provenance: an active file key (constructicon_list_provenance_options(scope="file")) or null;
      content_date: the content's own date, ISO or unix seconds, null clears;
      highlight: bool; is_brand_asset: bool; brand_role: "logo"/"icon"/... or null;
      sensitive: bool (marking: editor+; clearing: admin);
      transcript_vtt: a YouTube video's subtitles as WebVTT or SRT text (e.g. from yt-dlp): becomes its
        transcript (searchable) WITH cue times, so constructicon_get / search can cite a &t= moment.
    E.g. fields={"provenance": "created", "highlight": true, "content_date": "2017-07-31"}.
    dry_run=true returns the item as it would be, writing nothing.
    Returns the item plus batch_id. Private notes: constructicon_set_agent_notes.
    """
    items.get_item(slug)
    extra = _fields_arg(fields, ITEM_FIELDS, "constructicon_update")
    cols = {}
    if description is not None:
        cols["description"] = description
    if display_name is not None:
        cols["display_name"] = display_name
    if content_description is not None:
        cols["content_description"] = content_description
    if short_title is not None:
        cols["short_title"] = short_title
    if type_metadata is not None:
        # #563: merge (top-level keys), same as the web route; #425 keys cleaned in core.
        cols["type_metadata"] = dict(type_metadata)
    if "transcript_vtt" in extra:  # #789: parsed (and refused) before anything is written
        cols["type_metadata"] = {**cols.get("type_metadata", {}), **_transcript_from_vtt(slug, extra["transcript_vtt"])}
    if reset_display_date:
        cols["display_date_override"] = None
    elif display_date is not None:
        cols["display_date_override"] = display_date
    for key in ("provenance", "highlight"):
        if key in extra:
            cols[key] = extra[key]
    if "content_date" in extra:
        cols["content_date"] = items.parse_date(extra["content_date"], "content_date")  # naive ISO = Mountain
    if "is_brand_asset" in extra:
        cols["is_brand_asset"] = extra["is_brand_asset"]
        if extra["is_brand_asset"] and "brand_role" not in extra:
            cols["brand_role"] = items.get_item(slug).get("brand_role")  # absent = leave the role as it is
    if "brand_role" in extra:
        cols["brand_role"] = extra["brand_role"]
    batch_id = changes.new_batch_id()
    # #746: one transaction for the whole call; a dry run is the real thing rolled back, so every
    # field is checked against the item as the earlier fields left it.
    with db.transaction(dry_run=dry_run):
        if cols or tags is not None:
            # #541 phase C: free-text tags ride in the same call, so the whole update is ONE batch.
            items.update(slug, tags=tags, batch_id=batch_id, **cols)
        if "sensitive" in extra:
            items.set_sensitive(slug, extra["sensitive"], batch_id=batch_id)
        out = {**_to_public(db.get_by_slug(slug)), "batch_id": None if dry_run else _written(batch_id)}
    if dry_run:
        out["dry_run"] = True
    return out


# --- Captioning by an agent (#588): list what needs a caption, look at it, write one ---

VIEW_OCR_CHARS = 1500  # how much OCR text constructicon_view echoes next to the picture


@mcp.tool(alias_of=("constructicon_needs", "caption"))
def constructicon_list_needs_caption(limit: int = 50, include_failed: bool = True) -> dict:
    return _needs_caption(limit, include_failed)


@mcp.tool()
def constructicon_view(slug: str, size: str = "preview") -> list[McpImage | str]:
    """Look at an item: its picture as IMAGE content you can see, plus a text block (name, type,
    description, suggested caption, OCR text). size="preview" (default): up to 1024 px;
    "thumb": the 400 px thumbnail. Other types show through their renderer (a PDF's first page, an
    STL render, a video frame). Errors: bad_size, not_viewable (redacted, or no picture).
    """
    if size not in thumbnails.VIEW_SIZES:
        raise InvalidInput(f"size must be one of: {', '.join(thumbnails.VIEW_SIZES)}", code="bad_size")
    row = db.get_by_slug(slug)
    if row is None:
        raise NotFound(f"No item {slug!r}.")
    policy.require_view(row, message=f"No item {slug!r}.")  # #557
    if row["redacted"]:
        raise InvalidInput("This item was redacted: its file was removed, so there is nothing to look at.",
                           code="not_viewable")
    rendered = thumbnails.render_view(row, size)
    if rendered is None:
        # #644: say so when a parser limit stopped it (an oversized image is not "no picture").
        hit = next(iter(parse_limits.failures_for(thumbnails.limit_key(row))), None)
        if hit:
            raise InvalidInput(f"No picture of this item ({hit['what']}): {hit['message']}. It will be tried "
                               f"again in about {hit['retry_in_minutes']} min.", code="not_viewable")
        raise InvalidInput(f"{object_types.get_object_type(row.get('media_type')).label} items have no picture to view.",
                           code="not_viewable")
    policy.note_access(row, access_log.HOW_MCP_VIEW)  # #604 follow-up 7: sensitive items only
    data, mime, (width, height) = rendered
    tm = row.get("type_metadata") or {}
    lines = [f"{item_title.title_of(row)} [{slug}]",
             f"type: {row.get('media_type') or 'image'}; shown as {mime} {width}x{height}, {len(data) // 1024} KB ({size})"]
    if row.get("content_description"):
        lines.append(f"description: {row['content_description']}")
    if tm.get(captions.METADATA_KEY):
        lines.append(f"suggested caption ({tm.get(captions.STATUS_KEY) or 'unknown'}): {tm[captions.METADATA_KEY]}")
    ocr_text = (row.get("extracted_text") or "").strip()
    if ocr_text:
        shown = ocr_text[:VIEW_OCR_CHARS]
        lines.append("OCR text" + (f" (first {VIEW_OCR_CHARS} of {len(ocr_text)} characters)" if len(ocr_text) > VIEW_OCR_CHARS else "") + ":\n" + shown)
    else:
        lines.append("OCR text: none")
    return [McpImage(data=data, format=mime.split("/")[1]), "\n".join(lines)]


@mcp.tool()
def constructicon_set_caption(slug: str, text: str | None = None, title: str | None = None, accept: bool = False,
                              replace: bool = False) -> dict:
    """Write a caption and/or a short title for an item you have looked at (constructicon_view). No model runs.
    text: the long caption (1-2000 characters), the item's caption SUGGESTION (model "mcp-agent").
    title: a short title of a few words (at most 100 characters), the item's NAME on tiles; the long
    caption is never the title, so give one with every caption. Both wait in the owner's caption
    review; accept=true also sets content_description to the text and short_title to the title.
    Never overwrites silently: replacing a suggestion, description or short title that the local
    model did not write (yours, another agent's, the owner's) is refused with would_replace_suggestion
    (details.existing has each text that would go). Read it; pass replace=true only to replace it.
    E.g. set_caption(slug, text="Kendall's buggy airborne off the berm, dust trailing.",
    title="Kendall's 8IGHT-T carving the berm", accept=true).
    Errors: bad_caption, bad_title, redacted, would_replace_suggestion.
    Returns the item plus batch_id, accepted, replaced_suggestion (the suggestion text this call
    replaced, or null), replaced (every replaced text, by field) and in_review_queue.
    """
    row = items.get_item(slug)
    policy.require_view(row, message=f"No item {slug!r}.")  # #557
    result = items.set_caption(slug, text, title=title, accept=accept, replace=replace)
    it = result.item
    tm = it.get("type_metadata") or {}
    waiting_caption = bool((tm.get("auto_caption") or "").strip()) and not (it.get("content_description") or "").strip()
    waiting_title = bool((tm.get("auto_caption_title") or "").strip()) and not (it.get("short_title") or it.get("display_name"))
    return {**_to_public(it), "batch_id": result.batch_id, "accepted": bool(accept),
            "replaced_suggestion": result.data["replaced_suggestion"], "replaced": result.data["replaced"],
            "in_review_queue": waiting_caption or waiting_title}


CAPTION_REVIEW_PAGE_DEFAULT = 50  # #722: one review page; a caption can be a long paragraph


def _caption_review_rows(rows):
    """The review queue's entries for one SQL page (captions.review_entries applies the item policy),
    with the caption and description cut to previews (+ *_chars, *_truncated)."""
    out = []
    for entry in captions.review_entries(rows):
        for key in ("caption", "description"):
            text = entry.get(key) or ""
            entry[f"{key}_chars"] = len(text)
            entry[f"{key}_truncated"] = len(text) > TEXT_PREVIEW_CHARS
            if len(text) > TEXT_PREVIEW_CHARS:
                entry[key] = text[:TEXT_PREVIEW_CHARS] + _ELLIPSIS
        out.append(entry)
    return out


@mcp.tool(selector="action")
def constructicon_caption_review(action: typing.Literal["list", "accept", "retry", "skip"] = "list",
                                 slug: str | None = None, text: str | None = None, title: str | None = None,
                                 replace: bool = False, what: typing.Literal["all", "caption", "title"] = "all",
                                 advance: bool = False, limit: int | None = None,
                                 cursor: str | None = None) -> dict:
    """The owner's caption review queue (the web's Captions review page), and its buttons:
    "list": items whose caption or short-title suggestion waits for review, PAGED, newest first:
      {items: [{slug, name, kind ("caption" | "title": only a title waits), caption, caption_chars,
      caption_truncated, caption_by, description, description_chars, description_truncated, title,
      title_by, thumb_url, detail_url}], next_cursor, more, limit}. limit 1..500 (default 50); pass
      next_cursor back as cursor= (null = the end). caption / description are ~300-character previews;
      read a whole suggestion with constructicon_get(slug) (type_metadata.auto_caption).
    "accept" (slug): the suggestion becomes the description and short title, one undoable batch;
      text= / title= accept your edited wording instead. Text a local model didn't write is not
      replaced silently: would_replace_suggestion unless replace=true. -> {item, batch_id,
      replaced_suggestion, replaced}.
    "skip" (slug): reviewed, not used; it leaves the queue. what= all (default) | caption | title.
      -> {item, batch_id}.
    "retry" (slug): run the local caption model on it again (queued; type_metadata.auto_caption_status
      goes "pending"); advance=true tries the next prompt for variety. captions_disabled (409) on an
      install with no caption model: look at it (constructicon_view) and constructicon_set_caption.
      -> {item}.
    E.g. constructicon_caption_review(action="accept", slug="abc123").
    """
    if action == "list":
        _only_for('action="list"', slug=(slug, None), text=(text, None), title=(title, None),
                  replace=(replace, False), what=(what, "all"), advance=(advance, False))
        return _paged("captions", lambda n, o: db.list_unaccepted_captions(limit=n, offset=o), limit, cursor,
                      TEXT_PREVIEW_CHARS, shape=_caption_review_rows, default=CAPTION_REVIEW_PAGE_DEFAULT)
    _only_for(f'action="{action}"', limit=(limit, None), cursor=(cursor, None))
    if not (slug or "").strip():
        raise InvalidInput(f'action="{action}" needs slug (the item).')
    if action == "accept":
        _only_for('action="accept"', what=(what, "all"), advance=(advance, False))
        res = captions.accept(slug, text, title, replace)
        return {"ok": True, "item": _to_public(res.item), "batch_id": res.batch_id,
                "replaced_suggestion": res.data["replaced_suggestion"], "replaced": res.data["replaced"]}
    if action == "skip":
        _only_for('action="skip"', text=(text, None), title=(title, None), replace=(replace, False),
                  advance=(advance, False))
        res = captions.skip(slug, what)
        return {"ok": True, "item": _to_public(db.get_by_slug(slug)), "batch_id": _written(res.batch_id)}
    if action == "retry":
        _only_for('action="retry"', text=(text, None), title=(title, None), replace=(replace, False),
                  what=(what, "all"))
        return {"ok": True, "item": _to_public(captions.retry(slug, advance=advance))}
    raise InvalidInput(f"Unknown action {action!r}: use list, accept, skip or retry.")


@mcp.tool(selector="action")
def constructicon_undated(action: typing.Literal["list", "accept", "apply_auto"] = "list", slug: str | None = None,
                          source: str | None = None, include_inherited: bool = True, read_files: bool = False,
                          limit: int = 50, dry_run: bool = False) -> dict:
    """Items with no REAL date (the upload time and a file mtime from after the install's first upload
    are not dates), with date guesses, so you can date them.
    "list" (default): {items, total, needs_date}. Each item: slug, name, type, filename, imported,
      shows (what its card says: "Undated" or "≈ <range> · from <card>"), inherits (the card range it
      borrows, or null), needs_date (true = no range to borrow, so it is in the Curator), guesses
      [{date, epoch, source, confidence, evidence, auto, label}], best first. include_inherited=false
      lists only the needs_date ones (a card's write-up never needs a date). read_files=true also reads each file's own embedded date
      (slower: one parser process per file). limit 1..500.
    "accept": write `slug`'s guess from `source` (filename | file_metadata | youtube | ocr_text |
      siblings) as its content_date, source recorded. Returns {slug, batch_id, guess}.
    "apply_auto": write every high-confidence guess (in-file evidence: filename, file_metadata,
      youtube) to the undated items, ONE batch. Returns {applied, skipped, batch_id}; dry_run=true
      writes nothing and lists what it would do.
    Every write is undoable (constructicon_undo(batch_id)); an item that already has a real date is
    never overwritten.
    """
    if action == "list":
        if not 1 <= int(limit) <= 500:
            raise InvalidInput("limit must be between 1 and 500.", code="bad_limit")
        snap = item_dates.snapshot()
        rows = policy.filter_visible(item_dates.undated_rows(include_inherited=include_inherited, snap=snap))
        if not include_inherited:  # the Curator's set exactly (a card's write-up never needs a date)
            rows = [r for r in rows if item_dates.needs_date(r, snap=snap)]
        access = policy.card_access()  # #604: never name a card the caller can't see
        entries = [item_dates.listing_entry(r, read_file=read_files, snap=snap, access=access) for r in rows[:int(limit)]]
        return {"items": entries, "total": len(rows), "needs_date": sum(1 for e in entries if e["needs_date"])}
    if action == "accept":
        if not slug or not source:
            raise InvalidInput("accept needs slug and source.", code="bad_request")
        policy.writable_item(slug, message=f"No item {slug!r}.")  # #730: hidden = forbidden, saying why
        res = items.accept_date_guess(slug, source, dry_run=dry_run)
        return {"slug": slug, "batch_id": None if dry_run else _written(res.batch_id), "guess": res.data["guess"],
                **({"dry_run": True} if dry_run else {})}
    if action == "apply_auto":
        plan = item_dates.auto_plan(read_files=True)
        visible = {r["slug"] for r in policy.filter_visible([r for r, _g in plan])}
        plan = [(r, g) for r, g in plan if r["slug"] in visible]
        if not plan:
            return {"applied": [], "skipped": [], "batch_id": None, "message": "No undated item has a high-confidence guess."}
        res = items.apply_dates([(r["slug"], g.epoch, item_dates.source_record(g)) for r, g in plan], dry_run=dry_run)
        guesses = {r["slug"]: g.as_dict() for r, g in plan}
        return {"applied": [{"slug": s, **guesses[s]} for s in res.data["applied"]], "skipped": res.data["skipped"],
                "batch_id": None if dry_run else _written(res.batch_id), **({"dry_run": True} if dry_run else {})}
    raise InvalidInput(f"Unknown action {action!r}: use list, accept or apply_auto.")


def _redact(slug):
    result = items.redact(slug)
    return {**_to_public(result.item), "batch_id": result.batch_id}


def _unredact(slug):
    return _to_public(items.unredact(slug).item)


def _recover_redacted(slug):
    result = items.recover_redacted(slug)
    return {**_to_public(result.item), "batch_id": result.batch_id}


def _delete_redacted_file(slug, confirm):
    result = items.delete_redacted_file(slug, confirm)
    return {"deleted": True, "slug": slug, "bytes": result.data["bytes"]}


@mcp.tool(selector="action", annotations=_DESTRUCTIVE)
def constructicon_redact(slug: str, action: typing.Literal["redact", "unredact", "recover", "delete_file"] = "redact",
                         confirm: bool = False) -> dict:
    """Remove an item's file but keep its metadata (sensitive-content cleanup), and what follows:
    "redact" (default): the file is HELD in the trash, no expiry (empty-trash skips it), and the
      item is hidden from every listing. -> the item + batch_id.
    "recover": file back, un-redacted, exactly as before. -> the item + batch_id.
      Error no_redact_hold (the file was already deleted for good).
    "delete_file": PERMANENTLY erase the held file; needs confirm=true; NOT undoable. The item
      stays redacted, metadata only. -> {deleted: true, slug, bytes}. Error confirm_required.
    "unredact": visibility only, for a redaction whose file is already gone (refused with
      redact_hold_exists while a file is held: recover or delete_file first). -> the item.
    Errors: no_file, not_redacted. List them: constructicon_list_items(which="redacted").
    """
    if action == "redact":
        _only_for('action="redact"', confirm=(confirm, False))
        return _redact(slug)
    if action == "unredact":
        _only_for('action="unredact"', confirm=(confirm, False))
        return _unredact(slug)
    if action == "recover":
        _only_for('action="recover"', confirm=(confirm, False))
        return _recover_redacted(slug)
    if action == "delete_file":
        return _delete_redacted_file(slug, confirm)
    raise InvalidInput(f"Unknown action {action!r}: use redact, unredact, recover or delete_file.")


@mcp.tool(alias_of=("constructicon_redact", "unredact"))
def constructicon_unredact(slug: str) -> dict | None:
    return _unredact(slug)


@mcp.tool(alias_of=("constructicon_redact", "recover"))
def constructicon_recover_redacted(slug: str) -> dict:
    return _recover_redacted(slug)


@mcp.tool(alias_of=("constructicon_redact", "delete_file"))
def constructicon_delete_redacted_file(slug: str, confirm: bool = False) -> dict:
    return _delete_redacted_file(slug, confirm)
