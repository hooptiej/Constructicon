"""Load test for issue #732 ("How much can the warehouse hold?"). Stdlib only.

Target: constructicon-test ONLY. Never point it at production. Every scenario ramps its load up a
step at a time; a separate guard process on the host watches production and the box and writes a
STOP file the moment one guard trips. The load driver stops all load as soon as it sees that file,
and the whole run ends there (owner decision 2026-10-29: stop the whole run at the first step that
trips a guard).

Subcommands (one file, three places it runs):

  genfiles   (host)       Make the random upload files (never on Windows).
                          python3 load_test_732.py genfiles --dir DIR [--sizes-mib 100,500,...]
  guard      (host)       Watch prod and the box; write OUT/STOP when a guard trips.
                          python3 load_test_732.py guard --out OUT [--prod-url http://10.0.1.240]
  load       (container)  Drive the scenarios against the test web app and test MCP.
  cleanup    (container)  Delete every uploaded test item through the app, then empty test's trash.
  summarize  (anywhere)   Turn OUT/load.jsonl + OUT/guard.jsonl into markdown tables.

How it was run on the NAS (2026-10-29): the guard on the host as the hoop user (it calls
`sudo docker` itself), and the load driver in a throwaway twin of the test web container, which
reaches test at http://localhost:80 (its network namespace) and reads the install token from the
mounted secret, as scripts/run_tool.sh does:

  mkdir -p ~/lt732/{s,out,files} && chmod 777 ~/lt732/out
  python3 ~/lt732/s/load_test_732.py genfiles --dir ~/lt732/files
  nohup python3 ~/lt732/s/load_test_732.py guard --out ~/lt732/out > ~/lt732/out/guard.log 2>&1 &
  sudo docker run --rm -d --name lt732-load --network container:constructicon-test \
      --volumes-from constructicon-test -v ~/lt732/s:/s:ro -v ~/lt732/out:/out \
      -v ~/lt732/files:/files:ro --entrypoint python3 constructicon-test:latest \
      /s/load_test_732.py load --out /out --files /files
  ... then `load_test_732.py cleanup --out /out` (same docker run form) and `summarize --out ~/lt732/out`.

One scenario per run (run 2, owner decision 2026-10-29: a trip ends only that scenario): give each run
its own --out directory, guard and STOP file, start the guard, then the same docker run with
`--scenarios mcp` (or backlog, upload), then touch OUT/LOAD_DONE if the driver died before writing it,
and wait for the guard to exit. Let the box settle (load1 < 2, prod /healthz at baseline) between runs.
`cleanup --empty-trash` empties ALL of test's ordinary trash: count it first.

Guards (host, `guard`): any trip writes OUT/STOP (JSON: which guard, the numbers) once.
  - prod /healthz: any failure (non-200, timeout 5 s), or the p95 of the last 20 probes (1 Hz)
    above --prod-factor (3) x the idle baseline p95 measured first (--baseline-samples probes);
  - host MemAvailable below --mem-floor-kb (1,000,000 kB). ZFS ARC is NOT counted in MemAvailable,
    so the ARC-adjusted headroom (MemAvailable + ARC above its minimum) is logged beside it;
  - 1-minute load average above --load-max (8) for more than --load-secs (30) seconds;
  - unscrypted / unscrypted-test (and prod's constructicon-web / -mcp): a restart (StartedAt or
    RestartCount changed), not running, or health "unhealthy";
  - Ollama: the `ollama` container's StartedAt / RestartCount changed, or either ollama-restarter's
    log shows "restarted ollama" after the guard started.
The guard keeps sampling after a trip (to watch the box settle) until OUT/LOAD_DONE exists, then
samples --settle-secs more, writes OUT/settle.json and exits.

The token is read from a file (CONSTRUCTICON_INSTALL_TOKEN_FILE or /run/secrets/constructicon_token)
and only ever put in an Authorization header. It is never printed or written to any output file.

Exit codes: 0 done; 1 or 2 the script itself failed or refused to start (the reason is printed);
3 a guard stopped the run.
"""

import argparse
import concurrent.futures
import http.client
import json
import math
import os
import random
import re
import subprocess
import sys
import threading
import time
import traceback
import urllib.error
import urllib.parse
import urllib.request
import uuid
from pathlib import Path

MIB = 1024 * 1024


def now():
    return time.time()


def pct(values, p):
    """Nearest-rank percentile (the ceil(p% x n)-th smallest); None for an empty list. With the
    guard's 20-probe window, p95 is the 19th of 20: one slow probe alone never trips it, two do."""
    if not values:
        return None
    v = sorted(values)
    k = max(0, min(len(v) - 1, math.ceil(p / 100.0 * len(v)) - 1))
    return v[k]


class Out:
    """Appends JSON lines and a plain log under the output directory (thread-safe)."""

    def __init__(self, out_dir, name):
        self.dir = Path(out_dir)
        self.dir.mkdir(parents=True, exist_ok=True)
        self.jsonl = self.dir / f"{name}.jsonl"
        self.log_path = self.dir / f"{name}.log.txt"
        self.lock = threading.Lock()

    def log(self, msg):
        line = f"{time.strftime('%H:%M:%S')} {msg}"
        with self.lock:
            print(line, flush=True)
            with open(self.log_path, "a") as f:
                f.write(line + "\n")

    def rec(self, kind, **data):
        data = {"t": round(now(), 3), "kind": kind, **data}
        with self.lock:
            with open(self.jsonl, "a") as f:
                f.write(json.dumps(data) + "\n")
        return data


# --------------------------------------------------------------------------------------------
# genfiles (host)
# --------------------------------------------------------------------------------------------

def cmd_genfiles(a):
    d = Path(a.dir)
    d.mkdir(parents=True, exist_ok=True)
    block = os.urandom(64 * MIB)  # one random block, repeated: no dedup or compression on it matters
    for s in a.sizes_mib.split(","):
        mib = float(s)
        size = int(mib * MIB)
        p = d / f"lt732_{s.replace('.', 'p')}MiB.img"
        if p.exists() and p.stat().st_size == size:
            print(f"have {p.name} ({size} bytes)")
            continue
        with open(p, "wb") as f:
            left = size
            while left:
                n = min(left, len(block))
                f.write(block[:n])
                left -= n
        print(f"made {p.name} ({size} bytes)", flush=True)
    return 0


# --------------------------------------------------------------------------------------------
# guard (host)
# --------------------------------------------------------------------------------------------

