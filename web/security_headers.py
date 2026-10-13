"""Browser security headers on every app response (#645).

One pure-ASGI middleware, the outermost layer (so a refusal from the origin guard, the body caps or
the sign-in gate carries the headers too), sets these defaults on each HTTP response:

- `X-Content-Type-Options: nosniff`: everywhere.
- `Referrer-Policy: strict-origin-when-cross-origin`: everywhere. A link out sends only our origin,
  never the path (`/object/<slug>`, `?next=`). Not `same-origin`: YouTube embeds now refuse to play
  with no Referer at all, and the object page embeds them.
- `X-Frame-Options: SAMEORIGIN` and the CSP's `frame-ancestors 'self'`: no other site can frame an
  app page (clickjacking). Same-origin framing stays: the admin drawer iframes `/admin?embed=1`, and
  the HTML Rendered view iframes `/api/image/<slug>/rendered`.
- `Content-Security-Policy`: `APP_CSP` below on app pages and API answers. See CLAUDE.md "Browser
  security headers" for what each source is for. Script keeps `'unsafe-inline'` for now (35 inline
  `<script>` blocks in 29 templates; moving to nonces is #660), but inline event handlers are
  off (`script-src-attr 'none'`), which is what an innerHTML injection would use.
- `Permissions-Policy` (camera, microphone, geolocation, payment, usb off) and
  `Cross-Origin-Opener-Policy: same-origin`.
- `Strict-Transport-Security`: ONLY when the request itself is HTTPS (`scope["scheme"]`) AND
  `CONSTRUCTICON_HSTS_SECONDS` is a positive number (default 0 = off). Never over plain HTTP: the
  home install is HTTP-only.

Who gets what (`_rule_for`):
- `/f/...` (user files, the public hotlink route): `content_security` (#610) owns these. Its nosniff
  and sandbox CSP stay as they are; this adds only Referrer-Policy (and HSTS). No X-Frame-Options or
  app CSP, so another site can still embed a hotlinked file.
- `/preview/...` (the static-export preview, built from export_templates for GitHub Pages): a minimal
  CSP (`PREVIEW_CSP`: no plugins, no <base>, forms to self, framed by self only) that blocks nothing
  the exported site uses. Its `media/` files keep #610's sandbox CSP.
- Everything else: all of the above.

A header the response already set always wins (the Rendered view's sandbox CSP and `no-referrer`,
#610's file headers). A route that needs a default left OFF entirely calls `opt_out(response,
"x-frame-options", ...)`; the marker header it sets is removed before the response leaves.

Env (read per request, so tests can set them; a bad value falls back to the default):
- `CONSTRUCTICON_HSTS_SECONDS`: HSTS max-age for HTTPS requests; 0/unset/bad = no HSTS.
- `CONSTRUCTICON_CSP`: replaces `APP_CSP` for an install that needs another source; `off` = no CSP
  on app pages (X-Frame-Options still stops framing).
- `CONSTRUCTICON_CSP_REPORT_ONLY=1`: send the app CSP as `Content-Security-Policy-Report-Only` (a
  diagnostic escape hatch if a page breaks; frame-ancestors is ignored in that mode, XFO still holds).
"""

import os

HSTS_ENV = "CONSTRUCTICON_HSTS_SECONDS"
CSP_ENV = "CONSTRUCTICON_CSP"
CSP_REPORT_ONLY_ENV = "CONSTRUCTICON_CSP_REPORT_ONLY"

# The policy for app pages and API answers. Each source is there because the UI uses it (inventory
# in the #645 PR): Google Fonts (base.html), YouTube embeds (object page, core/object_types/youtube.py),
# same-origin iframes (admin drawer, HTML Rendered view). No eval anywhere, no workers, no blob:/data:
# images, no external fetches. (The vendored fflate's ASYNC functions start blob: workers, which
# worker-src 'self' refuses; ingest.js uses only the synchronous Zip / ZipPassThrough.)
APP_CSP = "; ".join([
    "default-src 'self'",
    "script-src 'self' 'unsafe-inline'",
    "script-src-attr 'none'",
    "style-src 'self' 'unsafe-inline' https://fonts.googleapis.com",
    "font-src 'self' https://fonts.gstatic.com",
    "img-src 'self'",
    "media-src 'self'",
    "connect-src 'self'",
    "frame-src 'self' https://www.youtube.com",
    "worker-src 'self'",
    "object-src 'none'",
    "base-uri 'self'",
    "form-action 'self'",
    "frame-ancestors 'self'",
])

