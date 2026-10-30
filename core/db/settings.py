"""The app settings key/value table.

Part of the core.db package (#547): import it as `from core import db` and use `db.<name>`;
core/db/__init__.py re-exports every name.
"""


from .conn import get_conn


def get_setting(key):
    """Reads one app_settings value (#55) — a generic key/value store for
    secrets and other app-level settings the app needs to remember across
    restarts (starting with a YouTube Data API key, see core/object_types.py's
    sibling issue #54), so a new integration doesn't need a docker-compose
    env var wired in from outside the app. Returns None if `key` was never
    set. Callers that only need to know whether a value is present (e.g. the
    admin pane's masked status indicator) should call has_setting instead —
    the real value should only ever be read by the code that actually needs
    to use it (never logged, never handed back to a browser)."""
    conn = get_conn()
    try:
        row = conn.execute("SELECT value FROM app_settings WHERE key = ?", (key,)).fetchone()
        return row["value"] if row else None
    finally:
        conn.close()


def has_setting(key):
    """True if `key` currently has a non-empty stored value. This is the
    presence-only check GET /api/settings and the admin pane's "Set" /
    "Not set" indicator use, so the real value never needs to round-trip
    back to the browser just to show whether one exists."""
    return bool(get_setting(key))


def set_setting(key, value):
    """Upserts one app_settings value. An empty/falsy `value` deletes the
    row instead of storing an empty string, so has_setting's presence check
    and "was this ever cleared" agree with each other. Plain-column storage
    (no encryption) — an accepted tradeoff for a single-owner, LAN-only,
    already-unauthenticated app (see web/app.py's module docstring); still
    never logged and never echoed back to a caller."""
    conn = get_conn()
    try:
        if value:
            conn.execute(
                "INSERT INTO app_settings (key, value) VALUES (?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                (key, value),
            )
        else:
            conn.execute("DELETE FROM app_settings WHERE key = ?", (key,))
        conn.commit()
    finally:
        conn.close()
