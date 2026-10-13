#!/usr/bin/env python3
"""The Ollama restart sidecar (#643): the ONLY process that holds the Docker socket.

The web app used to bind-mount /var/run/docker.sock so the #454 caption safety valve could restart
the Ollama container. That socket is root on the host, and the web container is also the process
that parses every uploaded file. This sidecar takes the socket away from it and offers exactly one
action in exchange:

    POST /restart      header  X-Restart-Token: <shared secret>      body ignored

which restarts ONE container, named in this sidecar's own config (RESTARTER_CONTAINER, default
"ollama"). The name never comes from the request: there is no path parameter, query string or body
field that can change it, and the request body is never read. The only Docker Engine API call this
file makes is `POST /containers/<RESTARTER_CONTAINER>/restart`.

    GET /healthz       no auth, no Docker call: {"ok": true} (a liveness probe)

Anything else is 404 (unknown path) or 405 (known path, wrong method).

Guard rails (owner decision on #643):
  * no generic Docker proxy: one fixed endpoint, one fixed container;
  * internal network only: compose puts this on an `internal: true` network shared with the web
    service and publishes no port;
  * shared secret: X-Restart-Token, compared in constant time; the token is never logged;
  * rate limited HERE (not only in the web app): at most one restart attempt per
    RESTARTER_MIN_INTERVAL_SECONDS (default 300). A refused request gets 429 + Retry-After. Every
    attempt that reaches Docker counts, failed or not, so a compromised web container cannot use
    this to keep Ollama down.

Configuration (environment):
  RESTARTER_CONTAINER               the one container it may restart (default "ollama")
  RESTARTER_TOKEN_FILE              file whose stripped contents are the shared secret (preferred)
  RESTARTER_TOKEN                   the secret itself (tests only)
  RESTARTER_MIN_INTERVAL_SECONDS    rate limit window (default 300)
  RESTARTER_STOP_TIMEOUT_SECONDS    docker restart ?t= (default 5: SIGTERM, then SIGKILL after 5s)
  RESTARTER_PORT                    listen port (default 8080)
  RESTARTER_DOCKER_SOCKET           default /var/run/docker.sock

Refuses to start without a token of at least 32 characters. Stdlib only.
"""

import hmac
import http.client
import http.server
import json
import os
import socket
import sys
import threading
import time

MIN_TOKEN_LEN = 32
TOKEN_HEADER = "X-Restart-Token"


def _env_int(name, default):
    raw = (os.environ.get(name) or "").strip()
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError:
        sys.exit(f"restarter: {name} must be an integer, got {raw!r}")


def load_token(env=None):
    """The shared secret, or exit. Never includes the secret in a message."""
    env = os.environ if env is None else env
    path = (env.get("RESTARTER_TOKEN_FILE") or "").strip()
    if path:
        try:
            with open(path, "r", encoding="utf-8") as f:
                token = f.read().strip()
        except OSError as exc:
            sys.exit(f"restarter: RESTARTER_TOKEN_FILE {path!r} can't be read ({exc.strerror or exc})")
    else:
        token = (env.get("RESTARTER_TOKEN") or "").strip()
    if len(token) < MIN_TOKEN_LEN:
        sys.exit(f"restarter: no shared secret, or shorter than {MIN_TOKEN_LEN} characters "
                 "(set RESTARTER_TOKEN_FILE)")
    return token


class _UnixHTTPConnection(http.client.HTTPConnection):
    def __init__(self, socket_path, timeout):
        super().__init__("localhost", timeout=timeout)
        self._socket_path = socket_path

    def connect(self):
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        sock.settimeout(self.timeout)
        sock.connect(self._socket_path)
        self.sock = sock


def docker_restart(socket_path, container, stop_timeout):
    """The one Docker Engine API call. Returns (ok: bool, detail: str)."""
    conn = _UnixHTTPConnection(socket_path, timeout=stop_timeout + 60)
    try:
        conn.request("POST", f"/containers/{container}/restart?t={int(stop_timeout)}")
        resp = conn.getresponse()
        body = resp.read()
        if resp.status in (200, 204):
            return True, f"docker answered {resp.status}"
        return False, f"docker answered {resp.status}: {body[:200]!r}"
    finally:
        conn.close()


