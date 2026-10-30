"""constructicon-mcp — MCP server for the Constructicon media gallery.

Uses the mcp package's v2 MCPServer API
(FastMCP was renamed/restructured in mcp 2.x — see the SDK migration guide).
Streamable-HTTP transport, host/port/stateless_http passed to run().

Exposes MCP tools for uploading, managing, tagging, and organizing media
in a Constructicon instance. Runs as a sidecar alongside constructicon-web.

Conventions shared by every tool (#560, #548; enforced by the `@mcp.tool()` wrapper below):
  * Actor: each call runs as the "mcp" actor (core/actor.py) when it came with the install token,
    or as "user:<name>" when it came with that user's own MCP token (#604 step 5,
    mcp_server/auth.py), so the change log records who without any tool passing it.
  * Errors: a refusal comes back as {"ok": false, "error": {"code", "message"[, "details"]}}
    (an isError tool result), with the same codes the HTTP API uses. Not found is an
    error too: a getter or setter whose target doesn't exist returns code "not_found"
    rather than None or False. Input validation is "bad_request" unless a more specific
    code applies (bad_status, invalid_choice, ...). Successful returns are unchanged.
  * Extracted text (#697): a tool that returns MANY items (search, list_items, get_project,
    get(include=related), related, a blog entry's items, ...) sends a ~300-character preview of
    each item's extracted_text plus extracted_text_chars and extracted_text_truncated
    (_to_public_listed). A single-item tool (constructicon_get, the setters) sends the whole
    text (_to_public).
  * #746: these conventions are told to the client ONCE, in the server's startup instructions
    (SERVER_INSTRUCTIONS), so the tool descriptions don't repeat them. Several old single-purpose
    tools were folded into fewer tools with an `action` (or `view` / `which` / `direction`)
    selector; every old name stays callable as a HIDDEN, logged alias (`@mcp.tool(alias_of=...)`)
    with its old signature, defaults and return shape. Who may run which (tool, action) is
    ACTION_ROLES below (all admin today: the MCP is one admin identity; ready for per-user tokens).
"""

import base64
import binascii
import functools
import inspect
import io
import json
import logging
import os
import re
import sys
import typing
import unicodedata
from datetime import datetime
from pathlib import Path

import pydantic

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

# #549: this is NOT the process that does background work. core.captions.run_caption checks this
# role and enqueues (caption_queue table) instead of calling the GPU; the web process drains it.
os.environ["CONSTRUCTICON_ROLE"] = "mcp"

from mcp.server.mcpserver import Image as McpImage, MCPServer
from mcp.types import CallToolResult, TextContent, ToolAnnotations

from core import access_log, actor as actor_ctx, mcp_tokens, policy, roles, users
from core import backup, captions, thumbnails, card_rules, cards, correlations, curation_queue, curator_needs, db, decisions, duplicates, errors, ingest, lookalikes, item_dates, item_title, items, object_types, ocr, physical_piece, provenance_options, provenance_rules, revisions, storage, timeline
from core.errors import InvalidInput, NotFound
from core import version as version_info
from core import blog, changes, hobbies, membership, parse_limits, reset, sharing
from core import besteffort, install_config
from core import hobby_label as hobby_label_rules
from core import tags as tags_svc
from core import vocab as vocab_svc
from core import paths
from core import similarity, transcripts

# #652: files this process writes (uploads, imports, thumbnails, backups) are not readable by other
# users on the host. Set at import, before main() runs init_db.
paths.restrict_new_files()

# #699: run as the server (not imported by a test), refuse to start, with the folder, its owner and
# the fix, when a data folder isn't writable. Here at import, before anything below reads the DB
# (the hobby label is read at import), which would otherwise fail with "unable to open database file".
if __name__ == "__main__":
    _unwritable = paths.unwritable_data_dirs(db.DB_PATH)
    if _unwritable:
        sys.exit(paths.data_dirs_error("constructicon-mcp", _unwritable))
    # #736: the install token file must be readable by this uid (the MCP never uses the caption
    # valve's secret, so only the token variables are checked here).
    _secret_problems = paths.secret_file_problems(
        variables=("CONSTRUCTICON_INSTALL_TOKEN_FILE", "CONSTRUCTICON_MCP_TOKEN_FILE"))
    if _secret_problems:
        sys.exit(paths.secret_files_message("constructicon-mcp", _secret_problems))

BASE_URL = os.environ.get("CONSTRUCTICON_BASE_URL", "http://constructicon-web:8000")

# #433 part 3: a read-only bind-mounted inbox (host: Media/constructicon/import,
# reachable over SMB) so large files are ingested by server-side path instead of
# base64 through one MCP message; never written to or deleted from by the app.
IMPORT_DIR = Path(os.getenv("CONSTRUCTICON_IMPORT_DIR", "/app/import"))

# #433: base64 inflates ~33% and the whole payload rides one MCP message;
# a 500 MB file would be ~670 MB of JSON
DOWNLOAD_INLINE_MAX_BYTES = 25 * 1024 * 1024

# --- #700: tool names and descriptions follow the install's hobby label ---
# The label is read ONCE, when this module loads (= when the MCP process starts): a label saved
# later in Admin > Install reaches the tools only after the MCP restarts (and clients reconnect).
# With the default "Hobby / Hobbies" every name is exactly as before. With e.g.
# "Department / Departments": `constructicon_create_hobby` is registered as
# `constructicon_create_department`, `constructicon_list_hobbies` as `constructicon_list_departments`,
# and the descriptions say department(s) (core.hobby_label.relabel_text: identifiers, `backticked`
# words, `hobby=` parameters and paths stay). PARAMETER names and JSON fields stay `hobby` (stable
# API). The old names (`*_hobby*`, and each earlier label's) stay callable as HIDDEN aliases: they
# answer tools/call but tools/list leaves them out, so clients see only the new names while old
# scripts keep working until they are updated. The process records the label it started with
# (install_config.record_mcp_started_label, in __main__ below) so Admin can say "restart the MCP".
STARTED_LABEL = install_config.hobby_label()
_ALIAS_LABELS = [hobby_label_rules.Label(one, many) for one, many in
                 ((o.replace("-", " "), m.replace("-", " ")) for o, m in install_config.known_label_slugs())]
_HIDDEN_ALIASES = set()
_TOOL_WORD = re.compile(r"(?<=_)(hobby|hobbies)(?=_|\b)")


def tool_name_for(name, label):
    """#700: the registered name of tool `name` (its function name) under `label`: the `hobby` /
    `hobbies` parts of the name become the label's words ("_" for any "-")."""
    if label.is_default:
        return name
    return _TOOL_WORD.sub(lambda m: label.one_ident if m.group(1) == "hobby" else label.many_ident, name)


def tool_description_for(doc, label):
    """#700: a tool's description under `label`: the words relabelled, and every tool name it
    mentions (`constructicon_list_hobbies`) given as that tool's name under the label. #746: the
    docstring's indentation is removed (inspect.cleandoc), which is all it does for the default label."""
    doc = inspect.cleandoc(doc) if doc else doc
    if label.is_default or not doc:
        return doc
    doc = re.sub(r"\bconstructicon_\w+", lambda m: tool_name_for(m.group(0), label), doc)
    return hobby_label_rules.relabel_text(doc, label)


# #746: the conventions every tool shares, said ONCE (MCP initialize -> instructions) instead of in
# each tool's description. Relabelled like the descriptions (#700).
SERVER_INSTRUCTIONS = """Constructicon: a personal archive. Items are files or links (by slug); cards are projects, things, actions, families, collections and events (by id or slug); hobbies group cards; plus tags and blog entries.

Conventions for every tool:
- Errors: a refusal is an error result {"ok": false, "error": {"code", "message", "details"?}}, with the HTTP API's codes, and nothing is written. A missing target is "not_found" (never null/false); bad input is "bad_request" unless a more specific code applies; "forbidden" says who may do it and why.
- Undo: every write is logged with row images and returns "batch_id" (null when nothing changed). One call is one batch: constructicon_undo(batch_id) reverses all of it; constructicon_list_changes shows the log. A tool that takes batch_id joins an earlier call's batch.
- dry_run=true answers as the write would and writes nothing. constructicon_bulk_edit, constructicon_resolve_decisions and constructicon_retype_links are dry-run by DEFAULT.
- Visibility: sensitive and restricted items are admin-only (or their uploader's). Private cards and hobbies (and everything under them) are seen only by admins, their owner and their members; constructicon_sharing manages that. A hidden item or card reads as not_found; a write that names a hidden item is refused as forbidden, saying why.
- Identity: the install token acts as the admin; a personal MCP token (made in the web app under Account > MCP tokens) acts as that user, with their role and what they can see.
- Text: a tool returning many items sends a ~300-character preview of each one's extracted_text, plus extracted_text_chars and extracted_text_truncated (text_chars=0..5000 where offered), and trims big type_metadata values (type_metadata_truncated, type_metadata_cut_keys). constructicon_get(slug) returns one item whole; constructicon_get(slug, text_offset=, text_chars=) reads a long text by the page (next_offset). Long lists page: limit + cursor -> next_cursor (null at the end).
- Dates: an ISO date or datetime with no offset is Mountain Time (America/Denver); unix seconds also work.
- Selector tools take action= (or view=, which=, direction=); each value is listed with what it returns.
- Names retired in 2026-10 (#746) still answer but are not listed; use the listed tools."""

# #508: version in server info (read at start; restart picks up a deploy)
mcp = MCPServer(name="constructicon-mcp", version=version_info.get_version(),
                instructions=tool_description_for(SERVER_INSTRUCTIONS, STARTED_LABEL))


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


# --- #746: who may run which (tool, action) ---------------------------------------------------
# One row per visible tool, and per action for a tool with a selector (action=, view=, which=,
# direction=, or delete's target). Mirrors the web's role labels (#557): reads viewer, curation
# writes editor, anything permanent or install-wide admin. The MCP is one admin identity today
# (mcp_server/auth.py), so nothing is refused yet; the table is what per-user tokens will check.
# Startup refuses a tool or an action value with no row (_check_action_roles). A hidden alias
# uses the row of the call it stands for (its alias_of).
V, E, A = roles.VIEWER, roles.EDITOR, roles.ADMIN
ACTION_ROLES = {
    ("constructicon_version", None): V,
    ("constructicon_upload", None): E,
    ("constructicon_replace_file", None): E,
    ("constructicon_search", None): V,
    ("constructicon_get", None): V,
    ("constructicon_download", None): V,
    ("constructicon_import", None): E,
    ("constructicon_list_import", None): E,
    ("constructicon_update", None): E,
    ("constructicon_view", None): V,
    ("constructicon_set_caption", None): E,
    ("constructicon_undated", "list"): V,        # #725: undated items and their date guesses
    ("constructicon_undated", "accept"): E,      # one guess written as content_date (undoable)
    ("constructicon_undated", "apply_auto"): E,  # every high-confidence guess, one batch (undoable)
    ("constructicon_set_agent_notes", "slug"): E,  # an item's private notes (#206)
    ("constructicon_set_agent_notes", "card"): E,  # a card's (#745); same rule as the item's
    ("constructicon_add_content", None): E,
    ("constructicon_backup", None): A,
    ("constructicon_redact", "redact"): E,
    ("constructicon_redact", "unredact"): E,
    ("constructicon_redact", "recover"): E,
    ("constructicon_redact", "delete_file"): A,  # permanent
    ("constructicon_delete", "slug"): E,
    ("constructicon_delete", "slugs"): E,
    ("constructicon_delete", "card"): E,
    ("constructicon_delete", "blog_entry"): E,
    ("constructicon_delete", "everything"): A,  # the whole archive, permanent
    ("constructicon_trash", "list"): A,
    ("constructicon_trash", "empty"): A,  # permanent
    ("constructicon_duplicates", "plan"): E,   # #615: read-only, filtered per actor (policy.can_view)
    ("constructicon_duplicates", "apply"): A,  # #615: trashes copies library-wide (undoable)
    ("constructicon_lookalikes", "list"): V,     # #759: GET /api/curator/lookalikes (viewer)
    ("constructicon_lookalikes", "confirm"): E,  # POST /api/curator/lookalikes/confirm (editor; one undoable fold)
    ("constructicon_lookalikes", "dismiss"): E,  # POST /api/curator/lookalikes/dismiss (editor; remembered)
    ("constructicon_proposals", "list"): V,      # #354 / #344: GET /api/curator/provenance-rules | correlations
    ("constructicon_proposals", "confirm"): E,   # POST .../confirm (editor; one undoable batch)
    ("constructicon_proposals", "dismiss"): E,   # POST .../dismiss (editor; remembered)
    ("constructicon_item_tags", "attach"): E,
    ("constructicon_item_tags", "detach"): E,
    ("constructicon_related", "add"): E,
    ("constructicon_related", "remove"): E,
    ("constructicon_list_items", "redacted"): A,
    ("constructicon_list_items", "restricted"): A,
    ("constructicon_list_items", "brand_assets"): V,
    ("constructicon_list_items", "tag"): V,
    ("constructicon_list_items", "unfiled"): V,  # #556: the /unfiled page (viewer)
    ("constructicon_list_items", "similar"): V,  # #556: GET /api/image/{slug}/similar (viewer)
    ("constructicon_caption_review", "list"): V,    # #556: GET /api/captions/unreviewed (viewer)
    ("constructicon_caption_review", "accept"): E,  # POST /api/captions/{slug}/accept (editor)
    ("constructicon_caption_review", "skip"): E,    # POST /api/captions/{slug}/skip (editor)
    ("constructicon_caption_review", "retry"): E,   # POST /api/image/{slug}/caption (editor)
    ("constructicon_revision", "supersede"): E,
    ("constructicon_revision", "remove"): E,
    ("constructicon_run_type_action", None): E,
    ("constructicon_list_projects", None): V,
    ("constructicon_create_project", None): E,
    ("constructicon_update_project", None): E,
    ("constructicon_get_project", None): V,
    ("constructicon_explain_card", None): V,
    ("constructicon_card_files", "add"): E,
    ("constructicon_card_files", "remove"): E,
    ("constructicon_card_files", "move"): E,
    ("constructicon_card_files", "copy"): E,
    ("constructicon_card_hobby", "add"): E,
    ("constructicon_card_hobby", "remove"): E,
    ("constructicon_family", "add"): E,
    ("constructicon_family", "remove"): E,
    ("constructicon_nest", "nest"): E,
    ("constructicon_nest", "unnest"): E,
    ("constructicon_sharing", "get"): V,
    ("constructicon_sharing", "users"): E,
    ("constructicon_sharing", "set_private"): E,  # core/sharing.py then allows only the owner or an admin
    ("constructicon_sharing", "add_member"): E,
    ("constructicon_sharing", "remove_member"): E,
    ("constructicon_link", "add"): E,
    ("constructicon_link", "remove"): E,
    ("constructicon_link", "retype"): E,
    ("constructicon_link", "list"): V,
    ("constructicon_retype_links", None): E,
    ("constructicon_split_card", None): E,
    ("constructicon_merge_cards", None): E,
    ("constructicon_convert", "card_to_hobby"): A,  # whole-card conversion (web: admin)
    ("constructicon_convert", "hobby_to_card"): A,
    ("constructicon_bulk_edit", None): E,
    ("constructicon_undo", None): E,
    ("constructicon_list_changes", None): E,
    ("constructicon_list_hobbies", None): V,
    ("constructicon_create_hobby", None): E,
    ("constructicon_update_hobby", None): E,
    ("constructicon_unmark_hobby", None): E,
    ("constructicon_list_blog_entries", None): V,
    ("constructicon_get_blog_entry", None): V,
    ("constructicon_create_blog_entry", None): E,
    ("constructicon_update_blog_entry", None): E,
    ("constructicon_list_tags", None): V,
    ("constructicon_create_tag", None): E,
    # #555: tag management. Reads viewer; every write is one undoable batch, so editor (like a card edit).
    ("constructicon_tags", "tree"): V,
    ("constructicon_tags", "get"): V,
    ("constructicon_tags", "sweep"): E,
    ("constructicon_tags", "rename"): E,
    ("constructicon_tags", "merge"): E,
    ("constructicon_tags", "move"): E,
    ("constructicon_tags", "delete"): E,
    ("constructicon_tags", "sweep_apply"): E,
    ("constructicon_list_provenance_options", None): V,
    ("constructicon_list_vocab", None): V,
    ("constructicon_needs", "queue"): V,
    ("constructicon_needs", "nudges"): V,
    ("constructicon_needs", "caption"): V,
    ("constructicon_needs", "card_decisions"): V,
    ("constructicon_needs", "pending"): V,
    ("constructicon_queue_item", "dismiss"): E,
    ("constructicon_queue_item", "defer"): E,
    ("constructicon_queue_item", "bring_back"): E,
    ("constructicon_resolve_pending_decision", None): E,
    ("constructicon_resolve_decisions", None): E,
    ("constructicon_sweep_stale_decisions", None): E,
}
del V, E, A


def role_for(tool, action=None):
    """#746: the role a (tool, action) needs; an unknown pair fails closed (admin)."""
    return ACTION_ROLES.get((tool, action), roles.ADMIN)


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


def _resolve_import_path(rel):
    """Resolve a relative path within IMPORT_DIR, rejecting absolute paths
    and escapes (../ or symlinks outside IMPORT_DIR). Returns Path | None."""
    # Reject absolute paths and path traversal attempts
    if Path(rel).is_absolute():
        return None
    candidate = (IMPORT_DIR / rel).resolve()
    # Ensure the resolved path stays within IMPORT_DIR bounds
    try:
        candidate.relative_to(IMPORT_DIR.resolve())
    except ValueError:  # silent-ok: outside IMPORT_DIR = None; both callers refuse or skip it
        # candidate is outside IMPORT_DIR
        return None
    # Verify it's a file and exists
    if not candidate.is_file():
        return None
    return candidate


def _run_in_thread(fn, *args):
    """Run a function in a background daemon thread, carrying the actor context (#560)."""
    actor_ctx.spawn(fn, *args)


def _ingest(filename, fileobj, file_size, description, tags, uploaded_by, source_modified_at, sensitive=False) -> dict:
    """Private helper for file ingestion: delegates to core/ingest.py.

    All post-validation logic is now centralized in the ingest module — this tool
    simply wraps its result for the MCP response format.

    Returns {"slug": ..., ..._to_public fields..., "duplicate": False} on success; a refusal
    raises InvalidInput (code upload_refused), which the tool wrapper turns into the shared
    error shape. May include "pending_decision_id" if the type's pre_store_fn deferred to the
    owner (#448).
    """
    result = ingest.ingest_file(
        fileobj,
        filename,
        size=file_size,
        source=uploaded_by,
        run_background=_run_in_thread,
        description=description,
        tags=tags,
        source_modified_at=source_modified_at,
        sensitive=sensitive,  # #603: set in the INSERT itself
    )

    if result.error:
        raise InvalidInput(result.error, code="upload_refused")
    elif result.duplicate:
        response = {**_to_public(result.row), "duplicate": True}
        if result.pending_decision_id:
            response["pending_decision_id"] = result.pending_decision_id
        return response
    else:
        response = {**_to_public(result.row), "duplicate": False}
        if result.pending_decision_id:
            response["pending_decision_id"] = result.pending_decision_id
        return response


def _to_public(row):
    spec = object_types.get_object_type(row.get("media_type"))
    return {
        "slug": row["slug"],
        "url": f"{BASE_URL}/f/{row['slug']}",
        "filename": row["filename"],
        # display_name (#11) here is the item's one canonical title (core/item_title.py, #542):
        # rename, then short title, then filename, then slug. "icon" is the type's badge (#704: the
        # per-object icon override column is gone).
        "display_name": item_title.title_of(row),
        "icon": spec.badge_icon,
        "media_type": row.get("media_type") or "image",
        "description": row["description"],
        "content_description": row.get("content_description"),  # #588: the item's long caption / description
        "short_title": row.get("short_title"),  # #542: the short name; the long caption is never the title
        "tags": tags_svc.visible_chip_names(row["tags"]),  # #555: the derived chips, none naming a hidden card
        "redacted": bool(row["redacted"]),
        "extracted_text": row["extracted_text"],
        "ocr_status": row["ocr_status"],
        # #261: per-type metadata bag (auto_caption/auto_caption_status,
        # YouTube view/like/comment counts, ID3 tags, ...) -- previously
        # invisible through every MCP tool. Same "always a dict, never
        # None/missing" contract as web/app.py's _to_object_detail;
        # core/db.py's _row_to_dict already parses the JSON column, so
        # this is only a guard against a stored JSON null.
        "type_metadata": row.get("type_metadata") or {},
        "timestamp": row["timestamp"],
        "content_date": row.get("content_date"),
        "display_date_override": row.get("display_date_override"),
        # #725: the REAL date or None (undated: the upload `timestamp` above is never the item's date);
        # date_shown is what its card says ("≈ <range> · from <card>" when it inherits its card's range).
        "effective_date": timeline.resolve_item_date(row),
        "date_shown": item_dates.item_date(row)["label"],
        # Curator (#341): provenance classification + highlight flag, so every
        # MCP object read (get_project items, search, list) carries them.
        "provenance": row.get("provenance"),
        "highlight": bool(row.get("highlight")),
        # Brand assets (#350): is_brand_asset flag + optional brand_role label,
        # so every MCP object read carries brand kit metadata.
        "is_brand_asset": bool(row.get("is_brand_asset")),
        "brand_role": row.get("brand_role"),
        # #448: per-type actions available on this object (e.g. YouTube's "fetch real date" moves here in PR 2).
        # The type file owns the handler; actions are declared in ObjectTypeSpec.actions.
        "actions": [a.key for a in spec.actions],
        # #604 step 1: the user who uploaded it ({id, username, name}); null = admin-owned (the
        # install token, the MCP, a script). #603: the sensitive lock and why (type or flag).
        "uploaded_by_user": users.owner_info(row.get("uploaded_by_user_id")),
        # #778: the personal MCP key it was uploaded with, as {"text": "<user>'s <token name>", ...}
        # (null = none: the web, the install token). #560: HOW it arrived (web | mcp | migration:<source>
        # | ...); `uploaded_by` above stays the free-text Source note, never who uploaded it.
        "uploaded_via": mcp_tokens.credit(row.get("uploaded_by_user_id"), row.get("uploaded_via_token_id")),
        "channel": row.get("channel"),
        "source_note": row.get("tech"),
        "sensitive": policy.restriction_reason(row) is not None,
        "sensitive_reason": policy.restriction_reason(row),
    }


