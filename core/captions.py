"""Auto-captioning via a local Ollama vision model (moondream) on the home
GPU — issue #239.

Runs once a capture_events row's file is saved, same background-task shape
as core/ocr.py: best-effort, never fails the upload, writes its result to a
`type_metadata` key (`auto_caption`) rather than any owner-facing column.
The caption is a *suggestion* for the owner to review and manually promote
into the real description — it is never auto-applied to `description`.

What gets captioned: whatever ObjectTypeSpec.caption_capable says (see
core/object_types/__init__.py) — the same rendered preview image OCR
already runs against (an uploaded image itself, or the generated
thumbnail/video frame for everything else). STL is explicitly excluded
there: abstract wireframe renders produced degenerate repeated-punctuation
garbage in testing, a hard failure rather than a weak caption.

Concurrency discipline (deliberately NOT the OCR semaphore — different
resource, different failure mode): CAPTION_LOCK serializes every model call
process-wide, one image at a time. History: #239 restarted the Ollama
container after every image, based on a 2026-09-09 observation on a
separate deployment that a long-lived Ollama didn't release resources
between calls. On 2026-09-30 that per-image restart cycled the GPU container
~180 times during a bulk import and hard-crashed the whole NAS. Measured on
this box right after (70 images, ~140 calls, no restarts): with
keep_alive:0 (now the primary unload, not insurance) GPU memory returns to
1-4 MiB after every call, container RAM stays 1.1-1.65 GB with no upward
trend, and there were no GPU errors. New rule (#454):
no routine restarts; restart only on model-call failure, guarded by a
cooldown (600s) and circuit breaker that opens after 3 consecutive
failures. A process restart resets the breaker.

#643: this container no longer holds the Docker socket (it parses every
upload, so a parser bug plus the socket was root on the NAS). The restart is
a request to the single-purpose restart sidecar (ops/ollama-restarter/), which
holds the socket, restarts only the container named in ITS config, needs a
shared secret and rate-limits itself. Configure with CAPTION_RESTART_URL
(e.g. http://ollama-restarter:8080/restart) and CAPTION_RESTART_TOKEN_FILE.
Unset, unreachable or rate-limited = no safety valve this time: logged, the
caption pipeline carries on (keep_alive:0 is the primary unload anyway).
The #454 RAM-ceiling check read container stats over the socket; the sidecar
deliberately makes no other Docker call, so that check is gone (RAM was
measured flat at 1.1-1.65 GB with keep_alive:0).

Network: the app container reaches Ollama at CAPTION_OLLAMA_URL (default
http://ollama:11434 — Docker DNS on Ollama's own compose network, which the
app container joins; the ipvlan LAN network the app otherwise lives on
can't reach the host's published port).

Defaults below (prompt, temperature, token cap) were settled with the admin
pane's live tuning panel (POST /api/captions/test) against real images —
see the PR for #239 for the runs that picked them.
"""

import base64
import json
import logging
import os
import threading
import time
import urllib.error
import urllib.request

from . import actor as actor_ctx, besteffort, db, object_types, policy, storage, thumbnails
from .errors import Conflict

log = logging.getLogger("constructicon.captions")

OLLAMA_URL = os.environ.get("CAPTION_OLLAMA_URL", "http://ollama:11434").rstrip("/")
OLLAMA_MODEL = os.environ.get("CAPTION_OLLAMA_MODEL", "moondream")
# #643: the restart sidecar. Which container it restarts is the SIDECAR's config, not ours.
RESTART_URL = os.environ.get("CAPTION_RESTART_URL", "").strip()
RESTART_TOKEN_HEADER = "X-Restart-Token"
# Set CAPTION_DISABLED=1 to skip scheduling captions entirely (e.g. a deploy
# with no Ollama reachable) rather than logging a failure per upload.
DISABLED = os.environ.get("CAPTION_DISABLED", "") not in ("", "0", "false", "no")

# #454: Guarded restart parameters. Cooldown prevents thrashing restarts (the
# sidecar has its own rate limit on top, #643). Circuit breaker opens after max
# consecutive failures.
def _parse_env_int(var, default):
    """Safely parse an env var to int, fall back to default on garbage."""
    try:
        return int(os.environ.get(var, default))
    except (ValueError, TypeError) as e:
        besteffort.warn(log, f"captions: env {var} is not an integer, using the default", e,
                        value=os.environ.get(var), default=default)
        return default

CAPTION_RESTART_COOLDOWN_SECONDS = _parse_env_int("CAPTION_RESTART_COOLDOWN_SECONDS", 600)  # #454
CAPTION_MAX_CONSECUTIVE_FAILURES = _parse_env_int("CAPTION_MAX_CONSECUTIVE_FAILURES", 3)  # #454

# Tuned defaults — see module docstring. Exposed via GET /api/captions/defaults
# so the admin tuning panel starts from what production actually uses.
DEFAULT_PROMPT = (
    "Describe only what is physically visible in this image: the main objects, "
    "the scene or setting, and materials or colors. One or two short plain "
    "sentences. Do not guess at anything you cannot clearly see. Do not read, "
    "quote, or describe any text, labels, or writing in the image."
)
# Settled 2026-09-09 with the tuning panel over a 4x3 temperature x cap grid
# on two real photos (see PR for #239): greedy decoding (0.0) gave the most
# detailed *and* most stable captions — 0.1 (the issue's starting point)
# was nearly as good but drifted at higher caps, 0.3 started inventing
# objects, 0.6 was garbage. No run came anywhere near 120 tokens (the model
# stops on its own at ~15-40), so the cap is purely a runaway guard, not a
# length target. Note 0.0 means "Regenerate" on the detail page returns the
# same caption for the same image — bump to 0.1 if variety matters more.
DEFAULT_TEMPERATURE = 0.0
DEFAULT_NUM_PREDICT = 120

