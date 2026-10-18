"""Date guesser (#725, "Import date is not a birthday"): what date an undated item probably has.

Pure: no database, no files, no network. Callers hand it the evidence (core/item_dates.py gathers
it: the row, the file's own embedded date read through its type's embedded_metadata_fn, the real
dates of the other files on the same card) and get back ranked `Guess`es, each with a source, a
confidence and the evidence it rests on, so a person (or a Claude session) can see WHY.

Two kinds of guess:

  auto (confidence "high"; the evidence is in the file itself), applied without asking as the
  item's content_date with the source recorded (type_metadata.content_date_source), one undoable
  batch (core/items.py apply_dates):
    filename       a date in the file's name: IMG_20190512_143022, 2019-05-12, 2019_05_12,
                   20190512, "Screenshot 2019-05-12 at 14.30.22", or a 10-digit unix timestamp
                   (13 with milliseconds) when it is the name's only long number;
    file_metadata  a date the file carries inside it, read by its type's embedded_metadata_fn:
                   a PDF's own CreationDate, a video/audio container date, an image's EXIF date,
                   an ISO volume date, ...;
    youtube        a YouTube row's stored publish date (type_metadata published_at), when a
                   sync stored one but content_date was never set. Nothing here fetches it.

  suggested (needs a click; "medium" or "low"):
    ocr_text       a date written in the item's extracted text ("medium" when it is the only
                   distinct date there, else "low", the most frequent one);
    siblings       the median real date of the other files on the same card ("low").

Naive dates (no zone of their own) are Mountain Time (core/timeline.py source_datetime_to_epoch).
Every guess must be plausible: from 1980 (EARLIEST) to now plus a day; anything else is dropped,
never clamped. A date-only guess is that day's midnight, Mountain Time.

Ranking among the auto guesses: filename, then file_metadata, then youtube. The file name is the
owner's (or the camera's) own label and the least likely to be a later re-save date; a PDF's
CreationDate can be the day it was printed to PDF and a container date the day it was re-encoded.
"""

import re
import statistics
import time
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone

from . import timeline

SRC_FILENAME = "filename"
SRC_FILE_METADATA = "file_metadata"
SRC_YOUTUBE = "youtube"
SRC_OCR = "ocr_text"
SRC_SIBLINGS = "siblings"

HIGH, MEDIUM, LOW = "high", "medium", "low"
AUTO_SOURCES = (SRC_FILENAME, SRC_FILE_METADATA, SRC_YOUTUBE)   # also their rank, best first
SUGGEST_SOURCES = (SRC_OCR, SRC_SIBLINGS)
SOURCES = AUTO_SOURCES + SUGGEST_SOURCES
SOURCE_LABELS = {
    SRC_FILENAME: "the file name",
    SRC_FILE_METADATA: "the file's own metadata",
    SRC_YOUTUBE: "YouTube's publish date",
    SRC_OCR: "a date in its text",
    SRC_SIBLINGS: "the other files on its card",
}

EARLIEST = 315532800.0          # 1980-01-01T00:00:00Z, the same floor as core/embedded_metadata.py
FUTURE_SLACK_SECONDS = 86400.0  # a camera clock a little fast
UNIX_FLOOR = 1000000000.0       # 2001-09-09: the first 10-digit unix timestamp
OCR_SCAN_CHARS = 20000          # how much extracted text is scanned for dates


@dataclass(frozen=True)
class Guess:
    epoch: float
    source: str
    confidence: str
    evidence: str

    @property
    def auto(self):
        return self.confidence == HIGH and self.source in AUTO_SOURCES

    def as_dict(self):
        d = asdict(self)
        d["auto"] = self.auto
        d["label"] = SOURCE_LABELS[self.source]
        d["date"] = timeline.epoch_to_local(self.epoch).isoformat()
        return d


def plausible(epoch, now=None):
    """The epoch as a float when it is a believable content date (1980 .. now + a day), else None."""
    if epoch is None or isinstance(epoch, bool):
        return None
    try:
        value = float(epoch)
    except (TypeError, ValueError):  # silent-ok: not a number = no date, by contract
        return None
    if value != value or value < EARLIEST:
        return None
    if value > (time.time() if now is None else now) + FUTURE_SLACK_SECONDS:
        return None
    return value


def _local(y, mo, d, hh=0, mm=0, ss=0):
    """A naive wall-clock date -> epoch (Mountain Time), or None when it isn't a real date."""
    try:
        return timeline.source_datetime_to_epoch(datetime(y, mo, d, hh, mm, ss))
    except (ValueError, OverflowError):  # silent-ok: an impossible date (Feb 30) = no date
        return None


# --- file names ------------------------------------------------------------------------------

