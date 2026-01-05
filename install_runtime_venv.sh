#!/usr/bin/env bash
set -euo pipefail

log() {
  echo "[$(date '+%Y-%m-%d %H:%M:%S')] $*"
}

die() {
  log "ERROR: $*"
  exit 1
}

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
VENV_DIR="$SCRIPT_DIR/venv-gst"
CONFIG_PATH="$SCRIPT_DIR/config.json"
RUN_SCRIPT="$SCRIPT_DIR/run_zone_detect.py"
SERVICE_NAME="web_server_8000"
PID_FILE="$SCRIPT_DIR/${SERVICE_NAME}.pid"
LOG_FILE="$SCRIPT_DIR/${SERVICE_NAME}.log"
INFER_SERVICE_NAME="zone_infer_default"
INFER_PID_FILE="$SCRIPT_DIR/${INFER_SERVICE_NAME}.pid"
INFER_LOG_DIR="$SCRIPT_DIR/logs/inference"
PACKAGES_DIR="$SCRIPT_DIR/packages"
REQ_FILE="$SCRIPT_DIR/requirements.txt"

START_WEB=${START_WEB:-1}
START_INFER=${START_INFER:-0}
ONLY_SETUP=${ONLY_SETUP:-0}
FORCE_SETUP=${FORCE_SETUP:-0}
FORCE_PIP=${FORCE_PIP:-0}

if [ "$ONLY_SETUP" = "1" ]; then
  START_WEB=0
  START_INFER=0
fi

[ -f "$CONFIG_PATH" ] || die "config.json not found in $SCRIPT_DIR"
[ -f "$RUN_SCRIPT" ] || die "run_zone_detect.py not found in $SCRIPT_DIR"

mkdir -p "$SCRIPT_DIR/logs" "$INFER_LOG_DIR" "$SCRIPT_DIR/events" "$SCRIPT_DIR/captures" "$SCRIPT_DIR/video_result/per_id"

if [ -x /usr/bin/python3 ]; then
  PYTHON_SYS="/usr/bin/python3"
elif command -v python3 >/dev/null 2>&1; then
  PYTHON_SYS="$(command -v python3)"
else
  die "python3 not found"
fi

PYTHON_VER="$($PYTHON_SYS -c 'import sys; print(f"{sys.version_info.major}.{sys.version_info.minor}")')"
case "$PYTHON_VER" in
  3.8|3.9|3.10|3.11|3.12) ;;
  *) die "unsupported python version: $PYTHON_VER";;
esac

APT_PREFIX=""
if command -v sudo >/dev/null 2>&1; then
  APT_PREFIX="sudo"
fi

setup_env() {
  if [ -d "$VENV_DIR" ] && [ -x "$VENV_DIR/bin/python" ] && [ "$FORCE_SETUP" != "1" ]; then
    log "Found existing venv at $VENV_DIR (set FORCE_SETUP=1 重新部署)."
    PYTHON_BIN="$VENV_DIR/bin/python"
    if [ "$FORCE_PIP" = "1" ] && [ -f "$REQ_FILE" ]; then
      "$PYTHON_BIN" -m pip install --upgrade pip
      "$PYTHON_BIN" -m pip install -r "$REQ_FILE"
    fi
    return
  fi

  log "Installing system prerequisites via apt..."
  $APT_PREFIX apt-get update
  $APT_PREFIX apt-get install -y python3-venv python3-pip python3-opencv gstreamer1.0-tools     gstreamer1.0-plugins-base gstreamer1.0-plugins-good gstreamer1.0-plugins-bad gstreamer1.0-libav     libdrm-dev pkg-config build-essential

  CV2_STATUS="$($PYTHON_SYS - <<'EOF'
try:
    import cv2
    info = cv2.getBuildInformation()
    lines = [l for l in info.splitlines() if "GStreamer" in l]
    has_gst = any(("GStreamer:" in l and "YES" in l) for l in lines)
    if has_gst:
        print("OK")
    else:
        print("NO_GST")
except Exception:
    print("NO_CV2")
EOF
)"
  case "$CV2_STATUS" in
    OK) log "opencv + gstreamer ready"
       ;;
    NO_CV2) die "cv2 unavailable on system python"
       ;;
    NO_GST) die "system cv2 has no GStreamer support"
       ;;
  esac

  if [ ! -d "$VENV_DIR" ]; then
    log "Creating venv at $VENV_DIR"
    "$PYTHON_SYS" -m venv --system-site-packages "$VENV_DIR"
  fi
  PYTHON_BIN="$VENV_DIR/bin/python"
  [ -x "$PYTHON_BIN" ] || die "venv python not found at $PYTHON_BIN"
  "$PYTHON_BIN" -m pip install --upgrade pip

  if [ -d "$PACKAGES_DIR" ]; then
    if [ -f "$PACKAGES_DIR/librknnrt.so" ]; then
      log "Installing librknnrt.so to /usr/lib"
      $APT_PREFIX cp -f "$PACKAGES_DIR/librknnrt.so" /usr/lib/librknnrt.so
      $APT_PREFIX chmod 755 /usr/lib/librknnrt.so || true
      $APT_PREFIX ldconfig || true
    fi
    PY_TAG="cp$(echo "$PYTHON_VER" | tr -d .)"
    RKNN_WHL=""
    if ls "$PACKAGES_DIR"/rknn_toolkit_lite*"$PY_TAG"*.whl >/dev/null 2>&1; then
      RKNN_WHL="$(ls "$PACKAGES_DIR"/rknn_toolkit_lite*"$PY_TAG"*.whl | head -n 1)"
    fi
    if [ -n "$RKNN_WHL" ]; then
      log "Installing $RKNN_WHL"
      "$PYTHON_BIN" -m pip install "$RKNN_WHL"
    fi
    if [ -f "$PACKAGES_DIR/librga.so" ]; then
      log "Installing librga.so to /usr/local/lib"
      $APT_PREFIX cp -f "$PACKAGES_DIR/librga.so" /usr/local/lib/librga.so
      $APT_PREFIX chmod 755 /usr/local/lib/librga.so || true
      $APT_PREFIX ldconfig || true
    fi
    if [ -f "$PACKAGES_DIR/im2d.h" ]; then
      log "Copying RGA headers"
      $APT_PREFIX mkdir -p /usr/local/include/rga
      $APT_PREFIX cp -f "$PACKAGES_DIR/im2d.h" /usr/local/include/rga/im2d.h
    fi
  fi

  if [ -f "$REQ_FILE" ]; then
    log "Installing python requirements"
    "$PYTHON_BIN" -m pip install -r "$REQ_FILE"
  fi

  "$PYTHON_BIN" - <<'EOF'
