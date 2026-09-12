"""Pure effective-date resolution for the Timeline feature — no DB access.
See docs/superpowers/specs/2026-09-11-constructicon-timeline-design.md.

Objects (capture_events rows) are a single point in time. Projects are a
span: they took time, they aren't moments.

Also home to the one timezone convention the date fields share (see
source_datetime_to_epoch): every stored date is UTC unix seconds, and a
real-world date read from a source that carries no timezone of its own is
interpreted as Mountain Time before it becomes one.
"""

from datetime import datetime
from zoneinfo import ZoneInfo

# The timezone a naive (timezone-less) real-world source date is taken to
# be in: an EXIF DateTimeOriginal ("2017:10:16 18:31:47" — the camera's
# wall clock, no offset unless the file also carries OffsetTimeOriginal),
# a blog post's bare "Jul 31, 2017", and whatever the next backfill finds.
# Decided 2026-09-12 during the Desk Build project's date backfill (see
# CLAUDE.md's non-obvious gotchas): the owner's cameras and calendar have
# always lived here, so this — DST-aware via zoneinfo, not a fixed
# offset — is the interpretation that makes those dates land on the right
# day. UTC was the first attempt and was wrong; scripts/
# backfill_from_hooptiej_site.py's bare-date-as-UTC-midnight parsing
# predates this decision and is the superseded convention, not a model.
LOCAL_TIMEZONE = "America/Denver"


def source_datetime_to_epoch(dt):
    """A datetime read from a source (file metadata, a blog date) -> UTC
    unix seconds, the form content_date/timestamp are stored in. A naive
    datetime is interpreted as LOCAL_TIMEZONE; one that already carries
    tzinfo (an ffprobe creation_time's Z suffix, an EXIF OffsetTimeOriginal)
    is trusted as-is — the file knew better than the convention does.

    The zone is resolved on each call rather than at import so a machine
    with no tz database (a Windows checkout without the `tzdata` package —
    zoneinfo raises ZoneInfoNotFoundError there) still imports this module
    and the rest of the app; only the naive-date path fails, loudly, inside
    the best-effort hook that called it. zoneinfo caches the ZoneInfo
    object itself, so this costs nothing after the first call."""
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=ZoneInfo(LOCAL_TIMEZONE))
    return dt.timestamp()


def resolve_item_date(row):
    """row is a capture_events dict (core.db.get_by_slug/_row_to_dict
    shape). timestamp is NOT NULL in the schema, so this always resolves.

    source_modified_at (the uploaded file's own last-modified time,
    already captured on upload -- see core.db.insert_upload) sits between
    content_date and timestamp: better than timestamp (which is only ever
    "when this row was created in Constructicon" -- an earlier bulk import
    of old phone videos used the import moment for every row, confirmed
    against real data where a video's own filename and source_modified_at
    independently agreed on a 2021 date while timestamp read as
    essentially "today"), but content_date -- when set -- is a more
    deliberate, verified real-world date and should still win."""
    if row.get("display_date_override") is not None:
        return row["display_date_override"]
    if row.get("content_date") is not None:
        return row["content_date"]
    if row.get("source_modified_at") is not None:
        return row["source_modified_at"]
    return row["timestamp"]


def resolve_project_span(project, items):
    """project is a core.db projects dict. items is that project's
    capture_events rows (core.db.list_project_items shape) — may be empty.
    Returns (start, end); an empty or single-moment project collapses to
    start == end, deliberately, rather than a special-cased "no span" branch.

    The write-up document (project["writeup_slug"], when it's also one of
    `items` — #156 projects can include their write-up in project_items
    like any other member) is excluded from the span calculation. A
    write-up's own timestamp is whenever it was authored/generated, not
    when the project's actual content happened — write-ups are being
    machine-generated fresh for existing projects, which would otherwise
    drag every project's effective_end to "whenever we wrote it up,"
    masking the real chronology entirely."""
    start_override = project.get("start_date_override")
    end_override = project.get("end_date_override")
    if start_override is not None and end_override is not None:
        return start_override, end_override

    writeup_slug = project.get("writeup_slug")
    dated_items = [item for item in items if item["slug"] != writeup_slug]
    item_dates = [resolve_item_date(item) for item in dated_items]
    derived_start = min(item_dates) if item_dates else project["created_at"]
    derived_end = max(item_dates) if item_dates else project["created_at"]

    start = start_override if start_override is not None else derived_start
    end = end_override if end_override is not None else derived_end
    return start, end
