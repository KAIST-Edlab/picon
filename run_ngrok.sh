#!/bin/bash

# Runs ngrok in the background and logs its output to a file.
# Usage: ./run_ngrok.sh <port> <ngrok_config> [static_domain]
#   e.g. bash ./run_ngrok.sh 9978 ~/.config/ngrok/ngrok.yml exponent-glimmer-leverage.ngrok-free.dev

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
LOG_DIR="${LOG_DIR:-$ROOT/logs}"
LOG_FILE="$LOG_DIR/ngrok.out"

NGROK_PORT="$1"
NGROK_CONFIG="$2"
NGROK_DOMAIN="$3"

if [ -z "$NGROK_PORT" ] || [ -z "$NGROK_CONFIG" ]; then
    echo "Error: port and ngrok config are required." >&2
    echo "Usage: ./run_ngrok.sh <port> <ngrok_config> [static_domain]" >&2
    exit 1
fi

if ! command -v ngrok &> /dev/null; then
    echo "Error: ngrok is not installed." >&2
    exit 1
fi

if [ ! -f "$NGROK_CONFIG" ]; then
    echo "Error: ngrok config not found: $NGROK_CONFIG" >&2
    exit 1
fi

EXTRA_ARGS=()
if [ -n "$NGROK_DOMAIN" ]; then
    EXTRA_ARGS+=(--url "$NGROK_DOMAIN")
fi

mkdir -p "$LOG_DIR"
nohup ngrok http "$NGROK_PORT" --config "$NGROK_CONFIG" "${EXTRA_ARGS[@]}" --log=stdout > "$LOG_FILE" 2>&1 &
PID=$!

sleep 5
if ! kill -0 "$PID" 2>/dev/null; then
    echo "Error: ngrok failed to start. See $LOG_FILE" >&2
    tail -n 20 "$LOG_FILE" >&2
    exit 1
fi
echo "Ngrok successfully activated! (:$NGROK_PORT, PID: $PID, log: $LOG_FILE)"
grep -o 'url=https://[^ ]*' "$LOG_FILE" | tail -1