WATCH_CONTAINERS = ["unscrypted", "unscrypted-test", "ollama", "constructicon-web", "constructicon-mcp"]
RESTARTERS = ["constructicon-test-ollama-restarter", "constructicon-ollama-restarter"]
STATS_CONTAINERS = ["constructicon-test", "constructicon-test-mcp", "ollama", "constructicon-web",
                    "constructicon-mcp", "unscrypted"]


def sh(args, timeout=30):
    r = subprocess.run(args, capture_output=True, text=True, timeout=timeout)
    if r.returncode != 0:
        raise RuntimeError(f"{' '.join(args[:3])}... exited {r.returncode}: {r.stderr.strip()[:300]}")
    return r.stdout


def meminfo():
    m = {}
    with open("/proc/meminfo") as f:
        for line in f:
            k, v = line.split(":", 1)
            m[k] = int(v.split()[0])
    arc = {}
    try:
        with open("/proc/spl/kstat/zfs/arcstats") as f:
            for line in f.readlines()[2:]:
                parts = line.split()
                if len(parts) == 3:
                    arc[parts[0]] = int(parts[2])
    except OSError:
        pass  # not ZFS: the ARC-adjusted figure is just MemAvailable
    avail = m.get("MemAvailable", 0)
    arc_spare_kb = max(0, arc.get("size", 0) - arc.get("c_min", 0)) // 1024
    return {"mem_avail_kb": avail, "arc_kb": arc.get("size", 0) // 1024,
            "mem_avail_plus_arc_kb": avail + arc_spare_kb}


def loadavg():
    with open("/proc/loadavg") as f:
        return float(f.read().split()[0])


def inspect(names):
    out = {}
    for n in names:
        try:
            raw = sh(["sudo", "docker", "inspect", n])
        except RuntimeError as e:
            out[n] = {"missing": str(e)[:120]}
            continue
        st = json.loads(raw)[0]
        s = st["State"]
        out[n] = {"started": s.get("StartedAt"), "restarts": st.get("RestartCount"),
                  "running": s.get("Running"), "health": (s.get("Health") or {}).get("Status")}
    return out


def parse_mem(s):
    s = s.strip()
    for unit, mult in (("GiB", 1024), ("MiB", 1), ("KiB", 1 / 1024), ("GB", 1000), ("MB", 1), ("kB", 1 / 1000), ("B", 1 / MIB)):
        if s.endswith(unit):
            return float(s[: -len(unit)]) * mult
    return None


def docker_stats():
    raw = sh(["sudo", "docker", "stats", "--no-stream", "--format", "{{.Name}}\t{{.MemUsage}}\t{{.CPUPerc}}",
              *STATS_CONTAINERS], timeout=60)
    res = {}
    for line in raw.splitlines():
        name, mem, cpu = line.split("\t")
        res[name] = {"mem_mib": parse_mem(mem.split("/")[0]), "cpu_pct": float(cpu.rstrip("%") or 0)}
    return res


def gpu():
    try:
        raw = sh(["nvidia-smi", "--query-gpu=memory.used,memory.total,utilization.gpu",
                  "--format=csv,noheader,nounits"], timeout=15)
    except (RuntimeError, FileNotFoundError, subprocess.TimeoutExpired) as e:
        return {"error": str(e)[:120]}
    used, total, util = [x.strip() for x in raw.splitlines()[0].split(",")]
    return {"vram_used_mib": int(used), "vram_total_mib": int(total), "gpu_util_pct": int(util)}


def probe(url, timeout=5.0):
    t = time.perf_counter()
    try:
        with urllib.request.urlopen(url, timeout=timeout) as r:
            r.read()
            return r.status, (time.perf_counter() - t) * 1000, None
    except urllib.error.HTTPError as e:
        return e.code, (time.perf_counter() - t) * 1000, f"HTTP {e.code}"
    except Exception as e:  # timeouts, refused, reset: every one is a failed probe, recorded
        return 0, (time.perf_counter() - t) * 1000, f"{type(e).__name__}: {e}"[:200]


