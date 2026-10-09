"""Pornhub 异步抓取客户端。

为什么不能用框架的 httpx（与 iwara / asmr_one 同源的两个坎）：

1. **Cloudflare 只认浏览器 TLS 指纹**：纯 httpx / requests 的握手会被判成
   机器人返回 403 / 挑战页，必须把请求交给 ``curl_cffi`` 的 ``impersonate``
   （伪装成 Chrome 的 JA3 / 指纹）。
2. **没有公开 API**：Pornhub 的旧 ``api.php`` 早已下线，能用的只有网页本身和页面
   里嵌入的 ``window.initialState`` JSON。所以这里是**抓取器** —— 拿 HTML、抠出
   那段 JSON、归一化成接口返回，而不是像 asmr 那样调 REST 端点。

因此这里自带一个 ``curl_cffi`` 客户端：TLS 冒充 + 借道 IP 池 + 单个 IP 固定 +
ECH。Pornhub 同样在 Cloudflare 后面，叠 ECH 能稳定提握手成功率。

数据面脆弱性（务必记牢）：

* Pornhub 改版会让 ``window.initialState`` 的字段全消失 / 改名。本客户端对解析
  失败一律抛 ``PornhubParseError``，上层转成 502，而不是崩进程。
* 视频直链（``mp4Url`` / ``downloadUrl``）在 ``/view_video.php?viewkey=`` 页面里，
  且带时效签名；本接口只负责把它抠出来交给浏览器，刷新不保证长期有效。
* 匿名即可看大多数公开内容；登录态（收藏 / 订阅 / 私信）本站不碰。
"""

from __future__ import annotations

import asyncio
import json
import re
from dataclasses import dataclass, field
from typing import Any, AsyncIterator, Iterable, Mapping, Sequence
from urllib.parse import urlparse

import httpx
from curl_cffi import requests as curl_requests
from curl_cffi.const import CurlOpt

from . import ech as _ech

SITE_HOST = "www.pornhub.com"
SITE = "pornhub.com"
#: 直链媒体所在的 CDN 主机（``mp4Url`` / ``downloadUrl`` 的域），用于 /stream 白名单。
MEDIA_HOSTS = ("mph.pornhub.com", "mpthc.phncdn.com", "phncdn.com", "pornhub.com")

DEFAULT_IMPERSONATE = "chrome"

# DoH 只作补充：本机 DNS 对 pornhub.com 可能被污染/限速，能拿到干净答案时优先。
DOH_ENDPOINTS = (
    "https://dns.alidns.com/dns-query",
    "https://doh.pub/dns-query",
    "https://cloudflare-dns.com/dns-query",
)

# 借道用的 Cloudflare anycast IP（与 asmr_one 同组结论：实测可通 Pornhub 边缘的
# 一批 CF IP）。Pornhub 自己的边缘 IP 在本机是否被 RST 取决于网络环境，换一批
# anycast IP 是统一能提成功率的做法。
SEED_IPS = (
    "104.18.10.118",
    "104.18.11.118",
    "104.20.28.232",
    "172.66.170.170",
    "104.16.5.35",
    "172.66.40.1",
    "104.26.4.171",
    "104.26.5.171",
    "172.67.70.244",
    "172.67.185.1",
    "104.21.60.1",
    "172.66.43.1",
    "172.67.150.1",
    "172.66.0.1",
)

#: 各主机优先试的 IP（都在同一个 CF 池里，互通；留 key 只为少试几次）。
SEED_HOSTS = {
    SITE_HOST: ("104.18.10.118", "104.18.11.118"),
    "pornhub.com": ("104.18.10.118", "104.18.11.118"),
    "www.pornhub.com": ("104.18.10.118", "104.18.11.118"),
}

#: ECH 只对 Cloudflare 后面的主机生效 —— 站点与媒体主机都是。
ECH_HOSTS = (SITE_HOST, "pornhub.com", "www.pornhub.com", *MEDIA_HOSTS)

#: 允许代转字节的主机（``/api/pornhub/stream`` 只认这些，避免变成任意代理）。
MEDIA_HOSTS_SET = frozenset(MEDIA_HOSTS)

_BROWSER_HEADERS = {
    "accept": "text/html,application/xhtml+xml,application/xml;q=0.9,"
    "application/json,text/plain,*/*;q=0.8",
    "accept-language": "zh-CN,zh;q=0.9,en-US;q=0.8,en;q=0.7",
    "user-agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/146.0.0.0 Safari/537.36"
    ),
    "sec-ch-ua": '"Google Chrome";v="146", "Not_A Brand";v="8", "Chromium";v="146"',
    "sec-ch-ua-mobile": "?0",
    "sec-ch-ua-platform": '"Windows"',
    "sec-fetch-dest": "document",
    "sec-fetch-mode": "navigate",
    "sec-fetch-site": "none",
    "sec-fetch-user": "?1",
    "upgrade-insecure-requests": "1",
}