# A calendar date, year first, with ONE separator style (2019-05-12, 2019_05_12, 2019.05.12,
# 20190512), not inside a longer run of digits; optionally a time after it (143022, 14-30-22,
# " at 14.30.22", T14:30:22), which may itself run on into milliseconds (PXL_..._143022123).
_FILENAME_DATE = re.compile(
    r"(?<![0-9])(?P<y>(?:19|20)[0-9]{2})(?P<s>[-_.]?)(?P<m>0[1-9]|1[0-2])(?P=s)(?P<d>0[1-9]|[12][0-9]|3[01])"
    r"(?:(?:[ _T-]|\s+at\s+)?(?P<H>[01][0-9]|2[0-3])[-_.:h]?(?P<M>[0-5][0-9])[-_.:m]?(?P<S>[0-5][0-9]))?"
    r"(?P<tail>[0-9]?)")
_LONG_NUMBER = re.compile(r"(?<![0-9])[0-9]{6,}(?![0-9])")


def from_filename(name, now=None):
    """The date in a file name, as a high-confidence Guess, or None."""
    if not name:
        return None
    stem = str(name).rsplit("/", 1)[-1].rsplit("\\", 1)[-1]
    if "." in stem:
        stem = stem.rsplit(".", 1)[0]
    for m in _FILENAME_DATE.finditer(stem):
        has_time = m.group("H") is not None
        if not has_time and m.group("tail"):
            continue  # a date running straight into more digits is part of some longer number
        y, mo, d = int(m.group("y")), int(m.group("m")), int(m.group("d"))
        if has_time:
            epoch = _local(y, mo, d, int(m.group("H")), int(m.group("M")), int(m.group("S")))
        else:
            epoch = _local(y, mo, d)
        epoch = plausible(epoch, now)
        if epoch is not None:
            evidence = m.group(0)[:-1] if m.group("tail") else m.group(0)  # the run-on digit isn't evidence
            return Guess(epoch, SRC_FILENAME, HIGH, evidence.strip())
    # A unix timestamp: only when it is the name's ONE long number. Facebook-style names
    # (543441562_818882900467741_26578027318789_n) are runs of ids, never a date.
    longs = _LONG_NUMBER.findall(stem)
    if len(longs) == 1 and len(longs[0]) in (10, 13):
        value = int(longs[0]) / (1000.0 if len(longs[0]) == 13 else 1.0)
        if value >= UNIX_FLOOR:
            epoch = plausible(value, now)
            if epoch is not None:
                return Guess(epoch, SRC_FILENAME, HIGH, longs[0])
    return None


# --- PDF dates -------------------------------------------------------------------------------

_PDF_DATE = re.compile(
    r"^(?:D:)?(?P<y>[0-9]{4})(?P<m>[0-9]{2})?(?P<d>[0-9]{2})?(?P<H>[0-9]{2})?(?P<M>[0-9]{2})?(?P<S>[0-9]{2})?"
    r"(?P<tz>Z|[+-][0-9]{2}'?(?:[0-9]{2}'?)?)?")


def parse_pdf_date(raw):
    """A PDF date string ("D:20190512143022-06'00'", "D:20190512", ...) -> UTC epoch, or None.
    A date with an offset (or Z) is trusted as is; one without is Mountain Time."""
    if not raw or not isinstance(raw, str):
        return None
    m = _PDF_DATE.match(raw.strip())
    if not m:
        return None
    try:
        parts = [int(m.group(k) or default) for k, default in
                 (("y", 0), ("m", 1), ("d", 1), ("H", 0), ("M", 0), ("S", 0))]
        dt = datetime(*parts)
    except ValueError:  # silent-ok: an impossible PDF date = no date, by contract
        return None
    tz = m.group("tz")
    if tz:
        if tz == "Z":
            dt = dt.replace(tzinfo=timezone.utc)
        else:
            digits = tz[1:].replace("'", "")
            hours, minutes = int(digits[:2]), int(digits[2:4] or 0)
            offset = timedelta(hours=hours, minutes=minutes)
            dt = dt.replace(tzinfo=timezone(offset if tz[0] == "+" else -offset))
    return timeline.source_datetime_to_epoch(dt)


# --- text (OCR) ------------------------------------------------------------------------------

_MONTHS = {name: i + 1 for i, names in enumerate((
    ("january", "jan"), ("february", "feb"), ("march", "mar"), ("april", "apr"), ("may",),
    ("june", "jun"), ("july", "jul"), ("august", "aug"), ("september", "sep", "sept"),
    ("october", "oct"), ("november", "nov"), ("december", "dec"))) for name in names}
