"""Item service (#541 phase B): the ONE place item (capture_events) writes happen.

Same contract as core/cards.py: validate everything first, then one `db.transaction()`, every
row written through `db.ImageLog` (row images in the change log), and a `Result`. Each op takes
`dry_run` (the same path, rolled back: files never move) and `actor` (None = the request's
actor context, core/actor.py). Undo is the generic `cards.undo` / POST /api/changes/{id}/undo /
constructicon_undo(batch_id); it calls back into this module for the file side of the trash.

  update(slug, **fields)   title (display_name), short_title (#542), description, content_description, type_metadata
                           (merged; physical-piece keys cleaned), file provenance, highlight, brand
                           asset/role, display-date override, content_date. One Save = one batch.
  redact(slug)             "remove file, keep info": the file goes to the trash as a HOLD (no expiry),
                           the row is hidden. Never auto-purged.
  recover_redacted(slug)   the held file comes back and the item is un-redacted, as before.
  delete_redacted_file(slug, confirm)  erases the held file for good; the item stays redacted, file-less.
  unredact(slug)           visibility only, for old file-less redactions (refused while a hold exists).
  retype(slug, type, run)  the media_type change is imaged; post-processing (OCR / thumbnail /
                           caption / embedded metadata) re-runs through the caller's runner.
  replace_file(slug, bytes)  (#617) new bytes under the SAME slug; the old file goes to the trash
                           (reason "replace", TRASH_DAYS) in the same batch as the row change, so one
                           undo brings it back. Same type only; re-runs the ingest post-processing.
  delete(slugs)            files -> <storage>/.trash/<batch_id>/, rows (and everything that points
                           at them) deleted with row images, one `trash` row per item.
  relate(a, b) / unrelate(a, b)   (phase C) the "related" link; relate also shares tags and
                           card memberships both ways (#16), all imaged.
  update(..., tags=[...])  (phase C) the free-text tags ride in the same batch (core/tags.py).
  set_sensitive(slugs, on) (#603) the "This is sensitive" flag: mark = editor+, clear = admin only.
  set_provenance(slugs, key) (#553) the file grids' bulk provenance, one batch.
  apply_dates(entries)      (#725) guessed dates -> content_date (+ type_metadata.content_date_source),
                           one batch, fill-only (an item with a real date is skipped).
  accept_date_guess(slug, source)  (#725) one item's guess from one source, via apply_dates.
  purge_expired() / empty_trash(confirm) / trash_summary()

Trash (owner decision on #541, 2026-10-04): a deleted file stays in the trash for TRASH_DAYS,
then the web worker's hourly purge removes it. Until then undo restores rows AND files. Once
purged, undo refuses with `trash_expired` and changes nothing; ZFS snapshots stay the long-term
net. A REDACT is different (owner decision, 2026-10-04): the file is held with no expiry until the
owner clicks Recover or "Delete file permanently"; neither the hourly purge nor "Empty trash now"
touches it. The trash lives under the storage root, so it is on the same dataset (atomic renames, in
the snapshots).

Pipeline writes stay raw on purpose: caption status/results (core/captions.py), upload-time
embedded metadata (core/embedded_metadata.py), a YouTube row's fetched publish date, OCR state.
They are machine bookkeeping, not curation, and imaging them would make every later undo of a
real edit conflict with the pipeline's own updates.
"""

import io
import json
import os
import time
from pathlib import Path
from datetime import datetime

from . import captions, cards, changes, db, embedded_metadata, ingest, item_title, membership, object_types, physical_piece, provenance_options
from . import parse_limits, paths, storage, text_sources, thumbnails, timeline
from . import tags as tags_svc
from .cards import Result
from .errors import AppError, Conflict, InvalidInput, NotFound

OP_UPDATE = "item_update"
OP_SET_CAPTION = "item_set_caption"
OP_RELATE = "item_relate"
OP_UNRELATE = "item_unrelate"
OP_REDACT = "item_redact"
OP_UNREDACT = "item_unredact"
OP_RETYPE = "item_retype"
OP_DELETE = "item_delete"
OP_PURGE = "trash_purge"
OP_RECOVER = "item_recover_redacted"
OP_ERASE = "item_redact_erase"
OP_SENSITIVE = "item_sensitive"  # #603
OP_REPLACE_FILE = "item_replace_file"  # #617
OP_BULK_PROVENANCE = "item_bulk_provenance"  # #553
OP_DATE_GUESS = "item_date_guess"  # #725: guessed dates applied as content_date, one batch
REASON_REDACT = "redact"
REASON_REPLACE = "replace"  # #617: the bytes a replace_file displaced

TRASH_DIR_NAME = paths.TRASH_DIR_NAME
TRASH_DAYS = 7
TRASH_TTL_SECONDS = TRASH_DAYS * 24 * 3600
EMPTY_TRASH_PHRASE = "EMPTY TRASH"

UPDATE_FIELDS = ("display_name", "short_title", "description", "content_description", "type_metadata",
                 "provenance", "highlight", "is_brand_asset", "brand_role", "display_date_override",
                 "content_date", "agent_notes")


# --- helpers ---------------------------------------------------------------------------

def get_item(slug, actor=None):
    """The item row; NotFound (404 `not_found`) when there is no such row, and the #709 refusal
    (403 `forbidden`, saying why: policy.refusal) when `actor` (None = the context's) may not see
    it. Every single-item op refuses a hidden item itself, whichever front end called it."""
    from . import policy  # lazy: policy is imported by db at call time
    if not slug:
        raise NotFound(f"No item {slug!r}.")
    return policy.viewable_rows([slug], actor=actor)[0][0]


def _slim(table, img):
    """Change summaries echo whole-row images; an item row can carry a megabyte of OCR text."""
    if img is None or table != "capture_events":
        return img
    return {k: img.get(k) for k in ("slug", "display_name", "short_title", "filename", "media_type", "stored_filename")}


def _flat(op, muts):
    out = []
    for m in muts:
        b, a = m.get("before"), m.get("after")
        if b is None or a is None:
            out.append({"op": op, "table": m["table"], "key": m["key"], "field": "(row)",
                        "before": _slim(m["table"], b), "after": _slim(m["table"], a)})
            continue
        for col in a:
            if b.get(col) != a[col]:
                out.append({"op": op, "table": m["table"], "key": m["key"], "field": col,
                            "before": b.get(col), "after": a[col]})
    return out


def _result(op, muts, batch_id, dry_run, item=None, **data):
    res = Result(True, _flat(op, muts), [], batch_id, dry_run, data)
    if item is not None:
        item.pop("embedding", None)
    res.item = item  # the row after the write (None for delete); adapters shape it
    return res


def _epoch(name, value):
    if value is None or value == "":
        return None
    if isinstance(value, bool):
        raise InvalidInput(f"{name} must be a unix-seconds number", code="bad_date")
    try:
        return float(value)
    except (TypeError, ValueError):
        raise InvalidInput(f"{name} must be a unix-seconds number", code="bad_date") from None


def parse_date(value, name="date"):
    """An ISO date/datetime (naive = Mountain Time, core/timeline.py), unix seconds, or None/""
    (clear). Returns epoch seconds or None."""
    if value is None or (isinstance(value, str) and not value.strip()):
        return None
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return float(value)
    s = str(value).strip()
    try:
        return float(s)
    except ValueError:  # silent-ok: not a bare number; tried as an ISO date next, then refused
        pass
    try:
        dt = datetime.fromisoformat(s)
    except ValueError:
        raise InvalidInput(f"Unrecognized {name} {value!r}: use an ISO date/datetime ('YYYY-MM-DD' or "
                           f"'YYYY-MM-DDTHH:MM:SS') or unix seconds", code="bad_date") from None
    return timeline.source_datetime_to_epoch(dt)


# --- field edits -------------------------------------------------------------------------

