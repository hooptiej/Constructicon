#!/usr/bin/env python3
"""Self-contained check for #858, "the footer fibs on test". Throwaway DB, no server:

    python scripts/test_footer_truth_858.py

On a mounted checkout (constructicon-test) core/VERSION.json is written only by deploy.sh, so a branch
checked out or copied in afterwards ran under the OLD stamp. When the checkout's .git is visible (the
test override mounts it read-only), core/version.py now asks `git status --porcelain=v2 --branch` and
the checkout wins: "build source <sha> (branch <name>)", "+ local changes" when a tracked file under
core/ web/ mcp_server/ assets/ differs from HEAD. A matching, clean checkout keeps the file's label
(with its date). No .git (prod) or a git that fails = the #835 behaviour, never an exception.

Covers: no .git -> git is never asked; a stale file vs the checkout's HEAD; a matching clean checkout;
local changes (also when the sha matches); a detached HEAD; no VERSION.json at all; git failing,
timing out or printing nothing; the cache; the real git against this checkout when it has one;
the footer and GET /api/version through the app. Exits 1 if any check fails.
"""
import json
import os
import subprocess
import sys
import types
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
import _testenv  # noqa: E402
TMP = _testenv.isolate("footertruth858-")
os.environ["CAPTION_DISABLED"] = "1"
os.environ.pop("CONSTRUCTICON_ENV", None)
ROOT = os.path.abspath(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
sys.path.insert(0, ROOT)
try:
    import cairosvg  # noqa: F401
except Exception:  # no libcairo on this machine (e.g. Windows): object types import it
    sys.modules["cairosvg"] = types.ModuleType("cairosvg")

from core import db  # noqa: E402
from core import version as V  # noqa: E402
_testenv.assert_isolated()

FAILS = []


def check(name, cond, extra=""):
    print(("PASS " if cond else "FAIL ") + name + (f"  [{extra}]" if (extra and not cond) else ""))
    if not cond:
        FAILS.append(name)


real_run = subprocess.run
real_version_file, real_git_dir = V.VERSION_FILE, V.GIT_DIR
FAKE_GIT_DIR = Path(TMP) / "checkout" / ".git"
FAKE_GIT_DIR.mkdir(parents=True)
FULL = "20bd5ad4f00dfeed0123456789abcdef01234567"


def porcelain(oid=FULL, head="test/c-lane1", changed=()):
    lines = [f"# branch.oid {oid}", f"# branch.head {head}", "# branch.upstream origin/" + head]
    lines += [f"1 .M N... 100644 100644 100644 aaa bbb {p}" for p in changed]
    return "\n".join(lines) + "\n"


class FakeGit:
    """Stands in for subprocess.run: answers `git status` (or fails) and counts calls."""

    def __init__(self, out="", raises=None, returncode=0):
        self.out, self.raises, self.returncode, self.calls, self.cmds = out, raises, returncode, 0, []

    def __call__(self, cmd, *a, **kw):
        self.calls += 1
        self.cmds.append(cmd)
        if self.raises:
            raise self.raises
        return subprocess.CompletedProcess(cmd, self.returncode, stdout=self.out, stderr="")


def setup(version_file, git_dir=FAKE_GIT_DIR):
    p = Path(TMP) / "VERSION.json"
    if version_file is None:
        if p.exists():
            p.unlink()
    else:
        p.write_text(json.dumps(version_file), encoding="utf-8")
    V.VERSION_FILE, V.GIT_DIR = p, git_dir
    V.reset_git_cache()


def info(fake):
    subprocess.run = fake
    try:
        return V.get_version_info()
    finally:
        subprocess.run = real_run


STALE = {"version": "2026.10.30.3-18-gc5ecced", "commit": "c5ecced", "deployed_at": "2026-11-07T18:04:40Z"}

# ---- 1. prod: no .git, so git is never asked and the baked stamp stands ------------------------------
setup(STALE, git_dir=Path(TMP) / "nowhere" / ".git")
g = FakeGit(out=porcelain())
i = info(g)
check("no .git: the baked label stands", i["build_label"] == "build c5ecced (2026-11-07)", i)
check("no .git: git is not run at all", g.calls == 0, g.calls)
check("no .git: build_branch / build_local_changes are None", i["build_branch"] is None and i["build_local_changes"] is None, i)

# ---- 2. THE BUG: the file says c5ecced, the checkout runs 20bd5ad on test/c-lane1 ----------------------
setup(STALE)
g = FakeGit(out=porcelain())
i = info(g)
check("stale file: build is the checkout's sha", i["build"] == "20bd5ad", i)
check("stale file: label names the sha and the branch",
      i["build_label"] == "build source 20bd5ad (branch test/c-lane1)", i)
check("stale file: origin is checkout, branch reported, no local changes",
      i["build_origin"] == "checkout" and i["build_branch"] == "test/c-lane1" and i["build_local_changes"] is False, i)
check("stale file: the version string still comes from the file", i["version"] == "2026.10.30.3-18-gc5ecced", i)
cmd = g.cmds[0] if g.cmds else []
check("git status is read-only and scoped to the mounted code",
      "--no-optional-locks" in cmd and "status" in cmd and "--porcelain=v2" in cmd
      and all(p in cmd for p in V.CHECKOUT_PATHS), cmd)
check("git trusts the mounted .git whatever its owner (safe.directory)", "safe.directory=*" in cmd, cmd)

# ---- 3. a clean checkout that matches the file keeps the file's label (and its date) -----------------
setup({"version": "2026.11.8", "commit": "20bd5ad", "deployed_at": "2026-11-08T15:00:00Z"})
i = info(FakeGit(out=porcelain(head="main")))
check("matching clean checkout: the file's label, with its date", i["build_label"] == "build 20bd5ad (2026-11-08)", i)
check("matching clean checkout: origin VERSION.json, branch main, no local changes",
      i["build_origin"] == "VERSION.json" and i["build_branch"] == "main" and i["build_local_changes"] is False, i)
setup({"version": "2026.11.8", "commit": "20bd5ad4f", "deployed_at": "2026-11-08T15:00:00Z"})
i = info(FakeGit(out=porcelain(head="main")))
check("a longer sha in the file still matches the checkout's short one", i["build_origin"] == "VERSION.json", i)

# ---- 4. local changes: a copy-in deploy over a clean checkout -----------------------------------------
setup({"version": "2026.11.8", "commit": "20bd5ad", "deployed_at": "2026-11-08T15:00:00Z"})
i = info(FakeGit(out=porcelain(head="main", changed=["web/templates/_nav_tabs.html"])))
check("same sha + modified files: says so", i["build_label"] == "build source 20bd5ad + local changes (branch main)", i)
check("same sha + modified files: build_local_changes True", i["build_local_changes"] is True and i["build_origin"] == "checkout", i)

# ---- 5. detached HEAD, and no VERSION.json at all -------------------------------------------------------
setup(STALE)
i = info(FakeGit(out=porcelain(head="(detached)")))
check("detached HEAD: label says so, branch None",
      i["build_label"] == "build source 20bd5ad (detached HEAD)" and i["build_branch"] is None, i)
setup(None)
i = info(FakeGit(out=porcelain(head="main")))
check("no VERSION.json + a checkout: version dev, label from the checkout",
      i["version"] == "dev" and i["build_label"] == "build source 20bd5ad (branch main)", i)

# ---- 6. git can't answer: fall back to the #835 behaviour, never raise --------------------------------
for label, fake in (("git not installed", FakeGit(raises=FileNotFoundError("git"))),
                    ("git times out", FakeGit(raises=subprocess.TimeoutExpired("git", 5))),
                    ("git exits 128 (dubious ownership, not a repo)", FakeGit(returncode=128)),
                    ("git prints nothing", FakeGit(out="")),
                    ("no commits yet", FakeGit(out="# branch.oid (initial)\n# branch.head main\n"))):
    setup(STALE)
    try:
        i, ok = info(fake), True
    except Exception as e:  # the whole point: nothing may raise
        i, ok = {"err": repr(e)}, False
    check(f"{label}: no exception, the file's label stands", ok and i["build_label"] == "build c5ecced (2026-11-07)", i)

# ---- 7. cached: a render doesn't spawn git every time --------------------------------------------------
setup(STALE)
g = FakeGit(out=porcelain())
for _ in range(6):
    info(g)
check("six renders ran git status once", g.calls == 1, g.calls)
V.reset_git_cache()
info(g)
check("reset_git_cache() forgets the checkout state too", g.calls == 2, g.calls)

# ---- 8. the real git against this checkout, when it has one --------------------------------------------
if (Path(ROOT) / ".git").exists():
    setup(STALE, git_dir=Path(ROOT) / ".git")
    try:
        want = real_run(["git", "rev-parse", "--short=7", "HEAD"], cwd=ROOT, capture_output=True, text=True,
                        timeout=5).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        want = ""
    if want:
        i = V.get_version_info()
        check("real git: build is this checkout's HEAD", i["build"] == want and i["build_origin"] == "checkout", (i, want))
    else:
        print("SKIP real git: git is not runnable here")
else:
    print("SKIP real git: this tree has no .git")

# ---- 9. through the app: GET /api/version and the footer ------------------------------------------------
db.init_db()
from web import app as webapp  # noqa: E402

client = _testenv.client(webapp.app)
setup(STALE)
subprocess.run = FakeGit(out=porcelain())
try:
    r = client.get("/api/version")
    page = client.get("/")
finally:
    subprocess.run = real_run
body = r.json() if r.status_code == 200 else {}
check("GET /api/version: build, build_branch, build_local_changes from the checkout",
      body.get("build") == "20bd5ad" and body.get("build_branch") == "test/c-lane1"
      and body.get("build_local_changes") is False, body)
check("footer: 'build source 20bd5ad (branch test/c-lane1)', not the stale c5ecced",
      page.status_code == 200 and "build source 20bd5ad (branch test/c-lane1)" in page.text
      and "build c5ecced" not in page.text, page.status_code)

V.VERSION_FILE, V.GIT_DIR = real_version_file, real_git_dir
V.reset_git_cache()
print()
print(f"FAILED: {', '.join(FAILS)}" if FAILS else "ALL PASS")
sys.exit(1 if FAILS else 0)
