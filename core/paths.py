"""Where the install keeps its files, resolved when used rather than at import (#578, #576).

Every on-disk location the app writes to (uploaded files, the trash, backups, static-export
builds) comes through here. Each function reads its environment override on every call, so a test
can import the app first and repoint the directories afterwards, and a forgotten override can't be
frozen in at import time.

    CONSTRUCTICON_STORAGE_DIR   uploaded files (default <repo>/storage). The trash lives inside it
                                (<storage>/.trash) and backups sit beside it (<storage>/../backups).
    CONSTRUCTICON_EXPORTS_DIR   static-export builds (default <repo>/exports): timestamped builds,
                                `current` (what /preview serves) and `.publish` work trees.

Unset, the defaults are exactly the paths the app has always used. The database has its own override
(CONSTRUCTICON_DB_PATH, core/db.py).

File permissions (#652): `restrict_new_files()` sets the process umask once at startup (the web app
and the MCP server both call it), so everything the app creates after that (the SQLite DB and its
-wal/-shm, stored uploads, thumbnails, the trash, backups, export builds) is not readable by other
users on the host: files 0640, directories 0750 (owner rw, the app's group read, nobody else).
Backup zips are tighter still, 0600 (core/backup.py sets it on the file itself).

    CONSTRUCTICON_UMASK         override the umask, in octal (default 027). An install whose data must
                                be readable by another account (e.g. a guest SMB share) can set 022.
"""

import logging
import os
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
TRASH_DIR_NAME = ".trash"

DEFAULT_UMASK = 0o027
log = logging.getLogger("constructicon.paths")


def configured_umask():
    """The umask to run with: CONSTRUCTICON_UMASK (octal, e.g. "027") or DEFAULT_UMASK. A bad value
    is logged and the default used, so a typo can never loosen permissions to 000."""
    raw = (os.environ.get("CONSTRUCTICON_UMASK") or "").strip()
    if not raw:
        return DEFAULT_UMASK
    try:
        value = int(raw, 8)
    except ValueError:
        value = None
        log.warning("CONSTRUCTICON_UMASK=%r is not an octal number", raw)
    if value is None or not 0 <= value <= 0o777:
        log.warning("CONSTRUCTICON_UMASK=%r is not an octal umask (e.g. 027); using %03o", raw, DEFAULT_UMASK)
        return DEFAULT_UMASK
    return value


def restrict_new_files():
    """Set the process umask (#652). Call once, at process start, before anything writes a file.
    Process-wide: it covers every thread and every subprocess (ffmpeg, ghostscript) started after.
    Returns the umask now in force."""
    mask = configured_umask()
    os.umask(mask)
    return mask


def _override(name, default):
    value = (os.environ.get(name) or "").strip()
    return Path(value) if value else default


def storage_dir():
    return _override("CONSTRUCTICON_STORAGE_DIR", REPO_ROOT / "storage")


def trash_dir(batch_id=None):
    root = storage_dir() / TRASH_DIR_NAME
    return root / batch_id if batch_id else root


def backup_dir():
    return storage_dir().parent / "backups"


def exports_dir():
    return _override("CONSTRUCTICON_EXPORTS_DIR", REPO_ROOT / "exports")


def current_export_dir():
    """The build /preview serves and publish pushes."""
    return exports_dir() / "current"


def publish_work_dir(target):
    return exports_dir() / ".publish" / target
