"""Every date the owner reads is Mountain Time (#544). Run: python scripts/test_mountain_time_544.py

Key case: 2026-01-01T03:00Z is still Dec 31, 2025 in Mountain (UTC-7). Before #544 the card faces
printed "Jan 2026" (UTC) while the item card printed "Dec 31, 2025" (Mountain). No server, no files:
pure formatting, but it still isolates the environment like every in-process test. The JS twin of
core/datefmt.py has its own test, scripts/test_mountain_time_544.js.
"""
import _testenv
TMP = _testenv.isolate("mountain-time-544-")

import calendar
import sys

from core import cards, datefmt, revisions
from core.object_types import _office, certkey, vpptoken
from web import shapes

failures = 0


def eq(name, got, want):
    global failures
    ok = got == want
    print(("ok   " if ok else "FAIL ") + name + ("" if ok else f": got {got!r} want {want!r}"))
    if not ok:
        failures += 1


def utc(y, mo, d, h=0, mi=0):
    return calendar.timegm((y, mo, d, h, mi, 0))


NEW_YEAR_UTC = utc(2026, 1, 1, 3, 0)       # Dec 31, 2025, 8:00 PM in Mountain (MST, UTC-7)
SEP30_EVENING = utc(2026, 10, 1, 1, 0)     # Sep 30, 2026, 7:00 PM in Mountain (MDT, UTC-6)
DST_NIGHT = utc(2026, 3, 8, 8, 30)         # Mar 8, 2026 1:30 AM MST, the night before spring-forward

# the key case: the card face's month range and the item card's day label agree
eq("card face month label is Dec 2025, not Jan 2026", datefmt.month(NEW_YEAR_UTC), "Dec 2025")
eq("item card day label is Dec 31, 2025", cards._day_label(NEW_YEAR_UTC), "Dec 31, 2025")
eq("single-moment range label", cards.date_range_label(NEW_YEAR_UTC, None), "Dec 2025")
eq("range straddling the UTC new year stays in Dec 2025", cards.date_range_label(utc(2025, 12, 31, 20), NEW_YEAR_UTC), "Dec 2025")
eq("range across a real month boundary", cards.date_range_label(NEW_YEAR_UTC, utc(2026, 1, 1, 12)), "Dec 2025 - Jan 2026")
eq("leafeater case: Oct 1 01:00Z is Sep 2026 on the card", cards.date_range_label(SEP30_EVENING, SEP30_EVENING), "Sep 2026")
eq("and Sep 30, 2026 on the item", shapes._friendly_date(SEP30_EVENING), "Sep 30, 2026")
eq("active card whose end is this month shows no 'now'", cards.date_range_label(NEW_YEAR_UTC, NEW_YEAR_UTC, active=True, now=utc(2025, 12, 31, 23)), "Dec 2025")
eq("active card ended earlier shows 'now'", cards.date_range_label(NEW_YEAR_UTC, NEW_YEAR_UTC, active=True, now=utc(2026, 2, 1)), "Dec 2025 - now")
eq("no start, no label", cards.date_range_label(None, None), "")

# the app-level helpers are the same code path
eq("_friendly_datetime", shapes._friendly_datetime(NEW_YEAR_UTC), "Dec 31, 2025 at 8:00 PM")
eq("_friendly_datetime across DST (MDT is UTC-6)", shapes._friendly_datetime(SEP30_EVENING), "Sep 30, 2026 at 7:00 PM")
eq("empty epoch", shapes._friendly_date(None), None)
eq("empty epoch (datetime)", shapes._friendly_datetime(0), None)
eq("iso_day", datefmt.iso_day(NEW_YEAR_UTC), "2025-12-31")
eq("iso_day empty", datefmt.iso_day(None), None)

# the other converted sites
eq("cert/key validity date", certkey._date(NEW_YEAR_UTC), "2025-12-31")
eq("VPP token date", vpptoken._date(NEW_YEAR_UTC), "2025-12-31")
eq("Office properties date", _office.date_label(NEW_YEAR_UTC), "2025-12-31")
eq("Office properties date, none", _office.date_label(None), None)
eq("revision reason day, same year as the other", revisions._day(DST_NIGHT, DST_NIGHT), "Mar 8")
eq("revision reason day, other year adds the year", revisions._day(NEW_YEAR_UTC, utc(2026, 6, 1)), "Dec 31 2025")
eq("revision reason day, same Mountain year even though UTC years differ", revisions._day(NEW_YEAR_UTC, utc(2025, 6, 1)), "Dec 31")

print(f"\n{failures} FAILED" if failures else "\nall passed")
if failures:
    sys.exit("test_mountain_time_544: " + str(failures) + " check(s) failed, see FAIL lines above")
