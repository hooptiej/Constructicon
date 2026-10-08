#!/usr/bin/env python3
"""Run every self-contained test and check in scripts/ and print one summary table (#552).

The pre-merge step. It runs, each in its own process from the repo root:

    scripts/test_*.py     in-process tests (throwaway DB, storage and exports; no server)
    scripts/check_*.py    static / structural checks
    scripts/test_*.js     client-side tests (needs `node` on PATH)

and prints, per script: result, how many checks passed, how many failed, and the time. It ends with
one overall line. The command for a throwaway container (the image has Python and the app's
dependencies but no Node; the checkout is mounted read-only, no network):

    docker run --rm --network none -v "$PWD":/app:ro -w /app constructicon:pr-NNN \\
        python scripts/run_checks.py

    docker run --rm --network none -v "$PWD":/app:ro -w /app node:22-alpine \\
        sh -c 'for f in scripts/test_*.js; do echo "== $f"; node "$f"; done'

NO EXIT CODES. This runner never looks at a script's exit status and does not set its own: a bare
number says nothing about WHAT failed (and some tests deliberately never call sys.exit(1), e.g.
test_attic_546.py). Each script's result is decided from what it PRINTS. The repo's scripts print
`PASS` / `ok` / `FAIL` / `FAILED: <names>` lines and a final verdict ("all checks passed", "ALL
PASS", "OK: ...", "0 failure(s)", "N/M passed", ...); `classify()` below reads those. Output it
cannot classify is reported as UNKNOWN with its last lines, never as a pass. A script that crashes
(a traceback with no verdict after it) or runs past --timeout is a FAIL with that reason.

What is skipped, and why (each skip prints its reason; none counts as a pass):
  * LIVE-SERVER scripts need a running instance and write to it. test_office.py (it says "LIVE-SERVER
    TEST" and takes `--base-url`) is skipped unless you pass `--live-url http://host:port`; point that at
    constructicon-test, NEVER production. check_curator_queue.py talks to localhost:80, so it must run
    inside the instance's own container (`docker exec constructicon-test python3
    scripts/check_curator_queue.py`) and is always skipped here, with that command printed.
  * Anything needing `node` when node is not on PATH, with the docker command to run it instead.
  * scripts/golden_master.py is a before/after snapshot tool, not a check: not collected.

Environment: CONSTRUCTICON_DB_PATH, CONSTRUCTICON_STORAGE_DIR and CONSTRUCTICON_EXPORTS_DIR are
pointed at fresh temp directories for each script unless you already set them (so a run from a
read-only mount, where the repo-relative defaults cannot be created, still works), and
PYTHONDONTWRITEBYTECODE is set so nothing is written into the checkout.

    python scripts/run_checks.py                 run everything
    python scripts/run_checks.py -k replace_file only scripts whose name contains this (repeatable)
    python scripts/run_checks.py -j 2            run two at a time (default 1; they are CPU/RAM heavy)
    python scripts/run_checks.py --list          show what would run or be skipped, run nothing
    python scripts/run_checks.py --show-output   print the full output of every non-PASS script
    python scripts/run_checks.py --selftest      check the output classifier against sample outputs
"""

import argparse
import concurrent.futures
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SCRIPTS = ROOT / "scripts"
DEFAULT_TIMEOUT = 900

PASS, FAIL, SKIP, UNKNOWN = "PASS", "FAIL", "SKIP", "UNKNOWN"

_ANSI = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")

# One check that failed: "FAIL name", "FAIL: name", "[FAIL] name", "FAILED: a, b", "✗ name: why".
_FAIL_LINE = re.compile(r"^\s*(?P<kw>\[\s*FAIL(?:ED)?\s*\]|FAIL(?:ED)?\b|✗|✘)[\s:\-]*(?P<rest>.*)$")
# One check that passed: "PASS name", "ok   name", "[PASS] name", "✓ name".
_PASS_LINE = re.compile(r"^\s*(?:\[\s*(?:PASS|ok)\s*\]|PASS\b|ok\s|✓|✔)")

