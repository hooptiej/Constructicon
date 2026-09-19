"""Export a self-contained static website for deployment to GitHub Pages or similar.

This module provides functionality to generate a flat, static website from Constructicon's
projects and blog entries. The exported site is fully self-contained with relative links,
making it suitable for deployment to any static host or viewing offline.
"""

import json
import shutil
import time
from datetime import datetime
from pathlib import Path
from jinja2 import Environment, FileSystemLoader, select_autoescape

from core import db, storage


EXPORTS_DIR = Path(__file__).resolve().parent.parent / "exports"
EXPORT_TEMPLATES_DIR = Path(__file__).resolve().parent.parent / "web" / "export_templates"


def build_site(config: dict, out_dir: str | Path = None) -> dict:
    """Build a self-contained static website from selected projects and blog entries.

    Args:
        config: Dict with optional keys:
            - project_slugs: List of project slugs to include (default: all active projects)
            - blog_entry_slugs: List of blog entry slugs to include (default: all "ready" entries)
            - site: Dict with:
                - title: Site title (default: "hooptiej.com")
                - tagline: Site tagline (default: "")
        out_dir: Output directory (if None, uses EXPORTS_DIR/<timestamp>/)

    Returns:
        Dict with keys:
            - projects: Number of projects included
            - blog_entries: Number of blog entries included
            - media_files: Number of media files copied
            - warnings: List of warning strings (missing files, empty projects, etc.)
    """
    if out_dir is None:
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        out_dir = EXPORTS_DIR / timestamp
    else:
        out_dir = Path(out_dir)

    out_dir.mkdir(parents=True, exist_ok=True)

    # Parse config
    project_slugs = config.get("project_slugs")
    blog_entry_slugs = config.get("blog_entry_slugs")
    site_config = config.get("site", {})
    site_title = site_config.get("title", "hooptiej.com")
    site_tagline = site_config.get("tagline", "")

    warnings = []
    media_count = 0

    # Gather projects
    if project_slugs is None:
        # Include all active projects
        all_projects = db.list_projects(status="active")
        projects = {p["id"]: p for p in all_projects}
    else:
        projects = {}
        for slug in project_slugs:
            p = db.get_project(slug)
            if p:
                projects[p["id"]] = p

    # Gather project items
    project_items = {}  # project_id -> list of items
    for project_id in projects:
        items = db.list_project_items(project_id)
        project_items[project_id] = items
        if not items:
            warnings.append(f"Project '{projects[project_id]['slug']}' has no items")

    # Gather blog entries
    if blog_entry_slugs is None:
        # Include all "ready" blog entries
        blog_entries = db.list_blog_entries(status="ready")
    else:
        blog_entries = []
        for slug in blog_entry_slugs:
            entry = db.get_blog_entry(slug)
            if entry:
                blog_entries.append(entry)

    blog_entries_dict = {e["id"]: e for e in blog_entries}

    # Gather blog entry projects and items
    entry_projects = {}  # entry_id -> list of projects with sort_order and note
    entry_items = {}  # entry_id -> list of items with sort_order and note
    for entry_id in blog_entries_dict:
        entry_projects[entry_id] = db.list_entry_projects(entry_id)
        entry_items[entry_id] = db.list_entry_items(entry_id)
        if not entry_items[entry_id]:
            warnings.append(f"Blog entry '{blog_entries_dict[entry_id]['slug']}' has no attachments")

    # Create media directory
    media_dir = out_dir / "media"
    media_dir.mkdir(parents=True, exist_ok=True)

    # Copy media files for projects
    all_items = {}  # slug -> item dict (for lookups)
    for items_list in project_items.values():
        for item in items_list:
            all_items[item["slug"]] = item
            if item.get("stored_filename"):
                src = storage.path_for(item["stored_filename"])
                if src.exists():
                    # Preserve the file extension
                    ext = Path(item["stored_filename"]).suffix
                    dest = media_dir / f"{item['slug']}{ext}"
                    shutil.copy2(src, dest)
                    media_count += 1
                else:
                    warnings.append(f"Media file missing: {item['stored_filename']} for item '{item['slug']}'")

    # Copy media files for blog entries
    for items_list in entry_items.values():
        for item in items_list:
            all_items[item["slug"]] = item
            if item.get("stored_filename"):
                src = storage.path_for(item["stored_filename"])
                if src.exists():
                    ext = Path(item["stored_filename"]).suffix
                    dest = media_dir / f"{item['slug']}{ext}"
                    if not dest.exists():  # Don't overwrite if already copied
                        shutil.copy2(src, dest)
                        media_count += 1
                else:
                    warnings.append(f"Media file missing: {item['stored_filename']} for item '{item['slug']}'")

    # Set up Jinja2 environment
    env = Environment(
        loader=FileSystemLoader(EXPORT_TEMPLATES_DIR),
        autoescape=select_autoescape(enabled_extensions=("html",)),
    )

    # Render pages
    # 1. Home page (index.html)
    home_template = env.get_template("home.html")
    home_html = home_template.render(
        root="",  # root index.html: links are relative to the site root itself
        site_title=site_title,
        site_tagline=site_tagline,
        projects=list(projects.values()),
        blog_entries=blog_entries,
    )
    (out_dir / "index.html").write_text(home_html)

    # 2. Projects index (projects/index.html)
    projects_dir = out_dir / "projects"
    projects_dir.mkdir(exist_ok=True)
    projects_index_template = env.get_template("projects_index.html")
    projects_index_html = projects_index_template.render(
        root="../",  # under projects/ — one level below the site root
        site_title=site_title,
        site_tagline=site_tagline,
        projects=list(projects.values()),
    )
    (projects_dir / "index.html").write_text(projects_index_html)

    # 3. Individual project pages (projects/<slug>.html)
    project_template = env.get_template("project.html")
    for project_id, project in projects.items():
        items = project_items.get(project_id, [])
        project_html = project_template.render(
            root="../",  # under projects/
            site_title=site_title,
            site_tagline=site_tagline,
            project=project,
            items=items,
        )
        (projects_dir / f"{project['slug']}.html").write_text(project_html)

    # 4. Blog index (blog/index.html)
    blog_dir = out_dir / "blog"
    blog_dir.mkdir(exist_ok=True)
    blog_index_template = env.get_template("blog_index.html")
    blog_index_html = blog_index_template.render(
        root="../",  # under blog/
        site_title=site_title,
        site_tagline=site_tagline,
        blog_entries=blog_entries,
    )
    (blog_dir / "index.html").write_text(blog_index_html)

    # 5. Individual blog post pages (blog/<slug>.html)
    blog_post_template = env.get_template("blog_post.html")
    for entry_id, entry in blog_entries_dict.items():
        projects_for_entry = entry_projects.get(entry_id, [])
        items_for_entry = entry_items.get(entry_id, [])
        blog_post_html = blog_post_template.render(
            root="../",  # under blog/
            site_title=site_title,
            site_tagline=site_tagline,
            entry=entry,
            projects=projects_for_entry,
            items=items_for_entry,
        )
        (blog_dir / f"{entry['slug']}.html").write_text(blog_post_html)

    # Copy static assets
    assets_src = EXPORT_TEMPLATES_DIR / "assets"
    assets_dest = out_dir / "assets"
    if assets_src.exists():
        if assets_dest.exists():
            shutil.rmtree(assets_dest)
        shutil.copytree(assets_src, assets_dest)

    # Build report
    report = {
        "projects": len(projects),
        "blog_entries": len(blog_entries),
        "media_files": media_count,
        "warnings": warnings,
    }

    # Update current pointer and prune old builds
    _update_current_pointer(out_dir)

    return report


