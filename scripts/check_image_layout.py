"""The image carries the app code, and only the app code (#688).

Text and YAML only: no Docker, no server, no database, no network. It checks that
  * the Dockerfile COPYs core/, web/, mcp_server/ and assets/ into /app, AFTER the pip installs (so a
    code change never invalidates the package layers), before `USER app` (so the code is root-owned
    and read-only to the app user), and stamps the version from a build arg after the code;
  * .dockerignore keeps secrets, data, .git, agent worktrees, backups, tests, scripts/ and a stray
    core/VERSION.json out of the build context, and still lets the code in. Docker's rule is
    emulated: the last pattern that matches the path or one of its parent directories wins;
  * docker-compose.yml.example bind-mounts no code into any service (prod runs the baked image), has
    no obsolete top-level `version:`, and names an image for the services that run the app;
  * docker-compose.override.yml.example (the test/dev fast loop) does mount the code.

Usage:  python scripts/check_image_layout.py
"""
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CODE_DIRS = ("core", "web", "mcp_server", "assets")
CODE_TARGETS = {f"/app/{d}" for d in CODE_DIRS} | {"/app/scripts"}

FAILS = []


def check(name, cond, extra=""):
    print(("PASS " if cond else "FAIL ") + name + (f"  [{extra}]" if (extra and not cond) else ""))
    if not cond:
        FAILS.append(name)


# ---- Dockerfile -------------------------------------------------------------------------------
def dockerfile_instructions(text):
    """[(line_no, INSTRUCTION, args)] with continuation lines joined and comments dropped."""
    out, buf, start = [], "", 0
    for i, raw in enumerate(text.splitlines(), 1):
        line = raw.strip()
        if not buf and (not line or line.startswith("#")):
            continue
        if not buf:
            start = i
        if line.endswith("\\"):
            buf += line[:-1] + " "
            continue
        buf += line
        parts = buf.split(None, 1)
        out.append((start, parts[0].upper(), parts[1] if len(parts) > 1 else ""))
        buf = ""
    return out


dockerfile = (ROOT / "Dockerfile").read_text(encoding="utf-8")
ins = dockerfile_instructions(dockerfile)


def first_index(pred):
    return next((n for n, (_, op, args) in enumerate(ins) if pred(op, args)), None)


pip_req = first_index(lambda op, a: op == "RUN" and "pip install" in a and "requirements.txt" in a)
user_app = first_index(lambda op, a: op == "USER" and a.strip().split(":")[0] == "app")
check("Dockerfile installs requirements.txt", pip_req is not None)
check("Dockerfile switches to USER app", user_app is not None)

copies = {}
for n, (_, op, args) in enumerate(ins):
    if op != "COPY":
        continue
    toks = [t for t in args.split() if not t.startswith("--")]
    if any(t.startswith("--chown") for t in args.split()):
        copies.setdefault("_chown", []).append(args)
    if len(toks) < 2:
        continue
    srcs, dest = toks[:-1], toks[-1]
    for s in srcs:
        copies[s.rstrip("/")] = (n, dest.rstrip("/"))
for d in CODE_DIRS:
    got = copies.get(d)
    check(f"Dockerfile COPYs {d}/ into /app/{d}", bool(got) and got[1] == f"/app/{d}", str(got))
    if got and pip_req is not None:
        check(f"COPY {d}/ comes after the pip installs (layer cache)", got[0] > pip_req)
    if got and user_app is not None:
        check(f"COPY {d}/ comes before USER app (root-owned code)", got[0] < user_app)
check("no COPY --chown (the code stays root-owned)", "_chown" not in copies, str(copies.get("_chown")))
check("Dockerfile never COPYs the whole context (`COPY . ...`)", "." not in copies, str(copies.get(".")))
check("Dockerfile does not COPY scripts/ (not runtime code; see #634)", "scripts" not in copies)

ver_arg = first_index(lambda op, a: op == "ARG" and a.split("=")[0].strip() == "CONSTRUCTICON_VERSION")
last_code_copy = max((copies[d][0] for d in CODE_DIRS if d in copies), default=None)
check("Dockerfile has ARG CONSTRUCTICON_VERSION", ver_arg is not None)
if ver_arg is not None and last_code_copy is not None:
    check("the version ARG comes after the code (a new version invalidates nothing earlier)",
          ver_arg > last_code_copy)
stamp = first_index(lambda op, a: op == "RUN" and "VERSION.json" in a and "CONSTRUCTICON_VERSION" in a)
check("a RUN writes core/VERSION.json from the build arg", stamp is not None and (ver_arg is None or stamp > ver_arg))
check("Dockerfile comments no longer say the code is bind-mounted, not baked",
      "not baked in" not in dockerfile and "are bind-mounted at run time" not in dockerfile)


# ---- .dockerignore ----------------------------------------------------------------------------
def load_ignore(text):
    pats = []
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        neg = line.startswith("!")
        p = line[1:] if neg else line
        p = p.strip().strip("/")
        if p.startswith("./"):
            p = p[2:]
        pats.append((neg, p))
    return pats


