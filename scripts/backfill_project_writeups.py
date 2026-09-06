"""Issue #156: backfill empty writeup documents for existing projects.

Background
----------
When #156 was implemented, api_create_project began auto-creating a
document-type capture_event for every new project. However, existing
projects created before this feature don't have writeup documents yet.

This script adds blank writeup documents to all existing projects that
don't yet have a writeup_slug set, making them ready for users to add
write-up content.

Matching strategy
-----------------
For each project in the database, if writeup_slug is NULL or missing,
this script:
  1. Creates a new document-type capture_event with empty body via
     POST /api/content, getting back its slug
  2. Attaches it to the project via POST /api/projects/{project_id},
     setting writeup_slug

The new writeup documents are created with:
  - media_type = 'document'
  - content_description = "{project.title} — Write-up"
  - type_metadata = {"body": ""}
  - source = the server's SOURCE_AUTHORED constant

Idempotency
-----------
Safe to re-run:
  - Projects that already have a writeup_slug are skipped
  - Each project gets at most one writeup document created

Usage
-----
Run against a running instance:
    python scripts/backfill_project_writeups.py --base-url http://localhost:8000

Or against the production instance on TrueNAS:
    python scripts/backfill_project_writeups.py --base-url http://10.0.1.250

(see docker logs to find the actual internal port if the public port differs)
"""

import argparse
import json
import sys
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from core import storage  # noqa: E402


def make_slug():
    """Generate a random slug the same way storage.make_slug does."""
    return storage.make_slug()


def post_request(url, data_dict):
    """Make a POST request with form data, return parsed JSON response."""
    payload = urllib.parse.urlencode(data_dict).encode("utf-8")
    req = urllib.request.Request(
        url, data=payload, method="POST",
        headers={"Content-Type": "application/x-www-form-urlencoded"},
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"POST {url} failed ({e.code}): {body}") from e
    except urllib.error.URLError as e:
        raise RuntimeError(
            f"Couldn't reach {url} ({e.reason}) — is the target instance running and --base-url correct?"
        ) from e


def get_request(url):
    """Make a GET request, return parsed JSON response."""
    req = urllib.request.Request(url, method="GET")
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"GET {url} failed ({e.code}): {body}") from e
    except urllib.error.URLError as e:
        raise RuntimeError(
            f"Couldn't reach {url} ({e.reason}) — is the target instance running and --base-url correct?"
        ) from e


def create_writeup_document(base_url, project_id, project_title):
    """Create a new blank document-type capture_event for the project's writeup,
    attaching it to the project's items in the same call (via /api/content's
    project_id param — the same path api_create_project uses) so the writeup
    is a normal, visible project item, same as one created at project-creation
    time. Returns the slug of the newly created document."""
    payload = {
        "media_type": "document",
        "content_description": f"{project_title} — Write-up",
        "type_metadata": json.dumps({"body": ""}),
        "project_id": str(project_id),
    }
    url = f"{base_url.rstrip('/')}/api/content"
    response = post_request(url, payload)
    return response.get("slug")


def attach_writeup_to_project(base_url, project_id, writeup_slug):
    """Attach a writeup document to a project by setting its writeup_slug."""
    url = f"{base_url.rstrip('/')}/api/projects/{project_id}"
    payload = {"writeup_slug": writeup_slug}
    post_request(url, payload)


def main():
    parser = argparse.ArgumentParser(
        description="Backfill writeup documents for existing projects without them."
    )
    parser.add_argument(
        "--base-url",
        default="http://localhost:8000",
        help="Base URL of the running Constructicon instance (default: http://localhost:8000)"
    )
    args = parser.parse_args()

    base_url = args.base_url
    print(f"Connecting to {base_url}...")

    # Fetch all projects from the running instance
    projects_url = f"{base_url.rstrip('/')}/api/projects"
    try:
        projects = get_request(projects_url)
    except RuntimeError as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)

    if not projects:
        print("No projects found.")
        return

    created_count = 0
    skipped_count = 0

    for project in projects:
        project_id = project["id"]
        project_title = project["title"]
        writeup_slug = project.get("writeup_slug")

        if writeup_slug:
            print(f"  ✓ {project_title}: already has writeup")
            skipped_count += 1
        else:
            print(f"  ⧐ {project_title}: creating writeup...")
            try:
                # Create the blank writeup document
                new_slug = create_writeup_document(base_url, project_id, project_title)
                # Attach it to the project
                attach_writeup_to_project(base_url, project_id, new_slug)
                print(f"    ✓ Created writeup ({new_slug})")
                created_count += 1
            except RuntimeError as e:
                print(f"    ✗ Failed: {e}", file=sys.stderr)

    print()
    print(f"Summary: created {created_count} writeup(s), skipped {skipped_count} existing")


if __name__ == "__main__":
    main()
