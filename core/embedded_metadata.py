"""Metadata embedded in an uploaded file itself — issues #255 and #265.

Some uploads carry their own descriptive metadata inside the file: an
audio file's ID3 tags (title, artist, album, ...), a photo's EXIF capture
timestamp, a video container's creation_time; conceivably an image's EXIF
caption or a PDF's document title later. This module is the one place
that reads it at upload time and seeds the row's content-side fields from
it, dispatching purely off ObjectTypeSpec.embedded_metadata_fn (see
core/object_types/__init__.py) the same way core/ocr.py dispatches off
text_extract_fn — a new type that carries embedded metadata registers a
function there; nothing here grows a media_type branch.

Where the values go — the same "content's own description/date" columns
#54's YouTube sync populates from the YouTube Data API, since a tag title
is to an MP3 exactly what the API title is to a video, and a camera's
capture timestamp is to a photo what publishedAt is to that video:

  - content_date (#265) <- the moment the content itself happened, as UTC
    unix seconds: EXIF DateTimeOriginal for an image, the container's
    creation_time for a video (core/object_types/image.py and video.py).
    This is the slot core/timeline.py's resolve_item_date trusts over
    everything except a manual display_date_override — it beats both
    source_modified_at and the upload timestamp — so a hook should only
    ever return a date that really is the content's own, not a file
    modification time (the file's mtime already has its own, weaker rung
    in that chain). A naive source date is interpreted as Mountain Time
    via timeline.source_datetime_to_epoch; see there for the convention.
    #769: a video's Apple capture-date tag beats its creation_time (see
    video.py), and a date the hook marks export-prone that sits at the
    upload moment is refused as an export time (EXPORT_TIME_WINDOW_SECONDS,
    resolve_file_date). Which tag dated the row, or why its file date was
    refused, goes in type_metadata.content_date_source.

  - the title -> content_description (the content's own title, as opposed
    to `description`, the uploader's note about it — see the
    capture_events comments in core/db.py), short_title (#542: the name in
    core/item_title.py's chain, display_name -> short_title -> filename ->
    slug; the long caption is never the title) AND display_name, so the title is the
    owner-visible name from the start and a later rename or caption edit
    can't change what the upload carried in. display_name is documented (#11) as "a
    per-object override of the filename"; a tag title is precisely that,
    and #241's inline rename still clears it back to the filename as
    before.
  - everything else (artist/album/track/year/genre) -> type_metadata,
    under the keys the type's metadata_fields document. A year is
    deliberately NOT promoted to content_date: a bare "2019" would become
    Jan 1 2019 with a precision the tag never had.

Never overwrites (fill_missing): a field is only written when the row has
nothing there yet — a value the owner typed, a #241 rename, a YouTube
sync's publishedAt, an earlier pass — all win over what the file says,
and type_metadata is merged key by key (only absent keys are added) rather
than replaced. So this is safe to call on a row more than once, and safe
as a backfill over rows uploaded before the hook for their type existed:
scripts/backfill_content_dates.py runs extract() over every undated
image/video row and writes the result through POST /api/image/{slug}.

Synchronous, not a background task like OCR/captioning: reading a tag
block is one ffprobe call over the file header — the same call the detail
page already makes on every view for duration/codec — milliseconds, not
the seconds-to-minutes tesseract or the vision model take. Running it
before /api/upload responds means the returned object already carries the
title, so the upload drawer's freshly inserted card shows it without a
poll. Best-effort throughout: an extraction or write failure is printed
and swallowed — it must never fail an upload whose row already exists (a
client that saw a 500 would retry straight into a 409 duplicate).
"""

import time

from . import db, item_title, object_types, parse_limits, storage

# A content_date a hook returns is only accepted inside this window. Real
# file metadata does carry sentinels — an MP4 whose creation time was never
# set decodes to QuickTime's 1904 epoch, a camera with an unset clock
# writes "0000:00:00 00:00:00" or its factory default — and none of those
# belongs in the slot the Timeline trusts over everything but a manual
# override. 1980 predates any digital camera or consumer video capture
# that could legitimately land here; anything past "now" (with a day of
# slack for a camera clock that's merely a little fast) is a clock set
# wrong. Rejected values are skipped, not clamped: no date beats a wrong
# one, since the row then falls back to its file mtime as before.
EARLIEST_PLAUSIBLE_CONTENT_DATE = 315532800.0  # 1980-01-01T00:00:00Z
FUTURE_SLACK_SECONDS = 86400.0


