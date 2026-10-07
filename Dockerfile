FROM python:3.12-slim

# 注意：本基础镜像（Debian bookworm）自带 OpenSSL 3.0，不含 ECH 所需的
# SSL_set1_ech_config_list（需要 OpenSSL 3.5+）。容器内若要用 ECH 模式，
# 请换用自带 OpenSSL 3.5+ 的基础镜像；否则用 PIXIV_BRIDGE_BYPASS_SNI=1
# 或挂 HTTPS_PROXY 走代理。详见 README「ECH 模式」「OpenSSL 3.5+ 安装」。

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY pixiv_bridge pixiv_bridge
COPY novel novel
COPY run.py .

EXPOSE 8765

CMD ["python", "run.py"]
