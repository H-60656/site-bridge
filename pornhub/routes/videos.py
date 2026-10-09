"""Pornhub 视频相关的只读接口（匿名可达）。

设计取舍：与 asmr_one 同形 —— 只暴露「浏览 → 找到 → 拿到能播的直链」这条主链。
Pornhub 没有公开 API，所以这里的每个端点都是**抓取器**：拿网页 → 抠
``window.initialState`` → 归一化。解析失败会转成 502（Pornhub 改版时常见），
而不是崩进程。

登录相关的端点（收藏 / 订阅 / 私信）不暴露：本站匿名就能看大多数公开内容，
那些端点需要登录态，且账号抓取更容易被风控。
"""

from urllib.parse import urlparse

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import StreamingResponse

from ..client import PornhubError, is_media_host
from . import get_client

router = APIRouter(prefix="/api/pornhub", tags=["pornhub"])


@router.get("/health")
async def health(request: Request):
    """探活：能拿到首页 HTML（或 initialState）即视为链路通。"""
    try:
        result = await get_client(request).health()
    except PornhubError as exc:
        raise HTTPException(status_code=exc.status, detail=exc.message) from exc
    return result


@router.get("/videos")
async def list_videos(request: Request, page: int = Query(1, ge=1)):
    """推荐 / 热门视频列表（``/video`` 首页，按页分页）。"""
    try:
        return await get_client(request).list_videos(page=page)
    except PornhubError as exc:
        raise HTTPException(status_code=exc.status, detail=exc.message) from exc


@router.get("/search")
async def search(
    request: Request,
    query: str = Query(..., min_length=1),
    page: int = Query(1, ge=1),
):
    """关键词搜索（``/video/search?search=``）。"""
    try:
        return await get_client(request).search(query, page=page)
    except PornhubError as exc:
        raise HTTPException(status_code=exc.status, detail=exc.message) from exc


@router.get("/video/{viewkey}")
async def video(request: Request, viewkey: str):
    """单个视频详情（含直链 ``mp4_url`` / ``download_url``）。

    直链带时效签名；要播就把它交给 ``/api/pornhub/stream?url=`` 代转。
    """
    try:
        return await get_client(request).video(viewkey)
    except PornhubError as exc:
        raise HTTPException(status_code=exc.status, detail=exc.message) from exc


@router.get("/stream")
async def stream(request: Request, url: str = Query(...)):
    """代转视频字节，透传 Range，让本机浏览器能直接播。

    直链本身在 Cloudflare 的媒体 CDN 上，浏览器所在机器同样到不了，所以由服务端
    代转。``url`` 的主机必须在 Pornhub 媒体白名单内，否则 400（避免变成任意代理）。
    """
    host = urlparse(url).hostname or ""
    if not is_media_host(host):
        raise HTTPException(status_code=400, detail=f"只允许 Pornhub 媒体主机：{host}")

    pass_through = {}
    for name in ("range", "if-range"):
        value = request.headers.get(name)
        if value:
            pass_through[name] = value

    try:
        status, headers, body = await get_client(request).open_stream(url, pass_through)
    except PornhubError as exc:
        raise HTTPException(status_code=exc.status, detail=exc.message) from exc

    if status >= 400:
        raise HTTPException(status_code=status, detail="上游视频源不可用")

    headers["Cache-Control"] = "private, max-age=3600"
    return StreamingResponse(
        body,
        status_code=status,
        media_type=headers.get("content-type") or "video/mp4",
        headers=headers,
    )