def plausible_content_date(value):
    """The value as a float if it's a usable content_date, else None. Also
    used by scripts/backfill_content_dates.py so its dry-run report shows
    exactly what fill_missing would accept."""
    if value is None or isinstance(value, bool):
        return None
    try:
        epoch = float(value)
    except (TypeError, ValueError):  # silent-ok: not a number = no usable date, by contract
        return None
    if epoch < EARLIEST_PLAUSIBLE_CONTENT_DATE or epoch > time.time() + FUTURE_SLACK_SECONDS:
        return None
    return epoch


# #769, "fresh from the export oven": a file date that sits right at the upload moment is the moment
# the file was EXPORTED for upload (macOS Photos and the iPhone's upload path re-mux a video and stamp
# its creation_time with "now"), not when the content happened. A date a hook marks export-prone
# (content_date_export_prone: video creation_time, not the Apple capture-date tag or EXIF
# DateTimeOriginal) is refused when it is from after the install's first upload (the same cutoff #725
# uses for file mtimes, timeline.first_upload) AND within this many seconds of the row's own upload
# time, either side. Measured on prod: the bad videos' creation_time was 1 minute before upload.
# Refused = "no date": the row falls back to its file-name date and then its card's range, and
# type_metadata.content_date_source records the refusal (never silent).
EXPORT_TIME_WINDOW_SECONDS = 600.0
SRC_EXPORT_TIME_REJECTED = "rejected_export_time"


def looks_like_export_time(epoch, uploaded_at, first_upload_at):
    """True when a file date `epoch` is an export moment by #769's rule (see
    EXPORT_TIME_WINDOW_SECONDS): after the install's first upload (when there is one) and within
    the window of `uploaded_at`, the row's upload time. Pure."""
    if epoch is None or uploaded_at is None:
        return False
    if first_upload_at is not None and epoch < first_upload_at:
        return False
    return abs(float(uploaded_at) - float(epoch)) <= EXPORT_TIME_WINDOW_SECONDS


def resolve_file_date(found, row, first_upload=None):
    """#769: (content_date or None, content_date_source record or None) for what extract() found.
    The plausibility gate first (nothing to record for a sentinel), then the export-time backstop for
    an export-prone date: refused -> (None, a `rejected_export_time` record with the refused value
    and why). An accepted date whose hook named its tag gets {source: <tag>, confidence: high,
    evidence: "<tag> <raw>", guessed: False}; a hook that names no tag (image EXIF, PDF) records
    nothing, as before. `first_upload` is the install's first upload (timeline.first_upload())."""
    epoch = plausible_content_date((found or {}).get("content_date"))
    if epoch is None:
        return None, None
    tag = found.get("content_date_tag")
    raw = str(found.get("content_date_raw") or "")[:120]
    label = (tag or "file date").replace("_", " ")
    if found.get("content_date_export_prone") and looks_like_export_time(epoch, row.get("timestamp"), first_upload):
        minutes = (float(row["timestamp"]) - epoch) / 60.0
        when = f"{abs(minutes):.0f} min {'before' if minutes >= 0 else 'after'}"
        return None, {"source": SRC_EXPORT_TIME_REJECTED, "rejected": True, "rejected_tag": tag,
                      "rejected_value": epoch, "guessed": False,
                      "evidence": (f"ignored the file's {label} {raw}: it is {when} the upload, so it is "
                                   f"when the file was exported, not when it was shot")[:200]}
    if not tag:
        return epoch, None
    return epoch, {"source": tag, "confidence": "high", "evidence": f"{label} {raw}"[:200], "guessed": False}


