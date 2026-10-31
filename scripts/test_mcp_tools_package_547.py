#!/usr/bin/env python3
"""mcp_server/server.py split into modules (#547 step 3): check the MCP still is the server it was.

Throwaway DB (scripts/_testenv.py), no network: it imports mcp_server.server and inspects it.

  1. The surface is unchanged: tools/list (each tool's name, description, inputSchema, annotations, as the
     SDK dumps it), IN ORDER, every registration in order (the hidden aliases too), ACTION_ROLES (rows and
     order) and the server's instructions equal scripts/data/mcp_tools_547.json, written from the
     pre-split server.py. A tool call still answers (version) and still refuses in the shared shape.
  2. Every name the old module had is still on `mcp_server.server`, with the same kind; the only new
     names are the new modules themselves.
  3. One copy of everything: each module's names are the server's objects, and no function, class or
     constant is defined in two files.
  4. Assigning to the server reaches the code inside the modules: `server._call_as_mcp = fake`
     (test_bulk_visibility_709) is what the wrapper calls, `server._run_in_thread = ...`
     (test_youtube_transcripts_724e) reaches every tool module that imported it. Restoring puts it back.
  5. The wrapper still covers every tool module: every function a tool module decorates with
     `@mcp.tool(...)` is registered through it (and nothing else registers tools), the startup check
     refuses a tool registered around it, and ACTION_ROLES covers every tool and selector value.
  6. Layout: each module imports only from the ones before it in the load order (no cycle), a tool
     module never imports another tool module, no new lazy import, server.py defines no tool, and no file
     has a doubled carriage return.

    python scripts/test_mcp_tools_package_547.py

Prints "ALL PASS", or "N FAILED: <names>" listing every failing check by name.
"""
import ast
import asyncio
import inspect
import json
import sys
import types
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import _testenv  # noqa: E402

TMP = _testenv.isolate("mcp-tools-package-547-")
ROOT = HERE.parent
sys.path.insert(0, str(ROOT))
try:
    import cairosvg  # noqa: F401
except OSError as exc:  # a bare Windows Python has no libcairo; nothing here renders an SVG
    print(f"NOTE: cairosvg can't load here ({str(exc).splitlines()[0][:80]}); using a stub module")
    sys.modules["cairosvg"] = types.ModuleType("cairosvg")
from core import db  # noqa: E402
from mcp_server import server  # noqa: E402  (sets CONSTRUCTICON_ROLE=mcp)

db.init_db()
FAILS = []


def check(name, cond, extra=""):
    print(("PASS " if cond else "FAIL ") + name + (f"  [{str(extra)[:600]}]" if (extra and not cond) else ""))
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


PKG = ROOT / "mcp_server"
SNAP = json.loads((HERE / "data" / "mcp_tools_547.json").read_text(encoding="utf-8"))
# The load order (mcp_server/server.py imports them in it; tools/ in tools/__init__.py's order).
SHARED = ["_boot", "app", "action_roles", "wrapper", "shapes", "_common", "listing"]
TOOL_MODULES = [m.__name__.rsplit(".", 1)[1] for m in server.tools.MODULES]
ORDER = SHARED + [f"tools.{t}" for t in TOOL_MODULES] + ["checks"]
MODULES = [sys.modules[f"mcp_server.{m}"] for m in ORDER]

# ---- 1. the surface ------------------------------------------------------------------------------
print("--- 1. tools/list, registrations, ACTION_ROLES: as before the split ---")
listed = asyncio.run(server.mcp.list_tools())
now = [t.model_dump(mode="json", by_alias=True, exclude_none=True) for t in listed]
check(f"tools/list has the same {len(SNAP['tools'])} tools in the same order",
      [t["name"] for t in now] == [t["name"] for t in SNAP["tools"]],
      [t["name"] for t in now])
differ = [b["name"] for a, b in zip(now, SNAP["tools"]) if a != b]
check("each tool is identical (description, inputSchema, annotations, ...)", not differ and len(now) == len(SNAP["tools"]),
      differ)
