#!/usr/bin/env python3
"""No stray control bytes in the source (#706).

A NUL (0x00) and a 0x11 sat in web/static/css/details.css where the CSS escape `"\\2192\\00a0"` belonged,
so the revision-stack separator rendered as garbage and some tools treated the stylesheet as binary.
This fails on any C0 control byte (below 0x20) other than tab, LF and CR in the text files under
web/, core/, mcp_server/ and scripts/ (.css .js .html .py .md .json .txt). Escapes written as text
(`"\\x00"`, `\\00a0`) are fine; only the raw bytes count.

    python scripts/check_text_bytes.py          # the checked folders (the suite runs this)
    python scripts/check_text_bytes.py --all    # every tracked text-like file in the repo, report only

Files come from `git ls-files` when the checkout is a git repo, else a walk of the folders (a clean
`git archive` has no .git). Prints one line per bad byte (file:line:col and the byte), then FAILED or OK.
"""

import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
FOLDERS = ("web", "core", "mcp_server", "scripts")
EXTENSIONS = frozenset({".css", ".js", ".html", ".py", ".md", ".json", ".txt"})
# --all: also the other text-like files a repo-wide scan should read
ALL_EXTENSIONS = EXTENSIONS | frozenset({".yml", ".yaml", ".sh", ".ps1", ".toml", ".cfg", ".ini", ".example",
                                         ".svg", ".xml", ".csv", ".sql", ".j2", ".jinja", ".lua", ".c", ".h",
                                         ".gitignore", ".dockerignore", ".gitattributes"})
ALL_NAMES = frozenset({"Dockerfile", "README", "LICENSE", "CHANGELOG"})
ALLOWED = frozenset({0x09, 0x0A, 0x0D})
SKIP_DIRS = frozenset({".git", "__pycache__", "node_modules", "worktrees", ".claude"})


def bad_bytes(data):
    """[(line, col, byte)] for every C0 control byte other than tab/LF/CR in `data` (1-based)."""
    out = []
    line, col = 1, 0
    for b in data:
        if b == 0x0A:
            line, col = line + 1, 0
            continue
        col += 1
        if b < 0x20 and b not in ALLOWED:
            out.append((line, col, b))
    return out


def _wanted(rel, every):
    p = Path(rel)
    if every:
        return p.suffix.lower() in ALL_EXTENSIONS or p.name in ALL_EXTENSIONS or p.name in ALL_NAMES
    return p.parts[0] in FOLDERS and p.suffix.lower() in EXTENSIONS


def tracked_files(root=ROOT, every=False):
    """Repo-relative paths to scan: git's tracked files, else a walk (skipping caches and worktrees)."""
    try:
        res = subprocess.run(["git", "-C", str(root), "ls-files", "-z"], capture_output=True, timeout=60)
        names = [n for n in res.stdout.decode("utf-8", "replace").split("\0") if n] if res.returncode == 0 else []
    except (OSError, subprocess.SubprocessError):  # silent-ok: no git (a clean archive); walk the folders below
        names = []
    if not names:
        tops = [root] if every else [root / f for f in FOLDERS]
        for top in tops:
            for dirpath, dirnames, filenames in os.walk(top):
                dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS]
                names += [os.path.relpath(os.path.join(dirpath, f), root).replace(os.sep, "/") for f in filenames]
    return sorted(n for n in set(names) if _wanted(n, every))


def scan(root=ROOT, every=False):
    """{relative path: [(line, col, byte)]} for every file with a bad byte."""
    hits = {}
    for rel in tracked_files(root, every):
        try:
            data = (Path(root) / rel).read_bytes()
        except OSError as e:
            print(f"WARN cannot read {rel}: {e}")
            continue
        found = bad_bytes(data)
        if found:
            hits[rel] = found
    return hits


def main(argv):
    every = "--all" in argv
    files = tracked_files(ROOT, every)
    hits = scan(ROOT, every)
    for rel, found in hits.items():
        for line, col, b in found[:20]:
            print(f"FAIL {rel}:{line}:{col}: control byte 0x{b:02x}")
        if len(found) > 20:
            print(f"FAIL {rel}: ... {len(found) - 20} more")
    if hits:
        print(f"FAILED: {', '.join(hits)}")
        return 1
    print(f"OK: no control bytes in {len(files)} text files" + (" (whole repo)" if every else ""))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