def cmd_guard(a):
    o = Out(a.out, "guard")
    out = Path(a.out)
    stopf, donef = out / "STOP", out / "LOAD_DONE"
    for f in (stopf, donef, out / "READY"):
        if f.exists():
            raise SystemExit(f"guard: {f} already exists from an earlier run; use a fresh --out directory")
    health = a.prod_url.rstrip("/") + "/healthz"
    start_iso = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())

    o.log(f"guard: measuring prod baseline, {a.baseline_samples} probes of {health} at 1 Hz")
    lat = []
    for _ in range(a.baseline_samples):
        st, ms, err = probe(health)
        if st != 200:
            o.log(f"guard: prod /healthz failed at baseline ({err}); refusing to start any load")
            return 2
        lat.append(ms)
        time.sleep(1)
    base = {"prod_p50_ms": pct(lat, 50), "prod_p95_ms": pct(lat, 95), "prod_max_ms": max(lat),
            "host": {**meminfo(), "load1": loadavg()}, "containers": inspect(WATCH_CONTAINERS),
            "stats": docker_stats(), "gpu": gpu(), "start_iso": start_iso}
    limit_ms = max(a.prod_factor * base["prod_p95_ms"], base["prod_p95_ms"] + a.prod_floor_ms)
    base["prod_limit_ms"] = limit_ms
    base["thresholds"] = {"prod_factor": a.prod_factor, "prod_floor_ms": a.prod_floor_ms,
                          "mem_floor_kb": a.mem_floor_kb, "load_max": a.load_max, "load_secs": a.load_secs}
    (out / "baseline.json").write_text(json.dumps(base, indent=1))
    o.rec("baseline", **base)
    o.log(f"guard: baseline prod p50 {base['prod_p50_ms']:.1f} ms p95 {base['prod_p95_ms']:.1f} ms -> "
          f"limit {limit_ms:.1f} ms; MemAvailable {base['host']['mem_avail_kb']} kB "
          f"(+ARC spare {base['host']['mem_avail_plus_arc_kb']} kB); load {base['host']['load1']}")
    for n, s in base["containers"].items():
        if "missing" in s:
            o.log(f"guard: WARNING {n} not inspectable at baseline ({s['missing']}); its guard is off")
    (out / "READY").write_text("1")

    tripped = threading.Event()
    stop_threads = threading.Event()

    def trip(guard, **detail):
        if tripped.is_set():
            o.rec("guard_trip_extra", guard=guard, **detail)
            return
        tripped.set()
        info = {"t": now(), "guard": guard, **detail}
        stopf.write_text(json.dumps(info))
        o.rec("guard_trip", **info)
        o.log(f"guard: TRIPPED {guard}: {detail}")

    # Seeded with the last 20 baseline probes, so the p95 is ALWAYS over 20 probes. An unseeded
    # window of 10-19 probes made "p95" the max, and one slow probe tripped it (#732 run 2).
    window = list(lat[-20:])

    def prod_loop():
        while not stop_threads.is_set():
            st, ms, err = probe(health)
            window.append(ms)
            del window[:-20]
            o.rec("prod", status=st, ms=round(ms, 2), err=err)
            if st != 200:
                trip("prod_healthz_failed", status=st, error=err)
            elif len(window) >= 20:
                p95 = pct(window, 95)
                if p95 > limit_ms:
                    trip("prod_latency", window_p95_ms=round(p95, 1), limit_ms=round(limit_ms, 1),
                         baseline_p95_ms=round(base["prod_p95_ms"], 1))
            time.sleep(max(0.0, 1.0 - ms / 1000))

    def host_loop():
        high_since = None
        while not stop_threads.is_set():
            m, l1 = meminfo(), loadavg()
            o.rec("host", load1=l1, **m)
            if m["mem_avail_kb"] < a.mem_floor_kb:
                trip("host_memory", mem_avail_kb=m["mem_avail_kb"], floor_kb=a.mem_floor_kb,
                     mem_avail_plus_arc_kb=m["mem_avail_plus_arc_kb"])
            if l1 > a.load_max:
                high_since = high_since or now()
                if now() - high_since > a.load_secs:
                    trip("load_average", load1=l1, above_for_s=round(now() - high_since, 1), max=a.load_max)
            else:
                high_since = None
            time.sleep(2)

    def docker_loop():
        last_logs = 0
        while not stop_threads.is_set():
            cur = inspect(WATCH_CONTAINERS)
            o.rec("containers", **cur)
            for n, s in cur.items():
                b = base["containers"].get(n, {})
                if "missing" in b:
                    continue
                if "missing" in s:
                    trip("container_gone", container=n, detail=s["missing"])
                    continue
                if s["started"] != b["started"] or s["restarts"] != b["restarts"]:
                    trip("ollama_restart" if n == "ollama" else "container_restart", container=n,
                         was={"started": b["started"], "restarts": b["restarts"]},
                         now={"started": s["started"], "restarts": s["restarts"]})
                elif not s["running"]:
                    trip("container_stopped", container=n)
                elif s["health"] == "unhealthy":
                    trip("container_unhealthy", container=n)
            if now() - last_logs > 10:
                last_logs = now()
                for r in RESTARTERS:
                    try:
                        logs = subprocess.run(["sudo", "docker", "logs", "--since", start_iso, r],
                                              capture_output=True, text=True, timeout=30)
                    except subprocess.TimeoutExpired:
                        o.log(f"guard: reading {r}'s log timed out (not a trip; the container StartedAt check still runs)")
                        continue
                    text = logs.stdout + logs.stderr
                    hits = [ln for ln in text.splitlines() if "restarted ollama" in ln or "POST /restart" in ln]
                    if hits:
                        trip("ollama_restart", restarter=r, log=hits[-3:])
                try:
                    plog = subprocess.run(["sudo", "docker", "logs", "--since", start_iso, "constructicon-web"],
                                          capture_output=True, text=True, timeout=30)
                    lines = [ln for ln in (plog.stdout + plog.stderr).splitlines() if "caption" in ln.lower()]
                    o.rec("prod_caption_log", lines=len(lines), last=lines[-3:])
                except subprocess.TimeoutExpired:
                    o.log("guard: reading prod's log for caption lines timed out (observation only)")
            time.sleep(4)

    def stats_loop():
        n = 0
        while not stop_threads.is_set():
            try:
                rec = {"stats": docker_stats()}
                if n % 5 == 0:
                    rec["gpu"] = gpu()
                o.rec("stats", **rec)
            except (RuntimeError, subprocess.TimeoutExpired) as e:
                o.log(f"guard: docker stats failed ({e}); memory samples missing for this interval")
            n += 1
            time.sleep(2)

    threads = []
    for fn in (prod_loop, host_loop, docker_loop, stats_loop):
        t = threading.Thread(target=_logged(o, fn), name=fn.__name__, daemon=True)
        t.start()
        threads.append(t)

    while not donef.exists():
        if not all(t.is_alive() for t in threads):
            dead = [t.name for t in threads if not t.is_alive()]
            trip("guard_internal_error", dead_threads=dead)  # a blind guard is a stop, never a shrug
            o.log(f"guard: a watcher thread died ({dead}); tripped STOP so no load runs unwatched")
            break
        time.sleep(1)
    o.log(f"guard: load finished; watching {a.settle_secs} s more for the settle")
    t_end = now() + a.settle_secs
    while now() < t_end:
        time.sleep(1)
    stop_threads.set()
    lat = []
    fails = 0
    for _ in range(30):
        st, ms, err = probe(health)
        lat.append(ms)
        fails += st != 200
        time.sleep(1)
    settle = {"prod_p50_ms": pct(lat, 50), "prod_p95_ms": pct(lat, 95), "prod_failures": fails,
              "host": {**meminfo(), "load1": loadavg()}, "containers": inspect(WATCH_CONTAINERS),
              "stats": docker_stats(), "gpu": gpu()}
    changed = [n for n, s in settle["containers"].items()
               if "missing" not in base["containers"].get(n, {"missing": 1})
               and (s.get("started") != base["containers"][n]["started"]
                    or s.get("restarts") != base["containers"][n]["restarts"])]
    settle["containers_restarted_since_baseline"] = changed
    (out / "settle.json").write_text(json.dumps(settle, indent=1))
    o.rec("settle", **settle)
    o.log(f"guard: settle prod p50 {settle['prod_p50_ms']:.1f} p95 {settle['prod_p95_ms']:.1f} ms, "
          f"{fails} failures; MemAvailable {settle['host']['mem_avail_kb']} kB; load {settle['host']['load1']}; "
          f"restarted since baseline: {changed or 'none'}")
    return 3 if tripped.is_set() else 0


def _logged(o, fn):
    def run():
        try:
            fn()
        except Exception:
            o.log(f"guard: {fn.__name__} crashed:\n{traceback.format_exc()}")
            raise
    return run


# --------------------------------------------------------------------------------------------
# load (container)
# --------------------------------------------------------------------------------------------

class Stopped(Exception):
    pass


