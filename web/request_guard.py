"""Request guard (#558, #559): same-origin enforcement for mutating requests, and
route-driven redaction rules for the audit log.

Why this exists. Constructicon has no login, so any web page the owner opens on the
LAN could make the owner's browser POST to it (a hidden cross-site <form> is a "simple
request": no CORS preflight). The guard rejects a mutating request whose browser-sent
Origin (or Referer) is not this app's own host.

Policy for the headers, deliberately:
  * Origin present  -> its host:port must equal the request's own Host header (or be in
    CONSTRUCTICON_ALLOWED_ORIGINS). Same-origin is install-agnostic: constructicon.local,
    the bare LAN IP, the test box and a work install all just work, with no config.
  * Origin absent   -> fall back to Referer with the same rule.
  * Both absent     -> ALLOWED. Non-browser clients (curl, scripts, the desktop uploader,
    urllib verification scripts) send neither header. Every modern browser sends Origin
    on a cross-origin POST and a page cannot suppress it, so a browser-borne forgery
    can't use the no-header path. #561 (MCP/install token) will tighten this case later.
  * "Origin: null" (sandboxed iframes, file://, some redirects) is cross-origin: rejected.
"""

import os
import re
from urllib.parse import urlsplit

from starlette.responses import JSONResponse

MUTATING_METHODS = {"POST", "PUT", "PATCH", "DELETE"}


def _allowed_extra_origins():
    """CONSTRUCTICON_ALLOWED_ORIGINS: comma list of origins (http://host:port) or bare
    host[:port] values that are also acceptable. Read per call so tests can set it."""
    raw = os.getenv("CONSTRUCTICON_ALLOWED_ORIGINS", "")
    return {part.strip().lower() for part in raw.split(",") if part.strip()}


def _strip_default_port(netloc):
    """Browsers omit a default port from Host and Origin; other clients (urllib) keep it.
    Treat host, host:80 and host:443 as the same authority so they compare equal."""
    for suffix in (":80", ":443"):
        if netloc.endswith(suffix):
            return netloc[: -len(suffix)]
    return netloc


def _host_of(origin_or_url):
    """host[:port] of an Origin/Referer value, lowercased, default port stripped; None if unparseable."""
    try:
        netloc = urlsplit(origin_or_url).netloc
    except ValueError:  # silent-ok: unparseable = None, and the guard refuses a mutating request with no usable host
        return None
    return _strip_default_port(netloc.lower()) or None


def origin_violation(method, headers):
    """None if the request passes, else a short reason string (for the 403 message)."""
    if method.upper() not in MUTATING_METHODS:
        return None
    own_host = _strip_default_port((headers.get("host") or "").strip().lower())
    extra = _allowed_extra_origins()
    extra_hosts = {_host_of(e) or e for e in extra}

    def ok(value):
        host = _host_of(value)
        if host is None:
            return False
        return host == own_host or host in extra_hosts or value.strip().lower() in extra

    origin = headers.get("origin")
    if origin is not None:
        # "null" has no host, so ok() is False -> rejected.
        return None if ok(origin.strip()) else f"cross-origin request refused (Origin: {origin[:80]})"
    referer = headers.get("referer")
    if referer is not None:
        return None if ok(referer.strip()) else "cross-origin request refused (Referer)"
    return None  # no browser-sent provenance headers: non-browser client, see module docstring


class OriginGuardMiddleware:
    """Pure ASGI middleware (no body buffering, so it can't disturb streaming uploads)."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] == "http" and scope["method"] in MUTATING_METHODS:
            headers = {k.decode("latin-1").lower(): v.decode("latin-1") for k, v in scope.get("headers", [])}
            reason = origin_violation(scope["method"], headers)
            if reason:
                response = JSONResponse(
                    {"ok": False, "error": {"code": "cross_origin", "message": reason},
                     "detail": reason},
                    status_code=403,
                )
                await response(scope, receive, send)
                return
        await self.app(scope, receive, send)


# --- Audit body redaction (#559): by route, with the field-name heuristic as backstop ---

REDACTED = "[REDACTED]"

# Routes whose request bodies carry (or may carry) secrets or nothing worth logging.
#  "none":       no body values logged at all.
#  "name_only":  the setting's *name* (field `key`) is kept, every other value is redacted.
AUDIT_ROUTE_RULES = {
    "/api/settings": "name_only",
    "/api/account/desktop-app-build": "none",
    # #467 step 1: everything that carries a password (sign-in, first-run setup, my password,
    # creating a user) logs no body values at all.
    "/api/auth/login": "none",
    "/api/auth/setup": "none",
    "/api/account/password": "none",
    "/api/users": "none",
}

# The same rules for parametrised paths (an admin's password reset for user N).
AUDIT_ROUTE_PATTERNS = (
    (re.compile(r"^/api/users/[^/]+/password$"), "none"),
)


def audit_route_rule(path):
    """The redaction rule for a request path: exact match first, then the patterns; None = the
    field-name backstop only."""
    rule = AUDIT_ROUTE_RULES.get(path)
    if rule:
        return rule
    for pattern, pat_rule in AUDIT_ROUTE_PATTERNS:
        if pattern.match(path or ""):
            return pat_rule
    return None

# Routes where a field that merely *looks* secret by name is known to be plain data.
# provenance-options send `key` = an option slug.
AUDIT_PLAIN_FIELDS = (
    ("/api/provenance-options/", {"key"}),
)

_SECRET_KEYWORDS = ("key", "secret", "token", "password", "api", "auth")


def redact_audit_error(path, reason):
    """#583: the same redaction for the reason text of a refused request. A route whose body is
    logged as redacted ("none" / "name_only", #559) may echo what was sent in its message, so only
    the error code (the part before ": ") is kept; elsewhere the text is stored as is."""
    if audit_route_rule(path) in ("none", "name_only"):
        code, sep, _ = reason.partition(": ")
        return code if sep else REDACTED
    return reason


def redact_audit_body(path, form_data):
    """Return a copy of form_data that is safe to write to audit_log."""
    if not form_data:
        return {}
    rule = audit_route_rule(path)
    if rule == "none":
        return {"_body": "not logged: this route carries secrets or file bodies"}
    if rule == "name_only":
        out = {}
        for k, v in form_data.items():
            if k == "key" and isinstance(v, str):
                out[k] = v  # the setting's name (a slug like youtube_data_api_key), not a secret
            else:
                out[k] = REDACTED
        return out
    plain = set()
    for prefix, fields in AUDIT_PLAIN_FIELDS:
        if path.startswith(prefix):
            plain |= fields
    out = {}
    for k, v in form_data.items():
        if k not in plain and any(w in k.lower() for w in _SECRET_KEYWORDS):
            out[k] = REDACTED
        elif hasattr(v, "filename"):
            out[k] = f"<file: {v.filename}>"
        else:
            out[k] = v
    return out
