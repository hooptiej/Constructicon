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
no routine restarts; restart only on model-call failure or if Ollama RAM
exceeds the ceiling (measured peak ~1650 MB, threshold ~3000 MB), guarded
by a cooldown (600s) and circuit breaker that opens after 3 consecutive
failures. A process restart resets the breaker. Socket access (for safety-valve
restarts + RAM reading) is now needed only as this guard, not per-image.

Network: the app container reaches Ollama at CAPTION_OLLAMA_URL (default
http://ollama:11434 — Docker DNS on Ollama's own compose network, which the
app container joins; the ipvlan LAN network the app otherwise lives on
can't reach the host's published port).

Defaults below (prompt, temperature, token cap) were settled with the admin
pane's live tuning panel (POST /api/captions/test) against real images —
see the PR for #239 for the runs that picked them.
"""

import base64
import http.client
import json
import logging
import os
import socket
import threading
import time
import urllib.error
import urllib.request

from . import actor as actor_ctx, besteffort, db, object_types, storage, thumbnails

log = logging.getLogger("constructicon.captions")

OLLAMA_URL = os.environ.get("CAPTION_OLLAMA_URL", "http://ollama:11434").rstrip("/")
OLLAMA_MODEL = os.environ.get("CAPTION_OLLAMA_MODEL", "moondream")
OLLAMA_CONTAINER = os.environ.get("CAPTION_OLLAMA_CONTAINER", "ollama")
DOCKER_SOCKET = os.environ.get("CAPTION_DOCKER_SOCKET", "/var/run/docker.sock")
# Set CAPTION_DISABLED=1 to skip scheduling captions entirely (e.g. a deploy
# with no Ollama reachable) rather than logging a failure per upload.
DISABLED = os.environ.get("CAPTION_DISABLED", "") not in ("", "0", "false", "no")

# #454: Guarded restart parameters. RAM ceiling is ~2x the measured peak
# (1650 MB + headroom). Cooldown prevents thrashing restarts. Circuit breaker
# opens after max consecutive failures.
def _parse_env_int(var, default):
    """Safely parse an env var to int, fall back to default on garbage."""
    try:
        return int(os.environ.get(var, default))
    except (ValueError, TypeError) as e:
        besteffort.warn(log, f"captions: env {var} is not an integer, using the default", e,
                        value=os.environ.get(var), default=default)
        return default

CAPTION_OLLAMA_MAX_MEM_MB = _parse_env_int("CAPTION_OLLAMA_MAX_MEM_MB", 3072)  # #454
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


# --- Docker Engine API over the unix socket ---

class _UnixHTTPConnection(http.client.HTTPConnection):
    def __init__(self, socket_path, timeout):
        super().__init__("localhost", timeout=timeout)
        self._socket_path = socket_path

    def connect(self):
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        sock.settimeout(self.timeout)
        sock.connect(self._socket_path)
        self.sock = sock


def docker_socket_available():
    return os.path.exists(DOCKER_SOCKET)


def _ollama_mem_mb():
    """Read Ollama container's memory usage (MB) via Docker Engine API.
    Returns int (MB) or None if socket unavailable or any error. Never raises.
    Memory = usage minus inactive_file cache (or .cache if inactive_file missing).
    #454: used by the RAM ceiling safety valve."""
    if not docker_socket_available():
        return None
    try:
        conn = _UnixHTTPConnection(DOCKER_SOCKET, timeout=5)
        try:
            conn.request("GET", f"/containers/{OLLAMA_CONTAINER}/stats?stream=false&one-shot=true")
            resp = conn.getresponse()
            if resp.status != 200:
                return None
            data = json.loads(resp.read().decode("utf-8"))
            memory_stats = data.get("memory_stats") or {}
            usage = memory_stats.get("usage", 0)
            stats = memory_stats.get("stats") or {}
            # Subtract inactive file cache (more reliable) or falls back to cache.
            inactive = stats.get("inactive_file") or stats.get("cache", 0)
            net_usage = max(0, usage - inactive)
            return int(net_usage / (1024 * 1024))
        finally:
            conn.close()
    except Exception as e:
        besteffort.warn(log, "captions: reading Ollama memory usage from the Docker socket", e)
        return None


