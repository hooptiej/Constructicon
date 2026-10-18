#!/usr/bin/env python3
"""#724 part d: a web link's page title and summary, through the guarded fetcher core/safe_fetch.py.

Throwaway DB + storage (scripts/_testenv.py). NO real DNS and NO real network: the resolver
(`safe_fetch._getaddrinfo`) and the HTTP transport (`safe_fetch._transport`, an httpx.MockTransport)
are fakes, and the parser sandbox is off so the fakes reach the reader:

    python scripts/test_safe_fetch_724d.py

Covers:
  * the guard: every refused address range (RFC 1918 incl. the LAN's 10/8, loopback, link-local,
    CGNAT, multicast, unspecified, reserved, IPv6 ULA / site-local / link-local, the IPv4 inside an
    IPv4-mapped / 6to4 / NAT64 address), public addresses allowed; a name resolving to ANY refused
    address is refused; an IP literal needs no DNS
  * non-http schemes, odd ports, credentials in the link
  * redirects followed by hand: relative ones work, one to a private address / to ftp: / past the
    limit is refused, every hop re-resolved and re-checked
  * DNS rebinding: the checked IP is pinned (the request goes to it, the real name in Host and SNI,
    the name resolved once per hop), a rebind on the next hop is refused
  * caps: declared and streamed size, the whole-fetch deadline (a slow stream, a slow DNS), a
    timeout and a connection error are transient; content type (text/html and xhtml only)
  * HTTP answers: a Cloudflare-style 403 / 503 challenge and a plain 403 -> "site blocks fetching",
    404 permanent, 429 / 5xx transient; an honest User-Agent, identity encoding
  * one fetch at a time with a pause between (the polite turn)
  * title / meta extraction (url.page_summary): title, description, og:title, og:description,
    deduplicated, entities, whitespace, caps, a <title> inside an inline <svg>, charsets, and
    malformed HTML (unclosed title, broken tags, garbage bytes)
  * the url type end to end (ocr.run_ocr): done with "Page title:" / "Page summary:" text and
    text_source 'page' (and a search hit); blocked / refused / not HTML / no title -> grey
    'unsupported' with the reason; a timeout -> 'failed' "try OCR again", and Retry then reads it;
    fetching turned off -> grey; no address -> grey
  * thumbnails._from_fetch (YouTube thumbnails) goes through the same guard
  * the link_page_text_724d backfill: re-queues failed / unsupported web links once (no network),
    leaves done / redacted / address-less / other-type rows, idempotent, registered and recorded

Exits 1 if any check fails.
"""

import io
import os
import sqlite3
import sys
import threading
import time

import _testenv  # noqa: E402  (scripts/_testenv.py: temp DB + storage + exports, refuses otherwise)
TMP = _testenv.isolate("safe-fetch-724d-")
os.environ["CONSTRUCTICON_PARSE_SANDBOX"] = "off"  # the fakes below must reach the reader
os.environ["CONSTRUCTICON_LINK_FETCH_DELAY_SECONDS"] = "0"
ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")
sys.path.insert(0, ROOT)

try:
    import cairosvg  # noqa: F401
except Exception:  # no libcairo here (e.g. Windows): object types import it
    import types
    sys.modules["cairosvg"] = types.ModuleType("cairosvg")

import httpx  # noqa: E402
from PIL import Image  # noqa: E402

from core import actor, db, ingest, object_types, ocr, safe_fetch, storage, thumbnails  # noqa: E402
from core.object_types import url as url_type  # noqa: E402
_testenv.assert_isolated()

FAILS = []


def check(name, cond, extra=""):
    print(("PASS " if cond else "FAIL ") + name + (f"  [{extra}]" if (extra and not cond) else ""))
    if not cond:
        FAILS.append(name)


# --- the fake network ---------------------------------------------------------------------------

class FakeNet:
    """DNS: host -> [ip, ...] or a callable(host) -> [ip, ...]. Routes: (host, path) -> handler(request)
    returning an httpx.Response. Every DNS lookup and request is logged."""

    def __init__(self):
        self.dns = {}
        self.routes = {}
        self.lookups = []
        self.requests = []

    def getaddrinfo(self, host, port, type=0, **_kw):
        self.lookups.append(host)
        entry = self.dns.get(host)
        if entry is None:
            raise OSError(f"[Errno -2] Name or service not known: {host}")
        ips = entry(host) if callable(entry) else entry
        return [(2 if ":" not in ip else 10, 1, 6, "", (ip, port)) for ip in ips]

    def handler(self, request):
        self.requests.append(request)
        key = (request.headers.get("host"), request.url.path)
        fn = self.routes.get(key)
        if fn is None:
            return httpx.Response(404, headers={"content-type": "text/html"}, content=b"<title>nope</title>")
        return fn(request)


NET = FakeNet()
safe_fetch._getaddrinfo = NET.getaddrinfo
safe_fetch._transport = httpx.MockTransport(NET.handler)


def html(body, status=200, ctype="text/html; charset=utf-8", headers=None):
    h = {"content-type": ctype}
    h.update(headers or {})
    return lambda req: httpx.Response(status, headers=h, content=body if isinstance(body, bytes) else body.encode())