# #697: a tool that returns MANY items sends a short preview of each one's extracted_text (OCR,
# document text, a transcript can each be hundreds of KB), never the whole text. One item's full
# text comes from constructicon_get. The preview is a window around the first word of the query
# found in the text (word-start, case and accents folded, like the search itself), else its start.
TEXT_PREVIEW_CHARS = 300
TEXT_PREVIEW_MAX_CHARS = 5000
_ELLIPSIS = "…"


def _fold(text):
    """Lowercased, accent-folded, SAME LENGTH as `text` (so a match offset is an offset in `text`)."""
    if text.isascii():
        return text.lower()
    return "".join((unicodedata.normalize("NFKD", c)[:1] or c).lower()[:1] or c for c in text)


def _first_hit(text, query):
    """Offset of the first word-start match of any query word in `text`, or None."""
    words = db._SEARCH_WORD_RE.findall(query or "")[:db._SEARCH_MAX_WORDS]
    if not words or not text:
        return None
    folded = _fold(text)
    best = None
    for w in {_fold(w) for w in words}:
        m = re.search(r"(?<![^\W_])" + re.escape(w), folded)
        if m and (best is None or m.start() < best):
            best = m.start()
    return best


def text_chars_arg(text_chars):
    """Validate a tool's `text_chars`: 0 (no text) up to TEXT_PREVIEW_MAX_CHARS; more is clamped."""
    if text_chars is None:
        return TEXT_PREVIEW_CHARS
    if isinstance(text_chars, bool) or not isinstance(text_chars, int) or text_chars < 0:
        raise InvalidInput(f"text_chars must be a whole number from 0 to {TEXT_PREVIEW_MAX_CHARS}.")
    return min(text_chars, TEXT_PREVIEW_MAX_CHARS)


