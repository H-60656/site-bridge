"""asmr.one 作品相关的只读接口。

设计取舍：与 iwara 同形 —— 只暴露「浏览 → 找到 → 拿到能听的字节」这条主链。
字节一律由服务端代转（``/api/asmr/stream``、``/api/asmr/cover/{id}``），因为
浏览器所在机器同样到不了那些 Cloudflare 边缘 IP，光给直链是播不了的。

登录相关的端点（``/api/tags``、``/api/user/works`` …）不暴露：本站匿名就能
浏览与播放，那些端点匿名一律 401。
"""

from urllib.parse import urlparse

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import StreamingResponse

from ..client import (
    COVER_TYPES,
    AsmrError,
    decorate_work,
    is_media_host,
)
from . import get_client

router = APIRouter(prefix="/api/asmr", tags=["asmr"])


def _decorate_page(page: dict) -> dict:
    """给列表里的每个作品补上封面代转地址。"""
    works = page.get("works")
    if isinstance(works, list):
        page = {**page, "works": [decorate_work(item) for item in works]}
    return page


@router.get("/works")
async def list_works(request: Request, page: int = Query(1, ge=1)):
    """作品列表（默认序，一页 20 条）。

    上游其实支持排序（``order`` ∈ ``create_date``/``release``/``rating``/
    ``dl_count``/``price``/``random``/``id``，``sort`` ∈ ``asc``/``desc``），
    非法值才会 400 —— 本站没把排序透出来，只给分页。
    """
    try:
        result = await get_client(request).list_works(page=page)
    except AsmrError as exc:
        raise HTTPException(status_code=exc.status, detail=exc.message) from exc
    return _decorate_page(result)


@router.get("/search")
async def search(
    request: Request, query: str = Query(..., min_length=1), page: int = Query(1, ge=1)
):
    """关键词搜索。

    上游真契约是 ``GET /api/search/{关键词}`` —— 关键词在**路径**里，不是
    查询参数。``/api/works?keyword=`` 与早前误用的 ``/api/search/works``
    都是不生效的（后者甚至会被当成「搜 works 这个词」，恒定返回同一批
    117 条）。本端点对外仍是 ``?query=&page=``。
    """
    try:
        result = await get_client(request).search(query, page=page)
    except AsmrError as exc:
        raise HTTPException(status_code=exc.status, detail=exc.message) from exc
    return _decorate_page(result)


@router.get("/work/{work_id}")
async def work_detail(request: Request, work_id: str):
    try:
        detail = await get_client(request).work(work_id)
    except AsmrError as exc:
        raise HTTPException(status_code=exc.status, detail=exc.message) from exc
    return decorate_work(detail)


@router.get("/tracks/{work_id}")
async def tracks(request: Request, work_id: str, flat: bool = True):
    """音轨（文件）列表。

    上游返回的是嵌套树（folder 里套 folder，叶子是 audio），这里默认展平成
    一列文件，并保留每个文件所在的文件夹路径；``flat=false`` 时按
    ``{"hash": ..., "folders": [...]}`` 的形状原样返回叶子。
    """
    try:
        items = await get_client(request).tracks(work_id)
    except AsmrError as exc:
        raise HTTPException(status_code=exc.status, detail=exc.message) from exc

    if flat:
        return {
            "work_id": work_id,
            "count": len(items),
            "tracks": [item.as_dict() for item in items],
        }
    return {
        "work_id": work_id,
        "count": len(items),
        "tracks": [{"hash": item.hash, "folders": list(item.folders)} for item in items],
    }


@router.get("/cover/{work_id}")
async def cover(
    request: Request,
    work_id: str,
    type: str = Query("main", pattern="^(" + "|".join(COVER_TYPES) + ")$"),
):
    """作品封面（``type``: main / sam / 240x240）。

    浏览器自己取不到 ``api.asmr.one`` 上的图（同样的 IP 阻断），所以由服务端
    代转。上游给的是 webp。
    """
    try:
        content, content_type = await get_client(request).fetch_cover(work_id, type)
    except AsmrError as exc:
        raise HTTPException(status_code=exc.status, detail=exc.message) from exc
    return StreamingResponse(
        iter([content]),
        media_type=content_type,
        headers={"Cache-Control": "public, max-age=86400"},
    )


@router.get("/stream")
async def stream(request: Request, url: str = Query(...)):
    """代转音频字节，透传 Range，让本机浏览器能直接播。

    直链本身是公开可取的，但浏览器所在机器到不了 ``raw.kiko-play-niptan.one``
    （同样的 Cloudflare 边缘 IP 阻断），所以必须由服务端代转。
    """
    host = urlparse(url).hostname or ""
    if not is_media_host(host):
        raise HTTPException(status_code=400, detail=f"只允许 asmr 媒体主机：{host}")

    pass_through = {}
    for name in ("range", "if-range"):
        value = request.headers.get(name)
        if value:
            pass_through[name] = value

    try:
        status, headers, body = await get_client(request).open_stream(url, pass_through)
    except AsmrError as exc:
        raise HTTPException(status_code=exc.status, detail=exc.message) from exc

    if status >= 400:
        raise HTTPException(status_code=status, detail="上游音频源不可用")

    headers["Cache-Control"] = "private, max-age=3600"
    return StreamingResponse(
        body,
        status_code=status,
        media_type=headers.get("content-type") or "audio/mp4",
        headers=headers,
    )


@router.get("/auth")
async def auth_status(request: Request):
    """登录态诊断：报匿名是否可用、当前是不是登录态。

    asmr 只对少数端点（标签 / 用户收藏 / 用户作品…）要求登录，本站不暴露那
    些端点，所以这里主要是「确认链路通不通」+「将来接登录时的落点」。
    """
    client = get_client(request)
    result = {
        "configured": False,
        "authenticated": False,
        "anonymous": True,
        "ech": bool(client.ech),
        "error": None,
    }
    try:
        info = await client.auth_me()
    except AsmrError as exc:
        result["error"] = exc.message
        return result
    user = (info or {}).get("user") or {}
    result["authenticated"] = bool(user.get("loggedIn"))
    result["anonymous"] = not result["authenticated"]
    return result