def redirect(location, status=302):
    return lambda req: httpx.Response(status, headers={"location": location})


def reset_net():
    NET.dns.clear()
    NET.routes.clear()
    NET.lookups.clear()
    NET.requests.clear()


def fetch_error(url, **kw):
    try:
        safe_fetch.fetch(url, **kw)
    except safe_fetch.FetchError as e:
        return e
    return None


PUBLIC = "93.184.216.34"

# --- 1. the address check -----------------------------------------------------------------------

REFUSED = {
    "10.0.1.78": "private", "10.255.255.255": "private", "172.16.0.1": "private", "172.31.255.1": "private",
    "192.168.1.1": "private", "127.0.0.1": "loopback", "127.8.8.8": "loopback", "169.254.169.254": "link-local",
    "100.64.0.1": "carrier-grade NAT", "100.127.255.254": "carrier-grade NAT", "224.0.0.1": "multicast",
    "239.255.255.250": "multicast", "0.0.0.0": "unspecified", "0.1.2.3": "private", "240.0.0.1": "reserved",
    "255.255.255.255": "", "198.18.0.1": "", "192.0.2.1": "", "::1": "loopback", "::": "unspecified",
    "fe80::1": "link-local", "fc00::1": "private", "fd12:3456::1": "private", "fec0::1": "site-local",
    "ff02::1": "multicast", "::ffff:10.0.0.1": "private network address inside an IPv6",
    "::ffff:127.0.0.1": "loopback address inside an IPv6", "::ffff:169.254.169.254": "link-local",
    "2002:a00:1::": "inside an IPv6", "64:ff9b::7f00:1": "loopback address inside an IPv6",
    "2001:db8::1": "", "2001:0:4136:e378:8000:63bf:3fff:fdd2": "",
}
for ip, word in REFUSED.items():
    why = safe_fetch.address_refusal(ip)
    check(f"address: {ip} refused" + (f" ({word})" if word else ""), bool(why) and word in why, why)
for ip in ("93.184.216.34", "140.82.112.3", "8.8.8.8", "1.1.1.1", "2606:4700:4700::1111", "::ffff:8.8.8.8",
           "2a00:1450:4001::200e"):
    check(f"address: public {ip} allowed", safe_fetch.address_refusal(ip) is None, safe_fetch.address_refusal(ip))

# --- 2. scheme, port, credentials ----------------------------------------------------------------

for bad, word in (("ftp://example.com/x", "only http and https"), ("file:///etc/passwd", "only http and https"),
                  ("gopher://example.com/", "only http and https"), ("javascript:alert(1)", "only http and https"),
                  ("data:text/html,<b>x</b>", "only http and https"), ("example.com/no-scheme", "only http"),
                  ("http://example.com:8080/", "port 8080"), ("https://example.com:22/", "port 22"),
                  ("http://example.com:443/", "port 443"), ("https://example.com:80/", "port 80"),
                  ("http://user:pw@example.com/", "username or password"),
                  ("http:///nohost", "")):
    e = fetch_error(bad)
    check(f"refused before any network: {bad}", isinstance(e, safe_fetch.Refused) and word in str(e) and not NET.lookups,
          f"{type(e).__name__}: {e}")
    reset_net()

reset_net()
NET.dns["example.com"] = [PUBLIC]
NET.routes[("example.com", "/")] = html("<title>Example Domain</title>")
r = safe_fetch.fetch("https://example.com:443/")
check("explicit default port 443 on https allowed", r.status == 200, r)
r = safe_fetch.fetch("http://example.com:80/")
check("explicit default port 80 on http allowed", r.status == 200, r)

# --- 3. DNS: refused ranges by name, mixed answers, IP literals, pinning --------------------------

for ip in ("10.0.1.78", "127.0.0.1", "169.254.169.254", "100.64.1.1", "::1", "fd00::5", "::ffff:192.168.0.1"):
    reset_net()
    NET.dns["evil.example"] = [ip]
    NET.routes[("evil.example", "/")] = html("<title>should never be read</title>")
    e = fetch_error("http://evil.example/")
    check(f"a name resolving to {ip} is refused, nothing requested",
          isinstance(e, safe_fetch.Refused) and "refused:" in str(e) and not NET.requests, f"{e!r} {NET.requests}")

reset_net()
NET.dns["mixed.example"] = [PUBLIC, "10.0.0.7"]
NET.routes[("mixed.example", "/")] = html("<title>x</title>")
e = fetch_error("http://mixed.example/")
check("a name with one public and one private address is refused (no picking)",
      isinstance(e, safe_fetch.Refused) and "10.0.0.7" in str(e) and not NET.requests, e)

reset_net()
e = fetch_error("http://10.0.1.78/")
check("http://10.0.1.78 refused without DNS", isinstance(e, safe_fetch.Refused) and "private" in str(e)
      and not NET.lookups and not NET.requests, e)
