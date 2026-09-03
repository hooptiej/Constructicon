"""Video support (issue #92): .mov and .mp4 file uploads with frame capture
for thumbnails via ffmpeg.

ffmpeg is installed as a system package (see Dockerfile) and shelled out to
for frame extraction. This is the stated preference from issue #92 — a lean
system package + subprocess call rather than adding a heavy Python video
library like moviepy or opencv.

A defensive capture_fn returns None on any failure (missing file, corrupt
video, ffmpeg missing/erroring, zero-duration video) rather than raising,
same as every other type's capture routine (see core/object_types/pdf.py).
"""

import subprocess
from pathlib import Path

from .. import storage


def capture_frame(path):
    """Extract the first frame of a video file at `path` as PNG bytes,
    or None on any failure (corrupt/unreadable video, zero-duration,
    ffmpeg missing or erroring, file not found).

    Captures at 00:00:01 (1 second) to skip over any black frames or
    intros that might appear at the very start. If the video is shorter
    than 1 second, ffmpeg will capture the nearest frame available.
    """
    if not path or not Path(path).exists():
        return None

    try:
        # ffmpeg -i <input> -ss 00:00:01 -vframes 1 -f image2pipe -vcodec png -
        # -i: input file
        # -ss: seek to 1 second (1 second in, skip any leader frames)
        # -vframes 1: capture exactly 1 frame
        # -f image2pipe: output format is raw image data piped to stdout
        # -vcodec png: encode as PNG
        # - : write to stdout
        result = subprocess.run(
            ["ffmpeg", "-i", str(path), "-ss", "00:00:01", "-vframes", "1",
             "-f", "image2pipe", "-vcodec", "png", "-"],
            capture_output=True,
            timeout=10,
        )

        if result.returncode != 0:
            # ffmpeg errored (corrupt file, unsupported codec, etc.)
            return None

        if not result.stdout:
            # No frame captured (shouldn't happen if returncode was 0, but be defensive)
            return None

        return result.stdout
    except FileNotFoundError:
        # ffmpeg binary not found
        print(f"ffmpeg not found — video thumbnail skipped")
        return None
    except subprocess.TimeoutExpired:
        print(f"ffmpeg timeout processing {path}")
        return None
    except Exception as e:
        print(f"Video frame capture failed for {path}: {e!r}")
        return None


def _stored_path(row):
    """Helper to get the full path to a stored video file, or None if
    the file doesn't exist or wasn't provided."""
    stored_filename = row.get("stored_filename")
    if not stored_filename:
        return None
    path = storage.path_for(stored_filename)
    return path if path.exists() else None


def capture_thumbnail(row):
    """ObjectTypeSpec.capture_fn for media_type='video' — see
    core/thumbnails.py. Extract and save a representative frame."""
    path = _stored_path(row)
    if not path:
        return None

    frame_bytes = capture_frame(path)
    if frame_bytes:
        return frame_bytes
    return None


# Registration: add this type to the object-type registry
from . import register, ObjectTypeSpec, ThumbnailSource

register(ObjectTypeSpec(
    key="video",
    label="Video file",
    thumbnail_source=ThumbnailSource.CAPTURE,
    ocr_capable=False,
    extensions=frozenset({".mov", ".mp4"}),
    capture_fn=capture_thumbnail,
    badge_icon="\U0001F3AC",
    badge_text="VIDEO",
))
