"""YouTube transcripts as a searchable text source (#724 part e).

Owner decision (2026-10-17): a video's captions become part of its extracted text, through
youtube-transcript-api (MIT, pinned in requirements.txt). It reads an undocumented YouTube
endpoint; the owner accepts that grey area. Findings that shaped this module (#724, a sweep of the
82 prod videos): only ~6 have real speech, RC clips come back as "[Music] foreign [Applause]", and
the library has NO timeout of its own (one run hung for 10+ minutes).

**Where it runs.** The type's `text_fetch_fn` (core/object_types/youtube.py) calls `fetch_fields`
from the OCR pipeline (core/ocr.py), before the type's text sources are combined, so a new video,
a Retry OCR and the startup self-heal all ask YouTube once. Rows waiting for an attempt
(`transcript_status` 'queued', from the `transcripts_724e` backfill) or due a retry are worked by
web's `_transcript_loop` (web/app.py, the OCR watchdog's style: a periodic pass, a few rows each,
one at a time), which stores the result and rebuilds the row's text without re-running OCR
(`db.set_text_source_fields(..., resettle=True)`, core/text_sources.py's resettle).

**Bounds, whatever the library does:**
- a HARD deadline (`DEADLINE_SECONDS`, 20 s) on the library's whole run: it runs in a
  core/parse_limits.py child process, which is killed when the time is up (no sandbox, e.g.
  Windows: a daemon thread that is abandoned at the deadline). Each HTTP request inside also gets
  a socket timeout (`HTTP_TIMEOUT_SECONDS`), but the deadline is what holds;
- polite pacing: the fetch takes core/safe_fetch.py's one-at-a-time turn (shared with page
  fetches, with `CONSTRUCTICON_LINK_FETCH_DELAY_SECONDS` between them), and the worker does a few
  rows per pass;
- an off switch: `CONSTRUCTICON_TRANSCRIPTS=off` (default on). Off = nothing is fetched (stored
  transcripts stay searchable); scripts/_testenv.py sets it, so no test reaches YouTube.

**What is stored** (type_metadata, all `transcript*` keys):
- `transcript_status`: `found` | `none` (settled: no captions, captions off, only noise, the video
  unavailable) | `retry` (a network error, a timeout, YouTube blocking us: tried again later with
  backoff, never `failed`) | `queued` (the backfill: not tried yet);
- `transcript` (the cleaned text, only when found), `transcript_language` ("English
  (auto-generated)"), `transcript_language_code`, `transcript_kind` (`manual` | `auto`),
  `transcript_words`, `transcript_note` (the plain reason for none / retry), `transcript_attempts`,
  `transcript_retry_at`, `transcript_checked_at`.
Manual captions are preferred over auto-generated ones, English first within each, then any
language (recorded). A retry NEVER removes a transcript found before, never touches ocr_status and
never empties the row's text: it only records the reason and when to try again.

**The noise filter** (`clean`): bracketed tags ([Music], [Applause], [Laughter], [Foreign], ...),
sound words in parentheses, music-note symbols and snippets that are only filler ("foreign", "um",
"uh", ...) are dropped. Fewer than `MIN_REAL_WORDS` (30) real words left = no transcript (`none`,
"its captions are only music or sound tags (N real words)").
"""

import logging
import os
import re
import threading
import time

log = logging.getLogger("constructicon.transcripts")

DEADLINE_SECONDS = 20.0       # the library's whole run, hard
HTTP_TIMEOUT_SECONDS = 8.0    # each HTTP request inside it (belt and braces)
SLOT_WAIT_SECONDS = 30.0      # waiting for a free parser-child slot before calling it "busy, retry"
MIN_REAL_WORDS = 30
MAX_CHARS = 200_000           # a very long live stream's captions are cut here (noted)
RETRY_BASE_SECONDS = 15 * 60  # first retry after 15 min, doubling...
RETRY_MAX_SECONDS = 24 * 3600  # ...up to a day
MAX_AUTO_ATTEMPTS = 8         # then it stops retrying by itself (Retry OCR still tries again)

QUEUED, FOUND, NONE, RETRY = "queued", "found", "none", "retry"
FIELDS = ("transcript", "transcript_status", "transcript_language", "transcript_language_code",
          "transcript_kind", "transcript_words", "transcript_note", "transcript_attempts",
          "transcript_retry_at", "transcript_checked_at")


def enabled():
    return os.environ.get("CONSTRUCTICON_TRANSCRIPTS", "on").strip().lower() not in ("off", "0", "false", "no")


# --- the noise filter ------------------------------------------------------------------------------

_BRACKETED = re.compile(r"\[[^\]]{0,60}\]")
_SOUND_PARENS = re.compile(
    r"\(\s*(?:music|applause|laughter|laughs|laughing|cheering|cheers|inaudible|silence|noise|foreign|"
    r"engine|engines|revving|crowd)[^)]{0,40}\)", re.IGNORECASE)
