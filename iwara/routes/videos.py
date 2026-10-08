"""iwara 视频相关的只读接口。

设计取舍：不照搬 pixiv 的 55 个端点，只暴露「浏览 → 找到 → 拿到能播的直链」
这条主链。直链由 ``/api/iwara/resolve`` 返回，并用 ``/api/iwara/stream``
和 ``/api/iwara/image`` 代转字节 —— 因为浏览器所在机器的系统 DNS 解析不了
iwara 那些花名 CDN 主机，光给直链是播不了的。
"""

from urllib.parse import urlparse

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import StreamingResponse

from .. import tokens
from ..client import (
    IwaraError,
    decorate_video,
    is_cdn_host,
    jwt_payload,
    thumbnail_url,
)
from . import get_client

router = APIRouter(prefix="/api/iwara", tags=["iwara"])


def _decorate_page(page: dict) -> dict:
    """给列表里的每个视频补上缩略图直链。"""
    results = page.get("results")
    if isinstance(results, list):
        page = {**page, "results": [decorate_video(item) for item in results]}
    return page


@router.get("/videos")
async def list_videos(
    request: Request,
    page: int = 0,
    limit: int = Query(20, ge=1, le=50),
    sort: str = "date",
    rating: str = "all",
    tags: str | None = None,
    user: str | None = None,
):
    """浏览 / 排行。``sort``: date|trending|popularity|likes|views；``rating``: all|general|ecchi。"""
    result = await get_client(request).list_videos(
        page=page, limit=limit, sort=sort, rating=rating, tags=tags, user_id=user
    )
    return _decorate_page(result)


@router.get("/search")
async def search(
    request: Request,
    query: str = Query(..., min_length=1),
    type: str = "videos",
    page: int = 0,
    limit: int = Query(20, ge=1, le=50),
):
    result = await get_client(request).search(query, type=type, page=page, limit=limit)
    return _decorate_page(result)


@router.get("/video/{video_id}")
async def video_detail(request: Request, video_id: str):
    return decorate_video(await get_client(request).video(video_id))


@router.get("/related/{video_id}")
async def related_videos(
    request: Request,
    video_id: str,
    page: int = 0,
    limit: int = Query(12, ge=1, le=50),
):
    """相关视频，形状同 ``/videos``（``{count,limit,page,results}``）。

    多余的 ``page``/``limit`` 只用于本地裁剪：上游这个端点一次返回 12 条，
    不接受分页参数。
    """
    try:
        result = await get_client(request).related(video_id)
    except IwaraError as exc:
        raise HTTPException(status_code=exc.status, detail=exc.message) from exc

    result = _decorate_page(result)
    results = result.get("results")
    if page or limit != 12:
        if isinstance(results, list):
            start = page * limit
            result["results"] = results[start : start + limit]
    return result


@router.get("/resolve/{video_id}")
async def resolve(request: Request, video_id: str, probe: bool = True):
    """把视频换成可直接播放的直链。

    默认会逐个源探测可达性（``probe=true``）—— 每个源都在不同的 CDN 主机上，
    其中一部分在本机不通，所以要挑一个真能出字节的返回。``probe=false``
    只返回 API 给的清单，速度快但可能拿到不可用的主机。
    """
    client = get_client(request)
    try:
        resolved = await client.resolve(video_id)
    except IwaraError as exc:
        raise HTTPException(status_code=exc.status, detail=exc.message) from exc

    chosen = None
    probe_error = None
    if probe:
        try:
            source, result = await client.probe_any(resolved)
            chosen = {
                "name": source.name,
                "mime": source.mime,
                "url": source.download,
                "host": urlparse(source.download).hostname,
                "content_range": result.get("content_range"),
                "verified": True,
            }
        except IwaraError as exc:
            probe_error = exc.message

    return {
        "id": resolved.video_id,
        "title": resolved.title,
        "rating": resolved.rating,
        "file_name": resolved.file_name,
        "file_size": resolved.file_size,
        "salt_used": resolved.salt_used,
        "sources": [source.to_dict() for source in resolved.ordered()],
        "chosen": chosen,
        "probe_error": probe_error,
    }


