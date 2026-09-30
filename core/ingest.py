"""#448: One unified ingest pipeline for all entry points (web upload/content + MCP tools).

Every upload/content-creation route — /api/upload, /api/content, constructicon_upload,
constructicon_import, constructicon_add_content — flows through this single module.
Entry points handle only transport (HTTP form parsing, MCP argument unpacking, response
serialization); the ingest logic itself lives here, so it's consistent across all of them.

All background work (OCR, thumbnail generation, captions, auto-matching) is delegated to
the caller's own task-dispatch mechanism (FastAPI's BackgroundTasks, threading.Thread, etc.)
via a `run_background` callable — the ingest module never spawns threads or calls the
scheduler itself.
"""

import json
from dataclasses import dataclass
from pathlib import Path

from . import automatch, captions, db, embedded_metadata, object_types, ocr, storage, thumbnails


@dataclass
class IngestResult:
    """Result of an ingest operation.

    - row: The created/found row dict, or None if error/duplicate (check duplicate flag).
    - duplicate: True if this is an existing row (file already uploaded).
    - error: Human-readable error message, or None if successful.
    - error_kind: Category of error — one of "too_large", "unsupported", "invalid", or None.
    """
    row: dict | None = None
    duplicate: bool = False
    error: str | None = None
    error_kind: str | None = None


