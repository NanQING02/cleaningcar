#!/usr/bin/env bash
set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
VENV_DIR="$SCRIPT_DIR/venv-gst"
SERVICE_NAME="web_server_8000"
PID_FILE="$SCRIPT_DIR/${SERVICE_NAME}.pid"
LOG_FILE="$SCRIPT_DIR/${SERVICE_NAME}.log"
CONFIG_PATH="$SCRIPT_DIR/config.json"
PACKAGES_DIR="$SCRIPT_DIR/packages"

MONITOR_CPU_LIMIT="${MONITOR_CPU_LIMIT:-600}"
MONITOR_INTERVAL="${MONITOR_INTERVAL:-10}"

if [ ! -f "$CONFIG_PATH" ]; then
  echo "config.json not found in $SCRIPT_DIR"
  exit 1
fi

run_apt() {
  local desc="$1"
  shift
  if ! "$@"; then
    echo "[setup] apt 命令失败: $desc"
    echo "[setup] 请检查:"
    echo "  1) 是否已连接网络"
    echo "  2) /etc/apt/sources.list 与 PPA 源是否可访问"
    echo "  3) 是否有 apt/lock 锁 (例如其他 apt 进程正在运行)"
    exit 1
  fi
}

start_cpu_monitor() {
  local pid="$1"
  (
    while ps -p "$pid" >/dev/null 2>&1; do
      local cpu_raw
      cpu_raw="$(ps -p "$pid" -o %cpu= 2>/dev/null | awk '{print int($1)}')"
      if [ -n "$cpu_raw" ] && [ "$cpu_raw" -gt "$MONITOR_CPU_LIMIT" ]; then
        echo "[monitor] 进程 $pid CPU 占用过高(${cpu_raw}%)，阈值=${MONITOR_CPU_LIMIT}% ，准备终止。"
        kill "$pid" 2>/dev/null || true
        sleep 5
        if ps -p "$pid" >/dev/null 2>&1; then
          echo "[monitor] 进程 $pid 未正常退出，执行 kill -9。"
          kill -9 "$pid" 2>/dev/null || true
        fi
        echo "[monitor] 已终止高占用进程 $pid，请查看日志: $LOG_FILE"
        break
      fi
      sleep "$MONITOR_INTERVAL"
    done
  ) &
}

if [ -x /usr/bin/python3 ]; then
  PYTHON_SYS="/usr/bin/python3"
elif command -v python3 >/dev/null 2>&1; then
  PYTHON_SYS="$(command -v python3)"
else
  echo "python3 not found"
  exit 1
fi
PYTHON_VER="$("$PYTHON_SYS" -c 'import sys; print(f"{sys.version_info.major}.{sys.version_info.minor}")')"
case "$PYTHON_VER" in
  3.8|3.9|3.10|3.11|3.12) ;;
  *) echo "unsupported python version: $PYTHON_VER"; exit 1;;
esac
if [ -d "$VENV_DIR" ] && [ -x "$VENV_DIR/bin/python" ] && [ -z "${FORCE_SETUP:-}" ]; then
  echo "Found existing venv at $VENV_DIR, skip system setup (set FORCE_SETUP=1 to force)."
  PYTHON_BIN="$VENV_DIR/bin/python"
