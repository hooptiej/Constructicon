#!/usr/bin/env python3
"""#852: 16-bit greyscale images get a thumbnail, and a failed thumbnail is loud.

Throwaway DB + storage (scripts/_testenv.py), the real app through TestClient, no server:

    python scripts/test_greyscale16_thumbs_852.py

Covers:
  * real 16-bit fixtures built here (no files in the repo): a 16-bit PNG (Pillow mode I;16), a 16-bit
    TIFF (I;16), a hand-built BIG-ENDIAN 16-bit TIFF (I;16B), a 32-bit integer TIFF (I) and a float TIFF
    (F); the raw Pillow .thumbnail() on I;16 still fails (the bug), storage.to_8bit_for_thumbnail brings
    each to 8-bit "L" with a stretch (a dim frame is not near-black; a flat black frame stays black)
  * storage.save_thumbnail_from_bytes writes a real JPEG thumbnail for each of them
  * a row whose file is a 16-bit PNG gets its thumbnail through thumbnails.ensure_thumbnail, no marker
  * never silent: bytes that are not an image raise storage.ThumbnailError (what was attempted and why);
    ensure_thumbnail logs a warning with the slug, returns False and leaves type_metadata.thumb_error on
    the item; a later success clears it
  * the web door: POST /api/image/<slug>/thumbnail/refresh on a failing item answers 422 with the reason
    (code thumbnail_failed); the object page shows #thumb-error-note, and not when there is no failure
  * scripts/regenerate_thumbnails.py reports a failed row instead of a bare print

Exits 1 if any check fails.
"""

import io
import logging
import os
import struct
import sys
import types

import _testenv  # noqa: E402  (scripts/_testenv.py: temp DB + storage + exports, refuses otherwise)
TMP = _testenv.isolate("greyscale16-852-")
ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")
sys.path.insert(0, ROOT)

try:
    import cairosvg  # noqa: F401
except Exception:  # no libcairo here (e.g. Windows): object types import it
    sys.modules["cairosvg"] = types.ModuleType("cairosvg")

from PIL import Image  # noqa: E402

from core import actor, besteffort, db, ingest, parse_limits, paths, storage, thumbnails  # noqa: E402
_testenv.assert_isolated()

FAILS = []


def check(name, cond, extra=""):
    print(("PASS " if cond else "FAIL ") + name + (f"  [{extra}]" if (extra and not cond) else ""))
    if not cond:
        FAILS.append(name)


try:
    from web import app as webapp  # noqa: E402
except Exception as e:  # web stack not importable here: the box run covers it
    webapp = None
    print(f"SKIP web checks (web import failed: {e!r})")

client = None
if webapp is not None:
    client = _testenv.client(webapp.app, raise_server_exceptions=False)
    with client:  # real startup (init_db + migrations)
        pass
else:
    db.init_db()

ingest.run_in_thread = lambda fn, *a: None
parse_limits.reset()
ctx = actor.acting_as(actor.ACTOR_UI)
ctx.__enter__()

W, H = 640, 480


