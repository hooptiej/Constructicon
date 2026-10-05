"""constructicon-mcp — MCP server for the Constructicon media gallery.

Uses the mcp package's v2 MCPServer API
(FastMCP was renamed/restructured in mcp 2.x — see the SDK migration guide).
Streamable-HTTP transport, host/port/stateless_http passed to run().

Exposes MCP tools for uploading, managing, tagging, and organizing media
in a Constructicon instance. Runs as a sidecar alongside constructicon-web.

Conventions shared by every tool (#560, #548; enforced by the `@mcp.tool()` wrapper below):
  * Actor: each call runs as the "mcp" actor (core/actor.py), so the change log records
    "mcp" without any tool passing it.
  * Errors: a refusal comes back as {"ok": false, "error": {"code", "message"[, "details"]}}
    (an isError tool result), with the same codes the HTTP API uses. Not found is an
    error too: a getter or setter whose target doesn't exist returns code "not_found"
    rather than None or False. Input validation is "bad_request" unless a more specific
    code applies (bad_status, invalid_choice, ...). Successful returns are unchanged.
"""

import base64
import functools
import io
import json
import os
import sys
from datetime import datetime
from pathlib import Path

import pydantic

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

# #549: this is NOT the process that does background work. core.captions.run_caption checks this
# role and enqueues (caption_queue table) instead of calling the GPU; the web process drains it.
os.environ["CONSTRUCTICON_ROLE"] = "mcp"

from mcp.server.mcpserver import MCPServer
from mcp.types import CallToolResult, TextContent

from core import actor as actor_ctx
from core import backup, card_rules, cards, curation_queue, curator_needs, db, decisions, errors, ingest, items, object_types, ocr, physical_piece, provenance_options, revisions, storage, timeline
from core.errors import InvalidInput, NotFound
from core import version as version_info
from core import blog, changes, hobbies, membership, reset
from core import tags as tags_svc

BASE_URL = os.environ.get("CONSTRUCTICON_BASE_URL", "http://constructicon-web:8000")

# #433 part 3: a read-only bind-mounted inbox (host: Media/constructicon/import,
# reachable over SMB) so large files are ingested by server-side path instead of
# base64 through one MCP message; never written to or deleted from by the app.
IMPORT_DIR = Path(os.getenv("CONSTRUCTICON_IMPORT_DIR", "/app/import"))

# #433: base64 inflates ~33% and the whole payload rides one MCP message;
# a 500 MB file would be ~670 MB of JSON
DOWNLOAD_INLINE_MAX_BYTES = 25 * 1024 * 1024

mcp = MCPServer(name="constructicon-mcp", version=version_info.get_version())  # #508: version in server info (read at start; restart picks up a deploy)


# --- One wrapper around every tool (#560 actor, #548 errors) ---
# `@mcp.tool()` below is this module's own decorator (installed over MCPServer.tool), so no
# tool can be registered without it:
#   * the tool body runs inside actor.acting_as("mcp"), so every core write it makes is
#     recorded with actor "mcp" without passing a literal;
#   * a refusal (core.errors.AppError: CardError, QueueError, decisions.*, NotFound, ...,
#     or a ValueError / pydantic ValidationError raised as input validation) becomes
#     {"ok": false, "error": {"code", "message"[, "details"]}}. Over MCP that payload is
#     returned as an isError tool result (text = the JSON, structuredContent = the dict);
#     a direct Python call of the tool function returns the dict itself.
#   * Not found: a getter or setter whose target doesn't exist returns the error shape with
#     code "not_found" (never None/False). Successful returns are unchanged.
#   * Anything else (a real bug) propagates, and the SDK reports it as an internal error.
# Argument-schema validation (a wrong type for a parameter) happens inside the SDK before the
# tool body runs, so it keeps the SDK's own isError text.

def _error_payload(exc):
    """The shared error payload for a refusal, or None for an unexpected exception."""
    if isinstance(exc, errors.AppError):
        return errors.to_payload(exc)
    if isinstance(exc, pydantic.ValidationError):
        return errors.error_body("validation_error", str(exc))
    if isinstance(exc, ValueError):
        return errors.error_body("bad_request", str(exc))
    return None


def _call_as_mcp(fn, args, kwargs):
    """(True, result) or (False, error payload)."""
    with actor_ctx.acting_as(actor_ctx.ACTOR_MCP):
        try:
            return True, fn(*args, **kwargs)
        except Exception as exc:  # noqa: BLE001 -- re-raised below unless it's a refusal
            payload = _error_payload(exc)
            if payload is None:
                raise
            return False, payload


_sdk_tool = mcp.tool
_WRAPPED_TOOLS = {}


def _tool(*dargs, **dkwargs):
    register = _sdk_tool(*dargs, **dkwargs)

    def decorate(fn):
        @functools.wraps(fn)
        def for_sdk(*args, **kwargs):
            ok, value = _call_as_mcp(fn, args, kwargs)
            if ok:
                return value
            return CallToolResult(is_error=True, content=[TextContent(type="text", text=json.dumps(value))],
                                  structured_content=value)

        @functools.wraps(fn)
        def direct(*args, **kwargs):
            return _call_as_mcp(fn, args, kwargs)[1]

        register(for_sdk)
        _WRAPPED_TOOLS[fn.__name__] = for_sdk
        return direct

    return decorate


mcp.tool = _tool


def _resolve_import_path(rel):
    """Resolve a relative path within IMPORT_DIR, rejecting absolute paths
    and escapes (../ or symlinks outside IMPORT_DIR). Returns Path | None."""
    # Reject absolute paths and path traversal attempts
    if Path(rel).is_absolute():
        return None
    candidate = (IMPORT_DIR / rel).resolve()
    # Ensure the resolved path stays within IMPORT_DIR bounds
    try:
        candidate.relative_to(IMPORT_DIR.resolve())
    except ValueError:
        # candidate is outside IMPORT_DIR
        return None
    # Verify it's a file and exists
    if not candidate.is_file():
        return None
    return candidate


def _run_in_thread(fn, *args):
    """Run a function in a background daemon thread, carrying the actor context (#560)."""
    actor_ctx.spawn(fn, *args)


def _ingest(filename, fileobj, file_size, description, tags, uploaded_by, source_modified_at) -> dict:
    """Private helper for file ingestion: delegates to core/ingest.py.

    All post-validation logic is now centralized in the ingest module — this tool
    simply wraps its result for the MCP response format.

    Returns {"slug": ..., ..._to_public fields..., "duplicate": False} on success; a refusal
    raises InvalidInput (code upload_refused), which the tool wrapper turns into the shared
    error shape. May include "pending_decision_id" if the type's pre_store_fn deferred to the
    owner (#448).
    """
    result = ingest.ingest_file(
        fileobj,
        filename,
        size=file_size,
        source=uploaded_by,
        run_background=_run_in_thread,
        description=description,
        tags=tags,
        source_modified_at=source_modified_at,
    )

    if result.error:
        raise InvalidInput(result.error, code="upload_refused")
    elif result.duplicate:
        response = {**_to_public(result.row), "duplicate": True}
        if result.pending_decision_id:
            response["pending_decision_id"] = result.pending_decision_id
        return response
    else:
        response = {**_to_public(result.row), "duplicate": False}
        if result.pending_decision_id:
            response["pending_decision_id"] = result.pending_decision_id
        return response


def _to_public(row):
    spec = object_types.get_object_type(row.get("media_type"))
    return {
        "slug": row["slug"],
        "url": f"{BASE_URL}/f/{row['slug']}",
        "filename": row["filename"],
        # display_name/icon (#11) — per-object overrides, falling back to
        # the same filename/content_description/slug and spec.badge_icon
        # chain the web app uses (see web/app.py's _to_public/_to_object_detail).
        "display_name": row.get("display_name") or row["filename"] or row.get("content_description") or row["slug"],
        "icon": row.get("icon") or spec.badge_icon,
        "media_type": row.get("media_type") or "image",
        "description": row["description"],
        "tags": row["tags"],
        "client": row["client"],
        "redacted": bool(row["redacted"]),
        "source": row["source"],
        "extracted_text": row["extracted_text"],
        "ocr_status": row["ocr_status"],
        # #261: per-type metadata bag (auto_caption/auto_caption_status,
        # YouTube view/like/comment counts, ID3 tags, ...) -- previously
        # invisible through every MCP tool. Same "always a dict, never
        # None/missing" contract as web/app.py's _to_object_detail;
        # core/db.py's _row_to_dict already parses the JSON column, so
        # this is only a guard against a stored JSON null.
        "type_metadata": row.get("type_metadata") or {},
        "artifact_link": f"{BASE_URL}{row['artifact_link']}" if row["artifact_link"] else None,
        "timestamp": row["timestamp"],
        "content_date": row.get("content_date"),
        "display_date_override": row.get("display_date_override"),
        "effective_date": timeline.resolve_item_date(row),
        # Curator (#341): provenance classification + highlight flag, so every
        # MCP object read (get_project items, search, list) carries them.
        "provenance": row.get("provenance"),
        "highlight": bool(row.get("highlight")),
        # Brand assets (#350): is_brand_asset flag + optional brand_role label,
        # so every MCP object read carries brand kit metadata.
        "is_brand_asset": bool(row.get("is_brand_asset")),
        "brand_role": row.get("brand_role"),
        # #448: per-type actions available on this object (e.g. YouTube's "fetch real date" moves here in PR 2).
        # The type file owns the handler; actions are declared in ObjectTypeSpec.actions.
        "actions": [a.key for a in spec.actions],
    }


def _to_public_project(project):
    items = db.list_project_items(project["id"])
    effective_start, effective_end = timeline.resolve_project_span(project, items)
    return {
        "id": project["id"],
        "slug": project["slug"],
        "title": project["title"],
        "description": project["description"],
        # Legacy v1 status: frozen (the static export still reads it). The live
        # status is kind / activity / stage / stop_reason below.
        "status": project["status"],
        **cards.status_fields(project),
        **cards.whereabouts_fields(project),
        "cover_slug": project.get("cover_slug"),
        "writeup_slug": project.get("writeup_slug"),
        "parent_id": project.get("parent_id"),
        "created_at": project["created_at"],
        "start_date_override": project.get("start_date_override"),
        "end_date_override": project.get("end_date_override"),
        "effective_start": effective_start,
        "effective_end": effective_end,
    }


def _to_public_blog_entry(entry):
    """Full detail shape for a blog entry, including hydrated projects and items."""
    projects = db.list_entry_projects(entry["id"])
    items = db.list_entry_items(entry["id"])
    return {
        "id": entry["id"],
        "slug": entry["slug"],
        "title": entry["title"],
        "subtitle": entry.get("subtitle", ""),
        "body": entry.get("body", ""),
        "status": entry["status"],
        "cover_slug": entry.get("cover_slug"),
        "content_date": entry.get("content_date"),
        "created_at": entry["created_at"],
        "updated_at": entry["updated_at"],
        # Carry the per-attachment note + sort_order (the reshaping helpers
        # drop them, but they're the point of the attachment).
        "projects": [{**_to_public_project(p), "note": p.get("note", ""), "sort_order": p.get("sort_order")} for p in projects],
        "items": [{**_to_public(i), "note": i.get("note", ""), "sort_order": i.get("sort_order")} for i in items],
    }


@mcp.tool()
def constructicon_version() -> dict:
    """Report which Constructicon build is running (#508): {version, commit,
    deployed_at, env}. Version is CalVer (YYYY.M.D, .N for repeats the same day),
    suffixed -dev on the test instance; "dev" means no deploy.sh version file."""
    return version_info.get_version_info()


@mcp.tool()
def constructicon_upload(filename: str, content_base64: str, description: str = "", tags: list[str] | None = None,
                      uploaded_by: str = db.SOURCE_AUTHORED,
                      source_modified_at: float | None = None) -> dict:
    """Upload an image, document, or other file to Constructicon.

    Returns a JSON object with the new object's metadata, including a stable hotlink URL.

    filename: original filename; extension (.png/.jpg/.pdf/.stl/.psd/.svg/.eps/.mp3/.wav, etc.)
      determines the media type.
    content_base64: raw file bytes, base64-encoded.
    tags: optional list of tag names to attach to the object.
    description: optional metadata description for the object.
    uploaded_by: the Source string to record (capture_events.tech). Defaults to
      "Claude — authored" (this tool call created the content). For content migrated from
      an external source, pass db.source_migrated_from("<source>") instead.
    source_modified_at: the source file's own last-modified time (unix seconds), if known.
      Used for duplicate detection; omit to skip checking for re-uploads of the same file.

    If filename, file size, and source_modified_at all match an existing upload, returns
    the existing object instead with "duplicate": true.

    OCR (for OCR-capable types) runs in the background, same as the web app's
    /api/upload: the returned object has ocr_status "pending" -- call
    constructicon_get on the slug later to read extracted_text once it's "done".

    An audio file's own tags (#255) are read synchronously, same as /api/upload:
    the tag title becomes the object's display_name/content_description and
    artist/album/track/year/genre land in type_metadata, so the returned object
    already reflects them. A tagless file simply gets none of that.
    """
    content = base64.b64decode(content_base64)
    return _ingest(filename, io.BytesIO(content), len(content), description, tags, uploaded_by, source_modified_at)


