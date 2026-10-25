#!/usr/bin/env bash
# Issue #71: one-command deploy for a checkout on the TrueNAS box
# (constructicon or constructicon-test). Requires the checkout's `origin`
# remote to already be a working SSH deploy-key remote -- see CLAUDE.md's
# Deployment section for how that's set up; this script doesn't configure
# credentials itself, only uses them.
#
# #698: any Docker host, not only the NAS. Needs: bash, git, python3, Docker with the Compose v2
# plugin (`docker compose`; required, there is no no-compose mode). `sudo` is used only when this
# user can't reach the Docker socket itself (a user in the `docker` group, or root, runs without it).
# ZFS is optional: see the pre-deploy backup below. README.md "Install on a new host" is the guide.
#
# Usage (run from inside the checkout, or pass its path):
#   ./scripts/deploy.sh                  # fetch + reset to origin/main, then (see the two modes below)
#   ./scripts/deploy.sh --build          # mounted mode: also rebuild the image (requirements.txt /
#                                        # Dockerfile changed). Baked mode always builds; accepted there.
#   ./scripts/deploy.sh --no-snapshot    # skip the pre-deploy backup (ZFS snapshot or db/ copy)
#   ./scripts/deploy.sh --backup-plan    # print which pre-deploy backup a deploy would take; change nothing
#   ./scripts/deploy.sh --rollback VER   # run the image built for VER again (no git, no build)
#   ./scripts/deploy.sh --image REF      # #736: pull a published image (e.g. ghcr.io/x/constructicon:2026.10.25)
#                                        # and run it (no git, no build; works on a host without the repo)
#   ./scripts/deploy.sh --write-version-only   # just write core/VERSION.json (mounted mode)
#
# #688: two modes, picked from the checkout's EFFECTIVE compose config (docker-compose.yml plus
# docker-compose.override.yml when present):
# - BAKED (prod: the web service has no code bind mounts): the code lives in the image. Build the
#   image with the version stamped in (build args from `git describe`), tag it <repo>:<version> as
#   well as the compose file's own tag (e.g. constructicon:latest, the moving tag), then
#   `docker compose up -d`, which recreates every service on that image (web + mcp). Keeps the newest
#   $IMAGE_KEEP version tags. Rollback = `--rollback <previous version>`: retag and `up -d`.
# - MOUNTED (constructicon-test, dev: an override bind-mounts core/ etc. over the baked copy): the
#   fast loop as before. Write core/VERSION.json into the checkout (the mount hides the baked one),
#   then `docker compose restart`; with --build, build the versioned image first and `up -d` too.
#
# #508: releases are cut on the dev machine (scripts/release.py -- the box's
# deploy key is read-only, so it can't push tags). This script fetches the tags
# and uses `git describe --tags --always` as the version. The dev instance
# (CONSTRUCTICON_ENV=dev) gets a "-dev" suffix, added by core/version.py at run
# time. The snapshot name carries the version that was running before the deploy.
#
# #442: before touching anything, this snapshots the ZFS dataset holding the
# instance's data (DB + storage), if that data lives in a DEDICATED dataset
# (prod: "Storage Pool/Media/constructicon"). Instant, atomic (DB, WAL and
# files captured at the same moment), and it only stores what changes after
# it. That replaces the old routine "/api/backup zip + cp -r code" before
# every deploy. Keeps the newest $SNAPSHOT_KEEP "@predeploy-*" snapshots.
# Restore: browse read-only copies under <dataset mountpoint>/.zfs/snapshot/<name>/
# (e.g. copy the DB back), or `zfs rollback` (destroys everything newer --
# owner call). Code rollback: baked mode = --rollback <version>; mounted mode = git
# (the snapshot name carries the commit that was running before the deploy).
#
# #698: everywhere else (no ZFS on the host, the data not in a dataset of its own, or no way to run
# `zfs` as root), the pre-deploy backup is a COPY OF THE DB FOLDER: every SQLite file in it copied
# with SQLite's online backup API (consistent while the app runs; never a raw copy of a live WAL DB),
# other files copied as they are, -wal/-shm/-journal skipped. It runs in a one-off container of the
# app image (no network, as the data folder's owner uid), into <instance>/predeploy-backups/<name>/
# (0700, files 0600), and keeps the newest $SNAPSHOT_KEEP. storage/ is NOT copied (too big; keep
# your own file backups). Restore: stop the app, copy the files back into the db folder, start it.
# The dev/test instance (web service CONSTRUCTICON_ENV=dev) takes NO pre-deploy backup at all
# (owner decision 2026-10-17); every other install takes one of the two above.
#
# #741: every compose service with a `build:` section is built on every deploy, not just web (the
# ollama-restarter sidecar used to keep its old image forever). Web gets the version build args;
# the others are built plainly, and `up -d` recreates whichever image changed.
#
# #740: it works in the folder ABOVE its own scripts/ folder. Run from anywhere else (a copy in
# /tmp), or with a broken compose file, it stops with what it tried, what happened and the fix,
# never a Python traceback. Note (#442): a change to this script takes effect on the deploy AFTER
# the one that installs it (the running copy is the one doing the git reset).

