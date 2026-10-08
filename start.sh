#!/usr/bin/env sh
# Pixiv Bridge / Iwara Bridge 一键启动脚本（Termux / Linux / macOS）
#
# 用法：
#   sh start.sh                  启动本目录里的接口项目
#   sh start.sh --port 9000      换个端口
#   sh start.sh --host 0.0.0.0   允许同一局域网的其他设备访问
#   sh start.sh --check          只检查环境和登录，不启动服务
#   BRIDGE_SITE=iwara sh start.sh  明确指定跑哪个接口项目
#
# 两个接口项目 pixiv/ 与 iwara/ 是**同级**的：各自一个 run.py，互不加载。
# 本脚本只启动其中一个：
#   * 设了 BRIDGE_SITE 就用它；
#   * 否则取 pixiv/run.py、iwara/run.py 里先存在的那个。
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

# ---------- 3. 选接口项目（pixiv 与 iwara 同级） ----------
SITE="${BRIDGE_SITE:-}"
if [ -z "$SITE" ]; then
    if [ -f pixiv/run.py ]; then
        SITE=pixiv
    elif [ -f iwara/run.py ]; then
        SITE=iwara
    fi
fi

if [ -z "$SITE" ] || [ ! -f "$SITE/run.py" ]; then
    echo "[x] 没有找到接口项目：本目录下应该有 pixiv/run.py 或 iwara/run.py。"
    echo "    请在包含它们的目录里运行：sh start.sh"
    exit 1
fi

# ---------- 4. 首次运行时生成 .env ----------
if [ ! -f .env ] && [ -f .env.example ]; then
    cp .env.example .env
    echo "[i] 已从 .env.example 生成配置文件 .env"
fi

if [ "$SITE" = "pixiv" ] && [ -f .env ] \
    && ! grep -q '^PIXIV_REFRESH_TOKEN=..*' .env && [ ! -f data/tokens.json ]; then
    echo "[!] 还没有 Pixiv 登录凭据：请编辑 .env 填入 PIXIV_REFRESH_TOKEN，"
    echo "    或启动后按 INSTALL.md「获取 refresh_token」用网页授权登录。"
    echo "    （只跑 iwara 接口项目时不需要 Pixiv 登录。）"
fi

# ---------- 5. 启动（依赖缺失会自动安装） ----------
echo "[i] 使用 $PY $("$PY" -c 'import sys; print(sys.version.split()[0])') 启动接口项目 $SITE；首次运行会自动安装依赖…"
exec "$PY" "$SITE/run.py" "$@"