# Failure verdicts that carry a count; the count must be above zero to count as failing.
_FAIL_COUNTS = [
    re.compile(r"(?P<n>\d+)\s+FAILED\b"),                       # "3 FAILED: [...]", "2 FAILED"
    re.compile(r"(?P<n>\d+)\s+(?:checks?\s+|tests?\s+)?failures?\b", re.I),   # "2 failure(s)"
    re.compile(r"(?P<n>\d+)\s*/\s*\d+\s+failed\b", re.I),       # "1/9 failed"
    re.compile(r"(?P<n>\d+)\s+violation\(s\)"),                 # check_layering
    re.compile(r"(?P<n>\d+)\s+problem\(s\)"),                   # check_policy_doors / check_routes_roles
    re.compile(r"(?P<n>\d+)\s+silent except handler"),          # check_no_silent_except
]
# "N/M passed": passing only when N == M.
_RATIO_PASSED = re.compile(r"(?P<a>\d+)\s*/\s*(?P<b>\d+)\s+passed\b", re.I)
# Failure lines that are not "FAIL ..." but are just as plain.
_FAIL_WORDS = re.compile(
    r"REFUSING TO RUN|^\s*(?:MISMATCH|UNESCAPED|PROVENANCE on the face)\b|\bFAILED\s*$|, FAILED\b")
# Passing verdicts.
_PASS_VERDICTS = [
    re.compile(r"(?<!not )\ball\b[^.\n]{0,40}?\bpass(?:ed)?\b", re.I),   # all passed, All 9 passed, All provenance option checks passed.
    re.compile(r"^\s*ALL\s+(?:CHECKS\s+)?PASS(?:ED)?\b"),                                   # ALL PASS, ALL CHECKS PASSED
    re.compile(r"^\s*OK\b"),                                                                # OK: ...
    re.compile(r"\w:\s+OK\b"),                                                              # check_layering: OK (...)
    re.compile(r"\ball match\b", re.I),                                                     # item cards ... all match
    re.compile(r"\b0\s+(?:checks?\s+|tests?\s+)?failures?\b", re.I),                        # 0 failure(s)
    re.compile(r"\b(\d+)\s*/\s*\1\s+passed\b", re.I),                                       # 24/24 passed
]
_TRACEBACK = "Traceback (most recent call last):"


