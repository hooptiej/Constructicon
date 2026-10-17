#!/usr/bin/env python3
"""The #706 byte check's own test, and the details.css fix. No DB, no server.

    python scripts/test_text_bytes_706.py

- `check_text_bytes.bad_bytes` flags NUL, 0x11, 0x1b, 0x7f-is-fine, and allows tab/LF/CR, with the
  right line and column;
- `scan()` over a throwaway tree finds a planted NUL in web/, ignores the same byte in a folder or an
  extension the check doesn't cover, and accepts escapes written as text;
- web/static/css/details.css's revision separator is the CSS escape `"\\2192\\00a0"` (an arrow and a
  no-break space) and the file has no control bytes left.
Exits 1 if any check fails.
"""
import os
import sys
import tempfile
from pathlib import Path

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import check_text_bytes as ctb  # noqa: E402

ROOT = Path(HERE).parent
FAILS = []


def check(name, cond, extra=""):
    print(("PASS " if cond else "FAIL ") + name + (f"  [{extra}]" if (extra and not cond) else ""))
    if not cond:
        FAILS.append(name)


check("clean text: nothing", ctb.bad_bytes(b"a\tb\r\nc\n") == [])
check("NUL and 0x11 found with line/col", ctb.bad_bytes(b"ok\nx\x11y\x00\n") == [(2, 2, 0x11), (2, 4, 0x00)],
      ctb.bad_bytes(b"ok\nx\x11y\x00\n"))
check("ESC and form feed are flagged too", [b for _, _, b in ctb.bad_bytes(b"\x1b[0m\x0c")] == [0x1b, 0x0c])
check("DEL (0x7f) and UTF-8 bytes are not C0", ctb.bad_bytes("\x7f→ ©".encode("utf-8")) == [])
check("escapes written as text are fine", ctb.bad_bytes(b'content: "\\2192\\00a0"; s = "\\x00"') == [])

with tempfile.TemporaryDirectory(prefix="bytes706-") as tmp:
    t = Path(tmp)
    (t / "web" / "static").mkdir(parents=True)
    (t / "core").mkdir()
    (t / "docs").mkdir()
    (t / "web" / "static" / "bad.css").write_bytes(b'a::before { content: "\x1192\x00a0"; }\n')
    (t / "web" / "static" / "good.css").write_bytes(b'a::before { content: "\\2192\\00a0"; }\n')
    (t / "core" / "blob.png").write_bytes(b"\x89PNG\r\n\x1a\n\x00\x00")
    (t / "docs" / "notes.md").write_bytes(b"not checked \x00\n")
    hits = ctb.scan(t)
    check("scan: the planted bytes in web/ are found", list(hits) == ["web/static/bad.css"], hits)
    check("scan: both bad bytes reported", [b for _, _, b in hits.get("web/static/bad.css", [])] == [0x11, 0x00])
    check("scan: binary extensions and other folders are not read", "core/blob.png" not in hits and "docs/notes.md" not in hits)
    check("scan --all: reaches other folders", "docs/notes.md" in ctb.scan(t, every=True))

css = (ROOT / "web" / "static" / "css" / "details.css").read_bytes()
i = css.find(b".rev-stack li + li::before")
rule = css[i:css.find(b"}", i) + 1]
check("details.css: the separator is the arrow + nbsp escape", b'content: "\\2192\\00a0";' in rule, rule)
check("details.css: no control bytes left", ctb.bad_bytes(css) == [], ctb.bad_bytes(css)[:5])
check("the repo's checked folders are clean", ctb.scan(ROOT) == {}, list(ctb.scan(ROOT))[:5])

if FAILS:
    print(f"FAILED: {', '.join(FAILS)}")
    sys.exit(1)
print("ALL PASS")