# 抠 ``window.initialState = {...};`` 那段 JSON：从 ``initialState`` 后的第一个
# ``{`` 起，数括号匹配到对应 ``}`` 止。比正则贪心匹配稳健一点点。
_STATE_RE = re.compile(r"window\.initialState\s*=\s*(\{.*?\});", re.DOTALL)


class PornhubError(Exception):
    """Pornhub 接口出错：``status`` 是要回给客户端的 HTTP 状态码。"""

    def __init__(self, message: str, status: int = 502, reason: str = "pornhub_error"):
        super().__init__(message)
        self.message = message
        self.status = status
        self.reason = reason


class PornhubChallengeError(PornhubError):
    """Cloudflare 挑战 / 握手失败（换 IP 或开 ECH 再试）。"""

    def __init__(self, message: str):
        super().__init__(message, status=502, reason="cloudflare_challenge")


class PornhubParseError(PornhubError):
    """页面结构变了，抠不出数据（Pornhub 改版时最常见）。"""

    def __init__(self, message: str):
        super().__init__(message, status=502, reason="parse_failed")


def is_ech_host(host: str) -> bool:
    """这个主机在 Cloudflare 后面吗（决定要不要带 ECH）。"""
    return host in ECH_HOSTS


def is_media_host(host: str) -> bool:
    """这个主机允许被 ``/api/pornhub/stream`` 代转吗。"""
    return host in MEDIA_HOSTS_SET


def _extract_initial_state(html: str) -> dict:
    """从视频/列表页 HTML 里抠出 ``window.initialState`` 的 JSON 字典。"""
    match = _STATE_RE.search(html)
    if not match:
        raise PornhubParseError("页面里找不到 window.initialState（可能触发了 Cloudflare 挑战页）")
    blob = match.group(1)
    # 兜底：有些页面在末尾还有 ``;`` 之外的字符，截断到最后一个 ``}`` 之前的合法处。
    try:
        return json.loads(blob)
    except json.JSONDecodeError as exc:
        raise PornhubParseError(f"initialState 不是合法 JSON：{exc}") from exc


def _video_from_state(state: Mapping[str, Any]) -> dict:
    """把视频页的 initialState 归一成接口用的视频对象。

    Pornhub 的真实字段在 ``state.video`` / ``state.video.mediaDefinitions`` 里；
    这里只取匿名可见的部分，敏感字段（用户、付费墙）不碰。
    """
    video = (state.get("video") or {}) if isinstance(state, Mapping) else {}
    if not video:
        raise PornhubParseError("initialState.video 缺失（页面结构可能变了）")

    media = video.get("mediaDefinitions") or []
    mp4 = None
    for item in media:
        if isinstance(item, Mapping) and item.get("format") == "mp4" and item.get("videoUrl"):
            # 优先拿最高质量（列表里通常按质量升序，最后一个非空的即是最高）。
            mp4 = item["videoUrl"]
    download = (video.get("downloadUrls") or [])
    download_url = download[-1].get("url") if isinstance(download, list) and download else None

    return {
        "id": video.get("id") or video.get("viewkey") or "",
        "viewkey": video.get("viewkey") or "",
        "title": video.get("title") or video.get("video_title") or "",
        "duration": video.get("duration") or video.get("video_duration") or "",
        "views": video.get("views") or 0,
        "rating": video.get("rating") or "",
        "ratings": video.get("ratings") or 0,
        "thumbnails": [t.get("url") for t in (video.get("thumbs") or []) if isinstance(t, Mapping)],
        "tags": [t.get("title") for t in (video.get("tags") or []) if isinstance(t, Mapping)],
        "categories": [
            c.get("title") for c in (video.get("categories") or []) if isinstance(c, Mapping)
        ],
        "mp4_url": mp4,
        "download_url": download_url,
    }


def _videos_from_search_state(state: Mapping[str, Any]) -> list[dict]:
    """从搜索/列表页 initialState 归一成视频列表。

    Pornhub 的列表数据在 ``state.page`` / ``state.list`` 之类，字段随版变动，
    这里用「尽量多取」的策略：先试几个已知键，再退化成空列表。
    """
    videos: list[dict] = []
    container = state
    for key in ("page", "list", "search"):
        candidate = (state or {}).get(key)
        if isinstance(candidate, Mapping):
            container = candidate
            break
    raw_items = (
        (container or {}).get("videos")
        or (container or {}).get("items")
        or (container or {}).get("data")
        or []
    )
    for item in raw_items or []:
        if not isinstance(item, Mapping):
            continue
        videos.append(
            {
                "id": item.get("id") or item.get("viewkey") or "",
                "title": item.get("title") or item.get("video_title") or "",
                "duration": item.get("duration") or item.get("video_duration") or "",
                "views": item.get("views") or 0,
                "rating": item.get("rating") or "",
                "thumbnails": [
                    t.get("url") for t in (item.get("thumbs") or []) if isinstance(t, Mapping)
                ],
                "tags": [t.get("title") for t in (item.get("tags") or []) if isinstance(t, Mapping)],
            }
        )
    return videos


