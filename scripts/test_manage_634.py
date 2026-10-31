#!/usr/bin/env python3
"""#634: the operational tools moved from scripts/ into core/manage/. Throwaway DB, no server.

    python scripts/test_manage_634.py

Covers, each in a real child process started exactly as the docs say (`python3 -m core.manage.<tool>`
from the repo root):
  * reset_password via the module path: --create-admin, a reset, --list; every change-log row it
    writes carries actor `script` (core/actor.py's process default now covers core/manage/, and main()
    runs inside acting_as(ACTOR_SCRIPT)); a module-path process that writes outside main() is `script`
    too (the process default on its own);
  * mcp_token generate: writes the file (mode 600 on POSIX), prints only the path, refuses to
    overwrite without --force, and imports nothing outside the stdlib (core/__init__.py and
    core/manage/__init__.py stay import-free, so it runs on a bare checkout);
  * mcp_token check: runs and reports the local config without printing the token;
  * the stubs left at scripts/reset_password.py and scripts/mcp_token.py exit non-zero and name the
    new command;
  * the two tests that drive reset_password in-process import it from core.manage.
"""

import ast
import os
import secrets
import sqlite3
import subprocess
import sys

import _testenv  # noqa: E402  (scripts/_testenv.py: temp DB + storage + exports, refuses otherwise)
TMP = _testenv.isolate("manage634-")
HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.abspath(os.path.join(HERE, ".."))
sys.path.insert(0, ROOT)
_testenv.assert_isolated()

FAILS = []


def check(name, cond, extra=""):
    print(("PASS " if cond else "FAIL ") + name + (f"  [{extra}]" if (extra and not cond) else ""))
    if not cond:
        FAILS.append(name)


def run(args, env_extra=None, cwd=ROOT):
    env = dict(os.environ)
    env.pop("PYTHONPATH", None)
    env.update(env_extra or {})
    return subprocess.run([sys.executable] + args, cwd=cwd, env=env, capture_output=True, text=True, timeout=120)


DB_PATH = os.environ["CONSTRUCTICON_DB_PATH"]


def audit_actors(op):
    conn = sqlite3.connect(DB_PATH)
    try:
        return [r[0] for r in conn.execute("SELECT actor FROM audit_log WHERE op = ? ORDER BY id", (op,))]
    finally:
        conn.close()


# --- 1. reset_password via `python3 -m core.manage.reset_password` -------------------------------
print("--- 1. reset_password via the module path ---")
admin = "rescue_" + secrets.token_hex(3)
r = run(["-m", "core.manage.reset_password", "--create-admin", admin, "--generate"])
check("--create-admin exits 0", r.returncode == 0, r.stderr[-400:])
check("--create-admin says it made the admin", f"Created admin {admin}" in r.stdout, r.stdout[-300:])
check("the generated password is printed once", r.stdout.count("store it now") == 1, r.stdout[-300:])

r = run(["-m", "core.manage.reset_password", admin, "--generate"])
check("a reset exits 0", r.returncode == 0, r.stderr[-400:])
check("a reset says what it did", f"Password reset for {admin}" in r.stdout, r.stdout[-300:])

r = run(["-m", "core.manage.reset_password", "--list"])
check("--list exits 0 and lists the admin", r.returncode == 0 and admin in r.stdout and "admin" in r.stdout,
      r.stdout[-300:] + r.stderr[-300:])
check("--list prints no hash", "scrypt$" not in r.stdout)

created = audit_actors("user_create")
pw_rows = audit_actors("user_set_password")
check("--create-admin is logged with actor script", created and set(created) == {"script"}, created)
check("the reset is logged with actor script", pw_rows and set(pw_rows) == {"script"}, pw_rows)
conn = sqlite3.connect(DB_PATH)
other = [r for r in conn.execute("SELECT actor, op FROM audit_log WHERE op IS NOT NULL AND actor != 'script'")]
conn.close()
check("no change-log row from the tool has another actor", not other, other[:5])

r = run(["-m", "core.manage.reset_password", "no_such_user_" + secrets.token_hex(2), "--generate"])
check("an unknown user is refused (exit 1, not a traceback)", r.returncode == 1 and "Traceback" not in r.stderr,
      r.stderr[-300:])

# The process default on its own: a process started as `python3 -m core.manage.<x>` (runpy sets
# sys.argv[0] to the module's file before its code runs) is `script` even with no acting_as, which is
# what a future core.manage tool, or a write before main()'s acting_as, gets. Simulated by setting
# argv[0] the way runpy does, so it needs no file written into the (read-only) checkout.
for name in ("reset_password.py", "mcp_token.py", "some_future_tool.py"):
    argv0 = os.path.join(ROOT, "core", "manage", name)
    r = run(["-c", "import sys; sys.argv[0] = " + repr(argv0) + "; from core import actor; "
                   "print('ACTOR=' + actor.current_actor())"])
    check(f"a process started from core/manage/{name} defaults to actor script", "ACTOR=script" in r.stdout,
          r.stdout[-200:] + r.stderr[-300:])
argv0 = os.path.join(ROOT, "core", "users.py")
r = run(["-c", "import sys; sys.argv[0] = " + repr(argv0) + "; from core import actor; "
               "print('ACTOR=' + str(actor._PROCESS_DEFAULT))"])
check("a process started from elsewhere in core/ is NOT a script", "ACTOR=None" in r.stdout,
      r.stdout[-200:] + r.stderr[-300:])
