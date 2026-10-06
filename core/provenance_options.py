"""Editable provenance lists (#529).

Two vocabularies describe where a thing came from, and both used to be fixed in code:

  scope 'card'  projects.provenance        created / found / collected / referenced / client_owned
  scope 'file'  capture_events.provenance  found / created / documented / result / reference / design

They now live in one small table, `provenance_options(scope, key, label, sort_order,
retired_at)`, seeded idempotently from those lists (plus "Purchased" in both) and managed
from /admin. The rules the owner set:

  * keys are stable lowercase slugs; renaming changes only the label;
  * a RETIRED option can't be picked for a new write, but a record that already holds it
    keeps it, still validates unchanged, and still displays with its label;
  * every write goes through the change log (db.ImageLog), so it's undoable.

Reads hit the table every time (it has a dozen rows), so a write is always visible
immediately and there is no cache to invalidate. Validation errors use the same
`bad_provenance` code the fixed lists did. The static export stores no vocabulary of its
own, so it is untouched.
"""

import logging
import re
import sqlite3
import time

from . import besteffort, changes, db
from .card_rules import CardError

log = logging.getLogger("constructicon.provenance_options")

SCOPES = ("card", "file")

# Seed data: (key, label) in picker order. Card labels are exactly what card_rules showed;
# file labels are the title-cased key (the old picker showed the bare key). The old
# file -> card-vocabulary reading on asset cards (card_rules.FILE_PROVENANCE_LABELS) is
# kept for the legacy six keys, see card_rules.file_provenance_label.
SEED = {
    "card": [
        ("created", "Created"),
        ("found", "Found"),
        ("collected", "Collected"),
        ("referenced", "Referenced"),
        ("client_owned", "Client-owned"),
        ("purchased", "Purchased"),
    ],
    "file": [
        ("found", "Found"),
        ("created", "Created"),
        ("documented", "Documented"),
        ("result", "Result"),
        ("reference", "Reference"),
        ("design", "Design"),
        ("purchased", "Purchased"),
    ],
}

KEY_RE = re.compile(r"^[a-z0-9][a-z0-9_]{0,39}$")
LABEL_MAX = 60

DDL = """
CREATE TABLE IF NOT EXISTS provenance_options (
    scope      TEXT NOT NULL,
    key        TEXT NOT NULL,
    label      TEXT NOT NULL,
    sort_order INTEGER NOT NULL DEFAULT 0,
    retired_at REAL,
    PRIMARY KEY (scope, key)
)"""


def ensure_table_and_seed(conn):
    """Creates the table and inserts any missing seed row. INSERT OR IGNORE only: it never
    renames, un-retires or re-adds something the owner changed, so it is safe on every
    init_db. Caller commits."""
    conn.execute(DDL)
    for scope, rows in SEED.items():
        for i, (key, label) in enumerate(rows):
            conn.execute(
                "INSERT OR IGNORE INTO provenance_options (scope, key, label, sort_order, retired_at) "
                "VALUES (?, ?, ?, ?, NULL)", (scope, key, label, (i + 1) * 10))


def _scope(scope):
    if scope not in SCOPES:
        raise CardError("bad_provenance_scope", f"Unknown provenance list {scope!r}. Choose one of: {', '.join(SCOPES)}.")
    return scope


def _row(r):
    return {"scope": r["scope"], "key": r["key"], "label": r["label"], "sort_order": r["sort_order"],
            "retired": r["retired_at"] is not None, "retired_at": r["retired_at"]}


def list_options(scope, include_retired=False):
    """Options of one list in picker order (sort_order, then key)."""
    _scope(scope)
    conn = db.get_conn()
    try:
        sql = "SELECT * FROM provenance_options WHERE scope = ?"
        if not include_retired:
            sql += " AND retired_at IS NULL"
        return [_row(r) for r in conn.execute(sql + " ORDER BY sort_order, key", (scope,))]
    finally:
        conn.close()


def active_keys(scope):
    return [o["key"] for o in list_options(scope)]


def get_option(scope, key):
    conn = db.get_conn()
    try:
        r = conn.execute("SELECT * FROM provenance_options WHERE scope = ? AND key = ?", (_scope(scope), key)).fetchone()
        return _row(r) if r else None
    finally:
        conn.close()


def label(scope, key, default=None):
    """The current label for `key`, active or retired. Unknown keys fall back to
    `default` (the raw key when omitted), so old data never displays blank."""
    if not key:
        return default if default is not None else ""
    try:
        o = get_option(scope, key)
    except sqlite3.Error as e:  # table absent (a minimal schema): fall back to the raw key
        besteffort.warn(log, "provenance_options: label lookup, falling back to the raw key", e, scope=scope, key=key)
        o = None
    return o["label"] if o else (default if default is not None else key)


def picker_options(scope, current=None):
    """[{key, label, retired}] for a <select>: every active option, plus the record's
    CURRENT value when it is retired, so editing a record never silently drops it."""
    out = [{"key": o["key"], "label": o["label"], "retired": False} for o in list_options(scope)]
    if current and all(o["key"] != current for o in out):
        cur = get_option(scope, current)
        if cur is not None:
            out.append({"key": cur["key"], "label": cur["label"] + " (retired)", "retired": True})
    return out