@mcp.tool()
def constructicon_search(query: str | None = None, tags: list[str] | None = None) -> list[dict]:
    """Search objects by description, filename, or tags.

    Returns a JSON list of matching objects. If both query and tags are
    provided, filters by both (AND logic). Redacted objects are excluded
    (#282) -- use constructicon_list_redacted to see those.
    """
    # #477: old revisions are still found, marked with `superseded_by` (the current revision's slug) and `rev`.
    return revisions.decorate([_to_public(r) for r in db.search(query=query, tags=tags, client=None)])


@mcp.tool()
def constructicon_get(slug: str) -> dict | None:
    """Get one object's metadata and hotlink URL by its slug.

    A missing slug returns the not_found error. `superseded_by` (the current revision's slug, or null)
    and `rev` (position in its revision chain, or null) say where it sits in a chain (#477).
    """
    row = db.get_by_slug(slug)
    if row is None:
        raise NotFound(f"No item {slug!r}.")
    return revisions.decorate([_to_public(row)])[0]


@mcp.tool()
def constructicon_download(slug: str) -> dict | None:
    """Download a file's actual bytes from Constructicon.

    Takes a slug and returns the file's content base64-encoded, along with
    filename and media_type for round-trip upload/download cycles.

    For files larger than the inline limit (~25MB), returns metadata + a stable
    hotlink URL instead of base64 — fetch it from the URL to avoid inflating
    the MCP message payload beyond practical limits (#433).

    Errors: not_found (no such object), no_file (the object has no uploaded file, e.g. a
    YouTube link or other content-only object), file_missing (the file is gone from disk).
    """
    row = db.get_by_slug(slug)
    if row is None:
        raise NotFound(f"No item {slug!r}.")
    if not row.get("stored_filename"):
        raise InvalidInput("This object has no uploaded file to download", code="no_file")
    path = storage.path_for(row["stored_filename"])
    if not path.exists():
        raise NotFound("File missing on disk", code="file_missing")
    size = path.stat().st_size
    if size > DOWNLOAD_INLINE_MAX_BYTES:
        # Return URL instead of base64 for large files
        return {
            "slug": row["slug"],
            "filename": row["filename"],
            "media_type": row.get("media_type") or "image",
            "size_bytes": size,
            "url": _to_public(row)["url"],
            "content_base64": None,
            "note": f"File is {size // (1024*1024)} MB, over the {DOWNLOAD_INLINE_MAX_BYTES // (1024*1024)} MB inline limit; fetch it from url instead.",
        }
    content = path.read_bytes()
    return {
        "slug": row["slug"],
        "filename": row["filename"],
        "media_type": row.get("media_type") or "image",
        "content_base64": base64.b64encode(content).decode("utf-8"),
    }


@mcp.tool()
def constructicon_import(path: str, description: str = "", tags: list[str] | None = None,
                        uploaded_by: str = db.SOURCE_AUTHORED,
                        source_modified_at: float | None = None) -> dict:
    """Import a file already sitting in the server-side import inbox.

    Use this for large files (up to the server's upload limit, e.g. hundreds of MB)
    that are impractical to send base64 via constructicon_upload. Path is relative
    to the inbox (e.g. "drawings/set-A.pdf"); anything resolving outside the inbox
    is refused. The file is COPIED into storage and left in place in the inbox.

    The stored filename is the file's basename. source_modified_at defaults to the
    file's own mtime, so re-importing the same unchanged file returns the existing
    object with duplicate: true. Same OCR/metadata behavior and return shape as
    constructicon_upload.
    """
    if not IMPORT_DIR.is_dir():
        raise errors.AppError("import_unavailable", f"Import inbox {IMPORT_DIR} is not mounted on this server",
                              status=503)
    p = _resolve_import_path(path)
    if p is None:
        raise NotFound(f"Not a file inside the import inbox: {path}")
    st = p.stat()
    with p.open("rb") as f:
        return _ingest(p.name, f, st.st_size, description, tags, uploaded_by,
                      source_modified_at if source_modified_at is not None else st.st_mtime)


@mcp.tool()
def constructicon_list_import() -> dict:
    """List files waiting in the import inbox (recursive).

    Returns {"inbox": str(IMPORT_DIR), "max_upload_mb": storage.MAX_MB,
    "files": [{"path": <posix path relative to inbox>, "size_bytes": n,
    "modified_at": mtime, "supported": bool(detect_media_type)}]} sorted by path,
    skipping hidden files/dirs (name starting with ".") and anything that resolves
    outside the inbox. If the inbox isn't mounted: the import_unavailable error.

    Caps the listing at 1000 files and adds "truncated": True if more.
    """
    if not IMPORT_DIR.is_dir():
        raise errors.AppError("import_unavailable", f"Import inbox {IMPORT_DIR} is not mounted on this server",
                              status=503)

    files = []
    try:
        for p in sorted(IMPORT_DIR.rglob("*")):
            rel = p.relative_to(IMPORT_DIR)
            if any(part.startswith(".") for part in rel.parts):
                continue
            # Same check constructicon_import applies: only list what it would
            # accept (regular files, symlinks resolving inside the inbox).
            if _resolve_import_path(rel) is None:
                continue
            st = p.stat()
            files.append({
                "path": rel.as_posix(),
                "size_bytes": st.st_size,
                "modified_at": st.st_mtime,
                "supported": object_types.detect_media_type(p.name) is not None,
            })
            if len(files) >= 1000:
                break
    except Exception as e:
        raise errors.AppError("import_error", f"Error listing import inbox: {e}", status=500) from e

    truncated = len(files) >= 1000
    result = {
        "inbox": str(IMPORT_DIR),
        "max_upload_mb": storage.MAX_MB,
        "files": files,
    }
    if truncated:
        result["truncated"] = True
    return result


@mcp.tool()
def constructicon_update(slug: str, description: str | None = None, tags: list[str] | None = None,
                   display_name: str | None = None, icon: str | None = None,
                   type_metadata: dict | None = None, display_date: float | None = None,
                   reset_display_date: bool = False) -> dict | None:
    """Update an object's metadata: description, tags, display name, icon, type-specific fields,
    and/or its timeline display date.

    Pass None for any field you don't want to change. type_metadata is MERGED by top-level key
    (same as the web app's POST /api/image/{slug}); set a key to "" to clear it.
    content_description (e.g. a YouTube video's title) isn't exposed through this tool yet —
    POST /api/image/{slug} can change it, this tool just doesn't take that parameter.

    display_date sets a manual override for this object's position on the Constructicon
    timeline (unix timestamp, e.g. what time.time() or a datetime's .timestamp() returns).
    reset_display_date=True clears the override, reverting to the computed default
    (content_date, falling back to the upload timestamp) — it wins over display_date if both
    are passed.

    #541: one change-log batch for the whole call (undo it with constructicon_undo); every
    field is validated before anything is written. Returns the updated object; a missing one
    returns the not_found error.
    """
    items.get_item(slug)
    fields = {}
    if description is not None:
        fields["description"] = description
    if display_name is not None:
        fields["display_name"] = display_name
    if icon is not None:
        fields["icon"] = icon
    if type_metadata is not None:
        # #563: merge (top-level keys), same as the web route; #425 keys cleaned in core.
        fields["type_metadata"] = dict(type_metadata)
    if reset_display_date:
        fields["display_date_override"] = None
    elif display_date is not None:
        fields["display_date_override"] = display_date
    if fields or tags is not None:
        # #541 phase C: free-text tags ride in the same call, so the whole update is ONE batch.
        row = items.update(slug, tags=tags, **fields).item
    else:
        row = db.get_by_slug(slug)
    return _to_public(row) if row else None


@mcp.tool()
def constructicon_redact(slug: str) -> dict | None:
    """Remove a file while keeping its metadata (for sensitive content cleanup).

    The file is HELD in the trash with no expiry: never auto-deleted, and constructicon_empty_trash
    skips it. The owner decides: constructicon_recover_redacted(slug) restores it,
    constructicon_delete_redacted_file(slug, confirm=True) erases it for good.
    Metadata (description, tags, etc.) is preserved. Returns the object plus "batch_id";
    errors: not_found, no_file.
    """
    result = items.redact(slug)
    return {**_to_public(result.item), "batch_id": result.batch_id}


@mcp.tool()
def constructicon_unredact(slug: str) -> dict | None:
    """Reverse of constructicon_redact (#282): clear the redacted flag so the
    object shows up in searches, project listings and tag walks again.

    Visibility only: it does NOT bring the file back, and it is for redactions whose file is
    already gone. While the file is still held it refuses (redact_hold_exists): use
    constructicon_recover_redacted to restore it, or constructicon_delete_redacted_file first.
    Returns the updated object; errors: not_found, not_redacted, redact_hold_exists.
    """
    return _to_public(items.unredact(slug).item)


@mcp.tool()
def constructicon_recover_redacted(slug: str) -> dict:
    """Recover a redacted object's held file and un-redact it: the object is exactly as it was
    before the redact (file, stored_filename, visible). Returns the object plus "batch_id" (undo
    re-holds the file). Errors: not_found, not_redacted, no_redact_hold (the file was already
    permanently deleted, or the redaction predates held files)."""
    result = items.recover_redacted(slug)
    return {**_to_public(result.item), "batch_id": result.batch_id}


@mcp.tool()
def constructicon_delete_redacted_file(slug: str, confirm: bool = False) -> dict:
    """PERMANENTLY delete a redacted object's held file. Pass confirm=true. The object stays
    redacted with metadata only; constructicon_recover_redacted refuses afterwards. Not undoable.
    Returns {"deleted": true, "slug", "bytes"}; errors: confirm_required, not_found, no_redact_hold."""
    result = items.delete_redacted_file(slug, confirm)
    return {"deleted": True, "slug": slug, "bytes": result.data["bytes"]}


@mcp.tool()
def constructicon_list_redacted() -> list[dict]:
    """List every redacted object (#282).

    Redacted objects are hidden from constructicon_search,
    constructicon_get_project and constructicon_get_posts_for_tag; this is
    the only listing that includes them. constructicon_get still works for
    one by slug.
    """
    return [_to_public(r) for r in db.list_redacted()]


@mcp.tool()
def constructicon_list_restricted() -> list[dict]:
    """List every restricted object: private keys, certificates, CSRs (#443).

    Restricted objects are the owner's private reference. They're kept out
    of constructicon_search and tag walks and never exported to the public
    site, but DO appear in constructicon_get_project for projects they're
    attached to (e.g. a repo's deploy key). This is the full list. Each
    entry adds "projects" (titles and slugs it's attached to).
    """
    return [
        {**_to_public(r), "projects": [{"title": p["title"], "slug": p["slug"]} for p in db.list_projects_for_post(r["slug"])]}
        for r in db.list_restricted()
    ]


@mcp.tool()
def constructicon_delete(slug: str) -> dict:
    """Delete an object: its row, its project/tag/relation/revision-chain/blog links and any
    open question about it. #541: the file goes to the trash for 7 days; until it is purged,
    constructicon_undo(batch_id) restores everything (row, links and file). After that the
    undo answers trash_expired.
    Returns {"deleted": true, "batch_id", "expires_at"}; an unknown slug returns not_found."""
    result = items.delete([slug])
    return {"deleted": True, "batch_id": result.batch_id, "expires_at": result.data["expires_at"]}


@mcp.tool()
def constructicon_delete_multiple(slugs: list[str]) -> dict:
    """Delete multiple objects by slug, as ONE undoable batch (files to the trash for 7 days,
    like constructicon_delete). Unknown slugs are silently skipped.
    Returns {"deleted": count, "batch_id"}.
    """
    result = items.delete(slugs, missing_ok=True)
    return {"deleted": result.data["deleted"], "batch_id": result.batch_id}


