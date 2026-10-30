"""The change log (V2 cards 3.13): row images (ImageLog), invert_image for undo, and the
change-log rows themselves.

Part of the core.db package (#547): import it as `from core import db` and use `db.<name>`;
core/db/__init__.py re-exports every name.
"""

import json
import time

# timeline is pure (no DB access — see its module docstring), so this
# direction of import can't cycle; it's here for list_project_items' sort.
# #557: policy owns the restricted-items browse filter (imports nothing from db at module load).
from .. import actor as actor_ctx, policy
from .conn import get_conn


# --- Change log (V2 cards 3.13) ---
# Core card operations write row images into the audit_log's extra columns so a
# later piece can undo them. All SQL stays here; core/changes.py is the API.

def insert_change_log(conn, op, actor, mutations, batch_id=None, affected_slugs=None, details=None):
    """Writes one change-log row on the caller's connection (the caller commits,
    so the log row lands in the same transaction as the write it describes).
    `mutations` is a list of {table, key, before, after} row images.
    `actor` None = the current actor context (core/actor.py, #560): every core write
    passes through here, so this is where a defaulted actor is resolved.
    `details` (#541 phase C): an optional JSON-able summary kept in form_body, for a record
    that has no row images (delete-all writes its per-table counts there)."""
    actor = actor_ctx.resolve(actor)
    # #604 step 3: the private-card write guard (a viewer member only looks). It raises before the
    # log row exists, so the caller's transaction rolls the write back.
    policy.check_write(mutations, actor)
    # #778: the personal MCP key this write was made with (only when `actor` is that key's user);
    # core/changes.via renders "<user>'s <token name>" from it at read time.
    via_token = actor_ctx.token_id_for(actor)
    cur = conn.execute(
        "INSERT INTO audit_log (method, path, form_body, affected_slugs, status_code, error_detail, timestamp, "
        "op, actor, batch_id, mutations, via_token_id) VALUES (?, ?, ?, ?, 200, NULL, ?, ?, ?, ?, ?, ?)",
        ("CORE", f"core:{op}", json.dumps(details or {}), json.dumps(affected_slugs or []), time.time(), op,
         actor, batch_id, json.dumps(mutations or []), via_token),
    )
    return cur.lastrowid


def list_change_log(card_slug=None, batch_id=None, limit=50):
    """Change-log rows (those with an `op`), newest first, mutations parsed."""
    conn = get_conn()
    try:
        sql = "SELECT * FROM audit_log WHERE op IS NOT NULL"
        args = []
        if batch_id:
            sql += " AND batch_id = ?"
            args.append(batch_id)
        if card_slug:
            sql += " AND affected_slugs LIKE ?"
            args.append(f'%"{card_slug}"%')
        rows = conn.execute(sql + " ORDER BY id DESC LIMIT ?", args + [limit]).fetchall()
        out = []
        for r in rows:
            d = dict(r)
            d["mutations"] = json.loads(d["mutations"]) if d.get("mutations") else []
            d["affected_slugs"] = json.loads(d["affected_slugs"]) if d.get("affected_slugs") else []
            out.append(d)
        return out
    finally:
        conn.close()


# --- Row images, undo, and card-operation write helpers (V2 cards piece 6, 3.13) ---
# Every composite card operation records `row images` ({table, key, before, after})
# so `undo` can invert them generically. The tables below are the ones an image may
# name; `rowid` rides along for tables without an INTEGER PRIMARY KEY so undoing a
# delete restores the row in its original position (membership order is rowid order).

IMAGE_TABLE_KEYS = {
    "projects": ("id",),
    "project_items": ("project_id", "post_slug"),
    "project_hobbies": ("project_id", "hobby_tag_id"),
    "family_members": ("family_id", "member_id"),
    "project_relations": ("slug_a", "slug_b", "type"),
    "blog_entry_projects": ("entry_id", "project_id"),
    "pending_decisions": ("id",),
    "curator_dismissals": ("nudge_key",),
    "blog_tags": ("id",),
    "capture_events": ("slug",),
    "post_tags": ("post_slug", "tag_id"),
    "provenance_options": ("scope", "key"),
    "vocab_terms": ("id",),  # #556: editable brand roles + wallpaper tags (core/vocab.py)
    "item_revisions": ("old_slug",),  # #477: one successor per item
    # #541 phase B (core/items.py): what an item delete touches, plus its trash entry.
    "capture_event_relations": ("slug_a", "slug_b"),
    "blog_entry_items": ("entry_id", "post_slug"),
    "trash": ("batch_id", "slug"),
    # #541 phase D (core/blog.py): the entry row itself.
    "blog_entries": ("id",),
    # #562: install identity (core/install_config.py) and per-hobby settings (core/hobbies.py).
    "install_config": ("key",),
    "hobby_settings": ("hobby_tag_id",),
    # #604 step 3 (core/sharing.py): a private card's / hobby's members.
    "share_members": ("target_kind", "target_id", "user_id"),
}
_IMAGE_ROWID_TABLES = ("project_items", "project_hobbies", "family_members", "project_relations",
                       "blog_entry_projects", "post_tags")


