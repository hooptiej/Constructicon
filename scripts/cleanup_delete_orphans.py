"""Issue #497: find (and optionally remove) the ghosts older project deletes left behind.

Before #497, deleting a project left behind its auto-created write-up document, typed
links pointing at its slug, and (since V2) family / hobby / blog-entry rows. This script
counts those orphans and can remove them.

DRY-RUN BY DEFAULT. With no flags it opens the database READ-ONLY (SQLite `mode=ro`) and
only prints counts and the queries it used. Nothing is changed unless you pass --execute,
and you should take a backup first (constructicon_backup, or copy the .db file).

What counts as an orphan
------------------------
  writeups_blank     blank "<title> - Write-up" documents (media_type document, empty body) that no project references (projects.writeup_slug), whose
                     title prefix matches no existing project, and that sit in no project_items / blog_entry_items. --execute deletes them
                     (plus their post_tags rows).
  writeups_with_text same shape but WITH text in the body. Reported only; never deleted.
  links              project_relations rows where either end is not an existing project slug.
  project_items      rows whose project_id no longer exists.
  project_hobbies    rows whose project_id no longer exists.
  family_members     rows whose family_id or member_id no longer exists.
  blog_entry_projects rows whose project_id no longer exists.
  decisions          OPEN pending_decisions on `card:<slug>` where the card no longer exists
                     (--execute resolves them with a note; rows are kept, not deleted).
  parent_ids         projects.parent_id / cover_project_id pointing at a missing project
                     (--execute sets them to NULL).

Usage
-----
    python scripts/cleanup_delete_orphans.py               # dry run (read-only)
    python scripts/cleanup_delete_orphans.py --execute     # apply (take a backup first)
    python scripts/cleanup_delete_orphans.py --db /path/to/imagerepo.db

The DB path is --db, else $CONSTRUCTICON_DB_PATH, else core.db.DB_PATH.
"""

import argparse
import json
import os
import sqlite3
import sys
import time

SLUGS = "SELECT slug FROM projects"
IDS = "SELECT id FROM projects"

QUERIES = {
    "writeups_blank": (
        "SELECT slug FROM capture_events WHERE media_type = 'document' "
        "AND content_description LIKE '% — Write-up' "
        "AND substr(content_description, 1, length(content_description) - 11) NOT IN (SELECT title FROM projects) "
        "AND TRIM(COALESCE(json_extract(type_metadata, '$.body'), '')) = '' "
        "AND slug NOT IN (SELECT writeup_slug FROM projects WHERE writeup_slug IS NOT NULL) "
        "AND slug NOT IN (SELECT post_slug FROM project_items) "
        "AND slug NOT IN (SELECT post_slug FROM blog_entry_items)"
    ),
    "writeups_with_text": (
        "SELECT slug FROM capture_events WHERE media_type = 'document' "
        "AND content_description LIKE '% — Write-up' "
        "AND substr(content_description, 1, length(content_description) - 11) NOT IN (SELECT title FROM projects) "
        "AND TRIM(COALESCE(json_extract(type_metadata, '$.body'), '')) != '' "
        "AND slug NOT IN (SELECT writeup_slug FROM projects WHERE writeup_slug IS NOT NULL) "
        "AND slug NOT IN (SELECT post_slug FROM project_items) "
        "AND slug NOT IN (SELECT post_slug FROM blog_entry_items)"
    ),
    "links": (
        f"SELECT slug_a, slug_b, type FROM project_relations "
        f"WHERE slug_a NOT IN ({SLUGS}) OR slug_b NOT IN ({SLUGS})"
    ),
    "project_items": f"SELECT project_id, post_slug FROM project_items WHERE project_id NOT IN ({IDS})",
    "project_hobbies": f"SELECT project_id, hobby_tag_id FROM project_hobbies WHERE project_id NOT IN ({IDS})",
    "family_members": (
        f"SELECT family_id, member_id FROM family_members "
        f"WHERE family_id NOT IN ({IDS}) OR member_id NOT IN ({IDS})"
    ),
    "blog_entry_projects": f"SELECT entry_id, project_id FROM blog_entry_projects WHERE project_id NOT IN ({IDS})",
    "decisions": (
        "SELECT id FROM pending_decisions WHERE resolved_at IS NULL AND post_slug LIKE 'card:%' "
        f"AND substr(post_slug, 6) NOT IN ({SLUGS})"
    ),
    "parent_ids": (
        f"SELECT id FROM projects WHERE (parent_id IS NOT NULL AND parent_id NOT IN ({IDS})) "
        f"OR (cover_project_id IS NOT NULL AND cover_project_id NOT IN ({IDS}))"
    ),
}


def _table_exists(conn, name):
    return conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name = ?", (name,)).fetchone() is not None


