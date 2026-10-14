#!/usr/bin/env python3
"""Self-contained check for the parser limits (#644): core/parse_limits.py.

Throwaway storage (scripts/_testenv.py), no server, no network. Every limit is hit for real in a
parser child process (Linux only: on a platform with no forkserver the child checks are skipped
and say so):

    python scripts/test_parse_limits_644.py

- an image over the pixel cap is refused when its header is read, before any pixel decodes
- a step that runs too long is killed (time), one that allocates too much fails (memory), one that
  dies is reported (crash); the app process survives every one of them
- a limit hit is remembered per file and step, raised again at once, and forget() clears it
- a PSD's declared canvas is checked against the pixel cap (psd-tools decodes outside Pillow)
- ensure_thumbnail() on a decompression-bomb PNG returns False, remembers the hit and writes
  no thumbnail
- the registry refuses a file-reading hook that can't be sent to a child (a lambda)

Exits 1 if any check fails.
"""
import os
import sys
import time
import types

import _testenv  # noqa: E402  (scripts/_testenv.py: temp DB + storage + exports, refuses otherwise)
TMP = _testenv.isolate("parselimits-")
ROOT = os.path.abspath(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
sys.path.insert(0, ROOT)
sys.modules.setdefault("cairosvg", types.ModuleType("cairosvg"))  # no libcairo needed here

# Small budgets so each limit is hit quickly (settings are read when used).
os.environ["CONSTRUCTICON_PARSE_TIMEOUT_SECONDS"] = "3"
os.environ["CONSTRUCTICON_PARSE_MEMORY_MB"] = "256"
os.environ["CONSTRUCTICON_MAX_IMAGE_MEGAPIXELS"] = "20"

from PIL import Image  # noqa: E402

from core import object_types, parse_limits, storage, thumbnails  # noqa: E402
_testenv.assert_isolated()  # now as core actually resolved the paths

FAILS = []


def check(name, cond, extra=""):
    print(("PASS " if cond else "FAIL ") + name + (f"  [{extra}]" if (extra and not cond) else ""))
    if not cond:
        FAILS.append(name)


def limit_kind(fn, *args, **kw):
    """The LimitExceeded kind fn raised through parse_limits.call, else what happened."""
    t0 = time.time()
    try:
        out = parse_limits.call(fn, *args, **kw)
        return ("returned", repr(out)[:60]), time.time() - t0
    except parse_limits.LimitExceeded as e:
        return ("limit", e.kind, e.cached), time.time() - t0
    except Exception as e:  # noqa: BLE001 - reported as the check's result
        return ("raised", type(e).__name__), time.time() - t0


# --- Pillow's pixel cap (in this process, every platform) ------------------------------------
bomb = os.path.join(TMP, "bomb.png")
Image.new("1", (6000, 6000)).save(bomb)  # 36 MP, a few KB on disk; cap is 20 MP
check("the pixel cap is the setting", Image.MAX_IMAGE_PIXELS == 20_000_000, str(Image.MAX_IMAGE_PIXELS))
try:
    with Image.open(bomb) as im:
        im.load()
    refused = None
except (Image.DecompressionBombError, Image.DecompressionBombWarning) as e:
    refused = e
check("an image over the cap is refused (just over, not only at 2x)", refused is not None)

small = os.path.join(TMP, "small.png")
Image.new("RGB", (400, 300), "green").save(small)
with Image.open(small) as im:
    im.load()
    check("an image under the cap still opens", im.size == (400, 300))

try:
    parse_limits.check_pixels(6000, 6000)
    psd_refused = False
except Image.DecompressionBombError:
    psd_refused = True
check("check_pixels refuses a declared canvas over the cap (PSD)", psd_refused)
try:
    parse_limits.check_pixels(4000, 3000)
    psd_ok = True
except Image.DecompressionBombError:
    psd_ok = False
check("check_pixels lets a 12 MP canvas through", psd_ok)

# --- The registry contract -------------------------------------------------------------------
spec = object_types.get_object_type("image")
check("every registered type's file hooks are module-level functions",
      all(object_types._importable_by_name(getattr(s, h))
          for s in object_types.OBJECT_TYPES.values()
          for h in object_types.HOOKS_RUN_IN_CHILD if getattr(s, h) is not None))
check("a lambda isn't importable by name", not object_types._importable_by_name(lambda p: None))

# --- Limits in a child process ---------------------------------------------------------------
if not parse_limits.sandbox_available():
    print("SKIP child-process checks: no forkserver/resource here (Windows, or CONSTRUCTICON_PARSE_SANDBOX=off)")
else:
    parse_limits.reset()
    (res, secs) = limit_kind(time.sleep, 30, what="slow")
    check("a step past the time limit is killed", res == ("limit", "time", False), str(res))
    check("...within the timeout plus slack", secs < 3 + 8, f"{secs:.1f}s")

    (res, _) = limit_kind(bytearray, 2_000_000_000, what="hog")
    check("a 2 GB allocation in a 256 MB budget is a memory limit", res == ("limit", "memory", False), str(res))

    (res, _) = limit_kind(os.abort, what="boom")
    check("a parser that dies is a crash, and the app survives", res[:2] == ("limit", "crash"), str(res))

    (res, _) = limit_kind(len, "abc", what="fine")
    check("an ordinary step returns its result from the child", res == ("returned", "3"), str(res))

    (res, _) = limit_kind(int, "nope", what="bad")
    check("an ordinary exception is re-raised as itself", res == ("raised", "ValueError"), str(res))

    # Remembering, per (key, step).
    parse_limits.reset()
    limit_kind(time.sleep, 30, what="thumbnail", key="hostile.png")
    (res, secs) = limit_kind(time.sleep, 30, what="thumbnail", key="hostile.png")
    check("a remembered hit is raised again at once", res == ("limit", "time", True) and secs < 1, f"{res} {secs:.2f}s")
    (res, _) = limit_kind(len, "x", what="properties", key="hostile.png")
    check("...only for that step (another step still runs)", res == ("returned", "1"), str(res))
    fails = parse_limits.failures_for("hostile.png")
    check("failures_for lists it for the object page",
          len(fails) == 1 and fails[0]["what"] == "thumbnail" and fails[0]["kind"] == "time", str(fails))
    parse_limits.forget("hostile.png")
    check("forget() clears it", parse_limits.failures_for("hostile.png") == [])

    # End to end: a decompression-bomb PNG's thumbnail.
    parse_limits.reset()
    stored = "bombslug.png"
    with open(bomb, "rb") as src, open(storage.path_for(stored), "wb") as dst:
        dst.write(src.read())
    row = {"slug": "bombslug", "media_type": "image", "stored_filename": stored}
    t0 = time.time()
    made = thumbnails.ensure_thumbnail(row)
    check("ensure_thumbnail on a pixel bomb returns False", made is False, repr(made))
    check("...and writes no thumbnail", not storage.thumb_path_for("bombslug").exists())
    check("...and remembers it as a pixel limit",
          [f["kind"] for f in parse_limits.failures_for(stored)] == ["pixels"], str(parse_limits.failures_for(stored)))
    t0 = time.time()
    thumbnails.ensure_thumbnail(row)
    check("a second request doesn't decode it again", time.time() - t0 < 0.5, f"{time.time() - t0:.2f}s")

    ok_stored = "okslug.png"
    with open(small, "rb") as src, open(storage.path_for(ok_stored), "wb") as dst:
        dst.write(src.read())
    made = thumbnails.ensure_thumbnail({"slug": "okslug", "media_type": "image", "stored_filename": ok_stored})
    check("a normal image still gets its thumbnail (written by the child)",
          made is True and storage.thumb_path_for("okslug").exists(), repr(made))

print("\n%s" % ("ALL PASS" if not FAILS else "FAILED: " + ", ".join(FAILS)))
sys.exit(1 if FAILS else 0)
