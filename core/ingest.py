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

import threading
from dataclasses import dataclass
from pathlib import Path

from . import actor as actor_ctx, automatch, captions, db, embedded_metadata, object_types, ocr, storage, thumbnails


@dataclass
class IngestResult:
    """Result of an ingest operation.

    - row: The created/found row dict, or None if error/duplicate (check duplicate flag).
    - duplicate: True if this is an existing row (file already uploaded).
    - error: Human-readable error message, or None if successful.
    - error_kind: Category of error — one of "too_large", "unsupported", "invalid", "rejected", or None.
    - pending_decision_id: #448: if action is "needs_decision", the id of the pending_decisions row queued.
    """
    row: dict | None = None
    duplicate: bool = False
    error: str | None = None
    error_kind: str | None = None
    pending_decision_id: int | None = None


def _apply_pre_store(spec, candidate):
    """#448: Helper to invoke pre_store_fn if present, or return accept().

    Args:
        spec: ObjectTypeSpec for this item's media_type.
        candidate: IngestCandidate with file/content details.

    Returns:
        PreStore decision.
    """
    if spec.pre_store_fn:
        return spec.pre_store_fn(candidate)
    return object_types.PreStore.accept()


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
    # #477: a new file whose name matches an existing item (same stem, other rev/date suffix)
    # queues a "does this replace ...?" question. Only asks; never links by itself.
    try:
        from . import revisions
        asked = revisions.queue_replace_question(slug)
        if asked:
            print(f"revisions {slug}: queued replace question #{asked}", flush=True)
    except Exception as e:
        print(f"revision check failed for {slug}: {e!r}", flush=True)


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