set -euo pipefail
cd "$(dirname "$0")/.."

SNAPSHOT_KEEP=10
IMAGE_KEEP=5
BUILD=""
SNAPSHOT=1
WRITE_VERSION_ONLY=""
BACKUP_PLAN=""
ROLLBACK=""
IMAGE_REF=""
while [ $# -gt 0 ]; do
  case "$1" in
    --build) BUILD=1 ;;
    --no-snapshot|--no-backup) SNAPSHOT="" ;;
    --backup-plan) BACKUP_PLAN=1 ;;
    --write-version-only) WRITE_VERSION_ONLY=1 ;;
    --rollback)
      shift
      ROLLBACK="${1:-}"
      if [ -z "$ROLLBACK" ]; then echo "--rollback needs a version (see: docker image ls)" >&2; exit 2; fi ;;
    --image)
      shift
      IMAGE_REF="${1:-}"
      if [ -z "$IMAGE_REF" ]; then echo "--image needs an image reference, e.g. --image ghcr.io/you/constructicon:2026.10.25" >&2; exit 2; fi ;;
    *) echo "Unknown option: $1" >&2; exit 2 ;;
  esac
  shift
done

# #698: how to reach Docker. DOCKER is `docker` when this user can use the socket (the `docker`
# group, or root), else `sudo docker`; SUDO is how to run a root-only command (zfs): empty as root,
# else `sudo` when it exists. Compose v2 is required; a missing piece stops here with the fix.
DOCKER=(docker)
SUDO=()
detect_docker() {
  local err
  if ! command -v docker >/dev/null 2>&1; then
    echo "deploy.sh: 'docker' isn't installed (or isn't on PATH). Install Docker Engine with the Compose" >&2
    echo "plugin (https://docs.docker.com/engine/install/), then run this again." >&2
    exit 1
  fi
  if [ "$(id -u)" != 0 ] && command -v sudo >/dev/null 2>&1; then SUDO=(sudo); fi
  if err="$(docker info 2>&1 >/dev/null)"; then
    DOCKER=(docker)
  elif [ "$(id -u)" = 0 ]; then
    echo "deploy.sh: can't reach the Docker daemon as root: $err" >&2
    echo "Start Docker (e.g. systemctl start docker) and run this again." >&2
    exit 1
  elif [ "${#SUDO[@]}" -gt 0 ]; then
    if ! err="$(sudo docker info 2>&1 >/dev/null)"; then
      echo "deploy.sh: can't reach the Docker daemon, not as $(id -un) and not with sudo: $err" >&2
      echo "Start Docker, or check this user's sudo rights, and run this again." >&2
      exit 1
    fi
    DOCKER=(sudo docker)
    echo "Docker: using 'sudo docker' ($(id -un) can't use the Docker socket directly)."
  else
    echo "deploy.sh: $(id -un) can't reach the Docker daemon ($err) and sudo isn't installed." >&2
    echo "Add this user to the 'docker' group (then log out and back in), or run deploy.sh as root." >&2
    exit 1
  fi
  if ! err="$("${DOCKER[@]}" compose version 2>&1)"; then
    echo "deploy.sh needs Docker Compose v2 ('docker compose'), and '${DOCKER[*]} compose version' failed:" >&2
    echo "  $err" >&2
    echo "Install the Compose plugin (Debian/Ubuntu: apt install docker-compose-plugin; others:" >&2
    echo "https://docs.docker.com/compose/install/linux/), then run this again. The old standalone" >&2
    echo "'docker-compose' (v1) is not supported." >&2
    exit 1
  fi
}

