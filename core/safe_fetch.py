"""Guarded outbound fetches (#724 part d): the one way the app reaches out to a web address it
was given (a pasted link, a link's page, a thumbnail URL).

A web link is untrusted input: anyone who can save one could aim the server at the LAN (the NAS's
own admin pages, the router, a container's localhost) or at a cloud metadata address. So every
fetch goes through `fetch()`, which:

- **allows http and https only**, on their standard ports only (80 / 443); a link that carries a
  username or password is refused;
- **resolves the host itself and refuses** any address that isn't public: private (RFC 1918,
  including the LAN's 10.0.0.0/8), loopback, link-local (169.254/16, fe80::/10), carrier-grade NAT
  (100.64/10), multicast, unspecified, reserved, IPv6 unique-local (fc00::/7) and site-local, and
  the IPv4 address inside an IPv4-mapped, 6to4 or NAT64 IPv6 address. If ANY address the name
  resolves to is refused, the fetch is refused (no "pick the public one" games);
- **pins the checked address for the connection**: the request goes to that IP, with the real
  host name in the Host header and as the TLS SNI name (so the certificate is still checked
  against the real name). The name is never resolved a second time, so DNS rebinding (public on
  the check, private on the connect) can't slip through;
- **never follows redirects by itself**: it follows them by hand, at most MAX_REDIRECTS, and every
  hop is checked again from the top (scheme, port, credentials, resolve, refuse, pin);
- **caps** the body at MAX_BYTES (counted as it streams; a bigger Content-Length is refused before
  reading), the whole fetch at TIMEOUT_SECONDS (DNS, connect and every read share one deadline),
  and accepts only the content types asked for (text/html and application/xhtml+xml by default);
- sends a plain, honest User-Agent naming Constructicon, and **never tries to get past a bot
  check**: a challenge or refusal page is reported as "site blocks fetching" and left there;
- takes **one fetch at a time** per container, with a short pause between fetches
  (`polite=True`, the default), so a backfill of many links to one site never bursts. The turn is
  held through a lock file, so it also holds across parse_limits child processes.

**Never fails silently:** every refusal or failure raises a `FetchError` whose message is the plain
reason ("refused: private network address (10.0.1.78)", "site blocks fetching (HTTP 403
challenge)", "timed out after 10 s", "not an HTML page (application/pdf)", "too large (over
2,000,000 bytes)"). `FetchError.transient` says whether trying again later could help (a timeout,
an unreachable site, HTTP 429 / 5xx), so a caller can tell "failed, try again" from "not
compatible".

Settings (read on every call): `CONSTRUCTICON_LINK_FETCH=off` turns page fetching off for the
install (an offline install, or the test suite: scripts/_testenv.py sets it), and
`CONSTRUCTICON_LINK_FETCH_DELAY_SECONDS` (default 2) is the pause between polite fetches.
"""

import ipaddress
import os
import socket
import tempfile
import threading
import time
from contextlib import contextmanager
from dataclasses import dataclass, field

import httpx

DEFAULT_PORTS = {"http": 80, "https": 443}
MAX_REDIRECTS = 5
MAX_BYTES = 2_000_000
TIMEOUT_SECONDS = 10.0
HTML_TYPES = ("text/html", "application/xhtml+xml")
DEFAULT_DELAY_SECONDS = 2.0
TURN_WAIT_SECONDS = 40.0  # how long a polite fetch waits for its turn before giving up (try again)
REDIRECT_STATUSES = (301, 302, 303, 307, 308)

_CGNAT = ipaddress.ip_network("100.64.0.0/10")
_NAT64 = ipaddress.ip_network("64:ff9b::/96")
_LOCK_PATH = os.path.join(tempfile.gettempdir(), "constructicon-link-fetch.lock")
_turn_lock = threading.Lock()
_last_fetch = [0.0]  # the in-process fallback when there's no fcntl (Windows)

# Test seams: scripts/test_safe_fetch_724d.py swaps these for fakes (no real DNS or network).
_getaddrinfo = socket.getaddrinfo
_transport = None  # an httpx transport; None = a real HTTPTransport


class FetchError(Exception):
    """A fetch that didn't give a usable body. The message is the plain reason (shown to people).
    `transient`: trying again later could help (time out, unreachable, HTTP 429/5xx)."""
    transient = False


class Refused(FetchError):
    """The guard refused the address: scheme, port, credentials, a non-public IP, redirects."""


class Blocked(FetchError):
    """The site refuses server fetches (HTTP 401/403, a bot-check challenge)."""


class WrongType(FetchError):
    """The answer isn't one of the content types asked for (not an HTML page)."""


class TooLarge(FetchError):
    """The body is over the size cap."""


