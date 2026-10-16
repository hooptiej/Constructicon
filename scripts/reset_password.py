"""Moved (#634): the password reset tool is now core/manage/reset_password.py.

scripts/ is no longer mounted into the app containers, so the tool moved into core/, which is.
This stub only says where it went (and exits non-zero), so an old command fails loudly with the
new one in hand instead of silently doing nothing.
"""

import shlex
import sys

_ARGS = " ".join(shlex.quote(a) for a in sys.argv[1:]) or "<username>"

sys.stderr.write(
    "reset_password.py moved (#634): use `python3 -m core.manage.reset_password ...` instead.\n"
    "  On the box:  sudo docker exec -it <web container> python3 -m core.manage.reset_password " + _ARGS + "\n"
    "  Same flags as before: <username>, --generate, --enable, --create-admin USERNAME, --display-name, --list.\n"
    "Nothing was changed.\n"
)
sys.exit(2)
