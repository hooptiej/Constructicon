#!/usr/bin/env python3
"""Batch 1, "Patch Tuesday for the Plumbing": #442 (backup download + backup folder), #736 (secret
files readable, compose example de-homed), #740 / #741 (deploy.sh's error paths and sidecar builds).

Throwaway DB + storage + exports (scripts/_testenv.py). No server, no network, no Docker: deploy.sh
runs against a fake `docker` on PATH. POSIX permission checks and the deploy.sh runs need Linux and
a non-root user (the app image runs as uid 1000); elsewhere they print SKIP.

    python scripts/test_plumbing_batch1.py

Checks:
  * #442 paths: CONSTRUCTICON_BACKUP_DIR moves backups, unset keeps <storage>/../backups;
    backup_dir_status says where, from what, and why it can't be written (a file in the way, an
    unwritable folder, an unwritable parent).
  * #442 core: list_backups is newest first, at most 3, skips .part files and stray names;
    backup_file refuses traversal and unknown names, and says what to do when there's none.
  * #442 HTTP: GET /api/backups (folder + list), the download streams the newest or a named zip
    (attachment, application/zip, the exact bytes), refuses a bad name, never builds a backup; anon
    401, viewer and editor 403; Admin shows the folder; the Account page shows the download to an
    admin and a greyed "admins only" control with the reason to everyone else (never hidden).
  * #736 secrets: missing / directory / empty / unreadable token files are reported with the fix; a
    required one stops web startup, an optional one (the restart valve) only prints a notice.
  * #736 compose example: no home IP or 999 hard-coded, the .env names documented, the secrets
    folder mount, backups mounted, create_host_path: false on the single-file secret mounts.
  * #736 .gitignore: db/, backups/, import/.
  * #740 / #741 deploy.sh (fake docker): no compose file, compose config failing, not-JSON output and
    no web service each stop with a plain message (no traceback, no "unbound variable"); a good
    config's --backup-plan names every service with a build: section; --image is documented.
"""
import os
import shutil
import subprocess
import sys
import types
from pathlib import Path

import _testenv  # noqa: E402
TMP = _testenv.isolate("plumbing1-")
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.modules.setdefault("cairosvg", types.ModuleType("cairosvg"))  # no libcairo needed here

FAILS = []


def check(name, cond, extra=""):
    print(("PASS " if cond else "FAIL ") + name + (f"  [{extra}]" if (extra and not cond) else ""))
    if not cond:
        FAILS.append(name)


from core import actor, backup, db, errors, paths, users  # noqa: E402

posix = hasattr(os, "getuid")
is_root = posix and os.geteuid() == 0
perm_ok = posix and not is_root

# ---- #442 paths ----------------------------------------------------------------------------------
print("--- #442 backup folder ---")
os.environ.pop("CONSTRUCTICON_BACKUP_DIR", None)
default_dir = paths.storage_dir().parent / "backups"
check("unset: backups beside storage (unchanged default)", paths.backup_dir() == default_dir)
st = paths.backup_dir_status()
check("unset: status says default", st["source"] == "default" and "not set" in st["source_text"], st)
check("unset: missing folder under a writable parent is writable, no problem",
      st["writable"] and st["problem"] is None, st)

custom = Path(TMP) / "elsewhere" / "zips"
os.environ["CONSTRUCTICON_BACKUP_DIR"] = str(custom)
check("env: backup_dir follows CONSTRUCTICON_BACKUP_DIR", paths.backup_dir() == custom)
st = paths.backup_dir_status()
check("env: status names the variable", st["source"] == "env" and "CONSTRUCTICON_BACKUP_DIR" in st["source_text"], st)
custom.mkdir(parents=True)
st = paths.backup_dir_status()
check("env: existing writable folder", st["exists"] and st["writable"] and st["problem"] is None, st)

blocker = Path(TMP) / "afile"
blocker.write_text("x")
os.environ["CONSTRUCTICON_BACKUP_DIR"] = str(blocker)
st = paths.backup_dir_status()
check("a file in the way: not writable, says so", not st["writable"] and "not a folder" in (st["problem"] or ""), st)

