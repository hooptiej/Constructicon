"""Constructicon web app: upload, gallery, and the public /f/{slug} hotlink
route.

No auth — this runs on a LAN-only dev server with no port forward, so the
network perimeter is the security boundary, not a login gate.
"""

import asyncio
import io
import json
import sys
import threading
import zipfile
from datetime import datetime
from pathlib import Path
from urllib.parse import unquote, quote

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from fastapi import FastAPI, Request, Form, UploadFile, File, HTTPException, BackgroundTasks
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.datastructures import FormData

from core import backup, captions, db, imgur_import, object_types, ocr, similarity, storage, thumbnails

app = FastAPI()
app.mount("/static", StaticFiles(directory=Path(__file__).parent / "static"), name="static")
# Brand assets (logo, wordmarks, favicons) live at the repo root in
# assets/brand/, independent of web/static/ — see README's "Retained art
# assets" section. Mounted separately rather than copied into web/static so
# there's a single source of truth for them.
_BRAND_DIR = Path(__file__).resolve().parent.parent / "assets" / "brand"
if _BRAND_DIR.is_dir():
    app.mount("/brand", StaticFiles(directory=_BRAND_DIR), name="brand")
templates = Jinja2Templates(directory=Path(__file__).parent / "templates")


# --- Audit logging middleware ---

def _scrub_secrets(form_data):
    """Remove values from form_data dict whose keys look like secrets (contain
    'key', 'secret', 'token', 'password', etc., case-insensitive) and replace
    with a redaction marker. Returns a new dict without mutating the original."""
    if not form_data:
        return {}
    scrubbed = {}
    secret_keywords = {"key", "secret", "token", "password", "api", "auth"}
    for key, value in form_data.items():
        key_lower = key.lower()
        # Check if any secret keyword is in the key name
        if any(keyword in key_lower for keyword in secret_keywords):
            scrubbed[key] = "[REDACTED]"
        elif hasattr(value, "filename"):
            # A multipart file field (Starlette UploadFile) — not
            # JSON-serializable and its content isn't audit-log-worthy
            # anyway, so log just enough to identify it.
            scrubbed[key] = f"<file: {value.filename}>"
        else:
            scrubbed[key] = value
    return scrubbed


class AuditLoggingMiddleware(BaseHTTPMiddleware):
    """Middleware to capture mutating /api/* requests (POST/PUT/DELETE) into
    the audit_log table. Reads the form body, scrubs secrets, logs the request
    with status and any error detail, then passes it through to the handler."""

    async def dispatch(self, request: Request, call_next):
        # Only audit mutating /api/* requests
        is_mutating = request.method in {"POST", "PUT", "DELETE"}
        is_api = request.url.path.startswith("/api/")
        should_audit = is_mutating and is_api

        form_data = {}
        if should_audit and request.method in {"POST", "PUT"}:
            # Read the request body so we can log it. Starlette automatically caches
            # the body after the first read, so the handler can read it again.
            try:
                body_bytes = await request.body()
                # Try to parse as form data — FastAPI routes use Form(...) parameters
                if body_bytes:
                    try:
                        # Starlette's FormData is a multi-dict — the bulk
                        # routes send `slugs=a&slugs=b&...` as repeated
                        # fields (`slugs: list[str] = Form(...)`), and a
                        # plain dict() would keep only the last value
                        # (#214). Keep every value: a repeated key becomes
                        # a list, a single one stays a scalar.
                        form = await request.form()
                        form_data = {}
                        for key in form.keys():
                            values = form.getlist(key)
                            form_data[key] = values if len(values) > 1 else values[0]
                    except Exception:
                        # If form parsing fails, try JSON (some endpoints might use JSON)
                        try:
                            form_data = json.loads(body_bytes)
                        except Exception:
                            # If both fail, leave form_data empty — don't break the request
                            pass
            except Exception:
                # If anything goes wrong reading the body, just proceed without
                # logging the request body — don't let an audit logging error
                # break the actual request
                pass

        # Call the actual route handler. A route can fail two ways: a caught
        # HTTPException/RequestValidationError, which Starlette's own
        # exception middleware (inside call_next) already turns into a
        # normal Response before it gets back here — no raise, just a 4xx/5xx
        # response, handled by the `else` branch below — or a truly unhandled
        # exception, which propagates out of call_next itself. The whole
        # point of #122 was making *that* second case debuggable after the
        # fact, so the audit row must still be written even though we
        # re-raise: do it in `finally`, not after a bare `try/except ...
        # raise` (which would skip the insert on every unhandled exception —
        # exactly the scenario this feature exists for).
        response = None
        status_code = 500
        error_detail = None
        try:
            response = await call_next(request)
            status_code = response.status_code
        except Exception as e:
            error_detail = str(e)
            raise
        finally:
            if should_audit:
                scrubbed_form = _scrub_secrets(form_data)
                affected_slugs = []
                # Try to extract affected slugs from the path (e.g., /api/image/{slug})
                if "/image/" in request.url.path:
                    parts = request.url.path.split("/")
                    if len(parts) > 3 and parts[1] == "api" and parts[2] == "image":
                        slug = parts[3]
                        affected_slugs = [slug]
                # Also check for slugs in form data if present. Repeated
                # form fields arrive as a list (see above); a single slug
                # arrives as a bare string, which is the slug itself — not
                # JSON to be parsed (#214). Only a string that actually
                # looks like a JSON array gets decoded (a JSON-body client).
                if "slugs" in form_data:
                    try:
                        slugs = form_data["slugs"]
                        if isinstance(slugs, str):
                            stripped = slugs.strip()
                            if stripped.startswith("["):
                                slugs = json.loads(stripped)
                            else:
                                slugs = [slugs]
                        if isinstance(slugs, list):
                            affected_slugs.extend(
                                s for s in slugs if isinstance(s, str) and s
                            )
                    except Exception:
                        pass
                # Deduplicate
                affected_slugs = list(set(affected_slugs))

                db.insert_audit_log(
                    method=request.method,
                    path=request.url.path,
                    form_body=scrubbed_form,
                    status_code=status_code,
                    error_detail=error_detail,
                    affected_slugs=affected_slugs,
                )

        return response


app.add_middleware(AuditLoggingMiddleware)

# Source (capture_events.tech): who or what actually added a row, and how —
# see core/db.py's SOURCE_* constants/source_group() for the full vocabulary
# and grouping logic. The web upload drawer and the desktop uploader app both
# POST to /api/upload with no client-supplied identity (this is a
# single-owner site, not a multi-tech tool) — the server tells them apart by
# the desktop app's identifying request header (see api_upload below) and
# stamps the right Source string itself rather than trusting a client field.
DESKTOP_APP_CLIENT_HEADER = "X-Constructicon-Client"
DESKTOP_APP_CLIENT_VALUE = "desktop-app"

DESKTOP_APP_DIR = Path(__file__).resolve().parent.parent / "desktop_app"
# Separate from both desktop_app/ (source) and storage/ (capture-event
# files) on purpose — this is neither. One file, whoever uploads last wins;
# there's no versioning, just the current build.
DESKTOP_APP_BUILD_DIR = Path(__file__).resolve().parent.parent / "desktop_app_build"
DESKTOP_APP_BUILD_PATH = DESKTOP_APP_BUILD_DIR / "Constructicon-Uploader.zip"


def _has_thumbnail(row, spec=None):
    """Whether `row` should expose a /f/<slug>/thumb URL at all — true for
    any type whose spec has a thumbnail strategy (see core/object_types.py),
    even if the thumbnail hasn't actually been produced yet (get_thumbnail
    below fetches/generates it lazily on first request). False for types
    with ThumbnailSource.NONE — a plain document post (no file at all) or,
    since #28, an uploaded audio file (a real file, but no visual frame to
    show as a thumbnail). Dispatches purely off the spec rather than
    short-circuiting on "row has a filename", since that assumption (true
    for image/pdf/stl/psd/svg/eps, whose uploaded-file types always have
    *some* visual to show) no longer holds once a type can have a stored
    file with nothing image-like to derive a thumbnail from.

    A CAPTURE-strategy type with no `capture_fn` yet (e.g. `url` — see
    core/object_types/url.py) can never actually produce one, regardless of
    thumbnail_source, so it's excluded here too rather than optimistically
    claiming a /f/<slug>/thumb URL that will only ever 404 (#197)."""
    spec = spec or object_types.get_object_type(row.get("media_type"))
    if spec.thumbnail_source == object_types.ThumbnailSource.CAPTURE and spec.capture_fn is None:
        return False
    return spec.thumbnail_source != object_types.ThumbnailSource.NONE


