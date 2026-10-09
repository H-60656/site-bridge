#!/usr/bin/env sh
# Pixiv Bridge / Iwara Bridge / ASMR Bridge 一键启动脚本（Termux / Linux / macOS）
#
# 用法：
#   sh start.sh                  打开项目导航，挑一个接口项目启动（回车＝默认的 pixiv）
#   sh start.sh iwara            直接启动指定的接口项目
#   sh start.sh asmr_one         直接启动该接口项目（匿名可用，不需要登录）
#   sh start.sh --port 9000      换个端口
#   sh start.sh --host 0.0.0.0   允许同一局域网的其他设备访问
#   sh start.sh --check          只检查环境和登录，不启动服务
#   BRIDGE_SITE=iwara sh start.sh  跳过询问，直接跑 iwara
#
# 各接口项目 pixiv/ 、 iwara/ 与 asmr_one/ 是**同级**的：各自一个 run.py，互不加载。
# 本脚本不做挑选，只把参数转发给框架的项目导航（`python -m pixiv_bridge`）：
#   * 设了 BRIDGE_SITE 就用它（环境变量，其次根目录 .env 里的同名行）；
#   * 只有一个接口项目就直接启动；
#   * 有终端就列出来问一句（回车＝第一个，也就是默认站点）；
#   * 没有终端（Docker / 计划任务 / 管道）就用第一个，不会挂着等输入。
#
# 想连扩展目录一起用（novel / iwara_web），在 .env 里加一行：
#   PIXIV_BRIDGE_EXTENSIONS=novel          （配 pixiv 接口项目）
#   PIXIV_BRIDGE_EXTENSIONS=iwara_web      （配 iwara 接口项目）
# 程序会自动去找 <扩展>/extension.py，扩展不再有自己的入口脚本
# （见 README「附属项目（扩展）」）。
# 首次运行会自动用 pip 安装依赖（fastapi / uvicorn / httpx，Termux 上改用
# 纯 Python 的 pydantic 1.x 依赖集），之后启动就很快。

set -eu
cd "$(dirname "$0")"

# ---------- 1. 找到 Python ----------
PY=""
for candidate in python3 python; do
    if command -v "$candidate" >/dev/null 2>&1; then
        PY="$candidate"
        break
    fi
done

if [ -z "$PY" ]; then
    echo "[x] 没有找到 Python。请先安装 Python 3.10+："
    echo "      Termux : pkg update && pkg install -y python"
    echo "      Ubuntu : sudo apt install -y python3 python3-venv python3-pip"
    echo "      macOS  : brew install python3"
    echo "    安装完再运行：sh start.sh"
    exit 1
fi

# ---------- 2. 检查版本 ----------
if ! "$PY" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)'; then
    echo "[x] Python 版本太低：$("$PY" -c 'import sys; print(sys.version.split()[0])')，需要 3.10 或更高。"
    echo "      Termux : pkg upgrade python"
    echo "      Ubuntu : sudo apt install -y python3.10 python3.10-venv"
    exit 1
fi

# ---------- 3. 首次运行时生成 .env ----------
if [ ! -f .env ] && [ -f .env.example ]; then
    cp .env.example .env
    echo "[i] 已从 .env.example 生成配置文件 .env"
fi

# ---------- 4. 首次使用提醒（只跑 iwara / asmr.one 时不需要 Pixiv 登录） ----------
if [ "${BRIDGE_SITE:-pixiv}" = "pixiv" ] && [ -f .env ] \
    && ! grep -q '^PIXIV_REFRESH_TOKEN=..*' .env && [ ! -f data/tokens.json ]; then
    echo "[!] 还没有 Pixiv 登录凭据：请编辑 .env 填入 PIXIV_REFRESH_TOKEN，"
    echo "    或启动后按 INSTALL.md「获取 refresh_token」用网页授权登录。"
    echo "    （只跑 iwara / asmr.one 接口项目时不需要 Pixiv 登录。）"
fi

# ---------- 5. 启动（依赖缺失会自动安装；站点挑选交给框架的导航） ----------
echo "[i] 使用 $PY $("$PY" -c 'import sys; print(sys.version.split()[0])') 启动；首次运行会自动安装依赖…"
exec "$PY" -m pixiv_bridge "$@"