def _text_preview(text, query=None, chars=TEXT_PREVIEW_CHARS):
    """{extracted_text: the preview, extracted_text_chars: full length, extracted_text_truncated}.
    The preview is at most `chars` characters of the text, plus "…" at each end that was cut."""
    full = len(text or "")
    if text is None or chars <= 0:
        return {"extracted_text": None if text is None or full else text,
                "extracted_text_chars": full, "extracted_text_truncated": full > 0}
    if full <= chars:
        return {"extracted_text": text, "extracted_text_chars": full, "extracted_text_truncated": False}
    hit = _first_hit(text, query)
    start = 0 if hit is None else min(max(0, hit - chars // 3), full - chars)
    end = start + chars
    preview = (_ELLIPSIS if start > 0 else "") + text[start:end] + (_ELLIPSIS if end < full else "")
    return {"extracted_text": preview, "extracted_text_chars": full, "extracted_text_truncated": True}


# #722: a multi-item result trims big type_metadata values too (a write-up's `body`, a video's
# `transcript` and its cue list can each be hundreds of KB). A text value longer than the preview is
# cut to it (with "…"); any other value (a list, an object) over META_VALUE_MAX_CHARS as JSON is
# left out. `type_metadata_truncated` says whether anything was, `type_metadata_cut_keys` which keys.
# constructicon_get(slug) still returns the whole type_metadata.
META_VALUE_MAX_CHARS = 1000


def _trim_meta(meta, chars=TEXT_PREVIEW_CHARS):
    """(type_metadata for a list result, [keys cut or left out])."""
    limit = max(chars or 0, TEXT_PREVIEW_CHARS)
    out, cut = {}, []
    for key, value in (meta or {}).items():
        if isinstance(value, str):
            if len(value) > limit:
                out[key] = value[:limit] + _ELLIPSIS
                cut.append(key)
            else:
                out[key] = value
        elif isinstance(value, (list, dict)) and len(json.dumps(value, default=str)) > META_VALUE_MAX_CHARS:
            cut.append(key)
        else:
            out[key] = value
    return out, cut


def _meta_fields(meta, chars=TEXT_PREVIEW_CHARS):
    """{type_metadata, type_metadata_truncated[, type_metadata_cut_keys]} for a trimmed answer."""
    trimmed, cut = _trim_meta(meta, chars)
    out = {"type_metadata": trimmed, "type_metadata_truncated": bool(cut)}
    if cut:
        out["type_metadata_cut_keys"] = cut
    return out


def _to_public_listed(row, query=None, text_chars=TEXT_PREVIEW_CHARS):
    """_to_public for an item in a multi-item result: extracted_text is a preview (#697), big
    type_metadata values are trimmed (#722), and a transcript hit carries its moment (#789:
    timestamp_seconds / timestamp_url for the first query word found in the transcript)."""
    pub = _to_public(row)
    out = {**pub, **_meta_fields(pub["type_metadata"], text_chars),
           **_text_preview(row.get("extracted_text"), query, text_chars)}
    if query and transcripts.has_cues(row):
        hit = _first_hit(row.get("extracted_text"), query)
        stamp = transcripts.cite(row, hit)
        if stamp["timestamp_seconds"] is not None:
            out.update(stamp)
    return out


def _to_public_project(project):
    items = db.list_project_items(project["id"])
    effective_start, effective_end = cards.card_span(project, items)  # #772: groups roll up members
    return {
        "id": project["id"],
        "slug": project["slug"],
        "title": project["title"],
        "description": project["description"],
        **cards.status_fields(project),
        **cards.whereabouts_fields(project),
        "cover_slug": project.get("cover_slug"),
        "writeup_slug": project.get("writeup_slug"),
        "parent_id": project.get("parent_id"),
        "created_at": project["created_at"],
        "created_by_user": users.owner_info(project.get("created_by_user_id")),  # #604 step 1 (null = admin-owned)
        "created_via": mcp_tokens.credit(project.get("created_by_user_id"), project.get("created_via_token_id")),  # #778
        "start_date_override": project.get("start_date_override"),
        "end_date_override": project.get("end_date_override"),
        "effective_start": effective_start,
        "effective_end": effective_end,
    }


def _hobby_creator(hobby_id):
    """#778: {created_by_user, created_via} for a hobby (null = admin-owned / no personal key)."""
    hp = db.hobby_privacy(hobby_id)
    return mcp_tokens.creator_fields(hp["created_by_user_id"], hp["created_via_token_id"])


def _to_public_blog_entry(entry):
    """Full detail shape for a blog entry, including hydrated projects and items."""
    projects = policy.filter_visible_cards(db.list_entry_projects(entry["id"]))  # #604 step 3
    items = policy.filter_visible(db.list_entry_items(entry["id"]))  # #557
    return {
        "id": entry["id"],
        "slug": entry["slug"],
        "title": entry["title"],
        "subtitle": entry.get("subtitle", ""),
        "body": entry.get("body", ""),
        "status": entry["status"],
        "cover_slug": entry.get("cover_slug"),
        "content_date": entry.get("content_date"),
        "created_at": entry["created_at"],
        "updated_at": entry["updated_at"],
        # #604 / #778: {created_by_user, created_via} ("<user>'s <token name>" for a personal key)
        **mcp_tokens.creator_fields(entry.get("created_by_user_id"), entry.get("created_via_token_id")),
        # Carry the per-attachment note + sort_order (the reshaping helpers
        # drop them, but they're the point of the attachment).
        "projects": [{**_to_public_project(p), "note": p.get("note", ""), "sort_order": p.get("sort_order")} for p in projects],
        "items": [{**_to_public_listed(i), "note": i.get("note", ""), "sort_order": i.get("sort_order")} for i in items],
    }


# --- #746: helpers the merged tools and their aliases share ------------------------------------

_NOTHING_CHANGED = "Nothing changed."


def _viewable_item(slug):
    """One item row for a read door: not_found when missing OR hidden by the item policy."""
    row = db.get_by_slug(slug)
    if row is None:
        raise NotFound(f"No item {slug!r}.")
    policy.require_view(row, message=f"No item {slug!r}.")  # #557
    return row


def _related_listed(slug):
    """The items related to `slug`, as the item policy lets the caller see them (listed shape)."""
    return [_to_public_listed(r) for r in policy.filter_visible(db.list_related(slug))]


def _written(batch_id):
    """`batch_id` when that batch wrote anything (so undo has something to reverse), else None."""
    return batch_id if batch_id and db.get_change_rows(batch_id=batch_id) else None


def _dedupe(rows):
    """Change rows of several ops in one batch, each once (some ops report the whole batch so far)."""
    out, seen = [], set()
    for r in rows:
        key = json.dumps(r, sort_keys=True, default=str)
        if key not in seen:
            seen.add(key)
            out.append(r)
    return out


class _Collect:
    """Gathers the changes and warnings of several core ops run under one batch."""

    def __init__(self):
        self.changes, self.warnings = [], []

    def __call__(self, res):
        self.changes.extend(getattr(res, "changes", None) or [])
        self.warnings.extend(w for w in (getattr(res, "warnings", None) or []) if w != _NOTHING_CHANGED)
        return res

    def result(self):
        return {"changes": _dedupe(self.changes), "warnings": list(dict.fromkeys(self.warnings))}


def _fields_arg(fields, spec, tool):
    """Validates a `fields={...}` argument against `spec` ({key: allowed python types}). Key present
    = set it (None clears where the field allows it), key absent = leave it."""
    if fields is None:
        return {}
    if not isinstance(fields, dict):
        raise InvalidInput("fields must be an object, e.g. {\"highlight\": true}.", code="bad_fields")
    if "agent_notes" in fields:
        raise InvalidInput("agent_notes are written only with constructicon_set_agent_notes.", code="unknown_field")
    unknown = sorted(set(fields) - set(spec))
    if unknown:
        raise InvalidInput(f"Unknown field(s) for {tool}: {', '.join(unknown)}. Allowed: {', '.join(sorted(spec))}.",
                           code="unknown_field", details={"unknown": unknown, "allowed": sorted(spec)})
    for key, value in fields.items():
        types = spec[key]
        if not isinstance(value, types) or (bool not in types and isinstance(value, bool)):
            names = " or ".join("null" if t is type(None) else t.__name__ for t in types)
            raise InvalidInput(f"fields.{key} must be {names}.", code="bad_fields")
    return dict(fields)


def _only_for(label, /, **passed):
    """Refuses parameters that don't apply to the chosen action: {name: (value, default)}.
    `label` is positional-only, so a tool parameter may be called anything (caption_review's `what`)."""
    extra = [k for k, (value, default) in passed.items() if value != default]
    if extra:
        raise InvalidInput(f"{', '.join(extra)} {'does' if len(extra) == 1 else 'do'} not apply to {label}. "
                           "Nothing was changed.")


@mcp.tool()
def constructicon_version() -> dict:
    """Which build is running: {version, commit, deployed_at, env}. CalVer YYYY.M.D (.N for a
    second release that day), "-dev" on a test instance; "dev" = no version file."""
    return version_info.get_version_info()


@mcp.tool()
def constructicon_upload(filename: str, content_base64: str, description: str = "", tags: list[str] | None = None,
                      uploaded_by: str = db.SOURCE_AUTHORED,
                      source_modified_at: float | None = None, sensitive: bool = False) -> dict:
    """Upload a file (base64) as a new item. Returns the item, with its hotlink url.

    filename: its extension decides the type (.png .pdf .stl .psd .svg .mp3 ...).
    tags: tag names to attach. description: the uploader's note.
    uploaded_by: a free-text Source NOTE (default "Claude — authored"), never who uploaded it: the
      uploader is the token's user (the install token = admin-owned). For migrated content pass
      "Claude — migrated from <source>" (sets channel "migration:<source>").
    source_modified_at: the file's own mtime (unix seconds). When filename, size and this match an
      existing item, that item comes back with "duplicate": true instead.
    sensitive: lock it from the start (admins only).
    OCR runs in the background (ocr_status "pending"; read extracted_text later with
    constructicon_get). An audio file's own tags fill its title and type_metadata at once.
    For big files use constructicon_import.
    """
    content = base64.b64decode(content_base64)
    return _ingest(filename, io.BytesIO(content), len(content), description, tags, uploaded_by, source_modified_at,
                   sensitive=sensitive)


@mcp.tool()
def constructicon_replace_file(slug: str, content_base64: str, filename: str | None = None) -> dict:
    """Replace an item's file bytes and KEEP the item: same slug and links, cards, tags, title,
    captions, relations and revision chain. Use it to revise a file in place.

    content_base64: the new bytes, standard base64 (no data: prefix). One MCP message, so at most
      the upload cap (constructicon_list_import's max_upload_mb); over it: file_too_large, nothing
      decoded. For a bigger file, constructicon_import it and use
      constructicon_revision(old, action="supersede", new=...).
    filename: the NEW file's name (decides the type check); omit to keep the item's name. A new
      extension keeps the stem and swaps the extension.
    Re-processed like an upload (thumbnail, text/OCR in the background, hash, captions, search).
    The old bytes go to the trash for 7 days; undo restores file, size and name.
    Returns the item plus {replaced, batch_id, previous_size, new_size, trash_days, expires_at}.
    Errors: replace_redacted, no_file, bad_base64, empty_file, file_too_large, unsupported_type,
    content_mismatch, replace_type_mismatch (another type: upload a new item and supersede),
    replace_needs_decision, replace_rejected, file_unchanged.
    """
    encoded = "".join((content_base64 or "").split())
    if len(encoded) * 3 // 4 > storage.MAX_BYTES + 3:
        raise errors.AppError("file_too_large", f"The replacement is about {len(encoded) * 3 // 4 / 1048576:.0f} MB "
                              f"as sent, over the {storage.MAX_MB} MB upload limit. Nothing was changed.", status=413,
                              details={"max_bytes": storage.MAX_BYTES})
    try:
        content = base64.b64decode(encoded, validate=True)
    except (binascii.Error, ValueError) as e:
        raise InvalidInput(f"content_base64 is not valid base64 ({e}). Send the raw file bytes, standard base64, "
                           "no data: prefix. Nothing was changed.", code="bad_base64") from e
    result = items.replace_file(slug, content, filename, run_background=_run_in_thread)
    return {**_to_public(result.item), "replaced": True, "batch_id": result.batch_id,
            "previous_size": result.data["previous_size"], "new_size": result.data["new_size"],
            "trash_days": result.data["trash_days"], "expires_at": result.data["expires_at"]}


@mcp.tool()
def constructicon_search(query: str | None = None, tags: list[str] | None = None,
                         text_chars: int = TEXT_PREVIEW_CHARS) -> list[dict]:
    """Search items by name (title, any filename it has had), tags, card titles, note or extracted
    text. Each word matches the START of a word ("tun" finds "Tuning"; a run-together filename is
    split too, so "camino" finds "ElCamino.jpeg"); all words are required; best match first. When
    nothing matches that way, the words run together are looked for INSIDE filenames ("el camino"
    finds "Elcamino4.jpeg"); each result's `matched` says which: "word_start" or "inside_filename"
    (null with no query). query and tags together: both must match.
    tags (any of them, any case) filters on the tag tree: an item carrying one of these tags, or any
    tag under one (a card's own tag counts). Redacted items are left out
    (constructicon_list_items(which="redacted")). The text preview is centred on the first query
    word found. Older revisions are found too, marked superseded_by.
    """
    chars = text_chars_arg(text_chars)
    # #477: old revisions are still found, marked with `superseded_by` (the current revision's slug) and `rev`.
    # #557: db.search applies the policy's browse clause; the per-item policy is checked here too.
    # #762: the filename fallback, and `matched` on every result (never a silent change of meaning).
    return revisions.decorate([
        {**_to_public_listed(r, query, chars),
         "matched": r.get("search_matched", db.SEARCH_MATCHED_WORD_START) if query else None}
        for r in policy.filter_visible(db.search(query=query, tags=tags, filename_fallback=True))])


GET_INCLUDES = ("related", "revisions", "agent_notes")
GET_WINDOW_MAX_CHARS = 50_000  # #722: the most text one windowed constructicon_get returns


def _whole_number(name, value):
    if isinstance(value, bool) or not isinstance(value, int):
        raise InvalidInput(f"{name} must be a whole number.")
    return value


def _text_window(row, text_offset, text_chars):
    """#722: one window of the row's extracted_text, plus where it sits and where the next one starts.
    text_chars None = from text_offset to the end (capped at GET_WINDOW_MAX_CHARS). Refuses an offset
    past the end or a size out of range (bad_request, saying the text's length)."""
    text = row.get("extracted_text") or ""
    total = len(text)
    _whole_number("text_offset", text_offset)
    if text_chars is not None:
        _whole_number("text_chars", text_chars)
        if not 1 <= text_chars <= GET_WINDOW_MAX_CHARS:
            raise InvalidInput(f"text_chars must be from 1 to {GET_WINDOW_MAX_CHARS:,}; this item's text is "
                               f"{total:,} characters.")
    if text_offset < 0 or (text_offset >= total and not (text_offset == 0 and total == 0)):
        raise InvalidInput(f"text_offset {text_offset:,} is outside this item's text ({total:,} characters"
                           + (f": use 0 to {total - 1:,})." if total else ", so only 0 works)."),
                           details={"extracted_text_chars": total})
    end = min(total, text_offset + (GET_WINDOW_MAX_CHARS if text_chars is None else text_chars))
    return {"extracted_text": text[text_offset:end] if text else row.get("extracted_text"),
            "extracted_text_chars": total, "text_offset": text_offset,
            "extracted_text_truncated": text_offset > 0 or end < total,
            "next_offset": end if end < total else None,
            # #789: the moment this window starts at, when it falls in a video's transcript
            **transcripts.cite(row, text_offset)}


@mcp.tool()
def constructicon_get(slug: str,
                      include: list[typing.Literal["related", "revisions", "agent_notes"]] | None = None,
                      text_offset: int = 0, text_chars: int | None = None) -> dict:
    """One item by slug: metadata, hotlink url, its WHOLE extracted_text and type_metadata, and
    `superseded_by` / `rev` (its place in a revision chain).
    Reading a long text by the page: text_chars=N (1..50000) returns only extracted_text[text_offset
    : text_offset+N] (text_offset alone = from there to the end, at most 50000), plus
    extracted_text_chars (the whole length), text_offset, extracted_text_truncated, next_offset
    (where the next page starts; null at the end) and timestamp_seconds / timestamp_url (for a YouTube
    video whose transcript has cue times: the moment that position is said, a &t= link; else null).
    A windowed read also trims big type_metadata values (type_metadata_truncated) as list results do.
    E.g. constructicon_get(slug, text_chars=20000), then text_offset=next_offset.
    include adds, on request: "related" -> `related`, the items linked to it (listed shape);
    "revisions" -> `revisions`, its chain {in_chain, chain: [{slug, title, filename, rev,
    is_current, is_this, redacted}], current, superseded, rev, of}; "agent_notes" ->
    `agent_notes`, the private notes from constructicon_set_agent_notes.
    E.g. constructicon_get(slug, include=["related"]).
    """
    wanted = list(dict.fromkeys(include or []))
    bad = [w for w in wanted if w not in GET_INCLUDES]
    if bad:
        raise InvalidInput(f"include: unknown {', '.join(map(str, bad))}; use {', '.join(GET_INCLUDES)}.")
    windowed = text_chars is not None or text_offset != 0
    row = db.get_by_slug(slug)
    if row is None:
        raise NotFound(f"No item {slug!r}.")
    policy.require_view(row, message=f"No item {slug!r}.")  # #557
    window = _text_window(row, text_offset, text_chars) if windowed else None  # validated before the access log
    policy.note_access(row, access_log.HOW_MCP_GET)  # #604 follow-up 7: sensitive items only
    out = revisions.decorate([_to_public(row)])[0]
    if window is not None:
        out.update(window)
        out.update(_meta_fields(out["type_metadata"], TEXT_PREVIEW_MAX_CHARS))
    if "related" in wanted:
        out["related"] = _related_listed(slug)
    if "revisions" in wanted:
        out["revisions"] = revisions.revision_view(slug)
    if "agent_notes" in wanted:
        out["agent_notes"] = row.get("agent_notes")
    return out


@mcp.tool(alias_of=("constructicon_get", None))
def constructicon_get_related(slug: str) -> list[dict]:
    _viewable_item(slug)
    return _related_listed(slug)


@mcp.tool(alias_of=("constructicon_get", None))
def constructicon_list_revisions(slug: str) -> dict:
    _viewable_item(slug)
    return {"ok": True, **revisions.revision_view(slug)}


@mcp.tool()
def constructicon_download(slug: str) -> dict | None:
    """An item's file bytes, base64, with filename and media_type. Over ~25 MB: metadata and the
    hotlink url instead (content_base64 null); fetch the url.
    Errors: no_file (a link or other file-less item), file_missing (gone from disk).
    """
    row = db.get_by_slug(slug)
    if row is None:
        raise NotFound(f"No item {slug!r}.")
    policy.require_view(row, message=f"No item {slug!r}.")  # #557 (the #561 note: restricted downloads are the policy's call)
    if not row.get("stored_filename"):
        raise InvalidInput("This object has no uploaded file to download", code="no_file")
    policy.note_access(row, access_log.HOW_MCP_DOWNLOAD)  # #604 follow-up 7: sensitive items only
    path = storage.path_for(row["stored_filename"])
    if not path.exists():
        raise NotFound("File missing on disk", code="file_missing")
    size = path.stat().st_size
    if size > DOWNLOAD_INLINE_MAX_BYTES:
        # Return URL instead of base64 for large files
        return {
            "slug": row["slug"],
            "filename": row["filename"],
            "media_type": row.get("media_type") or "image",
            "size_bytes": size,
            "url": _to_public(row)["url"],
            "content_base64": None,
            "note": f"File is {size // (1024*1024)} MB, over the {DOWNLOAD_INLINE_MAX_BYTES // (1024*1024)} MB inline limit; fetch it from url instead.",
        }
    content = path.read_bytes()
    return {
        "slug": row["slug"],
        "filename": row["filename"],
        "media_type": row.get("media_type") or "image",
        "content_base64": base64.b64encode(content).decode("utf-8"),
    }


@mcp.tool()
def constructicon_import(path: str, description: str = "", tags: list[str] | None = None,
                        uploaded_by: str = db.SOURCE_AUTHORED,
                        source_modified_at: float | None = None, sensitive: bool = False) -> dict:
    """Import a file from the server's import inbox (for files too big to send base64, up to the
    upload limit). path is relative to the inbox, e.g. "drawings/set-A.pdf"; nothing outside it.
    The file is COPIED (it stays in the inbox) and named by its basename. source_modified_at
    defaults to its mtime, so re-importing an unchanged file returns the existing item with
    duplicate: true. Otherwise as constructicon_upload. Error: import_unavailable (no inbox).
    """
    if not IMPORT_DIR.is_dir():
        raise errors.AppError("import_unavailable", f"Import inbox {IMPORT_DIR} is not mounted on this server",
                              status=503)
    p = _resolve_import_path(path)
    if p is None:
        raise NotFound(f"Not a file inside the import inbox: {path}")
    st = p.stat()
    with p.open("rb") as f:
        return _ingest(p.name, f, st.st_size, description, tags, uploaded_by,
                      source_modified_at if source_modified_at is not None else st.st_mtime, sensitive=sensitive)


@mcp.tool()
def constructicon_list_import() -> dict:
    """The files waiting in the import inbox (recursive, hidden ones skipped), sorted by path:
    {inbox, max_upload_mb, files: [{path, size_bytes, modified_at, supported}], truncated?}
    (at most 1000 files). Error: import_unavailable.
    """
    if not IMPORT_DIR.is_dir():
        raise errors.AppError("import_unavailable", f"Import inbox {IMPORT_DIR} is not mounted on this server",
                              status=503)

    files = []
    try:
        for p in sorted(IMPORT_DIR.rglob("*")):
            rel = p.relative_to(IMPORT_DIR)
            if any(part.startswith(".") for part in rel.parts):
                continue
            # Same check constructicon_import applies: only list what it would
            # accept (regular files, symlinks resolving inside the inbox).
            if _resolve_import_path(rel) is None:
                continue
            st = p.stat()
            files.append({
                "path": rel.as_posix(),
                "size_bytes": st.st_size,
                "modified_at": st.st_mtime,
                "supported": object_types.detect_media_type(p.name) is not None,
            })
            if len(files) >= 1000:
                break
    except Exception as e:
        raise errors.AppError("import_error", f"Error listing import inbox: {e}", status=500) from e

    truncated = len(files) >= 1000
    result = {
        "inbox": str(IMPORT_DIR),
        "max_upload_mb": storage.MAX_MB,
        "files": files,
    }
    if truncated:
        result["truncated"] = True
    return result


# #746: what constructicon_update(fields={...}) takes beyond its named parameters.
ITEM_FIELDS = {
    "provenance": (str, type(None)),
    "content_date": (str, int, float, type(None)),
    "highlight": (bool,),
    "is_brand_asset": (bool,),
    "brand_role": (str, type(None)),
    "sensitive": (bool,),
    "transcript_vtt": (str,),  # #789: a YouTube video's subtitles (WebVTT / SRT text) -> transcript + cue times
}


def _transcript_from_vtt(slug, vtt):
    """#789: the type_metadata a subtitle file gives a video item (refused for any other type)."""
    row = items.get_item(slug)
    spec = object_types.get_object_type(row.get("media_type"))
    if getattr(spec, "text_fetch_fn", None) is None:
        raise InvalidInput(f"fields.transcript_vtt is for a YouTube video item; {slug!r} is {spec.label}. "
                           "Nothing was changed.", code="bad_transcript")
    meta = transcripts.fields_from_vtt(vtt)
    meta["transcript_note"] = "from a subtitle file handed in through the MCP"
    return meta


@mcp.tool()
def constructicon_update(slug: str, description: str | None = None, tags: list[str] | None = None,
                   display_name: str | None = None,
                   type_metadata: dict | None = None, display_date: float | None = None,
                   reset_display_date: bool = False, content_description: str | None = None,
                   fields: dict | None = None, dry_run: bool = False, short_title: str | None = None) -> dict:
    """Edit an item, every change in ONE batch. Parameters left out (None) are left alone.
    description: the uploader's note. content_description: the item's long caption/description ("" clears).
    display_name: its title override. short_title: its short name, a few words ("" clears); the title
    is display_name, else short_title, else the filename. tags: the item's full tag list (replaces its chips in the tag tree).
    type_metadata: MERGED by top-level key; a key set to "" is cleared.
    display_date: timeline position override (unix seconds); reset_display_date=true clears it.
    fields: more fields, key present = set it, absent = leave it:
      provenance: an active file key (constructicon_list_provenance_options(scope="file")) or null;
      content_date: the content's own date, ISO or unix seconds, null clears;
      highlight: bool; is_brand_asset: bool; brand_role: "logo"/"icon"/... or null;
      sensitive: bool (marking: editor+; clearing: admin);
      transcript_vtt: a YouTube video's subtitles as WebVTT or SRT text (e.g. from yt-dlp): becomes its
        transcript (searchable) WITH cue times, so constructicon_get / search can cite a &t= moment.
    E.g. fields={"provenance": "created", "highlight": true, "content_date": "2017-07-31"}.
    dry_run=true returns the item as it would be, writing nothing.
    Returns the item plus batch_id. Private notes: constructicon_set_agent_notes.
    """
    items.get_item(slug)
    extra = _fields_arg(fields, ITEM_FIELDS, "constructicon_update")
    cols = {}
    if description is not None:
        cols["description"] = description
    if display_name is not None:
        cols["display_name"] = display_name
    if content_description is not None:
        cols["content_description"] = content_description
    if short_title is not None:
        cols["short_title"] = short_title
    if type_metadata is not None:
        # #563: merge (top-level keys), same as the web route; #425 keys cleaned in core.
        cols["type_metadata"] = dict(type_metadata)
    if "transcript_vtt" in extra:  # #789: parsed (and refused) before anything is written
        cols["type_metadata"] = {**cols.get("type_metadata", {}), **_transcript_from_vtt(slug, extra["transcript_vtt"])}
    if reset_display_date:
        cols["display_date_override"] = None
    elif display_date is not None:
        cols["display_date_override"] = display_date
    for key in ("provenance", "highlight"):
        if key in extra:
            cols[key] = extra[key]
    if "content_date" in extra:
        cols["content_date"] = items.parse_date(extra["content_date"], "content_date")  # naive ISO = Mountain
    if "is_brand_asset" in extra:
        cols["is_brand_asset"] = extra["is_brand_asset"]
        if extra["is_brand_asset"] and "brand_role" not in extra:
            cols["brand_role"] = items.get_item(slug).get("brand_role")  # absent = leave the role as it is
    if "brand_role" in extra:
        cols["brand_role"] = extra["brand_role"]
    batch_id = changes.new_batch_id()
    # #746: one transaction for the whole call; a dry run is the real thing rolled back, so every
    # field is checked against the item as the earlier fields left it.
    with db.transaction(dry_run=dry_run):
        if cols or tags is not None:
            # #541 phase C: free-text tags ride in the same call, so the whole update is ONE batch.
            items.update(slug, tags=tags, batch_id=batch_id, **cols)
        if "sensitive" in extra:
            items.set_sensitive(slug, extra["sensitive"], batch_id=batch_id)
        out = {**_to_public(db.get_by_slug(slug)), "batch_id": None if dry_run else _written(batch_id)}
    if dry_run:
        out["dry_run"] = True
    return out


# --- Captioning by an agent (#588): list what needs a caption, look at it, write one ---

VIEW_OCR_CHARS = 1500  # how much OCR text constructicon_view echoes next to the picture


def _caption_context(row):
    """The cards (and their hobbies) an item sits in, so an agent captions with context."""
    cards_in, hobbies_in = [], []
    for p in db.list_projects_for_post(row["slug"]):
        cards_in.append({"slug": p.get("slug"), "title": p.get("title"), "kind": p.get("kind")})
        for h in db.list_hobbies_for_project(p["id"]):
            if h["name"] not in hobbies_in:
                hobbies_in.append(h["name"])
    return cards_in, hobbies_in


def _needs_caption(limit, include_failed):
    """#588: caption-capable items with no caption yet, with their cards and hobbies."""
    rows = captions.needs_caption(limit=limit, include_failed=include_failed)
    out = []
    for row in rows:
        cards_in, hobbies_in = _caption_context(row)
        out.append({
            "slug": row["slug"],
            "name": item_title.title_of(row),
            "type": row.get("media_type") or "image",
            "filename": row.get("filename"),
            "caption_status": (row.get("type_metadata") or {}).get("auto_caption_status"),
            "projects": cards_in,
            "hobbies": hobbies_in,
        })
    return {"items": out, "total": captions.count_needs_caption(include_failed=include_failed)}


@mcp.tool(alias_of=("constructicon_needs", "caption"))
def constructicon_list_needs_caption(limit: int = 50, include_failed: bool = True) -> dict:
    return _needs_caption(limit, include_failed)


@mcp.tool()
def constructicon_view(slug: str, size: str = "preview") -> list[McpImage | str]:
    """Look at an item: its picture as IMAGE content you can see, plus a text block (name, type,
    description, suggested caption, OCR text). size="preview" (default): up to 1024 px;
    "thumb": the 400 px thumbnail. Other types show through their renderer (a PDF's first page, an
    STL render, a video frame). Errors: bad_size, not_viewable (redacted, or no picture).
    """
    if size not in thumbnails.VIEW_SIZES:
        raise InvalidInput(f"size must be one of: {', '.join(thumbnails.VIEW_SIZES)}", code="bad_size")
    row = db.get_by_slug(slug)
    if row is None:
        raise NotFound(f"No item {slug!r}.")
    policy.require_view(row, message=f"No item {slug!r}.")  # #557
    if row["redacted"]:
        raise InvalidInput("This item was redacted: its file was removed, so there is nothing to look at.",
                           code="not_viewable")
    rendered = thumbnails.render_view(row, size)
    if rendered is None:
        # #644: say so when a parser limit stopped it (an oversized image is not "no picture").
        hit = next(iter(parse_limits.failures_for(thumbnails.limit_key(row))), None)
        if hit:
            raise InvalidInput(f"No picture of this item ({hit['what']}): {hit['message']}. It will be tried "
                               f"again in about {hit['retry_in_minutes']} min.", code="not_viewable")
        raise InvalidInput(f"{object_types.get_object_type(row.get('media_type')).label} items have no picture to view.",
                           code="not_viewable")
    policy.note_access(row, access_log.HOW_MCP_VIEW)  # #604 follow-up 7: sensitive items only
    data, mime, (width, height) = rendered
    tm = row.get("type_metadata") or {}
    lines = [f"{item_title.title_of(row)} [{slug}]",
             f"type: {row.get('media_type') or 'image'}; shown as {mime} {width}x{height}, {len(data) // 1024} KB ({size})"]
    if row.get("content_description"):
        lines.append(f"description: {row['content_description']}")
    if tm.get(captions.METADATA_KEY):
        lines.append(f"suggested caption ({tm.get(captions.STATUS_KEY) or 'unknown'}): {tm[captions.METADATA_KEY]}")
    ocr_text = (row.get("extracted_text") or "").strip()
    if ocr_text:
        shown = ocr_text[:VIEW_OCR_CHARS]
        lines.append("OCR text" + (f" (first {VIEW_OCR_CHARS} of {len(ocr_text)} characters)" if len(ocr_text) > VIEW_OCR_CHARS else "") + ":\n" + shown)
    else:
        lines.append("OCR text: none")
    return [McpImage(data=data, format=mime.split("/")[1]), "\n".join(lines)]


@mcp.tool()
def constructicon_set_caption(slug: str, text: str | None = None, title: str | None = None, accept: bool = False,
                              replace: bool = False) -> dict:
    """Write a caption and/or a short title for an item you have looked at (constructicon_view). No model runs.
    text: the long caption (1-2000 characters), the item's caption SUGGESTION (model "mcp-agent").
    title: a short title of a few words (at most 100 characters), the item's NAME on tiles; the long
    caption is never the title, so give one with every caption. Both wait in the owner's caption
    review; accept=true also sets content_description to the text and short_title to the title.
    Never overwrites silently: replacing a suggestion, description or short title that the local
    model did not write (yours, another agent's, the owner's) is refused with would_replace_suggestion
    (details.existing has each text that would go). Read it; pass replace=true only to replace it.
    E.g. set_caption(slug, text="Kendall's buggy airborne off the berm, dust trailing.",
    title="Kendall's 8IGHT-T carving the berm", accept=true).
    Errors: bad_caption, bad_title, redacted, would_replace_suggestion.
    Returns the item plus batch_id, accepted, replaced_suggestion (the suggestion text this call
    replaced, or null), replaced (every replaced text, by field) and in_review_queue.
    """
    row = items.get_item(slug)
    policy.require_view(row, message=f"No item {slug!r}.")  # #557
    result = items.set_caption(slug, text, title=title, accept=accept, replace=replace)
    it = result.item
    tm = it.get("type_metadata") or {}
    waiting_caption = bool((tm.get("auto_caption") or "").strip()) and not (it.get("content_description") or "").strip()
    waiting_title = bool((tm.get("auto_caption_title") or "").strip()) and not (it.get("short_title") or it.get("display_name"))
    return {**_to_public(it), "batch_id": result.batch_id, "accepted": bool(accept),
            "replaced_suggestion": result.data["replaced_suggestion"], "replaced": result.data["replaced"],
            "in_review_queue": waiting_caption or waiting_title}


CAPTION_REVIEW_PAGE_DEFAULT = 50  # #722: one review page; a caption can be a long paragraph


def _caption_review_rows(rows):
    """The review queue's entries for one SQL page (captions.review_entries applies the item policy),
    with the caption and description cut to previews (+ *_chars, *_truncated)."""
    out = []
    for entry in captions.review_entries(rows):
        for key in ("caption", "description"):
            text = entry.get(key) or ""
            entry[f"{key}_chars"] = len(text)
            entry[f"{key}_truncated"] = len(text) > TEXT_PREVIEW_CHARS
            if len(text) > TEXT_PREVIEW_CHARS:
                entry[key] = text[:TEXT_PREVIEW_CHARS] + _ELLIPSIS
        out.append(entry)
    return out


@mcp.tool(selector="action")
def constructicon_caption_review(action: typing.Literal["list", "accept", "retry", "skip"] = "list",
                                 slug: str | None = None, text: str | None = None, title: str | None = None,
                                 replace: bool = False, what: typing.Literal["all", "caption", "title"] = "all",
                                 advance: bool = False, limit: int | None = None,
                                 cursor: str | None = None) -> dict:
    """The owner's caption review queue (the web's Captions review page), and its buttons:
    "list": items whose caption or short-title suggestion waits for review, PAGED, newest first:
      {items: [{slug, name, kind ("caption" | "title": only a title waits), caption, caption_chars,
      caption_truncated, caption_by, description, description_chars, description_truncated, title,
      title_by, thumb_url, detail_url}], next_cursor, more, limit}. limit 1..500 (default 50); pass
      next_cursor back as cursor= (null = the end). caption / description are ~300-character previews;
      read a whole suggestion with constructicon_get(slug) (type_metadata.auto_caption).
    "accept" (slug): the suggestion becomes the description and short title, one undoable batch;
      text= / title= accept your edited wording instead. Text a local model didn't write is not
      replaced silently: would_replace_suggestion unless replace=true. -> {item, batch_id,
      replaced_suggestion, replaced}.
    "skip" (slug): reviewed, not used; it leaves the queue. what= all (default) | caption | title.
      -> {item, batch_id}.
    "retry" (slug): run the local caption model on it again (queued; type_metadata.auto_caption_status
      goes "pending"); advance=true tries the next prompt for variety. captions_disabled (409) on an
      install with no caption model: look at it (constructicon_view) and constructicon_set_caption.
      -> {item}.
    E.g. constructicon_caption_review(action="accept", slug="abc123").
    """
    if action == "list":
        _only_for('action="list"', slug=(slug, None), text=(text, None), title=(title, None),
                  replace=(replace, False), what=(what, "all"), advance=(advance, False))
        return _paged("captions", lambda n, o: db.list_unaccepted_captions(limit=n, offset=o), limit, cursor,
                      TEXT_PREVIEW_CHARS, shape=_caption_review_rows, default=CAPTION_REVIEW_PAGE_DEFAULT)
    _only_for(f'action="{action}"', limit=(limit, None), cursor=(cursor, None))
    if not (slug or "").strip():
        raise InvalidInput(f'action="{action}" needs slug (the item).')
    if action == "accept":
        _only_for('action="accept"', what=(what, "all"), advance=(advance, False))
        res = captions.accept(slug, text, title, replace)
        return {"ok": True, "item": _to_public(res.item), "batch_id": res.batch_id,
                "replaced_suggestion": res.data["replaced_suggestion"], "replaced": res.data["replaced"]}
    if action == "skip":
        _only_for('action="skip"', text=(text, None), title=(title, None), replace=(replace, False),
                  advance=(advance, False))
        res = captions.skip(slug, what)
        return {"ok": True, "item": _to_public(db.get_by_slug(slug)), "batch_id": _written(res.batch_id)}
    if action == "retry":
        _only_for('action="retry"', text=(text, None), title=(title, None), replace=(replace, False),
                  what=(what, "all"))
        return {"ok": True, "item": _to_public(captions.retry(slug, advance=advance))}
    raise InvalidInput(f"Unknown action {action!r}: use list, accept, skip or retry.")


_DESTRUCTIVE = ToolAnnotations(destructiveHint=True)


@mcp.tool(selector="action")
def constructicon_undated(action: typing.Literal["list", "accept", "apply_auto"] = "list", slug: str | None = None,
                          source: str | None = None, include_inherited: bool = True, read_files: bool = False,
                          limit: int = 50, dry_run: bool = False) -> dict:
    """Items with no REAL date (the upload time and a file mtime from after the install's first upload
    are not dates), with date guesses, so you can date them.
    "list" (default): {items, total, needs_date}. Each item: slug, name, type, filename, imported,
      shows (what its card says: "Undated" or "≈ <range> · from <card>"), inherits (the card range it
      borrows, or null), needs_date (true = no range to borrow, so it is in the Curator), guesses
      [{date, epoch, source, confidence, evidence, auto, label}], best first. include_inherited=false
      lists only the needs_date ones (a card's write-up never needs a date). read_files=true also reads each file's own embedded date
      (slower: one parser process per file). limit 1..500.
    "accept": write `slug`'s guess from `source` (filename | file_metadata | youtube | ocr_text |
      siblings) as its content_date, source recorded. Returns {slug, batch_id, guess}.
    "apply_auto": write every high-confidence guess (in-file evidence: filename, file_metadata,
      youtube) to the undated items, ONE batch. Returns {applied, skipped, batch_id}; dry_run=true
      writes nothing and lists what it would do.
    Every write is undoable (constructicon_undo(batch_id)); an item that already has a real date is
    never overwritten.
    """
    if action == "list":
        if not 1 <= int(limit) <= 500:
            raise InvalidInput("limit must be between 1 and 500.", code="bad_limit")
        snap = item_dates.snapshot()
        rows = policy.filter_visible(item_dates.undated_rows(include_inherited=include_inherited, snap=snap))
        if not include_inherited:  # the Curator's set exactly (a card's write-up never needs a date)
            rows = [r for r in rows if item_dates.needs_date(r, snap=snap)]
        access = policy.card_access()  # #604: never name a card the caller can't see
        entries = [item_dates.listing_entry(r, read_file=read_files, snap=snap, access=access) for r in rows[:int(limit)]]
        return {"items": entries, "total": len(rows), "needs_date": sum(1 for e in entries if e["needs_date"])}
    if action == "accept":
        if not slug or not source:
            raise InvalidInput("accept needs slug and source.", code="bad_request")
        policy.writable_item(slug, message=f"No item {slug!r}.")  # #730: hidden = forbidden, saying why
        res = items.accept_date_guess(slug, source, dry_run=dry_run)
        return {"slug": slug, "batch_id": None if dry_run else _written(res.batch_id), "guess": res.data["guess"],
                **({"dry_run": True} if dry_run else {})}
    if action == "apply_auto":
        plan = item_dates.auto_plan(read_files=True)
        visible = {r["slug"] for r in policy.filter_visible([r for r, _g in plan])}
        plan = [(r, g) for r, g in plan if r["slug"] in visible]
        if not plan:
            return {"applied": [], "skipped": [], "batch_id": None, "message": "No undated item has a high-confidence guess."}
        res = items.apply_dates([(r["slug"], g.epoch, item_dates.source_record(g)) for r, g in plan], dry_run=dry_run)
        guesses = {r["slug"]: g.as_dict() for r, g in plan}
        return {"applied": [{"slug": s, **guesses[s]} for s in res.data["applied"]], "skipped": res.data["skipped"],
                "batch_id": None if dry_run else _written(res.batch_id), **({"dry_run": True} if dry_run else {})}
    raise InvalidInput(f"Unknown action {action!r}: use list, accept or apply_auto.")


def _redact(slug):
    result = items.redact(slug)
    return {**_to_public(result.item), "batch_id": result.batch_id}


def _unredact(slug):
    return _to_public(items.unredact(slug).item)


def _recover_redacted(slug):
    result = items.recover_redacted(slug)
    return {**_to_public(result.item), "batch_id": result.batch_id}


def _delete_redacted_file(slug, confirm):
    result = items.delete_redacted_file(slug, confirm)
    return {"deleted": True, "slug": slug, "bytes": result.data["bytes"]}


@mcp.tool(selector="action", annotations=_DESTRUCTIVE)
def constructicon_redact(slug: str, action: typing.Literal["redact", "unredact", "recover", "delete_file"] = "redact",
                         confirm: bool = False) -> dict:
    """Remove an item's file but keep its metadata (sensitive-content cleanup), and what follows:
    "redact" (default): the file is HELD in the trash, no expiry (empty-trash skips it), and the
      item is hidden from every listing. -> the item + batch_id.
    "recover": file back, un-redacted, exactly as before. -> the item + batch_id.
      Error no_redact_hold (the file was already deleted for good).
    "delete_file": PERMANENTLY erase the held file; needs confirm=true; NOT undoable. The item
      stays redacted, metadata only. -> {deleted: true, slug, bytes}. Error confirm_required.
    "unredact": visibility only, for a redaction whose file is already gone (refused with
      redact_hold_exists while a file is held: recover or delete_file first). -> the item.
    Errors: no_file, not_redacted. List them: constructicon_list_items(which="redacted").
    """
    if action == "redact":
        _only_for('action="redact"', confirm=(confirm, False))
        return _redact(slug)
    if action == "unredact":
        _only_for('action="unredact"', confirm=(confirm, False))
        return _unredact(slug)
    if action == "recover":
        _only_for('action="recover"', confirm=(confirm, False))
        return _recover_redacted(slug)
    if action == "delete_file":
        return _delete_redacted_file(slug, confirm)
    raise InvalidInput(f"Unknown action {action!r}: use redact, unredact, recover or delete_file.")


@mcp.tool(alias_of=("constructicon_redact", "unredact"))
def constructicon_unredact(slug: str) -> dict | None:
    return _unredact(slug)


@mcp.tool(alias_of=("constructicon_redact", "recover"))
def constructicon_recover_redacted(slug: str) -> dict:
    return _recover_redacted(slug)


@mcp.tool(alias_of=("constructicon_redact", "delete_file"))
def constructicon_delete_redacted_file(slug: str, confirm: bool = False) -> dict:
    return _delete_redacted_file(slug, confirm)


def _list_redacted(text_chars=TEXT_PREVIEW_CHARS):
    """Every redacted item (#282): the only listing that includes them (an admin listing)."""
    return [_to_public_listed(r, text_chars=text_chars) for r in db.list_redacted()]


def _list_restricted(text_chars=TEXT_PREVIEW_CHARS):
    """Every sensitive item (#443 types, #603 flags), with the cards each is on (an admin listing)."""
    return [
        {**_to_public_listed(r, text_chars=text_chars),
         "projects": [{"title": p["title"], "slug": p["slug"]} for p in db.list_projects_for_post(r["slug"])]}
        for r in db.list_restricted()
    ]


def _list_brand_assets(text_chars=TEXT_PREVIEW_CHARS):
    assets = policy.filter_visible(db.list_brand_assets())  # #557
    return [
        {**_to_public_listed(asset, text_chars=text_chars), "is_brand_asset": bool(asset.get("is_brand_asset")),
         "brand_role": asset.get("brand_role")}
        for asset in assets
    ]


# #722: paged item lists (which="tag" | "unfiled"): `limit` rows per page, read in SQL (never the
# whole list at once), and an opaque `cursor` naming the view and where the next page starts.
LIST_PAGE_DEFAULT = 100
LIST_PAGE_MAX = 500


def _page_limit(limit, default=None):
    if limit is None:
        return default or LIST_PAGE_DEFAULT
    if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= LIST_PAGE_MAX:
        raise InvalidInput(f"limit must be a whole number from 1 to {LIST_PAGE_MAX}.", code="bad_limit")
    return limit


def _make_cursor(view, offset):
    raw = json.dumps({"v": view, "o": offset}, separators=(",", ":")).encode()
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def _read_cursor(cursor, view):
    """The offset a cursor continues from (0 for none). bad_cursor when it isn't one of ours;
    cursor_view_mismatch when it belongs to another list (another tag, unfiled vs a tag)."""
    if cursor is None or cursor == "":
        return 0
    try:
        data = json.loads(base64.urlsafe_b64decode(str(cursor) + "=" * (-len(str(cursor)) % 4)))
        offset = data["o"]
        if isinstance(offset, bool) or not isinstance(offset, int) or offset < 0:
            raise ValueError("bad offset")
    except (ValueError, TypeError, KeyError, binascii.Error) as e:
        raise InvalidInput("cursor is not a next_cursor this tool gave out. Start again without it.",
                           code="bad_cursor") from e
    if data.get("v") != view:
        raise InvalidInput("cursor belongs to another list (another which= or tag). Use the next_cursor of "
                           "this same list, or start again without it.", code="cursor_view_mismatch")
    return offset


def _paged(view, fetch, limit, cursor, text_chars, shape=None, default=None):
    """One page: {items (listed shape), next_cursor (null at the end), more, limit}. `fetch(limit,
    offset)` reads rows in SQL (the policy's browse clause applies there); the per-item policy then
    drops any it hides, so a page can hold fewer than `limit` items while `more` is still true.
    `shape(rows)` (optional) replaces the listed shape and must apply the item policy itself."""
    limit = _page_limit(limit, default)
    offset = _read_cursor(cursor, view)
    rows = fetch(limit + 1, offset)
    more = len(rows) > limit
    page = rows[:limit]
    items_out = shape(page) if shape is not None else         [_to_public_listed(r, text_chars=text_chars) for r in policy.filter_visible(page)]
    return {"items": items_out,
            "next_cursor": _make_cursor(view, offset + limit) if more else None, "more": more, "limit": limit}


def _tag_or_404(tag_name):
    tag = tags_svc.find_any(tag_name)  # #563: a lookup must not create the tag
    if tag is None:
        raise NotFound(f"No such tag: {tag_name!r}")
    return tag


def _posts_for_tag(tag_name, text_chars, limit=None, cursor=None):
    """One page of the items tagged `tag_name` or any tag under it (#722: paged in SQL)."""
    tag = _tag_or_404(tag_name)
    return _paged(f"tag:{tag['id']}", lambda n, o: db.list_posts_for_tag(tag["id"], limit=n, offset=o),
                  limit, cursor, text_chars)  # the policy's browse clause applies in db; filter_visible in _paged


def _unfiled(text_chars, limit=None, cursor=None):
    """#556: one page of the items on no card: the /unfiled page's list (db.list_unfiled_items, its
    default view: no brand assets, current revisions only), newest first."""
    return _paged("unfiled", lambda n, o: db.list_unfiled_items(limit=n, offset=o), limit, cursor, text_chars)


def _similar(slug, text_chars):
    """#556: look-alikes of one item, as GET /api/image/{slug}/similar finds them (core/similarity.py,
    read-only), each with similarity_reason (visual | text | both) and similarity_score."""
    _viewable_item(slug)
    out = []
    for m in similarity.find_similar(slug):
        row = db.get_by_slug(m["slug"])
        if row is None or not policy.can_view(row):  # #557
            continue
        out.append({**_to_public_listed(row, text_chars=text_chars), "similarity_reason": m["reason"],
                    "similarity_score": round(m["score"], 3)})
    return out


@mcp.tool(selector="which")
def constructicon_list_items(which: typing.Literal["redacted", "restricted", "brand_assets", "tag", "unfiled", "similar"],
                             tag: str | None = None, text_chars: int = TEXT_PREVIEW_CHARS, slug: str | None = None,
                             limit: int | None = None, cursor: str | None = None) -> list[dict] | dict:
    """List items of one kind (listed shape: text previews, big type_metadata values trimmed):
    "tag": everything tagged `tag` or any tag under it (walks the tag tree; tag required). PAGED.
    "unfiled": the items on no card, as the web's Unfiled page lists them (no brand assets, current
      revisions), newest first. PAGED.
    "similar": look-alikes of the item `slug` (required): same picture re-saved, or similar text;
      each with similarity_reason (visual | text | both) and similarity_score; best first. A list.
    "brand_assets": logos, icons and other brand assets, with is_brand_asset and brand_role.
    "redacted": every redacted item (the only listing that has them; admin).
    "restricted": every sensitive item (keys, certificates, items marked sensitive), each with
      `projects` [{title, slug}] and `sensitive_reason` (admin).
    PAGED lists answer {items, next_cursor, more, limit}: limit 1..500 (default 100); pass
    next_cursor back as cursor= for the next page (null = the end). A page can hold fewer than limit
    items while more is true (items you can't see are left out).
    E.g. constructicon_list_items(which="tag", tag="3D Printing", limit=200).
    """
    chars = text_chars_arg(text_chars)
    if which == "tag":
        _only_for('which="tag"', slug=(slug, None))
        if not (tag or "").strip():
            raise InvalidInput('which="tag" needs tag (a tag name).')
        return _posts_for_tag(tag, chars, limit, cursor)
    if which == "unfiled":
        _only_for('which="unfiled"', tag=(tag, None), slug=(slug, None))
        return _unfiled(chars, limit, cursor)
    if which == "similar":
        _only_for('which="similar"', tag=(tag, None), limit=(limit, None), cursor=(cursor, None))
        if not (slug or "").strip():
            raise InvalidInput('which="similar" needs slug (the item to find look-alikes of).')
        return _similar(slug, chars)
    _only_for(f'which="{which}"', tag=(tag, None), slug=(slug, None), limit=(limit, None), cursor=(cursor, None))
    if which == "redacted":
        return _list_redacted(chars)
    if which == "restricted":
        return _list_restricted(chars)
    if which == "brand_assets":
        return _list_brand_assets(chars)
    raise InvalidInput(f"Unknown which {which!r}: use tag, unfiled, similar, brand_assets, redacted or restricted.")


@mcp.tool(alias_of=("constructicon_list_items", "redacted"))
def constructicon_list_redacted() -> list[dict]:
    return _list_redacted()


@mcp.tool(alias_of=("constructicon_list_items", "restricted"))
def constructicon_list_restricted() -> list[dict]:
    return _list_restricted()


def _delete_target(args):
    """Which delete a call asks for (its role row in ACTION_ROLES)."""
    if args.get("everything"):
        return "everything"
    return next((k for k in ("slugs", "card", "blog_entry") if args.get(k) is not None), "slug")


def _delete_card(card, dry_run, batch_id):
    res = cards.delete_card(card, dry_run=dry_run, batch_id=batch_id)
    return {**res.to_dict(), **res.data}


@mcp.tool(selector=(_delete_target, ("slug", "slugs", "card", "blog_entry", "everything")),
          annotations=_DESTRUCTIVE)
def constructicon_delete(slug: str | None = None, slugs: list[str] | None = None, card: str | int | None = None,
                         blog_entry: str | None = None, everything: bool = False, confirm: str | None = None,
                         missing_ok: bool = False, dry_run: bool = False, batch_id: str | None = None) -> dict:
    """Delete ONE kind of thing per call (undoable unless said otherwise):
    slug="x": an item, with its card/tag/relation/revision/blog links and open questions. The file
      goes to the trash for 7 days; undo restores all of it (after that: trash_expired).
      -> {deleted: true, batch_id, expires_at}.
    slugs=[...]: several items in one batch; unknown slugs are not_found unless missing_ok=true
      (then skipped). -> {deleted: <count>, slugs, batch_id, expires_at}.
    card=<id or slug>: a card. Its blank write-up goes too (a written one stays, with a warning);
      links, memberships and blog attachments go; nested cards stand alone; files stay.
      -> {ok, dry_run, changes, warnings, batch_id, deleted, children_orphaned, items_detached, writeup}.
    blog_entry=<slug>: a blog entry (its cards and files stay). -> {deleted: true, slug, batch_id}.
    everything=true AND confirm="DELETE EVERYTHING", nothing else: WIPE THE WHOLE ARCHIVE (items,
      files, cards, tags, hobbies, blog, questions, trash). PERMANENT, NOT undoable; settings,
      users and the change log stay. Either one alone is refused. Back up first
      (constructicon_backup). -> {deleted, counts, files_removed, trash_removed, batch_id}.
    dry_run and batch_id work for every kind except everything.
    """
    targets = [k for k, v in (("slug", slug), ("slugs", slugs), ("card", card), ("blog_entry", blog_entry))
               if v is not None]
    if everything:
        if targets:
            raise InvalidInput(f"everything=true wipes the whole archive and takes no other target (got "
                               f"{', '.join(targets)}). Nothing was deleted.")
        if confirm != reset.CONFIRM_PHRASE:
            raise errors.AppError("confirm_required", "Wiping the whole archive needs BOTH everything=true AND "
                                  f"confirm=\"{reset.CONFIRM_PHRASE}\". Nothing was deleted.", status=400)
        _only_for("everything=true (it is permanent: no dry run, no batch to join)",
                  dry_run=(dry_run, False), batch_id=(batch_id, None), missing_ok=(missing_ok, False))
        return reset.delete_everything(confirm)
    if confirm is not None:
        raise InvalidInput(f"confirm is only for everything=true (the whole-archive wipe); "
                           f"{'without everything=true it wipes nothing' if confirm == reset.CONFIRM_PHRASE else 'it is not needed here'}. "
                           "Nothing was deleted.")
    if len(targets) != 1:
        raise InvalidInput("Pass exactly one of slug, slugs, card or blog_entry (or everything=true with "
                           f"confirm=\"{reset.CONFIRM_PHRASE}\"). Nothing was deleted.")
    target = targets[0]
    if target != "slugs":
        _only_for(f"{target}=", missing_ok=(missing_ok, False))
    extra = {"dry_run": True} if dry_run else {}
    if target == "slug":
        result = items.delete([slug], dry_run=dry_run, batch_id=batch_id)
        return {"deleted": True, "batch_id": result.batch_id, "expires_at": result.data["expires_at"], **extra}
    if target == "slugs":
        if isinstance(slugs, str):
            raise InvalidInput("slugs must be a list; for one item pass slug=.")
        result = items.delete(slugs, missing_ok=missing_ok, dry_run=dry_run, batch_id=batch_id)
        return {"deleted": result.data["deleted"], "slugs": result.data["slugs"], "batch_id": result.batch_id,
                "expires_at": result.data["expires_at"], **extra}
    if target == "card":
        return _delete_card(card, dry_run, batch_id)
    res = blog.delete(blog_entry, dry_run=dry_run, batch_id=batch_id)
    return {"deleted": True, "slug": res.data["deleted"], "batch_id": res.batch_id, **extra}


@mcp.tool(alias_of=("constructicon_delete", "slugs"))
def constructicon_delete_multiple(slugs: list[str]) -> dict:
    result = items.delete(slugs, missing_ok=True)
    return {"deleted": result.data["deleted"], "batch_id": result.batch_id}


@mcp.tool(alias_of=("constructicon_delete", "everything"), annotations=_DESTRUCTIVE)
def constructicon_delete_all(confirm: str = "") -> dict:
    return reset.delete_everything(confirm)


def _empty_trash(confirm):
    r = items.empty_trash(confirm)
    return {**r, "message": f"Purged {r['purged']} deleted item(s) ({r['bytes']} bytes). "
                            f"{r['held_kept']} redacted file(s) are still held (not touched)."}


@mcp.tool(selector="action", annotations=_DESTRUCTIVE)
def constructicon_trash(action: typing.Literal["list", "empty"] = "list", confirm: str | None = None) -> dict:
    """The trash. Deleted items' files wait there 7 days (undo brings them back); redacted files
    are held until the owner decides.
    "list" (default): {count, bytes, oldest, next_expiry, days, items: [{slug, title, batch_id,
      reason, size_bytes, created_at, expires_at}], held: {count, bytes, items}}.
    "empty": PERMANENTLY purge every ordinary deleted file now; needs confirm="EMPTY TRASH".
      Redact holds are not touched; those deletes can no longer be undone.
      -> {purged, bytes, slugs, held_kept, message}. Error confirm_required.
    """
    if action == "list":
        _only_for('action="list"', confirm=(confirm, None))
        return items.trash_summary()
    if action == "empty":
        return _empty_trash(confirm or "")
    raise InvalidInput(f"Unknown action {action!r}: use list or empty.")


@mcp.tool(alias_of=("constructicon_trash", "list"))
def constructicon_list_trash() -> dict:
    return items.trash_summary()


@mcp.tool(alias_of=("constructicon_trash", "empty"))
def constructicon_empty_trash(confirm: str) -> dict:
    return _empty_trash(confirm)


@mcp.tool(selector="action", annotations=_DESTRUCTIVE)
def constructicon_duplicates(action: typing.Literal["plan", "apply"] = "plan", dry_run: bool = True,
                             expect_trash: list[str] | None = None) -> dict:
    """Byte-identical duplicate files: the same stored bytes under several items.
    "plan" (default, read-only): {summary, summary_text, groups: [{sha256, size_bytes, keep, why,
      trash: [{slug, title, filename, date, cards, ...}], inherit: {cards, tags, free_text_tags,
      fields, flags}, in_revision_chain}], skipped, unreadable}. The survivor is the copy with the
      earliest real date from before Constructicon's first upload (ties: more cards, more metadata,
      older upload).
    "apply": folds every group into its survivor (it inherits the copies' cards, tags, blank text
      fields, flags, related links, covers) and trashes the copies (7-day undo), ONE batch.
      DRY-RUN BY DEFAULT (dry_run=false writes). expect_trash = the slugs a dry run listed: if
      the fresh plan differs it refuses (plan_changed) and writes nothing. Admin only.
      -> {ok, dry_run, batch_id, changes, trashed, kept, expires_at, summary, groups}.
    """
    if action == "plan":
        _only_for('action="plan"', dry_run=(dry_run, True), expect_trash=(expect_trash, None))
        built = duplicates.plan()
        return {**built, "summary_text": duplicates.summary_text(built)}
    if action == "apply":
        res = duplicates.apply(dry_run=dry_run, expect_trash=expect_trash).to_dict()
        res["summary_text"] = duplicates.summary_text(res)
        return res
    raise InvalidInput(f"Unknown action {action!r}: use plan or apply.")


@mcp.tool(selector="action", annotations=_DESTRUCTIVE)
def constructicon_lookalikes(action: typing.Literal["list", "confirm", "dismiss"] = "list", keep: str | None = None,
                             slugs: list[str] | None = None, dry_run: bool = True, limit: int | None = None,
                             cursor: str | None = None) -> dict:
    """Look-alike photos: the same picture under different bytes (resized, recompressed, re-exported),
    found by perceptual hash and never folded without a yes. Byte-identical copies are
    constructicon_duplicates' job; items in one revision chain are never look-alikes.
    "list" (default, read-only, paged: limit 1..500, default 20, cursor): {items: [{id, slugs, keep,
      why, members: [{slug, title, filename, width, height, file_size, date, date_is_real, uploaded,
      cards, copy_marker}], pairs: [{a, b, distance}], answered, new_members}], next_cursor, more,
      limit, summary}. `keep` is the suggested keeper (same rule as duplicates), `why` says what
      decided; `answered` = pairs already said "Not the same", `new_members` = what is being asked now.
    "confirm" and "dismiss" are DRY-RUN BY DEFAULT: they answer as the write would and write nothing;
      pass dry_run=false to fold / record.
    "confirm" (keep=<slug>, slugs=optional subset of the group): "same photo, keep this one". The kept
      photo inherits the others' cards, tags, blank text fields, flags, related links and covers; the
      others go to the trash (7-day undo), ONE batch. A member answered "Not the same" against the
      keeper is left out (left_out). -> {ok, dry_run, batch_id, kept, trashed, left_out, plan, ...}.
      Refusals: not_found, forbidden, not_in_group, lookalike_conflict, nothing_to_fold.
    "dismiss" (slugs=[2+ photos of one group]): "not the same", remembered per pair so it is never
      asked again. -> {ok, batch_id, pairs}. Refusal: not_lookalikes.
    Example: constructicon_lookalikes(action="confirm", keep="abc123") (a dry run), then again with
    dry_run=False to fold.
    """
    if action == "list":
        _only_for('action="list"', keep=(keep, None), slugs=(slugs, None), dry_run=(dry_run, True))
        built = lookalikes.plan()
        groups = built["groups"]
        page = _paged("lookalikes", lambda n, o: groups[o:o + n], limit, cursor, 0, shape=lambda rows: rows,
                      default=20)
        return {**page, "summary": built["summary"], "unreadable": built["unreadable"]}
    _only_for(f'action="{action}"', limit=(limit, None), cursor=(cursor, None))
    if action == "confirm":
        return lookalikes.confirm(keep, slugs, dry_run=dry_run).to_dict()
    if action == "dismiss":
        _only_for('action="dismiss"', keep=(keep, None))
        return lookalikes.dismiss(slugs, dry_run=dry_run).to_dict()
    raise InvalidInput(f"Unknown action {action!r}: use list, confirm or dismiss.")


@mcp.tool(selector="action")
def constructicon_proposals(which: typing.Literal["provenance", "correlation"],
                            action: typing.Literal["list", "confirm", "dismiss"] = "list",
                            slugs: list[str] | None = None, pairs: list[list[str]] | None = None,
                            rule: str | None = None, dry_run: bool = True, limit: int | None = None,
                            cursor: str | None = None) -> dict:
    """The Curator's proposals that wait for a yes: nothing is written until confirmed.
    which="provenance": file provenance by gross rule. The first (and only) rule, text_heavy_reference:
      a PDF with 6,000+ characters of text, or a PDF / Word file whose name says manual, guide, datasheet,
      handbook, instructions, reference or spec, proposed as provenance "reference". A file whose
      provenance is already set is never proposed.
    which="correlation": card pairs that look related (text similarity of their files, shared tags,
      overlapping dates, shared files; score 0..1, suggested at 0.45+), never ones already linked,
      nested, sharing a parent or a family, or a family / collection.
    "list" (default, read-only, paged: limit 1..500, default 20, cursor): provenance -> {items: [{slug,
      title, filename, media_type, chars, reason, evidence}], rule, blocked, summary}; correlation ->
      {items: [{a, b, a_title, b_title, score, parts, evidence, why}], summary}.
    "confirm" and "dismiss" are DRY-RUN BY DEFAULT; pass dry_run=false to write. One call = one batch,
      one constructicon_undo(batch_id) reverses it.
    "confirm": provenance writes the rule's provenance on `slugs` (default: every current proposal);
      correlation links `pairs` ([[a, b], ...], card slugs; default: every current suggestion) as
      "related". -> {ok, dry_run, batch_id, applied | linked, skipped}. Refusals: forbidden, not_found,
      not_proposed, nothing_to_confirm, rule_blocked, bad_rule.
    "dismiss" ("not this one" / "not related", remembered so it is never proposed again): provenance
      `slugs`, correlation `pairs`. -> {ok, batch_id, slugs | pairs}. Refusal: not_proposed.
    Example: constructicon_proposals(which="provenance") then
    constructicon_proposals(which="provenance", action="confirm", slugs=["abc"], dry_run=False).
    """
    if which not in ("provenance", "correlation"):
        raise InvalidInput(f"Unknown which {which!r}: use provenance or correlation.")
    if which == "correlation":
        _only_for('which="correlation"', slugs=(slugs, None), rule=(rule, None))
    else:
        _only_for('which="provenance"', pairs=(pairs, None))
    if action == "list":
        _only_for('action="list"', slugs=(slugs, None), pairs=(pairs, None), dry_run=(dry_run, True))
        if which == "provenance":
            built = provenance_rules.plan(rule=rule)
            rp = built["rules"][0] if rule else None
            props = rp["proposals"] if rp else [p for r in built["rules"] for p in r["proposals"]]
            page = _paged(f"proposals:provenance:{rule or 'all'}", lambda n, o: props[o:o + n], limit, cursor, 0,
                          shape=lambda rows: rows, default=20)
            return {**page, "summary": built["summary"],
                    "rules": [{**r["rule"], "blocked": r["blocked"], "summary": r["summary"]} for r in built["rules"]]}
        built = correlations.plan()
        found = built["pairs"]
        page = _paged("proposals:correlation", lambda n, o: found[o:o + n], limit, cursor, 0,
                      shape=lambda rows: rows, default=20)
        return {**page, "summary": built["summary"]}
    _only_for(f'action="{action}"', limit=(limit, None), cursor=(cursor, None))
    if which == "provenance":
        if action == "confirm":
            return provenance_rules.confirm(slugs, rule=rule, dry_run=dry_run).to_dict()
        if action == "dismiss":
            return provenance_rules.dismiss(slugs, rule=rule, dry_run=dry_run).to_dict()
    else:
        if action == "confirm":
            return correlations.confirm(pairs, dry_run=dry_run).to_dict()
        if action == "dismiss":
            return correlations.dismiss(pairs, dry_run=dry_run).to_dict()
    raise InvalidInput(f"Unknown action {action!r}: use list, confirm or dismiss.")


@mcp.tool()
def constructicon_backup() -> dict:
    """Back up the whole database and storage to a timestamped zip:
    {filename, path, size, created_at, integrity}."""
    return backup.create_backup()


@mcp.tool()
def constructicon_add_content(media_type: str, external_url: str | None = None, content_description: str | None = None,
                           description: str = "", tags: list[str] | None = None,
                           uploaded_by: str = db.SOURCE_AUTHORED, sensitive: bool = False) -> dict:
    """Create an item with no uploaded file: a YouTube link, a web page, a written post.
    media_type must be a type that takes no file (e.g. "youtube"); external_url its link;
    content_description its own title. Files go through constructicon_upload. sensitive locks it
    from the start. uploaded_by is a free-text Source note, never who added it (the token's user).
    Text extraction runs in the background. Error: content_refused.
    """
    result = ingest.ingest_content(
        source=uploaded_by,
        run_background=_run_in_thread,
        media_type=media_type,
        external_url=external_url,
        content_description=content_description,
        description=description,
        tags=tags,
        sensitive=sensitive,
    )

    if result.error:
        error_msg = result.error
        if error_msg.endswith("require a file upload"):
            error_msg += " — use constructicon_upload"
        raise InvalidInput(error_msg, code="content_refused")
    return _to_public(result.row)


@mcp.tool(alias_of=("constructicon_update", None))
def constructicon_set_sensitive(slug: str, sensitive: bool = True, dry_run: bool = False) -> dict:
    result = items.set_sensitive(slug, sensitive, dry_run=dry_run)
    return {"ok": True, "dry_run": dry_run, "batch_id": result.batch_id, "slug": slug,
            "sensitive": result.data["sensitive"], "changed": result.data["changed"],
            "item": _to_public(result.item) if result.item else None}


def _relate(slug, other):
    policy.viewable_rows([slug, other])  # #709: missing = not_found, not visible = forbidden (says why)
    items.relate(slug, other)  # #541 phase C: one undoable batch (link + what it shares)
    return _related_listed(slug)


def _unrelate(slug, other):
    items.unrelate(slug, other)  # a door in core (#709)
    return _related_listed(slug)


@mcp.tool(selector="action")
def constructicon_related(slug: str, other: str, action: typing.Literal["add", "remove"] = "add") -> list[dict]:
    """Link two items as related (both directions), or unlink them.
    "add" (default): also shares their tags and card memberships both ways, as one batch (undo
      takes all of it back). "remove": the link only; what it shared stays.
    Returns the items now related to `slug` (listed shape). To read them:
    constructicon_get(slug, include=["related"]).
    """
    if action == "add":
        return _relate(slug, other)
    if action == "remove":
        return _unrelate(slug, other)
    raise InvalidInput(f"Unknown action {action!r}: use add or remove.")


@mcp.tool(alias_of=("constructicon_related", "add"))
def constructicon_add_related(slug: str, related_slug: str) -> list[dict]:
    return _relate(slug, related_slug)


@mcp.tool(alias_of=("constructicon_related", "remove"))
def constructicon_remove_related(slug: str, related_slug: str) -> list[dict]:
    return _unrelate(slug, related_slug)


@mcp.tool()
def constructicon_list_projects(kind: str | None = None, activity: str | None = None,
                                stage: str | None = None) -> list[dict]:
    """All cards, most recently updated first. Filters: kind (project | thing | action | family |
    collection | event), activity (active | inactive), stage (in_progress | in_use | idea | paused |
    done | stopped). Each card has its status fields and labels, and needs_input (an open question).
    """
    return [_to_public_project(p) for p in  # #604 step 3: not the private cards this caller isn't on
            policy.filter_visible_cards(db.list_projects(kind=kind, activity=activity, stage=stage))]


@mcp.tool()
def constructicon_create_project(title: str, description: str = "", cover_slug: str | None = None, parent_id: int | None = None,
                                 kind: str | None = None, stage: str | None = None, stop_reason: str | None = None) -> dict:
    """Create a card, with a root tag of the same name linked to it and a blank write-up, as one
    batch. kind: project (default) | thing | action | family | collection | event. stage:
    in_progress (default) | in_use | idea | paused | done | stopped (stopped needs stop_reason
    failed | abandoned). parent_id makes it "part of" that card (a family/collection can't nest or
    be a parent: nest_group_kind). Errors: bad_status, not_found. Returns the card + batch_id.
    """
    # #541 phase D: core validates everything first; tag + card + write-up are ONE undoable batch.
    result = cards.create(title, description=description, cover_slug=cover_slug, parent=parent_id,
                          kind=kind, stage=stage, stop_reason=stop_reason)
    return {**_to_public_project(result.data["card"]), "batch_id": result.batch_id}


# #746: what constructicon_update_project(fields={...}) takes beyond its named parameters.
CARD_FIELDS = {
    "activity": (str, type(None)),
    "force_kind": (bool,),
    "whereabouts": (str, type(None)),
    "whereabouts_note": (str, type(None)),
    "provenance": (str, type(None)),
    "provenance_credit": (str, type(None)),
    "highlight": (bool,),
    "home": (str, int, type(None)),
    "synopsis": (str, type(None)),
    "flavor": (str, type(None)),
    "writeup_slug": (str,),
    "owner_words": (bool,),
    "cover_card": (str, int),  # #554: borrow a nested child's or a member's cover (cards.set_cover)
}


def _set_writeup(project, slug, owner_words, batch_id):
    """#541 phase D: the write-up item goes on the card (no side effects), the owner-words mark, and
    writeup_slug: the caller's transaction and batch. Returns the card row after."""
    row = policy.viewable_rows([slug])[0][0]  # #709: missing = not_found, not visible = forbidden (says why)
    if not object_types.can_be_writeup(row):
        label = object_types.get_object_type(row.get("media_type")).label
        raise ValueError(f"{label} items can't be a project write-up (their type declares no writeup_body_key)")
    membership.add_files(project["id"], [slug], batch_id=batch_id, **membership.NO_EFFECTS)
    if owner_words is not None:
        items.update(slug, type_metadata={"owner_words": bool(owner_words)}, batch_id=batch_id)
    return cards.update(project["id"], writeup_slug=slug, batch_id=batch_id)


@mcp.tool()
def constructicon_update_project(project_id: str | int | None = None, title: str | None = None,
                                 description: str | None = None, cover_slug: str | None = None,
                                 status: str | None = None, start_date: float | None = None,
                                 reset_start_date: bool = False, end_date: float | None = None,
                                 reset_end_date: bool = False, kind: str | None = None,
                                 stage: str | None = None, stop_reason: str | None = None,
                                 card: str | int | None = None, fields: dict | None = None,
                                 dry_run: bool = False) -> dict:
    """Edit a card, every change in ONE batch, applied in order (kind, status, whereabouts,
    provenance, highlight, home, face text, write-up, then title and the rest).
    card (or project_id): its id or slug. Parameters left out (None) are left alone.
    kind: project | thing | action | family | collection | event. stage: in_progress | in_use
    (active) | idea | paused | done | stopped (inactive); stopped needs stop_reason failed |
    abandoned; an event can't be in_use or paused. status: the old v1 word (wip, complete,
    shelved ...), translated to a stage with a warning.
    start_date / end_date: timeline overrides (unix seconds); reset_start_date / reset_end_date clear.
    fields: more fields, key present = set it, absent = leave it; null or "" clears:
      activity (must agree with the stage), force_kind (bool: leave a family/collection that has
      members), whereabouts (have_it | partial | parted_out | sold | gifted | lost | abandoned |
      never_built; thing / project / collection only; anything but have_it greys the card's art;
      abandoned = the object was left behind (towed, impound), not stop_reason abandoned, which
      is walking away from the project), whereabouts_note, provenance (an active card key,
      constructicon_list_provenance_options(scope="card")), provenance_credit, highlight (bool),
      home ("card:<slug>" | "hobby:<slug>" | a card; null = automatic), synopsis (max 600: a few
      sentences saying what the card is; set it whenever you write a write-up), flavor (one italic
      line, max 140), writeup_slug (an item that can be a write-up; it joins the card),
      owner_words (bool: the write-up is the owner's own words), cover_card (a nested part or a
      family / collection member, id or slug: the card borrows its cover and follows it; bad_cover
      with details.reason when it can't: not_related, no_cover, cover_loop, hidden, ...).
    E.g. fields={"whereabouts": "have_it", "synopsis": "A 3D-printed rover."}.
    dry_run=true answers as the call would and writes nothing.
    Returns the card plus batch_id, changes and warnings (if any). Codes: bad_status, bad_kind,
    bad_whereabouts, bad_provenance, bad_home, bad_card_text, unknown_field.
    """
    if (project_id is None) == (card is None):
        raise InvalidInput("Pass the card as card= (or project_id=), exactly one of them.")
    ref = card if card is not None else project_id
    card_warnings = []
    project = db.get_project(ref)
    if project is None or not policy.can_view_card(project):  # #604 step 3: hidden = missing
        raise NotFound(f"No card {ref!r}.")
    f = _fields_arg(fields, CARD_FIELDS, "constructicon_update_project")
    if "force_kind" in f and not kind:
        raise InvalidInput("fields.force_kind only goes with kind=.")
    if status:
        legacy = card_rules.legacy_to_status(status)
        kind = kind or legacy["kind"]
        if not stage:
            stage, stop_reason = legacy["stage"], legacy["stop_reason"]
        card_warnings.extend(legacy["warnings"])
    new_start = None if reset_start_date else (start_date if start_date is not None else ...)
    new_end = None if reset_end_date else (end_date if end_date is not None else ...)
    cid = project["id"]
    take = _Collect()
    # #541 phase D: one transaction, one batch (one constructicon_undo reverses the whole call).
    # #746: a dry run is the same calls rolled back, so each step sees what the earlier ones did.
    batch_id = changes.new_batch_id()
    with db.transaction(dry_run=dry_run):
        if kind:
            take(cards.set_kind(cid, kind, force=f.get("force_kind", False), batch_id=batch_id))
        if stage or stop_reason or "activity" in f:
            if not (stage or stop_reason):  # activity alone: checked against the current stage
                now = db.get_project(cid)
                stage, stop_reason = now.get("stage"), now.get("stop_reason")
            take(cards.set_status(cid, stage, stop_reason, activity=f.get("activity"), batch_id=batch_id))
        if "whereabouts" in f or "whereabouts_note" in f:
            now = db.get_project(cid)
            take(cards.set_whereabouts(cid, f["whereabouts"] if "whereabouts" in f else now.get("whereabouts"),
                                       f["whereabouts_note"] if "whereabouts_note" in f else ...,
                                       batch_id=batch_id))
        if "provenance" in f or "provenance_credit" in f:
            now = db.get_project(cid)
            take(cards.set_provenance(cid, f["provenance"] if "provenance" in f else now.get("provenance"),
                                      f["provenance_credit"] if "provenance_credit" in f else ...,
                                      batch_id=batch_id))
        if "highlight" in f:
            take(cards.set_highlight(cid, f["highlight"], batch_id=batch_id))
        if "home" in f:
            take(cards.set_home(cid, f["home"], batch_id=batch_id))
        if "synopsis" in f or "flavor" in f:
            take(cards.set_text(cid, synopsis=f.get("synopsis", ...), flavor=f.get("flavor", ...), batch_id=batch_id))
        if "writeup_slug" in f:
            take(_set_writeup(project, f["writeup_slug"], f.get("owner_words"), batch_id))
        elif "owner_words" in f:
            current = db.get_project(cid).get("writeup_slug")
            if not current:
                raise InvalidInput("This card has no write-up yet: pass fields.writeup_slug too.")
            take(items.update(current, type_metadata={"owner_words": f["owner_words"]}, batch_id=batch_id))
        take(cards.update(cid, title=title, description=description, cover_slug=cover_slug,
                          start=new_start, end=new_end, batch_id=batch_id))
        if "cover_card" in f:
            if cover_slug:
                raise InvalidInput("Pass cover_slug or fields.cover_card, not both: a card has one cover.")
            take(cards.set_cover(cid, from_card=f["cover_card"], batch_id=batch_id))
        result = {**_to_public_project(db.get_project(cid)), "batch_id": None if dry_run else batch_id}
    collected = take.result()
    result["changes"] = collected["changes"]
    card_warnings.extend(collected["warnings"])
    if card_warnings:
        result["warnings"] = card_warnings
    if dry_run:
        result["dry_run"] = True
    return result


def _add_to_project(slug, project_id):
    policy.viewable_rows([slug])  # #709: missing = not_found, not visible = forbidden (says why)
    if not policy.can_view_card(db.get_project(project_id)):  # #604 step 3: hidden = missing
        raise NotFound(f"No card {project_id!r}.")
    membership.add_files(project_id, [slug], **membership.UI_EFFECTS)  # #541 phase C: same flags as the web
    return [_to_public_project(p) for p in db.list_projects_for_post(slug)]


def _remove_from_project(slug, project_id):
    policy.viewable_rows([slug])  # #709: missing = not_found, not visible = forbidden (says why)
    project = db.get_project(project_id)
    if project is None or not policy.can_view_card(project):  # #604 step 3: hidden = missing
        raise NotFound(f"No card {project_id!r}.")
    membership.remove_files(project["id"], [slug])  # membership only: tags and cover stay
    return [_to_public_project(p) for p in db.list_projects_for_post(slug)]


@mcp.tool(selector="action")
def constructicon_card_files(card: str | int, slugs: list[str],
                             action: typing.Literal["add", "remove", "move", "copy"] = "add",
                             from_card: str | int | None = None, missing_ok: bool = False,
                             dry_run: bool = False, batch_id: str | None = None) -> dict:
    """Put files (item slugs) on a card, take them off, or move / copy them between cards.
    "add" (default): like the web's add-to-card: the files get the card's linked tag (both tag
      stores) and a card with no cover takes the first. Unknown slugs: not_found, unless
      missing_ok=true (skipped). -> {ok, dry_run, changes, warnings, batch_id, card, slugs, added,
      already, skipped, tagged, cover, items: [the files, listed shape]}.
    "remove": membership only (tags and cover stay). -> {ok, ..., card, removed, items}.
    "move" / "copy": from from_card to card. A move drops from_card's linked tag and adds card's; a
      copy keeps the files on from_card and only adds card's tag. A card's own write-up can't move;
      bad_files if a file isn't on from_card. -> {ok, ..., tags_added, tags_removed}.
    E.g. constructicon_card_files("rover", ["a1b2", "c3d4"], action="move", from_card="misc").
    """
    if action in ("move", "copy"):
        if from_card is None:
            raise InvalidInput(f'action="{action}" needs from_card (the card the files are on now).')
        _only_for(f'action="{action}"', missing_ok=(missing_ok, False))
        op = cards.move_files if action == "move" else cards.copy_files
        return op(slugs, from_card, card, dry_run=dry_run, batch_id=batch_id).to_dict()
    if action not in ("add", "remove"):
        raise InvalidInput(f"Unknown action {action!r}: use add, remove, move or copy.")
    _only_for(f'action="{action}"', from_card=(from_card, None))
    if isinstance(slugs, str):
        raise InvalidInput("slugs must be a list of item slugs.")
    project = db.get_project(card)
    if project is None or not policy.can_view_card(project):  # #604 step 3: hidden = missing
        raise NotFound(f"No card {card!r}.")
    if not missing_ok:
        policy.viewable_rows(slugs)  # #709: unknown = not_found, hidden = forbidden (says why), before any write
    if action == "add":
        res = membership.add_files(project["id"], slugs, missing_ok=missing_ok, dry_run=dry_run, batch_id=batch_id,
                                   **membership.UI_EFFECTS)
        listed = res.data["slugs"]
    else:
        res = membership.remove_files(project["id"], slugs, dry_run=dry_run, batch_id=batch_id)
        listed = res.data["removed"]
    return {**res.to_dict(), "items": [_to_public_listed(r) for r in policy.filter_visible(
        [db.get_by_slug(s) for s in listed if db.get_by_slug(s)])]}


@mcp.tool(alias_of=("constructicon_card_files", "add"))
def constructicon_add_to_project(slug: str, project_id: str | int) -> list[dict]:
    return _add_to_project(slug, project_id)


@mcp.tool(alias_of=("constructicon_card_files", "remove"))
def constructicon_remove_from_project(slug: str, project_id: str | int) -> list[dict]:
    return _remove_from_project(slug, project_id)


@mcp.tool(alias_of=("constructicon_card_files", "add"))
def constructicon_add_items_to_project(project_id: str | int, slugs: list[str]) -> list[dict]:
    project = db.get_project(project_id)
    if project is None or not policy.can_view_card(project):  # #604 step 3: hidden = missing
        raise NotFound(f"No card {project_id!r}.")
    result = membership.add_files(project["id"], slugs, missing_ok=True, **membership.UI_EFFECTS)
    return [_to_public_listed(db.get_by_slug(slug)) for slug in slugs if slug in result.data["slugs"]]


@mcp.tool(alias_of=("constructicon_update_project", None))
def constructicon_set_project_writeup(project_id: str | int, slug: str, owner_words: bool | None = None) -> dict | None:
    project = db.get_project(project_id)
    if project is None or not policy.can_view_card(project):  # #604 step 3: hidden = missing
        raise NotFound(f"No card {project_id!r}.")
    # #541 phase D: membership (no side effects, as before) + the owner-words mark + writeup_slug
    # are one transaction and one batch.
    batch_id = changes.new_batch_id()
    with db.transaction():
        updated = _set_writeup(project, slug, owner_words, batch_id).data["card"]
    return {**_to_public_project(updated), "batch_id": batch_id}


@mcp.tool()
def constructicon_get_project(id_or_slug: str | int, text_chars: int = TEXT_PREVIEW_CHARS,
                              full_writeup: bool = False) -> dict | None:
    """One card in full: its fields and live status (kind, activity, stage, stop_reason + labels,
    needs_input), items (with tags; text previews), cover, write-up (a preview like the items: its
    text and type_metadata.body cut to text_chars (at least 300), with body_chars and body_truncated; full_writeup=true
    sends it whole, or read it with constructicon_get(writeup_slug)), open_decisions, families /
    members / children, links (both directions), its face
    text (synopsis, flavor, writeup_lead, face_text, face_text_source) and agent_notes (your private
    notes, from constructicon_set_agent_notes(card=...); null when none). For a read-before-change
    view with level, needs and recent changes: constructicon_explain_card.
    """
    chars = text_chars_arg(text_chars)
    project = db.get_project(id_or_slug)
    if project is None or not policy.can_view_card(project):  # #604 step 3: hidden = missing
        raise NotFound(f"No card {id_or_slug!r}.")

    # Get the project items (#557: the item policy decides what the card shows)
    items = policy.filter_visible(db.list_project_items(project["id"]))
    items_public = []
    for item in items:
        item_dict = _to_public_listed(item, text_chars=chars)
        # Add tags for each item
        tags = db.list_tags_for_post(item["slug"])
        item_dict["tags"] = [t["name"] for t in tags]
        items_public.append(item_dict)

    # Get the cover object if it exists
    cover = None
    if project.get("cover_slug"):
        cover_row = db.get_by_slug(project["cover_slug"])
        if cover_row and policy.can_view(cover_row):  # #557
            cover = _to_public_listed(cover_row, text_chars=chars)

    # Get the writeup object if it exists
    writeup = None
    if project.get("writeup_slug"):
        writeup_row = db.get_by_slug(project["writeup_slug"])
        if writeup_row and policy.can_view(writeup_row):  # #557
            body = (writeup_row.get("type_metadata") or {}).get("body")
            body_chars = len(body) if isinstance(body, str) else 0
            if full_writeup:
                writeup = {**_to_public(writeup_row), "body_chars": body_chars, "body_truncated": False}
            else:  # #722: a preview, like every other item here
                writeup = _to_public_listed(writeup_row, text_chars=chars)
                writeup["body_chars"] = body_chars
                writeup["body_truncated"] = "body" in writeup.get("type_metadata_cut_keys", [])

    return {
        **_to_public_project(project),
        "items": items_public,
        "cover": cover,
        "writeup": writeup,
        "open_decisions": [cards.decision_summary(d) for d in cards.open_card_decisions(project["slug"])],
        # V2 cards 3.6/3.7: families this card is in, members (if it is a family or
        # collection), and the cards nested under it ("part of").
        **cards.family_fields(project),
        # V2 cards 3.8: typed links, both directions, with labels.
        "links": cards.list_links(project["slug"]),
        "children": [{"id": c["id"], "slug": c["slug"], "title": c["title"]}
                     for c in policy.filter_visible_cards(db.list_child_projects(project["id"]))],
        # #604 step 3: private or open, inherited from where, owner, members (constructicon_sharing).
        "sharing": sharing.state("card", project["id"]),
        # #596: the face text (synopsis, flavor, the cached write-up lead, what the face shows).
        **cards.text_fields(project),
        # #745: the card's private agent notes. Only this read (and set_agent_notes) returns them.
        "agent_notes": db.get_card_agent_notes(project["id"]),
    }


# --- Hobbies (#360) ---

@mcp.tool()
def constructicon_list_hobbies() -> list[dict]:
    """All hobbies, by name: {id, name, slug, status (active | inactive, set by hand), group_code,
    project_count, flags, cover_slug, cover_picked, cover_source (picked | highlighted_project |
    recent_project | null), cover_project, picked_cover_slug, picked_cover_unavailable, created_by_user,
    created_via ("<user>'s <token name>")}.
    flags are computed, never stored: inactive_with_active_work, active_untouched (~2 years)."""
    out = []
    for h in policy.filter_visible_hobbies(db.list_hobbies()):  # #604 step 3
        f = cards.hobby_fields(h)
        out.append({
            "id": f["id"],
            "name": f["name"],
            "slug": f["slug"],
            "status": f["status"],
            "group_code": f["group_code"],
            "project_count": h.get("project_count", 0),
            "flags": f["flags"],
            **cards.hobby_cover_fields(cards.resolve_hobby_cover(h)),
            **_hobby_creator(h["id"]),  # #604 / #778
        })
    return out


@mcp.tool()
def constructicon_create_hobby(name: str, status: str = "active") -> dict:
    """Create a hobby from a name (a top-level tag, reused if it exists, marked as a hobby).
    status: active (default) | inactive. Returns {id, name, slug, status, group_code, batch_id}."""
    result = hobbies.create(name, status)
    return {**result.data["hobby"], "batch_id": result.batch_id}


def _convert_card_to_hobby(project_slug, dry_run):
    project = db.get_project(project_slug)
    if project is None or not policy.can_view_card(project):  # #604 step 3: hidden = missing
        raise NotFound(f"No card {project_slug!r}.")
    result = hobbies.convert_from_card(project["id"], dry_run=dry_run)
    hobby = result.data["hobby"]
    return {
        "id": hobby["id"],
        "name": hobby["name"],
        "slug": hobby["slug"],
        "status": hobby["status"],
        "summary": {
            "children_moved": result.data["children_moved"],
            "items_moved": result.data["items_moved"],
        },
        "dry_run": result.dry_run,
        "batch_id": result.batch_id,
        "changes": result.changes,
        "warnings": result.warnings,
    }


@mcp.tool(selector="direction")
def constructicon_convert(direction: typing.Literal["card_to_hobby", "hobby_to_card"], card: str | int | None = None,
                          hobby: str | int | None = None, kind: str | None = None, title: str | None = None,
                          into_hobby: str | int | None = None, dry_run: bool = False) -> dict:
    """Turn a card into a hobby or a hobby into a card, as ONE undoable batch.
    "card_to_hobby" (card=): the card becomes a hobby tag; nested cards join the hobby, its files
      get the tag, the card row goes (links, families, blog attachments, a blank write-up cleared).
      -> {id, name, slug, status, summary: {children_moved, items_moved}, dry_run, batch_id,
      changes, warnings}.
    "hobby_to_card" (hobby=, kind = family | collection | project, title (default the hobby's
      name), into_hobby (a hobby it joins)): the card reuses the hobby's tag; stage follows the
      hobby (active -> in_progress, inactive -> paused); member cards become family members or
      nested parts; loose files go onto it; homes that named the hobby name the card; the hobby is
      unmarked. Refused whole (nothing written): bad_membership, nest_group_kind,
      nest_second_parent. -> {ok, dry_run, changes, warnings, batch_id, card, card_id, kind,
      title, into_hobby, hobby, members: [{slug, how}], loose_moved, homes_moved}.
    E.g. direction="hobby_to_card", hobby="gi-joe", kind="family", into_hobby="collecting".
    """
    if direction == "card_to_hobby":
        if card is None:
            raise InvalidInput('direction="card_to_hobby" needs card.')
        _only_for('direction="card_to_hobby"', hobby=(hobby, None), kind=(kind, None), title=(title, None),
                  into_hobby=(into_hobby, None))
        return _convert_card_to_hobby(card, dry_run)
    if direction == "hobby_to_card":
        if hobby is None or not kind:
            raise InvalidInput('direction="hobby_to_card" needs hobby and kind (family, collection or project).')
        _only_for('direction="hobby_to_card"', card=(card, None))
        return hobbies.convert_to_card(hobby, kind, title, into_hobby, dry_run=dry_run).to_dict()
    raise InvalidInput(f"Unknown direction {direction!r}: use card_to_hobby or hobby_to_card.")


@mcp.tool(alias_of=("constructicon_convert", "card_to_hobby"))
def constructicon_convert_project_to_hobby(project_slug: str, dry_run: bool = False) -> dict | None:
    return _convert_card_to_hobby(project_slug, dry_run)


@mcp.tool(alias_of=("constructicon_convert", "hobby_to_card"))
def constructicon_convert_hobby_to_card(hobby: str | int, kind: str, title: str | None = None,
                                        into_hobby: str | int | None = None, dry_run: bool = False) -> dict:
    return hobbies.convert_to_card(hobby, kind, title, into_hobby, dry_run=dry_run).to_dict()


@mcp.tool()
def constructicon_unmark_hobby(hobby: str | int, dry_run: bool = False) -> dict:
    """Stop treating a tag as a hobby: its status and group code go, its card memberships are
    removed, and cards whose home was it go back to automatic. The tag and its files stay.
    -> {ok, dry_run, changes, warnings, batch_id, hobby, cards_removed, homes_cleared}."""
    return hobbies.unmark(hobby, dry_run=dry_run).to_dict()


@mcp.tool(selector="action")
def constructicon_card_hobby(card: str | int, hobby: str | int, action: typing.Literal["add", "remove"] = "add",
                             dry_run: bool = False, batch_id: str | None = None) -> dict:
    """Put a card in a hobby ("add", default; a card can be in several; twice is a no-op) or take
    it out ("remove"). -> {ok, dry_run, changes, warnings, batch_id}."""
    if action == "add":
        return cards.add_to_hobby(card, hobby, dry_run=dry_run, batch_id=batch_id).to_dict()
    if action == "remove":
        return cards.remove_from_hobby(card, hobby, dry_run=dry_run, batch_id=batch_id).to_dict()
    raise InvalidInput(f"Unknown action {action!r}: use add or remove.")


def _hobby_or_404(hobby):
    row = db.get_hobby(hobby)
    if row is None or not policy.can_view_hobby(row):  # #604 step 3: hidden = missing
        raise NotFound(f"No hobby {hobby!r}.")
    return row


@mcp.tool()
def constructicon_update_hobby(hobby: str | int, status: str | None = None, group_code: str | None = None,
                               cover_slug: str | None = None, clear_cover: bool = False,
                               synopsis: str | None = None, flavor: str | None = None,
                               name: str | None = None, cover_card: str | int | None = None,
                               dry_run: bool = False) -> dict:
    """Edit a hobby, every change in ONE batch. hobby: id or slug. Left out (None) = left alone.
    name: rename it; its page address (slug) follows and the old address redirects.
    status: active | inactive (the old words dormant / abandoned are stored as inactive, with a
      warning). group_code: 2-4 letters/digits shown on cards, unique, stored upper case.
    cover_slug: an item with a picture as its cover; cover_card: one of its cards (id or slug) whose
      cover it borrows and follows; clear_cover=true goes back to the automatic cover (its
      highlighted project's, else its latest project's). Only one of the three.
    synopsis (max 600) / flavor (max 140): its card face text; "" clears.
    dry_run=true answers as the call would and writes nothing.
    -> {ok, id, name, slug, status, group_code, flags, synopsis, flavor, cover: {cover_slug,
    cover_picked, cover_source, ...}, changes, warnings, batch_id, dry_run}.
    Codes: bad_hobby_activity, bad_group_code, group_code_conflict, bad_cover (details.reason),
    bad_card_text, hobby_name_conflict.
    """
    if sum(1 for x in (cover_slug, cover_card) if x not in (None, "")) + bool(clear_cover) > 1:
        raise InvalidInput("Pass only one of cover_slug, cover_card or clear_cover=true.")
    hid = _hobby_or_404(hobby)["id"]
    take = _Collect()
    batch_id = changes.new_batch_id()
    with db.transaction(dry_run=dry_run):
        if cover_slug is not None or cover_card not in (None, "") or clear_cover:  # first: set_cover reports the batch so far
            take(hobbies.set_cover(hid, None if clear_cover else cover_slug,
                                   cover_card=None if clear_cover else cover_card, batch_id=batch_id))
        if name is not None:
            take(hobbies.rename(hid, name, batch_id=batch_id))
        if status is not None:
            take(cards.set_hobby_activity(hid, status, batch_id=batch_id))
        if group_code is not None:
            take(cards.set_group_code(hid, group_code, batch_id=batch_id))
        if synopsis is not None or flavor is not None:
            take(hobbies.set_text(hid, synopsis=synopsis if synopsis is not None else ...,
                                  flavor=flavor if flavor is not None else ..., batch_id=batch_id))
        row = db.get_hobby(hid)
        f = cards.hobby_fields(row)
        out = {"ok": True, "id": f["id"], "name": f["name"], "slug": f["slug"], "status": f["status"],
               "group_code": f["group_code"], "flags": f["flags"], **hobbies.text_fields(row),
               "cover": cards.hobby_cover_fields(cards.resolve_hobby_cover(row)),
               **take.result(), "batch_id": None if dry_run else _written(batch_id), "dry_run": dry_run}
    return out


@mcp.tool(alias_of=("constructicon_card_hobby", "add"))
def constructicon_add_project_to_hobby(project_slug: str, hobby_slug: str) -> dict | None:
    project = db.get_project(project_slug)
    if project is None or not policy.can_view_card(project):  # #604 step 3: hidden = missing
        raise NotFound(f"No card {project_slug!r}.")

    hobby = db.get_hobby(hobby_slug)
    if hobby is None or not policy.can_view_hobby(hobby):  # #604 step 3: hidden = missing
        raise NotFound(f"No hobby {hobby_slug!r}.")

    hobbies.add_card(hobby["id"], project["id"])  # logged + undoable (V2 cards 3.13, #541)

    # Return the updated hobby
    updated_hobby = db.get_hobby(hobby["id"])
    projects = policy.filter_visible_cards(db.list_projects_for_hobby(hobby["id"]))  # #604 step 3

    return {
        "id": updated_hobby["id"],
        "name": updated_hobby["name"],
        "slug": updated_hobby["slug"],
        "status": updated_hobby.get("hobby_status"),
        "project_count": len(projects),
    }


@mcp.tool(alias_of=("constructicon_update_hobby", None))
def constructicon_set_hobby_status(hobby_slug: str, status: str) -> dict | None:
    hobby = db.get_hobby(hobby_slug)
    if hobby is None or not policy.can_view_hobby(hobby):  # #604 step 3: hidden = missing
        raise NotFound(f"No hobby {hobby_slug!r}.")
    result = cards.set_hobby_activity(hobby["id"], status)
    updated = cards.hobby_fields(db.get_hobby(hobby["id"]))
    return {
        "id": updated["id"],
        "name": updated["name"],
        "slug": updated["slug"],
        "status": updated["status"],
        "group_code": updated["group_code"],
        "flags": updated["flags"],
        "warnings": result.warnings,
        "changes": result.changes,
        "batch_id": result.batch_id,
    }


@mcp.tool(alias_of=("constructicon_update_hobby", None))
def constructicon_set_hobby_code(hobby_slug: str, group_code: str) -> dict | None:
    hobby = db.get_hobby(hobby_slug)
    if hobby is None or not policy.can_view_hobby(hobby):  # #604 step 3: hidden = missing
        raise NotFound(f"No hobby {hobby_slug!r}.")
    result = cards.set_group_code(hobby["id"], group_code)
    updated = db.get_hobby(hobby["id"])
    return {"id": updated["id"], "name": updated["name"], "slug": updated["slug"],
            "group_code": updated["group_code"], "changes": result.changes, "batch_id": result.batch_id}


@mcp.tool(alias_of=("constructicon_update_hobby", None))
def constructicon_set_hobby_cover(hobby: str | int, cover_slug: str | None = None, clear: bool = False,
                                  dry_run: bool = False) -> dict:
    if clear and cover_slug:
        raise InvalidInput("Pass either cover_slug or clear=true, not both.")
    return hobbies.set_cover(hobby, None if clear else cover_slug, dry_run=dry_run).to_dict()


@mcp.tool()
def constructicon_list_blog_entries(status: str | None = None) -> list[dict]:
    """All blog entries, most recently updated first; status filters (e.g. "draft", "ready")."""
    entries = db.list_blog_entries(status=status)
    return [
        {
            "id": e["id"],
            "slug": e["slug"],
            "title": e["title"],
            "subtitle": e.get("subtitle", ""),
            "status": e["status"],
            "cover_slug": e.get("cover_slug"),
            "content_date": e.get("content_date"),
            "created_at": e["created_at"],
            "updated_at": e["updated_at"],
            **mcp_tokens.creator_fields(e.get("created_by_user_id"), e.get("created_via_token_id")),  # #778
        }
        for e in entries
    ]


@mcp.tool()
def constructicon_create_blog_entry(title: str, subtitle: str = "", body: str = "", status: str = "draft",
                                    cover_slug: str | None = None,
                                    content_date: float | str | None = None) -> dict:
    """Create a blog entry (its slug comes from the title). status: "draft" (default), "ready" ...
    cover_slug: an item as its cover. content_date: the date it is about, best as an ISO date
    ("2026-10-01"); don't hand-compute unix seconds (UTC midnight shows as the day before).
    Attach cards and items with constructicon_update_blog_entry. -> the entry + batch_id.
    """
    result = blog.create(title, subtitle=subtitle, body=body, status=status, cover_slug=cover_slug,
                         content_date=content_date)  # #541 phase D: logged + undoable
    return {**_to_public_blog_entry(result.data["entry"]), "batch_id": result.batch_id}


@mcp.tool()
def constructicon_get_blog_entry(slug: str) -> dict | None:
    """One blog entry with its cards and items (each with its note and sort_order)."""
    entry = db.get_blog_entry(slug)
    if entry is None:
        raise NotFound(f"No blog entry {slug!r}.")
    return _to_public_blog_entry(entry)


def _attachments(rows, key, what):
    """[(key value, note)] from [{key, note?}], refusing anything else (bad_request, nothing written)."""
    if isinstance(rows, (str, dict)) or not isinstance(rows, list) or not all(isinstance(r, dict) for r in rows):
        raise InvalidInput(f"{what} must be a list of {{\"{key}\": ..., \"note\": \"...\"}} objects.")
    return [(r.get(key), r.get("note", "")) for r in rows]


@mcp.tool()
def constructicon_update_blog_entry(slug: str, title: str | None = None, subtitle: str | None = None,
                                    body: str | None = None, status: str | None = None,
                                    cover_slug: str | None = None, content_date: float | str | None = None,
                                    clear_cover_slug: bool = False, clear_content_date: bool = False,
                                    projects: list[dict] | None = None, items: list[dict] | None = None) -> dict:
    """Edit a blog entry, every change in ONE batch. Left out (None) = left alone.
    clear_cover_slug / clear_content_date clear those two. content_date as in create.
    projects: the cards attached, in order, as a FULL replace: [{"project_id": <card id or
      slug>, "note": "..."}]; [] removes them all. items: the same for items: [{"slug": "...",
      "note": "..."}]. An unknown card or item is not_found (nothing written).
    -> the entry (with its cards and items) + batch_id.
    """
    entry = db.get_blog_entry(slug)
    if entry is None:
        raise NotFound(f"No blog entry {slug!r}.")
    card_rows = _attachments(projects, "project_id", "projects") if projects is not None else None
    item_rows = _attachments(items, "slug", "items") if items is not None else None
    cover = None if clear_cover_slug else (cover_slug if cover_slug is not None else ...)
    cdate = None if clear_content_date else (content_date if content_date is not None else ...)
    entry_fields = any(v is not None for v in (title, subtitle, body, status, cover_slug, content_date)) or \
        clear_cover_slug or clear_content_date
    batch_id = changes.new_batch_id()
    with db.transaction():
        if entry_fields or (card_rows is None and item_rows is None):  # as before: an update bumps updated_at
            blog.update(entry["id"], title=title, subtitle=subtitle, body=body, status=status,
                        cover_slug=cover, content_date=cdate, batch_id=batch_id)
        if card_rows is not None:
            blog.set_projects(entry["id"], card_rows, batch_id=batch_id)
        if item_rows is not None:
            blog.set_items(entry["id"], item_rows, batch_id=batch_id)
        fresh = db.get_blog_entry(entry["id"])
    return {**_to_public_blog_entry(fresh), "batch_id": batch_id}


@mcp.tool(alias_of=("constructicon_delete", "blog_entry"))
def constructicon_delete_blog_entry(slug: str) -> bool:
    blog.delete(slug)
    return True


@mcp.tool(alias_of=("constructicon_update_blog_entry", None))
def constructicon_set_blog_entry_projects(slug: str, items: list[dict]) -> dict | None:
    entry = db.get_blog_entry(slug)
    if entry is None:
        raise NotFound(f"No blog entry {slug!r}.")

    # Convert dicts to (project_id, note) tuples. #541 phase D: logged + undoable; an unknown card
    # (id or slug) is not_found instead of a row no page can show.
    project_items = [(item.get("project_id"), item.get("note", "")) for item in items]
    result = blog.set_projects(entry["id"], project_items)
    return {**_to_public_blog_entry(result.data["entry"]), "batch_id": result.batch_id}


@mcp.tool(alias_of=("constructicon_update_blog_entry", None))
def constructicon_set_blog_entry_items(slug: str, items: list[dict]) -> dict | None:
    entry = db.get_blog_entry(slug)
    if entry is None:
        raise NotFound(f"No blog entry {slug!r}.")

    # Convert dicts to (post_slug, note) tuples. #541 phase D: logged + undoable; unknown file = not_found.
    post_items = [(item.get("slug"), item.get("note", "")) for item in items]
    result = blog.set_items(entry["id"], post_items)
    return {**_to_public_blog_entry(result.data["entry"]), "batch_id": result.batch_id}


@mcp.tool()
def constructicon_list_tags() -> list[dict]:
    """The tag tree: root tags, each with nested `children`."""
    return policy.filter_tag_tree(db.list_tag_tree())  # #604 step 3: no private card / hobby names


@mcp.tool()
def constructicon_create_tag(name: str, parent_name: str | None = None) -> dict:
    """Create a tag (under parent_name, or at the root), or return the one already there."""
    # #541 phase C: a created tag (and parent) is imaged, so constructicon_undo removes it.
    return tags_svc.create(name, parent_name).data["tag"]


@mcp.tool(selector="action", annotations=_DESTRUCTIVE)
def constructicon_tags(action: typing.Literal["tree", "get", "rename", "merge", "move", "delete", "sweep",
                                              "sweep_apply"] = "tree",
                       tag: str | int | None = None, name: str | None = None, into: str | int | None = None,
                       parent: str | int | None = None, tag_ids: list[int] | None = None,
                       dry_run: bool = False) -> dict:
    """Manage the tag tree (the one tag store; an item's chips are derived from it). `tag` / `into` /
    `parent` take a tag id or its name. Every write is one batch (constructicon_undo reverses it).
    "tree": {tags: [{id, name, path, items, cards, hobby, children}]}.
    "get": tag= -> {tags: [every tag with that name (any case), each with path, items, children, cards, hobby]}.
    "rename": tag=, name= -> {tag, old_name, items, batch_id}. A card's linked tag follows the card's
      title (rename the card); a name another tag has is refused (merge instead).
    "merge": tag= (the one that goes), into= -> its items and sub-tags move to `into`, then it is deleted.
    "move": tag=, parent= (omit or "" = the top level) -> no cycles.
    "delete": tag= -> it comes off every item. A card's or grouping's tag, or one with sub-tags, is refused.
    "sweep": the cleanup list, read-only: {entries: [{id, path, kinds (orphan | empty | near_duplicate),
      deletable, why_not, suggestion, duplicates}]}. Nothing is deleted.
    "sweep_apply": tag_ids= the picked entries -> deletes exactly those (all still deletable, sub-tags
      picked too, else the whole call is refused), ONE batch.
    dry_run=true answers as the write would and writes nothing.
    E.g. constructicon_tags(action="merge", tag="Ancient projects.", into="Ancient Projects").
    """
    if action == "tree":
        _only_for('action="tree"', tag=(tag, None), name=(name, None), into=(into, None), parent=(parent, None),
                  tag_ids=(tag_ids, None), dry_run=(dry_run, False))
        return {"tags": tags_svc.tree()}
    if action == "sweep":
        _only_for('action="sweep"', tag=(tag, None), name=(name, None), into=(into, None), parent=(parent, None),
                  tag_ids=(tag_ids, None), dry_run=(dry_run, False))
        return {"entries": tags_svc.sweep(), "kinds": list(tags_svc.SWEEP_KINDS)}
    if action == "sweep_apply":
        _only_for('action="sweep_apply"', tag=(tag, None), name=(name, None), into=(into, None), parent=(parent, None))
        if not tag_ids:
            raise InvalidInput('action="sweep_apply" needs tag_ids (the ids of the sweep entries to delete).')
        return tags_svc.sweep_apply(tag_ids, dry_run=dry_run).to_dict()
    if tag is None or (isinstance(tag, str) and not tag.strip()):
        raise InvalidInput(f'action="{action}" needs tag (a tag id or name).')
    if action == "get":
        _only_for('action="get"', name=(name, None), into=(into, None), parent=(parent, None), tag_ids=(tag_ids, None),
                  dry_run=(dry_run, False))
        if isinstance(tag, int) or str(tag).strip().isdigit():
            row = tags_svc.get_tag(tag)
            return {"tags": [{**row, **tags_svc.usage(row["id"])}]}
        return {"tags": tags_svc.get_by_name(str(tag))}
    if action == "rename":
        _only_for('action="rename"', into=(into, None), parent=(parent, None), tag_ids=(tag_ids, None))
        if not (name or "").strip():
            raise InvalidInput('action="rename" needs name (the new name).')
        return tags_svc.rename(tag, name, dry_run=dry_run).to_dict()
    if action == "merge":
        _only_for('action="merge"', name=(name, None), parent=(parent, None), tag_ids=(tag_ids, None))
        if into is None or (isinstance(into, str) and not into.strip()):
            raise InvalidInput('action="merge" needs into (the tag to keep).')
        return tags_svc.merge(tag, into, dry_run=dry_run).to_dict()
    if action == "move":
        _only_for('action="move"', name=(name, None), into=(into, None), tag_ids=(tag_ids, None))
        return tags_svc.move(tag, parent if parent not in ("",) else None, dry_run=dry_run).to_dict()
    if action == "delete":
        _only_for('action="delete"', name=(name, None), into=(into, None), parent=(parent, None), tag_ids=(tag_ids, None))
        return tags_svc.delete(tag, dry_run=dry_run).to_dict()
    raise InvalidInput(f"Unknown action {action!r}: use tree, get, rename, merge, move, delete, sweep or sweep_apply.")


@mcp.tool(alias_of=("constructicon_list_items", "tag"))
def constructicon_get_posts_for_tag(tag_name: str, text_chars: int = TEXT_PREVIEW_CHARS) -> list[dict]:
    # #722: still a bare list (its old shape), but only the first LIST_PAGE_MAX items, read in SQL;
    # list_items(which="tag") pages through the rest.
    return _posts_for_tag(tag_name, text_chars_arg(text_chars), limit=LIST_PAGE_MAX)["items"]


def _detach_tag(slug, tag_name):
    policy.viewable_rows([slug])  # #709: missing = not_found, not visible = forbidden (says why)
    tag = tags_svc.find_any(tag_name)  # #563: a lookup must not create the tag
    if tag is None:
        raise NotFound(f"No such tag: {tag_name!r}")
    tags_svc.detach(slug, tag["id"])
    return _to_public(db.get_by_slug(slug))


@mcp.tool(selector="action")
def constructicon_item_tags(slug: str, tag_names: list[str],
                            action: typing.Literal["attach", "detach"] = "attach") -> dict:
    """Add tags to an item ("attach", default; missing root tags are created) or take them off
    ("detach"; an unknown tag is not_found and nothing is changed). One batch either way. This is
    the tag tree (an item's chips are derived from it); constructicon_update(tags=[...]) replaces the chips.
    -> the item + batch_id.
    """
    if isinstance(tag_names, str) or not isinstance(tag_names, list):
        raise InvalidInput("tag_names must be a list of tag names.")
    if action == "attach":
        batch_id = tags_svc.attach(slug, tag_names).batch_id  # #541 phase C: one batch; created tags imaged too
    elif action == "detach":
        policy.viewable_rows([slug])  # #709: missing = not_found, not visible = forbidden (says why)
        found = []
        for name in dict.fromkeys(tag_names):
            tag = tags_svc.find_any(name)  # #563: a lookup must not create the tag
            if tag is None:
                raise NotFound(f"No such tag: {name!r}. Nothing was changed.")
            found.append(tag["id"])
        batch_id = changes.new_batch_id()
        with db.transaction():
            for tag_id in found:
                tags_svc.detach(slug, tag_id, batch_id=batch_id)
    else:
        raise InvalidInput(f"Unknown action {action!r}: use attach or detach.")
    return {**_to_public(db.get_by_slug(slug)), "batch_id": _written(batch_id)}


@mcp.tool(alias_of=("constructicon_item_tags", "attach"))
def constructicon_attach_tags(slug: str, tag_names: list[str]) -> dict | None:
    tags_svc.attach(slug, tag_names)  # #541 phase C: one batch; tags it creates are imaged too
    return _to_public(db.get_by_slug(slug))


@mcp.tool(alias_of=("constructicon_item_tags", "detach"))
def constructicon_detach_tag(slug: str, tag_name: str) -> dict | None:
    return _detach_tag(slug, tag_name)


RESERVED_ACTION = "retry_ocr"  # #746: built into constructicon_run_type_action; no type may declare it


def _retry_ocr(slug):
    """Force OCR to run (or re-run) on an item; refusals are bad_request, as they always were."""
    row = db.get_by_slug(slug)
    if row is None:
        raise NotFound(f"No item {slug!r}.")
    policy.require_view(row, message=f"No item {slug!r}.")  # #746: was missing (the old net never reached it)
    if row["redacted"]:
        raise ValueError("File was redacted — there's no content left to OCR")
    spec = object_types.get_object_type(row.get("media_type"))
    if not spec.ocr_capable:
        raise ValueError(f"OCR isn't available for {spec.label} content")
    parse_limits.forget(thumbnails.limit_key(row))  # #644: an explicit retry really runs again
    db.set_ocr_status(slug, "pending")
    actor_ctx.spawn(ocr.run_ocr, slug)
    return _to_public(db.get_by_slug(slug))


@mcp.tool(alias_of=("constructicon_run_type_action", None))
def constructicon_retry_ocr(slug: str) -> dict | None:
    return _retry_ocr(slug)


def _agent_notes_target(args):
    """Which notes a set_agent_notes call writes (its role row in ACTION_ROLES): a card's or an item's."""
    return "card" if args.get("card") is not None else "slug"


@mcp.tool(selector=(_agent_notes_target, ("slug", "card")))
def constructicon_set_agent_notes(slug: str | None = None, notes: str | None = None,
                                  card: str | int | None = None, append: bool = False) -> dict | None:
    """Set (or, with null, clear) your private working notes on an item (slug=) or a card (card=, its
    id or slug); exactly one of them. E.g. "Fusion 360 screenshot, not user-facing", or story details
    the owner mentioned for a later write-up. Never shown in the app, the web API, search or exports.
    append=true ADDS notes under what is there as a dated block ("--- 2026-10-27 ---", Mountain Time,
    then the text) instead of replacing it: the safe way to add to notes you haven't read (empty
    notes with append is refused).
    Read them back: constructicon_get(slug, include=["agent_notes"]) for an item,
    constructicon_get_project(card) for a card. Undoable.
    -> the item (or card) plus agent_notes (the whole notes after); a card, and any append, also
    gets batch_id.
    """
    if (slug is None) == (card is None):
        raise InvalidInput("Pass exactly one of slug= (an item) or card= (a card).")
    if not isinstance(append, bool):
        raise InvalidInput("append must be true or false.")
    if card is not None:  # #745: the card twin, through the card service (not_found if missing)
        res = cards.set_agent_notes(card, notes, append=append)  # #763: the append is atomic in core
        return {**_to_public_project(res.data["card"]), "agent_notes": res.data["agent_notes"],
                "batch_id": res.batch_id}
    if append:  # #763: read-and-add inside one transaction, in the item service
        res = items.append_agent_notes(slug, notes)
    else:
        res = items.update(slug, agent_notes=notes)  # #541 phase D: through the item service (not_found if missing)
    row = db.get_by_slug(slug)
    # Never return the raw DB row: it carries the `embedding` BLOB, which
    # isn't JSON-serializable, so the tool call itself failed for every row
    # that had been through OCR (#211).
    out = {**_to_public(row), "agent_notes": row.get("agent_notes")}
    if append:  # #763: the append's batch, so it can be undone; the plain set's answer is unchanged (#745)
        out["batch_id"] = _written(res.batch_id)
    return out


@mcp.tool()
def constructicon_list_provenance_options(scope: str = "card", include_retired: bool = False) -> dict:
    """The provenance lists the owner edits in /admin. scope: "card" (for
    constructicon_update_project fields.provenance) or "file" (for constructicon_update
    fields.provenance). include_retired=true adds retired keys (not settable on anything new).
    -> {ok, scope, options: [{key, label, sort_order, retired}]}.
    """
    opts = provenance_options.list_options(scope, include_retired=include_retired)
    return {"ok": True, "scope": scope,
            "options": [{k: o[k] for k in ("key", "label", "sort_order", "retired")} for o in opts]}


@mcp.tool()
def constructicon_list_vocab(vocab: str = "brand_role") -> dict:
    """The word lists the owner edits in /admin. vocab: "brand_role" (the suggested roles
    for constructicon_update fields.brand_role; a custom role is still allowed) or "wallpaper_tag"
    (the tag names that put an item on the Wallpaper page, any case).
    -> {ok, vocab, terms: [{term, sort_order}]}.
    """
    return {"ok": True, "vocab": vocab,
            "terms": [{"term": t["term"], "sort_order": t["sort_order"]} for t in vocab_svc.list_terms(vocab)]}


@mcp.tool(alias_of=("constructicon_update", None))
def constructicon_set_provenance(slug: str, provenance: str | None = None) -> dict | None:
    row = items.update(slug, provenance=provenance).item
    return {**_to_public(row), "provenance": row.get("provenance")}


@mcp.tool(alias_of=("constructicon_update", None))
def constructicon_set_content_date(slug: str, date: str | None = None) -> dict | None:
    items.get_item(slug)
    row = items.update(slug, content_date=items.parse_date(date)).item  # naive ISO -> Mountain Time
    return {**_to_public(row), "content_date": row.get("content_date")}


@mcp.tool(alias_of=("constructicon_update", None))
def constructicon_set_highlight(slug: str, on: bool = False) -> dict | None:
    row = items.update(slug, highlight=on).item  # the old default: no `on` UNmarks
    return {**_to_public(row), "highlight": row.get("highlight")}


@mcp.tool(alias_of=("constructicon_update", None))
def constructicon_set_brand_asset(slug: str, is_brand: bool = False, brand_role: str | None = None) -> dict | None:
    row = items.update(slug, is_brand_asset=is_brand, brand_role=brand_role).item  # old default: clears both
    return {**_to_public(row), "is_brand_asset": bool(row.get("is_brand_asset")), "brand_role": row.get("brand_role")}


@mcp.tool(alias_of=("constructicon_list_items", "brand_assets"))
def constructicon_list_brand_assets() -> list[dict]:
    return _list_brand_assets()


@mcp.tool(alias_of=("constructicon_update_project", None))
def constructicon_set_project_status(id_or_slug: str, status: str) -> dict | None:
    if not policy.can_view_card(db.get_project(id_or_slug)):  # #604 step 3: hidden = missing
        raise NotFound(f"No card {id_or_slug!r}.")
    legacy = card_rules.legacy_to_status(status)
    warnings = list(legacy["warnings"])
    if legacy["kind"]:
        warnings.extend(cards.set_kind(id_or_slug, legacy["kind"]).warnings)
    warnings.extend(cards.set_status(id_or_slug, legacy["stage"], legacy["stop_reason"]).warnings)
    return {**_to_public_project(db.get_project(id_or_slug)), "warnings": warnings}


@mcp.tool(alias_of=("constructicon_update_project", None))
def constructicon_set_status(card: str | int, stage: str, stop_reason: str | None = None,
                             activity: str | None = None, dry_run: bool = False) -> dict:
    return cards.set_status(card, stage, stop_reason, activity=activity, dry_run=dry_run).to_dict()


@mcp.tool(alias_of=("constructicon_update_project", None))
def constructicon_set_kind(card: str | int, kind: str, force: bool = False, dry_run: bool = False) -> dict:
    return cards.set_kind(card, kind, force=force, dry_run=dry_run).to_dict()


@mcp.tool(alias_of=("constructicon_update_project", None))
def constructicon_set_whereabouts(card: str | int, whereabouts: str | None = None, note: str | None = None,
                                  clear_note: bool = False, dry_run: bool = False) -> dict:
    return cards.set_whereabouts(card, whereabouts, "" if clear_note else (note if note is not None else ...),
                                 dry_run=dry_run).to_dict()


@mcp.tool(alias_of=("constructicon_update_project", None))
def constructicon_set_card_text(card: str | int | None = None, hobby: str | int | None = None,
                                synopsis: str | None = None, flavor: str | None = None,
                                clear_synopsis: bool = False, clear_flavor: bool = False,
                                dry_run: bool = False) -> dict:
    if (card is None) == (hobby is None):
        raise InvalidInput("Pass exactly one of card or hobby.")
    syn = "" if clear_synopsis else (synopsis if synopsis is not None else ...)
    fla = "" if clear_flavor else (flavor if flavor is not None else ...)
    if card is not None:
        return cards.set_text(card, synopsis=syn, flavor=fla, dry_run=dry_run).to_dict()
    return hobbies.set_text(hobby, synopsis=syn, flavor=fla, dry_run=dry_run).to_dict()


@mcp.tool(alias_of=("constructicon_update_project", None))
def constructicon_set_card_provenance(card: str | int, provenance: str | None = None, credit: str | None = None,
                                      clear_credit: bool = False, dry_run: bool = False) -> dict:
    return cards.set_provenance(card, provenance, "" if clear_credit else (credit if credit is not None else ...),
                                dry_run=dry_run).to_dict()


@mcp.tool(alias_of=("constructicon_update_project", None))
def constructicon_set_card_highlight(card: str | int, on: bool = False, dry_run: bool = False) -> dict:
    return cards.set_highlight(card, on, dry_run=dry_run).to_dict()  # the old default: no `on` UNmarks


@mcp.tool(selector="action")
def constructicon_nest(child: str | int, parent: str | int | None = None, replace: bool = False,
                       dry_run: bool = False, action: typing.Literal["nest", "unnest"] = "nest") -> dict:
    """Make `child` PART OF `parent` ("nest", default; it only makes sense inside it, e.g. a Lua
    script inside the truck it runs on), or take it out of its parent ("unnest"; no-op if none).
    Not family membership (constructicon_family). Refused: nest_self, nest_cycle, nest_group_kind
    (a family/collection can't nest or be a parent), nest_second_parent (already part of another
    card: replace=true moves it). -> {ok, dry_run, changes, warnings, batch_id}.
    """
    if action == "nest":
        if parent is None:
            raise InvalidInput('action="nest" needs parent.')
        return cards.nest(child, parent, replace=replace, dry_run=dry_run).to_dict()
    if action == "unnest":
        _only_for('action="unnest"', parent=(parent, None), replace=(replace, False))
        return cards.unnest(child, dry_run=dry_run).to_dict()
    raise InvalidInput(f"Unknown action {action!r}: use nest or unnest.")


@mcp.tool(alias_of=("constructicon_nest", "unnest"))
def constructicon_unnest(child: str | int, dry_run: bool = False) -> dict:
    return cards.unnest(child, dry_run=dry_run).to_dict()


@mcp.tool(selector="action")
def constructicon_family(family: str | int, member: str | int, action: typing.Literal["add", "remove"] = "add",
                         dry_run: bool = False) -> dict:
    """Put a card in a family or collection ("add", default; many-to-many, moves no files, twice is
    a no-op) or take it out ("remove"). `family` must be a family/collection card and `member` must
    not be one (bad_membership). -> {ok, dry_run, changes, warnings, batch_id}."""
    if action == "add":
        return cards.add_to_family(family, member, dry_run=dry_run).to_dict()
    if action == "remove":
        return cards.remove_from_family(family, member, dry_run=dry_run).to_dict()
    raise InvalidInput(f"Unknown action {action!r}: use add or remove.")


@mcp.tool(alias_of=("constructicon_family", "add"))
def constructicon_add_to_family(family: str | int, member: str | int, dry_run: bool = False) -> dict:
    return cards.add_to_family(family, member, dry_run=dry_run).to_dict()


@mcp.tool(alias_of=("constructicon_family", "remove"))
def constructicon_remove_from_family(family: str | int, member: str | int, dry_run: bool = False) -> dict:
    return cards.remove_from_family(family, member, dry_run=dry_run).to_dict()


# --- Private cards and hobbies: who else may see them ---

@mcp.tool(selector="action")
def constructicon_sharing(action: typing.Literal["get", "set_private", "add_member", "remove_member", "users"] = "get",
                          card: str | int | None = None, hobby: str | int | None = None,
                          private: bool | None = None, user: str | int | None = None,
                          role: typing.Literal["viewer", "editor"] | None = None, dry_run: bool = False) -> dict | list:
    """Private cards and hobbies. Cards and hobbies are open to everyone signed in unless ticked
    private; then only admins, the owner (its creator) and its members see it and everything under it
    (nested cards, family members, a hobby's cards and loose files). Give exactly one of card= or hobby=
    (id or slug), except for action="users".
    action: "get" (default) -> {private (ticked here), effective (here or above), inherited_from
    [{kind, id, title, slug}], owner, members [{user_id, username, name, role, capped}], can_manage};
    "set_private" (private=true|false); "add_member" (user=id or username, role=viewer|editor,
    default viewer; adding again changes the role); "remove_member" (user=); "users" -> the people
    you can add [{id, username, name, role}]. The writes return {ok, changes, warnings, batch_id,
    sharing} and only the owner or an admin may make them (forbidden otherwise). A viewer member only
    looks; an editor member may change the card and its files. The install role is the ceiling: a
    viewer can't be made an editor (role_ceiling). Example: constructicon_sharing(action="add_member",
    card="desk-build", user="sam", role="editor")."""
    if action == "users":
        _only_for('action="users"', card=(card, None), hobby=(hobby, None), private=(private, None),
                  user=(user, None), role=(role, None), dry_run=(dry_run, False))
        return sharing.user_choices()
    if (card is None) == (hobby is None):
        raise InvalidInput("Give exactly one of card= or hobby=.")
    kind, target = ("card", card) if card is not None else ("hobby", hobby)
    if action == "get":
        _only_for('action="get"', private=(private, None), user=(user, None), role=(role, None),
                  dry_run=(dry_run, False))
        return sharing.state(kind, target)
    if action == "set_private":
        _only_for('action="set_private"', user=(user, None), role=(role, None))
        if private is None:
            raise InvalidInput('action="set_private" needs private=true or private=false.')
        return sharing.set_private(kind, target, private, dry_run=dry_run).to_dict()
    _only_for(f'action="{action}"', private=(private, None))
    if user in (None, ""):
        raise InvalidInput(f'action="{action}" needs user= (a user id or username; action="users" lists them).')
    if action == "add_member":
        return sharing.add_member(kind, target, user, role or "viewer", dry_run=dry_run).to_dict()
    if action == "remove_member":
        _only_for('action="remove_member"', role=(role, None))
        return sharing.remove_member(kind, target, user, dry_run=dry_run).to_dict()
    raise InvalidInput(f"Unknown action {action!r}: use get, set_private, add_member, remove_member or users.")


# --- Typed links (V2 cards 3.8) ---

@mcp.tool(selector="action")
def constructicon_link(a: str | int, b: str | int | None = None, type: str | None = None, note: str | None = None,
                       dry_run: bool = False, batch_id: str | None = None,
                       action: typing.Literal["add", "remove", "retype", "list"] = "add",
                       from_type: str | None = None, to_type: str | None = None) -> dict | list:
    """Typed links between cards, read "a <type> b". type: built_for | applies_to | used_in |
    inspired_by (directed) | related (symmetric).
    "add" (default; b, type, note): a typed link over a related pair UPGRADES it; related over a
      typed pair is refused. Errors: bad_link (unknown type, self-link, a family/collection as the
      source of built_for/applies_to/used_in), link_conflict.
    "remove" (b, type optional): only that type, or with no type every link between the pair
      (no-op with a warning when nothing matches).
    "retype" (b, from_type, to_type): swap the type in one step; the note carries over unless note
      is given.
    "list": every link on card `a`, both directions: [{slug, title, kind, stage, type, direction
      (out | in | both), label, note}].
    Writes -> {ok, dry_run, changes, warnings, batch_id}; batch_id joins an earlier call's batch.
    E.g. constructicon_link("lua-script", "rc-truck", "used_in").
    """
    if action == "list":
        _only_for('action="list"', b=(b, None), type=(type, None), note=(note, None), dry_run=(dry_run, False),
                  batch_id=(batch_id, None), from_type=(from_type, None), to_type=(to_type, None))
        return cards.list_links(a)
    if b is None:
        raise InvalidInput(f'action="{action}" needs b (the other card).')
    if action == "add":
        if not type:
            raise InvalidInput('action="add" needs type.')
        _only_for('action="add"', from_type=(from_type, None), to_type=(to_type, None))
        return cards.link(a, b, type, "" if note is None else note, dry_run=dry_run, batch_id=batch_id).to_dict()
    if action == "remove":
        _only_for('action="remove"', note=(note, None), from_type=(from_type, None), to_type=(to_type, None))
        return cards.unlink(a, b, type, dry_run=dry_run, batch_id=batch_id).to_dict()
    if action == "retype":
        if not (from_type and to_type):
            raise InvalidInput('action="retype" needs from_type and to_type.')
        _only_for('action="retype" (use from_type / to_type)', type=(type, None))
        return cards.retype_link(a, b, from_type, to_type, note=note, dry_run=dry_run, batch_id=batch_id).to_dict()
    raise InvalidInput(f"Unknown action {action!r}: use add, remove, retype or list.")


@mcp.tool(alias_of=("constructicon_link", "remove"))
def constructicon_unlink(a: str | int, b: str | int, type: str | None = None, dry_run: bool = False) -> dict:
    return cards.unlink(a, b, type, dry_run=dry_run).to_dict()


@mcp.tool(alias_of=("constructicon_link", "retype"))
def constructicon_retype_link(a: str | int, b: str | int, from_type: str, to_type: str,
                              note: str | None = None, dry_run: bool = False) -> dict:
    return cards.retype_link(a, b, from_type, to_type, note=note, dry_run=dry_run).to_dict()


@mcp.tool()
def constructicon_retype_links(mapping: list[dict], dry_run: bool = True, partial_ok: bool = False) -> dict:
    """Retype many v1 `related` links at once. DRY-RUN BY DEFAULT (dry_run=false writes).
    mapping: [{a, b, to_type, from_type="related"}], each read "a <to_type> b". All-or-nothing
    in one transaction unless partial_ok=true (valid ones applied, the rest reported). Candidates:
    constructicon_needs(view="card_decisions", need="untyped_link").
    -> {ok, dry_run, applied, changes, warnings, batch_id, items: [{a, b, to_type, ok, changes,
    error?}]}."""
    return cards.retype_links(mapping, dry_run=dry_run, partial_ok=partial_ok)


@mcp.tool(alias_of=("constructicon_link", "list"))
def constructicon_list_links(card: str | int) -> list[dict] | dict:
    return cards.list_links(card)


@mcp.tool(alias_of=("constructicon_needs", "card_decisions"))
def constructicon_list_needs_decision(kind: str | None = None, need: str | None = None,
                                      hobby: str | None = None, limit: int | None = None,
                                      card: str | int | None = None) -> list[dict]:
    return cards.list_needs_decision(kind=kind, need=need, hobby=hobby, limit=limit, card=card)


# --- V2 cards piece 6: the reorganizing toolkit (spec 7.1 / 7.3) -------------------
# Thin wrappers over core/cards.py; no rule lives here. Single-card tools apply by
# default; bulk tools (bulk_edit, resolve_decisions, retype_links) are DRY-RUN by default.

@mcp.tool()
def constructicon_split_card(source: str | int, parts: list[dict], keep_in_source: bool = False,
                             dry_run: bool = False, batch_id: str | None = None) -> dict:
    """Carve files (and description) out of one card into new cards, in one batch.
    parts: [{title, kind="project", relation="sibling" | "child", stage, stop_reason, provenance,
    provenance_credit, whereabouts, whereabouts_note, description, move_description, file_slugs,
    link_to_source: {type, note?} | null, hobbies: "inherit" | [slugs], families: "inherit" |
    [slugs], highlight}]. A child is nested under the source; a sibling copies the source's hobbies
    and families by default. Each part is a full card with a blank write-up. Files are MOVED out of
    the source unless keep_in_source=true; the source's write-up can't be split out. Pass the
    returned batch_id to later calls (constructicon_link, constructicon_card_hobby) to keep a
    reorganization in one undo.
    -> {ok, dry_run, changes, warnings, batch_id, created: [{id, slug, title, kind, relation,
    files}]}."""
    return cards.split_card(source, parts, keep_in_source=keep_in_source, dry_run=dry_run,
                            batch_id=batch_id).to_dict()


@mcp.tool(alias_of=("constructicon_delete", "card"))
def constructicon_delete_project(card: str | int, dry_run: bool = False, batch_id: str | None = None) -> dict:
    return _delete_card(card, dry_run, batch_id)


@mcp.tool()
def constructicon_merge_cards(keep: str | int, absorb: list[str | int], dry_run: bool = False,
                              batch_id: str | None = None) -> dict:
    """Fold cards into `keep` (the reverse of split), in one batch. Files, hobbies and families are
    unioned; children re-parented; links and blog attachments re-pointed (duplicates dropped; typed
    beats related). keep's cover and write-up win; an absorbed blank write-up goes, a real one stays
    as a file. Undo restores the absorbed cards with their ids. Refused: nest_cycle, bad_merge (a
    family/collection with a non-group card). -> {ok, dry_run, changes, warnings, batch_id, keep,
    absorbed}."""
    return cards.merge_cards(keep, absorb, dry_run=dry_run, batch_id=batch_id).to_dict()


@mcp.tool(alias_of=("constructicon_card_files", "move"))
def constructicon_move_files(slugs: list[str], from_card: str | int, to_card: str | int, dry_run: bool = False,
                             batch_id: str | None = None) -> dict:
    return cards.move_files(slugs, from_card, to_card, dry_run=dry_run, batch_id=batch_id).to_dict()


@mcp.tool(alias_of=("constructicon_card_files", "copy"))
def constructicon_copy_files(slugs: list[str], from_card: str | int, to_card: str | int, dry_run: bool = False,
                             batch_id: str | None = None) -> dict:
    return cards.copy_files(slugs, from_card, to_card, dry_run=dry_run, batch_id=batch_id).to_dict()


@mcp.tool(alias_of=("constructicon_card_hobby", "add"))
def constructicon_add_to_hobby(card: str | int, hobby: str | int, dry_run: bool = False,
                               batch_id: str | None = None) -> dict:
    return cards.add_to_hobby(card, hobby, dry_run=dry_run, batch_id=batch_id).to_dict()


@mcp.tool(alias_of=("constructicon_card_hobby", "remove"))
def constructicon_remove_from_hobby(card: str | int, hobby: str | int, dry_run: bool = False,
                                    batch_id: str | None = None) -> dict:
    return cards.remove_from_hobby(card, hobby, dry_run=dry_run, batch_id=batch_id).to_dict()


@mcp.tool(alias_of=("constructicon_update_project", None))
def constructicon_set_home(card: str | int, target: str | int | None = None, dry_run: bool = False) -> dict:
    return cards.set_home(card, target, dry_run=dry_run).to_dict()


@mcp.tool()
def constructicon_explain_card(card: str | int) -> dict:
    """Everything about a card, to read BEFORE proposing a change (read-only): identity, status (+
    labels, `provisional`), whereabouts, provenance, highlight, hobbies, families, members, parent,
    children, links, `home` (+ source, dangling_override) and `home_chain`, `files` (counts, date
    range), `level` (the 0-5 pips, each with a reason), `caption_check` (the Curator's caption
    check: passed, excused, reason, excused_files = [{type, label, count, reason}] for files of a
    type that isn't captioned, e.g. Word/PowerPoint/spreadsheets), open_decisions, needs, warnings,
    suggestions (provenance, typed links) and recent_changes (last 5, with batch ids)."""
    return cards.explain_card(card)


@mcp.tool()
def constructicon_bulk_edit(op: str, items: list[dict], dry_run: bool = True, partial_ok: bool = False) -> dict:
    """Run ONE setter over many cards. DRY-RUN BY DEFAULT (dry_run=false writes).
    op: set_status, set_kind, set_whereabouts, set_card_provenance, set_home, set_card_highlight,
    add_to_hobby, remove_from_hobby, add_to_family, remove_from_family, nest, unnest, link, unlink,
    retype_link, set_hobby_activity (these are the core operation names).
    items: [{card, args}]: e.g. set_status {card: "x", args: {stage: "done"}}; add_to_hobby {card:
    "x", args: {hobby: "3d-printing"}}; add_to_family {card: <member>, args: {family: <family>}};
    nest {card: <child>, args: {parent}}; link {card: a, args: {b, type, note?}};
    set_hobby_activity {card: <hobby>, args: {value: "inactive"}}. Unknown args are refused.
    In order, one transaction; all-or-nothing unless partial_ok=true.
    -> {ok, dry_run, op, applied, changes, warnings, batch_id, items: [{card, ok, applied, changes,
    warnings, error?}]}."""
    return cards.bulk(op, items, dry_run=dry_run, partial_ok=partial_ok)


@mcp.tool()
def constructicon_resolve_decisions(items: list, accept_suggested: bool = False, dry_run: bool = True,
                                    partial_ok: bool = False) -> dict:
    """Answer many card questions (card_status, card_built_for, card_kind, card_family_members) at
    once. DRY-RUN BY DEFAULT (dry_run=false writes).
    items: [{decision_id, choice}] (or choices: [...] for multi-answer); with
    accept_suggested=true just decision ids, each answered with its OWN suggestion (none:
    skipped). Find them with constructicon_needs(view="card_decisions"). All-or-nothing unless
    partial_ok=true. -> {ok, dry_run, batch_id, applied, would_apply, skipped, failed, changes,
    warnings, items: [{decision_id, card_slug, choice, status, changes, reason?, error?}]}."""
    return cards.resolve_decisions(items, accept_suggested=accept_suggested, dry_run=dry_run,
                                   partial_ok=partial_ok)


@mcp.tool()
def constructicon_undo(audit_id: str | int, force: bool = False, dry_run: bool = False) -> dict:
    """Reverse a change-log row, or a whole batch (audit_id = a row id or a batch_id), newest
    first, in one transaction. An undo is logged, so it can be undone too. Refused: undo_conflict
    (changed since; the message says what; force=true overrides), undo_refused (a migration, an
    already-undone entry, no row images), trash_expired. -> {ok, dry_run, changes, warnings,
    batch_id, undone, undone_ops}."""
    return cards.undo(audit_id, force=force, dry_run=dry_run).to_dict()


@mcp.tool()
def constructicon_list_changes(card: str | int | None = None, batch_id: str | None = None, limit: int = 50) -> dict:
    """The change log, newest first: {changes: [{id, op, actor, batch_id, timestamp, affected_slugs,
    undone_by, mutations: [{table, key, before, after}]}], withheld, withheld_note}. Filter by card or
    batch_id. A non-admin token gets only rows touching nothing hidden from them; `withheld` counts
    the rest of the newest `limit` rows (withheld_note says why)."""
    return cards.list_changes(card=card, batch_id=batch_id, limit=limit)


@mcp.tool()
def constructicon_sweep_stale_decisions(dry_run: bool = False) -> dict:
    """Resolve every open question nothing can answer any more (its file or card is gone, a
    card-match question has under 2 candidates left, a "does this replace ...?" question has no
    candidate). The web app also runs this hourly; listing never resolves anything.
    -> {ok, dry_run, changes, warnings, batch_id, resolved: [{id, kind, post_slug, reason}], count,
    by_kind}."""
    return decisions.sweep_stale(dry_run=dry_run).to_dict()




# --- Curator: the queue, nudges, needs and questions ---

def _nudges(kind, limit):
    """Curator nudges (dismissed ones left out), filtered by kind, then capped."""
    needs = curator_needs.list_needs()
    if kind is not None:
        needs = [n for n in needs if n["kind"] == kind]
    if limit is not None:
        needs = needs[:limit]
    return needs


def _queue(card, include_deferred):
    """The Curator queue as the caller may see it (curation_queue.for_actor: the registered door;
    unchanged for an admin)."""
    q = curation_queue.for_actor(curation_queue.build_queue(card=card))
    if not include_deferred:
        q = {**q, "deferred": []}
    return q


@mcp.tool(selector="view")
def constructicon_needs(view: typing.Literal["queue", "nudges", "caption", "card_decisions", "pending"] = "queue",
                        card: str | int | None = None, include_deferred: bool = True, nudge_kind: str | None = None,
                        card_kind: str | None = None, need: str | None = None, hobby: str | None = None,
                        limit: int | None = None, include_failed: bool = True) -> dict | list:
    """What waits on curation, in one of five views:
    "queue" (default; card, include_deferred): every open question, nudge and need, grouped by
      card, as the owner's Curator tab shows it -> {groups, deferred, counts}. Groups: cards
      (active first, then most recently touched), then hobby / uploads / collection buckets;
      inside, questions first. Item: {type: question | nudge | need, key, label, kind, group,
      href, deferred, dismissible} (+ for a question: id, options, suggested, confidence, multi).
      counts.open is the tab's badge.
    "card_decisions" (card, card_kind, need, hobby, limit): cards waiting on a decision, each with
      a suggestion: [{need, card_slug, hobby_slug, title, detail, suggested, suggested_reason,
      confidence, decision_id, options, link?}]. Stored questions (card_status, card_built_for,
      card_kind, card_family_members: answer with constructicon_resolve_pending_decision or
      constructicon_resolve_decisions) and computed needs (hobby_inactive_with_active_work,
      hobby_active_untouched, untyped_link, missing_provenance, missing_provenance_credit,
      missing_whereabouts, status_conflict, blank_writeup_with_files: fix the field).
      card_kind "hobby" = hobby needs only.
    "pending" (no filters): every open stored question with its options: card questions,
      project_match (candidates), retype (options; the item's current type = keep it),
      item_supersedes (options, suggested).
    "nudges" (nudge_kind, limit): Curator nudges by priority: [{nudge_key, kind, target_type,
      target_id, target_slug, title, summary, priority, base_impact, status_weight, action,
      deferred}].
    "caption" (limit default 50, max 500; include_failed): items with no caption yet (never
      sensitive) -> {items: [{slug, name, type, filename, caption_status, projects, hobbies}],
      total}. Then constructicon_view and constructicon_set_caption.
    Act on an item with constructicon_queue_item(key, action).
    """
    defaults = {"card": (card, None), "include_deferred": (include_deferred, True), "nudge_kind": (nudge_kind, None),
                "card_kind": (card_kind, None), "need": (need, None), "hobby": (hobby, None), "limit": (limit, None),
                "include_failed": (include_failed, True)}
    applies = {"queue": ("card", "include_deferred"), "nudges": ("nudge_kind", "limit"),
               "caption": ("limit", "include_failed"), "card_decisions": ("card", "card_kind", "need", "hobby", "limit"),
               "pending": ()}
    if view not in applies:
        raise InvalidInput(f"Unknown view {view!r}: use {', '.join(applies)}.")
    _only_for(f'view="{view}"', **{k: v for k, v in defaults.items() if k not in applies[view]})
    if view == "queue":
        return _queue(card, include_deferred)
    if view == "nudges":
        return _nudges(nudge_kind, limit)
    if view == "caption":
        return _needs_caption(50 if limit is None else limit, include_failed)
    if view == "card_decisions":
        return cards.list_needs_decision(kind=card_kind, need=need, hobby=hobby, limit=limit, card=card)
    return _pending_decisions()


@mcp.tool(alias_of=("constructicon_needs", "nudges"))
def constructicon_list_needs(kind: str | None = None, limit: int | None = None) -> list[dict]:
    return _nudges(kind, limit)


@mcp.tool(alias_of=("constructicon_needs", "queue"))
def constructicon_curation_queue(card: str | None = None, include_deferred: bool = True) -> dict:
    return _queue(card, include_deferred)


@mcp.tool(selector="action")
def constructicon_queue_item(key: str, action: typing.Literal["defer", "bring_back", "dismiss"]) -> dict:
    """Act on one Curator queue item by its `key` (from constructicon_needs: "decision:<id>",
    "<kind>:project:<id>", "need:<need>:<slug>"):
    "defer": set it aside in the Deferred section, no timer, until answered or brought back.
    "bring_back": back into the main queue (a dismissed one is not).
    "dismiss": a nudge or need, for good. A question can't be dismissed: answer or defer it.
    Logged and undoable. -> {ok, changed}.
    """
    if action == "dismiss":
        return curation_queue.dismiss(key)  # a QueueError (400 bad_request) becomes the shared error shape
    if action == "defer":
        return curation_queue.defer(key)
    if action == "bring_back":
        return curation_queue.bring_back(key)
    raise InvalidInput(f"Unknown action {action!r}: use defer, bring_back or dismiss.")


@mcp.tool(alias_of=("constructicon_queue_item", "dismiss"))
def constructicon_dismiss_need(nudge_key: str) -> dict:
    return curation_queue.dismiss(nudge_key)  # a QueueError (400 bad_request) becomes the shared error shape


@mcp.tool(alias_of=("constructicon_queue_item", "defer"))
def constructicon_defer(key: str) -> dict:
    return curation_queue.defer(key)  # a QueueError (400 bad_request) becomes the shared error shape


@mcp.tool(alias_of=("constructicon_queue_item", "bring_back"))
def constructicon_bring_back(key: str) -> dict:
    return curation_queue.bring_back(key)  # a QueueError (400 bad_request) becomes the shared error shape


@mcp.tool(alias_of=("constructicon_needs", "pending"))
def constructicon_list_pending_decisions() -> list[dict]:
    return _pending_decisions()


def _pending_decisions():
    """Every open stored question (decisions.list_open: leaves out stale ones and questions about
    items the caller can't see; resolves nothing), shaped per kind."""
    result = []
    for item in decisions.list_open():
        if cards.is_card_decision_slug(item["post_slug"]):
            # V2 card question: slug is "card:<project slug>"; carries the suggested answer.
            summary = cards.decision_summary({"id": item["id"], "kind": item["kind"],
                                              "post_slug": item["post_slug"], "payload": item["payload"]})
            result.append({
                "id": item["id"],
                "kind": item["kind"],
                "slug": item["post_slug"],
                "card_slug": summary["card_slug"],
                "title": summary["title"],
                "media_type": None,
                "created_at": item["created_at"],
                "question": summary["question"],
                "legacy_status": summary["legacy_status"],
                "provisional": summary["provisional"],
                "suggested": summary["suggested"],
                "suggested_reason": summary["suggested_reason"],
                "confidence": summary["confidence"],
                "options": summary["options"],
            })
            continue
        row = item["row"]
        title = item_title.title_of(row)

        entry = {
            "id": item["id"],
            "kind": item["kind"],
            "slug": item["post_slug"],
            "title": title,
            "media_type": row.get("media_type"),
            "created_at": item["created_at"],
        }

        if item["kind"] == "project_match":
            entry["candidates"] = item.get("candidates", [])
        elif item["kind"] == "retype":
            entry["question"] = item.get("question", "")
            entry["options"] = item.get("options", [])
        elif item["kind"] == revisions.KIND_ITEM_SUPERSEDES:
            # #477: options are the earlier files this upload might replace (key = their slug) + "none".
            entry["question"] = item.get("question", "")
            entry["options"] = item.get("options", [])
            entry["suggested"] = item.get("suggested")

        result.append(entry)

    return result


@mcp.tool(selector="action")
def constructicon_revision(slug: str, action: typing.Literal["supersede", "remove"], new: str | None = None,
                           dry_run: bool = False, batch_id: str | None = None) -> dict:
    """Revision chains of files (linear: A -> B -> C; the CURRENT one has nothing newer). An older
    revision shows "Superseded" and leaves browse listings (search still finds it).
    "supersede" (slug = the older file, new = the file that replaces it): -> {ok, dry_run,
      batch_id, old, new, chain}. Errors: bad_revision (self-link, redacted), revision_cycle,
      revision_conflict (old already has a newer one, or new already replaces another).
    "remove": take `slug` out and close the gap (A -> B -> C minus B is A -> C). A no-op
      (removed: false) for a file in no chain. -> {ok, dry_run, batch_id, slug, removed, chain}.
    Read a chain with constructicon_get(slug, include=["revisions"]). Upload-time "does this
    replace ...?" questions are the owner's (constructicon_resolve_pending_decision).
    """
    if action == "supersede":
        if not new:
            raise InvalidInput('action="supersede" needs new (the item that replaces slug).')
        return revisions.mark_superseded(slug, new, batch_id=batch_id, dry_run=dry_run)
    if action == "remove":
        _only_for('action="remove"', new=(new, None))
        return revisions.remove_from_chain(slug, batch_id=batch_id, dry_run=dry_run)
    raise InvalidInput(f"Unknown action {action!r}: use supersede or remove.")


@mcp.tool(alias_of=("constructicon_revision", "supersede"))
def constructicon_mark_superseded(old: str, new: str, dry_run: bool = False, batch_id: str | None = None) -> dict:
    return revisions.mark_superseded(old, new, batch_id=batch_id, dry_run=dry_run)


@mcp.tool(alias_of=("constructicon_revision", "remove"))
def constructicon_remove_from_revisions(slug: str, dry_run: bool = False, batch_id: str | None = None) -> dict:
    return revisions.remove_from_chain(slug, batch_id=batch_id, dry_run=dry_run)


@mcp.tool()
def constructicon_resolve_pending_decision(decision_id: int, choice: str = "", project_ids: list[int] | None = None,
                                           choices: list[str] | None = None) -> dict:
    """Answer one stored question (see constructicon_needs(view="pending")), applied in one batch.
    project_match: project_ids = the cards to attach the file to ([] = none of these).
    retype: choice = the type to keep it as (the item's current type keeps it as is).
    item_supersedes: choice = the older file's slug (this one replaces it), "reverse:<slug>",
      "same:<slug>" (identical: this upload goes to the trash) or "none".
    card questions: choice = an option key, or choices = several for card_built_for /
      card_family_members (a family answer makes the card a family with the picked members).
    An answer that breaks a rule leaves the question open and returns that rule's error.
    -> {ok, applied, remaining}. Errors: already_resolved, unknown_decision_kind, invalid_choice.
    """
    if project_ids is None:
        project_ids = []

    return decisions.resolve(decision_id, choice=choice, project_ids=project_ids, choices=choices or ())


@mcp.tool()
def constructicon_run_type_action(slug: str, action: str) -> dict:
    """Run an action on an item. action: one of the item's `actions` (from constructicon_get, e.g.
    "fetch_youtube_metadata"), or the built-in "retry_ocr": run text extraction / OCR again on any
    OCR-capable item (in the background; ocr_status goes "pending").
    -> {ok, action, item, ...what the action adds}. Errors: redacted, unknown_action,
    action_failed; retry_ocr: bad_request (redacted, or a type without OCR).
    """
    if action == RESERVED_ACTION:
        return {"ok": True, "action": action, "item": _retry_ocr(slug)}
    row = db.get_by_slug(slug)
    if row is None:
        raise NotFound(f"No item {slug!r}.")
    policy.require_view(row, message=f"No item {slug!r}.")  # #746: was missing (the old net never reached it)

    if row.get("redacted"):
        raise InvalidInput("object is redacted", code="redacted")

    spec = object_types.get_object_type(row.get("media_type"))
    action_obj = next((a for a in spec.actions if a.key == action), None)
    if action_obj is None:
        raise InvalidInput(f"{spec.label} has no action '{action}'", code="unknown_action")
    if not action_obj.applies_to(row):  # #446: e.g. Reclassify is .exe-only
        raise InvalidInput(f"{spec.label} has no action '{action}' for this item", code="unknown_action")

    try:
        result = action_obj.handler(row)
    except errors.AppError:
        raise
    except Exception as e:
        print(f"Action '{action}' failed: {e!r}", flush=True)
        raise errors.AppError("action_failed", f"Action '{action}' failed: {e}", status=500) from e

    updated_row = db.get_by_slug(slug)
    return {
        "ok": True,
        "action": action,
        **(result or {}),
        "item": _to_public(updated_row)
    }


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


if __name__ == "__main__":
    # #746: every call through a retired tool name is one line in the container log ("MCP alias
    # call: old -> new"), so it shows when work Claude and scripts have moved to the new names.
    if not _alias_log.handlers:
        _alias_handler = logging.StreamHandler(sys.stdout)
        _alias_handler.setFormatter(logging.Formatter("constructicon-mcp: %(message)s"))
        _alias_log.addHandler(_alias_handler)
        _alias_log.propagate = False
    # #549: web owns background work (migrations, OCR self-heal, captions). This process only
    # runs the idempotent schema DDL; a stuck OCR row is healed by web's startup pass and its
    # periodic watchdog (same shared DB), and captions are enqueued, never run, here.
    db.init_db(migrate=False)
    # #700: record the label this process named its tools after, so Admin > Install can say
    # "restart the MCP" until it runs with the current one. Best effort: never blocks the start.
    try:
        install_config.record_mcp_started_label(STARTED_LABEL)
    except Exception as exc:  # noqa: BLE001 -- a locked or read-only DB must not stop the MCP
        besteffort.warn(logging.getLogger("constructicon-mcp"), "could not record the started hobby label", exc)
    print(f"constructicon-mcp: hobby label {STARTED_LABEL.one} / {STARTED_LABEL.many}"
          f" ({len(_HIDDEN_ALIASES)} hidden alias tool name(s))", flush=True)
    # #561: bearer install-token auth. Fail BEFORE touching anything else if misconfigured.
    import uvicorn
    from mcp_server import auth as mcp_auth
    try:
        _token = mcp_auth.load_token()
    except mcp_auth.TokenConfigError as exc:
        sys.exit(f"constructicon-mcp: refusing to start: {exc}")
    if _token:
        print("constructicon-mcp: bearer token auth ENABLED (install token = role admin, actor mcp; per-user tokens act as their user)", flush=True)
    elif roles.ENFORCE:
        # #467 step 2: roles are enforced, so an MCP without a token would be an open admin door.
        # Refuse to start (a crash-looping container is loud; a server that 401s everything looks
        # healthy). See CLAUDE.md "Auth enforcement" for the rollout.
        sys.exit("constructicon-mcp: refusing to start: no install token configured and roles are enforced. "
                 "Set CONSTRUCTICON_INSTALL_TOKEN_FILE (or CONSTRUCTICON_MCP_TOKEN_FILE) to a token file "
                 "made with: python3 -m core.manage.mcp_token generate <file>")
    else:
        logging.basicConfig(level=logging.WARNING)
        logging.getLogger("constructicon-mcp").warning(
            "MCP is running WITHOUT a token: anyone who can reach port 8100 can read, change and "
            "delete the archive. Set CONSTRUCTICON_MCP_TOKEN or CONSTRUCTICON_MCP_TOKEN_FILE "
            "(see core/manage/mcp_token.py)."
        )
        print("constructicon-mcp: WARNING: MCP is running WITHOUT a token (open access)", flush=True)
    _app = mcp_auth.wrap(
        mcp.streamable_http_app(host="0.0.0.0", stateless_http=True), _token)
    uvicorn.run(_app, host="0.0.0.0", port=8100, log_level="info")
