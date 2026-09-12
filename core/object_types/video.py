"""Video support (issue #92): .mov and .mp4 file uploads with frame capture
for thumbnails via ffmpeg.

ffmpeg is installed as a system package (see Dockerfile) and shelled out to
for frame extraction. This is the stated preference from issue #92 — a lean
system package + subprocess call rather than adding a heavy Python video
library like moviepy or opencv.

A defensive capture_fn returns None on any failure (missing file, corrupt
video, ffmpeg missing/erroring, zero-duration video) rather than raising,
same as every other type's capture routine (see core/object_types/pdf.py).

Recording date (#265): the container's creation_time tag (the mvhd atom's
creation time for .mp4/.mov — what a phone or webcam stamps when it starts
recording) is read once at upload time by get_embedded_metadata below and
promoted to content_date by core/embedded_metadata.py, and over existing
rows by scripts/backfill_content_dates.py. Read via the same ffprobe
subprocess get_properties already shells out to for duration/resolution.
Checked against every real production video while scoping this: 15 of 21
carried it, always as ISO-8601 UTC with a Z suffix
("2019-11-26T20:12:44.000000Z" for a webcam clip whose own filename said
13:12 local — i.e. genuinely UTC, not local time wearing a Z); the 6
without were re-encoded social-media downloads with no metadata at all,
which correctly stay on their file mtime.
"""

import json
import subprocess
from datetime import datetime
from pathlib import Path

from .. import storage, timeline


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


def read_creation_time(path):
    """The creation_time tag string exactly as ffprobe reports it, or None
    when the file has none. The container-level (format) tag wins; a
    stream-level one is the fallback for a muxer that only stamped the
    tracks. Raises on ffprobe/JSON failure; get_embedded_metadata is the
    best-effort wrapper."""
    cmd = [
        "ffprobe",
        "-v", "error",
        "-show_entries", "format_tags=creation_time:stream_tags=creation_time",
        "-of", "json",
        str(path),
    ]
    result = subprocess.run(cmd, capture_output=True, timeout=20, encoding="utf-8", errors="replace")
    if result.returncode != 0:
        return None
    data = json.loads(result.stdout)
    candidates = [(data.get("format", {}).get("tags") or {}).get("creation_time")]
    candidates += [(stream.get("tags") or {}).get("creation_time") for stream in data.get("streams", [])]
    for value in candidates:
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None


def parse_creation_time(text):
    """ffprobe's creation_time -> datetime: timezone-aware for the
    ISO-8601-with-offset/Z form every real file so far has carried, naive
    for a bare "YYYY-MM-DD HH:MM:SS" (older QuickTime muxers), so the
    caller can apply the naive-date convention only when the file really
    didn't say. Raises ValueError on anything fromisoformat can't read."""
    # fromisoformat accepts a literal Z on the Python this project targets,
    # but the replace keeps this readable on older interpreters too — same
    # note scripts/full_youtube_channel_sync.py makes for publishedAt.
    return datetime.fromisoformat(text.replace("Z", "+00:00"))


def get_embedded_metadata(path):
    """ObjectTypeSpec.embedded_metadata_fn for media_type='video' (#265) —
    {"content_date": <UTC unix seconds>} from the container's
    creation_time (see read_creation_time), or {} for a file with none or
    on any failure. An explicit offset/Z is trusted; a naive value is
    interpreted as Mountain Time per core/timeline.py's convention. Only
    content_date: a container title tag is rare enough on real uploads
    (none of production's carried one) that it isn't seeded here."""
    try:
        raw = read_creation_time(path)
        if not raw:
            return {}
        return {"content_date": timeline.source_datetime_to_epoch(parse_creation_time(raw))}
    except Exception as e:
        print(f"Video creation-time extraction failed for {path}: {e!r}")
        return {}


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
            "-show_entries", "format=duration,bit_rate:stream=codec_type,codec_name,width,height",
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

        # Resolution + codec, both from the first video stream
        streams = data.get("streams", [])
        for stream in streams:
            if stream.get("codec_type") == "video":
                width = stream.get("width")
                height = stream.get("height")
                if width and height:
                    props["Resolution"] = f"{width} × {height}"
                if stream.get("codec_name"):
                    props["Codec"] = stream["codec_name"].upper()
                break

        # Overall bitrate (format-level — more reliably present than a
        # per-stream bit_rate, which many containers, mp4 included, don't
        # always populate).
        bit_rate = data.get("format", {}).get("bit_rate")
        if bit_rate:
            try:
                props["Bitrate"] = f"{int(bit_rate) / 1_000_000:.1f} Mbps"
            except (ValueError, TypeError):
                pass

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
    caption_capable=True,  # #239: captions the existing generated thumbnail frame — one frame proved enough in testing, no extra sampling
    extensions=frozenset({".mov", ".mp4"}),
    capture_fn=capture_thumbnail,
    properties_fn=get_properties,
    # #265: container creation_time -> content_date at upload time (and via
    # scripts/backfill_content_dates.py for rows that predate this). See
    # get_embedded_metadata; writes nothing into type_metadata.
    embedded_metadata_fn=get_embedded_metadata,
    badge_icon="\U0001F3AC",
    badge_text="VIDEO",
))
