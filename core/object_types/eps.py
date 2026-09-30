"""EPS support (issue #30): rendered raster thumbnail via Ghostscript,
wired into the object-type registry (core/object_types.py) as a
CAPTURE-sourced type's capture_fn — same shape as PDF's core/pdf.py.

Unlike SVG (core/svg.py), EPS rasterization has no pure-Python or
small-pip-dependency option at all: EPS is PostScript, and short of
shelling out to a real PostScript interpreter there's nothing in the Python
packaging ecosystem that reads it. Ghostscript (`gs`) is that interpreter —
a real system binary, the same category of dependency STL's core/stl.py had
to route *around* for issue #14 (no GPU/Mesa on this box), but investigated
fresh here since EPS's situation is different: Ghostscript is a small,
extremely standard, well-packaged apt dependency (confirmed for issue #30:
installs cleanly via `apt-get install -y --no-install-recommends
ghostscript` on this Dockerfile's python:3.14-slim base, adding ~47MB to the
image — mostly URW base-35 fonts and X11/font-rendering libraries it pulls
in for text layout, not Ghostscript itself) rather than something requiring
a GPU or display server this headless container can't provide. Installed
the same way tesseract-ocr already is (see Dockerfile) — one more apt-get
install line, no build-from-source, no exotic packages.

No text_extract_fn (unlike PDF/SVG): EPS is raw PostScript drawing
commands, not a document format with a real text layer to pull structured
text out of generically (the (Hello) show style text-drawing operators in a
real-world EPS are arbitrary PostScript, not a parseable text stream).
ocr_capable=True routes OCR against the rendered raster instead, same as
PSD's core/psd.py.

Best-effort, same as every other type's capture_fn in this codebase: a
corrupt/unreadable EPS, a `gs` failure/timeout, or a missing file all
return None rather than raising, so a bad upload never breaks the upload
response or the OCR background task that calls this indirectly via
thumbnails.ensure_thumbnail.
"""

import subprocess
import tempfile
from pathlib import Path

from .. import storage

# Ghostscript's -r is a DPI, not a pixel count — EPS's %%BoundingBox is
# defined in 72-DPI points, so this DPI gives a preview a few hundred pixels
# on a side for a typical letter/logo-sized EPS, sharp enough once
# storage.save_thumbnail_from_bytes downscales it to the standard
# THUMB_MAX_DIM thumbnail without wasting time rendering a huge page-sized
# EPS at full fidelity for a preview nobody will view above thumbnail size.
RENDER_DPI = 150
RENDER_TIMEOUT_SECONDS = 20  # a hung/pathological PostScript file shouldn't block the OCR background task forever


def render_raster(path):
    """PNG bytes of `path` rasterized via Ghostscript, or None on any
    failure (corrupt/unreadable EPS, `gs` missing/erroring, or a timeout)."""
    with tempfile.TemporaryDirectory() as tmp:
        out_path = Path(tmp) / "out.png"
        try:
            subprocess.run(
                [
                    "gs",
                    "-dNOPAUSE", "-dBATCH", "-dSAFER",
                    "-dEPSCrop",  # crop to the EPS's own %%BoundingBox rather than a fixed page size
                    "-sDEVICE=png16m",
                    f"-r{RENDER_DPI}",
                    f"-sOutputFile={out_path}",
                    str(path),
                ],
                capture_output=True,
                timeout=RENDER_TIMEOUT_SECONDS,
                check=True,
            )
        except Exception as e:
            print(f"EPS render failed for {path}: {e!r}")
            return None
        if not out_path.exists():
            return None
        return out_path.read_bytes()


def _stored_path(row):
    stored_filename = row.get("stored_filename")
    if not stored_filename:
        return None
    path = storage.path_for(stored_filename)
    return path if path.exists() else None


