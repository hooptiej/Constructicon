#!/usr/bin/env bash
# Issue #71: one-command deploy for a checkout on the TrueNAS box
# (constructicon or constructicon-test). Requires the checkout's `origin`
# remote to already be a working SSH deploy-key remote -- see CLAUDE.md's
# Deployment section for how that's set up; this script doesn't configure
# credentials itself, only uses them.
#
# Usage (run from inside the checkout, or pass its path):
#   ./scripts/deploy.sh                  # fetch + reset to origin/main, then (see the two modes below)
#   ./scripts/deploy.sh --build          # mounted mode: also rebuild the image (requirements.txt /
#                                        # Dockerfile changed). Baked mode always builds; accepted there.
#   ./scripts/deploy.sh --no-snapshot    # skip the pre-deploy ZFS snapshot
#   ./scripts/deploy.sh --rollback VER   # run the image built for VER again (no git, no build)
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

set -euo pipefail
cd "$(dirname "$0")/.."

SNAPSHOT_KEEP=10
IMAGE_KEEP=5
BUILD=""
SNAPSHOT=1
WRITE_VERSION_ONLY=""
ROLLBACK=""
while [ $# -gt 0 ]; do
  case "$1" in
    --build) BUILD=1 ;;
    --no-snapshot) SNAPSHOT="" ;;
    --write-version-only) WRITE_VERSION_ONLY=1 ;;
    --rollback)
      shift
      ROLLBACK="${1:-}"
      if [ -z "$ROLLBACK" ]; then echo "--rollback needs a version (see: sudo docker image ls)" >&2; exit 2; fi ;;
    *) echo "Unknown option: $1" >&2; exit 2 ;;
  esac
  shift
done

# #688: one read of the effective compose config. Sets WEB_SVC (the app's web service: "web" on
# the NAS, "constructicon-web" in the example), WEB_IMAGE (its image: tag), CODE_MOUNTED (1 when
# /app/core is bind-mounted) and DATA_SRC (host path mounted at /app/data).
read_compose() {
  eval "$(sudo docker compose config --format json | python3 -c 'import json, shlex, sys
c = json.load(sys.stdin).get("services", {})
name = next((n for n in ("web", "constructicon-web") if n in c), "")
if not name:
    name = next((n for n, s in c.items() if "web.app:app" in " ".join(s.get("command") or [])), "")
s = c.get(name, {})
vols = s.get("volumes", [])
out = {
    "WEB_SVC": name,
    "WEB_IMAGE": s.get("image", ""),
    "CODE_MOUNTED": "1" if any(v.get("target") == "/app/core" for v in vols) else "",
    "DATA_SRC": next((v.get("source", "") for v in vols if v.get("target") == "/app/data"), ""),
}
for k, v in out.items():
    print(f"{k}={shlex.quote(v)}")')"
  if [ -z "$WEB_SVC" ]; then
    echo "Can't find the web service in this checkout's compose config." >&2
    exit 1
  fi
}

