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

from core import db, object_types, ocr, storage, thumbnails

BASE_URL = os.environ.get("IMAGEREPO_BASE_URL", "http://10.12.5.98:8000")

mcp = MCPServer(name="ccc-imagerepo-mcp")


def _to_public(row):
    return {
        "slug": row["slug"],
        "url": f"{BASE_URL}/f/{row['slug']}",
        "filename": row["filename"],
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


@mcp.tool()
def imagerepo_upload(filename: str, content_base64: str, description: str = "", tags: list[str] | None = None,
                      ticket_id: str | None = None, client: str | None = None, uploaded_by: str = db.SOURCE_AUTHORED,
                      source_modified_at: float | None = None) -> dict:
    """Upload an image or document to the repo and get back a stable hotlink URL.

    filename: original filename, used only to determine the extension (.png/.jpg/.jpeg/.pdf/.stl/.psd).
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