class HttpStatus(FetchError):
    """Any other non-success answer (404, 410, 429, 5xx...). `broken`: the address answers but the page
    isn't there (404 / 410): a dead link, or a private page this server can't see. Not transient
    (retrying now won't help), but a real fault, so the page reader shows it red."""

    def __init__(self, message, status, transient=False, broken=False):
        super().__init__(message)
        self.status = status
        self.transient = transient
        self.broken = broken


class TimedOut(FetchError):
    transient = True


class Unreachable(FetchError):
    transient = True


@dataclass
class FetchResult:
    url: str             # the final address (after any redirects)
    status: int
    content_type: str    # the bare media type, lower case ("text/html")
    charset: str         # from the Content-Type header, "" when it names none
    body: bytes
    hops: list = field(default_factory=list)  # every address fetched, in order
    truncated: bool = False  # only with truncate=True: the body is the first max_bytes, the rest unread


def enabled():
    return os.environ.get("CONSTRUCTICON_LINK_FETCH", "on").strip().lower() not in ("off", "0", "false", "no")


def polite_delay():
    raw = os.environ.get("CONSTRUCTICON_LINK_FETCH_DELAY_SECONDS", "")
    try:
        value = float(raw)
    except ValueError:  # silent-ok: unset or unparsable falls back to the default, as every setting does
        return DEFAULT_DELAY_SECONDS
    return value if value >= 0 else DEFAULT_DELAY_SECONDS


def user_agent():
    try:
        from . import version
        ver = version.get_version()
    except Exception:  # silent-ok: the UA still names Constructicon without a version
        ver = "dev"
    return f"Constructicon/{ver} (self-hosted personal archive; reads a saved link's page title and summary)"


# --- the address check --------------------------------------------------------------------------

def _embedded_v4(ip):
    """The IPv4 address an IPv6 address carries (IPv4-mapped, 6to4, NAT64), or None."""
    if ip.ipv4_mapped is not None:
        return ip.ipv4_mapped
    if ip.sixtofour is not None:
        return ip.sixtofour
    if ip in _NAT64:
        return ipaddress.IPv4Address(int(ip) & 0xFFFFFFFF)
    return None


def address_refusal(ip):
    """Why `ip` must not be fetched ("private network address"), or None when it's public."""
    ip = ipaddress.ip_address(ip)
    if ip.version == 6:
        inner = _embedded_v4(ip)
        if inner is not None:
            why = address_refusal(inner)
            return f"{why} inside an IPv6 address" if why else None
        if ip.teredo is not None:
            return "tunnelled (Teredo) address"
        if ip.is_site_local:
            return "site-local address"
    if ip.is_unspecified:
        return "unspecified address"
    if ip.is_loopback:
        return "loopback address"
    if ip.is_link_local:
        return "link-local address"
    if ip.is_multicast:
        return "multicast address"
    if ip.version == 4 and ip in _CGNAT:
        return "carrier-grade NAT address"
    if ip.is_reserved:
        return "reserved address"
    if ip.is_private:
        return "private network address"
    if not ip.is_global:
        return "non-public address"
    return None


def check_url(url):
    """The parsed httpx.URL when `url` may be fetched as far as scheme, port and credentials go,
    else Refused with the reason. (The address check needs DNS: see fetch.)"""
    try:
        u = httpx.URL(str(url).strip())
    except Exception:  # silent-ok: reported as the refusal reason
        raise Refused("refused: not a valid web address") from None
    scheme = (u.scheme or "").lower()
    if scheme not in DEFAULT_PORTS:
        raise Refused(f"refused: only http and https links are fetched (not {scheme + ':' if scheme else 'a link with no scheme'})")
    if u.userinfo:
        raise Refused("refused: the link carries a username or password")
    if not u.raw_host:
        raise Refused("refused: the link has no host")
    if u.port is not None and u.port != DEFAULT_PORTS[scheme]:
        raise Refused(f"refused: port {u.port} (only the standard web ports, 80 and 443, are fetched)")
    return u


def _resolve(host, port, timeout):
    """Every address `host` resolves to (an IP literal is its own), within `timeout` seconds."""
    try:
        return [ipaddress.ip_address(host)]
    except ValueError:  # silent-ok: not an IP literal, so it's a name to look up
        pass
    out = {}

    def work():
        try:
            out["infos"] = _getaddrinfo(host, port, type=socket.SOCK_STREAM)
        except Exception as e:  # silent-ok: handed back to the caller, which reports it as unreachable
            out["error"] = e

    t = threading.Thread(target=work, name="safe-fetch-dns", daemon=True)
    t.start()
    t.join(max(timeout, 0.01))
    if t.is_alive():
        raise TimedOut(f"timed out looking up {host}")
    if "error" in out:
        raise Unreachable(f"couldn't find the site ({host}: no such address)")
    ips = []
    for info in out.get("infos") or ():
        try:
            ip = ipaddress.ip_address(str(info[4][0]).split("%")[0])
        except (ValueError, IndexError, TypeError):  # silent-ok: an unparsable entry is refused below as "no address"
            continue
        if ip not in ips:
            ips.append(ip)
    if not ips:
        raise Unreachable(f"couldn't find the site ({host}: no address)")
    return ips


