"""Export a self-contained static website for deployment to GitHub Pages or similar.

This module provides functionality to generate a flat, static website from Constructicon's
projects and blog entries. The exported site is fully self-contained with relative links,
making it suitable for deployment to any static host or viewing offline.
"""

import json
import shutil
import subprocess
import time
import urllib.parse
from datetime import datetime
from pathlib import Path
from jinja2 import Environment, FileSystemLoader, select_autoescape

from core import db, storage


def _youtube_embed_url(url):
    """Return a YouTube /embed/<id> URL for a watch / youtu.be / embed link, or
    None if the URL isn't a recognizable YouTube link. The templates must NOT
    parse this themselves — the naive split() approach produced /embed/watch for
    the common youtube.com/watch?v=<id> form (#331)."""
    if not url or ("youtube.com" not in url and "youtu.be" not in url):
        return None
    parsed = urllib.parse.urlparse(url)
    if "youtu.be" in (parsed.netloc or ""):
        vid = parsed.path.lstrip("/").split("/")[0]
    elif "/embed/" in (parsed.path or ""):
        vid = parsed.path.split("/embed/")[-1].split("/")[0]
    else:
        vid = (urllib.parse.parse_qs(parsed.query or "").get("v") or [None])[0]
    return f"https://www.youtube.com/embed/{vid}" if vid else None


def _bundle_item_media(item, media_dir, copied_slugs, warnings):
    """Bundle one item's media into media_dir and set its display fields.

    Sets on the item dict:
      - embed_url: YouTube /embed URL (or None)
      - media_file: filename of the copied original (e.g. "<slug>.stl"), or None
      - thumb_file: filename of the copied rendered thumbnail ("<slug>_thumb.jpg"),
        or None. Every previewable type has a thumbnail (what /f/<slug>/thumb
        serves); the templates display it for types whose original isn't a
        web-native image (#333).
    Copies each file at most once per slug (an item can appear in several
    projects/entries). Returns the number of NEW files copied."""
    item["embed_url"] = _youtube_embed_url(item.get("external_url"))
    item["media_file"] = None
    item["thumb_file"] = None
    slug = item["slug"]
    first = slug not in copied_slugs
    n = 0

    stored = item.get("stored_filename")
    if stored:
        src = storage.path_for(stored)
        if src.exists():
            item["media_file"] = f"{slug}{Path(stored).suffix}"
            if first:
                shutil.copy2(src, media_dir / item["media_file"])
                n += 1
        else:
            warnings.append(f"Media file missing: {stored} for item '{slug}'")

    thumb_src = storage.thumb_path_for(slug)
    if thumb_src.exists():
        item["thumb_file"] = f"{slug}_thumb.jpg"
        if first:
            shutil.copy2(thumb_src, media_dir / item["thumb_file"])
            n += 1

    copied_slugs.add(slug)
    return n


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

    # Bundle each rendered item's media (original + rendered thumbnail) and set
    # its display fields (embed_url / media_file / thumb_file). One copy per slug
    # even if the item appears in multiple projects/entries (#333).
    copied_slugs = set()
    for items_list in list(project_items.values()) + list(entry_items.values()):
        for item in items_list:
            media_count += _bundle_item_media(item, media_dir, copied_slugs, warnings)

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