e = fetch_error("http://127.0.0.1/")
check("http://127.0.0.1 refused", isinstance(e, safe_fetch.Refused) and "loopback" in str(e), e)
e = fetch_error("http://[::1]/")
check("http://[::1] refused", isinstance(e, safe_fetch.Refused) and "loopback" in str(e), e)
e = fetch_error("http://169.254.169.254/latest/meta-data/")
check("the cloud metadata address refused", isinstance(e, safe_fetch.Refused) and "link-local" in str(e), e)
NET.dns["localhost"] = ["127.0.0.1"]
e = fetch_error("http://localhost/")
check("http://localhost refused (resolves to loopback)", isinstance(e, safe_fetch.Refused), e)
NET.dns["2130706433"] = ["127.0.0.1"]
e = fetch_error("http://2130706433/")
check("a decimal-IP name that resolves to loopback refused", isinstance(e, safe_fetch.Refused), e)

reset_net()
e = fetch_error("http://no-such-host.example/")
check("an unknown name -> unreachable, transient", isinstance(e, safe_fetch.Unreachable) and e.transient
      and "couldn't find the site" in str(e), e)

# pinning + rebinding: the first lookup says public, every later one says loopback
reset_net()
answers = iter([[PUBLIC], ["127.0.0.1"], ["127.0.0.1"]])
NET.dns["rebind.example"] = lambda host: next(answers)
NET.routes[("rebind.example", "/")] = html("<title>Rebind page</title>")
r = safe_fetch.fetch("http://rebind.example/")
req = NET.requests[0]
check("rebinding: the request went to the checked IP, not a fresh lookup", req.url.host == PUBLIC, req.url)
check("rebinding: the real name in the Host header", req.headers["host"] == "rebind.example", dict(req.headers))
check("rebinding: the real name as TLS SNI", req.extensions.get("sni_hostname") == "rebind.example", req.extensions)
check("rebinding: the name was resolved exactly once", NET.lookups == ["rebind.example"], NET.lookups)
check("rebinding: the page read fine", b"Rebind page" in r.body, r.body)
reset_net()
answers2 = iter([[PUBLIC], ["10.0.1.78"]])
NET.dns["rebind2.example"] = lambda host: next(answers2)
NET.routes[("rebind2.example", "/a")] = redirect("/b")
NET.routes[("rebind2.example", "/b")] = html("<title>internal</title>")
e = fetch_error("http://rebind2.example/a")
check("rebinding on the redirect hop: re-resolved and refused", isinstance(e, safe_fetch.Refused) and "10.0.1.78" in str(e)
      and len(NET.requests) == 1, f"{e!r} {[str(x.url) for x in NET.requests]}")

# https: pinned IP keeps the https scheme (TLS to the IP, cert checked against the SNI name)
reset_net()
NET.dns["secure.example"] = ["2606:4700:4700::1111"]
NET.routes[("secure.example", "/p")] = html("<title>v6</title>")
r = safe_fetch.fetch("https://secure.example/p?q=1#frag")
req = NET.requests[0]
check("https + IPv6: pinned URL is https://[ip]/p?q=1 (no fragment)", str(req.url) == "https://[2606:4700:4700::1111]/p?q=1",
      str(req.url))
check("an honest User-Agent naming Constructicon", req.headers["user-agent"].startswith("Constructicon/")
      and "Mozilla" not in req.headers["user-agent"], req.headers["user-agent"])
check("identity encoding asked for (the cap counts real bytes)", req.headers["accept-encoding"] == "identity")
check("the final URL is reported", r.url == "https://secure.example/p?q=1#frag" or r.url.startswith("https://secure.example/p"), r.url)

# --- 4. redirects --------------------------------------------------------------------------------

reset_net()
NET.dns["a.example"] = [PUBLIC]
NET.dns["b.example"] = ["140.82.112.3"]
NET.routes[("a.example", "/start")] = redirect("https://b.example/landing", 301)
NET.routes[("b.example", "/landing")] = redirect("/final", 308)
NET.routes[("b.example", "/final")] = html("<title>Landed</title>")
r = safe_fetch.fetch("http://a.example/start")
check("redirects followed by hand (absolute then relative)", b"Landed" in r.body and len(r.hops) == 3, r.hops)
check("each hop resolved its own host", NET.lookups == ["a.example", "b.example", "b.example"], NET.lookups)

reset_net()
NET.dns["a.example"] = [PUBLIC]
NET.routes[("a.example", "/")] = redirect("http://10.0.1.78/admin")
e = fetch_error("http://a.example/")
check("a redirect to a private IP is refused", isinstance(e, safe_fetch.Refused) and "private" in str(e)
      and len(NET.requests) == 1, e)
reset_net()
NET.dns["a.example"] = [PUBLIC]
NET.dns["internal.example"] = ["192.168.1.1"]
NET.routes[("a.example", "/")] = redirect("http://internal.example/")
e = fetch_error("http://a.example/")
check("a redirect to a name that resolves privately is refused", isinstance(e, safe_fetch.Refused), e)
reset_net()
NET.dns["a.example"] = [PUBLIC]
NET.routes[("a.example", "/")] = redirect("http://127.0.0.1:8080/")
e = fetch_error("http://a.example/")
check("a redirect to an odd port is refused", isinstance(e, safe_fetch.Refused) and "port 8080" in str(e), e)
NET.routes[("a.example", "/")] = redirect("ftp://a.example/file")
e = fetch_error("http://a.example/")
check("a redirect to ftp: is refused", isinstance(e, safe_fetch.Refused) and "only http" in str(e), e)
NET.routes[("a.example", "/")] = redirect("file:///etc/passwd")
e = fetch_error("http://a.example/")
check("a redirect to file: is refused", isinstance(e, safe_fetch.Refused), e)
reset_net()
NET.dns["loop.example"] = [PUBLIC]
for i in range(10):
    NET.routes[("loop.example", f"/{i}")] = redirect(f"/{i + 1}")