def _plan_update(row, fields):
    """Validates `fields` against `row`; returns the capture_events columns to write. A key's
    presence means "set it"; None / "" clears an optional field."""
    unknown = sorted(set(fields) - set(UPDATE_FIELDS))
    if unknown:
        raise InvalidInput(f"Unknown item field(s): {', '.join(unknown)}", code="unknown_field",
                           details={"allowed": list(UPDATE_FIELDS)})
    cols = {}
    if "display_name" in fields:
        v = fields["display_name"]
        cols["display_name"] = str(v) if v not in (None, "") else None
    if "short_title" in fields:  # #542: the short name (core/item_title.py); None / "" clears
        v = fields["short_title"]
        if v not in (None, "") and not isinstance(v, str):
            raise InvalidInput("short_title must be text", code="bad_short_title")
        clean = item_title.clean_short_title(v, limit=10 ** 6) if v else None
        if clean and len(clean) > item_title.SHORT_TITLE_MAX:
            raise InvalidInput(f"The short title is {len(clean)} characters; the limit is {item_title.SHORT_TITLE_MAX}. "
                               "A short title is a few words; the long caption is the description.",
                               code="bad_short_title")
        cols["short_title"] = clean
    if "description" in fields:
        cols["description"] = "" if fields["description"] is None else str(fields["description"])
    if "content_description" in fields:
        cols["content_description"] = fields["content_description"]
    if "type_metadata" in fields:
        tm = fields["type_metadata"]
        if tm is None:
            tm = {}
        if not isinstance(tm, dict):
            raise InvalidInput("type_metadata must be a JSON object", code="bad_type_metadata")
        existing = row.get("type_metadata") or {}
        merged = {**existing, **physical_piece.clean_fields(tm)}  # #425 keys; raises bad_physical_piece
        if merged != existing:
            cols["type_metadata"] = json.dumps(merged)
            _plan_text_sources(row, merged, cols)
    if "provenance" in fields:
        cols["provenance"] = provenance_options.validate("file", fields["provenance"] or None,
                                                         current=row.get("provenance"))
    if "highlight" in fields:
        cols["highlight"] = 1 if fields["highlight"] else 0
    if "is_brand_asset" in fields:
        if fields["is_brand_asset"]:
            cols["is_brand_asset"] = 1
            cols["brand_role"] = fields.get("brand_role") or None
        else:
            cols["is_brand_asset"], cols["brand_role"] = 0, None
    elif "brand_role" in fields:
        cols["brand_role"] = fields["brand_role"] or None
    if "display_date_override" in fields:
        cols["display_date_override"] = _epoch("display_date", fields["display_date_override"])
    if "content_date" in fields:
        cols["content_date"] = _epoch("content_date", fields["content_date"])
    if "agent_notes" in fields:  # #206 agent-only scratch notes (MCP set_agent_notes); None clears
        cols["agent_notes"] = fields["agent_notes"]
    _plan_hand_edits(row, fields, cols)
    return cols


def _plan_hand_edits(row, fields, cols):
    """#787 / #542: a description or short title changed by hand (this update didn't also say who
    wrote it) is no longer a model's draft. A short title's recorded model is dropped (absent = by
    hand). A description that carried a caption record is re-recorded as written by whoever edited it
    now: "owner" (a person: the item page, a script) or "mcp-agent" (an MCP call), so set_caption
    won't replace it silently and core/item_title.py can tell the owner's words from a model's
    (description_is_handwritten)."""
    from . import actor as actor_ctx
    given = fields.get("type_metadata") if isinstance(fields.get("type_metadata"), dict) else {}
    current = json.loads(cols["type_metadata"]) if "type_metadata" in cols else dict(row.get("type_metadata") or {})
    changed = False
    if ("content_description" in cols and (cols["content_description"] or None) != (row.get("content_description") or None)
            and captions.DESCRIPTION_MODEL_KEY not in given
            and (current.get(captions.DESCRIPTION_MODEL_KEY) or current.get(captions.DESCRIPTION_USED_AT_KEY))):
        who = actor_ctx.current_actor()
        current[captions.DESCRIPTION_MODEL_KEY] = captions.AGENT_MODEL if who == actor_ctx.ACTOR_MCP else captions.OWNER_MODEL
        current[captions.DESCRIPTION_STEP_LABEL_KEY] = _model_label(current[captions.DESCRIPTION_MODEL_KEY])
        changed = True
    if ("short_title" in cols and cols["short_title"] != (row.get("short_title") or None)
            and captions.SHORT_TITLE_MODEL_KEY not in given and current.get(captions.SHORT_TITLE_MODEL_KEY)):
        current.pop(captions.SHORT_TITLE_MODEL_KEY, None)
        changed = True
    if changed:
        cols["type_metadata"] = json.dumps(current)


def _plan_text_sources(row, merged_meta, cols):
    """#724 (c): a type_metadata edit can change one of the type's text sources (a YouTube
    description corrected after OCR settled). Rebuild extracted_text from the sources plus the
    picture text already stored (no OCR re-run) and write it in the SAME imaged update, so search
    re-indexes and one undo restores the old text, text_source and lamp with the old description.
    Only settled rows (text_sources.resettle); a pending run combines the sources itself."""
    spec = object_types.get_object_type(row.get("media_type"))
    if spec is None or not spec.ocr_capable or spec.text_sources_fn is None or row.get("redacted"):
        return
    new_row = {**row, "type_metadata": merged_meta}
    change = text_sources.resettle(new_row, spec, object_types.ocr_unsupported_note(new_row, spec))
    if change is None:
        return
    if change["status_changed"]:
        cols["ocr_status"] = change["status"]
    cols["extracted_text"] = change["text"]
    cols["type_metadata"] = json.dumps(text_sources.resettled_meta(merged_meta, change))


def update(slug, *, tags=None, dry_run=False, actor=None, batch_id=None, **fields):
    """Edits any of UPDATE_FIELDS on one item as ONE change-log entry (so one Save is one undo).
    Every field is validated before anything is written. A no-op edit logs nothing.
    `tags` (#541 phase C): the free-text tag list, full replace, with its post_tags sync
    (core/tags.py `write_free_text`; tags it creates are imaged too). None = leave tags alone."""
    if tags is not None and (isinstance(tags, str) or not isinstance(tags, (list, tuple))):
        raise InvalidInput("tags must be a list of names", code="bad_tags")
    batch_id = batch_id or changes.new_batch_id()
    with db.transaction(dry_run=dry_run):
        row = get_item(slug, actor)
        cols = _plan_update(row, fields)
        with db.ImageLog(OP_UPDATE, actor, batch_id, [slug]) as log:
            if cols:
                log.update("capture_events", {"slug": slug}, cols)
            if "type_metadata" in cols:
                cards.refresh_writeup_lead(log, slug)  # #596: a write-up's body feeds its card's face
            if tags is not None:
                tags_svc.write_free_text(log, slug, [str(t) for t in tags])
            muts = list(log.muts)
        item = db.get_by_slug(slug)
    return _result(OP_UPDATE, muts, batch_id, dry_run, item, slug=slug)


DISPLAY_DATE_MIN = 0.0          # 1970-01-01
DISPLAY_DATE_MAX = 4102444800.0  # 2100-01-01


def set_display_date(slug, epoch, *, dry_run=False, actor=None, batch_id=None):
    """(#593) Sets (epoch = unix seconds) or clears (epoch None) the item's display-date override:
    the "date set by hand" the project timeline reads (core/timeline.py resolve_item_date). The
    drag on the project timeline calls this. It is `update(display_date_override=...)`, so ONE
    change-log row and one undo; a no-op (same value) logs nothing. The date must be a finite
    number between 1970 and 2100, else 400 `bad_date`. `res.data` carries `changed` and the
    resolved `effective_date` / `set_by_hand`."""
    if epoch is not None:
        value = _epoch("display_date", epoch)
        if value is None or value != value or not (DISPLAY_DATE_MIN <= value <= DISPLAY_DATE_MAX):
            raise InvalidInput("display_date must be a date between 1970 and 2100", code="bad_date")
        epoch = value
    res = update(slug, dry_run=dry_run, actor=actor, batch_id=batch_id, display_date_override=epoch)
    res.data["changed"] = bool(res.changes)
    res.data["effective_date"] = timeline.resolve_item_date(res.item)
    res.data["set_by_hand"] = res.item.get("display_date_override") is not None
    return res


CAPTION_MAX_CHARS = 2000


def _model_label(model):
    if model == captions.AGENT_MODEL:
        return "written by an agent (MCP)"
    if model == captions.OWNER_MODEL:
        return "written or edited by hand"
    if model == captions.WORDS_MODEL:
        return "cut from the item's own description"
    return f"suggested by {model}" if model else "written by hand"


