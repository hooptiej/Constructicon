"""Delete-all (#541 phase C): the ONE full reset, called by POST /api/delete-all and MCP
constructicon_delete_all. Before this, each front end had its own line-for-line copy (raw SQL),
and both forgot blog entries, card questions, curator snoozes, the trash and more.

PERMANENT BY DESIGN: it is a reset, not a delete. Nothing goes to the trash and nothing can be
undone; it writes ONE change-log row (op `delete_all`, the actor from context) whose details hold
the per-table row counts and the number of files removed. Take a backup first (POST /api/backup,
MCP constructicon_backup).

Requires the typed phrase CONFIRM_PHRASE ("DELETE EVERYTHING", #558) on every path.

What it clears (CLEARED_TABLES): every item and everything that hangs off items, cards, tags and
hobbies (hobbies are blog_tags rows, so they go with the tags: both old copies did this), blog
entries, card/item questions and their snoozes, the caption queue and the trash. On disk: each
item's file and thumbnail, and the whole <storage>/.trash directory (held redacted files too).

What it keeps (KEPT_TABLES): settings, the client list, the editable provenance lists, the
migration record and the audit/change log (the reset's own record lives there). A table in
neither list fails scripts/test_membership_tags.py, so a new table has to be classified.
"""

import shutil

from . import changes, db, items, storage
from .errors import InvalidInput

OP_DELETE_ALL = "delete_all"
CONFIRM_PHRASE = "DELETE EVERYTHING"

# Children first (nothing here has ON DELETE rules, but the order reads as "what points at what").
CLEARED_TABLES = (
    "post_tags", "project_items", "capture_event_relations", "item_revisions", "caption_queue",
    "blog_entry_items", "blog_entry_projects", "blog_entries",
    "project_relations", "family_members", "project_hobbies",
    "curator_dismissals", "pending_decisions", "trash",
    "capture_events", "projects", "blog_tags",
)
KEPT_TABLES = ("app_settings", "audit_log", "clients", "client_domains", "provenance_options", "schema_migrations")


def delete_everything(confirm="", *, actor=None):
    """Wipes the archive (see the module docstring). Returns {deleted, counts, files_removed,
    trash_removed, batch_id}. A wrong phrase raises confirm_required and changes nothing."""
    if (confirm or "").strip() != CONFIRM_PHRASE:
        raise InvalidInput(f"Type {CONFIRM_PHRASE!r} in the confirm field to delete everything",
                           code="confirm_required")
    present = set(db.list_tables())
    tables = [t for t in CLEARED_TABLES if t in present]
    batch_id = changes.new_batch_id()
    with db.transaction():
        files = db.list_all_item_files()
        counts = db._clear_tables(tables)
        changes.record(OP_DELETE_ALL, actor, [], batch_id=batch_id, conn=db.get_conn(),
                       details={"counts": counts, "files": sum(1 for f in files if f.get("stored_filename"))})
    # Files only after the rows are gone for good (a failed transaction leaves both in place).
    removed = 0
    for f in files:
        if f.get("stored_filename"):
            try:
                storage.delete_files(f["slug"], f["stored_filename"])
                removed += 1
            except OSError as e:
                print(f"delete-all: could not remove {f['stored_filename']}: {e!r}", flush=True)
        else:
            storage.thumb_path_for(f["slug"]).unlink(missing_ok=True)
    trash_dir = items.trash_dir()
    trash_removed = sum(1 for p in trash_dir.rglob("*") if p.is_file()) if trash_dir.exists() else 0
    shutil.rmtree(trash_dir, ignore_errors=True)
    return {"deleted": counts.get("capture_events", 0), "counts": counts, "files_removed": removed,
            "trash_removed": trash_removed, "batch_id": batch_id}
