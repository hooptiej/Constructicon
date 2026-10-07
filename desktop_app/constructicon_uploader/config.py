"""Settings storage: a plain JSON file in Application Support.

Since Constructicon #467 step 2 the server needs the install token for uploads ("install_token",
sent as `Authorization: Bearer ...`). It is a secret, so the file is written owner-only (0600) and
the token is never shown in full in the UI. (The macOS Keychain would be nicer; a plain owner-only
file keeps the app dependency-free, the same trade the server makes with its token file.)
"""

import json
import os
from pathlib import Path

APP_SUPPORT_DIR = Path.home() / "Library" / "Application Support" / "Constructicon Uploader"
CONFIG_PATH = APP_SUPPORT_DIR / "config.json"

DEFAULTS = {
    "base_url": "http://10.12.5.98:8000",
    "watch_folder": str(Path.home() / "Desktop"),
    "install_token": "",
}


def load_config():
    if not CONFIG_PATH.exists():
        return dict(DEFAULTS)
    try:
        stored = json.loads(CONFIG_PATH.read_text())
    except (json.JSONDecodeError, OSError):
        return dict(DEFAULTS)
    return {**DEFAULTS, **stored}


def save_config(config):
    APP_SUPPORT_DIR.mkdir(parents=True, exist_ok=True)
    fd = os.open(CONFIG_PATH, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        f.write(json.dumps(config, indent=2))
    os.chmod(CONFIG_PATH, 0o600)  # a file from an older version may have been world-readable
