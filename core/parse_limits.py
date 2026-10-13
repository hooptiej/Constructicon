"""Bounds on parsing uploaded files (#644): time, memory, pixels, archive members.

Every registry hook that reads an uploaded file's bytes (a thumbnail render, text extraction,
upload-time metadata, a page's properties and preview, the type sniffers and pre-store checks)
plus the OCR image preparation and the perceptual hash run through ``call()``, which runs the
function in a **child process** with:

- a wall-clock timeout: the child (and anything it started, e.g. ffmpeg or Ghostscript, through
  its own process group) is killed when it runs out;
- a memory ceiling: ``RLIMIT_AS`` set to the child's starting size plus the budget, so an
  allocation past it fails inside the child (MemoryError, or the C library's own out-of-memory
  error) instead of taking memory from the app or the NAS;
- a CPU-time backstop (``RLIMIT_CPU``) and an alarm, in case the parent is gone;
- a concurrency cap per process, so a burst of uploads can't start more children than the box
  can hold.

A hostile file therefore costs at most one bounded child, and only the child dies. The result
types are unchanged: the child sends back exactly what the hook returned (or re-raises what it
raised); a limit hit raises ``LimitExceeded`` (an ``Exception``, so every existing best-effort
``except Exception`` around a hook still treats it as "no thumbnail" / "no properties").

Children come from a ``forkserver`` that preloads the type modules once (``PRELOAD``), so a
child costs about 10 ms instead of the 1.8 s an import of ``core.object_types`` takes. The
forkserver is the documented safe way to start processes from a threaded server (a plain
``fork`` of uvicorn could copy a lock some other thread holds); it costs about 85 MB of private
memory per app process, measured on the NAS (2026-10-13).

**A limit hit is remembered** per file (the stored filename, or the slug for a file-less item)
and step (``what``) for ``CONSTRUCTICON_PARSE_RETRY_SECONDS``, so a hostile file isn't re-parsed
on every page view or by every anonymous ``/f/<slug>/thumb`` request: the step fails at once
until then. ``forget(key)`` clears it (Retry OCR, Refresh thumbnail). ``failures_for(key)`` is
what the object page shows. It is per process (web and MCP each keep their own) and is lost on
restart, which just means one more bounded attempt.

**Two budgets** (``kind``):
- ``BACKGROUND``: work done once per file, at upload or in the OCR thread (thumbnail, text
  extraction, upload metadata, OCR image, similarity hash, pre-store check). Default 60 s and
  1,536 MB, 2 at a time per process. Measured on the real archive (constructicon-test,
  2026-10-13): the worst real file is a 22 MB STL's mesh stats at 15.6 s and 825 MB; the next
  is a PSD composite at 4.1 s / 327 MB; everything else is under 3.3 s.
- ``VIEW``: work done on every page view (properties, preview, type sniffing). Default 10 s and
  512 MB, 4 at a time, and a caller waits at most the timeout for a free slot. Worst real file:
  0.85 s / 47 MB.

**Images:** Pillow's ``MAX_IMAGE_PIXELS`` is set from ``CONSTRUCTICON_MAX_IMAGE_MEGAPIXELS``
(default 100; the largest real image is 25 MP) and ``DecompressionBombWarning`` is an error,
so an image over the cap is refused when its header is read, before any pixel is decoded, in
the app process and in every child.

**Archives:** ``archive_max_members()`` (``CONSTRUCTICON_ARCHIVE_MAX_MEMBERS``, default 5,000;
the largest real archive has 106) caps how many members a listing reads.

Every setting is read when used (tests set them) and falls back to its default when missing,
unparsable or not positive. ``CONSTRUCTICON_PARSE_SANDBOX=off`` runs the hooks in-process again
(the pixel and archive caps still apply); on a platform with no forkserver (Windows) they always
run in-process.
"""

import logging
import multiprocessing
import os
import pickle
import signal
import sys
import threading
import time
import warnings
from dataclasses import dataclass

from PIL import Image

log = logging.getLogger("constructicon.parse_limits")

BACKGROUND = "background"
VIEW = "view"

# Modules the forkserver imports once; a child unpickles hooks from these without importing.
PRELOAD = ["core.parse_limits", "core.object_types", "core.thumbnails"]

