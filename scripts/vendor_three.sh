#!/usr/bin/env bash
# #742: fetch (or just verify) the vendored three.js files for the STL 3D viewer.
#
#   scripts/vendor_three.sh            download into web/static/vendor/three/ and verify sha256
#   scripts/vendor_three.sh --verify   only check the files already there
#
# Exits non-zero, saying which file, on any download failure or checksum mismatch; a mismatched
# download is deleted rather than left in place. To bump the version: change VERSION and every
# sha256 below (and in web/static/vendor/three/README.txt), then check the object page's viewer.
set -euo pipefail

VERSION="0.170.0"
BASE="https://cdn.jsdelivr.net/npm/three@${VERSION}"
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
DEST="$ROOT/web/static/vendor/three"

# file name | upstream path | sha256
FILES=(
  "three.module.min.js|build/three.module.min.js|08fd7545d13d2c7fb65ab691530a802dafefd638596501854f267d0fb13c39e7"
  "STLLoader.js|examples/jsm/loaders/STLLoader.js|a0a83c88b269c94e25b690fae770d350c4728c81853195186976be7af0f8a3b3"
  "OrbitControls.js|examples/jsm/controls/OrbitControls.js|80efaadea4f8a636a65fb0bd08bfef62f3d93a0bb94e2e7500f23176c5c07f4e"
  "LICENSE|LICENSE|4c40a1ef62450b857c3b2aaf294936304cd552d965fbcd9d32d4c5bcf4ba4454"
)

verify_only=0
[ "${1:-}" = "--verify" ] && verify_only=1

sha() { if command -v sha256sum >/dev/null; then sha256sum "$1" | cut -d' ' -f1; else shasum -a 256 "$1" | cut -d' ' -f1; fi; }

mkdir -p "$DEST"
failed=0
for entry in "${FILES[@]}"; do
  IFS='|' read -r name path want <<<"$entry"
  out="$DEST/$name"
  if [ "$verify_only" -eq 0 ]; then
    if ! curl -sSfL --max-time 60 -o "$out.tmp" "$BASE/$path"; then
      echo "FAIL $name: download failed from $BASE/$path" >&2
      rm -f "$out.tmp"; failed=1; continue
    fi
    mv "$out.tmp" "$out"
  fi
  if [ ! -f "$out" ]; then
    echo "FAIL $name: missing ($out)" >&2; failed=1; continue
  fi
  got="$(sha "$out")"
  if [ "$got" != "$want" ]; then
    echo "FAIL $name: sha256 $got, expected $want" >&2
    [ "$verify_only" -eq 0 ] && rm -f "$out"
    failed=1; continue
  fi
  echo "ok   $name"
done
if [ "$failed" -ne 0 ]; then
  echo "three.js $VERSION: NOT all files are in place; the STL page will show the thumbnail and say why." >&2
  exit 1
fi
echo "three.js $VERSION: all files verified in $DEST"