# #688: one read of the effective compose config. Sets WEB_SVC (the app's web service: "web" on
# the NAS, "constructicon-web" in the example), WEB_IMAGE (its image: tag), CODE_MOUNTED (1 when
# /app/core is bind-mounted), DATA_SRC (host path mounted at /app/data), WEB_ENV (the web
# service's CONSTRUCTICON_ENV, "dev" on the test/dev instance, #310) and, #741, BUILD_SVCS (every
# OTHER service with a build: section, space-separated, e.g. the ollama-restarter).
# #740: each way this can fail says what was tried, what happened and what to do; no traceback.
READ_COMPOSE_PY='import json, shlex, sys
raw = sys.stdin.read()
try:
    c = json.loads(raw).get("services", {})
except ValueError as exc:
    shown = raw.strip()[:300] or "(nothing at all)"
    sys.exit(f"deploy.sh: docker compose config printed something that is not JSON ({exc}). It printed: {shown}")
name = next((n for n in ("web", "constructicon-web") if n in c), "")
if not name:
    name = next((n for n, s in c.items() if "web.app:app" in " ".join(s.get("command") or [])), "")
s = c.get(name, {})
vols = s.get("volumes", [])
env = s.get("environment") or {}
if isinstance(env, list):
    env = dict(e.split("=", 1) if "=" in e else (e, "") for e in env)
out = {
    "WEB_SVC": name,
    "WEB_IMAGE": s.get("image", ""),
    "CODE_MOUNTED": "1" if any(v.get("target") == "/app/core" for v in vols) else "",
    "DATA_SRC": next((v.get("source", "") for v in vols if v.get("target") == "/app/data"), ""),
    "WEB_ENV": str(env.get("CONSTRUCTICON_ENV") or "").strip().lower(),
    "BUILD_SVCS": " ".join(sorted(n for n, svc in c.items() if svc.get("build") and n != name)),
    "ALL_SVCS": ", ".join(sorted(c)) or "(none)",
}
for k, v in out.items():
    print(f"{k}={shlex.quote(v)}")'