class UndoConflict(Exception):
    """A row no longer matches the state the change log recorded (someone changed it)."""

    def __init__(self, message, details=None):
        super().__init__(message)
        self.details = details or {}


def _image_columns(conn, table):
    """Non-BLOB columns of `table` (embeddings are never imaged)."""
    return [r["name"] for r in conn.execute(f"PRAGMA table_info({table})") if (r["type"] or "").upper() != "BLOB"]


def _key_where(table, key):
    cols = IMAGE_TABLE_KEYS[table]
    return " AND ".join(f"{c} = ?" for c in cols), [key[c] for c in cols]


def image_get(conn, table, key):
    """The current full row image for `key` (None when absent)."""
    where, args = _key_where(table, key)
    cols = _image_columns(conn, table)
    select = ", ".join(cols)
    if table in _IMAGE_ROWID_TABLES:
        select = "rowid AS rowid, " + select
    row = conn.execute(f"SELECT {select} FROM {table} WHERE {where}", args).fetchone()
    return dict(row) if row else None


def _image_insert_row(conn, table, key, values):
    row = {**key, **values}
    cols = list(row)
    conn.execute(f"INSERT INTO {table} ({', '.join(cols)}) VALUES ({', '.join('?' for _ in cols)})",
                 [row[c] for c in cols])


def _image_update_row(conn, table, key, fields):
    fields = {k: v for k, v in fields.items() if k != "rowid" and k not in IMAGE_TABLE_KEYS[table]}
    if not fields:
        return
    where, args = _key_where(table, key)
    conn.execute(f"UPDATE {table} SET {', '.join(f'{c} = ?' for c in fields)} WHERE {where}",
                 list(fields.values()) + args)


def _image_delete_row(conn, table, key):
    where, args = _key_where(table, key)
    conn.execute(f"DELETE FROM {table} WHERE {where}", args)


class ImageLog:
    """Writes rows through a connection and collects their row images; flush() records
    them as ONE change-log row. Use inside `transaction()` (or any connection) so the
    writes and their log row commit together."""

    def __init__(self, op, actor, batch_id, affected_slugs=None, conn=None):
        self.conn = conn or get_conn()
        self.op, self.actor, self.batch_id = op, actor, batch_id
        self.slugs = list(affected_slugs or [])
        self.muts = []
        self.details = None  # #555: an optional JSON-able summary for the log row's form_body

    def get(self, table, key):
        return image_get(self.conn, table, key)

    def insert(self, table, key, values):
        _image_insert_row(self.conn, table, key, values)
        self.muts.append({"table": table, "key": dict(key), "before": None, "after": image_get(self.conn, table, key)})

    def insert_auto(self, table, values):
        """INSERT into a table whose key is an `id` INTEGER PRIMARY KEY the DB assigns (#541
        phase C: a new blog_tags row). Imaged like insert(); returns the new id."""
        cols = list(values)
        cur = self.conn.execute(f"INSERT INTO {table} ({', '.join(cols)}) VALUES ({', '.join('?' for _ in cols)})",
                                [values[c] for c in cols])
        key = {"id": cur.lastrowid}
        self.muts.append({"table": table, "key": key, "before": None, "after": image_get(self.conn, table, key)})
        return cur.lastrowid

    def update(self, table, key, fields):
        before = image_get(self.conn, table, key)
        if before is None:
            return False
        if all(before.get(c) == v for c, v in fields.items()):
            return False
        _image_update_row(self.conn, table, key, fields)
        after = image_get(self.conn, table, key)
        cols = [c for c in fields if before.get(c) != after.get(c)]
        self.muts.append({"table": table, "key": dict(key),
                          "before": {c: before.get(c) for c in cols}, "after": {c: after.get(c) for c in cols}})
        return True

    def delete(self, table, key):
        before = image_get(self.conn, table, key)
        if before is None:
            return False
        _image_delete_row(self.conn, table, key)
        self.muts.append({"table": table, "key": dict(key), "before": before, "after": None})
        return True

    def flush(self):
        if not self.muts:
            return None
        row_id = insert_change_log(self.conn, self.op, self.actor, self.muts, batch_id=self.batch_id,
                                   affected_slugs=sorted(set(self.slugs)), details=self.details)
        self.muts = []
        return row_id

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        # Inside transaction() these three calls are no-ops on the shared connection; on a
        # private connection they commit (or roll back) and release it.
        try:
            if exc_type is None:
                self.flush()
                self.conn.commit()
            else:
                self.conn.rollback()
        finally:
            self.conn.close()
        return False


