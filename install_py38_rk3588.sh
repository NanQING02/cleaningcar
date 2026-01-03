#!/usr/bin/env bash
set -e

####################################
# 0. 基础路径与变量
####################################
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PACKAGES_DIR="$SCRIPT_DIR/packages"

ANACONDA_INSTALLER="$PACKAGES_DIR/Anaconda3-2021.04-Linux-aarch64.sh"
CONDA_ENV_TAR="$PACKAGES_DIR/py38_rk3588.tar.gz"
RKN_LIB_SRC="$PACKAGES_DIR/librknnrt.so"

ENV_NAME="py38_rk3588"

SERVICE_NAME="web_server_8000"
PID_FILE="$SCRIPT_DIR/${SERVICE_NAME}.pid"
LOG_FILE="$SCRIPT_DIR/${SERVICE_NAME}.log"

####################################
# 1. 用户 & Python 路径
####################################
if [ "$(id -u)" -eq 0 ]; then
  TARGET_USER="$(stat -c '%U' "$SCRIPT_DIR")"
  TARGET_HOME="$(eval echo "~$TARGET_USER")"
else
  TARGET_USER="$USER"
  TARGET_HOME="$HOME"
fi

ANACONDA_PREFIX="$TARGET_HOME/anaconda3"
ENV_DIR="$ANACONDA_PREFIX/envs/$ENV_NAME"
PYTHON_BIN="$ENV_DIR/bin/python"

NEED_SETUP=0
if [ ! -d "$ENV_DIR/conda-meta" ] || [ ! -x "$PYTHON_BIN" ]; then
  NEED_SETUP=1
fi

if [ "$NEED_SETUP" -eq 1 ]; then
  for f in "$ANACONDA_INSTALLER" "$CONDA_ENV_TAR" "$RKN_LIB_SRC"; do
    [ -f "$f" ] || { echo "❌ 缺少文件: $f"; exit 1; }
  done

  [ -d "$ANACONDA_PREFIX" ] || bash "$ANACONDA_INSTALLER" -b -p "$ANACONDA_PREFIX"

  if [ ! -d "$ENV_DIR/conda-meta" ]; then
    rm -rf "$ENV_DIR"
    mkdir -p "$ENV_DIR"
    tar -xzf "$CONDA_ENV_TAR" -C "$ENV_DIR"
    [ -d "$ENV_DIR/conda-meta" ] || { echo "❌ conda 环境解压失败"; exit 1; }
  fi

  sudo cp -f "$RKN_LIB_SRC" /usr/lib/librknnrt.so
  sudo chmod 755 /usr/lib/librknnrt.so
  sudo ldconfig || true
fi

####################################
# 6. 启动 / 重启 Web 服务
####################################
CONFIG_PATH="$SCRIPT_DIR/config.json"

[ -f "$CONFIG_PATH" ] || { echo "⚠️ 未找到配置文件"; exit 0; }
[ -x "$PYTHON_BIN" ] || { echo "❌ Python 不存在"; exit 1; }

cd "$SCRIPT_DIR"

echo ">>> 检查服务状态"

if [ -f "$PID_FILE" ]; then
  OLD_PID=$(cat "$PID_FILE")
  if ps -p "$OLD_PID" >/dev/null 2>&1; then
    echo "🔁 服务正在运行，准备重启 (PID=$OLD_PID)"
    kill "$OLD_PID"

    # 等待进程退出（最多 20 秒）
    for i in {1..20}; do
      ps -p "$OLD_PID" >/dev/null 2>&1 || break
      sleep 1
    done

    if ps -p "$OLD_PID" >/dev/null 2>&1; then
      echo "⚠️ 未能正常停止，强制 kill -9"
      kill -9 "$OLD_PID" || true
    fi
  else
    echo "⚠️ PID 文件存在但进程不存在，清理"
  fi
  rm -f "$PID_FILE"
fi

echo ">>> 后台启动 Web 服务 (8000)"

nohup "$PYTHON_BIN" -m web.server \
  --config "$CONFIG_PATH" \
  --host 0.0.0.0 \
  --port 8000 \
  >> "$LOG_FILE" 2>&1 &

NEW_PID=$!
echo "$NEW_PID" > "$PID_FILE"

sleep 1
if ps -p "$NEW_PID" >/dev/null 2>&1; then
  echo "✅ 服务启动成功 (PID=$NEW_PID)"
  echo "📄 日志: $LOG_FILE"
else
  echo "❌ 服务启动失败，请检查日志"
  exit 1
fi
