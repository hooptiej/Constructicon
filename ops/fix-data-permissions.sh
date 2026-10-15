#!/usr/bin/env bash
# Tighten an existing install's data directories (#652). Idempotent; DRY RUN unless --apply.
#
#   bash ops/fix-data-permissions.sh [--apply] [--uid 1000] DATA_ROOT
#
# DATA_ROOT is the host directory holding the install's bind-mounted data (on the NAS:
# "/mnt/Storage Pool/Media/constructicon"). Only these subdirectories are touched, when present:
#
#   storage/ db/ exports/   directories 0750, files: group write and every "other" bit removed (0640)
#   backups/                the same, and every backup zip 0600
#
# Not touched: DATA_ROOT itself, import/ (the owner's drop-in inbox, mounted read-only into the MCP;
# it may be written over SMB by another account) and anything else in DATA_ROOT.
#
# It never chowns: entries not owned by the app uid (default 1000, the image's APP_UID) are listed
# so you can `sudo chown -R 1000:1000 <dir>` yourself (#643). New files are already created private
# by the app itself (core/paths.restrict_new_files, umask 027); this fixes what exists from before.
#
# Before --apply on a shared host: anything that reads these directories as ANOTHER user (a guest
# SMB share, a backup job, another container) loses access. Check that first.
set -euo pipefail

APPLY=0
APP_UID=1000
ROOT=""
while [ $# -gt 0 ]; do
  case "$1" in
    --apply) APPLY=1 ;;
    --uid) shift; APP_UID="$1" ;;
    -h|--help) sed -n '2,21p' "$0"; exit 0 ;;
    *) ROOT="$1" ;;
  esac
  shift
done
if [ -z "$ROOT" ] || [ ! -d "$ROOT" ]; then
  echo "usage: bash $0 [--apply] [--uid N] DATA_ROOT" >&2
  exit 2
fi

if [ "$APPLY" = 1 ]; then MODE="APPLY"; else MODE="DRY RUN (nothing changed; add --apply)"; fi
echo "== $MODE: $ROOT"

total=0
report() {  # report <label> <count> <find args...>: print a count and up to 5 examples
  local label="$1" count="$2"; shift 2
  printf '  %-58s %s\n' "$label" "$count"
  if [ "$count" -gt 0 ]; then
    find "$@" -print 2>/dev/null | head -5 | sed 's/^/      e.g. /'
  fi
}

for sub in storage db backups exports; do
  dir="$ROOT/$sub"
  [ -d "$dir" ] || { echo "-- $sub/: not present, skipped"; continue; }
  echo "-- $sub/  (now: $(stat -c '%A %U:%G' "$dir"))"

  dirs_args=("$dir" -type d -perm /0027)
  files_args=("$dir" -type f -perm /0027)
  zips_args=("$dir" -maxdepth 1 -type f -name 'constructicon-backup-*.zip*' -perm /0077)
  owner_args=("$dir" ! -uid "$APP_UID")

  n_dirs=$(find "${dirs_args[@]}" 2>/dev/null | wc -l)
  n_files=$(find "${files_args[@]}" 2>/dev/null | wc -l)
  report "directories to 0750 (drop group write, other rwx)" "$n_dirs" "${dirs_args[@]}"
  report "files to drop group write and other rwx (0644 -> 0640)" "$n_files" "${files_args[@]}"
  n_zips=0
  if [ "$sub" = backups ]; then
    n_zips=$(find "${zips_args[@]}" 2>/dev/null | wc -l)
    report "backup zips to 0600" "$n_zips" "${zips_args[@]}"
  fi
  n_owner=$(find "${owner_args[@]}" 2>/dev/null | wc -l)
  report "NOT owned by uid $APP_UID (not changed; chown them yourself)" "$n_owner" "${owner_args[@]}"
  total=$((total + n_dirs + n_files + n_zips))

  if [ "$APPLY" = 1 ]; then
    find "${dirs_args[@]}" -exec chmod g-w,o-rwx {} +
    find "${files_args[@]}" -exec chmod g-w,o-rwx {} +
    if [ "$sub" = backups ]; then
      find "${zips_args[@]}" -exec chmod 0600 {} +
    fi
    echo "   now: $(stat -c '%A %U:%G' "$dir")"
  fi
done

if [ "$APPLY" = 1 ]; then
  echo "== done: $total entries changed (run again: it should report 0)"
else
  echo "== $total entries would change; run again with --apply"
fi