def restart_ollama_container():
    """POST /containers/{name}/restart via the Engine API, then block until
    Ollama answers /api/tags again. Returns the number of seconds the whole
    bounce took. Raises RuntimeError if the socket isn't there or the API
    refuses, so the caller can log/degrade."""
    if not docker_socket_available():
        raise RuntimeError(f"docker socket {DOCKER_SOCKET} not mounted — cannot restart {OLLAMA_CONTAINER}")
    started = time.monotonic()
    conn = _UnixHTTPConnection(DOCKER_SOCKET, timeout=RESTART_TIMEOUT_SECONDS)
    try:
        # t=5: SIGTERM, then SIGKILL after 5s — Ollama exits promptly anyway.
        conn.request("POST", f"/containers/{OLLAMA_CONTAINER}/restart?t=5")
        resp = conn.getresponse()
        body = resp.read()
        if resp.status not in (204, 200):
            raise RuntimeError(f"docker restart of {OLLAMA_CONTAINER} returned {resp.status}: {body[:200]!r}")
    finally:
        conn.close()
    deadline = time.monotonic() + READY_POLL_SECONDS
    while time.monotonic() < deadline:
        if is_ollama_up(timeout=2):
            return time.monotonic() - started
        time.sleep(1)
    raise RuntimeError(f"{OLLAMA_CONTAINER} restarted but Ollama not answering after {READY_POLL_SECONDS}s")


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
        print(f"caption: restarting {OLLAMA_CONTAINER}: {reason}", flush=True)
        secs = restart_ollama_container()
        _last_restart_at = time.monotonic()
        return True, secs
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
        "max_mem_mb": CAPTION_OLLAMA_MAX_MEM_MB,
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
    optional guarded restart (on failure, or if RAM > ceiling), then release.
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
            # Check RAM ceiling as safety valve.
            mem = _ollama_mem_mb()
            if mem is not None and mem > CAPTION_OLLAMA_MAX_MEM_MB:
                restart_reason = f"Ollama RAM {mem} MB > {CAPTION_OLLAMA_MAX_MEM_MB} MB"
                restarted, restart_seconds = _maybe_restart(restart_reason)
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
_worker_thread = None
_worker_guard = threading.Lock()


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
    except Exception as e:
        print(f"caption enqueue failed for {slug}: {e!r}", flush=True)


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
    elif DISABLED or not spec.caption_capable:
        db._update_content_metadata(slug, type_metadata={STATUS_KEY: "failed"})
    else:
        run_caption(slug, item["start_step"], bool(item["cascade"]))
    db.dequeue_caption(slug)
    return True


def queue_worker_loop():
    delay = QUEUE_POLL_MIN_SECONDS
    while True:
        try:
            if _drain_one():
                delay = QUEUE_POLL_MIN_SECONDS
                time.sleep(0.5)  # breathe between GPU jobs
                continue
        except Exception as e:
            print(f"caption queue worker error: {e!r}", flush=True)
        time.sleep(delay)
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
    real letters in any script count."""
    return not any(ch.isalpha() for ch in (caption or ""))


def _bad_response_label(caption):
    """Log wording for a response _is_garbage rejected — tells the owner
    reading `docker logs` whether the model said nothing or said '!!!!'."""
    if not caption:
        return "empty response"
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

    #549: in the MCP process this does NOT run -- it enqueues (see enqueue_only) and the web
    process's queue worker calls it for real. Every caption trigger funnels through here, so
    no call site (upload, import, retype, a future one) can caption outside web."""
    if enqueue_only():
        enqueue_caption(slug, start_step, cascade)
        return
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
            db._update_content_metadata(slug, type_metadata={STATUS_KEY: "failed", STEP_KEY: step_index})
            return
        db._update_content_metadata(slug, type_metadata={
            METADATA_KEY: result["caption"],
            STATUS_KEY: "done",
            STEP_KEY: step_index,
            "auto_caption_model": OLLAMA_MODEL,
        })
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