_MONTH_RE = "|".join(sorted(_MONTHS, key=len, reverse=True))
_TEXT_PATTERNS = (
    # 2019-05-12 (ISO)
    (re.compile(r"(?<![0-9])((?:19|20)[0-9]{2})-(0[1-9]|1[0-2])-(0[1-9]|[12][0-9]|3[01])(?![0-9])"),
     lambda g: (int(g[0]), int(g[1]), int(g[2]))),
    # May 12, 2019 / May 12 2019 / Sept. 3, 2019
    (re.compile(rf"\b({_MONTH_RE})\.?\s+([0-9]{{1,2}})(?:st|nd|rd|th)?,?\s+((?:19|20)[0-9]{{2}})\b", re.I),
     lambda g: (int(g[2]), _MONTHS[g[0].lower()], int(g[1]))),
    # 12 May 2019
    (re.compile(rf"\b([0-9]{{1,2}})\s+({_MONTH_RE})\.?,?\s+((?:19|20)[0-9]{{2}})\b", re.I),
     lambda g: (int(g[2]), _MONTHS[g[1].lower()], int(g[0]))),
    # 05/12/2019 (US month first; the owner's documents are American)
    (re.compile(r"(?<![0-9/])(0?[1-9]|1[0-2])/(0?[1-9]|[12][0-9]|3[01])/((?:19|20)[0-9]{2})(?![0-9/])"),
     lambda g: (int(g[2]), int(g[0]), int(g[1]))),
)


def from_text(text, now=None):
    """A date written in the item's extracted text, as a suggested Guess, or None. "medium" when
    the text holds exactly one distinct date, else "low" for the most frequent (latest on a tie)."""
    if not text:
        return None
    text = str(text)[:OCR_SCAN_CHARS]
    found = {}   # epoch -> (count, first evidence)
    for pattern, ymd in _TEXT_PATTERNS:
        for m in pattern.finditer(text):
            epoch = plausible(_local(*ymd(m.groups())), now)
            if epoch is None:
                continue
            count, evidence = found.get(epoch, (0, m.group(0)))
            found[epoch] = (count + 1, evidence)
    if not found:
        return None
    epoch, (count, evidence) = max(found.items(), key=lambda kv: (kv[1][0], kv[0]))
    return Guess(epoch, SRC_OCR, MEDIUM if len(found) == 1 else LOW, evidence.strip())


# --- siblings and YouTube --------------------------------------------------------------------

def from_siblings(dates, card_title=None):
    """The median of the real dates of the other files on the item's card, or None (needs at
    least one). Low confidence: it says roughly when, never exactly."""
    real = sorted(d for d in (plausible(x) for x in (dates or ())) if d is not None)
    if not real:
        return None
    where = f" on {card_title}" if card_title else ""
    return Guess(float(statistics.median_low(real)), SRC_SIBLINGS, LOW,
                 f"median of {len(real)} dated file{'' if len(real) == 1 else 's'}{where}")


def from_youtube(type_metadata, now=None):
    """A stored YouTube publish date (type_metadata published_at / publishedAt, ISO 8601), or None."""
    tm = type_metadata or {}
    raw = tm.get("published_at") or tm.get("publishedAt")
    if not raw or not isinstance(raw, str):
        return None
    try:
        dt = datetime.fromisoformat(raw.strip().replace("Z", "+00:00"))
    except ValueError:  # silent-ok: not an ISO date = no date, by contract
        return None
    epoch = plausible(timeline.source_datetime_to_epoch(dt), now)
    return Guess(epoch, SRC_YOUTUBE, HIGH, raw.strip()) if epoch is not None else None


# --- all of it -------------------------------------------------------------------------------

def guesses(row, *, file_metadata_date=None, sibling_dates=(), card_title=None, now=None):
    """Every guess for one capture_events row, best first: the auto ones in AUTO_SOURCES order,
    then the suggestions (ocr_text, then siblings). `file_metadata_date` is the epoch the file's
    own type hook read (core/embedded_metadata.py extract()["content_date"]), or None;
    `sibling_dates` are the real dates of the other files on its card."""
    out = []
    g = from_filename(row.get("filename"), now)
    if g:
        out.append(g)
    fm = plausible(file_metadata_date, now)
    if fm is not None:
        out.append(Guess(fm, SRC_FILE_METADATA, HIGH, "embedded in the file"))
    if row.get("media_type") == "youtube":
        g = from_youtube(row.get("type_metadata"), now)
        if g:
            out.append(g)
    g = from_text(row.get("extracted_text"), now)
    if g:
        out.append(g)
    g = from_siblings(sibling_dates, card_title)
    if g:
        out.append(g)
    return out


def best(found):
    """The first (best) guess, or None."""
    return found[0] if found else None


def best_auto(found):
    """The best guess that may be applied without asking, or None."""
    return next((g for g in found if g.auto), None)
