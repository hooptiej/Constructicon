"""Pin watch (#676): every package installed in the app image must be pinned, at the version installed.

    python scripts/check_requirements_freeze.py

Run it inside the built app image (it needs no network, so it runs in the pre-merge suite):

    docker run --rm --network none -v "$PWD":/app:ro -w /app constructicon:pr-NNN python scripts/check_requirements_freeze.py

What it compares: every distribution installed in this interpreter (importlib.metadata, the same list
`pip freeze` prints) against the pins in requirements.txt plus the Dockerfile's `torch==` pin. It
prints one FAIL line per problem and ends with `FAILED: <packages>` or `ALL PASS`:
  - an installed package with no pin ("unpinned"): a new transitive dependency crept in, or
    requirements.txt was never a full freeze. Fix: add `name==<installed version>` to requirements.txt;
  - a pinned package installed at another version ("version differs");
  - a requirements.txt line that is not an exact `==` pin;
  - a pin for a package that is not installed ("stale pin"), so the file no longer describes the image;
  - torch without its `+cpu` label, i.e. the CUDA build got in instead of the Dockerfile's CPU wheel.

Not from requirements on purpose (ALLOWED below): pip, which ships in the base image. The base image
itself is pinned by digest in the Dockerfile, so pip's version moves only when that digest is bumped;
it is reported as a note, not checked against a pin. (python:3.14-slim has no setuptools or wheel of
its own; setuptools is pinned in requirements.txt because torch pulls it in.)

Outside the app image (Windows, a dev venv, any Python without the Dockerfile's CPU torch) the
installed set is unrelated to the image, so it prints SKIPPED with the reason and checks nothing.
"""

import importlib.metadata
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

# Installed but legitimately not pinned in requirements.txt / the Dockerfile -> why.
ALLOWED = {
    "pip": "ships in the base image, which the Dockerfile pins by digest",
}

FAILS = []


def norm(name):
    """PEP 503 normalised project name."""
    return re.sub(r"[-_.]+", "-", name).lower()


def same_version(a, b):
    try:
        from packaging.version import Version
        return Version(a) == Version(b)
    except Exception:
        return a == b


def fail(pkg, why):
    print(f"FAIL {pkg}: {why}")
    if pkg not in FAILS:
        FAILS.append(pkg)


def installed():
    """{normalised name: (display name, version)} for this interpreter's environment."""
    out = {}
    for dist in importlib.metadata.distributions():
        name = dist.metadata.get("Name")
        if name:
            out[norm(name)] = (name, dist.version)
    return out


def pins():
    """{normalised name: (display name, version, where)}; bad lines are reported as failures."""
    out = {}
    for n, raw in enumerate((ROOT / "requirements.txt").read_text(encoding="utf-8").splitlines(), 1):
        line = raw.split("#", 1)[0].strip()
        if not line:
            continue
        m = re.fullmatch(r"([A-Za-z0-9][A-Za-z0-9._-]*)\s*==\s*([^\s;,]+)", line)
        if not m:
            fail(line, f"requirements.txt line {n} is not an exact name==version pin")
            continue
        out[norm(m.group(1))] = (m.group(1), m.group(2), "requirements.txt")
    docker = (ROOT / "Dockerfile").read_text(encoding="utf-8")
    m = re.search(r"\btorch==([^\s]+)", docker)
    if not m:
        fail("torch", "no torch==<version> pin found in the Dockerfile")
    else:
        out["torch"] = ("torch", m.group(1), "Dockerfile")
    return out


def main():
    have = installed()
    torch = have.get("torch")
    if not sys.platform.startswith("linux") or not torch or not torch[1].endswith("+cpu"):
        why = ("not Linux" if not sys.platform.startswith("linux")
               else "torch is not installed" if not torch else f"torch is {torch[1]}, not the image's +cpu build")
        print(f"SKIPPED: this is not the app image ({why}), so its installed packages say nothing about the "
              "image's pins. Run it in the image: docker run --rm --network none -v \"$PWD\":/app:ro -w /app "
              "<app image> python scripts/check_requirements_freeze.py")
        return 0

    want = pins()
    for key in sorted(have):
        name, version = have[key]
        if key in ALLOWED:
            print(f"note: {name}=={version} not pinned on purpose: {ALLOWED[key]}")
            continue
        if key not in want:
            fail(name, f"installed at {version} but not pinned (add {name}=={version} to requirements.txt)")
            continue
        pname, pversion, where = want[key]
        local = version.split("+", 1)[0]
        if not same_version(local, pversion):
            fail(name, f"installed at {version} but {where} pins {pversion}")
        elif key == "torch" and not version.endswith("+cpu"):
            fail(name, f"installed at {version}, not the CPU build the Dockerfile installs")
    for key in sorted(set(want) - set(have)):
        pname, pversion, where = want[key]
        fail(pname, f"{where} pins {pversion} but it is not installed (stale pin)")

    if FAILS:
        print(f"\nFAILED: {', '.join(FAILS)} ({len(FAILS)} of {len(have)} installed packages not pinned as installed)")
        return 1
    print(f"\nALL PASS: all {len(have) - len(ALLOWED.keys() & have.keys())} installed packages are pinned at "
          f"the installed version (requirements.txt + the Dockerfile's torch pin)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