def _to_public(row):
    media_type = row.get("media_type") or "image"
    spec = object_types.get_object_type(media_type)
    # A redacted row has no file left, and /f/<slug>/thumb answers 410 for
    # it -- don't advertise a thumbnail URL that can't be fetched (#222).
    # Matches what the project-card shape below already does; the card
    # templates also check `redacted` themselves, so this is about the API
    # shape being consistent for any consumer that doesn't.
    has_thumb = _has_thumbnail(row, spec) and not row["redacted"]
    return {
        "slug": row["slug"],
        "url": f"/f/{row['slug']}",
        "thumb_url": f"/f/{row['slug']}/thumb" if has_thumb else None,
        "filename": row["filename"],
        # filename is None for content-only rows (youtube/document posts —
        # see insert_content in core/db.py); the gallery cards need
        # something readable to show in its place rather than the literal
        # string "null".
        # row["display_name"] (#11) is a per-object override — set via
        # /api/image/<slug> or the constructicon_rename MCP tool — that takes
        # priority over the old filename/content_description/slug fallback
        # chain when present.
        "display_name": row.get("display_name") or row["filename"] or row.get("content_description") or row["slug"],
        # File-kind badge (issue #12) — driven entirely by the type's
        # ObjectTypeSpec (core/object_types.py) so gallery cards never need
        # an if/else on media_type; a new type registered there picks up a
        # badge automatically. row["icon"] (#11) is a per-object override
        # that takes priority over the type's generic badge_icon.
        "media_type": media_type,
        "type_label": spec.label,
        "type_icon": row.get("icon") or spec.badge_icon,
        "type_badge": spec.badge_text,
        # Whether the gallery/home cards should render the actual thumbnail
        # image (thumb_url above) or a generic file icon — driven by the
        # type's spec (same _has_thumbnail used server-side for the detail
        # page and project covers), not a hardcoded filename-extension
        # check, so a type with a generated thumbnail (a PDF's rendered
        # first page, once a stream/URL capture is wired up) picks this up
        # for free instead of always falling back to the file icon.
        "has_thumbnail": has_thumb,
        "description": row["description"],
        "tags": row["tags"],
        "client": row["client"],
        "uploaded_at": row["timestamp"],
        # uploaded_by keeps the exact Source string (identity/filter key —
        # used by /api/gallery and search); uploaded_by_display is the short
        # grouping label (see core/db.py's source_group()) for compact card
        # UI, so a long Source sentence like "Hooptie J (me) — manual upload"
        # doesn't overflow a gallery card's small meta line. The full string
        # is only spelled out in full on the object detail page.
        "uploaded_by": row["tech"],
        "uploaded_by_display": db.source_group(row["tech"]),
        "redacted": bool(row["redacted"]),
        "source": row["source"],
        "extracted_text": row["extracted_text"],
        "ocr_status": row["ocr_status"],
        "artifact_link": row["artifact_link"],
        "type_metadata": row.get("type_metadata", {}),
    }


def _youtube_embed_url(external_url):
    """Builds a canonical embed URL from whatever YouTube URL shape is
    stored in external_url. Returns None if it doesn't look like a YouTube
    link at all — the template falls back to a plain external-link CTA in
    that case rather than rendering a broken iframe. Video-ID extraction is
    centralized in object_types.extract_youtube_id so this and the static
    thumbnail URL (see core/object_types.py's youtube_thumbnail_url) can't
    drift apart."""
    video_id = object_types.extract_youtube_id(external_url)
    return f"https://www.youtube.com/embed/{video_id}" if video_id else None


def _friendly_date(epoch):
    """'%-d'-style formatting (no leading zero) without relying on the
    platform-specific %-d/%-e strftime extension, which isn't available on
    Windows — this runs cross-platform."""
    if not epoch:
        return None
    dt = datetime.fromtimestamp(epoch)
    return f"{dt.strftime('%b')} {dt.day}, {dt.year}"


def _friendly_datetime(epoch):
    if not epoch:
        return None
    dt = datetime.fromtimestamp(epoch)
    hour12 = dt.hour % 12 or 12
    ampm = "AM" if dt.hour < 12 else "PM"
    return f"{_friendly_date(epoch)} at {hour12}:{dt.minute:02d} {ampm}"


def _friendly_file_size(size_bytes):
    """Convert a file size in bytes to a human-readable string (e.g.,
    '1.2 MB', '340 KB', '12 B'). Returns None if size_bytes is None."""
    if size_bytes is None:
        return None
    if size_bytes < 1024:
        return f"{size_bytes} B"
    elif size_bytes < 1024 * 1024:
        return f"{size_bytes / 1024:.1f} KB"
    elif size_bytes < 1024 * 1024 * 1024:
        return f"{size_bytes / (1024 * 1024):.1f} MB"
    else:
        return f"{size_bytes / (1024 * 1024 * 1024):.1f} GB"


def _call_properties_fn(spec, row):
    """Call the spec's properties_fn if it exists, with defensive error
    handling. Returns {} on any failure, same best-effort discipline as
    core/ocr.py/core/thumbnails.py's defensive-call patterns."""
    if not spec.properties_fn:
        return {}
    try:
        return spec.properties_fn(row) or {}
    except Exception as e:
        print(f"properties_fn failed for {row.get('slug')}: {e!r}")
        return {}


def _to_object_detail(row):
    """Full detail-page shape for GET /object/<slug> — unlike _to_public,
    this works for any media_type, not just uploaded images. filename can be
    None (a youtube/document row with no local file — see insert_content in
    core/db.py), so nothing here assumes it's set.
    """
    filename = row.get("filename")
    is_file = bool(filename)
    media_type = row.get("media_type") or "image"
    spec = object_types.get_object_type(media_type)
    has_thumb = _has_thumbnail(row, spec)
    return {
        "slug": row["slug"],
        "media_type": media_type,
        "type_label": spec.label,
        "type_icon": spec.badge_icon,
        "type_badge": spec.badge_text,
        "ocr_capable": spec.ocr_capable,
        # #239: drives the "Suggested caption" panel — the caption itself
        # lives in type_metadata.auto_caption (see core/captions.py).
        "caption_capable": spec.caption_capable and not captions.DISABLED,
        "filename": filename,
        "is_file": is_file,
        # Drives the full-size <img src="{{ item.url }}"> preview branch in
        # object_detail.html. Registry-driven (#218): a type whose thumbnail
        # *is* the uploaded file (image, gif -- see core/object_types/) can be
        # shown directly by the browser. The old hardcoded suffix tuple here
        # missed .webp/.bmp/.tiff/.ico, which image.py registers, so those
        # uploads fell through to the 400px-thumbnail branch instead.
        "is_image_file": is_file and spec.thumbnail_source == object_types.ThumbnailSource.UPLOADED_FILE,
        # #28: drives the <audio controls> mini player branch in
        # object_detail.html. media_type-based rather than an
        # extension-suffix check, since "audio" is registered ahead of
        # _to_object_detail via core/object_types.py and nothing here needs
        # to know its exact extensions.
        "is_audio_file": is_file and media_type == "audio",
        # #92: drives the <video controls> player branch in object_detail.html,
        # following the is_audio_file pattern — media_type-based check rather
        # than extension tuple, since "video" is registered in object_types.py.
        "is_video_file": is_file and media_type == "video",
        "url": f"/f/{row['slug']}" if is_file else None,
        "thumb_url": f"/f/{row['slug']}/thumb" if has_thumb else None,
        "external_url": row.get("external_url"),
        "youtube_embed_url": _youtube_embed_url(row.get("external_url")) if media_type == "youtube" else None,
        "content_description": row.get("content_description"),
        "content_date_display": _friendly_date(row.get("content_date")),
        # #54: freeform per-type metadata (view/like/comment counts, full
        # description, uploading channel if not the owner's own — see
        # core/object_types.py's "youtube" metadata_fields and
        # scripts/full_youtube_channel_sync.py). {} for every row nothing
        # has ever written type_metadata for, same "always a dict, never
        # missing" contract as row["tags"].
        "type_metadata": row.get("type_metadata") or {},
        # See _to_public's matching comment — row["display_name"]/row["icon"]
        # (#11) are per-object overrides that win over the generic fallbacks.
        "display_name": row.get("display_name") or filename or row.get("content_description") or row["slug"],
        "icon": row.get("icon") or spec.badge_icon,
        "description": row["description"],
        "tags": row["tags"],
        "client": row["client"],
        # #47: current project membership — a project selector needs to
        # show what's already attached, not just a blank picker, and (per
        # #51's backfill) an object can now belong to a project it was
        # never uploaded with. _to_project_option is the same slim shape
        # the upload drawer's dropdown already uses.
        "projects": [_to_project_option(p) for p in db.list_projects_for_post(row["slug"])],
        "uploaded_at": row["timestamp"],
        "uploaded_at_display": _friendly_datetime(row["timestamp"]),
        # Full Source string, unshortened — this is the one place it's meant
        # to be spelled out in full (see _to_public for the compact/grouped
        # version used everywhere else).
        "source_display": row["tech"],
        "redacted": bool(row["redacted"]),
        "extracted_text": row["extracted_text"],
        "ocr_status": row["ocr_status"],
        # #135: file size and original modification date for the Properties panel
        "file_size": row.get("file_size"),
        "file_size_display": _friendly_file_size(row.get("file_size")),
        "source_modified_at_display": _friendly_date(row.get("source_modified_at")),
        # #135: type-specific properties (dimensions, duration, etc.) via the
        # properties_fn hook, wrapped in defensive try/except at the call site
        # (properties_fn implementations are best-effort internally; this adds
        # a second layer of safety matching core/ocr.py/core/thumbnails.py's
        # defensive-call pattern).
        "properties": _call_properties_fn(spec, row),
    }


def _flatten_tags(nodes):
    """Depth-first flatten of the list_tag_tree() structure — used to look
    up a tag by slug from a query param without a dedicated db helper."""
    flat = []
    for node in nodes:
        flat.append(node)
        flat.extend(_flatten_tags(node.get("children") or []))
    return flat


def _project_cover_url(cover_slug):
    """cover_slug references a capture_events row (see projects.cover_slug
    in core/db.py) — reuse the same thumb route the gallery uses for images.
    A row whose type has no thumbnail concept (e.g. a plain document post),
    or a missing/deleted/redacted slug, falls back to None so the template
    can render a placeholder instead of a broken image."""
    if not cover_slug:
        return None
    row = db.get_by_slug(cover_slug)
    if not row or row.get("redacted") or not _has_thumbnail(row):
        return None
    return f"/f/{row['slug']}/thumb"


