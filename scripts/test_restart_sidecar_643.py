#!/usr/bin/env python3
"""Self-contained check for the Ollama restart sidecar and the web side that calls it (#643).

Throwaway DB + storage (scripts/_testenv.py), no Docker, no real Ollama. The sidecar
(ops/ollama-restarter/restarter.py) runs on 127.0.0.1 with its one Docker call replaced by a
recorder, so the checks prove what it WOULD ask Docker for:
  * no secret / wrong secret -> 401, nothing restarted
  * any other path, a query string, a path suffix or a body naming another container -> never a
    restart of anything but the configured container (404 for the path variants)
  * GET /restart -> 405; GET /healthz -> 200 without auth and without a Docker call
  * the rate limit: a second authorized request inside the window -> 429 + Retry-After, and a
    failed Docker call still counts
  * it refuses to start without a secret of at least 32 characters
and core/captions.py's side:
  * not configured, unreachable, rate-limited (429) or refused secret (401) -> soft: logged, the
    breaker stays closed, no exception escapes
  * a real restart answer -> restarted, cooldown recorded
  * the sidecar trying and failing (502) -> the breaker opens, as a failed restart did before
  * no Docker-socket code is left in the web app

    python scripts/test_restart_sidecar_643.py
"""

import importlib.util
import json
import os
import sys
import threading
import urllib.error
import urllib.request

import _testenv  # noqa: E402
TMP = _testenv.isolate("restart-sidecar-643-")
ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")
sys.path.insert(0, ROOT)

try:
    import cairosvg  # noqa: F401
except Exception:  # silent-ok: no libcairo here (e.g. Windows); object types import it
    import types
    sys.modules["cairosvg"] = types.ModuleType("cairosvg")

from core import captions  # noqa: E402
_testenv.assert_isolated()

spec = importlib.util.spec_from_file_location(
    "restarter", os.path.join(ROOT, "ops", "ollama-restarter", "restarter.py"))
restarter = importlib.util.module_from_spec(spec)
spec.loader.exec_module(restarter)

FAILS = []


def check(name, cond, extra=""):
    print(("PASS " if cond else "FAIL ") + name + (f"  [{extra}]" if (extra and not cond) else ""))
    if not cond:
        FAILS.append(name)


TOKEN = "t" * 40
CALLS = []


class Clock:
    now = 1000.0

    def __call__(self):
        return self.now


def make_server(restart_ok=True, min_interval=300):
    clock = Clock()

    def fake_restart(socket_path, container, stop_timeout):
        CALLS.append(container)
        if restart_ok is None:
            raise OSError("no such socket")
        return (True, "docker answered 204") if restart_ok else (False, "docker answered 404: no such container")

    r = restarter.Restarter(TOKEN, "ollama", min_interval, 5, "/nonexistent.sock",
                            restart_fn=fake_restart, clock=clock)
    srv = restarter.http.server.ThreadingHTTPServer(("127.0.0.1", 0), restarter.make_handler(r))
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv, clock, f"http://127.0.0.1:{srv.server_address[1]}"


def stop(srv):
    srv.shutdown()
    srv.server_close()  # close the listening socket too, so 'unreachable' really is


def call(url, method="POST", token=None, body=b""):
    headers = {restarter.TOKEN_HEADER: token} if token is not None else {}
    req = urllib.request.Request(url, data=body if method == "POST" else None, method=method, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=5) as resp:
            return resp.status, json.loads(resp.read() or b"{}"), dict(resp.headers)
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read() or b"{}"), dict(e.headers)


# --- the sidecar ---------------------------------------------------------------------------------
srv, clock, base = make_server()

s, body, _ = call(base + "/restart")
check("no secret -> 401", s == 401 and body.get("error") == "unauthorized", f"{s} {body}")
s, body, _ = call(base + "/restart", token="wrong" * 10)
check("wrong secret -> 401", s == 401, f"{s} {body}")
check("nothing restarted without the secret", CALLS == [], CALLS)

for path in ("/restart?container=constructicon-web", "/restart/constructicon-web",
             "/containers/constructicon-web/restart", "/", "/restart/"):
    s, body, _ = call(base + path, token=TOKEN)
    check(f"POST {path} with the secret -> 404", s == 404, f"{s} {body}")
check("no path variant restarted anything", CALLS == [], CALLS)

s, body, _ = call(base + "/restart", method="GET", token=TOKEN)
check("GET /restart -> 405", s == 405, f"{s} {body}")
s, body, _ = call(base + "/healthz", method="GET")
check("GET /healthz -> 200 without auth", s == 200 and body == {"ok": True}, f"{s} {body}")
s, body, _ = call(base + "/restart", method="DELETE", token=TOKEN)
check("DELETE /restart -> 405", s == 405, f"{s} {body}")
check("healthz and wrong methods made no Docker call", CALLS == [], CALLS)

s, body, _ = call(base + "/restart", token=TOKEN, body=json.dumps({"container": "constructicon-web"}).encode())
check("authorized POST /restart -> 200 restarted", s == 200 and body.get("restarted") is True, f"{s} {body}")
check("it restarted the CONFIGURED container, ignoring the body", CALLS == ["ollama"], CALLS)

clock.now += 10
s, body, hdrs = call(base + "/restart", token=TOKEN)
check("second restart inside the window -> 429", s == 429 and body.get("error") == "rate_limited", f"{s} {body}")
check("429 carries Retry-After", int(hdrs.get("Retry-After", "0")) > 0, hdrs)
check("rate-limited request made no Docker call", CALLS == ["ollama"], CALLS)

