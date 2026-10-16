"""Ollama sidecar shape (#687): docker-compose.yml.example's `ollama` stays pinned, optional and unexposed.

    python scripts/check_compose_ollama.py

Static (PyYAML, which the app image has; no Docker, no network), so it runs in the pre-merge suite.
It fails when the example's ollama service:
  - is not pinned to `ollama/ollama:<tag>@sha256:<digest>` (a bare tag, `:latest` or a digest alone);
  - publishes a port (it must be reachable only over the internal `captions` network);
  - is not behind the `captions` profile (a plain `docker compose up` must not start it);
  - is not named `ollama`, or the restart sidecar's RESTARTER_CONTAINER names something else;
  - carries a GPU reservation (CPU must work as shipped; the GPU lives in docker-compose.gpu.yml);
  - lost its first-start pull (the entrypoint script it mounts must exist, and read the same
    CAPTION_OLLAMA_MODEL the web service passes to the app);
and when the web service doesn't reach it as http://ollama:11434 over `captions`, or `captions`
is not an internal network. Live behaviour (the pull, reachability) was checked on the NAS in #687's PR.
Ends with `ALL PASS` or `FAILED: <names>`.
"""

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

try:
    import yaml
except ImportError:  # outside the app image (a bare Windows Python)
    print("SKIPPED: PyYAML is not installed here (it is in the app image)")
    sys.exit(0)

FAILS = []


def check(name, cond, extra=""):
    print(("PASS " if cond else "FAIL ") + name + (f"  [{extra}]" if (extra and not cond) else ""))
    if not cond:
        FAILS.append(name)


def env_map(service):
    env = service.get("environment") or {}
    if isinstance(env, dict):
        return {k: str(v) for k, v in env.items()}
    out = {}
    for line in env:
        key, _, value = str(line).partition("=")
        out[key] = value
    return out


def net_names(service):
    nets = service.get("networks") or []
    return list(nets) if isinstance(nets, (list, dict)) else []


example = yaml.safe_load((ROOT / "docker-compose.yml.example").read_text(encoding="utf-8"))
services = example.get("services") or {}
networks = example.get("networks") or {}
oll = services.get("ollama")
check("example defines an ollama service", isinstance(oll, dict))
oll = oll or {}

image = str(oll.get("image", ""))
check("ollama image is tag@sha256 pinned",
      re.fullmatch(r"ollama/ollama:[0-9][0-9A-Za-z.\-]*@sha256:[0-9a-f]{64}", image) is not None, image)
check("ollama image is not :latest", ":latest" not in image, image)
check("ollama publishes no host port", not oll.get("ports") and not oll.get("network_mode"),
      str(oll.get("ports") or oll.get("network_mode")))
check("ollama is behind the captions profile", "captions" in (oll.get("profiles") or []),
      str(oll.get("profiles")))
check("ollama container_name is ollama", oll.get("container_name") == "ollama", str(oll.get("container_name")))
check("ollama has no GPU reservation in the example (CPU works as shipped)", "deploy" not in oll)
check("ollama restarts unless stopped", oll.get("restart") == "unless-stopped", str(oll.get("restart")))

oll_nets = net_names(oll)
check("ollama is on the captions network", "captions" in oll_nets, str(oll_nets))
check("ollama is not on the default network", "default" not in oll_nets, str(oll_nets))
check("captions network is internal", (networks.get("captions") or {}).get("internal") is True,
      str(networks.get("captions")))

vols = [str(v) for v in oll.get("volumes") or []]
check("ollama keeps models on a volume at /root/.ollama", any(v.split(":")[-1] == "/root/.ollama" or
      ":/root/.ollama" in v for v in vols), str(vols))
entry = " ".join(str(x) for x in (oll.get("entrypoint") or []))
script_mounts = [v for v in vols if v.startswith("./ops/ollama/")]
check("ollama entrypoint runs the mounted serve-and-pull script",
      bool(script_mounts) and script_mounts[0].split(":")[1] in entry, f"{entry} / {script_mounts}")
check("serve-and-pull script exists in the repo", (ROOT / "ops/ollama/serve-and-pull.sh").is_file())
script = (ROOT / "ops/ollama/serve-and-pull.sh").read_text(encoding="utf-8") if \
    (ROOT / "ops/ollama/serve-and-pull.sh").is_file() else ""
check("serve-and-pull reads CAPTION_OLLAMA_MODEL", "CAPTION_OLLAMA_MODEL" in script)

oll_env = env_map(oll)
web = services.get("constructicon-web") or {}
web_env = env_map(web)
check("web reaches the sidecar at http://ollama:11434", web_env.get("CAPTION_OLLAMA_URL") == "http://ollama:11434",
      str(web_env.get("CAPTION_OLLAMA_URL")))
check("web is on the captions network", "captions" in net_names(web), str(net_names(web)))
check("web and the sidecar read the same model setting",
      web_env.get("CAPTION_OLLAMA_MODEL") == oll_env.get("CAPTION_OLLAMA_MODEL") and
      "CAPTION_OLLAMA_MODEL" in str(oll_env.get("CAPTION_OLLAMA_MODEL")),
      f"{web_env.get('CAPTION_OLLAMA_MODEL')} vs {oll_env.get('CAPTION_OLLAMA_MODEL')}")

restarter = services.get("ollama-restarter") or {}
check("restart sidecar targets the ollama container",
      env_map(restarter).get("RESTARTER_CONTAINER") == oll.get("container_name"),
      str(env_map(restarter).get("RESTARTER_CONTAINER")))

gpu_path = ROOT / "docker-compose.gpu.yml"
gpu = yaml.safe_load(gpu_path.read_text(encoding="utf-8")) if gpu_path.is_file() else {}
devices = (((((gpu.get("services") or {}).get("ollama") or {}).get("deploy") or {})
            .get("resources") or {}).get("reservations") or {}).get("devices") or []
check("docker-compose.gpu.yml reserves an NVIDIA GPU for ollama",
      any(d.get("driver") == "nvidia" for d in devices), str(devices))

print("\n%s" % ("ALL PASS" if not FAILS else "FAILED: " + ", ".join(FAILS)))
