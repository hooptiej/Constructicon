"""STL support (issue #14): a rendered isometric preview image, wired into
the object-type registry (core/object_types.py) as a CAPTURE-sourced type's
capture_fn — same shape as PDF's core/object_types/pdf.py.

Rendering a 2D preview of a 3D mesh is a genuinely different problem than
PDF's "just rasterize a page": there's no equivalent of PyMuPDF that ships a
software rasterizer for meshes, and the obvious "real" renderers (trimesh's
pyrender/OpenGL offscreen path, pyrender itself, etc.) need a GPU or at
least a working Mesa/EGL software-rendering stack — neither of which this
Dockerfile's plain `python:3.14-slim` base has (see Dockerfile: no mesa,
no libgl, no Xvfb, and the box itself has no GPU per the existing torch
CPU-only comment).

Instead: numpy-stl (import name `stl`) just parses the mesh's triangles into
a plain numpy array — no rendering, no system deps beyond numpy (already a
dependency). matplotlib's mplot3d toolkit (`Poly3DCollection`) then draws
those triangles as a flat-shaded isometric-ish projection using its Agg
backend, which is pure software rasterization — no OpenGL, no display, no
extra system packages. This is a rough preview (matplotlib's 3D toolkit
does a simple painter's-algorithm depth sort, not a real z-buffer, so a
convex mesh renders cleanly but a very concave one can occasionally show a
minor sorting artifact) rather than a polished render, which is exactly the
tradeoff the issue calls for over a fragile GPU-dependent pipeline.

Both entry points below are best-effort, same as every other type's
capture_fn in this codebase: a corrupt/empty/unreadable STL returns None
rather than raising, so a bad upload never breaks the upload response.
"""

import matplotlib

matplotlib.use("Agg")  # software rasterization only — no display, no GPU

from io import BytesIO

import matplotlib.pyplot as plt
import numpy as np
from mpl_toolkits.mplot3d.art3d import Poly3DCollection
from stl import mesh as stl_mesh

from .. import storage

# Preview render size in pixels (square) — same spirit as PDF's RENDER_ZOOM:
# sharp enough to look good once storage.save_thumbnail_from_bytes downscales
# it to the standard THUMB_MAX_DIM thumbnail.
RENDER_SIZE_PX = 500
RENDER_DPI = 100

# A preview render doesn't need every triangle of a dense scan/print mesh —
# it needs to look like the model. Above this many faces, evenly subsample
# down to it so a multi-million-triangle STL (a real risk for 3D-printing
# files sliced from scans) can't turn a thumbnail render into a multi-minute
# background task. This only affects the *preview image*, never the stored
# original file.
MAX_PREVIEW_FACES = 20000

# Colors matched to storage.THUMB_BG (the app's dark page background) so a
# rendered STL preview looks at home next to a PDF page thumbnail rather
# than showing a stray white/default matplotlib background.
BG_COLOR = "#14170f"
FACE_COLOR = "#8a8f7a"
EDGE_COLOR = "#2b2e22"


def _stored_path(row):
    stored_filename = row.get("stored_filename")
    if not stored_filename:
        return None
    path = storage.path_for(stored_filename)
    return path if path.exists() else None


def render_preview(path):
    """PNG bytes of an isometric-ish preview render of the mesh at `path`,
    or None on any failure (corrupt/empty STL, unreadable file). Downsamples
    very dense meshes (see MAX_PREVIEW_FACES) since this is a preview, not a
    print-quality inspection view."""
    try:
        mesh = stl_mesh.Mesh.from_file(str(path))
    except Exception as e:
        print(f"STL parse failed for {path}: {e!r}")
        return None

    try:
        vectors = mesh.vectors  # shape (n_faces, 3, 3) - already-two arrays of triangle corners
        if vectors.shape[0] == 0:
            return None
        if vectors.shape[0] > MAX_PREVIEW_FACES:
            step = vectors.shape[0] // MAX_PREVIEW_FACES
            vectors = vectors[::step]

        fig = plt.figure(figsize=(RENDER_SIZE_PX / RENDER_DPI, RENDER_SIZE_PX / RENDER_DPI), dpi=RENDER_DPI)
        fig.patch.set_facecolor(BG_COLOR)
        ax = fig.add_subplot(111, projection="3d")
        ax.set_facecolor(BG_COLOR)

        collection = Poly3DCollection(vectors, facecolor=FACE_COLOR, edgecolor=EDGE_COLOR, linewidths=0.15)
        ax.add_collection3d(collection)

        # auto_scale_xyz on the full (pre-downsample) point cloud so the
        # framing reflects the whole model even if the preview render itself
        # only drew a subset of faces.
        all_points = mesh.points.reshape(-1, 3)
        span = np.concatenate([all_points.min(axis=0), all_points.max(axis=0)])
        ax.auto_scale_xyz(span, span, span)
        ax.view_init(elev=25, azim=45)  # isometric-ish, same corner every model is viewed from
        ax.set_axis_off()

        buf = BytesIO()
        fig.savefig(buf, format="png", facecolor=fig.get_facecolor())
        plt.close(fig)
        return buf.getvalue()
    except Exception as e:
        print(f"STL render failed for {path}: {e!r}")
        try:
            plt.close(fig)
        except Exception:
            pass
        return None


def capture_thumbnail(row):
    """ObjectTypeSpec.capture_fn for media_type='stl' — see
    core/thumbnails.py. Same reasoning as PDF's capture_thumbnail: the row's
    own stored_filename really is the STL file, CAPTURE is used only because
    the representative image has to be rendered rather than being the file
    itself."""
    path = _stored_path(row)
    return render_preview(path) if path else None


# Registration: add this type to the object-type registry
from . import register, ObjectTypeSpec, ThumbnailSource

register(ObjectTypeSpec(
    key="stl",
    label="3D printing file",
    thumbnail_source=ThumbnailSource.CAPTURE,
    ocr_capable=False,  # binary mesh format, no meaningful text to extract
    extensions=frozenset(storage.STL_EXTENSIONS),
    capture_fn=capture_thumbnail,
    badge_icon="\U0001F9CA",  # ice cube — closest built-in glyph to a 3D-printed block
    badge_text="STL",
))
