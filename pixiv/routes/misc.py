"""杂项路由：``next_url`` 翻页，以及图片代理（``/img``）。

健康检查（``/api/health``）不在这里：它属于框架（``pixiv_bridge/routes/health.py``），
每个接口站点都天生有。
"""

from mimetypes import guess_type

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import Response, StreamingResponse

from pixiv_bridge import ranges

from . import get_api, get_image_proxy

router = APIRouter(tags=["misc"])

#: 图片代转时原样回给客户端的状态码：200 整包、206 区间、304 条件命中、416 区间越界。
_PASSTHROUGH_STATUSES = (200, 206, 304, 416)


@router.get("/api/next")
async def get_next(request: Request, url: str = Query(...)):
    return await get_api(request).next(url)


@router.get("/img")
async def proxy_image_url(request: Request, url: str = Query(...)):
    return await _stream_image(request, url=url)


@router.get("/img/{path:path}")
async def proxy_image_path(request: Request, path: str):
    return await _stream_image(request, path=path)


async def _stream_image(request, path="", url=None):
    """代转图片字节，透传 Range。

    上游（``i.pximg.net``）本来就支持 Range，所以这里只是把入站的 ``Range`` /
    ``If-Range`` 转过去，再把上游的 206 与续传相关响应头原样转回：浏览器因此可以
    断点续传、拖动进度、只取头部探测尺寸。以前这里一律回 200，206 还会被当成错误。
    """
    proxy = get_image_proxy(request)
    try:
        safe_path = proxy.resolve(path, url)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    upstream = await proxy.stream(safe_path, ranges.forwarded_headers(request))
    status = upstream.status_code
    if status not in _PASSTHROUGH_STATUSES:
        await upstream.aclose()
        raise HTTPException(status_code=status, detail="图片获取失败")
    headers = ranges.response_headers(upstream.headers)
    headers["Cache-Control"] = "public, max-age=86400"
    if status in (304, 416):
        # 两种都没有实体：条件命中（304）或区间越界（416）时上游只给头。
        await upstream.aclose()
        return Response(status_code=status, headers=headers)
    media_type = (
        upstream.headers.get("content-type")
        or guess_type(safe_path)[0]
        or "application/octet-stream"
    )
    return StreamingResponse(
        upstream.aiter_raw(),
        status_code=status,
        media_type=media_type,
        headers=headers,
    )