e = fetch_error("http://loop.example/0")
check("more than 5 redirects refused", isinstance(e, safe_fetch.Refused) and "more than 5 redirects" in str(e)
      and len(NET.requests) == 6, f"{e!r} {len(NET.requests)}")
reset_net()
NET.dns["a.example"] = [PUBLIC]
NET.routes[("a.example", "/")] = lambda req: httpx.Response(302)
e = fetch_error("http://a.example/")
check("a redirect with no Location is reported", isinstance(e, safe_fetch.HttpStatus) and "no address" in str(e), e)

# --- 5. caps, timeouts, content type, statuses -----------------------------------------------------

reset_net()
NET.dns["big.example"] = [PUBLIC]
NET.routes[("big.example", "/declared")] = html(b"x" * (safe_fetch.MAX_BYTES + 1))
e = fetch_error("http://big.example/declared")
check("a declared Content-Length over 2 MB refused (too large)", isinstance(e, safe_fetch.TooLarge) and "too large" in str(e), e)


class Chunks(httpx.SyncByteStream):
    def __init__(self, chunks, pause=0.0):
        self.chunks, self.pause = chunks, pause

    def __iter__(self):
        for c in self.chunks:
            if self.pause:
                time.sleep(self.pause)
            yield c


NET.routes[("big.example", "/stream")] = lambda req: httpx.Response(
    200, headers={"content-type": "text/html"}, stream=Chunks([b"y" * 4096] * 40))
e = fetch_error("http://big.example/stream", max_bytes=50_000)
check("a streamed body past the cap is cut off (no Content-Length)", isinstance(e, safe_fetch.TooLarge), e)
r = safe_fetch.fetch("http://big.example/stream", max_bytes=200_000)
check("a streamed body under the cap is read whole", len(r.body) == 4096 * 40, len(r.body))

NET.routes[("big.example", "/slow")] = lambda req: httpx.Response(
    200, headers={"content-type": "text/html"}, stream=Chunks([b"<p>z</p>"] * 20, pause=0.1))
t0 = time.monotonic()
e = fetch_error("http://big.example/slow", timeout=0.5)
check("the whole-fetch deadline stops a slow drip", isinstance(e, safe_fetch.TimedOut) and e.transient
      and time.monotonic() - t0 < 1.5, f"{e!r} {time.monotonic() - t0:.2f}s")


def raises(exc_cls):
    def fn(req):
        raise exc_cls("simulated", request=req)
    return fn


NET.routes[("big.example", "/timeout")] = raises(httpx.ReadTimeout)
e = fetch_error("http://big.example/timeout")
check("a read timeout -> timed out, transient", isinstance(e, safe_fetch.TimedOut) and e.transient and "timed out" in str(e), e)
NET.routes[("big.example", "/refused")] = raises(httpx.ConnectError)
e = fetch_error("http://big.example/refused")
check("a connection error -> unreachable, transient", isinstance(e, safe_fetch.Unreachable) and e.transient, e)

slow_dns_host = "slowdns.example"
NET.dns[slow_dns_host] = lambda host: (time.sleep(1.0), [PUBLIC])[1]
t0 = time.monotonic()
e = fetch_error(f"http://{slow_dns_host}/", timeout=0.3)
check("a slow DNS lookup is bounded by the deadline", isinstance(e, safe_fetch.TimedOut) and time.monotonic() - t0 < 0.9,
      f"{e!r} {time.monotonic() - t0:.2f}s")

reset_net()
NET.dns["t.example"] = [PUBLIC]
NET.routes[("t.example", "/pdf")] = html(b"%PDF-1.4", ctype="application/pdf")
e = fetch_error("http://t.example/pdf")
check("not an HTML page (application/pdf) refused", isinstance(e, safe_fetch.WrongType)
      and str(e) == "not an HTML page (application/pdf)", e)
NET.routes[("t.example", "/none")] = lambda req: httpx.Response(200, content=b"<title>x</title>")
e = fetch_error("http://t.example/none")
check("no content type at all refused", isinstance(e, safe_fetch.WrongType) and "no content type" in str(e), e)
NET.routes[("t.example", "/json")] = html(b"{}", ctype="application/json")
check("application/json refused", isinstance(fetch_error("http://t.example/json"), safe_fetch.WrongType))
NET.routes[("t.example", "/xhtml")] = html(b"<title>x</title>", ctype="application/xhtml+xml")
check("application/xhtml+xml accepted", safe_fetch.fetch("http://t.example/xhtml").content_type == "application/xhtml+xml")
NET.routes[("t.example", "/latin")] = html("<title>Café</title>".encode("latin-1"), ctype="TEXT/HTML; Charset=ISO-8859-1")
r = safe_fetch.fetch("http://t.example/latin")
check("content type matched case-blind, charset read", r.content_type == "text/html" and r.charset == "ISO-8859-1", (r.content_type, r.charset))

