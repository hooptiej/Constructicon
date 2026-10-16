#!/bin/sh
# #687: entrypoint for the `ollama` sidecar in docker-compose.yml.example.
#
# Starts `ollama serve`, waits until it answers, and pulls the caption model if it is
# not already in /root/.ollama. Idempotent: once the model is on the volume, a restart
# only runs `ollama show` (a local lookup) and never touches the network.
#
# The model name is CAPTION_OLLAMA_MODEL, the same setting core/captions.py reads, so
# one value in .env drives both the app and the pull. Default: moondream.
# A failed pull (no network, typo) is logged and Ollama keeps serving: the app's
# caption breaker handles a missing model like any other caption failure.
#
# POSIX sh on purpose (the ollama/ollama image is Ubuntu, no extra packages). Runs as
# PID 1, so it forwards SIGTERM/SIGINT to `ollama serve` and exits with its status:
# `docker stop` / the restart sidecar get a clean shutdown, not a 10 s kill.

MODEL="${CAPTION_OLLAMA_MODEL:-moondream}"
WAIT_SECONDS="${OLLAMA_READY_TIMEOUT:-120}"

ollama serve &
pid=$!
trap 'kill -TERM "$pid" 2>/dev/null' TERM INT

i=0
until ollama list >/dev/null 2>&1; do
    if ! kill -0 "$pid" 2>/dev/null; then
        echo "serve-and-pull: ollama serve exited before it was ready" >&2
        wait "$pid"
        exit $?
    fi
    i=$((i + 1))
    if [ "$i" -ge "$WAIT_SECONDS" ]; then
        echo "serve-and-pull: ollama not ready after ${WAIT_SECONDS}s; skipping the model check" >&2
        break
    fi
    sleep 1
done

if [ "$i" -lt "$WAIT_SECONDS" ]; then
    if ollama show "$MODEL" >/dev/null 2>&1; then
        echo "serve-and-pull: model '$MODEL' already present, no pull"
    else
        echo "serve-and-pull: model '$MODEL' not present, pulling it (first start)"
        if ollama pull "$MODEL"; then
            echo "serve-and-pull: pulled '$MODEL'"
        else
            echo "serve-and-pull: pull of '$MODEL' FAILED; serving anyway (captions will fail until it is pulled)" >&2
        fi
    fi
fi

wait "$pid"
status=$?
# A trapped signal interrupts the first wait; wait again for serve's real exit status.
if kill -0 "$pid" 2>/dev/null; then
    wait "$pid"
    status=$?
fi
exit "$status"
