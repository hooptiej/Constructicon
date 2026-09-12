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

set -euo pipefail
cd "$(dirname "$0")/.."

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

if [ "${1:-}" = "--build" ]; then
  echo "Rebuilding and restarting (requirements.txt/Dockerfile changed)..."
  sudo docker compose up -d --build
else
  echo "Restarting (bind-mounted code only)..."
  sudo docker compose restart
fi

echo "Waiting for startup..."
sleep 3
sudo docker compose ps
