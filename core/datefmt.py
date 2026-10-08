"""The one place a stored date becomes text the owner reads (#544).

Every stored date is UTC unix seconds; every date SHOWN is Mountain Time
(`timeline.LOCAL_TIMEZONE`, DST-aware). Before this module the same instant
was printed three ways: Mountain by the item pages, UTC by the card faces
(`2026-10-01T03:00Z` read "Oct 2026" on a card but "Sep 30, 2026" on the item
it belongs to), and browser-local by the timeline rails. The JS twin is
`web/static/js/mountain-time.js`; keep the two in step.

Formats are built by hand (no `%-d`, which is a platform extension that
doesn't exist on Windows) and are English month abbreviations, as before.
"""

from . import timeline

MONTHS = ("Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")


def day(epoch):
    """`Sep 30, 2026`, or None for an empty epoch."""
    if not epoch:
        return None
    d = timeline.epoch_to_local(epoch)
    return f"{MONTHS[d.month - 1]} {d.day}, {d.year}"


def month(epoch):
    """`Sep 2026` (month granularity: the card faces' range label)."""
    d = timeline.epoch_to_local(epoch)
    return f"{MONTHS[d.month - 1]} {d.year}"


def iso_day(epoch):
    """`2026-09-30`, or None for an empty epoch."""
    if not epoch:
        return None
    return timeline.epoch_to_local(epoch).strftime("%Y-%m-%d")


def short_day(epoch, other=None):
    """`Sep 30`, with the year added (`Sep 30 2026`) unless `other` falls in the same
    Mountain-Time year. For comparing two dates side by side (revision reasons)."""
    d = timeline.epoch_to_local(epoch)
    s = f"{MONTHS[d.month - 1]} {d.day}"
    if other is None or timeline.epoch_to_local(other).year != d.year:
        s += f" {d.year}"
    return s


def datetime_label(epoch):
    """`Sep 30, 2026 at 6:31 PM`, or None for an empty epoch."""
    if not epoch:
        return None
    d = timeline.epoch_to_local(epoch)
    hour12 = d.hour % 12 or 12
    ampm = "AM" if d.hour < 12 else "PM"
    return f"{day(epoch)} at {hour12}:{d.minute:02d} {ampm}"
