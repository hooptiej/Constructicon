"""Thin client for the Constructicon upload API: the same POST /api/upload the web drawer and the
MCP server use.

Auth (Constructicon #467 step 2): the server refuses anonymous uploads, so every request carries
the install token as `Authorization: Bearer <token>` (Settings > "Set Install Token..."; the admin
gets it from the server's token file). Without it, or with a wrong one, the server answers 401 and
upload_file raises AuthError with a message saying so.
"""

import os

import requests

REQUEST_TIMEOUT_SECONDS = 30

# Identifies every request from this app (both the silent Desktop-folder watcher and the in-app
# drop zone, see watcher.py/dropzone.py) as an automated/unattended upload, as opposed to a
# deliberate one-off drag-drop through the web UI's own upload drawer. The server uses it ONLY to
# pick the Source label for capture_events.tech (core/db.py's source_automated_upload()); since
# #467 step 2 it grants no access at all, the install token does.
CLIENT_IDENTITY_HEADERS = {"X-Constructicon-Client": "desktop-app"}

AUTH_HELP = "The server needs the install token: set it in the menu, Set Install Token..."


class UploadError(Exception):
    """Carries the server's actual error detail, not a generic failure."""


class DuplicateUploadError(UploadError):
    """The server recognized this exact file (name + size + mtime) as
    already uploaded — not a failure, just nothing new to do."""


class AuthError(UploadError):
    """401: no install token configured, or the wrong one."""


def request_headers(token=""):
    """The headers every request sends: the client identity, plus the bearer token when set."""
    headers = dict(CLIENT_IDENTITY_HEADERS)
    token = (token or "").strip()
    if token:
        headers["Authorization"] = f"Bearer {token}"
    return headers


def upload_file(base_url, path, description="", tags=None, client="", token=""):
    url = base_url.rstrip("/") + "/api/upload"
    data = {
        "description": description,
        "tags": _tags_json(tags or []),
        "client": client,
        "modified_at": str(int(os.path.getmtime(path) * 1000)),
    }
    with open(path, "rb") as f:
        files = {"file": (os.path.basename(path), f)}
        try:
            resp = requests.post(
                url, data=data, files=files, headers=request_headers(token), timeout=REQUEST_TIMEOUT_SECONDS,
                allow_redirects=False,
            )
        except requests.RequestException as e:
            raise UploadError(f"Couldn't reach {base_url}: {e}")
    if resp.status_code == 401:
        detail = _error_detail(resp)
        if (token or "").strip():
            raise AuthError(f"The install token was refused ({detail}). {AUTH_HELP}")
        raise AuthError(AUTH_HELP)
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
        body = resp.json()
    except ValueError:
        return f"HTTP {resp.status_code}"
    err = body.get("error") if isinstance(body, dict) else None
    if isinstance(err, dict) and err.get("message"):
        return err["message"]
    return body.get("detail", f"HTTP {resp.status_code}") if isinstance(body, dict) else f"HTTP {resp.status_code}"