_SYMBOLS = re.compile(r"[♪♫♬♩]+|>>+")
_WORD = re.compile(r"[^\W\d_]+")
FILLER = frozenset({"foreign", "um", "umm", "uh", "uhh", "uhm", "hmm", "hm", "mm", "mhm", "ah", "oh",
                    "er", "erm", "eh"})


def clean(snippets):
    """(text, real_word_count) from caption snippets with the noise taken out."""
    kept = []
    for raw in snippets or ():
        t = raw if isinstance(raw, str) else ""
        t = _SYMBOLS.sub(" ", _SOUND_PARENS.sub(" ", _BRACKETED.sub(" ", t)))
        t = " ".join(t.split())
        words = _WORD.findall(t)
        if not words or all(w.lower() in FILLER for w in words):
            continue
        kept.append(t)
    text = " ".join(kept)
    real = sum(1 for w in _WORD.findall(text) if w.lower() not in FILLER)
    return text, real


# --- the fetch, in a child process ---------------------------------------------------------------

def _short(exc):
    text = " ".join((str(exc) or type(exc).__name__).split())
    return text[:200] + ("…" if len(text) > 200 else "")


def _session(timeout):
    """A requests.Session whose every request gets `timeout` unless it names one."""
    import requests

    class _TimeoutSession(requests.Session):
        def request(self, *args, **kwargs):
            kwargs.setdefault("timeout", timeout)
            return super().request(*args, **kwargs)

    return _TimeoutSession()


def _pick(tracks):
    """Manual captions first, then auto-generated; English first within each, else the first."""
    manual = [t for t in tracks if not getattr(t, "is_generated", False)]
    auto = [t for t in tracks if getattr(t, "is_generated", False)]
    for group in (manual, auto):
        if group:
            english = [t for t in group if str(getattr(t, "language_code", "")).lower().startswith("en")]
            return (english or group)[0]
    return None


def _fetch_in_child(video_id, http_timeout):
    """The library's work, run under the deadline (a parser child, or a thread). Never raises: a
    plain dict comes back ({"outcome": "found", "snippets", "language", "language_code",
    "generated"} or {"outcome": "none" | "retry", "reason"}), so nothing unpicklable crosses."""
    try:
        import youtube_transcript_api as yta
    except ImportError as e:  # silent-ok: reported as the retry reason (an image without the package)
        return {"outcome": RETRY, "reason": f"youtube-transcript-api isn't installed ({_short(e)})"}

    def is_(exc, *names):
        classes = tuple(c for c in (getattr(yta, n, None) for n in names) if isinstance(c, type))
        return bool(classes) and isinstance(exc, classes)

    try:
        api = yta.YouTubeTranscriptApi(http_client=_session(http_timeout))
        chosen = _pick(list(api.list(video_id)))
        if chosen is None:
            return {"outcome": NONE, "reason": "YouTube has no captions for this video"}
        fetched = chosen.fetch()
        return {"outcome": FOUND, "snippets": [getattr(s, "text", "") for s in fetched],
                "language": str(getattr(chosen, "language", "") or ""),
                "language_code": str(getattr(chosen, "language_code", "") or ""),
                "generated": bool(getattr(chosen, "is_generated", False))}
    except Exception as e:  # classified below; every outcome is a reason, never a silent pass
        if is_(e, "TranscriptsDisabled"):
            return {"outcome": NONE, "reason": "captions are turned off for this video on YouTube"}
        if is_(e, "NoTranscriptFound"):
            return {"outcome": NONE, "reason": "YouTube has no captions for this video"}
        if is_(e, "VideoUnavailable", "InvalidVideoId"):
            return {"outcome": NONE, "reason": "the video is unavailable on YouTube (deleted or private?)"}
        if is_(e, "AgeRestricted"):
            return {"outcome": NONE, "reason": "age-restricted: YouTube gives no captions without signing in"}
        if is_(e, "VideoUnplayable"):
            return {"outcome": NONE, "reason": f"YouTube won't play the video ({_short(e).splitlines()[0][:120]})"}
        if is_(e, "IpBlocked"):  # HTTP 429 on the caption download, or a captcha page
            return {"outcome": RETRY, "blocked": True,
                    "reason": "YouTube is rate-limiting caption requests from this server's address (HTTP 429)"}
        if is_(e, "RequestBlocked"):
            return {"outcome": RETRY, "blocked": True,
                    "reason": "YouTube is blocking caption requests from this server (bot check)"}
        if is_(e, "PoTokenRequired"):
            return {"outcome": RETRY, "blocked": True,
                    "reason": "YouTube wants a proof-of-origin token for these captions, which the library can't give"}
        if is_(e, "YouTubeRequestFailed"):
            return {"outcome": RETRY, "reason": "YouTube answered with an error"}
        name = type(e).__name__
        if name in ("ConnectionError", "ConnectTimeout", "ReadTimeout", "Timeout", "SSLError", "ProxyError"):
            return {"outcome": RETRY, "reason": f"couldn't reach YouTube ({name})"}
        return {"outcome": RETRY, "reason": f"the caption fetch failed ({name}: {_short(e)[:120]})"}


