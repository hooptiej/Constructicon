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
        log.warning("CONSTRUCTICON_UMASK=%r is not an octal umask (e.g. 027); using %03o", raw, DEFAULT_UMASK)
        return DEFAULT_UMASK
    if not 0 <= value <= 0o777:
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


# --- Whose house is it? (#699) --------------------------------------------------------------------
# The image runs as a fixed uid (Dockerfile APP_UID, 1000). A bind-mounted data folder the host
# created as another user (root, or a host account that isn't 1000) used to surface as a cryptic
# "unable to open database file" or a Permission denied on the first upload. Both processes check
# their data folders at startup instead and refuse to start with a message that names the folder,
# its owner, the uid the app runs as and the one command that fixes it.

# Where docker-compose.yml.example mounts each container folder from, for the fix command.
_COMPOSE_EXAMPLE_HOST_DIR = {
    "/app/data": "./db",
    "/app/storage": "./storage",
    "/app/backups": "./backups",
    "/app/exports": "./exports",
}


def _owner_text(path):
    try:
        st = os.stat(path)
    except OSError as exc:
        return f"can't stat it ({exc.strerror or exc})"
    return f"owner uid {st.st_uid}, gid {st.st_gid}, mode {oct(st.st_mode & 0o7777)[2:].zfill(4)}"


def _try_write(directory):
    """Create and delete a throwaway file in `directory`: the real test (it also catches read-only
    mounts and ACLs that os.access can misjudge). Returns None, or the OS error text."""
    import tempfile
    try:
        fd, name = tempfile.mkstemp(prefix=".constructicon-write-check-", dir=directory)
    except OSError as exc:
        return exc.strerror or str(exc)
    os.close(fd)
    try:
        os.unlink(name)
    except OSError as exc:
        return f"created a test file but couldn't delete it: {exc.strerror or exc}"
    return None


def unwritable_data_dirs(db_path):
    """The data folders this process can't write, as a list of dicts {path, role, tried, error,
    owner}: the database folder (and the DB file and its -wal/-shm when they exist), storage,
    backups and exports. A folder that doesn't exist yet is judged by its nearest existing parent
    (the app creates it there on first use). import/ is not checked: the app never writes it."""
    problems = []
    db_path = Path(db_path)
    targets = [
        (db_path.parent, f"the database folder (CONSTRUCTICON_DB_PATH={db_path})"),
        (storage_dir(), "uploaded files (CONSTRUCTICON_STORAGE_DIR)"),
        (backup_dir(), "backups (next to the storage folder)"),
        (exports_dir(), "static-site exports (CONSTRUCTICON_EXPORTS_DIR)"),
    ]
    seen = set()
    for directory, role in targets:
        key = str(directory)
        if key in seen:
            continue
        seen.add(key)
        if directory.is_dir():
            error = _try_write(directory)
            if error:
                problems.append({"path": key, "folder": key, "role": role, "tried": "create a file in it",
                                 "error": error, "owner": _owner_text(directory)})
            continue
        parent = directory.parent
        while not parent.exists() and parent != parent.parent:
            parent = parent.parent
        if not os.access(parent, os.W_OK | os.X_OK):
            problems.append({"path": key, "folder": str(parent), "role": role,
                             "tried": f"create it (it doesn't exist yet) inside {parent}",
                             "error": "that folder isn't writable", "owner": f"{parent}: {_owner_text(parent)}"})
    for suffix in ("", "-wal", "-shm"):
        f = Path(f"{db_path}{suffix}")
        if f.is_file() and not os.access(f, os.R_OK | os.W_OK):
            problems.append({"path": str(f), "folder": str(db_path.parent),
                             "role": "the database file" if not suffix else f"the database's {suffix[1:]} file",
                             "tried": "open it for reading and writing", "error": "Permission denied",
                             "owner": _owner_text(f)})
    return problems


def _host_dir_hint(container_dir):
    """The host folder to chown for a container folder: docker-compose.yml.example's mount source,
    or a placeholder naming the container path when the install mounts something else."""
    for cdir, hdir in _COMPOSE_EXAMPLE_HOST_DIR.items():
        if container_dir == cdir or container_dir.startswith(cdir + "/"):
            return hdir
    return f"<the host folder mounted at {container_dir}>"


def data_dirs_error(process_name, problems):
    """The refusal message for `problems` (from unwritable_data_dirs): what was tried, what
    happened, and the exact command that fixes it."""
    uid = getattr(os, "getuid", lambda: None)()
    gid = getattr(os, "getgid", lambda: None)()
    who = f"uid {uid}, gid {gid}" if uid is not None else "the current user"
    ids = f"{uid}:{gid}" if uid is not None else "1000:1000"
    lines = [f"{process_name}: refusing to start: it can't write {len(problems)} of its data location(s) "
             f"(this process runs as {who}). (#699)"]
    host_dirs = []
    for p in problems:
        lines.append(f"  {p['path']}  ({p['role']})")
        lines.append(f"    tried: {p['tried']} -> {p['error']}")
        lines.append(f"    now:   {p['owner']}")
        hdir = _host_dir_hint(p["folder"])
        if hdir not in host_dirs:
            host_dirs.append(hdir)
    targets = " ".join(host_dirs)
    lines += [
        f"Fix: the host folders mounted there must be owned by {ids}. On the Docker host, from the folder",
        "holding docker-compose.yml (paths as in docker-compose.yml.example; use yours if you mount others):",
        f"    sudo chown -R {ids} {targets}",
    ]
    known = [h for h in host_dirs if h.startswith("./")]
    if known:
        lines.append("  or, without sudo, with the app image itself (one line per folder):")
    for h in known:
        lines.append(f"    docker run --rm --user 0 --entrypoint chown -v \"$PWD/{h[2:]}\":/d constructicon:latest -R {ids} /d")
    lines.append("Then start it again. See README.md, \"Install on a new host\".")
    return "\n".join(lines)
