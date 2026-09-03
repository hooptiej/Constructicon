"""Settings storage — a plain JSON file. Nothing here is a secret: there's no
auth token anymore, so there's nothing that needs the macOS Keychain.
"""

import json
from pathlib import Path

APP_SUPPORT_DIR = Path.home() / "Library" / "Application Support" / "Constructicon Uploader"
CONFIG_PATH = APP_SUPPORT_DIR / "config.json"

DEFAULTS = {
    "base_url": "http://10.12.5.98:8000",
    "watch_folder": str(Path.home() / "Desktop"),
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
    CONFIG_PATH.write_text(json.dumps(config, indent=2))