def _thread_deadline(fn, args, seconds):
    """No parser sandbox here (Windows, CONSTRUCTICON_PARSE_SANDBOX=off): run in a daemon thread
    and stop waiting at the deadline. The thread can't be killed; it is abandoned (its result,
    if it ever comes, is dropped)."""
    box = {}

    def work():
        try:
            box["result"] = fn(*args)
        except Exception as e:  # silent-ok: handed back below as the retry reason
            box["error"] = e

    t = threading.Thread(target=work, name="transcript-fetch", daemon=True)
    t.start()
    t.join(seconds)
    if t.is_alive():
        return {"outcome": RETRY, "reason": f"timed out after {seconds:g} s"}
    if "error" in box:
        return {"outcome": RETRY, "reason": f"the caption fetch failed ({_short(box['error'])})"}
    return box.get("result")


def run_with_deadline(fn, args, seconds):
    """`fn(*args)` with a hard deadline: a parser child that is killed at `seconds` (the sandbox),
    else a thread that is abandoned at `seconds`. A deadline or a limit is a retry outcome dict."""
    from . import parse_limits
    if not parse_limits.sandbox_available():
        return _thread_deadline(fn, args, seconds)
    try:
        return parse_limits.call(fn, *args, kind=parse_limits.BACKGROUND, what="transcript fetch",
                                 wait=SLOT_WAIT_SECONDS, seconds=seconds)
    except parse_limits.LimitExceeded as e:
        if e.kind == "time":
            return {"outcome": RETRY, "reason": f"timed out after {seconds:g} s"}
        return {"outcome": RETRY, "reason": e.message}
    except Exception as e:  # silent-ok: a child that died oddly is still just "try again later"
        return {"outcome": RETRY, "reason": f"the caption fetch failed ({_short(e)})"}


def fetch(video_id, deadline=None):
    """One polite, deadline-bound attempt at a video's captions: an outcome dict (see
    _fetch_in_child). Waiting for the polite turn is bounded on its own (safe_fetch's
    TURN_WAIT_SECONDS); the library itself gets `deadline` seconds (DEADLINE_SECONDS)."""
    from . import safe_fetch
    seconds = DEADLINE_SECONDS if deadline is None else deadline
    try:
        with safe_fetch.polite_turn():
            out = run_with_deadline(_fetch_in_child, (video_id, min(HTTP_TIMEOUT_SECONDS, seconds)), seconds)
    except safe_fetch.TimedOut as e:
        out = {"outcome": RETRY, "reason": str(e)}
    if not isinstance(out, dict) or out.get("outcome") not in (FOUND, NONE, RETRY):
        out = {"outcome": RETRY, "reason": "the caption fetch gave no answer"}
    if out.get("blocked"):
        _last_blocked[0] = time.monotonic()  # the background pass stops for now (work_due)
    return out


_last_blocked = [0.0]  # when YouTube last refused us (blocked / rate-limited), monotonic seconds


# --- outcome -> type_metadata fields ---------------------------------------------------------------

def _when(seconds):
    if seconds >= 3600:
        return f"{seconds / 3600:g} h"
    return f"{max(1, round(seconds / 60))} min"


