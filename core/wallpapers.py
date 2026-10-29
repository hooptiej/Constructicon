"""Wallpaper to go (#780): every wallpaper the viewer may take, as one streamed zip.

`plan(actor)` decides, up front and read-only, what goes in: the Wallpaper page's own list
(`db.list_wallpapers`, the tags in vocab 'wallpaper_tag'), then
  * `policy.filter_visible`: what this viewer can't see is left out and counted as "no access";
  * `policy.filter_exportable`: a sensitive item (restricted type or flagged) never leaves the
    install, even for an admin or its uploader, and is counted as "sensitive";
  * a link (no stored file) and a file missing on disk are counted too, by name (the viewer can see
    those).
Nothing is left out silently: the counts go on the page beside the button (`summary`) and into a
README.txt inside the zip (`readme`).

`stream(plan)` yields the zip as it is built, one chunk at a time (ZIP_STORED: wallpapers are already
compressed images, so deflating would only burn CPU). Nothing is held in memory beyond one read
buffer, and nothing is written to disk. Python's zipfile writes to an unseekable sink with data
descriptors, so the stream is a valid zip any unzip tool reads. A file that fails to read mid-stream
(deleted between the plan and the read) is skipped and named in the README, which is written last.

Each file keeps its original name; a clash (in any case, since Windows and macOS folders ignore case)
becomes "name (2).ext", "name (3).ext", ...; README.txt is reserved.
"""

import logging
import os
import time
import zipfile
from dataclasses import dataclass, field
from pathlib import PurePosixPath

from . import besteffort, db, item_title, policy, storage

log = logging.getLogger("constructicon.wallpapers")

README_NAME = "README.txt"
CHUNK = 1024 * 1024
ZIP_NAME = "wallpapers.zip"


@dataclass
class Entry:
    slug: str
    title: str
    arcname: str
    path: object
    size: int
    mtime: float


@dataclass
class Plan:
    entries: list = field(default_factory=list)
    no_access: int = 0
    sensitive: int = 0
    links: list = field(default_factory=list)      # titles of wallpapers with no stored file
    missing: list = field(default_factory=list)    # titles whose file is missing on disk

    @property
    def left_out(self):
        return self.no_access + self.sensitive + len(self.links) + len(self.missing)

    def summary(self):
        """The counts the page shows beside the button (and the route's headers)."""
        return {"included": len(self.entries), "bytes": sum(e.size for e in self.entries),
                "left_out": self.left_out, "no_access": self.no_access, "sensitive": self.sensitive,
                "links": len(self.links), "missing": len(self.missing),
                "notes": left_out_notes(self.no_access, self.sensitive, len(self.links), len(self.missing))}


def left_out_notes(no_access, sensitive, links, missing):
    """Plain sentences, one per reason, for the page and the README."""
    out = []
    if no_access:
        out.append(f"{no_access} not included (no access): you can't see {'it' if no_access == 1 else 'them'} "
                   "on this install.")
    if sensitive:
        out.append(f"{sensitive} not included (sensitive): sensitive and restricted items never leave the "
                   "install, whoever downloads.")
    if links:
        out.append(f"{links} not included (a link, not a file): there is no file to put in the zip.")
    if missing:
        out.append(f"{missing} not included (file missing on disk): ask an admin to check storage.")
    return out


def _safe_name(name, slug):
    """The original file name as a plain name in the zip root: no folders, no leading dots, never
    empty (falls back to the slug)."""
    base = PurePosixPath(str(name or "").replace("\\", "/")).name.strip().lstrip(".")
    base = "".join(c for c in base if c >= " " and c != "\x7f")
    return base or slug


def _dedupe(name, taken):
    stem, dot, ext = name.rpartition(".")
    if not dot or not stem:
        stem, ext = name, ""
    candidate, n = name, 1
    while candidate.lower() in taken:
        n += 1
        candidate = f"{stem} ({n}){'.' + ext if ext else ''}"
    taken.add(candidate.lower())
    return candidate


def plan(actor=None):
    """What the zip will hold for `actor` (default: the current actor context). Read-only."""
    rows = db.list_wallpapers()
    visible = policy.filter_visible(rows, actor)
    exportable = policy.filter_exportable(visible)
    p = Plan(no_access=len(rows) - len(visible), sensitive=len(visible) - len(exportable))
    taken = {README_NAME.lower()}
    for row in exportable:
        title = item_title.title_of(row)
        if not row.get("stored_filename"):
            p.links.append(title)
            continue
        path = storage.path_for(row["stored_filename"])
        try:
            st = os.stat(path)
        except OSError as e:
            besteffort.warn(log, "wallpaper zip: a wallpaper's file is missing on disk", e, slug=row["slug"])
            p.missing.append(title)
            continue
        p.entries.append(Entry(row["slug"], title, _dedupe(_safe_name(row.get("filename"), row["slug"]), taken),
                               path, st.st_size, st.st_mtime))
    return p


class _Sink:
    """A write-only file object zipfile writes into; the generator drains it after each write."""

    def __init__(self):
        self.parts = []

    def write(self, b):
        self.parts.append(bytes(b))
        return len(b)

    def flush(self):
        return None

    def take(self):
        out = b"".join(self.parts)
        self.parts.clear()
        return out


def _date_time(mtime):
    t = time.localtime(max(mtime, 315532800 + 86400))  # zip dates start in 1980
    return t[:6]


def readme(p, failed=(), who=None):
    lines = [f"Wallpapers, downloaded {time.strftime('%Y-%m-%d %H:%M')}{' by ' + who if who else ''}.", ""]
    ok = len(p.entries) - len(failed)
    lines.append(f"{ok} wallpaper{'s' if ok != 1 else ''} in this zip, under their original file names "
                 "(a name that clashes gets \" (2)\" and so on added).")
    notes = left_out_notes(p.no_access, p.sensitive, len(p.links), len(p.missing))
    if failed:
        notes.append(f"{len(failed)} not included (couldn't be read while the zip was made): "
                     + ", ".join(failed) + ".")
    if notes:
        lines += ["", "Left out:"] + [f"  - {n}" for n in notes]
        named = [("A link, not a file", p.links), ("File missing on disk", p.missing)]
        for label, titles in named:
            for t in titles:
                lines.append(f"      {label}: {t}")
    else:
        lines += ["", "Nothing was left out."]
    return "\r\n".join(lines) + "\r\n"


def stream(p, who=None):
    """Yields the zip's bytes as they are produced (see the module docstring)."""
    sink = _Sink()
    failed = []
    with zipfile.ZipFile(sink, "w", compression=zipfile.ZIP_STORED, allowZip64=True) as zf:
        for e in p.entries:
            zi = zipfile.ZipInfo(e.arcname, date_time=_date_time(e.mtime))
            zi.compress_type = zipfile.ZIP_STORED
            zi.external_attr = 0o644 << 16
            try:
                fh = open(e.path, "rb")
            except OSError as exc:
                besteffort.warn(log, "wallpaper zip: a file vanished before it was read", exc, slug=e.slug)
                failed.append(e.title)
                continue
            with fh, zf.open(zi, "w", force_zip64=e.size >= zipfile.ZIP64_LIMIT) as out:
                while True:
                    buf = fh.read(CHUNK)
                    if not buf:
                        break
                    out.write(buf)
                    chunk = sink.take()
                    if chunk:
                        yield chunk
            chunk = sink.take()
            if chunk:
                yield chunk
        zf.writestr(README_NAME, readme(p, failed, who))
    chunk = sink.take()
    if chunk:
        yield chunk
