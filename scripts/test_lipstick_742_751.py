#!/usr/bin/env python3
"""Batch 15, "Lipstick on the Warehouse": STL renders (#742) and bigger status lamps (#751).

Throwaway DB/storage (scripts/_testenv.py), the real FastAPI app through TestClient. Every STL is
generated here (numpy, nothing committed). Runs anywhere the requirements are installed; the GL
renderer needs the app image's Mesa/EGL libraries, so off the image (Windows) the GL-only checks say
SKIP with the reason and the fallback path is what gets exercised:

    python scripts/test_lipstick_742_751.py

Covers: the GL path is chosen when it works (faked, and for real when the GL stack imports); when it
fails, the matplotlib fallback draws the thumbnail, the failure is logged through besteffort.warn and
type_metadata.thumb_renderer records which renderer made it (through thumbnails.ensure_thumbnail);
the triangle cap (vertex clustering keeps it, closed surface); the object page's 3D viewer config
(file URL, size, cap from CONSTRUCTICON_STL_VIEWER_MAX_MB, the nonce'd import map, the vendored files
and their sha256); the lamp CSS sizes and the "Try anyway" label; the render_stl command line.
Exits 1 if any check fails.
"""
import contextlib
import hashlib
import io
import logging
import os
import re
import struct
import subprocess
import sys
import types

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
import _testenv  # noqa: E402
TMP = _testenv.isolate("lipstick742-")
os.environ["CAPTION_DISABLED"] = "1"
os.environ["CONSTRUCTICON_PARSE_SANDBOX"] = "off"  # hooks in-process, so the monkeypatches below apply
ROOT = os.path.abspath(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
sys.path.insert(0, ROOT)
sys.modules.setdefault("cairosvg", types.ModuleType("cairosvg"))  # no libcairo needed here

from pathlib import Path  # noqa: E402

import numpy as np  # noqa: E402
from PIL import Image  # noqa: E402

from core import besteffort, db, object_types, storage, thumbnails  # noqa: E402
from core.object_types import stl  # noqa: E402
_testenv.assert_isolated()

FAILS = []


def check(name, cond, extra=""):
    print(("PASS " if cond else "FAIL ") + name + (f"  [{extra}]" if (extra and not cond) else ""))
    if not cond:
        FAILS.append(name)


def skip(name, why):
    print(f"SKIP {name}: {why}")


def stl_bytes(tris):
    """A binary STL of the (n, 3, 3) triangles."""
    tris = np.asarray(tris, dtype=np.float32)
    out = io.BytesIO()
    out.write(b"lipstick742 test".ljust(80, b" "))
    out.write(struct.pack("<I", len(tris)))
    for t in tris:
        n = np.cross(t[1] - t[0], t[2] - t[0])
        n = n / (np.linalg.norm(n) or 1.0)
        out.write(struct.pack("<12fH", *n, *t.reshape(-1), 0))
    return out.getvalue()


def box_tris(sx=40.0, sy=30.0, sz=10.0):
    c = np.array([[x, y, z] for x in (0, sx) for y in (0, sy) for z in (0, sz)], dtype=float)
    quads = [(0, 1, 3, 2), (4, 6, 7, 5), (0, 4, 5, 1), (2, 3, 7, 6), (0, 2, 6, 4), (1, 5, 7, 3)]
    tris = []
    for a, b, cc, d in quads:
        tris += [(c[a], c[b], c[cc]), (c[a], c[cc], c[d])]
    return np.array(tris)


def sphere_tris(n_lat, n_lon, r=25.0):
    """A closed UV sphere: 2 * n_lat * n_lon triangles (minus the pole slivers' area, not count)."""
    th = np.linspace(0, np.pi, n_lat + 1)
    ph = np.linspace(0, 2 * np.pi, n_lon + 1)
    p = np.stack([r * np.sin(th)[:, None] * np.cos(ph)[None, :],
                  r * np.sin(th)[:, None] * np.sin(ph)[None, :],
                  r * np.cos(th)[:, None] * np.ones_like(ph)[None, :]], axis=-1)
    p[:, -1] = p[:, 0]   # the seam and the poles share exact corners, as an exported STL does
    p[0, :] = p[0, 0]
    p[-1, :] = p[-1, 0]
    a, b, c, d = p[:-1, :-1], p[1:, :-1], p[1:, 1:], p[:-1, 1:]
    t1 = np.stack([a, b, c], axis=2).reshape(-1, 3, 3)
    t2 = np.stack([a, c, d], axis=2).reshape(-1, 3, 3)
    return np.concatenate([t1, t2])


class Capture(logging.Handler):
    def __init__(self):
        super().__init__()
        self.records = []

    def emit(self, record):
        self.records.append(record.getMessage())


cap = Capture()
logging.getLogger("constructicon.stl").addHandler(cap)
logging.getLogger("constructicon.stl").setLevel(logging.WARNING)

WORK = Path(TMP) / "stl"
WORK.mkdir()
BOX = WORK / "box.stl"
BOX.write_bytes(stl_bytes(box_tris()))
DENSE = WORK / "dense.stl"
DENSE.write_bytes(stl_bytes(sphere_tris(150, 200)))  # 60,000 triangles, over the cap


def is_png(data, size=None):
    try:
        img = Image.open(io.BytesIO(data))
        return img.format == "PNG" and (size is None or img.size == (size, size))
    except Exception as e:
        print("   not a PNG:", e)
        return False


# ---- 1. which renderer: GL when it works, matplotlib (logged) when it doesn't ----------------
real_import_gl, real_render_gl = stl._import_gl, stl._render_gl

stl._render_gl = lambda vectors, size_px: b"\x89PNG-fake-gl"
png, used = stl.render_png(BOX)
check("GL path is chosen when it works", png == b"\x89PNG-fake-gl" and used == stl._gl_renderer_name(), used)
check("GL renderer name says the platform", used.startswith("pyrender-"), used)
stl._render_gl = real_render_gl


def broken_import():
    raise ImportError("libEGL.so.1: cannot open shared object file (test)")


stl._import_gl = broken_import
besteffort.reset()
cap.records.clear()
png, used = stl.render_png(BOX)
check("fallback: matplotlib draws it when GL can't import", used == stl.RENDERER_MATPLOTLIB and is_png(png, stl.RENDER_SIZE_PX), used)
check("fallback: the GL failure is logged (besteffort.warn), never silent",
      any("GL render failed" in m and "libEGL" in m for m in cap.records), cap.records)
try:
    stl.render_png(BOX, renderer="gl")
    check("renderer='gl' raises instead of falling back", False)
except ImportError:
    check("renderer='gl' raises instead of falling back", True)
png, used = stl.render_png(BOX, renderer="matplotlib")
check("renderer='matplotlib' is the old renderer", used == stl.RENDERER_MATPLOTLIB and is_png(png))
try:
    stl.render_png(BOX, renderer="vulkan")
    check("an unknown renderer name is refused", False)
except ValueError:
    check("an unknown renderer name is refused", True)
bad = WORK / "bad.stl"
bad.write_bytes(b"solid nope\nthis is not an stl\n")
check("a broken STL is (None, None), not a crash", stl.render_png(bad) == (None, None))

# capture_fn -> CaptureResult -> ensure_thumbnail records thumb_renderer on the row
db.init_db()
from web import app as webapp  # noqa: E402
client = _testenv.client(webapp.app, raise_server_exceptions=False)
with client:
    pass
r = client.post("/api/upload", files={"file": ("bracket.stl", BOX.read_bytes(), "application/octet-stream")})
check("upload an STL", r.status_code == 200, r.text[:200])
SLUG = (r.json().get("slug") or r.json().get("object", {}).get("slug")) if r.status_code == 200 else None
row = db.get_by_slug(SLUG)
check("it is an STL item", row and row.get("media_type") == "stl", row and row.get("media_type"))
res = stl.capture_thumbnail(row)
check("capture_thumbnail returns a CaptureResult", isinstance(res, object_types.CaptureResult)
      and res.type_metadata == {stl.THUMB_RENDERER_KEY: stl.RENDERER_MATPLOTLIB}, repr(res)[:120])
storage.thumb_path_for(SLUG).unlink(missing_ok=True)
check("ensure_thumbnail makes the thumbnail (fallback)", thumbnails.ensure_thumbnail(db.get_by_slug(SLUG)))
check("the row records thumb_renderer=matplotlib",
      (db.get_by_slug(SLUG)["type_metadata"] or {}).get(stl.THUMB_RENDERER_KEY) == stl.RENDERER_MATPLOTLIB,
      db.get_by_slug(SLUG)["type_metadata"])
stl._import_gl = real_import_gl


def _fake_gl_png(vectors, size_px):
    buf = io.BytesIO()
    Image.new("RGB", (size_px, size_px), (90, 110, 70)).save(buf, "PNG")
    return buf.getvalue()


stl._render_gl = _fake_gl_png

storage.thumb_path_for(SLUG).unlink(missing_ok=True)
thumbnails.ensure_thumbnail(db.get_by_slug(SLUG))
check("a later GL render updates thumb_renderer",
      (db.get_by_slug(SLUG)["type_metadata"] or {}).get(stl.THUMB_RENDERER_KEY) == stl._gl_renderer_name(),
      db.get_by_slug(SLUG)["type_metadata"])
stl._render_gl = real_render_gl

# the real GL stack, when it is here (the app image)
try:
    real_import_gl()
    gl_ok, gl_why = True, ""
except Exception as e:
    gl_ok, gl_why = False, f"the GL stack doesn't import here ({e!r}); expected off the app image"
if gl_ok:
    png, used = stl.render_png(BOX, renderer="gl")
    check("real GL render of a box", is_png(png, stl.RENDER_SIZE_PX) and used.startswith("pyrender-"), used)
    img = Image.open(io.BytesIO(png)).convert("RGB")
    bg = tuple(storage.THUMB_BG)
    corner, middle = img.getpixel((2, 2)), img.getpixel((img.width // 2, img.height // 2))
    check("real GL: background is the thumbnail background", max(abs(a - b) for a, b in zip(corner, bg)) <= 3, corner)
    check("real GL: the part is drawn in the middle", max(abs(a - b) for a, b in zip(middle, bg)) > 40, middle)
    png, used = stl.render_png(DENSE, renderer="gl")
    check("real GL render of a mesh over the triangle cap", is_png(png) and used.startswith("pyrender-"), used)
else:
    skip("real GL render", gl_why)

# ---- 2. the triangle cap -------------------------------------------------------------------
check("the triangle cap is unchanged", stl.MAX_PREVIEW_FACES == 20000, stl.MAX_PREVIEW_FACES)
dense = sphere_tris(150, 200)
verts, faces = stl._cluster_decimate(dense, stl.MAX_PREVIEW_FACES)
check("cluster decimation: at most MAX_PREVIEW_FACES", 0 < len(faces) <= stl.MAX_PREVIEW_FACES, len(faces))
check("cluster decimation: indices in range, no degenerate faces",
      faces.max() < len(verts) and np.all((faces[:, 0] != faces[:, 1]) & (faces[:, 1] != faces[:, 2]) & (faces[:, 0] != faces[:, 2])))
check("cluster decimation: keeps the shape (bounds within 5%)",
      np.allclose(verts.min(axis=0), dense.reshape(-1, 3).min(axis=0), atol=2.5)
      and np.allclose(verts.max(axis=0), dense.reshape(-1, 3).max(axis=0), atol=2.5))
edges = np.sort(np.concatenate([faces[:, [0, 1]], faces[:, [1, 2]], faces[:, [2, 0]]]), axis=1)
_, counts = np.unique(edges, axis=0, return_counts=True)
check("cluster decimation: no open edges (no holes in a closed part)", bool(np.all(counts >= 2)), str(np.bincount(counts)[:5].tolist()))
seen = {}
orig_cluster = stl._cluster_decimate
stl._cluster_decimate = lambda v, m: seen.setdefault("n", (len(v), m)) and orig_cluster(v, m)
if gl_ok:
    stl.render_png(DENSE, renderer="gl")
    check("the GL path decimates a mesh over the cap", seen.get("n") == (60000, stl.MAX_PREVIEW_FACES), seen)
else:
    skip("the GL path decimates a mesh over the cap", gl_why)
stl._cluster_decimate = orig_cluster
png, used = stl.render_png(DENSE, renderer="matplotlib")
check("matplotlib still draws a mesh over the cap", is_png(png))

# ---- 3. the object page: 3D viewer config + size cap ---------------------------------------
page = client.get(f"/object/{SLUG}").text
size = BOX.stat().st_size
m = re.search(r'<div class="stl-viewer" id="stl-viewer" data-src="([^"]+)" data-size="(\d*)" data-max="(\d+)" data-autoload="(\d+)"', page)
check("page: the viewer block is there", bool(m), page[page.find("stl"):][:200] if "stl" in page else "no stl markup")
if m:
    check("page: data-src is the file URL", m.group(1) == f"/f/{SLUG}", m.group(1))
    check("page: data-size is the file size", m.group(2) == str(size), (m.group(2), size))
    check("page: data-max is the default cap (64 MB)", int(m.group(3)) == 64 * 1024 * 1024, m.group(3))
    check("page: data-autoload below the cap", 0 < int(m.group(4)) < int(m.group(3)), m.group(4))
check("page: the rendered thumbnail stays inside as the fallback", 'id="preview-img"' in page and "/thumb" in page)
check("page: the visible note element", 'id="stl-viewer-note"' in page)
page2 = client.get(f"/object/{SLUG}")
csp_nonce = re.search(r"'nonce-([^']+)'", page2.headers.get("content-security-policy", ""))
im = re.search(r'<script type="importmap" nonce="([^"]+)">(.*?)</script>', page2.text, re.S)
check("page: a nonce'd import map", bool(im) and bool(csp_nonce) and im.group(1) == csp_nonce.group(1))
if im:
    check("page: import map names three, the loader and the controls",
          all(k in im.group(2) for k in ('"three"', '"three/stl-loader"', '"three/orbit-controls"', "/static/vendor/three/")))
check("page: the viewer module from /static", '<script type="module" src="/static/js/stl-viewer.js' in page2.text)
os.environ["CONSTRUCTICON_STL_VIEWER_MAX_MB"] = "1"
m2 = re.search(r'data-max="(\d+)"', client.get(f"/object/{SLUG}").text)
check("page: CONSTRUCTICON_STL_VIEWER_MAX_MB sets the cap", m2 and int(m2.group(1)) == 1024 * 1024, m2 and m2.group(1))
os.environ["CONSTRUCTICON_STL_VIEWER_MAX_MB"] = "nonsense"
check("a bad cap setting falls back to the default", stl.viewer_max_bytes() == 64 * 1024 * 1024)
os.environ.pop("CONSTRUCTICON_STL_VIEWER_MAX_MB")
r = client.get("/static/js/stl-viewer.js")
check("stl-viewer.js is served", r.status_code == 200)
js = r.text
for phrase, what in (("over the 3D viewer", "says when the file is over the cap"),
                     ("installed on this server", "says when the vendored files are missing"),
                     ("WebGL", "says when there's no WebGL"), ("download the file for the 3D view", "says when the fetch fails"),
                     ("read this STL", "says when the parse fails")):
    check(f"viewer JS {what}", phrase in js)
check("viewer JS: no eval / new Function / inline handlers", not re.search(r"\beval\(|new Function|\bon[a-z]+\s*=", js))
readme = (Path(ROOT) / "web/static/vendor/three/README.txt").read_text(encoding="utf-8")
for name in ("three.module.min.js", "STLLoader.js", "OrbitControls.js", "LICENSE"):
    path = Path(ROOT) / "web/static/vendor/three" / name
    want = re.search(re.escape(name) + r"\s+https?://\S+\s+sha256 ([0-9a-f]{64})", readme)
    got = hashlib.sha256(path.read_bytes()).hexdigest() if path.exists() else None
    check(f"vendored {name} is there and matches its sha256", bool(want) and got == want.group(1), got)
    check(f"vendored {name} is served", client.get(f"/static/vendor/three/{name}").status_code == 200)
# export mode is unchanged: the picture + link, no viewer
ctx = object_types.PreviewContext(item=db.get_by_slug(SLUG), media_url="media/x.stl", thumb_url="media/x.jpg", page_url=None, mode="export")
check("export preview has no viewer", "stl-viewer" not in str(stl.preview(ctx)))

# ---- 4. #751: lamp sizes and "Try anyway" ------------------------------------------------------
css = (Path(ROOT) / "web/static/css/details.css").read_text(encoding="utf-8")
base = re.search(r"\.ocr-panel-header \.ocr-lamp \{([^}]*)\}", css)
failed = re.search(r'\.ocr-panel-header \.ocr-lamp\[data-state="failed"\] \{([^}]*)\}', css)
check("lamp: item-page lamps are 14px", base and "width: 14px" in base.group(1) and "height: 14px" in base.group(1))
check("lamp: grey lamps get a visible ring", base and "var(--border-strong)" in base.group(1))
check("lamp: red (failed) is bigger, with a red halo and a pulse",
      failed and "width: 16px" in failed.group(1) and "226, 75, 74" in failed.group(1) and "animation:" in failed.group(1))
check("lamp: the pulse respects reduced motion", "prefers-reduced-motion" in css and "animation: none" in css)
style = (Path(ROOT) / "web/static/style.css").read_text(encoding="utf-8")
check("lamp: the grid/card lamp stays 8px", re.search(r"\.ocr-lamp \{\s*display: inline-block; width: 8px; height: 8px;", style))
tpl = (Path(ROOT) / "web/templates/object_detail.html").read_text(encoding="utf-8")
check("lamp: the page JS sets data-state on both lamps", "ocrLamp.dataset.state" in tpl and "captionLamp.dataset.state" in tpl)
check("'Run OCR' on a 'not compatible' (grey) item reads 'Try anyway'", "ocrStatus === 'unsupported' ? 'Try anyway'" in tpl)
check("'Run OCR' stays for a not-yet-run item", "'Run OCR'" in tpl)

# ---- 5. the command line renderer ------------------------------------------------------------
from core.manage import render_stl as _render_stl  # noqa: E402


class render_stl:  # noqa: N801 - the CLI's refusals go to stderr; kept out of this test's output
    """main() with stderr captured, so the CLI's own "FAILED: ..." lines (expected here) don't read
    as this test failing to scripts/run_checks.py."""
    @staticmethod
    def main(argv):
        with contextlib.redirect_stderr(io.StringIO()):
            return _render_stl.main(argv)


out = WORK / "out.png"
check("CLI: --renderer matplotlib writes a PNG, exit 0",
      render_stl.main([str(BOX), str(out), "--renderer", "matplotlib"]) == 0 and is_png(out.read_bytes()))
check("CLI: a missing file exits 1", render_stl.main([str(WORK / "nope.stl"), str(out)]) == 1)
check("CLI: a broken STL exits 1", render_stl.main([str(bad), str(out)]) == 1)
out.unlink(missing_ok=True)
rc = render_stl.main([str(BOX), str(out), "--size", "200"])
check("CLI: auto writes a PNG at the asked size", rc == 0 and is_png(out.read_bytes(), 200))
cmp_out = WORK / "cmp.png"
rc = render_stl.main([str(BOX), str(cmp_out), "--compare", "--size", "200"])
if gl_ok:
    check("CLI: --compare writes before|after side by side", rc == 0 and Image.open(cmp_out).size == (400, 224))
    check("CLI: --renderer gl exits 0", render_stl.main([str(BOX), str(out), "--renderer", "gl"]) == 0)
else:
    check("CLI: --compare without GL exits 1 (says why)", rc == 1)
    check("CLI: --renderer gl without GL exits 1 (says why)", render_stl.main([str(BOX), str(out), "--renderer", "gl"]) == 1)
try:
    render_stl.main([str(BOX), str(out), "--size", "5"])
    check("CLI: a silly --size is refused", False)
except SystemExit as e:
    check("CLI: a silly --size is refused", e.code == 2)
if sys.platform.startswith("linux"):  # the real `python3 -m` path (Windows has no libcairo for cairosvg)
    p = subprocess.run([sys.executable, "-m", "core.manage.render_stl", str(BOX), str(out), "--renderer", "matplotlib"],
                       cwd=ROOT, capture_output=True, text=True)
    check("CLI: python3 -m core.manage.render_stl runs", p.returncode == 0 and "wrote" in p.stdout, p.stderr[-300:])
else:
    skip("CLI as `python3 -m`", "not Linux (cairosvg needs libcairo, which this machine lacks); main() was exercised in-process")

# ---- 6. the backfill: regenerate_thumbnails.py --type stl -----------------------------------
import importlib.util  # noqa: E402
_spec = importlib.util.spec_from_file_location("regen", os.path.join(ROOT, "scripts", "regenerate_thumbnails.py"))
regen = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(regen)


def run_regen(*argv):
    buf = io.StringIO()
    old_argv = sys.argv
    sys.argv = ["regenerate_thumbnails.py", *argv]
    try:
        with contextlib.redirect_stdout(buf):
            rc = regen.main()
    finally:
        sys.argv = old_argv
    return rc, buf.getvalue()


thumb = storage.thumb_path_for(SLUG)
before = thumb.read_bytes() if thumb.exists() else None
rc, text = run_regen("--type", "stl", "--dry-run")
check("regen --type stl --dry-run lists the STL and writes nothing",
      rc == 0 and f"would regenerate: {SLUG}" in text and (thumb.read_bytes() if thumb.exists() else None) == before, text[-300:])
stl._import_gl = broken_import  # the fallback draws it; the row says so
rc, text = run_regen("--type", "stl")
check("regen --type stl re-renders and reports the renderer",
      rc == 0 and f"regenerated: {SLUG}" in text and "renderer=matplotlib" in text, text[-300:])
stl._import_gl = real_import_gl
orig_capture = object_types.get_object_type("stl").capture_fn
saved = thumb.read_bytes()
_stl_spec = object_types.get_object_type("stl")
object.__setattr__(_stl_spec, "capture_fn", lambda row: None)  # a render that fails (the spec may be frozen)
rc, text = run_regen("--type", "stl")
check("regen: a failed render keeps the old thumbnail and exits 1",
      rc == 1 and "FAILED (old thumbnail kept)" in text and thumb.exists() and thumb.read_bytes() == saved, text[-300:])
object.__setattr__(_stl_spec, "capture_fn", orig_capture)
rc, text = run_regen("--type", "nosuchtype")
check("regen: an unknown type is refused, exit 1", rc == 1 and "no object type" in text, text[-200:])

print()
if FAILS:
    print(f"FAILED: {len(FAILS)} check(s): " + "; ".join(FAILS))
    sys.exit(1)
print("ALL PASS")
