#!/usr/bin/env python3
"""Self-contained check for the item service (#541 phase B, core/items.py).

Builds a throwaway SQLite DB with the real `db.init_db()` (CONSTRUCTICON_DB_PATH points at a temp
file before core is imported) and a throwaway storage dir (storage.STORAGE_DIR is repointed), then
exercises: a multi-field edit as one undoable batch, validation-before-write, dry runs, redact +
undo (file back from the trash), unredact, retype + undo, delete of an item that is in a project,
tagged, related, in a blog entry, asked about and in the MIDDLE of a revision chain (A -> B -> C
re-link and its restore), bulk delete + undo, undo-of-undo, purge + trash_expired, and the
empty-trash phrase. No server needed:

    python scripts/test_items_service.py

Exits 1 if any check fails.
"""

import hashlib
import io
import os
import sqlite3
import sys
import tempfile
import time
from pathlib import Path

TMP = tempfile.mkdtemp(prefix="items-service-")
os.environ["CONSTRUCTICON_DB_PATH"] = os.path.join(TMP, "test.db")
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))

try:
    import cairosvg  # noqa: F401
except Exception:  # no libcairo here (e.g. Windows): object types import it
    import types
    sys.modules["cairosvg"] = types.ModuleType("cairosvg")

from PIL import Image  # noqa: E402

from core import actor, cards, changes, db, ingest, items, revisions, storage  # noqa: E402
from core.errors import AppError  # noqa: E402

storage.STORAGE_DIR = Path(TMP) / "storage"
storage.STORAGE_DIR.mkdir()
ingest.run_in_thread = lambda fn, *a: None  # no background OCR/thumbnail threads in a unit check
NOOP = lambda fn, *a: None  # noqa: E731

FAILS = []


def check(name, cond, extra=""):
    print(("PASS " if cond else "FAIL ") + name + (f"  [{extra}]" if (extra and not cond) else ""))
    if not cond:
        FAILS.append(name)


def code_of(fn, *a, **kw):
    try:
        fn(*a, **kw)
    except AppError as e:
        return e.code
    return None


def png_bytes(color):
    buf = io.BytesIO()
    Image.new("RGB", (8, 8), color).save(buf, "PNG")
    return buf.getvalue()


def mk(slug, color=(200, 10, 10)):
    """An image item with a real file and a thumbnail on disk."""
    sf = f"{slug}.png"
    (storage.STORAGE_DIR / sf).write_bytes(png_bytes(color))
    storage.thumb_path_for(slug).write_bytes(b"thumb-" + slug.encode())
    db.insert_upload(slug, f"{slug}.png", sf, "tester", media_type="image")
    return slug


def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def q(sql, *args):
    c = sqlite3.connect(os.environ["CONSTRUCTICON_DB_PATH"])
    c.row_factory = sqlite3.Row
    try:
        return [dict(r) for r in c.execute(sql, args)]
    finally:
        c.close()


def audit_count():
    return q("SELECT COUNT(*) AS n FROM audit_log WHERE op IS NOT NULL")[0]["n"]


def storage_files():
    return sorted(p.name for p in storage.STORAGE_DIR.iterdir() if p.is_file())


def trash_files():
    root = items.trash_dir()
    return sorted(p.relative_to(root).as_posix() for p in root.rglob("*") if p.is_file()) if root.exists() else []


db.init_db()
db.init_db()  # idempotent
check("trash table exists after init_db", q("SELECT name FROM sqlite_master WHERE name = 'trash'") != [])
check("trash is an imaged table", "trash" in db.IMAGE_TABLE_KEYS and "capture_event_relations" in db.IMAGE_TABLE_KEYS
      and "blog_entry_items" in db.IMAGE_TABLE_KEYS)

ctx = actor.acting_as(actor.ACTOR_UI)
ctx.__enter__()

# --- multi-field edit: one batch, one undo ------------------------------------------------
E = mk("edit1")
db.update_content_metadata(E, type_metadata={"auto_caption": "a red square"})  # pipeline data to keep
before = db.get_by_slug(E)
n0 = audit_count()
res = items.update(E, display_name="Red square", description="the owner's note", provenance="found",
                   content_date=1500000000, display_date_override=1600000000,
                   type_metadata={"medium": "  oil   paint ", "date_made": "2009-06"}, highlight=True)