def invert_image(conn, mutation, force=False):
    """Applies the inverse of one row image (after -> before). Unless `force`, raises
    UndoConflict when the row no longer equals the recorded `after`. Returns the
    mutation that was actually applied ({table, key, before: <what was there>,
    after: <what is there now>}) so the undo can itself be logged and undone."""
    table, key = mutation["table"], mutation["key"]
    if table not in IMAGE_TABLE_KEYS:
        raise UndoConflict(f"Can't undo a change to {table!r}.", {"table": table})
    expected, target = mutation.get("after"), mutation.get("before")
    # #704: a row image logged before a column was dropped still names it (capture_events.source,
    # client, ...). Compare and restore only the columns the table has now; the dropped ones held
    # nothing anything reads, and writing them back would fail.
    live = set(_image_columns(conn, table)) | {"rowid"}
    if table == "capture_events":
        # #555: `tags` is a cache derived from the tag tree (the chip triggers keep it), so an image
        # logged before #555 that names it is neither compared nor written back: the post_tags rows
        # that op imaged are what undo restores, and the cache follows them.
        live.discard("tags")
    if expected is not None:
        expected = {k: v for k, v in expected.items() if k in live}
    if target is not None:
        target = {k: v for k, v in target.items() if k in live}
    current = image_get(conn, table, key)
    if not force:
        if expected is None and current is not None:
            raise UndoConflict(f"A {table} row {key} exists now but didn't when the change was made.",
                               {"table": table, "key": key, "found": "row exists", "expected": "no row"})
        if expected is not None:
            if current is None:
                raise UndoConflict(f"The {table} row {key} was deleted since.",
                                   {"table": table, "key": key, "found": "no row", "expected": "row"})
            # updated_at last: it moves on every edit, so a real field difference is the better explanation.
            for col, want in sorted(expected.items(), key=lambda kv: kv[0] == "updated_at"):
                if col == "rowid":
                    continue
                if current.get(col) != want:
                    raise UndoConflict(
                        f"{table} {key} changed since: {col} is {current.get(col)!r}, was {want!r} when logged.",
                        {"table": table, "key": key, "field": col, "found": current.get(col), "expected": want})
    if target is None and table == "blog_tags" and current is not None and not force:
        # #541 phase C: undoing a tag's creation removes the tag. Refuse while something made
        # since still uses it (another item's tag link, a card's linked tag, a hobby, a child tag).
        n = conn.execute(
            "SELECT (SELECT COUNT(*) FROM post_tags WHERE tag_id = ?) + (SELECT COUNT(*) FROM projects WHERE tag_id = ?)"
            " + (SELECT COUNT(*) FROM project_hobbies WHERE hobby_tag_id = ?)"
            " + (SELECT COUNT(*) FROM blog_tags WHERE parent_id = ?) AS n",
            (key["id"],) * 4).fetchone()["n"]
        if n:
            raise UndoConflict(f"The tag {current.get('name')!r} is in use by {n} other row(s) now, so its creation "
                               "can't be undone.", {"table": table, "key": key, "in_use": n})
    if target is None:
        if current is not None:
            if table == "capture_events":
                conn.execute("DELETE FROM post_tags WHERE post_slug = ?", (key["slug"],))
            _image_delete_row(conn, table, key)
        return {"table": table, "key": key, "before": current, "after": None}
    if current is None:
        _image_insert_row(conn, table, key, dict(target))
    else:
        _image_update_row(conn, table, key, target)
    return {"table": table, "key": key, "before": current, "after": image_get(conn, table, key)}


def get_change_rows(audit_id=None, batch_id=None):
    """Change-log rows (op set) by audit id or batch id, oldest first, mutations parsed."""
    conn = get_conn()
    try:
        if audit_id is not None:
            rows = conn.execute("SELECT * FROM audit_log WHERE op IS NOT NULL AND id = ?", (audit_id,)).fetchall()
        else:
            rows = conn.execute("SELECT * FROM audit_log WHERE op IS NOT NULL AND batch_id = ? ORDER BY id",
                                (batch_id,)).fetchall()
        out = []
        for r in rows:
            d = dict(r)
            d["mutations"] = json.loads(d["mutations"]) if d.get("mutations") else []
            d["affected_slugs"] = json.loads(d["affected_slugs"]) if d.get("affected_slugs") else []
            out.append(d)
        return out
    finally:
        conn.close()


def mark_change_rows_undone(row_ids, undone_by):
    """Stamps audit rows as reversed by `undone_by` (None clears the stamp)."""
    conn = get_conn()
    try:
        conn.executemany("UPDATE audit_log SET undone_by = ? WHERE id = ?", [(undone_by, i) for i in row_ids])
        conn.commit()
    finally:
        conn.close()


def write_images(op, actor, batch_id, affected_slugs, fn):
    """Runs `fn(log)` with an ImageLog (log.insert / update / delete / get); the images are
    recorded as one change-log row. For merge's many small writes."""
    with ImageLog(op, actor, batch_id, affected_slugs) as log:
        return fn(log)
