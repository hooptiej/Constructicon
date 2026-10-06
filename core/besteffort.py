"""Best-effort failures: stay non-fatal, but never silent (#551 item 5).

An optional step (a thumbnail, a metadata extractor on a strange file, a probe of a
service that may be down) must not break the operation around it, but it must leave a
trace. ``warn`` logs a warning with the site name, the exception and any context
(slug, path, ...).

Hot paths (a page render, a poll) can hit the same failure thousands of times, so each
(logger, site) is rate limited: the first failure in a window is logged in full (with the
traceback), later ones only bump a counter that the next logged line reports.

Usage::

    log = logging.getLogger("constructicon.captions")
    ...
    except Exception as e:
        besteffort.warn(log, "ollama health probe", e, url=url)

``scripts/check_no_silent_except.py`` fails on an ``except`` that neither logs nor
re-raises; a handler that is pure control flow or input validation instead carries a
``# silent-ok: <reason>`` comment.
"""
import threading
import time

WINDOW_SECONDS = 60.0

_lock = threading.Lock()
_state = {}  # (logger name, site) -> [last_logged_at, suppressed_since]


def warn(log, site, exc=None, **ctx):
    """Log a best-effort failure at warning level, rate limited per (logger, site)."""
    key = (log.name, site)
    now = time.monotonic()
    with _lock:
        last, suppressed = _state.get(key, (None, 0))
        if last is not None and now - last < WINDOW_SECONDS:
            _state[key] = [last, suppressed + 1]
            return
        _state[key] = [now, 0]
    parts = [f"{k}={v!r}" for k, v in ctx.items() if v is not None]
    if suppressed:
        parts.append(f"{suppressed} similar failures suppressed in the last {int(WINDOW_SECONDS)}s")
    detail = f" ({', '.join(parts)})" if parts else ""
    log.warning(
        "best-effort step failed, continuing: %s: %r%s", site, exc, detail,
        exc_info=exc if isinstance(exc, BaseException) else None,
    )


def reset():
    """Forget the rate-limit state (tests)."""
    with _lock:
        _state.clear()
