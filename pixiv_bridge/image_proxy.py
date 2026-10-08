"""``i.pximg.net`` 图片代理：校验目标、按传输模式取流、原样透传给调用方。
"""

from urllib.parse import urlparse

import httpx

from .config import Settings
from .hoster import resolve, resolve_ech
from . import ech_client, sni_client

STREAM_CHUNK = 65536


class _BufferedImage:
    """把已完整读到的响应包装成路由需要的流式响应接口。

    直连/ECH 传输是一次性读完整包（无 SNI 的 TLS 会话不方便做流式），
    但路由只用到 status_code / headers / aiter_raw() / aclose() 四个成员。
    """

    def __init__(self, response):
        self.status_code = response.status_code
        self.headers = response.headers
        self._content = response.content

    async def aiter_raw(self):
        for start in range(0, len(self._content), STREAM_CHUNK):
            yield self._content[start : start + STREAM_CHUNK]

    async def aclose(self):
        return None


class ImageProxy:
    def __init__(self, settings, http):
        self._settings = settings
        self._http = http

    def _image_host(self):
        return urlparse(self._settings.image_base).hostname

    def resolve(self, path, url=None):
        if url:
            parsed = urlparse(url)
            if parsed.hostname != self._image_host():
                raise ValueError("仅允许代理 i.pximg.net 的图片地址")
            path = parsed.path
            if parsed.query:
                path = f"{path}?{parsed.query}"
        elif "://" in path or path.startswith("//"):
            raise ValueError("非法路径")
        if any(seg == ".." for seg in path.split("/")):
            raise ValueError("非法路径")
        return path.lstrip("/")

    async def stream(self, path):
        settings = self._settings
        host = self._image_host()
        url = f"{settings.image_base}/{path.lstrip('/')}"
        headers = {
            "Referer": settings.image_referer,
            "User-Agent": settings.user_agent,
        }
        try:
            if not settings.proxy and settings.ech:
                resp = await ech_client.request(
                    "GET",
                    url,
                    headers=headers,
                    timeout=settings.request_timeout,
                    ips=resolve_ech(host, settings.ech_ips),
                    settings=settings,
                )
                return _BufferedImage(resp)
            if not settings.proxy and settings.bypass_sni:
                # 图片 CDN 的默认虚拟主机就是 i.pximg.net，无 SNI 直连可用。
                resp = await sni_client.request(
                    "GET",
                    url,
                    headers=headers,
                    timeout=settings.request_timeout,
                    ips=resolve(host, settings.image_ips),
                )
                return _BufferedImage(resp)
        except (sni_client.SniConnectError, ech_client.EchConnectError, ech_client.EchUnavailable) as exc:
            raise httpx.ConnectError(str(exc)) from exc
        request = self._http.build_request("GET", url, headers=headers)
        return await self._http.send(request, stream=True)
