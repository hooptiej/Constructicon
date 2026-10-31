"""The one `@mcp.tool()` wrapper around every tool (#560 actor, #548 errors, #700 names, #746 aliases
and roles) (#547 step 3; moved verbatim from server.py).

Importing this module installs `_tool` over MCPServer.tool and the alias-hiding tools/list, so every
tool module registers through it (mcp_server/checks.py refuses to start otherwise).
"""

import functools
import inspect
import json
import logging
import typing

import pydantic
from mcp.types import CallToolResult, TextContent

from mcp_server.app import STARTED_LABEL, _ALIAS_LABELS, _HIDDEN_ALIASES, mcp, tool_description_for, tool_name_for
from mcp_server.action_roles import role_for

from core import actor as actor_ctx, roles, errors, hobby_label as hobby_label_rules


# --- One wrapper around every tool (#560 actor, #548 errors) ---
# `@mcp.tool()` below is this module's own decorator (installed over MCPServer.tool), so no
# tool can be registered without it:
#   * the tool body runs inside actor.acting_as("mcp"), so every core write it makes is
#     recorded with actor "mcp" without passing a literal;
#   * a refusal (core.errors.AppError: CardError, QueueError, decisions.*, NotFound, ...,
#     or a ValueError / pydantic ValidationError raised as input validation) becomes
#     {"ok": false, "error": {"code", "message"[, "details"]}}. Over MCP that payload is
#     returned as an isError tool result (text = the JSON, structuredContent = the dict);
#     a direct Python call of the tool function returns the dict itself.
#   * Not found: a getter or setter whose target doesn't exist returns the error shape with
#     code "not_found" (never None/False). Successful returns are unchanged.
#   * Anything else (a real bug) propagates, and the SDK reports it as an internal error.
# Argument-schema validation (a wrong type for a parameter) happens inside the SDK before the
# tool body runs, so it keeps the SDK's own isError text.

def _error_payload(exc):
    """The shared error payload for a refusal, or None for an unexpected exception."""
    if isinstance(exc, errors.AppError):
        return errors.to_payload(exc)
    if isinstance(exc, pydantic.ValidationError):
        return errors.error_body("validation_error", str(exc))
    if isinstance(exc, ValueError):
        return errors.error_body("bad_request", str(exc))
    return None


def _request_actor():
    """#604 step 5: who this call runs as. A request that came with a per-user MCP token runs as
    that user (`user:<name>`: their role, what they may see, the change log names them); the install
    token (and a direct in-process call) runs as `mcp` (admin)."""
    from mcp_server import auth as mcp_auth  # stdlib-only module, imported here to keep startup order
    ident = mcp_auth.current_identity()
    return ident["actor"] if ident else actor_ctx.ACTOR_MCP


def _call_as_mcp(fn, args, kwargs, need_role=None):
    """(True, result) or (False, error payload). `need_role` (#746, ACTION_ROLES): the role the
    caller must hold for this (tool, action); checked first, as the actor the call runs as."""
    from mcp_server import auth as mcp_auth  # stdlib-only module
    ident = mcp_auth.current_identity()
    # #560 / #778: the channel is "mcp" whoever the caller is; a personal key's id rides along so what
    # the call makes is credited "<user>'s <token name>" (core/actor.acting_via, core/mcp_tokens.credit).
    with actor_ctx.acting_as(_request_actor()),             actor_ctx.acting_via(actor_ctx.CHANNEL_MCP, ident.get("token_id") if ident else None):
        try:
            if need_role is not None:
                _require_role(need_role)
            return True, fn(*args, **kwargs)
        except Exception as exc:  # noqa: BLE001 -- re-raised below unless it's a refusal
            payload = _error_payload(exc)
            if payload is None:
                raise
            return False, payload


def _require_role(need):
    """#746: refuse (403 forbidden) unless the acting actor holds `need` or above. The install token
    runs as `mcp` (admin), so it is never refused; a per-user token (#604 step 5) runs as its user."""
    who = actor_ctx.current_actor()
    have = roles.role_of(who)
    if not roles.at_least(have, need):
        raise errors.AppError("forbidden", f"Nothing was done: this needs the {need} role, and {who} has {have}. "
                                           f"Ask an admin to change your role, or use a token of someone who has it.",
                              status=403, details={"need": need, "have": have})  # #779: the role, machine-readable


_sdk_tool = mcp.tool
_WRAPPED_TOOLS = {}  # registered tool name (aliases included) -> the wrapped function
_TOOL_FUNCS = {}     # tool function name -> the undecorated function (visible tools and aliases)
_SELECTORS = {}      # tool function name -> (how to read its action from the call, the allowed values)
_ALIAS_OF = {}       # #746: retired tool function name -> (the tool it stands for, its action or None)
_alias_log = logging.getLogger("constructicon.mcp.alias")


