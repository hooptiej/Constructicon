#!/usr/bin/env python3
"""The date guesser (#725, core/date_guess.py): pure, no DB, no files, no network.

    python scripts/test_date_guess_725.py

Covers: dates in file names (camera, phone, screenshot, messenger and plain ISO styles, separators,
times, milliseconds), what must NOT read as a date (Facebook id runs, a date running into more
digits, an impossible month, a year before 1980, the future, two long numbers next to a unix
timestamp), 10- and 13-digit unix timestamps; PDF dates with and without an offset; dates in OCR
text (ISO, "May 12, 2019", "12 May 2019", US "05/12/2019"), one distinct date = medium, several =
low; the siblings median; a stored YouTube publish date; the ranking (auto guesses first, in
filename, file_metadata, youtube order; suggestions after) and best / best_auto. Naive dates are
Mountain Time (MDT in May, MST in January). Exits 1 if any check fails.
"""

import os
import sys
from datetime import datetime, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))

from core import date_guess as G  # noqa: E402

FAILS = []
NOW = datetime(2026, 10, 17, tzinfo=timezone.utc).timestamp()


def check(name, cond, extra=""):
    print(("PASS " if cond else "FAIL ") + name + (f"  [{extra}]" if (extra and not cond) else ""))
    if not cond:
        FAILS.append(name)


def utc(*a):
    return datetime(*a, tzinfo=timezone.utc).timestamp()


def iso(g):
    return g.as_dict()["date"] if g else None


print("--- file names ---")
CASES = {
    "IMG_20190512_143022.jpg": "2019-05-12T14:30:22-06:00",
    "PXL_20230101_120000123.jpg": "2023-01-01T12:00:00-07:00",       # milliseconds run on; MST in January
    "Screenshot 2019-05-12 at 14.30.22.png": "2019-05-12T14:30:22-06:00",
    "WhatsApp Image 2019-05-12 at 2.30.22 PM.jpeg": "2019-05-12T00:00:00-06:00",  # 12-hour time: date only
    "VID-20190512-WA0001.mp4": "2019-05-12T00:00:00-06:00",
    "2019-05-12 shop floor.png": "2019-05-12T00:00:00-06:00",
    "scan_2019_05_12.pdf": "2019-05-12T00:00:00-06:00",
    "notes 2019.05.12.txt": "2019-05-12T00:00:00-06:00",
    "20190512.jpg": "2019-05-12T00:00:00-06:00",
    "dir/sub/IMG_20190512_143022.jpg": "2019-05-12T14:30:22-06:00",  # a path: the base name counts
    "1600000000.jpg": "2020-09-13T06:26:40-06:00",                   # unix seconds
    "1600000000000.png": "2020-09-13T06:26:40-06:00",                # unix milliseconds
}
for name, want in CASES.items():
    g = G.from_filename(name, now=NOW)
    check(f"{name!r} -> {want}", g is not None and iso(g) == want and g.source == G.SRC_FILENAME
          and g.confidence == G.HIGH and g.auto, (iso(g), g))
g = G.from_filename("IMG_20190512_143022.jpg", now=NOW)
check("evidence is the matched text", g.evidence == "20190512_143022", g.evidence)

NOT_DATES = [
    "543441562_818882900467741_26578027318789_n.jpg",   # Facebook id runs
    "622811069_10229086720944430_1618865197301_n.jpg",
    "201905121.jpg",                                    # a date running straight into another digit
    "part 20151301.stl",                                # month 13
    "19700101 epoch.png",                               # before 1980
    "20991231 future.png",                              # the future
    "photo_1600000000_v2_123456.jpg",                   # a unix timestamp beside another long number
    "0999999999.jpg",                                   # 10 digits but before 2001
    "clodapede.png", "", None,
]
for name in NOT_DATES:
    check(f"{name!r} -> no date", G.from_filename(name, now=NOW) is None, iso(G.from_filename(name, now=NOW)))

print("--- PDF dates ---")
check("D:20190512143022-06'00' -> 20:30:22 UTC", G.parse_pdf_date("D:20190512143022-06'00'") == utc(2019, 5, 12, 20, 30, 22))
check("D:20190512143022Z -> UTC as is", G.parse_pdf_date("D:20190512143022Z") == utc(2019, 5, 12, 14, 30, 22))
check("D:20190512143022+05'30' -> offset east", G.parse_pdf_date("D:20190512143022+05'30'") == utc(2019, 5, 12, 9, 0, 22))
check("D:20190512 (no time, no zone) -> Mountain midnight", G.parse_pdf_date("D:20190512") == utc(2019, 5, 12, 6))
check("20190512 without the D: prefix", G.parse_pdf_date("20190512") == utc(2019, 5, 12, 6))
for bad in ("", None, "garbage", "D:20191340", 42):
    check(f"{bad!r} -> None", G.parse_pdf_date(bad) is None)