CF = {"server": "cloudflare", "cf-ray": "8abc123-DEN", "cf-mitigated": "challenge"}
NET.routes[("t.example", "/cf403")] = html("<title>Just a moment...</title>", 403, headers=CF)
e = fetch_error("http://t.example/cf403")
check("a Cloudflare 403 challenge -> site blocks fetching (HTTP 403 challenge), not transient",
      isinstance(e, safe_fetch.Blocked) and str(e) == "site blocks fetching (HTTP 403 challenge)" and not e.transient, e)
NET.routes[("t.example", "/cf503")] = html("<title>Just a moment...</title>", 503, headers=CF)
e = fetch_error("http://t.example/cf503")
check("a Cloudflare 503 challenge -> blocked, not a transient 5xx", isinstance(e, safe_fetch.Blocked) and not e.transient, e)
NET.routes[("t.example", "/403")] = html("<title>Forbidden</title>", 403)
e = fetch_error("http://t.example/403")
check("a plain 403 -> site blocks fetching (HTTP 403)", isinstance(e, safe_fetch.Blocked) and str(e) == "site blocks fetching (HTTP 403)", e)
NET.routes[("t.example", "/404")] = html("<title>Not found</title>", 404)
e = fetch_error("http://t.example/404")
check("404 -> page not found, not transient", isinstance(e, safe_fetch.HttpStatus) and not e.transient and "not found" in str(e), e)
NET.routes[("t.example", "/429")] = html("", 429)
e = fetch_error("http://t.example/429")
check("429 -> transient", isinstance(e, safe_fetch.HttpStatus) and e.transient, e)
NET.routes[("t.example", "/502")] = html("", 502)
e = fetch_error("http://t.example/502")
check("502 -> transient, 'try again later'", isinstance(e, safe_fetch.HttpStatus) and e.transient and "try again" in str(e), e)
check("exactly one request per blocked page (no retry, no bypass attempt)",
      sum(1 for q in NET.requests if q.url.path == "/cf403") == 1, len(NET.requests))

# --- 6. one fetch at a time, with a pause ------------------------------------------------------------

reset_net()
NET.dns["polite.example"] = [PUBLIC]
spans = []


def slowpage(req):
    start = time.monotonic()
    time.sleep(0.15)
    spans.append((start, time.monotonic()))
    return httpx.Response(200, headers={"content-type": "text/html"}, content=b"<title>p</title>")


NET.routes[("polite.example", "/")] = slowpage
os.environ["CONSTRUCTICON_LINK_FETCH_DELAY_SECONDS"] = "0.3"
threads = [threading.Thread(target=safe_fetch.fetch, args=("http://polite.example/",)) for _ in range(3)]
for t in threads:
    t.start()
for t in threads:
    t.join(20)
spans.sort()
gaps = [b[0] - a[1] for a, b in zip(spans, spans[1:])]
check("polite: three fetches, never overlapping, >= the pause apart", len(spans) == 3 and all(g >= 0.28 for g in gaps),
      f"{len(spans)} {gaps}")
check("polite: a thumbnail-style fetch (polite=False) skips the turn",
      safe_fetch.fetch("http://polite.example/", polite=False).status == 200)
os.environ["CONSTRUCTICON_LINK_FETCH_DELAY_SECONDS"] = "0"

# --- 7. title / meta extraction ------------------------------------------------------------------------

ps = url_type.page_summary
t, s = ps(b"<html><head><title> hooptiej (Jason K.) &amp; friends </title>"
          b'<meta name="description" content="Builds &amp; hacks.">'
          b'<meta property="og:title" content="hooptiej (Jason K.) &amp; friends">'
          b'<meta property="og:description" content="Builds &amp; hacks.">'
          b"</head><body><title>second</title></body></html>")
check("title + description, entities unescaped, og duplicates dropped",
      t == ["hooptiej (Jason K.) & friends"] and s == ["Builds & hacks."], (t, s))
t, s = ps(b'<title>Steam</title><meta property="og:title" content="Kerbal Space Program">'
          b'<meta name="Description" content="Rocket game"><meta property="og:description" content="Build rockets">')
check("different og:title and og:description are kept (name= case-blind)",
      t == ["Steam", "Kerbal Space Program"] and s == ["Rocket game", "Build rockets"], (t, s))
t, s = ps(b'<meta property="og:title" content="Only OG"><meta property="og:description" content="OG desc">')
check("og-only page", t == ["Only OG"] and s == ["OG desc"], (t, s))
t, s = ps(b"<title>GitHub - hooptiej/x: A thing</title><meta property='og:title' content='hooptiej/x'>")
check("an og:title contained in the title is dropped", t == ["GitHub - hooptiej/x: A thing"], t)
t, s = ps(b'<title>Same</title><meta name="description" content="same">')
check("a description equal to the title is dropped", t == ["Same"] and s == [], (t, s))
t, s = ps(b"<svg><title>icon</title></svg><title>Real title</title>")
check("a <title> inside an inline <svg> is skipped", t == ["Real title"], t)
t, s = ps(b"<title>\n  spread\n\t over   lines </title>")
check("whitespace collapsed", t == ["spread over lines"], t)
t, s = ps(b"<title>" + b"A" * 1000 + b"</title><meta name=description content='" + b"d" * 5000 + b"'>")
check("title and summary capped with an ellipsis", len(t[0]) == url_type.TITLE_MAX_CHARS and t[0].endswith("…")
      and len(s[0]) == url_type.SUMMARY_MAX_CHARS, (len(t[0]), len(s[0])))
