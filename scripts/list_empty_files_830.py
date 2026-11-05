#!/usr/bin/env python3
"""List the items whose stored file is 0 bytes (#830). READ-ONLY.

A 0-byte upload used to be stored like a real file (an interrupted browser upload, a broken export, a
placeholder) and only showed up later as "File is not a zip file" or a blank thumbnail. New 0-byte uploads
are refused at ingest now (`empty_file`); this lists the ones already in the library so each can be
re-uploaded (the item page's More > Replace file) or removed.

It opens the SQLite file with a `mode=ro` URI (nothing is written, no migrations run) and only stats the
stored files (never reads or changes them). Items with no stored file (links, YouTube rows) and redacted
items (their file is held in the trash) are not checked; a stored file that is missing from the storage
folder is listed separately, since that is a different problem with a different fix.

    python3 scripts/list_empty_files_830.py [DB_PATH] [--storage DIR]

DB_PATH defaults to CONSTRUCTICON_DB_PATH, the storage folder to CONSTRUCTICON_STORAGE_DIR (else
<repo>/storage). On the NAS, from the host, in the install's checkout:

    ./scripts/run_tool.sh constructicon-test scripts/list_empty_files_830.py

Exit status: 0 after printing the report (empty files found or not), 2 when the database can't be opened.
"""

import argparse
import os
import pathlib
import sqlite3
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
from core import paths  # noqa: E402  (stdlib only; core/__init__.py imports nothing)


def open_ro(path):
    """A read-only connection (mode=ro); a WAL database in a read-only folder is retried with immutable=1
    (right only for a copy nothing writes to), and the retry is said out loud."""
    base = pathlib.Path(os.path.abspath(path)).as_uri()
    try:
        conn = sqlite3.connect(base + "?mode=ro", uri=True)
        conn.execute("SELECT 1 FROM capture_events LIMIT 1")
    except sqlite3.OperationalError as e:
        if "no such table" in str(e) or "unable to open" in str(e):
            raise
        print(f"note: mode=ro failed ({e}); opening with immutable=1 (only correct for a copy nothing writes to)")
        conn = sqlite3.connect(base + "?mode=ro&immutable=1", uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def find_empty(conn, storage_dir):
    """-> {"checked": n, "empty": [row dict], "missing": [row dict]} over the items that hold a stored file."""
    storage_dir = pathlib.Path(storage_dir)
    rows = conn.execute(
        "SELECT slug, filename, display_name, stored_filename, media_type, timestamp, tech FROM capture_events "
        "WHERE stored_filename IS NOT NULL AND stored_filename != '' AND COALESCE(redacted, 0) = 0 "
        "ORDER BY timestamp").fetchall()
    out = {"checked": 0, "empty": [], "missing": []}
    for r in rows:
        out["checked"] += 1
        try:
            size = (storage_dir / r["stored_filename"]).stat().st_size
        except FileNotFoundError:
            out["missing"].append(dict(r))
            continue
        if size == 0:
            out["empty"].append(dict(r))
    return out


def _label(r):
    return r["display_name"] or r["filename"] or r["stored_filename"]


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("db_path", nargs="?", default=os.environ.get("CONSTRUCTICON_DB_PATH"))
    ap.add_argument("--storage", default=None)
    a = ap.parse_args(argv)
    if not a.db_path:
        print("No database given: pass DB_PATH or set CONSTRUCTICON_DB_PATH. Nothing was read.")
        return 2
    storage = pathlib.Path(a.storage) if a.storage else paths.storage_dir()
    if not storage.is_dir():
        print(f"The storage folder {storage} does not exist, so no file could be checked. "
              "Pass --storage DIR or set CONSTRUCTICON_STORAGE_DIR. Nothing was read.")
        return 2
    try:
        conn = open_ro(a.db_path)
    except sqlite3.Error as e:
        print(f"Could not open {a.db_path} read-only: {e}. Nothing was read.")
        return 2
    try:
        found = find_empty(conn, storage)
    finally:
        conn.close()

    print(f"Checked {found['checked']} stored file(s) in {storage}.")
    print(f"{len(found['empty'])} item(s) have a 0-byte stored file:")
    for r in found["empty"]:
        print(f"  /object/{r['slug']}  {_label(r)!r}  ({r['media_type']}, source: {r['tech']})")
    if found["empty"]:
        print("Each can be re-uploaded (the item page's More > Replace file) or deleted. Nothing was changed.")
    if found["missing"]:
        print(f"{len(found['missing'])} item(s) name a stored file that is not in the storage folder "
              "(a different problem, listed so it is not lost):")
        for r in found["missing"]:
            print(f"  /object/{r['slug']}  {_label(r)!r}  ({r['stored_filename']})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