print("--- text ---")
g = G.from_text("Invoice dated May 12, 2019. Thank you.", now=NOW)
check("'May 12, 2019' -> medium (the only date)", g and iso(g) == "2019-05-12T00:00:00-06:00" and g.confidence == G.MEDIUM
      and g.source == G.SRC_OCR and not g.auto, g)
check("'12 May 2019'", iso(G.from_text("printed 12 May 2019", now=NOW)) == "2019-05-12T00:00:00-06:00")
check("'Sept. 3, 2019'", iso(G.from_text("Sept. 3, 2019", now=NOW)) == "2019-09-03T00:00:00-06:00")
check("'2019-05-12' (ISO)", iso(G.from_text("log 2019-05-12 ok", now=NOW)) == "2019-05-12T00:00:00-06:00")
check("'05/12/2019' (US month first)", iso(G.from_text("on 05/12/2019", now=NOW)) == "2019-05-12T00:00:00-06:00")
g = G.from_text("Due 2019-06-01. Issued May 12, 2019. Paid 2019-06-01.", now=NOW)
check("several dates: the most frequent, low confidence", g and iso(g) == "2019-06-01T00:00:00-06:00" and g.confidence == G.LOW, g)
check("no date in the text -> None", G.from_text("nothing to see 1234", now=NOW) is None)
check("an implausible year is ignored", G.from_text("in 1850-01-01 and 2099-01-01", now=NOW) is None)
check("empty text -> None", G.from_text("", now=NOW) is None and G.from_text(None, now=NOW) is None)

print("--- siblings and YouTube ---")
g = G.from_siblings([utc(2019, 1, 1), utc(2020, 1, 1), utc(2021, 1, 1)], "Desk Build")
check("siblings: the median, low confidence, says where", g and g.epoch == utc(2020, 1, 1) and g.confidence == G.LOW
      and "Desk Build" in g.evidence and not g.auto, g)
check("siblings: none -> None", G.from_siblings([]) is None and G.from_siblings(None) is None)
g = G.from_youtube({"published_at": "2017-07-31T15:00:00Z"}, now=NOW)
check("youtube: stored published_at, high, auto", g and g.epoch == utc(2017, 7, 31, 15) and g.auto, g)
check("youtube: no stored date -> None", G.from_youtube({}, now=NOW) is None and G.from_youtube({"published_at": "soon"}, now=NOW) is None)

print("--- ranking ---")
row = {"filename": "IMG_20190512_143022.jpg", "media_type": "image", "extracted_text": "May 1, 2018", "type_metadata": {}}
found = G.guesses(row, file_metadata_date=utc(2019, 5, 13), sibling_dates=[utc(2015, 1, 1)], now=NOW)
check("order: filename, file_metadata, then ocr_text, siblings",
      [x.source for x in found] == ["filename", "file_metadata", "ocr_text", "siblings"], [x.source for x in found])
check("best and best_auto are the filename guess", G.best(found).source == "filename" and G.best_auto(found).source == "filename")
row2 = {"filename": "scan.png", "media_type": "image", "extracted_text": "May 1, 2018", "type_metadata": {}}
found2 = G.guesses(row2, now=NOW)
check("only a text date: best is it, best_auto is None", G.best(found2).source == "ocr_text" and G.best_auto(found2) is None)
check("an implausible file_metadata date is dropped", G.guesses(row2, file_metadata_date=1.0, now=NOW)[0].source == "ocr_text")
yt = {"filename": None, "media_type": "youtube", "type_metadata": {"published_at": "2017-07-31T15:00:00Z"}, "extracted_text": ""}
check("a youtube row's stored publish date is an auto guess", G.best_auto(G.guesses(yt, now=NOW)).source == "youtube")
check("nothing -> no guesses", G.guesses({"filename": "x.png", "media_type": "image"}, now=NOW) == [])
d = G.best(found).as_dict()
check("as_dict carries date, auto and a label", d["auto"] is True and d["label"] == "the file name" and d["date"].startswith("2019-05-12"))

print()
if FAILS:
    print("FAILED: " + ", ".join(FAILS))
    sys.exit(1)
print("ALL PASS")
