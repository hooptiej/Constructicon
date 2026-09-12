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

  - the title -> content_description (the content's own title, as opposed
    to `description`, the uploader's note about it — see the
    capture_events comments in core/db.py) AND display_name. Both, because
    web/app.py's display-name fallback chain is display_name -> filename
    -> content_description -> slug: for a file upload the filename always
    wins over content_description, so a title stored only there would
    never reach a gallery tile or the detail page header — which is the
    whole point of pulling it. display_name is documented (#11) as "a
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

from . import db, object_types, storage

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
    except (TypeError, ValueError):
        return None
    if epoch < EARLIEST_PLAUSIBLE_CONTENT_DATE or epoch > time.time() + FUTURE_SLACK_SECONDS:
        return None
    return epoch


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
        return spec.embedded_metadata_fn(path) or {}
    except Exception as e:
        print(f"embedded_metadata_fn failed for {row.get('slug')}: {e!r}")
        return {}


def fill_missing(slug):
    """Seeds content_description, display_name, type_metadata and
    content_date from the file's embedded metadata, touching only what the
    row doesn't already have (see the module docstring for the rule and
    why both title columns). Returns the row afterwards — re-read if
    anything was written — or None for an unknown slug."""
    row = db.get_by_slug(slug)
    if row is None:
        return None
    try:
        found = extract(row)
        if not found:
            return row
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
        if content_description is not None or new_tm:
            row = db.update_content_metadata(slug, content_description=content_description, type_metadata=new_tm or None)
        if title and row is not None and not row.get("display_name"):
            row = db.rename_object(slug, display_name=title)
        # content_date (#265): same fill-only-missing rule. `is None`, not
        # falsiness — 0.0 would be a real (if implausible, see the gate)
        # value, and an existing one from any source is left alone.
        content_date = plausible_content_date(found.get("content_date"))
        if content_date is not None and row is not None and row.get("content_date") is None:
            db.set_content_date(slug, content_date)
            row = db.get_by_slug(slug)
    except Exception as e:
        print(f"embedded metadata fill failed for {slug}: {e!r}")
    return row
