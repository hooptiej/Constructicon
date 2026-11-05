"""Build version (#508, #688).

core/VERSION.json ({version, commit, deployed_at}) comes from one of two places:
- the image build (#688): the Dockerfile writes it into the baked /app/core from
  the CONSTRUCTICON_VERSION / _COMMIT / _BUILT_AT build args that
  scripts/deploy.sh passes (prod runs that image with no code mounts);
- scripts/deploy.sh on a checkout whose code is bind-mounted over the image
  (constructicon-test, dev): it writes the gitignored file into the checkout's
  core/ after the reset, since the mount hides the baked one.
A tar-over-ssh deploy has no such file, so the app reports "dev". Read fresh on
every call (the file is tiny) so a deploy is picked up even by a process that
wasn't restarted. The "-dev" suffix for CONSTRUCTICON_ENV=dev is added here at
read time, so one image reports correctly wherever it runs.
"""
import json
import logging
import os
import re
import subprocess
import time
from pathlib import Path

from . import besteffort

log = logging.getLogger("constructicon.version")

VERSION_FILE = Path(__file__).resolve().parent / "VERSION.json"
REPO_ROOT = Path(__file__).resolve().parent.parent

# #835: the build stamp. VERSION.json already carries it ("commit" = the git short sha, "deployed_at"
# = when the image was built, or when deploy.sh wrote the file on a mounted instance), so there is
# no second stamp file. When there is no VERSION.json (a plain checkout, a tar-deployed branch, an
# image built without the version args) the stamp is "source" plus the git short sha if git answers.
BUILD_SOURCE = "source"
_GIT_CACHE_SECONDS = 60.0  # a page render must not spawn git every time; a `git pull` shows within a minute
_git_cache: dict = {}  # {"at": monotonic seconds, "sha": str | None}
_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}")


def reset_git_cache() -> None:
    """Forget the cached git sha (tests)."""
    _git_cache.clear()


def _git_short_sha(root: Path | None = None) -> str | None:
    """The checkout's `git rev-parse --short HEAD`, or None when git can't say (not installed, not
    a checkout, slow). Never raises and never warns: no git is the normal case inside the image."""
    root = root or REPO_ROOT
    now = time.monotonic()
    cached = _git_cache.get("at")
    if cached is not None and _git_cache.get("root") == str(root) and now - cached < _GIT_CACHE_SECONDS:
        return _git_cache.get("sha")
    sha = None
    try:
        done = subprocess.run(["git", "rev-parse", "--short", "HEAD"], cwd=str(root), capture_output=True,
                              text=True, timeout=2, check=False)
        if done.returncode == 0 and done.stdout.strip():
            sha = done.stdout.strip()
        else:
            log.info("build stamp: git gave no sha in %s (exit %s); the stamp says source", root, done.returncode)
    except (OSError, subprocess.SubprocessError) as e:  # silent-ok: no git here is normal (the image has none); logged at info, the stamp says source
        log.info("build stamp: git could not run (%s); the stamp says source", e)
    _git_cache.update({"at": now, "root": str(root), "sha": sha})
    return sha


def _build_stamp(commit: str | None, built_at: str | None) -> dict:
    """{build, build_origin, build_label} for the footer and /api/version. Never raises."""
    date = built_at[:10] if built_at and _DATE_RE.match(built_at) else None
    if commit:
        label = f"build {commit}" + (f" ({date})" if date else "")
        return {"build": commit, "build_origin": "VERSION.json", "build_label": label}
    sha = _git_short_sha()
    if sha:
        return {"build": sha, "build_origin": "git", "build_label": f"build {BUILD_SOURCE} {sha}"}
    return {"build": BUILD_SOURCE, "build_origin": BUILD_SOURCE, "build_label": f"build {BUILD_SOURCE}"}


def env_name() -> str:
    return "dev" if os.getenv("CONSTRUCTICON_ENV", "prod").strip().lower() == "dev" else "prod"


def get_version_info() -> dict:
    """{version, commit, deployed_at, env, build, built_at, build_origin, build_label}. Never raises."""
    data = {}
    try:
        loaded = json.loads(VERSION_FILE.read_text(encoding="utf-8"))
        if isinstance(loaded, dict):
            data = loaded
    except FileNotFoundError:  # silent-ok: no VERSION.json = a dev checkout, reported as "dev"
        pass
    except (OSError, ValueError) as e:
        besteffort.warn(log, "version: VERSION.json exists but can't be read", e, path=str(VERSION_FILE))
    env = env_name()
    version = str(data.get("version") or "").strip()
    if not version:
        version = "dev"  # env is reported separately in the "env" field
    elif env == "dev" and not version.endswith("-dev"):
        version += "-dev"  # older deploy.sh runs wrote the suffix into the file already
    commit = str(data.get("commit") or "").strip() or None
    deployed_at = str(data.get("deployed_at") or "").strip() or None
    stamp = _build_stamp(commit, deployed_at)
    return {
        "version": version,
        "commit": commit,
        "deployed_at": deployed_at,
        "env": env,
        # #835: the build stamp. build = the baked git short sha (or the checkout's, or "source");
        # built_at = when the image was built / deploy.sh wrote VERSION.json (same value as deployed_at).
        "build": stamp["build"],
        "built_at": deployed_at,
        "build_origin": stamp["build_origin"],
        "build_label": stamp["build_label"],
    }


def get_build_label() -> str:
    """#835: "build a1b2c3d (2026-11-01)", "build source a1b2c3d" or "build source", for the page footer."""
    return get_version_info()["build_label"]


def get_version() -> str:
    return get_version_info()["version"]
