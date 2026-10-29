"""Editable word lists (#556, after the provenance-options pattern of #529).

Two small vocabularies used to be constants in core/db.py:

  vocab 'brand_role'     the suggested roles of a brand asset (capture_events.brand_role, #350):
                         logo, logotype, icon, color, typography, swag, template-asset, other.
                         brand_role stays FREEFORM text: an item may hold a custom role that is
                         not on the list (the item page's "custom" choice); the list only feeds
                         the picker.
  vocab 'wallpaper_tag'  the tag names the Wallpaper page gathers (#422): wallpaper,
                         desktop picture. Matched against tag names case-insensitively.

They now live in one table, `vocab_terms(id, vocab, term, sort_order)`, seeded once by the
`vocab_seed_556` migration from `SEED` (the old constants) and managed from /admin (admins
only). Terms are stored lowercase, so a list can't hold the same word twice in two cases.

Every write goes through the change log (db.ImageLog, `vocab_terms` is in db.IMAGE_TABLE_KEYS),
returns a `Result` with its batch_id, and takes dry_run. The rules:

  * add: a new term at the end of its list (409 vocab_conflict when it is already there).
  * rename: a brand role's rename carries every item holding the old role along with it (their
    capture_events rows are imaged in the same batch, so one undo restores both). A wallpaper
    term's rename only changes which tag name the page gathers; `data` says how many wallpapers
    the page shows before and after.
  * remove: a brand role still held by items is REFUSED (409 vocab_in_use), naming the items;
    merge it into another role instead (merge moves the items and drops the term, one batch).
    Removing a wallpaper term is allowed, and `data.wallpapers_leaving` says how many items
    leave the page. The last term of a list can't be removed (409 vocab_last_term).
  * move: one place up or down.

Reads hit the table every time (a dozen rows), so a write is visible immediately.
"""

import logging
import re
import sqlite3

from . import besteffort, changes, db, item_title
from .cards import Result
from .errors import AppError, NotFound

log = logging.getLogger("constructicon.vocab")

BRAND_ROLE = "brand_role"
WALLPAPER_TAG = "wallpaper_tag"
VOCABS = (BRAND_ROLE, WALLPAPER_TAG)
LABELS = {BRAND_ROLE: "brand role", WALLPAPER_TAG: "wallpaper tag"}

# The seeds: exactly the constants these lists replaced (db.BRAND_ROLES, db.WALLPAPER_TAG_NAMES).
SEED = {
    BRAND_ROLE: ["logo", "logotype", "icon", "color", "typography", "swag", "template-asset", "other"],
    WALLPAPER_TAG: ["wallpaper", "desktop picture"],
}

BRAND_ROLE_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,39}$")
WALLPAPER_TAG_MAX = 100
IN_USE_LISTED = 20  # how many items a vocab_in_use refusal names (the count is always whole)

OP_ADD = "vocab_add"
OP_RENAME = "vocab_rename"
OP_REMOVE = "vocab_remove"
OP_MERGE = "vocab_merge"
OP_MOVE = "vocab_move"

DDL = """
CREATE TABLE IF NOT EXISTS vocab_terms (
    id         INTEGER PRIMARY KEY,
    vocab      TEXT NOT NULL,
    term       TEXT NOT NULL,
    sort_order INTEGER NOT NULL DEFAULT 0,
    UNIQUE (vocab, term)
)"""


class VocabError(AppError):
    """A refused vocab write: 409 for a clash with what is there, else 400. Nothing was written."""

    def __init__(self, code, message, details=None, status=409):
        super().__init__(code, message, status=status, details=details)


def ensure_seed(conn):
    """Inserts any missing seed term (INSERT OR IGNORE: never re-adds or renames what an admin
    changed once the migration has run). Caller commits."""
    conn.execute(DDL)
    for vocab, terms in SEED.items():
        for i, term in enumerate(terms):
            conn.execute("INSERT OR IGNORE INTO vocab_terms (vocab, term, sort_order) VALUES (?, ?, ?)",
                         (vocab, term, (i + 1) * 10))


# --- reads ----------------------------------------------------------------------------------

def _vocab(vocab):
    if vocab not in VOCABS:
        raise VocabError("bad_vocab", f"Unknown word list {vocab!r}. Choose one of: {', '.join(VOCABS)}.",
                         {"vocabs": list(VOCABS)}, status=400)
    return vocab


