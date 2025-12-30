#!/usr/bin/env bash
set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PACKAGES_DIR="$SCRIPT_DIR/packages"

ANACONDA_INSTALLER="$PACKAGES_DIR/Anaconda3-2021.04-Linux-aarch64.sh"
CONDA_ENV_TAR="$PACKAGES_DIR/py38_rk3588.tar.gz"
RKN_LIB_SRC="$PACKAGES_DIR/librknnrt.so"

ENV_NAME="py38_rk3588"

if [ "$(id -u)" -eq 0 ]; then
  TARGET_USER="$(stat -c '%U' "$SCRIPT_DIR")"
  if [ "$TARGET_USER" = "root" ]; then
    case "$SCRIPT_DIR" in
      /home/*/*)
        CANDIDATE="${SCRIPT_DIR#/home/}"
        CANDIDATE="${CANDIDATE%%/*}"
        if id "$CANDIDATE" >/dev/null 2>&1; then
          TARGET_USER="$CANDIDATE"
        fi
        ;;
    esac
  fi
  TARGET_HOME="$(eval echo "~$TARGET_USER")"
  ANACONDA_PREFIX="$TARGET_HOME/anaconda3"
  echo "当前以 root 运行，将为用户 $TARGET_USER 安装到 $ANACONDA_PREFIX"
else
  TARGET_USER="$USER"
  TARGET_HOME="$HOME"
  ANACONDA_PREFIX="$TARGET_HOME/anaconda3"
  echo "当前以普通用户 $TARGET_USER 运行，Anaconda 安装到 $ANACONDA_PREFIX"
fi

echo "脚本所在目录: $SCRIPT_DIR"
echo "使用 packages 目录: $PACKAGES_DIR"
echo "目标环境名: $ENV_NAME"
echo

if [ ! -f "$ANACONDA_INSTALLER" ]; then
  echo "找不到 Anaconda 安装包: $ANACONDA_INSTALLER"
  exit 1
fi

if [ ! -f "$CONDA_ENV_TAR" ]; then
  echo "找不到打包的 conda 环境: $CONDA_ENV_TAR"
  exit 1
fi

if [ ! -f "$RKN_LIB_SRC" ]; then
  echo "找不到 librknnrt.so: $RKN_LIB_SRC"
  exit 1
fi

if [ ! -d "$ANACONDA_PREFIX" ]; then
  echo "开始静默安装 Anaconda3 到 $ANACONDA_PREFIX"
  bash "$ANACONDA_INSTALLER" -b -p "$ANACONDA_PREFIX"
else
  echo "检测到已存在的 Anaconda3 目录: $ANACONDA_PREFIX，跳过安装"
fi

ENV_DIR="$ANACONDA_PREFIX/envs/$ENV_NAME"
if [ -d "$ENV_DIR" ] && [ -d "$ENV_DIR/conda-meta" ]; then
  echo "检测到已存在环境: $ENV_DIR，跳过解压"
else
  mkdir -p "$ENV_DIR"
  echo "解压 conda 环境到 $ENV_DIR"
  tar -xzf "$CONDA_ENV_TAR" -C "$ENV_DIR"
  if [ -d "$ENV_DIR/conda-meta" ]; then
    echo "环境已解压到: $ENV_DIR"
  else
    echo "注意: 解压后未找到目录 $ENV_DIR/conda-meta，请检查压缩包结构"
  fi
fi

echo "需要 sudo 权限将 librknnrt.so 拷贝到 /usr/lib"
sudo cp -f "$RKN_LIB_SRC" /usr/lib/librknnrt.so
sudo chmod 755 /usr/lib/librknnrt.so
sudo ldconfig || true

echo
echo "一键安装完成:"
echo "  Anaconda3 安装路径: $ANACONDA_PREFIX"
echo "  环境目录: $ANACONDA_PREFIX/envs/$ENV_NAME"
echo "  动态库已安装: /usr/lib/librknnrt.so"
echo
echo "用户 $TARGET_USER 激活环境命令:"
echo "  source \"$ANACONDA_PREFIX/bin/activate\" $ENV_NAME"
echo
cd "$SCRIPT_DIR"
CONFIG_PATH="$TARGET_HOME/cleaningcar_v2/config.json"
if [ ! -f "$CONFIG_PATH" ]; then
  echo "未找到配置文件 $CONFIG_PATH，跳过自动启动 Web 服务"
  exit 0
fi
echo "自动激活环境并启动 Web 服务"
. "$ANACONDA_PREFIX/bin/activate" "$ENV_NAME"
python3 -m web.server --config "$CONFIG_PATH" --host 0.0.0.0 --port 8000
