#!/usr/bin/env python3
"""core/db is a package now (#547 step 1): check it still behaves like the one module it was.

No server and no DB file: it imports core.db and inspects it.

  1. Every name the old core/db.py had (scripts/data/db_names_547.txt, written from the pre-move
     module) is still on `core.db`, with the same kind (function, class, module, value), and the only
     new names are the submodules themselves.
  2. One copy of everything: wherever two submodules bind one name, it is the same object.
  3. Assigning to the package reaches the code inside it (tests set `db.DB_PATH` and monkeypatch
     `db.get_conn`, `db._ensure_chip_triggers`, ...): the submodules see the new value, and a db
     function that calls the patched one internally runs the patch. Restoring puts everything back.
  4. The submodules import only from the ones before them in __init__'s list (no cycle), and the
     old core/db.py file is gone (a second copy would shadow nothing but confuse every reader).

    python scripts/test_db_package_547.py

Prints "ALL PASS", or "FAILED: <names>" listing every failing check by name.
"""
import ast
import inspect
import sys
import types
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import _testenv  # noqa: E402

TMP = _testenv.isolate("db-package-547-")
sys.path.insert(0, str(HERE.parent))
from core import db  # noqa: E402

FAILS = []


def check(name, cond, extra=""):
    print(("PASS " if cond else "FAIL ") + name + (f"  [{extra}]" if (extra and not cond) else ""))
    if not cond:
        FAILS.append(name)


def kind(v):
    if isinstance(v, types.ModuleType):
        return "module"
    if inspect.isclass(v):
        return "class"
    if callable(v):
        return "callable"
    return "value"


PKG_DIR = HERE.parent / "core" / "db"
SUBMODULES = [m for m in vars(db).values() if isinstance(m, types.ModuleType) and m.__name__.startswith("core.db.")]
SUB_NAMES = {m.__name__.rsplit(".", 1)[1] for m in SUBMODULES}

# ---- 1. the names ----------------------------------------------------------------------------
print("--- 1. every old name, same kind; nothing new but the submodules ---")
old = {}
for line in (HERE / "data" / "db_names_547.txt").read_text(encoding="utf-8").splitlines():
    if line and not line.startswith("#"):
        name, k = line.split("\t")
        old[name] = k
now = {n for n in vars(db) if not (n.startswith("__") and n.endswith("__"))}
missing = sorted(set(old) - now)
check(f"all {len(old)} old names are on core.db", not missing, missing)
wrong_kind = sorted(n for n in old if n in now and kind(getattr(db, n)) != old[n])
check("each keeps its kind", not wrong_kind, [(n, old[n], kind(getattr(db, n))) for n in wrong_kind])
extra = now - set(old)
check("the only new names are the submodules", extra == SUB_NAMES, sorted(extra ^ SUB_NAMES))
check("the package lists its submodules (19+)", len(SUBMODULES) >= 19, sorted(SUB_NAMES))
check("`from core.db import X` works for a private name",
      __import__("core.db", fromlist=["_ensure_chip_triggers"])._ensure_chip_triggers is db._ensure_chip_triggers)

# ---- 2. one copy -----------------------------------------------------------------------------
print("--- 2. one copy of every name ---")
split = []
for mod in SUBMODULES:
    for name, value in vars(mod).items():
        if name.startswith("__") and name.endswith("__"):
            continue
        if getattr(db, name, None) is not value:
            split.append(f"{mod.__name__}.{name}")
check("every submodule's names are the package's objects", not split, split[:10])
holders = [m.__name__ for m in SUBMODULES if "_ambient" in vars(m)]
check("one ambient transaction state (_ambient), shared", len({id(vars(m)["_ambient"]) for m in SUBMODULES
                                                            if "_ambient" in vars(m)}) == 1, holders)
defined_in = {}
for p in sorted(PKG_DIR.glob("*.py")):
    if p.name == "__init__.py":
        continue
    tree = ast.parse(p.read_text(encoding="utf-8"))
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.ClassDef)):
            defined_in.setdefault(node.name, []).append(p.name)
twice = {n: f for n, f in defined_in.items() if len(f) > 1}
check("no function or class is defined in two submodules", not twice, twice)

# ---- 3. assignment reaches the submodules ----------------------------------------------------
print("--- 3. db.X = ... reaches the code inside the package ---")
saved_path = db.DB_PATH
db.DB_PATH = Path(TMP) / "elsewhere.db"
holders = [m for m in SUBMODULES if "DB_PATH" in vars(m)]
check("db.DB_PATH = ... is what every submodule reads", holders and all(vars(m)["DB_PATH"] == db.DB_PATH for m in holders),
      [m.__name__ for m in holders])
check("get_conn opens the new path", str(db.DB_PATH) in str(db.conn.DB_PATH))
db.DB_PATH = saved_path
check("restoring DB_PATH restores it everywhere", all(vars(m)["DB_PATH"] is saved_path for m in holders))


class _Sentinel(Exception):
    pass


def _boom(*a, **k):
    raise _Sentinel()


real_get_conn = db.get_conn
db.get_conn = _boom
try:
    raised = False
    try:
        db.get_setting("anything")  # settings.py calls get_conn() internally
    except _Sentinel:
        raised = True
    check("a patched db.get_conn is what db.get_setting calls", raised)
    raised = False
    try:
        db.search(query="x")  # search_index.py
    except _Sentinel:
        raised = True
    check("... and db.search", raised)
finally:
    db.get_conn = real_get_conn
check("restoring get_conn restores it in every submodule",
      all(vars(m).get("get_conn", real_get_conn) is real_get_conn for m in SUBMODULES))

real_chip = db._ensure_chip_triggers
db._ensure_chip_triggers = _boom
check("a patched private helper reaches schema.py (init_db's caller of it)", db.schema._ensure_chip_triggers is _boom)
db._ensure_chip_triggers = real_chip
check("... and is restored", db.schema._ensure_chip_triggers is real_chip and db.tags._ensure_chip_triggers is real_chip)

db.brand_new_name_547 = 1
check("a new attribute just lands on the package", db.brand_new_name_547 == 1)
del db.brand_new_name_547

# ---- 4. layout -------------------------------------------------------------------------------
print("--- 4. no cycle, no old file ---")
order = []
init_tree = ast.parse((PKG_DIR / "__init__.py").read_text(encoding="utf-8"))
for node in init_tree.body:
    if isinstance(node, ast.ImportFrom) and node.level == 1 and node.module is None:
        order = [a.name for a in node.names]
check("__init__ imports every submodule file", set(order) == {p.stem for p in PKG_DIR.glob("*.py")} - {"__init__"},
      order)
upward = []
for i, name in enumerate(order):
    tree = ast.parse((PKG_DIR / f"{name}.py").read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.level == 1:
            src = node.module or ""
            if src not in order[:i]:
                upward.append(f"{name} imports from .{src}")
check("each submodule imports only from the ones before it", not upward, upward)
check("core/db.py is gone (the package is the only copy)", not (HERE.parent / "core" / "db.py").exists())
crlf_bad = [p.name for p in PKG_DIR.glob("*.py") if b"\r\r\n" in p.read_bytes()]
check("no doubled carriage returns in the package", not crlf_bad, crlf_bad)

print()
print("ALL PASS" if not FAILS else f"{len(FAILS)} FAILED: {FAILS}")
sys.exit(1 if FAILS else 0)