def classify(output):
    """Decide a script's result from its printed output alone.

    Returns a dict: status (PASS / FAIL / SKIP / UNKNOWN), passed, failed (ints, or None when the script
    prints no per-check lines or counts), failing (list of failing check names), detail (a short
    reason or the last lines for UNKNOWN). Pure: no I/O, so --selftest can feed it samples.
    """
    text = _ANSI.sub("", output or "")
    lines = [ln.rstrip() for ln in text.splitlines()]
    nonblank = [(i, ln) for i, ln in enumerate(lines) if ln.strip()]

    passed_lines = sum(1 for ln in lines if _PASS_LINE.match(ln))
    failing, summary_names, fail_signals = [], [], []
    for ln in lines:
        if _PASS_LINE.match(ln):
            continue   # a passing check's own text ("PASS the build failed cleanly") is never failure evidence
        m = _FAIL_LINE.match(ln)
        if m:
            rest = m.group("rest").strip()
            # "FAIL: 3 problem(s):" and "FAILED: 2 type(s) failed ..." are summaries, not check names.
            if re.match(r"^\d+\s", rest) or not rest:
                fail_signals.append(ln.strip())
            elif m.group("kw") == "FAILED":
                summary_names.append(rest)   # "FAILED: a, b": repeats the FAIL lines above it, if any
            else:
                failing.append(rest)
            continue
        if _FAIL_WORDS.search(ln):
            fail_signals.append(ln.strip())
            continue
        for rx in _FAIL_COUNTS:
            m = rx.search(ln)
            if m and int(m.group("n")) > 0:
                fail_signals.append(ln.strip())
                break
        else:
            m = _RATIO_PASSED.search(ln)
            if m and m.group("a") != m.group("b"):
                fail_signals.append(ln.strip())

    # Counts for the table, from a "N/M passed" or "N/M failed" line when there are no per-check lines.
    passed = passed_lines if passed_lines else None
    failed = len(failing) if failing else None
    for ln in reversed(lines):
        m = _RATIO_PASSED.search(ln)
        if m:
            if passed is None:
                passed = int(m.group("a"))
            if failed is None:
                failed = int(m.group("b")) - int(m.group("a"))
            break
        m = re.search(r"(\d+)\s*/\s*(\d+)\s+failed\b", ln, re.I)
        if m:
            if failed is None:
                failed = int(m.group(1))
            if passed is None:
                passed = int(m.group(2)) - int(m.group(1))
            break

    # (A script that prints only that it skipped itself is SKIP, decided below.)
    # A verdict counts only near the end: a "passed" line in the middle of a run that then went quiet
    # (killed, hung) must not read as success.
    tail_idx = {i for i, _ in nonblank[-6:]}
    last_verdict = -1
    for i, ln in nonblank:
        if any(rx.search(ln) for rx in _PASS_VERDICTS) or _FAIL_LINE.match(ln):
            last_verdict = i
    last_tb = max((i for i, ln in enumerate(lines) if _TRACEBACK in ln), default=-1)

    if failing or summary_names or fail_signals:
        names = failing or summary_names or fail_signals
        return dict(status=FAIL, passed=passed, failed=failed if failed is not None else len(names),
                    failing=names, detail="")
    if last_tb > last_verdict:
        tail = [ln for ln in lines[last_tb:] if ln.strip()]
        return dict(status=FAIL, passed=passed, failed=failed, failing=[],
                    detail="crashed: " + (tail[-1].strip() if tail else "traceback with no message"))
    if last_verdict in tail_idx and any(rx.search(lines[last_verdict]) for rx in _PASS_VERDICTS):
        return dict(status=PASS, passed=passed, failed=0 if failed is None else failed, failing=[], detail="")
    skips = [ln.strip() for _, ln in nonblank if re.search(r"\bskipp(?:ing|ed)\b", ln, re.I)]
    if skips and not passed_lines:
        return dict(status=SKIP, passed=None, failed=None, failing=[], detail="the script skipped itself: " + skips[0])
    tail = [ln.strip() for _, ln in nonblank[-3:]]
    return dict(status=UNKNOWN, passed=passed, failed=failed, failing=[],
                detail=" | ".join(tail) if tail else "(no output at all)")


# ---------------------------------------------------------------------------------------------
# Collecting and running

def _live_kind(path):
    """None for an in-process script; "url" for a live script that takes --base-url; "inside" for a
    live script that must run inside the instance's own container (it talks to localhost)."""
    try:
        src = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    if re.search(r"add_argument\(\s*['\"]--base-url", src):
        return "url"
    if "LIVE-SERVER TEST" in src or "Run INSIDE the app container" in src:
        return "inside"
    return None


def _is_live(path):
    return _live_kind(path) is not None


def _needs_node(path):
    if path.suffix == ".js":
        return True
    try:
        src = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return False
    return 'which("node")' in src or "which('node')" in src


def collect():
    found = []
    for pattern in ("test_*.py", "test_*.js", "check_*.py"):
        found.extend(SCRIPTS.glob(pattern))
    return sorted(set(found), key=lambda p: (p.suffix != ".py", p.name))


def plan(path, live_url):
    """(action, reason): action is 'run' or 'skip'."""
    kind = _live_kind(path)
    if kind == "inside":
        return "skip", ("live-server script: it talks to the instance on localhost, so it has to run inside that "
                        f"instance's container (constructicon-test, never production): "
                        f"docker exec <container> python3 scripts/{path.name}")
    if kind == "url" and not live_url:
        return "skip", ("live-server script: needs a running instance and writes to it; "
                        "pass --live-url http://host:port (constructicon-test, never production)")
    if _needs_node(path) and not shutil.which("node"):
        rel = path.relative_to(ROOT).as_posix()
        return "skip", (f"node is not on PATH. Run it with: docker run --rm --network none "
                        f"-v \"$PWD\":/app:ro -w /app node:22-alpine node {rel}"
                        if path.suffix == ".js" else
                        "this check drives node and node is not on PATH. Run it from an image that has node")
    return "run", ""


