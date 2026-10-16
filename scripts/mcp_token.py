"""Moved (#634): the install token tool is now core/manage/mcp_token.py.

scripts/ is no longer mounted into the app containers, so the tool moved into core/, which is.
This stub only says where it went (and exits non-zero). Never prints a token.
"""

import shlex
import sys

_ARGS = " ".join(shlex.quote(a) for a in sys.argv[1:]) or "generate|check ..."

sys.stderr.write(
    "mcp_token.py moved (#634): use `python3 -m core.manage.mcp_token ...` instead.\n"
    "  On the host, from the checkout:  python3 -m core.manage.mcp_token " + _ARGS + "\n"
    "  In a container:                  sudo docker exec <web container> python3 -m core.manage.mcp_token " + _ARGS + "\n"
    "Nothing was written.\n"
)
sys.exit(2)
