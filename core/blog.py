"""Blog-entry service (#541 phase D): the ONE place blog entries are written.

Same contract as core/cards.py: validate everything first, one `db.transaction()`, every row
written through `db.ImageLog` (row images in the change log), a `Result`, `dry_run`, and `actor`
(None = the request's actor context). Undo is the generic `cards.undo`. The web routes
(web/routes/blog_export.py) and the MCP tools are thin adapters over these.

  create(title, subtitle, body, status, cover_slug, content_date)   a new entry (unique slug)
  update(entry, title=None, ..., cover_slug=..., content_date=...)  None = leave alone; for
                                       cover_slug / content_date the ... sentinel means "leave
                                       alone" and None clears (the old db.update_blog_entry rules)
  delete(entry)                        the entry and its attached cards/items rows
  set_projects(entry, [(card, note)])  the ordered card list (full replace)
  set_items(entry, [(slug, note)])     the ordered file list (full replace)

`entry` is an entry slug (a string is ALWAYS a slug, #318) or an int id. A card in set_projects is
an id or a slug. Unknown cards / files are refused (not_found) instead of being stored as rows no
page can show.
"""

import time

from . import changes, db
from .cards import Result, _changes_from_log
from .errors import InvalidInput, NotFound

OP_CREATE = "blog_entry_create"
OP_UPDATE = "blog_entry_update"
OP_DELETE = "blog_entry_delete"
OP_SET_PROJECTS = "blog_entry_set_projects"
OP_SET_ITEMS = "blog_entry_set_items"


def get_entry(entry):
    row = db.get_blog_entry(entry) if entry not in (None, "") else None
    if row is None:
        raise NotFound(f"No blog entry {entry!r}.")
    return row


def _date(value, name="content_date"):
    if value is None:
        return None
    if isinstance(value, bool):
        raise InvalidInput(f"Invalid {name} format")
    try:
        return float(value)
    except (TypeError, ValueError):
        raise InvalidInput(f"Invalid {name} format") from None


def _done(entry_id, batch_id):
    """(the batch's change-log rows, the entry as it is now) -- read inside the transaction, so a
    dry run reports what it would have written."""
    rows = db.get_change_rows(batch_id=batch_id)
    entry = db.get_blog_entry(entry_id) if entry_id is not None else None
    return rows, entry


def create(title, subtitle="", body="", status="draft", cover_slug=None, content_date=None, *, dry_run=False,
           actor=None, batch_id=None):
    """A new entry; its slug comes from the title (numeric suffix on a clash). data: {entry}."""
    title = (title or "").strip()
    if not title:
        raise InvalidInput("Title is required")
    content_date = _date(content_date)
    batch_id = batch_id or changes.new_batch_id()
    with db.transaction(dry_run=dry_run):
        with db.ImageLog(OP_CREATE, actor, batch_id) as log:
            base = slug = db._slugify(title)
            n = 2
            while log.conn.execute("SELECT 1 FROM blog_entries WHERE slug = ?", (slug,)).fetchone():
                slug = f"{base}-{n}"
                n += 1
            now = time.time()
            entry_id = log.insert_auto("blog_entries", {
                "slug": slug, "title": title, "subtitle": subtitle or "", "body": body or "",
                "status": status or "draft", "cover_slug": cover_slug, "content_date": content_date,
                "created_at": now, "updated_at": now})
            log.slugs.append(f"blog:{slug}")
        rows, entry = _done(entry_id, batch_id)
    return Result(True, _changes_from_log(rows), [], batch_id, dry_run, {"entry": entry})


def update(entry, *, title=None, subtitle=None, body=None, status=None, cover_slug=..., content_date=...,
           dry_run=False, actor=None, batch_id=None):
    """Partial update; bumps updated_at (every call, as before). data: {entry}."""
    row = get_entry(entry)
    fields = {}
    for k, v in (("title", title), ("subtitle", subtitle), ("body", body), ("status", status)):
        if v is not None:
            fields[k] = v
    if cover_slug is not ...:
        fields["cover_slug"] = cover_slug or None
    if content_date is not ...:
        fields["content_date"] = _date(content_date)
    fields["updated_at"] = time.time()
    batch_id = batch_id or changes.new_batch_id()
    with db.transaction(dry_run=dry_run):
        with db.ImageLog(OP_UPDATE, actor, batch_id, [f"blog:{row['slug']}"]) as log:
            log.update("blog_entries", {"id": row["id"]}, fields)
        rows, fresh = _done(row["id"], batch_id)
    return Result(True, _changes_from_log(rows), [], batch_id, dry_run, {"entry": fresh})