for field in ("description", "inputSchema"):
    check(f"every {field} is byte-identical", [t.get(field) for t in now] == [t.get(field) for t in SNAP["tools"]])
registered = [t.name for t in server.mcp._tool_manager.list_tools()]
check(f"every registration ({len(SNAP['registered'])}, hidden aliases included) in the same order",
      registered == SNAP["registered"], [(i, a, b) for i, (a, b) in enumerate(zip(registered, SNAP["registered"])) if a != b][:5])
check("ACTION_ROLES: the same rows in the same order",
      [[k[0], k[1], v] for k, v in server.ACTION_ROLES.items()] == SNAP["action_roles"])
check("the server's instructions are unchanged", server.mcp.instructions == SNAP["instructions"])
answer = asyncio.run(server.mcp.call_tool("constructicon_version", {}))
check("a tool call answers through the SDK (version)", not getattr(answer, "is_error", False), answer)
refused = server.constructicon_get("no-such-slug-547")
check("a refusal keeps the shared shape (get of a missing slug -> not_found)",
      isinstance(refused, dict) and refused.get("ok") is False and refused["error"]["code"] == "not_found", refused)

# ---- 2. names ------------------------------------------------------------------------------------
print("--- 2. every old name on mcp_server.server, same kind; nothing new but the modules ---")
names = {n for n in vars(server) if not (n.startswith("__") and n.endswith("__"))}
missing = sorted(set(SNAP["names"]) - names)
check(f"all {len(SNAP['names'])} old names are on mcp_server.server", not missing, missing)
wrong = sorted(n for n in SNAP["names"] if n in names and kind(getattr(server, n)) != SNAP["names"][n])
check("each keeps its kind", not wrong, [(n, SNAP["names"][n], kind(getattr(server, n))) for n in wrong])
new_names = names - set(SNAP["names"])
expected_new = set(SHARED) | {"tools", "checks"}
check("the only new names are the modules", new_names == expected_new, sorted(new_names ^ expected_new))
check("the tool functions are the wrapper's direct callables, as before",
      all(getattr(getattr(server, n), "alias_of", "missing") != "missing" for n in server._TOOL_FUNCS))

# ---- 3. one copy ---------------------------------------------------------------------------------
print("--- 3. one copy of every name ---")
split = [f"{m.__name__}.{n}" for m in MODULES for n, v in vars(m).items()
         if not (n.startswith("__") and n.endswith("__")) and getattr(server, n, None) is not v]
check("every module's names are the server's objects", not split, split[:10])
defined_in = {}
for p in sorted(PKG.rglob("*.py")):
    if "__pycache__" in p.parts or p.name in ("auth.py",):
        continue
    for node in ast.parse(p.read_text(encoding="utf-8")).body:
        targets = []
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            targets = [node.name]
        elif isinstance(node, ast.Assign):
            targets = [t.id for t in node.targets if isinstance(t, ast.Name)]
        for t in targets:
            defined_in.setdefault(t, []).append(p.relative_to(ROOT).as_posix())
twice = {n: f for n, f in defined_in.items() if len(f) > 1}
check("no function, class or constant is defined in two files", not twice, twice)
check("one MCPServer, one wrapper table",
      server.mcp is server.app.mcp is server.wrapper.mcp and server._WRAPPED_TOOLS is server.checks._WRAPPED_TOOLS)

# ---- 4. assignment reaches the modules -----------------------------------------------------------
print("--- 4. server.X = ... reaches the code inside the modules ---")
real_call = server._call_as_mcp
seen = []


def fake_call(fn, args, kwargs, need_role=None):
    seen.append((fn.__name__, need_role))
    return True, "patched"


server._call_as_mcp = fake_call
try:
    check("server._call_as_mcp = ... rebinds it in wrapper.py, where the decorator calls it",
          server.wrapper._call_as_mcp is fake_call)
    out = server.constructicon_version()
    check("a tool then runs through the patched _call_as_mcp (test_bulk_visibility_709's pattern)",
          out == "patched" and seen == [("constructicon_version", "viewer")], (out, seen))
