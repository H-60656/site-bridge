"""E-Hentai 异步客户端。

这个站点的传输层比 asmr / iwara 都别扭，原因只有一个：**GFW 按 SNI 字符串
封 `e-hentai.org`**，而且本地 DNS 对这个域名的答案是被投毒的。

三件事决定了这里的实现：

1. **必须走 HTTP/3（QUIC）。** 因为封锁判据是 TLS ClientHello 里的明文 SNI，
   所以 TCP + TLS 无论钉哪个 IP 都是 RST —— 把 `SNI=e-hentai.org` 打到
   `github.com` 的 IP 上也照样 RST，证明它跟目标 IP 无关。QUIC 的
   ClientHello 是加密的、且 GFW 不处理 UDP:443，因此只有 H3 能通。
   ``curl_cffi`` 自带的 libcurl 编译了 ngtcp2/nghttp3，系统 curl 没有。
2. **必须借道 Cloudflare 边缘。** `e-hentai.org` 自己解析出来的那几个
   CF IP（``172.66.132.196`` / ``172.66.140.62``）在本机被 IP 级 RST，直接
   用不了。但 CF 边缘按 SNI/Host 路由内容，所以把它**钉到别的 CF anycast
   IP** 上一样能取到本站内容，见 ``EDGE_IPS``。
3. **大图不在 Cloudflare 上。** 画廊的图片挂在 ``*.hath.network``（H@H
   网络，各节点是别人的家用带宽），那里 H3 没有意义、也不需要对它借道，
   直接连即可 —— 但**节点会随机死**（实测约 1/3 取不到）。唯一有效的重试
   杠杆是：换一个 CF 边缘重新取一次 ``/s/`` 大图页，H@H 会返回**另一个**
   节点。所以 ``open_proxy`` 的重试是「重新解析 /s/ 页」而不是「重试同一个
   图片 URL」。

另外两点与 asmr 一致：``CurlOpt.RESOLVE`` **只认一条**记录（不是候选列表），
所以一个 host 一个会话、一次钉一个 IP；以及**不引入 selectolax** —— 页面结构
固定，正则够用，而且本机装不上它（见 ``parse.py`` 的模块说明）。
"""

from __future__ import annotations

import asyncio
import json
import re
from typing import Any, AsyncIterator, Iterable, Mapping, Sequence
from urllib.parse import urlparse

from curl_cffi import requests as curl_requests
from curl_cffi.const import CurlHttpVersion, CurlOpt

from . import parse as _parse

SITE = "e-hentai.org"
SITE_HOST = "e-hentai.org"
API_HOST = "api.e-hentai.org"
BASE_URL = "https://e-hentai.org"
API_URL = "https://api.e-hentai.org/api.php"

#: 借道用的 Cloudflare anycast IP。**故意不含** `e-hentai.org` 自己解析到的那
#: 几个（它们在本机被 IP 级 RST）。2026-10 实测：16 个候选的 TCP:443 全部可
#: 建连，配上 HTTP/3 后都能取到本站内容。
EDGE_IPS = (
    "104.17.0.1",
    "103.21.244.0",
    "103.21.244.1",
    "103.31.4.1",
    "104.19.0.1",
    "104.21.0.1",
    "104.24.0.1",
    "104.25.0.1",
    "104.26.0.1",
    "104.27.0.1",
    "172.66.0.1",
    "104.18.27.97",
    "104.18.20.135",
    "104.21.56.202",
    "188.114.96.9",
    "162.159.140.9",
)

#: 图片/封面所在的主机后缀（H@H 节点，**不在 Cloudflare 上**）。
MEDIA_SUFFIX = ".hath.network"
MEDIA_HOST = "hath.network"

#: 一个画廊的页数上限（详情页一次只给 20 个 `/s/` 链接，翻页靠 `?p=`）。
PAGE_SIZE = 20