@mcp.tool()
def constructicon_list_trash() -> dict:
    """What the trash holds (#541). Ordinary deletes, kept 7 days so the delete can be undone:
    {count, bytes, oldest, next_expiry, days, items: [{slug, title, batch_id, reason,
    size_bytes, created_at, expires_at}]}. Redact holds (no expiry, kept until the owner
    recovers or deletes them) are listed separately under `held`: {count, bytes, items}.
    Undo a delete with constructicon_undo(batch_id)."""
    return items.trash_summary()


@mcp.tool()
def constructicon_empty_trash(confirm: str) -> dict:
    """Permanently purge every ORDINARY deleted object's file now (#541). Pass confirm="EMPTY TRASH".
    Redact holds are NOT touched (they wait for the owner). The deletes those files came from
    can no longer be undone (trash_expired). Returns {purged, bytes, slugs, held_kept, message};
    a wrong phrase returns confirm_required."""
    r = items.empty_trash(confirm)
    return {**r, "message": f"Purged {r['purged']} deleted item(s) ({r['bytes']} bytes). "
                            f"{r['held_kept']} redacted file(s) are still held (not touched)."}


@mcp.tool()
def constructicon_delete_all(confirm: str = "") -> dict:
    """Wipe the whole archive: every object (and its files), card, tag, hobby, blog entry,
    question and the trash. A full reset: permanent, NOT undoable, nothing goes to the trash.

    Pass confirm="DELETE EVERYTHING" (the same typed phrase /admin asks for); anything else
    returns confirm_required and changes nothing. Call constructicon_backup first if you want to
    keep the current content. Settings, the client list, provenance lists and the change log are
    kept. Returns {deleted, counts: {table: rows}, files_removed, trash_removed, batch_id}; one
    change-log row (op delete_all) records the counts.
    """
    return reset.delete_everything(confirm)


@mcp.tool()
def constructicon_backup() -> dict:
    """Create a timestamped backup of the entire database and storage.

    Returns {"filename": ..., "path": ..., "size": ..., "created_at": ...}
    (see core.backup.create_backup).
    """
    return backup.create_backup()


@mcp.tool()
def constructicon_add_content(media_type: str, external_url: str | None = None, content_description: str | None = None,
                           description: str = "", tags: list[str] | None = None,
                           uploaded_by: str = db.SOURCE_AUTHORED) -> dict:
    """Create an object with no uploaded file (e.g., a YouTube link or external document).

    Use constructicon_upload instead for file-backed content.

    For OCR-capable types, OCR runs in the background (#225) -- the returned
    object has ocr_status "pending"; read it back with constructicon_get later.

    Returns a JSON object with the new object's metadata; a refusal returns the shared
    error shape (code content_refused).
    Unknown media_type values are rejected (issue #448).
    """
    result = ingest.ingest_content(
        source=uploaded_by,
        run_background=_run_in_thread,
        media_type=media_type,
        external_url=external_url,
        content_description=content_description,
        description=description,
        tags=tags,
    )

    if result.error:
        error_msg = result.error
        if error_msg.endswith("require a file upload"):
            error_msg += " — use constructicon_upload"
        raise InvalidInput(error_msg, code="content_refused")
    return _to_public(result.row)


@mcp.tool()
def constructicon_add_related(slug: str, related_slug: str) -> list[dict]:
    """Link two objects as related (bidirectional).

    Also merges tags and project membership between the two.
    Returns the updated list of related objects.
    """
    if db.get_by_slug(slug) is None or db.get_by_slug(related_slug) is None:
        raise NotFound(f"No item {slug!r}." if db.get_by_slug(slug) is None else f"No item {related_slug!r}.")
    items.relate(slug, related_slug)  # #541 phase C: one undoable batch (link + what it shares)
    return [_to_public(r) for r in db.list_related(slug)]


@mcp.tool()
def constructicon_remove_related(slug: str, related_slug: str) -> list[dict]:
    """Remove a related-object link (both directions). The tags and projects the link shared
    stay; undo the add (constructicon_undo) to take those back too.

    Returns the updated list of related objects.
    """
    items.unrelate(slug, related_slug)
    return [_to_public(r) for r in db.list_related(slug)]


@mcp.tool()
def constructicon_get_related(slug: str) -> list[dict]:
    """Get all objects related to this one.

    Returns a list of related objects, both manually linked and auto-detected.
    """
    if db.get_by_slug(slug) is None:
        raise NotFound(f"No item {slug!r}.")
    return [_to_public(r) for r in db.list_related(slug)]


@mcp.tool()
def constructicon_list_projects(kind: str | None = None, activity: str | None = None,
                                stage: str | None = None) -> list[dict]:
    """List all projects (cards), most-recently-updated first.

    Optional filters on the live V2 status: kind (project|thing|action|family|
    collection|event), activity (active|inactive), stage (in_progress|in_use|idea|
    paused|done|stopped). Each card carries kind, activity, stage, stop_reason,
    their labels, and needs_input (true while a question about it is still open).
    """
    return [_to_public_project(p) for p in db.list_projects(kind=kind, activity=activity, stage=stage)]


@mcp.tool()
def constructicon_create_project(title: str, description: str = "", cover_slug: str | None = None, parent_id: int | None = None,
                                 kind: str | None = None, stage: str | None = None, stop_reason: str | None = None) -> dict:
    """Create a new project (card).

    Also creates a root-level tag with the same name and links it, so tagged
    objects surface through both project and tag browsing.

    parent_id optionally makes this card "part of" a parent card (#133). Nesting
    rules apply (a family or collection can't be a parent or be nested; the parent
    must exist): a violation returns {"ok": false, "error": {"code": "nest_group_kind"
    | "not_found", ...}}.

    kind: project (default) | thing | action | family | collection | event.
    stage: in_progress (default) | in_use | idea | paused | done | stopped
    (stopped needs stop_reason failed|abandoned). Invalid combinations return
    {"ok": false, "error": {"code": "bad_status", ...}}.
    """
    # #541 phase D: core validates everything first; tag + card + write-up are ONE undoable batch.
    result = cards.create(title, description=description, cover_slug=cover_slug, parent=parent_id,
                          kind=kind, stage=stage, stop_reason=stop_reason)
    return {**_to_public_project(result.data["card"]), "batch_id": result.batch_id}


@mcp.tool()
def constructicon_update_project(project_id: str | int, title: str | None = None,
                                 description: str | None = None, cover_slug: str | None = None,
                                 status: str | None = None, start_date: float | None = None,
                                 reset_start_date: bool = False, end_date: float | None = None,
                                 reset_end_date: bool = False, kind: str | None = None,
                                 stage: str | None = None, stop_reason: str | None = None) -> dict | None:
    """Update a project's metadata, including its timeline span.

    kind / stage / stop_reason set the live V2 status through the same rules as
    constructicon_set_kind / constructicon_set_status (a violation returns
    {"ok": false, "error": {code, message}}). `status` is the DEPRECATED v1 word
    (wip, complete, shelved, ...): it is translated to a stage (a warning says so).

    start_date/end_date set manual overrides for this project's position on the Constructicon
    timeline (unix timestamps). reset_start_date/reset_end_date each clear that one override,
    reverting it to the computed default (earliest/latest item date, falling back to the
    project's created_at when it has no items) — a reset flag wins over its corresponding
    date param if both are passed.

    Returns the updated project; a missing one returns the not_found error.
    """
    card_warnings = []
    project = db.get_project(project_id)
    if project is None:
        raise NotFound(f"No card {project_id!r}.")
    if status:
        legacy = card_rules.legacy_to_status(status)
        kind = kind or legacy["kind"]
        if not stage:
            stage, stop_reason = legacy["stage"], legacy["stop_reason"]
        card_warnings.extend(legacy["warnings"])
    new_start = None if reset_start_date else (start_date if start_date is not None else ...)
    new_end = None if reset_end_date else (end_date if end_date is not None else ...)
    # #541 phase D: one transaction, one batch (one constructicon_undo reverses the whole call).
    batch_id = changes.new_batch_id()
    with db.transaction():
        if kind:
            card_warnings.extend(cards.set_kind(project["id"], kind, batch_id=batch_id).warnings)
        if stage or stop_reason:
            card_warnings.extend(cards.set_status(project["id"], stage, stop_reason, batch_id=batch_id).warnings)
        cards.update(project["id"], title=title, description=description, cover_slug=cover_slug,
                     start=new_start, end=new_end, batch_id=batch_id)
    result = {**_to_public_project(db.get_project(project["id"])), "batch_id": batch_id}
    if card_warnings:
        result["warnings"] = card_warnings
    return result


@mcp.tool()
def constructicon_add_to_project(slug: str, project_id: str | int) -> list[dict]:
    """Add an object to a project, exactly like the web app's item page.

    If the project has a linked tag, the object is tagged with it (and the tag name joins its
    free-text tags), and a project with no cover takes this object as its cover. Undoable as
    one batch (constructicon_undo). Returns the object's updated project list.
    """
    if db.get_by_slug(slug) is None:
        raise NotFound(f"No item {slug!r}.")
    if db.get_project(project_id) is None:
        raise NotFound(f"No card {project_id!r}.")
    membership.add_files(project_id, [slug], **membership.UI_EFFECTS)  # #541 phase C: same flags as the web
    return [_to_public_project(p) for p in db.list_projects_for_post(slug)]


@mcp.tool()
def constructicon_remove_from_project(slug: str, project_id: str | int) -> list[dict]:
    """Remove an object from a project (does not untag it).

    Returns the object's updated project list.
    """
    if db.get_by_slug(slug) is None:
        raise NotFound(f"No item {slug!r}.")
    project = db.get_project(project_id)
    if project is None:
        raise NotFound(f"No card {project_id!r}.")
    membership.remove_files(project["id"], [slug])  # membership only: tags and cover stay
    return [_to_public_project(p) for p in db.list_projects_for_post(slug)]


@mcp.tool()
def constructicon_add_items_to_project(project_id: str | int, slugs: list[str]) -> list[dict]:
    """Add multiple objects to a project in a single call.

    Same effects as constructicon_add_to_project (linked tag, free-text tag name, cover when the
    project has none), as ONE undoable batch. Unknown slugs are skipped.
    Returns the list of added objects.
    """
    project = db.get_project(project_id)
    if project is None:
        raise NotFound(f"No card {project_id!r}.")
    result = membership.add_files(project["id"], slugs, missing_ok=True, **membership.UI_EFFECTS)
    return [_to_public(db.get_by_slug(slug)) for slug in slugs if slug in result.data["slugs"]]


@mcp.tool()
def constructicon_set_project_writeup(project_id: str | int, slug: str, owner_words: bool | None = None) -> dict | None:
    """Set a project's write-up document to a given object.

    The object must exist and be an item whose type declares writeup_body_key
    (i.e., can serve as a write-up). The item is also added to the project's
    items if not already present.
    owner_words (optional): true marks the write-up as holding the OWNER'S OWN wording (the
    oral-history flow), which earns the card its "owner words" pip (V2 cards 3.11); false
    clears the mark; omit to leave it alone.
    Returns the updated project; a missing one returns the not_found error.
    """
    project = db.get_project(project_id)
    if project is None:
        raise NotFound(f"No card {project_id!r}.")
    row = db.get_by_slug(slug)
    if row is None:
        raise NotFound(f"No item {slug!r}.")
    if not object_types.can_be_writeup(row):
        label = object_types.get_object_type(row.get("media_type")).label
        raise ValueError(f"{label} items can't be a project write-up (their type declares no writeup_body_key)")

    # #541 phase D: membership (no side effects, as before) + the owner-words mark + writeup_slug
    # are one transaction and one batch.
    batch_id = changes.new_batch_id()
    with db.transaction():
        membership.add_files(project["id"], [slug], batch_id=batch_id, **membership.NO_EFFECTS)
        if owner_words is not None:
            items.update(slug, type_metadata={"owner_words": bool(owner_words)}, batch_id=batch_id)
        updated = cards.update(project["id"], writeup_slug=slug, batch_id=batch_id).data["card"]
    return {**_to_public_project(updated), "batch_id": batch_id}


