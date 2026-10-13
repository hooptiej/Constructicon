"""Dependency audit (#650): no installed Python package may carry a known advisory.

    python scripts/check_dependency_audit.py                      audit the environment it runs in
    python scripts/check_dependency_audit.py --requirements FILE  audit a pinned file (a pip freeze)

Run it in the app image WITH network (the advisory database is online), so it audits exactly what
the containers run, transitive packages, pip and setuptools included:

    docker run --rm -v "$PWD":/app:ro -w /app constructicon:pr-NNN python scripts/check_dependency_audit.py

What it does: lists every installed distribution as name==version (a local label such as torch's
"+cpu" is dropped, the advisory database doesn't know it), writes them to a temp file and runs
`pip-audit -r <file> --no-deps --disable-pip` on it. pip-audit is NOT in the app image (it would
ship a scanner and its dependencies to production for a check that runs before a deploy). If it
isn't importable, this script installs the pinned PIP_AUDIT into a temp directory and runs it from
there, in a subprocess, so that copy never mixes into the environment being audited and nothing in
the image or the checkout changes.

Offline it SKIPS, with the reason: scripts/run_checks.py runs with --network none, so in the
pre-merge suite this check shows as SKIP, never as a pass. A known advisory prints one
`FAIL <package>==<version>: <id> (fixed in ...)` line per advisory and a final `FAILED: <packages>`.
An advisory accepted on purpose goes in IGNORE with its reason (none so far).
"""

import argparse
import importlib.metadata
import importlib.util
import json
import os
import re
import socket
import subprocess
import sys
import tempfile
from pathlib import Path

PIP_AUDIT = "pip-audit==2.10.1"
ADVISORY_HOST = ("pypi.org", 443)   # pip-audit's default source is PyPI's vulnerability data

# Advisory ID -> why it is accepted. Keep this empty unless the owner decides otherwise.
IGNORE = {}


def installed():
    """name==version for every distribution in this interpreter's environment, sorted."""
    seen = {}
    for dist in importlib.metadata.distributions():
        name = dist.metadata.get("Name")
        if not name:
            continue
        version = dist.version.split("+", 1)[0]
        seen[re.sub(r"[-_.]+", "-", name).lower()] = f"{name}=={version}"
    return [seen[k] for k in sorted(seen)]


def online():
    try:
        socket.create_connection(ADVISORY_HOST, timeout=5).close()
        return True
    except OSError:
        return False


def pip_audit_command(tmp):
    """(argv prefix, env) that runs pip-audit, installing the pinned copy into tmp if needed."""
    if importlib.util.find_spec("pip_audit") is not None:
        return [sys.executable, "-m", "pip_audit"], dict(os.environ)
    target = Path(tmp) / "pip-audit"
    r = subprocess.run([sys.executable, "-m", "pip", "install", "--quiet", "--disable-pip-version-check",
                        "--no-warn-script-location", "--target", str(target), PIP_AUDIT],
                       capture_output=True, text=True)
    if r.returncode != 0:
        return None, (r.stderr or r.stdout).strip().splitlines()[-1:]
    env = dict(os.environ, PYTHONPATH=str(target))
    return [sys.executable, "-m", "pip_audit"], env


def main(argv):
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--requirements", metavar="FILE", help="audit this pinned file instead of the environment")
    args = ap.parse_args(argv)

    if args.requirements:
        pins = [ln.strip() for ln in Path(args.requirements).read_text().splitlines()
                if ln.strip() and not ln.lstrip().startswith("#")]
        what = args.requirements
    else:
        pins = installed()
        what = "the installed environment"

    if not online():
        print(f"SKIPPED: no network ({ADVISORY_HOST[0]} unreachable), so the advisory database can't be read. "
              "Run it with network: "
              "docker run --rm -v \"$PWD\":/app:ro -w /app <app image> python scripts/check_dependency_audit.py")
        return 0

    with tempfile.TemporaryDirectory(prefix="dep-audit-") as tmp:
        cmd, env = pip_audit_command(tmp)
        if cmd is None:
            print(f"SKIPPED: could not install {PIP_AUDIT} into a temp dir: {' '.join(env)}")
            return 0
        req = Path(tmp) / "pins.txt"
        req.write_text("\n".join(p.split("+", 1)[0] for p in pins) + "\n")
        r = subprocess.run(cmd + ["-r", str(req), "--no-deps", "--disable-pip", "--progress-spinner", "off",
                                  "--format", "json"], capture_output=True, text=True, env=env)
        try:
            report = json.loads(r.stdout)
        except ValueError:
            tail = (r.stderr or r.stdout).strip().splitlines()[-3:]
            print("FAIL pip-audit did not produce a report: " + " | ".join(tail))
            return 1

    deps = report.get("dependencies", report) if isinstance(report, dict) else report
    bad, count = [], 0
    for dep in deps:
        vulns = list({v["id"]: v for v in dep.get("vulns", []) if v.get("id") not in IGNORE}.values())
        if dep.get("skip_reason"):
            print(f"note: {dep['name']} not audited: {dep['skip_reason']}")
        if not vulns:
            continue
        bad.append(dep["name"])
        for v in vulns:
            count += 1
            fixes = ", ".join(v.get("fix_versions") or []) or "no fixed version yet"
            aliases = ", ".join(a for a in v.get("aliases") or [] if a.startswith("CVE-"))
            print(f"FAIL {dep['name']}=={dep['version']}: {v['id']}"
                  f"{' (' + aliases + ')' if aliases else ''}, fixed in {fixes}")
    for adv, why in IGNORE.items():
        print(f"note: ignoring {adv}: {why}")
    if bad:
        print(f"FAILED: {', '.join(bad)} ({count} known advisories in {what})")
        return 1
    print(f"OK: no known advisories in {len(deps)} packages ({what}, {PIP_AUDIT})")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