class Restarter:
    """The state and the decision, separate from HTTP so it can be tested without a socket."""

    def __init__(self, token, container, min_interval, stop_timeout, socket_path,
                 restart_fn=docker_restart, clock=time.monotonic):
        self._token = token.encode("utf-8")
        self.container = container
        self.min_interval = min_interval
        self.stop_timeout = stop_timeout
        self.socket_path = socket_path
        self._restart_fn = restart_fn
        self._clock = clock
        self._lock = threading.Lock()
        self._last_attempt = None

    def authorized(self, presented):
        if not presented:
            return False
        return hmac.compare_digest(presented.encode("utf-8"), self._token)

    def restart(self):
        """Returns (http_status, payload dict, extra headers dict). Called only once authorized."""
        with self._lock:  # one restart at a time; a second caller waits, then sees the rate limit
            now = self._clock()
            if self._last_attempt is not None:
                elapsed = now - self._last_attempt
                if elapsed < self.min_interval:
                    retry = int(self.min_interval - elapsed) + 1
                    return 429, {"restarted": False, "error": "rate_limited",
                                 "retry_after_seconds": retry}, {"Retry-After": str(retry)}
            self._last_attempt = now  # counts whether or not Docker succeeds
            started = self._clock()
            try:
                ok, detail = self._restart_fn(self.socket_path, self.container, self.stop_timeout)
            except Exception as exc:
                log(f"restart of {self.container} failed: {exc!r}")
                return 502, {"restarted": False, "error": "docker_unreachable", "detail": repr(exc)}, {}
            secs = round(self._clock() - started, 2)
            if ok:
                log(f"restarted {self.container} in {secs}s ({detail})")
                return 200, {"restarted": True, "container": self.container, "seconds": secs}, {}
            log(f"restart of {self.container} refused: {detail}")
            return 502, {"restarted": False, "error": "docker_error", "detail": detail}, {}


def log(msg):
    print(f"restarter: {msg}", flush=True)


def make_handler(restarter):
    class Handler(http.server.BaseHTTPRequestHandler):
        server_version = "ollama-restarter"
        sys_version = ""

        def _send(self, status, payload, headers=None):
            body = json.dumps(payload).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            for k, v in (headers or {}).items():
                self.send_header(k, v)
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, fmt, *args):  # one line per request, never the headers
            log(f"{self.client_address[0]} {fmt % args}")

        def do_GET(self):
            if self.path == "/healthz":
                return self._send(200, {"ok": True})
            if self.path == "/restart":
                return self._send(405, {"error": "method_not_allowed"}, {"Allow": "POST"})
            return self._send(404, {"error": "not_found"})

        def do_POST(self):
            # Exact match: "/restart?container=x" or "/restart/x" is a 404, not a restart.
            if self.path != "/restart":
                return self._send(404, {"error": "not_found"})
            if not restarter.authorized(self.headers.get(TOKEN_HEADER)):
                log(f"refused an unauthenticated restart request from {self.client_address[0]}")
                return self._send(401, {"error": "unauthorized"})
            status, payload, headers = restarter.restart()
            return self._send(status, payload, headers)

        def _not_allowed(self):
            return self._send(405, {"error": "method_not_allowed"}, {"Allow": "GET, POST"})

        do_PUT = do_DELETE = do_PATCH = _not_allowed

    return Handler


def main():
    restarter = Restarter(
        token=load_token(),
        container=(os.environ.get("RESTARTER_CONTAINER") or "ollama").strip(),
        min_interval=_env_int("RESTARTER_MIN_INTERVAL_SECONDS", 300),
        stop_timeout=_env_int("RESTARTER_STOP_TIMEOUT_SECONDS", 5),
        socket_path=(os.environ.get("RESTARTER_DOCKER_SOCKET") or "/var/run/docker.sock").strip(),
    )
    port = _env_int("RESTARTER_PORT", 8080)
    server = http.server.ThreadingHTTPServer(("0.0.0.0", port), make_handler(restarter))
    log(f"listening on :{port}; may restart only '{restarter.container}', "
        f"at most once per {restarter.min_interval}s")
    server.serve_forever()


if __name__ == "__main__":
    main()