row = db.get_by_slug(E)
check("multi-field edit writes every field",
      row["display_name"] == "Red square" and row["description"] == "the owner's note" and row["provenance"] == "found"
      and row["content_date"] == 1500000000 and row["display_date_override"] == 1600000000 and row["highlight"] == 1)
check("type_metadata merged (pipeline keys kept) and physical-piece keys cleaned",
      row["type_metadata"] == {"auto_caption": "a red square", "medium": "oil paint", "date_made": "2009-06"},
      row["type_metadata"])
logged = db.get_change_rows(batch_id=res.batch_id)
check("...as ONE change-log row (one Save = one batch)", audit_count() == n0 + 1 and len(logged) == 1
      and logged[0]["op"] == "item_update" and logged[0]["actor"] == "owner-ui", [r["op"] for r in logged])
cards.undo(res.batch_id)
row = db.get_by_slug(E)
check("undo restores every field",
      all(row[k] == before[k] for k in ("display_name", "description", "provenance", "content_date",
                                        "display_date_override", "highlight", "type_metadata")))

n0 = audit_count()
check("bad provenance key refused", code_of(items.update, E, display_name="X", provenance="bogus") == "bad_provenance")
check("bad physical-piece date refused",
      code_of(items.update, E, display_name="X", type_metadata={"date_made": "June"}) == "bad_physical_piece")
check("unknown field refused", code_of(items.update, E, title="X") == "unknown_field")
check("bad date refused", code_of(items.update, E, content_date="soon") == "bad_date")
check("missing item -> not_found", code_of(items.update, "nope", display_name="X") == "not_found")
check("...and none of them wrote anything", db.get_by_slug(E)["display_name"] == before["display_name"]
      and audit_count() == n0)
res = items.update(E, display_name="Dry", dry_run=True)
check("dry run reports the change but writes nothing",
      res.dry_run and res.changes and db.get_by_slug(E)["display_name"] == before["display_name"] and audit_count() == n0)
res = items.update(E, display_name=before["display_name"] or "")
check("a no-op edit logs nothing", audit_count() == n0 and res.changes == [])
items.update(E, is_brand_asset=True, brand_role="logo")
check("brand asset + role", db.get_by_slug(E)["is_brand_asset"] == 1 and db.get_by_slug(E)["brand_role"] == "logo")
items.update(E, is_brand_asset=False, brand_role="logo")
check("clearing brand asset clears the role", db.get_by_slug(E)["brand_role"] is None)
check("parse_date: naive ISO is Mountain Time", items.parse_date("2017-07-31") == 1501480800.0, items.parse_date("2017-07-31"))

# --- redact / unredact ---------------------------------------------------------------------
R = mk("redact1", (10, 200, 10))
orig_hash = sha(storage.STORAGE_DIR / "redact1.png")
check("unredact of a visible item -> not_redacted", code_of(items.unredact, R) == "not_redacted")
res = items.redact(R)
row = db.get_by_slug(R)
check("redact hides the row and clears stored_filename", row["redacted"] == 1 and row["stored_filename"] is None)
check("redact moves file + thumb into the trash",
      "redact1.png" not in storage_files() and trash_files() == sorted([f"{res.batch_id}/redact1.png",
                                                                        f"{res.batch_id}/redact1_thumb.jpg"]),
      trash_files())
check("no_file on a second redact", code_of(items.redact, R) == "no_file")
cards.undo(res.batch_id)
row = db.get_by_slug(R)
check("undoing the redact brings the row AND the file back",
      row["redacted"] == 0 and row["stored_filename"] == "redact1.png"
      and sha(storage.STORAGE_DIR / "redact1.png") == orig_hash and storage.thumb_path_for(R).exists())
check("...and the trash is empty again", trash_files() == [] and q("SELECT * FROM trash WHERE slug = ?", R) == [])
res = items.redact(R)
items.unredact(R)
check("unredact is visibility only (file stays in the trash)",
      db.get_by_slug(R)["redacted"] == 0 and "redact1.png" not in storage_files())

# --- retype -------------------------------------------------------------------------------
T = mk("retype1")
check("unknown type refused", code_of(items.retype, T, "no-such-type", NOOP) == "unknown_media_type")
res = items.retype(T, "document", NOOP)
check("retype changes media_type", db.get_by_slug(T)["media_type"] == "document")
cards.undo(res.batch_id)
check("undo restores the old media_type", db.get_by_slug(T)["media_type"] == "image")

