"""constructicon-mcp — MCP server for the Constructicon media gallery.

Uses the mcp package's v2 MCPServer API
(FastMCP was renamed/restructured in mcp 2.x — see the SDK migration guide).
Streamable-HTTP transport, host/port/stateless_http passed to run().

Exposes MCP tools for uploading, managing, tagging, and organizing media
in a Constructicon instance. Runs as a sidecar alongside constructicon-web.

Conventions shared by every tool (#560, #548; enforced by the `@mcp.tool()` wrapper, mcp_server/wrapper.py):
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
    ACTION_ROLES (mcp_server/action_roles.py; all admin today: the MCP is one admin identity; ready for per-user tokens).

#547 step 3 ("Four-Thousand-Line Club"): this file was the whole 3,660-line server. It is now the entry
point (`python -m mcp_server.server`) and nothing else; the code moved verbatim, with no behaviour
change, into modules that load in this order (each imports only from the ones above it):

    _boot.py         the process role (#549), the file mode (#652), then core, in the old order
    app.py           settings, the hobby-label tool names (#700), SERVER_INSTRUCTIONS, `mcp`
    action_roles.py  ACTION_ROLES and role_for (#746)
    wrapper.py       the one `@mcp.tool()` wrapper (#560 actor, #548 errors, aliases, roles)
    shapes.py        `_to_public`, `_to_public_listed` and the other shapers, the text preview (#697)
    _common.py       helpers more than one tool module shares
    listing.py       the item lists and #722's paging
    tools/           the tools, one module per area; tools/__init__.py's order is the tools/list order
    checks.py        the startup checks: every tool through the wrapper, ACTION_ROLES complete

Callers and tests don't change: `from mcp_server import server`, then `server.<name>` reaches every name
the old module had (this file copies every module's names onto itself). Two rules, the same as
core/db and core/cards (#547 steps 1-2):

* One copy of every name: each is defined in one module, the others import that object; loading refuses
  if two modules bind one name to different objects.
* Assigning to this module reaches the others: `server._call_as_mcp = fake` (test_bulk_visibility_709)
  rebinds it in every module that held the same object, so the wrapper calls the fake, as it did when it
  read its own module's globals. Patch through `mcp_server.server`, never a submodule
  (`mcp_server.wrapper._call_as_mcp = ...` reaches only that file).

scripts/test_mcp_tools_package_547.py checks both, and that tools/list (names, descriptions, schemas,
order) is exactly the pre-split one (scripts/data/mcp_tools_547.json).
"""

import sys
import types as _types
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from mcp_server import _boot  # noqa: E402  (the MCP role and file mode, then core, before anything below)
from mcp_server._boot import db, paths  # noqa: E402

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

# Everything else loads now (the hobby label is read in app.py), in the order the old file ran it.
from mcp_server import app, action_roles, wrapper, shapes, _common, listing, tools, checks  # noqa: E402

_SUBMODULES = (_boot, app, action_roles, wrapper, shapes, _common, listing) + tools.MODULES + (checks,)


def _export():
    owner = {}
    for mod in _SUBMODULES:
        for name, value in vars(mod).items():
            if name.startswith("__") and name.endswith("__"):
                continue
            if name in owner and owner[name][1] is not value:
                raise ImportError(f"mcp_server.server: {name!r} is bound to different objects in {owner[name][0]} and "
                                  f"{mod.__name__}; each name must live in one module (#547)")
            owner.setdefault(name, (mod.__name__, value))
    g = globals()
    for name, (_mod, value) in owner.items():
        g[name] = value


class _Server(_types.ModuleType):
    """mcp_server.server's module type: `server.X = v` also rebinds X in each module that held the old object."""

    _submodules = _SUBMODULES

    def __setattr__(self, name, value):
        missing = object()
        old = self.__dict__.get(name, missing)
        if old is not missing and not (name.startswith("__") and name.endswith("__")):
            for mod in type(self)._submodules:
                if vars(mod).get(name, missing) is old:
                    setattr(mod, name, value)
        super().__setattr__(name, value)


_export()
sys.modules[__name__].__class__ = _Server
del _types, _SUBMODULES, _export, _Server


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
