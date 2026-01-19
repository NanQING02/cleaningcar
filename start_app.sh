#!/usr/bin/env bash
# Unified start script for CleaningCar Dual Stream System

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
VENV_PATH="$SCRIPT_DIR/venv-gst/bin/python"

# 1. Ensure venv exists
if [ ! -f "$VENV_PATH" ]; then
    echo "[start] Virtual environment not found. Running setup..."
    bash "$SCRIPT_DIR/install_runtime_venv.sh"
fi

# 2. Kill existing instances
PID_FILE="$SCRIPT_DIR/web_server_8000.pid"
if [ -f "$PID_FILE" ]; then
    OLD_PID=$(cat "$PID_FILE")
    if ps -p "$OLD_PID" > /dev/null; then
        echo "[start] Stopping existing web server (PID: $OLD_PID)..."
        kill "$OLD_PID"
        sleep 2
    fi
fi

# 3. Start Web Server (which now auto-starts both Washing and Detour streams)
echo "[start] Launching Web Console and Dual Inference Processes..."
nohup "$VENV_PATH" -m web.server --config "$SCRIPT_DIR/config_washing.json" --host 0.0.0.0 --port 8000 >> "$SCRIPT_DIR/app.log" 2>&1 &
NEW_PID=$!
echo "$NEW_PID" > "$PID_FILE"

echo "[start] System started."
echo "[start] Web Console: http://localhost:8000"
echo "[start] Logging to: $SCRIPT_DIR/app.log"
