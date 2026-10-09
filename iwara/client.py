"""iwara.tv 异步客户端。

为什么不能用主项目的 httpx：

1. 本机（以及部分网络环境）的系统 DNS 把 ``*.iwara.tv`` 解析到无关网段
   （``www.iwara.tv → 199.59.149.x``、``api.iwara.tv → 128.242.245.x``），
   连上去必然超时。**DoH 也救不了**：这网络里能连上的 DoH（doh.pub /
   alidns / 360）对 iwara 同样只给污染答案，而 cloudflare-dns.com /
   dns.google / dns.quad9.net 根本连不上（见 ``SEED_IPS`` 处的说明）。
   所以这里不依赖任何解析，直接用一张实测过的种子 IP 表，再用
   ``CURLOPT_RESOLVE`` 把请求固定到单个边缘 IP。
2. ``api.iwara.tv`` 在 Cloudflare 后面，普通 Python 的 TLS 指纹会被判定为
   机器人，返回 ``403`` + ``cf-mitigated: challenge``。需要 ``curl_cffi``
   冒充浏览器（``impersonate="chrome"``）才能拿到 200。

这里两个手段必须同时用：只修 DNS 仍被 Cloudflare 拦，只冒充 TLS 也解析不到。
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import time
from dataclasses import dataclass, field
from typing import Any, AsyncIterator, Iterable, Mapping, Sequence
from urllib.parse import urlparse

import httpx
from curl_cffi import requests as curl_requests
from curl_cffi.const import CurlOpt

from . import ech as _ech

API_HOST = "api.iwara.tv"
SITE = "www.iwara.tv"

# X-Version = sha1("{file uuid}_{expires}_{salt}")。2023 年的旧盐已失效，
# 现在生效的是下面这个（多个客户端一致）。
SALT_CURRENT = "mSvL05GfEmeEmsEYfGCnVpEjYgTJraJN"
SALT_LEGACY = "5nFp9kmbNnHdAFhaqMvt"
SALTS = (SALT_CURRENT, SALT_LEGACY)

# 视频源优先级：Source 是原画，其余是低码率转码。仅用于排序，
# 实际取用前必须逐个探测可达性（见 probe_any）。
SOURCE_PREFERENCE = ("Source", "540", "360", "preview")

DOH_ENDPOINTS = (
    "https://doh.pub/dns-query",
    "https://dns.alidns.com/dns-query",
    "https://cloudflare-dns.com/dns-query",
)

# 本机（以及大量国内线路）对 *.iwara.tv 的 DNS 是**被污染**的：系统 DNS 和
# 所有能连上的 DoH（doh.pub / alidns / 360）都会返回 Facebook、Twitter 段的
# 地址，而 cloudflare-dns.com / dns.google / dns.quad9.net 在这里根本连不上。
# 实测这些污染地址 100% 超时，但 iwara 的真实边缘 IP 一直可用。
#
# 更关键的是：视频 CDN 的边缘**按内容路由、各花名子域互通** —— 同一份内容
# 钉到池中任一可用 IP 都能取到（少数转码源除外，所以要全池轮询）。
# 因此这里维护一张实测可用的种子 IP 表，DNS 只作为补充候选：
#   * 服务主机（api / files / filesq）用 SEED_HOSTS 里对应的 IP；
#   * 视频 CDN 的花名主机（firefly / bailu / …）不在表里，直接用整池去试。
SEED_IPS = (
    "5.61.209.181",     # files/filesq 与视频边缘
    "151.115.89.180",   # firefly
    "151.115.90.3",     # yukong
    "151.115.97.14",    # blade
    "62.210.95.208",    # bailu
    "51.158.63.130",    # swan
    "62.210.173.43",    # pela
)

SEED_HOSTS = {
    "api.iwara.tv": ("104.20.28.232", "172.66.170.170"),
    "www.iwara.tv": ("104.20.28.232", "172.66.170.170"),
    "files.iwara.tv": ("5.61.209.181",),
    "filesq.iwara.tv": ("5.61.209.181",),
    "i.iwara.tv": ("5.61.209.181",),
}

# 这两个接口自己就是「认证」本身，不能被自动续期逻辑递归进去。
AUTH_PATHS = ("/user/login", "/user/token")

DEFAULT_IMPERSONATE = "chrome"

# 允许代理的图片/文件主机。缩略图在 files.iwara.tv 上，而视频源在
# 每个视频各自的花名 CDN 主机（firefly / zanya / …）上，后者由
# `is_cdn_host` 按后缀放行，不在这里穷举。
IMAGE_HOSTS = ("files.iwara.tv", "filesq.iwara.tv", "i.iwara.tv")

# ECH 只对**在 Cloudflare 后面**的主机生效：这些主机的证书是 Google Trust /
# WE1（SAN iwara.tv,*.iwara.tv），外层 SNI 用 cloudflare-ech.com 能握手成功。
# files / filesq / i 与各花名视频主机是 Let's Encrypt 的证书，不在 CF 后面，
# 给它们开 ECH 会得到 60: SSL certificate problem: Hostname mismatch。
ECH_HOSTS = (API_HOST, SITE, "iwara.tv")

_CDN_SUFFIX = ".iwara.tv"

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


class IwaraError(Exception):
    """iwara 相关错误。`status` 用于映射到 HTTP 状态码。"""

    def __init__(self, message: str, status: int = 502, reason: str = "iwara_error"):
        super().__init__(message)
        self.message = message
        self.status = status
        self.reason = reason


class IwaraChallengeError(IwaraError):
    """Cloudflare 挑战页（TLS 冒充失效或 IP 被封）。"""

    def __init__(self, message: str, status: int = 502):
        super().__init__(message, status=status, reason="cloudflare_challenge")


class IwaraAuthError(IwaraError):
    def __init__(self, message: str, status: int = 401):
        super().__init__(message, status=status, reason="auth_required")


def is_cdn_host(host: str) -> bool:
    """iwara 的 CDN 主机都挂在 .iwara.tv 下（每个视频一个花名子域）。"""
    if not host:
        return False
    return host == "iwara.tv" or host.endswith(_CDN_SUFFIX)


def is_ech_host(host: str) -> bool:
    """这个主机能不能走 ECH（即它是不是在 Cloudflare 后面）。

    只有 ``api`` / ``www`` / 裸域这三个：它们的证书由 Cloudflare 边缘签发，
    用 ``cloudflare-ech.com`` 那份通用配置能握手成功。视频与图片的 CDN
    主机（``files`` / ``filesq`` / ``i`` 与各花名子域）不在 Cloudflare
    后面，给它们开 ECH 只会握手失败。
    """
    return host in ECH_HOSTS


# 缩略图固定放在 files.iwara.tv（i.iwara.tv 是同一份存储，但会对
# 「路径以视频扩展名结尾」的请求回 Cloudflare 挑战页，所以图片也走 files）。
THUMBNAIL_HOST = "files.iwara.tv"


def thumbnail_url(video: Mapping[str, Any], *, index: int = 0, original: bool = False) -> str:
    """由视频对象算出缩略图地址。

    注意 ``thumbnail`` 字段是个整数（帧序号），不是路径 —— 真正的地址要用
    ``file.id`` 拼：``/image/{thumbnail|original}/{file.id}/thumbnail-NN.jpg``。
    """
    file_id = (video.get("file") or {}).get("id")
    if not file_id:
        return ""
    kind = "original" if original else "thumbnail"
    return f"https://{THUMBNAIL_HOST}/image/{kind}/{file_id}/thumbnail-{index:02d}.jpg"


def decorate_video(video: Mapping[str, Any]) -> dict:
    """给上游视频对象补上现成的缩略图地址，省得调用方自己拼。"""
    if not isinstance(video, Mapping) or "file" not in video:
        return dict(video) if isinstance(video, Mapping) else video
    decorated = dict(video)
    decorated["thumbnailUrl"] = thumbnail_url(video)
    decorated["posterUrl"] = thumbnail_url(video, index=1, original=True)
    return decorated


def x_version(file_url: str, salt: str = SALT_CURRENT) -> str:
    """算出取文件清单要带的 X-Version 头。

    fileUrl 形如 ``https://filesq.iwara.tv/file/<uuid>?expires=<ms>&hash=<hex>``，
    uuid 是路径最后一段，expires 是毫秒时间戳。
    """
    parsed = urlparse(file_url)
    file_id = parsed.path.rstrip("/").split("/")[-1]
    expires = ""
    for pair in parsed.query.split("&"):
        key, _, value = pair.partition("=")
        if key == "expires":
            expires = value
            break
    if not file_id or not expires:
        raise IwaraError(f"无法从 fileUrl 解析 id/expires: {file_url}", status=502)
    return hashlib.sha1(f"{file_id}_{expires}_{salt}".encode()).hexdigest()


def jwt_payload(token: str) -> dict:
    """不验签地解出 JWT 载荷，只看 type / exp。"""
    try:
        _header, body, _sig = token.split(".")
        body += "=" * (-len(body) % 4)
        return json.loads(base64.urlsafe_b64decode(body))
    except Exception:  # noqa: BLE001
        return {}


@dataclass
class Source:
    name: str
    mime: str
    view: str
    download: str

    @classmethod
    def from_api(cls, raw: Mapping[str, Any]) -> "Source":
        def absolute(value: str | None) -> str:
            if not value:
                return ""
            # API 返回的是协议相对地址（//host/...）。
            return f"https:{value}" if value.startswith("//") else value

        src = raw.get("src") or {}
        return cls(
            name=raw.get("name") or "",
            mime=raw.get("type") or "",
            view=absolute(src.get("view")),
            download=absolute(src.get("download")),
        )

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "mime": self.mime,
            "view": self.view,
            "download": self.download,
            "host": urlparse(self.download).hostname or "",
        }


@dataclass
class Resolved:
    video_id: str
    title: str
    rating: str
    file_url: str
    file_name: str
    file_size: int
    sources: list[Source]
    salt_used: str = ""
    raw: dict = field(default_factory=dict)

    def best(self) -> Source:
        by_name = {s.name: s for s in self.sources}
        for name in SOURCE_PREFERENCE:
            if name in by_name:
                return by_name[name]
        if not self.sources:
            raise IwaraError("该视频没有返回任何视频源", status=404)
        return self.sources[-1]

    def ordered(self) -> list[Source]:
        """所有源，按偏好排序，供逐主机失败重试用。"""
        ranked = [s for name in SOURCE_PREFERENCE for s in self.sources if s.name == name]
        rest = [s for s in self.sources if s not in ranked]
        return ranked + rest


class IwaraClient:
    """异步 iwara 客户端。复用一个实例，它会缓存边缘 IP 与会话。"""

    def __init__(
        self,
        *,
        site: str = SITE,
        impersonate: str | None = DEFAULT_IMPERSONATE,
        timeout: float = 30.0,
        connect_timeout: float = 8.0,
        probe_timeout: float = 3.0,
        ip_attempts: int = 7,
        access_token: str | None = None,
        refresh_token: str | None = None,
        doh_endpoints: Iterable[str] = DOH_ENDPOINTS,
        ech: bool = False,
        ech_config: str = "",
        ech_ips: Sequence[str] = (),
    ) -> None:
        self.site = site
        self.impersonate = impersonate
        self.timeout = timeout
        self.connect_timeout = connect_timeout
        self.probe_timeout = probe_timeout
        self.ip_attempts = max(1, int(ip_attempts))
        self.access_token = access_token
        self.refresh_token = refresh_token
        self._doh_endpoints = tuple(doh_endpoints)
        # ECH：只对 Cloudflare 后面的主机开（见 is_ech_host），默认关。
        # available() 在 session_for 里再判一次（参数名 ech 在这里遮蔽了模块）。
        self.ech = bool(ech)
        self.ech_config = ech_config
        self.ech_ips = tuple(ech_ips)
        # refresh token 是坏的时候，别在每个请求上重复撞墙：失败一次就记下
        # 原因并停手，退回匿名；/api/iwara/auth 会把 auth_error 报给用户。
        self.auth_error: str = ""
        self._auth_failed = False
        # 403 触发的「刷新后重放」只允许一次，避免死循环。
        self._auth_retry_used = False
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
        """给出该主机的候选 IP：种子池优先，DoH 只作补充。

        污染的 DoH 答案必须排在种子之后，否则 ``pick_ip`` 会先去试一个
        必然超时的地址。视频用花名 CDN 主机不在 ``SEED_HOSTS`` 里，此时
        整张 ``SEED_IPS`` 都是候选（边缘按内容路由，互通）。
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
            # ECH 模式下额外给的那批 IP 排在种子之后、DoH 之前。
            if is_ech_host(host):
                for ip in self.ech_ips:
                    add(ip)
            if host not in SEED_HOSTS:
                # 未登记的主机（各视频的花名 CDN 子域）走整池。
                for ip in SEED_IPS:
                    add(ip)

            results = await asyncio.gather(
                *(self._query_doh(endpoint, host) for endpoint in self._doh_endpoints)
            )
            for ips in results:
                for ip in ips:
                    add(ip)

            if not merged:
                raise IwaraError(f"没有可用的 {host} 候选地址", status=502)
            self._ip_cache[host] = merged
            return merged

    async def _tcp_reachable(self, ip: str, port: int = 443) -> bool:
        """快速判断某个边缘 IP 的 443 通不通（只建 TCP，不发 TLS）。"""
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
        """挑一个真的连得上的边缘 IP。

        这里**不能**把多个 IP 一起塞进 ``CurlOpt.RESOLVE``：libcurl 对同一
        host:port 只认最后一条记录，它不是候选列表 —— 被污染的那个 IP 只要
        排在最后就会让整个请求超时，而且不会自动换下一个。所以只能自己先
        探测，再固定到**单个** IP，并在失败时换一个。
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
        raise IwaraError(
            f"{host} 的所有候选 IP 都连不上（已试 {len(candidates[:self.ip_attempts])} 个）",
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
                # 只固定这一个 IP（CurlOpt.RESOLVE 不是候选列表！
                # 同一 host:port 传多条时 libcurl 只认最后一条），
                # SNI/Host 仍按真实域名走。
                CurlOpt.RESOLVE: [f"{host}:443:{ip}"],
                # 部分视频 CDN 主机不通，别每次都干等 21 秒。
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

        会话级 ``curl_options`` 每次请求都会重新下发（curl_cffi 的
        ``_request_once`` 把它传给 ``set_curl_options``），所以只需在建会话时
        设一次，四个 ``impersonate=`` 调用点都不用动。
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
        headers["x-site"] = self.site
        if self.access_token:
            headers["authorization"] = f"Bearer {self.access_token}"
        if extra:
            headers.update(extra)
        return headers

    async def _call_with_ip_retry(self, host: str, call, *, retry_statuses=()):
        """在某个 host 上执行一次请求，失败就换 IP 重试。

        因为 ``session_for`` 把会话固定到了单个 IP，一旦那个 IP 失联或
        「这个边缘不服务这份内容」（回 404）就必须换人：这里捕获取消/超时/
        连接类错误，并在响应命中 ``retry_statuses`` 时也视为该 IP 不可用，
        把当前 IP 拉黑后重建会话。

        注意 ``retry_statuses`` 只适合 CDN 内容主机 —— API 的 404 是真实业务
        状态，换 IP 也没用。
        """
        last_exc: Exception | None = None
        attempts = 0
        while attempts < self.ip_attempts:
            attempts += 1
            # 先建会话（首次会在这里探测并选定 IP），再读回实际固定的是哪个。
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
            if resp.status_code in retry_statuses and ip is not None and attempts < self.ip_attempts:
                await self.mark_ip_dead(host, ip)
                continue
            return resp
        raise last_exc if last_exc else IwaraError(f"{host} 请求失败", status=502)

    async def _prepare_auth(self, path: str) -> None:
        """请求业务接口前确认登录态；失败就安静退回匿名。

        有 refresh token 就能换出 access token。换不出（token 过期/被吊销）
        时不抛异常、也不拦截请求 —— iwara 的绝大部分内容匿名就能看，为了一个
        失效的 token 让整站不可用是本末倒置。原因记在 ``auth_error`` 里。
        """
        if self._auth_failed:
            return
        # 认证接口自己不能被这层拦，否则 refresh 里再触发 refresh 就递归了。
        if path in AUTH_PATHS:
            return
        if not self.refresh_token and not self.access_token:
            return
        try:
            await self.ensure_access_token()
        except IwaraError as exc:
            # IwaraAuthError 是 IwaraError 的子类，这里一并兜住。
            self._auth_failed = True
            self.auth_error = str(getattr(exc, "message", exc))
            self.access_token = None

    async def request(
        self,
        method: str,
        path: str,
        *,
        host: str = API_HOST,
        url: str | None = None,
        params: Mapping[str, Any] | None = None,
        json_body: Any = None,
        headers: Mapping[str, str] | None = None,
        timeout: float | None = None,
        retry_statuses: Iterable[int] = (),
    ):
        await self._prepare_auth(path)
        target = url or f"https://{host}{path}"
        request_host = urlparse(target).hostname or host

        async def call(session):
            return await session.request(
                method,
                target,
                params=params,
                json=json_body,
                headers=self.headers(headers),
                timeout=timeout or self.timeout,
                impersonate=self.impersonate,
            )

        try:
            resp = await self._call_with_ip_retry(
                request_host, call, retry_statuses=tuple(retry_statuses)
            )
        except Exception as exc:  # noqa: BLE001
            raise IwaraError(f"{method} {target} 失败: {exc}", status=502) from exc

        if resp.headers.get("cf-mitigated") == "challenge" or (
            resp.status_code == 403
            and "text/html" in (resp.headers.get("content-type") or "")
        ):
            raise IwaraChallengeError(
                f"Cloudflare 挑战未通过（{target}）；impersonate={self.impersonate!r}"
            )

        # 这个 API 用 403 表示「token 过期」而不是 401。带 token 收到 403 时
        # 强制刷一次再重放，但只放一次，避免 token 真失效时无限循环。
        if (
            resp.status_code == 403
            and self.refresh_token
            and not self._auth_retry_used
            and path not in AUTH_PATHS
        ):
            self._auth_retry_used = True
            try:
                self._auth_failed = False
                self.access_token = None
                await self.refresh_access()
            except IwaraError as exc:
                self.auth_error = str(getattr(exc, "message", exc))
                self._auth_failed = True
                self.access_token = None
                return resp
            return await self.request(
                method,
                path,
                host=host,
                url=url,
                params=params,
                json_body=json_body,
                headers=headers,
                timeout=timeout,
                retry_statuses=retry_statuses,
            )
        return resp

    async def get_json(self, path: str, **kw) -> Any:
        resp = await self.request("GET", path, **kw)
        if resp.status_code >= 400:
            raise IwaraError(
                f"GET {path} -> {resp.status_code}: {resp.text[:200]}",
                status=resp.status_code,
            )
        # 200 也可能不是 JSON（Cloudflare 拦截页、上游改版回 HTML）。
        # 裸 resp.json() 抛的 JSONDecodeError 是 ValueError，路由层只兜
        # IwaraError → 会一路逃成裸 500。
        try:
            return resp.json()
        except ValueError as exc:
            raise IwaraError(
                f"GET {path} 返回的不是 JSON（可能被拦了或改版了）：{resp.text[:200]}",
                status=502,
                reason="bad_upstream_body",
            ) from exc

    # ---------- 登录（尚未对着真实账号验证过） ----------

    async def login(self, email: str, password: str) -> dict:
        resp = await self.request(
            "POST", "/user/login", json_body={"email": email, "password": password}
        )
        if resp.status_code == 429:
            raise IwaraError(
                "登录被限流（errors.tooManyRequests），请等约 60 秒",
                status=429,
                reason="too_many_requests",
            )
        if resp.status_code >= 400:
            try:
                message = resp.json().get("message", "")
            except Exception:  # noqa: BLE001
                message = resp.text[:200]
            raise IwaraAuthError(f"登录失败（{resp.status_code}）: {message}")
        try:
            self.refresh_token = resp.json().get("token")
        except ValueError as exc:
            raise IwaraAuthError(
                f"登录返回的不是 JSON（可能被拦了或改版了）：{resp.text[:200]}"
            ) from exc
        if not self.refresh_token:
            raise IwaraAuthError(f"登录未返回 token: {resp.text[:200]}")
        self.access_token = None
        return {"refresh_token": self.refresh_token, "access_token": await self.refresh_access()}

    async def refresh_access(self) -> str:
        """``POST /user/token`` 用空 body，refresh token 放在 authorization 头里。"""
        if not self.refresh_token:
            raise IwaraAuthError("没有 refresh token，请先登录")
        resp = await self.request(
            "POST",
            "/user/token",
            headers={"authorization": f"Bearer {self.refresh_token}"},
        )
        if resp.status_code >= 400:
            # 注意：本 API 里过期 token 返回 403 而不是 401，两者都要当失败处理。
            raise IwaraAuthError(f"刷新 token 失败（{resp.status_code}）: {resp.text[:200]}")
        try:
            token = resp.json().get("accessToken")
        except ValueError as exc:
            raise IwaraAuthError(
                f"刷新返回的不是 JSON（可能被拦了或改版了）：{resp.text[:200]}"
            ) from exc
        if not token:
            raise IwaraAuthError(f"刷新未返回 accessToken: {resp.text[:200]}")
        self.access_token = token
        return token

    async def ensure_access_token(self) -> str:
        """访问 token 快过期时提前刷新（有效期约 1 小时）。"""
        if not self.access_token:
            return await self.refresh_access()
        payload = jwt_payload(self.access_token)
        exp = payload.get("exp")
        if not exp or exp - time.time() < 300:
            return await self.refresh_access()
        return self.access_token

    # ---------- 业务接口 ----------

    async def me(self) -> dict:
        return await self.get_json("/user")

    async def list_videos(
        self,
        *,
        page: int = 0,
        limit: int = 20,
        sort: str = "date",
        rating: str = "all",
        tags: str | None = None,
        user_id: str | None = None,
        subscribed: bool | None = None,
    ) -> dict:
        params: dict[str, Any] = {
            "page": page,
            "limit": limit,
            "sort": sort or "date",
            "rating": rating or "all",
        }
        for key, value in (("tags", tags), ("user", user_id)):
            if value:
                params[key] = value
        if subscribed is not None:
            params["subscribed"] = "true" if subscribed else "false"
        return await self.get_json("/videos", params=params)

    async def search(
        self, query: str, *, type: str = "videos", page: int = 0, limit: int = 20
    ) -> dict:
        params: dict[str, Any] = {
            "query": query,
            "type": type,
            "page": page,
            "limit": limit,
        }
        return await self.get_json("/search", params=params)

    async def video(self, video_id: str) -> dict:
        return await self.get_json(f"/video/{video_id}")

    async def related(self, video_id: str) -> dict:
        """相关视频。返回的形状和 ``/videos`` 一样：``{count,limit,page,results}``。

        每项都是完整的视频对象（含 ``file`` / ``user`` / ``tags``），所以详情页
        可以直接拿它渲染卡片，也可以复用 ``decorate_video`` 补缩略图。
        """
        return await self.get_json(f"/video/{video_id}/related")

    async def resolve(self, video_id: str) -> Resolved:
        """把视频详情换成一组可直接播放的直链。

        第一步 GET ``/video/{id}`` 拿 ``fileUrl``，第二步 GET 那个 fileUrl 并带上
        ``X-Version`` 头（一个 SHA-1）。注意这里是 GET 不是 POST，且没有 JSON body：
        少了 X-Version 头，CDN 只会返回空内容。
        """
        detail = await self.video(video_id)
        file_url = detail.get("fileUrl")
        if not file_url:
            # 有些条目本身就不是 iwara 托管的文件：`embedUrl` 指向 YouTube 等外站
            # 镜像（`file` / `fileUrl` 都是 null）。这个要跟「真需要登录」分开说，
            # 否则用户会去查登录状态，白折腾。
            if detail.get("embedUrl"):
                raise IwaraError(
                    f"视频 {video_id} 是外站嵌入（{detail['embedUrl']}），没有 iwara 直链",
                    status=404,
                    reason="embedded_only",
                )
            raise IwaraAuthError(
                f"视频 {video_id} 没有 fileUrl —— 私有视频，或需要登录", status=403
            )
        file_host = urlparse(file_url).hostname or "files.iwara.tv"

        errors: list[str] = []
        for salt in SALTS:
            resp = await self.request(
                "GET",
                "",
                url=file_url,
                host=file_host,
                headers={"x-version": x_version(file_url, salt)},
                # 边缘按内容路由：池里某些 IP 会回 404 page not found，
                # 那不是「清单不存在」，而是「这个边缘不服务这份内容」，
                # 换一个 IP 就有了。
                retry_statuses=(404,),
            )
            if resp.status_code != 200:
                errors.append(f"salt={salt}: HTTP {resp.status_code}")
                continue
            try:
                raw_sources = resp.json()
            except Exception:  # noqa: BLE001 - X-Version 不对时返回空 body
                errors.append(f"salt={salt}: 非 JSON（{len(resp.content)}B）")
                continue
            if not isinstance(raw_sources, list) or not raw_sources:
                errors.append(f"salt={salt}: 源列表为空")
                continue

            file_info = detail.get("file") or {}
            return Resolved(
                video_id=video_id,
                title=detail.get("title") or "",
                rating=detail.get("rating") or "",
                file_url=file_url,
                file_name=file_info.get("name") or "",
                file_size=int(file_info.get("size") or 0),
                sources=[Source.from_api(item) for item in raw_sources],
                salt_used=salt,
                raw=detail,
            )
        raise IwaraError(f"解析 {video_id} 失败: {'; '.join(errors)}", status=502)

    async def probe_range(self, url: str, *, nbytes: int = 1024) -> dict:
        """确认某个 CDN 直链真的能出 MP4 字节，并且支持 Range。"""
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
            "is_mp4": b"ftyp" in head,
        }

    async def probe_any(self, resolved: Resolved, *, nbytes: int = 1024) -> tuple[Source, dict]:
        """逐个源试到能出字节为止。

        每个源都在不同的花名主机上，而这些主机在本机并非全都可达
        （``pela`` / ``welt`` 超时，``firefly`` / ``zani`` / ``hanya`` 等正常），
        所以「最优先的源」不等于「能用的源」，必须按顺序失败重试。
        """
        attempts: list[str] = []
        for source in resolved.ordered():
            host = urlparse(source.download).hostname or ""
            try:
                probe = await self.probe_range(source.download, nbytes=nbytes)
            except Exception as exc:  # noqa: BLE001 - 主机不通，换下一个
                attempts.append(f"{source.name}@{host}: {exc}")
                continue
            if probe["status"] in (200, 206) and probe["is_mp4"]:
                return source, probe
            attempts.append(
                f"{source.name}@{host}: HTTP {probe['status']} "
                f"type={probe['content_type']} mp4={probe['is_mp4']}"
            )
        raise IwaraError("所有视频源都不可用；已尝试: " + "; ".join(attempts), status=502)

    async def fetch_image(self, url: str) -> tuple[bytes, str]:
        """取缩略图/原图字节。

        浏览器无法自己加载这些图：本机系统 DNS 同样解析不了 ``files.iwara.tv``，
        所以要由服务端代取。

        另外图片在 ``files.iwara.tv`` / ``filesq.iwara.tv`` / ``i.iwara.tv`` 上是
        同一份存储，而这三者的 DNS 答案会随解析器和时间变化（本机实测
        ``files`` 一度只解析到一个连不上的 IP，``i`` 也是），所以一个主机取不到
        就换另一个同路径取。
        """
        parsed = urlparse(url)
        hosts = [parsed.hostname or ""]
        for host in IMAGE_HOSTS:
            if host not in hosts:
                hosts.append(host)

        failures: list[str] = []
        for host in hosts:
            if not host:
                continue
            candidate = url
            if host != parsed.hostname:
                candidate = parsed._replace(netloc=parsed.netloc.replace(parsed.hostname, host)).geturl()
            try:
                return await self._fetch_image_once(candidate)
            except IwaraError as exc:
                failures.append(f"{host}: {exc.message}")
        raise IwaraError("取图失败；已尝试: " + "; ".join(failures), status=502)

    async def _fetch_image_once(self, url: str) -> tuple[bytes, str]:
        host = urlparse(url).hostname or ""

        async def call(session):
            return await session.get(
                url,
                headers=self.headers(
                    {
                        "accept": "image/avif,image/webp,image/png,image/svg+xml,image/*,*/*;q=0.8",
                        "referer": f"https://{self.site}/",
                    }
                ),
                timeout=self.timeout,
                impersonate=self.impersonate,
            )

        resp = await self._call_with_ip_retry(host, call, retry_statuses=(404,))
        if resp.status_code >= 400:
            raise IwaraError(
                f"取图失败 {url} -> HTTP {resp.status_code}", status=resp.status_code
            )
        content_type = resp.headers.get("content-type") or "image/jpeg"
        return resp.content, content_type

    async def open_stream(
        self, url: str, headers: Mapping[str, str] | None = None
    ) -> tuple[int, dict, AsyncIterator[bytes]]:
        """打开一个流式下载，先把状态码和响应头拿回来，再惰性产出字节。

        这里必须用 ``stream=True``：默认情况下 curl_cffi 会把整个响应体读进内存，
        视频动辄几十上百 MB，直接缓存会把服务撑爆。带 ``stream=True`` 时
        ``resp.content`` 在迭代前是空的，控制器可以先看 ``content-range``
        决定回 206 还是一整个 200。

        返回的生成器负责在结束时关闭上游响应。
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

        # 注意：只有「拿到响应头」这一步能重试——一旦开始 yield 字节就不能
        # 换 IP 重来了，否则会把半截视频混进同一路响应里。
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