# Environment a child takes from the parent on every call (the forkserver's own copy dates from
# when it started): install paths and settings, OCR settings, and PATH (the tools hooks run).
_ENV_PREFIXES = ("CONSTRUCTICON_", "OCR_")
_ENV_NAMES = ("PATH",)

DEFAULTS = {
    "CONSTRUCTICON_PARSE_TIMEOUT_SECONDS": 60,
    "CONSTRUCTICON_PARSE_MEMORY_MB": 1536,
    "CONSTRUCTICON_PARSE_MAX_CONCURRENCY": 2,
    "CONSTRUCTICON_PARSE_VIEW_TIMEOUT_SECONDS": 10,
    "CONSTRUCTICON_PARSE_VIEW_MEMORY_MB": 512,
    "CONSTRUCTICON_PARSE_VIEW_CONCURRENCY": 4,
    "CONSTRUCTICON_PARSE_RETRY_SECONDS": 3600,
    "CONSTRUCTICON_MAX_IMAGE_MEGAPIXELS": 100,
    "CONSTRUCTICON_ARCHIVE_MAX_MEMBERS": 5000,
}


def setting(name):
    """A positive number from the environment, else its default."""
    default = DEFAULTS[name]
    raw = os.environ.get(name)
    if raw is None or not raw.strip():
        return default
    try:
        value = float(raw)
    except ValueError:  # silent-ok: an unparsable setting means "use the default", documented above
        return default
    if value <= 0:
        return default
    return int(value) if float(value).is_integer() else value


@dataclass(frozen=True)
class Budget:
    seconds: float
    memory_mb: int
    concurrency: int


def budget(kind):
    if kind == VIEW:
        return Budget(setting("CONSTRUCTICON_PARSE_VIEW_TIMEOUT_SECONDS"),
                      int(setting("CONSTRUCTICON_PARSE_VIEW_MEMORY_MB")),
                      int(setting("CONSTRUCTICON_PARSE_VIEW_CONCURRENCY")))
    return Budget(setting("CONSTRUCTICON_PARSE_TIMEOUT_SECONDS"),
                  int(setting("CONSTRUCTICON_PARSE_MEMORY_MB")),
                  int(setting("CONSTRUCTICON_PARSE_MAX_CONCURRENCY")))


def archive_max_members():
    return int(setting("CONSTRUCTICON_ARCHIVE_MAX_MEMBERS"))


# --- Images ----------------------------------------------------------------------------------

_bomb_filter_added = False


def max_image_pixels():
    return int(setting("CONSTRUCTICON_MAX_IMAGE_MEGAPIXELS") * 1_000_000)


def apply_image_limits():
    """Pillow refuses an image over the pixel cap when it reads the header. Its own rule is a
    warning above MAX_IMAGE_PIXELS and an error above twice that; the warning is made an error,
    so the cap is exactly the setting."""
    global _bomb_filter_added
    Image.MAX_IMAGE_PIXELS = max_image_pixels()
    if not _bomb_filter_added:
        warnings.filterwarnings("error", category=Image.DecompressionBombWarning)
        _bomb_filter_added = True


apply_image_limits()


# --- The exception and what a user is told ---------------------------------------------------

KINDS = ("time", "memory", "pixels", "crash", "busy", "unavailable")


class LimitExceeded(Exception):
    """A parse step was stopped by a limit. ``kind``: time | memory | pixels | crash (the parser
    died) | busy (no free slot in time; never remembered) | unavailable (no child could start;
    never remembered)."""

    def __init__(self, kind, what, message, cached=False):
        super().__init__(f"{what}: {message}")
        self.kind = kind
        self.what = what
        self.message = message
        self.cached = cached

    def __reduce__(self):
        return (LimitExceeded, (self.kind, self.what, self.message, self.cached))


