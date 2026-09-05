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

import json
import subprocess
from pathlib import Path

from .. import storage


def _ffmpeg_frame_at(path, seek):
    """One ffmpeg attempt at extracting a single PNG frame, seeking to
    `seek` seconds first (0 for no seek). Returns PNG bytes, or None if
    ffmpeg errored or produced no frame."""
    cmd = ["ffmpeg"]
    if seek:
        cmd += ["-ss", str(seek)]
    cmd += ["-i", str(path), "-vframes", "1", "-f", "image2pipe", "-vcodec", "png", "-"]
    result = subprocess.run(cmd, capture_output=True, timeout=10)
    if result.returncode != 0 or not result.stdout:
        return None
    return result.stdout


def capture_frame(path):
    """Extract a representative frame of a video file at `path` as PNG
    bytes, or None on any failure (corrupt/unreadable video, ffmpeg
    missing or erroring, file not found).

    Tries seeking to 1 second first, to skip any black frames/intros at
    the very start. **`-ss` seeking to or past a video's actual duration
    silently yields zero frames rather than clamping to the last frame**
    (confirmed directly: a real 1.00s test video seeked to exactly
    00:00:01 produced "Output file is empty" with returncode 0, no
    error) — so any video 1 second or shorter would otherwise get no
    thumbnail at all. Falls back to no seek (the very first frame) if
    the 1-second attempt comes back empty.
    """
    if not path or not Path(path).exists():
        return None

    try:
        frame = _ffmpeg_frame_at(path, seek=1)
        if frame:
            return frame
        return _ffmpeg_frame_at(path, seek=0)
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


def get_properties(row):
    """ObjectTypeSpec.properties_fn for media_type='video' — returns duration
    and resolution using ffprobe, or {} on any failure."""
    path = _stored_path(row)
    if not path:
        return {}

    try:
        cmd = [
            "ffprobe",
            "-v", "error",
            "-show_entries", "format=duration:stream=width,height",
            "-of", "json",
            str(path)
        ]
        result = subprocess.run(cmd, capture_output=True, timeout=20, text=True)
        if result.returncode != 0:
            return {}

        data = json.loads(result.stdout)
        props = {}

        # Extract duration
        duration_sec = data.get("format", {}).get("duration")
        if duration_sec:
            try:
                duration_sec = float(duration_sec)
                minutes = int(duration_sec // 60)
                seconds = int(duration_sec % 60)
                hours = minutes // 60
                if hours > 0:
                    props["Duration"] = f"{hours}:{minutes % 60:02d}:{seconds:02d}"
                else:
                    props["Duration"] = f"{minutes}:{seconds:02d}"
            except (ValueError, TypeError):
                pass

        # Extract resolution (width x height from first video stream)
        streams = data.get("streams", [])
        for stream in streams:
            if stream.get("codec_type") == "video":
                width = stream.get("width")
                height = stream.get("height")
                if width and height:
                    props["Resolution"] = f"{width} × {height}"
                    break

        return props
    except (FileNotFoundError, subprocess.TimeoutExpired, json.JSONDecodeError, Exception) as e:
        print(f"Video properties extraction failed for {path}: {e!r}")
        return {}


# Registration: add this type to the object-type registry
from . import register, ObjectTypeSpec, ThumbnailSource

register(ObjectTypeSpec(
    key="video",
    label="Video file",
    thumbnail_source=ThumbnailSource.CAPTURE,
    ocr_capable=False,
    extensions=frozenset({".mov", ".mp4"}),
    capture_fn=capture_thumbnail,
    properties_fn=get_properties,
    badge_icon="\U0001F3AC",
    badge_text="VIDEO",
))