@mcp.tool()
def constructicon_get_project(id_or_slug: str | int) -> dict | None:
    """Get a project with full details: metadata, items, tags, cover, and write-up body.

    Returns a comprehensive dict with:
    - id, slug, title, description, cover_slug, writeup_slug, parent_id
    - kind, activity, stage, stop_reason (+ labels) and needs_input: the live V2 status;
      `status` is the frozen legacy v1 word
    - open_decisions: any still-open questions about this card (with the suggested answer)
    - families: the families/collections this card is in; members: the members of this
      card when it is a family or collection; children: cards nested under it ("part of")
    - links: typed links in both directions (see constructicon_list_links)
    - items: list of objects in the project (with their tags)
    - cover: the cover object if cover_slug is set, else None
    - writeup: the writeup document object if writeup_slug is set, else None

    A missing card returns the not_found error.
    """
    project = db.get_project(id_or_slug)
    if project is None:
        raise NotFound(f"No card {id_or_slug!r}.")

    # Get the project items
    items = db.list_project_items(project["id"])
    items_public = []
    for item in items:
        item_dict = _to_public(item)
        # Add tags for each item
        tags = db.list_tags_for_post(item["slug"])
        item_dict["tags"] = [t["name"] for t in tags]
        items_public.append(item_dict)

    # Get the cover object if it exists
    cover = None
    if project.get("cover_slug"):
        cover_row = db.get_by_slug(project["cover_slug"])
        if cover_row:
            cover = _to_public(cover_row)

    # Get the writeup object if it exists
    writeup = None
    if project.get("writeup_slug"):
        writeup_row = db.get_by_slug(project["writeup_slug"])
        if writeup_row:
            writeup = _to_public(writeup_row)

    return {
        **_to_public_project(project),
        "items": items_public,
        "cover": cover,
        "writeup": writeup,
        "open_decisions": [cards.decision_summary(d) for d in cards.open_card_decisions(project["slug"])],
        # V2 cards 3.6/3.7: families this card is in, members (if it is a family or
        # collection), and the cards nested under it ("part of").
        **cards.family_fields(project),
        # V2 cards 3.8: typed links, both directions, with labels.
        "links": cards.list_links(project["slug"]),
        "children": [{"id": c["id"], "slug": c["slug"], "title": c["title"]}
                     for c in db.list_child_projects(project["id"])],
    }


# --- Hobbies (#360) ---

@mcp.tool()
def constructicon_list_hobbies() -> list[dict]:
    """List all hobbies (tags marked is_hobby=1) with their metadata and project counts.

    Each hobby: {id, name, slug, status, group_code, project_count, flags}.
    status is the manual Active/Inactive switch ('active' | 'inactive'); group_code is the
    2-4 char code shown on cards. flags are COMPUTED on every call (never stored), a list of
    {code, label, detail}: inactive_with_active_work (an inactive hobby that has an active
    project) and active_untouched (an active hobby nothing in has been touched for ~2 years).
    A flag only points at a mismatch; the owner flips the switch by hand.
    Ordered by name."""
    out = []
    for h in db.list_hobbies():
        f = cards.hobby_fields(h)
        out.append({
            "id": f["id"],
            "name": f["name"],
            "slug": f["slug"],
            "status": f["status"],
            "group_code": f["group_code"],
            "project_count": h.get("project_count", 0),
            "flags": f["flags"],
        })
    return out


@mcp.tool()
def constructicon_create_hobby(name: str, status: str = "active") -> dict:
    """Create an empty hobby from just a name (#418).

    Mirrors POST /api/hobbies: creates (or reuses) a top-level blog_tags row with the
    given name and marks it as a hobby, as one undoable batch (core/hobbies.py). The
    HTTP route always uses status='active'; this tool also accepts an explicit
    status so a hobby can be stood up inactive in one call.

    status: 'active' or 'inactive' (default 'active'); the deprecated v1 words
    'dormant'/'abandoned' are accepted and stored as 'inactive'.
    Raises ValueError if the name is blank or the status is invalid.
    Returns the new hobby dict {id, name, slug, status, group_code, batch_id}."""
    result = hobbies.create(name, status)
    return {**result.data["hobby"], "batch_id": result.batch_id}


@mcp.tool()
def constructicon_convert_project_to_hobby(project_slug: str, dry_run: bool = False) -> dict | None:
    """Convert an existing project into a hobby (DESTRUCTIVE, but one undoable batch).

    The project is converted into a hobby tag, its child projects are moved to the hobby
    via project_hobbies, its items are tagged with the hobby, and the project row is deleted
    (its links, family rows, blog-entry attachments and a blank write-up are cleared first, as a
    delete does). constructicon_undo(batch_id) restores the card exactly. The reverse is
    constructicon_convert_hobby_to_card. dry_run=true reports the changes and writes nothing.

    Returns the new hobby tag dict with a summary of what was moved, plus batch_id and changes;
    not_found if the project doesn't exist."""
    project = db.get_project(project_slug)
    if project is None:
        raise NotFound(f"No card {project_slug!r}.")
    result = hobbies.convert_from_card(project["id"], dry_run=dry_run)
    hobby = result.data["hobby"]
    return {
        "id": hobby["id"],
        "name": hobby["name"],
        "slug": hobby["slug"],
        "status": hobby["status"],
        "summary": {
            "children_moved": result.data["children_moved"],
            "items_moved": result.data["items_moved"],
        },
        "dry_run": result.dry_run,
        "batch_id": result.batch_id,
        "changes": result.changes,
        "warnings": result.warnings,
    }


@mcp.tool()
def constructicon_convert_hobby_to_card(hobby: str | int, kind: str, title: str | None = None,
                                        into_hobby: str | int | None = None, dry_run: bool = False) -> dict:
    """Turn a hobby into a card (the reverse of constructicon_convert_project_to_hobby), as ONE
    undoable batch. E.g. the "GI Joe" hobby -> a family card inside the Collecting hobby:
    hobby="gi-joe", kind="family", into_hobby="collecting".

    kind: family | collection | project. title: defaults to the hobby's name.
    into_hobby: optional hobby the new card joins.
    Rules: the card reuses the hobby's tag as its linked tag and gets the usual blank write-up;
    its stage follows the hobby (active -> in_progress, inactive -> paused). The hobby's top-level
    member cards become family members (family / collection) or nested parts (project); members
    nested under another member stay under it. A member that is a family/collection, or (for
    project) already part of a card outside the hobby, refuses the whole conversion
    (bad_membership / nest_group_kind / nest_second_parent; nothing written). Loose objects go onto
    the card, home overrides that named the hobby now name the card, and the hobby is unmarked.
    dry_run=true reports every change and writes nothing.
    Returns {ok, dry_run, changes, warnings, batch_id, card, card_id, kind, title, into_hobby,
    hobby, members: [{slug, how}], loose_moved, homes_moved}."""
    return hobbies.convert_to_card(hobby, kind, title, into_hobby, dry_run=dry_run).to_dict()


@mcp.tool()
def constructicon_unmark_hobby(hobby: str | int, dry_run: bool = False) -> dict:
    """Stop treating a tag as a hobby: its activity and group code are cleared, its card
    memberships removed, and cards whose home override named it go back to an automatic home.
    The tag itself (and every file tagged with it) stays. One undoable batch.
    Returns {ok, dry_run, changes, warnings, batch_id, hobby, cards_removed, homes_cleared}."""
    return hobbies.unmark(hobby, dry_run=dry_run).to_dict()


@mcp.tool()
def constructicon_add_project_to_hobby(project_slug: str, hobby_slug: str) -> dict | None:
    """Add a project to a hobby's many-to-many relation.

    Both project_slug and hobby_slug are resolved to their respective ids.
    Returns the updated hobby with its project count; not_found if either doesn't exist."""
    project = db.get_project(project_slug)
    if project is None:
        raise NotFound(f"No card {project_slug!r}.")

    hobby = db.get_hobby(hobby_slug)
    if hobby is None:
        raise NotFound(f"No hobby {hobby_slug!r}.")

    hobbies.add_card(hobby["id"], project["id"])  # logged + undoable (V2 cards 3.13, #541)

    # Return the updated hobby
    updated_hobby = db.get_hobby(hobby["id"])
    projects = db.list_projects_for_hobby(hobby["id"])

    return {
        "id": updated_hobby["id"],
        "name": updated_hobby["name"],
        "slug": updated_hobby["slug"],
        "status": updated_hobby.get("hobby_status"),
        "project_count": len(projects),
    }


@mcp.tool()
def constructicon_set_hobby_status(hobby_slug: str, status: str) -> dict | None:
    """Set a hobby's manual Active/Inactive switch (V2 cards 3.3).

    status: 'active' | 'inactive'. The v1 words 'dormant' and 'abandoned' are deprecated
    aliases: they are stored as 'inactive' and the result carries a `warnings` entry.
    The change is recorded in the change log. Mismatch flags (an inactive hobby with
    active work, an active hobby untouched ~2 years) are computed, never stored: the
    result's `flags` shows them as of now.

    Returns {id, name, slug, status, group_code, flags, warnings, changes, batch_id}, the
    not_found error if the hobby isn't found, or {"ok": false, "error": {code: "bad_hobby_activity",
    message}} for an unknown value."""
    hobby = db.get_hobby(hobby_slug)
    if hobby is None:
        raise NotFound(f"No hobby {hobby_slug!r}.")
    result = cards.set_hobby_activity(hobby["id"], status)
    updated = cards.hobby_fields(db.get_hobby(hobby["id"]))
    return {
        "id": updated["id"],
        "name": updated["name"],
        "slug": updated["slug"],
        "status": updated["status"],
        "group_code": updated["group_code"],
        "flags": updated["flags"],
        "warnings": result.warnings,
        "changes": result.changes,
        "batch_id": result.batch_id,
    }


@mcp.tool()
def constructicon_set_hobby_code(hobby_slug: str, group_code: str) -> dict | None:
    """Set a hobby's 2-4 char group code (the code shown on cards, e.g. COL, 3DP, RCA).

    Letters/digits only, unique across hobbies; stored uppercase. Returns
    {id, name, slug, group_code, changes, batch_id}, not_found if the hobby isn't found, or
    {"ok": false, "error": {code: "bad_group_code" | "group_code_conflict", message}}."""
    hobby = db.get_hobby(hobby_slug)
    if hobby is None:
        raise NotFound(f"No hobby {hobby_slug!r}.")
    result = cards.set_group_code(hobby["id"], group_code)
    updated = db.get_hobby(hobby["id"])
    return {"id": updated["id"], "name": updated["name"], "slug": updated["slug"],
            "group_code": updated["group_code"], "changes": result.changes, "batch_id": result.batch_id}


@mcp.tool()
def constructicon_list_blog_entries(status: str | None = None) -> list[dict]:
    """List all blog entries, optionally filtered by status (e.g. 'draft', 'published').

    Returns entries ordered by most-recently-updated first.
    """
    entries = db.list_blog_entries(status=status)
    return [
        {
            "id": e["id"],
            "slug": e["slug"],
            "title": e["title"],
            "subtitle": e.get("subtitle", ""),
            "status": e["status"],
            "cover_slug": e.get("cover_slug"),
            "content_date": e.get("content_date"),
            "created_at": e["created_at"],
            "updated_at": e["updated_at"],
        }
        for e in entries
    ]


@mcp.tool()
def constructicon_create_blog_entry(title: str, subtitle: str = "", body: str = "", status: str = "draft",
                                    cover_slug: str | None = None, content_date: float | None = None) -> dict:
    """Create a new blog entry.

    title: The entry's title (required; a slug is auto-generated from this).
    subtitle, body: Optional metadata and content.
    status: The entry's status (default 'draft'). Common values: 'draft', 'published'.
    cover_slug: Optional slug of a Constructicon object to use as the entry's cover image.
    content_date: Optional unix timestamp for the entry's content date (distinct from created_at).

    Returns the created entry with projects and items (initially empty).
    """
    result = blog.create(title, subtitle=subtitle, body=body, status=status, cover_slug=cover_slug,
                         content_date=content_date)  # #541 phase D: logged + undoable
    return {**_to_public_blog_entry(result.data["entry"]), "batch_id": result.batch_id}


@mcp.tool()
def constructicon_get_blog_entry(slug: str) -> dict | None:
    """Get a single blog entry by slug, with full hydration: projects and items.

    A missing one returns the not_found error.
    """
    entry = db.get_blog_entry(slug)
    if entry is None:
        raise NotFound(f"No blog entry {slug!r}.")
    return _to_public_blog_entry(entry)


@mcp.tool()
def constructicon_update_blog_entry(slug: str, title: str | None = None, subtitle: str | None = None,
                                    body: str | None = None, status: str | None = None,
                                    cover_slug: str | None = None, content_date: float | None = None,
                                    clear_cover_slug: bool = False, clear_content_date: bool = False) -> dict | None:
    """Update a blog entry. Every field defaults to "leave unchanged".

    Pass a value to set it. cover_slug/content_date can't be cleared just by
    passing None (None means "leave unchanged" here), so to clear one, set its
    clear_* flag instead: clear_cover_slug / clear_content_date.

    (The Ellipsis sentinel blog.update uses internally can't cross the
    MCP tool boundary — the schema generator treats an Ellipsis default as a
    required arg — so this tool maps None/clear-flags onto that sentinel.)

    Returns the updated entry with full hydration; a missing one returns the not_found error.
    """
    cover = None if clear_cover_slug else (cover_slug if cover_slug is not None else ...)
    cdate = None if clear_content_date else (content_date if content_date is not None else ...)
    result = blog.update(slug, title=title, subtitle=subtitle, body=body, status=status,
                         cover_slug=cover, content_date=cdate)  # not_found for an unknown slug
    return {**_to_public_blog_entry(result.data["entry"]), "batch_id": result.batch_id}


