"""STL support (issue #14): a rendered preview image, wired into
the object-type registry (core/object_types.py) as a CAPTURE-sourced type's
capture_fn — same shape as PDF's core/object_types/pdf.py.

#742 (owner decision: headless OpenGL): the thumbnail is now a real render. trimesh builds the mesh,
pyrender draws it offscreen through Mesa's software rasterizer (llvmpipe) over EGL, with a z-buffer,
smooth shading that keeps sharp edges, three lights and an auto-framed 3/4 camera. The image carries
the pinned Mesa/EGL libraries (Dockerfile) and sets PYOPENGL_PLATFORM=egl. Details:
- `_import_gl()` is the only place the GL stack is imported, lazily, inside the parser child: a GL
  problem can never stop the app booting. It stubs `pyrender.viewer` (the interactive pyglet window,
  which would pull in pyglet and X11 libraries the offscreen path never uses) and restores `np.infty`
  (removed in numpy 2, still read by pyrender 0.1.45's Mesh.bounds).
- Dense meshes: the MAX_PREVIEW_FACES cap stays, but the GL path reduces by vertex clustering
  (`_cluster_decimate`) instead of keeping every n-th triangle, which leaves holes in a solid render.
- **Never silent:** if the GL render fails for any reason (a library missing, as on Windows, a broken
  mesh, a context that won't start) the old matplotlib renderer below draws it and the failure is
  logged through `besteffort.warn`. Which one drew the thumbnail is recorded in type_metadata
  `thumb_renderer` (`pyrender-egl` / `pyrender-osmesa` / `matplotlib`) via `CaptureResult`.
- `render_png(path, renderer="auto"|"gl"|"matplotlib")` is the shared entry point; the command line
  `python3 -m core.manage.render_stl <in.stl> <out.png>` uses it for before/after comparisons.
- The object page also gets an in-page 3D viewer (three.js, vendored; `preview()` below and
  web/static/js/stl-viewer.js), capped at VIEWER_MAX_MB.

The original matplotlib notes, still true of the fallback:

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

import logging
import os
import sys
import types
from io import BytesIO

import matplotlib.pyplot as plt
import numpy as np
from mpl_toolkits.mplot3d.art3d import Poly3DCollection
from stl import mesh as stl_mesh

from .. import besteffort, storage

log = logging.getLogger("constructicon.stl")

# #742: which renderer made a thumbnail, recorded in type_metadata[THUMB_RENDERER_KEY].
THUMB_RENDERER_KEY = "thumb_renderer"
RENDERER_MATPLOTLIB = "matplotlib"
GL_PLATFORM_DEFAULT = "egl"  # the Dockerfile sets PYOPENGL_PLATFORM=egl too; osmesa also works

# #742 GL render: drawn at SUPERSAMPLE x the size and scaled down (cheap anti-aliasing).
GL_SUPERSAMPLE = 2
GL_YFOV_DEG = 30.0
GL_VIEW_AZIMUTH_DEG = -45.0   # from the front-right (-Y is a printed part's usual front)
GL_VIEW_ELEVATION_DEG = 28.0  # and a little above: the classic 3/4 view
GL_FRAME_MARGIN = 1.08        # the part fills the frame with ~8% to spare
GL_SMOOTH_ANGLE_DEG = 30.0    # faces meeting at less than this shade smooth; sharper edges stay crisp
GL_SMOOTH_ANGLE_DECIMATED_DEG = 60.0  # after clustering: needle slivers sit 30-60 deg off; 90 deg edges stay crisp
GL_MESH_COLOR = (0.62, 0.66, 0.52, 1.0)  # a pale sage: the app's olive, light enough to read on dark
GL_AMBIENT = 0.18

# #742 in-page viewer: files above this stay a thumbnail (the browser parses the whole file).
VIEWER_MAX_MB_DEFAULT = 64
VIEWER_MAX_MB_ENV = "CONSTRUCTICON_STL_VIEWER_MAX_MB"

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


def _load_vectors(path):
    """(vectors, all_points) of the STL at `path`: the (n, 3, 3) triangle corners with any
    non-finite triangle dropped, and the full point cloud for framing. None when the file can't be
    parsed or holds no triangles (logged)."""
    try:
        mesh = stl_mesh.Mesh.from_file(str(path))
    except Exception as e:
        print(f"STL parse failed for {path}: {e!r}")
        return None
    vectors = np.asarray(mesh.vectors, dtype=np.float64)  # shape (n_faces, 3, 3)
    if vectors.ndim != 3 or vectors.shape[0] == 0:
        print(f"STL has no triangles: {path}")
        return None
    finite = np.isfinite(vectors).all(axis=(1, 2))
    if not finite.all():
        vectors = vectors[finite]
        if vectors.shape[0] == 0:
            print(f"STL has no finite triangles: {path}")
            return None
    return vectors, vectors.reshape(-1, 3)


def render_png(path, renderer="auto", size_px=RENDER_SIZE_PX):
    """#742: (PNG bytes, renderer name) for the STL at `path`, or (None, None) when it can't be
    parsed or drawn at all. `renderer`: "auto" (GL, else matplotlib, the fallback logged),
    "gl" (GL only: no fallback, the failure raises), or "matplotlib" (the pre-#742 renderer)."""
    if renderer not in ("auto", "gl", "matplotlib"):
        raise ValueError(f"unknown STL renderer {renderer!r}: use auto, gl or matplotlib")
    loaded = _load_vectors(path)
    if loaded is None:
        return None, None
    vectors, all_points = loaded
    if renderer in ("auto", "gl"):
        try:
            return _render_gl(vectors, size_px), _gl_renderer_name()
        except Exception as e:
            if renderer == "gl":
                raise
            besteffort.warn(log, "stl: GL render failed, falling back to the matplotlib renderer", e,
                            path=str(path), platform=_gl_platform())
    png = _render_matplotlib(vectors, all_points, size_px, path)
    return (png, RENDERER_MATPLOTLIB) if png else (None, None)


def render_preview(path):
    """PNG bytes of the preview render of the mesh at `path` (GL, else matplotlib), or None on any
    failure. Kept for callers that only want the picture; see render_png."""
    return render_png(path)[0]


# --- #742: the headless OpenGL renderer -------------------------------------------------------

def _gl_platform():
    return os.environ.get("PYOPENGL_PLATFORM") or GL_PLATFORM_DEFAULT


def _gl_renderer_name():
    return f"pyrender-{_gl_platform()}"


def _import_gl():
    """(pyrender, trimesh), imported for offscreen rendering, or raise. PYOPENGL_PLATFORM must be
    set before PyOpenGL is first imported, so it defaults here. pyrender's package __init__ imports
    its interactive Viewer (pyglet, which needs X11/GLU and a display): a stub module stands in for
    it, since only OffscreenRenderer is used. numpy 2 removed np.infty, which pyrender 0.1.45 still
    reads (Mesh.bounds); it is restored as an alias of np.inf."""
    os.environ.setdefault("PYOPENGL_PLATFORM", GL_PLATFORM_DEFAULT)
    if "pyrender.viewer" not in sys.modules:
        stub = types.ModuleType("pyrender.viewer")
        stub.Viewer = None  # never used: offscreen only
        sys.modules["pyrender.viewer"] = stub
    try:
        np.infty  # noqa: B018
    except AttributeError:  # silent-ok: numpy 2 removed the alias; restoring it is the whole point
        np.infty = np.inf
    import pyrender
    import trimesh
    return pyrender, trimesh


def _cluster_decimate(vectors, max_faces):
    """(vertices, faces) of a triangle soup reduced to at most `max_faces` by vertex clustering:
    corners are snapped to a grid and merged, and triangles that collapse are dropped. Unlike
    keeping every n-th triangle, the surface stays closed, so a solid render shows no holes. The
    grid gets coarser until the count fits."""
    pts = vectors.reshape(-1, 3)
    lo = pts.min(axis=0)
    span = float((pts.max(axis=0) - lo).max()) or 1.0
    # Corners that are "the same" but differ in the last bits (an exporter's rounding) could land
    # either side of a grid line and open a crack: snap them together first (to 1e-6 of the size).
    quantum = span * 1e-6
    pts = np.round((pts - lo) / quantum) * quantum + lo
    for res in (1024, 768, 512, 384, 256, 192, 128, 96, 64, 48, 32, 24, 16):
        q = np.minimum(np.floor((pts - lo) / (span / res)).astype(np.int64), res)
        key = (q[:, 0] * (res + 1) + q[:, 1]) * (res + 1) + q[:, 2]
        uniq, inv = np.unique(key, return_inverse=True)
        faces = inv.reshape(-1, 3)
        keep = (faces[:, 0] != faces[:, 1]) & (faces[:, 1] != faces[:, 2]) & (faces[:, 0] != faces[:, 2])
        faces = faces[keep]
        _, first = np.unique(np.sort(faces, axis=1), axis=0, return_index=True)
        faces = faces[np.sort(first)]
        if len(faces) <= max_faces:
            break
    if len(faces) > max_faces:  # a pathological mesh: the coarsest grid still didn't fit
        faces = faces[:: int(np.ceil(len(faces) / max_faces))]
    counts = np.bincount(inv, minlength=len(uniq)).astype(np.float64)
    verts = np.stack([np.bincount(inv, weights=pts[:, i], minlength=len(uniq)) for i in range(3)], axis=1)
    verts /= np.maximum(counts, 1.0)[:, None]
    return verts, faces


def _look_at(eye, target, up=(0.0, 0.0, 1.0)):
    """A 4x4 camera/light pose at `eye` looking at `target` (pyrender looks down its -Z axis)."""
    eye = np.asarray(eye, dtype=np.float64)
    back = eye - np.asarray(target, dtype=np.float64)
    back /= np.linalg.norm(back)
    up = np.asarray(up, dtype=np.float64)
    if abs(float(np.dot(up, back))) > 0.999:  # looking straight up or down: any other up will do
        up = np.array([0.0, 1.0, 0.0])
    right = np.cross(up, back)
    right /= np.linalg.norm(right)
    true_up = np.cross(back, right)
    pose = np.eye(4)
    pose[:3, 0], pose[:3, 1], pose[:3, 2], pose[:3, 3] = right, true_up, back, eye
    return pose


def _view_direction(azimuth_deg=GL_VIEW_AZIMUTH_DEG, elevation_deg=GL_VIEW_ELEVATION_DEG):
    az, el = np.radians(azimuth_deg), np.radians(elevation_deg)
    return np.array([np.cos(el) * np.cos(az), np.cos(el) * np.sin(az), np.sin(el)])


def _frame_camera(points, yfov_deg=GL_YFOV_DEG):
    """(pose, center, distance, znear, zfar) for a perspective camera on the 3/4 view direction that
    fits every point with GL_FRAME_MARGIN to spare (exact: each point's own distance need, not a
    bounding sphere)."""
    lo, hi = points.min(axis=0), points.max(axis=0)
    center = (lo + hi) / 2.0
    radius = float(np.linalg.norm(hi - lo)) / 2.0
    if not radius > 0:
        raise ValueError("the mesh has no extent (every point is the same)")
    back = _view_direction()
    pose = _look_at(center + back, center)
    right, up = pose[:3, 0], pose[:3, 1]
    rel = points - center
    u, v, w = np.abs(rel @ right), np.abs(rel @ up), rel @ back
    t = np.tan(np.radians(yfov_deg) / 2.0) / GL_FRAME_MARGIN
    distance = float(np.max(w + np.maximum(u, v) / t))
    pose = _look_at(center + back * distance, center)
    nearest = distance - float(w.max())
    znear = max(nearest * 0.5, distance * 1e-3)
    zfar = (distance - float(w.min())) * 1.5 + radius
    return pose, center, distance, znear, zfar


def _render_gl(vectors, size_px):
    """PNG bytes of a headless OpenGL render (pyrender offscreen). Raises on any failure; render_png
    decides whether to fall back."""
    pyrender, trimesh = _import_gl()
    from PIL import Image

    decimated = len(vectors) > MAX_PREVIEW_FACES
    if decimated:
        verts, faces = _cluster_decimate(vectors, MAX_PREVIEW_FACES)
    else:
        verts, faces = vectors.reshape(-1, 3), np.arange(len(vectors) * 3).reshape(-1, 3)
    tm = trimesh.Trimesh(vertices=verts, faces=faces, process=True)
    if decimated and len(tm.faces):
        # Clustering can fold a sliver over so it faces inward: a dark speck in the render, or a hole
        # if it were dropped. Turn those faces back round (reverse their winding) to match their
        # corners' averaged normals.
        corner = tm.vertex_normals[tm.faces].mean(axis=1)
        flipped = np.einsum("ij,ij->i", corner, tm.face_normals) < 0
        if flipped.any():
            faces = np.array(tm.faces)
            faces[flipped] = faces[flipped][:, ::-1]
            tm = trimesh.Trimesh(vertices=tm.vertices, faces=faces, process=True)
    if len(tm.faces) == 0:
        raise ValueError("no drawable triangles after cleanup")
    try:  # smooth curved surfaces, keep sharp edges (faces split where they meet at > the angle)
        angle = GL_SMOOTH_ANGLE_DECIMATED_DEG if decimated else GL_SMOOTH_ANGLE_DEG
        tm = trimesh.graph.smooth_shade(tm, angle=np.radians(angle))
        positions, normals, indices = tm.vertices, tm.vertex_normals, tm.faces
    except Exception as e:  # flat shading still renders correctly; say so
        besteffort.warn(log, "stl: smooth shading failed, rendering flat", e)
        tri = tm.vertices[tm.faces].reshape(-1, 3)
        positions, normals = tri, np.repeat(tm.face_normals, 3, axis=0)
        indices = np.arange(len(tri)).reshape(-1, 3)

    material = pyrender.MetallicRoughnessMaterial(
        baseColorFactor=GL_MESH_COLOR, metallicFactor=0.05, roughnessFactor=0.6, doubleSided=True)
    primitive = pyrender.Primitive(positions=np.asarray(positions, dtype=np.float32),
                                   normals=np.asarray(normals, dtype=np.float32),
                                   indices=np.asarray(indices, dtype=np.uint32), material=material)
    bg = [c / 255.0 for c in storage.THUMB_BG] + [1.0]
    scene = pyrender.Scene(bg_color=bg, ambient_light=[GL_AMBIENT] * 3)
    scene.add(pyrender.Mesh(primitives=[primitive]))

    pose, center, distance, znear, zfar = _frame_camera(np.asarray(tm.vertices, dtype=np.float64))
    scene.add(pyrender.PerspectiveCamera(yfov=np.radians(GL_YFOV_DEG), aspectRatio=1.0, znear=znear, zfar=zfar),
              pose=pose)
    # Three directional lights in the camera's frame: a key from upper left, a soft fill from the
    # right, and a rim from behind so the silhouette separates from the dark background.
    right, up, back = pose[:3, 0], pose[:3, 1], pose[:3, 2]
    lights = [(-0.6 * right + 0.8 * up + back, 2.4),
              (0.9 * right + 0.1 * up + back, 0.8),
              (0.2 * right + 0.9 * up - back, 1.5)]
    for offset, intensity in lights:
        scene.add(pyrender.DirectionalLight(color=np.ones(3), intensity=intensity),
                  pose=_look_at(center + offset * distance, center))

    px = int(size_px) * GL_SUPERSAMPLE
    renderer = pyrender.OffscreenRenderer(px, px)
    try:
        color, _depth = renderer.render(scene)
    finally:
        renderer.delete()
    img = Image.fromarray(np.ascontiguousarray(color[..., :3])).resize((int(size_px), int(size_px)), Image.LANCZOS)
    buf = BytesIO()
    img.save(buf, format="PNG", optimize=True)
    return buf.getvalue()


# --- the pre-#742 renderer: matplotlib, now the fallback -------------------------------------

def _render_matplotlib(vectors, all_points, size_px, path):
    """PNG bytes of the matplotlib (mplot3d, painter's algorithm) render, or None (logged)."""
    fig = None
    try:
        if vectors.shape[0] > MAX_PREVIEW_FACES:
            step = vectors.shape[0] // MAX_PREVIEW_FACES
            vectors = vectors[::step]

        fig = plt.figure(figsize=(size_px / RENDER_DPI, size_px / RENDER_DPI), dpi=RENDER_DPI)
        fig.patch.set_facecolor(BG_COLOR)
        ax = fig.add_subplot(111, projection="3d")
        ax.set_facecolor(BG_COLOR)

        collection = Poly3DCollection(vectors, facecolor=FACE_COLOR, edgecolor=EDGE_COLOR, linewidths=0.15)
        ax.add_collection3d(collection)

        # auto_scale_xyz on the full (pre-downsample) point cloud so the
        # framing reflects the whole model even if the preview render itself
        # only drew a subset of faces.
        span =np.concatenate([all_points.min(axis=0), all_points.max(axis=0)])
        ax.auto_scale_xyz(span, span, span)
        ax.view_init(elev=25, azim=45)  # isometric-ish, same corner every model is viewed from
        ax.set_axis_off()

        buf = BytesIO()
        fig.savefig(buf, format="png", facecolor=fig.get_facecolor())
        plt.close(fig)
        return buf.getvalue()
    except Exception as e:
        print(f"STL render failed for {path}: {e!r}")
        if fig is not None:
            try:
                plt.close(fig)
            except Exception:  # silent-ok: closing the figure after the render failure that was logged above
                pass
        return None


def capture_thumbnail(row):
    """ObjectTypeSpec.capture_fn for media_type='stl' — see
    core/thumbnails.py. Same reasoning as PDF's capture_thumbnail: the row's
    own stored_filename really is the STL file, CAPTURE is used only because
    the representative image has to be rendered rather than being the file
    itself. #742: returns a CaptureResult so the row records which renderer drew it."""
    path = _stored_path(row)
    if not path:
        return None
    png, renderer = render_png(path)
    if not png:
        return None
    from . import CaptureResult
    return CaptureResult(png, {THUMB_RENDERER_KEY: renderer})


# #449: full mesh stats are too slow to compute on every page view (7.9 s for
# a real 22 MB / 465k-triangle file), so they're computed ONCE at upload by
# get_embedded_metadata (core/embedded_metadata.py runs it synchronously in
# the ingest pipeline) and stored under type_metadata[STATS_KEY]. The page
# only formats what's stored; until then it shows header-only facts.
STATS_KEY = "stl_stats"


def _binary_triangle_count(path):
    """Triangle count from a binary STL's header (uint32 at byte 80), or None
    if the file isn't a well-formed binary STL. Constant time."""
    size = path.stat().st_size
    if size < 84:
        return None
    with open(path, "rb") as f:
        f.seek(80)
        n = int.from_bytes(f.read(4), "little")
    return n if size == 84 + 50 * n else None


def get_embedded_metadata(path):
    """ObjectTypeSpec.embedded_metadata_fn for media_type='stl' (#449): the
    full mesh stats, computed once at upload and stored in type_metadata.
    Uses numpy-stl's EXACT watertight check (the fast one warns it can give
    false positives/negatives); it's affordable because it runs once.
    Returns {} on any failure, like every other embedded_metadata_fn."""
    try:
        mesh = stl_mesh.Mesh.from_file(str(path))
        dims = [float(v) for v in (mesh.max_ - mesh.min_)]
        stats = {
            "triangles": int(len(mesh.vectors)),
            "dims": dims,
            "format": "binary" if _binary_triangle_count(path) is not None else "ASCII",
            "watertight": bool(mesh.is_closed(exact=True)),
        }
        if stats["watertight"]:
            volume, _, _ = mesh.get_mass_properties()
            stats["volume"] = abs(float(volume))
        return {"type_metadata": {STATS_KEY: stats}}
    except Exception as e:
        print(f"STL stats extraction failed for {path}: {e!r}")
        return {}


def get_properties(row):
    """ObjectTypeSpec.properties_fn for media_type='stl': formats the stats
    stored at upload (see get_embedded_metadata). Rows without them yet (not
    backfilled) get only header facts. Never parses the mesh here."""
    try:
        stats = (row.get("type_metadata") or {}).get(STATS_KEY)
        if stats:
            x, y, z = stats["dims"]
            props = {
                "Triangles": f"{stats['triangles']:,}",
                "Dimensions": f"{x:.2f} × {y:.2f} × {z:.2f} (model units)",
                "Format": stats["format"],
                "Watertight": "yes" if stats["watertight"] else "no",
            }
            if stats.get("volume") is not None:
                props["Volume"] = f"{stats['volume']:.2f} (model units³)"
            return props
        path = _stored_path(row)
        if not path:
            return {}
        n = _binary_triangle_count(path)
        if n is not None:
            return {"Triangles": f"{n:,}", "Format": "binary"}
        return {"Format": "ASCII"}
    except Exception as e:
        print(f"STL properties failed for {row.get('slug')}: {e!r}")
        return {}


from markupsafe import Markup

from . import _preview


VIEWER_AUTOLOAD_MB = 8  # #742: smaller files open in 3D at once; bigger ones wait for a click


def viewer_max_bytes():
    """#742: the largest STL the in-page viewer will load (CONSTRUCTICON_STL_VIEWER_MAX_MB, default
    VIEWER_MAX_MB_DEFAULT; a bad or non-positive value falls back to the default)."""
    raw = os.environ.get(VIEWER_MAX_MB_ENV, "").strip()
    try:
        mb = float(raw) if raw else VIEWER_MAX_MB_DEFAULT
    except ValueError:  # silent-ok: a bad setting means the default, documented above
        mb = VIEWER_MAX_MB_DEFAULT
    if not mb > 0:
        mb = VIEWER_MAX_MB_DEFAULT
    return int(mb * 1024 * 1024)


def _file_size(ctx):
    try:
        if ctx.file_path is not None and ctx.file_path.exists():
            return int(ctx.file_path.stat().st_size)
    except OSError as e:
        besteffort.warn(log, "stl: couldn't read the file size for the 3D viewer", e, slug=ctx.item.get("slug"))
    size = ctx.item.get("file_size")
    return int(size) if isinstance(size, (int, float)) else None


def preview(ctx):
    """#449 preview_fn. Export: the rendered thumbnail + link, as before. Live (#742): the in-page 3D
    viewer (web/static/js/stl-viewer.js, three.js vendored under web/static/vendor/three/) around the
    rendered thumbnail, which stays as the fallback (and is what shows until the viewer loads, when
    the file is over the viewer's cap, or when the vendored files are missing). The page carries the
    file URL, its size and the caps as data attributes; the JS decides and always says why when it
    doesn't show the 3D view. None (-> the page's generic fallback) with no thumbnail and no file."""
    if ctx.mode != "live":
        return _preview.thumb_with_original_link(ctx) if ctx.thumb_url else None
    if not ctx.thumb_url and not ctx.media_url:
        return None
    inner = _preview.thumb_with_original_link(ctx) if ctx.thumb_url else Markup(
        '<div class="capture-preview">' + _preview.original_link_html(ctx.media_url, ctx.item) + "</div>")
    if not ctx.media_url:
        return inner
    size = _file_size(ctx)
    return Markup(
        '<div class="stl-viewer" id="stl-viewer" data-src="{src}" data-size="{size}" data-max="{mx}" '
        'data-autoload="{auto}">'
        '<div class="stl-viewer-stage" id="stl-viewer-stage" hidden></div>'
        '{inner}'
        '<div class="stl-viewer-bar"><button type="button" class="dp-btn stl-viewer-load" id="stl-viewer-load" hidden>'
        'View in 3D</button><span class="stl-viewer-note muted" id="stl-viewer-note" role="status"></span></div>'
        '</div>'
    ).format(src=ctx.media_url, size="" if size is None else size, mx=viewer_max_bytes(),
             auto=VIEWER_AUTOLOAD_MB * 1024 * 1024, inner=inner)


# Registration: add this type to the object-type registry
from . import register, ObjectTypeSpec, ThumbnailSource

register(ObjectTypeSpec(
    preview_fn=preview,  # #449
    preview_assets=("stl_viewer",),  # #742: the in-page three.js viewer
    key="stl",
    label="3D printing file",
    thumbnail_source=ThumbnailSource.CAPTURE,
    ocr_capable=False,  # binary mesh format, no meaningful text to extract
    # #239: explicitly NOT captioned. The plain-background wireframe preview
    # above produced degenerate repeated-punctuation garbage from moondream
    # in testing (2026-09-09) — a hard failure, not a weak caption. This is
    # its own deliberate exclusion, not a side effect of ocr_capable=False.
    caption_capable=False,
    extensions=frozenset({".stl"}),
    capture_fn=capture_thumbnail,
    properties_fn=get_properties,  # #449
    embedded_metadata_fn=get_embedded_metadata,  # #449: stats once, at upload
    badge_icon="\U0001F9CA",  # ice cube — closest built-in glyph to a 3D-printed block
    badge_text="STL",
))
