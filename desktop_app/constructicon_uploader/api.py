"""Thin client for the Constructicon upload API — the same POST /api/upload the
web drawer and the MCP server use. No auth: Constructicon runs on a LAN-only
dev server with no port forward, so anyone who can reach it can upload.
"""

import os

import requests

REQUEST_TIMEOUT_SECONDS = 30

# Identifies every request from this app (both the silent Desktop-folder
# watcher and the in-app drop zone — see watcher.py/dropzone.py) as an
# automated/unattended upload, as opposed to a deliberate one-off drag-drop
# through the web UI's own upload drawer. The server (web/app.py's
# api_upload) uses this to pick the right Source string for capture_events.tech
# — see core/db.py's SOURCE_AUTOMATED_UPLOAD/SOURCE_MANUAL_UPLOAD.
CLIENT_IDENTITY_HEADERS = {"X-Constructicon-Client": "desktop-app"}


class UploadError(Exception):
    """Carries the server's actual error detail, not a generic failure."""


class DuplicateUploadError(UploadError):
    """The server recognized this exact file (name + size + mtime) as
    already uploaded — not a failure, just nothing new to do."""


def upload_file(base_url, path, description="", tags=None, ticket_id="", client=""):
    url = base_url.rstrip("/") + "/api/upload"
    data = {
        "description": description,
        "tags": _tags_json(tags or []),
        "ticket_id": ticket_id,
        "client": client,
        "modified_at": str(int(os.path.getmtime(path) * 1000)),
    }
    with open(path, "rb") as f:
        files = {"file": (os.path.basename(path), f)}
        try:
            resp = requests.post(
                url, data=data, files=files, headers=CLIENT_IDENTITY_HEADERS, timeout=REQUEST_TIMEOUT_SECONDS
            )
        except requests.RequestException as e:
            raise UploadError(f"Couldn't reach {base_url}: {e}")
    if resp.status_code == 409:
        detail = _error_detail(resp)
        raise DuplicateUploadError(detail)
    if not resp.ok:
        raise UploadError(_error_detail(resp))
    return resp.json()


def _tags_json(tags):
    import json
    return json.dumps(tags)


def _error_detail(resp):
    try:
        return resp.json().get("detail", f"HTTP {resp.status_code}")
    except ValueError:
        return f"HTTP {resp.status_code}"
