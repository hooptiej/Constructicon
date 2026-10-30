#!/usr/bin/env python3
"""Self-contained check for scripts/check_dead_code.py, the reference scan (#546).

Builds a tiny fake repo in a temp directory (no DB, no app import, no server, no network) and checks
what the scan lists and what it leaves alone, then runs it once over this checkout to prove it stays
report-only (exit 0, an "OK: report only" verdict) and that the names #546 deleted are not back.

    python scripts/test_dead_code_546.py

Prints "ALL PASS", or "FAILED: <names>" listing every failing check by name.
"""
import contextlib
import io
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import check_dead_code as cdc  # noqa: E402

FAILS = []


def check(name, cond, extra=""):
    print(("PASS " if cond else "FAIL ") + name + (f"  [{extra}]" if (extra and not cond) else ""))
    if not cond:
        FAILS.append(name)


def write(root, rel, text):
    p = Path(root) / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text, encoding="utf-8")


with tempfile.TemporaryDirectory(prefix="deadcode546-") as tmp:
    root = Path(tmp)
    write(root, "core/a.py", (
        "LIVE_CONST = 1\n"
        "DEAD_CONST = 2\n"
        "TEST_CONST = 3\n"
        "lower_name = 4\n"
        "def used_fn():\n    return LIVE_CONST\n\n"
        "def dead_fn():\n    \"\"\"dead_fn mentions itself here.\"\"\"\n    return dead_fn()\n\n"
        "def tested_fn():\n    return 1\n\n"
        "def dynamic_fn():\n    return 1\n\n"
        "def archive_only_fn():\n    return 1\n\n"
        "@router.get('/x')\ndef route_fn():\n    return used_fn()\n\n"
        "class DeadClass:\n    pass\n\n"
        "def __getattr__(name):\n    return None\n"
    ))
    write(root, "web/b.py", "from core import a\nv = getattr(a, 'dynamic_fn')\n")
    write(root, "web/templates/page.html", '<div class="used-class built-{{ kind }}"></div>\n')
    write(root, "web/static/js/x.js", "el.className = 'state-' + s; el.classList.add(`tone-${t}`);\n")
    write(root, "web/static/style.css", (
        "/* .commented-out { color: red; } */\n"
        ".used-class { margin: .5em; }\n"
        ".dead-class, .used-class:hover { color: red; }\n"
        ".built-thing { color: blue; }\n"
        ".state-open { color: green; }\n"
        ".tone-warm { color: orange; }\n"
        "@media (max-width: 600px) { .dead-in-media { display: none; } }\n"
    ))
    write(root, "scripts/test_a.py", "from core import a\na.tested_fn(); a.TEST_CONST\n")
    write(root, "scripts/archive/old.py", "raise SystemExit('retired')\nfrom core import a\na.archive_only_fn()\n")

    unref, test_only = cdc.scan_python(root)
    un = {n for _, _, _, n in unref}
    to = {n for _, _, _, n in test_only}
    check("an unused function is listed", "dead_fn" in un, sorted(un))
    check("a function used only by itself (recursion, its own docstring) is still listed", "dead_fn" in un)
    check("an unused UPPER_CASE constant is listed", "DEAD_CONST" in un, sorted(un))
    check("an unused class is listed", "DeadClass" in un, sorted(un))
    check("a used function and constant are not listed", not ({"used_fn", "LIVE_CONST"} & (un | to)))
    check("a name looked up by string (getattr) counts as used", "dynamic_fn" not in un | to)
    check("a decorated function (a route) is skipped", "route_fn" not in un | to)
    check("dunder names are skipped", "__getattr__" not in un | to)
    check("lower-case module variables are not treated as constants", "lower_name" not in un | to)
    check("a name used only by scripts/test_* is test-only, not unreferenced",
          {"tested_fn", "TEST_CONST"} <= to and not ({"tested_fn", "TEST_CONST"} & un), (sorted(to), sorted(un)))
    check("a use inside scripts/archive/ (retired, refuses to run) does not count", "archive_only_fn" in un, sorted(un))
    line = {n: (rel, ln) for rel, ln, _, n in unref}
    check("each entry carries its file and line", line.get("DEAD_CONST") == ("core/a.py", 2), line.get("DEAD_CONST"))

    dead_css = {c for _, _, c in cdc.scan_css(root)}
    check("an unused CSS class is listed", "dead-class" in dead_css, sorted(dead_css))
    check("an unused class inside @media is listed", "dead-in-media" in dead_css, sorted(dead_css))
    check("a used class is not listed (plain or :hover)", "used-class" not in dead_css)
    check("classes built from strings ('x-' + s, `x-${s}`, x-{{ k }}) are not listed",
          not ({"state-open", "tone-warm", "built-thing"} & dead_css), sorted(dead_css))
    check("a commented-out selector is ignored", "commented-out" not in dead_css)
    check("a decimal value (.5em) is not read as a class", "5em" not in dead_css)

    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        rc = cdc.main(["--root", str(root)])
    out = buf.getvalue()
    check("main() exits 0 even with findings (report only)", rc == 0, rc)
    check("main() ends with the OK: report only verdict", out.strip().splitlines()[-1].startswith("OK: report only"), out[-200:])
    check("main() output has no FAIL line run_checks.py would read as a failure",
          not any(ln.lstrip().startswith(("FAIL", "MISMATCH")) for ln in out.splitlines()))

# ---- the real checkout -------------------------------------------------------------------------
buf = io.StringIO()
with contextlib.redirect_stdout(buf):
    rc = cdc.main([])
out = buf.getvalue()
check("the scan over this checkout exits 0", rc == 0, rc)
check("the scan over this checkout ends with the OK verdict",
      out.strip().splitlines()[-1].startswith("OK: report only"), out[-200:])
GONE_546 = ["add_test_client", "count_table_rows", "count_project_links_by_type", "list_recent_posts",
            "card_json", "STACK_UNDER", "ACTIVITIES", "HOME_SOURCES", "CARD_PROVENANCE", "cached_counts",
            "invalidate_cache", "DEDUPE_PAIRS", "count_active_needs", "_get_related_projects",
            "normalize_avatar", "to_text", "_project_has_tag", "PROVENANCE_TYPES"]
back = [n for n in GONE_546 if f" {n}\n" in out]
check("none of the names #546 deleted is defined again (and dead)", not back, back)
check("db.PROVENANCE_TYPES is gone", not any("PROVENANCE_TYPES =" in p.read_text(encoding="utf-8")
                                             for p in (HERE.parent / "core" / "db").glob("*.py")))  # #547: a package

print()
print("FAILED: " + ", ".join(FAILS) if FAILS else "ALL PASS")
