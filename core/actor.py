"""Who is doing this? The request-scoped actor (#560, phase A of the service layer #541).

Every change-log row (`audit_log.actor`) and every request-log row records an actor. It
comes from a `contextvars.ContextVar` set once per entry point, never from a string literal
at a call site:

  * HTTP  -> web/middleware.py's ActorMiddleware sets ACTOR_ANONYMOUS per request; web/auth.py
             overrides it with "user:<name>" (a live session) or ACTOR_TOKEN (the install token).
  * MCP   -> mcp_server/server.py's tool registration wraps every tool in acting_as(ACTOR_MCP).
  * Boot, the OCR watchdog, the caption queue worker and other background work -> ACTOR_SYSTEM.
  * A script run from scripts/, or a tool run as `python3 -m core.manage.<tool>` (#634)
             -> ACTOR_SCRIPT by default (see _process_default).

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

from . import besteffort

ACTOR_UI = "owner-ui"        # legacy: an anonymous web request before #467 step 2 (old log rows; in-process tests)
ACTOR_ANONYMOUS = "anonymous"  # #467 step 2: a web request with no live session and no install token
ACTOR_TOKEN = "token"        # #467 step 2: a web request carrying the install token (scripts)
ACTOR_MCP = "mcp"
ACTOR_SYSTEM = "system"      # boot, migrations' surrounding work, OCR/caption workers
ACTOR_SCRIPT = "script"      # a CLI script under scripts/ or core/manage/ calling core directly
ACTOR_MIGRATION = "migration"  # one-time data migrations' change-log rows (not undoable)

_current = contextvars.ContextVar("constructicon_actor", default=None)
_log = logging.getLogger("constructicon.actor")
_warned_sites = set()
_warned_lock = threading.Lock()

_CORE_DIR = os.path.dirname(os.path.abspath(__file__))
_REPO_DIR = os.path.dirname(_CORE_DIR)
_SKIP_FILES = (contextlib.__file__, threading.__file__)


# Where a process counts as a script: scripts/ (tests, checks, maintenance tools) and core/manage/
# (the operational tools moved into the app by #634, run as `python3 -m core.manage.<tool>`; with -m,
# sys.argv[0] is the module's file path, so the same check covers both).
_SCRIPT_DIRS = (os.path.join(_REPO_DIR, "scripts"), os.path.join(_CORE_DIR, "manage"))


def _process_default():
    """ACTOR_SCRIPT when this process was started as a script from scripts/ or core/manage/, else None."""
    try:
        main = os.path.abspath(sys.argv[0]) if sys.argv and sys.argv[0] else ""
    except Exception as e:
        besteffort.warn(_log, "actor: reading sys.argv[0]", e)
        return None
    if main and os.path.dirname(main) in _SCRIPT_DIRS:
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


# --- #560 / #778: HOW it arrived (the channel) and WHICH personal MCP key did it ----------------
# The actor says who; the channel says how a write reached the app (capture_events.channel): the web
# app or its API ("web"), the MCP ("mcp"), a script, the system, a migration. A per-user MCP token
# (#604 step 5) runs as `user:<name>` like a web session does, so the actor alone can't tell them apart;
# the MCP's tool wrapper sets the channel and the token's id here (acting_via) for the call. The token
# id rides on what the call creates and on its change-log rows, so the credit "<user>'s <token name>"
# is rendered from the token's CURRENT name at read time (core/mcp_tokens.credit).
CHANNEL_WEB = "web"
CHANNEL_MCP = "mcp"
CHANNEL_SCRIPT = "script"
CHANNEL_SYSTEM = "system"
CHANNEL_MIGRATION = "migration"
MIGRATION_CHANNEL_PREFIX = "migration:"  # "migration:<source>": content brought in from somewhere else

_via = contextvars.ContextVar("constructicon_via", default=None)  # {"channel", "token_id", "actor"}


def channel_for_actor(actor):
    """The channel an actor implies when nothing set one explicitly: the web's own actors (a session,
    the install token, anonymous, the legacy owner-ui) -> web; mcp -> mcp; script / system / migration
    -> themselves."""
    if actor == ACTOR_MCP:
        return CHANNEL_MCP
    if actor in (ACTOR_SCRIPT, ACTOR_SYSTEM, ACTOR_MIGRATION):
        return actor
    return CHANNEL_WEB  # user:<name>, token, anonymous, owner-ui


def current_channel():
    """The channel of the code running now (acting_via's, else the actor's)."""
    via = _via.get()
    if via and via.get("channel"):
        return via["channel"]
    return channel_for_actor(current_actor())


def token_id_for(actor=None):
    """The personal MCP token id behind this write, or None. Only when `actor` (default: the
    context's) is the user the token belongs to, so an explicit actor override never borrows a
    credit; the install token and the web have none."""
    via = _via.get()
    if not via or via.get("token_id") is None:
        return None
    return via["token_id"] if resolve(actor) == via.get("actor") else None


@contextlib.contextmanager
def acting_via(channel, token_id=None):
    """Run a block as arriving through `channel` (and, for a per-user MCP token, that token's id,
    tied to the actor in force when the block starts)."""
    token = _via.set({"channel": channel, "token_id": token_id, "actor": current_actor() if token_id else None})
    try:
        yield channel
    finally:
        _via.reset(token)


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