read_compose() {
  local f found="" errfile cfg parsed
  # #740 check 1: a compose file must be here (unless COMPOSE_FILE names one).
  if [ -z "${COMPOSE_FILE:-}" ]; then
    for f in compose.yaml compose.yml docker-compose.yaml docker-compose.yml; do
      if [ -f "$f" ]; then found="$f"; break; fi
    done
    if [ -z "$found" ]; then
      echo "deploy.sh: no docker-compose.yml in $PWD." >&2
      echo "  It works in the folder above its own scripts/ folder (this copy is $0), so a copy run from" >&2
      echo "  anywhere else looks in the wrong place. Nothing was changed." >&2
      echo "  Run it as ./scripts/deploy.sh from inside the instance's checkout (the folder holding" >&2
      echo "  docker-compose.yml)." >&2
      exit 1
    fi
  fi
  # #740 check 2: compose must accept the file; show ITS error, not a JSON traceback.
  errfile="$(mktemp)"
  if ! cfg="$("${DOCKER[@]}" compose config --format json 2>"$errfile")"; then
    echo "deploy.sh: '${DOCKER[*]} compose config' failed in $PWD, so there is nothing to deploy. It said:" >&2
    sed 's/^/  /' "$errfile" >&2
    rm -f "$errfile"
    echo "  Nothing was changed. Fix the compose file (or the .env it reads), then run this again." >&2
    exit 1
  fi
  rm -f "$errfile"
  if [ -z "$cfg" ]; then
    echo "deploy.sh: '${DOCKER[*]} compose config' printed nothing in $PWD. Nothing was changed." >&2
    echo "  Check that ${found:-the files in COMPOSE_FILE} define services, then run this again." >&2
    exit 1
  fi
  if ! parsed="$(printf '%s' "$cfg" | python3 -c "$READ_COMPOSE_PY")"; then
    echo "  Nothing was changed. Run '${DOCKER[*]} compose config' yourself to see the full output." >&2
    exit 1
  fi
  eval "$parsed"
  # #740 check 3: say which services there are, never "WEB_SVC: unbound variable".
  if [ -z "$WEB_SVC" ]; then
    echo "deploy.sh: can't find the web service in $PWD's compose config. Services found: $ALL_SVCS." >&2
    echo "  It looks for a service named 'web' or 'constructicon-web', or one whose command runs" >&2
    echo "  web.app:app. Nothing was changed. Rename the service, or check you're in the right checkout." >&2
    exit 1
  fi
}

# #698: which pre-deploy backup this host can take. Sets BACKUP_KIND (skip | zfs | copy | none),
# BACKUP_WHY (why, in words) and, for zfs, ZFS_DS (the dataset).
plan_backup() {
  local src="$DATA_SRC" inst fstype mnt
  BACKUP_KIND="" BACKUP_WHY="" ZFS_DS=""
  # Owner decision 2026-10-17: the dev/test instance never takes a pre-deploy backup (as before
  # #698); every other install does, ZFS or not. The signal is the web service's
  # CONSTRUCTICON_ENV=dev, the app's own "this is the dev instance" marker (the DEV- tab title,
  # the -dev version suffix). Not the code mounts: an override can be dropped on test for a baked
  # check (still test), and nothing stops another install from using one (not test).
  if [ "$WEB_ENV" = dev ]; then
    BACKUP_KIND=skip BACKUP_WHY="dev/test instance: CONSTRUCTICON_ENV=dev; owner decision 2026-10-17"
    return 0
  fi
  if [ -z "$src" ]; then
    BACKUP_KIND=none BACKUP_WHY="the web service has no /app/data mount"
    return 0
  fi
  inst="$(dirname "$src")"   # e.g. /mnt/Storage Pool/Media/constructicon
  # GNU df only (Linux); anything else (macOS, busybox) reads as "not ZFS" and gets the copy.
  fstype="$(df --output=fstype "$inst" 2>/dev/null | tail -1 | sed 's/[[:space:]]*$//' || true)"
  if [ "$fstype" != "zfs" ]; then
    BACKUP_KIND=copy BACKUP_WHY="$inst is not on ZFS (filesystem: ${fstype:-unknown})"
    return 0
  fi
  ZFS_DS="$(df --output=source "$inst" | tail -1 | sed 's/[[:space:]]*$//')"
  mnt="$(df --output=target "$inst" | tail -1 | sed 's/[[:space:]]*$//')"
  if [ "$mnt" != "$inst" ]; then
    BACKUP_KIND=copy BACKUP_WHY="$inst is inside the shared dataset '$ZFS_DS' (mounted at $mnt), not its own"
    return 0
  fi
  if [ "$(id -u)" != 0 ] && [ "${#SUDO[@]}" -eq 0 ]; then
    BACKUP_KIND=copy BACKUP_WHY="$inst is its own ZFS dataset, but there's no sudo to run zfs as root"
    return 0
  fi
  BACKUP_KIND=zfs BACKUP_WHY="$inst is its own ZFS dataset ('$ZFS_DS')"
}