if perm_ok:
    locked = Path(TMP) / "locked"
    locked.mkdir()
    os.chmod(locked, 0o500)
    try:
        os.environ["CONSTRUCTICON_BACKUP_DIR"] = str(locked)
        st = paths.backup_dir_status()
        check("unwritable folder: warning with the owner and the fix",
              not st["writable"] and "can't be written" in (st["problem"] or "") and "uid" in st["problem"]
              and "CONSTRUCTICON_BACKUP_DIR" in st["problem"], st)
        os.environ["CONSTRUCTICON_BACKUP_DIR"] = str(locked / "sub" / "dir")
        st = paths.backup_dir_status()
        check("missing folder under an unwritable parent: says it can't be created",
              not st["writable"] and "can't be created" in (st["problem"] or ""), st)
        problems = paths.unwritable_data_dirs(db.DB_PATH)
        check("startup check (#699) names CONSTRUCTICON_BACKUP_DIR for that folder",
              any("CONSTRUCTICON_BACKUP_DIR" in p["role"] for p in problems), problems)
    finally:
        os.chmod(locked, 0o700)
else:
    print("SKIP unwritable-folder checks (need POSIX and a non-root user)")

os.environ["CONSTRUCTICON_BACKUP_DIR"] = str(custom)

# ---- #442 core -----------------------------------------------------------------------------------
print("--- #442 list / pick ---")
check("no backups: list is empty", backup.list_backups() == [])
try:
    backup.backup_file()
    check("no backups: newest refuses", False)
except errors.NotFound as exc:
    check("no backups: NotFound says to press Back up now", "Back up now" in exc.message, exc.message)

names = [f"constructicon-backup-2026010{i}-120000.zip" for i in range(1, 6)]
for i, n in enumerate(names):
    (custom / n).write_bytes(b"PK" + bytes([i]) * 10)
(custom / "constructicon-backup-20260109-120000.zip.part").write_bytes(b"half")
(custom / "notes.txt").write_text("not a backup")
listed = backup.list_backups()
check("list: newest first, 3 of them", [b["filename"] for b in listed] == names[::-1][:3],
      [b["filename"] for b in listed])
check("list: sizes reported", all(b["size"] == 12 for b in listed))
check("newest = the last name", backup.backup_file().name == names[-1])
check("named pick", backup.backup_file(names[1]).name == names[1])
for bad in ("../imagerepo.db", "constructicon-backup-x.zip", "/etc/passwd", "constructicon-backup-20260109-120000.zip.part"):
    try:
        backup.backup_file(bad)
        check(f"bad name refused: {bad}", False)
    except errors.NotFound:
        check(f"bad name refused: {bad}", True)
try:
    backup.backup_file("constructicon-backup-20200101-000000.zip")
    check("unknown (pruned) name refused", False)
except errors.NotFound as exc:
    check("unknown name: NotFound mentions pruning", "pruned" in exc.message, exc.message)
for n in names:
    (custom / n).unlink()
(custom / "constructicon-backup-20260109-120000.zip.part").unlink()

# ---- #442 HTTP -----------------------------------------------------------------------------------
print("--- #442 routes ---")
from starlette.testclient import TestClient  # noqa: E402
from web import app as webapp  # noqa: E402

db.init_db()
users.limiter.reset()
tok = _testenv.client(webapp.app)
anon = TestClient(webapp.app, follow_redirects=False)
r = tok.get("/api/backups")
check("GET /api/backups 200 for admin", r.status_code == 200, r.text[:200])
body = r.json()
check("lists nothing yet, shows the folder", body["backups"] == [] and body["dir"]["path"] == str(custom)
      and body["retention"] == backup.BACKUP_RETENTION_COUNT, body)
r = tok.get("/api/backups/download")
check("download with no backup: 404 with the reason", r.status_code == 404 and "Back up now" in r.json()["error"]["message"],
      r.text[:200])
check("a GET never makes a backup", backup.list_backups() == [])

