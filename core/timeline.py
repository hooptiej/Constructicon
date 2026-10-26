"""Pure effective-date resolution for the Timeline feature — no DB access.
See docs/superpowers/specs/2026-09-11-constructicon-timeline-design.md.

Objects (capture_events rows) are a single point in time. Projects are a
span: they took time, they aren't moments.

Also home to the one timezone convention the date fields share (see
source_datetime_to_epoch): every stored date is UTC unix seconds, and a
real-world date read from a source that carries no timezone of its own is
interpreted as Mountain Time before it becomes one.
"""

import json
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


def epoch_to_local(epoch):
    """The inverse of source_datetime_to_epoch: a stored UTC unix
    timestamp -> a LOCAL_TIMEZONE-aware datetime, for anywhere a date gets
    shown to or edited by the owner (web/app.py's _friendly_date/
    _friendly_datetime/_datetime_local_value). `datetime.fromtimestamp(
    epoch)` with no tz argument uses the process's own system timezone —
    UTC inside this app's container — which is why an 18:31 MDT photo
    used to display as "Oct 17" instead of "Oct 16" before this existed.
    Always call this rather than a bare fromtimestamp() for anything the
    owner will read as a calendar date."""
    return datetime.fromtimestamp(epoch, tz=ZoneInfo(LOCAL_TIMEZONE))


def _date_made(row):
    """type_metadata.date_made (#425) as epoch seconds, or None. Imported lazily: physical_piece
    imports this module back for the timezone convention."""
    tm = row.get("type_metadata")
    if not tm:
        return None
    from core import physical_piece
    return physical_piece.date_made_epoch(tm)


# --- #725: only REAL dates count ------------------------------------------------------------
# The upload time is when a row was created in Constructicon, never the content's date ("Import
# date is not a birthday"). A file's own modified time counts only when it is from BEFORE the
# install's first upload: one from after it is almost certainly a download / copy date. The cutoff
# lives in the database (core/db.py first_upload_epoch, MIN(capture_events.timestamp)), so this pure
# module asks a provider core.db registers at import (set_first_upload_provider). With no provider
# (a unit test importing only this module) every file mtime counts, the pre-#725 behaviour for it.
AUTO = object()          # "ask the provider": the default for every first_upload= argument below
_first_upload_provider = None


def set_first_upload_provider(fn):
    """core.db registers its first_upload_epoch here (called once, at its import)."""
    global _first_upload_provider
    _first_upload_provider = fn


def first_upload():
    """The install's first upload (UTC unix seconds), or None (no provider, or no items yet)."""
    return _first_upload_provider() if _first_upload_provider is not None else None


def _cutoff(first_upload_arg):
    return first_upload() if first_upload_arg is AUTO else first_upload_arg


def mtime_is_real(mtime, first_upload_at):
    """A file's own modified time is a real date only when it is from before the install's first
    upload (#725). `first_upload_at` None (no items yet / no provider) = every mtime counts."""
    if mtime is None:
        return False
    return first_upload_at is None or mtime < first_upload_at


def date_source(row, first_upload=AUTO):
    """Which rung of the chain dates this row: "hand" (display_date_override), "made" (#425 date
    made), "content" (content_date: EXIF, a container date, a YouTube publish date, an accepted
    guess), "file" (the file's own mtime, from before the first upload), or None (no real date)."""
    if row.get("display_date_override") is not None:
        return "hand"
    if _date_made(row) is not None:
        return "made"
    if row.get("content_date") is not None:
        return "content"
    if mtime_is_real(row.get("source_modified_at"), _cutoff(first_upload)):
        return "file"
    return None


def resolve_item_date(row, first_upload=AUTO):
    """row is a capture_events dict (core.db.get_by_slug/_row_to_dict shape). Returns the item's
    REAL date as UTC unix seconds, or None when it has none (#725: "Undated").

    The chain: display_date_override (set by hand) -> date made (#425, a physical piece) ->
    content_date (the content's own date: EXIF, a container date, a YouTube publish date, an
    accepted date guess) -> source_modified_at (the file's own last-modified time) ONLY when it is
    from before the install's first upload (see mtime_is_real). The upload time (`timestamp`) is
    never the item's date: it is shown only as "Imported <date>". Every caller copes with None:
    the card face says "Undated" (or the approximate range it inherits from its card,
    core/item_dates.py), sorts put undated items last (date_sort_key), the timeline leaves them
    off its axis, spans skip them. Pass `first_upload` to resolve a batch against one cutoff."""
    if row.get("display_date_override") is not None:
        return row["display_date_override"]
    # #425: a physical piece's own "date made" outranks content_date (a scan's creation time).
    made = _date_made(row)
    if made is not None:
        return made
    if row.get("content_date") is not None:
        return row["content_date"]
    mtime = row.get("source_modified_at")
    if mtime_is_real(mtime, _cutoff(first_upload)):
        return mtime
    return None