@mcp.tool()
def constructicon_delete_blog_entry(slug: str) -> bool:
    """Delete a blog entry and all its attached projects/items (the cards and files stay).
    Undoable since #541 phase D: find the batch with constructicon_list_changes and pass it to
    constructicon_undo.

    Returns True if deleted; an unknown slug returns the not_found error.
    """
    blog.delete(slug)
    return True


@mcp.tool()
def constructicon_set_blog_entry_projects(slug: str, items: list[dict]) -> dict | None:
    """Set the ordered list of projects attached to a blog entry.

    items: list of dicts with 'project_id' (int) and optional 'note' (str) keys,
      in desired display order. Each dict becomes a (project_id, note) tuple.

    Returns the updated entry with full hydration; a missing one returns the not_found error.
    """
    entry = db.get_blog_entry(slug)
    if entry is None:
        raise NotFound(f"No blog entry {slug!r}.")

    # Convert dicts to (project_id, note) tuples. #541 phase D: logged + undoable; an unknown card
    # (id or slug) is not_found instead of a row no page can show.
    project_items = [(item.get("project_id"), item.get("note", "")) for item in items]
    result = blog.set_projects(entry["id"], project_items)
    return {**_to_public_blog_entry(result.data["entry"]), "batch_id": result.batch_id}


@mcp.tool()
def constructicon_set_blog_entry_items(slug: str, items: list[dict]) -> dict | None:
    """Set the ordered list of Constructicon objects attached to a blog entry.

    items: list of dicts with 'slug' (str) and optional 'note' (str) keys,
      in desired display order. Each dict becomes a (post_slug, note) tuple.

    Returns the updated entry with full hydration; a missing one returns the not_found error.
    """
    entry = db.get_blog_entry(slug)
    if entry is None:
        raise NotFound(f"No blog entry {slug!r}.")

    # Convert dicts to (post_slug, note) tuples. #541 phase D: logged + undoable; unknown file = not_found.
    post_items = [(item.get("slug"), item.get("note", "")) for item in items]
    result = blog.set_items(entry["id"], post_items)
    return {**_to_public_blog_entry(result.data["entry"]), "batch_id": result.batch_id}


@mcp.tool()
def constructicon_list_tags() -> list[dict]:
    """Get the complete tag tree (hierarchical).

    Returns the root-level tags, each with a nested children array.
    """
    return db.list_tag_tree()


@mcp.tool()
def constructicon_create_tag(name: str, parent_name: str | None = None) -> dict:
    """Create a tag or return the existing one if it already exists under the parent.

    Returns the tag metadata.
    """
    # #541 phase C: a created tag (and parent) is imaged, so constructicon_undo removes it.
    return tags_svc.create(name, parent_name).data["tag"]


@mcp.tool()
def constructicon_get_posts_for_tag(tag_name: str) -> list[dict]:
    """Get all objects tagged with a specific tag (including all descendant tags).

    Returns a list of objects.
    """
    tag = tags_svc.find_any(tag_name)  # #563: a lookup must not create the tag
    if tag is None:
        raise NotFound(f"No such tag: {tag_name!r}")
    rows = db.list_posts_for_tag(tag["id"], limit=10000)
    return [_to_public(r) for r in rows]


@mcp.tool()
def constructicon_attach_tags(slug: str, tag_names: list[str]) -> dict | None:
    """Add tags to an object.

    Returns the updated object; a missing one returns the not_found error.
    """
    tags_svc.attach(slug, tag_names)  # #541 phase C: one batch; tags it creates are imaged too
    return _to_public(db.get_by_slug(slug))


@mcp.tool()
def constructicon_detach_tag(slug: str, tag_name: str) -> dict | None:
    """Remove a tag from an object.

    Returns the updated object; a missing one returns the not_found error.
    """
    row = db.get_by_slug(slug)
    if row is None:
        raise NotFound(f"No item {slug!r}.")
    tag = tags_svc.find_any(tag_name)  # #563: a lookup must not create the tag
    if tag is None:
        raise NotFound(f"No such tag: {tag_name!r}")
    tags_svc.detach(slug, tag["id"])
    return _to_public(db.get_by_slug(slug))


@mcp.tool()
def constructicon_retry_ocr(slug: str) -> dict | None:
    """Force OCR to run (or re-run) on an object.

    Returns the updated object; a missing one returns the not_found error. Raises an error if
    the object is redacted or doesn't support OCR.
    """
    row = db.get_by_slug(slug)
    if row is None:
        raise NotFound(f"No item {slug!r}.")
    if row["redacted"]:
        raise ValueError("File was redacted — there's no content left to OCR")
    spec = object_types.get_object_type(row.get("media_type"))
    if not spec.ocr_capable:
        raise ValueError(f"OCR isn't available for {spec.label} content")
    db.set_ocr_status(slug, "pending")
    actor_ctx.spawn(ocr.run_ocr, slug)
    return _to_public(db.get_by_slug(slug))


@mcp.tool()
def constructicon_set_agent_notes(slug: str, notes: str | None = None) -> dict | None:
    """Set or clear agent-authored working notes for an object (#206).

    Agent-only scratch space for Claude working state, never exposed in the
    public API. For example: "this is a Fusion 360 screenshot, not user-facing"
    or "already inventoried, skip on re-run."

    notes: the note text, or None to clear existing notes.
    Returns the updated object (the usual public shape plus "agent_notes"),
    (a missing one returns the not_found error).
    """
    items.update(slug, agent_notes=notes)  # #541 phase D: through the item service (not_found if missing)
    row = db.get_by_slug(slug)
    # Never return the raw DB row: it carries the `embedding` BLOB, which
    # isn't JSON-serializable, so the tool call itself failed for every row
    # that had been through OCR (#211).
    return {**_to_public(row), "agent_notes": row.get("agent_notes")}


@mcp.tool()
def constructicon_list_provenance_options(scope: str = "card", include_retired: bool = False) -> dict:
    """The live, owner-editable provenance lists (#529). Read-only; the owner adds,
    renames, retires and reorders options in /admin.

    scope: "card" (what constructicon_set_card_provenance accepts) or "file" (what
    constructicon_set_provenance accepts). include_retired=true also returns retired
    options (retired ones can't be set on anything new).
    Returns {"ok": true, "scope", "options": [{key, label, sort_order, retired}]}.
    """
    opts = provenance_options.list_options(scope, include_retired=include_retired)
    return {"ok": True, "scope": scope,
            "options": [{k: o[k] for k in ("key", "label", "sort_order", "retired")} for o in opts]}


@mcp.tool()
def constructicon_set_provenance(slug: str, provenance: str | None = None) -> dict | None:
    """Set or clear an object's provenance classification (#341).

    Provenance describes how an object came to be captured. The allowed values are
    an editable list (#529) the owner manages in /admin; it starts as found, created,
    documented, result (outcome of a process), reference (cited or sourced from
    elsewhere), design (drafted/designed) and purchased. Call
    constructicon_list_provenance_options(scope="file") for the live list. A value
    that isn't an active key returns {"ok": false, "error": {"code":
    "bad_provenance", ...}} naming the active keys; an object that already holds a
    since-retired key keeps it.

    provenance: an active key, or None to clear.
    Returns the updated object (the usual public shape plus "provenance"),
    (a missing one returns the not_found error).
    """
    row = items.update(slug, provenance=provenance).item
    return {**_to_public(row), "provenance": row.get("provenance")}


@mcp.tool()
def constructicon_set_content_date(slug: str, date: str | None = None) -> dict | None:
    """Set or clear an object's content_date — the content's OWN real-world date
    (#415), the date sibling of constructicon_set_provenance. Distinct from the
    upload timestamp; it's what the Timeline and date displays key off.

    date:
      - an ISO date or datetime string, e.g. "2017-07-31" or "2017-07-31 14:30"
        or "2017-07-31T14:30:00". A naive (offset-less) value is interpreted as
        Mountain Time (America/Denver), matching the archive's timeline
        convention; a string carrying an explicit offset is trusted as-is.
      - a bare unix-seconds number (as a string) is used directly.
      - None or "" clears the date.

    Returns the updated object (usual public shape plus "content_date" in unix
    seconds); not_found if the object doesn't exist.
    """
    items.get_item(slug)
    row = items.update(slug, content_date=items.parse_date(date)).item  # naive ISO -> Mountain Time
    return {**_to_public(row), "content_date": row.get("content_date")}


@mcp.tool()
def constructicon_set_highlight(slug: str, on: bool = False) -> dict | None:
    """Mark or unmark an object as highlighted (#341).

    Highlight is an orthogonal boolean flag for "cool/unique" objects —
    featured in project exports and curated views.

    on: True to mark as highlighted, False to clear.
    Returns the updated object (the usual public shape plus "highlight"),
    (a missing one returns the not_found error).
    """
    row = items.update(slug, highlight=on).item
    return {**_to_public(row), "highlight": row.get("highlight")}


@mcp.tool()
def constructicon_set_brand_asset(slug: str, is_brand: bool = False, brand_role: str | None = None) -> dict | None:
    """Mark or unmark an object as a brand asset (#350).

    Brand assets are reusable branding objects (logos, icons, colors, etc.)
    that live outside the project/hobby structure and are consumed by
    export templates.

    slug: the object's slug.
    is_brand: True to mark as a brand asset, False to clear.
    brand_role: optional role label (e.g., "logo", "icon", "color").
                One of the standard BRAND_ROLES or a custom value.
                Ignored when is_brand is False; brand_role is cleared
                when the flag is cleared.

    Returns the updated object (the usual public shape plus
    "is_brand_asset" and "brand_role"); a missing one returns the not_found error.
    """
    row = items.update(slug, is_brand_asset=is_brand, brand_role=brand_role).item
    return {**_to_public(row), "is_brand_asset": bool(row.get("is_brand_asset")), "brand_role": row.get("brand_role")}


@mcp.tool()
def constructicon_list_brand_assets() -> list[dict]:
    """List all brand assets (#350), grouped by role and ordered by recency.

    Returns a list of brand asset dicts (the usual public shape plus
    "is_brand_asset" and "brand_role").
    """
    assets = db.list_brand_assets()
    return [
        {**_to_public(asset), "is_brand_asset": bool(asset.get("is_brand_asset")), "brand_role": asset.get("brand_role")}
        for asset in assets
    ]


@mcp.tool()
def constructicon_set_project_status(id_or_slug: str, status: str) -> dict | None:
    """DEPRECATED alias of constructicon_set_status that still accepts the v1 words.

    Legacy words are translated to a stage and the result carries `warnings`
    saying so: wip/active -> in_progress; complete/archived/published/
    means-to-an-end -> done (ambiguous: use set_status with in_use if it is still
    in use); shelved -> paused; abandoned -> stopped (abandoned); failed -> stopped
    (failed); idea -> idea; reference-only -> kind collection + in_use. A v2 stage
    name is accepted too. Returns the updated project dict (with `warnings`), not_found
    if it doesn't exist, or {"ok": false, "error": {code, message}} for an invalid status.
    """
    if db.get_project(id_or_slug) is None:
        raise NotFound(f"No card {id_or_slug!r}.")
    legacy = card_rules.legacy_to_status(status)
    warnings = list(legacy["warnings"])
    if legacy["kind"]:
        warnings.extend(cards.set_kind(id_or_slug, legacy["kind"]).warnings)
    warnings.extend(cards.set_status(id_or_slug, legacy["stage"], legacy["stop_reason"]).warnings)
    return {**_to_public_project(db.get_project(id_or_slug)), "warnings": warnings}


@mcp.tool()
def constructicon_set_status(card: str | int, stage: str, stop_reason: str | None = None,
                             activity: str | None = None, dry_run: bool = False) -> dict:
    """Set a card's status: stage (+ stop_reason when stopped). V2 cards 3.2.

    stage: in_progress | in_use (both active) or idea | paused | done | stopped
    (all inactive). Activity is derived from the stage; passing `activity` that
    disagrees with it is an error. stopped REQUIRES stop_reason failed|abandoned,
    and a stop_reason on any other stage is an error. An idea can never be active.
    An event only allows idea / in_progress / done / stopped. Any stage can follow
    any other (no state machine). Rule violations return
    {"ok": false, "error": {"code": "bad_status", "message": ...}}; nothing is written.

    card: project id or slug. dry_run=true previews without writing.
    Returns {ok, dry_run, changes: [{card, field, before, after}], warnings, batch_id}.
    """
    return cards.set_status(card, stage, stop_reason, activity=activity, dry_run=dry_run).to_dict()