def _to_project_card(project):
    writeup_excerpt = None
    if project.get("writeup_slug"):
        writeup_doc = db.get_by_slug(project["writeup_slug"])
        if writeup_doc:
            body = writeup_doc.get("type_metadata", {}).get("body", "")
            if body:
                # Truncate to ~200 chars at a word boundary
                words = body.split()
                excerpt_words = []
                char_count = 0
                for word in words:
                    if char_count + len(word) + 1 > 200:
                        break
                    excerpt_words.append(word)
                    char_count += len(word) + 1
                writeup_excerpt = " ".join(excerpt_words)
                if len(body) > char_count:
                    writeup_excerpt += "…"

    return {
        "slug": project["slug"],
        "title": project["title"],
        "description": project["description"],
        "status": project["status"],
        "cover_url": _project_cover_url(project.get("cover_slug")),
        # #56: front-page sort control needs a date to sort "Newest"/"Oldest"
        # by — created_at was already stored on every project row, just never
        # exposed to this card shape before.
        "created_at": project["created_at"],
        "writeup_excerpt": writeup_excerpt,
    }


def _project_has_tag(project, member_slugs):
    return any(item["slug"] in member_slugs for item in db.list_project_items(project["id"]))


def _build_breadcrumbs(from_param, current_item_name):
    """Build a breadcrumb trail for the object detail page based on the `from`
    query parameter. Returns a list of dicts with "label" and "href" keys.
    The last item (current_item_name) has no href since it's not a link.

    #137: breadcrumb navigation on object detail page.
    """
    breadcrumbs = []

    if not from_param:
        # No from param — default to just Home
        breadcrumbs.append({"label": "Home", "href": "/"})
    elif from_param == "unfiled":
        breadcrumbs.append({"label": "Home", "href": "/"})
        breadcrumbs.append({"label": "Unfiled", "href": "/unfiled"})
    elif from_param.startswith("project:"):
        # Extract project slug and look up the project
        project_slug = from_param[8:]  # Remove "project:" prefix
        project = db.get_project(project_slug)
        if project:
            breadcrumbs.append({"label": "Home", "href": "/"})
            # No dedicated projects index route, so Projects links to home
            breadcrumbs.append({"label": "Projects", "href": "/"})
            breadcrumbs.append({"label": project["title"], "href": f"/project/{project_slug}"})
        else:
            # Project doesn't exist or was deleted — fall back to Home only
            breadcrumbs.append({"label": "Home", "href": "/"})
    elif from_param.startswith("user:"):
        # Extract and URL-decode the uploader name
        uploader = unquote(from_param[5:])  # Remove "user:" prefix
        breadcrumbs.append({"label": "Home", "href": "/"})
        breadcrumbs.append({"label": f"{uploader}'s uploads", "href": f"/gallery/user/{quote(uploader)}"})
    else:
        # Unrecognized from param — default to Home
        breadcrumbs.append({"label": "Home", "href": "/"})

    # Add the current item as a non-linked breadcrumb
    breadcrumbs.append({"label": current_item_name, "href": None})

    return breadcrumbs


def _to_content_public(row, project_slug=None):
    """Public shape for a project-item card. Broader than _to_public: a
    project can contain backfilled youtube/document posts as well as real
    uploaded files, and those have no filename/stored_filename to build a
    thumb from (see core/db.py's insert_content) — but every row, regardless
    of media_type, now gets its own local /object/<slug> detail page, so
    cards always link locally instead of bouncing straight to external_url.

    If project_slug is provided, appends ?from=project:{project_slug} to the
    link for breadcrumb navigation (#137).
    """
    is_file = bool(row.get("filename"))
    media_type = row.get("media_type") or "image"
    spec = object_types.get_object_type(media_type)
    has_thumb = _has_thumbnail(row)
    link = f"/object/{row['slug']}"
    if project_slug:
        link = f"{link}?from=project:{project_slug}"
    return {
        "slug": row["slug"],
        "title": row.get("content_description") or row.get("description") or row.get("filename") or row["slug"],
        "media_type": media_type,
        "type_icon": spec.badge_icon,
        "type_badge": spec.badge_text,
        "is_file": is_file,
        "thumb_url": f"/f/{row['slug']}/thumb" if has_thumb and not row.get("redacted") else None,
        "link": link,
        "external": not is_file,
        "tags": row["tags"],
        "content_date": row.get("content_date"),
        "timestamp": row.get("timestamp"),
    }


OCR_WATCHDOG_INTERVAL_SECONDS = 60
OCR_STALE_THRESHOLD_SECONDS = 600  # 10 min — well past normal queueing even under a big batch


def _refire_ocr(slug):
    # set_ocr_status(..., "pending") stamps a fresh ocr_started_at, so this
    # attempt gets its own staleness clock — without that, a row re-fired
    # here would still look exactly as stale to the watchdog on its very
    # next tick, and get fired again every interval instead of once.
    db.set_ocr_status(slug, "pending")
    threading.Thread(target=ocr.run_ocr, args=(slug,), daemon=True).start()


def _ensure_capture_thumbnail(slug):
    """For a CAPTURE-sourced type that ISN'T ocr_capable (STL today — a
    binary mesh format with no text worth OCR'ing), there's no OCR
    background task to piggyback a thumbnail render onto the way PDF's is
    (see core/ocr.py's _ocr_source_path, which calls
    thumbnails.ensure_thumbnail as a side effect of preparing an OCR
    source). Without this, get_thumbnail's own lazy-generate fallback below
    only fires for content-only rows (stored_filename is None), so a
    file-backed CAPTURE type would silently serve the raw original file
    instead of a real thumbnail on every request until someone happened to
    run backfill_thumbnails.py. Scheduled as its own background task,
    same spirit as OCR, so it doesn't block the upload response."""
    row = db.get_by_slug(slug)
    if row is not None:
        thumbnails.ensure_thumbnail(row)


async def _ocr_watchdog():
    while True:
        await asyncio.sleep(OCR_WATCHDOG_INTERVAL_SECONDS)
        try:
            stale = db.list_stale_pending_ocr(OCR_STALE_THRESHOLD_SECONDS)
            for row in stale:
                print(f"OCR watchdog: re-firing {row['slug']} — pending for over {OCR_STALE_THRESHOLD_SECONDS}s")
                _refire_ocr(row["slug"])
        except Exception as e:
            print(f"OCR watchdog error: {e!r}")


@app.on_event("startup")
async def startup():
    db.init_db()
    db.ensure_special_clients()
    # Self-heal: a redeploy/restart while OCR was still queued or running for
    # a row leaves it stuck at ocr_status="pending" forever otherwise, since
    # nothing else will ever retry it. Fired as background threads, not run
    # here directly — this must not block the app from starting up, which is
    # exactly what happened before this fix when several rows were stuck at
    # once (each blocking, one after another, on the way to accepting any
    # requests at all).
    stuck = db.list_pending_ocr()
    if stuck:
        print(f"re-running OCR for {len(stuck)} row(s) left pending by a prior process")
        for row in stuck:
            _refire_ocr(row["slug"])
    asyncio.create_task(_ocr_watchdog())


@app.get("/healthz")
def healthz():
    return {"ok": True}


# --- Pages ---

@app.get("/", response_class=HTMLResponse)
def home_page(request: Request, tag: str = "", scope: str = "top"):
    """Home is the gallery itself (left third) plus a curated Projects
    section (right two-thirds) — see README's Projects/tag-tree note for why
    projects and blog_tags are separate concepts. ?tag=<slug> filters the
    Projects section down to cards with at least one item under that tag or
    one of its descendants; the gallery pane (client-side, /api/gallery) is
    unaffected by it.
    """
    tag_tree = db.list_tag_tree()
    selected_tag = None
    if tag:
        selected_tag = next((t for t in _flatten_tags(tag_tree) if t["slug"] == tag), None)
    all_projects = db.list_projects()
    # #173: the pill row is a project filter, not a general tag browser —
    # pills are derived from projects themselves (via each project's linked
    # tag_id), not from every root-level blog_tags row. This naturally
    # excludes pre-Projects "category" tags with no matching project (e.g.
    # "AlienWhoop & TinyShark") that #154's child-project-only exclusion
    # missed, and ?scope=all opts into including child projects' pills too.
    # selected_tag above still matches against the full tag tree, so a
    # direct ?tag= link works regardless of which pills are shown.
    tags_by_id = {t["id"]: t for t in _flatten_tags(tag_tree)}
    pill_source = all_projects if scope == "all" else [p for p in all_projects if p.get("parent_id") is None]
    project_pills, seen_tag_ids = [], set()
    for p in sorted(pill_source, key=lambda p: p["title"]):
        pill_tag = tags_by_id.get(p.get("tag_id"))
        if not pill_tag or pill_tag["id"] in seen_tag_ids:
            continue
        seen_tag_ids.add(pill_tag["id"])
        project_pills.append({**pill_tag, "name": p["title"]})
    # #149: only top-level projects belong on the front-page widget — a
    # child project (parent_id set, #133) is reached via its parent's
    # project detail page, not as its own tile here.
    projects = [p for p in all_projects if p.get("parent_id") is None]
    if selected_tag:
        member_slugs = {r["slug"] for r in db.list_posts_for_tag(selected_tag["id"], limit=10000)}
        projects = [p for p in projects if _project_has_tag(p, member_slugs)]
    # Owner name/initials for the combined gallery+upload pop-out's tab
    # (#17) — SOURCE_GROUPS[0] is the site's single-owner display label
    # (e.g. "Hooptie J (me)"); strip the "(me)" qualifier for the tab's
    # name text and derive initials from what's left ("Hooptie J" -> "HJ").
    _owner_label = db.SOURCE_GROUPS[0].split(" (")[0]
    _owner_initials = "".join(w[0] for w in _owner_label.split()[:2]).upper()
    # #41: uploads with no project membership at all — always shown on the
    # home page (not just in the hover pop-out) so the page never looks
    # empty/broken just because no projects exist yet or an upload wasn't
    # filed into one. See db.list_unfiled_items's docstring for the incident
    # this fixes.
    unfiled_items = [_to_public(r) for r in db.list_unfiled_items()]
    # #107: most recent uploads per media_type. Each type gets its own N
    # most recent items, independent of upload activity in other types. This
    # ensures every type that has any uploads appears in the Files widget tabs.
    # Embedded as JSON keyed by media_type so the Files widget can render
    # per-type tabs and fetch each type's own list without competing within a
    # shared global pool.
    recent_by_type = {
        mt: [_to_public(r) for r in rows]
        for mt, rows in db.list_recent_items_by_type().items()
    }
    return templates.TemplateResponse(
        request, "home.html",
        {
            "active": "home",
            "top_tags": project_pills,
            "selected_tag_slug": tag or None,
            "pill_scope": scope,
            "projects": [_to_project_card(p) for p in projects],
            "owner_name": _owner_label,
            "owner_initials": _owner_initials,
            "unfiled_items": unfiled_items,
            "recent_by_type": recent_by_type,
        },
    )