class Client:
    def __init__(self, base, mcp_url, token):
        self.base = base.rstrip("/")
        self.mcp_url = mcp_url
        self._auth = "Bearer " + token

    def get(self, path, timeout=30):
        """(status, ms, bytes, error) for one GET; never raises for HTTP/network failures."""
        req = urllib.request.Request(self.base + path, headers={"Authorization": self._auth})
        t = time.perf_counter()
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                body = r.read()
                return r.status, (time.perf_counter() - t) * 1000, len(body), None, body
        except urllib.error.HTTPError as e:
            body = e.read()
            return e.code, (time.perf_counter() - t) * 1000, len(body), _err_text(body, e.code), body
        except Exception as e:  # network-level failure: status 0 with the reason, counted as an error
            return 0, (time.perf_counter() - t) * 1000, 0, f"{type(e).__name__}: {e}"[:200], b""

    def get_json(self, path):
        st, _ms, _n, err, body = self.get(path)
        if st != 200:
            raise RuntimeError(f"GET {path} -> {st} {err}")
        return json.loads(body)

    def post_form(self, path, fields, timeout=60):
        data = urllib.parse.urlencode(fields, doseq=True).encode()
        req = urllib.request.Request(self.base + path, data=data, method="POST",
                                     headers={"Authorization": self._auth,
                                              "Content-Type": "application/x-www-form-urlencoded"})
        t = time.perf_counter()
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return r.status, (time.perf_counter() - t) * 1000, json.loads(r.read() or b"null"), None
        except urllib.error.HTTPError as e:
            body = e.read()
            return e.code, (time.perf_counter() - t) * 1000, None, _err_text(body, e.code)
        except Exception as e:
            return 0, (time.perf_counter() - t) * 1000, None, f"{type(e).__name__}: {e}"[:200]

    def mcp_call(self, tool, args, timeout=60):
        """One stateless streamable-HTTP tools/call. (ok, ms, error)."""
        payload = json.dumps({"jsonrpc": "2.0", "id": random.randint(1, 10**9), "method": "tools/call",
                              "params": {"name": tool, "arguments": args}}).encode()
        req = urllib.request.Request(self.mcp_url, data=payload, method="POST",
                                     headers={"Authorization": self._auth, "Content-Type": "application/json",
                                              "Accept": "application/json, text/event-stream"})
        t = time.perf_counter()
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                raw = r.read().decode("utf-8", "replace")
                ctype = r.headers.get("Content-Type", "")
        except urllib.error.HTTPError as e:
            return False, (time.perf_counter() - t) * 1000, _err_text(e.read(), e.code)
        except Exception as e:
            return False, (time.perf_counter() - t) * 1000, f"{type(e).__name__}: {e}"[:200]
        ms = (time.perf_counter() - t) * 1000
        msg = None
        try:
            if "event-stream" in ctype:
                # SSE: one event's data may span several "data:" lines (joined with "\n"); events
                # end at a blank line. The last event carrying data is the JSON-RPC answer.
                # Split on SSE's own line ends only: str.splitlines() would also cut at U+2028,
                # U+0085 and friends, which JSON may carry unescaped inside a string (#732 run 2).
                events, cur = [], []
                for line in re.split(r"\r\n|\n|\r", raw) + [""]:
                    if line.startswith("data:"):
                        cur.append(line[5:][1:] if line[5:].startswith(" ") else line[5:])
                    elif line == "" and cur:
                        events.append("\n".join(cur))
                        cur = []
                if events:
                    msg = json.loads(events[-1])
            else:
                msg = json.loads(raw)
        except ValueError as e:  # an answer we can't parse is a failed call, recorded with why
            return False, ms, f"unparseable MCP answer ({ctype}, {len(raw)} chars): {e}"[:200]
        if msg is None:
            return False, ms, "no JSON-RPC message in the answer"
        if "error" in msg:
            return False, ms, f"jsonrpc error {msg['error']}"[:200]
        res = msg.get("result") or {}
        if res.get("isError"):
            text = " ".join(c.get("text", "") for c in res.get("content", []))
            return False, ms, f"tool error: {text}"[:200]
        return True, ms, None


def _err_text(body, status):
    try:
        j = json.loads(body)
        e = j.get("error") or {}
        return f"HTTP {status} {e.get('code', '')}: {e.get('message') or j.get('detail')}"[:240]
    except (ValueError, AttributeError):
        return f"HTTP {status}: {body[:160]!r}"


def read_token():
    p = os.environ.get("CONSTRUCTICON_INSTALL_TOKEN_FILE") or "/run/secrets/constructicon_token"
    try:
        tok = Path(p).read_text().strip()
    except OSError as e:
        raise SystemExit(f"load: cannot read the install token file {p}: {e.strerror}")
    if len(tok) < 32:
        raise SystemExit(f"load: the install token in {p} is too short to be real; refusing to run")
    return tok