@mcp.tool()
def constructicon_set_kind(card: str | int, kind: str, force: bool = False, dry_run: bool = False) -> dict:
    """Set a card's kind: project | thing | action | family | collection | event.

    A thing is one physical object (a one-object build is a Thing, not a Project).
    family and collection are group kinds: they can't be nested under another
    card or have nested children. Leaving a group kind that has members is refused
    unless force=true (drops the memberships). The card's current stage must stay
    valid for the new kind (an event can't be in use or paused).

    Returns {ok, dry_run, changes, warnings, batch_id} or
    {"ok": false, "error": {code, message}}.
    """
    return cards.set_kind(card, kind, force=force, dry_run=dry_run).to_dict()


@mcp.tool()
def constructicon_set_whereabouts(card: str | int, whereabouts: str | None = None, note: str | None = None,
                                  clear_note: bool = False, dry_run: bool = False) -> dict:
    """Set (or clear) where a card's physical thing is now. V2 cards 3.4.

    whereabouts: have_it | partial | parted_out | sold | gifted | lost | never_built,
    or omit/null to CLEAR it (null = not recorded). Applies to thing, project and
    collection cards only; an action, event or family is refused. Cross-rules with
    the card's stage: an in_use card can only be have_it or partial; a never_built
    card can't be in_progress or in_use. Violations return
    {"ok": false, "error": {"code": "bad_whereabouts", "message": ...}}; nothing is written.
    note: optional free text ("Skyhawk model on the shelf; parts printed, not
    assembled"); leave it out to keep the existing note, pass clear_note=true to erase it.

    card: project id or slug. dry_run=true previews without writing.
    Returns {ok, dry_run, changes: [{card, field, before, after}], warnings, batch_id}.
    """
    return cards.set_whereabouts(card, whereabouts, "" if clear_note else (note if note is not None else ...),
                                 dry_run=dry_run).to_dict()


@mcp.tool()
def constructicon_set_card_provenance(card: str | int, provenance: str | None = None, credit: str | None = None,
                                      clear_credit: bool = False, dry_run: bool = False) -> dict:
    """Set (or clear) a CARD's provenance and optional credit. V2 cards 3.5.

    provenance: an active key of the editable card list (#529; starts as created |
    found | collected | referenced | client_owned | purchased; the owner manages it in
    /admin, and constructicon_list_provenance_options(scope="card") returns the live
    list), or omit/null to CLEAR it. A card that already holds a since-retired key
    keeps it. One value per card; a card with mixed origins should be split into
    separate cards, never "found + own". credit: who designed it / where it came from
    (leave out to keep the existing credit; clear_credit=true erases it). Bad values
    return {"ok": false, "error": {"code": "bad_provenance", ...}}.

    This is NOT constructicon_set_provenance, which sets the per-FILE provenance
    (found/created/documented/result/reference/design) on one object by file slug and
    is unchanged. A file with no provenance of its own shows its card's for display only.

    card: project id or slug. dry_run=true previews without writing.
    Returns {ok, dry_run, changes, warnings, batch_id}.
    """
    return cards.set_provenance(card, provenance, "" if clear_credit else (credit if credit is not None else ...),
                                dry_run=dry_run).to_dict()


@mcp.tool()
def constructicon_set_card_highlight(card: str | int, on: bool = False, dry_run: bool = False) -> dict:
    """Mark or unmark a CARD as highlighted ("this one is special"). V2 cards 3.12.
    Independent of the per-file highlight (constructicon_set_highlight, which takes a
    file slug). card: project id or slug. Returns {ok, dry_run, changes, warnings, batch_id}."""
    return cards.set_highlight(card, on, dry_run=dry_run).to_dict()


@mcp.tool()
def constructicon_nest(child: str | int, parent: str | int, replace: bool = False, dry_run: bool = False) -> dict:
    """Make `child` PART OF `parent` (nesting: the child only makes sense inside its
    parent, e.g. a Lua script inside the truck it runs on). V2 cards 3.7.

    Refused with {"ok": false, "error": {code, message}}:
    - nest_self: a card can't be part of itself
    - nest_cycle: the parent is already nested under the child
    - nest_group_kind: a family or collection can't be nested or be a parent (use
      constructicon_add_to_family instead)
    - nest_second_parent: the child is already part of another card; pass
      replace=true to move it (or constructicon_unnest first)

    Independent of family membership. dry_run=true previews. Returns
    {ok, dry_run, changes, warnings, batch_id}.
    """
    return cards.nest(child, parent, replace=replace, dry_run=dry_run).to_dict()


@mcp.tool()
def constructicon_unnest(child: str | int, dry_run: bool = False) -> dict:
    """Take `child` out of its parent so it stands on its own (no-op if it has none)."""
    return cards.unnest(child, dry_run=dry_run).to_dict()


@mcp.tool()
def constructicon_add_to_family(family: str | int, member: str | int, dry_run: bool = False) -> dict:
    """Put `member` in a family or collection (V2 cards 3.6). Membership is
    many-to-many: a card can be in several families, and a family has many members;
    it is NOT nesting and moves no files. Adding twice is a no-op.

    `family` must be a card of kind family or collection; `member` must not be one
    (families don't contain each other). Violations return
    {"ok": false, "error": {"code": "bad_membership", ...}}. dry_run=true previews.
    Returns {ok, dry_run, changes, warnings, batch_id}.
    """
    return cards.add_to_family(family, member, dry_run=dry_run).to_dict()


@mcp.tool()
def constructicon_remove_from_family(family: str | int, member: str | int, dry_run: bool = False) -> dict:
    """Take `member` out of a family or collection (no-op if it wasn't in it).
    Neither card is otherwise changed."""
    return cards.remove_from_family(family, member, dry_run=dry_run).to_dict()


# --- Typed links (V2 cards 3.8) ---

@mcp.tool()
def constructicon_link(a: str | int, b: str | int, type: str, note: str = "", dry_run: bool = False,
                       batch_id: str | None = None) -> dict:
    """Link two cards: "a <type> b". V2 cards 3.8.

    type: built_for (a was built for b) | applies_to (a is applied to b) | used_in
    (a is used in b) | inspired_by (a was inspired by b) | related (symmetric, stored
    both ways). Directed types are stored once and read in reverse from b's side
    (constructicon_list_links shows both). A pair can't be both typed and related:
    a typed link over a related pair UPGRADES it (the related rows are removed),
    while related over a typed pair is refused.

    Errors come back as {"ok": false, "error": {code, message}}, same codes as HTTP:
    bad_link (unknown type, self-link, a family/collection as the source of
    built_for/applies_to/used_in), link_conflict (already linked, or related over a
    typed pair), not_found (no such card). dry_run=true previews.
    batch_id (optional): join an earlier call's batch so one constructicon_undo reverses
    them together (e.g. the split_card, link, add_to_hobby of a reorganization).
    Returns {ok, dry_run, changes, warnings, batch_id}.
    """
    return cards.link(a, b, type, note, dry_run=dry_run, batch_id=batch_id).to_dict()


@mcp.tool()
def constructicon_unlink(a: str | int, b: str | int, type: str | None = None, dry_run: bool = False) -> dict:
    """Remove link(s) between two cards. With `type`, only that type ("a <type> b"
    exactly for a directed type; related removes both rows); without it, every link
    between the pair in either direction. A no-op (with a warning) when nothing matches.
    Returns {ok, dry_run, changes, warnings, batch_id} or {"ok": false, "error": ...}."""
    return cards.unlink(a, b, type, dry_run=dry_run).to_dict()


@mcp.tool()
def constructicon_retype_link(a: str | int, b: str | int, from_type: str, to_type: str,
                              note: str | None = None, dry_run: bool = False) -> dict:
    """Change a link's type in one step (the upgrade/downgrade path): replaces the
    `from_type` link between the pair (found in either direction) with
    "a <to_type> b". The note carries over unless `note` is given. Errors: not_found
    (no such link), bad_link (same type / unknown type / group-kind source),
    link_conflict (the new link already exists, or related while another typed link
    is on the pair). dry_run=true previews."""
    return cards.retype_link(a, b, from_type, to_type, note=note, dry_run=dry_run).to_dict()


@mcp.tool()
def constructicon_retype_links(mapping: list[dict], dry_run: bool = True, partial_ok: bool = False) -> dict:
    """Bulk-retype v1 `related` links to typed ones. DRY-RUN BY DEFAULT: pass
    dry_run=false to write.

    mapping: [{a, b, to_type, from_type?='related'}] where each item reads
    "a <to_type> b" (card ids or slugs). Every item is validated first; the whole
    batch is applied in one transaction (all-or-nothing) unless partial_ok=true,
    which applies the valid items and reports the rest. Find candidates with
    constructicon_list_needs_decision(need='untyped_link').
    Returns {ok, dry_run, applied, changes: [{card, field, before, after}], warnings,
    batch_id, items: [{a, b, to_type, ok, changes, error?}]}."""
    return cards.retype_links(mapping, dry_run=dry_run, partial_ok=partial_ok)


@mcp.tool()
def constructicon_list_links(card: str | int) -> list[dict] | dict:
    """Every link on a card, both directions: [{slug, title, kind, stage, type,
    direction, label, note}]. direction is out (this card is the source: "this
    <type> that"), in (this card is the target: labelled in reverse, e.g. "Made
    for this") or both (related, symmetric)."""
    return cards.list_links(card)


@mcp.tool()
def constructicon_list_needs_decision(kind: str | None = None, need: str | None = None,
                                      hobby: str | None = None, limit: int | None = None,
                                      card: str | int | None = None) -> list[dict]:
    """List cards waiting on an owner decision, with a suggested answer for each.

    Two sources. STORED questions queued by the v1 -> v2 migration:
    need = card_status (done vs in use / paused vs collection), card_built_for
    (a means-to-an-end card: which card was it built for), card_kind (Thing or
    Project?), card_family_members (is this nested-parent really a family, and which
    cards belong in it: pick several); answer one with constructicon_resolve_pending_decision
    (decision_id, choice = an option key). COMPUTED needs, recomputed on every
    call and never stored (decision_id is null; card_slug is null and hobby_slug
    is set, since they belong to a hobby): need = hobby_inactive_with_active_work
    (an Inactive hobby that has an active project) and hobby_active_untouched (an
    Active hobby untouched for ~2 years). Fix those with constructicon_set_hobby_status.
    Also computed: need = untyped_link (a v1 'related' pair where a typed reading is
    plausible; `link` = {a, b, type} is the suggestion, apply with constructicon_retype_links).
    Also computed: need = missing_provenance (a card with no provenance; `suggested` is the
    majority file provenance mapped to a card value when >50% of its non-write-up files
    agree, never applied; fix with constructicon_set_card_provenance),
    missing_provenance_credit (a found/collected card with no credit) and
    missing_whereabouts (a Thing with none; fix with constructicon_set_whereabouts).
    Also computed: need = status_conflict (active/inactive disagrees with the parent card, or Done
    with nested work still in progress) and blank_writeup_with_files (the auto-made write-up is
    still empty although the card has files). To clear many stored questions at once, see
    constructicon_resolve_decisions (accept_suggested + dry-run).

    Filters: card (one card, slug or id), kind (the card's kind, or 'hobby' for hobby needs only; any other kind
    leaves hobby needs out), need, hobby (slug), limit.
    Each row: {need, card_slug, title, detail, suggested, suggested_reason,
    confidence, decision_id, options: [{key, label}]}. `suggested` is only a
    suggestion; nothing is applied until the decision is resolved.
    """
    return cards.list_needs_decision(kind=kind, need=need, hobby=hobby, limit=limit, card=card)


# --- V2 cards piece 6: the reorganizing toolkit (spec 7.1 / 7.3) -------------------
# Thin wrappers over core/cards.py; no rule lives here. Single-card tools apply by
# default; bulk tools (bulk_edit, resolve_decisions, retype_links) are DRY-RUN by default.

