"""Every retired script under scripts/archive/ refuses to run (#633).

Moving a spent one-shot into scripts/archive/ does not make it inert: some of them still find
`core` from there and can write (cleanup_delete_orphans.py --execute deletes rows). So each
one's FIRST statement must be the guard

    raise SystemExit("Retired script: ran on prod around <date> (#N). Kept for reference only; it will not run. See CLAUDE.md, Scripts.")

before any import or argument parsing, so nothing else in the file ever executes.

This check, for every .py under scripts/archive/ (subfolders included):
  * parses it (AST, nothing imported) and requires that guard as the module's first statement;
  * then runs it (`python -I`, from a temp directory, with no arguments) and requires that it
    exits with status 1, prints the guard's message and nothing else. It only runs a file whose
    guard the AST step found, so an unguarded script is never executed.
It also checks that scripts/run_checks.py does not collect anything under scripts/archive/.

No server, no database, no network.  Usage:  python scripts/check_archive_guard.py
"""
import ast
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
ARCHIVE = ROOT / "scripts" / "archive"
PREFIX = "Retired script: "
SUFFIX = " Kept for reference only; it will not run. See CLAUDE.md, Scripts."

FAILS = []


def check(name, cond, extra=""):
    print(("PASS " if cond else "FAIL ") + name + (f"  [{extra}]" if (extra and not cond) else ""))
    if not cond:
        FAILS.append(name)


def guard_message(path):
    """The guard's message if the module's first statement is `raise SystemExit("<str>")`, else None."""
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    if not tree.body:
        return None
    first = tree.body[0]
    if not (isinstance(first, ast.Raise) and isinstance(first.exc, ast.Call)
            and isinstance(first.exc.func, ast.Name) and first.exc.func.id == "SystemExit"
            and len(first.exc.args) == 1 and not first.exc.keywords
            and isinstance(first.exc.args[0], ast.Constant) and isinstance(first.exc.args[0].value, str)):
        return None
    return first.exc.args[0].value


files = sorted(ARCHIVE.rglob("*.py"))
check("scripts/archive/ holds scripts", bool(files), str(ARCHIVE))

with tempfile.TemporaryDirectory(prefix="archive-guard-") as tmp:
    for path in files:
        rel = path.relative_to(ROOT).as_posix()
        msg = guard_message(path)
        check(f"{rel}: first statement is the guard", msg is not None)
        if msg is None:
            continue
        check(f"{rel}: message says when and which issue",
              msg.startswith(PREFIX) and msg.endswith(SUFFIX) and "(#" in msg, msg)
        proc = subprocess.run([sys.executable, "-I", str(path)], cwd=tmp, capture_output=True,
                              text=True, timeout=60)
        check(f"{rel}: refuses to run (exit 1, the message, nothing else)",
              proc.returncode == 1 and proc.stderr.strip() == msg and not proc.stdout,
              f"exit {proc.returncode}, stdout {proc.stdout[-200:]!r}, stderr {proc.stderr[-300:]!r}")

sys.path.insert(0, str(ROOT / "scripts"))
import run_checks  # noqa: E402  (its main() runs only under __main__)

collected = [p.resolve() for p in run_checks.collect()]
check("run_checks.py collects nothing under scripts/archive/",
      not any(ARCHIVE.resolve() in p.parents for p in collected),
      ", ".join(str(p) for p in collected if ARCHIVE.resolve() in p.parents))

print("\n%s" % ("ALL PASS" if not FAILS else "FAILED: " + ", ".join(FAILS)))
sys.exit(1 if FAILS else 0)