def attach_to_project(slug, project_id):
    """Shared by /api/upload and /api/content: adds the new row to the given
    project's curated item list (so it shows up on the project's own detail
    page) and, if that project has a linked tag (see api_create_project /
    core/db.py's create_project), also tags the row with it — the "tied to
    the site tags" half of #1, so the object surfaces through tag-based
    browsing too, not just the project page. A project_id that doesn't
    resolve to a real project (bad/stale value) is silently ignored rather
    than failing the whole upload over a cosmetic mismatch.

    #274: also merges the tag's own name into the row's free-text `tags`
    column (db.add_tags — same merge path a person typing a tag by hand
    goes through), not just post_tags. Before this, a project-linked tag
    surfaced the item in tag-tree browsing but never showed up as a chip
    in the item's own TAGS box on its detail page — an inconsistent, easy
    to miss picture of "what tags does this item actually have." Once
    merged in this way it's indistinguishable from a typed tag (by
    design, per the owner's call on #274) — removing it later from the
    free-text box fully detaches it, independent of project membership,
    same as any other typed tag.

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
        tag = db.get_tag(project["tag_id"])
        if tag:
            db.add_tags(slug, [tag["name"]])
    # Auto-set cover to first item if project has no cover yet
    if not project.get("cover_slug"):
        db.update_project(project["id"], cover_slug=slug)


def auto_match(slug, texts):
    """#240 glue between core/automatch.py (which decides and applies tags)
    and attach_to_project (which owns project side effects: linked tag,
    first-item cover). Best-effort, same discipline as the OCR/caption
    background steps: a matching bug must never fail an upload that has
    already been stored."""
    try:
        already_in = [p["id"] for p in db.list_projects_for_post(slug)]
        result = automatch.apply_to_upload(slug, texts, exclude_project_ids=already_in)
        if result["project"]:
            attach_to_project(slug, result["project"]["id"])
        if result["tags"] or result["project"] or result["pending_id"]:
            print(
                f"automatch {slug}: tags={result['tags']} "
                f"project={result['project']['title'] if result['project'] else None} "
                f"pending={result['pending_id']} ({len(result['candidates'])} candidates)",
                flush=True,
            )
    except Exception as e:
        print(f"automatch failed for {slug}: {e!r}", flush=True)


def ensure_capture_thumbnail(slug):
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


def post_insert(slug, spec, run_background):
    """Run all post-insert background tasks (OCR, thumbnail generation, captions).

    This consolidates the logic that was duplicated in /api/upload and /api/content.
    Carries over the OCR/CAPTURE/#239 comments from their original locations.

    Args:
        slug: The newly-inserted row's slug
        spec: The ObjectTypeSpec for this row's media_type
        run_background: Callable to schedule background work (e.g., background_tasks.add_task)
    """
    row = db.get_by_slug(slug)
    # Runs after this response is sent — OCR happens once the upload/tag step
    # is actually done, not as part of what the user is waiting on. The client
    # polls GET /api/image/{slug} to see ocr_status flip from "pending".
    if spec.ocr_capable and row["ocr_status"] == "pending":
        run_background(ocr.run_ocr, slug)
    elif spec.thumbnail_source == object_types.ThumbnailSource.CAPTURE:
        # A CAPTURE-sourced type with no OCR pass to piggyback a thumbnail
        # render onto (STL today) still needs one generated somewhere —
        # see ensure_capture_thumbnail above.
        run_background(ensure_capture_thumbnail, slug)
    # #239: auto-caption suggestion via the local vision model. Its own
    # background task, its own serialization (core/captions.py's
    # CAPTION_LOCK + per-image Ollama restart) — deliberately not folded
    # into OCR's semaphore, it's a different resource (the GPU).
    if captions.should_caption(spec):
        run_background(captions.run_caption, slug)


def ingest_file(
    fileobj,
    filename,
    *,
    size,
    source,
    run_background,
    description="",
    tags=None,
    client=None,
    source_modified_at=None,
    project_id=None,
    folder_name="",
) -> IngestResult:
    """Main entry point for file-based uploads.

    Steps through validation, deduplication, storage, database insertion, and
    background task scheduling in a consistent order. Used by /api/upload (via
    FastAPI's run_in_threadpool), constructicon_upload, and constructicon_import.

    Args:
        fileobj: File-like object (file.file from UploadFile, or io.BytesIO, or open file)
        filename: Original filename (used for media_type detection and duplicate check)
        size: File size in bytes
        source: Source string (db.SOURCE_MANUAL_UPLOAD, db.SOURCE_AUTOMATED_UPLOAD, etc.)
        run_background: Callable to schedule background work
        description: Metadata description
        tags: List of tag names (or None)
        client: Optional client identifier
        source_modified_at: Source file's mtime (unix seconds) for duplicate detection
        project_id: Optional project to attach to
        folder_name: Optional folder name (for folder-drop uploads)

    Returns:
        IngestResult with row (newly created or duplicate) or error details
    """
    # Early size check
    if size > storage.MAX_BYTES:
        return IngestResult(
            error=f"File exceeds {storage.MAX_MB}MB limit",
            error_kind="too_large"
        )

    # Duplicate check
    dupe = db.find_duplicate(filename, size, source_modified_at)
    if dupe is not None:
        return IngestResult(row=dupe, duplicate=True)

    # Media type detection
    media_type = object_types.detect_media_type(filename)
    if media_type is None:
        return IngestResult(
            error=f"Unsupported file type: {Path(filename).suffix}",
            error_kind="unsupported"
        )

    spec = object_types.get_object_type(media_type)

    # Save file to storage
    try:
        slug, stored_filename, _ = storage.save_stream(filename, fileobj)
    except ValueError as e:
        return IngestResult(
            error=str(e),
            error_kind="too_large"
        )

    # Insert into database
    db.insert_upload(
        slug, filename, stored_filename, source,
        description=description,
        tags=tags or [],
        client=client or None,
        file_size=size,
        source_modified_at=source_modified_at,
        media_type=media_type,
        ocr_status="pending" if spec.ocr_capable else None,
    )

    # #255: seed content_description/display_name/type_metadata from the
    # file's own tags (an MP3's ID3 title/artist/album/...). Synchronous
    # and before the response on purpose: one ffprobe header read, not an
    # OCR/caption-sized job, and the JSON returned below then already
    # carries the title for the upload drawer's new card. Dispatches off
    # spec.embedded_metadata_fn — a type without one is a no-op — and only
    # ever fills fields the row doesn't have yet; see core/embedded_metadata.py.
    embedded_metadata.fill_missing(slug)

    # Background tasks
    post_insert(slug, spec, run_background)

    # Project and auto-matching
    attach_to_project(slug, project_id or None)
    # #240: name-based auto-tag / auto-project, AFTER the explicit project
    # pick above so an already-chosen project is excluded from the
    # candidate set rather than re-asked about. folder_name is the dropped
    # top-level folder for a folder-drop upload (#134), empty otherwise.
    auto_match(slug, [Path(filename).stem, folder_name])

    return IngestResult(row=db.get_by_slug(slug))


def ingest_content(
    *,
    source,
    run_background,
    media_type=None,
    external_url=None,
    content_description=None,
    content_date=None,
    description="",
    tags=None,
    client=None,
    type_metadata=None,
    project_id=None,
) -> IngestResult:
    """Main entry point for content-only items (no file upload).

    Used by /api/content and constructicon_add_content. Handles YouTube links,
    plain URLs, and other content that lives outside local storage.

    Args:
        source: Source string (db.SOURCE_MANUAL_UPLOAD, db.SOURCE_AUTOMATED_UPLOAD, etc.)
        run_background: Callable to schedule background work
        media_type: Content media type (required unless external_url provided for auto-classify)
        external_url: External URL (for YouTube, web pages, etc.)
        content_description: Description of the content itself
        content_date: Content's real-world date (unix seconds)
        description: Metadata description
        tags: List of tag names (or None)
        client: Optional client identifier
        type_metadata: Optional freeform per-type properties (dict)
        project_id: Optional project to attach to

    Returns:
        IngestResult with row or error details
    """
    # Determine media type
    if media_type is None:
        if not external_url:
            return IngestResult(
                error="media_type or external_url is required",
                error_kind="invalid"
            )
        media_type = object_types.classify_url(external_url)

    # Validate media_type is registered
    try:
        spec = object_types.get_object_type(media_type)
    except (KeyError, AttributeError):
        return IngestResult(
            error=f"Unknown media_type: {media_type}",
            error_kind="invalid"
        )

    # #194: a generic web page has no title-fetch path the way YouTube does
    # (real title via scripts/full_youtube_channel_sync.py's API call) --
    # without this, content_description stays empty and _to_public's
    # display_name fallback chain (filename/content_description/slug) shows
    # the bare random slug on the page title/breadcrumb, with no visible
    # trace of the URL the owner actually pasted.
    if media_type == "url" and external_url and not content_description:
        content_description = external_url

    # Validate that this type doesn't require a file upload
    if spec.thumbnail_source == object_types.ThumbnailSource.UPLOADED_FILE:
        return IngestResult(
            error=f"{spec.label} objects require a file upload — use /api/upload",
            error_kind="invalid"
        )

    # Create slug and insert
    slug = storage.make_slug()
    db.insert_content(
        slug, source, media_type,
        external_url=external_url or None,
        content_description=content_description or None,
        content_date=content_date,
        description=description,
        tags=tags or [],
        client=client or None,
        type_metadata=type_metadata,
    )

    # Background tasks
    post_insert(slug, spec, run_background)

    # Project attachment
    attach_to_project(slug, project_id or None)

    return IngestResult(row=db.get_by_slug(slug))