@app.post("/api/delete-all")
def api_delete_all():
    """Wipe every capture_events row (and its files), plus tags and
    projects — a full reset. Stands in for imagerepo's old per-user
    'delete my uploads' button now that multi-user accounts are gone;
    single-owner site, so 'my uploads' and 'everything' are the same set.
    Development convenience while content/schema are still in flux, not a
    feature meant to stick around once the site has real content worth
    protecting."""
    rows = db.search(limit=100000)
    for row in rows:
        if row.get("stored_filename"):
            storage.delete_files(row["slug"], row["stored_filename"])
        db.delete_upload(row["slug"])
    conn = db.get_conn()
    conn.execute("DELETE FROM post_tags")
    conn.execute("DELETE FROM project_items")
    conn.execute("DELETE FROM projects")
    conn.execute("DELETE FROM blog_tags")
    conn.commit()
    conn.close()
    return JSONResponse({"deleted": len(rows)})


@app.post("/api/delete")
def api_delete_selected(slugs: list[str] = Form(...)):
    """Selective delete (#19) — remove just the given objects, any mix of
    sources (uploaded images, youtube rows, etc.), without touching tags
    or projects. That global wipe is specific to /api/delete-all's
    full-reset button; this is the day-to-day 'clear this test content'
    path, driven by gallery checkboxes or a single object's detail page.
    Reuses the same per-row deletion primitives as /api/delete-all."""
    deleted = 0
    for slug in slugs:
        row = db.get_by_slug(slug)
        if row is None:
            continue
        if row.get("stored_filename"):
            storage.delete_files(row["slug"], row["stored_filename"])
        db.delete_upload(row["slug"])
        deleted += 1
    return JSONResponse({"deleted": deleted})


@app.post("/api/backup")
def api_backup():
    """Standalone backup safety net (#20) — zips the DB and every file in
    storage/ into a timestamped archive under core.backup.BACKUP_DIR, then
    prunes down to the most recent BACKUP_RETENTION_COUNT archives.

    Deliberately its own button/endpoint, not called from /api/delete-all or
    /api/delete: a backup that only ran as a side effect of a delete could
    be mistaken for "already backed up" when it wasn't (see #19's history).
    Triggered on demand only — no scheduled job here, see #20's discussion
    for that as separate future work."""
    try:
        info = backup.create_backup()
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Backup failed: {e}")
    return JSONResponse(info)


# API Keys (#55) — the admin pane's allowlist of settings keys it knows how
# to display/accept. A secret's storage/endpoints (below, and
# core.db.get_setting/has_setting/set_setting) are generic key/value, so a
# future second key (or any other app-level setting) only needs an entry
# here plus a labeled row in _admin_pane.html, not a schema change.
# thingiverse_app_token (#62): read-only Thingiverse API access token for
# pulling the owner's own public models — no user-auth flow needed on
# Thingiverse's side, so this is exactly the same "paste one static secret"
# shape as youtube_data_api_key. No storage/endpoint changes required; this
# confirms #55/#59's genericness holds for a second key.
# imgur_client_id / imgur_username (#200): public-gallery-only Imgur import
# (account/{username}/submissions), Client-ID auth — same "paste one static
# secret" shape again. imgur_username isn't itself a secret but rides the
# same generic settings mechanism rather than a one-off config path.
KNOWN_SETTINGS = {
    "youtube_data_api_key": "YouTube Data API Key",
    "thingiverse_app_token": "Thingiverse App Token",
    "imgur_client_id": "Imgur Client ID",
    "imgur_username": "Imgur Username",
}


@app.get("/api/settings")
def api_get_settings():
    """Presence-only view of every known setting — never the actual value.
    {"youtube_data_api_key": true} means a key is stored, not what it is.
    This is deliberately the only way the admin pane's UI learns whether a
    setting exists; the real value is never sent to the browser, on this
    route or any other, after it's been saved (see api_set_setting)."""
    return JSONResponse({key: db.has_setting(key) for key in KNOWN_SETTINGS})


@app.post("/api/settings")
def api_set_setting(key: str = Form(...), value: str = Form("")):
    """Saves one named setting (or, given an empty value, clears it). `key`
    must be one of KNOWN_SETTINGS above — the storage layer is generic, but
    this endpoint only accepts keys the app actually knows how to use, so it
    can't become an arbitrary junk-drawer for an unauthenticated LAN app.
    Deliberately returns only the same presence flag GET /api/settings
    reports, never the value it was just given, so the browser can't get the
    real value echoed back to it after a save."""
    if key not in KNOWN_SETTINGS:
        raise HTTPException(status_code=400, detail=f"Unknown setting key: {key!r}")
    db.set_setting(key, value)
    return JSONResponse({key: db.has_setting(key)})


@app.get("/api/audit-log")
def api_get_audit_log(limit: int = 100):
    """Fetch recent audit log entries (most recent first). Returns a list of
    audit log rows, each with method, path, scrubbed form_body, affected_slugs,
    status_code, error_detail, and a human-friendly timestamp."""
    rows = db.list_recent_audit_logs(limit=limit)
    # Add human-friendly timestamp to each row
    result = []
    for row in rows:
        result.append(
            {
                **row,
                "timestamp_friendly": _friendly_datetime(row["timestamp"]),
            }
        )
    return JSONResponse(result)


@app.get("/upload")
def upload_page_redirect():
    # Upload is now a pane on the home page, not its own screen.
    return RedirectResponse("/", status_code=308)


@app.get("/gallery", response_class=HTMLResponse)
def gallery_page_redirect(request: Request):
    # The gallery is now the home page itself — kept as an alias so old
    # links/bookmarks still land somewhere sensible.
    return RedirectResponse("/", status_code=308)


@app.get("/project/{slug}", response_class=HTMLResponse)
def project_detail_page(request: Request, slug: str):
    project = db.get_project(slug)
    if project is None:
        raise HTTPException(status_code=404, detail="not found")
    items = [_to_content_public(r, project_slug=slug) for r in db.list_project_items(project["id"])]
    child_projects = db.list_child_projects(project["id"])
    ancestors = db.list_project_ancestors(project["id"])
    # #156: fetch the writeup document and pass its body to the template
    writeup_body = None
    if project.get("writeup_slug"):
        writeup_doc = db.get_by_slug(project["writeup_slug"])
        if writeup_doc:
            writeup_body = writeup_doc.get("type_metadata", {}).get("body", "")
    return templates.TemplateResponse(
        request, "project_detail.html",
        {
            "project": project,
            "cover_url": _project_cover_url(project.get("cover_slug")),
            "items": items,
            "child_projects": child_projects,
            "ancestors": ancestors,
            "writeup_body": writeup_body,
        },
    )


@app.get("/unfiled", response_class=HTMLResponse)
def unfiled_page(request: Request):
    """Issue #98: full-page version of home.html's compact Unfiled widget,
    with bulk selection/filing tools the widget has no room for. Reuses the
    exact same db.list_unfiled_items()/_to_public() data shape the widget
    already uses, so the gallery-card markup is identical everywhere."""
    unfiled_items = [_to_public(r) for r in db.list_unfiled_items()]
    return templates.TemplateResponse(
        request, "unfiled.html",
        {"unfiled_items": unfiled_items},
    )


@app.get("/gallery/user/{uploader}", response_class=HTMLResponse)
def user_gallery_page(request: Request, uploader: str):
    rows = db.search(uploaded_by=uploader, limit=1000)
    items = [_to_public(r) for r in rows]
    return templates.TemplateResponse(
        request, "user_gallery.html",
        {"uploader": uploader, "uploader_display": uploader, "items": items},
    )


@app.get("/object/{slug}", response_class=HTMLResponse)
def object_detail_page(request: Request, slug: str):
    """Generic detail page for any capture_events row, regardless of
    media_type — image, youtube, document, or anything else. Canonical route
    for what used to be the image-only /image/<slug> page (see the redirect
    below); the template branches on item.media_type/is_file to render the
    right preview (uploaded image, embedded YouTube player, or plain text
    content) instead of assuming every row has an uploaded file.
    """
    row = db.get_by_slug(slug)
    if row is None:
        raise HTTPException(status_code=404, detail="not found")
    item = _to_object_detail(row)
    full_url = str(request.base_url).rstrip("/") + item["url"] if item["is_file"] else None
    full_object_url = str(request.base_url).rstrip("/") + f"/object/{slug}"
    # #52: relations (core/db.py's add_relation/remove_relation, #16) are
    # type-agnostic — a plain slug-to-slug link with no media_type or
    # is_file check on the backend — so this used to gate the Related panel
    # on item["is_file"] was a leftover from before the object-type registry
    # existed (predating #15) that accidentally hid "Add related" for every
    # content-only row (youtube, document posts), not just non-file types.
    # Always computed now so every object type gets the same panel.
    related = [_to_public(r) for r in db.list_related(slug)]
    # #137: breadcrumb navigation — read the from param and build the breadcrumb list
    from_param = request.query_params.get("from")
    breadcrumbs = _build_breadcrumbs(from_param, item["display_name"])
    return templates.TemplateResponse(
        request, "object_detail.html",
        {"item": item, "full_url": full_url, "full_object_url": full_object_url, "related": related, "breadcrumbs": breadcrumbs},
    )


