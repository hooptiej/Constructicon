"""Thin client for the imagerepo upload API — the same POST /api/upload the
web drawer and the MCP server use, authenticated with a Bearer API token
instead of a browser session.
"""

import os

import requests

REQUEST_TIMEOUT_SECONDS = 30


class UploadError(Exception):
    """Carries the server's actual error detail, not a generic failure."""


class DuplicateUploadError(UploadError):
    """The server recognized this exact file (name + size + mtime) as
    already uploaded — not a failure, just nothing new to do."""


def upload_file(base_url, token, path, description="", tags=None, ticket_id="", client=""):
    if not token:
        raise UploadError("No API token configured — set one from imagerepo account settings")
    url = base_url.rstrip("/") + "/api/upload"
    headers = {"Authorization": f"Bearer {token}"}
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
            resp = requests.post(url, headers=headers, data=data, files=files, timeout=REQUEST_TIMEOUT_SECONDS)
        except requests.RequestException as e:
            raise UploadError(f"Couldn't reach {base_url}: {e}")
    if resp.status_code == 409:
        detail = _error_detail(resp)
        raise DuplicateUploadError(detail)
    if resp.status_code == 401:
        raise UploadError("Token rejected — it may have been revoked. Set a new one from imagerepo account settings.")
    if not resp.ok:
        raise UploadError(_error_detail(resp))
    return resp.json()


def validate_token(base_url, token):
    """True if this token is currently accepted — used right after a tech
    pastes one in, so a typo or an already-revoked token is caught
    immediately instead of surfacing as a mystery failure on the next
    screenshot."""
    if not token:
        return False
    url = base_url.rstrip("/") + "/api/account/tokens"
    try:
        resp = requests.get(url, headers={"Authorization": f"Bearer {token}"}, timeout=REQUEST_TIMEOUT_SECONDS)
    except requests.RequestException:
        return False
    return resp.ok


def _tags_json(tags):
    import json
    return json.dumps(tags)


def _error_detail(resp):
    try:
        return resp.json().get("detail", f"HTTP {resp.status_code}")
    except ValueError:
        return f"HTTP {resp.status_code}"
