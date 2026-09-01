"""Standalone backup safety net (#20).

Deliberately NOT wired into any delete path (/api/delete-all, /api/delete,
/api/image/{slug}/delete) — see #19's history: a delete-all test once wiped
storage files while only the DB got backed up, leaving orphaned rows with no
files behind. A backup that only ran as a side effect of deleting could let
someone assume "that delete already backed this up" when it hadn't. This is
triggered on its own (POST /api/backup), never automatically.

Scope: one zip containing both the sqlite DB (a consistent point-in-time
snapshot, not just a raw copy of a file that other connections may be
writing to) and every file under storage/ — a files-only or DB-only backup
isn't restorable on its own, since the DB rows point at filenames on disk
and the files are meaningless without the rows describing them.

Destination: BACKUP_DIR is derived from storage.STORAGE_DIR the same way
STORAGE_DIR itself is derived (sibling of storage/, alongside imagerepo.db)
so it naturally lands at .../constructicon/backups/ wherever the app is
deployed, without hardcoding any particular environment's path.
"""

import sqlite3
import tempfile
import zipfile
from datetime import datetime
from pathlib import Path

from core import db, storage

BACKUP_DIR = storage.STORAGE_DIR.parent / "backups"

# Simple retention: keep only the N most recent backups, deleting older ones
# on every new backup. Not a config knob — just a constant, per the issue's
# guidance not to build config infrastructure for this.
BACKUP_RETENTION_COUNT = 10

DB_ARCNAME = "imagerepo.db"
STORAGE_ARCPREFIX = "storage"


def _snapshot_db(dest_path):
    """Copy the live DB to dest_path using sqlite3's backup API rather than
    reading the file's bytes directly — this gives a consistent snapshot
    even if another connection is mid-write, and never blocks/locks the
    source for writers (source connection is opened read-only-ish; the
    backup() call takes its own short-lived read lock per page)."""
    source = sqlite3.connect(f"file:{db.DB_PATH}?mode=ro", uri=True)
    try:
        dest = sqlite3.connect(dest_path)
        try:
            source.backup(dest)
        finally:
            dest.close()
    finally:
        source.close()


def _existing_backups():
    """All backup zips currently in BACKUP_DIR, oldest first."""
    if not BACKUP_DIR.is_dir():
        return []
    return sorted(BACKUP_DIR.glob("constructicon-backup-*.zip"), key=lambda p: p.name)


def _enforce_retention():
    backups = _existing_backups()
    excess = len(backups) - BACKUP_RETENTION_COUNT
    for old in backups[:max(excess, 0)]:
        old.unlink(missing_ok=True)


def create_backup():
    """Create a new timestamped backup zip containing the DB snapshot and
    every file in storage/, write it to BACKUP_DIR, enforce retention, and
    return metadata about the archive. Read-only with respect to the live
    DB and storage/ — nothing in either is modified or deleted by this."""
    BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    filename = f"constructicon-backup-{timestamp}.zip"
    dest = BACKUP_DIR / filename

    with tempfile.TemporaryDirectory() as tmp_dir:
        tmp_db = Path(tmp_dir) / "imagerepo.db"
        _snapshot_db(tmp_db)

        # Write to a .part file first so a crash/kill mid-zip never leaves a
        # half-written archive sitting there looking like a real backup.
        tmp_zip = dest.with_suffix(".zip.part")
        with zipfile.ZipFile(tmp_zip, "w", zipfile.ZIP_DEFLATED) as zf:
            zf.write(tmp_db, arcname=DB_ARCNAME)
            if storage.STORAGE_DIR.is_dir():
                for path in sorted(storage.STORAGE_DIR.rglob("*")):
                    if not path.is_file():
                        continue
                    arcname = f"{STORAGE_ARCPREFIX}/{path.relative_to(storage.STORAGE_DIR)}"
                    zf.write(path, arcname=arcname)
        tmp_zip.replace(dest)

    _enforce_retention()

    stat = dest.stat()
    return {
        "filename": filename,
        "path": str(dest),
        "size": stat.st_size,
        "created_at": stat.st_mtime,
    }