class Driver:
    def __init__(self, a, o):
        self.a = a
        self.o = o
        self.out = Path(a.out)
        self.stop = threading.Event()
        self.c = Client(a.base_url, a.mcp_url, read_token())
        threading.Thread(target=self._watch_stop, daemon=True).start()

    def _watch_stop(self):
        while not self.stop.is_set():
            if (self.out / "STOP").exists():
                self.stop.set()
                self.o.log("load: STOP seen, stopping all load now")
            time.sleep(0.25)

    def check(self):
        if self.stop.is_set():
            raise Stopped()

    def stop_info(self):
        try:
            return json.loads((self.out / "STOP").read_text())
        except (OSError, ValueError):
            return None

    # -- test /healthz prober, one per step ---------------------------------------------------
    def healthz_probe(self, done):
        res = []
        while not done.is_set():
            st, ms, _n, err, _b = self.c.get("/healthz", timeout=10)
            res.append((st, ms))
            done.wait(1.0)
        return res

    def step(self, scenario, name, fn, **meta):
        """Runs one ramp step with a test /healthz prober beside it, records it, raises Stopped."""
        self.check()
        self.o.log(f"load: {scenario} step {name} starting")
        done = threading.Event()
        hz = []
        prober = threading.Thread(target=lambda: hz.extend(self.healthz_probe(done)), daemon=True)
        t0 = now()
        prober.start()
        try:
            data = fn()
        finally:
            done.set()
            prober.join(15)
        t1 = now()
        hz_ms = [ms for st, ms in hz if st == 200]
        rec = self.o.rec("step", scenario=scenario, step=name, t0=t0, t1=t1, stopped=self.stop.is_set(),
                         test_healthz={"n": len(hz), "fail": sum(1 for st, _ in hz if st != 200),
                                       "p50_ms": pct(hz_ms, 50), "p95_ms": pct(hz_ms, 95), "max_ms": max(hz_ms, default=None)},
                         **meta, **data)
        self.o.log(f"load: {scenario} step {name} done: {json.dumps({k: v for k, v in rec.items() if k not in ('t', 'kind', 'per_op', 'results', 'items')})[:600]}")
        self.check()
        return rec

    # -- discovery ----------------------------------------------------------------------------
    def discover(self):
        cards = [p["slug"] for p in self.c.get_json("/api/projects") if p.get("slug")]
        hobbies = [h["slug"] for h in self.c.get_json("/api/hobbies") if h.get("slug")]
        grid = self.c.get_json("/api/items/grid?source=home&limit=500")
        items = grid.get("items") or []
        thumbs = [i["slug"] for i in items if i.get("has_thumbnail") or i.get("thumb_url")]
        images = [i["slug"] for i in items if i.get("media_type") == "image" and not i.get("redacted")]
        words = set()
        for p in self.c.get_json("/api/projects"):
            for w in (p.get("title") or "").split():
                w = "".join(ch for ch in w if ch.isalnum())
                if len(w) >= 4:
                    words.add(w.lower())
        d = {"cards": cards, "hobbies": hobbies, "items": [i["slug"] for i in items], "thumbs": thumbs,
             "images": images, "words": sorted(words)}
        self.o.log("load: discovered " + ", ".join(f"{k}={len(v)}" for k, v in d.items()))
        for k in ("cards", "items", "thumbs", "words"):
            if not d[k]:
                raise SystemExit(f"load: discovery found no {k} on the test instance; nothing realistic to browse")
        self.o.rec("discover", **{k: len(v) for k, v in d.items()})
        return d

    # -- scenario 1: browsing -----------------------------------------------------------------
    def browse_op(self, d):
        r = random.random()
        if r < 0.10:
            return "home", "/"
        if r < 0.30:
            return "card", "/project/" + random.choice(d["cards"])
        if r < 0.35 and d["hobbies"]:
            return "hobby", "/hobby/" + random.choice(d["hobbies"])
        if r < 0.55:
            sort = random.choice(["date_newest", "newest", "az"])
            return "grid", f"/api/items/grid?source=home&limit=120&sort={sort}"
        if r < 0.75:
            return "search", "/api/search?query=" + urllib.parse.quote(random.choice(d["words"]))
        if r < 0.95:
            return "thumb", f"/f/{random.choice(d['thumbs'])}/thumb"
        return "object", "/object/" + random.choice(d["items"])

    def scenario_browse(self, d):
        for n in [int(x) for x in self.a.browse_steps.split(",")]:
            def run(n=n):
                results = []
                lock = threading.Lock()
                deadline = now() + self.a.browse_secs

                def worker():
                    while now() < deadline and not self.stop.is_set():
                        op, path = self.browse_op(d)
                        st, ms, nbytes, err, _b = self.c.get(path)
                        with lock:
                            results.append((op, st, ms, nbytes, err))

                with concurrent.futures.ThreadPoolExecutor(n) as ex:
                    for f in [ex.submit(worker) for _ in range(n)]:
                        f.result()
                return summarize_requests(results, self.a.browse_secs)
            self.step("browse", f"{n} clients", run, clients=n)

    # -- scenario 3: MCP ----------------------------------------------------------------------
    def scenario_mcp(self, d):
        ok, ms, err = self.c.mcp_call("constructicon_search", {"query": d["words"][0]})
        if not ok:
            raise SystemExit(f"load: the test MCP at {self.a.mcp_url} did not answer a plain tools/call ({err}); "
                             "fix the URL or the transport before measuring it")
        calls = [
            lambda: ("search", "constructicon_search", {"query": random.choice(d["words"])}),
            lambda: ("get", "constructicon_get", {"slug": random.choice(d["items"])}),
            lambda: ("list_items", "constructicon_list_items", {"which": "unfiled", "limit": 50}),
            lambda: ("get_project", "constructicon_get_project", {"id_or_slug": random.choice(d["cards"])}),
        ]
        for n in [int(x) for x in self.a.mcp_steps.split(",")]:
            def run(n=n):
                results = []
                lock = threading.Lock()
                deadline = now() + self.a.mcp_secs

                def worker():
                    while now() < deadline and not self.stop.is_set():
                        op, tool, args = random.choice(calls)()
                        ok, ms, err = self.c.mcp_call(tool, args)
                        with lock:
                            results.append((op, 200 if ok else 0, ms, 0, err))

                with concurrent.futures.ThreadPoolExecutor(n) as ex:
                    for f in [ex.submit(worker) for _ in range(n)]:
                        f.result()
                return summarize_requests(results, self.a.mcp_secs)
            self.step("mcp", f"{n} parallel", run, parallel=n)

    # -- scenario 2: uploads ------------------------------------------------------------------
    def upload_one(self, path, name):
        """Streams one multipart upload from disk (1 MiB chunks; the file is never loaded whole)."""
        size = path.stat().st_size
        b = uuid.uuid4().hex
        pre = (f'--{b}\r\nContent-Disposition: form-data; name="description"\r\n\r\n'
               f"load test #732, delete me\r\n"
               f'--{b}\r\nContent-Disposition: form-data; name="file"; filename="{name}"\r\n'
               f"Content-Type: application/octet-stream\r\n\r\n").encode()
        post = f"\r\n--{b}--\r\n".encode()
        u = urllib.parse.urlsplit(self.c.base)
        conn = http.client.HTTPConnection(u.hostname, u.port or 80, timeout=900)
        t = time.perf_counter()
        sent = 0
        send_error = None
        try:
            conn.putrequest("POST", "/api/upload")
            conn.putheader("Authorization", self.c._auth)
            conn.putheader("Content-Type", f"multipart/form-data; boundary={b}")
            conn.putheader("Content-Length", str(len(pre) + size + len(post)))
            conn.endheaders()
            conn.send(pre)
            with open(path, "rb") as f:
                while chunk := f.read(MIB):
                    if self.stop.is_set():
                        conn.close()
                        return {"name": name, "status": "aborted_by_guard", "sent_mib": round(sent / MIB, 1),
                                "secs": round(time.perf_counter() - t, 2)}
                    conn.send(chunk)
                    sent += len(chunk)
            conn.send(post)
        except (BrokenPipeError, ConnectionResetError, OSError) as e:
            send_error = f"{type(e).__name__} after {sent / MIB:.1f} MiB sent"
        t_sent = time.perf_counter()
        try:
            r = conn.getresponse()
            body = r.read()
            status = r.status
        except Exception as e:  # no readable answer at all: recorded as such, a finding in itself
            conn.close()
            return {"name": name, "status": 0, "error": f"no response readable: {type(e).__name__}: {e}"[:200],
                    "send_error": send_error, "sent_mib": round(sent / MIB, 1), "secs": round(time.perf_counter() - t, 2)}
        conn.close()
        secs = time.perf_counter() - t
        res = {"name": name, "status": status, "bytes": size, "sent_mib": round(sent / MIB, 1),
               "secs": round(secs, 2), "server_after_send_s": round(time.perf_counter() - t_sent, 2),
               "mib_s": round(size / MIB / secs, 1) if status == 200 else None, "send_error": send_error}
        try:
            j = json.loads(body)
        except ValueError:
            j = None
        if status == 200 and isinstance(j, dict) and j.get("slug"):
            res["slug"] = j["slug"]
            self.o.rec("uploaded", slug=j["slug"], name=name, bytes=size)
        else:
            res["error"] = _err_text(body, status) if body else f"HTTP {status}, empty body"
            if isinstance(j, dict) and isinstance(j.get("error"), dict):
                res["error_details"] = j["error"].get("details")
        return res

    def scenario_upload(self, d):
        files = {p.name: p for p in Path(self.a.files).glob("lt732_*.img")}
        run_id = uuid.uuid4().hex[:6]
        plan = []
        for spec in self.a.upload_steps.split(","):
            label, conc = spec.split("x")
            fname = f"lt732_{label.replace('.', 'p')}MiB.img"
            if fname not in files:
                raise SystemExit(f"load: upload file {fname} missing in {self.a.files}; run genfiles first")
            plan.append((label, int(conc), files[fname]))
        for label, conc, path in plan:
            def run(label=label, conc=conc, path=path):
                with concurrent.futures.ThreadPoolExecutor(conc) as ex:
                    futs = [ex.submit(self.upload_one, path, f"lt732-{run_id}-{label}MiB-c{conc}-{i}.img")
                            for i in range(conc)]
                    results = [f.result() for f in futs]
                return {"results": results, "ok": sum(1 for r in results if r.get("status") == 200),
                        "max_secs": max(r.get("secs", 0) for r in results)}
            self.step("upload", f"{label} MiB x{conc}", run, size_mib=float(label), concurrency=conc)

    # -- scenario 4: OCR + caption backlog ----------------------------------------------------
    def scenario_backlog(self, d):
        pool = list(d["images"])
        random.shuffle(pool)
        if len(pool) < 2 * sum(int(x) for x in self.a.backlog_steps.split(",")):
            raise SystemExit(f"load: only {len(pool)} images on test; not enough distinct items for the backlog ramp")
        for n in [int(x) for x in self.a.backlog_steps.split(",")]:
            cap, ocr = pool[:n], pool[n:2 * n]
            del pool[:2 * n]

            def run(cap=cap, ocr=ocr, n=n):
                queued = {}
                refused = []
                for s in cap:
                    st, ms, _j, err = self.c.post_form(f"/api/image/{s}/caption", {"advance": "false"})
                    (refused.append({"slug": s, "kind": "caption", "status": st, "error": err}) if st != 200
                     else queued.__setitem__(("caption", s), now()))
                for s in ocr:
                    st, ms, _j, err = self.c.post_form(f"/api/image/{s}/ocr", {})
                    (refused.append({"slug": s, "kind": "ocr", "status": st, "error": err}) if st != 200
                     else queued.__setitem__(("ocr", s), now()))
                t_q = now()
                finished = {}
                deadline = t_q + max(self.a.backlog_timeout, 60 * n)
                while len(finished) < len(queued) and now() < deadline:
                    if self.stop.is_set():
                        break
                    for (kind, s), tq in queued.items():
                        if (kind, s) in finished:
                            continue
                        st, _ms, _n, err, body = self.c.get(f"/api/image/{s}")
                        if st != 200:
                            continue
                        row = json.loads(body)
                        state = ((row.get("type_metadata") or {}).get("auto_caption_status") if kind == "caption"
                                 else row.get("ocr_status"))
                        if state and state not in ("pending", "running", "processing", "queued"):
                            finished[(kind, s)] = (state, now() - tq)
                    time.sleep(2)
                out = {"queued": len(queued), "refused": refused}
                for kind in ("caption", "ocr"):
                    fin = {k: v for k, v in finished.items() if k[0] == kind}
                    q = sum(1 for k in queued if k[0] == kind)
                    secs = [v[1] for v in fin.values()]
                    states = {}
                    for v in fin.values():
                        states[v[0]] = states.get(v[0], 0) + 1
                    out[kind] = {"queued": q, "finished": len(fin), "states": states,
                                 "unfinished": [k[1] for k in queued if k[0] == kind and k not in finished],
                                 "drain_s": round(max(secs), 1) if secs else None,
                                 "per_item_s_p50": round(pct(secs, 50), 1) if secs else None,
                                 "items_per_min": round(len(fin) / (max(secs) / 60), 2) if secs and max(secs) else None}
                return out
            self.step("backlog", f"{n} caption + {n} OCR", run, n=n)