def _match(pattern, path):
    """Docker-ish: `**` spans directories, `*` stays inside one segment."""
    rx = ""
    i = 0
    while i < len(pattern):
        if pattern.startswith("**/", i):
            rx += "(?:.*/)?"
            i += 3
        elif pattern.startswith("**", i):
            rx += ".*"
            i += 2
        elif pattern[i] == "*":
            rx += "[^/]*"
            i += 1
        elif pattern[i] == "?":
            rx += "[^/]"
            i += 1
        else:
            rx += re.escape(pattern[i])
            i += 1
    return re.fullmatch(rx, path) is not None


def excluded(pats, path):
    parts = path.split("/")
    prefixes = ["/".join(parts[:k]) for k in range(1, len(parts) + 1)]
    result = False
    for neg, p in pats:
        if any(_match(p, pre) for pre in prefixes):
            result = not neg
    return result


ign_path = ROOT / ".dockerignore"
check(".dockerignore exists", ign_path.exists())
pats = load_ignore(ign_path.read_text(encoding="utf-8")) if ign_path.exists() else []
MUST_EXCLUDE = [
    "secrets/constructicon_token", "secrets/ollama_restart_token", ".env", ".env.local",
    ".git/config", ".git/HEAD", ".claude/worktrees/agent-x/core/db.py",
    "storage/abc.jpg", "storage-test/abc.jpg", "db/imagerepo.db", "db-test/imagerepo.db-wal",
    "data/imagerepo.db", "backups/backup.zip", "exports/current/index.html", "import/x.jpg",
    "imagerepo.db", "imagerepo.db-journal",
    "docker-compose.yml", "docker-compose.yml.bak-pre-688", "docker-compose.override.yml",
    "web.bak-295/app.py", "web.backup.1788571094/app.py", "core.bak/db.py", "web/app.py.bak",
    "scripts/test_auth_step1.py", "scripts/run_checks.py", "scripts/deploy.sh",
    "node_modules/x/index.js", "web/static/node_modules/x.js",
    "core/__pycache__/db.cpython-314.pyc", "core/VERSION.json", "app.log",
    "notes/x.md", "docs/x.md", "CHANGELOG.md", "README.md",
]
MUST_INCLUDE = [
    "Dockerfile", "requirements.txt", "core/db.py", "core/object_types/image.py",
    "core/backup.py", "web/app.py", "web/templates/base.html", "web/static/js/cards.js",
    "mcp_server/server.py", "assets/brand/logo.png",
]
for p in MUST_EXCLUDE:
    check(f".dockerignore keeps out {p}", excluded(pats, p))
for p in MUST_INCLUDE:
    check(f".dockerignore lets in {p}", not excluded(pats, p))


# ---- compose examples -------------------------------------------------------------------------
try:
    import yaml
except ImportError:  # outside the app image (a bare Windows Python)
    yaml = None


def volume_target(v):
    if isinstance(v, dict):
        return (v.get("target") or "").rstrip("/")
    parts = str(v).split(":")
    return parts[1].rstrip("/") if len(parts) >= 2 else ""


example_text = (ROOT / "docker-compose.yml.example").read_text(encoding="utf-8")
check("docker-compose.yml.example has no obsolete top-level `version:`",
      not re.search(r"(?m)^version\s*:", example_text))
override_path = ROOT / "docker-compose.override.yml.example"
check("docker-compose.override.yml.example exists (the test/dev fast loop)", override_path.exists())

if yaml is None:
    print("SKIPPED: the compose checks need PyYAML (it is in the app image); the Dockerfile and "
          ".dockerignore checks above ran")
else:
    services = (yaml.safe_load(example_text) or {}).get("services", {})
    app_services = [n for n, s in services.items()
                    if "web.app:app" in str(s.get("command", "")) or "mcp_server" in str(s.get("command", ""))]
    check("the example has app services", len(app_services) >= 2, str(app_services))
    for name, svc in services.items():
        mounted = sorted(t for t in map(volume_target, svc.get("volumes") or []) if t in CODE_TARGETS)
        check(f"example service {name} bind-mounts no code", not mounted, ", ".join(mounted))
    for name in app_services:
        img = services[name].get("image", "")
        check(f"example service {name} names its image (deploy.sh tags it per version)", bool(img), img)
    if override_path.exists():
        over = (yaml.safe_load(override_path.read_text(encoding="utf-8")) or {}).get("services", {})
        for name, svc in over.items():
            targets = {volume_target(v) for v in svc.get("volumes") or []}
            check(f"override service {name} is one of the example's services", name in services)
            check(f"override service {name} mounts core/, web/ and mcp_server/",
                  {"/app/core", "/app/web", "/app/mcp_server"} <= targets, str(sorted(targets)))
            check(f"override service {name} mounts no data or secrets",
                  not any(t.startswith(("/app/data", "/app/storage", "/run/secrets")) for t in targets))

if FAILS:
    print(f"FAILED: {', '.join(FAILS)}")
else:
    print("ALL PASS")
