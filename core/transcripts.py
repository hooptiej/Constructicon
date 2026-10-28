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
- an off switch: `CONSTRUCTICON_TRANSCRIPTS=off` (default on; the home prod install runs with it
  off until YouTube stops answering the NAS with HTTP 429, owner on PR #756). Off = nothing is
  fetched, a new video is marked `queued` (so turning it on later works the backlog), stored
  transcripts stay searchable, and the "Captions" line says "transcripts are turned off on this
  install". scripts/_testenv.py sets it, so no test reaches YouTube;
- web only (owner, PR #756): the MCP process never fetches (`queue_only`, CONSTRUCTICON_ROLE=mcp);
  its OCR runs mark the video `queued` for web's pass.

**What is stored** (type_metadata, all `transcript*` keys):
- `transcript_status`: `found` | `none` (settled: no captions, captions off, only noise, the video
  unavailable) | `retry` (a network error, a timeout, YouTube blocking us: tried again later with
  backoff, never `failed`) | `queued` (the backfill: not tried yet);
- `transcript` (the cleaned text, only when found), `transcript_cues` (#789: where each kept caption
  starts, as compact `[start_seconds, char_offset]` pairs into `transcript`, so a position in the
  text maps to a moment in the video: `cite`), `transcript_language` ("English
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

**Timestamps (#789).** Each kept caption's start time is kept beside the text (`transcript_cues`),
from the library's snippets (`start`) or from a WebVTT / SRT file (`parse_vtt`, for subtitles fetched
some other way, e.g. yt-dlp, handed in through MCP `constructicon_update(fields={"transcript_vtt":
...})`, `fields_from_vtt`). `cite(row, offset)` maps a character position in the row's
`extracted_text` (or in `transcript` itself) to `{timestamp_seconds, timestamp_url}` (a `&t=<s>s`
link); a row stored before #789, or a position outside the transcript, gives nulls.
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
FIELDS = ("transcript", "transcript_cues", "transcript_status", "transcript_language", "transcript_language_code",
          "transcript_kind", "transcript_words", "transcript_note", "transcript_attempts",
          "transcript_retry_at", "transcript_checked_at")


def enabled():
    return os.environ.get("CONSTRUCTICON_TRANSCRIPTS", "on").strip().lower() not in ("off", "0", "false", "no")


OFF_NOTE = "transcripts are turned off on this install"


def queue_only():
    """True in the MCP process (CONSTRUCTICON_ROLE=mcp, set by mcp_server/server.py; owner, PR #756:
    "web loop only"). It never fetches: an OCR run there marks the video `queued`, and web's
    background pass does the fetching. The same switch core/captions.py uses (#549)."""
    return os.environ.get("CONSTRUCTICON_ROLE", "").strip().lower() == "mcp"


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
    text, real, _cues = clean_with_cues(snippets)
    return text, real


def _start_of(value):
    """A cue start in seconds (a finite number >= 0), else None."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return round(float(value), 1) if 0 <= float(value) < 10 ** 7 else None


def clean_with_cues(snippets, starts=None):
    """(text, real_word_count, cues) from caption snippets with the noise taken out. `starts` (#789):
    each snippet's start time in seconds (same order; None or missing = unknown). `cues` is
    [[start_seconds, char_offset_in_text], ...] for the kept snippets with a known start, in order
    (a start earlier than the previous kept one is dropped, so the list stays sorted)."""
    kept, cues, at = [], [], 0
    starts = list(starts or ())
    for i, raw in enumerate(snippets or ()):
        t = raw if isinstance(raw, str) else ""
        t = _SYMBOLS.sub(" ", _SOUND_PARENS.sub(" ", _BRACKETED.sub(" ", t)))
        t = " ".join(t.split())
        words = _WORD.findall(t)
        if not words or all(w.lower() in FILLER for w in words):
            continue
        if kept:
            at += 1  # the joining space
        start = _start_of(starts[i]) if i < len(starts) else None
        if start is not None and (not cues or start >= cues[-1][0]):
            cues.append([start, at])
        kept.append(t)
        at += len(t)
    text = " ".join(kept)
    real = sum(1 for w in _WORD.findall(text) if w.lower() not in FILLER)
    return text, real, cues


# --- WebVTT / SRT subtitles (#789) -------------------------------------------------------------------

_CUE_TIME = re.compile(r"^\s*((?:\d+:)?\d{1,2}:\d{2}[.,]\d{1,3})\s*-->\s*((?:\d+:)?\d{1,2}:\d{2}[.,]\d{1,3})")
_INLINE_TAG = re.compile(r"<[^>]{0,80}>")
VTT_MAX_BYTES = 5 * 1024 * 1024  # a subtitle file is text; 5 MB is hours of captions


def _seconds(stamp):
    parts = stamp.replace(",", ".").split(":")
    secs = float(parts[-1])
    mins = int(parts[-2]) if len(parts) >= 2 else 0
    hours = int(parts[-3]) if len(parts) >= 3 else 0
    return hours * 3600 + mins * 60 + secs


def parse_vtt(text):
    """[(start_seconds, caption text), ...] from a WebVTT (or SRT) file's text, in file order. Inline
    tags (<c>, <00:00:01.234>, <v Name>) are removed, and a line one of the last few cues already
    showed is dropped: YouTube's auto-generated VTT (yt-dlp) repeats the previous line at the top of
    every cue, so without this every sentence would appear twice. Blocks with no timing line (the
    WEBVTT header, NOTE / STYLE / REGION) are skipped, and so are SRT's counter lines. Pure."""
    if not isinstance(text, str):
        return []
    out, recent = [], []
    for block in re.split(r"\r?\n[ \t]*\r?\n", text.replace("﻿", "")):
        lines = [ln for ln in block.splitlines() if ln.strip()]
        timing = next((i for i, ln in enumerate(lines) if _CUE_TIME.match(ln)), None)
        if timing is None:
            continue
        start = _seconds(_CUE_TIME.match(lines[timing]).group(1))
        said = []
        for ln in lines[timing + 1:]:
            line = " ".join(_INLINE_TAG.sub("", ln).split())
            if not line or line in recent:
                continue
            said.append(line)
            recent = (recent + [line])[-3:]
        if said:
            out.append((start, " ".join(said)))
    return out


def fields_from_vtt(text, now=None):
    """The type_metadata a transcript from a subtitle file gives: what a found fetch writes (status
    found, transcript, transcript_cues; kind auto when the file carries YouTube's word-timing tags,
    else manual; no language). InvalidInput (400 `bad_transcript`) when it is empty, too big, holds no
    timed captions, or too few real words to count as speech (the noise filter's rule): nothing is
    guessed or stored then."""
    from .errors import InvalidInput
    if not isinstance(text, str) or not text.strip():
        raise InvalidInput("transcript_vtt must be the subtitle file's text (WebVTT or SRT). Nothing was changed.",
                           code="bad_transcript")
    if len(text.encode("utf-8", "replace")) > VTT_MAX_BYTES:
        raise InvalidInput(f"transcript_vtt is over {VTT_MAX_BYTES // 1048576} MB; a subtitle file is never that "
                           "big. Nothing was changed.", code="bad_transcript")
    cues = parse_vtt(text)
    if not cues:
        raise InvalidInput("transcript_vtt has no timed captions (no '00:00:01.000 --> 00:00:04.000' lines). "
                           "Send the .vtt or .srt file's text. Nothing was changed.", code="bad_transcript")
    outcome = {"outcome": FOUND, "snippets": [c[1] for c in cues], "starts": [c[0] for c in cues],
               "language": None, "language_code": None, "generated": bool(re.search(r"<\d{2}:\d{2}", text))}
    fields = fields_for(outcome, {}, now)
    if fields.get("transcript_status") != FOUND:
        raise InvalidInput(f"transcript_vtt holds no real speech: {fields.get('transcript_note')}. Nothing was changed.",
                           code="bad_transcript")
    return fields


# --- citing a moment (#789) -------------------------------------------------------------------------

def _cues_of(meta):
    cues = meta.get("transcript_cues") if isinstance(meta, dict) else None
    if not isinstance(cues, list):
        return []
    return [c for c in cues if isinstance(c, (list, tuple)) and len(c) == 2
            and _start_of(c[0]) is not None and isinstance(c[1], int) and not isinstance(c[1], bool)]


def has_cues(row):
    """True when the row carries transcript cue times (a transcript stored since #789)."""
    return bool(_cues_of((row or {}).get("type_metadata") or {}))


def transcript_start(row):
    """Where the transcript sits inside the row's extracted_text (its first character), or None when
    the text doesn't hold it (not folded in yet, an old row)."""
    meta = (row or {}).get("type_metadata") or {}
    transcript = meta.get("transcript") if isinstance(meta, dict) else None
    if not isinstance(transcript, str) or not transcript.strip():
        return None
    at = (row.get("extracted_text") or "").find(transcript.strip())
    return at if at >= 0 else None


def timestamp_url(row, seconds):
    """The video's watch URL starting at `seconds` (`&t=<s>s`), or None when it isn't a YouTube video."""
    from .object_types.youtube import extract_youtube_id
    vid = extract_youtube_id((row or {}).get("external_url"))
    if not vid or seconds is None:
        return None
    return f"https://www.youtube.com/watch?v={vid}&t={int(seconds)}s"


def cite(row, offset, *, within="extracted_text"):
    """{timestamp_seconds, timestamp_url} for character `offset` of the row's extracted_text (or of
    its `transcript`, within="transcript"): the start of the caption that position falls in. Both
    None when the row has no cue times (stored before #789, a type without transcripts) or the
    position is outside the transcript (in the description or the thumbnail text)."""
    nothing = {"timestamp_seconds": None, "timestamp_url": None}
    meta = (row or {}).get("type_metadata") or {}
    cues = _cues_of(meta)
    if not cues or offset is None:
        return nothing
    if within == "transcript":
        rel = offset
    else:
        base = transcript_start(row)
        if base is None:
            return nothing
        rel = offset - base
    if rel < 0 or rel >= len((meta.get("transcript") or "").strip()):
        return nothing
    best = cues[0][0]
    for start, at in cues:
        if at > rel:
            break
        best = start
    return {"timestamp_seconds": best, "timestamp_url": timestamp_url(row, best)}


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
                "starts": [getattr(s, "start", None) for s in fetched],  # #789: kept as transcript_cues
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
        text, words, cues = clean_with_cues(outcome.get("snippets"), outcome.get("starts"))
        if words >= MIN_REAL_WORDS:
            note = None
            if len(text) > MAX_CHARS:
                text = text[:MAX_CHARS].rsplit(" ", 1)[0]
                note = f"the transcript is long: only its first {MAX_CHARS:,} characters are kept"
                cues = [c for c in cues if c[1] < len(text)]
            out.update({"transcript_status": FOUND, "transcript": text, "transcript_cues": cues or None,
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
        out.update({"transcript_status": NONE, "transcript": None, "transcript_cues": None, "transcript_language": None,
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
    type_metadata changes. Nothing is fetched:
    - in the MCP process (queue_only): the video is marked `queued` (also on a Retry OCR of a
      found / none / retry row, so the web pass asks again; a stored transcript is kept);
    - with transcripts turned off: a video with no status yet is marked `queued`, so turning them on
      later works the backlog; any other status is left as it is (None)."""
    if not video_id:
        return None
    meta = row.get("type_metadata") or {}
    meta = meta if isinstance(meta, dict) else {}
    if queue_only():
        return {"transcript_status": QUEUED, "transcript_retry_at": None, "transcript_attempts": None,
                "transcript_note": None}
    if not enabled():
        return None if meta.get("transcript_status") else {"transcript_status": QUEUED}
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
    on = enabled()
    if not on and status in (None, QUEUED, RETRY):  # owner, PR #756: the off state is said, not silent
        last = f" (last try: {note})" if status == RETRY and note else ""
        held = "; an earlier transcript is kept" if meta.get("transcript") else ""
        return f"not fetched: {OFF_NOTE}{last}{held}"
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
        return "waiting to be fetched" + (" again (an earlier transcript is kept)" if meta.get("transcript") else "")
    return None