# /preview: the exported static site, shown in-app before it is published to GitHub Pages (which sets
# its own headers). Restricts only what the export never uses, plus framing.
PREVIEW_CSP = "object-src 'none'; base-uri 'self'; form-action 'self'; frame-ancestors 'self'"

PERMISSIONS_POLICY = "camera=(), microphone=(), geolocation=(), payment=(), usb=()"

# A route sets this (via opt_out) to leave named defaults off; never sent to the browser.
OPT_OUT_HEADER = "x-constructicon-skip-headers"


def opt_out(response, *header_names):
    """Leave these default headers off this response (names are case-insensitive)."""
    names = [n.strip().lower() for n in header_names if n and n.strip()]
    existing = response.headers.get(OPT_OUT_HEADER)
    if existing:
        names = [n.strip() for n in existing.split(",") if n.strip()] + names
    response.headers[OPT_OUT_HEADER] = ", ".join(names)
    return response


def hsts_seconds():
    """CONSTRUCTICON_HSTS_SECONDS as a positive int, else 0 (off)."""
    raw = (os.getenv(HSTS_ENV) or "").strip()
    try:
        value = int(raw)
    except ValueError:  # silent-ok: unset or not a number = HSTS off (the documented default)
        return 0
    return value if value > 0 else 0


def app_csp():
    """(header name, policy) for app pages, or None when CONSTRUCTICON_CSP=off."""
    policy = (os.getenv(CSP_ENV) or "").strip() or APP_CSP
    if policy.lower() == "off":
        return None
    report_only = (os.getenv(CSP_REPORT_ONLY_ENV) or "").strip().lower() in ("1", "true", "yes", "on")
    return ("content-security-policy-report-only" if report_only else "content-security-policy", policy)


def _rule_for(path):
    """'file' (/f/), 'preview' (/preview), or 'app'."""
    if path == "/f" or path.startswith("/f/"):
        return "file"
    if path == "/preview" or path.startswith("/preview/"):
        return "preview"
    return "app"


def defaults_for(scope):
    """The default headers (lowercase name -> value) for this request, before what the response set."""
    rule = _rule_for(scope.get("path") or "")
    headers = {"referrer-policy": "strict-origin-when-cross-origin"}
    if rule != "file":
        headers["x-content-type-options"] = "nosniff"
        headers["x-frame-options"] = "SAMEORIGIN"
        headers["permissions-policy"] = PERMISSIONS_POLICY
        headers["cross-origin-opener-policy"] = "same-origin"
        if rule == "preview":
            headers["content-security-policy"] = PREVIEW_CSP
        else:
            csp = app_csp()
            if csp:
                headers[csp[0]] = csp[1]
    # HSTS only over a real HTTPS request. #646 (proxy support) is what will make scope["scheme"]
    # say "https" behind a TLS-terminating proxy (uvicorn --proxy-headers + --forwarded-allow-ips);
    # do NOT read X-Forwarded-Proto here, an untrusted client could set it.
    seconds = hsts_seconds()
    if seconds and scope.get("scheme") == "https":
        headers["strict-transport-security"] = f"max-age={seconds}"
    return headers


class SecurityHeadersMiddleware:
    """Pure ASGI (no body buffering, so streaming uploads and file responses are untouched)."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        defaults = defaults_for(scope)

        async def send_with_headers(message):
            if message["type"] == "http.response.start":
                raw = list(message.get("headers") or [])
                present = set()
                skip = set()
                kept = []
                for name, value in raw:
                    lname = name.decode("latin-1").lower()
                    if lname == OPT_OUT_HEADER:
                        skip.update(n.strip().lower() for n in value.decode("latin-1").split(",") if n.strip())
                        continue
                    present.add(lname)
                    kept.append((name, value))
                # A response with its own CSP (#610 files, the Rendered view) keeps it whole: no
                # second policy (browsers would enforce both) and no report-only twin.
                has_own_csp = "content-security-policy" in present
                for name, value in defaults.items():
                    if name in present or name in skip:
                        continue
                    if has_own_csp and name.startswith("content-security-policy"):
                        continue
                    kept.append((name.encode("latin-1"), value.encode("latin-1")))
                message = {**message, "headers": kept}
            await send(message)

        await self.app(scope, receive, send_with_headers)