r = run(["-c", "from core import actor; print('ACTOR=' + str(actor._PROCESS_DEFAULT))"])
check("a plain `python -c` process is NOT a script (no false positive)", "ACTOR=None" in r.stdout,
      r.stdout[-200:] + r.stderr[-300:])

# --- 2. mcp_token -----------------------------------------------------------------------------
print("--- 2. mcp_token via the module path ---")
tok_file = os.path.join(TMP, "secrets", "tok")
os.makedirs(os.path.dirname(tok_file), exist_ok=True)
r = run(["-m", "core.manage.mcp_token", "generate", tok_file])
check("generate exits 0", r.returncode == 0, r.stderr[-300:])
token = open(tok_file).read().strip() if os.path.exists(tok_file) else ""
check("generate writes a long token", len(token) >= 32)
check("generate prints only the path", r.stdout.strip() == f"written to {tok_file}" and token not in r.stdout + r.stderr)
if os.name == "posix":
    check("generate makes the file mode 600", (os.stat(tok_file).st_mode & 0o777) == 0o600, oct(os.stat(tok_file).st_mode))
r = run(["-m", "core.manage.mcp_token", "generate", tok_file])
check("generate refuses to overwrite without --force", r.returncode == 1 and open(tok_file).read().strip() == token)
r = run(["-m", "core.manage.mcp_token", "generate", tok_file, "--force"])
check("generate --force rotates it", r.returncode == 0 and open(tok_file).read().strip() != token)
token = open(tok_file).read().strip()

r = run(["-m", "core.manage.mcp_token", "check"],
        env_extra={"CONSTRUCTICON_INSTALL_TOKEN_FILE": tok_file, "CONSTRUCTICON_INSTALL_TOKEN": ""})
check("check reports the local config", r.returncode == 0 and "token configured via" in r.stdout, r.stdout + r.stderr[-300:])
check("check never prints the token", token not in r.stdout + r.stderr)

# Stdlib only: import the generate path and list every module it pulled in that isn't stdlib.
# Modules already loaded at startup (__main__, a site-packages .pth hook such as setuptools'
# _distutils_hack) are not the import's doing, so only what the import ADDS is counted.
code = ("import sys\nbefore = set(sys.modules)\nimport core.manage.mcp_token\n"
        "std = set(sys.stdlib_module_names)\n"
        "bad = sorted(m for m in set(sys.modules) - before if m.split('.')[0] not in std and m.split('.')[0] != 'core')\n"
        "core_mods = sorted(m for m in sys.modules if m.split('.')[0] == 'core')\n"
        "print('NONSTD=' + ','.join(bad))\nprint('CORE=' + ','.join(core_mods))\n")
r = run(["-c", code])
nonstd = next((l[7:] for l in r.stdout.splitlines() if l.startswith("NONSTD=")), "?")
core_mods = next((l[5:] for l in r.stdout.splitlines() if l.startswith("CORE=")), "?")
check("mcp_token imports nothing outside the stdlib", nonstd == "", nonstd + r.stderr[-300:])
check("mcp_token imports only core, core.manage, core.install_token from core",
      set(core_mods.split(",")) == {"core", "core.manage", "core.install_token", "core.manage.mcp_token"}, core_mods)
for init in ("core/__init__.py", "core/manage/__init__.py"):
    tree = ast.parse(open(os.path.join(ROOT, init)).read())
    imports = [n for n in ast.walk(tree) if isinstance(n, (ast.Import, ast.ImportFrom))]
    check(f"{init} imports nothing", not imports, init)

# --- 3. the stubs -----------------------------------------------------------------------------
print("--- 3. the scripts/ stubs ---")
for tool, args in (("reset_password", ["someone", "--generate"]), ("mcp_token", ["generate", os.path.join(TMP, "x")])):
    r = run([os.path.join("scripts", tool + ".py")] + args)
    check(f"scripts/{tool}.py exits non-zero", r.returncode != 0, r.returncode)
    check(f"scripts/{tool}.py names the new command", f"python3 -m core.manage.{tool}" in r.stderr, r.stderr[-300:])
check("the mcp_token stub wrote nothing", not os.path.exists(os.path.join(TMP, "x")))

# --- 4. no live reference to the old paths ------------------------------------------------------
print("--- 4. references ---")
for rel in ("scripts/test_auth_step1.py", "scripts/test_setup_race_648.py"):
    src = open(os.path.join(ROOT, rel)).read()
    check(f"{rel} imports reset_password from core.manage", "from core.manage import reset_password" in src)
MCP_FILES = sorted(os.path.relpath(os.path.join(d, f), ROOT).replace(os.sep, "/")  # #547 step 3: a package now
                   for d, _dirs, fs in os.walk(os.path.join(ROOT, "mcp_server")) for f in fs if f.endswith(".py"))
for rel in ["web/templates/setup.html", "core/users.py", "core/install_token.py"] + MCP_FILES + [
            "docker-compose.yml.example"]:
    src = open(os.path.join(ROOT, rel)).read()
    check(f"{rel} no longer points at scripts/reset_password.py or scripts/mcp_token.py",
          "scripts/reset_password" not in src and "scripts/mcp_token" not in src)
compose = open(os.path.join(ROOT, "docker-compose.yml.example")).read()
mounts = [l.strip() for l in compose.splitlines() if l.strip().startswith("- ./scripts")]
check("docker-compose.yml.example mounts scripts/ into no service", not mounts, mounts)

if FAILS:
    print("FAILED: " + ", ".join(FAILS))
    sys.exit(1)
print("ALL PASS")