# --- delete: everything that points at the item, chain re-link ----------------------------
A, B, C, X = mk("revA"), mk("revB", (1, 2, 3)), mk("revC"), mk("relX")
revisions.mark_superseded(A, B)
revisions.mark_superseded(B, C)
check("chain A -> B -> C built", db.revision_pairs() == {A: B, B: C})
proj = db.create_project("Delete Test Card", with_writeup=False)
db.add_item_to_project(proj["id"], A)
db.add_item_to_project(proj["id"], B)
db.add_item_to_project(proj["id"], X)
tag = db.get_or_create_tag("trash-test-tag")
db.attach_tags(B, [tag["id"]])
db.add_relation(B, X)
entry = db.create_blog_entry("A post")
db.set_entry_items(entry["id"], [(A, ""), (B, "middle one")])
did = db.add_pending_decision("project_match", B, {"options": []})
db.set_curator_state(f"decision:{did}", "defer")
db.set_embedding(B, b"\x01\x02\x03\x04")
b_hash = sha(storage.STORAGE_DIR / "revB.png")
snap = {
    "items": q("SELECT rowid, * FROM project_items WHERE post_slug = ?", B),
    "tags": q("SELECT * FROM post_tags WHERE post_slug = ?", B),
    "rel": q("SELECT * FROM capture_event_relations WHERE slug_a = ? OR slug_b = ? ORDER BY slug_a", B, B),
    "blog": q("SELECT * FROM blog_entry_items WHERE post_slug = ?", B),
    "dec": q("SELECT * FROM pending_decisions WHERE id = ?", did),
    "cur": q("SELECT * FROM curator_dismissals WHERE nudge_key = ?", f"decision:{did}"),
    "row": q("SELECT * FROM capture_events WHERE slug = ?", B),
}
check("fixture: B is in a project, tagged, related both ways, in a post, asked about, deferred",
      len(snap["items"]) == 1 and len(snap["tags"]) == 1 and len(snap["rel"]) == 2 and len(snap["blog"]) == 1
      and len(snap["dec"]) == 1 and len(snap["cur"]) == 1)

n0 = audit_count()
check("delete of an unknown slug -> not_found, nothing written",
      code_of(items.delete, [B, "ghost"]) == "not_found" and db.get_by_slug(B) is not None and audit_count() == n0)
res = items.delete([B], dry_run=True)
check("dry-run delete writes and moves nothing",
      db.get_by_slug(B) is not None and "revB.png" in storage_files() and audit_count() == n0 and res.data["deleted"] == 1)
res = items.delete([B])
DEL_B = res.batch_id
check("delete removes the row", db.get_by_slug(B) is None and res.data["deleted"] == 1 and res.data["trashed"] == 1)
check("...re-links the chain A -> C", db.revision_pairs() == {A: C}, db.revision_pairs())
check("...and drops membership, tags, relation, blog link, decision, curator state",
      not q("SELECT 1 FROM project_items WHERE post_slug = ?", B) and not q("SELECT 1 FROM post_tags WHERE post_slug = ?", B)
      and not q("SELECT 1 FROM capture_event_relations WHERE slug_a = ? OR slug_b = ?", B, B)
      and not q("SELECT 1 FROM blog_entry_items WHERE post_slug = ?", B)
      and not q("SELECT 1 FROM pending_decisions WHERE id = ?", did)
      and not q("SELECT 1 FROM curator_dismissals WHERE nudge_key = ?", f"decision:{did}"))
check("file + thumb are in the trash, gone from storage",
      "revB.png" not in storage_files() and f"{DEL_B}/revB.png" in trash_files() and f"{DEL_B}/revB_thumb.jpg" in trash_files())
t = db.get_trash_row(DEL_B, B)
check("trash row: reason, 7-day expiry, embedding kept",
      t and t["reason"] == "delete" and abs(t["expires_at"] - t["created_at"] - 7 * 86400) < 1 and t["embedding"] == b"\x01\x02\x03\x04")
imaged = {m["table"] for r in db.get_change_rows(batch_id=DEL_B) for m in r["mutations"]}
check("every touched table is imaged", imaged == {"capture_events", "capture_event_relations", "item_revisions",
                                                  "project_items", "post_tags", "blog_entry_items", "pending_decisions",
                                                  "curator_dismissals", "trash"}, imaged)

