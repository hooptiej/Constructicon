"""The MCP tools, one module per area (#547 step 3: they were the second half of mcp_server/server.py).

Importing this package registers every tool, through the `@mcp.tool()` wrapper (mcp_server/wrapper.py),
in the order the old server.py registered them. That order is what tools/list shows, so **the order of
the modules below is part of the MCP's surface**: each module holds a run of tools that sat together in
the old file, and a new tool goes at the end of the module whose area it belongs to (or in a new module
at the end of this list), never in between, unless reordering tools/list is the point.
scripts/test_mcp_tools_package_547.py compares tools/list with the pre-split snapshot
(scripts/data/mcp_tools_547.json); update the snapshot only on purpose.

    files         version, upload, replace_file, search, get (+ aliases), download, import, list_import
    items         update, the caption tools (view, set_caption, caption_review), undated, redact (+ aliases)
    lists         list_items (+ aliases)
    housekeeping  delete, trash, duplicates, lookalikes, proposals, backup (+ aliases)
    content       add_content, related (+ aliases)
    cards         list / create / update / get project, card_files (+ aliases)
    hobbies       list / create / update hobby, convert, unmark, card_hobby (+ aliases)
    blog          blog entries (+ aliases)
    tags          list_tags, create_tag, tags, item_tags (+ aliases)
    fields        set_agent_notes, provenance options, word lists, the retired one-field setters (aliases)
    card_ops      nest, family, sharing, link, retype_links, split / merge, explain, bulk_edit,
                  resolve_decisions, undo, list_changes, sweep_stale_decisions (+ aliases)
    curator       needs, queue_item, revision, resolve_pending_decision, run_type_action (+ aliases)

A tool module imports only the shared modules (mcp_server/app.py, wrapper.py, shapes.py, _common.py,
listing.py), never another tool module: a helper two tool modules need lives in _common.py (or
listing.py). Patch a name through `mcp_server.server` (`server._run_in_thread = ...`), which reaches
every module holding it; see mcp_server/server.py.
"""

from . import (files, items, lists, housekeeping, content, cards, hobbies, blog, tags, fields,  # noqa: F401
               card_ops, curator)

MODULES = (files, items, lists, housekeeping, content, cards, hobbies, blog, tags, fields, card_ops, curator)