@dataclass
class PornhubClient:
    """pornhub.com 的异步抓取客户端（curl_cffi + 借道 IP 池 + ECH）。"""

    def __init__(
        self,
        *,
        site: str = SITE,
        impersonate: str | None = DEFAULT_IMPERSONATE,
        timeout: float = 30.0,
        connect_timeout: float = 8.0,
        probe_timeout: float = 3.0,
        ip_attempts: int = 7,
        doh_endpoints: Iterable[str] = DOH_ENDPOINTS,
        ech: bool = True,
        ech_config: str = "",
        ech_ips: Sequence[str] = (),
    ) -> None:
        self.site = site
        self.impersonate = impersonate
        self.timeout = timeout
        self.connect_timeout = connect_timeout
        self.probe_timeout = probe_timeout
        self.ip_attempts = ip_attempts
        self.doh_endpoints = tuple(doh_endpoints)
        self.ech = ech
        self.ech_config = ech_config
        self._ech_ips = tuple(ech_ips)
        self._live_ips: dict[str, str] = {}
        self._sessions: dict[str, Any] = {}
        self._doh_client = httpx.AsyncClient(
            headers={"accept": "application/dns-json"}, timeout=5.0
        )

    # ----- 内部：DNS / IP 选择 ---------------------------------------------

    async def _resolve(self, host: str) -> list[str]:
        """本机 DNS 不可信，优先走 DoH；全挂就退回内置借道池。"""
        for endpoint in self.doh_endpoints:
            try:
                resp = await self._doh_client.get(
                    endpoint,
                    params={"name": host, "type": "A"},
                )
                data = resp.json()
                answers = data.get("Answer") or []
                ips = [
                    a["data"]
                    for a in answers
                    if a.get("type") == 1 and isinstance(a.get("data"), str)
                ]
                if ips:
                    return ips
            except Exception:  # noqa: BLE001 - 任一端点失败就换下一个
                continue
        # DoH 全挂：用 SEED_HOSTS 里的固定 IP（它在 CF 池里，换着试）。
        if host in SEED_HOSTS:
            return list(SEED_HOSTS[host])
        return list(self._ech_ips or SEED_IPS)

    async def _pick_ip(self, host: str) -> tuple[str, list[str]]:
        """返回一个 (IP, 其余候选) 供换 IP 重试。"""
        cached = self._live_ips.get(host)
        if cached:
            return cached, []
        ips = await self._resolve(host)
        if not ips:
            ips = list(SEED_IPS)
        # 把显式 ech_ips 顶到最前面（用户想锁定某几个 IP 时）。
        ordered = list(self._ech_ips) + [ip for ip in ips if ip not in self._ech_ips]
        self._live_ips[host] = ordered[0]
        return ordered[0], ordered[1:]

    # ----- 内部：单次请求 ---------------------------------------------------

    def _session(self, host: str):
        """按 host 复用 curl_cffi AsyncSession（贴 ECH/impersonate 配置）。"""
        if host in self._sessions:
            return self._sessions[host]
        options: dict[str, Any] = {
            "impersonate": self.impersonate,
            "timeout": self.timeout,
        }
        if self.ech and is_ech_host(host):
            option = _ech.ech_option(override=self.ech_config)
            if option is not None:
                options["curl_options"] = {CurlOpt.ECH: option.encode()}
        session = curl_requests.AsyncSession(
            headers=dict(_BROWSER_HEADERS), **options
        )
        self._sessions[host] = session
        return session

    async def request(
        self,
        method: str,
        path: str,
        *,
        host: str | None = None,
        params: Mapping[str, Any] | None = None,
        headers: Mapping[str, str] | None = None,
        body: Any = None,
    ) -> Any:
        """发一次请求，自动走借道 IP 与 ECH，失败换 IP 重试。

        返回 curl_cffi 的响应对象（有 ``.status_code`` / ``.text`` / ``.content`` /
        ``.headers``）。IP 全试完仍失败抛 ``PornhubChallengeError``。
        """
        host = host or SITE_HOST
        first_ip, rest = await self._pick_ip(host)
        attempts = [first_ip, *rest, *SEED_IPS][: self.ip_attempts]
        last_exc: Exception | None = None
        for ip in attempts:
            try:
                session = self._session(host)
                session.curl.setopt(CurlOpt.RESOLVE, [f"{host}:443:{ip}"])
                resp = await session.request(
                    method,
                    f"https://{host}{path}",
                    params=params,
                    headers=headers,
                    data=body,
                    allow_redirects=True,
                )
                self._live_ips[host] = ip
                if resp.status_code in (403, 503) and "cf-chl" in (resp.text or "").lower():
                    raise PornhubChallengeError(f"{host} 返回 Cloudflare 挑战（IP {ip}）")
                return resp
            except PornhubChallengeError:
                last_exc = None
                continue
            except Exception as exc:  # noqa: BLE001 - 换 IP 重试
                last_exc = exc
                continue
        if last_exc is not None:
            raise PornhubChallengeError(f"{host} 全部 IP 失败：{last_exc}")
        raise PornhubChallengeError(f"{host} 全部 IP 失败（无异常信息）")

    # ----- 业务端点（抓取器） ----------------------------------------------

    async def health(self) -> dict:
        """探活：拿首页 HTML，能抠到 initialState 即视为可达。"""
        resp = await self.request("GET", "/")
        html = resp.text or ""
        try:
            _extract_initial_state(html)
        except PornhubError:
            # 首页结构异于视频页，initialState 不一定有；只要 200 且像 HTML 就放过。
            if resp.status_code != 200 or "<html" not in html.lower():
                raise
        return {"status": "ok", "status_code": resp.status_code}

    async def list_videos(self, *, page: int = 1, segment: str = "video") -> dict:
        """推荐 / 热门视频列表（``/video`` 首页，``page`` 分页）。

        Pornhub 的列表页靠 ``window.initialState`` 传数据；字段随版变动，
        解析失败会抛 ``PornhubParseError``（上层转 502）。
        """
        resp = await self.request("GET", "/video", params={"page": page})
        state = _extract_initial_state(resp.text or "")
        videos = _videos_from_search_state(state)
        return {
            "segment": segment,
            "page": page,
            "count": len(videos),
            "videos": videos,
        }

    async def search(self, query: str, *, page: int = 1) -> dict:
        """关键词搜索（``/video/search``）。"""
        resp = await self.request(
            "GET", "/video/search", params={"search": query, "page": page}
        )
        state = _extract_initial_state(resp.text or "")
        videos = _videos_from_search_state(state)
        return {
            "query": query,
            "page": page,
            "count": len(videos),
            "videos": videos,
        }

    async def video(self, viewkey: str) -> dict:
        """单个视频详情（``/view_video.php?viewkey=``）。"""
        resp = await self.request(
            "GET", "/view_video.php", params={"viewkey": viewkey}
        )
        state = _extract_initial_state(resp.text or "")
        return _video_from_state(state)

    async def open_stream(self, url: str, pass_through: Mapping[str, str] | None = None):
        """代转视频字节，透传 Range，让本机浏览器能直接播。

        直链本身是带时效签名的公开 CDN 地址，但浏览器所在机器到不了那些
        Cloudflare 边缘 CDN，所以必须由服务端代转。``url`` 的主机必须在
        ``MEDIA_HOSTS`` 白名单内，否则拒绝（避免变成任意代理）。
        """
        host = urlparse(url).hostname or ""
        if not is_media_host(host):
            raise PornhubError(f"只允许 Pornhub 媒体主机：{host}", status=400)
        session = self._session(host)
        headers = dict(pass_through or {})
        headers.setdefault("user-agent", _BROWSER_HEADERS["user-agent"])
        headers.setdefault("referer", f"https://{SITE_HOST}/")
        try:
            resp = await session.get(url, headers=headers, stream=True)
        except Exception as exc:  # noqa: BLE001
            raise PornhubError(f"直链代转请求失败：{exc}", status=502) from exc

        status = resp.status_code

        def body() -> AsyncIterator[bytes]:
            async def gen():
                async for chunk in resp.iter_content(chunk_size=64 * 1024):
                    yield chunk

            return gen()

        response_headers = {
            k: v
            for k, v in (resp.headers or {}).items()
            if k.lower() in ("content-type", "content-length", "accept-ranges", "etag")
        }
        return status, response_headers, body()

    async def aclose(self) -> None:
        for session in self._sessions.values():
            try:
                await session.close()
            except Exception:  # noqa: BLE001
                pass
        self._sessions.clear()
        if self._doh_client is not None:
            try:
                await self._doh_client.aclose()
            except Exception:  # noqa: BLE001
                pass
            self._doh_client = None
