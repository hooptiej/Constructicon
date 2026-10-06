"""Archive file support (issue #128): content listing extraction for ZIP
and 7z archive formats.

Archives (ZIP and 7z) are text-extractable via a listing of member filenames —
this is useful for searching/indexing what's inside an archive without
actually extracting or OCR'ing the full contents (which would be slow and
noisy for a 1GB zip full of binary files).

Both formats are handled via stdlib (zipfile) and a pure-Python 7z library
(py7zr), requiring no new system packages. No visual thumbnail concept —
archives are NONE-sourced, same as written posts.

Best-effort, same as every other type's text_extract_fn in this codebase: a
corrupt/unreadable archive returns "" rather than raising, so a bad upload
never breaks the upload response or the OCR background task.
"""

from pathlib import Path
import tarfile
import time
import zipfile
from markupsafe import Markup, escape

try:
    import py7zr
except ImportError:  # silent-ok: optional dependency; 7z is simply unsupported without it
    py7zr = None

from .. import storage
from . import _preview, _textstats

# #485: tar family. Listed by reading member headers only: never extracted to
# disk, never following symlinks (a link is reported as "name -> target" and
# nothing more). The caps bound a hostile archive's cost.
TAR_EXTENSIONS = (".tar.gz", ".tgz", ".tar")
STATS_KEY = "archive_stats"
MAX_TAR_MEMBERS = 5000      # members listed/counted; beyond this = truncated
MAX_TAR_SECONDS = 20.0      # wall-clock budget (a gzip bomb decompresses slowly)
MAX_NAME_CHARS = 300


def is_tar_name(name):
    return str(name).lower().endswith(TAR_EXTENSIONS)


def _clean_name(name):
    name = "".join(ch if ch.isprintable() else "?" for ch in str(name))
    return name[:MAX_NAME_CHARS]


def scan_tar(path):
    """Read member headers of a .tar/.tar.gz/.tgz -> (names, stats). Never
    extracts, never opens a member, caps members and time. Raises only if the
    archive can't be opened at all; a stream that breaks midway keeps what was
    read so far."""
    names = []
    stats = {"entries": 0, "folders": 0, "links": 0, "uncompressed": 0,
             "truncated": False, "damaged": False}
    deadline = time.monotonic() + MAX_TAR_SECONDS
    with tarfile.open(path, "r:*") as tf:
        try:
            while True:
                member = tf.next()
                if member is None:
                    break
                tf.members = []  # tarfile caches every member; don't let it grow
                if stats["entries"] + stats["folders"] >= MAX_TAR_MEMBERS or time.monotonic() > deadline:
                    stats["truncated"] = True
                    break
                label = _clean_name(member.name)
                if member.isdir():
                    stats["folders"] += 1
                    names.append(label if label.endswith("/") else label + "/")
                    continue
                stats["entries"] += 1
                if member.issym() or member.islnk():
                    stats["links"] += 1
                    label += " -> " + _clean_name(member.linkname)[:120]
                else:
                    stats["uncompressed"] += max(int(member.size or 0), 0)
                names.append(label)
        except (tarfile.TarError, EOFError, OSError, UnicodeError) as e:
            print(f"Tar listing stopped early for {path}: {e!r}")
            stats["damaged"] = True
    return names, stats


def _stored_path(row):
    stored_filename = row.get("stored_filename")
    if not stored_filename:
        return None
    path = storage.path_for(stored_filename)
    return path if path.exists() else None


def extract_file_list(path):
    """Newline-joined list of member filenames in the archive at `path`, or
    "" on any failure (corrupt archive, wrong format, missing file, py7zr not
    installed)."""
    if not path:
        return ""

    ext = Path(path).suffix.lower()

    try:
        if is_tar_name(path):
            names, _stats = scan_tar(path)
            return "\n".join(names)
        if ext == ".zip":
            with zipfile.ZipFile(path, "r") as zf:
                names = zf.namelist()
                return "\n".join(names) if names else ""
        elif ext == ".7z":
            if py7zr is None:
                return ""
            with py7zr.SevenZipFile(path, "r") as zf:
                names = zf.getnames()
                return "\n".join(names) if names else ""
        else:
            return ""
    except Exception as e:
        print(f"Archive listing failed for {path}: {e!r}")
        return ""


def extract_text_for_row(row):
    """ObjectTypeSpec.text_extract_fn for media_type='archive' — see
    core/ocr.py."""
    path = _stored_path(row)
    return extract_file_list(path) if path else ""


def get_embedded_metadata(path):
    """ObjectTypeSpec.embedded_metadata_fn (#485): a tar's counts need a pass
    over the whole stream (a .tar.gz must be decompressed to be walked), so
    they're computed once here and stored, never per view. Zip/7z keep their
    cheap central-directory read in properties_fn and return {}."""
    if not is_tar_name(path):
        return {}
    try:
        _names, stats = scan_tar(path)
        return {"type_metadata": {STATS_KEY: stats}}
    except Exception as e:
        print(f"Tar stats failed for {path}: {e!r}")
        return {}


