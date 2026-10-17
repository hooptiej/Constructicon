#!/usr/bin/env python3
"""#699: the app refuses to start, clearly, when it can't write its data folders.

Throwaway DB + storage + exports (scripts/_testenv.py). No server, no network. POSIX permissions,
so run it on Linux as a non-root user (the app image runs as uid 1000):

    python scripts/test_data_dirs_699.py

Checks: writable folders pass; an unwritable storage folder, an unwritable DB folder, a DB file the
process can't write and a missing folder under an unwritable parent are each reported with the path,
what was tried, the owner and the fix command; the web startup hook raises with that message before
it touches the DB; the MCP server exits non-zero with it before init_db; and deploy.sh still parses
and keeps its #698 pieces (compose check, optional sudo, the ZFS route, the db/ copy fallback).
"""
import os
import shutil
import subprocess
import sys
import types
from pathlib import Path

import _testenv  # noqa: E402
TMP = _testenv.isolate("datadirs699-")
ROOT = os.path.abspath(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
sys.path.insert(0, ROOT)
sys.modules.setdefault("cairosvg", types.ModuleType("cairosvg"))  # no libcairo needed here

FAILS = []


def check(name, cond, extra=""):
    print(("PASS " if cond else "FAIL ") + name + (f"  [{extra}]" if (extra and not cond) else ""))
    if not cond:
        FAILS.append(name)


from core import paths  # noqa: E402

posix = hasattr(os, "getuid")
is_root = posix and os.geteuid() == 0
db_path = Path(os.environ["CONSTRUCTICON_DB_PATH"])
db_path.parent.mkdir(parents=True, exist_ok=True)
paths.storage_dir().mkdir(parents=True, exist_ok=True)
paths.exports_dir().mkdir(parents=True, exist_ok=True)

check("writable folders: no problems", paths.unwritable_data_dirs(db_path) == [],
      paths.unwritable_data_dirs(db_path))
check("the check leaves no write-test file behind",
      not any(p.name.startswith(".constructicon-write-check-") for p in paths.storage_dir().iterdir()))

if not posix or is_root:
    print("SKIP the refusal checks: they need POSIX permissions and a non-root user "
          "(run in the app image, which runs as uid 1000)")
else:
    storage = paths.storage_dir()
    os.chmod(storage, 0o500)
    try:
        problems = paths.unwritable_data_dirs(db_path)
        hit = [p for p in problems if p["path"] == str(storage)]
        check("unwritable storage folder is reported", len(hit) == 1, problems)
        if hit:
            p = hit[0]
            check("  says what was tried", p["tried"] == "create a file in it", p)
            check("  says what happened", "Permission denied" in p["error"], p)
            check("  names its owner and mode", f"owner uid {os.getuid()}" in p["owner"] and "mode 0500" in p["owner"], p)
        msg = paths.data_dirs_error("constructicon-web", problems)
        print("---- message ----\n" + msg + "\n-----------------")
        check("message: refuses to start", msg.startswith("constructicon-web: refusing to start"), msg)
        check("message: the uid the app runs as", f"runs as uid {os.getuid()}, gid {os.getgid()}" in msg, msg)
        check("message: the path", str(storage) in msg, msg)
        check("message: the one-line fix", f"sudo chown -R {os.getuid()}:{os.getgid()} " in msg, msg)
        check("message: points at the README", "Install on a new host" in msg, msg)

        # The web startup hook refuses before it opens the DB.
        from web import app as web_app
        raised = None
        try:
            web_app._startup_as_system()
        except RuntimeError as exc:
            raised = str(exc)
        check("web startup raises with the message", raised is not None and "refusing to start" in raised
              and str(storage) in raised, raised)
        check("web startup refused before creating the DB", not db_path.exists())

        # The MCP server exits non-zero with it, before anything reads the DB (it reads the hobby
        # label at import, so the DB folder is locked too: that used to crash with "unable to open
        # database file" before any check ran).
        os.chmod(db_path.parent, 0o500)
        env = dict(os.environ)
        r = subprocess.run([sys.executable, "-m", "mcp_server.server"], cwd=ROOT, env=env,
                           capture_output=True, text=True, timeout=300)
        check("MCP exits non-zero", r.returncode != 0, r.returncode)
        check("MCP prints the message", "constructicon-mcp: refusing to start" in r.stderr
              and str(storage) in r.stderr and str(db_path.parent) in r.stderr
              and "sudo chown -R" in r.stderr, r.stderr[-800:])
        check("MCP never got as far as opening the DB", "unable to open database file" not in r.stderr,
              r.stderr[-800:])
        check("MCP refused before creating the DB", not db_path.exists())
    finally:
        os.chmod(storage, 0o750)
        os.chmod(db_path.parent, 0o750)

    # A DB folder the app can't write, and the known compose path in the fix.
    dbdir = db_path.parent
    os.chmod(dbdir, 0o500)
    try:
        problems = paths.unwritable_data_dirs(db_path)
        check("unwritable DB folder is reported", any(p["path"] == str(dbdir) for p in problems), problems)
    finally:
        os.chmod(dbdir, 0o750)
    msg = paths.data_dirs_error("x", [{"path": "/app/data", "folder": "/app/data", "role": "db",
                                       "tried": "t", "error": "e", "owner": "o"}])
    check("compose path for /app/data is ./db", " ./db" in msg and '"$PWD/db":/d' in msg, msg)

    # A DB file it can't write (the folder is fine).
    db_path.write_bytes(b"")
    os.chmod(db_path, 0o400)
    try:
        problems = paths.unwritable_data_dirs(db_path)
        check("unwritable DB file is reported", any(p["path"] == str(db_path) for p in problems), problems)
    finally:
        os.chmod(db_path, 0o640)
        db_path.unlink()

    # A folder that doesn't exist yet, under a parent it can't write.
    parent = Path(TMP) / "locked"
    parent.mkdir()
    os.environ["CONSTRUCTICON_EXPORTS_DIR"] = str(parent / "exports")
    os.chmod(parent, 0o500)
    try:
        problems = paths.unwritable_data_dirs(db_path)
        hit = [p for p in problems if p["path"] == str(parent / "exports")]
        check("missing folder under an unwritable parent is reported", len(hit) == 1, problems)
        check("  names the parent to fix", bool(hit) and hit[0]["folder"] == str(parent), hit)
    finally:
        os.chmod(parent, 0o750)
    os.environ["CONSTRUCTICON_EXPORTS_DIR"] = str(Path(TMP) / "exports")

# deploy.sh (#698): parses, and keeps its pieces. Behaviour is exercised on a Docker host (PR notes).
deploy = Path(ROOT, "scripts", "deploy.sh")
text = deploy.read_bytes()
check("deploy.sh has LF endings only", b"\r" not in text)
text = text.decode()
bash = shutil.which("bash")
if bash and posix:
    r = subprocess.run([bash, "-n", str(deploy)], capture_output=True, text=True)
    check("deploy.sh parses (bash -n)", r.returncode == 0, r.stderr)
for needle, why in [
    ("compose version", "checks for Docker Compose v2"),
    ("DOCKER=(sudo docker)", "sudo only when the socket needs it"),
    ("zfs snapshot", "keeps the ZFS snapshot route"),
    ("source.backup(target)", "the no-ZFS fallback uses the SQLite backup API"),
    ("--backup-plan", "has the dry run"),
    ('if [ "$WEB_ENV" = dev ]; then', "skips the backup on the dev/test instance (owner, 2026-10-17)"),
]:
    check(f"deploy.sh {why}", needle in text)
check("deploy.sh runs no bare `sudo docker compose`", "sudo docker compose " not in
      "\n".join(l for l in text.splitlines() if not l.lstrip().startswith("#")))

print("\n%s" % ("ALL PASS" if not FAILS else "FAILED: " + ", ".join(FAILS)))