def _env_for(base_tmp, name):
    env = dict(os.environ)
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    env["PYTHONUNBUFFERED"] = "1"
    own = Path(base_tmp) / name
    own.mkdir(parents=True, exist_ok=True)
    if not env.get("CONSTRUCTICON_DB_PATH"):
        (own / "db").mkdir(exist_ok=True)
        env["CONSTRUCTICON_DB_PATH"] = str(own / "db" / "imagerepo.db")
    if not env.get("CONSTRUCTICON_STORAGE_DIR"):
        (own / "storage").mkdir(exist_ok=True)
        env["CONSTRUCTICON_STORAGE_DIR"] = str(own / "storage")
    if not env.get("CONSTRUCTICON_EXPORTS_DIR"):
        (own / "exports").mkdir(exist_ok=True)
        env["CONSTRUCTICON_EXPORTS_DIR"] = str(own / "exports")
    return env


def run_one(path, base_tmp, timeout, live_url):
    name = path.name
    cmd = ["node" if path.suffix == ".js" else sys.executable, str(path)]
    if live_url and _live_kind(path) == "url":
        cmd += ["--base-url", live_url]
    started = time.time()
    try:
        done = subprocess.run(cmd, cwd=str(ROOT), env=_env_for(base_tmp, path.stem), stdin=subprocess.DEVNULL,
                              stdout=subprocess.PIPE, stderr=subprocess.STDOUT, timeout=timeout)
        output = done.stdout.decode("utf-8", errors="replace")
        result = classify(output)
    except subprocess.TimeoutExpired as e:
        output = (e.stdout or b"").decode("utf-8", errors="replace")
        result = dict(status=FAIL, passed=None, failed=None, failing=[],
                      detail=f"timed out after {timeout}s (last output: "
                             f"{' | '.join(l.strip() for l in output.splitlines()[-2:] if l.strip()) or 'none'})")
    except OSError as e:
        output = ""
        result = dict(status=FAIL, passed=None, failed=None, failing=[],
                      detail=f"could not start {cmd[0]}: {type(e).__name__}: {e}")
    result.update(name=name, seconds=time.time() - started, output=output)
    return result


# ---------------------------------------------------------------------------------------------
# Reporting

def _cell(v):
    return "-" if v is None else str(v)


def print_table(rows):
    header = ("script", "result", "passed", "failed", "time")
    body = [(r["name"], r["status"], _cell(r.get("passed")), _cell(r.get("failed")),
             "-" if r["status"] == SKIP else f"{r['seconds']:.1f}s") for r in rows]
    widths = [max(len(header[i]), *(len(b[i]) for b in body)) if body else len(header[i]) for i in range(5)]
    fmt = "{:<%d}  {:<%d}  {:>%d}  {:>%d}  {:>%d}" % tuple(widths)
    print(fmt.format(*header))
    print("  ".join("-" * w for w in widths))
    for b in body:
        print(fmt.format(*b))


def print_details(rows, show_output):
    for r in rows:
        if r["status"] == PASS:
            continue
        print()
        if r["status"] == FAIL:
            print(f"FAIL  {r['name']}")
            for n in r["failing"]:
                print(f"      failing check: {n}")
            if r["detail"]:
                print(f"      {r['detail']}")
        elif r["status"] == UNKNOWN:
            print(f"UNKNOWN: {r['detail']}  <- {r['name']} (its output matched no pass or fail pattern; read it)")
        else:
            print(f"SKIP  {r['name']}: {r['detail']}")
        if show_output and r["status"] != SKIP:
            print("      --- full output ---")
            for ln in r["output"].splitlines():
                print("      " + ln)