def _rows(conn, vocab):
    return [dict(r) for r in conn.execute(
        "SELECT id, vocab, term, sort_order FROM vocab_terms WHERE vocab = ? ORDER BY sort_order, term", (vocab,))]


def terms(vocab):
    """The terms of one list, in order (plain strings). A missing table (a minimal schema) falls
    back to the seed, logged."""
    _vocab(vocab)
    conn = db.get_conn()
    try:
        return [r["term"] for r in _rows(conn, vocab)]
    except sqlite3.OperationalError as e:
        besteffort.warn(log, "vocab: the vocab_terms table is unreadable, using the seed list", e, vocab=vocab)
        return list(SEED[vocab])
    finally:
        conn.close()


def _usage(conn, vocab, term):
    """How many items a term reaches: brand role = items holding it; wallpaper tag = non-redacted
    items tagged with a tag of that name (any case)."""
    if vocab == BRAND_ROLE:
        return conn.execute("SELECT COUNT(*) FROM capture_events WHERE brand_role = ?", (term,)).fetchone()[0]
    return conn.execute(
        "SELECT COUNT(DISTINCT ce.slug) FROM capture_events ce JOIN post_tags pt ON pt.post_slug = ce.slug "
        "JOIN blog_tags t ON t.id = pt.tag_id WHERE lower(t.name) = ? AND ce.redacted = 0", (term,)).fetchone()[0]


def _wallpaper_count(conn):
    """Non-redacted items the Wallpaper page gathers right now (before the viewer's policy)."""
    return conn.execute(
        "SELECT COUNT(DISTINCT ce.slug) FROM capture_events ce JOIN post_tags pt ON pt.post_slug = ce.slug "
        "JOIN blog_tags t ON t.id = pt.tag_id WHERE ce.redacted = 0 AND lower(t.name) IN "
        "(SELECT term FROM vocab_terms WHERE vocab = ?)", (WALLPAPER_TAG,)).fetchone()[0]


def list_terms(vocab, with_usage=False):
    """[{id, term, sort_order[, items]}] for one list. `items` (with_usage) counts every item the
    term reaches, whoever can see it: it is for the admin list, which only an admin reads."""
    _vocab(vocab)
    conn = db.get_conn()
    try:
        out = _rows(conn, vocab)
        if with_usage:
            for r in out:
                r["items"] = _usage(conn, vocab, r["term"])
        return [{k: r[k] for k in ("id", "term", "sort_order", "items") if k in r} for r in out]
    finally:
        conn.close()


def _find(conn, vocab, term):
    r = conn.execute("SELECT id, vocab, term, sort_order FROM vocab_terms WHERE vocab = ? AND term = ?",
                     (vocab, term)).fetchone()
    return dict(r) if r else None


def _must_find(conn, vocab, term):
    r = _find(conn, vocab, normalize(vocab, term, check=False))
    if r is None:
        raise NotFound(f"There is no {LABELS[vocab]} {term!r} on the list.", code="not_found",
                       details={"vocab": vocab, "term": term})
    return r


def normalize(vocab, term, check=True):
    """The stored form of a term: brand roles lowercased (a slug: letters, digits, '-' and '_');
    wallpaper tags lowercased with whitespace collapsed. check=False only cleans."""
    _vocab(vocab)
    text = " ".join(str(term or "").split()).lower()
    if not check:
        return text
    if vocab == BRAND_ROLE:
        if not BRAND_ROLE_RE.match(text):
            raise VocabError("bad_vocab_term",
                             "A brand role is one short lowercase word: letters, digits, '-' and '_', starting with "
                             f"a letter or digit, at most 40 characters (got {term!r}). Nothing was changed.",
                             {"vocab": vocab}, status=400)
    elif not text:
        raise VocabError("bad_vocab_term", "A wallpaper tag name can't be empty. Nothing was changed.",
                         {"vocab": vocab}, status=400)
    elif len(text) > WALLPAPER_TAG_MAX:
        raise VocabError("bad_vocab_term",
                         f"A wallpaper tag name is at most {WALLPAPER_TAG_MAX} characters. Nothing was changed.",
                         {"vocab": vocab}, status=400)
    return text


def _result(vocab, op, muts, batch_id, dry_run, **data):
    from .items import _flat  # lazy: items imports a lot
    return Result(True, _flat(op, muts), [], batch_id if muts else None, dry_run,
                  {"vocab": vocab, "terms": list_terms(vocab, with_usage=True), **data})


