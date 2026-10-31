"""The startup checks (#560/#548, #746) (#547 step 3; moved verbatim from server.py).

Importing this module (mcp_server/server.py does, after every tool module) refuses to start when a
registered tool bypassed the wrapper, or when ACTION_ROLES doesn't cover every tool and selector
value exactly.
"""

from mcp_server import tools  # noqa: F401  (every tool is registered before the checks run)
from mcp_server.app import mcp
from mcp_server.action_roles import ACTION_ROLES
from mcp_server.wrapper import _ALIAS_OF, _SELECTORS, _TOOL_FUNCS, _WRAPPED_TOOLS
from mcp_server._common import RESERVED_ACTION

from core import object_types


def _check_every_tool_wrapped():
    """#560/#548: refuse to start if any registered tool bypassed the actor/error wrapper
    (e.g. registered via mcp.add_tool or the SDK's original decorator)."""
    manager = getattr(mcp, "_tool_manager", None)
    if manager is None:  # SDK internals moved: nothing to check against
        return
    unwrapped = [t.name for t in manager.list_tools() if _WRAPPED_TOOLS.get(t.name) is not t.fn]
    if unwrapped:
        raise RuntimeError(f"MCP tools registered without the actor/error wrapper: {unwrapped}")


def _check_action_roles():
    """#746: refuse to start unless every visible tool (and each value of its selector) has exactly
    the ACTION_ROLES rows it needs, every alias stands for a listed (tool, action), and no object
    type declares the built-in run_type_action key."""
    problems = []
    expected = set()
    for name in _TOOL_FUNCS:
        if name in _ALIAS_OF:
            continue
        sel = _SELECTORS.get(name)
        expected |= {(name, v) for v in sel[1]} if sel else {(name, None)}
    problems += [f"no ACTION_ROLES row for {k}" for k in sorted(expected - set(ACTION_ROLES), key=str)]
    problems += [f"stale ACTION_ROLES row {k}" for k in sorted(set(ACTION_ROLES) - expected, key=str)]
    problems += [f"alias {a} stands for {t}, which has no ACTION_ROLES row"
                 for a, t in _ALIAS_OF.items() if t not in ACTION_ROLES]
    for spec in object_types.OBJECT_TYPES.values():
        if any(a.key == RESERVED_ACTION for a in spec.actions):
            problems.append(f"type {spec.label!r} declares the reserved action {RESERVED_ACTION!r}")
    if problems:
        raise RuntimeError("MCP tool roles (#746): " + "; ".join(problems))


_check_every_tool_wrapped()
_check_action_roles()
