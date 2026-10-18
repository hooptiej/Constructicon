"""Generates the representative thumbnail image for a capture_events row.

This is the single place media_type dispatch happens for "how do I get a
picture of this thing" — see core/object_types.py for the per-type
ThumbnailSource specs this reads. Issues #13 (PDF) and #14 (STL), or any
future object type, only need to register an ObjectTypeSpec with a
thumbnail_url_fn or capture_fn; nothing here needs to change.

Best-effort throughout: a thumbnail failure (network error, bad URL, capture
routine not implemented yet) never raises — callers (upload, OCR, the
backfill script) all treat "no thumbnail" as a normal, recoverable state.
For UPLOADED_FILE types, ensure_thumbnail is called synchronously by the
ingest pipeline so the upload response's thumb_url is ready immediately.
"""

import io
import logging

from PIL import Image

from . import besteffort, object_types, parse_limits, safe_fetch, storage

log = logging.getLogger("constructicon.thumbnails")

FETCH_TIMEOUT_SECONDS = 10
FETCH_MAX_BYTES = 10_000_000  # a thumbnail image; YouTube's hqdefault is ~30 KB

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


THUMBNAIL_STEP = "thumbnail"  # the parse_limits step name (the object page shows it)


def limit_key(row):
    """What core/parse_limits.py remembers a limit hit under: the stored file, else the slug."""
    return row.get("stored_filename") or row.get("slug")


def ensure_thumbnail(row, wait=None):
    """Generates and saves a thumbnail for `row` if its type has one and it
    isn't already on disk. Returns True if a thumbnail exists on disk after
    this call (freshly made or already there), False if this type has no
    thumbnail concept or generating one failed.

    #644: the work (decoding, rendering, fetching) runs in a parser child process
    (core/parse_limits.py, background budget), which writes the thumbnail file itself. A limit
    hit is remembered, so until it expires (or Retry / Refresh clears it) this returns False at
    once instead of rendering a hostile file again. `wait`: seconds to wait for a free parser
    slot (a request passes one; background work waits as long as it takes)."""
    slug = row["slug"]
    if storage.thumb_path_for(slug).exists():
        return True
    spec = object_types.get_object_type(row.get("media_type"))
    if spec.thumbnail_source == object_types.ThumbnailSource.NONE:
        return False
    if spec.thumbnail_source == object_types.ThumbnailSource.CAPTURE and spec.capture_fn is None:
        return False
    try:
        return bool(parse_limits.call(_generate, row, kind=parse_limits.BACKGROUND, what=THUMBNAIL_STEP,
                                      key=limit_key(row), wait=wait))
    except parse_limits.LimitExceeded:  # silent-ok: parse_limits logged the hit and remembers it for the page
        return False
    except Exception as e:
        print(f"thumbnail generation failed for {slug}: {e!r}")
    return False


def _generate(row):
    """ensure_thumbnail's work, in the parser child: dispatch on the type's thumbnail source."""
    spec = object_types.get_object_type(row.get("media_type"))
    if spec.thumbnail_source == object_types.ThumbnailSource.UPLOADED_FILE:
        return _from_uploaded_file(row)
    if spec.thumbnail_source == object_types.ThumbnailSource.FETCH_URL:
        return _from_fetch(row, spec)
    if spec.thumbnail_source == object_types.ThumbnailSource.CAPTURE:
        return _from_capture(row, spec)
    return False


def _from_uploaded_file(row):
    """UPLOADED_FILE-sourced types get their thumbnail via the ingest pipeline
    calling ensure_thumbnail after insert_upload (#448). This path also handles
    the case where ensure_thumbnail is called when a thumbnail was lost or
    didn't exist yet, without needing a caller to know the difference between
    object types."""
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
    # #724 (d): through the SSRF guard (public addresses only, pinned, every redirect re-checked,
    # image/* only, capped), like every outbound fetch. A refusal or failure raises FetchError with
    # its reason, which ensure_thumbnail logs and reports as "no thumbnail", as before.
    resp = safe_fetch.fetch(url, accept=("image/",), max_bytes=FETCH_MAX_BYTES, timeout=FETCH_TIMEOUT_SECONDS,
                            headers=FETCH_HEADERS, polite=False)
    storage.save_thumbnail_from_bytes(row["slug"], resp.body)
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