_DEFAULT_HEADERS = {
    "accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,"
    "image/webp,*/*;q=0.8",
    "accept-language": "zh-CN,zh;q=0.9,en-US;q=0.8,en;q=0.7,ja;q=0.6",
    "user-agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/146.0.0.0 Safari/537.36"
    ),
    "referer": BASE_URL + "/",
    "upgrade-insecure-requests": "1",
}

_GALLERY_PATH_RE = re.compile(r"^/g/(\d+)/([0-9a-fA-F]+)/?$")
_IMAGE_PAGE_RE = re.compile(r"^/s/[0-9a-fA-F]+/\d+-\d+/?$")


class EhError(Exception):
    """E-Hentai 接口出错：``status`` 是要回给客户端的 HTTP 状态码。"""

    def __init__(self, message: str, status: int = 502, reason: str = "eh_error"):
        super().__init__(message)
        self.message = message
        self.status = status
        self.reason = reason


class EhUnavailableError(EhError):
    """连不上（边缘全挂 / 图片节点死了）。"""

    def __init__(self, message: str):
        super().__init__(message, status=502, reason="eh_unavailable")


class EhAuthError(EhError):
    """这个页面要登录（exhentai 或里站内容）。"""

    def __init__(self, message: str):
        super().__init__(message, status=401, reason="auth_required")


class EhNotFoundError(EhError):
    def __init__(self, message: str):
        super().__init__(message, status=404, reason="not_found")


# ---------------------------------------------------------------- 地址判定


def is_eh_host(host: str) -> bool:
    """这个主机是不是 `*.e-hentai.org`（= 在 Cloudflare 上、要走 H3 + 借道）。"""
    host = (host or "").lower()
    return host == SITE_HOST or host.endswith("." + SITE_HOST)


def is_media_host(host: str) -> bool:
    """这个主机是不是 H@H 图片节点（= 直连、可代转字节）。"""
    host = (host or "").lower()
    return host == MEDIA_HOST or host.endswith(MEDIA_SUFFIX)


def parse_gallery_path(url: str) -> tuple[str, str] | None:
    """``https://e-hentai.org/g/1234/abcd/`` → ``("1234", "abcd")``。"""
    parts = urlparse(url or "")
    if not is_eh_host(parts.hostname or ""):
        return None
    match = _GALLERY_PATH_RE.match(parts.path or "")
    return (match.group(1), match.group(2)) if match else None


def is_image_page(url: str) -> bool:
    """是不是 ``/s/<key>/<gid>-<n>`` 大图页。"""
    parts = urlparse(url or "")
    if not is_eh_host(parts.hostname or "") or parts.scheme not in ("http", "https"):
        return False
    return bool(_IMAGE_PAGE_RE.match(parts.path or ""))


def is_media_url(url: str) -> bool:
    """是不是一个 H@H 图片直链。"""
    parts = urlparse(url or "")
    return parts.scheme in ("http", "https") and is_media_host(parts.hostname or "")


def is_allowed_proxy(url: str) -> bool:
    """``/api/eh/proxy`` 只代转这两类地址，别的拒绝（避免变成任意代理）。"""
    return is_image_page(url) or is_media_url(url)


def split_ips(value: str) -> tuple[str, ...]:
    """``"1.1.1.1, 2.2.2.2;3.3.3.3"`` → 三元组（逗号/分号/空白都认）。"""
    items = str(value or "").replace(";", ",").replace(" ", ",").replace("\t", ",")
    return tuple(part.strip() for part in items.split(",") if part.strip())