def _rows(conn, table, entry_id, key_col):
    return [dict(r) for r in conn.execute(
        f"SELECT {key_col} FROM {table} WHERE entry_id = ? ORDER BY sort_order, rowid", (entry_id,))]


def delete(entry, *, dry_run=False, actor=None, batch_id=None):
    """Deletes the entry and its attached card and file rows (the cards and files themselves stay).
    data: {deleted: slug}."""
    row = get_entry(entry)
    batch_id = batch_id or changes.new_batch_id()
    with db.transaction(dry_run=dry_run):
        with db.ImageLog(OP_DELETE, actor, batch_id, [f"blog:{row['slug']}"]) as log:
            for r in _rows(log.conn, "blog_entry_projects", row["id"], "project_id"):
                log.delete("blog_entry_projects", {"entry_id": row["id"], "project_id": r["project_id"]})
            for r in _rows(log.conn, "blog_entry_items", row["id"], "post_slug"):
                log.delete("blog_entry_items", {"entry_id": row["id"], "post_slug": r["post_slug"]})
            log.delete("blog_entries", {"id": row["id"]})
        rows, _ = _done(None, batch_id)
    return Result(True, _changes_from_log(rows), [], batch_id, dry_run, {"deleted": row["slug"]})


def _replace(log, table, key_col, entry_id, wanted):
    """Full replace of an entry's ordered rows: what's gone is deleted, the rest is (re)written with
    sort_order = its index and the given note. A row that didn't change writes nothing."""
    current = {r[key_col]: r for r in (dict(x) for x in log.conn.execute(
        f"SELECT * FROM {table} WHERE entry_id = ?", (entry_id,)))}
    keep = {k for k, _ in wanted}
    for k in current:
        if k not in keep:
            log.delete(table, {"entry_id": entry_id, key_col: k})
    for i, (k, note) in enumerate(wanted):
        key = {"entry_id": entry_id, key_col: k}
        values = {"sort_order": i, "note": note or ""}
        if k in current:
            log.update(table, key, values)
        else:
            log.insert(table, key, values)


def _pairs(items, what):
    out = []
    for it in items or []:
        if isinstance(it, dict):
            raise InvalidInput(f"Each {what} must be a ({what}, note) pair")
        ref, note = (it[0], it[1] if len(it) > 1 else "") if isinstance(it, (list, tuple)) else (it, "")
        if ref is None or ref == "":
            raise InvalidInput(f"{what} is required")
        out.append((ref, note))
    return out


def set_projects(entry, items, *, dry_run=False, actor=None, batch_id=None):
    """The entry's ordered card list, full replace. `items`: [(card id or slug, note)]. Every card
    must exist (not_found) and appear once (bad_request). data: {entry}."""
    row = get_entry(entry)
    wanted, seen = [], set()
    for ref, note in _pairs(items, "project_id"):
        card = db.get_project(ref)
        if card is None:
            raise NotFound(f"No card {ref!r}.")
        if card["id"] in seen:
            raise InvalidInput(f"Card {card['slug']!r} is listed twice")
        seen.add(card["id"])
        wanted.append((card["id"], note))
    batch_id = batch_id or changes.new_batch_id()
    with db.transaction(dry_run=dry_run):
        with db.ImageLog(OP_SET_PROJECTS, actor, batch_id, [f"blog:{row['slug']}"]) as log:
            _replace(log, "blog_entry_projects", "project_id", row["id"], wanted)
        rows, fresh = _done(row["id"], batch_id)
    return Result(True, _changes_from_log(rows), [], batch_id, dry_run, {"entry": fresh})


def set_items(entry, items, *, dry_run=False, actor=None, batch_id=None):
    """The entry's ordered file list, full replace. `items`: [(file slug, note)]. Every file must
    exist (not_found) and appear once (bad_request). data: {entry}."""
    row = get_entry(entry)
    wanted, seen = [], set()
    for ref, note in _pairs(items, "slug"):
        if db.get_by_slug(ref) is None:
            raise NotFound(f"No item {ref!r}.")
        if ref in seen:
            raise InvalidInput(f"Item {ref!r} is listed twice")
        seen.add(ref)
        wanted.append((ref, note))
    batch_id = batch_id or changes.new_batch_id()
    with db.transaction(dry_run=dry_run):
        with db.ImageLog(OP_SET_ITEMS, actor, batch_id, [f"blog:{row['slug']}"]) as log:
            _replace(log, "blog_entry_items", "post_slug", row["id"], wanted)
        rows, fresh = _done(row["id"], batch_id)
    return Result(True, _changes_from_log(rows), [], batch_id, dry_run, {"entry": fresh})