@mcp.tool()
def constructicon_split_card(source: str | int, parts: list[dict], keep_in_source: bool = False,
                             dry_run: bool = False, batch_id: str | None = None) -> dict:
    """Carve files (and a description) out of one card into new cards. V2 cards 6 / 7.4.

    `parts` is a list; each part is {title, kind='project', relation='sibling'|'child',
    stage, stop_reason, provenance, provenance_credit, whereabouts, whereabouts_note,
    description, move_description, file_slugs: [file slugs], link_to_source: {type, note?}
    or null, hobbies: 'inherit'|[hobby slugs], families: 'inherit'|[family slugs], highlight}.
    A `child` is nested under the source (nest rules apply); a `sibling` has no parent and,
    by default, gets the source's hobbies and family memberships copied (a child gets none
    unless you say so). Each part gets its own kind, provenance, stage and whereabouts, and
    is a full card with the usual blank write-up. Files in file_slugs are MOVED out of the
    source (one file may be named by several parts; it lands in each) unless
    keep_in_source=true (then copied); the source's write-up can't be split out.

    All in one transaction under one batch_id: any rule violation writes nothing. Pass that
    batch_id to later calls (constructicon_link, constructicon_add_to_hobby) to keep the
    whole reorganization in one batch, and constructicon_undo(batch_id) reverses it.
    dry_run=true previews exactly what would happen. Returns {ok, dry_run, changes, warnings,
    batch_id, created: [{id, slug, title, kind, relation, files}]}."""
    return cards.split_card(source, parts, keep_in_source=keep_in_source, dry_run=dry_run,
                            batch_id=batch_id).to_dict()


@mcp.tool()
def constructicon_delete_project(card: str | int, dry_run: bool = False, batch_id: str | None = None) -> dict:
    """Delete a project/card cleanly (#497). Its own auto-made write-up goes with it only while
    it is still blank; a write-up with text is kept as an ordinary unfiled document and named in
    a warning. Links in either direction, family/hobby/file memberships and blog-entry attachments
    are removed; open questions about the card are resolved as stale. Nested children are
    orphaned (stand on their own), never deleted; files themselves are never deleted. Every row
    is imaged: constructicon_undo(batch_id) restores the whole thing. dry_run=true previews.
    Returns {ok, dry_run, changes, warnings, batch_id, deleted, children_orphaned, items_detached, writeup}."""
    res = cards.delete_card(card, dry_run=dry_run, batch_id=batch_id)
    return {**res.to_dict(), **res.data}


@mcp.tool()
def constructicon_merge_cards(keep: str | int, absorb: list[str | int], dry_run: bool = False,
                              batch_id: str | None = None) -> dict:
    """Fold one or more cards into `keep` (the reverse of split). V2 cards 6.

    Files are unioned (deduped); hobbies and family memberships unioned; the absorbed
    cards' children are re-parented to `keep`; links are re-pointed (self-links and duplicates
    dropped; a typed link beats a related one on the same pair); blog-entry attachments
    re-pointed. keep's cover and write-up win; an absorbed card's blank write-up goes with it
    and a real one stays as an ordinary file. Open questions about absorbed cards are resolved
    as stale. The absorbed card rows are deleted but row-imaged: constructicon_undo(batch_id)
    brings them back with the same id and files. Refused (nothing written) with nest_cycle, or
    bad_merge for a family/collection merged with a non-group card. dry_run=true previews.
    Returns {ok, dry_run, changes, warnings, batch_id, keep, absorbed}."""
    return cards.merge_cards(keep, absorb, dry_run=dry_run, batch_id=batch_id).to_dict()


@mcp.tool()
def constructicon_move_files(slugs: list[str], from_card: str | int, to_card: str | int, dry_run: bool = False,
                             batch_id: str | None = None) -> dict:
    """Move files (by slug) from one card to another: they leave from_card and join to_card.
    A card's own write-up can't be moved. Refused with bad_files if a file isn't in from_card.
    dry_run=true previews. Returns {ok, dry_run, changes, warnings, batch_id}."""
    return cards.move_files(slugs, from_card, to_card, dry_run=dry_run, batch_id=batch_id).to_dict()


@mcp.tool()
def constructicon_copy_files(slugs: list[str], from_card: str | int, to_card: str | int, dry_run: bool = False,
                             batch_id: str | None = None) -> dict:
    """Add files from one card to another WITHOUT removing them (files are many-to-many, so
    a photo can live in a project and in a Thing). Same rules as constructicon_move_files."""
    return cards.copy_files(slugs, from_card, to_card, dry_run=dry_run, batch_id=batch_id).to_dict()


@mcp.tool()
def constructicon_add_to_hobby(card: str | int, hobby: str | int, dry_run: bool = False,
                               batch_id: str | None = None) -> dict:
    """Put a card in a hobby (many allowed; adding twice is a no-op). Logged and undoable.
    (constructicon_add_project_to_hobby is the older name for the same thing.)
    Returns {ok, dry_run, changes, warnings, batch_id}."""
    return cards.add_to_hobby(card, hobby, dry_run=dry_run, batch_id=batch_id).to_dict()


@mcp.tool()
def constructicon_remove_from_hobby(card: str | int, hobby: str | int, dry_run: bool = False,
                                    batch_id: str | None = None) -> dict:
    """Take a card out of a hobby (no-op if it wasn't in it). Logged and undoable."""
    return cards.remove_from_hobby(card, hobby, dry_run=dry_run, batch_id=batch_id).to_dict()


@mcp.tool()
def constructicon_set_home(card: str | int, target: str | int | None = None, dry_run: bool = False) -> dict:
    """Override a card's home (the breadcrumb parent / export link target), or clear the override.
    V2 cards 3.10. The automatic home is: the parent, else the first family the card is in, else
    its first hobby, else the home page; a manual override beats all of that.

    target: 'card:<slug>', 'hobby:<slug>', or a bare card slug/id (a bare name that is no card
    is tried as a hobby). Omit/null to CLEAR the override (back to automatic). A card can't be its
    own home (bad_home). A dangling override (target deleted) silently falls back to automatic and
    shows up in constructicon_explain_card's warnings. Home is not something to curate: this is for
    the rare correction. Returns {ok, dry_run, changes, warnings, batch_id}."""
    return cards.set_home(card, target, dry_run=dry_run).to_dict()


@mcp.tool()
def constructicon_explain_card(card: str | int) -> dict:
    """Everything about a card in one call: what to read BEFORE proposing a change. Read-only.
    V2 cards 6.

    Returns identity (id, slug, title, description), kind, activity/stage/stop_reason (+ labels),
    `provisional` (true while the status is still the migration's guess), whereabouts/provenance
    (+ notes, credits), highlight, `hobbies` (with group codes and activity), `families`,
    `members` (for a family/collection), `parent`, `children`, `links` (typed, with direction),
    `home` (resolved + source: override|parent|family|hobby|none, + dangling_override) and
    `home_chain` (the breadcrumb), `files` (total, counts per media type, earliest/latest date),
    `level` (the 0-5 pips: cover, dates, writeup, owner_words, stack, each with a reason),
    `open_decisions` (stored questions, with suggested answers), `needs` (computed needs for this
    card), `warnings`, `suggestions` (computed provenance, suggested typed links) and
    `recent_changes` (the last 5 change-log rows, with batch ids for undo)."""
    return cards.explain_card(card)


@mcp.tool()
def constructicon_bulk_edit(op: str, items: list[dict], dry_run: bool = True, partial_ok: bool = False) -> dict:
    """Run ONE setter over many cards. DRY-RUN BY DEFAULT: pass dry_run=false to write. V2 cards 6.

    op (allow-listed): set_status, set_kind, set_whereabouts, set_card_provenance, set_home,
    set_card_highlight, add_to_hobby, remove_from_hobby, add_to_family, remove_from_family, nest,
    unnest, link, unlink, retype_link, set_hobby_activity.
    items: [{card, args: {...}}] where `card` is the subject and args are that setter's other
    arguments, e.g. op='set_status' -> {card: 'x', args: {stage: 'done'}}; op='add_to_hobby' ->
    {card: 'x', args: {hobby: '3d-printing'}}; op='add_to_family' -> {card: <member>, args:
    {family: <family>}}; op='nest' -> {card: <child>, args: {parent: <parent>}}; op='link' ->
    {card: a, args: {b, type, note?}}; op='set_hobby_activity' -> {card: <hobby slug>, args:
    {value: 'inactive'}}. Unknown arguments are refused.

    Items run in order inside one transaction (later ones see earlier ones). Every item is
    validated and reported with its before/after. All-or-nothing: if ANY item fails nothing is
    written, unless partial_ok=true, which applies the valid ones and reports the rest. One
    batch_id: constructicon_undo(batch_id) reverses the lot.
    Returns {ok, dry_run, op, applied, changes, warnings, batch_id, items: [{card, ok, applied,
    changes, warnings, error?}]}."""
    return cards.bulk(op, items, dry_run=dry_run, partial_ok=partial_ok)


@mcp.tool()
def constructicon_resolve_decisions(items: list, accept_suggested: bool = False, dry_run: bool = True,
                                    partial_ok: bool = False) -> dict:
    """Clear many card questions (card_status / card_built_for / card_kind / card_family_members)
    at once. DRY-RUN BY DEFAULT: pass dry_run=false to write. V2 cards 7.3.

    items: [{decision_id, choice}] (or {decision_id, choices: [..]} for multi-answer questions);
    with accept_suggested=true, just a list of decision ids: each is answered with ITS OWN
    suggested option, and a question with no suggestion is skipped (reported, not an error).
    Find ids and suggestions with constructicon_list_needs_decision. Each answer runs through the
    same validators as a manual edit. One transaction, one batch_id (undoable): if a decision can't
    be applied nothing is written, unless partial_ok=true.
    Returns {ok, dry_run, batch_id, applied, would_apply, skipped, failed, changes, warnings,
    items: [{decision_id, card_slug, choice, status: applied|would_apply|skipped|failed|rolled_back,
    changes, reason?, error?}]}."""
    return cards.resolve_decisions(items, accept_suggested=accept_suggested, dry_run=dry_run,
                                   partial_ok=partial_ok)


@mcp.tool()
def constructicon_undo(audit_id: str | int, force: bool = False, dry_run: bool = False) -> dict:
    """Reverse a change-log entry, or a whole batch. V2 cards 3.13.

    audit_id: one change-log row id (from constructicon_list_changes), or a batch_id (every row
    of that operation or bulk call, reversed newest-first in one transaction). Writes its own
    log row, so an undo can itself be undone. REFUSES, writing nothing, with
    {"ok": false, "error": {code}}: undo_conflict (a row it would restore was changed since;
    the message names the field and values; force=true overrides), undo_refused (a migration row,
    an entry that was already undone, or one that recorded no row images), not_found.
    dry_run=true reports what would be reversed. Returns {ok, dry_run, changes, warnings,
    batch_id, undone: [audit ids], undone_ops}."""
    return cards.undo(audit_id, force=force, dry_run=dry_run).to_dict()


@mcp.tool()
def constructicon_list_changes(card: str | int | None = None, batch_id: str | None = None, limit: int = 50) -> list[dict]:
    """The change log, newest first: [{id, op, actor, batch_id, timestamp, affected_slugs,
    undone_by, mutations}]. Every core write is recorded with row images (table, key, before,
    after). `card` narrows to one card's slug/id; `batch_id` to one operation. Feed an `id` or a
    `batch_id` to constructicon_undo."""
    return cards.list_changes(card=card, batch_id=batch_id, limit=limit)


@mcp.tool()
def constructicon_sweep_stale_decisions(dry_run: bool = False) -> dict:
    """Resolve every open question that can't be answered any more (#551): its file or card was
    deleted, a project-match question has fewer than 2 candidate cards left, or a "does this
    replace...?" question has no candidate left. The web app runs this sweep at startup and hourly;
    listing questions never resolves anything. Logged (op sweep_stale_decisions) and undoable with
    constructicon_undo(batch_id). Returns {ok, dry_run, changes, warnings, batch_id, resolved:
    [{id, kind, post_slug, reason}], count, by_kind}."""
    return decisions.sweep_stale(dry_run=dry_run).to_dict()




# --- Curator Stage 3a: Nudges ---

@mcp.tool()
def constructicon_list_needs(kind: str | None = None, limit: int | None = None) -> list[dict]:
    """Curator Stage 3a: list current nudges (actionable needs).

    Returns a ranked list of nudges, sorted by priority DESC. Each nudge includes:
        - nudge_key: stable id for dismissal
        - kind: nudge type (missing_cover, unfiled_objects, confirm_automatch, etc.)
        - target_type: 'project' or 'global'
        - target_id, target_slug: project id/slug or None for global nudges
        - title, summary: human-readable text
        - priority: computed score (base_impact * status_weight)
        - base_impact, status_weight: components of priority
        - action: descriptor dict (type, project_slug, etc.) for the UI/agent to interpret

    Optional filters:
        - kind: filter by nudge kind (e.g., 'missing_cover', 'unfiled_objects')
        - limit: cap the result count (default: all)

    Dismissed nudges are excluded automatically. Deferred ones stay in the list with
    deferred=true. For the whole picture (questions + nudges + needs grouped by card, with
    deferred items split out) use constructicon_curation_queue.
    """
    needs = curator_needs.list_needs()

    # Filter by kind if requested
    if kind is not None:
        needs = [n for n in needs if n["kind"] == kind]

    # Limit if requested
    if limit is not None:
        needs = needs[:limit]

    return needs


