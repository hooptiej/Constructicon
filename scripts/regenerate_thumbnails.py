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
the retired backfill script it replaced, and unlike most of scripts/, which
go over HTTP -- because there is no HTTP route that regenerates an existing
thumbnail, and adding one for a one-time repair is more surface than the
job needs.

    ./scripts/run_tool.sh constructicon-web scripts/regenerate_thumbnails.py --dry-run
    ./scripts/run_tool.sh constructicon-web scripts/regenerate_thumbnails.py
    ./scripts/run_tool.sh constructicon-web scripts/regenerate_thumbnails.py --slug <slug> [--slug <slug> ...]
    ./scripts/run_tool.sh constructicon-web scripts/regenerate_thumbnails.py --type stl --dry-run
    ./scripts/run_tool.sh constructicon-web scripts/regenerate_thumbnails.py --type stl [--slug <slug> ...]

#742, `--type <media type>`: re-render every thumbnail of that type through the type's own pipeline
(thumbnails.ensure_thumbnail: the parser child and its limits, the type's capture_fn), e.g. the STL
backfill after the GL renderer landed. The old thumbnail is moved aside first and put back if the new
render fails, so a failure never leaves an item without a picture; each row prints what it had and
what it got (STL: type_metadata.thumb_renderer), failures print the reason and make the exit status 1.
With --slug as well, only those slugs (of that type). Owner rule for the STL backfill: run it only
after the before/after sign-off (`python3 -m core.manage.render_stl <in> <out> --compare`).

(from the host, in the checkout: a one-off twin of the web container with the same DB and storage,
plus scripts/ read-only; #634, the app containers don't mount scripts/ any more.)

Scope: only rows whose type's thumbnail_source is UPLOADED_FILE. Fetched/captured
thumbnails (youtube, pdf, stl, ...) never had this bug -- nothing upstream of them
carries a camera Orientation tag. With --slug the named image rows are regenerated
whether or not they carry the tag.

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

from core import db, object_types, storage, thumbnails  # noqa: E402


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
    parser.add_argument("--type", metavar="MEDIA_TYPE",
                        help="#742: re-render every thumbnail of this media type (e.g. stl) through its own pipeline")
    args = parser.parse_args()
    if args.type:
        return regenerate_type(args.type, args.slug, args.dry_run)

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
        rows = db.search(limit=100000, include_brand=True)

    candidates = regenerated = unreadable = 0
    manual_rotation = []
    for row in rows:
        stored = row.get("stored_filename")
        spec = object_types.get_object_type(row.get("media_type"))
        if not stored or spec.thumbnail_source != object_types.ThumbnailSource.UPLOADED_FILE:
            if explicit:
                print(f"skip (not an uploaded-file type): {row['slug']} {row.get('filename')}")
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


def _renderer(slug):
    row = db.get_by_slug(slug) or {}
    return (row.get("type_metadata") or {}).get("thumb_renderer") or "unrecorded"


def regenerate_type(media_type, only_slugs, dry_run):
    """#742: re-render every thumbnail of `media_type` (optionally only `only_slugs`). Returns the exit
    status: 0 when every render succeeded (or a dry run), 1 when any failed (each one says why)."""
    spec = object_types.get_object_type(media_type)
    if spec.key != media_type:
        print(f"FAILED: no object type {media_type!r} (known: {', '.join(sorted(object_types.OBJECT_TYPES))})")
        return 1
    if spec.thumbnail_source == object_types.ThumbnailSource.NONE:
        print(f"FAILED: type {media_type!r} has no thumbnails to regenerate")
        return 1
    rows = [r for r in db.search(limit=100000, include_brand=True) if r.get("media_type") == media_type]
    if only_slugs:
        wanted = set(only_slugs)
        missing = wanted - {r["slug"] for r in rows}
        for slug in sorted(missing):
            print(f"skip (no {media_type} row with this slug): {slug}")
        rows = [r for r in rows if r["slug"] in wanted]
    done = failed = skipped = 0
    by_renderer = {}
    for row in rows:
        slug = row["slug"]
        stored = row.get("stored_filename")
        file_based = spec.thumbnail_source in (object_types.ThumbnailSource.UPLOADED_FILE,
                                               object_types.ThumbnailSource.CAPTURE)
        if file_based and not (stored and storage.path_for(stored).exists()):
            print(f"skip (file missing on disk): {slug} {row.get('filename')}")
            skipped += 1
            continue
        label = f"{slug} {row.get('filename')} thumb={_thumb_size(slug)} renderer={_renderer(slug)}"
        if dry_run:
            print(f"would regenerate: {label}")
            done += 1
            continue
        thumb = storage.thumb_path_for(slug)
        aside = thumb.with_name(thumb.name + ".regen-old")
        had = thumb.exists()
        if had:
            thumb.replace(aside)
        try:
            ok = thumbnails.ensure_thumbnail(row)
        except Exception as e:  # ensure_thumbnail is best-effort and shouldn't raise; say so if it does
            print(f"  error: {e!r}")
            ok = False
        if ok:
            if had:
                aside.unlink(missing_ok=True)
            new = _renderer(slug)
            by_renderer[new] = by_renderer.get(new, 0) + 1
            print(f"regenerated: {label} -> thumb={_thumb_size(slug)} renderer={new}")
            done += 1
        else:
            if had:
                aside.replace(thumb)
            print(f"FAILED (old thumbnail kept): {label}; see the log lines above for why")
            failed += 1
    if dry_run:
        print()
        print(f"{done} {media_type} thumbnail(s) would be regenerated (dry run, nothing written); {skipped} skipped")
        return 0
    summary = ", ".join(f"{n} by {r}" for r, n in sorted(by_renderer.items())) or "none"
    print()
    print(f"regenerated {done} of {len(rows)} {media_type} thumbnail(s) ({summary}); {failed} failed; {skipped} skipped")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