# #246/#250: DEFAULT_PROMPT's constraints ("do not guess", "do not read
# text", stay to one or two sentences) occasionally make the model pick the
# end-of-output token as its very first token on an otherwise describable
# image, returning empty — confirmed via the tuning panel that this is the
# prompt's doing, not the image: a bare "Describe this image." at the same
# temperature 0.0 produced a real caption immediately on the same photo.
#
# STEPS is the one shared escalation ladder: index 0 is always the strict
# default; each step after it loosens the prompt first, then turns up the
# heat, in small linear moves that stay well under the ~0.3 mark where
# #239's tuning saw invented objects. Two jobs use the same ladder —
# run_caption() cascades through it automatically on an empty response
# (or, #262, a letter-free degenerate one — see _is_garbage) in the
# upload/import pipeline, and a manual "Regenerate" click advances
# exactly one step at a time (see api_retry_caption in web/app.py) so
# repeated clicks give real variety instead of repeating the same greedy
# default. 3-5 steps total, then wraps back to 0 — not a pyramid.
FALLBACK_PROMPT = "Describe this image."
STEPS = (
    (DEFAULT_PROMPT, DEFAULT_TEMPERATURE),
    (FALLBACK_PROMPT, DEFAULT_TEMPERATURE),  # loosen the prompt first
    (FALLBACK_PROMPT, 0.15),  # then turn up the heat, a little
    (FALLBACK_PROMPT, 0.25),  # ...and a little more
)

GENERATE_TIMEOUT_SECONDS = 180  # first call after a restart includes loading the model onto the GPU
RESTART_TIMEOUT_SECONDS = 60
READY_POLL_SECONDS = 90  # how long to wait for Ollama to answer /api/tags again after a restart

CAPTION_LOCK = threading.Lock()

# #454: Module state (only touched while holding CAPTION_LOCK)
_last_restart_at = None  # time.monotonic() of the last successful restart
_consecutive_failures = 0  # count of consecutive caption call failures
_breaker_reason = None  # str describing why the circuit breaker is open, or None if closed

METADATA_KEY = "auto_caption"
STATUS_KEY = "auto_caption_status"  # "done" | "failed" (absent = never attempted)
STEP_KEY = "auto_caption_step"  # #250: index into STEPS last attempted/used (absent = step 0)

# #251: separate from STEP_KEY, which gets overwritten by every subsequent
# Regenerate click — these record which ladder step actually produced the
# text the owner chose to use, at the moment "Use this caption" is clicked
# (see api_use_caption in web/app.py), so it survives further regenerating
# and is still on record after the fact. Absent = no caption has ever been
# adopted into description for this row.
DESCRIPTION_STEP_KEY = "description_caption_step"
DESCRIPTION_STEP_LABEL_KEY = "description_caption_step_label"  # precomputed describe_step() text, for templates that shouldn't need to know STEPS
DESCRIPTION_MODEL_KEY = "description_caption_model"
DESCRIPTION_USED_AT_KEY = "description_caption_used_at"

# #542 (owner decision 2026-10-18): a caption comes with a SHORT TITLE. The suggestion lives beside
# the caption suggestion until the owner (or an agent with accept=true) takes it into the item's
# `short_title` column, the name in core/item_title.py's chain.
TITLE_KEY = "auto_caption_title"                  # the suggested short title
TITLE_MODEL_KEY = "auto_caption_title_model"      # who suggested it (a local model, "mcp-agent", "owner")
TITLE_DISMISSED_KEY = "auto_caption_title_dismissed"
TITLE_STATUS_KEY = "short_title_proposal_status"  # a backfill proposal: queued | done | failed
SHORT_TITLE_MODEL_KEY = "short_title_model"       # who wrote the accepted short_title (absent = by hand)
OWNER_MODEL = "owner"  # a caption or title the owner wrote or edited on the caption review page
# The local model is NOT asked for a title with a prompt of its own: measured 2026-10-25 on
# constructicon-test (moondream via Ollama, 6 real photos x 4 prompts), every constrained prompt
# ("in five words", "a short phrase", "the main subject in a few words") came back starting
# mid-word ("urn of paint...", "ids: HOOPT 13", "iced tea" for a knife) on every photo, and only the
# plain "Describe this image." gave whole sentences. So the short title is DERIVED from a caption
# (title_from_caption): the backfill asks with FALLBACK_PROMPT and derives one from the answer.
TITLE_STEPS = ((FALLBACK_PROMPT, DEFAULT_TEMPERATURE), (FALLBACK_PROMPT, 0.15))
TITLE_MAX_WORDS = 8


def local_models():
    """The captioners that run on this install's own GPU. Their suggestions are drafts: a new
    caption may replace one freely (#787); anything else (an agent's, the owner's) is protected."""
    return {"moondream", OLLAMA_MODEL}


def is_local_model(model):
    return bool(model) and model in local_models()


def describe_step(step_index):
    """Short human label for STEPS[step_index] — 'default (temp 0.0)' for
    step 0, otherwise 'loosened prompt, temp X' — used wherever the owner
    needs to see which rung of the ladder produced a caption (#251)."""
    prompt, temperature = STEPS[step_index % len(STEPS)]
    kind = "default prompt" if prompt == DEFAULT_PROMPT else "loosened prompt"
    return f"{kind}, temp {temperature}"


# --- Ollama HTTP ---

def _ollama_get(path, timeout=5):
    with urllib.request.urlopen(f"{OLLAMA_URL}{path}", timeout=timeout) as resp:
        return resp.status, resp.read()


