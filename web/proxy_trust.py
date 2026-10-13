"""Trusted reverse proxies (#646): the request's real scheme and client address behind a proxy.

Behind a TLS-terminating reverse proxy (Caddy, nginx) the app sees the PROXY as its TCP peer and
plain `http`. The proxy says what it saw in `X-Forwarded-Proto` and `X-Forwarded-For`, but any
client can send those headers too, so they mean something only when the peer IS a proxy we trust.

The setting: `CONSTRUCTICON_TRUSTED_PROXIES`, a comma-separated list of IP addresses and/or CIDR
networks (`172.30.0.5`, `172.30.0.0/24`, `::1`). Read per request (cached per value), so tests can
set it. **Unset or empty = no trust at all: forwarded headers are ignored everywhere**, which is
exactly the behaviour before #646 and what the home install (plain HTTP, no proxy) runs with. A bad
entry is logged and skipped (fail closed: it never widens trust). There is no wildcard.

`ProxyHeadersMiddleware` is the ONE place forwarded headers are read. It is the outermost layer
(web/app.py), so every layer inside it (security headers, origin guard, body caps, session, access
gate, audit logger, CSRF, routes, Starlette's own URL building) sees the corrected ASGI scope:
  * when the TCP peer (`scope["client"]`) is in the trusted list:
      - `X-Forwarded-Proto` (its right-most value, `http` or `https`; anything else is ignored)
        becomes `scope["scheme"]` (`ws`/`wss` for a websocket);
      - `X-Forwarded-For`: walked from the RIGHT, skipping addresses that are themselves trusted
        proxies; the first one that isn't is the client (`scope["client"]`). The left-most entries
        are whatever the client sent and are never believed. If every entry is a trusted proxy, the
        left-most is used. An entry that isn't an IP address stops the walk (the client stays the
        last trusted hop: fail closed, never a made-up key).
  * otherwise nothing changes, whatever headers the request carries.

Deliberately NOT read: `X-Forwarded-Host` (the proxy must pass the original `Host` header: Caddy does
by default, nginx needs `proxy_set_header Host $host`), `X-Forwarded-Port`, and the RFC 7239
`Forwarded` header (neither Caddy nor nginx sends it by default; one format keeps the parsing small).

Everything else asks this module rather than the scope directly:
  * `client_ip(scope_or_request)`: the sign-in limiter's per-IP key (web/routes/auth.py), the audit
    log's `client_ip` column (web/middleware.py), the "install token refused" log line (web/auth.py).
  * `is_https(scope_or_request)`: the session cookie's `Secure` flag (web/auth.py), HSTS
    (web/security_headers.py), the same-origin guard's scheme (web/request_guard.py).

uvicorn has its own `--proxy-headers`, on by default but trusting only 127.0.0.1 (in-container
loopback). It runs before the app and can't be configured from here, so the compose example turns it
off (`--no-proxy-headers`) to keep this the only place; see CLAUDE.md "Behind a reverse proxy".
"""

import ipaddress
import logging
import os
import threading

log = logging.getLogger("constructicon.proxy_trust")

ENV = "CONSTRUCTICON_TRUSTED_PROXIES"
_SCHEMES = {"http": ("http", "ws"), "https": ("https", "wss")}

_cache_lock = threading.Lock()
_cache = {"raw": None, "nets": ()}


def _parse(raw):
    nets = []
    for part in (raw or "").split(","):
        part = part.strip()
        if not part:
            continue
        try:
            nets.append(ipaddress.ip_network(part, strict=False))
        except ValueError:
            log.warning("%s: ignoring %r (not an IP address or CIDR network)", ENV, part[:64])
    return tuple(nets)


def trusted_networks():
    """The trusted proxy networks from the environment (an empty tuple = trust nothing)."""
    raw = os.getenv(ENV, "")
    with _cache_lock:
        if _cache["raw"] != raw:
            _cache["raw"] = raw
            _cache["nets"] = _parse(raw)
            if _cache["nets"]:
                log.info("trusting forwarded headers from: %s", ", ".join(str(n) for n in _cache["nets"]))
        return _cache["nets"]


def _ip(value):
    """An ipaddress object for a forwarded/peer address ("1.2.3.4", "1.2.3.4:5678", "[::1]:80",
    "::ffff:1.2.3.4" -> its IPv4), or None when it isn't one."""
    if not isinstance(value, str):
        return None
    v = value.strip()
    if v.startswith("["):
        v = v[1:].split("]", 1)[0]
    elif v.count(":") == 1:  # IPv4 with a port
        v = v.split(":", 1)[0]
    try:
        addr = ipaddress.ip_address(v)
    except ValueError:  # silent-ok: not an address -> None, the caller treats it as untrusted
        return None
    if addr.version == 6 and addr.ipv4_mapped:
        addr = addr.ipv4_mapped
    return addr


def is_trusted(address, nets=None):
    """True when `address` (a string) is inside one of the trusted networks."""
    nets = trusted_networks() if nets is None else nets
    addr = _ip(address)
    return bool(addr is not None and nets and any(addr in n for n in nets))


def client_from_forwarded_for(values, nets):
    """The client address from X-Forwarded-For header value(s), right-most untrusted first.
    Returns the address string, or None when there is nothing usable."""
    entries = [e.strip() for v in values for e in v.split(",") if e.strip()]
    chosen = None
    for entry in reversed(entries):
        addr = _ip(entry)
        if addr is None:
            break  # garbage in the chain: stop, keep the last trusted hop
        chosen = str(addr)
        if not any(addr in n for n in nets):
            return chosen
    return chosen


def _scope(scope_or_request):
    return getattr(scope_or_request, "scope", scope_or_request)


def client_ip(scope_or_request):
    """The request's client address (after the trusted-proxy rewrite), or None."""
    client = _scope(scope_or_request).get("client")
    return client[0] if client else None


def is_https(scope_or_request):
    """True when the request reached the install over HTTPS (directly, or via a trusted proxy)."""
    return _scope(scope_or_request).get("scheme") in ("https", "wss")


class ProxyHeadersMiddleware:
    """Pure ASGI, outermost. Rewrites scope["scheme"] and scope["client"] from a trusted proxy's
    forwarded headers; leaves everything else (and every untrusted request) untouched."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] in ("http", "websocket"):
            nets = trusted_networks()
            peer = scope.get("client")
            if nets and peer and is_trusted(peer[0], nets):
                scope = self._rewrite(scope, nets)
        await self.app(scope, receive, send)

    @staticmethod
    def _rewrite(scope, nets):
        proto, xff = None, []
        for k, v in scope.get("headers", ()):
            if k == b"x-forwarded-proto":
                proto = v.decode("latin-1")  # the last header line wins, like its right-most value
            elif k == b"x-forwarded-for":
                xff.append(v.decode("latin-1"))
        scope = dict(scope)
        if proto is not None:
            last = proto.split(",")[-1].strip().lower()
            if last in _SCHEMES:
                scope["scheme"] = _SCHEMES[last][0 if scope["type"] == "http" else 1]
        if xff:
            client = client_from_forwarded_for(xff, nets)
            if client:
                scope["client"] = (client, 0)
        return scope