@app.get("/image/{slug}")
def image_detail_redirect(slug: str):
    # /image/<slug> was the original imagerepo-era canonical route (image-only).
    # /object/<slug> replaced it so any bookmarked/hotlinked old URLs still resolve.
    return RedirectResponse(f"/object/{slug}", status_code=308)


@app.get("/account", response_class=HTMLResponse)
def account_page(request: Request):
    return templates.TemplateResponse(request, "account.html", {})


# --- API ---

@app.post("/api/upload")
async def api_upload(
    request: Request,
    background_tasks: BackgroundTasks,
    file: UploadFile = File(...),
    description: str = Form(""),
    tags: str = Form("[]"),
    client: str = Form(""),
    project_id: str = Form(""),
    modified_at: str = Form(""),
):
    # Which Source string a browser upload gets is decided server-side, not
    # by a client-supplied field — the desktop uploader app (see
    # desktop_app/constructicon_uploader/api.py) identifies itself with this
    # header on every request; the web upload drawer sends nothing extra, so
    # its absence is what marks a deliberate one-off drag-drop through the
    # browser UI.
    is_desktop_app = request.headers.get(DESKTOP_APP_CLIENT_HEADER) == DESKTOP_APP_CLIENT_VALUE
    user = db.SOURCE_AUTOMATED_UPLOAD if is_desktop_app else db.SOURCE_MANUAL_UPLOAD
    content = await file.read()
    file_size = len(content)
    try:
        source_modified_at = float(modified_at) / 1000 if modified_at else None
    except ValueError:
        # Same clean-400 contract type_metadata gets in /api/content (#221),
        # rather than a 500 traceback on a garbage timestamp.
        raise HTTPException(status_code=400, detail="modified_at must be a unix-milliseconds number")
    dupe = db.find_duplicate(file.filename, file_size, source_modified_at)
    if dupe is not None:
        # _friendly_datetime, not a raw strftime with %-d/%-I -- those are the
        # platform-specific extensions that helper exists to avoid (#211).
        dupe_date = _friendly_datetime(dupe["timestamp"])
        raise HTTPException(
            status_code=409,
            detail=f"Already uploaded by {dupe['tech']} on {dupe_date} — see /object/{dupe['slug']}",
        )
    # media_type from the uploaded file's extension — the only place this
    # decision has to be extension-based, since that's all /api/upload has
    # to go on. Everything downstream (thumbnail, OCR, badge, delete,
    # backup) dispatches off this media_type via core/object_types.py's
    # registry, not off the extension again.
    media_type = object_types.detect_media_type(file.filename)
    if media_type is None:
        raise HTTPException(status_code=400, detail=f"Unsupported file type: {Path(file.filename).suffix}")
    try:
        slug, stored_filename = storage.save_file(file.filename, content)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    try:
        tag_list = json.loads(tags) if tags else []
    except json.JSONDecodeError:
        tag_list = []
    spec = object_types.get_object_type(media_type)
    db.insert_upload(
        slug, file.filename, stored_filename, user,
        description=description, tags=tag_list,
        client=client or None,
        file_size=file_size, source_modified_at=source_modified_at,
        media_type=media_type,
        ocr_status="pending" if spec.ocr_capable else None,
    )
    # Runs after this response is sent — OCR happens once the upload/tag step
    # is actually done, not as part of what the user is waiting on. The client
    # polls GET /api/image/{slug} to see ocr_status flip from "pending".
    if spec.ocr_capable:
        background_tasks.add_task(ocr.run_ocr, slug)
    elif spec.thumbnail_source == object_types.ThumbnailSource.CAPTURE:
        # A CAPTURE-sourced type with no OCR pass to piggyback a thumbnail
        # render onto (STL today) still needs one generated somewhere —
        # see _ensure_capture_thumbnail above.
        background_tasks.add_task(_ensure_capture_thumbnail, slug)
    # #239: auto-caption suggestion via the local vision model. Its own
    # background task, its own serialization (core/captions.py's
    # CAPTION_LOCK + per-image Ollama restart) — deliberately not folded
    # into OCR's semaphore, it's a different resource (the GPU).
    if captions.should_caption(spec):
        background_tasks.add_task(captions.run_caption, slug)
    _attach_to_project(slug, project_id or None)
    return JSONResponse(_to_public(db.get_by_slug(slug)))


@app.post("/api/content")
async def api_create_content(
    request: Request,
    background_tasks: BackgroundTasks,
    media_type: str | None = Form(None),
    external_url: str = Form(""),
    content_description: str = Form(""),
    content_date: str = Form(""),
    description: str = Form(""),
    tags: str = Form("[]"),
    client: str = Form(""),
    project_id: str = Form(""),
    type_metadata: str | None = Form(None),
):
    """Creates a capture_events row for content with no uploaded file — a
    YouTube link today, a stream/URL capture once a future issue wires up
    its capture_fn (see core/object_types.py). Distinct from /api/upload,
    which is for actual uploaded files and stays UPLOADED_FILE-only.

    Mirrors /api/upload's OCR handling: db.insert_content already stamps
    ocr_status="pending" for any OCR-capable type (see core/db.py), and this
    schedules the same background ocr.run_ocr — which fetches/generates the
    type's thumbnail before running OCR against it (see core/ocr.py).

    type_metadata (#54): an optional JSON object of freeform per-type
    properties (view/like/comment counts, full description, uploading
    channel — see core/object_types.py's "youtube" metadata_fields) to set
    at creation time, so a caller that already has this data (e.g.
    scripts/full_youtube_channel_sync.py, which fetches it from the YouTube
    Data API in the same pass it decides to create the row) doesn't need a
    separate follow-up call the way api_update_image's equivalent field
    does for correcting an EXISTING row.
    media_type auto-classifies from external_url (YouTube vs. a plain web
    page — see object_types.classify_url) when the caller doesn't already
    know which type it wants, matching the upload drawer's generic "paste a
    link" field (#184): the client no longer decides youtube-vs-url itself,
    it just posts the URL and lets the server figure out what it is.
    """
    if media_type is None:
        if not external_url:
            raise HTTPException(status_code=400, detail="media_type or external_url is required")
        media_type = object_types.classify_url(external_url)
    # #194: a generic web page has no title-fetch path the way YouTube does
    # (real title via scripts/full_youtube_channel_sync.py's API call) --
    # without this, content_description stays empty and _to_public's
    # display_name fallback chain (filename/content_description/slug) shows
    # the bare random slug on the page title/breadcrumb, with no visible
    # trace of the URL the owner actually pasted.
    if media_type == "url" and external_url and not content_description:
        content_description = external_url
    spec = object_types.get_object_type(media_type)
    if spec.thumbnail_source == object_types.ThumbnailSource.UPLOADED_FILE:
        raise HTTPException(status_code=400, detail=f"{spec.label} objects require a file upload — use /api/upload")
    is_desktop_app = request.headers.get(DESKTOP_APP_CLIENT_HEADER) == DESKTOP_APP_CLIENT_VALUE
    user = db.SOURCE_AUTOMATED_UPLOAD if is_desktop_app else db.SOURCE_MANUAL_UPLOAD
    try:
        tag_list = json.loads(tags) if tags else []
    except json.JSONDecodeError:
        tag_list = []
    try:
        parsed_type_metadata = json.loads(type_metadata) if type_metadata else None
    except json.JSONDecodeError:
        raise HTTPException(status_code=400, detail="type_metadata must be valid JSON")
    try:
        content_date_epoch = float(content_date) if content_date else None
    except ValueError:
        raise HTTPException(status_code=400, detail="content_date must be a unix-seconds number")
    slug = storage.make_slug()
    db.insert_content(
        slug, user, media_type,
        external_url=external_url or None,
        content_description=content_description or None,
        content_date=content_date_epoch,
        description=description, tags=tag_list,
        client=client or None,
        type_metadata=parsed_type_metadata,
    )
    row = db.get_by_slug(slug)
    if spec.ocr_capable and row["ocr_status"] == "pending":
        background_tasks.add_task(ocr.run_ocr, slug)
    elif spec.thumbnail_source == object_types.ThumbnailSource.CAPTURE:
        background_tasks.add_task(_ensure_capture_thumbnail, slug)
    if captions.should_caption(spec):
        background_tasks.add_task(captions.run_caption, slug)  # #239, see /api/upload
    _attach_to_project(slug, project_id or None)
    return JSONResponse(_to_public(db.get_by_slug(slug)))


@app.post("/api/imgur/import")
def api_imgur_import(background_tasks: BackgroundTasks):
    """Issue #200: the upload drawer's "Import from Imgur" button. Runs
    core.imgur_import.sync_public_gallery() synchronously — a personal
    gallery's submission count is small enough that one request/response
    round trip is fine, no background job needed — then schedules OCR for
    each newly created row the same way /api/content does."""
    try:
        summary = imgur_import.sync_public_gallery()
    except imgur_import.ImgurImportError as e:
        raise HTTPException(status_code=400, detail=str(e))
    spec = object_types.get_object_type("imgur")
    if spec.ocr_capable:
        for slug in summary["slugs"]:
            row = db.get_by_slug(slug)
            if row and row["ocr_status"] == "pending":
                background_tasks.add_task(ocr.run_ocr, slug)
    return JSONResponse(summary)


@app.post("/api/imgur/import-url")
def api_imgur_import_url(background_tasks: BackgroundTasks, url: str = Form(...)):
    """Issue #203: the generic link field's Imgur-aware counterpart to
    /api/content (see _upload_drawer.html's addImgurUrlAndTrack) — one
    pasted Imgur post/album URL, one row, giving the owner exact control
    over what enters Constructicon instead of /api/imgur/import's
    account-wide all-or-nothing pull. Returns the same public-item shape
    /api/content does (so the drawer's OCR-poll logic works unchanged),
    or {"skipped": true} if that item was already imported."""
    try:
        result = imgur_import.import_from_url(url)
    except imgur_import.ImgurImportError as e:
        raise HTTPException(status_code=400, detail=str(e))
    if not result["slug"]:
        return JSONResponse({"skipped": True})
    slug = result["slug"]
    spec = object_types.get_object_type("imgur")
    row = db.get_by_slug(slug)
    if spec.ocr_capable and row["ocr_status"] == "pending":
        background_tasks.add_task(ocr.run_ocr, slug)
    return JSONResponse(_to_public(db.get_by_slug(slug)))