def summarize_requests(results, secs):
    oks = [r[2] for r in results if r[1] == 200]
    errs = [r for r in results if r[1] != 200]
    per_op = {}
    for op in sorted({r[0] for r in results}):
        rs = [r for r in results if r[0] == op]
        ms = [r[2] for r in rs if r[1] == 200]
        per_op[op] = {"n": len(rs), "err": len(rs) - len(ms), "p50": _r(pct(ms, 50)), "p95": _r(pct(ms, 95)),
                      "p99": _r(pct(ms, 99))}
    err_kinds = {}
    for r in errs:
        key = (r[4] or f"HTTP {r[1]}")[:120]
        err_kinds[key] = err_kinds.get(key, 0) + 1
    return {"requests": len(results), "errors": len(errs),
            "error_rate": round(len(errs) / len(results), 4) if results else None,
            "rps": round(len(results) / secs, 1), "p50_ms": _r(pct(oks, 50)), "p95_ms": _r(pct(oks, 95)),
            "p99_ms": _r(pct(oks, 99)), "max_ms": _r(max(oks, default=None)), "per_op": per_op,
            "error_kinds": err_kinds}


def _r(x):
    return None if x is None else round(x, 1)


def cmd_load(a):
    o = Out(a.out, "load")
    out = Path(a.out)
    t_wait = now() + 600
    while not (out / "READY").exists():
        if (out / "STOP").exists():
            raise SystemExit("load: the guard stopped before it was ready; no load sent")
        if now() > t_wait:
            raise SystemExit("load: the guard never wrote READY (is `guard` running on the host?); no load sent")
        time.sleep(1)
    drv = Driver(a, o)
    st, ms, _n, err, _b = drv.c.get("/api/auth/me")
    if st != 200:
        raise SystemExit(f"load: the install token was refused by {a.base_url} ({err}); no load sent")
    d = drv.discover()
    scen = {"browse": drv.scenario_browse, "mcp": drv.scenario_mcp, "upload": drv.scenario_upload,
            "backlog": drv.scenario_backlog}
    code = 0
    try:
        for name in a.scenarios.split(","):
            if name not in scen:
                raise SystemExit(f"load: unknown scenario {name!r} (have {', '.join(scen)})")
            o.rec("scenario_start", scenario=name)
            scen[name](d)
            o.rec("scenario_end", scenario=name)
    except Stopped:
        info = drv.stop_info()
        o.rec("run_stopped", stop=info)
        o.log(f"load: run stopped by guard: {info}")
        code = 3
    finally:
        (out / "LOAD_DONE").write_text(str(now()))
    return code