def set_caption(slug, text=None, *, title=None, accept=False, replace=False, model=None, dry_run=False,
                actor=None, batch_id=None):
    """(#588, #787, #542) Writes a caption SUGGESTION (`text`, the long caption, into
    type_metadata.auto_caption) and/or a short-title suggestion (`title`, into
    type_metadata.auto_caption_title), the way the vision model's result lands (status "done"), so
    they show up in the caption review queue (/captions/review). `model` says who wrote them
    (default "mcp-agent"; the review page passes "owner"). With accept=True the caption also becomes
    the item's content_description and the title its short_title (the name, core/item_title.py),
    like "Accept" on the review page. ONE imaged batch through update(), so one undo reverts all of
    it; works with CAPTION_DISABLED, since no model runs.

    #787, never overwrite silently: replacing text a local captioner did NOT write (an agent's or the
    owner's suggestion, a hand-written or agent-accepted description or short title) is refused with
    409 `would_replace_suggestion`, details.existing holding each text that would go, unless
    `replace=True`. A local model's draft (moondream) is replaced freely. Every answer reports
    `replaced_suggestion` (the caption suggestion this call replaced, or None) and `replaced` (every
    text it replaced, by field). A new suggestion re-opens a previously skipped one."""
    text = (text or "").strip() or None
    if title is not None and not isinstance(title, str):
        raise InvalidInput("The title must be text.", code="bad_title")
    if title is not None and title.strip():
        clean = item_title.clean_short_title(title, limit=10 ** 6)
        if clean is None:
            raise InvalidInput("The short title is empty once quotes and spaces are taken off.", code="bad_title")
        if len(clean) > item_title.SHORT_TITLE_MAX:
            raise InvalidInput(f"The short title is {len(clean)} characters; the limit is {item_title.SHORT_TITLE_MAX}. "
                               "A short title is a few words; the long caption goes in `text`.", code="bad_title")
        title = clean
    else:
        title = None
    if text is None and title is None:
        raise InvalidInput("Give the caption text, a short title, or both.", code="bad_caption")
    if text is not None and len(text) > CAPTION_MAX_CHARS:
        raise InvalidInput(f"The caption is {len(text)} characters; the limit is {CAPTION_MAX_CHARS}.", code="bad_caption")
    row = get_item(slug, actor)
    if row.get("redacted"):
        raise InvalidInput("File was redacted: there's no image left to caption.", code="redacted")
    existing = row.get("type_metadata") or {}
    model = model or captions.AGENT_MODEL

    # What this call would replace, and which of those a local model did not write (#787).
    replaced, protected = {}, {}

    def consider(field, old, old_model, new, what):
        old = (old or "").strip()
        if not old or new is None or old == new:
            return
        replaced[field] = old
        if not captions.is_local_model(old_model):
            protected[field] = {"text": old, "by": old_model or "by hand", "what": what}

    old_sugg_model = existing.get("auto_caption_model")
    consider("suggestion", existing.get(captions.METADATA_KEY), old_sugg_model, text, "the caption suggestion")
    consider("title_suggestion", existing.get(captions.TITLE_KEY),
             existing.get(captions.TITLE_MODEL_KEY) or old_sugg_model, title, "the short-title suggestion")
    if accept:
        consider("description", row.get("content_description"), existing.get(captions.DESCRIPTION_MODEL_KEY),
                 text, "the item's description (its accepted caption)")
        consider("short_title", row.get("short_title"), existing.get(captions.SHORT_TITLE_MODEL_KEY),
                 title, "the item's short title (its name)")
    if protected and not replace:
        names = "; ".join(f"{v['what']} ({_model_label(None if v['by'] == 'by hand' else v['by'])}): {v['text'][:120]!r}"
                          for v in protected.values())
        raise Conflict(f"Nothing was changed: this would replace {names}. Read it first; to replace it anyway, "
                       "send replace=true.", code="would_replace_suggestion",
                       details={"existing": protected, "slug": slug})

    tm = {}
    if text is not None:
        same = (existing.get(captions.METADATA_KEY) or "").strip() == text
        tm.update({
            captions.METADATA_KEY: text,
            captions.STATUS_KEY: "done",
            "auto_caption_model": old_sugg_model if (same and old_sugg_model) else model,
            "auto_caption_dismissed": None,  # null = not dismissed (the review query tests IS NULL)
        })
        if "auto_caption_failed_reason" in existing:
            tm["auto_caption_failed_reason"] = None
    if title is not None:
        same_t = (existing.get(captions.TITLE_KEY) or "").strip() == title
        old_t_model = existing.get(captions.TITLE_MODEL_KEY)
        tm.update({captions.TITLE_KEY: title,
                   captions.TITLE_MODEL_KEY: old_t_model if (same_t and old_t_model) else model,
                   captions.TITLE_DISMISSED_KEY: None})
    fields = {"type_metadata": tm}
    if accept:
        if text is not None:
            text_model = tm["auto_caption_model"]
            local = captions.is_local_model(text_model)
            fields["content_description"] = text
            tm.update({
                captions.DESCRIPTION_STEP_KEY: existing.get(captions.STEP_KEY, 0) if local else None,
                captions.DESCRIPTION_STEP_LABEL_KEY: (captions.describe_step(existing.get(captions.STEP_KEY, 0))
                                                      if local else _model_label(text_model)),
                captions.DESCRIPTION_MODEL_KEY: text_model,
                captions.DESCRIPTION_USED_AT_KEY: time.time(),
            })
        if title is not None:
            fields["short_title"] = title
            tm[captions.SHORT_TITLE_MODEL_KEY] = tm[captions.TITLE_MODEL_KEY]
        elif (text is not None and not (row.get("short_title") or "").strip()
              and item_title.title_shaped(text, item_title.CAPTION_AS_TITLE_MAX)):
            # #542: an accepted caption short enough to read as a name, with no title given and none
            # on the item, is also its short title (the same rule the seed migration applied).
            fields["short_title"] = text
            tm[captions.SHORT_TITLE_MODEL_KEY] = tm["auto_caption_model"]
    res = update(slug, dry_run=dry_run, actor=actor, batch_id=batch_id, **fields)
    res.data.update(accepted=bool(accept), replaced_suggestion=replaced.get("suggestion"), replaced=replaced)
    return res


# --- short titles (#542) and mid-word captions (#743) -------------------------------------

OP_SHORT_TITLE_SEED = "item_short_title_seed"
OP_TITLE_PROPOSALS = "item_short_title_proposals"
OP_TITLE_PROPOSAL = "item_short_title_proposal"
OP_TITLE_WORDS = "item_short_title_proposal_words"
OP_REJECT_MIDWORD = "item_caption_midword_requeue"


def _is_caption_text(row):
    """True when the row's content_description is a model's caption (the rule lives in
    core/item_title.py: description_is_handwritten)."""
    return item_title.description_is_handwritten(row) is False


def seed_short_titles(*, dry_run=False, actor=None, batch_id=None):
    """(#542) The `short_title_seed_542` migration's write: every item with no short_title whose
    content_description already reads as a name gets it, verbatim, as its short_title (so its name
    doesn't change when the long caption leaves the title chain). A content's own title counts up to
    item_title.SHORT_TITLE_MAX characters, an accepted caption up to CAPTION_AS_TITLE_MAX; either on
    one line. ONE imaged row (op item_short_title_seed). data: {seeded: [slugs], left_long: n}."""
    rows = db.list_short_title_candidates()
    batch_id = batch_id or changes.new_batch_id()
    seeded, left_long = [], 0
    with db.transaction(dry_run=dry_run):
        with db.ImageLog(OP_SHORT_TITLE_SEED, actor, batch_id, [r["slug"] for r in rows]) as log:
            for row in rows:
                cd = row.get("content_description") or ""
                limit = item_title.CAPTION_AS_TITLE_MAX if _is_caption_text(row) else item_title.SHORT_TITLE_MAX
                if item_title.title_shaped(cd, limit):
                    log.update("capture_events", {"slug": row["slug"]}, {"short_title": cd.strip()})
                    seeded.append(row["slug"])
                else:
                    left_long += 1
            muts = list(log.muts)
    return _result(OP_SHORT_TITLE_SEED, muts, batch_id, dry_run, None, seeded=seeded, left_long=left_long)


def queue_title_proposals(slugs, *, dry_run=False, actor=None, batch_id=None):
    """(#542) Marks items as waiting for a short-title proposal from the local captioner
    (type_metadata.short_title_proposal_status = "queued"), as ONE imaged row; the proposals the
    caption worker then writes (record_title_proposal) join the SAME batch, so one undo takes the
    whole backfill back: every proposal written so far, and the marks, so the worker skips the rest.
    Nothing is applied: a proposal only waits in the caption review queue. Items must be visible to
    the actor (#709). data: {queued: [slugs]}."""
    from . import policy
    wanted = list(dict.fromkeys(s for s in (slugs or []) if s))
    if not wanted:
        raise InvalidInput("No items to propose short titles for.", code="no_items")
    rows, _missing = policy.viewable_rows(wanted, actor=actor)
    batch_id = batch_id or changes.new_batch_id()
    with db.transaction(dry_run=dry_run):
        with db.ImageLog(OP_TITLE_PROPOSALS, actor, batch_id, [r["slug"] for r in rows]) as log:
            for row in rows:
                meta = {**(row.get("type_metadata") or {}), captions.TITLE_STATUS_KEY: "queued"}
                meta.pop("short_title_proposal_note", None)
                log.update("capture_events", {"slug": row["slug"]}, {"type_metadata": json.dumps(meta)})
            muts = list(log.muts)
    return _result(OP_TITLE_PROPOSALS, muts, batch_id, dry_run, None, queued=[r["slug"] for r in rows])


def propose_titles_from_words(entries, *, dry_run=False, actor=None, batch_id=None):
    """(#542, owner decision 2026-10-25) Words-first proposals: writes short-title SUGGESTIONS cut
    from each item's own description (captions.title_from_words via the caller), no model, as ONE
    imaged row (op item_short_title_proposal_words). `entries`: [(slug, title)]. Nothing is applied:
    each waits in the caption review queue (author "words"); one undo removes them all. An item
    that got a name or a short title meanwhile is skipped. data: {proposed: [slugs], skipped: [...]}."""
    from . import policy
    entries = [(s_, t) for s_, t in (entries or []) if s_ and t]
    if not entries:
        raise InvalidInput("No short titles to propose.", code="no_items")
    rows, _missing = policy.viewable_rows([e[0] for e in entries], actor=actor)
    by_slug = {r["slug"]: r for r in rows}
    batch_id = batch_id or changes.new_batch_id()
    proposed, skipped = [], []
    with db.transaction(dry_run=dry_run):
        with db.ImageLog(OP_TITLE_WORDS, actor, batch_id, list(by_slug)) as log:
            for slug, title in entries:
                row = db.get_by_slug(slug) or by_slug.get(slug)
                if row is None or row.get("display_name") or row.get("short_title"):
                    skipped.append({"slug": slug, "reason": "has a name already"})
                    continue
                meta = {**(row.get("type_metadata") or {}), captions.TITLE_KEY: title,
                        captions.TITLE_MODEL_KEY: captions.WORDS_MODEL, captions.TITLE_DISMISSED_KEY: None,
                        captions.TITLE_STATUS_KEY: "done"}
                log.update("capture_events", {"slug": slug}, {"type_metadata": json.dumps(meta)})
                proposed.append(slug)
            muts = list(log.muts)
    return _result(OP_TITLE_WORDS, muts, batch_id, dry_run, None, proposed=proposed, skipped=skipped)