def has_real_date(row, first_upload=AUTO):
    """True if the row has a real date (resolve_item_date is not None): the Curator's date coverage
    (#406) and card_level's dates pip count exactly the dates the cards show."""
    return resolve_item_date(row, first_upload) is not None


def date_sort_key(epoch, newest_first=False):
    """A sort key that puts None (undated) LAST in either direction, for a plain ascending sort:
    `sorted(xs, key=lambda x: date_sort_key(d(x), newest_first=True))`. Stable among the undated."""
    if epoch is None:
        return (1, 0.0)
    return (0, -epoch if newest_first else epoch)


def real_dates(items, first_upload=AUTO):
    """The real dates of `items`, undated ones skipped (for min/max/median/gap maths)."""
    cutoff = _cutoff(first_upload)
    return [d for d in (resolve_item_date(i, cutoff) for i in items) if d is not None]


def is_blank_writeup_document(item):
    """#672: True for a written-post document whose body is empty or whitespace (the same test as
    core.db.blank_document_body, on a row already in hand). type_metadata may be a dict or JSON."""
    if item.get("media_type") != "document":
        return False
    meta = item.get("type_metadata") or {}
    if isinstance(meta, str):
        try:
            meta = json.loads(meta)
        except ValueError:
            return False  # silent-ok: unparseable metadata is not "known empty"; keep the item
    return not str((meta or {}).get("body") or "").strip()


def member_span_dates(member_spans):
    """#772: the endpoints of a group card's members' spans ((start, end) pairs, None ends
    skipped) as dates a span can be built from. One level only: a member's span is its own
    (members of a family or collection can't be groups, card_rules.validate_membership)."""
    return [d for span in (member_spans or ()) for d in span if d is not None]


def resolve_project_span(project, items, first_upload=AUTO, member_spans=()):
    """project is a core.db projects dict. items is that project's
    capture_events rows (core.db.list_project_items shape) — may be empty.
    Returns (start, end) from REAL dates only (#725), or (None, None) when the card has none: no
    item with a real date and no Timeline start/end override. Upload times, file mtimes from after
    the install's first upload and the card's own created_at never count (they are not dates of the
    work). A single-moment card collapses to start == end; a card with only one override set (and
    nothing else real) collapses onto it.

    The card's own Timeline start/end overrides (hand-set) win, as before. An undated item's
    approximate date (inherited FROM this span, core/item_dates.py) is never an input here, so the
    span can't feed on itself.

    The write-up document (project["writeup_slug"], when it's also one of
    `items` — #156 projects can include their write-up in project_items
    like any other member) is excluded from the span calculation. A
    write-up's own timestamp is whenever it was authored/generated, not
    when the project's actual content happened.

    #672: an EMPTY write-up document (a written post with no body, e.g. the blank write-up a card
    was born with, left behind when the card got a different write-up) is skipped too, whether
    or not it is the current write-up.

    #772: `member_spans` is a family's or collection's members' own spans ((start, end) pairs,
    core/cards.py member_spans): the group's span covers them as well as its own files, earliest
    member start to latest member end. The group's own start/end overrides still win."""
    if project.get("start_date_override") is not None and project.get("end_date_override") is not None:
        return project["start_date_override"], project["end_date_override"]
    writeup_slug = project.get("writeup_slug")
    dated_items = [item for item in items
                   if item["slug"] != writeup_slug and not is_blank_writeup_document(item)]
    return span_from_dates(project, real_dates(dated_items, first_upload) + member_span_dates(member_spans))


def span_from_dates(project, item_dates):
    """resolve_project_span's second half: the card's own start/end overrides over the span of
    `item_dates` (its eligible items' REAL dates, already resolved). (None, None) when nothing.
    core/item_dates.py uses it for every card at once from one query."""
    start_override = project.get("start_date_override")
    end_override = project.get("end_date_override")
    derived_start = min(item_dates) if item_dates else None
    derived_end = max(item_dates) if item_dates else None

    start = start_override if start_override is not None else derived_start
    end = end_override if end_override is not None else derived_end
    if start is None:
        start = end
    if end is None:
        end = start
    return start, end