clock.now += 300
s, body, _ = call(base + "/restart", token=TOKEN)
check("after the window -> restarted again", s == 200 and CALLS == ["ollama", "ollama"], f"{s} {CALLS}")
stop(srv)

CALLS.clear()
srv, clock, base = make_server(restart_ok=False)
s, body, _ = call(base + "/restart", token=TOKEN)
check("Docker refusing -> 502 docker_error", s == 502 and body.get("error") == "docker_error", f"{s} {body}")
s, body, _ = call(base + "/restart", token=TOKEN)
check("a failed attempt still counts for the rate limit", s == 429 and CALLS == ["ollama"], f"{s} {CALLS}")
stop(srv)

CALLS.clear()
srv, clock, base = make_server(restart_ok=None)
s, body, _ = call(base + "/restart", token=TOKEN)
check("socket missing -> 502 docker_unreachable", s == 502 and body.get("error") == "docker_unreachable", f"{s} {body}")
stop(srv)

for env, label in (({}, "no secret"), ({"RESTARTER_TOKEN": "zq7-tiny"}, "a short secret"),
                   ({"RESTARTER_TOKEN_FILE": os.path.join(TMP, "missing")}, "an unreadable secret file")):
    try:
        restarter.load_token(env)
        check(f"refuses to start with {label}", False)
    except SystemExit as e:
        check(f"refuses to start with {label}", True)
        check(f"the refusal for {label} doesn't echo a secret", "zq7-tiny" not in str(e), str(e))
tok_file = os.path.join(TMP, "restart_token")
with open(tok_file, "w") as f:
    f.write(TOKEN + "\n")
check("reads the secret from RESTARTER_TOKEN_FILE", restarter.load_token({"RESTARTER_TOKEN_FILE": tok_file}) == TOKEN)

src = open(os.path.join(ROOT, "ops", "ollama-restarter", "restarter.py"), encoding="utf-8").read()
check("the sidecar's only Docker endpoint is /containers/<configured>/restart",
      src.count("/containers/") == 2 and 'f"/containers/{container}/restart' in src)


# --- the web side (core/captions.py) ------------------------------------------------------------
def reset_state():
    captions._breaker_reason = None
    captions._last_restart_at = None
    captions._consecutive_failures = 0


captions.is_ollama_up = lambda timeout=3: True  # don't need a real Ollama to "come back"
captions.READY_POLL_SECONDS = 2

reset_state()
captions.RESTART_URL = ""
os.environ.pop("CAPTION_RESTART_TOKEN_FILE", None)
os.environ.pop("CAPTION_RESTART_TOKEN", None)
check("restart_available() is False when unconfigured", captions.restart_available() is False)
restarted, _ = captions._maybe_restart("test: unconfigured")
check("unconfigured: soft, no restart, breaker closed", restarted is False and captions._breaker_reason is None,
      captions._breaker_reason)

CALLS.clear()
srv, clock, base = make_server()
captions.RESTART_URL = base + "/restart"
os.environ["CAPTION_RESTART_TOKEN_FILE"] = tok_file
check("restart_available() is True when configured", captions.restart_available() is True)
reset_state()
restarted, secs = captions._maybe_restart("test: real path")
check("configured: restarted through the sidecar", restarted is True and CALLS == ["ollama"], f"{restarted} {CALLS}")
check("configured: cooldown recorded", captions._last_restart_at is not None)

captions._last_restart_at = None  # skip the web-side cooldown to reach the sidecar's own limit
restarted, _ = captions._maybe_restart("test: rate limited")
check("sidecar 429: soft, no restart, breaker closed",
      restarted is False and captions._breaker_reason is None and CALLS == ["ollama"], f"{captions._breaker_reason} {CALLS}")
stop(srv)

srv, clock, base = make_server()
captions.RESTART_URL = base + "/restart"
os.environ["CAPTION_RESTART_TOKEN"] = "x" * 40
os.environ.pop("CAPTION_RESTART_TOKEN_FILE")
reset_state()
restarted, _ = captions._maybe_restart("test: wrong secret")
check("wrong secret (401): soft, breaker closed", restarted is False and captions._breaker_reason is None,
      captions._breaker_reason)
os.environ.pop("CAPTION_RESTART_TOKEN")
os.environ["CAPTION_RESTART_TOKEN_FILE"] = tok_file
stop(srv)

captions.RESTART_URL = base + "/restart"  # that server is shut down now
reset_state()
restarted, _ = captions._maybe_restart("test: unreachable")
check("sidecar unreachable: soft, breaker closed", restarted is False and captions._breaker_reason is None,
      captions._breaker_reason)

srv, clock, base = make_server(restart_ok=False)
captions.RESTART_URL = base + "/restart"
reset_state()
restarted, _ = captions._maybe_restart("test: docker refused")
check("sidecar 502 (restart attempted, failed): breaker opens",
      restarted is False and captions._breaker_reason is not None, captions._breaker_reason)
stop(srv)
reset_state()

web_src = open(os.path.join(ROOT, "core", "captions.py"), encoding="utf-8").read()
check("captions.py no longer talks to a Docker socket",
      "AF_UNIX" not in web_src and "/containers/" not in web_src and "docker.sock" not in web_src.split('"""', 2)[2])
check("captions.py never names the container it wants restarted", "OLLAMA_CONTAINER" not in web_src)

print("\n%s" % ("ALL PASS" if not FAILS else "FAILED: " + ", ".join(FAILS)))
sys.exit(1 if FAILS else 0)
