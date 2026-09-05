"""Audio type spec and registration.

Audio files are file uploads with no visual thumbnail frame. The object
detail page renders a <audio controls> mini player instead of a thumbnail
image (see web/app.py's is_audio_file and object_detail.html), and gallery
tiles fall back to the generic file icon with this type's badge overlaid.
"""

import json
import subprocess

from .. import storage


def _stored_path(row):
    """Helper to get the full path to a stored audio file, or None if the
    file doesn't exist or wasn't provided."""
    stored_filename = row.get("stored_filename")
    if not stored_filename:
        return None
    path = storage.path_for(stored_filename)
    return path if path.exists() else None


def get_properties(row):
    """ObjectTypeSpec.properties_fn for media_type='audio' — returns duration
    using ffprobe, or {} on any failure."""
    path = _stored_path(row)
    if not path:
        return {}

    try:
        cmd = [
            "ffprobe",
            "-v", "error",
            "-show_entries", "format=duration,bit_rate:stream=codec_type,codec_name",
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

        for stream in data.get("streams", []):
            if stream.get("codec_type") == "audio" and stream.get("codec_name"):
                props["Codec"] = stream["codec_name"].upper()
                break

        bit_rate = data.get("format", {}).get("bit_rate")
        if bit_rate:
            try:
                props["Bitrate"] = f"{int(bit_rate) // 1000} kbps"
            except (ValueError, TypeError):
                pass

        return props
    except (FileNotFoundError, subprocess.TimeoutExpired, json.JSONDecodeError, Exception) as e:
        print(f"Audio properties extraction failed for {path}: {e!r}")
        return {}


from . import register, ObjectTypeSpec, ThumbnailSource

register(ObjectTypeSpec(
    key="audio",
    label="Audio file",
    thumbnail_source=ThumbnailSource.NONE,
    ocr_capable=False,
    extensions=frozenset({".mp3", ".m4a", ".ogg", ".wav"}),
    properties_fn=get_properties,
    badge_icon="\U0001F3B5",
    badge_text="AUDIO",
))