r = tok.post("/api/backup")
check("POST /api/backup makes one", r.status_code == 200, r.text[:300])
made = r.json()["filename"]
r = tok.get("/api/backups")
row = r.json()["backups"][0]
check("listed with a download url", row["filename"] == made and row["download_url"].endswith(made), row)
r = tok.get("/api/backups/download")
data = (custom / made).read_bytes()
check("download newest: 200 application/zip", r.status_code == 200 and r.headers["content-type"].startswith("application/zip"),
      r.headers)
check("download newest: attachment named after the zip",
      "attachment" in r.headers.get("content-disposition", "") and made in r.headers.get("content-disposition", ""),
      r.headers.get("content-disposition"))
check("download newest: nosniff + no-store", r.headers.get("x-content-type-options") == "nosniff"
      and r.headers.get("cache-control") == "no-store", r.headers)
check("download newest: exactly the file's bytes", r.content == data)
r = tok.get("/api/backups/download", params={"name": made})
check("download by name", r.status_code == 200 and r.content == data)
r = tok.get("/api/backups/download", params={"name": "../db/imagerepo.db"})
check("download traversal: 404", r.status_code == 404, r.text[:200])
check("downloads made no new backup", [b["filename"] for b in backup.list_backups()] == [made])

r = anon.get("/api/backups/download")
check("anonymous: 401", r.status_code == 401, r.text[:200])
r = anon.get("/api/backups")
check("anonymous list: 401", r.status_code == 401, r.text[:200])

with actor.acting_as(actor.ACTOR_SCRIPT):
    users.create_user("boss1", "boss-password-123", "admin")
    users.create_user("viewer1", "viewer-password-123", "viewer")
    users.create_user("editor1", "editor-password-123", "editor")
signed = {}
for name, pw in (("boss1", "boss-password-123"), ("viewer1", "viewer-password-123"), ("editor1", "editor-password-123")):
    c = TestClient(webapp.app, base_url="http://testhost.local", follow_redirects=False)
    r = c.post("/api/auth/login", json={"username": name, "password": pw}, headers={"Origin": "http://testhost.local"})
    check(f"sign in {name}", r.status_code == 200, r.text[:200])
    signed[name] = c
for name in ("viewer1", "editor1"):
    r = signed[name].get("/api/backups/download")
    check(f"{name}: download 403 forbidden with the role it needs", r.status_code == 403
          and r.json()["error"]["code"] == "forbidden" and "admin" in r.json()["error"]["message"], r.text[:200])
r = signed["boss1"].get("/api/backups/download")
check("signed-in admin: download 200", r.status_code == 200 and r.content == data)

r = signed["boss1"].get("/admin")
check("Admin page has the backup folder + download list", r.status_code == 200 and 'id="admin-backup-dir"' in r.text
      and 'id="admin-backup-list"' in r.text, r.status_code)
r = signed["boss1"].get("/account/password")
_i = r.text.find("admins only")
check("Account page (admin): download link", 'href="/api/backups/download"' in r.text and 'id="account-backup-locked"' not in r.text,
      (r.status_code, 'href="/api/backups/download"' in r.text, r.text[max(_i - 200, 0):_i + 40]))
for name in ("viewer1", "editor1"):
    r = signed[name].get("/account/password")
    check(f"Account page ({name}): backup control shown greyed, labelled, with the reason",
          r.status_code == 200 and 'id="account-backup-locked"' in r.text and 'aria-disabled="true"' in r.text
          and "(admins only)" in r.text and "needs the admin role" in r.text
          and 'href="/api/backups/download"' not in r.text, r.status_code)

# ---- #736 secret files ---------------------------------------------------------------------------
print("--- #736 secrets ---")
sec = Path(TMP) / "secrets"
sec.mkdir()
good = sec / "constructicon_token"
good.write_text("x" * 40)
check("readable token: no problem", paths.secret_file_problems({"CONSTRUCTICON_INSTALL_TOKEN_FILE": str(good)}) == [])
check("unset variables: nothing checked", paths.secret_file_problems({}) == [])
missing = paths.secret_file_problems({"CONSTRUCTICON_INSTALL_TOKEN_FILE": str(sec / "nope")})
check("missing token: kind missing, required", len(missing) == 1 and missing[0]["kind"] == "missing"
      and missing[0]["required"], missing)