finally:
    server._call_as_mcp = real_call
check("restoring _call_as_mcp restores it in wrapper.py", server.wrapper._call_as_mcp is real_call)

real_rit = server._run_in_thread
holders = [m.__name__ for m in MODULES if vars(m).get("_run_in_thread") is real_rit]


def fake_rit(fn, *a):
    return None


server._run_in_thread = fake_rit
check("server._run_in_thread = ... (test_youtube_transcripts_724e) reaches every module holding it (3+)",
      len(holders) >= 3 and all(vars(sys.modules[h])["_run_in_thread"] is fake_rit for h in holders), holders)
server._run_in_thread = real_rit
check("... and is restored", all(vars(sys.modules[h])["_run_in_thread"] is real_rit for h in holders))

real_tp = server._to_public
holders = [m.__name__ for m in MODULES if vars(m).get("_to_public") is real_tp]
server._to_public = fake_rit
check("a patched shaper reaches every tool module that uses it (8+)",
      len(holders) >= 8 and all(vars(sys.modules[h])["_to_public"] is fake_rit for h in holders), holders)
server._to_public = real_tp
check("... and is restored", all(vars(sys.modules[h])["_to_public"] is real_tp for h in holders))
server.brand_new_name_547 = 1
check("a new attribute just lands on the server", server.brand_new_name_547 == 1
      and not any("brand_new_name_547" in vars(m) for m in MODULES))
del server.brand_new_name_547

# ---- 5. the wrapper covers every tool module -----------------------------------------------------
print("--- 5. every tool through the wrapper; ACTION_ROLES complete ---")
decorated = {}
for t in TOOL_MODULES:
    tree = ast.parse((PKG / "tools" / f"{t}.py").read_text(encoding="utf-8"))
    for node in tree.body:
        if isinstance(node, ast.FunctionDef) and any(
                isinstance(d, ast.Call) and isinstance(d.func, ast.Attribute) and d.func.attr == "tool"
                and isinstance(d.func.value, ast.Name) and d.func.value.id == "mcp" for d in node.decorator_list):
            decorated[node.name] = t
    src = (PKG / "tools" / f"{t}.py").read_text(encoding="utf-8")
    check(f"tools/{t}.py registers nothing around the wrapper (no add_tool, _sdk_tool)",
          "add_tool" not in src and "_sdk_tool" not in src)
check(f"every `@mcp.tool` function of the tool modules ({len(decorated)}) went through the wrapper, and no other",
      set(decorated) == set(server._TOOL_FUNCS), sorted(set(decorated) ^ set(server._TOOL_FUNCS)))
wrong_home = sorted(n for n, t in decorated.items() if server._TOOL_FUNCS[n].__module__ != f"mcp_server.tools.{t}")
check("each tool function lives in its tool module", not wrong_home, wrong_home)
check("every registration is the wrapper's function",
      all(server._WRAPPED_TOOLS.get(t.name) is t.fn for t in server.mcp._tool_manager.list_tools()))
try:
    server._check_every_tool_wrapped()
    server._check_action_roles()
    ok = True
except RuntimeError as exc:
    ok = exc
check("the startup checks pass", ok is True, ok)
server._sdk_tool(name="constructicon_bypass_547")(lambda: 1)
try:
    server._check_every_tool_wrapped()
    caught = None
except RuntimeError as exc:
    caught = str(exc)
finally:
    server.mcp._tool_manager._tools.pop("constructicon_bypass_547", None)
check("... and refuse a tool registered around the wrapper", caught and "constructicon_bypass_547" in caught, caught)
saved = server.ACTION_ROLES.pop(("constructicon_version", None))
try:
    server._check_action_roles()
    caught = None
except RuntimeError as exc:
    caught = str(exc)
finally:
    server.ACTION_ROLES[("constructicon_version", None)] = saved