def record_title_proposal(slug, title, *, model, batch_id, note=None, actor=None):
    """(#542) The caption worker's answer to one queued proposal, imaged into the backfill's own
    batch: the suggested short title (or, when the model gave nothing usable, status "failed" and
    the reason in short_title_proposal_note). Never touches short_title itself."""
    row = get_item(slug, actor)
    meta = dict(row.get("type_metadata") or {})
    if title:
        meta.update({captions.TITLE_KEY: title, captions.TITLE_MODEL_KEY: model, captions.TITLE_DISMISSED_KEY: None,
                     captions.TITLE_STATUS_KEY: "done"})
        meta.pop("short_title_proposal_note", None)
    else:
        meta.update({captions.TITLE_STATUS_KEY: "failed", "short_title_proposal_note": note or "no usable title"})
    with db.transaction():
        with db.ImageLog(OP_TITLE_PROPOSAL, actor, batch_id, [slug]) as log:
            log.update("capture_events", {"slug": slug}, {"type_metadata": json.dumps(meta)})
            muts = list(log.muts)
    return _result(OP_TITLE_PROPOSAL, muts, batch_id, False, db.get_by_slug(slug), slug=slug)


def requeue_midword_captions(slugs, *, dry_run=False, actor=None, batch_id=None):
    """(#743) Puts `done` captions that start mid-word back in the needs-caption queue as ONE imaged
    row (op item_caption_midword_requeue), undoable: the fragment moves to
    type_metadata.auto_caption_rejected, the suggestion is cleared (so it leaves the review queue)
    and the status becomes "failed" with the reason, which is what needs_caption, the MCP's
    needs(view="caption") and Admin's "Caption skipped items" pick up. No model runs here: Admin's
    button (or an agent) re-captions them, and the caption ladder now treats a fragment as garbage.
    An item whose suggestion no longer starts mid-word, or is not `done`, is skipped. A
    content_description is never touched. data: {requeued: [slugs], skipped: [{slug, reason}]}."""
    from . import policy
    wanted = list(dict.fromkeys(s for s in (slugs or []) if s))
    if not wanted:
        raise InvalidInput("No captions to re-queue.", code="no_items")
    rows, _missing = policy.viewable_rows(wanted, actor=actor)
    batch_id = batch_id or changes.new_batch_id()
    requeued, skipped = [], []
    with db.transaction(dry_run=dry_run):
        with db.ImageLog(OP_REJECT_MIDWORD, actor, batch_id, [r["slug"] for r in rows]) as log:
            for row in rows:
                meta = dict(row.get("type_metadata") or {})
                cap = meta.get(captions.METADATA_KEY) or ""
                if meta.get(captions.STATUS_KEY) != "done" or not captions.starts_mid_word(cap):
                    skipped.append({"slug": row["slug"], "reason": "not a done mid-word caption"})
                    continue
                meta.update({"auto_caption_rejected": cap, captions.METADATA_KEY: None, captions.STATUS_KEY: "failed",
                             "auto_caption_failed_reason": "the caption started mid-word (#743), re-queued"})
                log.update("capture_events", {"slug": row["slug"]}, {"type_metadata": json.dumps(meta)})
                requeued.append(row["slug"])
            muts = list(log.muts)
    return _result(OP_REJECT_MIDWORD, muts, batch_id, dry_run, None, requeued=requeued, skipped=skipped)


# --- the sensitive flag (#603, #604 step 2) ------------------------------------------------

def _forbidden(message):
    return AppError("forbidden", message, status=403)


def set_sensitive(slugs, sensitive, *, dry_run=False, actor=None, batch_id=None):
    """Marks (sensitive=True) or clears the per-item "This is sensitive" flag on one or more items,
    as ONE imaged change-log row (op item_sensitive), undoable. A flagged item is then locked by the
    item policy on every door exactly like a restricted type: only admins and its uploader see it.
      * Marking: editor or above (locking is the safe direction). Stamps sensitive_by (the actor) and
        sensitive_at, which Admin's restricted list shows as the reason.
      * Clearing: admin only (403 `forbidden` for anyone else, checked before the items are even
        looked up, so the answer says nothing about whether they exist).
    Every slug must exist and be visible to the actor (else 404 not_found, nothing written). An item
    already in the wanted state is left alone. data: {slugs, changed, sensitive}."""
    from . import actor as actor_ctx, policy, roles  # lazy: policy is imported by db at call time
    wanted = [slugs] if isinstance(slugs, str) else list(slugs or [])
    wanted = list(dict.fromkeys(s for s in wanted if s))
    if not wanted:
        raise InvalidInput("Give at least one item.", code="no_items")
    want = bool(sensitive)
    who = actor_ctx.resolve(actor)
    if want and not roles.at_least(roles.role_of(who), roles.EDITOR):
        raise _forbidden("Marking an item sensitive needs an editor or an admin.")
    rows = []
    for slug in wanted:
        row = db.get_by_slug(slug)
        if row is None or not policy.can_view(row, who):
            raise NotFound(f"No item {slug!r}.")
        if not want and row.get("sensitive") and not policy.can_unmark_sensitive(who, row):
            raise _forbidden("Only the uploader or an admin can clear the sensitive flag.")
        rows.append(row)
    batch_id = batch_id or changes.new_batch_id()
    now = time.time()
    with db.transaction(dry_run=dry_run):
        with db.ImageLog(OP_SENSITIVE, actor, batch_id, wanted) as log:
            for row in rows:
                if bool(row.get("sensitive")) == want:
                    continue
                fields = ({"sensitive": 1, "sensitive_by": who, "sensitive_at": now} if want
                          else {"sensitive": 0, "sensitive_by": None, "sensitive_at": None})
                log.update("capture_events", {"slug": row["slug"]}, fields)
            muts = list(log.muts)
        item = db.get_by_slug(wanted[0]) if len(wanted) == 1 else None
    return _result(OP_SENSITIVE, muts, batch_id, dry_run, item, slugs=wanted,
                   changed=[m["key"]["slug"] for m in muts], sensitive=want)


def set_provenance(slugs, provenance, *, dry_run=False, actor=None, batch_id=None):
    """(#553) Sets the file provenance of several items as ONE imaged change-log row (op
    item_bulk_provenance), undoable with the generic undo: the file grids' bulk "Set provenance".
    `provenance` is validated per item exactly as update() validates it (an active key, or the
    item's own current retired key; "" / None clears). Every slug must exist and be visible to the
    actor (else 404 not_found, nothing written). An item already holding that value is left alone.
    data: {slugs, changed, provenance}."""
    from . import actor as actor_ctx, policy  # lazy: policy is imported by db at call time
    wanted = [slugs] if isinstance(slugs, str) else list(slugs or [])
    wanted = list(dict.fromkeys(s for s in wanted if s))
    if not wanted:
        raise InvalidInput("Give at least one item.", code="no_items")
    who = actor_ctx.resolve(actor)
    plan = []
    for slug in wanted:
        row = db.get_by_slug(slug)
        if row is None or not policy.can_view(row, who):
            raise NotFound(f"No item {slug!r}.")
        value = provenance_options.validate("file", provenance or None, current=row.get("provenance"))
        if value != row.get("provenance"):
            plan.append((slug, value))
    batch_id = batch_id or changes.new_batch_id()
    with db.transaction(dry_run=dry_run):
        with db.ImageLog(OP_BULK_PROVENANCE, actor, batch_id, wanted) as log:
            for slug, value in plan:
                log.update("capture_events", {"slug": slug}, {"provenance": value})
            muts = list(log.muts)
        item = db.get_by_slug(wanted[0]) if len(wanted) == 1 else None
    return _result(OP_BULK_PROVENANCE, muts, batch_id, dry_run, item, slugs=wanted,
                   changed=[m["key"]["slug"] for m in muts], provenance=provenance or None)


DATE_SOURCE_KEY = "content_date_source"  # #725: type_metadata key recording where a guessed date came from