# --- one fetch at a time --------------------------------------------------------------------------

@contextmanager
def _polite_turn(delay):
    """Hold the one fetch turn (per container: a lock file, so parse_limits children share it) and
    keep at least `delay` seconds since the previous fetch finished. Gives up after
    TURN_WAIT_SECONDS with a transient error rather than queueing forever."""
    if delay <= 0:
        yield
        return
    try:
        import fcntl
    except ImportError:  # silent-ok: Windows has no fcntl; an in-process lock is the whole app there
        fcntl = None
    give_up = time.monotonic() + TURN_WAIT_SECONDS
    if not _turn_lock.acquire(timeout=TURN_WAIT_SECONDS):
        raise TimedOut("another page fetch is still running; try again")
    try:
        if fcntl is None:
            time.sleep(max(0.0, _last_fetch[0] + delay - time.time()))
            try:
                yield
            finally:
                _last_fetch[0] = time.time()
            return
        with open(_LOCK_PATH, "a+") as fh:
            while True:
                try:
                    fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except OSError:
                    if time.monotonic() > give_up:
                        raise TimedOut("another page fetch is still running; try again") from None
                    time.sleep(0.2)
            try:
                fh.seek(0)
                try:
                    last = float(fh.read().strip() or 0)
                except ValueError:  # silent-ok: a garbled stamp just means no pause owed
                    last = 0.0
                time.sleep(min(delay, max(0.0, last + delay - time.time())))
                try:
                    yield
                finally:
                    fh.seek(0)
                    fh.truncate()
                    fh.write(f"{time.time():.3f}")
                    fh.flush()
            finally:
                fcntl.flock(fh, fcntl.LOCK_UN)
    finally:
        _turn_lock.release()


# --- the fetch ------------------------------------------------------------------------------------

def _type_ok(mime, accept):
    return any(mime.startswith(a) if a.endswith("/") else mime == a for a in accept)


def _media_type(header):
    parts = [p.strip() for p in (header or "").split(";")]
    mime = parts[0].lower()
    charset = ""
    for p in parts[1:]:
        k, _, v = p.partition("=")
        if k.strip().lower() == "charset":
            charset = v.strip().strip("\"'")
    return mime, charset


def _is_challenge(resp):
    """A bot-check page (Cloudflare's "Just a moment...", which every cults3d.com link answers
    with): recognised from its headers only, never solved."""
    h = resp.headers
    return (h.get("cf-mitigated", "").lower() == "challenge" or "cloudflare" in h.get("server", "").lower()
            or "cf-ray" in h)


def _status_error(resp):
    code = resp.status_code
    if code in (401, 403):
        return Blocked(f"site blocks fetching (HTTP {code} challenge)" if _is_challenge(resp)
                       else f"site blocks fetching (HTTP {code})")
    if code == 503 and _is_challenge(resp):
        return Blocked(f"site blocks fetching (HTTP {code} challenge)")
    if code == 429:
        return HttpStatus("the site asked us to slow down (HTTP 429); try again later", code, transient=True)
    if code in (404, 410):
        return HttpStatus(f"page not found (HTTP {code}): dead link, or a private page this server can't see",
                          code, broken=True)
    if code >= 500:
        return HttpStatus(f"the site had an error (HTTP {code}); try again later", code, transient=True)
    return HttpStatus(f"the site answered HTTP {code}", code)


def _remaining(deadline, timeout):
    left = deadline - time.monotonic()
    if left <= 0:
        raise TimedOut(f"timed out after {timeout:g} s")
    return left