# #508: the version being replaced rides along with the commit (describe is
# sanitised to ZFS-legal characters; "unversioned" if there are no tags yet).
backup_name() {
  local ver
  ver="$(git describe --tags --always 2>/dev/null | tr -c 'A-Za-z0-9._:\n-' '_' | tr -d '\n' || true)"
  printf 'predeploy-%s-%s-v%s' "$(date +%Y%m%d-%H%M%S)" "$(git rev-parse --short HEAD 2>/dev/null || echo nogit)" "${ver:-unversioned}"
}

snapshot_before_deploy() {
  local name
  plan_backup
  case "$BACKUP_KIND" in
    skip)
      echo "Pre-deploy backup: skipped ($BACKUP_WHY)."
      return 0 ;;
    none)
      echo "Pre-deploy backup: none ($BACKUP_WHY); skipping."
      return 0 ;;
    copy)
      backup_db_copy
      return 0 ;;
  esac
  name="$(backup_name)"
  # ${SUDO[@]+...}: an empty array under `set -u` is an error in bash < 4.4 (macOS's bash 3.2).
  ${SUDO[@]+"${SUDO[@]}"} zfs snapshot "$ZFS_DS@$name"
  echo "Snapshot: $ZFS_DS@$name"
  echo "Pre-deploy backup: ZFS snapshot ($BACKUP_WHY)."
  # Retention: only ever touches @predeploy-* snapshots of this dataset.
  ${SUDO[@]+"${SUDO[@]}"} zfs list -H -t snapshot -o name -s creation -d 1 "$ZFS_DS" \
    | grep "@predeploy-" | head -n "-$SNAPSHOT_KEEP" \
    | while IFS= read -r old; do ${SUDO[@]+"${SUDO[@]}"} zfs destroy "$old" && echo "Snapshot pruned: $old"; done
}

# #698: the no-ZFS backup, a consistent copy of the DB folder (see the header). Any failure stops
# the deploy (set -e), like a failed snapshot; --no-snapshot deploys without a backup.
backup_db_copy() {
  local src="$DATA_SRC" inst owner name listing
  inst="$(dirname "$src")"
  if [ ! -d "$src" ]; then
    echo "Pre-deploy backup: $src doesn't exist yet (first deploy?), so there's no database to back up."
    return 0
  fi
  # Empty = nothing to back up. (A folder this user can't list is NOT taken as empty: the copy runs
  # as root in a container and reads it anyway.)
  if listing="$(ls -A "$src" 2>/dev/null)" && [ -z "$listing" ]; then
    echo "Pre-deploy backup: $src is empty (first deploy?), so there's no database to back up."
    return 0
  fi
  require_image_name
  if ! "${DOCKER[@]}" image inspect "$WEB_IMAGE" >/dev/null 2>&1; then
    echo "Pre-deploy backup: can't copy $src: the copy runs in the app image, and $WEB_IMAGE isn't on" >&2
    echo "this host yet. Build or pull it first, or deploy without a backup: --no-snapshot." >&2
    exit 1
  fi
  owner="$(stat -c '%u:%g' "$src" 2>/dev/null || stat -f '%u:%g' "$src")"
  name="$(backup_name)"
  echo "Pre-deploy backup: copying the database folder ($BACKUP_WHY, so no ZFS snapshot)..."
  "${DOCKER[@]}" run --rm --network none --user 0 --entrypoint python3 \
    -v "$src":/src -v "$inst":/inst "$WEB_IMAGE" -c "$DB_COPY_PY" \
    /src /inst/predeploy-backups "$name" "$owner" "$SNAPSHOT_KEEP"
  echo "Pre-deploy backup: db/ copy (SQLite backup API) at $inst/predeploy-backups/$name"
  echo "  Restore: stop the app, copy those files back into $src, start it. storage/ is not in it."
}