def capture_thumbnail(row):
    """ObjectTypeSpec.capture_fn for media_type='eps' — see
    core/thumbnails.py. media_type='eps' rows are still UPLOADED_FILE-shaped
    (the row's own stored_filename really is the EPS); CAPTURE is used here
    only because the representative image has to be rendered rather than
    being the file itself, the same reasoning as PDF/STL/PSD/SVG."""
    path = _stored_path(row)
    return render_raster(path) if path else None


def get_properties(row):
    """ObjectTypeSpec.properties_fn for media_type='eps' — reads first 256 KB,
    extracts DSC comments (BoundingBox, Title, Creator, CreationDate, LanguageLevel),
    or {} on any failure."""
    path = _stored_path(row)
    if not path:
        return {}
    try:
        with open(path, "rb") as f:
            data = f.read(256 * 1024)  # first 256 KB

        # Check for DOS-EPS binary header
        if data.startswith(b"\xc5\xd0\xd3\xc6"):
            # Binary EPS: extract PostScript section offset/length
            if len(data) >= 12:
                offset = int.from_bytes(data[4:8], byteorder="little")
                length = int.from_bytes(data[8:12], byteorder="little")
                if offset < len(data) and offset + length <= len(data):
                    ps_data = data[offset:offset+length]
                    return _parse_eps_dsc(ps_data.decode("latin-1", errors="ignore"))

        # Text EPS: decode directly
        text = data.decode("latin-1", errors="ignore")
        return _parse_eps_dsc(text)
    except Exception as e:
        print(f"EPS properties extraction failed for {path}: {e!r}")
        return {}


def _parse_eps_dsc(text):
    """Parse DSC comments from EPS text."""
    props = {}
    for line in text.split("\n"):
        line = line.strip()
        if line.startswith("%%BoundingBox:"):
            parts = line.split(":", 1)[1].strip().split()
            if len(parts) >= 4:
                try:
                    llx, lly, urx, ury = map(int, parts[:4])
                    w, h = urx - llx, ury - lly
                    props["Size"] = f"{w:g} × {h:g} pt ({w/72:.2f} × {h/72:.2f} in)"
                except (ValueError, ZeroDivisionError):
                    pass
        elif line.startswith("%%Title:"):
            title = line.split(":", 1)[1].strip()
            if title.startswith("(") and title.endswith(")"):
                title = title[1:-1]
            if title:
                props["Title"] = title
        elif line.startswith("%%Creator:"):
            creator = line.split(":", 1)[1].strip()
            if creator.startswith("(") and creator.endswith(")"):
                creator = creator[1:-1]
            if creator:
                props["Creator"] = creator
        elif line.startswith("%%CreationDate:"):
            created = line.split(":", 1)[1].strip()
            if created.startswith("(") and created.endswith(")"):
                created = created[1:-1]
            if created:
                props["Created"] = created
        elif line.startswith("%%LanguageLevel:"):
            level = line.split(":", 1)[1].strip()
            if level:
                props["PostScript level"] = level
    return props


from . import _preview


def preview(ctx):
    """#449 preview_fn: rendered raster + "View original" link. None (-> the page's generic fallback) when there's no thumbnail."""
    return _preview.thumb_with_original_link(ctx) if ctx.thumb_url else None


# Registration: add this type to the object-type registry
from . import register, ObjectTypeSpec, ThumbnailSource

register(ObjectTypeSpec(
    preview_fn=preview,  # #449
    key="eps",
    label="Vector graphic (EPS)",
    # Ghostscript-rendered raster (core/eps.py) — a real system binary,
    # investigated and found to be a small, standard apt dependency
    # rather than the kind of GPU/display-dependent tooling STL (#14)
    # had to route around.
    thumbnail_source=ThumbnailSource.CAPTURE,
    ocr_capable=True,  # OCR runs against the rendered raster; no text layer to extract directly (see core/eps.py)
    caption_capable=True,  # #239: the Ghostscript-rendered raster
    extensions=frozenset({".eps"}),
    capture_fn=capture_thumbnail,
    properties_fn=get_properties,  # #449
    badge_icon="\U0001F5A8️",  # printer — PostScript's original target device
    badge_text="EPS",
))