def tool_registrations(name, doc, label=None, hidden_all=False):
    """#700: [(registered name, description, hidden)] for one tool function under `label` (default:
    the label this process started with): the visible tool, then its hidden aliases (the original
    name and each earlier label's name, when they differ). `description` is the docstring with its
    indentation removed (#746), relabelled under a non-default label. `hidden_all` (#746): every
    registration hidden (a retired name). Pure: the tests build the whole list with any label."""
    label = STARTED_LABEL if label is None else label
    visible = tool_name_for(name, label)
    out = [(visible, tool_description_for(doc, label), hidden_all)]
    seen = {visible}
    for old in [hobby_label_rules.DEFAULT] + _ALIAS_LABELS:
        alias = tool_name_for(name, old)
        if alias not in seen:
            seen.add(alias)
            out.append((alias, out[0][1], True))
    return out


def _selector_values(fn, param):
    """The allowed values of a selector parameter, from its Literal[...] annotation."""
    hint = typing.get_type_hints(fn).get(param)
    for part in (typing.get_args(hint) if typing.get_origin(hint) not in (typing.Literal, None) else (hint,)):
        if typing.get_origin(part) is typing.Literal:
            return tuple(typing.get_args(part))
    raise RuntimeError(f"{fn.__name__}: selector {param!r} needs a Literal[...] annotation")


def _action_of(name, sig, args, kwargs):
    """The action a call asks for (None for a tool without a selector), read from its arguments."""
    how = _SELECTORS.get(name)
    if how is None:
        return None
    try:
        bound = sig.bind_partial(*args, **kwargs)
    except TypeError:  # silent-ok: a bad call; the tool itself refuses it, the role check fails closed
        return "?"
    bound.apply_defaults()
    reader = how[0]
    return reader(bound.arguments) if callable(reader) else bound.arguments.get(reader)


def _empty_list_visible(value):
    """#746: tools/list carries no outputSchema, so a result reaches the client as text only. The
    SDK turns a list into one text block per entry, which makes an empty list NO content at all;
    send "[]" instead so "nothing found" is visible."""
    if isinstance(value, list) and not value:
        return CallToolResult(content=[TextContent(type="text", text="[]")])
    return value


def _tool(*dargs, alias_of=None, selector=None, **dkwargs):
    """This module's `@mcp.tool()` (#560, #548, #700, #746).
    alias_of=(tool, action): a RETIRED name. Every registration of it is hidden (tools/call answers,
      tools/list doesn't), each call is logged ("old -> new"), and its role is that call's.
    selector: the parameter naming the action (its values from its Literal annotation), or
      (function(bound arguments) -> action, values) when the action is derived (delete's target).
    No tool publishes an outputSchema (#746, structured_output=False): results are the JSON text."""
    def decorate(fn):
        name = fn.__name__
        sig = inspect.signature(fn)
        _TOOL_FUNCS[name] = fn
        if selector is not None:
            _SELECTORS[name] = (selector, _selector_values(fn, selector)) if isinstance(selector, str) else selector
        if alias_of is not None:
            _ALIAS_OF[name] = alias_of

        def run(args, kwargs):
            if alias_of is not None:
                target, act = alias_of
                _alias_log.warning("MCP alias call: %s -> %s%s (deprecated name, #746)", name, target,
                                   f" [{act}]" if act else "")
                need = role_for(*alias_of)
            else:
                need = role_for(name, _action_of(name, sig, args, kwargs))
            return _call_as_mcp(fn, args, kwargs, need)

        @functools.wraps(fn)
        def for_sdk(*args, **kwargs):
            ok, value = run(args, kwargs)
            if ok:
                return _empty_list_visible(value)
            return CallToolResult(is_error=True, content=[TextContent(type="text", text=json.dumps(value))],
                                  structured_content=value)

        @functools.wraps(fn)
        def direct(*args, **kwargs):
            return run(args, kwargs)[1]

        doc = fn.__doc__
        if alias_of is not None:
            doc = f"Deprecated name (#746): use {alias_of[0]}" + (f"(action={alias_of[1]!r})." if alias_of[1] else ".")
        for reg_name, description, hidden in tool_registrations(name, doc, hidden_all=alias_of is not None):
            kwargs = dict(dkwargs)
            kwargs.setdefault("structured_output", False)
            if reg_name != name:
                kwargs["name"] = reg_name
            if description is not None:
                kwargs["description"] = description
            _sdk_tool(*dargs, **kwargs)(for_sdk)
            _WRAPPED_TOOLS[reg_name] = for_sdk
            if hidden:
                _HIDDEN_ALIASES.add(reg_name)
        direct.alias_of = alias_of
        return direct

    return decorate


mcp.tool = _tool

_sdk_list_tools = mcp.list_tools


async def _list_tools_without_aliases():
    """#700: tools/list without the hidden aliases (tools/call still answers them)."""
    return [t for t in await _sdk_list_tools() if t.name not in _HIDDEN_ALIASES]


mcp.list_tools = _list_tools_without_aliases