def apply_dates(entries, *, dry_run=False, actor=None, batch_id=None):
    """(#725) Writes guessed dates as content_date, with where each came from in
    type_metadata.content_date_source, as ONE imaged change-log row (op item_date_guess) for the
    whole batch, so one undo takes every date back. `entries`: [(slug, epoch, source_record)]
    (core/item_dates.py source_record). Fill-only: an item that has a real date by now (hand-set,
    date made, content_date, a pre-install file mtime) is SKIPPED, never overwritten, and listed in
    `skipped` with the reason. Every item must exist and be visible to the actor (#709: else the
    whole request is refused, nothing written); every date must be plausible (1980 .. now, else 400
    `bad_date`). data: {applied: [slugs], skipped: [{slug, reason}]}."""
    from . import date_guess, policy  # lazy: policy is imported by db at call time
    entries = list(entries or [])
    if not entries:
        raise InvalidInput("Give at least one item to date.", code="no_items")
    for slug, epoch, _record in entries:
        if date_guess.plausible(epoch) is None:
            raise InvalidInput(f"Nothing was changed: {epoch!r} is not a believable date for {slug!r} "
                               "(it must be between 1980 and now).", code="bad_date")
    rows, _missing = policy.viewable_rows([e[0] for e in entries], actor=actor)
    by_slug = {r["slug"]: r for r in rows}
    batch_id = batch_id or changes.new_batch_id()
    applied, skipped = [], []
    with db.transaction(dry_run=dry_run):
        with db.ImageLog(OP_DATE_GUESS, actor, batch_id, list(by_slug)) as log:
            for slug, epoch, record in entries:
                row = db.get_by_slug(slug) or by_slug[slug]
                if timeline.resolve_item_date(row) is not None:
                    skipped.append({"slug": slug, "reason": "already_dated"})
                    continue
                meta = {**(row.get("type_metadata") or {}), DATE_SOURCE_KEY: dict(record or {})}
                log.update("capture_events", {"slug": slug},
                           {"content_date": float(epoch), "type_metadata": json.dumps(meta)})
                applied.append(slug)
            muts = list(log.muts)
        item = db.get_by_slug(entries[0][0]) if len(entries) == 1 else None
    return _result(OP_DATE_GUESS, muts, batch_id, dry_run, item, applied=applied, skipped=skipped)


def accept_date_guess(slug, source, *, dry_run=False, actor=None, batch_id=None):
    """(#725) The Curator's one-click "Use this date" / MCP accept: re-computes the item's guesses
    (reading the file's own embedded date too), takes the one from `source` (core/date_guess.py
    SOURCES) and writes it through apply_dates. Refuses, writing nothing: an item that already has
    a real date (409 `already_dated`), a source with no guess for this item (400 `no_such_guess`,
    the available sources in details), an unknown source (400 `bad_source`)."""
    from . import date_guess, item_dates  # lazy: item_dates reads db / policy
    if source not in date_guess.SOURCES:
        raise InvalidInput(f"Unknown date source {source!r}: use one of {', '.join(date_guess.SOURCES)}.",
                           code="bad_source")
    row = get_item(slug, actor)
    real = timeline.resolve_item_date(row)
    if real is not None:
        raise Conflict(f"Nothing was changed: {item_title.title_of(row)} already has a real date "
                       f"({_day(real)}). Change it in the item page's DATES group.", code="already_dated")
    found = item_dates.guesses_for(row, read_file=True)
    guess = next((g for g in found if g.source == source), None)
    if guess is None:
        have = [g.source for g in found]
        raise InvalidInput(f"Nothing was changed: there is no {date_guess.SOURCE_LABELS[source]} guess for "
                           f"{item_title.title_of(row)}" + (f" (it has: {', '.join(have)})." if have else
                                                            " (it has no guesses at all; set the date by hand)."),
                           code="no_such_guess", details={"available": have})
    record = item_dates.source_record(guess, accepted_by="by_hand")
    res = apply_dates([(slug, guess.epoch, record)], dry_run=dry_run, actor=actor, batch_id=batch_id)
    if res.data["skipped"]:  # it got a real date between the check above and the write
        raise Conflict(f"Nothing was changed: {item_title.title_of(row)} got a real date in the meantime.",
                       code="already_dated")
    res.data["guess"] = guess.as_dict()
    return res


def _day(epoch):
    from . import datefmt  # lazy, keeps this module's import list as it was
    return datefmt.day(epoch)


def check_undo_allowed(rows, actor=None):
    """Called by the generic undo (cards.undo) before it writes anything: undoing a change that SET
    the sensitive flag would clear it, which is an unmark, so it is admin-only too (403 forbidden).
    Undoing an unmark (re-locking) is open to anyone who may undo."""
    from . import policy  # lazy
    for r in rows:
        for m in r.get("mutations") or []:
            before, after = m.get("before"), m.get("after")
            if m.get("table") != "capture_events" or before is None or after is None:
                continue
            if after.get("sensitive") and "sensitive" in before and not before.get("sensitive"):
                if not policy.can_unmark_sensitive(actor, after):
                    raise _forbidden("Undoing this would clear an item's sensitive flag, which only its "
                                     "uploader or an admin can do.")


# --- related items (#16) -----------------------------------------------------------------

def relate(slug, other, *, dry_run=False, actor=None, batch_id=None):
    """Links two items as related (stored both directions) and, as #16 always did, shares their
    categorization both ways: each picks up the other's tags (post_tags) and card memberships
    (plain project_items rows, no linked-tag / cover side effects). It runs even when the pair
    was already related, as before. Every row is imaged: one undo removes the link AND what it
    shared. Relating an item to itself is a no-op."""
    batch_id = batch_id or changes.new_batch_id()
    with db.transaction(dry_run=dry_run):
        get_item(slug, actor)
        get_item(other, actor)
        with db.ImageLog(OP_RELATE, actor, batch_id, [slug, other]) as log:
            if slug != other:
                now = time.time()
                for a, b in ((slug, other), (other, slug)):
                    if log.get("capture_event_relations", {"slug_a": a, "slug_b": b}) is None:
                        log.insert("capture_event_relations", {"slug_a": a, "slug_b": b}, {"created_at": now})
                tags_a, tags_b = db.tag_ids_for_posts([slug]), db.tag_ids_for_posts([other])
                for tid in sorted(tags_b - tags_a):
                    tags_svc.link(log, slug, tid)
                for tid in sorted(tags_a - tags_b):
                    tags_svc.link(log, other, tid)
                cards_a = {p["id"] for p in db.list_projects_for_post(slug)}
                cards_b = {p["id"] for p in db.list_projects_for_post(other)}
                for pid in sorted(cards_b - cards_a):
                    membership.write_items(log, pid, [slug])
                for pid in sorted(cards_a - cards_b):
                    membership.write_items(log, pid, [other])
            muts = list(log.muts)
    return _result(OP_RELATE, muts, batch_id, dry_run, None, slug=slug, related=other)


def unrelate(slug, other, *, dry_run=False, actor=None, batch_id=None):
    """Removes the related link (both directions). Like before, the tags and cards the link
    shared stay (only undoing the relate takes those back). Unknown slugs stay a no-op; an item the
    actor may not see refuses with #709's 403 forbidden."""
    from . import policy  # lazy: policy is imported by db at call time
    batch_id = batch_id or changes.new_batch_id()
    with db.transaction(dry_run=dry_run):
        policy.viewable_rows([slug, other], missing_ok=True, actor=actor)
        with db.ImageLog(OP_UNRELATE, actor, batch_id, [slug, other]) as log:
            for a, b in ((slug, other), (other, slug)):
                log.delete("capture_event_relations", {"slug_a": a, "slug_b": b})
            muts = list(log.muts)
    return _result(OP_UNRELATE, muts, batch_id, dry_run, None, slug=slug, related=other)


# --- trash: files ----------------------------------------------------------------------

def trash_dir(batch_id=None):
    return paths.trash_dir(batch_id)


def _file_pairs(entry):
    """[(path in storage, path in trash)] for the files an entry holds."""
    d = trash_dir(entry["batch_id"])
    pairs = []
    if entry.get("has_original") and entry.get("stored_filename"):
        pairs.append((paths.storage_dir() / entry["stored_filename"], d / entry["stored_filename"]))
    if entry.get("has_thumb"):
        pairs.append((storage.thumb_path_for(entry["slug"]), d / f"{entry['slug']}_thumb.jpg"))
    return pairs


def _move(src, dst, moved):
    dst.parent.mkdir(parents=True, exist_ok=True)
    os.replace(src, dst)
    moved.append((src, dst))


def rollback_moves(moved):
    """Puts files back after a failed transaction (newest move first). Best effort, logged."""
    for src, dst in reversed(moved):
        try:
            if dst.exists() and not src.exists():
                src.parent.mkdir(parents=True, exist_ok=True)
                os.replace(dst, src)
        except OSError as e:
            print(f"items: could not move {dst} back to {src}: {e!r}", flush=True)
    moved.clear()


def _rmdir_empty(batch_id):
    try:
        trash_dir(batch_id).rmdir()
    except OSError:
        pass  # silent-ok: not empty (another item of the batch) or already gone