@app.get("/api/image/{slug}")
def api_get_image(request: Request, slug: str):
    row = db.get_by_slug(slug)
    if row is None:
        raise HTTPException(status_code=404, detail="not found")
    return JSONResponse(_to_public(row))


@app.post("/api/image/{slug}/ocr")
def api_retry_ocr(request: Request, slug: str, background_tasks: BackgroundTasks):
    """Force a (re-)run of OCR — for images that never got it, or a lousy
    first pass worth retrying."""
    row = db.get_by_slug(slug)
    if row is None:
        raise HTTPException(status_code=404, detail="not found")
    if row["redacted"]:
        raise HTTPException(status_code=400, detail="File was redacted — there's no image left to OCR")
    spec = object_types.get_object_type(row.get("media_type"))
    if not spec.ocr_capable:
        raise HTTPException(status_code=400, detail=f"OCR isn't available for {spec.label} content")
    db.set_ocr_status(slug, "pending")
    background_tasks.add_task(ocr.run_ocr, slug)
    return JSONResponse(_to_public(db.get_by_slug(slug)))


@app.post("/api/image/{slug}/caption")
def api_retry_caption(request: Request, slug: str, background_tasks: BackgroundTasks):
    """#239: (re-)run the auto-caption suggestion for one object — for rows
    that predate captioning, a failed attempt, or a caption worth another
    roll. Same background/best-effort shape as api_retry_ocr; the detail
    page polls GET /api/image/{slug} for type_metadata.auto_caption_status
    to leave "pending"."""
    row = db.get_by_slug(slug)
    if row is None:
        raise HTTPException(status_code=404, detail="not found")
    if row["redacted"]:
        raise HTTPException(status_code=400, detail="File was redacted — there's no image left to caption")
    spec = object_types.get_object_type(row.get("media_type"))
    if not captions.should_caption(spec):
        raise HTTPException(status_code=400, detail=f"Captioning isn't available for {spec.label} content")
    db.update_content_metadata(slug, type_metadata={captions.STATUS_KEY: "pending"})
    background_tasks.add_task(captions.run_caption, slug)
    return JSONResponse(_to_public(db.get_by_slug(slug)))


@app.get("/api/captions/defaults")
def api_caption_defaults():
    """#239: what the pipeline actually runs with, so the admin pane's
    tuning panel starts from production's real values rather than its own
    copy of them."""
    return JSONResponse({
        "model": captions.OLLAMA_MODEL,
        "prompt": captions.DEFAULT_PROMPT,
        "temperature": captions.DEFAULT_TEMPERATURE,
        "num_predict": captions.DEFAULT_NUM_PREDICT,
        "ollama_up": captions.is_ollama_up(),
        "docker_socket": captions.docker_socket_available(),
    })


@app.post("/api/captions/test")
def api_caption_test(
    slug: str = Form(...),
    temperature: float = Form(captions.DEFAULT_TEMPERATURE),
    num_predict: int = Form(captions.DEFAULT_NUM_PREDICT),
    prompt: str = Form(""),
):
    """#239: the admin pane's live tuning panel — one synchronous model call
    against an existing object's real preview image with the given
    settings, WITHOUT writing anything to the row. Goes through the exact
    same caption_once() cycle as the pipeline (lock + post-call Ollama
    restart), so a tuning run can never overlap a real one and measures
    the same thing production will."""
    row = db.get_by_slug(slug.strip())
    if row is None:
        raise HTTPException(status_code=404, detail="No object with that slug")
    if row["redacted"]:
        raise HTTPException(status_code=400, detail="That object was redacted")
    spec = object_types.get_object_type(row.get("media_type"))
    if not spec.caption_capable:
        raise HTTPException(status_code=400, detail=f"{spec.label} objects aren't caption-capable (see core/object_types)")
    image_path = captions._caption_source_path(row, spec)
    if image_path is None:
        raise HTTPException(status_code=400, detail="No preview image available for that object")
    if not 0.0 <= temperature <= 2.0:
        raise HTTPException(status_code=400, detail="temperature must be between 0 and 2")
    if not 1 <= num_predict <= 1000:
        raise HTTPException(status_code=400, detail="num_predict must be between 1 and 1000")
    result = captions.caption_once(
        image_path,
        prompt=prompt.strip() or None,
        temperature=temperature,
        num_predict=num_predict,
    )
    return JSONResponse({
        **result,
        "slug": row["slug"],
        "media_type": spec.key,
        "thumb_url": f"/f/{row['slug']}/thumb" if _has_thumbnail(row, spec) else None,
        "settings": {"temperature": temperature, "num_predict": num_predict, "prompt": prompt.strip() or captions.DEFAULT_PROMPT},
    })


@app.post("/api/image/{slug}")
def api_update_image(
    request: Request,
    slug: str,
    description: str | None = Form(None),
    tags: str | None = Form(None),
    client: str | None = Form(None),
    display_name: str | None = Form(None),
    icon: str | None = Form(None),
    content_description: str | None = Form(None),
    type_metadata: str | None = Form(None),
):
    # #213 / A5 fix: only parse and pass tags if they were actually provided
    # in the form. Defaults of None mean "don't touch this field", allowing
    # partial updates (e.g. rename-only) without inadvertently wiping tags.
    tag_list = None
    if tags is not None:
        try:
            tag_list = json.loads(tags) if tags else []
        except json.JSONDecodeError:
            tag_list = []
    row = db.update_tags(slug, description=description, tags=tag_list, client=client)
    if row is None:
        raise HTTPException(status_code=404, detail="not found")
    # display_name/icon (#11) — no dedicated UI yet (see #24's "Coming soon
    # (#11)" admin-pane stub), but the field/endpoint exists so a "rename"
    # or "set icon" is at least possible by hand (a form POST here).
    if display_name is not None or icon is not None:
        row = db.rename_object(slug, display_name=display_name, icon=icon)
    # content_description/type_metadata (#54): lets a caller correct a
    # row's title-ish blurb and/or per-type metadata after creation — added
    # for scripts/full_youtube_channel_sync.py's correction pass (site-
    # scraped titles overwritten with the real YouTube Data API title, plus
    # view/like/comment counts and, when applicable, the uploading channel).
    # type_metadata is a JSON object string, MERGED into whatever the row
    # already has (see db.update_content_metadata) rather than replacing it
    # wholesale, so this can't be used to accidentally wipe out a field some
    # other future writer already set.
    if content_description is not None or type_metadata is not None:
        parsed_metadata = None
        if type_metadata is not None:
            try:
                parsed_metadata = json.loads(type_metadata) if type_metadata else {}
            except json.JSONDecodeError:
                raise HTTPException(status_code=400, detail="type_metadata must be valid JSON")
        row = db.update_content_metadata(slug, content_description=content_description, type_metadata=parsed_metadata)
    return JSONResponse(_to_public(row))


@app.post("/api/image/{slug}/redact")
def api_redact_image(request: Request, slug: str):
    """Delete the file only — sensitive content (e.g. a visible password) —
    but keep the metadata for future correlation."""
    row = db.get_by_slug(slug)
    if row is None:
        raise HTTPException(status_code=404, detail="not found")
    if not row.get("stored_filename"):
        raise HTTPException(status_code=400, detail="This row has no uploaded file to redact")
    storage.delete_files(slug, row["stored_filename"])
    updated = db.mark_redacted(slug)
    return JSONResponse(_to_public(updated))


@app.post("/api/image/{slug}/delete")
def api_delete_image(request: Request, slug: str):
    """Full delete — file and metadata both gone, no recovery."""
    row = db.get_by_slug(slug)
    if row is None:
        raise HTTPException(status_code=404, detail="not found")
    if row.get("stored_filename"):
        storage.delete_files(slug, row["stored_filename"])
    db.delete_upload(slug)
    return JSONResponse({"deleted": True})


@app.post("/api/image/{slug}/related")
def api_add_related(request: Request, slug: str, related_slug: str = Form(...)):
    if db.get_by_slug(slug) is None:
        raise HTTPException(status_code=404, detail="not found")
    if db.get_by_slug(related_slug) is None:
        raise HTTPException(status_code=404, detail="related image not found")
    db.add_relation(slug, related_slug)
    return JSONResponse([_to_public(r) for r in db.list_related(slug)])


@app.post("/api/image/{slug}/related/remove")
def api_remove_related(request: Request, slug: str, related_slug: str = Form(...)):
    db.remove_relation(slug, related_slug)
    return JSONResponse([_to_public(r) for r in db.list_related(slug)])


@app.post("/api/image/{slug}/project")
def api_add_object_to_project(request: Request, slug: str, project_id: str = Form(...)):
    """#47: the object detail page's project editor — same membership
    primitive as an upload-time project pick (_attach_to_project, #1), just
    reachable after the fact instead of only at upload time. Unlike
    _attach_to_project's silent-ignore-on-bad-id (fine for a stale value
    riding along with an upload), a bad project_id here is a real error —
    it's the only thing this request is trying to do."""
    if db.get_by_slug(slug) is None:
        raise HTTPException(status_code=404, detail="not found")
    if db.get_project(project_id) is None:
        raise HTTPException(status_code=404, detail="project not found")
    _attach_to_project(slug, project_id)
    return JSONResponse([_to_project_option(p) for p in db.list_projects_for_post(slug)])


