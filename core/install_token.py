"""The install token (#467 step 2; #561): ONE secret shared by the MCP sidecar and the web app.

Who presents it:
  * the MCP transport (mcp_server/auth.py): `Authorization: Bearer <token>` on every request,
    role admin, actor `mcp`;
  * non-browser web clients (scripts, verification): the same header on
    web requests, role admin, actor `token` (web/auth.py's AccessMiddleware). No CSRF needed:
    a browser can't attach this header cross-site without a CORS preflight the app never grants.

Where it lives (first hit wins; every name is read, so one secret file can serve both containers):
  CONSTRUCTICON_INSTALL_TOKEN        the token itself (tests, dev)
  CONSTRUCTICON_INSTALL_TOKEN_FILE   path to a file whose stripped contents are the token (preferred)
  CONSTRUCTICON_MCP_TOKEN            older names (#561), still honoured
  CONSTRUCTICON_MCP_TOKEN_FILE

Create the file with `python scripts/mcp_token.py generate <file>` (mode 600, prints only the path).
A token shorter than MIN_TOKEN_LEN, an unreadable file or an empty file is a configuration error
(TokenConfigError): the MCP refuses to start, and so does the web app (web/app.py startup). Rotate
by regenerating the file and restarting BOTH containers.

Stdlib only, no DB import (scripts/mcp_token.py loads it on a bare machine). Never logs, prints or
echoes the token; error messages name the variable or path only.
"""

import hmac
import os
import threading

MIN_TOKEN_LEN = 32

# (variable, is_file), in precedence order.
SOURCES = (
    ("CONSTRUCTICON_INSTALL_TOKEN", False),
    ("CONSTRUCTICON_INSTALL_TOKEN_FILE", True),
    ("CONSTRUCTICON_MCP_TOKEN", False),
    ("CONSTRUCTICON_MCP_TOKEN_FILE", True),
)


class TokenConfigError(RuntimeError):
    pass


def configured_source(env=None):
    """The name of the variable the token would come from, or None when none is set."""
    env = os.environ if env is None else env
    for name, _is_file in SOURCES:
        if (env.get(name) or "").strip():
            return name
    return None


def load(env=None):
    """The configured token, or '' when none is set. TokenConfigError when the source that IS set
    is unreadable, empty or too short. Never includes the token in a message."""
    env = os.environ if env is None else env
    name = configured_source(env)
    if name is None:
        return ""
    value = env.get(name).strip()
    if dict(SOURCES)[name]:
        try:
            with open(value, "r", encoding="utf-8") as f:
                token = f.read().strip()
        except OSError as exc:
            raise TokenConfigError(f"{name} is set to {value!r} but it can't be read ({exc.strerror or exc})")
        if not token:
            raise TokenConfigError(f"{name} {value!r} is empty")
    else:
        token = value
    if len(token) < MIN_TOKEN_LEN:
        raise TokenConfigError(
            f"the install token from {name} is too short (need at least {MIN_TOKEN_LEN} characters). "
            "Generate one with: python scripts/mcp_token.py generate <file>")
    return token


_cache = {"loaded": False, "token": b""}
_lock = threading.Lock()


def reset_cache():
    """Forget the cached token (tests that change the environment; nothing else needs it)."""
    with _lock:
        _cache["loaded"] = False
        _cache["token"] = b""


def current():
    """The token as bytes ('' when none is configured), read once per process and cached.
    Raises TokenConfigError on a broken configuration (each time, until it is fixed)."""
    with _lock:
        if not _cache["loaded"]:
            _cache["token"] = load().encode("utf-8")
            _cache["loaded"] = True
        return _cache["token"]


def enabled():
    return bool(current())


def matches(presented):
    """True when `presented` (str or bytes) is the configured token. Always False when no token is
    configured, so an install without one accepts no bearer at all (fail closed)."""
    token = current()
    if not token or not presented:
        return False
    if isinstance(presented, str):
        presented = presented.encode("utf-8", "replace")
    return hmac.compare_digest(presented, token)


def bearer_value(authorization):
    """The credential from an `Authorization: Bearer <x>` header value (str or bytes), or None when
    the header is absent or another scheme."""
    if not authorization:
        return None
    if isinstance(authorization, bytes):
        authorization = authorization.decode("latin-1")
    scheme, _, value = authorization.strip().partition(" ")
    if scheme.lower() != "bearer":
        return None
    return value.strip()
