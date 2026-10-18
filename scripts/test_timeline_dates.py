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
from core import cards
from core.timeline import date_sort_key, date_source, has_real_date, resolve_item_date, resolve_project_span

db.DB_PATH = tempfile.mktemp(suffix=".db")
db.init_db()


def make_item(slug, timestamp, content_date=None, source_modified_at=None, display_date_override=None):
    db.insert_upload(slug, None, None, "test", media_type="image")
    if content_date is not None:
        conn = db.get_conn()
        conn.execute("UPDATE capture_events SET content_date = ? WHERE slug = ?", (content_date, slug))
        conn.commit()
        conn.close()
    if source_modified_at is not None:
        conn = db.get_conn()
        conn.execute("UPDATE capture_events SET source_modified_at = ? WHERE slug = ?", (source_modified_at, slug))
        conn.commit()
        conn.close()
    conn = db.get_conn()
    conn.execute("UPDATE capture_events SET timestamp = ? WHERE slug = ?", (timestamp, slug))
    conn.commit()
    conn.close()
    if display_date_override is not None:
        db._set_display_date_override(slug, display_date_override)
    return db.get_by_slug(slug)


# #725: realistic epochs. The install's first upload is MIN(timestamp) over the items; every fixture
# item is uploaded at or after T0, so a file mtime before T0 is real and one after it is not.
T0 = 1788300000.0          # 2026-09-01-ish: the "install's first upload"
OLD = 1500000000.0         # 2017: a real date
LATER = T0 + 86400 * 20    # a download date, after the install started


def test_cutoff_is_the_first_upload():
    make_item("t0", timestamp=T0)
    assert db.first_upload_epoch() == T0, "first upload = MIN(timestamp)"


def test_item_with_only_an_upload_time_is_undated():
    row = make_item("t1", timestamp=T0 + 1000)
    assert resolve_item_date(row) is None, "#725: the upload time is never the item's date"
    assert not has_real_date(row)
    assert date_source(row) is None


def test_item_date_prefers_content_date_over_timestamp():
    row = make_item("t2", timestamp=T0 + 1000, content_date=OLD)
    assert resolve_item_date(row) == OLD, "content_date is a real date"
    assert date_source(row) == "content"


def test_item_date_prefers_override_over_everything():
    row = make_item("t3", timestamp=T0 + 1000, content_date=OLD, display_date_override=OLD - 500)
    assert resolve_item_date(row) == OLD - 500, "override beats content_date"
    assert date_source(row) == "hand"


def test_pre_install_mtime_is_real():
    # Real-world case that surfaced this (2026-09-11): an old phone video's bulk import set
    # timestamp to the import moment, while source_modified_at (the file's own mtime) agreed with
    # the filename's own embedded date, years earlier.
    row = make_item("t8", timestamp=T0 + 5000, source_modified_at=OLD)
    assert resolve_item_date(row) == OLD, "a file mtime from before the first upload counts"
    assert date_source(row) == "file"


def test_post_install_mtime_is_not_real():
    row = make_item("t8b", timestamp=T0 + 86400 * 21, source_modified_at=LATER)
    assert resolve_item_date(row) is None, "#725: an mtime after the first upload is a download date"
    assert resolve_item_date(row, first_upload=None) == LATER, "with no cutoff (no items yet) it counts"


def test_item_date_prefers_content_date_over_source_modified_at():
    row = make_item("t9", timestamp=T0 + 5000, source_modified_at=OLD, content_date=OLD - 3600)
    assert resolve_item_date(row) == OLD - 3600, "content_date still wins over source_modified_at"


def test_date_sort_key_puts_undated_last():
    xs = [None, 5.0, None, 1.0, 3.0]
    assert sorted(xs, key=date_sort_key) == [1.0, 3.0, 5.0, None, None]
    assert sorted(xs, key=lambda d: date_sort_key(d, newest_first=True)) == [5.0, 3.0, 1.0, None, None]


def test_project_span_from_real_items_only():
    make_item("t4a", timestamp=T0 + 10, content_date=OLD)
    make_item("t4b", timestamp=T0 + 20, content_date=OLD + 300)
    make_item("t4c", timestamp=T0 + 30, source_modified_at=OLD + 200)  # pre-install mtime: real
    make_item("t4d", timestamp=T0 + 40)                                # upload time only
    make_item("t4e", timestamp=T0 + 86400 * 30, source_modified_at=LATER)  # post-install mtime
    project = db._create_project("Span Project", with_writeup=False)
    for slug in ("t4a", "t4b", "t4c", "t4d", "t4e"):
        db._add_item_to_project(project["id"], slug)
    items = db.list_project_items(project["id"])
    start, end = resolve_project_span(project, items)
    assert (start, end) == (OLD, OLD + 300), f"only real dates count: {(start, end)}"
    assert [i["slug"] for i in items][-2:] in (["t4d", "t4e"], ["t4e", "t4d"]), "undated items list last"


def test_project_span_empty_is_none():
    project = db._create_project("Empty Project", with_writeup=False)
    assert resolve_project_span(project, []) == (None, None), "#725: no real dates = no span (not created_at)"


def test_project_span_one_override_collapses_onto_it():
    project = db._create_project("Half Project", with_writeup=False)
    cards.update(project["id"], start=OLD)
    project = db.get_project(project["id"])
    assert resolve_project_span(project, []) == (OLD, OLD)


def test_project_span_respects_overrides():
    make_item("t6a", timestamp=T0 + 50, content_date=OLD)
    project = db._create_project("Overridden Project")
    db._add_item_to_project(project["id"], "t6a")
    cards.update(project["id"], start=OLD - 100, end=OLD + 999)
    project = db.get_project(project["id"])
    items = db.list_project_items(project["id"])
    start, end = resolve_project_span(project, items)
    assert (start, end) == (OLD - 100, OLD + 999), "explicit overrides win over derived item dates"


def test_project_span_excludes_writeup():
    # The write-up's own date is whenever it was authored, not the project's real chronology.
    make_item("t7a", timestamp=T0 + 60, content_date=OLD)
    make_item("t7b", timestamp=T0 + 70, content_date=OLD + 100)
    make_item("t7-writeup", timestamp=T0 + 80, content_date=OLD + 99999)  # authored long after
    # with_writeup=False: since #423 _create_project attaches its own blank write-up.
    project = db._create_project("Writeup Project", with_writeup=False)
    db._add_item_to_project(project["id"], "t7a")
    db._add_item_to_project(project["id"], "t7b")
    db._add_item_to_project(project["id"], "t7-writeup")
    conn = db.get_conn()  # fixture: an image as the write-up (cards.update only accepts document types)
    conn.execute("UPDATE projects SET writeup_slug = ? WHERE id = ?", ("t7-writeup", project["id"]))
    conn.commit()
    conn.close()
    project = db.get_project(project["id"])
    items = db.list_project_items(project["id"])
    start, end = resolve_project_span(project, items)
    assert (start, end) == (OLD, OLD + 100), "writeup's own date must not affect the span"


if __name__ == "__main__":
    # In file order: test_cutoff_is_the_first_upload creates the earliest item (the cutoff) first.
    tests = [v for k, v in globals().items() if k.startswith("test_")]
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
