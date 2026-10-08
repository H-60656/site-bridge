"""杂项路由：健康检查、``next_url`` 翻页，以及图片代理（``/img``）。
"""

from mimetypes import guess_type

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import StreamingResponse

from . import get_api, get_image_proxy

router = APIRouter(tags=["misc"])


@router.get("/api/health")
async def health():
    return {"ok": True}


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
    proxy = get_image_proxy(request)
    try:
        safe_path = proxy.resolve(path, url)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    upstream = await proxy.stream(safe_path)
    if upstream.status_code != 200:
        await upstream.aclose()
        raise HTTPException(status_code=upstream.status_code, detail="图片获取失败")
    media_type = (
        upstream.headers.get("content-type")
        or guess_type(safe_path)[0]
        or "application/octet-stream"
    )
    return StreamingResponse(
        upstream.aiter_raw(),
        media_type=media_type,
        headers={"Cache-Control": "public, max-age=86400"},
    )