def extract(row):
    """Runs the row's type's embedded_metadata_fn against its stored file.
    Returns that fn's dict ({"content_description": str, "type_metadata":
    dict, "content_date": float}, every key optional) or {} — for a type
    with no hook, a row with no file on disk, or any failure."""
    spec = object_types.get_object_type(row.get("media_type"))
    if not spec.embedded_metadata_fn or not row.get("stored_filename"):
        return {}
    path = storage.path_for(row["stored_filename"])
    if not path.exists():
        return {}
    try:
        # #644: in a parser child, background budget; a limit hit leaves the row as it was
        # (nothing filled), is logged and is shown on the object page.
        return parse_limits.call(spec.embedded_metadata_fn, path, kind=parse_limits.BACKGROUND,
                                 what="file metadata", key=row["stored_filename"]) or {}
    except Exception as e:
        print(f"embedded_metadata_fn failed for {row.get('slug')}: {e!r}")
        return {}


def fill_missing(slug):
    """Seeds content_description, display_name, type_metadata and
    content_date from the file's embedded metadata, touching only what the
    row doesn't already have (see the module docstring for the rule and
    why both title columns). #725: then, when the row still has no real
    date, a date in its file name (_date_from_filename). Returns the row
    afterwards — re-read if anything was written — or None for an unknown slug."""
    row = db.get_by_slug(slug)
    if row is None:
        return None
    try:
        found = extract(row)
        if not found:
            return _date_from_filename(slug, row)  # #725: a name can still carry the date
        title = (found.get("content_description") or "").strip()
        existing_tm = row.get("type_metadata") or {}
        new_tm = {
            key: value
            for key, value in (found.get("type_metadata") or {}).items()
            if key not in existing_tm and value not in (None, "")
        }
        # None means "leave alone" to update_content_metadata, same as the
        # /api/image/{slug} route's own partial-update convention.
        content_description = title if title and not row.get("content_description") else None
        # #542: the content's own title is also its short title (the name in the title chain), same
        # fill-only rule. A title too long or multi-line to be a name stays a description only.
        short_title = (title if title and not row.get("short_title")
                       and item_title.title_shaped(title, item_title.SHORT_TITLE_MAX) else None)
        if content_description is not None or new_tm or short_title is not None:
            row = db._update_content_metadata(slug, content_description=content_description, type_metadata=new_tm or None,
                                              short_title=short_title)
        if title and row is not None and not row.get("display_name"):
            row = db._rename_object(slug, display_name=title)
        # content_date (#265): same fill-only-missing rule. `is None`, not
        # falsiness — 0.0 would be a real (if implausible, see the gate)
        # value, and an existing one from any source is left alone. #769:
        # resolve_file_date applies the export-time backstop and says which
        # tag won (or why the date was refused) for content_date_source.
        if row is not None and row.get("content_date") is None:
            from . import timeline  # lazy: keeps this module's import list as it was
            content_date, record = resolve_file_date(found, row, timeline.first_upload())
            if content_date is not None:
                db._set_content_date(slug, content_date)
            if record is not None:
                db._update_content_metadata(slug, type_metadata={"content_date_source": record})
            if content_date is not None or record is not None:
                row = db.get_by_slug(slug)
    except Exception as e:
        print(f"embedded metadata fill failed for {slug}: {e!r}")
    return _date_from_filename(slug, row)


def _date_from_filename(slug, row):
    """#725: an upload that still has no real date (no EXIF / container / PDF date, and a file
    mtime from after the install's first upload, which is a download date, not a real one) takes a
    date written in its file name (IMG_20190512_..., 2019-05-12, a unix timestamp: core/date_guess.py)
    as content_date, recording where it came from (type_metadata.content_date_source). Same
    fill-only rule and same best-effort contract as the rest of fill_missing."""
    if row is None:
        return row
    try:
        from . import date_guess, timeline  # lazy: keeps this module's import list as it was
        if timeline.resolve_item_date(row) is not None:
            return row
        guess = date_guess.from_filename(row.get("filename"))
        if guess is None:
            return row
        record = {"source": guess.source, "confidence": guess.confidence, "evidence": guess.evidence[:200],
                  "guessed": True, "accepted": "auto"}
        # #769: a file date refused as an export time just before stays visible under the guess.
        previous = (row.get("type_metadata") or {}).get("content_date_source") or {}
        if previous.get("rejected"):
            record["rejected_file_date"] = previous
        db._set_content_date(slug, guess.epoch)
        db._update_content_metadata(slug, type_metadata={"content_date_source": record})
        row = db.get_by_slug(slug)
    except Exception as e:
        print(f"embedded metadata fill failed for {slug}: {e!r}")
    return row
