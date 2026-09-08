"""Issue #200: pull the owner's public Imgur gallery submissions into
Constructicon. Client-ID auth only — no OAuth — so this deliberately only
ever sees content actually posted to the public gallery
(account/{username}/submissions), never private/ungallery'd uploads (that
would need account/{username}/images, which requires a real OAuth2 user
token). Full-account OAuth import is an explicit later follow-up, not this.

Triggered synchronously from web/app.py's POST /api/imgur/import (the
upload drawer's "Import from Imgur" button) — small enough personal
galleries that a single request/response round trip is fine, no background
job needed.
"""

import json
import urllib.error
import urllib.request

from core import db, storage
from core.object_types.imgur import extract_imgur_id

IMGUR_API_BASE = "https://api.imgur.com/3"


class ImgurImportError(Exception):
    """Raised for anything that should surface as a plain error message in
    the upload drawer — missing settings, an Imgur API failure, network
    error. Never a raw exception traceback."""


def _fetch_page(client_id, username, page):
    url = f"{IMGUR_API_BASE}/account/{username}/submissions/{page}"
    req = urllib.request.Request(url, headers={"Authorization": f"Client-ID {client_id}"})
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            body = json.loads(resp.read())
    except urllib.error.HTTPError as e:
        detail = e.read().decode("utf-8", "ignore")[:200]
        raise ImgurImportError(f"Imgur API error {e.code} for {username!r}: {detail}")
    except urllib.error.URLError as e:
        raise ImgurImportError(f"Couldn't reach Imgur: {e.reason}")
    if not body.get("success"):
        raise ImgurImportError(f"Imgur API reported failure: {body}")
    return body.get("data") or []


def fetch_all_submissions(client_id, username, max_pages=50):
    """Paginate account/{username}/submissions/{page} (0-indexed, ~50 items
    per page per Imgur's docs) until an empty page comes back. max_pages is
    a hard safety cap against a runaway loop — not expected to ever bind
    for a personal account."""
    items = []
    for page in range(max_pages):
        batch = _fetch_page(client_id, username, page)
        if not batch:
            break
        items.extend(batch)
    return items


def _normalize(item):
    """One submission (single image or album) -> the fields
    core/object_types/imgur.py's spec + db.insert_content need. Album
    handling: represented by its first/cover image for
    external_url/thumbnail purposes — the album's own page link and image
    count go in type_metadata instead. Returns None for a malformed item
    with no usable image link at all (best-effort — skip, don't crash the
    whole import over one bad entry)."""
    images = item.get("images") or []
    is_album = bool(images)
    if is_album:
        cover_link = images[0].get("link")
        permalink = f"https://imgur.com/a/{item.get('id')}"
        image_count = len(images)
    else:
        cover_link = item.get("link")
        permalink = cover_link
        image_count = 1
    if not cover_link:
        return None
    return {
        "external_url": cover_link,
        "title": item.get("title") or "",
        "description": item.get("description") or "",
        "datetime": item.get("datetime"),
        "type_metadata": {
            "is_album": is_album,
            "image_count": image_count,
            "permalink": permalink,
        },
    }


def sync_public_gallery():
    """The whole button-triggered flow: read the stored Client ID/username,
    fetch every public gallery submission, skip anything already imported
    (matched by Imgur image id extracted from external_url — same
    discipline as the YouTube importer's video-id matching), create a row
    for everything else. Returns a summary dict for the upload drawer to
    show."""
    client_id = db.get_setting("imgur_client_id")
    username = db.get_setting("imgur_username")
    if not client_id or not username:
        raise ImgurImportError("Set an Imgur Client ID and Username in the admin pane first.")

    existing_ids = {extract_imgur_id(url) for url in db.list_external_urls_by_media_type("imgur")}
    existing_ids.discard(None)

    submissions = fetch_all_submissions(client_id, username)
    imported_slugs = []
    skipped = 0
    for item in submissions:
        norm = _normalize(item)
        if norm is None:
            continue
        imgur_id = extract_imgur_id(norm["external_url"])
        if imgur_id and imgur_id in existing_ids:
            skipped += 1
            continue
        slug = storage.make_slug()
        db.insert_content(
            slug, db.SOURCE_AUTOMATED_UPLOAD, "imgur",
            external_url=norm["external_url"],
            content_description=norm["title"] or None,
            content_date=float(norm["datetime"]) if norm["datetime"] else None,
            description=norm["description"],
            type_metadata=norm["type_metadata"],
        )
        imported_slugs.append(slug)
        if imgur_id:
            existing_ids.add(imgur_id)

    return {
        "found": len(submissions),
        "imported": len(imported_slugs),
        "skipped": skipped,
        "slugs": imported_slugs,
    }