@app.post("/api/image/{slug}/project/remove")
def api_remove_object_from_project(request: Request, slug: str, project_id: str = Form(...)):
    """Removes membership only — deliberately leaves the project's linked
    tag (if any) alone, same as removing a manually-curated Related item
    never untags anything either. The tag field is already separately
    editable right above this on the detail page if the user wants it gone
    too."""
    if db.get_by_slug(slug) is None:
        raise HTTPException(status_code=404, detail="not found")
    project = db.get_project(project_id)
    if project is None:
        raise HTTPException(status_code=404, detail="project not found")
    db.remove_item_from_project(project["id"], slug)
    return JSONResponse([_to_project_option(p) for p in db.list_projects_for_post(slug)])


@app.get("/api/image/{slug}/similar")
def api_get_similar(request: Request, slug: str):
    """Auto-detected candidates — visual (perceptual hash) and/or semantic
    (text embedding) — distinct from the manually-curated Related panel.
    Each result carries similarity_reason ("visual"/"text"/"both") and
    similarity_score so the UI can label why it's suggested.
    """
    if db.get_by_slug(slug) is None:
        raise HTTPException(status_code=404, detail="not found")
    matches = similarity.find_similar(slug)
    results = []
    for m in matches:
        row = db.get_by_slug(m["slug"])
        if row is None:
            continue
        item = _to_public(row)
        item["similarity_reason"] = m["reason"]
        item["similarity_score"] = round(m["score"], 3)
        results.append(item)
    return JSONResponse(results)


@app.get("/api/gallery")
def api_gallery(request: Request, query: str = "", client: str = "", per_user: int = 4):
    """Grouped-by-uploader gallery data: each uploader's most recent N items
    plus their real total, queried per-uploader so no single prolific
    uploader's activity can push others out of a global result limit.
    """
    uploaders = db.list_uploaders(query=query or None, client=client or None)
    groups = []
    for u in uploaders:
        items = db.search(query=query or None, client=client or None, uploaded_by=u["uploaded_by"], limit=per_user)
        groups.append({
            # u["uploaded_by"] is already the short group key (see
            # db.list_uploaders) — used both as the display label and as the
            # /gallery/user/<uploader> link target.
            "uploaded_by": u["uploaded_by"],
            "uploaded_by_display": u["uploaded_by"],
            "total": u["total"],
            "items": [_to_public(r) for r in items],
        })
    return JSONResponse(groups)


@app.get("/downloads/constructicon-uploader-source.zip")
def download_desktop_app_source(request: Request):
    """Source only, not a built .app — py2app has to run on an actual Mac,
    which this server can't do (it's the same Linux/Docker box everything
    else runs on). Zipped fresh from disk on every request rather than a
    pre-built artifact, so it's never out of sync with what's actually in
    the repo."""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for path in sorted(DESKTOP_APP_DIR.rglob("*")):
            if not path.is_file() or "__pycache__" in path.parts:
                continue
            arcname = Path("constructicon-uploader-source") / path.relative_to(DESKTOP_APP_DIR)
            zf.write(path, arcname=str(arcname))
    return Response(
        content=buf.getvalue(),
        media_type="application/zip",
        headers={"Content-Disposition": "attachment; filename=constructicon-uploader-source.zip"},
    )


@app.get("/api/account/desktop-app-build")
def api_get_desktop_app_build(request: Request):
    if not DESKTOP_APP_BUILD_PATH.exists():
        return JSONResponse({"exists": False})
    stat = DESKTOP_APP_BUILD_PATH.stat()
    return JSONResponse({"exists": True, "size": stat.st_size, "uploaded_at": stat.st_mtime})


@app.post("/api/account/desktop-app-build")
async def api_upload_desktop_app_build(request: Request, file: UploadFile = File(...)):
    """A tech who's built the app locally (py2app has to run on an actual
    Mac — this server can't build one itself) uploads the resulting zip
    here so everyone else can just download a working binary instead of
    building their own. No versioning: whoever uploads last is what
    everyone gets next."""
    if not file.filename.lower().endswith(".zip"):
        raise HTTPException(status_code=400, detail="Expected a .zip file (zip the built .app, don't upload it unzipped)")
    content = await file.read()
    if not zipfile.is_zipfile(io.BytesIO(content)):
        raise HTTPException(status_code=400, detail="That file isn't a valid zip archive")
    DESKTOP_APP_BUILD_DIR.mkdir(parents=True, exist_ok=True)
    DESKTOP_APP_BUILD_PATH.write_bytes(content)
    stat = DESKTOP_APP_BUILD_PATH.stat()
    return JSONResponse({"exists": True, "size": stat.st_size, "uploaded_at": stat.st_mtime})


@app.get("/downloads/constructicon-uploader.zip")
def download_desktop_app_build(request: Request):
    if not DESKTOP_APP_BUILD_PATH.exists():
        raise HTTPException(
            status_code=404,
            detail="No built app has been uploaded yet — download the source zip and build it with Build.command, "
                   "or ask whoever last built one to upload it from account settings.",
        )
    return FileResponse(DESKTOP_APP_BUILD_PATH, media_type="application/zip", filename="Constructicon Uploader.zip")


@app.get("/api/clients")
def api_clients(request: Request):
    return JSONResponse(db.list_clients())


def _to_project_option(project):
    """Slim shape for the upload drawer's Project dropdown — just enough to
    populate a <select> and let the client hand project_id back on upload.
    parent_id (#133) is included too — project_detail.html's parent-project
    selector reuses this same endpoint and needs it client-side to exclude
    a project's own descendants from its own "choose a parent" dropdown
    (the backend's cycle check is the real guard; this just keeps the
    dropdown itself from offering a choice guaranteed to be rejected).
    writeup_slug (#156) is also included so the backfill script can see
    which projects already have writeups."""
    return {"id": project["id"], "slug": project["slug"], "title": project["title"], "status": project["status"], "parent_id": project.get("parent_id"), "writeup_slug": project.get("writeup_slug")}


@app.get("/api/projects")
def api_projects(request: Request):
    """Populates the upload drawer's Project dropdown (#1) — every project,
    most-recently-updated first, same ordering list_projects() already uses
    for the home page's Projects column."""
    return JSONResponse([_to_project_option(p) for p in db.list_projects()])


def _create_writeup_for_project(project):
    """#156's auto-writeup logic, factored out so every project-creation
    path gets it -- #191 found that /api/projects/from-selection and
    /api/projects/from-related each called db.create_project() directly and
    silently never got a writeup at all, since this was only ever inlined
    into api_create_project below. Creates a blank document-type
    capture_event as the project's write-up, adds it to project_items, and
    sets the project's writeup_slug to that document's slug. Returns the
    project row refreshed with its new writeup_slug."""
    writeup_slug = storage.make_slug()
    db.insert_content(
        slug=writeup_slug,
        uploaded_by=db.SOURCE_AUTHORED,
        media_type="document",
        content_description=f"{project['title']} — Write-up",
        type_metadata={"body": ""},
    )
    db.add_item_to_project(project["id"], writeup_slug)
    # Tag the write-up with the project's linked tag, the same way
    # _attach_to_project does for every other member (#219) -- otherwise the
    # write-up is invisible to tag browsing (/?tag=<project>). Deliberately
    # *not* routed through _attach_to_project itself, which would also make
    # a blank write-up the project's auto-cover.
    if project.get("tag_id"):
        db.attach_tags(writeup_slug, [project["tag_id"]])
    db.update_project(project["id"], writeup_slug=writeup_slug)
    return db.get_project(project["id"])


@app.post("/api/projects")
def api_create_project(request: Request, title: str = Form(...), parent_id: str = Form(None)):
    """Creates a project from the upload drawer's "+ New project..." flow
    (#1) — distinct from scripts/seed_example_projects.py's one-off seeding,
    this is the first real UI-driven way to make a project.

    Also creates (or reuses) a root-level blog_tags row with the same name
    and links it via projects.tag_id, so every object later tagged to this
    project also becomes reachable through the ordinary tag-based browsing
    the rest of the site already has (see core/db.py's create_project
    docstring and README's "tied to the site tags" note) — not a parallel
    system, just handing the existing tag tree a project-shaped entry point.

    parent_id (#133) optionally sets this project as a child of another project.

    Also auto-creates a document-type capture_event (#156) as the project's
    write-up, adds it to project_items, and sets the project's writeup_slug
    to that document's slug.
    """
    title = title.strip()
    if not title:
        raise HTTPException(status_code=400, detail="Project name can't be empty")

    parent_id_int = None
    if parent_id:
        try:
            parent_id_int = int(parent_id)
            if db.get_project(parent_id_int) is None:
                raise HTTPException(status_code=400, detail="Parent project not found")
        except ValueError:
            raise HTTPException(status_code=400, detail="Invalid parent_id")

    tag = db.get_or_create_tag(title, parent_id=None)
    project = db.create_project(title, tag_id=tag["id"], parent_id=parent_id_int)
    project = _create_writeup_for_project(project)
    return JSONResponse(_to_project_option(project))


@app.post("/api/projects/from-selection")
def api_create_project_from_selection(slugs: list[str] = Form(...), title: str = Form(...)):
    """Issue #98: "turn this selection into a project" — the Unfiled page's
    bulk-tag follow-up prompt, and usable standalone. Deliberately operates
    on the exact slugs passed in, not a tag-name lookup: this app has two
    separate, unlinked tag systems (the flat per-object `tags` array bulk
    tagging above writes to, vs. the hierarchical blog_tags/post_tags tree
    /api/tags and list_posts_for_tag walk) — a tag-name lookup here would
    silently miss items that were only ever bulk-tagged the flat way.

    Registered ahead of /api/projects/{project_id} below on purpose — FastAPI
    matches routes in registration order, and that dynamic path param route
    would otherwise swallow this literal path (project_id="from-selection"),
    404ing instead of ever reaching this handler. Same for from-related."""
    title = title.strip()
    if not title:
        raise HTTPException(status_code=400, detail="Project name can't be empty")
    tag = db.get_or_create_tag(title, parent_id=None)
    project = db.create_project(title, tag_id=tag["id"])
    project = _create_writeup_for_project(project)
    for slug in slugs:
        if db.get_by_slug(slug) is not None:
            _attach_to_project(slug, project["id"])
    return JSONResponse(_to_project_option(project))