def find_orphans(conn):
    """{name: [rows]} for every orphan class. Pure SELECTs."""
    out = {}
    for name, sql in QUERIES.items():
        needed = {"links": "project_relations", "project_hobbies": "project_hobbies", "family_members": "family_members",
                  "blog_entry_projects": "blog_entry_projects"}.get(name)
        if needed and not _table_exists(conn, needed):
            out[name] = []
            continue
        if name == "links" and "type" not in {r[1] for r in conn.execute("PRAGMA table_info(project_relations)")}:
            # Pre-V2 schema (no typed links yet): pairs only.
            sql = sql.replace("slug_a, slug_b, type", "slug_a, slug_b, 'related'")
        out[name] = [tuple(r) for r in conn.execute(sql, {})]
    return out


def apply_cleanup(conn, orphans):
    """Deletes/repairs what find_orphans reported (writeups_with_text is never touched).
    One transaction. Returns {name: count}."""
    done = {}
    conn.execute("BEGIN IMMEDIATE")
    try:
        for (slug,) in orphans["writeups_blank"]:
            conn.execute("DELETE FROM post_tags WHERE post_slug = ?", (slug,))
            conn.execute("DELETE FROM capture_events WHERE slug = ?", (slug,))
        done["writeups_blank"] = len(orphans["writeups_blank"])
        for a, b, t in orphans["links"]:
            conn.execute("DELETE FROM project_relations WHERE slug_a = ? AND slug_b = ? AND type = ?", (a, b, t))
        done["links"] = len(orphans["links"])
        for pid, slug in orphans["project_items"]:
            conn.execute("DELETE FROM project_items WHERE project_id = ? AND post_slug = ?", (pid, slug))
        done["project_items"] = len(orphans["project_items"])
        for pid, tag in orphans["project_hobbies"]:
            conn.execute("DELETE FROM project_hobbies WHERE project_id = ? AND hobby_tag_id = ?", (pid, tag))
        done["project_hobbies"] = len(orphans["project_hobbies"])
        for fam, mem in orphans["family_members"]:
            conn.execute("DELETE FROM family_members WHERE family_id = ? AND member_id = ?", (fam, mem))
        done["family_members"] = len(orphans["family_members"])
        for eid, pid in orphans["blog_entry_projects"]:
            conn.execute("DELETE FROM blog_entry_projects WHERE entry_id = ? AND project_id = ?", (eid, pid))
        done["blog_entry_projects"] = len(orphans["blog_entry_projects"])
        for (did,) in orphans["decisions"]:
            row = conn.execute("SELECT payload FROM pending_decisions WHERE id = ?", (did,)).fetchone()
            payload = json.loads(row[0]) if row and row[0] else {}
            payload["resolution"] = {"stale": "card no longer exists (#497 cleanup)"}
            conn.execute("UPDATE pending_decisions SET resolved_at = ?, payload = ? WHERE id = ?",
                         (time.time(), json.dumps(payload), did))
        done["decisions"] = len(orphans["decisions"])
        for (pid,) in orphans["parent_ids"]:
            conn.execute("UPDATE projects SET parent_id = NULL WHERE id = ? AND parent_id NOT IN (SELECT id FROM projects)", (pid,))
            conn.execute("UPDATE projects SET cover_project_id = NULL WHERE id = ? AND cover_project_id NOT IN (SELECT id FROM projects)", (pid,))
        done["parent_ids"] = len(orphans["parent_ids"])
        conn.commit()
    except BaseException:
        conn.rollback()
        raise
    return done


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--execute", action="store_true", help="actually apply the cleanup (default is a read-only dry run)")
    ap.add_argument("--db", help="database path (default $CONSTRUCTICON_DB_PATH, else core.db.DB_PATH)")
    ap.add_argument("--show-queries", action="store_true", help="print the SQL used for each count")
    args = ap.parse_args(argv)

    path = args.db or os.environ.get("CONSTRUCTICON_DB_PATH")
    if not path:
        sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(globals().get("__file__", "."))), ".."))
        try:
            from core import db as _db
            path = str(_db.DB_PATH)
        except Exception:  # standalone use (piped into another container)
            raise SystemExit("Give --db (core.db not importable).")
    mode = "rw" if args.execute else "ro"
    conn = sqlite3.connect(f"file:{path}?mode={mode}", uri=True, timeout=30)
    try:
        orphans = find_orphans(conn)
        print(f"Database: {path}  ({'EXECUTE' if args.execute else 'DRY RUN, read-only'})")
        for name, rows in orphans.items():
            print(f"  {name:22s} {len(rows)}")
            for r in rows[:5]:
                print(f"      e.g. {r}")
        if args.show_queries:
            for name, sql in QUERIES.items():
                print(f"\n-- {name}\n{sql}")
        if args.execute:
            done = apply_cleanup(conn, orphans)
            print("Applied:", json.dumps(done))
        else:
            print("\nDry run: nothing changed. Re-run with --execute to apply (back up first).")
    finally:
        conn.close()


if __name__ == "__main__":
    main()