def run_in_thread(fn, *args):
    """Run fn(*args) on a background daemon thread. The `run_background` for callers with no
    FastAPI BackgroundTasks (retype from a decision / type action, #563): OCR, thumbnail and
    captions all run for real, and captions.run_caption still takes CAPTION_LOCK and honours
    the circuit breaker itself, exactly as on upload. #560: carries the caller's actor context
    into the thread (ContextVars don't cross threading.Thread on their own)."""
    actor_ctx.spawn(fn, *args)


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

    # Media type detection (extension-only check first, no path yet)
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

    # Now with the path available, detect media_type again (may sniff the content)
    path = storage.path_for(stored_filename)
    media_type_sniffed = object_types.detect_media_type(filename, path)

    # If sniffing failed to find a type, the file isn't supported
    if media_type_sniffed is None:
        path.unlink(missing_ok=True)
        return IngestResult(
            error=f"Unsupported file type: {Path(filename).suffix}",
            error_kind="unsupported"
        )

    # Use sniffed type if different
    if media_type_sniffed != media_type:
        media_type = media_type_sniffed
        spec = object_types.get_object_type(media_type)

    # #448: pre-store hook — allows the type to reject, skip, or defer
    candidate = object_types.IngestCandidate(
        filename=filename,
        path=path,
        media_type=media_type,
        source=source,
    )
    decision = _apply_pre_store(spec, candidate)

    if decision.action == "reject":
        path.unlink(missing_ok=True)
        return IngestResult(
            error=decision.reason,
            error_kind="rejected"
        )

    if decision.action == "metadata_only":
        path.unlink(missing_ok=True)
        db.insert_upload(
            slug, filename, None, source,
            description=description,
            tags=tags or [],
            client=client or None,
            file_size=size,
            source_modified_at=source_modified_at,
            media_type=media_type,
            type_metadata=decision.type_metadata,
        )
        attach_to_project(slug, project_id or None)
        auto_match(slug, [Path(filename).stem, folder_name])
        return IngestResult(row=db.get_by_slug(slug))

    if decision.action == "needs_decision":
        # Provisional type must be registered
        provisional = decision.decision["provisional_type"]
        if provisional not in object_types.OBJECT_TYPES:
            path.unlink(missing_ok=True)
            raise object_types.ObjectTypeContractError(
                f"pre_store_fn returned needs_decision with unregistered provisional_type: {provisional}"
            )

        # The row lives as the provisional type until the owner answers, so
        # every downstream step (OCR/thumbnail/captions) follows that type,
        # not the one that was detected.
        provisional_spec = object_types.get_object_type(provisional)
        db.insert_upload(
            slug, filename, stored_filename, source,
            description=description,
            tags=tags or [],
            client=client or None,
            file_size=size,
            source_modified_at=source_modified_at,
            media_type=provisional,
            ocr_status="pending" if provisional_spec.ocr_capable else None,
        )

        # #448: replaces storage.save_stream's old extension-based save-time
        # thumbnail; synchronous on purpose so the upload response's thumb_url
        # is ready immediately, same as before #448.
        if provisional_spec.thumbnail_source == object_types.ThumbnailSource.UPLOADED_FILE:
            thumbnails.ensure_thumbnail(db.get_by_slug(slug))

        embedded_metadata.fill_missing(slug)
        post_insert(slug, provisional_spec, run_background)
        attach_to_project(slug, project_id or None)
        auto_match(slug, [Path(filename).stem, folder_name])

        # Queue the pending decision; detected_type lets the question say
        # what the sniffer thought it was.
        decision_id = db.add_pending_decision(
            "retype", slug, {**decision.decision, "detected_type": media_type}
        )
        return IngestResult(row=db.get_by_slug(slug), pending_decision_id=decision_id)

    # action == "accept" — the normal path
    # Apply row_overrides (only allowed keys: content_description, content_date, type_metadata)
    insert_kwargs = {
        "description": description,
        "tags": tags or [],
        "client": client or None,
        "file_size": size,
        "source_modified_at": source_modified_at,
        "media_type": media_type,
        "ocr_status": "pending" if spec.ocr_capable else None,
    }

    for key, value in decision.row_overrides.items():
        if key in ("content_description", "content_date", "type_metadata"):
            insert_kwargs[key] = value
        else:
            print(f"Ignoring unrecognized row_override key: {key}", flush=True)

    db.insert_upload(
        slug, filename, stored_filename, source,
        **insert_kwargs
    )

    # #448: replaces storage.save_stream's old extension-based save-time
    # thumbnail; synchronous on purpose so the upload response's thumb_url
    # is ready immediately, same as before #448.
    if spec.thumbnail_source == object_types.ThumbnailSource.UPLOADED_FILE:
        thumbnails.ensure_thumbnail(db.get_by_slug(slug))

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

    # #448: validate against the registry. get_object_type() never raises --
    # it falls back to DEFAULT_SPEC -- so a typo'd media_type used to create
    # a silently broken "unknown" row.
    if media_type not in object_types.OBJECT_TYPES:
        return IngestResult(
            error=f"Unknown media_type: {media_type}",
            error_kind="invalid"
        )
    spec = object_types.get_object_type(media_type)

    # Validate that this type doesn't require a file upload
    if spec.thumbnail_source == object_types.ThumbnailSource.UPLOADED_FILE:
        return IngestResult(
            # Callers append their own "use X instead" hint (web vs MCP).
            error=f"{spec.label} objects require a file upload",
            error_kind="invalid"
        )

    # #448: pre-store hook — for content, reject/metadata_only/needs_decision are type-file bugs
    candidate = object_types.IngestCandidate(
        filename=None,
        path=None,
        media_type=media_type,
        source=source,
        external_url=external_url,
        content_description=content_description,
        type_metadata=type_metadata,
    )
    decision = _apply_pre_store(spec, candidate)

    if decision.action != "accept":
        raise object_types.ObjectTypeContractError(
            f"Type {spec.key}'s pre_store_fn returned '{decision.action}' for content-only ingest; "
            f"only 'accept' is valid for rows with no uploaded file"
        )

    # Create slug and insert
    slug = storage.make_slug()

    # Apply row_overrides (only allowed keys: content_description, content_date, type_metadata)
    insert_kwargs = {
        "description": description,
        "tags": tags or [],
        "client": client or None,
        "type_metadata": type_metadata,
    }

    for key, value in decision.row_overrides.items():
        if key in ("content_description", "content_date", "type_metadata"):
            insert_kwargs[key] = value
        else:
            print(f"Ignoring unrecognized row_override key: {key}", flush=True)

    db.insert_content(
        slug, source, media_type,
        external_url=external_url or None,
        content_description=insert_kwargs.get("content_description", content_description),
        content_date=insert_kwargs.get("content_date", content_date),
        **{k: v for k, v in insert_kwargs.items() if k not in ("content_description", "content_date")}
    )

    # Background tasks
    post_insert(slug, spec, run_background)

    # Project attachment
    attach_to_project(slug, project_id or None)

    return IngestResult(row=db.get_by_slug(slug))


def retype(slug, new_media_type, run_background):
    """#448: Change a row's media_type to a different registered type.

    Resets OCR status if the new type is OCR-capable, deletes any existing
    thumbnail, and re-runs embedded metadata extraction and post-insert steps.

    Args:
        slug: The row's slug.
        new_media_type: The new media_type key (must be registered).
        run_background: Callable to schedule background work.

    Returns:
        The updated row dict, or raises ObjectTypeContractError if new_media_type is unregistered.
    """
    if new_media_type not in object_types.OBJECT_TYPES:
        raise object_types.ObjectTypeContractError(f"Unknown media_type: {new_media_type}")

    spec = object_types.get_object_type(new_media_type)

    # Update the media_type
    db.set_media_type(slug, new_media_type)

    # Reset OCR status if the new type is OCR-capable
    if spec.ocr_capable:
        db.set_ocr_status(slug, "pending")

    # Delete any existing thumbnail
    storage.thumb_path_for(slug).unlink(missing_ok=True)

    # Re-run embedded metadata extraction
    embedded_metadata.fill_missing(slug)

    # Re-run post-insert steps (OCR, thumbnail, captions)
    post_insert(slug, spec, run_background)

    return db.get_by_slug(slug)
