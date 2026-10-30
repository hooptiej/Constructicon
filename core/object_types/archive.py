"""Archive file support (issue #128): content listing extraction for ZIP
and 7z archive formats.

Archives (ZIP and 7z) are text-extractable via a listing of member filenames —
this is useful for searching/indexing what's inside an archive without
actually extracting or OCR'ing the full contents (which would be slow and
noisy for a 1GB zip full of binary files).

Both formats are handled via stdlib (zipfile) and a pure-Python 7z library
(py7zr), requiring no new system packages. No visual thumbnail concept —
archives are NONE-sourced, same as written posts.

A bad upload never breaks the upload response or the OCR background task, but
a corrupt archive is not "no text" (#724 b2): the listing raises
UnreadableFile ("corrupt or unreadable .zip: ..."), which core/ocr.py reports.
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

from .. import parse_limits, storage
from . import _preview, _textstats

# #485: tar family. Listed by reading member headers only: never extracted to
# disk, never following symlinks (a link is reported as "name -> target" and
# nothing more). The caps bound a hostile archive's cost.
TAR_EXTENSIONS = (".tar.gz", ".tgz", ".tar", ".tar.bz2", ".tbz2", ".tar.xz", ".txz")  # #605: + bzip2 / xz
STATS_KEY = "archive_stats"
MAX_TAR_SECONDS = 20.0      # wall-clock budget (a gzip bomb decompresses slowly)
MAX_NAME_CHARS = 300

# #644: every format lists (and counts) at most max_members() members: parse_limits'
# CONSTRUCTICON_ARCHIVE_MAX_MEMBERS, default 5,000 (the largest real archive has 106). The whole
# listing is also capped at storage.MAX_EXTRACTED_TEXT_CHARS (a zip member's name can be 64 KB).
# Beyond either = truncated. Nothing is ever extracted. The parse itself runs in a parser child
# process (core/parse_limits.py), so its time and memory are bounded too.


def max_members():
    return parse_limits.archive_max_members()


def _capped_names(names):
    """(list, truncated): the first max_members() names whose joined listing fits the text cap."""
    limit, budget = max_members(), storage.MAX_EXTRACTED_TEXT_CHARS
    out, used = [], 0
    for name in names:
        used += len(name) + 1
        if len(out) >= limit or used > budget:
            return out, True
        out.append(name)
    return out, False


def is_tar_name(name):
    return str(name).lower().endswith(TAR_EXTENSIONS)


def _clean_name(name):
    name = "".join(ch if ch.isprintable() else "?" for ch in str(name))
    return name[:MAX_NAME_CHARS]


def scan_tar(path, errors=None):
    """Read member headers of a .tar/.tar.gz/.tgz -> (names, stats). Never
    extracts, never opens a member, caps members and time. Raises only if the
    archive can't be opened at all; a stream that breaks midway keeps what was
    read so far (stats["damaged"]), and the error is appended to `errors` when
    the caller passes a list (#724 b2: the text reader reports it)."""
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
                if stats["entries"] + stats["folders"] >= max_members() or time.monotonic() > deadline:
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
            if errors is not None:
                errors.append(e)
    return names, stats


def _stored_path(row):
    stored_filename = row.get("stored_filename")
    if not stored_filename:
        return None
    path = storage.path_for(stored_filename)
    return path if path.exists() else None


def extract_file_list(path):
    """Newline-joined list of member filenames in the archive at `path`; "" for
    an archive that opens and is empty (or a format this type doesn't list).

    #724 (b2): never "" for a broken archive. A zip / 7z / tar that can't be
    opened raises UnreadableFile ("corrupt or unreadable .zip: File is not a zip
    file"); a tar whose stream breaks partway raises it with the names read so
    far as partial_text (core/ocr.py keeps them as the text, with the warning);
    7z with py7zr missing raises NotCompatibleFile (grey: this install can't
    list .7z)."""
    if not path:
        return ""
    from . import NotCompatibleFile, UnreadableFile, unreadable

    ext = Path(path).suffix.lower()

    try:
        if is_tar_name(path):
            errors = []
            names, _stats = scan_tar(path, errors)
            listing = "\n".join(names)
            if errors:  # the stream broke partway: keep what was listed, and say so
                raise unreadable(path, errors[0], partial_text=listing)
            return listing
        if ext == ".zip":
            with zipfile.ZipFile(path, "r") as zf:
                names, _truncated = _capped_names(info.filename for info in zf.infolist())
                return "\n".join(names) if names else ""
        if ext == ".7z":
            if py7zr is None:
                raise NotCompatibleFile("7z archive: the 7z reader isn't installed here")
            with py7zr.SevenZipFile(path, "r") as zf:
                names, _truncated = _capped_names(zf.getnames())
                return "\n".join(names) if names else ""
        return ""
    except (MemoryError, UnreadableFile):
        raise  # already says why (NotCompatibleFile is an UnreadableFile too)
    except Exception as e:
        print(f"Archive listing failed for {path}: {e!r}")
        raise unreadable(path, e) from e


def extract_text_for_row(row):
    """ObjectTypeSpec.text_extract_fn for media_type='archive' — see
    core/ocr.py."""
    path = _stored_path(row)
    return extract_file_list(path) if path else ""


BY_TYPE_KEEP = 20  # #605: the summary keeps the 20 most common extensions, the rest as "other"


def _summarise(stats, names):
    """#605: adds by_type ({".html": 729, ...}, most common first) and max_depth (folders deep)."""
    by_type, depth = {}, 0
    for name in names:
        name = str(name).replace("\\", "/").rstrip("/")
        parts = [p for p in name.split("/") if p not in ("", ".")]
        if not parts:
            continue
        depth = max(depth, len(parts) - 1)
        ext = Path(parts[-1]).suffix.lower() or "(none)"
        by_type[ext] = by_type.get(ext, 0) + 1
    ranked = sorted(by_type.items(), key=lambda kv: (-kv[1], kv[0]))
    kept = dict(ranked[:BY_TYPE_KEEP])
    other = sum(n for _ext, n in ranked[BY_TYPE_KEEP:])
    if other:
        kept["other"] = other
    stats["by_type"], stats["max_depth"] = kept, depth
    return stats


def archive_stats(path):
    """#605: the member counts every archive stores in type_metadata[STATS_KEY] at upload: entries,
    folders, links, uncompressed (and compressed for zip), encrypted, truncated, damaged, by_type,
    max_depth. Reads the zip central directory / 7z header / tar member headers only (never a
    member's data, except that a compressed tar must be decompressed to be walked)."""
    limit = max_members()
    if is_tar_name(path):
        names, stats = scan_tar(path)
        return _summarise(stats, [n for n in names if not n.endswith("/") and " -> " not in n])
    ext = Path(path).suffix.lower()
    stats = {"entries": 0, "folders": 0, "links": 0, "uncompressed": 0, "truncated": False, "damaged": False,
             "encrypted": False}
    files = []
    if ext == ".zip":
        stats["compressed"] = 0
        with zipfile.ZipFile(path, "r") as zf:
            infos = zf.infolist()
            stats["truncated"] = len(infos) > limit
            for info in infos[:limit]:
                if info.is_dir():
                    stats["folders"] += 1
                    continue
                stats["entries"] += 1
                if ((info.external_attr >> 16) & 0o170000) == 0o120000:
                    stats["links"] += 1
                stats["uncompressed"] += max(info.file_size, 0)
                stats["compressed"] += max(info.compress_size, 0)
                stats["encrypted"] = stats["encrypted"] or bool(info.flag_bits & 0x1)
                files.append(info.filename)
    elif ext == ".7z":
        if py7zr is None:
            return {}
        with py7zr.SevenZipFile(path, "r") as zf:
            stats["encrypted"] = bool(zf.needs_password())
            members = zf.list()
            stats["truncated"] = len(members) > limit
            for info in members[:limit]:
                if info.is_directory:
                    stats["folders"] += 1
                    continue
                stats["entries"] += 1
                stats["uncompressed"] += int(getattr(info, "uncompressed", 0) or 0)
                files.append(info.filename)
    else:
        return {}
    return _summarise(stats, files)


def get_embedded_metadata(path):
    """ObjectTypeSpec.embedded_metadata_fn (#485, #605): the member counts and summary, computed once
    at upload and stored as type_metadata[STATS_KEY], never per view. #485 did this for tar only
    (its counts need a pass over the stream); #605 found zip and 7z items with an empty
    type_metadata (`{}`, the work install's Venza manual) because this returned {} for them, so
    every format stores its stats now. Existing items get them from the `archive_stats_605`
    migration. A failure is logged and leaves the row as it was (the properties panel still reads
    the central directory itself)."""
    try:
        stats = archive_stats(path)
    except Exception as e:
        print(f"Archive stats failed for {path}: {e!r}")
        return {}
    return {"type_metadata": {STATS_KEY: stats}} if stats else {}


def _tar_format(path):
    low = str(path).lower()
    if low.endswith((".gz", ".tgz")):
        return "tar (gzip)"
    if low.endswith((".bz2", ".tbz2")):
        return "tar (bzip2)"
    if low.endswith((".xz", ".txz")):
        return "tar (xz)"
    return "tar"


def _tar_properties(path, row):
    stats = (row.get("type_metadata") or {}).get(STATS_KEY)
    if not stats:
        stats = (get_embedded_metadata(path).get("type_metadata") or {}).get(STATS_KEY)
    if not stats:
        return {}
    plus = "+" if stats.get("truncated") else ""
    props = {
        "Format": _tar_format(path),
        "Entries": f"{stats['entries']:,}{plus}",
        "Folders": f"{stats['folders']:,}{plus if stats['folders'] else ''}",
    }
    if stats.get("links"):
        props["Symlinks/hardlinks"] = f"{stats['links']:,} (not followed)"
    if stats.get("uncompressed"):
        props["Uncompressed size"] = _textstats.human_size(stats["uncompressed"]) + (
            " (so far)" if stats.get("truncated") else "")
    if stats.get("truncated"):
        props["Listing"] = f"capped at {max_members():,} members"
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

                infos = zf.infolist()
                truncated = len(infos) > max_members()
                for info in infos[:max_members()]:
                    if info.is_dir():
                        folders.add(info.filename)
                    else:
                        entries.append(info.filename)
                    total_uncompressed += info.file_size
                    total_compressed += info.compress_size
                    if info.flag_bits & 0x1:
                        encrypted = True

                plus = "+" if truncated else ""
                props["Entries"] = f"{len(entries):,}{plus}"
                props["Folders"] = f"{len(folders)}{plus if folders else ''}"
                if total_uncompressed > 0:
                    props["Uncompressed size"] = _textstats.human_size(total_uncompressed) + (
                        " (so far)" if truncated else "")
                    ratio = 1 - (total_compressed / total_uncompressed) if total_uncompressed else 0
                    props["Compression"] = f"{ratio:.0%}"
                props["Encrypted"] = "yes" if encrypted else "no"
                if truncated:
                    props["Listing"] = f"capped at {max_members():,} members"

        elif ext == ".7z":
            if py7zr is None:
                props["Format"] = "7z"
                return props

            with py7zr.SevenZipFile(path, "r") as archive:
                entries = 0
                folders = set()
                total_uncompressed = 0

                members = archive.list()
                truncated = len(members) > max_members()
                for name in members[:max_members()]:
                    if name.is_directory:
                        folders.add(name.filename)
                    else:
                        entries += 1
                    total_uncompressed += name.uncompressed

                plus = "+" if truncated else ""
                props["Entries"] = f"{entries:,}{plus}"
                props["Folders"] = f"{len(folders)}{plus if folders else ''}"
                if total_uncompressed > 0:
                    props["Uncompressed size"] = _textstats.human_size(total_uncompressed) + (
                        " (so far)" if truncated else "")
                props["Encrypted"] = "yes" if archive.needs_password() else "no"
                if truncated:
                    props["Listing"] = f"capped at {max_members():,} members"

    except Exception as e:
        print(f"Archive properties extraction failed for {path}: {e!r}")
        pass

    return {**props, **_summary_properties(row)}


def _summary_properties(row):
    """#605: "Inside" (the most common kinds) and "Deepest folder", from the stats stored at upload."""
    stats = (row.get("type_metadata") or {}).get(STATS_KEY) or {}
    out = {}
    by_type = stats.get("by_type") or {}
    if by_type:
        top = list(by_type.items())[:5]
        rest = sum(by_type.values()) - sum(n for _e, n in top)
        out["Inside"] = ", ".join(f"{n:,} {ext}" for ext, n in top) + (f", {rest:,} more" if rest else "")
    if stats.get("max_depth"):
        out["Deepest folder"] = f"{stats['max_depth']} level{'s' if stats['max_depth'] != 1 else ''} down"
    return out


def _unpack(row, params):
    """#605 TypeAction handler: preview / start / status of turning this archive into a card. The
    work lives in core/unpack.py (a service module); this file only declares the action."""
    from .. import unpack  # lazy: unpack imports cards, ingest and the registry itself
    return unpack.run_action(row, params)


def _can_unpack(row):
    """#605: offered for a stored zip / 7z / tar archive (not a redacted one)."""
    from .. import archive_reader  # lazy: keeps this module light for the parser children
    return bool(row.get("stored_filename")) and not row.get("redacted") and \
        archive_reader.archive_format(row.get("filename") or row.get("stored_filename") or "") is not None


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
from . import register, ObjectTypeSpec, ThumbnailSource, TypeAction

register(ObjectTypeSpec(
    key="archive",
    label="Archive",
    thumbnail_source=ThumbnailSource.NONE,
    ocr_capable=True,  # Enable OCR background task so text_extract_fn gets called (no actual OCR since no thumbnail)
    extensions=frozenset({".zip", ".7z", ".tar", ".tar.gz", ".tgz", ".tar.bz2", ".tbz2", ".tar.xz", ".txz"}),
    # #605 "Unpack the Crate": turn the archive into a collection or project of its members. Takes
    # params (mode=preview|start|status, shape, depth); the item page opens its own dialog
    # (ui="unpack", static/js/unpack-crate.js) instead of the plain confirm-and-POST.
    actions=(TypeAction("unpack", "Unpack into a card…", _unpack, applies_fn=_can_unpack, takes_params=True,
                        ui="unpack"),),
    embedded_metadata_fn=get_embedded_metadata,  # #485: tar counts, once at upload
    text_extract_fn=extract_text_for_row,
    properties_fn=get_properties,
    preview_fn=preview,
    badge_icon="🗄️",
    badge_text="ZIP",
))