# --------------------------------------------------------------------------------------------
# cleanup (container)
# --------------------------------------------------------------------------------------------

def cmd_cleanup(a):
    o = Out(a.out, "cleanup")
    c = Client(a.base_url, a.mcp_url, read_token())
    slugs = []
    jl = Path(a.out) / "load.jsonl"
    if jl.exists():
        for line in jl.read_text().splitlines():
            r = json.loads(line)
            if r.get("kind") == "uploaded":
                slugs.append(r["slug"])
    # Belt and braces: anything named like a load-test upload, found by search, is ours too.
    st, _ms, _n, err, body = c.get("/api/search?query=lt732")
    if st == 200:
        found = json.loads(body)
        if isinstance(found, dict):
            found = found.get("results") or found.get("items") or []
        for r in found:
            fn = (r.get("filename") or r.get("display_name") or "")
            if fn.startswith("lt732-") and r.get("slug") not in slugs:
                slugs.append(r["slug"])
    else:
        o.log(f"cleanup: search for leftover lt732 uploads failed ({err}); relying on load.jsonl only")
    o.log(f"cleanup: {len(slugs)} uploaded test items to delete")
    failed = 0
    for i in range(0, len(slugs), 50):
        part = slugs[i:i + 50]
        st, _ms, j, err = c.post_form("/api/delete", {"slugs": part})
        o.rec("delete", status=st, n=len(part), answer=j, error=err)
        if st != 200:
            failed += 1
            o.log(f"cleanup: delete of {len(part)} items failed: {err}")
    if a.empty_trash:
        st, _ms, j, err = c.post_form("/api/trash/empty", {"confirm": "EMPTY TRASH"})
        o.rec("empty_trash", status=st, purged=(j or {}).get("purged"), bytes=(j or {}).get("bytes"),
              held_kept=(j or {}).get("held_kept"), error=err)
        o.log(f"cleanup: empty trash -> {st} purged={(j or {}).get('purged')} bytes={(j or {}).get('bytes')} "
              f"held_kept={(j or {}).get('held_kept')} {err or ''}")
        if st != 200:
            failed += 1
    left = []
    for s in slugs:
        st, _ms, _n, _err, _b = c.get(f"/api/image/{s}")
        if st != 404:
            left.append((s, st))
    o.rec("verify", remaining=left)
    o.log(f"cleanup: items still answering after delete: {left or 'none'}")
    return 2 if failed or left else 0


# --------------------------------------------------------------------------------------------
# summarize (anywhere)
# --------------------------------------------------------------------------------------------

def _load_jsonl(p):
    return [json.loads(l) for l in Path(p).read_text().splitlines() if l.strip()] if Path(p).exists() else []


