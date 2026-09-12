"""Audio type spec and registration.

Audio files are file uploads with no visual thumbnail frame. The object
detail page renders a <audio controls> mini player instead of a thumbnail
image (see web/app.py's is_audio_file and object_detail.html), and gallery
tiles fall back to the generic file icon with this type's badge overlaid.

Tag metadata (#255): the file's own tags — title, artist, album, track,
year, genre — are read once at upload time by get_embedded_metadata below
and routed into the row's content-side fields by core/embedded_metadata.py
(title -> content_description + display_name, the rest -> type_metadata
under the keys metadata_fields documents; see that module for the field
mapping and the never-overwrite rule). get_properties then shows the
stored type_metadata on the detail page's Properties panel without
re-reading the file.

Read via ffprobe rather than a dedicated tag library (mutagen et al.):
ffmpeg is already in the image and already the subprocess this module
shells out to for duration/codec/bitrate, and libavformat normalizes each
container's tag vocabulary (ID3v2 TIT2/TPE1/TALB/TRCK/TDRC, Vorbis
TITLE/ARTIST/TRACKNUMBER/DATE, RIFF INAM/IART/...) to one key set for us.
Checked against the two real production audio uploads while scoping #255
(one ID3v2.3-tagged, one a tagless ID3v2.4 DASH->mp3 remux of a YouTube
rip): a mutagen raw-frame dump of the same files surfaced exactly the
frames ffprobe's format.tags already showed and nothing more, so a new
dependency would have bought nothing for what this gallery actually
holds. If a future upload ever shows a tag mutagen reads and ffprobe
drops, that's the moment to revisit — not before.
"""

import json
import re
import subprocess

from .. import storage


# ffprobe tag key (lowercased) -> type_metadata key. Deliberately an
# allowlist, not a dump of format.tags: a real file's tag block is full of
# things that must never land in a row — encoder strings, `major_brand:
# dash` from a DASH remux, binary id3v2_priv.* PRIV frames, "converted by
# ..." comments — all seen on the production files this was scoped
# against. "tracknumber" is the raw Vorbis-comment spelling, kept as a
# belt-and-braces alias for ffmpeg's own normalization to "track".
_TAG_TO_METADATA = (
    ("artist", "artist"),
    ("album", "album"),
    ("track", "track"),
    ("tracknumber", "track"),
    ("date", "year"),
    ("year", "year"),
    ("genre", "genre"),
)

# type_metadata key -> Properties-panel label, in display order.
_METADATA_LABELS = (
    ("artist", "Artist"),
    ("album", "Album"),
    ("track", "Track"),
    ("year", "Year"),
    ("genre", "Genre"),
)


def _stored_path(row):
    """Helper to get the full path to a stored audio file, or None if the
    file doesn't exist or wasn't provided."""
    stored_filename = row.get("stored_filename")
    if not stored_filename:
        return None
    path = storage.path_for(stored_filename)
    return path if path.exists() else None


def read_tags(path):
    """Every tag ffprobe finds on the file: keys lowercased, values
    stripped, empty values dropped. Container-level (format) tags win over
    stream-level ones where both exist — ffmpeg attaches ID3/RIFF tags to
    the format but Vorbis comments to the stream, so both are read.
    Raises on ffprobe/JSON failure; get_embedded_metadata is the
    best-effort wrapper."""
    cmd = [
        "ffprobe",
        "-v", "error",
        "-show_entries", "format_tags:stream_tags",
        "-of", "json",
        str(path),
    ]
    # errors="replace" rather than the default strict decode: a single
    # non-UTF-8 byte in some comment frame shouldn't cost the title.
    result = subprocess.run(cmd, capture_output=True, timeout=20, encoding="utf-8", errors="replace")
    if result.returncode != 0:
        return {}
    data = json.loads(result.stdout)
    tags = {}
    for stream in data.get("streams", []):
        tags.update(stream.get("tags") or {})
    tags.update(data.get("format", {}).get("tags") or {})
    return {key.lower(): value.strip() for key, value in tags.items() if isinstance(value, str) and value.strip()}


def get_embedded_metadata(path):
    """ObjectTypeSpec.embedded_metadata_fn for media_type='audio' (#255) —
    the file's title (-> content_description) and its artist/album/track/
    year/genre (-> type_metadata, see metadata_fields), or {} for a tagless
    file or on any failure. Junk-tolerant per what real files carry: a
    track of "0" (an encoder placeholder — tracks are 1-based) is treated
    as absent, and a date is reduced to its leading four-digit year."""
    try:
        tags = read_tags(path)
    except Exception as e:
        print(f"Audio tag extraction failed for {path}: {e!r}")
        return {}
    found = {}
    if tags.get("title"):
        found["content_description"] = tags["title"]
    metadata = {}
    for tag_key, meta_key in _TAG_TO_METADATA:
        value = tags.get(tag_key)
        if not value or meta_key in metadata:
            continue
        if meta_key == "year":
            m = re.match(r"(\d{4})", value)
            if not m:
                continue
            value = int(m.group(1))
        elif meta_key == "track":
            m = re.match(r"(\d+)", value)
            if m and int(m.group(1)) == 0:
                continue
        metadata[meta_key] = value
    if metadata:
        found["type_metadata"] = metadata
    return found


def get_properties(row):
    """ObjectTypeSpec.properties_fn for media_type='audio' — the #255 tag
    metadata already stored in the row's type_metadata (read from the row,
    not the file: no second ffprobe per page view), plus duration/codec/
    bitrate via ffprobe. Returns whatever subset could be produced, {} at
    worst."""
    props = {}
    type_metadata = row.get("type_metadata") or {}
    for key, label in _METADATA_LABELS:
        if type_metadata.get(key) not in (None, ""):
            props[label] = str(type_metadata[key])

    path = _stored_path(row)
    if not path:
        return props

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
            return props

        data = json.loads(result.stdout)

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
        return props


from . import register, ObjectTypeSpec, ThumbnailSource, MetadataField

register(ObjectTypeSpec(
    key="audio",
    label="Audio file",
    thumbnail_source=ThumbnailSource.NONE,
    ocr_capable=False,
    extensions=frozenset({".mp3", ".m4a", ".ogg", ".wav"}),
    properties_fn=get_properties,
    embedded_metadata_fn=get_embedded_metadata,
    # #255: written at upload time by core/embedded_metadata.py from the
    # file's own tags (get_embedded_metadata above) — see that module for
    # the field mapping (the title goes to content_description/display_name,
    # not here) and the never-overwrite rule. Only keys the file actually
    # carries are written: a tagless file (e.g. the DASH->mp3 YouTube-rip
    # remux that is one of the two real production audio uploads) gets
    # none of them, and that's expected, not a failure.
    metadata_fields=(
        MetadataField("artist", "Artist (ID3 TPE1 / Vorbis ARTIST)"),
        MetadataField("album", "Album (ID3 TALB / Vorbis ALBUM)"),
        MetadataField("track", "Track number as tagged — the raw string (\"3\" or \"3/12\", the of-total form is worth keeping), never an int; a tagged \"0\" is treated as absent"),
        MetadataField("year", "Four-digit year as an int, from the leading digits of the date/year tag (2019 from \"2019-05-01\"); deliberately not promoted to content_date"),
        MetadataField("genre", "Genre (ID3 TCON / Vorbis GENRE)"),
    ),
    badge_icon="\U0001F3B5",
    badge_text="AUDIO",
))