def _holders(conn, role):
    return [dict(r) for r in conn.execute(
        "SELECT * FROM capture_events WHERE brand_role = ? ORDER BY timestamp DESC, slug", (role,))]


def _in_use_details(rows):
    return {"count": len(rows),
            "items": [{"slug": r["slug"], "title": item_title.title_of(r)} for r in rows[:IN_USE_LISTED]]}


# --- writes ---------------------------------------------------------------------------------

def add(vocab, term, *, dry_run=False, actor=None, batch_id=None):
    """A new term at the end of its list. data: {term, items} (items it already reaches)."""
    text = normalize(vocab, term)
    conn = db.get_conn()
    try:
        if _find(conn, vocab, text) is not None:
            raise VocabError("vocab_conflict", f"The {LABELS[vocab]} list already has {text!r}. Nothing was changed.",
                             {"vocab": vocab, "term": text})
    finally:
        conn.close()
    batch_id = batch_id or changes.new_batch_id()
    with db.transaction(dry_run=dry_run) as tx:
        with db.ImageLog(OP_ADD, actor, batch_id, conn=tx.conn) as lg:
            top = lg.conn.execute("SELECT COALESCE(MAX(sort_order), 0) FROM vocab_terms WHERE vocab = ?",
                                  (vocab,)).fetchone()[0]
            lg.insert_auto("vocab_terms", {"vocab": vocab, "term": text, "sort_order": top + 10})
            muts = list(lg.muts)
        reach = _usage(tx.conn, vocab, text)
        res = _result(vocab, OP_ADD, muts, batch_id, dry_run, term=text, items=reach)
    return res


def rename(vocab, term, new_term, *, dry_run=False, actor=None, batch_id=None):
    """Renames a term. A brand role's items move with it (same batch). data: {old_term, term,
    items_moved (brand role) | wallpapers_before, wallpapers_after (wallpaper tag)}."""
    new = normalize(vocab, new_term)
    conn = db.get_conn()
    try:
        row = _must_find(conn, vocab, term)
        if new == row["term"]:
            return Result(True, [], [f"Nothing changed: the {LABELS[vocab]} is already called {new!r}."], None,
                          dry_run, {"vocab": vocab, "terms": list_terms(vocab, with_usage=True),
                                    "old_term": row["term"], "term": new})
        if _find(conn, vocab, new) is not None:
            hint = (f" To fold {row['term']!r} into {new!r}, merge it instead (its items take the role {new!r})."
                    if vocab == BRAND_ROLE else f" Remove {row['term']!r} instead; {new!r} already gathers its tag.")
            raise VocabError("vocab_conflict",
                             f"Nothing was changed: the {LABELS[vocab]} list already has {new!r}.{hint}",
                             {"vocab": vocab, "term": row["term"], "existing": new})
    finally:
        conn.close()
    batch_id = batch_id or changes.new_batch_id()
    with db.transaction(dry_run=dry_run) as tx:
        before = _wallpaper_count(tx.conn) if vocab == WALLPAPER_TAG else None
        holders = _holders(tx.conn, row["term"]) if vocab == BRAND_ROLE else []
        with db.ImageLog(OP_RENAME, actor, batch_id, [h["slug"] for h in holders], conn=tx.conn) as lg:
            lg.update("vocab_terms", {"id": row["id"]}, {"term": new})
            for h in holders:
                lg.update("capture_events", {"slug": h["slug"]}, {"brand_role": new})
            muts = list(lg.muts)
        extra = ({"items_moved": len(holders)} if vocab == BRAND_ROLE
                 else {"wallpapers_before": before, "wallpapers_after": _wallpaper_count(tx.conn)})
        res = _result(vocab, OP_RENAME, muts, batch_id, dry_run, old_term=row["term"], term=new, **extra)
    return res


def _refuse_last(conn, vocab, row, verb):
    if len(_rows(conn, vocab)) <= 1:
        raise VocabError("vocab_last_term",
                         f"Nothing was changed: {row['term']!r} is the only {LABELS[vocab]} left, and the list needs at "
                         f"least one. Add another before you {verb} this one.", {"vocab": vocab, "term": row["term"]})