t, s = ps(b"<title>never closed " + b"<p>word " * 20000)
check("malformed: an unclosed <title> is capped, not the whole page", t and len(t[0]) <= url_type.TITLE_MAX_CHARS, t and len(t[0]))
t, s = ps(b"<title>ok</title><meta name=description content=\"x<<>>\"/><<<!-- unclosed comment <b <i")
check("malformed: broken tags and an unclosed comment don't crash", t == ["ok"] and s == ["x<<>>"], (t, s))
t, s = ps(os.urandom(4096))
check("malformed: random bytes give no text and no crash", isinstance(t, list) and isinstance(s, list))
t, s = ps(b"")
check("empty page -> nothing", t == [] and s == [], (t, s))
t, s = ps('<meta charset="windows-1252"><title>Café – menu</title>'.encode("cp1252"))
check("charset from <meta charset>", t == ["Café – menu"], t)
t, s = ps("<title>Café</title>".encode("latin-1"), "ISO-8859-1")
check("charset from the header", t == ["Café"], t)
t, s = ps(b"<title>x</title>", "no-such-charset")
check("an unknown charset falls back to utf-8", t == ["x"], t)
t, s = ps(b"<TITLE>Upper</TITLE><META NAME=DESCRIPTION CONTENT='Shouty'>")
check("upper-case tags", t == ["Upper"] and s == ["Shouty"], (t, s))
t, s = ps(b"<title><script>alert(1)</script> &lt;b&gt;</title>")
check("markup in a title stays text (stored as plain text)", t and "alert(1)" in t[0] and "<b>" in t[0], t)

# --- 8. the url type end to end -------------------------------------------------------------------------

client = None
try:
    from web import app as webapp
    client = _testenv.client(webapp.app, raise_server_exceptions=False)
    with client:  # real startup (init_db + migrations)
        pass
except Exception as e:  # web stack not importable here: the box run covers it
    print(f"SKIP web startup (web import failed: {e!r}); init_db directly")
    db.init_db()

ingest.run_in_thread = lambda fn, *a: None  # nothing runs OCR behind the test's back
ctx = actor.acting_as(actor.ACTOR_UI)
ctx.__enter__()
os.environ["CONSTRUCTICON_LINK_FETCH"] = "on"


def mklink(slug, url, media_type="url"):
    db.insert_content(slug, "tester", media_type, external_url=url)
    return slug


def state(slug):
    r = db.get_by_slug(slug)
    meta = r.get("type_metadata") or {}
    return r["ocr_status"], meta.get("ocr_note"), r["extracted_text"], meta.get("text_source")


spec = object_types.get_object_type("url")
check("registry: url has the page reader, text_source 'page'", spec.text_extract_fn is url_type.page_text
      and spec.text_source == "page")
check("registry: a web link is no longer 'nothing to read'", not object_types.nothing_to_read({"media_type": "url"}, spec))

reset_net()
NET.dns["github.com"] = ["140.82.112.3"]
NET.routes[("github.com", "/hooptiej")] = html(
    '<title>hooptiej (Hooptastic) - GitHub</title><meta name="description" content="hooptiej has 12 repositories available.">'
    '<meta property="og:title" content="hooptiej - Overview"><meta property="og:description" content="hooptiej has 12 repositories available.">')
GH = mklink("gh_link", "https://github.com/hooptiej")
ocr.run_ocr(GH)
st, note, text, src = state(GH)
check("github-style page -> done", st == "done" and not note, (st, note))
check("text is the headed title + summary", text == ("Page title:\nhooptiej (Hooptastic) - GitHub\nhooptiej - Overview\n\n"
                                                     "Page summary:\nhooptiej has 12 repositories available."), text)
check("type_metadata.text_source = 'page'", src == "page", src)
hits = [h["slug"] for h in db.search("Hooptastic")]
check("a word only in the page title is a search hit", GH in hits, hits)

NET.dns["cults3d.com"] = ["104.18.10.10"]
NET.routes[("cults3d.com", "/en/3d-model/gadget/rover")] = html("<title>Just a moment...</title>", 403, headers=CF)
CU = mklink("cults_link", "https://cults3d.com/en/3d-model/gadget/rover")
ocr.run_ocr(CU)
st, note, text, src = state(CU)
check("cults3d (Cloudflare challenge) -> grey unsupported", st == "unsupported", st)
check("... reason 'web link: site blocks fetching (HTTP 403 challenge)'", note == "web link: site blocks fetching (HTTP 403 challenge)", note)
check("... no text, no text_source", not text and not src, (text, src))

