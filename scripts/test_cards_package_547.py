#!/usr/bin/env python3
"""core/cards is a package now (#547 step 2): check it still behaves like the one module it was.

No server and no DB file: it imports core.cards and inspects it.

  1. Every name the old core/cards.py had (scripts/data/cards_names_547.txt, written from the pre-move
     module) is still on `core.cards`, with the same kind (function, class, module, value), and the
     only new names are the submodules themselves.
  2. One copy of everything: wherever two submodules bind one name, it is the same object.
  3. Assigning to the package reaches the code inside it (test_async_blocking_550 sets
     `cards.update = slow_update`): the submodules see the new value, and a cards function that calls
     the patched one internally runs the patch. Restoring puts everything back.
  4. The submodules import only from the ones before them in __init__'s list (no cycle), and the
     old core/cards.py file is gone (a second copy would shadow nothing but confuse every reader).

    python scripts/test_cards_package_547.py

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

TMP = _testenv.isolate("cards-package-547-")
sys.path.insert(0, str(HERE.parent))
from core import cards  # noqa: E402
from core import items as _items, membership as _membership  # noqa: E402,F401 (they import Result from cards)

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


PKG_DIR = HERE.parent / "core" / "cards"
SUBMODULES = [m for m in vars(cards).values() if isinstance(m, types.ModuleType)
              and m.__name__.startswith("core.cards.")]
SUB_NAMES = {m.__name__.rsplit(".", 1)[1] for m in SUBMODULES}

# ---- 1. the names ----------------------------------------------------------------------------
print("--- 1. every old name, same kind; nothing new but the submodules ---")
old = {}
for line in (HERE / "data" / "cards_names_547.txt").read_text(encoding="utf-8").splitlines():
    if line and not line.startswith("#"):
        name, k = line.split("\t")
        old[name] = k
now = {n for n in vars(cards) if not (n.startswith("__") and n.endswith("__"))}
missing = sorted(set(old) - now)
check(f"all {len(old)} old names are on core.cards", not missing, missing)
wrong_kind = sorted(n for n in old if n in now and kind(getattr(cards, n)) != old[n])
check("each keeps its kind", not wrong_kind, [(n, old[n], kind(getattr(cards, n))) for n in wrong_kind])
extra = now - set(old)
check("the only new names are the submodules", extra == SUB_NAMES, sorted(extra ^ SUB_NAMES))
check("the package lists its submodules (14)", len(SUBMODULES) == 14, sorted(SUB_NAMES))
check("no submodule is named after a cards function (bulk, undo, ...)", not (SUB_NAMES & set(old)),
      sorted(SUB_NAMES & set(old)))
check("`from core.cards import X` works for a private name",
      __import__("core.cards", fromlist=["_changes_from_log"])._changes_from_log is cards._changes_from_log)
check("the module-level imports are still core's modules",
      cards.db is sys.modules["core.db"] and cards.policy is sys.modules["core.policy"]
      and cards.CardError is sys.modules["core.card_rules"].CardError)

# ---- 2. one copy -----------------------------------------------------------------------------
print("--- 2. one copy of every name ---")
split = []
for mod in SUBMODULES:
    for name, value in vars(mod).items():
        if name.startswith("__") and name.endswith("__"):
            continue
        if getattr(cards, name, None) is not value:
            split.append(f"{mod.__name__}.{name}")
check("every submodule's names are the package's objects", not split, split[:10])
defined_in = {}
for p in sorted(PKG_DIR.glob("*.py")):
    if p.name == "__init__.py":
        continue
    tree = ast.parse(p.read_text(encoding="utf-8"))
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.ClassDef)):
            defined_in.setdefault(node.name, []).append(p.name)
        elif isinstance(node, ast.Assign):
            for t in node.targets:
                if isinstance(t, ast.Name):
                    defined_in.setdefault(t.id, []).append(p.name)
twice = {n: f for n, f in defined_in.items() if len(f) > 1}
check("no function, class or constant is defined in two submodules", not twice, twice)
check("one Result class (every service module's `from .cards import Result` is it)",
      _membership.Result is cards.Result and _items.Result is cards.Result)

# ---- 3. assignment reaches the submodules ----------------------------------------------------
print("--- 3. cards.X = ... reaches the code inside the package ---")


class _Sentinel(Exception):
    pass


def _boom(*a, **k):
    raise _Sentinel()


real_get_card = cards.get_card
holders = [m.__name__ for m in SUBMODULES if vars(m).get("get_card") is real_get_card]
cards.get_card = _boom
try:
    check("cards.get_card = ... rebinds it in every submodule that used it (8+)",
          len(holders) >= 8 and all(vars(sys.modules[h])["get_card"] is _boom for h in holders), holders)
    for label, call in [("set_status (ops.py)", lambda: cards.set_status(1, "in_progress")),
                        ("link (links.py)", lambda: cards.link(1, 2, "related")),
                        ("explain_card (explain.py)", lambda: cards.explain_card(1)),
                        ("nest (families.py)", lambda: cards.nest(1, 2))]:
        raised = False
        try:
            call()
        except _Sentinel:
            raised = True
        check(f"a patched cards.get_card is what cards.{label} calls", raised)
finally:
    cards.get_card = real_get_card
check("restoring get_card restores it in every submodule",
      all(vars(m).get("get_card", real_get_card) is real_get_card for m in SUBMODULES))

real_update = cards.update
cards.update = _boom
check("cards.update = ... (test_async_blocking_550) reaches edit.py, where set_cover calls it",
      cards.edit.update is _boom)
cards.update = real_update
check("... and is restored", cards.edit.update is real_update)

real_cfl = cards._changes_from_log
holders = [m.__name__ for m in SUBMODULES if vars(m).get("_changes_from_log") is real_cfl]
cards._changes_from_log = _boom
check("a patched private helper reaches every submodule holding it",
      len(holders) >= 4 and all(vars(sys.modules[h])["_changes_from_log"] is _boom for h in holders), holders)
cards._changes_from_log = real_cfl
check("... and is restored", all(vars(sys.modules[h])["_changes_from_log"] is real_cfl for h in holders))

cards.brand_new_name_547 = 1
check("a new attribute just lands on the package", cards.brand_new_name_547 == 1)
del cards.brand_new_name_547

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
lazy_pkg = []
for name in order:
    tree = ast.parse((PKG_DIR / f"{name}.py").read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.col_offset > 0 and node.level == 1:
            lazy_pkg.append(f"{name}:{node.lineno}")
check("no lazy import of a sibling submodule (the lazy ones reach core, `from .. import`)", not lazy_pkg, lazy_pkg)
check("core/cards.py is gone (the package is the only copy)", not (HERE.parent / "core" / "cards.py").exists())
crlf_bad = [p.name for p in PKG_DIR.glob("*.py") if b"\r\r\n" in p.read_bytes()]
check("no doubled carriage returns in the package", not crlf_bad, crlf_bad)

print()
print("ALL PASS" if not FAILS else f"{len(FAILS)} FAILED: {FAILS}")
sys.exit(1 if FAILS else 0)