@app.post("/api/projects/from-related")
def api_create_project_from_related(slug: str = Form(...), title: str = Form(...)):
    """Issue #98: "turn this object + its related items into a project" —
    surfaced on the object detail page's Related panel. See from-selection
    above for why this is registered ahead of /api/projects/{project_id}."""
    title = title.strip()
    if not title:
        raise HTTPException(status_code=400, detail="Project name can't be empty")
    if db.get_by_slug(slug) is None:
        raise HTTPException(status_code=404, detail="not found")
    tag = db.get_or_create_tag(title, parent_id=None)
    project = db.create_project(title, tag_id=tag["id"])
    project = _create_writeup_for_project(project)
    _attach_to_project(slug, project["id"])
    for related in db.list_related(slug):
        _attach_to_project(related["slug"], project["id"])
    return JSONResponse(_to_project_option(project))


@app.post("/api/projects/{project_id}")
def api_update_project(
    request: Request,
    project_id: str,
    title: str = Form(None),
    description: str = Form(None),
    cover_slug: str = Form(None),
    status: str = Form(None),
    parent_id: str = Form(None),
    writeup_slug: str = Form(None),
):
    """Updates a project's properties (issue #103). Allows setting any
    combination of title, description, cover_slug (slug of an attached item
    to use as the cover image), and status. Only overwrites fields that were
    passed; omitted fields are left unchanged. Returns the updated project
    in _to_project_option shape (same as the list endpoint).

    parent_id (#133) can be set to create/remove a parent-child relationship.
    Pass empty string to remove a parent, or a project ID to set one.

    writeup_slug (#156) can be set to point to a document-type item as the
    project's write-up. Pass empty string to remove a writeup."""
    project = db.get_project(project_id)
    if project is None:
        raise HTTPException(status_code=404, detail="Project not found")

    parent_id_value = ...  # "..." means don't update parent_id
    if parent_id is not None:
        parent_id_value = None
        if parent_id:
            try:
                parent_id_int = int(parent_id)
                # Compare against the resolved row's id, not the path
                # param: project_id may be a slug (db.get_project accepts
                # either), and int("some-slug") would land in the
                # except ValueError below as a bogus "Invalid parent_id"
                # (#217).
                if parent_id_int == project["id"]:
                    raise HTTPException(status_code=400, detail="A project cannot be its own parent")
                if db.get_project(parent_id_int) is None:
                    raise HTTPException(status_code=400, detail="Parent project not found")
                parent_id_value = parent_id_int
            except ValueError:
                raise HTTPException(status_code=400, detail="Invalid parent_id")

    writeup_slug_value = ...  # "..." means don't update writeup_slug
    if writeup_slug is not None:
        writeup_slug_value = writeup_slug if writeup_slug else None

    try:
        updated = db.update_project(
            project_id,
            title=title,
            description=description,
            cover_slug=cover_slug,
            status=status,
            parent_id=parent_id_value,
            writeup_slug=writeup_slug_value,
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))

    return JSONResponse(updated or {})


@app.get("/api/projects/{project_id}/export.zip")
def api_export_project(project_id: str):
    """Export a project as a zip file containing the manifest and all uploaded
    files. The zip contains:
    - manifest.json: Project metadata and item descriptions
    - files/: Directory with uploaded files for each item

    Useful for offline review or agent analysis without repeated API calls."""
    try:
        from core import project_export
        zip_bytes = project_export.export_project(project_id)
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Export failed: {e}")

    # Get project name for the download filename
    try:
        project = db.get_project(project_id)
        filename = f"{project['slug']}-export.zip" if project else "project-export.zip"
    except Exception:
        filename = "project-export.zip"

    return Response(
        content=zip_bytes,
        media_type="application/zip",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


def _attach_to_project(slug, project_id):
    """Shared by /api/upload and /api/content: adds the new row to the given
    project's curated item list (so it shows up on the project's own detail
    page) and, if that project has a linked tag (see api_create_project /
    core/db.py's create_project), also tags the row with it — the "tied to
    the site tags" half of #1, so the object surfaces through tag-based
    browsing too, not just the project page. A project_id that doesn't
    resolve to a real project (bad/stale value) is silently ignored rather
    than failing the whole upload over a cosmetic mismatch.

    Also auto-sets the project's cover_slug to this item's slug if the
    project currently has no cover (issue #103) — fires only once per
    project, on the first item it receives."""
    if not project_id:
        return
    project = db.get_project(project_id)
    if project is None:
        return
    db.add_item_to_project(project["id"], slug)
    if project.get("tag_id"):
        db.attach_tags(slug, [project["tag_id"]])
    # Auto-set cover to first item if project has no cover yet
    if not project.get("cover_slug"):
        db.update_project(project["id"], cover_slug=slug)


@app.post("/api/bulk/add-to-project")
def api_bulk_add_to_project(slugs: list[str] = Form(...), project_id: str = Form(...)):
    """Issue #98: the Unfiled page's bulk "add to project" action. Same
    per-slug primitive as a single upload's project pick (_attach_to_project)
    — a bad/stale project_id is silently a no-op for every slug, same as the
    single-object path, rather than partially failing the batch."""
    count = 0
    for slug in slugs:
        if db.get_by_slug(slug) is not None:
            _attach_to_project(slug, project_id)
            count += 1
    return JSONResponse({"count": count})


@app.post("/api/bulk/attach-tags")
def api_bulk_attach_tags(slugs: list[str] = Form(...), tag_names: list[str] = Form(...)):
    """Issue #98: the Unfiled page's bulk tagging action. db.update_tags
    fully replaces a row's tags/description/client with whatever's passed —
    the single-object edit form gets away with this because it always
    resends the complete current description+tags together. A bulk action
    can't do that: it must read each row first and merge, or it would wipe
    out every selected item's existing tags and description. tag_names are
    unioned onto each row's own existing tags (deduped); description/client
    are passed back unchanged from the row itself."""
    tag_names = [t.strip() for t in tag_names if t.strip()]
    if not tag_names:
        return JSONResponse({"count": 0})
    count = 0
    for slug in slugs:
        row = db.get_by_slug(slug)
        if row is None:
            continue
        merged_tags = sorted(set(row["tags"]) | set(tag_names))
        db.update_tags(slug, description=row["description"], tags=merged_tags, client=row.get("client"))
        count += 1
    return JSONResponse({"count": count})


@app.get("/api/tags")
def api_tags(request: Request):
    """Return the tag tree flattened with breadcrumb paths for autocomplete.
    Each tag includes its full path from root (e.g. "Parent > Child > Leaf")
    for display in suggestions."""
    def flatten_with_paths(nodes, path=""):
        """Recursively flatten tree nodes with breadcrumb paths."""
        flat = []
        for node in nodes:
            # Build the breadcrumb path for this node
            node_path = f"{path} > {node['name']}" if path else node['name']
            flat.append({
                "id": node["id"],
                "name": node["name"],
                "slug": node["slug"],
                "path": node_path,  # Full breadcrumb for display
                "parent_id": node["parent_id"],
            })
            # Recursively add children
            if node.get("children"):
                flat.extend(flatten_with_paths(node["children"], node_path))
        return flat

    tag_tree = db.list_tag_tree()
    flat_tags = flatten_with_paths(tag_tree)
    return JSONResponse(flat_tags)


@app.get("/api/search")
def api_search(request: Request, query: str = "", tags: str = "", client: str = ""):
    tag_list = [t for t in tags.split(",") if t] or None
    results = db.search(query=query or None, tags=tag_list, client=client or None)
    return JSONResponse([_to_public(r) for r in results])


# --- Public hotlink (no auth — Hudu/Slack need to fetch this directly) ---

@app.get("/f/{slug}")
def get_file(slug: str):
    row = db.get_by_slug(slug)
    if row is None:
        raise HTTPException(status_code=404, detail="not found")
    if row["redacted"]:
        raise HTTPException(status_code=410, detail="file was redacted (sensitive content) — metadata is still on the image page")
    if not row.get("stored_filename"):
        # Content-only row (youtube/imgur/url/document — see core/db.py's
        # insert_content): there is no local file to serve. Without this
        # guard storage.path_for(None) raises TypeError and the route 500s
        # (#211), even though _to_public advertises /f/<slug> for every row.
        raise HTTPException(status_code=404, detail="this object has no uploaded file — see its /object page")
    path = storage.path_for(row["stored_filename"])
    if not path.exists():
        raise HTTPException(status_code=404, detail="file missing on disk")
    return FileResponse(path, filename=row["filename"])


@app.get("/f/{slug}/thumb")
def get_thumbnail(slug: str):
    row = db.get_by_slug(slug)
    if row is None:
        raise HTTPException(status_code=404, detail="not found")
    if row["redacted"]:
        raise HTTPException(status_code=410, detail="file was redacted (sensitive content)")
    if not storage.thumb_path_for(slug).exists() and not row.get("stored_filename"):
        # Content-only row (youtube and friends — see core/db.py's
        # insert_content) whose thumbnail hasn't been fetched/captured yet,
        # e.g. the background OCR pass hasn't run or its fetch failed
        # transiently. Try once, synchronously, so a first page load isn't
        # stuck with a permanently broken thumbnail just because of timing.
        thumbnails.ensure_thumbnail(row)
    path = storage.thumb_path_or_original(slug, row["stored_filename"])
    if not path.exists():
        raise HTTPException(status_code=404, detail="file missing on disk")
    return FileResponse(path)