else
  if ! command -v sudo >/dev/null 2>&1; then
    APT_PREFIX=""
  else
    APT_PREFIX="sudo"
  fi
  run_apt "apt-get update" $APT_PREFIX apt-get update
  run_apt "安装 python3-venv/python3-opencv 及 GStreamer 组件" \
    $APT_PREFIX apt-get install -y python3-venv python3-pip python3-opencv \
      gstreamer1.0-tools gstreamer1.0-plugins-base \
      gstreamer1.0-plugins-good gstreamer1.0-plugins-bad gstreamer1.0-libav
  CV2_STATUS="$("$PYTHON_SYS" - << 'EOF'
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
  if [ "$CV2_STATUS" = "NO_CV2" ]; then
    echo "cv2 not available in system python"
    exit 1
  fi
  if [ "$CV2_STATUS" = "NO_GST" ]; then
    echo "system cv2 has no GStreamer support"
    exit 1
  fi
  if [ ! -d "$VENV_DIR" ]; then
    "$PYTHON_SYS" -m venv --system-site-packages "$VENV_DIR"
  fi
  PYTHON_BIN="$VENV_DIR/bin/python"
  if [ ! -x "$PYTHON_BIN" ]; then
    echo "venv python not found at $PYTHON_BIN"
    exit 1
  fi
  "$PYTHON_BIN" -m pip install --upgrade pip
  if [ -d "$PACKAGES_DIR" ]; then
    RKN_LIB_SRC="$PACKAGES_DIR/librknnrt.so"
    if [ -f "$RKN_LIB_SRC" ]; then
      $APT_PREFIX cp -f "$RKN_LIB_SRC" /usr/lib/librknnrt.so
      $APT_PREFIX chmod 755 /usr/lib/librknnrt.so || true
      $APT_PREFIX ldconfig || true
    fi
    PY_MAJOR="$(echo "$PYTHON_VER" | cut -d. -f1)"
    PY_MINOR="$(echo "$PYTHON_VER" | cut -d. -f2)"
    PY_TAG="cp${PY_MAJOR}${PY_MINOR}"
    RKNN_WHL=""
    if ls "$PACKAGES_DIR"/rknn_toolkit_lite*"$PY_TAG"*.whl >/dev/null 2>&1; then
      RKNN_WHL="$(ls "$PACKAGES_DIR"/rknn_toolkit_lite*"$PY_TAG"*.whl 2>/dev/null | head -n 1)"
    fi
    if [ -n "$RKNN_WHL" ]; then
      "$PYTHON_BIN" -m pip install "$RKNN_WHL"
    fi
    RGA_SO_SRC="$PACKAGES_DIR/librga.so"
    if [ -f "$RGA_SO_SRC" ]; then
      $APT_PREFIX cp -f "$RGA_SO_SRC" /usr/local/lib/librga.so
      $APT_PREFIX chmod 755 /usr/local/lib/librga.so || true
      $APT_PREFIX ldconfig || true
    fi
    RGA_HDR_SRC="$PACKAGES_DIR/im2d.h"
    if [ -f "$RGA_HDR_SRC" ]; then
      $APT_PREFIX mkdir -p /usr/local/include/rga
      $APT_PREFIX cp -f "$RGA_HDR_SRC" /usr/local/include/rga/im2d.h
    fi
  fi
  REQ_FILE="$SCRIPT_DIR/requirements.txt"
  if [ -f "$REQ_FILE" ]; then
    "$PYTHON_BIN" -m pip install -r "$REQ_FILE"
  fi
  "$PYTHON_BIN" - << 'EOF'
try:
    import rga  # type: ignore
except Exception:
    print("[setup] Python module 'rga' not found (optional, can be ignored).")
    print("[setup] 当前方案使用系统库 librga.so + rga_resize_plugin.py 实现 RGA 预处理。")
try:
    from rknnlite.api import RKNNLite  # type: ignore
    print("[setup] RKNNLite 可用。")
except Exception as exc:
    print("[setup] 警告: 无法导入 RKNNLite:", repr(exc))
    print("[setup] 请检查 rknn_toolkit_lite2 是否安装、librknnrt.so 是否在系统库路径。")
EOF
fi
if [ -f "$VENV_DIR/bin/activate" ]; then
  echo "[setup] 激活虚拟环境: $VENV_DIR"
  . "$VENV_DIR/bin/activate"
fi
cd "$SCRIPT_DIR"
if [ -f "$PID_FILE" ]; then
  OLD_PID=$(cat "$PID_FILE" 2>/dev/null || true)
  if [ -n "$OLD_PID" ] && ps -p "$OLD_PID" >/dev/null 2>&1; then
    kill "$OLD_PID" || true
    for i in $(seq 1 20); do
      if ! ps -p "$OLD_PID" >/dev/null 2>&1; then
        break
      fi
      sleep 1
    done
    if ps -p "$OLD_PID" >/dev/null 2>&1; then
      kill -9 "$OLD_PID" || true
    fi
  fi
  rm -f "$PID_FILE"
fi
nohup "$PYTHON_BIN" -m web.server --config "$CONFIG_PATH" --host 0.0.0.0 --port 8000 >> "$LOG_FILE" 2>&1 &
NEW_PID=$!
echo "$NEW_PID" > "$PID_FILE"
sleep 1
if ps -p "$NEW_PID" >/dev/null 2>&1; then
  echo "service started, pid=$NEW_PID, log=$LOG_FILE"
  start_cpu_monitor "$NEW_PID"
else
  echo "service failed to start, check log: $LOG_FILE"
  exit 1
fi
