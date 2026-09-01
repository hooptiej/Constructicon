"""ccc-imagerepo-mcp — MCP server for the Computer Cats image/document repo.

Sandbox build on CCC-SV-Dev1. Uses the mcp package's v2 MCPServer API
(FastMCP was renamed/restructured in mcp 2.x — see the SDK migration guide).
Streamable-HTTP transport, host/port/stateless_http passed to run().
"""

import base64
import os
import sys
import threading
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from mcp.server.mcpserver import MCPServer

from core import backup, db, object_types, ocr, storage, thumbnails, youtube

BASE_URL = os.environ.get("IMAGEREPO_BASE_URL", "http://10.12.5.98:8000")

mcp = MCPServer(name="ccc-imagerepo-mcp")


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
        "ticket_id": row["ticket_id"],
        "client": row["client"],
        "redacted": bool(row["redacted"]),
        "source": row["source"],
        "extracted_text": row["extracted_text"],
        "ocr_status": row["ocr_status"],
        "artifact_link": f"{BASE_URL}{row['artifact_link']}" if row["artifact_link"] else None,
    }


def _to_public_project(project):
    return {
        "id": project["id"],
        "slug": project["slug"],
        "title": project["title"],
        "description": project["description"],
        "status": project["status"],
        "cover_slug": project.get("cover_slug"),
    }


@mcp.tool()
def imagerepo_upload(filename: str, content_base64: str, description: str = "", tags: list[str] | None = None,
                      ticket_id: str | None = None, client: str | None = None, uploaded_by: str = db.SOURCE_AUTHORED,
                      source_modified_at: float | None = None) -> dict:
    """Upload an image or document to the repo and get back a stable hotlink URL.

    filename: original filename, used only to determine the extension (.png/.jpg/.jpeg/.pdf/.stl/.psd/.svg/.eps/.mp3/.m4a/.ogg/.wav).
    content_base64: raw file bytes, base64-encoded.
    uploaded_by: the Source string to record (capture_events.tech) — defaults to
      "Claude — authored" (this tool call created the content directly). Pass
      db.source_migrated_from("<source>") instead when the content is being brought
      in from somewhere else rather than authored fresh.
    source_modified_at: the source file's own last-modified time (unix seconds), if known —
      used to detect re-uploads of the exact same file. If omitted, duplicate detection is skipped.
    If filename, file size, and source_modified_at all match an existing entry, no new row is
    created — the existing entry is returned instead with "duplicate": true.
    """
    content = base64.b64decode(content_base64)
    dupe = db.find_duplicate(filename, len(content), source_modified_at)
    if dupe is not None:
        return {**_to_public(dupe), "duplicate": True}
    ext = Path(filename).suffix.lower()
    if ext in storage.PDF_EXTENSIONS:
        media_type = "pdf"
    elif ext in storage.STL_EXTENSIONS:
        media_type = "stl"
    elif ext in storage.PSD_EXTENSIONS:
        media_type = "psd"
    elif ext in storage.AUDIO_EXTENSIONS:
        media_type = "audio"
    elif ext in storage.SVG_EXTENSIONS:
        media_type = "svg"
    elif ext in storage.EPS_EXTENSIONS:
        media_type = "eps"
    else:
        media_type = "image"
    spec = object_types.get_object_type(media_type)
    slug, stored_filename = storage.save_file(filename, content)
    db.insert_upload(slug, filename, stored_filename, uploaded_by, description, tags, ticket_id, client,
                      file_size=len(content), source_modified_at=source_modified_at,
                      media_type=media_type,
                      ocr_status="pending" if spec.ocr_capable else None)
    if spec.ocr_capable:
        ocr.run_ocr(slug)
    elif spec.thumbnail_source == object_types.ThumbnailSource.CAPTURE:
        # No OCR pass to piggyback a thumbnail render onto for a
        # CAPTURE-sourced, non-OCR-capable type (STL) — see the matching
        # comment on web/app.py's _ensure_capture_thumbnail. Synchronous
        # here since this MCP tool call has no background-task mechanism.
        thumbnails.ensure_thumbnail(db.get_by_slug(slug))
    return {**_to_public(db.get_by_slug(slug)), "duplicate": False}


