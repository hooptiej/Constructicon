"""One-off script: seed a handful of real example `projects` rows so the
home page's Projects column (right two-thirds, see web/app.py's home_page
route) has something real to show, and so there's something to click into
on the project detail page.

This is deliberately NOT wired into app startup or the backfill script —
`projects`/`project_items` are curated by hand, not derived data, so seeding
them is a one-time editorial decision an owner makes explicitly, same as
seed_test_data.py's fake gallery uploads are opt-in rather than automatic.

Pulls real content that scripts/backfill_from_hooptiej_site.py already
populated: for each of a few of the site's top-level tag categories, grabs
whatever posts/videos are filed under that tag (or its descendants) and
groups them into one curated project. Run this AFTER the backfill script has
populated blog_tags/post_tags — if a named tag category doesn't exist yet
(backfill hasn't run) or has no tagged content, that project is skipped with
a message rather than created empty.

Run with the venv's python from the Constructicon repo root:
    python scripts/seed_example_projects.py
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from core import db  # noqa: E402

# Matches the top-level category names scripts/backfill_from_hooptiej_site.py
# creates from the site's projects/*.html pages (see that script and the
# README's "Tag taxonomy" section) — not a fixed schema, just what the real
# backfilled data currently uses.
PROJECT_DEFS = [
    {
        "title": "AlienWhoop & TinyShark Builds",
        "description": "A curated look at the AlienWhoop and TinyShark micro-quad builds — "
                        "posts and videos pulled from the tag tree into one collection.",
        "tag_name": "AlienWhoop & TinyShark",
        "max_items": 6,
    },
    {
        "title": "FPV and Flight Highlights",
        "description": "Flights, builds, and field notes from the FPV side of things.",
        "tag_name": "FPV and Flight",
        "max_items": 6,
    },
    {
        "title": "3D Modeling and Printing Roundup",
        "description": "A sampler of 3D-printed builds and prints, gathered into one project card.",
        "tag_name": "3D Modeling and Printing",
        "max_items": 6,
    },
]


def find_tag_by_name(name):
    for tag in db.list_tag_tree():
        if tag["name"] == name:
            return tag
    return None


def main():
    db.init_db()
    seeded = 0
    for project_def in PROJECT_DEFS:
        tag = find_tag_by_name(project_def["tag_name"])
        if tag is None:
            print(f"skip {project_def['title']!r}: no {project_def['tag_name']!r} tag found "
                  f"— run scripts/backfill_from_hooptiej_site.py first")
            continue
        posts = db.list_posts_for_tag(tag["id"], limit=project_def["max_items"])
        if not posts:
            print(f"skip {project_def['title']!r}: {project_def['tag_name']!r} has no tagged content yet")
            continue
        project = db.create_project(
            project_def["title"],
            description=project_def["description"],
            cover_slug=posts[0]["slug"],
        )
        for post in posts:
            db.add_item_to_project(project["id"], post["slug"])
        seeded += 1
        print(f"seeded project {project['slug']!r} ({project['title']}) with {len(posts)} item(s)")

    if seeded == 0:
        print("Nothing seeded — no matching tagged content found. Run the backfill script first.")


if __name__ == "__main__":
    main()
