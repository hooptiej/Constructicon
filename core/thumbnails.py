"""Generates the representative thumbnail image for a capture_events row.

This is the single place media_type dispatch happens for "how do I get a
picture of this thing" — see core/object_types.py for the per-type
ThumbnailSource specs this reads. Issues #13 (PDF) and #14 (STL), or any
future object type, only need to register an ObjectTypeSpec with a
thumbnail_url_fn or capture_fn; nothing here needs to change.

Best-effort throughout: a thumbnail failure (network error, bad URL, capture
routine not implemented yet) never raises — callers (upload, OCR, the
backfill script) all treat "no thumbnail" as a normal, recoverable state,
same as storage.make_thumbnail already did for corrupt uploaded images.
"""

import httpx

from . import object_types, storage

FETCH_TIMEOUT_SECONDS = 10

# #284: i.imgur.com answers HTTP 429 (Retry-After: 0, empty body, no
# Cloudflare ray id) to any request that doesn't carry a browser-like
# User-Agent -- UA-based bot blocking dressed up as a rate limit, not a real
# quota. httpx's default `python-httpx/x.y` UA trips it, which silently took
# down the whole Imgur -> thumbnail -> OCR -> caption chain from the box.
# A Chrome-style UA flips that to a clean 200 with real image bytes. Accept
# is kept to `image/*` (no avif/webp advertised) so a CDN has no reason to
# content-negotiate into a format Pillow might not decode.
#
# Sent on every FETCH_URL fetch, not just Imgur's: this dispatcher is
# deliberately type-agnostic (see module docstring), and YouTube's static
# thumbnail host serves browser UAs all day, so there's nothing to gain from
# special-casing -- and a per-type header would need exactly the
# `if media_type == ...` branch this module is meant never to grow.
FETCH_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36"
    ),
    "Accept": "image/*,*/*;q=0.8",
}


def ensure_thumbnail(row):
    """Generates and saves a thumbnail for `row` if its type has one and it
    isn't already on disk. Returns True if a thumbnail exists on disk after
    this call (freshly made or already there), False if this type has no
    thumbnail concept or generating one failed."""
    slug = row["slug"]
    if storage.thumb_path_for(slug).exists():
        return True
    spec = object_types.get_object_type(row.get("media_type"))
    try:
        if spec.thumbnail_source == object_types.ThumbnailSource.UPLOADED_FILE:
            return _from_uploaded_file(row)
        if spec.thumbnail_source == object_types.ThumbnailSource.FETCH_URL:
            return _from_fetch(row, spec)
        if spec.thumbnail_source == object_types.ThumbnailSource.CAPTURE:
            return _from_capture(row, spec)
    except Exception as e:
        print(f"thumbnail generation failed for {slug}: {e!r}")
    return False


def _from_uploaded_file(row):
    """UPLOADED_FILE-sourced types normally already get their thumbnail at
    upload time (see storage.save_file -> storage.make_thumbnail); this path
    exists so ensure_thumbnail is still correct if it's ever called before
    that's happened, or the thumbnail was lost, without needing a caller to
    know the difference between object types."""
    stored_filename = row.get("stored_filename")
    if not stored_filename:
        return False
    path = storage.path_for(stored_filename)
    if not path.exists():
        return False
    storage.save_thumbnail_from_bytes(row["slug"], path.read_bytes())
    return storage.thumb_path_for(row["slug"]).exists()


def _from_fetch(row, spec):
    if spec.thumbnail_url_fn is None:
        return False
    url = spec.thumbnail_url_fn(row.get("external_url"))
    if not url:
        return False
    resp = httpx.get(url, headers=FETCH_HEADERS, timeout=FETCH_TIMEOUT_SECONDS, follow_redirects=True)
    resp.raise_for_status()
    storage.save_thumbnail_from_bytes(row["slug"], resp.content)
    return storage.thumb_path_for(row["slug"]).exists()


def _from_capture(row, spec):
    """CAPTURE-sourced types (a stream's OSD/wait-card frame, a URL's
    screenshot, ...) need a real capture_fn wired up by whichever issue
    implements that type — see core/object_types.py. Nothing to do until
    then, but every future implementation plugs in here without this
    dispatcher changing."""
    if spec.capture_fn is None:
        return False
    image_bytes = spec.capture_fn(row)
    if not image_bytes:
        return False
    storage.save_thumbnail_from_bytes(row["slug"], image_bytes)
    return storage.thumb_path_for(row["slug"]).exists()