def pagination(page: int, showing: dict | None, page_size: int = PAGE_SIZE) -> dict:
    """详情页的页码分页算术（``/gallery`` 与 ``/images`` 共用）。

    E-Hentai 的**列表页**是光标分页（``?next=``，页码会被忽略），但**详情页**
    是页码分页（0 基 ``?p=``，一屏 ``PAGE_SIZE`` 张）。后者能靠 ``showing.total``
    算出总屏数，所以这里把「还有没有下一页」这个判断收成一处，免得每个调用方
    各算一遍还各算错一次（``next_cursor`` 曾经就是写死的 ``None``）。
    """
    try:
        page = max(1, int(page))
    except (TypeError, ValueError):
        page = 1
    try:
        page_size = max(1, int(page_size))
    except (TypeError, ValueError):
        page_size = PAGE_SIZE
    raw_total = (showing or {}).get("total")
    try:
        total = int(raw_total) if raw_total not in (None, "") else None
    except (TypeError, ValueError):
        total = None
    if not total or total <= 0:
        # 数不出来时不要瞎猜：宁可说「未知」，也不给一个会把客户端带进
        # 空页的页码（那会让翻页循环停不下来）。
        return {"total": total, "total_pages": None, "next_page": None, "has_next": None}
    total_pages = (total + page_size - 1) // page_size
    return {
        "total": total,
        "total_pages": total_pages,
        "next_page": page + 1 if page < total_pages else None,
        "has_next": page < total_pages,
    }


# ---------------------------------------------------------------- 客户端


