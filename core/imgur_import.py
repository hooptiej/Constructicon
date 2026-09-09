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

Issue #203: sync_public_gallery() above is all-or-nothing — every public
submission not already imported comes in on every click, which doesn't
work for an owner who doesn't want every public post touching
Constructicon. import_from_url() below is the narrower alternative: one
pasted Imgur post/album link, one row, giving exact control over what
comes in. Same Client-ID auth, same _normalize() shaping — just a
different fetch (a single image/album lookup instead of the account-wide
submissions list).
"""

import json
import re
import urllib.error
import urllib.request

from core import db, storage
from core.object_types.imgur import extract_imgur_id

IMGUR_API_BASE = "https://api.imgur.com/3"

# Matches an Imgur album URL, e.g. https://imgur.com/a/AbC123d — checked
# before IMGUR_ITEM_URL_RE so an album link doesn't get misread as a bare
# image id.
IMGUR_ALBUM_URL_RE = re.compile(r"imgur\.com/a/([A-Za-z0-9]+)")
# Matches a single-image post URL, plain (imgur.com/AbC123d) or under
# /gallery/ (imgur.com/gallery/AbC123d — Imgur's "shared to the public
# gallery" form of the same post).
IMGUR_ITEM_URL_RE = re.compile(r"imgur\.com/(?:gallery/)?([A-Za-z0-9]+)")


class ImgurImportError(Exception):
    """Raised for anything that should surface as a plain error message in
    the upload drawer — missing settings, an Imgur API failure, network
    error. Never a raw exception traceback."""


def _get_json(url, client_id, what):
    """One authenticated GET against the Imgur API, returning the parsed
    `data` payload. Every failure mode becomes an ImgurImportError (#220):
    not just HTTPError/URLError, but also a read timeout after connect
    (TimeoutError isn't a URLError), a non-JSON body (a Cloudflare/HTML
    error page), or a JSON body that isn't the {success, data} envelope --
    any of those used to escape as a raw 500 in the drawer. `what` names
    the thing being fetched for the error message."""
    req = urllib.request.Request(url, headers={"Authorization": f"Client-ID {client_id}"})
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            raw = resp.read()
    except urllib.error.HTTPError as e:
        detail = e.read().decode("utf-8", "ignore")[:200]
        raise ImgurImportError(f"Imgur API error {e.code} for {what}: {detail}")
    except urllib.error.URLError as e:
        raise ImgurImportError(f"Couldn't reach Imgur: {e.reason}")
    except (TimeoutError, OSError) as e:
        raise ImgurImportError(f"Imgur request failed for {what}: {e!r}")
    try:
        body = json.loads(raw)
    except ValueError:
        raise ImgurImportError(f"Imgur returned a non-JSON response for {what}: {raw[:200]!r}")
    if not isinstance(body, dict):
        raise ImgurImportError(f"Imgur returned an unexpected response shape for {what}: {raw[:200]!r}")
    if not body.get("success"):
        raise ImgurImportError(f"Imgur API reported failure: {body}")
    return body.get("data")


def _epoch(value):
    """Imgur's `datetime` is a unix-seconds integer; treat anything that
    doesn't coerce as unknown rather than crashing the whole import on one
    odd item (#220)."""
    if value is None or value == "":
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _fetch_page(client_id, username, page):
    url = f"{IMGUR_API_BASE}/account/{username}/submissions/{page}"
    data = _get_json(url, client_id, f"account {username!r}")
    return data if isinstance(data, list) else []


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
    if not isinstance(item, dict):
        return None
    images = item.get("images") or []
    if not isinstance(images, list) or not all(isinstance(i, dict) for i in images):
        images = []
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
            content_date=_epoch(norm["datetime"]),
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


def parse_imgur_url(url):
    """A pasted URL -> ("album", id) or ("image", id), or (None, None) if
    it doesn't look like an Imgur post/album link at all. Checks the album
    form first so imgur.com/a/<id> isn't misread as a bare image id, then
    falls back to a direct i.imgur.com file link (extract_imgur_id, same
    regex the object type itself uses), then a plain/gallery post link."""
    if not url:
        return None, None
    m = IMGUR_ALBUM_URL_RE.search(url)
    if m:
        return "album", m.group(1)
    direct_id = extract_imgur_id(url)
    if direct_id:
        return "image", direct_id
    m = IMGUR_ITEM_URL_RE.search(url)
    if m:
        return "image", m.group(1)
    return None, None


def _fetch_item(client_id, kind, item_id):
    path = "album" if kind == "album" else "image"
    url = f"{IMGUR_API_BASE}/{path}/{item_id}"
    data = _get_json(url, client_id, f"{path} {item_id!r}")
    if not isinstance(data, dict):
        raise ImgurImportError(f"Imgur returned no {path} data for {item_id!r}")
    return data


def import_from_url(url):
    """Issue #203: import exactly one pasted Imgur post/album URL — the
    owner picks precisely what comes in, instead of sync_public_gallery's
    all-or-nothing account-wide pull. Same Client-ID auth, same
    _normalize() shaping, same dedup-by-Imgur-id discipline; only the
    fetch itself differs (a single image/album lookup, not the
    account/{username}/submissions list)."""
    client_id = db.get_setting("imgur_client_id")
    if not client_id:
        raise ImgurImportError("Set an Imgur Client ID in the admin pane first.")

    kind, item_id = parse_imgur_url(url)
    if not item_id:
        raise ImgurImportError(f"Doesn't look like an Imgur post or album link: {url!r}")

    item = _fetch_item(client_id, kind, item_id)
    norm = _normalize(item)
    if norm is None:
        raise ImgurImportError("Imgur returned no usable image for that link.")

    imgur_id = extract_imgur_id(norm["external_url"])
    if imgur_id and imgur_id in {extract_imgur_id(u) for u in db.list_external_urls_by_media_type("imgur")}:
        return {"imported": 0, "skipped": 1, "slug": None}

    slug = storage.make_slug()
    db.insert_content(
        slug, db.SOURCE_MANUAL_UPLOAD, "imgur",
        external_url=norm["external_url"],
        content_description=norm["title"] or None,
        content_date=_epoch(norm["datetime"]),
        description=norm["description"],
        type_metadata=norm["type_metadata"],
    )
    return {"imported": 1, "skipped": 0, "slug": slug}
