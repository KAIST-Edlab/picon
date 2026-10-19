#!/bin/bash

# This script runs a proxy server in the background and logs its output to a file.
# With --foreground, it runs in the foreground instead (e.g. as the main process of a Docker container).

# Set the root directory and log file path
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
LOG_DIR="${LOG_DIR:-$ROOT/logs}"
LOG_FILE="$LOG_DIR/proxy_server.out"

# Arguments: host, port, backend url and optional --foreground
HOST="$1"
PORT="$2"
BACKEND_URL="$3"
MODE="$4"

# Check if the required arguments are provided
if [ -z "$HOST" ] || [ -z "$PORT" ] || [ -z "$BACKEND_URL" ]; then
    echo "Error: host, port and backend url are required." >&2
    echo "Usage: ./run_proxy_server.sh <host> <port> <backend_url> [--foreground]" >&2
    exit 1
fi

# Check if the optional argument is valid
if [ -n "$MODE" ] && [ "$MODE" != "--foreground" ]; then
    echo "Error: unknown option: $MODE" >&2
    echo "Usage: ./run_proxy_server.sh <host> <port> <backend_url> [--foreground]" >&2
    exit 1
fi

mkdir -p "$LOG_DIR"
cd "$ROOT/api" || exit 1

# Foreground execution: replace this shell with the proxy so that it keeps the container alive and receives signals.
# Output goes to both stdout (docker logs) and the log file.
if [ "$MODE" = "--foreground" ]; then
    echo "Proxy starting in foreground... ($HOST:$PORT -> $BACKEND_URL, log: $LOG_FILE)"
    exec > >(tee -a "$LOG_FILE") 2>&1
    exec python3 -u proxy.py --proxy_host "$HOST" --proxy_port "$PORT" --backend_url "$BACKEND_URL"
fi

# Background execution
nohup python3 proxy.py --proxy_host "$HOST" --proxy_port "$PORT" --backend_url "$BACKEND_URL" > "$LOG_FILE" 2>&1 &
PID=$!


# Wait for a second until the background process is revealed, then check if it is still alive.
sleep 3
if ! kill -0 "$PID" 2>/dev/null; then
    echo "Error: proxy server failed to start. See $LOG_FILE" >&2
    tail -n 20 "$LOG_FILE" >&2
    exit 1
fi
echo "Proxy successfully activated! ($HOST:$PORT -> $BACKEND_URL, PID: $PID, log: $LOG_FILE)"
