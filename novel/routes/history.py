"""阅读历史接口：记下「在读什么、读到哪」，落在项目目录下的 history.jsonl。

个人使用，不做用户区分：写入的条目只保留书名、作者、章节与进度。
"""

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

from site_bridge.compat import dump_model
from ..history import DEFAULT_LIMIT, normalize_entry
from . import get_history

router = APIRouter(prefix="/api", tags=["history"])


class HistoryEntry(BaseModel):
    id: int
    title: str = ""
    user: str = ""
    series_id: int | None = None
    series_title: str = ""
    chapter_index: int | None = None
    chapters: int | None = None
    progress: float | None = None
    read_at: str | None = None


@router.get("/history")
async def get_history_entries(request: Request, limit: int = DEFAULT_LIMIT):
    """每本小说一条最新记录，按阅读时间倒序；同时告诉前端文件落在哪里。"""
    return get_history(request).status(limit)


@router.post("/history")
async def post_history_entry(body: HistoryEntry, request: Request):
    store = get_history(request)
    try:
        entry = normalize_entry(dump_model(body))
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    store.append(entry)
    return {"ok": True, "file": str(store.path), "entry": entry}