@mcp.tool()
def imagerepo_search(query: str | None = None, tags: list[str] | None = None, client: str | None = None) -> list[dict]:
    """Search uploaded images/documents by description/filename text, tags, or client name."""
    return [_to_public(r) for r in db.search(query=query, tags=tags, client=client)]


@mcp.tool()
def imagerepo_get(slug: str) -> dict | None:
    """Get one upload's metadata and hotlink URL by its slug."""
    row = db.get_by_slug(slug)
    return _to_public(row) if row else None


@mcp.tool()
def imagerepo_tag(slug: str, description: str | None = None, tags: list[str] | None = None,
                   ticket_id: str | None = None, client: str | None = None) -> dict | None:
    """Update an existing upload's description, tags, linked ticket, or client."""
    row = db.update_tags(slug, description=description, tags=tags, ticket_id=ticket_id, client=client)
    return _to_public(row) if row else None


@mcp.tool()
def imagerepo_redact(slug: str) -> dict | None:
    """Delete just the file (e.g. it has a visible password or other sensitive
    content) while keeping the description/tags/ticket/client metadata for
    future correlation. Irreversible — the file itself cannot be recovered."""
    row = db.get_by_slug(slug)
    if row is None:
        return None
    storage.delete_files(slug, row["stored_filename"])
    return _to_public(db.mark_redacted(slug))


@mcp.tool()
def imagerepo_delete(slug: str) -> bool:
    """Fully delete an upload — file and all metadata. Irreversible."""
    row = db.get_by_slug(slug)
    if row is None:
        return False
    storage.delete_files(slug, row["stored_filename"])
    db.delete_upload(slug)
    return True


@mcp.tool()
def imagerepo_delete_selected(slugs: list[str]) -> dict:
    """Delete a specific set of objects (any mix of media types) by slug,
    without touching tags or projects — the MCP equivalent of the web app's
    selective-delete gallery checkboxes (POST /api/delete, #19). Irreversible.
    Unknown slugs are silently skipped rather than erroring the whole batch."""
    deleted = 0
    for slug in slugs:
        row = db.get_by_slug(slug)
        if row is None:
            continue
        if row.get("stored_filename"):
            storage.delete_files(slug, row["stored_filename"])
        db.delete_upload(slug)
        deleted += 1
    return {"deleted": deleted}


@mcp.tool()
def imagerepo_delete_all() -> dict:
    """Wipe every object (and its files), plus all tags and projects — a
    full reset. The MCP equivalent of the web app's POST /api/delete-all.
    Irreversible — call imagerepo_backup first if the current content is
    worth keeping."""
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
    return {"deleted": len(rows)}


@mcp.tool()
def imagerepo_backup() -> dict:
    """Zip the DB and every file in storage/ into a timestamped archive,
    pruning down to the most recent retained backups — the MCP equivalent
    of the web app's POST /api/backup (#20)."""
    return backup.create_backup()


@mcp.tool()
def imagerepo_rename(slug: str, display_name: str | None = None, icon: str | None = None) -> dict | None:
    """Set an object's display name and/or icon override (#11) — the two
    still-missing pieces of "Objects - Move, delete, rename, nesting, set
    Display name and Icon" from the original issue. Neither field existed
    anywhere in the app before this; both are optional per-object overrides
    that fall back to the existing filename/content_description/slug and
    media-type badge_icon behavior when unset. Pass "" to clear a field back
    to its fallback. The MCP equivalent of POST /api/image/<slug> with a
    display_name and/or icon field.

    Note: there's still no "move" (relocate into a folder/parent) or
    "nesting" concept for objects anywhere in the app — see this issue's PR
    description for why that's being left as a larger follow-up rather than
    built here."""
    row = db.rename_object(slug, display_name=display_name, icon=icon)
    return _to_public(row) if row else None


@mcp.tool()
def imagerepo_list_projects() -> list[dict]:
    """List every project, most-recently-updated first — the MCP equivalent
    of GET /api/projects."""
    return [_to_public_project(p) for p in db.list_projects()]


