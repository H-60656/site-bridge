"""E-Hentai 的只读接口（``/api/eh/*``）。

九个端点，和 asmr 的布局一一对应；区别只有三处，都是这个站点的传输特性逼
出来的：

- **列表翻页用光标不用页码**：E-Hentai 的列表页只认 ``?next=<最后一个 gid>``，
  ``?page=N`` 会被完全忽略（实测），所以 ``/list`` 收的是 ``cursor``。
- **详情翻页用页码**：画廊详情页反过来认 0 基的 ``?p=``，一屏 20 张，所以
  ``/gallery`` 与 ``/images`` 收的是 ``page``，并用 ``showing.total`` 算出
  总屏数与下一页（``/images`` 返回 ``next_page``）。
- **图片要服务端代转**：图片挂在 ``*.hath.network`` 上，浏览器直连能不能通
  取决于运气（节点是随机分配的家用带宽节点），所以 ``/proxy`` 由服务端取字节
  再转发，并在遇到死节点时换边缘重解析；``/page`` 则只解析直链与图元信息，
  不下载字节。
"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import StreamingResponse

from ..client import EhError, is_allowed_proxy, is_image_page
from . import get_client

router = APIRouter(prefix="/api/eh", tags=["eh"])

#: 代转时透传给上游的请求头（Range 必须留下，否则浏览器没法断点续传）。
_FORWARD_HEADERS = ("range", "if-range", "if-none-match", "if-modified-since")


def _pick_forward_headers(request: Request) -> dict:
    headers = {}
    for name in _FORWARD_HEADERS:
        value = request.headers.get(name)
        if value:
            headers[name] = value
    return headers


def _raise(exc: EhError):
    raise HTTPException(status_code=exc.status, detail=exc.message) from exc


@router.get("/list", summary="画廊列表")
async def list_galleries(
    request: Request,
    cursor: str = Query("", description="翻页光标：上一页最后一个 gid（首页留空）"),
    query: str = Query("", description="同时做关键词搜索时填这里"),
    direction: str = Query(
        "next",
        pattern="^(next|prev)$",
        description="翻页方向：next 向后（默认），prev 向前",
    ),
):
    """首页（或带关键词的搜索页）。

    返回里带 ``next`` / ``prev`` 两个光标。**用哪个光标就得配哪个方向**：往回翻要
    传 ``direction=prev``，不能把 ``prev`` 的值塞进 ``cursor`` 当 next 用——那样
    上游不会报错，只会静默返回错误的一页。``next_url`` / ``prev_url`` 是上游原始
    地址，仅供排查。
    """
    client = get_client(request)
    try:
        return await client.list_galleries(
            cursor=cursor.strip(), query=query.strip(), direction=direction
        )
    except EhError as exc:
        _raise(exc)


@router.get("/popular", summary="热门画廊")
async def popular(request: Request):
    """``/popular`` 热门页（**一次给完，没有翻页**）。

    实测这一页不印 ``Found N results``、也没有 ``dnext`` 光标，喂 ``?next=`` 上去
    返回的字节数完全一样，所以它跟 ``/list`` 不是同一套分页：这里只返回一页，
    ``next`` 永远为空，别拿它循环翻页。
    """
    client = get_client(request)
    try:
        return await client.popular()
    except EhError as exc:
        _raise(exc)


@router.get("/search", summary="关键词搜索")
async def search(
    request: Request,
    query: str = Query(..., min_length=1, description="搜索词，支持 E-Hentai 的语法"),
    cursor: str = Query("", description="翻页光标"),
    direction: str = Query(
        "next",
        pattern="^(next|prev)$",
        description="翻页方向：next 向后（默认），prev 向前",
    ),
):
    """搜索画廊（``f_search`` 参数，语法与站内搜索框一致）。

    与 ``/list`` 一样，往回翻要传 ``direction=prev``，且光标必须来自**同一个
    查询**的结果页——上游的光标不能跨查询复用。
    """
    client = get_client(request)
    try:
        return await client.search(query, cursor=cursor.strip(), direction=direction)
    except EhError as exc:
        _raise(exc)


@router.get("/gallery/{gid}/{token}", summary="画廊详情")
async def gallery(
    request: Request,
    gid: str,
    token: str,
    page: int = Query(1, ge=1, description="第几屏（一屏 20 张图片链接）"),
):
    """元数据 + 标签 + 条带封面 + 这一屏的 ``/s/`` 大图页链接。

    ``page`` 从 1 开始（内部换算成上游 0 基的 ``p``）。``showing`` 里给出
    ``total``，据此能算出总屏数。
    """
    client = get_client(request)
    try:
        return await client.gallery(gid, token, page=page)
    except EhError as exc:
        _raise(exc)


@router.get("/images/{gid}/{token}", summary="列出图片页地址")
async def images(
    request: Request,
    gid: str,
    token: str,
    page: int = Query(1, ge=1),
):
    """这一屏的 ``/s/`` 大图页地址列表（把它们逐个丢给 ``/proxy`` 即可看图）。

    顺带回真实分页信息：``total`` 是全画廊图片数，``total_pages`` 是总屏数，
    ``next_page`` 是下一屏页码（最后一屏为 ``null``）。``page`` 超出总屏数时
    ``items`` 会为空、``next_page`` 也是 ``null``。
    """
    client = get_client(request)
    try:
        data = await client.gallery(gid, token, page=page)
    except EhError as exc:
        _raise(exc)
    links = data.get("page_links") or []
    page_size = data.get("page_size") or 20
    return {
        "gid": data.get("gid"),
        "token": data.get("token"),
        "page": data.get("page"),
        "page_size": page_size,
        "total": data.get("total"),
        "total_pages": data.get("total_pages"),
        "next_page": data.get("next_page"),
        "has_next": data.get("has_next"),
        "count": len(links),
        "items": [
            {"index": (page - 1) * page_size + offset + 1, "url": url}
            for offset, url in enumerate(links)
        ],
        "next_cursor": None,
    }


@router.get("/page", summary="解析大图页直链")
async def image_page(
    request: Request,
    url: str = Query(..., description="/s/ 大图页地址（例如 /images 返回的 items[].url）"),
):
    """把 ``/s/`` 大图页解析成**真实图片直链**与图元信息（不下字节）。

    和 ``/proxy`` 的分工：``/proxy`` 取字节给你看图，``/page`` 只告诉你在哪、
    多大——因为直链在 ``*.hath.network`` 上是随机的 H@H 节点，客户端想自己下
    （或换个 Referer 自己试）时用它。直链同样只在 H@H 上才会返回，否则 502。
    """
    if not is_image_page(url):
        raise HTTPException(
            status_code=400,
            detail="只解析 e-hentai.org 的 /s/ 大图页，其它地址一律拒绝",
        )
    client = get_client(request)
    try:
        page = await client.image_page(url)
    except EhError as exc:
        _raise(exc)
    return page


@router.get("/metadata", summary="官方 gdata 元数据")
async def metadata(
    request: Request,
    gid: int = Query(..., ge=1, description="画廊 gid"),
    token: str = Query(..., min_length=1, description="画廊 token"),
):
    """走官方 ``api.php`` 的 gdata（不走页面解析，字段更规整）。"""
    client = get_client(request)
    try:
        return await client.metadata(gid, token)
    except EhError as exc:
        _raise(exc)


@router.get("/proxy", summary="代转图片字节")
async def proxy(
    request: Request,
    url: str = Query(..., description="/s/ 大图页地址，或直接给 H@H 图片直链"),
):
    """服务端取图再转发（``/s/`` 页会自动解析出直链并处理死节点）。

    只接受 E-Hentai 自己的大图页与 ``*.hath.network`` 直链，别的地址一律 400，
    免得这里变成任意代理。
    """
    if not is_allowed_proxy(url):
        raise HTTPException(
            status_code=400,
            detail="只代转 e-hentai.org 的 /s/ 大图页或 *.hath.network 图片直链",
        )
    client = get_client(request)
    try:
        status, headers, body = await client.open_proxy(
            url, _pick_forward_headers(request)
        )
    except EhError as exc:
        _raise(exc)

    if status >= 400:
        # 先把上游响应关掉，否则连接会一直挂着。
        async for _ in body:  # noqa: B007 - 只为触发 finally 里的 aclose
            break
        raise HTTPException(status_code=status, detail=f"上游返回 HTTP {status}")

    headers.setdefault("cache-control", "public, max-age=86400")
    return StreamingResponse(body, status_code=status, headers=headers)


@router.get("/auth", summary="登录状态")
async def auth(request: Request):
    """匿名状态与当前借道诊断（这个站点匿名就能用全部只读接口）。"""
    client = get_client(request)
    try:
        return await client.auth_status()
    except EhError as exc:
        _raise(exc)