def ramp(lo, hi):
    """W*H 16-bit values: a left-to-right ramp from lo to hi."""
    return [lo + (hi - lo) * x // (W - 1) for _y in range(H) for x in range(W)]


def mode_image(mode, values):
    img = Image.new(mode, (W, H))
    img.putdata(values)
    return img


def encode(img, fmt):
    buf = io.BytesIO()
    img.save(buf, fmt)
    return buf.getvalue()


def big_endian_tiff16(values):
    """A minimal baseline TIFF, BIG-endian ('MM'), one 16-bit greyscale strip: Pillow reads it as I;16B
    (it cannot write that mode itself, so the bytes are built by hand)."""
    entries = [
        (256, 4, 1, W), (257, 4, 1, H), (258, 3, 1, 16), (259, 3, 1, 1), (262, 3, 1, 1),
        (273, 4, 1, 0), (277, 3, 1, 1), (278, 4, 1, H), (279, 4, 1, W * H * 2),
    ]
    data_offset = 8 + 2 + len(entries) * 12 + 4
    out = bytearray(b"MM\x00\x2a" + struct.pack(">I", 8) + struct.pack(">H", len(entries)))
    for tag, typ, count, val in entries:
        if tag == 273:
            val = data_offset
        out += struct.pack(">HHI", tag, typ, count)
        out += struct.pack(">HH", val, 0) if typ == 3 else struct.pack(">I", val)
    out += struct.pack(">I", 0)
    out += struct.pack(f">{W * H}H", *values)
    return bytes(out)


DIM = ramp(1000, 3000)           # a dim 16-bit frame: only 3% of the range (astronomy / microscope style)
FULL = ramp(0, 65535)
BLACK = [0] * (W * H)

FIXTURES = {
    "16-bit PNG (I;16)": (encode(mode_image("I;16", FULL), "PNG"), "I;16"),
    "16-bit TIFF (I;16)": (encode(mode_image("I;16", FULL), "TIFF"), "I;16"),
    "big-endian 16-bit TIFF (I;16B)": (big_endian_tiff16(FULL), "I;16B"),
    "32-bit integer TIFF (I)": (encode(mode_image("I", FULL), "TIFF"), "I"),
    "float TIFF (F)": (encode(mode_image("F", [v / 65535.0 for v in FULL]), "TIFF"), "F"),
}
DIM_PNG = encode(mode_image("I;16", DIM), "PNG")
BLACK_PNG = encode(mode_image("I;16", BLACK), "PNG")

# ============================================================================================
# 1. the bug, and the conversion
# ============================================================================================
raw = Image.open(io.BytesIO(FIXTURES["16-bit PNG (I;16)"][0]))
check("fixture: the 16-bit PNG really opens as I;16", raw.mode == "I;16", raw.mode)
try:
    raw.thumbnail((100, 100))
    raw_fails = False
except ValueError:
    raw_fails = True
check("the bug: Pillow's own .thumbnail() refuses an I;16 image (ValueError)", raw_fails)

for name, (data, mode) in FIXTURES.items():
    img = Image.open(io.BytesIO(data))
    check(f"fixture: {name} opens as Pillow mode {mode}", img.mode == mode, img.mode)
    out = storage.to_8bit_for_thumbnail(img)
    lo, hi = out.getextrema()
    check(f"to_8bit_for_thumbnail: {name} -> 8-bit L, full ramp stretched to ~0..255",
          out.mode == "L" and lo <= 2 and hi >= 253, (out.mode, lo, hi))
    out.thumbnail((100, 100))
    check(f"... and the converted image thumbnails ({name})", out.size[0] <= 100 and out.size[1] <= 100, out.size)

rgb = Image.new("RGB", (10, 10), "red")
check("to_8bit_for_thumbnail leaves an ordinary RGB image alone (same object)", storage.to_8bit_for_thumbnail(rgb) is rgb)
dim = storage.to_8bit_for_thumbnail(Image.open(io.BytesIO(DIM_PNG)))
dlo, dhi = dim.getextrema()
check("tone mapping: a dim frame (1000..3000 of 65535) is stretched to the full 8-bit range, not near-black",
      dlo <= 2 and dhi >= 253, (dlo, dhi))
blk = storage.to_8bit_for_thumbnail(Image.open(io.BytesIO(BLACK_PNG)))
check("tone mapping: a flat black frame stays black", blk.getextrema() == (0, 0), blk.getextrema())

# ============================================================================================
# 2. save_thumbnail_from_bytes writes a real thumbnail
# ============================================================================================
for i, (name, (data, mode)) in enumerate(FIXTURES.items()):
    slug = f"g16-{i}"
    storage.save_thumbnail_from_bytes(slug, data)
    tp = storage.thumb_path_for(slug)
    ok = tp.exists()
    info = None
    if ok:
        with Image.open(tp) as t:
            info = (t.format, t.mode, t.size, t.convert("L").getextrema())
    check(f"thumbnail comes out for {name}: a JPEG within {storage.THUMB_MAX_DIM}px, with tonal range",
          ok and info[0] == "JPEG" and max(info[2]) <= storage.THUMB_MAX_DIM and info[3][1] - info[3][0] > 150, info)

storage.save_thumbnail_from_bytes("g16-dim", DIM_PNG)
with Image.open(storage.thumb_path_for("g16-dim")) as t:
    drange = t.convert("L").getextrema()
check("the dim frame's thumbnail shows its detail (not a near-black square)", drange[1] - drange[0] > 150, drange)

# ============================================================================================
# 3. through ensure_thumbnail, on a real row
# ============================================================================================
def mkfile(slug, data, ext, media_type="image"):
    sf = f"{slug}{ext}"
    (paths.storage_dir() / sf).write_bytes(data)
    db.insert_upload(slug, sf, sf, "tester", media_type=media_type, file_size=len(data), ocr_status="pending")
    return db.get_by_slug(slug)


class Capture(logging.Handler):
    def __init__(self):
        super().__init__(logging.WARNING)
        self.lines = []

    def emit(self, record):
        self.lines.append(record.getMessage())


cap = Capture()
besteffort.reset()  # the warning is rate limited per site; start a fresh window
logging.getLogger("constructicon.thumbnails").addHandler(cap)

row = mkfile("grey16png", FIXTURES["16-bit PNG (I;16)"][0], ".png")
ok = thumbnails.ensure_thumbnail(row)
check("ensure_thumbnail: a 16-bit PNG item gets its thumbnail", ok is True and storage.thumb_path_for("grey16png").exists())
check("... with no failure note on the item", not (db.get_by_slug("grey16png").get("type_metadata") or {}).get("thumb_error"))
row = mkfile("grey16tif", FIXTURES["16-bit TIFF (I;16)"][0], ".tif")
check("ensure_thumbnail: a 16-bit TIFF item gets its thumbnail",
      thumbnails.ensure_thumbnail(row) is True and storage.thumb_path_for("grey16tif").exists())

# ============================================================================================
# 4. never silent
# ============================================================================================
try:
    storage.save_thumbnail_from_bytes("junk-direct", b"this is not an image at all")
    raised, msg = False, ""
except storage.ThumbnailError as e:
    raised, msg = True, str(e)
check("save_thumbnail_from_bytes: bytes that are not an image raise ThumbnailError (no bare print)", raised)
check("... and the message says what was attempted and why", "couldn't make a thumbnail" in msg and "UnidentifiedImageError" in msg, msg)
check("... and no thumbnail file was left behind", not storage.thumb_path_for("junk-direct").exists())

bad = mkfile("badimg", b"\x89PNG\r\n\x1a\nnot really a png", ".png")
ok = thumbnails.ensure_thumbnail(bad)
note = (db.get_by_slug("badimg").get("type_metadata") or {}).get("thumb_error")
check("ensure_thumbnail: a file Pillow cannot read returns False", ok is False)
check("... and leaves the reason on the item (type_metadata.thumb_error)", bool(note) and "couldn't make a thumbnail" in note, note)
check("... and logs a warning naming the slug", any("badimg" in line for line in cap.lines), cap.lines)

# a later success clears the note: put a readable image in place of the broken file and retry
(paths.storage_dir() / "badimg.png").write_bytes(FIXTURES["16-bit PNG (I;16)"][0])
ok = thumbnails.ensure_thumbnail(db.get_by_slug("badimg"))
check("ensure_thumbnail: once the file reads, the thumbnail is made", ok is True and storage.thumb_path_for("badimg").exists())
check("... and the old failure note is cleared", not (db.get_by_slug("badimg").get("type_metadata") or {}).get("thumb_error"),
      (db.get_by_slug("badimg").get("type_metadata") or {}))

# ============================================================================================
# 5. the web door
# ============================================================================================
if client is not None:
    bad2 = mkfile("badimg2", b"GIF89a-not-really", ".gif")
    thumbnails.ensure_thumbnail(bad2)
    r = client.post("/api/image/badimg2/thumbnail/refresh")
    body = r.json() if r.headers.get("content-type", "").startswith("application/json") else {}
    err = body.get("error") or {}
    check("refresh thumbnail on a failing item: 422 with code thumbnail_failed and the reason",
          r.status_code == 422 and err.get("code") == "thumbnail_failed" and "couldn't make a thumbnail" in err.get("message", ""),
          (r.status_code, r.text[:300]))
    page = client.get("/object/badimg2")
    check("object page: shows #thumb-error-note with the reason", page.status_code == 200 and 'id="thumb-error-note"' in page.text
          and "No thumbnail could be made" in page.text, page.status_code)
    page = client.get("/object/grey16png")
    check("object page: no note on an item whose thumbnail was made", page.status_code == 200 and 'id="thumb-error-note"' not in page.text,
          page.status_code)

# ============================================================================================
# 6. the maintenance script says so
# ============================================================================================
src = open(os.path.join(ROOT, "scripts", "regenerate_thumbnails.py"), encoding="utf-8").read()
check("regenerate_thumbnails.py catches ThumbnailError, prints FAILED and exits non-zero",
      "except storage.ThumbnailError" in src and 'print(f"FAILED:' in src and "sys.exit(1)" in src)

print()
if FAILS:
    print(f"FAILED: {len(FAILS)} check(s): {FAILS}")
    sys.exit(1)
print("ALL PASS")
