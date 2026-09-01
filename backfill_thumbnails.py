"""One-off: generate thumbnails for rows that don't have one yet.

Originally just uploads that predated the thumbnail feature. Now that
thumbnail generation is generalized across object types (see
core/object_types.py / core/thumbnails.py), this also covers e.g. a
backfilled YouTube row whose thumbnail-fetch never ran or failed the first
time — thumbnails.ensure_thumbnail already knows how to get a picture for
any registered type, so this script doesn't need its own image-vs-not
special-casing anymore.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from core import db, object_types, storage, thumbnails

rows = db.search(limit=10000)
made = 0
for row in rows:
    if storage.thumb_path_for(row["slug"]).exists():
        continue
    spec = object_types.get_object_type(row.get("media_type"))
    if spec.thumbnail_source == object_types.ThumbnailSource.NONE:
        continue  # this type has no thumbnail concept at all (e.g. a plain document post) — not a failure
    if thumbnails.ensure_thumbnail(row):
        made += 1
    else:
        print(f"skip (thumbnail generation failed): {row.get('filename') or row['slug']}")

print(f"generated {made} thumbnails")
