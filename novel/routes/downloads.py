"""下载历史接口：按「书 + 章节」记录导出过什么。

- `GET /api/downloads`：折叠后的下载记录（可按 series_id / book_id 过滤）；
  给了 series_id 时额外返回 `series`（章节序号 → 最近一次导出的文件与大小）。
- `POST /api/downloads`：追加一条（前端每次导出 EPUB 成功后就写一条）。
- `POST /api/downloads/clear`：清空下载历史（本地台账，随时可以重来）。
"""

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

from pixiv_bridge.compat import dump_model
from pixiv_bridge.jsonl import DEFAULT_LIMIT
from ..downloads import normalize_download
from . import get_downloads

router = APIRouter(prefix="/api", tags=["downloads"])


class DownloadEntry(BaseModel):
    book_id: int
    chapter_index: int | None = None
    chapter_id: int | None = None
    chapter_title: str = ""
    title: str = ""
    user: str = ""
    series_id: int | None = None
    series_title: str = ""
    merge_key: str = ""
    format: str = "epub"
    file: str = ""
    bytes: int | None = 0
    chapters: int | None = None
    at: str | None = None


@router.get("/downloads")
async def list_downloads(
    request: Request,
    limit: int = DEFAULT_LIMIT,
    series_id: int | None = None,
    book_id: int | None = None,
):
    store = get_downloads(request)
    data = store.status(limit, series_id=series_id, book_id=book_id)
    if series_id is not None:
        data["series"] = store.series_progress(series_id)
    return data


@router.post("/downloads")
async def add_download(body: DownloadEntry, request: Request):
    store = get_downloads(request)
    try:
        entry = normalize_download(dump_model(body))
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    store.append(entry)
    return {"ok": True, "file": str(store.path), "entry": entry}


@router.post("/downloads/clear")
async def clear_downloads(request: Request):
    store = get_downloads(request)
    store.clear()
    return {"ok": True, "file": str(store.path)}