def overall_line(rows):
    n = {s: sum(1 for r in rows if r["status"] == s) for s in (PASS, FAIL, SKIP, UNKNOWN)}
    ran = n[PASS] + n[FAIL] + n[UNKNOWN]
    parts = [f"{n[PASS]} passed", f"{n[FAIL]} failed", f"{n[UNKNOWN]} unclassified", f"{n[SKIP]} skipped"]
    if n[FAIL] or n[UNKNOWN]:
        bad = [r["name"] for r in rows if r["status"] in (FAIL, UNKNOWN)]
        return f"RESULT: NOT CLEAN: {', '.join(parts)} (of {len(rows)}): {', '.join(bad)}"
    if not ran:
        return f"RESULT: NOTHING RAN: {', '.join(parts)} (of {len(rows)})"
    tail = f" ({n[SKIP]} skipped, see reasons above)" if n[SKIP] else ""
    return f"RESULT: ALL {n[PASS]} SCRIPTS THAT RAN PASSED{tail}"


# ---------------------------------------------------------------------------------------------
# Self-test: the classifier against the shapes the repo's scripts print

SAMPLES = [
    ("PASS a\nPASS b\n\nall checks passed\n", PASS, []),
    ("PASS a\nFAIL b (got 1)\n\nFAILED: b\n", FAIL, ["b (got 1)"]),
    ("PASS a\n\nALL PASS\n", PASS, []),
    ("PASS a\nFAIL some check\n\nFAILED: some check\n", FAIL, ["some check"]),
    ("ok   x\nok   y\nall passed\n", PASS, []),
    ("ok   x\nFAIL y\nFAILED: ['y']\n", FAIL, ["y"]),
    ("PASS x\n\n0 failure(s)\n", PASS, []),
    ("PASS x\nFAIL y\n\n1 failure(s)\n", FAIL, ["y"]),
    ("PASS a\nPASS b\n2/2 passed\n", PASS, []),
    ("PASS a\nFAIL b\n1/2 passed\n", FAIL, ["b"]),
    ("2/3 passed\n", FAIL, []),
    ("PASS a\nFAIL x\n\n1/9 failed\n", FAIL, ["x"]),
    ("All 9 passed\n", PASS, []),
    ("check_layering: OK (12 private writers)\n", PASS, []),
    ("check_layering: 2 violation(s)\n  core/x.py calls db._y\n", FAIL, []),
    ("OK: no silent except handlers\n", PASS, []),
    ("web/a.py:3: silent except in f()\n\n1 silent except handler(s). Log with context.\n", FAIL, []),
    ("known doors: 5\nFAIL: 2 problem(s):\n  door x\n", FAIL, []),
    ("OK: 31 types pass all checks.\n", PASS, []),
    ("✓ a\n✗ b: bad\n\nFAILED: 1 type(s) failed compliance checks:\n  b: bad\n", FAIL, ["b: bad"]),
    ("INFO x\n\nALL CHECKS PASSED\n", PASS, []),
    ("PASS a\n\n2 FAILED: ['a', 'b']\n", FAIL, []),
    ("item cards: 4 fixture(s) compared, all match, hostile input escaped\n", PASS, []),
    ("MISMATCH for x mini\nitem cards: 4 fixture(s) compared, FAILED\n", FAIL, []),
    ("_testenv: REFUSING TO RUN: the DB path resolves to /app/x\n", FAIL, []),
    ("PASS a\nTraceback (most recent call last):\n  File \"x\", line 1\nValueError: boom\n", FAIL, []),
    ("Build failed: patched failure\nTraceback (most recent call last):\n  File \"x\"\nOSError: patched\nPASS ok\n\nall checks passed\n", PASS, []),
    ("PASS a\nsomething else happened\n", UNKNOWN, []),
    ("All provenance option checks passed.\n", PASS, []),
    ("PASS a\nnot all checks passed\n", UNKNOWN, []),
    ("node not found: skipping the JS/macro comparison\n", SKIP, []),
    ("", UNKNOWN, []),
]


