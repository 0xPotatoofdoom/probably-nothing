#!/bin/sh
# Dual-mode entrypoint.
#   serve  → FastAPI + WebSocket server (used by docker-compose)
#   *      → pass-through to the CLI (first arg is the GitHub URL)
set -e

if [ "$1" = "serve" ]; then
    shift
    exec uvicorn main:app --host 0.0.0.0 --port 8000 "$@"
fi

exec python -m cli "$@"