def _one_hop(u, deadline, timeout, accept, max_bytes, headers, truncate=False):
    """Fetch `u` once (no redirects): (redirect target URL or None, FetchResult or None)."""
    host = u.raw_host.decode("ascii")
    scheme = u.scheme.lower()
    ips = _resolve(host, DEFAULT_PORTS[scheme], _remaining(deadline, timeout))
    for ip in ips:
        why = address_refusal(ip)
        if why:
            raise Refused(f"refused: {why} ({ip})")
    pinned = u.copy_with(host=str(ips[0]), fragment=None)
    send_headers = {"User-Agent": user_agent(), "Accept": ", ".join(accept) + ", */*;q=0.1",
                    "Accept-Encoding": "identity"}
    send_headers.update(headers or {})
    send_headers["Host"] = host  # always the real name (the URL now holds the pinned IP)
    left = _remaining(deadline, timeout)
    transport = _transport if _transport is not None else httpx.HTTPTransport(retries=0)
    # A new client per hop: a pooled connection to the same IP under another name must never be reused.
    with httpx.Client(transport=transport, trust_env=False, follow_redirects=False, verify=True) as client:
        req = client.build_request("GET", pinned, headers=send_headers, timeout=httpx.Timeout(left),
                                   extensions={"sni_hostname": host})
        try:
            resp = client.send(req, stream=True, follow_redirects=False)
        except httpx.TimeoutException:
            raise TimedOut(f"timed out after {timeout:g} s") from None
        except httpx.HTTPError as e:
            raise Unreachable(f"couldn't reach {host} ({type(e).__name__})") from None
        try:
            if resp.status_code in REDIRECT_STATUSES:
                location = resp.headers.get("location", "").strip()
                if not location:
                    raise HttpStatus(f"the site answered a redirect (HTTP {resp.status_code}) with no address", resp.status_code)
                return u.join(location), None
            if not 200 <= resp.status_code < 300:
                raise _status_error(resp)
            mime, charset = _media_type(resp.headers.get("content-type"))
            if not _type_ok(mime, accept):
                what = "an HTML page" if tuple(accept) == HTML_TYPES else "the expected type (" + ", ".join(accept) + ")"
                raise WrongType(f"not {what}: {mime or 'no content type'}" if tuple(accept) != HTML_TYPES
                                else f"not {what} ({mime or 'no content type'})")
            declared = resp.headers.get("content-length", "")
            if not truncate and declared.isdigit() and int(declared) > max_bytes:
                raise TooLarge(f"too large ({int(declared):,} bytes, over the {max_bytes:,} byte cap)")
            body = bytearray()
            truncated = False
            try:
                for chunk in resp.iter_bytes():
                    body += chunk
                    if len(body) > max_bytes:
                        if not truncate:
                            raise TooLarge(f"too large (over the {max_bytes:,} byte cap)")
                        # #724 d owner decision: keep the first max_bytes and stop reading (the cap
                        # stays a hard read limit; a page's title and meta are in its <head>)
                        del body[max_bytes:]
                        truncated = True
                        break
                    _remaining(deadline, timeout)
            except httpx.TimeoutException:
                raise TimedOut(f"timed out after {timeout:g} s") from None
            except httpx.HTTPError as e:
                raise Unreachable(f"the connection to {host} broke ({type(e).__name__}); try again") from None
            return None, FetchResult(url=str(u), status=resp.status_code, content_type=mime, charset=charset,
                                     body=bytes(body), truncated=truncated)
        finally:
            resp.close()


def fetch(url, *, accept=HTML_TYPES, max_bytes=MAX_BYTES, timeout=TIMEOUT_SECONDS,
          max_redirects=MAX_REDIRECTS, headers=None, polite=True, truncate=False):
    """GET `url` through the guard (see the module docstring) and return a FetchResult, or raise a
    FetchError with the plain reason. `accept`: the media types allowed (an entry ending in "/" is
    a prefix, e.g. "image/"). `headers` adds to (never replaces Host on) the request headers.
    `polite`: take the one-fetch-at-a-time turn with a pause (the page reader; a thumbnail of a
    fixed image host passes False). `truncate`: a body over `max_bytes` isn't refused; the first
    `max_bytes` are returned (`FetchResult.truncated`) and the rest is never read (the page reader:
    the title and meta live in <head>). Without it, an over-cap body raises TooLarge."""
    u = check_url(url)
    try:  # an IP literal is refused at once, without waiting for the polite turn
        literal = ipaddress.ip_address(u.raw_host.decode("ascii"))
    except ValueError:  # silent-ok: a host name; it is resolved and checked inside the turn
        literal = None
    if literal is not None and address_refusal(literal):
        raise Refused(f"refused: {address_refusal(literal)} ({literal})")
    with _polite_turn(polite_delay() if polite else 0):
        deadline = time.monotonic() + timeout
        hops = []
        for _ in range(max_redirects + 1):
            hops.append(str(u))
            target, result = _one_hop(u, deadline, timeout, tuple(accept), max_bytes, headers, truncate)
            if result is not None:
                result.hops = hops
                return result
            u = check_url(target)  # every hop is checked again from the top
        raise Refused(f"refused: more than {max_redirects} redirects")