undone = cards.undo(DEL_B)
check("undo brings the row back", db.get_by_slug(B) is not None and undone.ok)
check("...the chain is A -> B -> C again", db.revision_pairs() == {A: B, B: C}, db.revision_pairs())
check("...membership (same rowid / sort order), tag, relation, blog link, decision, curator state",
      q("SELECT rowid, * FROM project_items WHERE post_slug = ?", B) == snap["items"]
      and q("SELECT * FROM post_tags WHERE post_slug = ?", B) == snap["tags"]
      and q("SELECT * FROM capture_event_relations WHERE slug_a = ? OR slug_b = ? ORDER BY slug_a", B, B) == snap["rel"]
      and q("SELECT * FROM blog_entry_items WHERE post_slug = ?", B) == snap["blog"]
      and q("SELECT * FROM pending_decisions WHERE id = ?", did) == snap["dec"]
      and q("SELECT * FROM curator_dismissals WHERE nudge_key = ?", f"decision:{did}") == snap["cur"])
check("...the whole capture_events row incl. id and embedding", q("SELECT * FROM capture_events WHERE slug = ?", B) == snap["row"])
check("...and the original bytes + thumbnail", sha(storage.STORAGE_DIR / "revB.png") == b_hash
      and storage.thumb_path_for(B).exists() and not (items.trash_dir(DEL_B)).exists())
check("undo summary is slim (no OCR text echoed)",
      all("extracted_text" not in (c.get("before") or {}) for c in undone.changes if c["table"] == "capture_events"))

# undo of the undo = delete again; undo that = back again (files follow both ways)
redo = cards.undo(undone.batch_id)
check("undoing the undo deletes again and re-trashes the file",
      db.get_by_slug(B) is None and "revB.png" not in storage_files() and f"{DEL_B}/revB.png" in trash_files()
      and db.revision_pairs() == {A: C})
check("...keeping the embedding with the trash entry", db.get_trash_row(DEL_B, B)["embedding"] == b"\x01\x02\x03\x04")
cards.undo(redo.batch_id)
check("...and undoing that restores it all again", db.get_by_slug(B) is not None and db.revision_pairs() == {A: B, B: C}
      and sha(storage.STORAGE_DIR / "revB.png") == b_hash and db.get_embedding(B) == b"\x01\x02\x03\x04")

# --- bulk delete --------------------------------------------------------------------------
bulk = [mk("bulk1"), mk("bulk2"), mk("bulk3")]
before_files = storage_files()
res = items.delete(bulk + ["not-there"], missing_ok=True)
check("bulk delete: 3 deleted, unknown skipped, one batch",
      res.data["deleted"] == 3 and all(db.get_by_slug(s) is None for s in bulk)
      and len({r["batch_id"] for r in db.get_change_rows(batch_id=res.batch_id)}) == 1)
cards.undo(res.batch_id)
check("bulk undo restores all 3 rows and files", all(db.get_by_slug(s) for s in bulk) and storage_files() == before_files)

# --- purge + trash_expired ----------------------------------------------------------------
P = mk("purge1")
res = items.delete([P])
c = sqlite3.connect(os.environ["CONSTRUCTICON_DB_PATH"])
c.execute("UPDATE trash SET expires_at = ? WHERE slug = ?", (time.time() - 1, P))
c.commit()
c.close()
pr = items.purge_expired()
check("purge removes only the expired entry", pr["purged"] == 1 and pr["slugs"] == [P]
      and not items.trash_dir(res.batch_id).exists(), pr)
check("...stamping purged_at", db.get_trash_row(res.batch_id, P)["purged_at"] is not None)
n0 = audit_count()
check("undo after purge -> trash_expired", code_of(cards.undo, res.batch_id) == "trash_expired")
check("...and changes nothing", db.get_by_slug(P) is None and audit_count() == n0
      and db.get_change_rows(batch_id=res.batch_id)[0]["undone_by"] is None)
check("...even with force", code_of(cards.undo, res.batch_id, force=True) == "trash_expired")

summary = items.trash_summary()
check("trash summary counts the redacted file still in the trash", summary["count"] == 1 and summary["bytes"] > 0, summary)
check("empty trash without the phrase -> confirm_required", code_of(items.empty_trash, "yes") == "confirm_required"
      and items.trash_summary()["count"] == 1)
er = items.empty_trash("EMPTY TRASH")
check("empty trash with the phrase purges everything", er["purged"] == 1 and items.trash_summary()["count"] == 0
      and trash_files() == [])

ctx.__exit__(None, None, None)
print()
print("FAILED: %d" % len(FAILS) if FAILS else "all checks passed")
sys.exit(1 if FAILS else 0)
