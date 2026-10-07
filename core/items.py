"""Item service (#541 phase B): the ONE place item (capture_events) writes happen.

Same contract as core/cards.py: validate everything first, then one `db.transaction()`, every
row written through `db.ImageLog` (row images in the change log), and a `Result`. Each op takes
`dry_run` (the same path, rolled back: files never move) and `actor` (None = the request's
actor context, core/actor.py). Undo is the generic `cards.undo` / POST /api/changes/{id}/undo /
constructicon_undo(batch_id); it calls back into this module for the file side of the trash.

  update(slug, **fields)   title/icon, description, client, content_description, type_metadata
                           (merged; physical-piece keys cleaned), file provenance, highlight, brand
                           asset/role, display-date override, content_date. One Save = one batch.
  redact(slug)             "remove file, keep info": the file goes to the trash as a HOLD (no expiry),
                           the row is hidden. Never auto-purged.
  recover_redacted(slug)   the held file comes back and the item is un-redacted, as before.
  delete_redacted_file(slug, confirm)  erases the held file for good; the item stays redacted, file-less.
  unredact(slug)           visibility only, for old file-less redactions (refused while a hold exists).
  retype(slug, type, run)  the media_type change is imaged; post-processing (OCR / thumbnail /
                           caption / embedded metadata) re-runs through the caller's runner.
  delete(slugs)            files -> <storage>/.trash/<batch_id>/, rows (and everything that points
                           at them) deleted with row images, one `trash` row per item.
  relate(a, b) / unrelate(a, b)   (phase C) the "related" link; relate also shares tags and
                           card memberships both ways (#16), all imaged.
  update(..., tags=[...])  (phase C) the free-text tags ride in the same batch (core/tags.py).
  set_sensitive(slugs, on) (#603) the "This is sensitive" flag: mark = editor+, clear = admin only.
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

import json
import os
import time
from datetime import datetime

from . import captions, cards, changes, db, embedded_metadata, ingest, membership, object_types, physical_piece, provenance_options
from . import paths, storage, thumbnails, timeline
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
REASON_REDACT = "redact"

TRASH_DIR_NAME = paths.TRASH_DIR_NAME
TRASH_DAYS = 7
TRASH_TTL_SECONDS = TRASH_DAYS * 24 * 3600
EMPTY_TRASH_PHRASE = "EMPTY TRASH"

UPDATE_FIELDS = ("display_name", "icon", "description", "client", "content_description", "type_metadata",
                 "provenance", "highlight", "is_brand_asset", "brand_role", "display_date_override",
                 "content_date", "agent_notes")


# --- helpers ---------------------------------------------------------------------------

def get_item(slug):
    row = db.get_by_slug(slug) if slug else None
    if row is None:
        raise NotFound(f"No item {slug!r}.")
    return row


def title_of(row):
    return row.get("display_name") or row.get("content_description") or row.get("filename") or row["slug"]


def _slim(table, img):
    """Change summaries echo whole-row images; an item row can carry a megabyte of OCR text."""
    if img is None or table != "capture_events":
        return img
    return {k: img.get(k) for k in ("slug", "display_name", "filename", "media_type", "stored_filename")}


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
    for k in ("display_name", "icon"):
        if k in fields:
            v = fields[k]
            cols[k] = str(v) if v not in (None, "") else None
    if "description" in fields:
        cols["description"] = "" if fields["description"] is None else str(fields["description"])
    if "client" in fields:
        cols["client"] = fields["client"]
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
    return cols


def update(slug, *, tags=None, dry_run=False, actor=None, batch_id=None, **fields):
    """Edits any of UPDATE_FIELDS on one item as ONE change-log entry (so one Save is one undo).
    Every field is validated before anything is written. A no-op edit logs nothing.
    `tags` (#541 phase C): the free-text tag list, full replace, with its post_tags sync
    (core/tags.py `write_free_text`; tags it creates are imaged too). None = leave tags alone."""
    if tags is not None and (isinstance(tags, str) or not isinstance(tags, (list, tuple))):
        raise InvalidInput("tags must be a list of names", code="bad_tags")
    batch_id = batch_id or changes.new_batch_id()
    with db.transaction(dry_run=dry_run):
        row = get_item(slug)
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


CAPTION_MAX_CHARS = 2000


def set_caption(slug, text, *, accept=False, dry_run=False, actor=None, batch_id=None):
    """(#588) Writes `text` as the item's caption SUGGESTION, the way the vision model's result
    lands (type_metadata.auto_caption, status "done", model "mcp-agent"), so it shows up in the
    caption-review queue (/captions/review) and the processing view reads it as done. With
    accept=True it is also copied into content_description exactly like "Use this caption" in
    the review page (replaces the description, records which caption was used). ONE imaged batch
    through update(), so one undo reverts all of it; works with CAPTION_DISABLED, since no model runs.
    A new suggestion re-opens a previously skipped one (auto_caption_dismissed cleared)."""
    text = (text or "").strip()
    if not text:
        raise InvalidInput("The caption text is empty.", code="bad_caption")
    if len(text) > CAPTION_MAX_CHARS:
        raise InvalidInput(f"The caption is {len(text)} characters; the limit is {CAPTION_MAX_CHARS}.", code="bad_caption")
    row = get_item(slug)
    if row.get("redacted"):
        raise InvalidInput("File was redacted: there's no image left to caption.", code="redacted")
    existing = row.get("type_metadata") or {}
    tm = {
        captions.METADATA_KEY: text,
        captions.STATUS_KEY: "done",
        "auto_caption_model": captions.AGENT_MODEL,
        "auto_caption_dismissed": None,  # null = not dismissed (the review query tests IS NULL)
    }
    if "auto_caption_failed_reason" in existing:
        tm["auto_caption_failed_reason"] = None
    fields = {"type_metadata": tm}
    if accept:
        fields["content_description"] = text
        tm.update({
            captions.DESCRIPTION_STEP_KEY: None,
            captions.DESCRIPTION_STEP_LABEL_KEY: "written by an agent (MCP)",
            captions.DESCRIPTION_MODEL_KEY: captions.AGENT_MODEL,
            captions.DESCRIPTION_USED_AT_KEY: time.time(),
        })
    res = update(slug, dry_run=dry_run, actor=actor, batch_id=batch_id, **fields)
    res.data.update(accepted=bool(accept))
    return res


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
    if not want and not policy.can_unmark_sensitive(who):
        raise _forbidden("Only an admin can clear the sensitive flag.")
    if want and not roles.at_least(roles.role_of(who), roles.EDITOR):
        raise _forbidden("Marking an item sensitive needs an editor or an admin.")
    rows = []
    for slug in wanted:
        row = db.get_by_slug(slug)
        if row is None or not policy.can_view(row, who):
            raise NotFound(f"No item {slug!r}.")
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
                if not policy.can_unmark_sensitive(actor):
                    raise _forbidden("Undoing this would clear an item's sensitive flag, which only an admin can do.")


# --- related items (#16) -----------------------------------------------------------------

def relate(slug, other, *, dry_run=False, actor=None, batch_id=None):
    """Links two items as related (stored both directions) and, as #16 always did, shares their
    categorization both ways: each picks up the other's tags (post_tags) and card memberships
    (plain project_items rows, no linked-tag / cover side effects). It runs even when the pair
    was already related, as before. Every row is imaged: one undo removes the link AND what it
    shared. Relating an item to itself is a no-op."""
    batch_id = batch_id or changes.new_batch_id()
    with db.transaction(dry_run=dry_run):
        get_item(slug)
        get_item(other)
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
    shared stay (only undoing the relate takes those back)."""
    batch_id = batch_id or changes.new_batch_id()
    with db.transaction(dry_run=dry_run):
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


def _trash_insert(log, batch_id, row, reason, embedding=None):
    """Images a trash row for `row`'s files (original and/or thumbnail, whichever exist on disk).
    Returns the entry to move, or None when the item has no files at all."""
    slug, sf = row["slug"], row.get("stored_filename")
    orig = paths.storage_dir() / sf if sf else None
    has_orig = bool(orig is not None and orig.is_file())
    thumb = storage.thumb_path_for(slug)
    has_thumb = thumb.is_file()
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
        "title": title_of(row), "reason": reason, "created_at": now, "expires_at": None if hold else now + TRASH_TTL_SECONDS,
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
            row = get_item(slug)
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
        row = get_item(slug)
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
            row = get_item(slug)
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
    row = get_item(slug)
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
        get_item(slug)
        with db.ImageLog(OP_RETYPE, actor, batch_id, [slug]) as log:
            log.update("capture_events", {"slug": slug}, {"media_type": media_type})
            muts = list(log.muts)
    if not dry_run:
        _reprocess(slug, run_background or ingest.run_in_thread)
    return _result(OP_RETYPE, muts, batch_id, dry_run, db.get_by_slug(slug), slug=slug)


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
    skip them, as before). Returns data {deleted, slugs, trashed, expires_at}."""
    if isinstance(slugs, str):
        slugs = [slugs]
    wanted = list(dict.fromkeys(s for s in (slugs or []) if s))
    batch_id = batch_id or changes.new_batch_id()
    moved, all_muts, done, trashed = [], [], [], 0
    try:
        with db.transaction(dry_run=dry_run):
            rows = []
            for s in wanted:
                row = db.get_by_slug(s)
                if row is None:
                    if missing_ok:
                        continue
                    raise NotFound(f"No item {s!r}.")
                rows.append(row)
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