def _ollama_post_json(path, payload, timeout):
    req = urllib.request.Request(
        f"{OLLAMA_URL}{path}",
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


def is_ollama_up(timeout=3):
    try:
        status, _ = _ollama_get("/api/tags", timeout=timeout)
        return status == 200
    except Exception as e:
        besteffort.warn(log, "captions: Ollama health probe (treated as down)", e)
        return False


def generate_caption(image_path, prompt=None, temperature=None, num_predict=None):
    """One model call. Returns (caption_text, elapsed_seconds). Raises on any
    failure — callers decide whether that's fatal (the tuning endpoint
    surfaces it, the pipeline records it as a failed attempt).

    Does NOT take CAPTION_LOCK or restart anything itself — see
    caption_once() for the full one-image cycle."""
    with open(image_path, "rb") as f:
        image_b64 = base64.b64encode(f.read()).decode("ascii")
    payload = {
        "model": OLLAMA_MODEL,
        "prompt": prompt if prompt is not None else DEFAULT_PROMPT,
        "images": [image_b64],
        "stream": False,
        # Unload the model as soon as the response is done — the primary
        # unload now, not insurance. Releases GPU memory to 1-4 MiB.
        "keep_alive": 0,
        "options": {
            "temperature": DEFAULT_TEMPERATURE if temperature is None else float(temperature),
            "num_predict": DEFAULT_NUM_PREDICT if num_predict is None else int(num_predict),
        },
    }
    started = time.monotonic()
    data = _ollama_post_json("/api/generate", payload, timeout=GENERATE_TIMEOUT_SECONDS)
    elapsed = time.monotonic() - started
    text = (data.get("response") or "").strip()
    return text, elapsed


# --- #643: the restart sidecar (ops/ollama-restarter/) ---

class RestartUnavailable(RuntimeError):
    """The safety valve can't be used right now (not configured, sidecar unreachable,
    rate-limited, or our secret refused). Soft: logged, no breaker trip."""


def _restart_token():
    """The sidecar's shared secret from CAPTION_RESTART_TOKEN_FILE (preferred) or
    CAPTION_RESTART_TOKEN, or '' when neither is set. Never logged."""
    path = os.environ.get("CAPTION_RESTART_TOKEN_FILE", "").strip()
    if path:
        try:
            with open(path, "r", encoding="utf-8") as f:
                return f.read().strip()
        except OSError as e:
            besteffort.warn(log, "captions: reading CAPTION_RESTART_TOKEN_FILE", e, path=path)
            return ""
    return os.environ.get("CAPTION_RESTART_TOKEN", "").strip()


def restart_available():
    """True when the restart sidecar is configured (URL + secret). No network call:
    the admin status reads it on every poll."""
    return bool(RESTART_URL) and bool(_restart_token())


def _request_sidecar_restart():
    """One authenticated POST to the sidecar. Returns the sidecar's JSON answer on success.
    Raises RestartUnavailable (soft) or RuntimeError (the sidecar tried and Docker failed)."""
    token = _restart_token()
    if not (RESTART_URL and token):
        raise RestartUnavailable("no restart sidecar configured (CAPTION_RESTART_URL / CAPTION_RESTART_TOKEN_FILE)")
    req = urllib.request.Request(RESTART_URL, data=b"", method="POST",
                                 headers={RESTART_TOKEN_HEADER: token})
    try:
        with urllib.request.urlopen(req, timeout=RESTART_TIMEOUT_SECONDS) as resp:
            return json.loads(resp.read().decode("utf-8") or "{}")
    except urllib.error.HTTPError as e:
        body = e.read()[:300]
        if e.code == 429:
            raise RestartUnavailable(f"restart sidecar rate-limited the request (Retry-After {e.headers.get('Retry-After')}s)")
        if e.code in (401, 403):
            raise RestartUnavailable(f"restart sidecar refused our secret ({e.code}): check CAPTION_RESTART_TOKEN_FILE matches the sidecar's")
        raise RuntimeError(f"restart sidecar answered {e.code}: {body!r}")
    except (urllib.error.URLError, OSError) as e:
        raise RestartUnavailable(f"restart sidecar unreachable at {RESTART_URL}: {e!r}")


def restart_ollama_container():
    """Ask the restart sidecar to bounce Ollama, then block until Ollama answers
    /api/tags again. Returns the number of seconds the whole bounce took.
    Raises RestartUnavailable when the valve can't be used this time, RuntimeError
    when the restart was attempted and failed, so the caller can log/degrade."""
    started = time.monotonic()
    _request_sidecar_restart()
    deadline = time.monotonic() + READY_POLL_SECONDS
    while time.monotonic() < deadline:
        if is_ollama_up(timeout=2):
            return time.monotonic() - started
        time.sleep(1)
    raise RuntimeError(f"Ollama restarted but not answering after {READY_POLL_SECONDS}s")


def _maybe_restart(reason):
    """#454: Guarded restart step. Returns (restarted: bool, seconds).
    Checks circuit breaker and cooldown before attempting. Never raises."""
    global _last_restart_at, _breaker_reason
    if _breaker_reason:
        return False, 0.0
    if _last_restart_at is not None:
        elapsed = time.monotonic() - _last_restart_at
        if elapsed < CAPTION_RESTART_COOLDOWN_SECONDS:
            print(f"caption: restart wanted ({reason}) but last restart was {elapsed:.1f}s ago; cooling down", flush=True)
            return False, 0.0
    try:
        print(f"caption: restarting Ollama via the restart sidecar: {reason}", flush=True)
        secs = restart_ollama_container()
        _last_restart_at = time.monotonic()
        return True, secs
    except RestartUnavailable as e:
        # #643: no safety valve this time. Not a breaker trip: the consecutive-failure count
        # still opens the breaker if Ollama keeps failing.
        print(f"caption: restart skipped, no safety valve available: {e}", flush=True)
        return False, 0.0
    except Exception as e:
        _breaker_reason = f"Ollama did not come back after restart ({e!r})"
        print(f"caption: circuit breaker OPEN: {_breaker_reason}", flush=True)
        return False, 0.0


# --- Breaker status and reset ---

def breaker_status():
    """#454: Returns dict with current circuit-breaker state, memory ceiling,
    cooldown duration, and seconds since the last restart (if any).
    Meant for the admin page's status display and the /api/captions/defaults response.
    Deliberately does NOT take CAPTION_LOCK: that lock is held for a whole
    model call (up to GENERATE_TIMEOUT_SECONDS), and a status read must not
    stall an HTTP request behind a caption in progress."""
    secs_since = None if _last_restart_at is None else time.monotonic() - _last_restart_at
    return {
        "open": _breaker_reason is not None,
        "reason": _breaker_reason,
        "consecutive_failures": _consecutive_failures,
        "seconds_since_restart": secs_since,
        "cooldown_seconds": CAPTION_RESTART_COOLDOWN_SECONDS,
    }


def reset_breaker():
    """#454: Clears the circuit breaker and failure counter. Under CAPTION_LOCK."""
    global _consecutive_failures, _breaker_reason
    with CAPTION_LOCK:
        _consecutive_failures = 0
        _breaker_reason = None


# --- The one-image cycle ---

def caption_once(image_path, prompt=None, temperature=None, num_predict=None, label=None):
    """#454: The full per-image cycle under CAPTION_LOCK: one model call,
    optional guarded restart (on failure, via the #643 sidecar), then release.
    Returns dict {caption, elapsed_seconds, restarted, restart_seconds,
    restart_reason, error}. `error` is set (and caption None) on failure.
    If circuit breaker is open, returns immediately with error message."""
    global _consecutive_failures, _breaker_reason
    with CAPTION_LOCK:
        if label:
            print(f"caption: model call start {label}", flush=True)  # #549: proves serialization in the logs
        if _breaker_reason:
            return {
                "caption": None,
                "elapsed_seconds": 0.0,
                "restarted": False,
                "restart_seconds": 0.0,
                "restart_reason": None,
                "error": f"captioning paused: {_breaker_reason}",
            }
        caption, elapsed, error = None, 0.0, None
        restarted, restart_seconds, restart_reason = False, 0.0, None
        try:
            caption, elapsed = generate_caption(image_path, prompt=prompt, temperature=temperature, num_predict=num_predict)
            _consecutive_failures = 0  # reset on success
        except Exception as e:
            error = repr(e)
            _consecutive_failures += 1
            if _consecutive_failures >= CAPTION_MAX_CONSECUTIVE_FAILURES:
                _breaker_reason = f"{_consecutive_failures} consecutive caption failures, last: {error}"
                print(f"caption: circuit breaker OPEN: {_breaker_reason}", flush=True)
            else:
                restart_reason = f"caption call failed: {error}"
                restarted, restart_seconds = _maybe_restart(restart_reason)
    return {
        "caption": caption,
        "elapsed_seconds": round(elapsed, 2),
        "restarted": restarted,
        "restart_seconds": round(restart_seconds, 2),
        "restart_reason": restart_reason,
        "error": error,
    }


# --- #549: one captioner, in the web process ---
# The GPU guard (CAPTION_LOCK, breaker, cooldown) is process-local, so only ONE process may
# ever caption. The MCP process (CONSTRUCTICON_ROLE=mcp, set in mcp_server/server.py) enqueues
# into the shared caption_queue table instead; web's queue worker drains it one at a time
# through run_caption, so the lock/breaker/cooldown apply exactly as for a web upload.

QUEUE_POLL_MIN_SECONDS = 2.0
QUEUE_POLL_MAX_SECONDS = 15.0
ORPHAN_GRACE_SECONDS = 600  # #592: pending with no queue row for this long = forgotten, re-queue it
_worker_thread = None
_worker_guard = threading.Lock()
# #592: every caption (web upload, retype, regenerate click, MCP) is persisted into caption_queue
# first; this Event just nudges web's worker so an upload still captions right away instead of
# waiting out the idle poll. In the MCP process nothing waits on it (setting it is harmless).
_wake = threading.Event()


def wake_worker():
    _wake.set()


def enqueue_only():
    """True in a process that must not caption itself (the MCP server)."""
    return os.environ.get("CONSTRUCTICON_ROLE", "").strip().lower() == "mcp"


def enqueue_caption(slug, start_step=0, cascade=True):
    """Marks the item caption-pending and queues it for web's worker. Best-effort, never raises."""
    try:
        row = db.get_by_slug(slug)
        if row is None or row["redacted"]:
            return
        spec = object_types.get_object_type(row.get("media_type"))
        if not (spec.caption_capable and not DISABLED):
            return
        db._update_content_metadata(slug, type_metadata={STATUS_KEY: "pending"})
        db.enqueue_caption(slug, start_step, cascade)
        print(f"caption: queued {slug} for the web worker", flush=True)
        wake_worker()
    except Exception as e:
        print(f"caption enqueue failed for {slug}: {e!r}", flush=True)


def requeue_orphans(grace_seconds=ORPHAN_GRACE_SECONDS):
    """#592: the self-heal. A caption-capable item sitting at `pending` with no caption_queue row
    for longer than `grace_seconds` was forgotten (the process that held it died before the queue
    existed for web uploads, or a row was lost): queue it again. Returns how many it re-queued.
    Does nothing when captions are off: the processing view already shows those as `off` (#585)."""
    if DISABLED:
        return 0
    rows = db.list_orphan_pending_captions(caption_capable_types(), time.time() - grace_seconds)
    queued = 0
    for row in policy.filter_visible(rows):
        print(f"caption self-heal: re-queuing {row['slug']} (pending with no queue row)", flush=True)
        enqueue_caption(row["slug"])
        queued += 1
    return queued


def _drain_one():
    """Runs the oldest queued caption, if any and if the breaker allows. Returns True if it
    did work (so the loop can go straight to the next one without sleeping)."""
    if _breaker_reason:  # captioning paused: leave the queue alone until the owner resets it
        return False
    item = db.peek_caption_queue()
    if item is None:
        return False
    slug = item["slug"]
    row = db.get_by_slug(slug)
    spec = object_types.get_object_type(row.get("media_type")) if row else None
    if row is None or row["redacted"]:
        pass
    elif item.get("mode") == "title":
        _run_title_now(slug, item.get("batch_id"))  # #542: a short-title proposal only, never the caption
    elif DISABLED or not spec.caption_capable:
        db._update_content_metadata(slug, type_metadata={STATUS_KEY: "failed"})
    else:
        _run_caption_now(slug, item["start_step"], bool(item["cascade"]))
    db.dequeue_caption(slug)
    return True


def queue_worker_loop():
    delay = QUEUE_POLL_MIN_SECONDS
    while True:
        _wake.clear()  # before the drain, so a nudge that lands mid-drain is not lost
        try:
            if _drain_one():
                delay = QUEUE_POLL_MIN_SECONDS
                time.sleep(0.5)  # breathe between GPU jobs
                continue
        except Exception as e:
            print(f"caption queue worker error: {e!r}", flush=True)
        _wake.wait(delay)  # #592: an upload wakes this at once; idle otherwise
        delay = min(delay * 1.5, QUEUE_POLL_MAX_SECONDS)  # idle backoff, never a hot loop


def _queue_worker_as_system():
    """#560: the queue worker is background work, so whatever it writes is the 'system' actor."""
    with actor_ctx.acting_as(actor_ctx.ACTOR_SYSTEM):
        queue_worker_loop()


def start_queue_worker():
    """Starts the single drain thread (idempotent). Web process only."""
    global _worker_thread
    with _worker_guard:
        if _worker_thread is not None and _worker_thread.is_alive():
            return False
        _worker_thread = threading.Thread(target=_queue_worker_as_system, name="caption-queue", daemon=True)
        _worker_thread.start()
        return True


# --- Pipeline entry point ---

def _caption_source_path(row, spec):
    """Same image OCR would use (see core/ocr.py's _ocr_source_path): the
    uploaded file itself for UPLOADED_FILE types, otherwise the generated
    thumbnail (video frame, rendered PSD/SVG/EPS raster)."""
    if spec.thumbnail_source == object_types.ThumbnailSource.UPLOADED_FILE:
        path = storage.path_for(row["stored_filename"]) if row.get("stored_filename") else None
        return path if path and path.exists() else None
    thumbnails.ensure_thumbnail(row)
    thumb = storage.thumb_path_for(row["slug"])
    return thumb if thumb.exists() else None


def mark_pending(slug):
    """Caption bookkeeping for a manual (re)generate click: the item shows "pending" until the run
    lands. A pipeline write, deliberately not change-logged (#541: see core/items.py)."""
    db._update_content_metadata(slug, type_metadata={STATUS_KEY: "pending"})


def should_caption(spec):
    """#454: Return False if breaker is open (captioning paused), in addition
    to existing checks. Keeps bulk uploads from queueing captions once the
    breaker has tripped."""
    if _breaker_reason:
        return False
    return bool(spec.caption_capable) and not DISABLED


# --- Short titles (#542) ---

PROPOSE_MAX = 2000  # one backfill call queues at most this many proposals; it can be repeated

# Lead-ins a describing model opens with. Stripped only as whole phrases at the very start (a
# phrase boundary: the next character is a space, a comma or a colon), never by position, so a
# word can't be cut (#743's suspicion; there never was a strip here before).
_LEAD_INS = (
    "the image shows", "the image features", "the image depicts", "the image displays", "the image captures",
    "the image presents", "the image contains", "the image is of", "this image shows", "this image features",
    "this image depicts", "this is an image of", "this is a photo of", "this is a picture of", "in the image",
    "in this image", "the photo shows", "the photograph shows", "the picture shows", "a photo of",
    "a photograph of", "a picture of", "an image of", "a close-up of", "a closeup of",
)
_CLAUSE_CUTS = (",", ";", ":", " - ", " \u2014 ", " \u2013 ", " which ", " that ", " while ", " where ", " who ")
_TRAILING_STOP = frozenset("a an the and or with of on in at to for by from its his her their is are as".split())


def _strip_lead_in(t):
    low = t.lower()
    for phrase in sorted(_LEAD_INS, key=len, reverse=True):
        if low.startswith(phrase) and (len(t) == len(phrase) or t[len(phrase)] in " ,:"):
            return t[len(phrase):].lstrip(" ,:")
    return t


def title_from_caption(caption, max_words=TITLE_MAX_WORDS):
    """#542: a short title derived from a caption: its first sentence, a describing lead-in ("The
    image shows ...") and a leading article taken off on phrase boundaries, cut at the first clause
    break (a comma, "which", "while", ...), at most `max_words` words, with no dangling "with"/"and"
    at the end, first letter capitalised. "The image shows a green monster truck with six wheels,
    parked on a wooden workbench." -> "Green monster truck with six wheels". None when nothing usable
    is left (a fragment, no letters). Pure."""
    from . import item_title
    if _is_garbage(caption):  # empty, no letters, or a mid-word fragment: nothing to build on
        return None
    t = " ".join((caption or "").split())
    if t.startswith("(") and 0 < t.find(")") <= 20:  # "(video) ..." / "(animated) ..."
        t = t[t.find(")") + 1:].strip()
    for end in (". ", "! ", "? "):
        if end in t:
            t = t.split(end, 1)[0]
    t = t.rstrip(".!?")
    t = _strip_lead_in(t)
    low = " " + t.lower()
    cut = min((low.find(c) for c in _CLAUSE_CUTS if low.find(c) > 0), default=-1)
    if cut > 0:
        t = t[:cut - 1]
    words = t.split()
    if len(words) > 1 and words[0].lower() in ("a", "an", "the"):
        words = words[1:]
    if len(words) > 6:  # a long phrase: its head, up to the first "with" / "next to" past the second word
        for i in range(2, len(words)):
            if words[i].lower() == "with" or (words[i].lower() == "next" and i + 1 < len(words)
                                              and words[i + 1].lower() == "to"):
                words = words[:i]
                break
    words = words[:max_words]
    while len(words) > 1 and words[-1].lower().strip(",.;:") in _TRAILING_STOP:
        words.pop()
    if not words:
        return None
    title = item_title.clean_short_title(" ".join(words))
    if not title:
        return None
    title = title[0].upper() + title[1:]
    if not any(ch.isalpha() for ch in title):
        return None
    return title


def suggest_title(image_path, label=None):
    """#542, the backfill: asks the local model to describe the image (TITLE_STEPS: "Describe this
    image." at temp 0, then 0.15 when the answer is garbage) and derives a short title from the
    answer (title_from_caption). Returns (title or None, reason when None). Never raises."""
    why = "not run"
    for prompt, temperature in TITLE_STEPS:
        result = caption_once(image_path, prompt=prompt, temperature=temperature,
                              label=f"{label} (title)" if label else None)
        if result["error"]:
            return None, f"model call failed: {result['error']}"
        if _is_garbage(result["caption"]):
            why = _bad_response_label(result["caption"])
            continue
        title = title_from_caption(result["caption"])
        if title:
            return title, None
        why = f"no title could be derived from {result['caption'][:60]!r}"
    return None, why


def _run_title_now(slug, batch_id):
    """#542: one queued short-title proposal (the backfill), run by web's worker. Skipped when the
    item is no longer waiting for one (the backfill was undone, the owner named it meanwhile, or it
    was answered). The answer is written into the backfill's own batch (items.record_title_proposal),
    so one undo removes every proposal. Never raises."""
    from . import items  # lazy: items imports this module
    try:
        row = db.get_by_slug(slug)
        if row is None or row["redacted"]:
            return
        tm = row.get("type_metadata") or {}
        if tm.get(TITLE_STATUS_KEY) != "queued":
            print(f"caption: short-title proposal for {slug} skipped (no longer queued: undone or answered)", flush=True)
            return
        if row.get("display_name") or row.get("short_title"):
            items.record_title_proposal(slug, None, model=OLLAMA_MODEL, batch_id=batch_id,
                                        note="the item got a name meanwhile")
            return
        spec = object_types.get_object_type(row.get("media_type"))
        if not should_caption(spec):
            items.record_title_proposal(slug, None, model=OLLAMA_MODEL, batch_id=batch_id,
                                        note="captioning is off or paused, or this type has no picture to read")
            return
        image_path = _caption_source_path(row, spec)
        if image_path is None:
            items.record_title_proposal(slug, None, model=OLLAMA_MODEL, batch_id=batch_id, note="no picture to read")
            return
        title, why = suggest_title(image_path, label=slug)
        items.record_title_proposal(slug, title, model=OLLAMA_MODEL, batch_id=batch_id, note=why)
        print(f"caption: short-title proposal for {slug}: {title!r}" if title else
              f"caption: no short-title proposal for {slug}: {why}", flush=True)
    except Exception as e:
        print(f"caption: short-title proposal failed for {slug}: {e!r}", flush=True)


def title_backfill_candidates(limit=PROPOSE_MAX):
    """#542: the items whose title used to be their long caption (no rename, no short title, a
    description longer than a name), with a picture the local model can read; the actor's view."""
    from . import item_title
    rows = db.list_long_caption_items(caption_capable_types(), limit=max(1, min(int(limit), PROPOSE_MAX)))
    rows = [r for r in rows if not item_title.title_shaped(r.get("content_description"), item_title.CAPTION_AS_TITLE_MAX)]
    return policy.filter_visible(rows)


def propose_short_titles(limit=PROPOSE_MAX, dry_run=True):
    """#542 backfill: has the local captioner PROPOSE a short title for every item whose title used
    to be its long caption, into the caption review queue. Nothing is applied: each proposal waits
    for the owner (Accept / edit / Skip on /captions/review). Dry run by DEFAULT: answers what it
    would queue. A real run marks the items in ONE imaged batch (items.queue_title_proposals) and
    queues them for web's worker (behind any real caption); the proposals join that batch as they
    land, so constructicon_undo / POST /api/changes/<batch>/undo takes back every proposal written
    so far and the worker skips the rest. Refuses with `captions_disabled` when captioning is off.
    Returns {dry_run, count, batch_id, examples: [{slug, title, description}]}."""
    from . import item_title, items  # lazy: items imports this module
    if DISABLED:
        raise Conflict("Captions are off on this install (CAPTION_DISABLED), so the local model can't propose "
                       "short titles. Turn captioning on and restart, or write them through the MCP "
                       "(constructicon_set_caption with title=).", code="captions_disabled")
    rows = title_backfill_candidates(limit)
    examples = [{"slug": r["slug"], "title": item_title.title_of(r),
                 "description": (r.get("content_description") or "")[:120]} for r in rows[:10]]
    if dry_run or not rows:
        return {"dry_run": bool(dry_run), "count": len(rows), "batch_id": None, "examples": examples}
    res = items.queue_title_proposals([r["slug"] for r in rows])
    for slug in res.data["queued"]:
        db.enqueue_caption(slug, mode="title", batch_id=res.batch_id)
    wake_worker()
    print(f"caption: queued {len(res.data['queued'])} short-title proposal(s), batch {res.batch_id}", flush=True)
    return {"dry_run": False, "count": len(res.data["queued"]), "batch_id": res.batch_id, "examples": examples}


# --- Mid-word captions (#743) ---

def scan_mid_word():
    """#743, read-only: every `done` caption suggestion that starts mid-word (starts_mid_word), the
    actor's view. Returns {count, by_model, accepted, items: [{slug, title, caption, model, step,
    accepted}]} where accepted = the fragment was also taken into content_description."""
    from . import item_title
    out = []
    for r in policy.filter_visible(db.list_done_captions()):
        tm = r.get("type_metadata") or {}
        cap = tm.get(METADATA_KEY) or ""
        if not starts_mid_word(cap):
            continue
        out.append({"slug": r["slug"], "title": item_title.title_of(r), "caption": cap,
                    "model": tm.get("auto_caption_model"), "step": tm.get(STEP_KEY),
                    "accepted": (r.get("content_description") or "").strip() == cap.strip()})
    by_model = {}
    for it in out:
        by_model[it["model"] or "unknown"] = by_model.get(it["model"] or "unknown", 0) + 1
    return {"count": len(out), "by_model": by_model, "accepted": sum(1 for it in out if it["accepted"]), "items": out}


def requeue_mid_word(dry_run=True):
    """#743: re-queues every `done` mid-word caption as ONE undoable batch
    (items.requeue_midword_captions; dry run by DEFAULT). Answers {dry_run, count, batch_id,
    slugs}. They then show in needs_caption / Admin's "Caption skipped items"."""
    from . import items  # lazy: items imports this module
    found = scan_mid_word()["items"]
    slugs = [it["slug"] for it in found]
    if not slugs:
        return {"dry_run": bool(dry_run), "count": 0, "batch_id": None, "slugs": []}
    res = items.requeue_midword_captions(slugs, dry_run=dry_run)
    return {"dry_run": bool(dry_run), "count": len(res.data["requeued"]),
            "batch_id": None if dry_run else res.batch_id, "slugs": res.data["requeued"]}


# --- Items that still need a caption (#585, #588) ---

AGENT_MODEL = "mcp-agent"  # auto_caption_model for a caption an MCP agent wrote (#588)
QUEUE_SKIPPED_MAX = 1000   # one Admin click queues at most this many; the click can be repeated


def caption_capable_types():
    """Registry keys of the types that can be captioned (STL and friends are explicitly not)."""
    return sorted(k for k, spec in object_types.OBJECT_TYPES.items() if spec.caption_capable)


def needs_caption(limit=50, include_failed=True):
    """(#588) Caption-capable items with no done caption and no accepted description, newest
    first: the status is absent or, with include_failed, failed/skipped (what a captions-off
    install leaves behind, #585). Redacted items and anything the item policy hides are left out.
    Works with captions disabled: that is exactly the install that needs an agent."""
    limit = max(1, min(int(limit), 500))
    rows = db.list_needs_caption(caption_capable_types(), include_failed=include_failed, limit=limit)
    # #603: a sensitive item (flagged, or a restricted type) is never handed to a captioning agent,
    # even an admin one: like an export, the picture would leave the install.
    return policy.filter_exportable(policy.filter_visible(rows))[:limit]


def count_needs_caption(include_failed=True):
    """How many items needs_caption would list with no limit (Admin shows this)."""
    return db.list_needs_caption(caption_capable_types(), include_failed=include_failed, count_only=True)


def queue_skipped(limit=QUEUE_SKIPPED_MAX):
    """(#585) Admin's "Caption skipped items": queue every caption-capable item whose caption was
    never done or was marked failed/skipped and that has no description yet, for the web worker
    (one at a time, behind the lock and breaker like any upload). Refuses with `captions_disabled`
    when this install has captioning off. Returns {"queued", "remaining"}; at most QUEUE_SKIPPED_MAX
    per call (`limit` can lower that, newest items first). Pipeline bookkeeping like the rest of this module: not change-logged."""
    if DISABLED:
        raise Conflict("Captions are off on this install (CAPTION_DISABLED), so there is nothing to run. "
                       "Turn captioning on and restart, or caption items through the MCP tools.",
                       code="captions_disabled")
    rows = db.list_needs_caption(caption_capable_types(), include_failed=True,
                                 limit=max(1, min(int(limit), QUEUE_SKIPPED_MAX)))
    queued = 0
    for row in policy.filter_visible(rows):
        enqueue_caption(row["slug"])
        queued += 1
    return {"queued": queued, "remaining": count_needs_caption()}


def _is_garbage(caption):
    """#262: True when a model response is empty *or* degenerate — no
    alphabetic character anywhere in it. moondream occasionally emits a long
    run of '!!!!!...' (the same failure the STL exclusion in object_types
    guards against, but seen on a perfectly ordinary photo too); that's
    non-empty, so the plain `not caption` check let it through as a false
    "done". run_caption treats both cases identically: cascade to the next
    STEPS rung, or mark failed if the ladder runs out.

    Deliberately the issue's literal spec and nothing more: a caption with
    even one real word (however much punctuation surrounds it) is NOT
    garbage — it's a weak suggestion the owner can see and Regenerate,
    whereas a hidden retry costs a full Ollama restart cycle per rung and
    risks skipping past a usable caption. str.isalpha is Unicode-aware, so
    real letters in any script count.

    #743: a caption that starts MID-WORD is garbage too ("urn with wires...", "ids and numbers...":
    37 such moondream captions sat on prod as `done`, every one at step 0). There is no prefix
    strip anywhere in this pipeline that could have cut them (checked: the model's text is stored
    as Ollama returns it, only .strip()'d), so the fragment is the model's own output; see
    starts_mid_word for the rule."""
    return not any(ch.isalpha() for ch in (caption or "")) or starts_mid_word(caption)


# A sentence may legitimately start with one of these in lower case (moondream sometimes drops the
# capital); any other lower-case first word is a fragment of a longer one.
_LOWER_STARTS = frozenset("""a an the this that these those one two three four five six seven eight nine ten
    some several many few two-tone close-up closeup black white red blue green yellow""".split())
_LEAD_PUNCT = "\"'`([{“‘*-_ "


def starts_mid_word(text):
    """#743: True when `text` begins with a lower-case fragment of a word: its first word starts with
    a lower-case letter, has no capital in it (so "iPhone", "eBay" pass) and is not an article or a
    plain sentence start (_LOWER_STARTS). A leading quote or bracket is skipped first; a text whose
    first character is not a letter (a digit, "(video) ...") is not a fragment."""
    t = (text or "").lstrip()
    if t.startswith("(") and 0 < t.find(")") <= 20:  # an agent's "(video) ..." / "(animated) ..." label
        t = t[t.find(")") + 1:]
    t = t.lstrip(_LEAD_PUNCT)
    if not t or not t[0].isalpha() or not t[0].islower():
        return False
    word = ""
    for ch in t:
        if ch.isalpha() or ch in "-'":
            word += ch
        else:
            break
    if any(c.isupper() for c in word):
        return False
    return word.lower().strip("-'") not in _LOWER_STARTS


def _bad_response_label(caption):
    """Log wording for a response _is_garbage rejected — tells the owner
    reading `docker logs` whether the model said nothing, said '!!!!', or started mid-word."""
    if not caption:
        return "empty response"
    if any(ch.isalpha() for ch in caption) and starts_mid_word(caption):
        return f"mid-word fragment {caption[:30]!r} (#743)"
    return f"degenerate response {caption[:20]!r}{'…' if len(caption) > 20 else ''} ({len(caption)} chars, no letters)"


def run_caption(slug, start_step=0, cascade=True):
    """Background task, mirrors ocr.run_ocr: best-effort end to end, writes
    the result (or a failed marker) into type_metadata, never raises.

    start_step/cascade let the same function serve both callers of STEPS:
    the upload/import pipeline (start_step=0, cascade=True — try the
    strict default, auto-advance through the ladder on an empty response)
    and a manual "Regenerate" click (#250; cascade=False — run exactly the
    one step the caller picked via api_retry_caption's step-advance logic,
    even if it comes back empty, so repeated clicks give real variety
    instead of hidden multi-step jumps behind one click).

    #549/#592: this does NOT caption: it persists the request into caption_queue and wakes web's
    worker, which runs it through _run_caption_now (lock, breaker, cooldown as always). Every
    caption trigger funnels through here, in web and in MCP alike, so nothing is held only in
    memory: a restart leaves the queue rows and the worker resumes them."""
    enqueue_caption(slug, start_step, cascade)


def _run_caption_now(slug, start_step=0, cascade=True):
    """The actual caption run. Only web's queue worker calls this (see run_caption)."""
    try:
        row = db.get_by_slug(slug)
        if row is None or row["redacted"]:
            return
        spec = object_types.get_object_type(row.get("media_type"))
        if not should_caption(spec):
            return
        # "pending" while queued behind the lock / running, so the detail
        # page can show progress and poll — same idea as ocr_status.
        db._update_content_metadata(slug, type_metadata={STATUS_KEY: "pending"})
        image_path = _caption_source_path(row, spec)
        if image_path is None:
            print(f"caption: no source image for {slug} ({spec.key}) — skipping", flush=True)
            db._update_content_metadata(slug, type_metadata={STATUS_KEY: "failed"})
            return
        step_index = start_step % len(STEPS)
        step_prompt, step_temperature = STEPS[step_index]
        result = caption_once(image_path, prompt=step_prompt, temperature=step_temperature, label=slug)
        if cascade:
            for next_index in range(step_index + 1, len(STEPS)):
                if result["error"] or not _is_garbage(result["caption"]):
                    break
                step_prompt, step_temperature = STEPS[next_index]
                print(f"caption {_bad_response_label(result['caption'])} for {slug} — retrying with prompt {step_prompt!r} at temperature {step_temperature}", flush=True)
                result = caption_once(image_path, prompt=step_prompt, temperature=step_temperature, label=slug)
                step_index = next_index
        if result["error"] or _is_garbage(result["caption"]):
            print(f"caption failed for {slug} at step {step_index}: {result['error'] or _bad_response_label(result['caption'])}", flush=True)
            reason = result["error"] or _bad_response_label(result["caption"])
            db._update_content_metadata(slug, type_metadata={STATUS_KEY: "failed", STEP_KEY: step_index,
                                                             "auto_caption_failed_reason": reason[:300]})
            return
        # #542: the caption comes with a short title, derived from it (no second model call).
        title = title_from_caption(result["caption"])
        title_note = "no title could be derived from the caption"
        meta = {
            METADATA_KEY: result["caption"],
            STATUS_KEY: "done",
            STEP_KEY: step_index,
            "auto_caption_model": OLLAMA_MODEL,
            "auto_caption_failed_reason": None,
        }
        if title:
            meta.update({TITLE_KEY: title, TITLE_MODEL_KEY: OLLAMA_MODEL, TITLE_DISMISSED_KEY: None})
        else:
            print(f"caption: no short title for {slug}: {title_note}", flush=True)
        db._update_content_metadata(slug, type_metadata=meta)
        restart_note = ""
        if result["restarted"]:
            restart_note = f" restart=yes ({result['restart_seconds']}s, {result['restart_reason']})"
        print(
            f"caption done for {slug}: step {step_index}, {result['elapsed_seconds']}s model{restart_note}",
            flush=True,
        )
    except Exception as e:
        print(f"caption pipeline failed for {slug}: {e!r}", flush=True)
        try:
            db._update_content_metadata(slug, type_metadata={STATUS_KEY: "failed"})
        except Exception as e2:
            print(f"could not mark {slug} caption-failed: {e2!r}", flush=True)
