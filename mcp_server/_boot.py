"""Boot steps the MCP takes before anything else loads (#547 step 3; moved verbatim from server.py).

Imported first by mcp_server/server.py, and by mcp_server/app.py and mcp_server/action_roles.py, so
whichever MCP module loads first sets the process role (#549) and the file mode (#652) before core
loads. The core imports keep the old server.py's order (they are what every tool module reads).
"""

import os
from datetime import datetime  # noqa: F401  (unused: the old server.py imported it, so server.datetime stays)

# #549: this is NOT the process that does background work. core.captions.run_caption checks this
# role and enqueues (caption_queue table) instead of calling the GPU; the web process drains it.
os.environ["CONSTRUCTICON_ROLE"] = "mcp"


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
