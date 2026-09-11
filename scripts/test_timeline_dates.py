"""Standalone test for the Timeline feature's effective-date resolution
logic (no pytest in this repo — see CLAUDE.md). Uses a disposable SQLite
DB, never the real imagerepo.db. Run: python scripts/test_timeline_dates.py
"""
import os
import sys
import tempfile
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import core.db as db
from core.timeline import resolve_item_date, resolve_project_span

db.DB_PATH = tempfile.mktemp(suffix=".db")
db.init_db()


def make_item(slug, timestamp, content_date=None, display_date_override=None):
    db.insert_upload(slug, None, None, "test", media_type="image")
    if content_date is not None:
        conn = db.get_conn()
        conn.execute("UPDATE capture_events SET content_date = ? WHERE slug = ?", (content_date, slug))
        conn.commit()
        conn.close()
    conn = db.get_conn()
    conn.execute("UPDATE capture_events SET timestamp = ? WHERE slug = ?", (timestamp, slug))
    conn.commit()
    conn.close()
    if display_date_override is not None:
        db.set_display_date_override(slug, display_date_override)
    return db.get_by_slug(slug)


def test_item_date_falls_back_to_timestamp():
    row = make_item("t1", timestamp=1000.0)
    assert resolve_item_date(row) == 1000.0, "no content_date/override -> timestamp"


def test_item_date_prefers_content_date_over_timestamp():
    row = make_item("t2", timestamp=1000.0, content_date=500.0)
    assert resolve_item_date(row) == 500.0, "content_date beats timestamp"


def test_item_date_prefers_override_over_everything():
    row = make_item("t3", timestamp=1000.0, content_date=500.0, display_date_override=200.0)
    assert resolve_item_date(row) == 200.0, "override beats content_date and timestamp"


def test_project_span_from_items():
    make_item("t4a", timestamp=100.0)
    make_item("t4b", timestamp=300.0)
    make_item("t4c", timestamp=200.0)
    project = db.create_project("Span Project")
    db.add_item_to_project(project["id"], "t4a")
    db.add_item_to_project(project["id"], "t4b")
    db.add_item_to_project(project["id"], "t4c")
    items = db.list_project_items(project["id"])
    start, end = resolve_project_span(project, items)
    assert start == 100.0, "start is earliest item date"
    assert end == 300.0, "end is latest item date"


def test_project_span_empty_falls_back_to_created_at():
    project = db.create_project("Empty Project")
    start, end = resolve_project_span(project, [])
    assert start == project["created_at"] == end, "empty project collapses to a point at created_at"


def test_project_span_respects_overrides():
    make_item("t6a", timestamp=100.0)
    project = db.create_project("Overridden Project")
    db.add_item_to_project(project["id"], "t6a")
    db.set_project_date_overrides(project["id"], start=50.0, end=999.0)
    project = db.get_project(project["id"])
    items = db.list_project_items(project["id"])
    start, end = resolve_project_span(project, items)
    assert (start, end) == (50.0, 999.0), "explicit overrides win over derived item dates"


if __name__ == "__main__":
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    failures = 0
    for t in tests:
        try:
            t()
            print(f"PASS {t.__name__}")
        except AssertionError as e:
            failures += 1
            print(f"FAIL {t.__name__}: {e}")
        except Exception as e:
            failures += 1
            print(f"ERROR {t.__name__}: {type(e).__name__}: {e}")
    try:
        os.remove(db.DB_PATH)
    except OSError:
        pass
    if failures:
        print(f"\n{failures}/{len(tests)} failed")
        sys.exit(1)
    print(f"\nAll {len(tests)} passed")