@mcp.tool()
def constructicon_dismiss_need(nudge_key: str) -> dict:
    """Dismiss a nudge or computed need for good. (The timed snooze is gone: to set
    something aside without losing it, use constructicon_defer.)

    nudge_key: the `key` of a nudge/need from constructicon_curation_queue (or the
        nudge_key from constructicon_list_needs). A question ("decision:<id>") can't be
        dismissed: answer it or defer it.

    Returns {ok: true, changed}. Recorded in the change log, so it can be undone."""
    return curation_queue.dismiss(nudge_key)  # a QueueError (400 bad_request) becomes the shared error shape


@mcp.tool()
def constructicon_curation_queue(card: str | None = None, include_deferred: bool = True) -> dict:
    """The Curator queue: every open question, nudge and need in ONE list, grouped by card
    (the same thing the owner sees in the Curator tab).

    Returns {groups, deferred, counts}. Each group is one card (type 'card': slug, title,
    card_kind, activity, last_touched) or a non-card bucket (type 'hobby' = one hobby's
    flags, 'uploads' = file-level questions about fresh uploads, 'collection' = whole-
    collection nudges like unfiled objects). Order: Active cards first, then most recently
    touched; then hobbies, uploads, collection. Inside a group: questions first (lowest
    confidence first), then nudges, then needs.

    Each item: {type: question|nudge|need, key, label, kind, group, href, deferred,
    dismissible} plus, for a question, {id, options, suggested: {picks, labels, reason},
    confidence, multi}. `group` is the details-panel group that fixes it (the page
    /project/<slug>?edit=<group>). Answer a question with constructicon_resolve_pending_decision
    (id, choice) or constructicon_resolve_decisions (accept_suggested). Set something aside with
    constructicon_defer (no timer); bring it back with constructicon_bring_back; dismiss a
    nudge/need for good with constructicon_dismiss_need. counts.open is the number on the
    Curator tab's badge (open, non-deferred items).

    card: limit to one card's slice (slug). include_deferred=false drops the Deferred section."""
    q = curation_queue.build_queue(card=card)
    if not include_deferred:
        q = {**q, "deferred": []}
    return q


@mcp.tool()
def constructicon_defer(key: str) -> dict:
    """Defer a queue item (a question, nudge or need) from constructicon_curation_queue.
    It moves to the trailing Deferred section, with no timer, and stays there until it
    is answered or brought back (constructicon_bring_back). Recorded in the change log.

    key: the item's `key` ("decision:<id>", "<kind>:project:<id>", "need:<need>:<slug>")."""
    return curation_queue.defer(key)  # a QueueError (400 bad_request) becomes the shared error shape


@mcp.tool()
def constructicon_bring_back(key: str) -> dict:
    """Bring a deferred queue item back into the main queue (undo constructicon_defer).
    key: the item's `key`. A dismissed nudge is not brought back by this."""
    return curation_queue.bring_back(key)  # a QueueError (400 bad_request) becomes the shared error shape


@mcp.tool()
def constructicon_list_pending_decisions() -> list[dict]:
    """#240/#446/#448: List all open pending decisions, with automatic stale-cleanup.

    The "Needs your input" queue surfaces two decision kinds:
    - project_match: an upload matched multiple project titles (pick which to attach to)
    - retype: a file's type was deferred (pick the actual media type)

    Returns a list of dicts, each with:
        {
            "id": int (decision id),
            "kind": "project_match" | "retype",
            "slug": str (capture_events.slug),
            "title": str (object display name),
            "media_type": str (current media_type),
            "created_at": float (UTC unix seconds),
            "question": str (retype only),
            "options": [{"key": str, "label": str, ...}] (retype only),
            "candidates": [{"id": int, "title": str, "slug": str}] (project_match only),
        }

    Decisions become stale and are automatically resolved if:
    - The object (post_slug) has been deleted
    - A project_match has fewer than 2 candidates remaining

    For a retype question, answer with constructicon_resolve_pending_decision
    and one option's key as `choice`; the option whose key equals the item's
    current media_type means "keep it as it is".
    """
    result = []
    for item in decisions.list_open():
        if cards.is_card_decision_slug(item["post_slug"]):
            # V2 card question: slug is "card:<project slug>"; carries the suggested answer.
            summary = cards.decision_summary({"id": item["id"], "kind": item["kind"],
                                              "post_slug": item["post_slug"], "payload": item["payload"]})
            result.append({
                "id": item["id"],
                "kind": item["kind"],
                "slug": item["post_slug"],
                "card_slug": summary["card_slug"],
                "title": summary["title"],
                "media_type": None,
                "created_at": item["created_at"],
                "question": summary["question"],
                "legacy_status": summary["legacy_status"],
                "provisional": summary["provisional"],
                "suggested": summary["suggested"],
                "suggested_reason": summary["suggested_reason"],
                "confidence": summary["confidence"],
                "options": summary["options"],
            })
            continue
        row = item["row"]
        # Use the same title logic as the web endpoint
        title = row.get("display_name") or row.get("filename") or row.get("content_description") or row["slug"]

        entry = {
            "id": item["id"],
            "kind": item["kind"],
            "slug": item["post_slug"],
            "title": title,
            "media_type": row.get("media_type"),
            "created_at": item["created_at"],
        }

        if item["kind"] == "project_match":
            entry["candidates"] = item.get("candidates", [])
        elif item["kind"] == "retype":
            entry["question"] = item.get("question", "")
            entry["options"] = item.get("options", [])
        elif item["kind"] == revisions.KIND_ITEM_SUPERSEDES:
            # #477: options are the earlier files this upload might replace (key = their slug) + "none".
            entry["question"] = item.get("question", "")
            entry["options"] = item.get("options", [])
            entry["suggested"] = item.get("suggested")

        result.append(entry)

    return result


@mcp.tool()
def constructicon_mark_superseded(old: str, new: str, dry_run: bool = False, batch_id: str | None = None) -> dict:
    """Revision tracking (#477): file `new` supersedes file `old` ("rev C replaces rev B"), so `old`
    shows "Superseded, see <current>" and drops out of browse listings (it stays findable by search
    and one click away). Both are item slugs. A chain is linear: A -> B -> C, and the CURRENT revision
    is the one with nothing newer. This is the explicit tool; the upload-time "does this replace ...?"
    question is only ever answered by the owner (constructicon_resolve_pending_decision, kind
    item_supersedes, choice = the older file's slug or "none").

    Errors ({"ok": false, "error": {code, message}}): not_found; bad_revision (self-link, or a
    redacted item); revision_cycle (new is already at or before old in the chain);
    revision_conflict (old already has a newer revision, or new already replaces another item).
    dry_run=true validates only. One change-log entry: constructicon_undo(batch_id) reverses it.
    Returns {ok, dry_run, batch_id, old, new, chain: [slugs, oldest first]}."""
    return revisions.mark_superseded(old, new, batch_id=batch_id, dry_run=dry_run)


@mcp.tool()
def constructicon_remove_from_revisions(slug: str, dry_run: bool = False, batch_id: str | None = None) -> dict:
    """Take a file out of its revision chain (#477) and close the gap: A -> B -> C minus B is
    A -> C; minus the oldest, B -> C stands alone; minus the current one, the previous revision
    becomes current. A no-op ({"removed": false}) for a file in no chain. Undoable with
    constructicon_undo(batch_id). Returns {ok, dry_run, batch_id, slug, removed, chain}."""
    return revisions.remove_from_chain(slug, batch_id=batch_id, dry_run=dry_run)


@mcp.tool()
def constructicon_list_revisions(slug: str) -> dict:
    """The revision chain a file belongs to (#477), oldest first: [{slug, title, filename, rev,
    is_current, is_this, redacted}], plus `current` (the newest revision), `superseded` (is this
    file an older one) and this file's `rev` of `of`. {"in_chain": false} for a file with no revisions."""
    if db.get_by_slug(slug) is None:
        raise NotFound(f"No item {slug!r}.")
    return {"ok": True, **revisions.revision_view(slug)}


@mcp.tool()
def constructicon_resolve_pending_decision(decision_id: int, choice: str = "", project_ids: list[int] | None = None,
                                           choices: list[str] | None = None) -> dict:
    """#240/#446/#448: Resolve a pending decision with the owner's choice.

    For project_match decisions:
        project_ids: list of project IDs to attach to (may be empty for "none of these")
        choice: unused

    For retype decisions:
        choice: the media_type key to retype to (or empty to skip)
        project_ids: unused

    For V2 card decisions (kind card_status / card_built_for / card_kind, slug
    "card:<project slug>"):
        choice: one option key from constructicon_list_pending_decisions (e.g. "in_use")
        choices: several option keys for multi-answer questions (card_built_for,
        card_family_members). A family answer turns the card into a family, adds the
        picked cards as members and takes any picked nested child out of the nesting
        (all in one logged batch); "none" leaves everything as it is.
        The option's change runs through the same validators as a manual edit; if it
        is invalid today the decision stays open and {"ok": false, "error": {code,
        message}} comes back. card_built_for answers that create a link (a candidate
        card) aren't available until typed links land (piece 4); "is_event" and
        "none" work now.

    Returns:
        {"ok": true, "applied": [...], "remaining": count}
        where "applied" is the list of actions taken (project ids or the choice)

    On error, the shared shape {"ok": false, "error": {code, message}}: not_found (no such
    decision), already_resolved, unknown_decision_kind, invalid_choice, or a card rule's code.
    """
    if project_ids is None:
        project_ids = []

    return decisions.resolve(decision_id, choice=choice, project_ids=project_ids, choices=choices or ())


@mcp.tool()
def constructicon_run_type_action(slug: str, action: str) -> dict:
    """#448: Run a per-type action on an object. Actions are declared per type
    (ObjectTypeSpec.actions). See constructicon_get for the available actions
    on a specific object.

    slug: object slug
    action: action key (e.g. "fetch_youtube_metadata")

    Returns {ok: true, action: key, item: {...}} with the updated object.
    Errors (shared shape): not_found, redacted, unknown_action, action_failed.
    """
    row = db.get_by_slug(slug)
    if row is None:
        raise NotFound(f"No item {slug!r}.")

    if row.get("redacted"):
        raise InvalidInput("object is redacted", code="redacted")

    spec = object_types.get_object_type(row.get("media_type"))
    action_obj = next((a for a in spec.actions if a.key == action), None)
    if action_obj is None:
        raise InvalidInput(f"{spec.label} has no action '{action}'", code="unknown_action")
    if not action_obj.applies_to(row):  # #446: e.g. Reclassify is .exe-only
        raise InvalidInput(f"{spec.label} has no action '{action}' for this item", code="unknown_action")

    try:
        result = action_obj.handler(row)
    except errors.AppError:
        raise
    except Exception as e:
        print(f"Action '{action}' failed: {e!r}", flush=True)
        raise errors.AppError("action_failed", f"Action '{action}' failed: {e}", status=500) from e

    updated_row = db.get_by_slug(slug)
    return {
        "ok": True,
        "action": action,
        **(result or {}),
        "item": _to_public(updated_row)
    }


def _check_every_tool_wrapped():
    """#560/#548: refuse to start if any registered tool bypassed the actor/error wrapper
    (e.g. registered via mcp.add_tool or the SDK's original decorator)."""
    manager = getattr(mcp, "_tool_manager", None)
    if manager is None:  # SDK internals moved: nothing to check against
        return
    unwrapped = [t.name for t in manager.list_tools() if _WRAPPED_TOOLS.get(t.name) is not t.fn]
    if unwrapped:
        raise RuntimeError(f"MCP tools registered without the actor/error wrapper: {unwrapped}")


_check_every_tool_wrapped()


if __name__ == "__main__":
    # #549: web owns background work (migrations, OCR self-heal, captions). This process only
    # runs the idempotent schema DDL; a stuck OCR row is healed by web's startup pass and its
    # periodic watchdog (same shared DB), and captions are enqueued, never run, here.
    db.init_db(migrate=False)
    mcp.run(transport="streamable-http", host="0.0.0.0", port=8100, stateless_http=True)