class EhClient:
    """E-Hentai 的异步客户端（curl_cffi + HTTP/3 + CF 边缘轮换）。"""

    def __init__(
        self,
        *,
        edge_ips: Sequence[str] = (),
        http3: bool = True,
        impersonate: str = "",
        timeout: float = 30.0,
        connect_timeout: float = 10.0,
        probe_timeout: float = 3.0,
        edge_attempts: int = 5,
        image_attempts: int = 4,
    ) -> None:
        self.edge_ips = tuple(edge_ips) or EDGE_IPS
        # H3 不是「可选优化」——TCP 路径在本机是死的，关掉它等于关掉整个站点。
        self.http3 = bool(http3)
        self.impersonate = impersonate or ""
        self.timeout = float(timeout)
        self.connect_timeout = float(connect_timeout)
        self.probe_timeout = float(probe_timeout)
        self.edge_attempts = max(1, int(edge_attempts))
        self.image_attempts = max(1, int(image_attempts))

        self._good_edges: list[str] | None = None
        self._edge_cursor = 0
        self._sessions: dict[tuple[str, str], Any] = {}
        self._plain: Any = None
        self._lock = asyncio.Lock()

    # ---------- 会话 ----------

    @property
    def plain_session(self):
        """不钉 IP、不强制 H3 的会话（给 ``*.hath.network`` 用）。"""
        if self._plain is None:
            self._plain = curl_requests.AsyncSession(
                curl_options={CurlOpt.CONNECTTIMEOUT: int(self.connect_timeout)}
            )
        return self._plain

    def _call_kwargs(self, extra: Mapping[str, str] | None) -> dict:
        headers = dict(_DEFAULT_HEADERS)
        if extra:
            headers.update(extra)
        kwargs: dict = {"headers": headers, "timeout": self.timeout}
        if self.impersonate:
            kwargs["impersonate"] = self.impersonate
        return kwargs

    async def probe_edge(self, ip: str) -> bool:
        """只建 TCP（不发 TLS），所以不会被 SNI 判据拦下 —— 能建连就算这个
        边缘可用。"""
        try:
            _, writer = await asyncio.wait_for(
                asyncio.open_connection(ip, 443), timeout=self.probe_timeout
            )
        except (OSError, asyncio.TimeoutError):
            return False
        writer.close()
        try:
            await writer.wait_closed()
        except Exception:  # noqa: BLE001
            pass
        return True

    async def edges(self) -> list[str]:
        """可用边缘列表（并发探测一次，之后缓存）。"""
        if self._good_edges:
            return self._good_edges
        async with self._lock:
            if self._good_edges:
                return self._good_edges
            checks = await asyncio.gather(*(self.probe_edge(ip) for ip in self.edge_ips))
            good = [ip for ip, ok in zip(self.edge_ips, checks) if ok]
            if not good:
                raise EhUnavailableError(
                    f"{len(self.edge_ips)} 个候选边缘全部连不上（HTTP/3 需要 UDP:443 出站）"
                )
            self._good_edges = good
            return good

    def next_edge(self, good: Sequence[str]) -> str:
        """按轮转取下一个边缘 —— 这是「换边缘拿新图床节点」的基础，所以**不能**
        每次都返回同一个。"""
        ip = good[self._edge_cursor % len(good)]
        self._edge_cursor += 1
        return ip

    def mark_edge_dead(self, ip: str) -> None:
        if not self._good_edges or len(self._good_edges) <= 1:
            return
        if ip in self._good_edges:
            self._good_edges.remove(ip)
        for key in [k for k in self._sessions if k[1] == ip]:
            self._sessions.pop(key, None)

    async def session_for(self, host: str, ip: str):
        key = (host, ip)
        session = self._sessions.get(key)
        if session is not None:
            return session
        options: dict = {
            # RESOLVE 只认一条记录，所以这里是「钉死单个 IP」而不是候选列表。
            CurlOpt.RESOLVE: [f"{host}:443:{ip}"],
            CurlOpt.CONNECTTIMEOUT: int(self.connect_timeout),
        }
        if self.http3:
            options[CurlOpt.HTTP_VERSION] = CurlHttpVersion.V3
        session = curl_requests.AsyncSession(curl_options=options)
        self._sessions[key] = session
        return session

    # ---------- 请求 ----------

    async def request(
        self,
        method: str,
        url: str,
        *,
        params: Mapping[str, Any] | None = None,
        headers: Mapping[str, str] | None = None,
        data: Any = None,
        stream: bool = False,
        retry_statuses: Iterable[int] = (),
        attempts: int | None = None,
    ):
        """发一次请求；``*.e-hentai.org`` 失败就换边缘重试。"""
        host = urlparse(url).hostname or ""
        kwargs = self._call_kwargs(headers)
        kwargs["params"] = params
        kwargs["data"] = data
        if stream:
            kwargs["stream"] = True

        if not is_eh_host(host):
            # H@H 节点：直连，不借道也不换 IP（换的是「重新解析 /s/ 页」）。
            return await self.plain_session.request(method, url, **kwargs)

        good = await self.edges()
        total = self.edge_attempts if attempts is None else max(1, int(attempts))
        last_exc: Exception | None = None
        for _ in range(total):
            ip = self.next_edge(good)
            session = await self.session_for(host, ip)
            try:
                resp = await session.request(method, url, **kwargs)
            except Exception as exc:  # noqa: BLE001 - 换边缘再试
                last_exc = exc
                self.mark_edge_dead(ip)
                continue
            if resp.status_code in retry_statuses:
                self.mark_edge_dead(ip)
                continue
            return resp
        raise EhUnavailableError(
            f"{method} {url} 在 {total} 个边缘上都失败：{last_exc}"
        )

    async def _get_html(self, url: str, **kw) -> str:
        resp = await self.request("GET", url, **kw)
        if resp.status_code == 401:
            raise EhAuthError(f"{url} 需要登录（里站内容或 exhentai 专属页）")
        if resp.status_code == 404:
            raise EhNotFoundError(f"{url} 不存在")
        if resp.status_code >= 400:
            raise EhError(f"GET {url} -> HTTP {resp.status_code}", status=resp.status_code)
        return resp.text

    # ---------- 列表 / 搜索 ----------

    async def list_galleries(self, cursor: str = "", query: str = "") -> dict:
        """首页或搜索结果。

        注意翻页：E-Hentai **不用页码**，``?page=N`` 会被完全忽略；唯一有效的
        是 ``?next=<上一页最后一个 gid>`` 这个光标，就在列表页的 ``id="dnext"``
        上。所以这一层给的是 ``nav.next`` 而不是页码。
        """
        params: dict = {}
        if query:
            params["f_search"] = query
        if cursor:
            params["next"] = cursor
        body = await self._get_html(BASE_URL + "/", params=params or None)
        items = _parse.parse_galleries(body)
        nav = _parse.parse_list_nav(body)
        return {
            "query": query,
            "cursor": cursor,
            "count": len(items),
            "found": nav["found"],
            "next": nav["next"],
            "prev": nav["prev"],
            "next_url": nav["next_url"],
            "items": items,
        }

    async def search(self, query: str, cursor: str = "") -> dict:
        query = (query or "").strip()
        if not query:
            raise EhError("搜索关键词为空", status=400)
        return await self.list_galleries(cursor=cursor, query=query)

    async def popular(self) -> dict:
        """``/popular`` 热门页（**一次给完，不能翻页**）。

        实测：``/popular`` 返回约 74 行、页面里**没有** ``dnext`` 那条翻页链接，
        喂 ``?next=<gid>`` 上去字节数一模一样（165242 vs 165242），所以它跟首页
        不是同一套分页。这里因此不接 ``cursor``，也不假装有下一页——只把
        ``found`` 留空（该页本来就不印 "Found N results"）。
        """
        body = await self._get_html(BASE_URL + "/popular")
        items = _parse.parse_galleries(body)
        nav = _parse.parse_list_nav(body)
        return {
            "query": "",
            "cursor": "",
            "count": len(items),
            "found": nav["found"],
            "next": "",
            "prev": "",
            "next_url": "",
            "items": items,
        }

    # ---------- 详情 / 页面 ----------

    async def gallery(self, gid: Any, token: str, page: int = 1) -> dict:
        """画廊详情页（第 ``page`` 屏的 20 个 ``/s/`` 链接一起返回）。

        详情页一次只列 20 张，第 N 屏靠 ``?p=<N-1>`` 取（E-Hentai 的详情页
        用 0 基的 ``p``）。
        """
        gid = str(gid).strip()
        token = str(token or "").strip()
        if not gid.isdigit() or not token:
            raise EhError("gid 必须是数字、token 不能为空", status=400)
        page = max(1, int(page))
        url = f"{BASE_URL}/g/{gid}/{token}/"
        if page > 1:
            url += f"?p={page - 1}"
        body = await self._get_html(url)
        data = _parse.parse_detail(body)
        if not data.get("gid"):
            data["gid"] = int(gid)
        if not data.get("token"):
            data["token"] = token
        data["page"] = page
        data["page_size"] = PAGE_SIZE
        data["page_links"] = _parse.parse_page_links(body)
        data["showing"] = _parse.parse_showing(body)
        # 详情页是**页码**分页（0 基 ?p=），所以总屏数能直接算出来；
        # 让调用方不必自己拿 showing.total 去除以 20。
        data.update(pagination(page, data["showing"], data["page_size"]))
        return data

    async def image_page(self, url: str) -> dict:
        """``/s/`` 大图页 → 真实图片直链 + 前后页。"""
        if not is_image_page(url):
            raise EhError(f"不是 E-Hentai 的大图页：{url}", status=400)
        body = await self._get_html(url)
        page = _parse.parse_s_page(body)
        page["url"] = url
        if not page.get("image_url"):
            raise EhError(f"大图页里没有图片直链：{url}", status=502)
        return page

    # ---------- 字节代理 ----------

    async def open_stream(
        self, url: str, headers: Mapping[str, str] | None = None
    ) -> tuple[int, dict, AsyncIterator[bytes]]:
        """打开一个流式下载，先拿到状态码与响应头，再惰性产字节。

        ``/h/`` 大图可以到几 MB，必须 ``stream=True``，否则整张图会先落内存。
        """
        extra = dict(headers or {})
        if not is_media_url(url):
            raise EhError(f"只代转 H@H 图片直链：{url}", status=400)
        resp = await self.request("GET", url, headers=extra, stream=True)
        response_headers = {}
        for name in (
            "content-type",
            "content-length",
            "content-range",
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

    async def open_proxy(
        self, url: str, headers: Mapping[str, str] | None = None
    ) -> tuple[int, dict, AsyncIterator[bytes]]:
        """把 ``/s/`` 大图页或 H@H 直链变成可流式转发的字节。

        ``/s/`` 页那条路带重试：解析出来的图片节点是随机的 H@H 家用节点，
        实测约 1/3 是死的，**唯一有效的重试是换一个 CF 边缘重新取 /s/ 页**
        （H@H 会返回另一个节点），而不是重试同一个已经死掉的 URL。
        """
        if is_media_url(url):
            return await self.open_stream(url, headers)

        if not is_image_page(url):
            raise EhError(f"只代转 /s/ 大图页或 H@H 直链：{url}", status=400)

        referer = {"referer": url}
        if headers:
            referer.update({k: v for k, v in headers.items() if k.lower() != "range"})
        range_header = {k: v for k, v in (headers or {}).items() if k.lower() == "range"}

        last_exc: Exception | None = None
        for _ in range(self.image_attempts):
            page = await self.image_page(url)
            image_url = page.get("image_url") or ""
            if not is_media_url(image_url):
                last_exc = EhError(f"图片直链不在 H@H 上：{image_url}", status=502)
                continue
            try:
                status, response_headers, body = await self.open_stream(
                    image_url, {**referer, **range_header}
                )
            except Exception as exc:  # noqa: BLE001 - 换边缘重解析
                last_exc = exc
                continue
            response_headers = {
                **response_headers,
                "x-eh-image-url": image_url,
                "x-eh-page-url": url,
            }
            return status, response_headers, body
        raise EhUnavailableError(
            f"{url} 连续 {self.image_attempts} 次都落在死图片节点上：{last_exc}"
        )

    # ---------- 官方 API ----------

    async def gdata(self, items: Sequence[Sequence[Any]]) -> dict:
        """``api.php`` 的 gdata：按 (gid, token) 批量取元数据。

        ``data`` 必须是 bytes，``content-type`` 得是 ``application/json``，
        否则 CF 会挡。
        """
        gidlist = []
        for entry in items or ():
            try:
                # 非数字的 gid（例如误把字符串整条传进来）要当畸形项跳过，
                # 而不是让 int() 的 ValueError 冒出去（那会变成 500）。
                gid, token = int(entry[0]), str(entry[1])
            except (TypeError, ValueError, IndexError):
                continue
            gidlist.append([gid, token])
        if not gidlist:
            raise EhError("gidlist 为空", status=400)
        payload = json.dumps(
            {"method": "gdata", "gidlist": gidlist, "namespace": 1}
        ).encode()
        resp = await self.request(
            "POST",
            API_URL,
            data=payload,
            headers={"content-type": "application/json", "accept": "application/json"},
        )
        if resp.status_code >= 400:
            raise EhError(
                f"gdata -> HTTP {resp.status_code}: {resp.text[:200]}",
                status=resp.status_code,
            )
        try:
            return resp.json()
        except Exception as exc:  # noqa: BLE001
            raise EhError(f"gdata 返回的不是 JSON：{resp.text[:200]}") from exc

    async def metadata(self, gid: Any, token: str) -> dict:
        """单个画廊的 gdata 结果（``{"gmetadata": [...]}``）。"""
        return await self.gdata([(gid, token)])

    # ---------- 诊断 ----------

    async def auth_status(self) -> dict:
        """匿名状态诊断（这个站点匿名就能用全部只读接口，登录只影响里站）。"""
        edges = await self.edges()
        return {
            "site": SITE,
            "anonymous": True,
            "http3": self.http3,
            "impersonate": self.impersonate or None,
            "edge_count": len(edges),
            "edge_ips": list(edges),
            "pinned_edge": edges[self._edge_cursor % len(edges)] if edges else None,
        }

    async def aclose(self) -> None:
        for session in self._sessions.values():
            try:
                await session.close()
            except Exception:  # noqa: BLE001
                pass
        self._sessions.clear()
        if self._plain is not None:
            try:
                await self._plain.close()
            except Exception:  # noqa: BLE001
                pass
            self._plain = None
