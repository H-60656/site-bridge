"""收藏接口：本地台账 + 上游 pixiv 收藏的写入。

pixiv 的 app-api 只有收藏的「写」接口，没有「列出我收藏的小说」的读接口，
所以这里两边都写：
- 上游写成功 → pixiv 账号里的收藏状态正确（换客户端/官网也能看到）；
- 本地也写一条 → 页面随时能列出收藏列表，上游抖动时也不丢。
上游失败（比如网络不通）不会让请求失败，返回 `synced: false` 与原因，
本地记录照写不误。
"""

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

from site_bridge.compat import dump_model
from site_bridge.jsonl import DEFAULT_LIMIT
from ..bookmarks import normalize_bookmark
from . import get_api, get_bookmarks

router = APIRouter(prefix="/api", tags=["bookmarks"])


class BookmarkEntry(BaseModel):
    id: int
    title: str = ""
    user: str = ""
    user_id: int | None = None
    series_id: int | None = None
    series_title: str = ""
    restrict: str = "public"
    at: str | None = None


class BookmarkDelete(BaseModel):
    id: int


async def _sync_upstream(request, novel_id, deleted, restrict):
    """尽力同步到 pixiv；失败时返回原因字符串（不影响本地记录）。"""
    api = get_api(request)
    try:
        if deleted:
            await api.novel_bookmark_delete(novel_id)
        else:
            await api.novel_bookmark_add(novel_id, restrict=restrict)
        return None
    except Exception as exc:  # noqa: BLE001 - 上游任何失败都不该让本地记录写不进去
        message = getattr(exc, "message", "") or str(exc)
        return "%s: %s" % (exc.__class__.__name__, message) if message else exc.__class__.__name__


@router.get("/bookmarks")
async def list_bookmarks(request: Request, limit: int = DEFAULT_LIMIT):
    """本地收藏台账（折叠后按收藏时间倒序），已取消收藏的不在列表里。"""
    return get_bookmarks(request).status(limit)


@router.post("/bookmarks")
async def add_bookmark(body: BookmarkEntry, request: Request):
    store = get_bookmarks(request)
    try:
        entry = normalize_bookmark(dump_model(body))
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    sync_error = await _sync_upstream(request, entry["id"], False, entry["restrict"])
    store.append(entry)
    return {
        "ok": True,
        "file": str(store.path),
        "entry": entry,
        "synced": sync_error is None,
        "sync_error": sync_error,
    }


@router.post("/bookmarks/delete")
async def delete_bookmark(body: BookmarkDelete, request: Request):
    store = get_bookmarks(request)
    sync_error = await _sync_upstream(request, body.id, True, "public")
    entry = store.remove(body.id)
    return {
        "ok": True,
        "file": str(store.path),
        "entry": entry,
        "synced": sync_error is None,
        "sync_error": sync_error,
    }