# Runs in the one-off container as root: makes the backup folder, then drops to the data folder's
# owner before it opens anything, so nothing in db/ (a -shm the backup creates) ends up root-owned.
DB_COPY_PY='import os, shutil, sqlite3, sys
src, root, name, owner, keep = sys.argv[1], sys.argv[2], sys.argv[3], sys.argv[4], int(sys.argv[5])
uid, gid = (int(x) for x in owner.split(":"))
os.makedirs(root, exist_ok=True)
os.chown(root, uid, gid)
os.chmod(root, 0o700)
os.setgroups([])
os.setgid(gid)
os.setuid(uid)
os.umask(0o077)
dest = os.path.join(root, name)
os.mkdir(dest)
for entry in sorted(os.listdir(src)):
    path = os.path.join(src, entry)
    if not os.path.isfile(path):
        print(f"  skipped {entry}/ (not a file)")
        continue
    if entry.endswith(("-wal", "-shm", "-journal")):
        continue  # folded into the SQLite copy of their database
    with open(path, "rb") as f:
        is_sqlite = f.read(16) == b"SQLite format 3\x00"
    out = os.path.join(dest, entry)
    if is_sqlite:
        source = sqlite3.connect(path, timeout=30)
        target = sqlite3.connect(out)
        source.backup(target)
        check = target.execute("PRAGMA quick_check").fetchone()[0]
        target.close()
        source.close()
        if check != "ok":
            sys.exit(f"  {entry}: the copy failed its integrity check: {check}")
        print(f"  {entry}: SQLite backup API, quick_check ok, {os.path.getsize(out)} bytes")
    else:
        shutil.copy2(path, out)
        os.chmod(out, 0o600)
        print(f"  {entry}: copied as is, {os.path.getsize(out)} bytes")
old = sorted(d for d in os.listdir(root) if d.startswith("predeploy-"))[:-keep]
for d in old:
    shutil.rmtree(os.path.join(root, d))
    print(f"  pruned {d}")
'

# The version of the checked-out commit, as a legal image tag.
current_version() {
  git describe --tags --always | tr -c 'A-Za-z0-9_.\n-' '_' | tr -d '\n'
}