LAN = mklink("lan_link", "http://10.0.1.78/ui/")
ocr.run_ocr(LAN)
st, note, *_ = state(LAN)
check("a LAN link -> grey, 'refused: private network address'", st == "unsupported" and note.startswith("web link: refused: private network address"), (st, note))
LO = mklink("lo_link", "http://127.0.0.1:8000/")
ocr.run_ocr(LO)
st, note, *_ = state(LO)
check("a localhost link with a port -> grey, refused", st == "unsupported" and "refused" in note, (st, note))

NET.dns["files.example"] = [PUBLIC]
NET.routes[("files.example", "/manual.pdf")] = html(b"%PDF", ctype="application/pdf")
PDF = mklink("pdf_link", "https://files.example/manual.pdf")
ocr.run_ocr(PDF)
st, note, *_ = state(PDF)
check("a link to a PDF -> grey 'not an HTML page'", st == "unsupported" and note == "web link: not an HTML page (application/pdf)", (st, note))

NET.routes[("files.example", "/bare")] = html("<html><body><p>No head at all</p></body></html>")
BARE = mklink("bare_link", "https://files.example/bare")
ocr.run_ocr(BARE)
st, note, *_ = state(BARE)
check("a page with no title or summary -> grey with that reason", st == "unsupported" and note == "web link: the page has no title or summary", (st, note))

NET.routes[("files.example", "/flaky")] = raises(httpx.ConnectTimeout)
FL = mklink("flaky_link", "https://files.example/flaky")
ocr.run_ocr(FL)
st, note, *_ = state(FL)
check("a timeout -> failed (worth retrying), never silent", st == "failed" and note and "timed out" in note and "try OCR again" in note, (st, note))
NET.routes[("files.example", "/flaky")] = html("<title>Back up now</title>")
db.set_ocr_status(FL, "pending")  # what Retry does (web and MCP)
ocr.run_ocr(FL)
st, note, text, src = state(FL)
check("Retry after the site recovers -> done, the old note gone", st == "done" and not note and "Back up now" in text and src == "page",
      (st, note, text))
NET.routes[("files.example", "/down")] = html("", 503)
DOWN = mklink("down_link", "https://files.example/down")
ocr.run_ocr(DOWN)
st, note, *_ = state(DOWN)
check("a 503 -> failed, 'try again later'", st == "failed" and "try again later" in note and "try OCR again" not in note, (st, note))

os.environ["CONSTRUCTICON_LINK_FETCH"] = "off"
before = len(NET.requests)
OFF = mklink("off_link", "https://github.com/hooptiej")
ocr.run_ocr(OFF)
st, note, *_ = state(OFF)
check("fetching turned off -> grey, no request made", st == "unsupported" and note == "web link: page fetching is turned off on this install"
      and len(NET.requests) == before, (st, note))
os.environ["CONSTRUCTICON_LINK_FETCH"] = "on"
NOURL = "nourl_link"
db.insert_content(NOURL, "tester", "url", external_url=None)
ocr.run_ocr(NOURL)
st, note, *_ = state(NOURL)
check("no address -> grey 'no page address to read'", st == "unsupported" and note == "web link: no page address to read", (st, note))

# --- 9. thumbnails go through the guard ------------------------------------------------------------------

buf = io.BytesIO()
Image.new("RGB", (32, 18), (90, 110, 40)).save(buf, "JPEG")
JPEG = buf.getvalue()
reset_net()
NET.dns["img.youtube.com"] = ["142.250.72.14"]
NET.dns["i.ytimg.com"] = ["142.250.72.15"]
NET.routes[("img.youtube.com", "/vi/jNQXAC9IVRw/hqdefault.jpg")] = redirect("https://i.ytimg.com/vi/jNQXAC9IVRw/hqdefault.jpg")
NET.routes[("i.ytimg.com", "/vi/jNQXAC9IVRw/hqdefault.jpg")] = lambda req: httpx.Response(
    200, headers={"content-type": "image/jpeg"}, content=JPEG)
yt_spec = object_types.get_object_type("youtube")
row = {"slug": "yt_thumb", "media_type": "youtube", "external_url": "https://www.youtube.com/watch?v=jNQXAC9IVRw"}
ok = thumbnails._from_fetch(row, yt_spec)
check("thumbnail: fetched through the guard (redirect followed, pinned)", ok and storage.thumb_path_for("yt_thumb").exists()
      and NET.requests[-1].url.host == "142.250.72.15", [str(r.url) for r in NET.requests])
check("thumbnail: polite=False, its own headers (image Accept)", "image/" in NET.requests[-1].headers["accept"], NET.requests[-1].headers)
NET.routes[("img.youtube.com", "/vi/abcdefghijk/hqdefault.jpg")] = redirect("http://169.254.169.254/latest/")
row2 = {"slug": "yt_thumb2", "media_type": "youtube", "external_url": "https://www.youtube.com/watch?v=abcdefghijk"}
try:
    thumbnails._from_fetch(row2, yt_spec)
    outcome = "no error"
except safe_fetch.Refused as e:
    outcome = str(e)
check("thumbnail: a redirect to the metadata address is refused", "link-local" in outcome, outcome)
check("thumbnail: ensure_thumbnail reports it as no thumbnail (logged, not raised)",
      thumbnails.ensure_thumbnail(row2) is False and not storage.thumb_path_for("yt_thumb2").exists())