def _message(kind, b, detail=""):
    if kind == "time":
        return f"took longer than {b.seconds:g} s, so it was stopped"
    if kind == "memory":
        return f"needed more than {b.memory_mb:,} MB of memory, so it was stopped"
    if kind == "pixels":
        return (f"the image is larger than {setting('CONSTRUCTICON_MAX_IMAGE_MEGAPIXELS'):g} megapixels, "
                f"so it wasn't decoded" + (f" ({detail})" if detail else ""))
    if kind == "crash":
        return "the parser crashed on this file" + (f" ({detail})" if detail else "")
    if kind == "busy":
        return "the server was busy with other files; it will be tried again"
    return "the parser couldn't be started" + (f" ({detail})" if detail else "")


# --- Remembered limit hits -------------------------------------------------------------------

_failures = {}  # (key, what) -> {"what", "kind", "message", "at"}
_failures_lock = threading.Lock()


def _ttl():
    return setting("CONSTRUCTICON_PARSE_RETRY_SECONDS")


def _remember(key, exc):
    with _failures_lock:
        _failures[(str(key), exc.what)] = {"what": exc.what, "kind": exc.kind, "message": exc.message,
                                           "at": time.time()}


def _recent(key, what):
    with _failures_lock:
        rec = _failures.get((str(key), what))
        if rec and time.time() - rec["at"] > _ttl():
            del _failures[(str(key), what)]
            return None
        return rec