check("... and refuse a tool with no ACTION_ROLES row", caught and "constructicon_version" in caught, caught)
server.ACTION_ROLES.clear()
server.ACTION_ROLES.update({(r[0], r[1]): r[2] for r in SNAP["action_roles"]})
check("ACTION_ROLES restored (same rows, same order)",
      [[k[0], k[1], v] for k, v in server.ACTION_ROLES.items()] == SNAP["action_roles"])

# ---- 6. layout -----------------------------------------------------------------------------------
print("--- 6. load order, no cycle, no lazy import, no doubled CR ---")
init = ast.parse((PKG / "tools" / "__init__.py").read_text(encoding="utf-8"))
listed_order = next([a.name for a in n.names] for n in init.body if isinstance(n, ast.ImportFrom) and n.level == 1)
check("tools/__init__.py imports every tool module file, in MODULES' order",
      listed_order == TOOL_MODULES and set(TOOL_MODULES) == {p.stem for p in (PKG / "tools").glob("*.py")} - {"__init__"},
      (listed_order, TOOL_MODULES))


def mcp_imports(path):
    """(module imported from mcp_server, line, top level?) for each import of an mcp_server module."""
    out = []
    tree = ast.parse(path.read_text(encoding="utf-8"))
    top = {id(n) for n in tree.body}
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module and node.module.split(".")[0] == "mcp_server":
            if node.module == "mcp_server":
                mods = [a.name for a in node.names]
            elif node.module == "mcp_server.tools":
                mods = ["tools"]
            else:
                mods = [node.module[len("mcp_server."):]]
            out += [(m, node.lineno, id(node) in top) for m in mods]
    return out


upward, lazy, tool_to_tool = [], [], []
for i, name in enumerate(ORDER):
    path = PKG / (name.replace(".", "/") + ".py")
    for mod, line, top in mcp_imports(path):
        if not top:
            if mod != "auth":  # the two `from mcp_server import auth` in wrapper.py predate the split
                lazy.append(f"{name}:{line} {mod}")
            continue
        if mod == "tools" and name == "checks":
            continue  # checks imports the tools package so every tool is registered before it runs
        if mod not in ORDER[:i]:
            upward.append(f"{name}:{line} imports {mod}")
        if name.startswith("tools.") and mod.startswith("tools."):
            tool_to_tool.append(f"{name}:{line} imports {mod}")
check("each module imports only from the ones before it", not upward, upward)
check("no tool module imports another tool module", not tool_to_tool, tool_to_tool)
check("no lazy import of an MCP module (only wrapper.py's old `from mcp_server import auth`)", not lazy, lazy)
lazy_auth = [(n, l) for n in ORDER for m, l, top in mcp_imports(PKG / (n.replace(".", "/") + ".py")) if m == "auth" and not top]
check("wrapper.py keeps its two lazy auth imports and nothing else does", [n for n, _ in lazy_auth] == ["wrapper", "wrapper"],
      lazy_auth)
server_tree = ast.parse((PKG / "server.py").read_text(encoding="utf-8"))
check("server.py defines no tool (it is the entry point; the tools are in mcp_server/tools/)",
      not any(isinstance(n, ast.FunctionDef) and n.name.startswith("constructicon_") for n in server_tree.body))
check("nothing in the SHARED modules registers a tool",
      not any(isinstance(n, ast.FunctionDef) and n.decorator_list and "constructicon_" in n.name
              for s in SHARED for n in ast.parse((PKG / f"{s}.py").read_text(encoding="utf-8")).body))
crlf_bad = [p.relative_to(ROOT).as_posix() for p in list(PKG.rglob("*.py")) + [HERE / "data" / "mcp_tools_547.json",
                                                                               Path(__file__)]
            if "__pycache__" not in p.parts and b"\r\r\n" in p.read_bytes()]
check("no doubled carriage returns", not crlf_bad, crlf_bad)

print()
print("ALL PASS" if not FAILS else f"{len(FAILS)} FAILED: {FAILS}")
sys.exit(1 if FAILS else 0)
