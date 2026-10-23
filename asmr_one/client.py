"""asmr.one 异步客户端。

为什么不能用框架的 httpx（与 iwara 同源的两个坎，但表现不同）：

1. **IP 级阻断**：asmr.one 自己的边缘 IP（``104.26.4.171`` / ``104.26.5.171``
   / ``172.67.70.244``）在本机一律被 RST —— 明文 GET 也是，所以不是 TLS 的
   问题，而是这些 IP 直接不通。实测的解法是把它钉到 **Cloudflare 别的
   anycast IP** 上（CF 边缘按 Host 头路由，换 IP 不影响内容），见
   ``SEED_IPS``。
2. **必须叠 ECH**：换 IP 之后不带 ECH 仍然全部握手失败（``curl: (35)``）。
   带上 Cloudflare 那份通用 ECH 配置后 14/15 个借道 IP 都能出 200 JSON。
   所以这个站点实际上**依赖** ECH（``AsmrSettings.ech`` 默认开）。

因此这里自带一个 ``curl_cffi`` 客户端：TLS 冒充 + 借道 IP 池 + 单个 IP 固定
+ ECH。asmr 的接口全是 JSON（不像 iwara 那样要解析 HTML），所以不需要
selectolax。
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import Any, AsyncIterator, Iterable, Mapping, Sequence
from urllib.parse import quote, urlparse

import httpx
from curl_cffi import requests as curl_requests
from curl_cffi.const import CurlOpt

from . import ech as _ech

API_HOST = "api.asmr.one"
SITE = "asmr.one"
#: 音频/视频字节所在的媒体主机（``mediaStreamUrl`` / ``mediaDownloadUrl`` 的域）。
MEDIA_HOST = "raw.kiko-play-niptan.one"

DEFAULT_IMPERSONATE = "chrome"

# DoH 只作补充：asmr.one 的 DNS 答案**没有被污染**（alidns / 223.5.5.5 / doh.pub
# 三个端点一致），真正的问题是那些 IP 本身不通，所以主力是下面的借道池。
DOH_ENDPOINTS = (
    "https://dns.alidns.com/dns-query",
    "https://doh.pub/dns-query",
    "https://cloudflare-dns.com/dns-query",
)

# 借道用的 Cloudflare anycast IP（2026-10-08 实测：20 个候选里 TCP 通 15 个，
# 其中 14 个叠 ECH 后能取到 200 JSON；同一请求连打 5 次 5/5 成功）。
# 注意这里**故意不包含** asmr.one 自己解析到的那几个 IP —— 它们在本机被 RST。
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
    "api.asmr.one": ("104.18.10.118", "104.18.11.118"),
    "asmr.one": ("104.18.10.118", "104.18.11.118"),
    "www.asmr.one": ("104.18.10.118", "104.18.11.118"),
    "raw.kiko-play-niptan.one": ("104.18.10.118", "104.18.11.118"),
}

#: ECH 只对 Cloudflare 后面的主机生效 —— 这里 API、站点与媒体主机全是。
ECH_HOSTS = (API_HOST, SITE, "www.asmr.one", MEDIA_HOST)

#: 允许代转字节的主机（``/api/asmr/stream`` 只认这些，避免变成任意代理）。
MEDIA_HOSTS = (MEDIA_HOST,)

#: 封面 ``type`` 的合法取值（实测 ``type=thumb`` 是 400；缩略图用 240x240）。
COVER_TYPES = ("main", "sam", "240x240")

_BROWSER_HEADERS = {
    "accept": "application/json, text/plain, */*",
    "accept-language": "zh-CN,zh;q=0.9,en-US;q=0.8,en;q=0.7,ja;q=0.6",
    "user-agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/146.0.0.0 Safari/537.36"
    ),
    "sec-ch-ua": '"Google Chrome";v="146", "Not_A Brand";v="8", "Chromium";v="146"',
    "sec-ch-ua-mobile": "?0",
    "sec-ch-ua-platform": '"Windows"',
    "sec-fetch-dest": "empty",
    "sec-fetch-mode": "cors",
    "sec-fetch-site": "same-site",
}


class AsmrError(Exception):
    """asmr 接口出错：``status`` 是要回给客户端的 HTTP 状态码。"""

    def __init__(self, message: str, status: int = 502, reason: str = "asmr_error"):
        super().__init__(message)
        self.message = message
        self.status = status
        self.reason = reason


class AsmrChallengeError(AsmrError):
    """Cloudflare 挑战 / 握手失败（换 IP 或开 ECH 再试）。"""

    def __init__(self, message: str):
        super().__init__(message, status=502, reason="cloudflare_challenge")


class AsmrAuthError(AsmrError):
    """这个端点要登录（asmr 只对少数端点要求，匿名够用）。"""

    def __init__(self, message: str):
        super().__init__(message, status=401, reason="auth_required")


@dataclass
class Track:
    """音轨树里的一个叶子（可播放文件）。"""

    hash: str
    title: str
    work_id: str = ""
    work_title: str = ""
    stream_url: str = ""
    download_url: str = ""
    duration: float = 0.0
    size: int = 0
    #: 所在文件夹的层级路径（``["04：omake", "03：撮り下ろし…"]``）
    folders: tuple = ()
    raw: dict = field(default_factory=dict)

    @classmethod
    def from_node(cls, node: Mapping[str, Any], folders: Sequence[str] = ()) -> "Track":
        work = node.get("work") or {}
        return cls(
            hash=str(node.get("hash") or ""),
            title=str(node.get("title") or ""),
            work_id=str(work.get("id") or ""),
            work_title=str(node.get("workTitle") or ""),
            stream_url=str(node.get("mediaStreamUrl") or ""),
            download_url=str(node.get("mediaDownloadUrl") or ""),
            duration=float(node.get("duration") or 0),
            size=int(node.get("size") or 0),
            folders=tuple(folders),
            raw=dict(node),
        )

    def as_dict(self) -> dict:
        return {
            "hash": self.hash,
            "title": self.title,
            "work_id": self.work_id,
            "work_title": self.work_title,
            "stream_url": self.stream_url,
            "download_url": self.download_url,
            "duration": self.duration,
            "size": self.size,
            "folders": list(self.folders),
        }


def flatten_tracks(nodes: Any, folders: Sequence[str] = ()) -> list[Track]:
    """把音轨树展平成叶子列表。

    上游 ``/api/tracks/{id}`` 返回的是**顶层就是 list** 的树：文件夹节点是
    ``{"type":"folder","title":...,"children":[...]}``，文件节点是
    ``{"type":"audio",...}``。浏览器（以及大多数播放器）要的是一列文件，
    所以这里按树的顺序展平，并把文件夹路径留在每个叶子上。
    """
    tracks: list[Track] = []
    for node in nodes or []:
        if not isinstance(node, Mapping):
            continue
        kind = node.get("type")
        title = str(node.get("title") or "")
        if kind == "folder":
            tracks.extend(flatten_tracks(node.get("children") or (), [*folders, title]))
        elif kind == "audio":
            tracks.append(Track.from_node(node, folders))
    return tracks


def cover_url(work_id: str, cover_type: str = "main") -> str:
    """封面地址（``type`` 必须是 ``COVER_TYPES`` 之一）。"""
    return f"https://{API_HOST}/api/cover/{work_id}.jpg?type={cover_type}"


def decorate_work(work: Any) -> Any:
    """给作品补上**本站**的封面代转地址。

    浏览器自己去 ``api.asmr.one`` 取不到图（同样的 IP 阻断），所以列表里
    每条都补一份走 ``/api/asmr/cover/{id}`` 的地址。
    """
    if not isinstance(work, Mapping) or work.get("id") is None:
        return work
    work_id = work["id"]
    return {
        **work,
        "cover_url": f"/api/asmr/cover/{work_id}?type=main",
        "thumbnail_url": f"/api/asmr/cover/{work_id}?type=240x240",
    }


class AsmrClient:
    """asmr.one 的异步客户端（curl_cffi + 借道 IP 池 + ECH）。"""

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
        self.ip_attempts = max(1, int(ip_attempts))
        self._doh_endpoints = tuple(doh_endpoints)
        # ECH 默认开：这个站点离开它就通不了（见模块 docstring）。
        self.ech = bool(ech)
        self.ech_config = ech_config
        self.ech_ips = tuple(ech_ips)
        self._ip_cache: dict[str, list[str]] = {}
        self._live_ips: dict[str, str] = {}
        self._dead_ips: dict[str, set[str]] = {}
        self._sessions: dict[str, Any] = {}
        self._doh_client: httpx.AsyncClient | None = None
        self._lock = asyncio.Lock()

    # ---------- 传输层 ----------

    @property
    def doh_client(self) -> httpx.AsyncClient:
        if self._doh_client is None:
            self._doh_client = httpx.AsyncClient(timeout=httpx.Timeout(8.0, connect=5.0))
        return self._doh_client

    async def _query_doh(self, endpoint: str, host: str) -> list[str]:
        try:
            resp = await self.doh_client.get(
                endpoint,
                params={"name": host, "type": "A"},
                headers={"accept": "application/dns-json"},
            )
            answers = resp.json().get("Answer", [])
            return [a["data"] for a in answers if a.get("type") == 1]
        except Exception:  # noqa: BLE001 - 单个解析器挂了不影响其它
            return []

    async def resolve_host(self, host: str) -> list[str]:
        """给出该主机的候选 IP：借道池优先，DNS 答案排在后面。

        asmr.one 的 DNS 没被污染，但它自己那几个 IP 是不通的，所以解析结果
        只能当补充 —— 真正常用的是 ``SEED_IPS`` 这批借道地址。
        """
        if host in self._ip_cache:
            return self._ip_cache[host]
        async with self._lock:
            if host in self._ip_cache:
                return self._ip_cache[host]
            merged: list[str] = []

            def add(ip: str) -> None:
                if ip and ip not in merged:
                    merged.append(ip)

            for ip in SEED_HOSTS.get(host, ()):
                add(ip)
            if is_ech_host(host):
                for ip in self.ech_ips:
                    add(ip)
            if host not in SEED_HOSTS:
                for ip in SEED_IPS:
                    add(ip)

            results = await asyncio.gather(
                *(self._query_doh(endpoint, host) for endpoint in self._doh_endpoints)
            )
            for ips in results:
                for ip in ips:
                    add(ip)

            if not merged:
                raise AsmrError(f"没有可用的 {host} 候选地址", status=502)
            self._ip_cache[host] = merged
            return merged

    async def _tcp_reachable(self, ip: str, port: int = 443) -> bool:
        """快速判断某个 IP 的 443 通不通（只建 TCP，不发 TLS）。"""
        try:
            _, writer = await asyncio.wait_for(
                asyncio.open_connection(ip, port), timeout=self.probe_timeout
            )
        except (OSError, asyncio.TimeoutError):
            return False
        writer.close()
        try:
            await writer.wait_closed()
        except Exception:  # noqa: BLE001
            pass
        return True

    async def pick_ip(self, host: str) -> str:
        """挑一个真的连得上的 IP。

        这里**不能**把多个 IP 一起塞进 ``CurlOpt.RESOLVE``：libcurl 对同一
        host:port 只认最后一条记录，它不是候选列表。所以只能自己先探测，
        再固定到**单个** IP，失败时换下一个。
        """
        if host in self._live_ips:
            return self._live_ips[host]
        ips = await self.resolve_host(host)
        dead = self._dead_ips.setdefault(host, set())
        candidates = [ip for ip in ips if ip not in dead] or ips
        checks = await asyncio.gather(
            *(self._tcp_reachable(ip) for ip in candidates[: self.ip_attempts])
        )
        for ip, ok in zip(candidates[: self.ip_attempts], checks):
            if ok:
                self._live_ips[host] = ip
                return ip
        raise AsmrError(
            f"{host} 的所有候选 IP 都连不上（已试 "
            f"{len(candidates[:self.ip_attempts])} 个）",
            status=502,
        )

    async def mark_ip_dead(self, host: str, ip: str) -> None:
        """某个 IP 实际请求失败时拉黑它，下次换一个候选。"""
        self._dead_ips.setdefault(host, set()).add(ip)
        if self._live_ips.get(host) == ip:
            self._live_ips.pop(host, None)
        self._sessions.pop(host, None)

    async def session_for(self, host: str):
        session = self._sessions.get(host)
        if session is None:
            ip = await self.pick_ip(host)
            options: dict = {
                # 只固定这一个 IP（CurlOpt.RESOLVE 不是候选列表！），
                # SNI/Host 仍按真实域名走。
                CurlOpt.RESOLVE: [f"{host}:443:{ip}"],
                CurlOpt.CONNECTTIMEOUT: int(self.connect_timeout),
            }
            value = self.ech_option(host)
            if value:
                options[CurlOpt.ECH] = value
            session = curl_requests.AsyncSession(curl_options=options)
            self._sessions[host] = session
        return session

    def ech_option(self, host: str) -> str | None:
        """这个 host 该不该带 ECH；带的话返回给 CURLOPT_ECH 的取值。

        会话级 ``curl_options`` 每次请求都会重新下发，所以只需在建会话时
        设一次。
        """
        if not self.ech or not _ech.available() or not is_ech_host(host):
            return None
        return _ech.ech_option(override=self.ech_config)

    def pinned_ip(self, host: str) -> str | None:
        """当前这个 host 固定用的 IP（仅用于日志/诊断）。"""
        return self._live_ips.get(host)

    def headers(self, extra: Mapping[str, str] | None = None) -> dict:
        headers = dict(_BROWSER_HEADERS)
        headers["origin"] = f"https://{self.site}"
        headers["referer"] = f"https://{self.site}/"
        if extra:
            headers.update(extra)
        return headers

    async def _call_with_ip_retry(self, host: str, call, *, retry_statuses=()):
        """在某个 host 上执行一次请求，失败就换 IP 重试。

        ``retry_statuses`` 只适合**内容主机**（媒体文件的 404 表示「这个边缘
        上没有这份内容」，换一个可能有）；API 的 404 是真实业务状态，换 IP
        也没用。
        """
        last_exc: Exception | None = None
        attempts = 0
        while attempts < self.ip_attempts:
            attempts += 1
            session = await self.session_for(host)
            ip = self.pinned_ip(host)
            try:
                resp = await call(session)
            except Exception as exc:  # noqa: BLE001 - 连不上就换 IP
                last_exc = exc
                if ip is None:
                    raise
                await self.mark_ip_dead(host, ip)
                if attempts >= self.ip_attempts:
                    raise
                continue
            if (
                resp.status_code in retry_statuses
                and ip is not None
                and attempts < self.ip_attempts
            ):
                await self.mark_ip_dead(host, ip)
                continue
            return resp
        raise last_exc if last_exc else AsmrError(f"{host} 请求失败", status=502)

    async def request(
        self,
        method: str,
        path: str,
        *,
        host: str = API_HOST,
        url: str | None = None,
        params: Mapping[str, Any] | None = None,
        headers: Mapping[str, str] | None = None,
        timeout: float | None = None,
        retry_statuses: Iterable[int] = (),
    ):
        target = url or f"https://{host}{path}"
        request_host = urlparse(target).hostname or host

        async def call(session):
            return await session.request(
                method,
                target,
                params=params,
                headers=self.headers(headers),
                timeout=timeout or self.timeout,
                impersonate=self.impersonate,
            )

        try:
            resp = await self._call_with_ip_retry(
                request_host, call, retry_statuses=tuple(retry_statuses)
            )
        except Exception as exc:  # noqa: BLE001
            raise AsmrError(f"{method} {target} 失败: {exc}", status=502) from exc

        if resp.headers.get("cf-mitigated") == "challenge" or (
            resp.status_code == 403
            and "text/html" in (resp.headers.get("content-type") or "")
        ):
            raise AsmrChallengeError(
                f"Cloudflare 挑战未通过（{target}）；impersonate={self.impersonate!r}"
            )
        return resp

    async def get_json(self, path: str, **kw) -> Any:
        resp = await self.request("GET", path, **kw)
        if resp.status_code == 401:
            raise AsmrAuthError(
                f"GET {path} 需要登录（asmr 只对少数端点要求；本站匿名可用）"
            )
        if resp.status_code >= 400:
            raise AsmrError(
                f"GET {path} -> {resp.status_code}: {resp.text[:200]}",
                status=resp.status_code,
            )
        # 200 也可能不是 JSON（Cloudflare 拦截页、上游改版回 HTML）。
        # 裸 resp.json() 抛的 JSONDecodeError 是 ValueError，路由层只兜
        # AsmrError → 会一路逃成裸 500。
        try:
            return resp.json()
        except ValueError as exc:
            raise AsmrError(
                f"GET {path} 返回的不是 JSON（可能被拦了或改版了）：{resp.text[:200]}",
                status=502,
                reason="bad_upstream_body",
            ) from exc

    # ---------- 业务接口 ----------

    async def list_works(
        self, page: int = 1, order: str = "", sort: str = ""
    ) -> dict:
        """作品列表（一页 20 条）。

        上游支持排序：``order`` 接受 ``create_date`` / ``release`` /
        ``rating`` / ``dl_count`` / ``price`` / ``random`` / ``id``，``sort``
        只接受 ``asc`` / ``desc``；非法值（例如 ``order=desc``）会被 400
        ``Invalid value`` 拒绝 —— 是拒绝非法值，不是拒绝排序本身。

        ``order`` / ``sort`` 留空时不下发这两个参数，走上游默认序。
        """
        params: dict[str, Any] = {"page": max(1, int(page))}
        if order:
            params["order"] = order
        if sort:
            params["sort"] = sort
        return await self.get_json("/api/works", params=params)

    async def search(self, keyword: str, page: int = 1) -> dict:
        """关键词搜索。

        真契约是 ``GET /api/search/{关键词}`` —— **关键词在 URL 路径里**，
        不是查询参数（``order`` / ``sort`` / ``page`` / ``pageSize`` 那些才在
        query string）。上游前端也是这么调的：``/api/search/ + encodeURIComponent(kw)``。

        ⚠️ 曾经的 ``/api/search/works`` 是**不存在的端点**：上游把 ``works``
        当成了关键词，于是任何查询都恒定返回「搜 works 这个词」的同一批
        117 条。用 ``/api/works?keyword=`` 同样不生效（返回同一份全量列表）。

        关键词必须 ``quote(..., safe="")``：带斜杠的关键词若保留 ``/``，会被
        上游当成多级路径，命中 ``404 Cannot GET /api/search/a/b``。

        空关键词上游会退化成全量列表（totalCount 62626），所以这里直接拒掉。
        """
        keyword = (keyword or "").strip()
        if not keyword:
            raise AsmrError("搜索关键词为空", status=400)
        return await self.get_json(
            f"/api/search/{quote(keyword, safe='')}", params={"page": max(1, int(page))}
        )

    async def work(self, work_id: str) -> dict:
        """作品详情（比列表多 ``review_text`` / ``progress`` / ``updated_at`` 等）。"""
        return await self.get_json(f"/api/work/{work_id}")

    async def tracks(self, work_id: str) -> list[Track]:
        """音轨树展平后的叶子列表。

        上游返回的是嵌套树（顶层就是 list），这里展平成文件列表并保留每个
        文件所在的文件夹路径。
        """
        payload = await self.get_json(f"/api/tracks/{work_id}")
        return flatten_tracks(payload)

    async def auth_me(self) -> dict:
        """登录态诊断：``{"user":{"loggedIn":...},"auth":...,"reg":...}``。"""
        return await self.get_json("/api/auth/me")

    async def fetch_cover(self, work_id: str, cover_type: str = "main") -> tuple[bytes, str]:
        """取封面字节（浏览器自己取不到，必须服务端代转）。"""
        if cover_type not in COVER_TYPES:
            raise AsmrError(f"封面 type 非法：{cover_type}", status=400)
        url = cover_url(work_id, cover_type)

        async def call(session):
            return await session.get(
                url,
                headers=self.headers(
                    {
                        "accept": "image/avif,image/webp,image/png,image/*,*/*;q=0.8",
                        "referer": f"https://{self.site}/",
                    }
                ),
                timeout=self.timeout,
                impersonate=self.impersonate,
            )

        resp = await self._call_with_ip_retry(
            API_HOST, call, retry_statuses=(404,)
        )
        if resp.status_code >= 400:
            raise AsmrError(
                f"取封面失败 {url} -> HTTP {resp.status_code}", status=resp.status_code
            )
        return resp.content, resp.headers.get("content-type") or "image/webp"

    async def probe_range(self, url: str, *, nbytes: int = 1024) -> dict:
        """确认某个媒体直链真的能出字节，并且支持 Range。"""
        host = urlparse(url).hostname or ""

        async def call(session):
            return await session.get(
                url,
                headers=self.headers({"range": f"bytes=0-{nbytes - 1}"}),
                timeout=self.timeout,
                impersonate=self.impersonate,
            )

        resp = await self._call_with_ip_retry(host, call, retry_statuses=(404,))
        head = resp.content[:12]
        return {
            "status": resp.status_code,
            "content_type": resp.headers.get("content-type"),
            "content_range": resp.headers.get("content-range"),
            "bytes": len(resp.content),
            "is_media": b"ftyp" in head or b"RIFF" in head or b"ID3" in head,
        }

    async def open_stream(
        self, url: str, headers: Mapping[str, str] | None = None
    ) -> tuple[int, dict, AsyncIterator[bytes]]:
        """打开一个流式下载，先把状态码和响应头拿回来，再惰性产出字节。

        必须用 ``stream=True``：默认行为会把整个响应体读进内存，音频动辄
        几十上百 MB，直接缓存会把服务撑爆。
        """
        host = urlparse(url).hostname or ""

        async def call(session):
            return await session.get(
                url,
                headers=self.headers(headers),
                timeout=self.timeout,
                impersonate=self.impersonate,
                stream=True,
            )

        # 只有「拿到响应头」这一步能重试 —— 一旦开始 yield 字节就不能换 IP
        # 重来了，否则会把半截音频混进同一路响应。
        resp = await self._call_with_ip_retry(host, call, retry_statuses=(404,))

        response_headers = {}
        for name in (
            "content-type",
            "content-range",
            "content-length",
            "accept-ranges",
            "etag",
            "last-modified",
        ):
            value = resp.headers.get(name)
            if value:
                response_headers[name] = value

        async def body() -> AsyncIterator[bytes]:
            try:
                async for chunk in resp.aiter_content():
                    yield chunk
            finally:
                try:
                    await resp.aclose()
                except Exception:  # noqa: BLE001
                    pass

        return resp.status_code, response_headers, body()

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


def is_ech_host(host: str) -> bool:
    """这个主机在 Cloudflare 后面吗（决定要不要带 ECH）。"""
    return host in ECH_HOSTS


def is_media_host(host: str) -> bool:
    """这个主机允许被 ``/api/asmr/stream`` 代转吗。"""
    return host in MEDIA_HOSTS
