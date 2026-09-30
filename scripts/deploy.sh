#!/usr/bin/env bash
# Issue #71: one-command deploy for a checkout on the TrueNAS box
# (constructicon or constructicon-test). Requires the checkout's `origin`
# remote to already be a working SSH deploy-key remote -- see CLAUDE.md's
# Deployment section for how that's set up; this script doesn't configure
# credentials itself, only uses them.
#
# Usage (run from inside the checkout, or pass its path):
#   ./scripts/deploy.sh            # fetch + reset to origin/main, restart
#   ./scripts/deploy.sh --build    # same, but `docker compose up -d --build`
#                                   # instead of `restart` -- use when
#                                   # requirements.txt or the Dockerfile
#                                   # changed, since core/web/scripts/
#                                   # mcp_server are bind-mounted and don't
#                                   # need a rebuild for an ordinary code change.
#   ./scripts/deploy.sh --no-snapshot   # skip the pre-deploy ZFS snapshot
#
# #442: before touching anything, this snapshots the ZFS dataset holding the
# instance's data (DB + storage), if that data lives in a DEDICATED dataset
# (prod: "Storage Pool/Media/constructicon"). Instant, atomic (DB, WAL and
# files captured at the same moment), and it only stores what changes after
# it. That replaces the old routine "/api/backup zip + cp -r code" before
# every deploy. Keeps the newest $SNAPSHOT_KEEP "@predeploy-*" snapshots.
# Restore: browse read-only copies under <dataset mountpoint>/.zfs/snapshot/<name>/
# (e.g. copy the DB back), or `zfs rollback` (destroys everything newer --
# owner call). Code rollback is git: the snapshot name carries the commit
# that was running before the deploy.

set -euo pipefail
cd "$(dirname "$0")/.."

SNAPSHOT_KEEP=10
BUILD=""
SNAPSHOT=1
for arg in "$@"; do
  case "$arg" in
    --build) BUILD=1 ;;
    --no-snapshot) SNAPSHOT="" ;;
    *) echo "Unknown option: $arg" >&2; exit 2 ;;
  esac
done

snapshot_before_deploy() {
  # Host path mounted at /app/data by the web service (#453 directory mount).
  local src inst ds mnt name
  src="$(sudo docker compose config --format json | python3 -c 'import json, sys
c = json.load(sys.stdin)
vols = c.get("services", {}).get("web", {}).get("volumes", [])
print(next((v.get("source", "") for v in vols if v.get("target") == "/app/data"), ""))')"
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
  name="predeploy-$(date +%Y%m%d-%H%M%S)-$(git rev-parse --short HEAD)"
  sudo zfs snapshot "$ds@$name"
  echo "Snapshot: $ds@$name"
  # Retention: only ever touches @predeploy-* snapshots of this dataset.
  sudo zfs list -H -t snapshot -o name -s creation -d 1 "$ds" \
    | grep "@predeploy-" | head -n "-$SNAPSHOT_KEEP" \
    | while IFS= read -r old; do sudo zfs destroy "$old" && echo "Snapshot pruned: $old"; done
}

if [ -n "$SNAPSHOT" ]; then
  snapshot_before_deploy
fi

echo "Fetching origin/main..."
git fetch origin

before="$(git rev-parse HEAD)"
after="$(git rev-parse origin/main)"

if [ "$before" = "$after" ]; then
  echo "Already up to date at $(git rev-parse --short HEAD)."
else
  echo "Resetting $(git rev-parse --short "$before") -> $(git rev-parse --short "$after")..."
  git reset --hard origin/main
fi

if [ -n "$BUILD" ]; then
  echo "Rebuilding and restarting (requirements.txt/Dockerfile changed)..."
  sudo docker compose up -d --build
else
  echo "Restarting (bind-mounted code only)..."
  sudo docker compose restart
fi

echo "Waiting for startup..."
sleep 3
sudo docker compose ps
