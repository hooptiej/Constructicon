"""One-off: generate thumbnails for uploads that predate the thumbnail feature."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from core import db, storage

rows = db.search(limit=10000)
made = 0
for row in rows:
    ext = Path(row["filename"]).suffix.lower()
    if ext not in storage.IMAGE_EXTENSIONS:
        continue
    if storage.thumb_path_for(row["slug"]).exists():
        continue
    original = storage.path_for(row["stored_filename"])
    if not original.exists():
        print(f"skip (missing original): {row['filename']}")
        continue
    storage.make_thumbnail(row["slug"], original.read_bytes(), ext)
    made += 1

print(f"generated {made} thumbnails")