@mcp.tool()
def imagerepo_create_project(title: str) -> dict:
    """Create a new project (and a same-named root tag it's linked to, so
    objects tagged into it surface through ordinary tag browsing too) — the
    MCP equivalent of POST /api/projects."""
    title = title.strip()
    if not title:
        raise ValueError("Project name can't be empty")
    tag = db.get_or_create_tag(title, parent_id=None)
    project = db.create_project(title, tag_id=tag["id"])
    return _to_public_project(project)


@mcp.tool()
def imagerepo_add_content(media_type: str, external_url: str | None = None, content_description: str | None = None,
                           description: str = "", tags: list[str] | None = None, ticket_id: str | None = None,
                           client: str | None = None, uploaded_by: str = db.SOURCE_AUTHORED) -> dict:
    """Create an object with no uploaded file — a YouTube link today, any
    future URL/stream capture type — the MCP equivalent of POST /api/content.
    Use imagerepo_upload instead for anything backed by an actual file."""
    spec = object_types.get_object_type(media_type)
    if spec.thumbnail_source == object_types.ThumbnailSource.UPLOADED_FILE:
        raise ValueError(f"{spec.label} objects require a file upload — use imagerepo_upload")
    slug = storage.make_slug()
    # #44: same oEmbed-based enrichment as web/app.py's POST /api/content —
    # see core/youtube.py for exactly what's fetched/kept and why.
    type_metadata = None
    if media_type == "youtube" and external_url:
        oembed_title, oembed_metadata = youtube.youtube_metadata_for_content(external_url)
        if not content_description and oembed_title:
            content_description = oembed_title
        type_metadata = oembed_metadata or None
    db.insert_content(
        slug, uploaded_by, media_type,
        external_url=external_url, content_description=content_description,
        description=description, tags=tags, ticket_id=ticket_id, client=client,
        type_metadata=type_metadata,
    )
    row = db.get_by_slug(slug)
    if spec.ocr_capable and row["ocr_status"] == "pending":
        ocr.run_ocr(slug)
    elif spec.thumbnail_source == object_types.ThumbnailSource.CAPTURE:
        thumbnails.ensure_thumbnail(row)
    return _to_public(db.get_by_slug(slug))


@mcp.tool()
def imagerepo_relate(slug: str, related_slug: str) -> list[dict]:
    """Link two objects as related (bidirectional) — also merges tags and
    project membership both ways (see core/db.py's add_relation), the MCP
    equivalent of POST /api/image/<slug>/related."""
    if db.get_by_slug(slug) is None or db.get_by_slug(related_slug) is None:
        raise ValueError("one or both slugs not found")
    db.add_relation(slug, related_slug)
    return [_to_public(r) for r in db.list_related(slug)]


@mcp.tool()
def imagerepo_unrelate(slug: str, related_slug: str) -> list[dict]:
    """Remove a related-object link — the MCP equivalent of
    POST /api/image/<slug>/related/remove."""
    db.remove_relation(slug, related_slug)
    return [_to_public(r) for r in db.list_related(slug)]


if __name__ == "__main__":
    db.init_db()
    # Self-heal: this process (or the web one) may have been killed while OCR
    # was still queued/running for a row, leaving it stuck at "pending"
    # forever otherwise — shared DB, so either process catches the other's.
    # Fired as background threads, not run here directly, so a pile-up of
    # stuck rows can't block this process from ever reaching mcp.run().
    # The periodic watchdog for rows that go stale while this process stays
    # up lives in web/app.py — same shared DB, no need to duplicate it here.
    stuck = db.list_pending_ocr()
    if stuck:
        print(f"re-running OCR for {len(stuck)} row(s) left pending by a prior process")
        for row in stuck:
            db.set_ocr_status(row["slug"], "pending")
            threading.Thread(target=ocr.run_ocr, args=(row["slug"],), daemon=True).start()
    mcp.run(transport="streamable-http", host="0.0.0.0", port=8100, stateless_http=True)
