#!/usr/bin/env bash
# Run a maintenance script from scripts/ against a running install (#634), from the HOST.
#
#   cd <checkout> && ./scripts/run_tool.sh <web container> scripts/<tool>.py [args...]
#   e.g. ./scripts/run_tool.sh constructicon-test scripts/backfill_content_dates.py --base-url http://localhost:80 --dry-run
#
# The app containers no longer mount scripts/ (#634), so `docker exec <web> python3 scripts/x.py`
# fails there. This starts a ONE-OFF container that is the web container's twin for the length of the
# run, plus this checkout's scripts/ mounted read-only, and removes it afterwards:
#   * the same image, environment (DB path, token file, ...) and mounts (DB dir, storage, secrets, core,
#     web, ...) as the running web container (--volumes-from), so it sees the same DB and the token;
#   * the web container's network namespace (--network container:<web>), so http://localhost:<port>
#     is the running app, exactly as it was from `docker exec`;
#   * the image's own user (uid 1000, `app`), like the service.
# `docker compose run --rm -v ./scripts:/app/scripts:ro <service> ...` would be shorter but fails on
# the NAS: those services have a fixed `ipv4_address`, which a second container can't take
# ("Address already in use"). On an install without fixed addresses that form works too.
#
# Pass -it through by setting RUN_TOOL_TTY=1 (for a script that prompts). Never point this at
# production for a script that writes until it has been run against constructicon-test.
set -euo pipefail

if [ $# -lt 2 ]; then
    echo "usage: $0 <web container> scripts/<tool>.py [args...]" >&2
    exit 2
fi
web="$1"; shift
here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

image="$(sudo docker inspect -f '{{.Config.Image}}' "$web")"
env_args=()
while IFS= read -r line; do
    [ -n "$line" ] && env_args+=(-e "$line")
done < <(sudo docker inspect -f '{{range .Config.Env}}{{println .}}{{end}}' "$web")

tty=()
[ "${RUN_TOOL_TTY:-}" = "1" ] && tty=(-it)

exec sudo docker run --rm "${tty[@]}" \
    --volumes-from "$web" \
    --network "container:$web" \
    -v "$here:/app/scripts:ro" \
    "${env_args[@]}" \
    -w /app \
    "$image" python3 "$@"