def _trash_insert(log, batch_id, row, reason, embedding=None, with_thumb=True):
    """Images a trash row for `row`'s files (original and/or thumbnail, whichever exist on disk).
    Returns the entry to move, or None when the item has no files at all. `with_thumb=False` leaves
    the thumbnail out (a replaced file's thumbnail is derived data: rebuilt, never held)."""
    slug, sf = row["slug"], row.get("stored_filename")
    orig = paths.storage_dir() / sf if sf else None
    has_orig = bool(orig is not None and orig.is_file())
    thumb = storage.thumb_path_for(slug)
    has_thumb = with_thumb and thumb.is_file()
    if not (has_orig or has_thumb):
        return None
    now = time.time()
    hold = reason == REASON_REDACT  # a redact hold has no expiry
    entry = {"batch_id": batch_id, "slug": slug, "stored_filename": sf, "has_original": int(has_orig),
             "has_thumb": int(has_thumb)}
    log.insert("trash", {"batch_id": batch_id, "slug": slug}, {
        "stored_filename": sf, "has_original": int(has_orig), "has_thumb": int(has_thumb),
        "dir": f"{TRASH_DIR_NAME}/{batch_id}",
        "size_bytes": (orig.stat().st_size if has_orig else 0) + (thumb.stat().st_size if has_thumb else 0),
        "title": item_title.title_of(row), "reason": reason, "created_at": now, "expires_at": None if hold else now + TRASH_TTL_SECONDS,
        "purged_at": None, "embedding": embedding,
    })
    return entry


# --- hide / unhide -----------------------------------------------------------------------

def redact(slug, *, dry_run=False, actor=None, batch_id=None):
    """Remove the file, keep the info: the file (and thumbnail) move to the trash as a hold with
    no expiry, the row is flagged redacted with no stored_filename. The owner then recovers it
    (recover_redacted, or undoing this batch) or deletes the file permanently."""
    batch_id = batch_id or changes.new_batch_id()
    moved = []
    try:
        with db.transaction(dry_run=dry_run):
            row = get_item(slug, actor)
            if not row.get("stored_filename"):
                raise InvalidInput("This row has no uploaded file to redact", code="no_file")
            with db.ImageLog(OP_REDACT, actor, batch_id, [slug]) as log:
                log.update("capture_events", {"slug": slug}, {"redacted": 1, "stored_filename": None})
                entry = _trash_insert(log, batch_id, row, REASON_REDACT)
                muts = list(log.muts)
            if entry and not dry_run:
                for src, dst in _file_pairs(entry):
                    _move(src, dst, moved)
            item = db.get_by_slug(slug)
    except BaseException:
        rollback_moves(moved)
        raise
    return _result(OP_REDACT, muts, batch_id, dry_run, item, slug=slug, trashed=bool(entry),
                   expires_at=_expiry(muts))


def unredact(slug, *, dry_run=False, actor=None, batch_id=None):
    """Clears the redacted flag (the row rejoins browsing). Visibility only, for old redactions
    whose file is already gone. While a hold exists it refuses: recover the file (which also
    un-redacts) or delete it permanently first, so a held file is never orphaned."""
    batch_id = batch_id or changes.new_batch_id()
    with db.transaction(dry_run=dry_run):
        row = get_item(slug, actor)
        if not row["redacted"]:
            raise Conflict("This row isn't redacted", code="not_redacted")
        if db.get_redact_hold(slug):
            raise Conflict("The redacted file is still stored on the NAS. Recover it (which un-redacts the "
                           "item), or delete the file permanently first.", code="redact_hold_exists")
        with db.ImageLog(OP_UNREDACT, actor, batch_id, [slug]) as log:
            log.update("capture_events", {"slug": slug}, {"redacted": 0})
            muts = list(log.muts)
        item = db.get_by_slug(slug)
    return _result(OP_UNREDACT, muts, batch_id, dry_run, item, slug=slug)


def recover_redacted(slug, *, dry_run=False, actor=None, batch_id=None):
    """Brings a held redacted file back: the item is exactly as before the redact (file,
    stored_filename, visible). Its own undoable batch (undo re-holds the file)."""
    batch_id = batch_id or changes.new_batch_id()
    moved, t = [], None
    try:
        with db.transaction(dry_run=dry_run):
            row = get_item(slug, actor)
            t = db.get_redact_hold(slug)
            if t is None:
                raise _no_hold(slug, row)
            pairs = _file_pairs(t)
            if not all(dst.is_file() for _src, dst in pairs):
                raise AppError("hold_file_missing", f"The held file for {slug} is missing from the trash, so it "
                               "can't be recovered. A ZFS snapshot is the way back.", status=410)
            busy = [src.name for src, _dst in pairs if src.exists()]
            if busy:
                raise AppError("trash_conflict", f"Can't recover {slug}: {', '.join(busy)} already exists in "
                               "storage. Nothing was changed.", status=409, details={"files": busy})
            with db.ImageLog(OP_RECOVER, actor, batch_id, [slug]) as log:
                log.update("capture_events", {"slug": slug}, {"redacted": 0, "stored_filename": t["stored_filename"]})
                log.delete("trash", {"batch_id": t["batch_id"], "slug": slug})
                muts = list(log.muts)
            if not dry_run:
                for src, dst in pairs:
                    src.parent.mkdir(parents=True, exist_ok=True)
                    os.replace(dst, src)
                    moved.append((dst, src))
            item = db.get_by_slug(slug)
    except BaseException:
        rollback_moves(moved)
        raise
    if not dry_run:
        _rmdir_empty(t["batch_id"])
        if not t.get("has_thumb") and item is not None:
            ingest.run_in_thread(thumbnails.ensure_thumbnail, item)
    return _result(OP_RECOVER, muts, batch_id, dry_run, item, slug=slug)


def delete_redacted_file(slug, confirm=False, *, dry_run=False, actor=None, batch_id=None):
    """Permanently erases a held redacted file. The item stays redacted with metadata only
    (stored_filename NULL), exactly like the old behaviour. Not undoable. Requires confirm=True."""
    if confirm is not True and str(confirm).strip().lower() not in ("true", "1", "yes", "on"):
        raise InvalidInput("Pass confirm=true to permanently delete the redacted file", code="confirm_required")
    row = get_item(slug, actor)
    t = db.get_redact_hold(slug)
    if t is None:
        raise _no_hold(slug, row)
    batch_id = batch_id or changes.new_batch_id()
    if dry_run:
        return Result(True, [], [], batch_id, True, {"slug": slug, "bytes": t["size_bytes"]})
    for _src, dst in _file_pairs(t):
        dst.unlink(missing_ok=True)
    _rmdir_empty(t["batch_id"])
    db._mark_trash_purged(t["batch_id"], slug, time.time())
    # A record, not an undoable change (no row images): the file is gone for good, like a purge.
    changes.record(OP_ERASE, actor, [], batch_id=batch_id, affected_slugs=[slug])
    return Result(True, [], [], batch_id, False, {"slug": slug, "bytes": t["size_bytes"], "permanent": True})


def _no_hold(slug, row):
    """The refusal when `slug` has no live hold."""
    if row.get("redacted"):
        return Conflict(f"There is no stored file to recover for {slug}: it was permanently deleted (or "
                        "redacted before files were held). Only the metadata remains.", code="no_redact_hold")
    return Conflict(f"{slug} isn't redacted, so there is no held file.", code="not_redacted")


def _expiry(muts):
    for m in muts:
        if m["table"] == "trash" and m.get("after"):
            return m["after"].get("expires_at")
    return None


# --- retype ----------------------------------------------------------------------------

def _reprocess(slug, run_background):
    """Post-processing after a media_type change (OCR, thumbnail, captions, embedded metadata).
    Machine work, not imaged (see the module docstring)."""
    row = db.get_by_slug(slug)
    if row is None:
        return
    spec = object_types.get_object_type(row.get("media_type"))
    if spec.ocr_capable:
        db.set_ocr_status(slug, "pending")
    storage.thumb_path_for(slug).unlink(missing_ok=True)
    parse_limits.forget(thumbnails.limit_key(row))  # #644: a new type re-reads the file from scratch
    embedded_metadata.fill_missing(slug)
    ingest.post_insert(slug, spec, run_background)


def retype(slug, media_type, run_background=None, *, dry_run=False, actor=None, batch_id=None):
    """#448: change an item's media_type to another registered type. The row change is imaged
    (undo restores the old type, then re-runs post-processing for it); post-processing runs
    through `run_background` (FastAPI BackgroundTasks.add_task, or ingest.run_in_thread)."""
    batch_id = batch_id or changes.new_batch_id()
    if media_type not in object_types.OBJECT_TYPES:
        raise InvalidInput(f"Unknown media_type: {media_type}", code="unknown_media_type")
    with db.transaction(dry_run=dry_run):
        get_item(slug, actor)
        with db.ImageLog(OP_RETYPE, actor, batch_id, [slug]) as log:
            log.update("capture_events", {"slug": slug}, {"media_type": media_type})
            muts = list(log.muts)
    if not dry_run:
        _reprocess(slug, run_background or ingest.run_in_thread)
    return _result(OP_RETYPE, muts, batch_id, dry_run, db.get_by_slug(slug), slug=slug)


# --- replace the file, keep the item (#617) --------------------------------------------

def _refuse(code, message, status=400, **details):
    return AppError(code, message, status=status, details=details or None)


def _final_filename(row, filename):
    """The filename the item keeps after a replace. The item's own name stays (so its title and
    every link to it stay), except that the extension must tell the truth about the new bytes: when
    the new file's extension differs, the stem is kept and the extension swapped."""
    current = row.get("filename") or ""
    if not filename:
        return current or row.get("stored_filename") or ""
    new_ext = object_types.file_extension(filename)
    cur_ext = object_types.file_extension(current) if current else ""
    if current and new_ext == cur_ext:
        return current
    stem = current[: len(current) - len(cur_ext)] if current else Path(filename).stem
    return f"{stem}{new_ext}"