# #508: write core/VERSION.json (version, commit, deploy time) after the reset.
# Only ever touches that one gitignored file. Mounted mode only: in baked mode
# the image carries its own (Dockerfile ARG CONSTRUCTICON_VERSION).
write_version_file() {
  local ver commit
  ver="$(git describe --tags --always)"
  commit="$(git rev-parse --short HEAD)"
  python3 - "$ver" "$commit" <<'PYEOF'
import datetime, json, sys
version, commit = sys.argv[1], sys.argv[2]
with open("core/VERSION.json", "w") as f:
    json.dump({"version": version, "commit": commit,
               "deployed_at": datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")}, f)
    f.write("\n")
PYEOF
  echo "Version: $ver ($commit)"
}

# Image repository (no tag) of WEB_IMAGE, e.g. constructicon:latest -> constructicon.
image_repo() {
  local img="$WEB_IMAGE"
  case "${img##*/}" in *:*) img="${img%:*}" ;; esac
  printf '%s' "$img"
}

require_image_name() {
  if [ -z "$WEB_IMAGE" ]; then
    echo "The $WEB_SVC service has no image: name in the compose file; add one (e.g. image: constructicon:latest)." >&2
    exit 1
  fi
}

# #688: build the web service's image with the version stamped in, and tag it <repo>:<version>
# next to the compose file's own (moving) tag.
build_versioned_image() {
  local ver commit built repo
  ver="$(current_version)"
  commit="$(git rev-parse --short HEAD)"
  built="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
  repo="$(image_repo)"
  echo "Building $WEB_IMAGE as version $ver ($commit)..."
  "${DOCKER[@]}" compose build \
    --build-arg "CONSTRUCTICON_VERSION=$ver" \
    --build-arg "CONSTRUCTICON_COMMIT=$commit" \
    --build-arg "CONSTRUCTICON_BUILT_AT=$built" \
    "$WEB_SVC"
  if [ "$WEB_IMAGE" != "$repo:$ver" ]; then
    "${DOCKER[@]}" tag "$WEB_IMAGE" "$repo:$ver"
  fi
  echo "Image: $repo:$ver (also $WEB_IMAGE)"
  build_sidecars
}

# #741: build every other service that has a build: section (the ollama-restarter, ...). Plain
# builds, no version args (they don't take them). Cached layers make an unchanged one near-instant.
build_sidecars() {
  local svcs=()
  read -r -a svcs <<< "${BUILD_SVCS:-}"
  if [ "${#svcs[@]}" -eq 0 ]; then
    return 0
  fi
  echo "Building the other services with a build: section: ${svcs[*]}..."
  "${DOCKER[@]}" compose build "${svcs[@]}"
}

# #736: no git checkout = can't fetch and build; say what works instead.
require_git_checkout() {
  if ! git rev-parse --git-dir >/dev/null 2>&1; then
    echo "deploy.sh: $PWD is not a git checkout, so it can't fetch and build a new version. Nothing was changed." >&2
    echo "  On a host without the repo, run a published image instead: ./scripts/deploy.sh --image <repo:tag>" >&2
    exit 1
  fi
}

# Keep the newest $IMAGE_KEEP version tags of this repo (never the compose file's own tag; an
# image a container still uses refuses to go, which is fine).
prune_version_tags() {
  local repo own
  repo="$(image_repo)"
  own="${WEB_IMAGE##*:}"
  "${DOCKER[@]}" image ls "$repo" --format '{{.CreatedAt}}\t{{.Tag}}' \
    | sort -r | cut -f2 \
    | grep -v -x -e "$own" -e '<none>' -e 'latest' \
    | tail -n "+$((IMAGE_KEEP + 1))" \
    | while IFS= read -r tag; do
        "${DOCKER[@]}" image rm "$repo:$tag" >/dev/null 2>&1 && echo "Image pruned: $repo:$tag" || true
      done || true   # nothing to prune = grep finds nothing; not a failure
}

# Which version is the running web process actually reporting? (Proof, not the checkout.)
report_running_version() {
  echo "Running version:"
  "${DOCKER[@]}" compose exec -T "$WEB_SVC" python3 -c \
    'from core import version; i = version.get_version_info(); print(" ", i["version"], i["commit"], i["deployed_at"])' \
    || echo "  (couldn't ask the web container; check: GET /api/version)"
}

detect_docker
read_compose

# #698: say which pre-deploy backup a deploy would take, and stop (reads only: compose config, df).
if [ -n "$BACKUP_PLAN" ]; then
  plan_backup
  echo "Pre-deploy backup plan: $BACKUP_KIND ($BACKUP_WHY)"
  if [ "$BACKUP_KIND" = zfs ]; then echo "  would run: ${SUDO[*]:-} zfs snapshot $ZFS_DS@predeploy-..."; fi
  if [ "$BACKUP_KIND" = copy ]; then echo "  would copy $DATA_SRC into $(dirname "$DATA_SRC")/predeploy-backups/ (SQLite backup API)"; fi
  # #741: what a deploy would build.
  echo "Images a deploy builds: $WEB_SVC (versioned)${BUILD_SVCS:+, $BUILD_SVCS}"
  exit 0
fi

# Lets you run just the version-writing step (used to verify #508 on the test
# box without resetting its checkout): ./scripts/deploy.sh --write-version-only
if [ -n "$WRITE_VERSION_ONLY" ]; then
  write_version_file
  exit 0
fi

if [ -n "$ROLLBACK" ]; then
  require_image_name
  repo="$(image_repo)"
  if ! "${DOCKER[@]}" image inspect "$repo:$ROLLBACK" >/dev/null 2>&1; then
    echo "No image $repo:$ROLLBACK on this box. Versions available:" >&2
    "${DOCKER[@]}" image ls "$repo" --format '  {{.Tag}}  ({{.CreatedSince}})' >&2
    exit 1
  fi
  if [ -n "$CODE_MOUNTED" ]; then
    echo "Note: this checkout bind-mounts its code, so the containers keep running the checkout's" >&2
    echo "files, not $ROLLBACK's. Roll the code back with git instead (or drop the override)." >&2
  fi
  if [ -n "$SNAPSHOT" ]; then
    snapshot_before_deploy
  fi
  echo "Rolling back: $WEB_IMAGE -> $repo:$ROLLBACK"
  echo "  (database migrations only go forward: if the newer version migrated the DB and the old"
  echo "   code can't read it, restore the DB from the pre-deploy snapshot as well)"
  "${DOCKER[@]}" tag "$repo:$ROLLBACK" "$WEB_IMAGE"
  "${DOCKER[@]}" compose up -d
  echo "Waiting for startup..."
  sleep 3
  "${DOCKER[@]}" compose ps
  report_running_version
  exit 0
fi

# #736: run a published image by tag (pull it, retag it as the compose tag, up -d). No git, no
# build: the path for a host that has only the compose file and this script, and for "rollback =
# run the previous tag" from a registry. Services with their own build: (the restarter) are not
# rebuilt here.
if [ -n "$IMAGE_REF" ]; then
  require_image_name
  echo "Pulling $IMAGE_REF..."
  if ! "${DOCKER[@]}" pull "$IMAGE_REF"; then
    if "${DOCKER[@]}" image inspect "$IMAGE_REF" >/dev/null 2>&1; then
      echo "Couldn't pull $IMAGE_REF (see above), but it is already on this host: using that copy."
    else
      echo "deploy.sh: couldn't pull $IMAGE_REF (see the error above), and it isn't on this host either." >&2
      echo "  Nothing was changed. Check the name and tag (and 'docker login' for a private registry)." >&2
      exit 1
    fi
  fi
  if [ -n "$CODE_MOUNTED" ]; then
    echo "Note: this checkout bind-mounts its code, so the containers keep running the checkout's" >&2
    echo "files, not the image's. Drop the override to run the image as published." >&2
  fi
  if [ -n "$SNAPSHOT" ]; then
    snapshot_before_deploy
  fi
  echo "Running $IMAGE_REF as $WEB_IMAGE..."
  "${DOCKER[@]}" tag "$IMAGE_REF" "$WEB_IMAGE"
  "${DOCKER[@]}" compose up -d
  echo "Waiting for startup..."
  sleep 3
  "${DOCKER[@]}" compose ps
  report_running_version
  exit 0
fi

require_git_checkout

if [ -n "$SNAPSHOT" ]; then
  snapshot_before_deploy
fi

echo "Fetching origin/main (and release tags)..."
git fetch origin --tags

before="$(git rev-parse HEAD)"
after="$(git rev-parse origin/main)"

if [ "$before" = "$after" ]; then
  echo "Already up to date at $(git rev-parse --short HEAD)."
else
  echo "Resetting $(git rev-parse --short "$before") -> $(git rev-parse --short "$after")..."
  git reset --hard origin/main
fi

# (The mode read above still holds: the compose files aren't tracked, so the reset can't change them.)
if [ -z "$CODE_MOUNTED" ]; then
  # BAKED (prod): the image is the version.
  require_image_name
  build_versioned_image
  echo "Recreating the services on the new image..."
  "${DOCKER[@]}" compose up -d
  prune_version_tags
else
  # MOUNTED (test/dev): code comes from the checkout through the override's bind mounts.
  write_version_file
  if [ -n "$BUILD" ]; then
    require_image_name
    build_versioned_image
    "${DOCKER[@]}" compose up -d
  elif [ -n "${BUILD_SVCS:-}" ]; then
    # #741: sidecars carry no code mount, so they ship only by being built; do it on every deploy.
    build_sidecars
    read -r -a _sidecars <<< "$BUILD_SVCS"
    "${DOCKER[@]}" compose up -d "${_sidecars[@]}"
  fi
  # Always restart: `up -d` leaves a container whose image and config didn't change running the old
  # code, and only a restart re-reads the bind-mounted files.
  echo "Restarting (bind-mounted code)..."
  "${DOCKER[@]}" compose restart
fi

echo "Waiting for startup..."
sleep 3
"${DOCKER[@]}" compose ps
report_running_version