try:
    import rga  # type: ignore
except Exception:
    print("[setup] Python module 'rga' not found (optional, libs已部署).")
EOF
}

stop_service() {
  local pid_file="$1"
  local label="$2"
  if [ ! -f "$pid_file" ]; then
    return
  fi
  local old_pid
  old_pid=$(cat "$pid_file" 2>/dev/null || true)
  if [ -n "$old_pid" ] && ps -p "$old_pid" >/dev/null 2>&1; then
    log "Stopping $label (PID=$old_pid)"
    kill "$old_pid" || true
    for _ in $(seq 1 20); do
      ps -p "$old_pid" >/dev/null 2>&1 || break
      sleep 1
    done
    if ps -p "$old_pid" >/dev/null 2>&1; then
      log "$label still running, force kill -9"
      kill -9 "$old_pid" || true
    fi
  fi
  rm -f "$pid_file"
}

start_web() {
  stop_service "$PID_FILE" "Web"
  log "Starting FastAPI web console on :8000"
  nohup "$PYTHON_BIN" -m web.server --config "$CONFIG_PATH" --host 0.0.0.0 --port 8000 >> "$LOG_FILE" 2>&1 &
  local new_pid=$!
  echo "$new_pid" > "$PID_FILE"
  sleep 1
  if ps -p "$new_pid" >/dev/null 2>&1; then
    log "Web console running (PID=$new_pid, log=$LOG_FILE)"
  else
    die "web server failed to start, check $LOG_FILE"
  fi
}

start_inference() {
  if [ "$START_INFER" != "1" ]; then
    return
  fi
  stop_service "$INFER_PID_FILE" "Inference"
  log "Starting run_zone_detect.py (后台 nohup)"
  local timestamp
  timestamp="manual_$(date '+%Y%m%d_%H%M%S')"
  local infer_log="$INFER_LOG_DIR/${timestamp}.log"
  nohup "$PYTHON_BIN" "$RUN_SCRIPT" --config "$CONFIG_PATH" >> "$infer_log" 2>&1 &
  local new_pid=$!
  echo "$new_pid" > "$INFER_PID_FILE"
  sleep 1
  if ps -p "$new_pid" >/dev/null 2>&1; then
    log "Inference running (PID=$new_pid, log=$infer_log)"
  else
    die "inference failed to start, check $infer_log"
  fi
}

main() {
  setup_env
  if [ -z "${PYTHON_BIN:-}" ]; then
    PYTHON_BIN="$VENV_DIR/bin/python"
  fi
  [ -x "$PYTHON_BIN" ] || die "python executable missing"

  if [ "$START_WEB" = "1" ]; then
    start_web
  else
    log "Skipping web console start (START_WEB=$START_WEB)"
  fi

  start_inference

  if [ "$START_WEB" != "1" ] && [ "$START_INFER" != "1" ]; then
    log "Setup completed (ONLY_SETUP mode)"
  fi
}

main "$@"