def _same_bytes(path, content):
    try:
        return path.is_file() and path.stat().st_size == len(content) and path.read_bytes() == content
    except OSError as e:
        print(f"items: could not compare {path} with the replacement bytes: {e!r}", flush=True)
        return False


def _reprocess_file(slug, run_background):
    """Everything derived from the file's bytes, re-derived through the ingest path: the old text,
    perceptual hash, embedding and thumbnail are dropped, then embedded metadata (fill-only), the
    thumbnail (synchronously for a type whose thumbnail is the file), OCR / text extraction and
    captions run exactly as after an upload. Machine work, not imaged (see the module docstring).
    The search index follows by trigger (extracted_text / filename)."""
    row = db.get_by_slug(slug)
    if row is None:
        return
    spec = object_types.get_object_type(row.get("media_type"))
    db.set_extracted_text(slug, "")
    db.set_perceptual_hash(slug, None)
    db.set_embedding(slug, None)
    db.set_ocr_status(slug, "pending" if spec.ocr_capable else None)
    storage.thumb_path_for(slug).unlink(missing_ok=True)
    parse_limits.forget(thumbnails.limit_key(row))  # #644: new bytes, so old limit hits don't apply
    if spec.thumbnail_source == object_types.ThumbnailSource.UPLOADED_FILE:
        thumbnails.ensure_thumbnail(db.get_by_slug(slug))
    embedded_metadata.fill_missing(slug)
    ingest.post_insert(slug, spec, run_background)


def replace_file(slug, content, filename=None, run_background=None, *, dry_run=False, actor=None, batch_id=None):
    """#617: swaps an item's file bytes and keeps everything else: slug (so /f/<slug> and every link
    stay), cards, tags, title, captions, relations and revision chain.

    The displaced file goes to the trash in the SAME batch as the row change (reason "replace",
    kept TRASH_DAYS like a delete), so `cards.undo(batch_id)` / constructicon_undo puts the old
    bytes and the old size/name back. The new bytes live under a fresh stored name, so the two files
    never collide; after an undo the replacement stays in storage (so a redo works).

    Rules, each its own error code:
      forbidden             403, below editor (checked first, says nothing about the item)
      not_found             404, no such item, or one the actor may not see (core/policy.py)
      replace_redacted      409, a redacted item has no file to replace (recover it first)
      no_file               400, a content-only item (YouTube, URL ...) has no uploaded file
      empty_file            400, zero bytes
      file_too_large        413, over the upload cap (storage.MAX_MB)
      unsupported_type / content_mismatch   400, the same checks an upload gets
      replace_type_mismatch 422, the bytes are another TYPE than the item (a .md replaced with a
                            .pdf): the type drives the item's metadata, icon and processing, so a
                            type change is an upload + constructicon_revision(action="supersede"), not a replace
      replace_needs_decision / replace_rejected   422, the type's pre-store hook would not just accept
      file_unchanged        409, the bytes are identical to the current file
    `filename` is the new file's name: it decides the type check; the item keeps its own name unless
    the extension differs (see _final_filename). Post-processing runs through `run_background`
    (default ingest.run_in_thread). Returns a Result; data: slug, filename, previous_stored_filename,
    previous_size, new_size, trashed, expires_at, trash_days."""
    from . import actor as actor_ctx, policy, roles  # lazy: policy is imported by db at call time
    who = actor_ctx.resolve(actor)
    if not roles.at_least(roles.role_of(who), roles.EDITOR):
        raise _forbidden("Replacing a file needs an editor or an admin.")
    row = db.get_by_slug(slug) if slug else None
    if row is None or not policy.can_view(row, who):
        raise NotFound(f"No item {slug!r}.")
    if row.get("redacted"):
        raise _refuse("replace_redacted", f"{slug} is redacted: its file was removed on purpose, so it can't be "
                      "replaced. Recover the held file first (constructicon_redact(slug, action='recover')).", 409, slug=slug)
    if not row.get("stored_filename"):
        raise _refuse("no_file", f"{slug} has no uploaded file to replace (it is a {row.get('media_type')} link or "
                      "content-only item).", 400, slug=slug, media_type=row.get("media_type"))
    if not isinstance(content, (bytes, bytearray)) or len(content) == 0:
        raise _refuse("empty_file", "The replacement file is empty (0 bytes). Nothing was changed.", 400)
    if len(content) > storage.MAX_BYTES:
        raise _refuse("file_too_large", f"The replacement is {len(content) / 1048576:.1f} MB, over the "
                      f"{storage.MAX_MB} MB upload limit. Nothing was changed.", 413,
                      size_bytes=len(content), max_bytes=storage.MAX_BYTES)
    content = bytes(content)
    if _same_bytes(storage.path_for(row["stored_filename"]), content):
        raise _refuse("file_unchanged", f"These bytes are identical to {slug}'s current file; nothing to replace.",
                      409, slug=slug)

    name_for_type = filename or row.get("filename") or row["stored_filename"]
    if object_types.detect_media_type(name_for_type) is None:
        raise _refuse("unsupported_type", f"Unsupported file type: {object_types.file_extension(name_for_type) or name_for_type!r}.",
                      400, filename=name_for_type)
    _unused_slug, new_stored, _n = storage.save_stream(name_for_type, io.BytesIO(content))
    new_path = storage.path_for(new_stored)
    batch_id = batch_id or changes.new_batch_id()
    moved, committed = [], False
    try:
        sniffed = object_types.detect_media_type(name_for_type, new_path)
        if sniffed is None:
            raise _refuse("content_mismatch", object_types.mismatch_reason(name_for_type), 400, filename=name_for_type)
        if sniffed != row.get("media_type"):
            raise _refuse("replace_type_mismatch",
                          f"That file is a {sniffed!r}, but {slug} is a {row.get('media_type')!r}. Replacing keeps the "
                          "item's type; to swap in a different kind of file, upload it as a new item and use "
                          "constructicon_revision(old, action=\"supersede\", new=...). Nothing was changed.", 422,
                          slug=slug, current_type=row.get("media_type"), new_type=sniffed, filename=name_for_type)
        spec = object_types.get_object_type(sniffed)
        decision = ingest._apply_pre_store(spec, object_types.IngestCandidate(
            filename=name_for_type, path=new_path, media_type=sniffed, source=who))
        if decision.action == "reject":
            raise _refuse("replace_rejected", decision.reason or f"The {spec.label} type refused this file.", 422)
        if decision.action != "accept":
            raise _refuse("replace_needs_decision", f"The {spec.label} type would ask the owner a question about this "
                          "file (as on upload), which a replace can't do. Upload it as a new item instead.", 422,
                          media_type=sniffed)
        final_name = _final_filename(row, filename)
        with db.transaction(dry_run=dry_run):
            fresh = get_item(slug, actor)  # re-read under the write lock
            if fresh.get("redacted") or fresh.get("stored_filename") != row["stored_filename"]:
                raise _refuse("conflict_changed", f"{slug} changed while its file was being replaced (redacted, or "
                              "replaced by someone else). Nothing was changed; try again.", 409)
            with db.ImageLog(OP_REPLACE_FILE, actor, batch_id, [slug]) as log:
                log.update("capture_events", {"slug": slug}, {
                    "stored_filename": new_stored, "filename": final_name, "file_size": len(content),
                    "source_modified_at": None})
                entry = _trash_insert(log, batch_id, fresh, REASON_REPLACE, with_thumb=False)
                muts = list(log.muts)
            if entry and not dry_run:
                for src, dst in _file_pairs(entry):
                    _move(src, dst, moved)
        committed = True
    except BaseException:
        rollback_moves(moved)
        raise
    finally:
        if dry_run or not committed:
            new_path.unlink(missing_ok=True)
    if not dry_run:
        _reprocess_file(slug, run_background or ingest.run_in_thread)
    item = db.get_by_slug(slug)
    return _result(OP_REPLACE_FILE, muts, batch_id, dry_run, item, slug=slug, filename=final_name,
                   previous_stored_filename=row["stored_filename"], previous_size=row.get("file_size"),
                   new_size=len(content), trashed=bool(entry), expires_at=_expiry(muts), trash_days=TRASH_DAYS)


# --- delete ----------------------------------------------------------------------------

def _delete_one(row, batch_id, actor):
    """Deletes one item and every row that points at it, imaging all of it. Returns
    (trash entry or None, mutations)."""
    slug = row["slug"]
    refs = db.item_references(slug)
    embedding = db.get_embedding(slug)
    with db.ImageLog(OP_DELETE, actor, batch_id, [slug]) as log:
        for a, b in refs["relations"]:
            if log.delete("capture_event_relations", {"slug_a": a, "slug_b": b}):
                log.slugs.append(b if a == slug else a)
        # #477: take it out of its revision chain, closing the gap (A -> B -> C minus B = A -> C).
        succ, pred = refs["successor"], refs["predecessor"]
        if succ is not None:
            log.delete("item_revisions", {"old_slug": slug})  # first: new_slug is UNIQUE
            log.slugs.append(succ)
        if pred is not None:
            if succ is not None:
                log.update("item_revisions", {"old_slug": pred}, {"new_slug": succ})
            else:
                log.delete("item_revisions", {"old_slug": pred})
            log.slugs.append(pred)
        for pid in refs["project_items"]:
            log.delete("project_items", {"project_id": pid, "post_slug": slug})
        for tid in refs["post_tags"]:
            log.delete("post_tags", {"post_slug": slug, "tag_id": tid})
        for eid in refs["blog_entry_items"]:
            log.delete("blog_entry_items", {"entry_id": eid, "post_slug": slug})
        for did in refs["decisions"]:
            log.delete("curator_dismissals", {"nudge_key": f"decision:{did}"})  # its deferred/dismissed state
            log.delete("pending_decisions", {"id": did})
        log.delete("capture_events", {"slug": slug})
        cards.refresh_writeup_lead(log, slug)  # #596: a deleted write-up leaves no lead on its card's face
        entry = _trash_insert(log, batch_id, row, "delete", embedding)
        muts = list(log.muts)
    return entry, muts


