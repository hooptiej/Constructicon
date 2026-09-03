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
            result = subprocess.run(
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


# Registration: add this type to the object-type registry
from . import register, ObjectTypeSpec, ThumbnailSource

register(ObjectTypeSpec(
    key="eps",
    label="Vector graphic (EPS)",
    # Ghostscript-rendered raster (core/eps.py) — a real system binary,
    # investigated and found to be a small, standard apt dependency
    # rather than the kind of GPU/display-dependent tooling STL (#14)
    # had to route around.
    thumbnail_source=ThumbnailSource.CAPTURE,
    ocr_capable=True,  # OCR runs against the rendered raster; no text layer to extract directly (see core/eps.py)
    extensions=frozenset(storage.EPS_EXTENSIONS),
    capture_fn=capture_thumbnail,
    badge_icon="\U0001F5A8️",  # printer — PostScript's original target device
    badge_text="EPS",
))