# --- A picture for an agent to look at (#588) -------------------------------------------------

VIEW_MAX_EDGE = 1024          # long edge of a "preview" picture
VIEW_MAX_BYTES = 1_500_000    # encoded size cap, so one view never floods a model's context
VIEW_SIZES = {"thumb": storage.THUMB_MAX_DIM, "preview": VIEW_MAX_EDGE}


def _view_source(row, size):
    """The best picture of the item to show: for "preview", the uploaded image itself when the
    type's thumbnail IS the file (images, GIF, PSD, ...); otherwise (and for "thumb") the
    type's generated thumbnail (PDF page, STL render, video frame, ...). None when it has none."""
    spec = object_types.get_object_type(row.get("media_type"))
    if size == "preview" and spec.thumbnail_source == object_types.ThumbnailSource.UPLOADED_FILE and row.get("stored_filename"):
        path = storage.path_for(row["stored_filename"])
        if path.exists():
            return path
    ensure_thumbnail(row)
    thumb = storage.thumb_path_for(row["slug"])
    return thumb if thumb.exists() else None


def _encode_view(img, edge, has_alpha, prefer_png=False):
    """PNG for PNG sources and alpha/greyscale images (screenshots keep readable text) when it fits the byte cap, else
    JPEG stepped down in quality, then in size, until it does. Returns (bytes, mime, (w, h))."""
    img.thumbnail((edge, edge))
    if prefer_png or has_alpha or img.mode in ("L", "1"):
        buf = io.BytesIO()
        img.save(buf, "PNG", optimize=True)
        if buf.tell() <= VIEW_MAX_BYTES:
            return buf.getvalue(), "image/png", img.size
    if has_alpha:
        rgba = img.convert("RGBA")
        flat = Image.new("RGB", img.size, (255, 255, 255))
        flat.paste(rgba, mask=rgba.split()[-1])
    else:
        flat = img.convert("RGB")
    while True:
        for q in (85, 70, 55):
            buf = io.BytesIO()
            flat.save(buf, "JPEG", quality=q)
            if buf.tell() <= VIEW_MAX_BYTES:
                return buf.getvalue(), "image/jpeg", flat.size
        if max(flat.size) <= 128:  # cannot happen in practice; return the smallest rather than loop
            return buf.getvalue(), "image/jpeg", flat.size
        flat = flat.resize((max(1, int(flat.width * 0.75)), max(1, int(flat.height * 0.75))))


VIEW_STEP = "view picture"


def render_view(row, size="preview"):
    """(image bytes, mime type, (width, height)) of a picture of this item sized for an agent to
    look at (long edge <= VIEW_MAX_EDGE, encoded <= VIEW_MAX_BYTES), or None when the item has no
    visual (a certificate, an archive, a type with no renderer). `size` is "thumb" or "preview".
    Reuses the types' own renderers via ensure_thumbnail. Never raises: an unreadable file is
    "no visual". #644: decoded in a parser child (background budget)."""
    try:
        return parse_limits.call(_render_view, row, size, kind=parse_limits.BACKGROUND, what=VIEW_STEP,
                                 key=limit_key(row))
    except Exception as e:  # a limit hit or an unreadable file: "no visual"
        besteffort.warn(log, "thumbnails: couldn't render a view picture", e, slug=row["slug"])
        return None


def _render_view(row, size):
    """render_view's work, in the parser child."""
    edge = VIEW_SIZES[size]
    path = _view_source(row, size)
    if path is None:
        return None
    try:
        with Image.open(path) as img:
            source_is_png = img.format == "PNG"
            img = storage.exif_upright(img)
            img.load()
            has_alpha = img.mode in ("RGBA", "LA") or (img.mode == "P" and "transparency" in img.info)
            if img.mode not in ("RGB", "RGBA", "L", "LA", "1"):
                img = img.convert("RGBA" if has_alpha else "RGB")
            return _encode_view(img.copy(), edge, has_alpha, prefer_png=source_is_png)
    except Exception as e:  # not an image Pillow can read: treated as "no visual"
        besteffort.warn(log, "thumbnails: couldn't render a view picture", e, slug=row["slug"])
        return None