d = sec / "ollama_restart_token"
d.mkdir()
probs = paths.secret_file_problems({"CAPTION_RESTART_TOKEN_FILE": str(d)})
check("restart token as a directory: explained (Docker made it), optional",
      len(probs) == 1 and probs[0]["kind"] == "directory" and "root-owned" in probs[0]["detail"]
      and not probs[0]["required"] and "rmdir" in probs[0]["fix"], probs)
msg = paths.secret_files_message("constructicon-web", probs)
check("optional problem: a notice, not a refusal", "refusing" not in msg and "is OFF" in msg
      and "to turn it on" in msg, msg)
empty = sec / "empty_token"
empty.write_text("")
probs = paths.secret_file_problems({"CONSTRUCTICON_INSTALL_TOKEN_FILE": str(empty)})
check("empty token file: kind empty", len(probs) == 1 and probs[0]["kind"] == "empty", probs)
msg = paths.secret_files_message("constructicon-web", probs)
check("required problem: refusal with what happened and the fix",
      "refusing to start" in msg and "what happened" in msg and "fix:" in msg, msg)
check("messages never contain a token value", "x" * 40 not in msg)
if perm_ok:
    os.chmod(good, 0o000)
    try:
        probs = paths.secret_file_problems({"CONSTRUCTICON_INSTALL_TOKEN_FILE": str(good)})
        check("unreadable token: kind unreadable with owner + chown fix",
              len(probs) == 1 and probs[0]["kind"] == "unreadable" and "chown 1000:1000" in probs[0]["fix"]
              and "uid" in probs[0]["detail"], probs)
    finally:
        os.chmod(good, 0o600)
else:
    print("SKIP unreadable-secret check (need POSIX and a non-root user)")

# The web startup hook refuses on a broken required secret, and only notes an optional one.
os.environ["CAPTION_RESTART_TOKEN_FILE"] = str(d)
try:
    webapp._startup_checks()
    check("web startup: optional secret problem does not stop it", True)
except RuntimeError as exc:
    check("web startup: optional secret problem does not stop it", False, str(exc)[:300])
os.environ.pop("CAPTION_RESTART_TOKEN_FILE", None)
os.environ["CONSTRUCTICON_INSTALL_TOKEN_FILE"] = str(d)
try:
    webapp._startup_checks()
    check("web startup: unreadable install token stops it", False)
except RuntimeError as exc:
    check("web startup: unreadable install token stops it, saying why", "refusing to start" in str(exc)
          and "directory" in str(exc), str(exc)[:300])
finally:
    os.environ.pop("CONSTRUCTICON_INSTALL_TOKEN_FILE", None)

# ---- #736 compose example + .gitignore ------------------------------------------------------------
print("--- #736 compose example ---")
compose = (ROOT / "docker-compose.yml.example").read_text(encoding="utf-8")
check("no home IP in the example", "10.0.1." not in compose)
check("socket group from .env", '"${DOCKER_SOCKET_GID:-999}"' in compose and '- "999"' not in compose)
for var in ("CONSTRUCTICON_URL", "CONSTRUCTICON_TEST_URL", "DOCKER_SOCKET_GID", "BACKUP_HOST_DIR"):
    check(f"{var} documented in the header", f"#   {var}=" in compose)
check("web mounts the secrets folder read-only", "- ./secrets:/run/secrets:ro" in compose)
check("no single-file mount of the optional restart secret on web",
      "- ./secrets/ollama_restart_token:/run/secrets/ollama_restart_token:ro" not in compose)
check("single-file secret mounts refuse a missing file", compose.count("bind:\n          create_host_path: false") == 2)
check("backups mounted + CONSTRUCTICON_BACKUP_DIR set", "${BACKUP_HOST_DIR:-./backups}:/app/backups" in compose
      and "CONSTRUCTICON_BACKUP_DIR=/app/backups" in compose)