def selftest():
    bad = 0
    for i, (text, want, want_names) in enumerate(SAMPLES, 1):
        got = classify(text)
        ok = got["status"] == want and (not want_names or got["failing"] == want_names)
        print(f"{'PASS' if ok else 'FAIL'} sample {i}: want {want}{' ' + str(want_names) if want_names else ''}, got {got['status']}"
              f"{' ' + str(got['failing']) if got['failing'] else ''}")
        bad += 0 if ok else 1
    print(f"classifier self-test: {len(SAMPLES) - bad}/{len(SAMPLES)} passed" + (f", {bad} FAILED" if bad else ""))


# ---------------------------------------------------------------------------------------------

def main():
    ap = argparse.ArgumentParser(description="Run every self-contained test/check and summarise (#552).")
    ap.add_argument("-k", dest="only", action="append", default=[], metavar="TEXT",
                    help="only scripts whose file name contains TEXT (repeatable)")
    ap.add_argument("-j", "--jobs", type=int, default=1, help="scripts to run at once (default 1)")
    ap.add_argument("--timeout", type=int, default=DEFAULT_TIMEOUT, help=f"seconds per script (default {DEFAULT_TIMEOUT})")
    ap.add_argument("--live-url", metavar="URL", help="also run the live-server scripts against this base URL")
    ap.add_argument("--list", action="store_true", help="show what would run or be skipped; run nothing")
    ap.add_argument("--show-output", action="store_true", help="print the full output of every non-PASS script")
    ap.add_argument("--selftest", action="store_true", help="check the output classifier against sample outputs")
    args = ap.parse_args()

    if args.selftest:
        selftest()
        return

    scripts = [p for p in collect() if not args.only or any(t in p.name for t in args.only)]
    if not scripts:
        print(f"RESULT: NOTHING RAN: no script in {SCRIPTS} matches {args.only or 'test_*.py / test_*.js / check_*.py'}")
        return

    todo, rows = [], []
    for p in scripts:
        action, reason = plan(p, args.live_url)
        if action == "skip":
            rows.append(dict(name=p.name, status=SKIP, passed=None, failed=None, failing=[], detail=reason,
                             seconds=0.0, output=""))
        else:
            todo.append(p)

    if args.list:
        for p in todo:
            print(f"run   {p.name}")
        for r in rows:
            print(f"skip  {r['name']}: {r['detail']}")
        return

    base_tmp = tempfile.mkdtemp(prefix="run-checks-")
    print(f"Running {len(todo)} script(s) ({len(rows)} skipped), {args.jobs} at a time, from {ROOT}")
    print(f"Scratch directories: {base_tmp}\n")
    finished = 0
    with concurrent.futures.ThreadPoolExecutor(max_workers=max(1, args.jobs)) as pool:
        futures = {pool.submit(run_one, p, base_tmp, args.timeout, args.live_url): p for p in todo}
        for fut in concurrent.futures.as_completed(futures):
            r = fut.result()
            finished += 1
            rows.append(r)
            extra = ""
            if r["status"] == FAIL and r["failing"]:
                extra = "  <- " + "; ".join(r["failing"][:3])
            elif r["status"] in (FAIL, UNKNOWN) and r["detail"]:
                extra = "  <- " + r["detail"][:160]
            print(f"[{finished}/{len(todo)}] {r['status']:<7} {r['name']} ({r['seconds']:.1f}s){extra}", flush=True)

    try:
        shutil.rmtree(base_tmp)
    except OSError as e:
        print(f"note: could not remove the scratch directory {base_tmp}: {type(e).__name__}: {e}")

    rows.sort(key=lambda r: (r["name"].endswith(".js"), r["name"]))
    print()
    print_table(rows)
    print_details(rows, args.show_output)
    print()
    print(overall_line(rows))


if __name__ == "__main__":
    main()
