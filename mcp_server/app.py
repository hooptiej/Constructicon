"""The MCPServer instance and what it is named after (#547 step 3; moved verbatim from server.py).

Settings (BASE_URL, IMPORT_DIR, DOWNLOAD_INLINE_MAX_BYTES), the hobby-label naming of tools (#700),
SERVER_INSTRUCTIONS (#746) and `mcp`. mcp_server/wrapper.py replaces `mcp.tool` with the wrapper.
"""

import inspect
import os
import re
from pathlib import Path

from mcp.server.mcpserver import MCPServer

from mcp_server import _boot  # noqa: F401  (the MCP role and file mode first, #549 / #652)

from core import version as version_info, install_config, hobby_label as hobby_label_rules


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