@router.get("/stream")
async def stream(request: Request, url: str = Query(...)):
    """代转视频字节，透传 Range，让本机浏览器能直接播。

    直链本身是可以交给浏览器的，但浏览器所在机器的系统 DNS 解析不了
    那些花名 CDN 主机（``bailu`` / ``acheron`` …），所以由服务端代转。
    """
    host = urlparse(url).hostname or ""
    if not is_cdn_host(host):
        raise HTTPException(status_code=400, detail="只允许 iwara CDN 主机")

    pass_through = {}
    for name in ("range", "if-range"):
        value = request.headers.get(name)
        if value:
            pass_through[name] = value

    try:
        status, headers, body = await get_client(request).open_stream(url, pass_through)
    except IwaraError as exc:
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


@router.get("/image")
async def image(request: Request, url: str = Query(...)):
    """代转缩略图 / 原图（浏览器自己加载不了 files.iwara.tv）。"""
    if not is_cdn_host(urlparse(url).hostname or ""):
        raise HTTPException(status_code=400, detail="只允许 iwara CDN 主机")
    try:
        content, content_type = await get_client(request).fetch_image(url)
    except IwaraError as exc:
        raise HTTPException(status_code=exc.status, detail=exc.message) from exc
    return StreamingResponse(
        iter([content]),
        media_type=content_type,
        headers={"Cache-Control": "public, max-age=86400"},
    )


@router.get("/thumbnail/{video_id}")
async def thumbnail(
    request: Request,
    video_id: str,
    index: int = Query(0, ge=0, le=99),
    original: bool = False,
):
    """按视频 id 直接取缩略图，省得前端自己拼 file.id。

    图片地址由服务端拼好后代取，前端只需要 ``<img src="/api/iwara/thumbnail/<id>">``。
    """
    client = get_client(request)
    try:
        detail = await client.video(video_id)
    except IwaraError as exc:
        raise HTTPException(status_code=exc.status, detail=exc.message) from exc

    url = thumbnail_url(detail, index=index, original=original)
    if not url:
        raise HTTPException(status_code=404, detail="该视频没有缩略图")
    try:
        content, content_type = await client.fetch_image(url)
    except IwaraError as exc:
        raise HTTPException(status_code=exc.status, detail=exc.message) from exc
    return StreamingResponse(
        iter([content]),
        media_type=content_type,
        headers={"Cache-Control": "public, max-age=86400"},
    )


@router.get("/me")
async def me(request: Request):
    """当前账号；匿名时上游返回 401 的匿名对象。"""
    try:
        return await get_client(request).me()
    except IwaraError as exc:
        raise HTTPException(status_code=exc.status, detail=exc.message) from exc


@router.get("/auth")
async def auth_status(request: Request):
    """登录态诊断：只报「有没有配 token、能不能换出 access token」。

    不请求 ``/user``（匿名时那会返回 401，看起来像出错），而是直接看客户端
    手里的 token 状态；``auth_error`` 非空说明 token 没救，已经退回匿名。
    """
    client = get_client(request)
    configured = bool(client.refresh_token or client.access_token)
    result = {
        "configured": configured,
        "authenticated": bool(client.access_token),
        "anonymous": not client.refresh_token and not client.access_token,
        "error": client.auth_error or None,
        "token_file": str(_token_path_hint(request)),
    }
    # 有 refresh token 但还没换过 access token 时，主动试一次，好让用户当场
    # 知道 token 到底还能不能用。
    if client.refresh_token and not client.access_token and not client.auth_error:
        try:
            await client.ensure_access_token()
            result.update(authenticated=True, anonymous=False, error=None)
        except IwaraError as exc:
            message = str(getattr(exc, "message", exc))
            client.auth_error = message
            result.update(authenticated=False, error=message)
    if result["authenticated"]:
        payload = jwt_payload(client.access_token or "")
        result["expires_at"] = payload.get("exp")
        result["username_hint"] = payload.get("username")
    return result


def _token_path_hint(request: Request):
    """token 文件实际位置（方便用户知道该往哪放）。"""
    settings = getattr(request.app.state, "settings", None)
    data_dir = getattr(settings, "data_dir", None)
    return tokens.default_path(data_dir)
