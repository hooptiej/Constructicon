"""Export a project as a zip file for offline/agent analysis.

This module provides functionality to bundle a project's contents (metadata,
items, and all uploaded files) into a single zip archive. The exported zip
contains:
- manifest.json: Project metadata and item descriptions
- files/: Directory containing all uploaded files for items in the project
"""

import io
import json
import zipfile
from pathlib import Path

from core import db, storage


def export_project(project_id_or_slug):
    """Export a project as a zip file containing manifest and all files.

    Args:
        project_id_or_slug: Numeric project id or slug string

    Returns:
        bytes: The zip file contents as bytes (suitable for streaming)

    Raises:
        ValueError: If the project is not found
    """
    project = db.get_project(project_id_or_slug)
    if project is None:
        raise ValueError(f"Project not found: {project_id_or_slug}")

    # Fetch all items in this project
    items = db.list_project_items(project["id"])

    # Fetch writeup body if writeup_slug is set
    writeup_body = None
    if project.get("writeup_slug"):
        writeup_doc = db.get_by_slug(project["writeup_slug"])
        if writeup_doc:
            writeup_body = writeup_doc.get("type_metadata", {}).get("body", "")

    # Build the manifest
    manifest = {
        "project": {
            "id": project["id"],
            "slug": project["slug"],
            "title": project["title"],
            "description": project.get("description"),
            "status": project.get("status"),
            "writeup_body": writeup_body,
        },
        "items": [],
    }

    # Collect item data for manifest and prepare files to include
    files_to_include = []  # List of (arcname, file_path) tuples

    for item in items:
        item_data = {
            "slug": item["slug"],
            "media_type": item["media_type"],
            "description": item.get("description"),
            "content_description": item.get("content_description"),
            "tags": [tag["name"] for tag in db.list_tags_for_post(item["slug"])],
            "type_metadata": item.get("type_metadata"),
            "timestamp": item.get("timestamp"),
            "content_date": item.get("content_date"),
            "source_modified_at": item.get("source_modified_at"),
            "external_url": item.get("external_url"),
        }

        # Include filename for uploaded files
        if item.get("stored_filename"):
            item_data["filename"] = item["filename"]
            item_data["stored_filename"] = item["stored_filename"]

            # Add file to the list to include in zip
            file_path = storage.path_for(item["stored_filename"])
            if file_path.exists():
                # Archive name: files/<slug>_<original_filename>
                arcname = f"files/{item['slug']}_{item['filename']}"
                files_to_include.append((arcname, file_path))

        manifest["items"].append(item_data)

    # Create zip in memory
    zip_buffer = io.BytesIO()
    with zipfile.ZipFile(zip_buffer, "w", zipfile.ZIP_DEFLATED) as zf:
        # Add manifest
        manifest_json = json.dumps(manifest, indent=2)
        zf.writestr("manifest.json", manifest_json)

        # Add files
        for arcname, file_path in files_to_include:
            zf.write(file_path, arcname=arcname)

    zip_buffer.seek(0)
    return zip_buffer.getvalue()