def fields_for(outcome, meta, now=None):
    """The type_metadata changes for one attempt's outcome ({key: value}; None removes the key)."""
    now = time.time() if now is None else now
    meta = meta if isinstance(meta, dict) else {}
    out = {"transcript_checked_at": round(now)}
    kind = outcome.get("outcome")
    if kind == FOUND:
        text, words = clean(outcome.get("snippets"))
        if words >= MIN_REAL_WORDS:
            note = None
            if len(text) > MAX_CHARS:
                text = text[:MAX_CHARS].rsplit(" ", 1)[0]
                note = f"the transcript is long: only its first {MAX_CHARS:,} characters are kept"
            out.update({"transcript_status": FOUND, "transcript": text,
                        "transcript_language": outcome.get("language") or None,
                        "transcript_language_code": outcome.get("language_code") or None,
                        "transcript_kind": "auto" if outcome.get("generated") else "manual",
                        "transcript_words": words, "transcript_note": note,
                        "transcript_attempts": None, "transcript_retry_at": None})
            return out
        kind = NONE
        outcome = {"reason": f"its captions are only music or sound tags ({words} real word"
                             f"{'' if words == 1 else 's'}, fewer than {MIN_REAL_WORDS})"}
    if kind == NONE:
        out.update({"transcript_status": NONE, "transcript": None, "transcript_language": None,
                    "transcript_language_code": None, "transcript_kind": None, "transcript_words": None,
                    "transcript_note": outcome.get("reason") or "no captions",
                    "transcript_attempts": None, "transcript_retry_at": None})
        return out
    # RETRY: keep whatever transcript is stored; say why, and when it tries again
    reason = outcome.get("reason") or "the caption fetch failed"
    try:
        attempts = int(meta.get("transcript_attempts") or 0) + 1
    except (TypeError, ValueError):  # silent-ok: a garbled counter restarts the backoff
        attempts = 1
    out["transcript_status"] = RETRY
    out["transcript_attempts"] = attempts
    if attempts >= MAX_AUTO_ATTEMPTS:
        out["transcript_retry_at"] = None
        out["transcript_note"] = (f"{reason}; stopped retrying by itself after {attempts} tries "
                                  "(Retry OCR tries again)")
    else:
        wait = min(RETRY_BASE_SECONDS * 2 ** (attempts - 1), RETRY_MAX_SECONDS)
        out["transcript_retry_at"] = round(now + wait)
        out["transcript_note"] = f"{reason}; will try again in about {_when(wait)}"
    return out


def fetch_fields(row, video_id, now=None):
    """The type's text_fetch_fn body: one attempt for `row` (whose video is `video_id`), as
    type_metadata changes. None when transcripts are off (nothing fetched, nothing changed)."""
    if not enabled() or not video_id:
        return None
    outcome = fetch(video_id)
    fields = fields_for(outcome, row.get("type_metadata") or {}, now)
    if outcome.get("outcome") == RETRY:
        log.warning("transcript for %s: %s", row.get("slug"), fields.get("transcript_note"))
    return fields


# --- the background pass (web's _transcript_loop) -----------------------------------------------

def _fetch_types():
    from . import object_types
    return [k for k, s in object_types.OBJECT_TYPES.items() if getattr(s, "text_fetch_fn", None) is not None]


def refresh(slug):
    """Fetch one row's network text now and fold it into its stored text (no OCR re-run). Returns
    the transcript status written, or None when nothing was attempted (off, gone, redacted)."""
    from . import db, object_types
    row = db.get_by_slug(slug)
    if row is None or row.get("redacted"):
        return None
    spec = object_types.get_object_type(row.get("media_type"))
    fn = getattr(spec, "text_fetch_fn", None)
    if fn is None:
        return None
    fields = fn(row)
    if not fields:
        return None
    db.set_text_source_fields(slug, fields, resettle=True)
    return fields.get("transcript_status")


def work_due(limit=10, now=None):
    """One pass: up to `limit` rows queued for a first attempt or due a retry, one at a time
    (each fetch takes the polite turn). Stops early when YouTube blocks or rate-limits us, so a
    backlog doesn't keep knocking (the row that hit it is retried on its own backoff; the rest
    wait for the next pass). {"tried": n, "found": n, "none": n, "retry": n, "stopped": reason}."""
    from . import db
    counts = {"tried": 0, FOUND: 0, NONE: 0, RETRY: 0, "stopped": None}
    if not enabled():
        return counts
    types = _fetch_types()
    if not types:
        return counts
    for slug in db.list_due_text_fetches(types, time.time() if now is None else now, limit):
        blocked_before = _last_blocked[0]
        status = refresh(slug)
        if status is None:
            continue
        counts["tried"] += 1
        if status in counts:
            counts[status] += 1
        if _last_blocked[0] != blocked_before:
            counts["stopped"] = "YouTube is blocking or rate-limiting caption requests; the rest wait for the next pass"
            break
    return counts


def describe(meta):
    """The "Captions" line for the properties panel, or None when there's nothing to say."""
    meta = meta if isinstance(meta, dict) else {}
    status = meta.get("transcript_status")
    note = meta.get("transcript_note")
    if status == FOUND:
        lang = meta.get("transcript_language") or meta.get("transcript_language_code") or "unknown language"
        kind = "auto-generated" if meta.get("transcript_kind") == "auto" else "manual"
        if "auto-generated" in str(lang):
            kind = ""
        words = meta.get("transcript_words")
        bits = [f"{lang}" + (f" ({kind})" if kind else "")]
        if isinstance(words, int):
            bits.append(f"{words:,} words")
        return ", ".join(bits) + (f"; {note}" if note else "")
    if status == NONE:
        return f"none: {note or 'no captions'}"
    if status == RETRY:
        held = " (an earlier transcript is kept)" if meta.get("transcript") else ""
        return f"not fetched yet: {note or 'will try again'}{held}"
    if status == QUEUED:
        return "waiting to be fetched" if enabled() else "not fetched: transcripts are turned off on this install"
    return None
