"""Build version (#508).

scripts/deploy.sh writes core/VERSION.json (gitignored, inside the
bind-mounted core/ dir) after it resets the checkout. A tar-over-ssh deploy
has no such file, so the app reports "dev". Read fresh on every call (the file
is tiny) so a deploy is picked up even by a process that wasn't restarted.
"""
import json
import os
from pathlib import Path

VERSION_FILE = Path(__file__).resolve().parent / "VERSION.json"


def env_name() -> str:
    return "dev" if os.getenv("CONSTRUCTICON_ENV", "prod").strip().lower() == "dev" else "prod"


def get_version_info() -> dict:
    """{version, commit, deployed_at, env}. Never raises."""
    data = {}
    try:
        loaded = json.loads(VERSION_FILE.read_text(encoding="utf-8"))
        if isinstance(loaded, dict):
            data = loaded
    except (OSError, ValueError):
        pass
    env = env_name()
    version = str(data.get("version") or "").strip()
    if not version:
        version = "dev"  # env is reported separately in the "env" field
    return {
        "version": version,
        "commit": data.get("commit") or None,
        "deployed_at": data.get("deployed_at") or None,
        "env": env,
    }


def get_version() -> str:
    return get_version_info()["version"]