def publish_build(remote_url, branch, build_dir, work_dir, *, auth_url=None, commit_message=None, author=("Constructicon", "constructicon@localhost")) -> dict:
    """Publish a build to a git remote repository.

    Args:
        remote_url: CLEAN git remote URL (no credentials). This is the only URL
                    ever written to .git/config (as origin), so a token never
                    lands on disk.
        auth_url:   Optional network URL used ONLY for the fetch/push subprocess
                    invocations (may carry a token, e.g.
                    https://x-access-token:<tok>@github.com/owner/repo.git). It is
                    passed as an explicit argument each time and never saved as a
                    remote. Defaults to remote_url (for tokenless remotes like a
                    local file:// bare repo used in tests).
        branch: Target branch name (e.g., "master", "main").
        build_dir: Source directory containing the built files (e.g., exports/current/).
        work_dir: Working checkout directory (will be cloned/updated here).
        commit_message: Optional commit message (default: "Publish site <ISO timestamp>").
        author: Tuple of (name, email) for git commit identity (default: ("Constructicon", "constructicon@localhost")).

    Returns:
        Dict with keys:
            - commit: Commit SHA (None if no changes)
            - files: List of file paths that were in the build
            - branch: Target branch name
            - changed: Boolean indicating whether new commit was created
            - detail: Optional detail message for no-change case

    Raises:
        RuntimeError: On git operation failure (clone, fetch, reset, commit, push).
    """
    build_dir = Path(build_dir)
    work_dir = Path(work_dir)

    if not build_dir.exists():
        raise RuntimeError(f"Build directory does not exist: {build_dir}")

    # Ensure work_dir parent exists
    work_dir.parent.mkdir(parents=True, exist_ok=True)

    # Default commit message with ISO timestamp
    if commit_message is None:
        timestamp = datetime.now().isoformat()
        commit_message = f"Publish site {timestamp}"

    author_name, author_email = author

    # net_url may carry a token; it is only ever passed as an explicit fetch/push
    # argument, never stored. origin (in .git/config) is always the CLEAN URL.
    net_url = auth_url or remote_url

    # Ensure a checkout exists with a CLEAN origin. The token is NEVER written to
    # .git/config: we init locally + set origin to the clean URL, and reach the
    # network only via `git fetch/push <net_url>` with net_url as an explicit arg.
    if not (work_dir / ".git").exists():
        work_dir.mkdir(parents=True, exist_ok=True)
        subprocess.run(["git", "-C", str(work_dir), "init"], check=True, capture_output=True, text=True, timeout=10)
        subprocess.run(["git", "-C", str(work_dir), "remote", "add", "origin", remote_url], check=True, capture_output=True, text=True, timeout=10)
    else:
        # Reused per-target checkout: force origin back to the clean URL in case an
        # older build ever persisted a token-bearing one.
        subprocess.run(["git", "-C", str(work_dir), "remote", "set-url", "origin", remote_url], capture_output=True, text=True, timeout=10)

    # Fetch the current remote branch via net_url (explicit, not saved). Tolerate an
    # empty/new remote or a branch that doesn't exist there yet.
    fetch_res = subprocess.run(["git", "-C", str(work_dir), "fetch", net_url, branch], capture_output=True, text=True, timeout=60)
    if fetch_res.returncode == 0:
        # Point the local branch at what we just fetched (handles both first sync
        # and updates), replacing any prior working state.
        subprocess.run(["git", "-C", str(work_dir), "checkout", "-B", branch, "FETCH_HEAD"], check=True, capture_output=True, text=True, timeout=10)
    else:
        # New/empty remote or missing branch: just be on a fresh branch.
        subprocess.run(["git", "-C", str(work_dir), "checkout", "-B", branch], capture_output=True, text=True, timeout=10)

    # Preserve CNAME if it exists
    cname_path = work_dir / "CNAME"
    cname_content = None
    if cname_path.exists():
        cname_content = cname_path.read_text()

    # Replace working tree with build contents (except .git)
    git_dir = work_dir / ".git"
    if git_dir.exists():
        # Preserve .git
        for item in work_dir.iterdir():
            if item.name != ".git":
                if item.is_dir():
                    shutil.rmtree(item)
                else:
                    item.unlink()

    # Copy build contents
    for item in build_dir.iterdir():
        dest = work_dir / item.name
        if item.is_dir():
            shutil.copytree(item, dest)
        else:
            shutil.copy2(item, dest)

    # Restore CNAME if it was there
    if cname_content is not None:
        cname_path.write_text(cname_content)

    # Write .nojekyll
    (work_dir / ".nojekyll").touch()

    # Collect file list
    files = []
    for item in work_dir.rglob("*"):
        if item.is_file() and not item.relative_to(work_dir).parts[0] == ".git":
            files.append(str(item.relative_to(work_dir)))

    # Stage all changes
    try:
        subprocess.run(
            ["git", "-C", str(work_dir), "add", "-A"],
            check=True,
            capture_output=True,
            text=True,
            timeout=10,
        )
    except subprocess.CalledProcessError as e:
        raise RuntimeError(f"Failed to stage changes: {e.stderr}") from e

    # Check if there are changes to commit
    try:
        status_output = subprocess.run(
            ["git", "-C", str(work_dir), "status", "--porcelain"],
            check=True,
            capture_output=True,
            text=True,
            timeout=10,
        )
    except subprocess.CalledProcessError as e:
        raise RuntimeError(f"Failed to check git status: {e.stderr}") from e

    # If no changes, return early
    if not status_output.stdout.strip():
        return {
            "commit": None,
            "files": files,
            "branch": branch,
            "changed": False,
            "detail": "No changes to publish"
        }

    # Commit with per-invocation identity
    try:
        subprocess.run(
            [
                "git",
                "-C", str(work_dir),
                "-c", f"user.name={author_name}",
                "-c", f"user.email={author_email}",
                "commit",
                "-m", commit_message
            ],
            check=True,
            capture_output=True,
            text=True,
            timeout=10,
        )
    except subprocess.CalledProcessError as e:
        raise RuntimeError(f"Failed to commit: {e.stderr}") from e

    # Get the commit SHA
    try:
        commit_output = subprocess.run(
            ["git", "-C", str(work_dir), "rev-parse", "HEAD"],
            check=True,
            capture_output=True,
            text=True,
            timeout=10,
        )
        commit_sha = commit_output.stdout.strip()
    except subprocess.CalledProcessError as e:
        raise RuntimeError(f"Failed to get commit SHA: {e.stderr}") from e

    # Push to remote
    try:
        subprocess.run(
            ["git", "-C", str(work_dir), "push", net_url, f"HEAD:{branch}"],
            check=True,
            capture_output=True,
            text=True,
            timeout=30,
        )
    except subprocess.CalledProcessError as e:
        # Strip token from error message if present
        error_msg = e.stderr
        error_msg = _strip_token_from_error(error_msg)
        raise RuntimeError(f"Failed to push: {error_msg}") from e

    return {
        "commit": commit_sha,
        "files": files,
        "branch": branch,
        "changed": True,
    }


def _strip_token_from_error(error_msg: str) -> str:
    """Remove any x-access-token credentials from error messages."""
    import re
    # Remove https://x-access-token:<token>@github.com/... patterns
    return re.sub(r"x-access-token:[^@]+@", "x-access-token:[REDACTED]@", error_msg)


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
