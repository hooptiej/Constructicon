#!/usr/bin/env python3
"""#658: the embedding model loads from the local Hugging Face cache and never asks the Hub.

Before the fix, core/similarity.py's SentenceTransformer(name) asked huggingface.co for the latest files
on every load, even with the model baked into the image: 58 lookups and 144 s of retries in a
--network none container, and one hung connection stalled a live upload's OCR.

Each load runs in a child process (huggingface_hub reads HF_HUB_OFFLINE once, at import) with every
DNS lookup and socket connect recorded, and HF_HUB_OFFLINE / TRANSFORMERS_OFFLINE REMOVED from its
environment, so the module's own default is what is tested, not the image's ENV:
  * cached model: loads and embeds, with zero network attempts, in well under the old stall;
  * empty cache: fails fast with the download hint, also with zero network attempts;
  * HF_HUB_OFFLINE=0 is the one, explicit, way to go online (checked without loading anything);
  * the Dockerfile sets the offline variables, after the build step that bakes the model in.

    python scripts/test_hf_offline_658.py

Needs the app image's packages (sentence-transformers, torch) and its baked cache; without
sentence-transformers it says so and skips. No network, no DB.
"""
import json
import os
import subprocess
import sys
import tempfile
import time

import _testenv  # noqa: E402  (scripts/_testenv.py: temp DB + storage + exports; the children inherit it)
TMP = _testenv.isolate("hf658-")
ROOT = os.path.abspath(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
sys.path.insert(0, ROOT)

FAILS = []


def check(name, cond, extra=""):
    print(("PASS " if cond else "FAIL ") + name + (f"  [{extra}]" if (extra and not cond) else ""))
    if not cond:
        FAILS.append(name)


try:
    import importlib.util
    have_st = importlib.util.find_spec("sentence_transformers") is not None
except Exception:  # silent-ok: a broken install is reported as "not available" just below
    have_st = False
if not have_st:
    print("SKIPPED: sentence-transformers is not installed here; run this in the app image")
    sys.exit(0)

CHILD = r"""
import json, socket, sys, time
attempts = []
_gai, _connect = socket.getaddrinfo, socket.socket.connect
def gai(host, *a, **k):
    attempts.append("getaddrinfo %s" % (host,))
    return _gai(host, *a, **k)
def connect(self, addr):
    attempts.append("connect %s" % (addr,))
    return _connect(self, addr)
socket.getaddrinfo, socket.socket.connect = gai, connect
sys.path.insert(0, sys.argv[1])
out = {}
t = time.time()
try:
    from core import similarity
    vec = similarity.compute_embedding("hello world")
    out["ok"] = True
    out["bytes"] = len(vec)
except Exception as e:
    out["ok"] = False
    out["error"] = "%s: %s" % (type(e).__name__, e)
out["seconds"] = time.time() - t
out["attempts"] = attempts
import os
out["env_offline"] = os.environ.get("HF_HUB_OFFLINE")
print("CHILD-RESULT " + json.dumps(out))
"""


def run_child(extra_env=None):
    env = {k: v for k, v in os.environ.items() if k not in ("HF_HUB_OFFLINE", "TRANSFORMERS_OFFLINE")}
    env.update(extra_env or {})
    t = time.time()
    p = subprocess.run([sys.executable, "-c", CHILD, ROOT], capture_output=True, text=True, env=env, timeout=600)
    wall = time.time() - t
    for line in p.stdout.splitlines():
        if line.startswith("CHILD-RESULT "):
            res = json.loads(line[len("CHILD-RESULT "):])
            res["wall"] = wall
            return res
    return {"ok": False, "error": "child printed no result: " + (p.stdout + p.stderr)[-800:], "attempts": [],
            "seconds": wall, "wall": wall}


# 1. The cached model (the image's HF_HOME) loads with no network at all.
cached = run_child()
print(f"   cached load: ok={cached['ok']} in {cached['seconds']:.2f}s (process {cached['wall']:.2f}s), "
      f"{len(cached['attempts'])} network attempt(s)")
if not cached["ok"] and "not in the local Hugging Face cache" in cached.get("error", ""):
    print("SKIPPED: the embedding model is not cached here (HF_HOME=%s); run this in the app image"
          % os.environ.get("HF_HOME", "~/.cache/huggingface"))
    sys.exit(0)
check("cached model loads and embeds", cached["ok"] and cached.get("bytes") == 384 * 4, cached.get("error", ""))
check("cached load makes no network attempt", cached["attempts"] == [], "; ".join(cached["attempts"][:5]))
check("cached load is quick (no Hub retries)", cached["seconds"] < 60, f"{cached['seconds']:.1f}s")
check("unset HF_HUB_OFFLINE is written back as 1 before the Hub is imported", cached.get("env_offline") == "1",
      repr(cached.get("env_offline")))

# 2. An empty cache fails fast, says how to download, and still never reaches out.
with tempfile.TemporaryDirectory(prefix="hf658-empty-") as empty:
    miss = run_child({"HF_HOME": empty})
print(f"   empty cache: ok={miss['ok']} in {miss['seconds']:.2f}s, {len(miss['attempts'])} network attempt(s)")
check("empty cache refuses (no silent download)", not miss["ok"])
check("empty cache error names the model and the download command",
      "all-MiniLM-L6-v2" in miss.get("error", "") and "HF_HUB_OFFLINE=0" in miss.get("error", ""),
      miss.get("error", "")[:300])
check("empty cache makes no network attempt", miss["attempts"] == [], "; ".join(miss["attempts"][:5]))
check("empty cache fails fast", miss["seconds"] < 60, f"{miss['seconds']:.1f}s")

# 3. The switch itself (no model load): only an explicit false value goes online.
from core import similarity  # noqa: E402
saved = os.environ.pop("HF_HUB_OFFLINE", None)
try:
    check("unset -> offline", similarity._offline() is True)
    check("unset -> environment now says 1", os.environ.get("HF_HUB_OFFLINE") == "1")
    for v in ("0", "false", "No", "off"):
        os.environ["HF_HUB_OFFLINE"] = v
        check(f"HF_HUB_OFFLINE={v} -> online (the explicit download path)", similarity._offline() is False)
    for v in ("1", "true", "YES"):
        os.environ["HF_HUB_OFFLINE"] = v
        check(f"HF_HUB_OFFLINE={v} -> offline", similarity._offline() is True)
finally:
    os.environ.pop("HF_HUB_OFFLINE", None)
    if saved is not None:
        os.environ["HF_HUB_OFFLINE"] = saved

# 4. The image: offline at run time, set after the one step that downloads.
with open(os.path.join(ROOT, "Dockerfile"), encoding="utf-8") as f:
    dockerfile = f.read()
bake = dockerfile.find("SentenceTransformer('all-MiniLM-L6-v2')")
env_at = dockerfile.find("ENV HF_HUB_OFFLINE=1")
check("Dockerfile bakes the model in", bake != -1)
check("Dockerfile sets HF_HUB_OFFLINE=1 after the bake", env_at != -1 and env_at > bake)
check("Dockerfile sets TRANSFORMERS_OFFLINE=1 too", "TRANSFORMERS_OFFLINE=1" in dockerfile[env_at:] if env_at != -1 else False)

print("\n%s" % ("ALL PASS" if not FAILS else "FAILED: " + ", ".join(FAILS)))
sys.exit(1 if FAILS else 0)