def _tar_properties(path, row):
    stats = (row.get("type_metadata") or {}).get(STATS_KEY)
    if not stats:
        stats = (get_embedded_metadata(path).get("type_metadata") or {}).get(STATS_KEY)
    if not stats:
        return {}
    plus = "+" if stats.get("truncated") else ""
    props = {
        "Format": "tar (gzip)" if str(path).lower().endswith((".gz", ".tgz")) else "tar",
        "Entries": f"{stats['entries']:,}{plus}",
        "Folders": f"{stats['folders']:,}{plus if stats['folders'] else ''}",
    }
    if stats.get("links"):
        props["Symlinks/hardlinks"] = f"{stats['links']:,} (not followed)"
    if stats.get("uncompressed"):
        props["Uncompressed size"] = _textstats.human_size(stats["uncompressed"]) + (
            " (so far)" if stats.get("truncated") else "")
    if stats.get("truncated"):
        props["Listing"] = f"capped at {MAX_TAR_MEMBERS:,} members"
    if stats.get("damaged"):
        props["Integrity"] = "archive damaged or truncated; listing is partial"
    return props


def get_properties(row):
    """ObjectTypeSpec.properties_fn for media_type='archive': Entries,
    Folders, Uncompressed size, Compression, Encrypted. Returns {} on any
    failure."""
    path = _stored_path(row)
    if not path:
        return {}
    if is_tar_name(path):
        try:
            return _tar_properties(path, row)
        except Exception as e:
            print(f"Archive properties extraction failed for {path}: {e!r}")
            return {}

    ext = Path(path).suffix.lower()
    props = {}

    try:
        if ext == ".zip":
            with zipfile.ZipFile(path, "r") as zf:
                entries = []
                folders = set()
                total_uncompressed = 0
                total_compressed = 0
                encrypted = False

                for info in zf.infolist():
                    if info.is_dir():
                        folders.add(info.filename)
                    else:
                        entries.append(info.filename)
                    total_uncompressed += info.file_size
                    total_compressed += info.compress_size
                    if info.flag_bits & 0x1:
                        encrypted = True

                props["Entries"] = f"{len(entries):,}"
                props["Folders"] = str(len(folders))
                if total_uncompressed > 0:
                    props["Uncompressed size"] = _textstats.human_size(total_uncompressed)
                    ratio = 1 - (total_compressed / total_uncompressed) if total_uncompressed else 0
                    props["Compression"] = f"{ratio:.0%}"
                props["Encrypted"] = "yes" if encrypted else "no"

        elif ext == ".7z":
            if py7zr is None:
                props["Format"] = "7z"
                return props

            with py7zr.SevenZipFile(path, "r") as archive:
                entries = 0
                folders = set()
                total_uncompressed = 0

                for name in archive.list():
                    if name.is_directory:
                        folders.add(name.filename)
                    else:
                        entries += 1
                    total_uncompressed += name.uncompressed

                props["Entries"] = f"{entries:,}"
                props["Folders"] = str(len(folders))
                if total_uncompressed > 0:
                    props["Uncompressed size"] = _textstats.human_size(total_uncompressed)
                props["Encrypted"] = "yes" if archive.needs_password() else "no"

    except Exception as e:
        print(f"Archive properties extraction failed for {path}: {e!r}")
        pass

    return props


def preview(ctx):
    """#449 preview_fn: archive member listing (first 300 lines).
    The static export keeps its download link."""
    if ctx.mode != "live":
        return _preview.file_icon(ctx)
    text = ctx.item.get("extracted_text") or ""
    if not text:
        return None

    lines = text.split("\n")
    shown_lines = lines[:300]
    truncated = len(lines) > 300

    html = f'<pre class="ocr-text mono" style="max-height:70vh;overflow:auto">{escape(chr(10).join(shown_lines))}</pre>'
    if truncated:
        html += '<p class="muted">…truncated, showing first 300 entries</p>'

    return Markup(html)


# Registration: add this type to the object-type registry
from . import register, ObjectTypeSpec, ThumbnailSource

register(ObjectTypeSpec(
    key="archive",
    label="Archive",
    thumbnail_source=ThumbnailSource.NONE,
    ocr_capable=True,  # Enable OCR background task so text_extract_fn gets called (no actual OCR since no thumbnail)
    extensions=frozenset({".zip", ".7z", ".tar", ".tar.gz", ".tgz"}),
    embedded_metadata_fn=get_embedded_metadata,  # #485: tar counts, once at upload
    text_extract_fn=extract_text_for_row,
    properties_fn=get_properties,
    preview_fn=preview,
    badge_icon="🗄️",
    badge_text="ZIP",
))
