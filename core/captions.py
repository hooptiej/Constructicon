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
process-wide, one image at a time, and the Ollama *container* is restarted
after every single captioned image. A long-lived Ollama process was
confirmed (2026-09-09, on a separate real deployment) not to release
resources between successive inference calls on its own — usage
accumulates until it's bounced. So the cycle per image is: send one image,
get the caption, restart the container, wait for it to come back, release
the lock. Never batch multiple images against one running Ollama process.

The restart goes through the Docker Engine API over a bind-mounted
/var/run/docker.sock (plain http.client over AF_UNIX — no docker CLI in the
app image). If the socket isn't mounted (CAPTION_DOCKER_SOCKET missing),
the restart step degrades to Ollama's own `keep_alive: 0` model-unload
(already sent on every request as belt-and-braces) plus a loud warning, so
captioning still works but without the full container bounce the issue
calls for. Production needs the socket mounted for the real behavior.

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
import os
import socket
import threading
import time
import urllib.error
import urllib.request

from . import db, object_types, storage, thumbnails

OLLAMA_URL = os.environ.get("CAPTION_OLLAMA_URL", "http://ollama:11434").rstrip("/")
OLLAMA_MODEL = os.environ.get("CAPTION_OLLAMA_MODEL", "moondream")
OLLAMA_CONTAINER = os.environ.get("CAPTION_OLLAMA_CONTAINER", "ollama")
DOCKER_SOCKET = os.environ.get("CAPTION_DOCKER_SOCKET", "/var/run/docker.sock")
# Set CAPTION_DISABLED=1 to skip scheduling captions entirely (e.g. a deploy
# with no Ollama reachable) rather than logging a failure per upload.
DISABLED = os.environ.get("CAPTION_DISABLED", "") not in ("", "0", "false", "no")

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
# (upload/import pipeline), and a manual "Regenerate" click advances
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
    except Exception:
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
        # Unload the model as soon as the response is done — cheap insurance
        # on top of the container restart, and the only unload we get when
        # the docker socket isn't available.
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


def _bounce_after_call():
    """The post-image restart step. Returns (restarted: bool, seconds).
    Never raises — a failed restart is logged, not fatal to the caption that
    was already produced."""
    try:
        return True, restart_ollama_container()
    except Exception as e:
        print(f"caption: could not restart {OLLAMA_CONTAINER} after call ({e!r}) — relying on keep_alive=0 unload only", flush=True)
        return False, 0.0


# --- The one-image cycle ---

def caption_once(image_path, prompt=None, temperature=None, num_predict=None):
    """The full per-image discipline from #239 point 3, under CAPTION_LOCK:
    one model call, then a container restart, then release. Returns a dict
    {caption, elapsed_seconds, restarted, restart_seconds, error}. `error`
    is set (and caption None) when the model call itself failed — the
    restart still happens in that case, since whatever the failed call
    allocated needs releasing just the same."""
    with CAPTION_LOCK:
        caption, elapsed, error = None, 0.0, None
        try:
            caption, elapsed = generate_caption(image_path, prompt=prompt, temperature=temperature, num_predict=num_predict)
        except Exception as e:
            error = repr(e)
        restarted, restart_seconds = _bounce_after_call()
    return {
        "caption": caption,
        "elapsed_seconds": round(elapsed, 2),
        "restarted": restarted,
        "restart_seconds": round(restart_seconds, 2),
        "error": error,
    }


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


def should_caption(spec):
    return bool(spec.caption_capable) and not DISABLED


def run_caption(slug, start_step=0, cascade=True):
    """Background task, mirrors ocr.run_ocr: best-effort end to end, writes
    the result (or a failed marker) into type_metadata, never raises.

    start_step/cascade let the same function serve both callers of STEPS:
    the upload/import pipeline (start_step=0, cascade=True — try the
    strict default, auto-advance through the ladder on an empty response)
    and a manual "Regenerate" click (#250; cascade=False — run exactly the
    one step the caller picked via api_retry_caption's step-advance logic,
    even if it comes back empty, so repeated clicks give real variety
    instead of hidden multi-step jumps behind one click)."""
    try:
        row = db.get_by_slug(slug)
        if row is None or row["redacted"]:
            return
        spec = object_types.get_object_type(row.get("media_type"))
        if not should_caption(spec):
            return
        # "pending" while queued behind the lock / running, so the detail
        # page can show progress and poll — same idea as ocr_status.
        db.update_content_metadata(slug, type_metadata={STATUS_KEY: "pending"})
        image_path = _caption_source_path(row, spec)
        if image_path is None:
            print(f"caption: no source image for {slug} ({spec.key}) — skipping", flush=True)
            db.update_content_metadata(slug, type_metadata={STATUS_KEY: "failed"})
            return
        step_index = start_step % len(STEPS)
        step_prompt, step_temperature = STEPS[step_index]
        result = caption_once(image_path, prompt=step_prompt, temperature=step_temperature)
        if cascade:
            for next_index in range(step_index + 1, len(STEPS)):
                if result["error"] or result["caption"]:
                    break
                step_prompt, step_temperature = STEPS[next_index]
                print(f"caption empty for {slug} — retrying with prompt {step_prompt!r} at temperature {step_temperature}", flush=True)
                result = caption_once(image_path, prompt=step_prompt, temperature=step_temperature)
                step_index = next_index
        if result["error"] or not result["caption"]:
            print(f"caption failed for {slug} at step {step_index}: {result['error'] or 'empty response'}", flush=True)
            db.update_content_metadata(slug, type_metadata={STATUS_KEY: "failed", STEP_KEY: step_index})
            return
        db.update_content_metadata(slug, type_metadata={
            METADATA_KEY: result["caption"],
            STATUS_KEY: "done",
            STEP_KEY: step_index,
            "auto_caption_model": OLLAMA_MODEL,
        })
        print(
            f"caption done for {slug}: step {step_index}, {result['elapsed_seconds']}s model, "
            f"restart={'yes' if result['restarted'] else 'NO'} ({result['restart_seconds']}s)",
            flush=True,
        )
    except Exception as e:
        print(f"caption pipeline failed for {slug}: {e!r}", flush=True)
        try:
            db.update_content_metadata(slug, type_metadata={STATUS_KEY: "failed"})
        except Exception as e2:
            print(f"could not mark {slug} caption-failed: {e2!r}", flush=True)
