"""Regenerate the cached thumbnail for image rows whose original carries an
EXIF Orientation tag other than 1 -- the rows uploaded before #288 taught
storage.save_thumbnail_from_bytes to honor that tag -- or for explicitly
named slugs.

Why a script at all: a thumbnail is a cached file (storage/<slug>_thumb.jpg)
that nothing rebuilds on its own. thumbnails.ensure_thumbnail is a no-op
when the file already exists, and /f/<slug>/thumb falls back to serving the
full-size original (not to regenerating) if it's missing. So a row uploaded
before the fix keeps its sideways thumbnail forever unless something
rewrites it; this rewrites it in place, once, and is safe to re-run
(regenerating an already-upright thumbnail from the same original gives the
same result). The original file is never touched.

Runs inside the app container and imports core directly -- like
backfill_thumbnails.py at the repo root, and unlike most of scripts/, which
go over HTTP -- because there is no HTTP route that regenerates an existing
thumbnail, and adding one for a one-time repair is more surface than the
job needs.

    sudo docker exec constructicon-web python3 /app/scripts/regenerate_thumbnails.py --dry-run
    sudo docker exec constructicon-web python3 /app/scripts/regenerate_thumbnails.py
    sudo docker exec constructicon-web python3 /app/scripts/regenerate_thumbnails.py --slug <slug> [--slug <slug> ...]

Scope: only rows with a stored image file (storage.IMAGE_EXTENSIONS).
Fetched/captured thumbnails (youtube, pdf, stl, ...) never had this bug --
nothing upstream of them carries a camera Orientation tag. With --slug the
named image rows are regenerated whether or not they carry the tag.

#266 interaction: a row that also has a manual type_metadata.rotation set
is regenerated like any other, but its rotation value is reported and left
alone -- a person set it, and once the thumbnail is upright it will
compound into a double rotation, so it's the person's call to clear it
(the detail page's rotate button cycles back to 0). As of 2026-09-12 no
production row had one set, so this is a safety net, not an expected case.
"""

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from PIL import Image  # noqa: E402

from core import db, storage  # noqa: E402


def _orientation(path):
    """The file's EXIF Orientation (1 when absent), or None if Pillow can't
    open it at all."""
    try:
        with Image.open(path) as img:
            return img.getexif().get(storage.EXIF_ORIENTATION_TAG, 1)
    except Exception:
        return None


def _thumb_size(slug):
    thumb = storage.thumb_path_for(slug)
    if not thumb.exists():
        return None
    try:
        with Image.open(thumb) as t:
            return t.size
    except Exception:
        return None


def main():
    parser = argparse.ArgumentParser(
        description="Regenerate cached thumbnails for EXIF-rotated image rows (#288), or for named slugs.",
    )
    parser.add_argument("--slug", action="append", default=[], metavar="SLUG",
                        help="regenerate this image row's thumbnail regardless of its EXIF tag (repeatable)")
    parser.add_argument("--dry-run", action="store_true",
                        help="report what would be regenerated; write nothing")
    args = parser.parse_args()

    explicit = bool(args.slug)
    if explicit:
        rows = []
        for slug in args.slug:
            row = db.get_by_slug(slug)
            if row is None:
                print(f"no such row: {slug}")
            else:
                rows.append(row)
    else:
        # Same enumerate-everything discovery pass the other maintenance
        # scripts use; redacted rows are excluded by default and have no
        # file to regenerate from anyway.
        rows = db.search(limit=100000)

    candidates = regenerated = unreadable = 0
    manual_rotation = []
    for row in rows:
        stored = row.get("stored_filename")
        if not stored or Path(stored).suffix.lower() not in storage.IMAGE_EXTENSIONS:
            if explicit:
                print(f"skip (not an image file): {row['slug']} {row.get('filename')}")
            continue
        path = storage.path_for(stored)
        if not path.exists():
            if explicit:
                print(f"skip (file missing on disk): {row['slug']} {row.get('filename')}")
            continue
        orientation = _orientation(path)
        if orientation is None:
            print(f"skip (Pillow can't open it): {row['slug']} {row.get('filename')}")
            unreadable += 1
            continue
        if not explicit and orientation == 1:
            continue
        candidates += 1
        if (row.get("type_metadata") or {}).get("rotation"):
            manual_rotation.append((row["slug"], row.get("filename"), row["type_metadata"]["rotation"]))
        before = _thumb_size(row["slug"])
        label = f"{row['slug']} {row.get('filename')} orientation={orientation} thumb={before}"
        if args.dry_run:
            print(f"would regenerate: {label}")
            continue
        storage.save_thumbnail_from_bytes(row["slug"], path.read_bytes())
        after = _thumb_size(row["slug"])
        print(f"regenerated: {label} -> {after}")
        regenerated += 1

    if args.dry_run:
        print(f"\n{candidates} thumbnail(s) would be regenerated (dry run, nothing written); {unreadable} unreadable")
    else:
        print(f"\nregenerated {regenerated} of {candidates} thumbnail(s); {unreadable} unreadable")

    if manual_rotation:
        print("\nRows with a manual #266 rotation set -- left untouched. With an upright thumbnail "
              "that rotation now compounds; if the image looks wrong, clear it via the detail "
              "page's rotate button:")
        for slug, filename, deg in manual_rotation:
            print(f"  {slug} {filename} rotation={deg}")


if __name__ == "__main__":
    main()
