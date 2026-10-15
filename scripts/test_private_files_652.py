#!/usr/bin/env python3
"""#652: nothing the app writes under its data paths is readable by other users on the host.

Throwaway DB + storage (scripts/_testenv.py), the real FastAPI app through TestClient, the real
core/backup.py and the MCP module import. No server, no network. POSIX permissions, so run it on
Linux (the app image), not a Windows checkout:

    python scripts/test_private_files_652.py

Checks: the web app and the MCP server set the umask (027) when they load; CONSTRUCTICON_UMASK
overrides it and a bad value can't loosen it; the DB and its WAL, an uploaded file, its thumbnail,
the trash and an export build have no "other" bits; a backup zip is 0600 even under umask 000.
"""
import io
import os
import stat
import sys
import types

import _testenv  # noqa: E402
TMP = _testenv.isolate("private652-")
ROOT = os.path.abspath(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
sys.path.insert(0, ROOT)
sys.modules.setdefault("cairosvg", types.ModuleType("cairosvg"))  # no libcairo needed here

FAILS = []


def check(name, cond, extra=""):
    print(("PASS " if cond else "FAIL ") + name + (f"  [{extra}]" if (extra and not cond) else ""))
    if not cond:
        FAILS.append(name)


def current_umask():
    mask = os.umask(0)
    os.umask(mask)
    return mask


def mode(path):
    return stat.S_IMODE(os.stat(path).st_mode)


def private(path):
    """No permission bits for "other", and no group write."""
    return mode(path) & 0o027 == 0


# A loose umask, as a container starts with, before the app loads.
os.umask(0o022)
from core import paths  # noqa: E402

# --- the umask setting itself ---
os.environ.pop("CONSTRUCTICON_UMASK", None)
check("default umask is 027", paths.configured_umask() == 0o027, oct(paths.configured_umask()))
for raw, want in (("022", 0o022), ("077", 0o077), (" 0o27 ", 0o027), ("garbage", 0o027), ("999", 0o027),
                  ("-1", 0o027), ("", 0o027)):
    os.environ["CONSTRUCTICON_UMASK"] = raw
    got = paths.configured_umask()
    check(f"CONSTRUCTICON_UMASK={raw!r} -> {want:03o}", got == want, oct(got))
os.environ.pop("CONSTRUCTICON_UMASK", None)

# --- the web app sets it when it loads ---
from web import app as webapp  # noqa: E402
from core import backup, db  # noqa: E402
_testenv.assert_isolated()
check("importing web.app sets umask 027", current_umask() == 0o027, oct(current_umask()))

# --- the DB, created by the app's startup ---
client = _testenv.client(webapp.app, raise_server_exceptions=False)
with client:
    pass
check("DB file is not world-readable", private(db.DB_PATH), oct(mode(db.DB_PATH)))
wal = str(db.DB_PATH) + "-wal"
if os.path.exists(wal):
    check("DB -wal file is not world-readable", private(wal), oct(mode(wal)))

# --- an upload, its thumbnail, the trash ---
from PIL import Image  # noqa: E402
buf = io.BytesIO()
Image.new("RGB", (64, 48), (90, 120, 40)).save(buf, "PNG")
r = client.post("/api/upload", files={"file": ("olive.png", buf.getvalue(), "image/png")})
check("upload succeeds", r.status_code == 200, (r.status_code, r.text[:200]))
slug = (r.json().get("slug") or (r.json().get("object") or {}).get("slug")) if r.status_code == 200 else None
row = db.get_by_slug(slug) if slug else None
if row:
    stored = paths.storage_dir() / row["stored_filename"]
    check("stored upload is not world-readable", stored.exists() and private(stored), stored.exists() and oct(mode(stored)))
    client.get(f"/f/{slug}/thumb")
    thumb = paths.storage_dir() / f"{slug}_thumb.jpg"
    check("thumbnail exists and is not world-readable", thumb.exists() and private(thumb),
          thumb.exists() and oct(mode(thumb)))
    d = client.post(f"/api/image/{slug}/delete")
    check("delete succeeds (to the trash)", d.status_code == 200, (d.status_code, d.text[:200]))
    trash = paths.trash_dir()
    trashed = [p for p in trash.rglob("*")] if trash.exists() else []
    check("trash dir and what's in it are not world-readable",
          trash.exists() and private(trash) and trashed and all(private(p) for p in trashed),
          [(str(p), oct(mode(p))) for p in [trash, *trashed] if p.exists()])

# --- an export build ---
from core import site_export  # noqa: E402
try:
    out = os.path.join(TMP, "exports", "perm-build")
    site_export.build_site({}, out_dir=out)
    built = list(__import__("pathlib").Path(out).rglob("*"))
    loose = [p for p in built if not private(p)]
    check("export build (dirs, pages, copied assets) is not world-readable",
          os.path.isdir(out) and private(out) and any(p.parent.name == "assets" for p in built) and not loose,
          [(str(p), oct(mode(p))) for p in loose[:5]])
except Exception as exc:  # the export needs nothing special here; report rather than crash
    check("export build runs", False, repr(exc))

# --- a backup zip is 0600 whatever the umask ---
os.umask(0)
try:
    res = backup.create_backup()
finally:
    os.umask(0o027)
check("backup zip is 0600 under umask 000", mode(res["path"]) == 0o600, oct(mode(res["path"])))
check("backup dir is created 0750 under umask 000", mode(paths.backup_dir()) == 0o750, oct(mode(paths.backup_dir())))
check("no .part left behind", not list(paths.backup_dir().glob("*.part")))
res2 = backup.create_backup()  # under the app's own umask too
check("backup zip is 0600 under umask 027", mode(res2["path"]) == 0o600, oct(mode(res2["path"])))

# --- the MCP server sets it when it loads ---
os.umask(0o022)
import mcp_server.server  # noqa: E402,F401
check("importing mcp_server.server sets umask 027", current_umask() == 0o027, oct(current_umask()))

print("\n%s" % ("ALL PASS" if not FAILS else "FAILED: " + ", ".join(FAILS)))
