FROM python:3.12-slim

# 注意：本基础镜像（Debian bookworm）自带 OpenSSL 3.0，不含 ECH 所需的
# SSL_set1_ech_config_list（ECH 是 OpenSSL 4.0 才加入的，3.x 全系都没有）。
# 项目自带的 openssl-libs/ 目前是 win32-amd64 与 android-arm64，在 Linux 容器里
# 用不了；容器内要用 ECH 模式，请把 Linux 版库放进 openssl-libs/linux-x86_64/
# 再一起 COPY，或换用自带 OpenSSL 4.0+ 的基础镜像；否则用
# PIXIV_BRIDGE_BYPASS_SNI=1 或挂 HTTPS_PROXY 走代理。
# 详见 README「ECH 模式」「OpenSSL 4.0+」。

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# iwara 子项目的额外依赖（curl_cffi）。镜像里默认跑纯接口（CMD 是 run.py），
# 但既然 iwara/ 已 COPY 进来，就把它的依赖一并装好，容器里改跑
# `python iwara/run.py` 即可用；不想要这几 MB 的可以删掉这两行。
COPY iwara/requirements.txt iwara/requirements.txt
RUN pip install --no-cache-dir -r iwara/requirements.txt

COPY pixiv_bridge pixiv_bridge
COPY novel novel
COPY iwara iwara
COPY iwara_web iwara_web
COPY run.py .
# 可选：容器里要用 ECH，把 Linux 版库放进 openssl-libs/linux-x86_64/ 后取消下面一行的注释。
# （COPY 要求源路径存在，仓库目前只带 Windows 与 Android 版，所以默认注释掉，保证镜像能直接构建。）
# COPY openssl-libs/linux-x86_64 openssl-libs/linux-x86_64

EXPOSE 8765

# 默认只跑纯接口。要在容器里带上小说阅读器，加一个环境变量即可 ——
# 扩展目录已 COPY 进来，程序会去找 novel/extension.py：
#   docker run -e PIXIV_BRIDGE_EXTENSIONS=novel ...
# iwara 是另一个独立接口项目，容器里改跑 `python iwara/run.py` 即可；
# 要 iwara 的视频页面再加上扩展（它调 /api/iwara/*，所以必须跟 iwara 入口一起用）：
#   docker run -e PIXIV_BRIDGE_EXTENSIONS=iwara_web python iwara/run.py
CMD ["python", "run.py"]
