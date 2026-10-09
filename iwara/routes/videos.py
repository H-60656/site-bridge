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


def _page_envelope(page: dict, *, total: int | None) -> dict:
    """把上游列表页规整成对外统一形状。

    ``count`` 一律是**本页条数**（恒等于 ``len(results)``），``total`` 是总数 ——
    上游不给就写 ``None``，绝不编一个数字顶上。

    这条规矩是被上游的伪字段逼出来的：``/videos`` 的顶层 ``count`` 实测恒等于
    ``page*limit + len(results) + 1``（20 组 page×limit 组合全部成立，翻到越界
    空页时退化成 ``page*limit``）。它既不是总数也不是本页条数，只是「请求过的
    条数 + 1」—— 原样透传会让调用方看到「共 N 个」随翻页无限增长。真总数只在
    ``/search`` 上给（跨页恒定）。这与 ``asmr_one`` 那边 ``count``=本页、
    ``total``=总数的口径一致。
    """
    results = page.get("results")
    if not isinstance(results, list):
        return page
    return {**page, "count": len(results), "total": total}


@router.get("/videos")
async def list_videos(
    request: Request,
    page: int = 0,
    limit: int = Query(20, ge=1, le=50),
    sort: str = "date",
    rating: str = "all",
    tags: str | None = None,
    user: str | None = None,
    subscribed: bool | None = None,
):
    """浏览 / 排行。``sort``: date|trending|popularity|likes|views；``rating``: all|general|ecchi。

    ``subscribed=true`` 只看订阅作者 —— **需要登录**才有效；匿名下上游会
    静默忽略它（实测三种取值返回完全相同的 8/20 条，见 DIRECT.md 第 5 节第 3 条）。

    返回 ``{count,total,limit,page,results}``：``count`` 是本页条数，
    ``total`` 固定为 ``null`` —— 上游 ``/videos`` 根本没给总数（它的顶层
    ``count`` 是个伪字段，见 ``_page_envelope``）。
    """
    try:
        result = await get_client(request).list_videos(
            page=page,
            limit=limit,
            sort=sort,
            rating=rating,
            tags=tags,
            user_id=user,
            subscribed=subscribed,
        )
    except IwaraError as exc:
        raise HTTPException(status_code=exc.status, detail=exc.message) from exc
    return _page_envelope(_decorate_page(result), total=None)


@router.get("/search")
async def search(
    request: Request,
    query: str = Query(..., min_length=1),
    type: str = "videos",
    page: int = 0,
    limit: int = Query(20, ge=1, le=50),
):
    """搜索。``count`` 是本页条数，``total`` 是真总数 —— 全站只有这个端点给总数。"""
    try:
        result = await get_client(request).search(query, type=type, page=page, limit=limit)
    except IwaraError as exc:
        raise HTTPException(status_code=exc.status, detail=exc.message) from exc
    result = _decorate_page(result)
    # 上游 /search 的 count 是跨页恒定的真总数，先取出来再交给 _page_envelope 改写 count。
    total = result.get("count") if isinstance(result.get("count"), int) else None
    return _page_envelope(result, total=total)


@router.get("/video/{video_id}")
async def video_detail(request: Request, video_id: str):
    try:
        detail = await get_client(request).video(video_id)
    except IwaraError as exc:
        raise HTTPException(status_code=exc.status, detail=exc.message) from exc
    return decorate_video(detail)


@router.get("/related/{video_id}")
async def related_videos(
    request: Request,
    video_id: str,
    page: int = 0,
    limit: int = Query(12, ge=1, le=50),
):
    """相关视频，形状同 ``/videos``（``{count,total,limit,page,results}``）。

    多余的 ``page``/``limit`` 只用于本地裁剪：上游这个端点一次返回 12 条，
    不接受分页参数。**裁剪后必须重算元数据** —— 上游给的 ``count``/``limit``/
    ``page`` 描述的是裁剪前的整页，原样留下就会出现「``count`` 说 12 条、
    ``results`` 里 0 条」这种自相矛盾的响应（``?page=1`` 就会命中）。
    这里 ``count`` 一律是**裁剪后本页的真实条数**，``total`` 是上游整页条数
    （一次性给出、不分页，所以它就是总数）。
    """
    try:
        result = await get_client(request).related(video_id)
    except IwaraError as exc:
        raise HTTPException(status_code=exc.status, detail=exc.message) from exc

    result = _decorate_page(result)
    results = result.get("results")
    if not isinstance(results, list):
        return result

    total = len(results)
    if page or limit != 12:
        start = page * limit
        results = results[start : start + limit]

    result["results"] = results
    result["count"] = len(results)
    result["total"] = total
    result["limit"] = limit
    result["page"] = page
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
    """登录态诊断：报「有没有配 token、能不能换出 access token、是哪个账号」。

    **匿名时刻意不请求 ``/user``**（那会返回 401，看起来像出错），只看客户端
    手里的 token 状态；``auth_error`` 非空说明 token 没救，已经退回匿名。
    一旦认证成功就会问一次 ``/user``：access token 的载荷里没有用户名，想问出
    「现在是谁」只有这条路（见下面的注释）。
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
        # access token 的载荷只有 exp/iat/id/iss/premium/role/type —— **没有
        # username**。想报出「现在是哪个账号」只能问一次 /user；此处已认证，
        # 那次请求不会再看到 401。
        result["username_hint"] = await _username_hint(client)
    return result


async def _username_hint(client) -> str | None:
    """认证成功后问一次 ``/user`` 拿用户名；失败就返回 None（不影响诊断）。"""
    try:
        info = await client.me()
    except Exception:  # noqa: BLE001 - 只是锦上添花的字段
        return None
    user = (info or {}).get("user") or {}
    return user.get("username") or user.get("name") or None


def _token_path_hint(request: Request):
    """token 文件实际位置（方便用户知道该往哪放）。"""
    settings = getattr(request.app.state, "settings", None)
    data_dir = getattr(settings, "data_dir", None)
    return tokens.default_path(data_dir)
