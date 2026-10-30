"""Render an STL file to a PNG from the command line (#742), for before/after comparisons.

Run it inside the app image (it needs the image's GL libraries for the new renderer):

    sudo docker exec <web container> python3 -m core.manage.render_stl <in.stl> <out.png>
        the thumbnail renderer as the app runs it (GL, falling back to matplotlib, logged)
    ... --renderer gl          GL only: fails (non-zero, with the reason) instead of falling back
    ... --renderer matplotlib  the pre-#742 renderer (the "before" picture)
    ... --compare              one PNG: matplotlib (before) on the left, GL (after) on the right
    ... --size 500             the render's edge in pixels (default: the thumbnail render size)

It reads only the given file and writes only <out.png>; it never touches the DB, storage or any
thumbnail. It runs in this process, not in a bounded parser child (core/parse_limits.py): point it
at files you chose. Exit 0 on success (prints which renderer drew it and how long it took), 1 when
the file can't be read or drawn (the reason is printed), 2 for bad arguments.
"""

import argparse
import logging
import sys
import time
from io import BytesIO
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))  # the repo root, if run as a file


def _render(path, renderer, size):
    from core.object_types import stl
    started = time.monotonic()
    try:
        png, used = stl.render_png(path, renderer=renderer, size_px=size)
    except Exception as e:  # --renderer gl: the GL failure, said plainly
        print(f"FAILED: the {renderer} renderer could not draw {path}: {e!r}", file=sys.stderr)
        return None, None, None
    if not png:
        print(f"FAILED: {path} could not be read as an STL (or has no drawable triangles); "
              "see the message above", file=sys.stderr)
        return None, None, None
    return png, used, time.monotonic() - started


def main(argv=None):
    parser = argparse.ArgumentParser(description="Render an STL to a PNG (#742 before/after tool).")
    parser.add_argument("input", type=Path, help="the .stl file")
    parser.add_argument("output", type=Path, help="the .png to write")
    parser.add_argument("--renderer", choices=("auto", "gl", "matplotlib"), default="auto")
    parser.add_argument("--compare", action="store_true",
                        help="write matplotlib (left) and GL (right) side by side")
    parser.add_argument("--size", type=int, default=None, help="render edge in pixels (default 500)")
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(name)s: %(message)s")

    from core.object_types import stl
    size = args.size or stl.RENDER_SIZE_PX
    if not 32 <= size <= 4096:
        parser.error("--size must be between 32 and 4096")
    if not args.input.is_file():
        print(f"FAILED: no such file: {args.input}", file=sys.stderr)
        return 1

    if not args.compare:
        png, used, secs = _render(args.input, args.renderer, size)
        if png is None:
            return 1
        args.output.write_bytes(png)
        note = " (GL failed, see the warning above)" if args.renderer == "auto" and used == stl.RENDERER_MATPLOTLIB else ""
        print(f"wrote {args.output} ({len(png):,} bytes) with {used} in {secs:.2f}s{note}")
        return 0

    from PIL import Image, ImageDraw
    before, used_b, secs_b = _render(args.input, "matplotlib", size)
    after, used_a, secs_a = _render(args.input, "gl", size)
    if before is None or after is None:
        return 1
    left, right = Image.open(BytesIO(before)).convert("RGB"), Image.open(BytesIO(after)).convert("RGB")
    canvas = Image.new("RGB", (left.width + right.width, max(left.height, right.height) + 24), (20, 23, 15))
    canvas.paste(left, (0, 24))
    canvas.paste(right, (left.width, 24))
    draw = ImageDraw.Draw(canvas)
    draw.text((8, 6), f"before: {used_b} ({secs_b:.2f}s)", fill=(220, 220, 200))
    draw.text((left.width + 8, 6), f"after: {used_a} ({secs_a:.2f}s)", fill=(220, 220, 200))
    canvas.save(args.output, format="PNG")
    print(f"wrote {args.output}: before {used_b} {secs_b:.2f}s | after {used_a} {secs_a:.2f}s")
    return 0


if __name__ == "__main__":
    sys.exit(main())
