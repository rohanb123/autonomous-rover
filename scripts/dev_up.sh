#!/usr/bin/env bash
# Start GX10 services: brain :8000, vision :8100, mapper :8200, overhead :8300. Ctrl+C stops all.
set -euo pipefail
source "$(dirname "$0")/_env.sh"

cd "$ROOT/gx10"
pids=()
trap 'kill "${pids[@]}" 2>/dev/null || true' EXIT INT TERM

uvicorn brain.app:app    --host 0.0.0.0 --port 8000 & pids+=($!)
uvicorn vision.app:app   --host 0.0.0.0 --port 8100 & pids+=($!)
uvicorn mapper.app:app   --host 0.0.0.0 --port 8200 & pids+=($!)
uvicorn overhead.app:app --host 0.0.0.0 --port 8300 & pids+=($!)

wait
