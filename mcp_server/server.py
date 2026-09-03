"""constructicon-mcp — MCP server for the Constructicon media gallery.

Uses the mcp package's v2 MCPServer API
(FastMCP was renamed/restructured in mcp 2.x — see the SDK migration guide).
Streamable-HTTP transport, host/port/stateless_http passed to run().

Exposes MCP tools for uploading, managing, tagging, and organizing media
in a Constructicon instance. Runs as a sidecar alongside constructicon-web.
"""

import base64
import json
import os
import sys
import threading
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from mcp.server.mcpserver import MCPServer

from core import backup, db, object_types, ocr, storage, thumbnails

BASE_URL = os.environ.get("CONSTRUCTICON_BASE_URL", "http://constructicon-web:8000")

mcp = MCPServer(name="constructicon-mcp")


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
    """
    content = base64.b64decode(content_base64)
    dupe = db.find_duplicate(filename, len(content), source_modified_at)
    if dupe is not None:
        return {**_to_public(dupe), "duplicate": True}
    media_type = object_types.detect_media_type(filename)
    if media_type is None:
        return {"error": f"Unsupported file type: {Path(filename).suffix}"}
    spec = object_types.get_object_type(media_type)
    try:
        slug, stored_filename = storage.save_file(filename, content)
    except ValueError as e:
        return {"error": str(e)}
    db.insert_upload(slug, filename, stored_filename, uploaded_by, description, tags,
                      file_size=len(content), source_modified_at=source_modified_at,
                      media_type=media_type,
                      ocr_status="pending" if spec.ocr_capable else None)
    if spec.ocr_capable:
        ocr.run_ocr(slug)
    elif spec.thumbnail_source == object_types.ThumbnailSource.CAPTURE:
        thumbnails.ensure_thumbnail(db.get_by_slug(slug))
    return {**_to_public(db.get_by_slug(slug)), "duplicate": False}


@mcp.tool()
def constructicon_search(query: str | None = None, tags: list[str] | None = None) -> list[dict]:
    """Search objects by description, filename, or tags.

    Returns a JSON list of matching objects. If both query and tags are
    provided, filters by both (AND logic).
    """
    return [_to_public(r) for r in db.search(query=query, tags=tags, client=None)]


@mcp.tool()
def constructicon_get(slug: str) -> dict | None:
    """Get one object's metadata and hotlink URL by its slug.

    Returns None if the object is not found.
    """
    row = db.get_by_slug(slug)
    return _to_public(row) if row else None


@mcp.tool()
def constructicon_update(slug: str, description: str | None = None, tags: list[str] | None = None,
                   display_name: str | None = None, icon: str | None = None,
                   type_metadata: dict | None = None) -> dict | None:
    """Update an object's metadata: description, tags, display name, icon, and/or type-specific fields.

    Pass None for any field you don't want to change. type_metadata is replaced wholesale, not
    merged — read the object's current type_metadata first if you only want to change one key.
    There is no way to change content_description after creation (e.g. a YouTube video's title) —
    the database has no update path for that column, only insert-time.

    Returns the updated object, or None if not found.
    """
    row = db.get_by_slug(slug)
    if row is None:
        return None
    if description is not None or tags is not None:
        row = db.update_tags(slug, description=description, tags=tags, client=None)
    if display_name is not None or icon is not None:
        row = db.rename_object(slug, display_name=display_name, icon=icon)
    if type_metadata is not None:
        db.set_type_metadata(slug, type_metadata)
        row = db.get_by_slug(slug)
    return _to_public(row) if row else None


@mcp.tool()
def constructicon_redact(slug: str) -> dict | None:
    """Delete a file while keeping its metadata (for sensitive content cleanup).

    Irreversible — the file itself cannot be recovered. Metadata (description,
    tags, etc.) is preserved.
    """
    row = db.get_by_slug(slug)
    if row is None:
        return None
    if not row.get("stored_filename"):
        raise ValueError("This object has no uploaded file to redact")
    storage.delete_files(slug, row["stored_filename"])
    return _to_public(db.mark_redacted(slug))


@mcp.tool()
def constructicon_delete(slug: str) -> bool:
    """Fully delete an object — file and all metadata. Irreversible."""
    row = db.get_by_slug(slug)
    if row is None:
        return False
    if row.get("stored_filename"):
        storage.delete_files(slug, row["stored_filename"])
    db.delete_upload(slug)
    return True


@mcp.tool()
def constructicon_delete_multiple(slugs: list[str]) -> dict:
    """Delete multiple objects by slug without touching tags or projects.

    Returns {"deleted": count}. Unknown slugs are silently skipped.
    Irreversible.
    """
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
def constructicon_delete_all() -> dict:
    """Wipe every object, tag, and project — a full reset. Irreversible.

    Call constructicon_backup first if you want to preserve the current content.
    """
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
def constructicon_backup() -> dict:
    """Create a timestamped backup of the entire database and storage.

    Returns {"backup_path": "...","size": ...}.
    """
    return backup.create_backup()


@mcp.tool()
def constructicon_add_content(media_type: str, external_url: str | None = None, content_description: str | None = None,
                           description: str = "", tags: list[str] | None = None,
                           uploaded_by: str = db.SOURCE_AUTHORED) -> dict:
    """Create an object with no uploaded file (e.g., a YouTube link or external document).

    Use constructicon_upload instead for file-backed content.
    """
    spec = object_types.get_object_type(media_type)
    if spec.thumbnail_source == object_types.ThumbnailSource.UPLOADED_FILE:
        raise ValueError(f"{spec.label} objects require a file upload — use constructicon_upload")
    slug = storage.make_slug()
    db.insert_content(
        slug, uploaded_by, media_type,
        external_url=external_url, content_description=content_description,
        description=description, tags=tags, client=None,
    )
    row = db.get_by_slug(slug)
    if spec.ocr_capable and row["ocr_status"] == "pending":
        ocr.run_ocr(slug)
    elif spec.thumbnail_source == object_types.ThumbnailSource.CAPTURE:
        thumbnails.ensure_thumbnail(row)
    return _to_public(db.get_by_slug(slug))


@mcp.tool()
def constructicon_add_related(slug: str, related_slug: str) -> list[dict]:
    """Link two objects as related (bidirectional).

    Also merges tags and project membership between the two.
    Returns the updated list of related objects.
    """
    if db.get_by_slug(slug) is None or db.get_by_slug(related_slug) is None:
        raise ValueError("one or both slugs not found")
    db.add_relation(slug, related_slug)
    return [_to_public(r) for r in db.list_related(slug)]


@mcp.tool()
def constructicon_remove_related(slug: str, related_slug: str) -> list[dict]:
    """Remove a related-object link.

    Returns the updated list of related objects.
    """
    db.remove_relation(slug, related_slug)
    return [_to_public(r) for r in db.list_related(slug)]


@mcp.tool()
def constructicon_get_related(slug: str) -> list[dict]:
    """Get all objects related to this one.

    Returns a list of related objects, both manually linked and auto-detected.
    """
    if db.get_by_slug(slug) is None:
        raise ValueError("slug not found")
    return [_to_public(r) for r in db.list_related(slug)]


@mcp.tool()
def constructicon_list_projects() -> list[dict]:
    """List all projects, most-recently-updated first."""
    return [_to_public_project(p) for p in db.list_projects()]


@mcp.tool()
def constructicon_create_project(title: str, description: str = "", cover_slug: str | None = None) -> dict:
    """Create a new project.

    Also creates a root-level tag with the same name and links it, so tagged
    objects surface through both project and tag browsing.
    """
    title = title.strip()
    if not title:
        raise ValueError("Project name can't be empty")
    tag = db.get_or_create_tag(title, parent_id=None)
    project = db.create_project(title, description=description, cover_slug=cover_slug, tag_id=tag["id"])
    return _to_public_project(project)


@mcp.tool()
def constructicon_update_project(project_id: str | int, title: str | None = None,
                                 description: str | None = None, cover_slug: str | None = None,
                                 status: str | None = None) -> dict | None:
    """Update a project's metadata.

    Returns the updated project, or None if not found.
    """
    project = db.update_project(project_id, title=title, description=description,
                                cover_slug=cover_slug, status=status)
    return _to_public_project(project) if project else None


@mcp.tool()
def constructicon_add_to_project(slug: str, project_id: str | int) -> list[dict]:
    """Add an object to a project.

    If the project has a linked tag, the object is also tagged with it.
    Returns the object's updated project list.
    """
    if db.get_by_slug(slug) is None:
        raise ValueError("slug not found")
    if db.get_project(project_id) is None:
        raise ValueError("project not found")
    project = db.get_project(project_id)
    db.add_item_to_project(project["id"], slug)
    if project.get("tag_id"):
        db.attach_tags(slug, [project["tag_id"]])
    return [_to_public_project(p) for p in db.list_projects_for_post(slug)]


@mcp.tool()
def constructicon_remove_from_project(slug: str, project_id: str | int) -> list[dict]:
    """Remove an object from a project (does not untag it).

    Returns the object's updated project list.
    """
    if db.get_by_slug(slug) is None:
        raise ValueError("slug not found")
    project = db.get_project(project_id)
    if project is None:
        raise ValueError("project not found")
    db.remove_item_from_project(project["id"], slug)
    return [_to_public_project(p) for p in db.list_projects_for_post(slug)]


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
    parent_id = None
    if parent_name:
        parent = db.get_or_create_tag(parent_name, parent_id=None)
        parent_id = parent["id"]
    tag = db.get_or_create_tag(name, parent_id=parent_id)
    return tag


@mcp.tool()
def constructicon_get_posts_for_tag(tag_name: str) -> list[dict]:
    """Get all objects tagged with a specific tag (including all descendant tags).

    Returns a list of objects.
    """
    tag = db.get_or_create_tag(tag_name, parent_id=None)
    rows = db.list_posts_for_tag(tag["id"], limit=10000)
    return [_to_public(r) for r in rows]


@mcp.tool()
def constructicon_attach_tags(slug: str, tag_names: list[str]) -> dict | None:
    """Add tags to an object.

    Returns the updated object, or None if not found.
    """
    row = db.get_by_slug(slug)
    if row is None:
        return None
    tag_ids = []
    for name in tag_names:
        tag = db.get_or_create_tag(name, parent_id=None)
        tag_ids.append(tag["id"])
    if tag_ids:
        db.attach_tags(slug, tag_ids)
    return _to_public(db.get_by_slug(slug))


@mcp.tool()
def constructicon_detach_tag(slug: str, tag_name: str) -> dict | None:
    """Remove a tag from an object.

    Returns the updated object, or None if not found.
    """
    row = db.get_by_slug(slug)
    if row is None:
        return None
    tag = db.get_or_create_tag(tag_name, parent_id=None)
    db.detach_tag(slug, tag["id"])
    return _to_public(db.get_by_slug(slug))


@mcp.tool()
def constructicon_retry_ocr(slug: str) -> dict | None:
    """Force OCR to run (or re-run) on an object.

    Returns the updated object, or None if not found. Raises an error if
    the object is redacted or doesn't support OCR.
    """
    row = db.get_by_slug(slug)
    if row is None:
        return None
    if row["redacted"]:
        raise ValueError("File was redacted — there's no content left to OCR")
    spec = object_types.get_object_type(row.get("media_type"))
    if not spec.ocr_capable:
        raise ValueError(f"OCR isn't available for {spec.label} content")
    db.set_ocr_status(slug, "pending")
    threading.Thread(target=ocr.run_ocr, args=(slug,), daemon=True).start()
    return _to_public(db.get_by_slug(slug))


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
