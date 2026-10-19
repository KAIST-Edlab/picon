#!/bin/bash

# Runs the PICon FastAPI backend (api/main.py) with uvicorn.
# Background by default; with --foreground it becomes the container's main process.

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
LOG_DIR="${LOG_DIR:-$ROOT/logs}"
LOG_FILE="$LOG_DIR/backend.out"

HOST="$1"
PORT="$2"
MODE="$3"

if [ -z "$HOST" ] || [ -z "$PORT" ]; then
    echo "Error: host and port are required." >&2
    echo "Usage: ./run_backend.sh <host> <port> [--foreground]" >&2
    exit 1
fi

if [ -n "$MODE" ] && [ "$MODE" != "--foreground" ]; then
    echo "Error: unknown option: $MODE" >&2
    echo "Usage: ./run_backend.sh <host> <port> [--foreground]" >&2
    exit 1
fi

if ! command -v uvicorn &> /dev/null; then
    echo "Error: uvicorn is not installed." >&2
    exit 1
fi

mkdir -p "$LOG_DIR" "$ROOT/data"
cd "$ROOT/api" || exit 1

if [ "$MODE" = "--foreground" ]; then
    exec > >(tee -a "$LOG_FILE") 2>&1
    echo "Backend starting in foreground... ($HOST:$PORT, log: $LOG_FILE)"
    export PYTHONUNBUFFERED=1
    exec uvicorn main:app --host "$HOST" --port "$PORT"
fi

nohup uvicorn main:app --host "$HOST" --port "$PORT" > "$LOG_FILE" 2>&1 &
PID=$!
sleep 3
if ! kill -0 "$PID" 2>/dev/null; then
    echo "Error: backend failed to start. See $LOG_FILE" >&2
    tail -n 20 "$LOG_FILE" >&2
    exit 1
fi
echo "Backend successfully activated! ($HOST:$PORT, PID: $PID, log: $LOG_FILE)"
