"""Who is doing this? The request-scoped actor (#560, phase A of the service layer #541).

Every change-log row (`audit_log.actor`) and every request-log row records an actor. It
comes from a `contextvars.ContextVar` set once per entry point, never from a string literal
at a call site:

  * HTTP  -> web/middleware.py's ActorMiddleware sets ACTOR_UI per request (the hook where
             #467 will later put the logged-in user).
  * MCP   -> mcp_server/server.py's tool registration wraps every tool in acting_as(ACTOR_MCP).
  * Boot, the OCR watchdog, the caption queue worker and other background work -> ACTOR_SYSTEM.
  * A script run from scripts/ -> ACTOR_SCRIPT by default (see _process_default).

Core operations keep an `actor=` keyword for an explicit override, defaulting to None, which
means "whoever the context says" (resolved by `resolve()` at the point the row is written).

ContextVars do not cross `threading.Thread` on their own. Start threads with `spawn()` (or wrap
the target with `carry()`), which copies the current context into the new thread. asyncio tasks,
Starlette's run_in_threadpool, BackgroundTasks and anyio.to_thread copy the context already.

No context at all (a code path nobody wrapped) falls back to ACTOR_SYSTEM and logs one warning
per call site, so a missed entry point shows up in the logs without breaking the owner's scripts.
"""

import contextlib
import contextvars
import logging
import os
import sys
import threading

ACTOR_UI = "owner-ui"
ACTOR_MCP = "mcp"
ACTOR_SYSTEM = "system"      # boot, migrations' surrounding work, OCR/caption workers
ACTOR_SCRIPT = "script"      # a CLI script under scripts/ calling core directly
ACTOR_MIGRATION = "migration"  # card_migration's change-log rows (not undoable)

_current = contextvars.ContextVar("constructicon_actor", default=None)
_log = logging.getLogger("constructicon.actor")
_warned_sites = set()
_warned_lock = threading.Lock()

_CORE_DIR = os.path.dirname(os.path.abspath(__file__))
_REPO_DIR = os.path.dirname(_CORE_DIR)
_SKIP_FILES = (contextlib.__file__, threading.__file__)


def _process_default():
    """ACTOR_SCRIPT when this process was started as a script from scripts/, else None."""
    try:
        main = os.path.abspath(sys.argv[0]) if sys.argv and sys.argv[0] else ""
    except Exception:
        return None
    if main and os.path.dirname(main) == os.path.join(_REPO_DIR, "scripts"):
        return ACTOR_SCRIPT
    return None


_PROCESS_DEFAULT = _process_default()


def _call_site():
    """(file, line) of the first frame outside core/, i.e. the code that called into core
    without an actor context. Falls back to the outermost core frame."""
    frame = sys._getframe(2)
    last = frame
    while frame is not None:
        fn = os.path.abspath(frame.f_code.co_filename)
        if not fn.startswith(_CORE_DIR + os.sep) and fn not in _SKIP_FILES:
            return fn, frame.f_lineno
        last = frame
        frame = frame.f_back
    return os.path.abspath(last.f_code.co_filename), last.f_lineno


def current_actor():
    """The actor for the code running now. Never None."""
    actor = _current.get()
    if actor:
        return actor
    if _PROCESS_DEFAULT:
        return _PROCESS_DEFAULT
    site = _call_site()
    with _warned_lock:
        first = site not in _warned_sites
        _warned_sites.add(site)
    if first:
        _log.warning("no actor context at %s:%d; recording the write as %r", site[0], site[1], ACTOR_SYSTEM)
    return ACTOR_SYSTEM


def resolve(actor=None):
    """An explicit actor if one was passed, else the context's."""
    return actor or current_actor()


def has_context():
    return bool(_current.get())


@contextlib.contextmanager
def acting_as(actor):
    """Run a block as `actor`; restores the previous actor afterwards (nests safely)."""
    token = _current.set(actor)
    try:
        yield actor
    finally:
        _current.reset(token)


def set_actor(actor):
    """Set the actor for the rest of the current context (a thread's or task's body).
    Returns the token for `_current.reset` if the caller wants to undo it."""
    return _current.set(actor)


def carry(fn):
    """`fn` bound to a copy of the current context, for handing to another thread."""
    ctx = contextvars.copy_context()

    def run(*args, **kwargs):
        return ctx.run(fn, *args, **kwargs)

    run.__name__ = getattr(fn, "__name__", "carried")
    return run


def spawn(fn, *args, name=None, daemon=True):
    """threading.Thread(target=fn, args=args).start(), carrying the current actor."""
    t = threading.Thread(target=carry(fn), args=args, name=name, daemon=daemon)
    t.start()
    return t