def remove(vocab, term, *, dry_run=False, actor=None, batch_id=None):
    """Removes a term. A brand role still held by items is refused (409 vocab_in_use, naming them):
    merge it into another role instead. data: {term, wallpapers_leaving (wallpaper tag)}."""
    conn = db.get_conn()
    try:
        row = _must_find(conn, vocab, term)
        _refuse_last(conn, vocab, row, "remove")
        if vocab == BRAND_ROLE:
            holders = _holders(conn, row["term"])
            if holders:
                det = _in_use_details(holders)
                names = ", ".join(f"'{i['title']}'" for i in det["items"][:5])
                more = f" and {len(holders) - 5} more" if len(holders) > 5 else ""
                raise VocabError("vocab_in_use",
                                 f"Nothing was changed: the brand role {row['term']!r} is still used by {len(holders)} "
                                 f"item{'s' if len(holders) != 1 else ''} ({names}{more}). Merge it into another role "
                                 "(its items move there), or change those items' role first.",
                                 {"vocab": vocab, "term": row["term"], **det})
    finally:
        conn.close()
    batch_id = batch_id or changes.new_batch_id()
    with db.transaction(dry_run=dry_run) as tx:
        before = _wallpaper_count(tx.conn) if vocab == WALLPAPER_TAG else None
        with db.ImageLog(OP_REMOVE, actor, batch_id, conn=tx.conn) as lg:
            lg.delete("vocab_terms", {"id": row["id"]})
            muts = list(lg.muts)
        extra = {"wallpapers_leaving": before - _wallpaper_count(tx.conn)} if vocab == WALLPAPER_TAG else {}
        res = _result(vocab, OP_REMOVE, muts, batch_id, dry_run, term=row["term"], **extra)
    return res


def merge(vocab, term, into, *, dry_run=False, actor=None, batch_id=None):
    """Brand roles only: every item holding `term` takes the role `into` (which must be on the
    list), and `term` leaves the list. One batch. data: {term, into, items_moved}."""
    if _vocab(vocab) != BRAND_ROLE:
        raise VocabError("bad_vocab_merge",
                         "Only brand roles merge (their items move to the other role). A wallpaper tag is just "
                         "removed. Nothing was changed.", {"vocab": vocab}, status=400)
    conn = db.get_conn()
    try:
        row = _must_find(conn, vocab, term)
        target = _must_find(conn, vocab, into)
        if target["id"] == row["id"]:
            raise VocabError("bad_vocab_merge", f"Nothing was changed: can't merge {row['term']!r} into itself.",
                             {"vocab": vocab, "term": row["term"]}, status=400)
    finally:
        conn.close()
    batch_id = batch_id or changes.new_batch_id()
    with db.transaction(dry_run=dry_run) as tx:
        holders = _holders(tx.conn, row["term"])
        with db.ImageLog(OP_MERGE, actor, batch_id, [h["slug"] for h in holders], conn=tx.conn) as lg:
            for h in holders:
                lg.update("capture_events", {"slug": h["slug"]}, {"brand_role": target["term"]})
            lg.delete("vocab_terms", {"id": row["id"]})
            muts = list(lg.muts)
        res = _result(vocab, OP_MERGE, muts, batch_id, dry_run, term=row["term"], into=target["term"],
                      items_moved=len(holders))
    return res


def move(vocab, term, direction, *, dry_run=False, actor=None, batch_id=None):
    """Moves a term one place 'up' or 'down' (a no-op at either end: no batch)."""
    if direction not in ("up", "down"):
        raise VocabError("bad_vocab_move", "direction must be 'up' or 'down'. Nothing was changed.",
                         {"vocab": vocab}, status=400)
    conn = db.get_conn()
    try:
        row = _must_find(conn, vocab, term)
        order = [r["id"] for r in _rows(conn, vocab)]
    finally:
        conn.close()
    i = order.index(row["id"])
    j = i - 1 if direction == "up" else i + 1
    if j < 0 or j >= len(order):
        return Result(True, [], [f"Nothing changed: {row['term']!r} is already at the {'top' if j < 0 else 'bottom'}."],
                      None, dry_run, {"vocab": vocab, "terms": list_terms(vocab, with_usage=True), "term": row["term"]})
    order[i], order[j] = order[j], order[i]
    batch_id = batch_id or changes.new_batch_id()
    with db.transaction(dry_run=dry_run) as tx:
        with db.ImageLog(OP_MOVE, actor, batch_id, conn=tx.conn) as lg:
            for n, tid in enumerate(order):  # dense renumber, so ties can't make a move a silent no-op
                lg.update("vocab_terms", {"id": tid}, {"sort_order": (n + 1) * 10})
            muts = list(lg.muts)
        res = _result(vocab, OP_MOVE, muts, batch_id, dry_run, term=row["term"])
    return res