snapshot_before_deploy() {
  # Host path mounted at /app/data by the web service (#453 directory mount).
  local src="$DATA_SRC" inst ds mnt name ver
  if [ -z "$src" ]; then
    echo "Snapshot: no /app/data mount on the web service; skipping."
    return 0
  fi
  inst="$(dirname "$src")"   # e.g. /mnt/Storage Pool/Media/constructicon
  ds="$(df --output=source "$inst" | tail -1 | sed 's/[[:space:]]*$//')"
  mnt="$(df --output=target "$inst" | tail -1 | sed 's/[[:space:]]*$//')"
  if [ "$mnt" != "$inst" ]; then
    echo "Snapshot: $inst is inside the shared dataset '$ds' (mounted at $mnt), not its own; skipping."
    return 0
  fi
  # #508: the version being replaced rides along with the commit (describe is
  # sanitised to ZFS-legal characters; "unversioned" if there are no tags yet).
  ver="$(git describe --tags --always 2>/dev/null | tr -c 'A-Za-z0-9._:\n-' '_' | tr -d '\n' || true)"
  name="predeploy-$(date +%Y%m%d-%H%M%S)-$(git rev-parse --short HEAD)-v${ver:-unversioned}"
  sudo zfs snapshot "$ds@$name"
  echo "Snapshot: $ds@$name"
  # Retention: only ever touches @predeploy-* snapshots of this dataset.
  sudo zfs list -H -t snapshot -o name -s creation -d 1 "$ds" \
    | grep "@predeploy-" | head -n "-$SNAPSHOT_KEEP" \
    | while IFS= read -r old; do sudo zfs destroy "$old" && echo "Snapshot pruned: $old"; done
}

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
  sudo docker compose build \
    --build-arg "CONSTRUCTICON_VERSION=$ver" \
    --build-arg "CONSTRUCTICON_COMMIT=$commit" \
    --build-arg "CONSTRUCTICON_BUILT_AT=$built" \
    "$WEB_SVC"
  if [ "$WEB_IMAGE" != "$repo:$ver" ]; then
    sudo docker tag "$WEB_IMAGE" "$repo:$ver"
  fi
  echo "Image: $repo:$ver (also $WEB_IMAGE)"
}

# Keep the newest $IMAGE_KEEP version tags of this repo (never the compose file's own tag; an
# image a container still uses refuses to go, which is fine).
prune_version_tags() {
  local repo own
  repo="$(image_repo)"
  own="${WEB_IMAGE##*:}"
  sudo docker image ls "$repo" --format '{{.CreatedAt}}\t{{.Tag}}' \
    | sort -r | cut -f2 \
    | grep -v -x -e "$own" -e '<none>' -e 'latest' \
    | tail -n "+$((IMAGE_KEEP + 1))" \
    | while IFS= read -r tag; do
        sudo docker image rm "$repo:$tag" >/dev/null 2>&1 && echo "Image pruned: $repo:$tag" || true
      done || true   # nothing to prune = grep finds nothing; not a failure
}

# Which version is the running web process actually reporting? (Proof, not the checkout.)
report_running_version() {
  echo "Running version:"
  sudo docker compose exec -T "$WEB_SVC" python3 -c \
    'from core import version; i = version.get_version_info(); print(" ", i["version"], i["commit"], i["deployed_at"])' \
    || echo "  (couldn't ask the web container; check: GET /api/version)"
}

read_compose

# Lets you run just the version-writing step (used to verify #508 on the test
# box without resetting its checkout): ./scripts/deploy.sh --write-version-only
if [ -n "$WRITE_VERSION_ONLY" ]; then
  write_version_file
  exit 0
fi

if [ -n "$ROLLBACK" ]; then
  require_image_name
  repo="$(image_repo)"
  if ! sudo docker image inspect "$repo:$ROLLBACK" >/dev/null 2>&1; then
    echo "No image $repo:$ROLLBACK on this box. Versions available:" >&2
    sudo docker image ls "$repo" --format '  {{.Tag}}  ({{.CreatedSince}})' >&2
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
  sudo docker tag "$repo:$ROLLBACK" "$WEB_IMAGE"
  sudo docker compose up -d
  echo "Waiting for startup..."
  sleep 3
  sudo docker compose ps
  report_running_version
  exit 0
fi

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
  sudo docker compose up -d
  prune_version_tags
else
  # MOUNTED (test/dev): code comes from the checkout through the override's bind mounts.
  write_version_file
  if [ -n "$BUILD" ]; then
    require_image_name
    build_versioned_image
    sudo docker compose up -d
  fi
  # Always restart: `up -d` leaves a container whose image and config didn't change running the old
  # code, and only a restart re-reads the bind-mounted files.
  echo "Restarting (bind-mounted code)..."
  sudo docker compose restart
fi

echo "Waiting for startup..."
sleep 3
sudo docker compose ps
report_running_version
