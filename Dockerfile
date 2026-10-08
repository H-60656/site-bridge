FROM python:3.12-slim

# 注意：本基础镜像（Debian bookworm）自带 OpenSSL 3.0，不含 ECH 所需的
# SSL_set1_ech_config_list（ECH 是 OpenSSL 4.0 才加入的，3.x 全系都没有）。
# 项目自带的 openssl-libs/ 目前是 win32-amd64 与 android-arm64，在 Linux 容器里
# 用不了；容器内要用 ECH 模式，请把 Linux 版库放进 openssl-libs/linux-x86_64/
# （下面的 COPY . . 会一起带进镜像），或换用自带 OpenSSL 4.0+ 的基础镜像；否则用
# PIXIV_BRIDGE_BYPASS_SNI=1 或挂 HTTPS_PROXY 走代理。
# 详见 README「ECH 模式」「OpenSSL 4.0+」。

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# 整个项目一起进镜像：框架 pixiv_bridge/ 加上**镜像里实际有的**站点与扩展目录。
# 所以这份 Dockerfile 在 pixiv-api / iwara-api 等分享包里都能直接构建 ——
# 分享包里没有的目录（比如 iwara 包里的 pixiv/）不会被 COPY，也就不会报错。
# 凭据与虚拟环境由 .dockerignore 排除。
COPY . .

# 各站点的额外依赖（curl_cffi，编译包）。目录跟着进镜像时才装；没跟进来
# 也不影响构建，真去跑那个站点时 deps.py 会自己补装。
# iwara 与 asmr_one 都要 curl_cffi，各有一份 requirements.txt。
RUN for req in iwara/requirements.txt asmr_one/requirements.txt; do \
        if [ -f "$req" ]; then pip install --no-cache-dir -r "$req"; fi; \
    done

EXPOSE 8765

# 站点用一个入口脚本挑（与 start.sh 同一套逻辑：`python -m pixiv_bridge` 的项目
# 导航，有终端就列出来问一句，没终端就用第一个站点），所以同一份镜像换个包也能
# 用；要显式指定就加环境变量：
#   docker run -e BRIDGE_SITE=iwara ...
#   docker run -e BRIDGE_SITE=asmr_one ...    （asmr.one 匿名可用，不需要登录）
# 挂扩展照旧（扩展目录已在镜像里，程序自己去找 <扩展>/extension.py）：
#   docker run -e PIXIV_BRIDGE_EXTENSIONS=novel ...
#   docker run -e PIXIV_BRIDGE_EXTENSIONS=iwara_web -e BRIDGE_SITE=iwara ...
CMD ["sh", "start.sh"]