NET.routes[("img.youtube.com", "/vi/htmlhtmlhtm/hqdefault.jpg")] = html("<title>not an image</title>")
row3 = {"slug": "yt_thumb3", "media_type": "youtube", "external_url": "https://www.youtube.com/watch?v=htmlhtmlhtm"}
try:
    thumbnails._from_fetch(row3, yt_spec)
    outcome = "no error"
except safe_fetch.WrongType as e:
    outcome = str(e)
check("thumbnail: an HTML answer where an image was expected is refused", outcome == "not the expected type (image/): text/html", outcome)

# --- 10. the backfill ------------------------------------------------------------------------------------


def q(sql, *args):
    c = sqlite3.connect(db.DB_PATH)
    c.row_factory = sqlite3.Row
    try:
        return [dict(r) for r in c.execute(sql, args)]
    finally:
        c.close()


def setrow(slug, status, note=None, redacted=0):
    c = sqlite3.connect(db.DB_PATH)
    meta = '{"keep": 1' + (f', "ocr_note": "{note}"' if note else "") + "}"
    c.execute("UPDATE capture_events SET ocr_status = ?, type_metadata = ?, redacted = ? WHERE slug = ?",
              (status, meta, redacted, slug))
    c.commit()
    c.close()


B_UNS = mklink("bf_unsupported", "https://cults3d.com/en/3d-model/a")
B_FAIL = mklink("bf_failed", "https://store.steampowered.com/app/220200/")
B_DONE = mklink("bf_done", "https://github.com/hooptiej/x")
B_RED = mklink("bf_redacted", "https://example.com/r")
B_YT = mklink("bf_youtube", "https://www.youtube.com/watch?v=jNQXAC9IVRw", media_type="youtube")
B_NOURL = "bf_nourl"
db.insert_content(B_NOURL, "tester", "url", external_url="  ")
setrow(B_UNS, "unsupported", "web link: no readable text yet")
setrow(B_FAIL, "failed")
setrow(B_DONE, "done")
setrow(B_RED, "unsupported", "web link: no readable text yet", redacted=1)
setrow(B_YT, "unsupported", "video: no description or captions yet")
setrow(B_NOURL, "unsupported", "web link: no readable text yet")
before_requests = len(NET.requests)
c = sqlite3.connect(db.DB_PATH)
c.execute("DELETE FROM schema_migrations WHERE name = 'link_page_text_724d'")
c.commit()
c.close()
ran = db.run_pending_migrations()
check("backfill: run by run_pending_migrations", ran == ["link_page_text_724d"], ran)
check("backfill: recorded in schema_migrations", "link_page_text_724d" in db.applied_migrations())
check("backfill: registered in MIGRATIONS, last", db.MIGRATIONS[-1][0] == "link_page_text_724d", db.MIGRATIONS[-1][0])
check("backfill: no network during the migration", len(NET.requests) == before_requests)
rows = {r["slug"]: r for r in q("SELECT slug, ocr_status, ocr_started_at, type_metadata FROM capture_events")}
check("backfill: the unsupported web link is pending, note gone, other keys kept",
      rows[B_UNS]["ocr_status"] == "pending" and rows[B_UNS]["type_metadata"] == '{"keep":1}' and rows[B_UNS]["ocr_started_at"],
      rows[B_UNS])
check("backfill: the failed web link is pending", rows[B_FAIL]["ocr_status"] == "pending", rows[B_FAIL])
check("backfill: a done web link is left alone", rows[B_DONE]["ocr_status"] == "done", rows[B_DONE])
check("backfill: a redacted web link is left alone", rows[B_RED]["ocr_status"] == "unsupported", rows[B_RED])
check("backfill: a YouTube row is left alone", rows[B_YT]["ocr_status"] == "unsupported", rows[B_YT])
check("backfill: a web link with no address is left alone", rows[B_NOURL]["ocr_status"] == "unsupported", rows[B_NOURL])
pending_before = sorted(r["slug"] for r in q("SELECT slug FROM capture_events WHERE ocr_status = 'pending'"))
check("backfill: the self-heal list holds exactly them", [r["slug"] for r in db.list_pending_ocr()] and
      {B_UNS, B_FAIL} <= {r["slug"] for r in db.list_pending_ocr()}, pending_before)
db._mig_link_page_text_724d()  # a second run: nothing more to re-queue except rows already pending
check("backfill: idempotent (a second run changes nothing)",
      sorted(r["slug"] for r in q("SELECT slug FROM capture_events WHERE ocr_status = 'pending'")) == pending_before)
reset_net()
NET.dns["cults3d.com"] = ["104.18.10.10"]
NET.routes[("cults3d.com", "/en/3d-model/a")] = html("<title>Just a moment...</title>", 403, headers=CF)
ocr.run_ocr(B_UNS)
st, note, *_ = state(B_UNS)
check("backfill: the re-run settles the cults3d row 'site blocks fetching'", st == "unsupported" and "site blocks fetching" in note, (st, note))

ctx.__exit__(None, None, None)
if FAILS:
    print(f"\nFAILED: {', '.join(FAILS)}")
    sys.exit(1)
print("\nALL PASS")
