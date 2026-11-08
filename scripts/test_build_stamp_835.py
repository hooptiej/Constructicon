#!/usr/bin/env python3
"""Self-contained check for #835, the build stamp. Throwaway DB, no server:

    python scripts/test_build_stamp_835.py

Which build am I looking at? core/version.py already reads core/VERSION.json ({version, commit,
deployed_at}, written by the image build's CONSTRUCTICON_* args or by deploy.sh). #835 adds the
stamp the owner reads: `build` (git short sha) + `built_at`, on GET /api/version and in the page
footer, with a fallback that never crashes: no VERSION.json -> "source" plus the checkout's git
short sha when git answers, else just "source".

Covers: the baked file is read; a plain checkout (no file) says source + the git sha (git faked,
and the real git when this checkout has one); no file and no git degrades to "source"; a corrupt
or blank-commit file never raises; git runs once per cache window, not once per render;
GET /api/version returns version / build / built_at; the footer on more than one page shows
version + build. Exits 1 if any check fails.
"""
import json
import os
import subprocess
import sys
import tempfile
import types
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
import _testenv  # noqa: E402
TMP = _testenv.isolate("buildstamp835-")
os.environ["CAPTION_DISABLED"] = "1"
ROOT = os.path.abspath(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
sys.path.insert(0, ROOT)
try:
    import cairosvg  # noqa: F401
except Exception:  # no libcairo on this machine (e.g. Windows): object types import it
    sys.modules["cairosvg"] = types.ModuleType("cairosvg")

from core import db  # noqa: E402
from core import version as V  # noqa: E402
_testenv.assert_isolated()
# #858: a visible .git makes the checkout's own HEAD win over VERSION.json. These checks are about the
# file and the `git rev-parse` fallback, so they run as if there were no checkout (#858 has its own test).
V.GIT_DIR = Path(TMP) / "no-checkout" / ".git"

FAILS = []


def check(name, cond, extra=""):
    print(("PASS " if cond else "FAIL ") + name + (f"  [{extra}]" if (extra and not cond) else ""))
    if not cond:
        FAILS.append(name)


real_run = subprocess.run
real_version_file = V.VERSION_FILE


class FakeGit:
    """Stands in for subprocess.run: answers `git rev-parse --short HEAD` (or fails), counts calls."""

    def __init__(self, sha=None, raises=None, returncode=0):
        self.sha, self.raises, self.returncode, self.calls = sha, raises, returncode, 0

    def __call__(self, cmd, *a, **kw):
        self.calls += 1
        if self.raises:
            raise self.raises
        return subprocess.CompletedProcess(cmd, self.returncode, stdout=(self.sha or "") + "\n", stderr="")


def with_file(content):
    p = Path(TMP) / "VERSION.json"
    if content is None:
        if p.exists():
            p.unlink()
    else:
        p.write_text(content if isinstance(content, str) else json.dumps(content), encoding="utf-8")
    V.VERSION_FILE = p
    V.reset_git_cache()


def info_with_git(fake):
    subprocess.run = fake
    try:
        return V.get_version_info()
    finally:
        subprocess.run = real_run


# ---- 1. the baked stamp is read ------------------------------------------------------------------
os.environ.pop("CONSTRUCTICON_ENV", None)
with_file({"version": "2026.11.1", "commit": "a1b2c3d", "deployed_at": "2026-11-01T15:04:05Z"})
git = FakeGit(sha="ffffff0")
i = info_with_git(git)
check("baked: version comes from the file", i["version"] == "2026.11.1", i)
check("baked: build is the baked sha", i["build"] == "a1b2c3d", i)
check("baked: built_at is the baked timestamp", i["built_at"] == "2026-11-01T15:04:05Z", i)
check("baked: build_label reads 'build a1b2c3d (2026-11-01)'", i["build_label"] == "build a1b2c3d (2026-11-01)", i)
check("baked: build_origin says VERSION.json", i["build_origin"] == "VERSION.json", i)
check("baked: git is not even asked", git.calls == 0, git.calls)
check("baked: the old fields are unchanged (commit, deployed_at, env)",
      i["commit"] == "a1b2c3d" and i["deployed_at"] == "2026-11-01T15:04:05Z" and i["env"] == "prod", i)
check("baked: get_build_label() is the same label", V.get_build_label() == "build a1b2c3d (2026-11-01)")

with_file({"version": "2026.11.1", "commit": "a1b2c3d"})
i = info_with_git(FakeGit(sha="ffffff0"))
check("baked, no build time: label has the sha and no date, built_at is None",
      i["build_label"] == "build a1b2c3d" and i["built_at"] is None, i)

# ---- 2. THE NO-STAMP-FILE FALLBACK: a plain checkout ----------------------------------------------
with_file(None)
git = FakeGit(sha="9f8e7d6")
i = info_with_git(git)
check("no stamp file + git: build is the git short sha", i["build"] == "9f8e7d6", i)
check("no stamp file + git: label says source + the sha", i["build_label"] == "build source 9f8e7d6", i)
check("no stamp file + git: origin is git, built_at is None", i["build_origin"] == "git" and i["built_at"] is None, i)
check("no stamp file + git: version is 'dev' (as before), not blank", i["version"] == "dev", i)
check("no stamp file + git: asked git exactly once", git.calls == 1, git.calls)

# the cache: a page render must not spawn git every time
for _ in range(5):
    info_with_git(git)
check("git is cached: five more renders did not run git again", git.calls == 1, git.calls)
V.reset_git_cache()
info_with_git(git)
check("...and reset_git_cache() makes it ask again", git.calls == 2, git.calls)

# the real git, when this checkout has one (a clean archive or the image has none: skipped, said so)
with_file(None)
if (Path(ROOT) / ".git").exists():
    try:
        want = real_run(["git", "rev-parse", "--short", "HEAD"], cwd=ROOT, capture_output=True, text=True,
                        timeout=5).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        want = ""
    if want:
        i = V.get_version_info()
        check("no stamp file + the REAL git: build is this checkout's HEAD", i["build"] == want, (i, want))
    else:
        print("SKIP real git: git is not runnable here")
else:
    print("SKIP real git: this tree has no .git")

# ---- 3. no file AND no git: degrade, never crash -------------------------------------------------
for label, fake in (("git not installed (OSError)", FakeGit(raises=FileNotFoundError("git"))),
                    ("git times out", FakeGit(raises=subprocess.TimeoutExpired("git", 2))),
                    ("git exits non-zero (not a checkout)", FakeGit(returncode=128)),
                    ("git prints nothing", FakeGit(sha=""))):
    with_file(None)
    try:
        i = info_with_git(fake)
        ok = True
    except Exception as e:  # the whole point: nothing may raise
        i, ok = {"err": repr(e)}, False
    check(f"neither file nor git, {label}: no exception", ok, i)
    check(f"neither file nor git, {label}: build is 'source', label 'build source'",
          ok and i["build"] == "source" and i["build_label"] == "build source" and i["build_origin"] == "source", i)
    check(f"neither file nor git, {label}: version still not blank", ok and bool(i["version"]), i)

# ---- 4. a damaged file falls back too ------------------------------------------------------------
with_file("{corrupt")
try:
    i = info_with_git(FakeGit(sha="1234abc"))
    ok = True
except Exception as e:
    i, ok = {"err": repr(e)}, False
check("corrupt VERSION.json: no exception, falls back to source + git", ok and i["build_label"] == "build source 1234abc", i)
with_file({"version": "2026.11.1", "commit": "  ", "deployed_at": "not a date"})
i = info_with_git(FakeGit(sha="1234abc"))
check("blank commit in the file counts as no stamp (falls back to git)", i["build_label"] == "build source 1234abc", i)
check("a deployed_at that isn't a date still reports as built_at but adds no date to the label",
      i["built_at"] == "not a date" and "(" not in i["build_label"], i)

# ---- 5. web: /api/version and the footer ---------------------------------------------------------
db.init_db()
from web import app as webapp  # noqa: E402

client = _testenv.client(webapp.app)

with_file({"version": "2026.11.1", "commit": "a1b2c3d", "deployed_at": "2026-11-01T15:04:05Z"})
r = client.get("/api/version")
body = r.json() if r.status_code == 200 else {}
check("GET /api/version answers 200", r.status_code == 200, f"{r.status_code} {r.text[:150]}")
check("...with version", body.get("version") == "2026.11.1", body)
check("...with build", body.get("build") == "a1b2c3d", body)
check("...with built_at", body.get("built_at") == "2026-11-01T15:04:05Z", body)
check("...and commit / deployed_at / env kept", all(k in body for k in ("commit", "deployed_at", "env")), body)

for path in ("/", "/hobbies", "/admin"):
    r = client.get(path)
    check(f"{path}: 200", r.status_code == 200, f"{r.status_code}")
    check(f"{path}: footer shows version + build ('v2026.11.1 - build a1b2c3d (2026-11-01)')",
          'class="app-version"' in r.text and "v2026.11.1 - build a1b2c3d (2026-11-01)" in r.text,
          r.text[r.text.find("app-version") - 5:r.text.find("app-version") + 200] if "app-version" in r.text else "no app-version div")

# the plain-checkout footer, through the page
with_file(None)
subprocess.run = FakeGit(sha="9f8e7d6")
try:
    r = client.get("/")
finally:
    subprocess.run = real_run
check("plain checkout: the page still renders", r.status_code == 200, r.status_code)
check("plain checkout: footer says 'dev - build source 9f8e7d6'", "dev - build source 9f8e7d6" in r.text)

# and with no git either: the page must not blank or 500
with_file(None)
subprocess.run = FakeGit(raises=FileNotFoundError("git"))
try:
    r = client.get("/")
finally:
    subprocess.run = real_run
check("no stamp and no git: the page still renders", r.status_code == 200, r.status_code)
check("no stamp and no git: footer says 'dev - build source'", "dev - build source<" in r.text)

# the dev tab-title prefix is untouched (#310): it is a separate global
from web import common  # noqa: E402
check("the DEV- tab-title global (is_dev) is still there, separate from the stamp",
      "is_dev" in common.templates.env.globals and "app_build" in common.templates.env.globals)

V.VERSION_FILE = real_version_file
print()
print(f"{len(FAILS)} failed" if FAILS else "ALL PASS")
sys.exit(1 if FAILS else 0)
