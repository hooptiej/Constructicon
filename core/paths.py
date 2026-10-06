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
"""

import os
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
TRASH_DIR_NAME = ".trash"


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
