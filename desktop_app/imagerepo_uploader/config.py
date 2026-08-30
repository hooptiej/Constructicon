"""Settings + API token storage.

Non-secret settings (base URL, watched folder) live in a plain JSON file —
there's nothing there worth protecting. The API token is different: it's a
bearer credential that acts as this tech in imagerepo, so it goes in the
macOS Keychain via the `security` CLI, never in a config file on disk.
"""

import json
import subprocess
from pathlib import Path

APP_SUPPORT_DIR = Path.home() / "Library" / "Application Support" / "ImageRepo Uploader"
CONFIG_PATH = APP_SUPPORT_DIR / "config.json"

KEYCHAIN_SERVICE = "ImageRepo Uploader"
KEYCHAIN_ACCOUNT = "api-token"

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


def get_token():
    result = subprocess.run(
        ["security", "find-generic-password", "-s", KEYCHAIN_SERVICE, "-a", KEYCHAIN_ACCOUNT, "-w"],
        capture_output=True, text=True,
    )
    if result.returncode != 0:
        return None
    return result.stdout.strip() or None


def set_token(raw_token):
    # -U updates the existing item in place instead of erroring on a
    # duplicate — a tech pasting a fresh token after revoking the old one
    # shouldn't have to know to delete it first.
    subprocess.run(
        ["security", "add-generic-password", "-U", "-s", KEYCHAIN_SERVICE, "-a", KEYCHAIN_ACCOUNT, "-w", raw_token],
        capture_output=True, text=True, check=True,
    )


def clear_token():
    subprocess.run(
        ["security", "delete-generic-password", "-s", KEYCHAIN_SERVICE, "-a", KEYCHAIN_ACCOUNT],
        capture_output=True, text=True,
    )