def validate(scope, value, current=None):
    """None / '' clear. Otherwise the value must be an ACTIVE key, or equal `current`
    (the value the record already holds, even if retired). Raises CardError
    'bad_provenance' naming the active keys; returns the value unchanged."""
    if value in (None, ""):
        return None
    _scope(scope)
    if current is not None and value == current and get_option(scope, value) is not None:
        return value
    opt = get_option(scope, value)
    active = active_keys(scope)
    if opt is None:
        raise CardError("bad_provenance",
                        f"Unknown provenance {value!r}. Choose one of: {', '.join(active)}.",
                        {"scope": scope, "active_keys": active})
    if opt["retired"]:
        raise CardError("bad_provenance",
                        f"Provenance {value!r} is retired: it stays on records that already have it but can't be "
                        f"set on others. Choose one of: {', '.join(active)}.",
                        {"scope": scope, "active_keys": active})
    return value


# --- Writes (change-logged, so undoable) ---------------------------------------

def _clean_label(text):
    text = " ".join((text or "").split())
    if not text:
        raise CardError("bad_provenance_label", "A provenance label can't be empty.")
    if len(text) > LABEL_MAX:
        raise CardError("bad_provenance_label", f"A provenance label is at most {LABEL_MAX} characters.")
    return text


def _log(op, actor, batch_id):
    return db.ImageLog(op, actor, batch_id or changes.new_batch_id())


def _must_exist(scope, key):
    o = get_option(scope, key)
    if o is None:
        raise CardError("not_found", f"No {scope} provenance option {key!r}.")
    return o


def add(scope, key, label_text, actor=None, batch_id=None):
    """A new active option at the end of the list. The key must be a lowercase slug and
    unique within the scope (a retired key still counts, un-retire it instead)."""
    _scope(scope)
    key = (key or "").strip()
    if not KEY_RE.match(key):
        raise CardError("bad_provenance_key",
                        "A provenance key is a lowercase slug: letters, digits and underscores, starting with a "
                        f"letter or digit, at most 40 characters (got {key!r}).")
    text = _clean_label(label_text)
    if get_option(scope, key) is not None:
        raise CardError("provenance_conflict", f"The {scope} list already has a {key!r} option.")
    with _log("provenance_option_add", actor, batch_id) as log:
        top = log.conn.execute("SELECT COALESCE(MAX(sort_order), 0) AS m FROM provenance_options WHERE scope = ?",
                               (scope,)).fetchone()["m"]
        log.insert("provenance_options", {"scope": scope, "key": key},
                   {"label": text, "sort_order": top + 10, "retired_at": None})
    return get_option(scope, key)


def rename(scope, key, label_text, actor=None, batch_id=None):
    """Changes only the display label; the key (and every record using it) is untouched."""
    _must_exist(scope, key)
    text = _clean_label(label_text)
    with _log("provenance_option_rename", actor, batch_id) as log:
        log.update("provenance_options", {"scope": scope, "key": key}, {"label": text})
    return get_option(scope, key)


def retire(scope, key, actor=None, batch_id=None):
    """Hides the option from every picker and refuses it for new writes. Records that
    hold it are unchanged. The last active option of a list can't be retired."""
    o = _must_exist(scope, key)
    if not o["retired"] and len(list_options(scope)) <= 1:
        raise CardError("provenance_conflict", f"The {scope} list needs at least one active option.")
    with _log("provenance_option_retire", actor, batch_id) as log:
        log.update("provenance_options", {"scope": scope, "key": key},
                   {"retired_at": o["retired_at"] or time.time()})
    return get_option(scope, key)


def unretire(scope, key, actor=None, batch_id=None):
    _must_exist(scope, key)
    with _log("provenance_option_unretire", actor, batch_id) as log:
        log.update("provenance_options", {"scope": scope, "key": key}, {"retired_at": None})
    return get_option(scope, key)


def move(scope, key, direction, actor=None, batch_id=None):
    """Moves an option one place 'up' or 'down' (among all options, retired ones too, so
    the order is stable when something is un-retired). Swaps sort_order with the
    neighbour; a no-op at either end."""
    if direction not in ("up", "down"):
        raise CardError("bad_provenance_move", "direction must be 'up' or 'down'.")
    _must_exist(scope, key)
    opts = list_options(scope, include_retired=True)
    i = next(n for n, o in enumerate(opts) if o["key"] == key)
    j = i - 1 if direction == "up" else i + 1
    if j < 0 or j >= len(opts):
        return opts
    # Renumber the whole list densely (10, 20, ...) with the two swapped, so ties
    # from hand-edited data can't make a move a silent no-op.
    order = [o["key"] for o in opts]
    order[i], order[j] = order[j], order[i]
    with _log("provenance_option_move", actor, batch_id) as log:
        for n, k in enumerate(order):
            log.update("provenance_options", {"scope": scope, "key": k}, {"sort_order": (n + 1) * 10})
    return list_options(scope, include_retired=True)