gi = (ROOT / ".gitignore").read_text(encoding="utf-8").split()
for pat in ("/db/", "/backups/", "/import/"):
    check(f".gitignore has {pat}", pat in gi)

# ---- #740 / #741 deploy.sh --------------------------------------------------------------------------
print("--- #740 / #741 deploy.sh ---")
deploy_text = (ROOT / "scripts" / "deploy.sh").read_text(encoding="utf-8")
check("deploy.sh documents --image", "--image REF" in deploy_text and 'IMAGE_REF="${1:-}"' in deploy_text)
check("deploy.sh builds sidecars", "build_sidecars" in deploy_text and "BUILD_SVCS" in deploy_text)
bash = shutil.which("bash")
if not (bash and posix):
    print("SKIP the deploy.sh runs (need bash on a POSIX host)")
else:
    work = Path(TMP) / "deploytest"
    (work / "inst" / "scripts").mkdir(parents=True)
    (work / "bin").mkdir()
    shutil.copy(ROOT / "scripts" / "deploy.sh", work / "inst" / "scripts" / "deploy.sh")
    fake = work / "bin" / "docker"
    fake.write_text("""#!/bin/sh
# fake docker for test_plumbing_batch1.py
case "$1 $2" in
  "info "*) exit 0 ;;
  "compose version") echo "Docker Compose version v2.99.0"; exit 0 ;;
  "compose config")
    case "$FAKE_MODE" in
      fail) echo "yaml: line 3: mapping values are not allowed in this context" >&2; exit 15 ;;
      garbage) echo "this is not json"; exit 0 ;;
      noweb) echo '{"services": {"db": {}, "cache": {"build": {"context": "."}}}}'; exit 0 ;;
      good) echo '{"services": {"web": {"image": "constructicon:latest", "build": {"context": "."}, "environment": {"CONSTRUCTICON_ENV": "dev"}, "volumes": []}, "mcp": {"image": "constructicon:latest"}, "restarter": {"build": {"context": "./ops/ollama-restarter"}}}}'; exit 0 ;;
    esac ;;
esac
echo "fake docker: unexpected call: $*" >&2
exit 99
""")
    os.chmod(fake, 0o755)
    env = {**os.environ, "PATH": f"{work / 'bin'}:{os.environ.get('PATH', '/usr/bin:/bin')}"}
    env.pop("COMPOSE_FILE", None)

    def run(mode, *args):
        r = subprocess.run([bash, str(work / "inst" / "scripts" / "deploy.sh"), *args], cwd="/",
                           env={**env, "FAKE_MODE": mode}, capture_output=True, text=True, timeout=60)
        return r.returncode, r.stdout + r.stderr

    rc, out = run("good", "--backup-plan")
    check("wrong folder (no compose file): exits non-zero", rc != 0, out)
    check("wrong folder: says no docker-compose.yml, where, and how to run it",
          "no docker-compose.yml in" in out and "./scripts/deploy.sh" in out and "Nothing was changed" in out, out)
    (work / "inst" / "docker-compose.yml").write_text("services: {}\n")
    for mode, needle in (("fail", "mapping values are not allowed"), ("garbage", "not JSON"),
                         ("noweb", "Services found: cache, db")):
        rc, out = run(mode, "--backup-plan")
        check(f"{mode}: exits non-zero with a plain message ({needle!r})", rc != 0 and needle in out, out)
        check(f"{mode}: no Python traceback, no unbound variable",
              "Traceback" not in out and "unbound variable" not in out, out)
    rc, out = run("good", "--backup-plan")
    check("good config: backup plan runs", rc == 0 and "Pre-deploy backup plan: skip" in out, out)
    check("good config: names every service with a build: section",
          "Images a deploy builds: web (versioned), restarter" in out, out)
    rc, out = run("good", "--image")
    check("--image with no ref: usage error", rc == 2 and "--image needs an image reference" in out, out)

print()
if FAILS:
    print(f"FAILED: {', '.join(FAILS)}")
else:
    print("ALL PASS")