def _update_current_pointer(build_dir: Path) -> None:
    """Update the 'current' pointer to the latest build and prune to last 2 builds.

    Uses a 'current' symlink on Unix-like systems (or a current/ directory on Windows)
    that the StaticFiles mount can follow.
    """
    EXPORTS_DIR.mkdir(parents=True, exist_ok=True)

    # Strategy: Use a 'current' directory that we re-populate on each build.
    # This is more portable than symlinks and works on Windows.
    current_dir = EXPORTS_DIR / "current"

    # Remove old current directory if it exists
    if current_dir.exists():
        shutil.rmtree(current_dir)

    # Copy the new build to current/
    shutil.copytree(build_dir, current_dir)

    # Prune: keep only the last 2 timestamped builds
    _prune_old_builds()


def _prune_old_builds(keep: int = 2) -> None:
    """Remove old timestamped builds, keeping only the most recent `keep` builds."""
    if not EXPORTS_DIR.exists():
        return

    # List all timestamped directories (matching YYYYMMDD_HHMMSS pattern)
    import re

    builds = []
    for item in EXPORTS_DIR.iterdir():
        if item.is_dir() and item.name != "current" and re.match(r"^\d{8}_\d{6}$", item.name):
            builds.append(item)

    # Sort by name (which preserves chronological order for YYYYMMDD_HHMMSS)
    builds.sort(reverse=True)

    # Remove builds beyond the kept count
    for build_dir in builds[keep:]:
        shutil.rmtree(build_dir)
