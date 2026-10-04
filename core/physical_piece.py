"""Physical-piece fields (#425): a drawing, painting, print or other made-by-hand object that
was scanned or photographed into Constructicon.

Nothing here touches the schema. The fields live in capture_events.type_metadata (the same
place `medium` already lived for the first piece):

    medium            "ink on paper"          free text, autocompleted from earlier values
    dimensions        "8.5 x 11 in"           free text
    date_made         "2009", "2009-06", "2009-06-14"   when the piece was MADE (not scanned)
    original_location "framed, office"        optional: where the physical original is now

Date made feeds the item's effective date (core/timeline.py resolve_item_date): it ranks below
a hand-set timeline date (display_date_override) and above content_date, because content_date
for a scan is the scan's EXIF/creation time, which is exactly the date this field exists to
correct. Everything is pure except `medium_suggestions` and `in_traditional_media`.
"""

import json
import re
from datetime import datetime

KEYS = ("medium", "dimensions", "date_made", "original_location")
LABELS = {
    "medium": "Medium",
    "dimensions": "Dimensions",
    "date_made": "Date made",
    "original_location": "Original is",
}
MAX_LEN = 200
HOBBY_NAME = "traditional media"

_DATE_RE = re.compile(r"^(\d{4})(?:-(\d{2})(?:-(\d{2}))?)?$")


def parse_date_made(value):
    """'YYYY', 'YYYY-MM' or 'YYYY-MM-DD' -> (year, month, day) with missing parts defaulted
    (a bare year is July 1, a year-month is the 15th: mid-period, so it sorts sensibly). Returns
    None for blank or unparseable text. A real calendar date is required."""
    if not isinstance(value, str):
        return None
    m = _DATE_RE.match(value.strip())
    if not m:
        return None
    y, mo, d = int(m.group(1)), m.group(2), m.group(3)
    if y < 1000:
        return None
    month = int(mo) if mo else 7
    day = int(d) if d else (15 if mo else 1)
    try:
        datetime(y, month, day)
    except ValueError:
        return None
    return (y, month, day)


def date_made_epoch(type_metadata):
    """The date_made in a type_metadata dict as UTC unix seconds (local noon, so a timezone
    shift can never roll it onto the neighbouring day), or None."""
    if isinstance(type_metadata, str):
        try:
            type_metadata = json.loads(type_metadata)
        except ValueError:
            return None
    if not isinstance(type_metadata, dict):
        return None
    ymd = parse_date_made(type_metadata.get("date_made"))
    if ymd is None:
        return None
    # Imported here so this module stays importable on its own.
    from core import timeline
    return timeline.source_datetime_to_epoch(datetime(ymd[0], ymd[1], ymd[2], 12, 0))


def clean_fields(md):
    """Validate/normalize the physical-piece keys of a type_metadata update dict (other keys
    pass through untouched). Strings are trimmed and capped at MAX_LEN; an empty string is kept
    (the route merges, so "" is how a field is cleared). Raises ValueError for a date_made that
    is not YYYY / YYYY-MM / YYYY-MM-DD."""
    out = dict(md)
    for k in KEYS:
        if k not in out:
            continue
        v = out[k]
        if v is None:
            out[k] = ""
            continue
        if not isinstance(v, str):
            raise ValueError(f"{k} must be text")
        v = " ".join(v.split())[:MAX_LEN] if k != "original_location" else v.strip()[:MAX_LEN]
        if k == "date_made" and v and parse_date_made(v) is None:
            raise ValueError("date_made must look like 2009, 2009-06 or 2009-06-14")
        out[k] = v
    return out


def has_any(type_metadata):
    tm = type_metadata or {}
    return any(str(tm.get(k) or "").strip() for k in KEYS)


def rows(type_metadata):
    """[(key, label, value)] for the fact sheet and the edit form, in display order."""
    tm = type_metadata or {}
    return [(k, LABELS[k], str(tm.get(k) or "").strip()) for k in KEYS]


def in_traditional_media(db, slug, tag_names=()):
    """True when the item sits in a project that is (or whose home chain reaches) the
    Traditional Media hobby, or carries a tag of that name. Best-effort: a lookup failure
    means 'no' (the group still shows when any field is set)."""
    try:
        if any(str(t).strip().lower() == HOBBY_NAME for t in (tag_names or ())):
            return True
        for p in db.list_projects_for_post(slug):
            if any((h.get("name") or "").strip().lower() == HOBBY_NAME
                   for h in db.list_hobbies_for_project(p["id"])):
                return True
        return False
    except Exception:
        return False


def medium_suggestions(db, limit=50):
    """Distinct non-empty `medium` values across all items, most-used first (ties
    alphabetical, case-insensitive), for the edit field's datalist (same shape as
    db.distinct_card_values, #523). The key is fixed here, never caller-supplied."""
    limit = max(1, min(int(limit), 200))
    conn = db.get_conn()
    try:
        raw = conn.execute(
            "SELECT type_metadata FROM capture_events "
            "WHERE type_metadata LIKE '%\"medium\"%' AND redacted = 0").fetchall()
    finally:
        conn.close()
    counts = {}
    for r in raw:
        try:
            v = (json.loads(r[0]) or {}).get("medium")
        except (ValueError, TypeError):
            continue
        if isinstance(v, str) and v.strip():
            v = " ".join(v.split())
            counts[v] = counts.get(v, 0) + 1
    ordered = sorted(counts.items(), key=lambda kv: (-kv[1], kv[0].lower()))
    return [v for v, _ in ordered[:limit]]
