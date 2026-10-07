"""Install-token auth for scripts that talk HTTP to a Constructicon web app (#467 step 2).

Since step 2 every page and API needs a signed-in user or the install token, so a script sends
`Authorization: Bearer <install token>` (role admin, logged as actor `token`). Every script does it
the same way, with one line after it knows its target:

    import _http
    _http.install(args.base_url)     # then plain urllib.request.urlopen(...) as before

`install` puts a global urllib opener in place that adds the header ONLY to requests for that
base URL's scheme/host/port (a script that also calls YouTube or GitHub never leaks the token
there), and never across a redirect (`add_unredirected_header`).

Where the token comes from (first hit wins; never printed):
  CONSTRUCTICON_TOKEN          the token itself
  CONSTRUCTICON_TOKEN_FILE     a file holding it (e.g. a copy of the install's secret file)
  then the install's own variables (core/install_token.py: CONSTRUCTICON_INSTALL_TOKEN(_FILE),
  CONSTRUCTICON_MCP_TOKEN(_FILE)), so a script run inside the web container
  (`docker exec <web container> python3 scripts/x.py`) just works.

No token found: `install` warns once on stderr and requests go out anonymous (expect 401).
"""

import os
import sys
import urllib.request
from urllib.parse import urlsplit

_HERE = os.path.dirname(os.path.abspath(__file__))
if os.path.dirname(_HERE) not in sys.path:
    sys.path.insert(0, os.path.dirname(_HERE))


class TokenError(RuntimeError):
    pass


def token():
    """The install token for scripts, or '' when none is configured. TokenError (without the token
    in the message) when a configured source is unreadable or empty."""
    value = (os.environ.get("CONSTRUCTICON_TOKEN") or "").strip()
    if value:
        return value
    path = (os.environ.get("CONSTRUCTICON_TOKEN_FILE") or "").strip()
    if path:
        try:
            with open(path, "r", encoding="utf-8") as f:
                value = f.read().strip()
        except OSError as exc:
            raise TokenError(f"CONSTRUCTICON_TOKEN_FILE {path!r} can't be read ({exc.strerror or exc})") from None
        if not value:
            raise TokenError(f"CONSTRUCTICON_TOKEN_FILE {path!r} is empty")
        return value
    from core import install_token  # stdlib only
    try:
        return install_token.load()
    except install_token.TokenConfigError as exc:
        raise TokenError(str(exc)) from None


def _origin(url):
    parts = urlsplit(url)
    scheme = (parts.scheme or "http").lower()
    port = parts.port or (443 if scheme == "https" else 80)
    return scheme, (parts.hostname or "").lower(), port


def headers(extra=None, tok=None):
    """`extra` plus the Authorization header (when a token is configured). For code that builds
    its own urllib.request.Request rather than relying on install()."""
    h = dict(extra or {})
    tok = token() if tok is None else tok
    if tok:
        h["Authorization"] = f"Bearer {tok}"
    return h


class BearerHandler(urllib.request.BaseHandler):
    handler_order = 100  # before the default handlers

    def __init__(self, base_url, tok):
        self.origin = _origin(base_url)
        self._token = tok

    def _add(self, req):
        if _origin(req.full_url) == self.origin and not req.has_header("Authorization"):
            req.add_unredirected_header("Authorization", f"Bearer {self._token}")
        return req

    http_request = _add
    https_request = _add


def install(base_url, *, quiet=False):
    """Make every urllib request to `base_url`'s origin carry the install token. Returns True when
    a token was found. Call again for another target (the last call wins)."""
    tok = token()
    if not tok:
        if not quiet:
            print("note: no install token (set CONSTRUCTICON_TOKEN_FILE); requests to "
                  f"{base_url} go out anonymous and will get 401", file=sys.stderr)
        urllib.request.install_opener(urllib.request.build_opener())
        return False
    urllib.request.install_opener(urllib.request.build_opener(BearerHandler(base_url, tok)))
    return True
