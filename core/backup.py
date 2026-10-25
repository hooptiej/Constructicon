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

Destination: paths.backup_dir() is derived from paths.storage_dir() the same way
the storage dir itself is derived (sibling of storage/, alongside imagerepo.db)
so it naturally lands at .../constructicon/backups/ wherever the app is
deployed, without hardcoding any particular environment's path. #442: CONSTRUCTICON_BACKUP_DIR
moves it anywhere else (Admin > Maintenance shows where, and warns when it can't be written).

Download (#442): GET /api/backups lists the newest zips; GET /api/backups/download streams the
newest (or a named one) to the browser. Reading only: a GET never builds a backup.
"""

import os
import re
import sqlite3
import tempfile
import zipfile
from datetime import datetime
from pathlib import Path

from core import db, paths
from core.errors import NotFound

# Simple retention: keep only the N most recent backups, deleting older ones
# on every new backup. Not a config knob — just a constant, per the issue's
# guidance not to build config infrastructure for this.
# Each backup zip is a full DB + storage/ snapshot (~1 GB and growing with the
# library), so keep the count low — this is a rolling safety net, not an archive.
# (#351: was 10, which quietly hoarded ~9 GB before anyone noticed.)
BACKUP_RETENTION_COUNT = 3

# #652: a backup zip is readable by the app's own user only.
BACKUP_FILE_MODE = 0o600

DB_ARCNAME = "imagerepo.db"
STORAGE_ARCPREFIX = "storage"


def _snapshot_db(dest_path):
    """Copy the live DB to dest_path using sqlite3's backup API rather than
    reading the file's bytes directly — this gives a consistent snapshot
    even if another connection is mid-write, and never blocks/locks the
    source for writers (source connection is opened read-only-ish; the
    backup() call takes its own short-lived read lock per page)."""
    # #453: a plain connection, not `?mode=ro`. A read-only URI connection
    # that can't use the WAL index can read the main file without the WAL
    # and snapshot stale rows. backup() still only reads from the source.
    source = sqlite3.connect(db.DB_PATH, timeout=30)
    try:
        dest = sqlite3.connect(dest_path)
        try:
            source.backup(dest)
        finally:
            dest.close()
    finally:
        source.close()


def _integrity(db_path):
    """'ok', or the first few PRAGMA integrity_check complaints (#453)."""
    conn = sqlite3.connect(db_path)
    try:
        rows = [r[0] for r in conn.execute("PRAGMA integrity_check").fetchmany(5)]
    finally:
        conn.close()
    return "ok" if rows == ["ok"] else rows


def _existing_backups():
    """All backup zips currently in the backup dir, oldest first."""
    backup_dir = paths.backup_dir()
    if not backup_dir.is_dir():
        return []
    return sorted(backup_dir.glob("constructicon-backup-*.zip"), key=lambda p: p.name)


BACKUP_NAME_RE = re.compile(r"constructicon-backup-\d{8}-\d{6}\.zip")


def list_backups(limit=BACKUP_RETENTION_COUNT):
    """#442: the newest `limit` finished backup zips, newest first, as {filename, size, created_at}.
    Reads the folder only: never makes a backup. A half-written `.zip.part` is never listed."""
    out = []
    for p in reversed(_existing_backups()):
        if not BACKUP_NAME_RE.fullmatch(p.name):
            continue
        try:
            st = p.stat()
        except OSError:  # silent-ok: pruned by a concurrent backup between the glob and the stat
            continue
        out.append({"filename": p.name, "size": st.st_size, "created_at": st.st_mtime})
        if len(out) >= limit:
            break
    return out


def backup_file(filename=None):
    """#442: the Path of a finished backup to download: `filename` (one of list_backups()' names,
    nothing else, so no path can be smuggled in), or the newest when None. NotFound, saying what
    to do, when there is none."""
    folder = paths.backup_dir()
    if filename is None:
        newest = list_backups(limit=1)
        if not newest:
            raise NotFound(f"There is no backup yet in {folder}. Make one with \"Back up now\" in "
                           "Admin > Maintenance, then download it.")
        filename = newest[0]["filename"]
    if not BACKUP_NAME_RE.fullmatch(filename or ""):
        raise NotFound(f"{filename!r} is not a backup name (constructicon-backup-YYYYMMDD-HHMMSS.zip).")
    path = folder / filename
    if not path.is_file():
        raise NotFound(f"No backup named {filename} in {folder} (only the newest "
                       f"{BACKUP_RETENTION_COUNT} are kept; older ones are pruned).")
    return path


def _enforce_retention():
    backups = _existing_backups()
    excess = len(backups) - BACKUP_RETENTION_COUNT
    for old in backups[:max(excess, 0)]:
        old.unlink(missing_ok=True)


def create_backup():
    """Create a new timestamped backup zip containing the DB snapshot and
    every file in storage/, write it to the backup dir, enforce retention, and
    return metadata about the archive. Read-only with respect to the live
    DB and storage/ — nothing in either is modified or deleted by this."""
    backup_dir = paths.backup_dir()
    backup_dir.mkdir(parents=True, exist_ok=True, mode=0o750)  # #652 (an existing dir keeps its mode)
    timestamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    filename = f"constructicon-backup-{timestamp}.zip"
    dest = backup_dir / filename

    with tempfile.TemporaryDirectory() as tmp_dir:
        tmp_db = Path(tmp_dir) / "imagerepo.db"
        _snapshot_db(tmp_db)
        # #453: report the snapshot's integrity with every backup so corruption
        # can't sit unnoticed. Never blocks the backup: a copy of a damaged DB
        # is still worth keeping.
        integrity = _integrity(tmp_db)

        # Write to a .part file first so a crash/kill mid-zip never leaves a
        # half-written archive sitting there looking like a real backup.
        tmp_zip = dest.with_suffix(".zip.part")
        # #652: the zip holds the whole archive (sensitive items, password hashes), so it is
        # owner-only (0600) from its first byte, whatever the process umask, and stays so.
        fd = os.open(tmp_zip, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, BACKUP_FILE_MODE)
        os.chmod(tmp_zip, BACKUP_FILE_MODE)  # a leftover .part keeps its old mode through O_CREAT
        with os.fdopen(fd, "wb") as fh, zipfile.ZipFile(fh, "w", zipfile.ZIP_DEFLATED) as zf:
            zf.write(tmp_db, arcname=DB_ARCNAME)
            store = paths.storage_dir()
            if store.is_dir():
                for path in sorted(store.rglob("*")):
                    if not path.is_file():
                        continue
                    arcname = f"{STORAGE_ARCPREFIX}/{path.relative_to(store)}"
                    zf.write(path, arcname=arcname)
        tmp_zip.replace(dest)

    _enforce_retention()

    stat = dest.stat()
    return {
        "filename": filename,
        "path": str(dest),
        "size": stat.st_size,
        "created_at": stat.st_mtime,
        "integrity": integrity,
    }