def cmd_summarize(a):
    out = Path(a.out)
    load, guard = _load_jsonl(out / "load.jsonl"), _load_jsonl(out / "guard.jsonl")
    host = [g for g in guard if g["kind"] == "host"]
    prod = [g for g in guard if g["kind"] == "prod"]
    stats = [g for g in guard if g["kind"] == "stats"]

    def window(rows, t0, t1):
        return [r for r in rows if t0 <= r["t"] <= t1]

    def box(t0, t1):
        h = window(host, t0, t1)
        p = [r["ms"] for r in window(prod, t0, t1) if r["status"] == 200]
        s = window(stats, t0, t1)
        tmem = [r["stats"].get("constructicon-test", {}).get("mem_mib") for r in s]
        tmem = [m for m in tmem if m is not None]
        mmem = [r["stats"].get("constructicon-test-mcp", {}).get("mem_mib") for r in s]
        mmem = [m for m in mmem if m is not None]
        vram = [r["gpu"].get("vram_used_mib") for r in s if isinstance(r.get("gpu"), dict) and "vram_used_mib" in r["gpu"]]
        return {"min_avail_mib": round(min(r["mem_avail_kb"] for r in h) / 1024) if h else None,
                "min_avail_arc_mib": round(min(r["mem_avail_plus_arc_kb"] for r in h) / 1024) if h else None,
                "max_load1": max((r["load1"] for r in h), default=None),
                "prod_p95": _r(pct(p, 95)), "prod_max": _r(max(p, default=None)),
                "prod_fail": sum(1 for r in window(prod, t0, t1) if r["status"] != 200),
                "test_mem_max": _r(max(tmem, default=None)), "mcp_mem_max": _r(max(mmem, default=None)),
                "vram_max": max(vram, default=None)}

    lines = []
    steps = [r for r in load if r["kind"] == "step"]
    for scen in ("browse", "mcp", "upload", "backlog"):
        ss = [s for s in steps if s["scenario"] == scen]
        if not ss:
            continue
        lines.append(f"\n### {scen}\n")
        if scen in ("browse", "mcp"):
            lines.append("| step | req | rps | err | p50 ms | p95 ms | p99 ms | max ms | test /healthz p95 | test mem max MiB | MCP mem max MiB | host avail min MiB (+ARC) | load1 max | prod /healthz p95 / max / fails |")
            lines.append("|---|---|---|---|---|---|---|---|---|---|---|---|---|---|")
            for s in ss:
                b = box(s["t0"], s["t1"])
                lines.append(f"| {s['step']}{' (STOPPED)' if s['stopped'] else ''} | {s['requests']} | {s['rps']} | {s['errors']} | {s['p50_ms']} | {s['p95_ms']} | {s['p99_ms']} | {s['max_ms']} | {s['test_healthz']['p95_ms'] and round(s['test_healthz']['p95_ms'], 1)} | {b['test_mem_max']} | {b['mcp_mem_max']} | {b['min_avail_mib']} ({b['min_avail_arc_mib']}) | {b['max_load1']} | {b['prod_p95']} / {b['prod_max']} / {b['prod_fail']} |")
            lines.append("\nPer request type, p95 ms (errors):\n")
            ops = sorted({op for s in ss for op in s["per_op"]})
            lines.append("| step | " + " | ".join(ops) + " |")
            lines.append("|---|" + "---|" * len(ops))
            for s in ss:
                lines.append(f"| {s['step']} | " + " | ".join(
                    f"{s['per_op'].get(op, {}).get('p95')} ({s['per_op'].get(op, {}).get('err', 0)})" for op in ops) + " |")
            errs = {k: v for s in ss for k, v in s.get("error_kinds", {}).items()}
            if errs:
                lines.append("\nErrors seen: " + "; ".join(f"{k} x{v}" for k, v in errs.items()))
        elif scen == "upload":
            lines.append("| step | ok | slowest s | per-file results | test /healthz p95 / max ms | test mem max MiB | host avail min MiB (+ARC) | load1 max | prod /healthz p95 / max / fails |")
            lines.append("|---|---|---|---|---|---|---|---|---|")
            for s in ss:
                b = box(s["t0"], s["t1"])
                per = "; ".join(f"{r.get('status')} {r.get('secs')}s" + (f" {r.get('mib_s')} MiB/s" if r.get("mib_s") else "")
                                + (f" [{r.get('error')}]" if r.get("error") else "") + (f" [send: {r.get('send_error')}]" if r.get("send_error") else "")
                                for r in s["results"])
                th = s["test_healthz"]
                lines.append(f"| {s['step']}{' (STOPPED)' if s['stopped'] else ''} | {s['ok']}/{len(s['results'])} | {s['max_secs']} | {per} | {th['p95_ms'] and round(th['p95_ms'], 1)} / {th['max_ms'] and round(th['max_ms'], 1)} (fails {th['fail']}) | {b['test_mem_max']} | {b['min_avail_mib']} ({b['min_avail_arc_mib']}) | {b['max_load1']} | {b['prod_p95']} / {b['prod_max']} / {b['prod_fail']} |")
        else:
            lines.append("| step | caption: done/queued, states, drain s, items/min, p50 s/item | OCR: done/queued, states, drain s, items/min | refused | VRAM max MiB | host avail min MiB (+ARC) | load1 max | prod /healthz p95 / max / fails |")
            lines.append("|---|---|---|---|---|---|---|---|")
            for s in ss:
                b = box(s["t0"], s["t1"])
                c, k = s["caption"], s["ocr"]
                lines.append(f"| {s['step']}{' (STOPPED)' if s['stopped'] else ''} | {c['finished']}/{c['queued']} {c['states']} {c['drain_s']} s, {c['items_per_min']}/min, {c['per_item_s_p50']} s | {k['finished']}/{k['queued']} {k['states']} {k['drain_s']} s, {k['items_per_min']}/min | {len(s['refused'])} {[r['error'] for r in s['refused']][:2]} | {b['vram_max']} | {b['min_avail_mib']} ({b['min_avail_arc_mib']}) | {b['max_load1']} | {b['prod_p95']} / {b['prod_max']} / {b['prod_fail']} |")
    trips = [g for g in guard if g["kind"] == "guard_trip"]
    stops = [r for r in load if r["kind"] == "run_stopped"]
    lines.append("\n### Guard\n")
    lines.append("Trips: " + (json.dumps(trips) if trips else "none"))
    if stops:
        lines.append("\nLoad stopped: " + json.dumps(stops[-1]))
    print("\n".join(lines))
    return 0


# --------------------------------------------------------------------------------------------

def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    sub = p.add_subparsers(dest="cmd", required=True)
    g = sub.add_parser("genfiles")
    g.add_argument("--dir", required=True)
    g.add_argument("--sizes-mib", default="100,500,1000,1990,2000.5,2100")
    g = sub.add_parser("guard")
    g.add_argument("--out", required=True)
    g.add_argument("--prod-url", default="http://10.0.1.240")
    g.add_argument("--baseline-samples", type=int, default=60)
    g.add_argument("--prod-factor", type=float, default=3.0)
    g.add_argument("--prod-floor-ms", type=float, default=0.0,
                   help="limit = max(factor x baseline p95, baseline p95 + this); 0 = the factor alone")
    g.add_argument("--mem-floor-kb", type=int, default=1_000_000)
    g.add_argument("--load-max", type=float, default=8.0)
    g.add_argument("--load-secs", type=float, default=30.0)
    g.add_argument("--settle-secs", type=int, default=120)
    for name in ("load", "cleanup"):
        g = sub.add_parser(name)
        g.add_argument("--out", required=True)
        g.add_argument("--base-url", default="http://localhost:80")
        g.add_argument("--mcp-url", default="http://10.0.1.242:8100/mcp")
        if name == "load":
            g.add_argument("--files", default="/files")
            g.add_argument("--scenarios", default="browse,mcp,upload,backlog")
            g.add_argument("--browse-steps", default="2,4,8,16,32")
            g.add_argument("--browse-secs", type=float, default=40)
            g.add_argument("--mcp-steps", default="1,2,4,8,16")
            g.add_argument("--mcp-secs", type=float, default=30)
            g.add_argument("--upload-steps",
                           default="100x1,100x2,100x3,500x1,500x2,500x3,1000x1,1000x2,1000x3,1990x1,1990x2,1990x3,2000.5x1,2100x1")
            g.add_argument("--backlog-steps", default="2,5,10,20")
            g.add_argument("--backlog-timeout", type=float, default=300)
        else:
            g.add_argument("--empty-trash", action="store_true")
    g = sub.add_parser("summarize")
    g.add_argument("--out", required=True)
    a = p.parse_args(argv)
    fn = {"genfiles": cmd_genfiles, "guard": cmd_guard, "load": cmd_load, "cleanup": cmd_cleanup,
          "summarize": cmd_summarize}[a.cmd]
    try:
        return fn(a)
    except SystemExit:
        raise
    except Exception:
        print(f"load_test_732 {a.cmd}: FAILED\n{traceback.format_exc()}", file=sys.stderr, flush=True)
        return 2


if __name__ == "__main__":
    sys.exit(main())