def failures_for(key):
    """The remembered limit hits for one file, oldest first:
    [{"what", "kind", "message", "retry_in_minutes"}]. [] when none (or no key)."""
    if not key:
        return []
    now, ttl, out = time.time(), _ttl(), []
    with _failures_lock:
        for (k, _what), rec in list(_failures.items()):
            if k != str(key):
                continue
            left = ttl - (now - rec["at"])
            if left <= 0:
                del _failures[(k, _what)]
                continue
            out.append({"what": rec["what"], "kind": rec["kind"], "message": rec["message"],
                        "at": rec["at"], "retry_in_minutes": max(1, int(left // 60))})
    return sorted(out, key=lambda r: r["at"])


def has_failure(key, what):
    return bool(key) and _recent(key, what) is not None


def forget(key):
    """Clear every remembered limit hit for one file, so the next attempt really runs."""
    if not key:
        return
    with _failures_lock:
        for k in [k for k in _failures if k[0] == str(key)]:
            del _failures[k]


def reset():
    """Forget everything (tests)."""
    with _failures_lock:
        _failures.clear()


# --- Running a step in a child ---------------------------------------------------------------

_IN_CHILD = False
_ctx = None
_ctx_lock = threading.Lock()
_slots = {}
_slots_lock = threading.Lock()
_inline_warned = set()


def sandbox_available():
    if os.environ.get("CONSTRUCTICON_PARSE_SANDBOX", "").strip().lower() in ("off", "0", "false", "no"):
        return False
    try:
        import resource  # noqa: F401  (POSIX only)
    except ImportError:  # silent-ok: no resource module (Windows) = no sandbox, the documented fallback
        return False
    return "forkserver" in multiprocessing.get_all_start_methods()


_CHILD_NAME_PREFIX = "constructicon-parse:"


def _install_no_main_fixup():
    """A forkserver child normally re-imports the parent's __main__ before it runs (the "spawn"
    idiom): for the MCP that is all of mcp_server/server.py per child, and a script without a
    main guard would run itself again. Our children need nothing from __main__ (every hook is a
    module-level function in core), so for processes named with our prefix the main-module
    entries are dropped from the data the child prepares itself from. Other users of
    multiprocessing are untouched."""
    from multiprocessing import spawn
    if getattr(spawn.get_preparation_data, "_constructicon_no_main", False):
        return
    original = spawn.get_preparation_data

    def get_preparation_data(name):
        data = original(name)
        if str(name).startswith(_CHILD_NAME_PREFIX):
            data.pop("init_main_from_name", None)
            data.pop("init_main_from_path", None)
        return data

    get_preparation_data._constructicon_no_main = True
    spawn.get_preparation_data = get_preparation_data


def _context():
    global _ctx
    with _ctx_lock:
        if _ctx is None:
            _install_no_main_fixup()
            ctx = multiprocessing.get_context("forkserver")
            ctx.set_forkserver_preload(PRELOAD)
            _ctx = ctx
        return _ctx


def _slot(kind, size):
    """The per-process semaphore for a budget (rebuilt if the setting changed; tests do that)."""
    with _slots_lock:
        current = _slots.get(kind)
        if current is None or current[0] != size:
            current = _slots[kind] = (size, threading.BoundedSemaphore(size))
        return current[1]


def _env_snapshot():
    return {k: v for k, v in os.environ.items() if k.startswith(_ENV_PREFIXES) or k in _ENV_NAMES}


def call(fn, *args, kind=BACKGROUND, what="parse", key=None, wait=None):
    """Run ``fn(*args)`` under the ``kind`` budget and return its result (or raise what it raised).

    ``what`` names the step for logs and the object page ("thumbnail", "properties", ...).
    ``key`` (the stored filename, or the slug of a file-less item) turns on remembering: a
    limit hit is raised again at once, without running anything, until it expires or
    ``forget(key)`` clears it. ``wait``: seconds to wait for a free slot (default: forever for
    BACKGROUND, the timeout for VIEW), else ``LimitExceeded("busy")``.

    ``fn`` and ``args`` must pickle (module-level functions, plain data). Inside a child, or
    with the sandbox off, ``fn`` simply runs here.
    """
    if key:
        rec = _recent(key, what)
        if rec:
            raise LimitExceeded(rec["kind"], what, rec["message"], cached=True)
    if _IN_CHILD or not sandbox_available():
        return fn(*args)
    b = budget(kind)
    if wait is None and kind == VIEW:
        wait = b.seconds
    sem = _slot(kind, b.concurrency)
    if not sem.acquire(timeout=wait):
        raise LimitExceeded("busy", what, _message("busy", b))
    try:
        return _run_child(fn, args, b, what)
    except LimitExceeded as e:
        if e.kind not in ("busy", "unavailable"):
            log.warning("parse limit hit: %s for %s: %s", what, key or "(no key)", e.message)
            if key:
                _remember(key, e)
        raise
    finally:
        sem.release()


def _run_child(fn, args, b, what):
    ctx = _context()
    parent_conn, child_conn = ctx.Pipe(duplex=False)
    proc = ctx.Process(target=_child_main, args=(child_conn, fn, args, b.seconds, b.memory_mb, _env_snapshot()),
                       daemon=True, name=f"{_CHILD_NAME_PREFIX}{what}")
    started = False
    try:
        try:
            proc.start()
            started = True
        except (pickle.PicklingError, AttributeError, TypeError) as e:
            # A hook that can't be sent to a child (a test's monkeypatched stand-in). The registry
            # refuses such hooks at startup, so a real type never gets here.
            name = getattr(fn, "__qualname__", repr(fn))
            if name not in _inline_warned:
                _inline_warned.add(name)
                log.warning("parse step %s (%s) can't run in a child (%r); running it in-process", what, name, e)
            return fn(*args)
        except OSError as e:
            raise LimitExceeded("unavailable", what, _message("unavailable", b, repr(e))) from e
        finally:
            child_conn.close()
        if not parent_conn.poll(b.seconds):
            _kill(proc)
            raise LimitExceeded("time", what, _message("time", b))
        try:
            status, payload = parent_conn.recv()
        except EOFError:
            proc.join(5)
            code = proc.exitcode
            if code == -signal.SIGXCPU:
                raise LimitExceeded("time", what, _message("time", b)) from None
            if code == -signal.SIGKILL:  # nobody here killed it: the kernel's OOM killer did
                raise LimitExceeded("memory", what, _message("memory", b)) from None
            raise LimitExceeded("crash", what, _message("crash", b, f"exit code {code}")) from None
        if status == "ok":
            return payload
        if status == "limit":
            kind, detail = payload
            raise LimitExceeded(kind, what, _message(kind, b, detail))
        payload.add_note(f"raised in a parser child process ({what}, core/parse_limits.py)")
        raise payload
    finally:
        parent_conn.close()
        if started:
            proc.join(5)
            if proc.is_alive():
                _kill(proc)
            try:
                proc.close()
            except ValueError:  # silent-ok: still running after the kill + joins; the forkserver reaps it
                pass


def _kill(proc):
    """SIGKILL the child's whole process group (it made itself a group leader), so a tool it
    started (ffmpeg, gs) dies with it; fall back to the child alone."""
    try:
        os.killpg(proc.pid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError, OSError):  # silent-ok: the group is gone or was never made; kill the child itself
        try:
            proc.kill()
        except (ProcessLookupError, OSError, ValueError):  # silent-ok: already dead
            pass
    proc.join(5)


def _classify(exc, depth=0):
    """Which limit an exception means, or None for an ordinary failure."""
    if isinstance(exc, LimitExceeded):
        return exc.kind, ""
    if isinstance(exc, (Image.DecompressionBombError, Image.DecompressionBombWarning)):
        return "pixels", str(exc)[:200]
    if isinstance(exc, MemoryError):
        return "memory", ""
    text = str(exc)
    low = text.lower()
    if "NO_MEMORY" in text or "out of memory" in low or ("malloc" in low and "fail" in low):
        return "memory", ""
    cause = exc.__cause__ or exc.__context__
    if cause is not None and depth < 3:
        return _classify(cause, depth + 1)
    return None


class _LimitWatcher:
    """Notices a limit error even when the hook catches it (most hooks are best-effort and turn
    any exception into None), via sys.monitoring's RAISE event, which fires for every exception
    raised in Python code, including one coming out of a C call."""

    TOOL = 4  # unassigned by CPython (0 debugger, 1 coverage, 2 profiler, 5 optimizer)

    def __init__(self):
        self.hit = None
        self.active = False
        mon = getattr(sys, "monitoring", None)
        if mon is None:
            return
        try:
            mon.use_tool_id(self.TOOL, "constructicon.parse_limits")
            mon.register_callback(self.TOOL, mon.events.RAISE, self._on_raise)
            mon.set_events(self.TOOL, mon.events.RAISE)
            self.active = True
        except (ValueError, RuntimeError) as e:  # tool id taken: still bounded, only less precise
            log.warning("parse limits: no exception watcher in the child: %r", e)

    def _on_raise(self, code, offset, exc):
        if self.hit is None:
            try:
                self.hit = _classify(exc)
            except Exception:  # silent-ok: str() of a strange exception failed; it isn't a limit we know
                pass

    def stop(self):
        if self.active:
            sys.monitoring.set_events(self.TOOL, 0)
            self.active = False
        return self.hit


def _vm_size_bytes():
    with open("/proc/self/status") as f:
        for line in f:
            if line.startswith("VmSize:"):
                return int(line.split()[1]) * 1024
    return 0


def _child_main(conn, fn, args, seconds, memory_mb, env):
    global _IN_CHILD
    _IN_CHILD = True
    import resource
    try:
        os.setpgid(0, 0)
    except OSError as e:  # still bounded by the limits below; only the group kill is lost
        log.warning("parse child: setpgid failed: %r", e)
    os.environ.update(env)
    apply_image_limits()
    signal.alarm(int(seconds) + 10)
    cpu = int(seconds) + 5
    resource.setrlimit(resource.RLIMIT_CPU, (cpu, cpu + 5))
    ceiling = _vm_size_bytes() + int(memory_mb) * 1024 * 1024
    _soft, hard = resource.getrlimit(resource.RLIMIT_AS)
    if hard != resource.RLIM_INFINITY:
        ceiling = min(ceiling, hard)
    resource.setrlimit(resource.RLIMIT_AS, (ceiling, hard if hard != resource.RLIM_INFINITY else ceiling))
    watcher = _LimitWatcher()
    try:
        try:
            result = fn(*args)
        except BaseException as e:
            hit = watcher.stop() or _classify(e)
            if hit:
                conn.send(("limit", hit))
                return
            try:
                conn.send(("err", e))
            except Exception:  # silent-ok: the exception doesn't pickle; send its text instead
                conn.send(("err", RuntimeError(f"{type(e).__name__}: {e}")))
            return
        hit = watcher.stop()
        if hit and not result:
            conn.send(("limit", hit))
            return
        try:
            conn.send(("ok", result))
        except MemoryError:
            conn.send(("limit", ("memory", "")))
        except Exception as e:  # the result doesn't pickle: a hook bug, reported as an error
            conn.send(("err", RuntimeError(f"parse step result couldn't be sent back: {e!r}")))
    finally:
        conn.close()