def delete(slugs, *, missing_ok=False, dry_run=False, actor=None, batch_id=None):
    """Deletes items: rows (with everything that references them) inside one transaction, files
    into the trash for TRASH_DAYS. One batch for the whole call, so one undo restores them all.
    Unknown slugs raise not_found before anything is written, unless `missing_ok` (bulk paths
    skip them, as before). #709: an item the actor may not see (policy.viewable_rows) refuses the
    WHOLE call (403 forbidden, saying how many and why), so nothing is deleted. Returns data
    {deleted, slugs, trashed, expires_at}."""
    from . import policy  # lazy: policy is imported by db at call time
    batch_id = batch_id or changes.new_batch_id()
    moved, all_muts, done, trashed = [], [], [], 0
    try:
        with db.transaction(dry_run=dry_run):
            rows, _ = policy.viewable_rows(slugs, missing_ok=missing_ok, actor=actor)
            entries = []
            for row in rows:
                entry, muts = _delete_one(row, batch_id, actor)
                all_muts += muts
                done.append(row["slug"])
                if entry:
                    entries.append(entry)
            trashed = len(entries)
            if not dry_run:
                for entry in entries:
                    for src, dst in _file_pairs(entry):
                        _move(src, dst, moved)
    except BaseException:
        rollback_moves(moved)
        raise
    return _result(OP_DELETE, all_muts, batch_id, dry_run, None, deleted=len(done), slugs=done,
                   trashed=trashed, expires_at=_expiry(all_muts), trash_days=TRASH_DAYS)


# --- undo hooks (called by cards.undo) -------------------------------------------------

def undo_prepare(rows):
    """Plans the file side of undoing change-log `rows`, in undo order. A trash row being removed
    means "restore its files"; one being re-created (undoing an undo) means "trash them again".
    Refuses BEFORE anything is written: trash_expired when a file was already purged,
    trash_conflict when a restore target is occupied."""
    plan = []
    for r in sorted(rows, key=lambda x: x["id"], reverse=True):
        for m in reversed(r["mutations"]):
            if m["table"] != "trash":
                continue
            key = m["key"]
            if m.get("before") is None and m.get("after") is not None:
                t = db.get_trash_row(key["batch_id"], key["slug"])
                pairs = _file_pairs(t) if t else []
                if t is None or t.get("purged_at") is not None or not all(dst.is_file() for _src, dst in pairs):
                    raise AppError("trash_expired",
                                   f"The file for {key['slug']} is no longer in the trash (deleted items are kept "
                                   f"{TRASH_DAYS} days), so this can't be undone. Nothing was changed; a ZFS "
                                   "snapshot is the way back now.", status=410,
                                   details={"slug": key["slug"], "batch_id": key["batch_id"],
                                            "purged_at": t.get("purged_at") if t else None})
                busy = [str(src.name) for src, _dst in pairs if src.exists()]
                if busy:
                    raise AppError("trash_conflict", f"Can't restore {key['slug']}: {', '.join(busy)} already "
                                   "exists in storage. Nothing was changed.", status=409,
                                   details={"slug": key["slug"], "files": busy})
                plan.append(("restore", t))
            elif m.get("before") is not None and m.get("after") is None:
                entry = dict(m["before"])
                plan.append(("retrash", {**entry, "embedding": db.get_embedding(key["slug"])}))
    return plan


def undo_apply(plan, moved):
    """Runs inside the undo's transaction, after the row images were inverted."""
    for kind, t in plan:
        if kind == "restore":
            for src, dst in _file_pairs(t):
                src.parent.mkdir(parents=True, exist_ok=True)
                os.replace(dst, src)
                moved.append((dst, src))
            if t.get("embedding") is not None and db.get_by_slug(t["slug"]) is not None:
                db.set_embedding(t["slug"], t["embedding"])
        else:
            for src, dst in _file_pairs(t):
                if src.is_file():
                    _move(src, dst, moved)
            if t.get("embedding") is not None:
                db._set_trash_embedding(t["batch_id"], t["slug"], t["embedding"])


def after_undo(rows, plan):
    """After the undo committed: tidy empty trash dirs, rebuild missing thumbnails of restored
    items, and re-run post-processing for an undone retype (the old type's OCR/thumbnail)."""
    for kind, t in plan:
        _rmdir_empty(t["batch_id"])
    for kind, t in plan:
        if kind == "restore" and not t.get("has_thumb"):
            row = db.get_by_slug(t["slug"])
            if row is not None:
                ingest.run_in_thread(thumbnails.ensure_thumbnail, row)
    for r in rows:
        if r.get("op") == OP_RETYPE:
            for slug in r.get("affected_slugs") or []:
                _reprocess(slug, ingest.run_in_thread)
    for slug in _swapped_files(rows):  # #617: the bytes changed (a replace undone, or such an undo undone)
        _reprocess_file(slug, ingest.run_in_thread)


def _swapped_files(rows):
    """Slugs whose stored file changed in these change-log rows (a replace_file, its undo, or the
    undo of that undo): their derived data (text, thumbnail, hash) belongs to the other file."""
    out = []
    for r in rows:
        for m in r.get("mutations") or []:
            b, a = m.get("before"), m.get("after")
            if (m.get("table") == "capture_events" and b and a and "stored_filename" in b and "stored_filename" in a
                    and b["stored_filename"] != a["stored_filename"] and m["key"]["slug"] not in out):
                out.append(m["key"]["slug"])
    return out


def needs_after_undo(rows):
    """True when undoing `rows` needs the post-commit tidy-up even with no trash files to move."""
    return any(r.get("op") == OP_RETYPE for r in rows) or bool(_swapped_files(rows))


# --- purge -----------------------------------------------------------------------------

def purge_expired(now=None, *, everything=False, actor=None):
    """Permanently removes trash entries past expires_at (all of them with everything=True).
    Their rows stay, stamped purged_at, so undo can refuse with trash_expired."""
    now = now or time.time()
    # Redact holds (expires_at NULL) are never purged here: only the owner's click deletes them.
    entries = db.list_trash(expired_before=None if everything else now, holds=False)
    purged, freed = [], 0
    for t in entries:
        for _src, dst in _file_pairs(t):
            try:
                dst.unlink(missing_ok=True)
            except OSError as e:
                print(f"trash purge: could not remove {dst}: {e!r}", flush=True)
                continue
        _rmdir_empty(t["batch_id"])
        db._mark_trash_purged(t["batch_id"], t["slug"], now)
        purged.append(t["slug"])
        freed += t.get("size_bytes") or 0
    if purged:
        # A record, not an undoable change (no row images): the files are gone.
        changes.record(OP_PURGE, actor, [], affected_slugs=purged)
    return {"purged": len(purged), "bytes": freed, "slugs": purged, "held_kept": len(db.list_trash(holds=True))}


def empty_trash(confirm="", *, actor=None):
    """Purges every ORDINARY delete in the trash now (not redact holds: those wait for the owner).
    Requires the typed phrase, like delete-all."""
    if (confirm or "").strip() != EMPTY_TRASH_PHRASE:
        raise InvalidInput(f"Type {EMPTY_TRASH_PHRASE!r} in the confirm field to empty the trash",
                           code="confirm_required")
    return purge_expired(everything=True, actor=actor)


def _entry(t):
    return {"slug": t["slug"], "title": t["title"], "batch_id": t["batch_id"], "reason": t["reason"],
            "size_bytes": t["size_bytes"], "created_at": t["created_at"], "expires_at": t["expires_at"]}


def trash_summary():
    """{count, bytes, oldest, next_expiry, days, items, held: {count, bytes, items}}: ordinary
    deletes (purged after `days`) and, separately under `held`, redact holds (no expiry; kept
    until recovered or permanently deleted)."""
    entries = db.list_trash(holds=False)
    held = db.list_trash(holds=True)
    return {
        "count": len(entries),
        "bytes": sum(t.get("size_bytes") or 0 for t in entries),
        "oldest": min((t["created_at"] for t in entries), default=None),
        "next_expiry": min((t["expires_at"] for t in entries), default=None),
        "days": TRASH_DAYS,
        "items": [_entry(t) for t in entries],
        "held": {"count": len(held), "bytes": sum(t.get("size_bytes") or 0 for t in held),
                 "items": [_entry(t) for t in held]},
    }
