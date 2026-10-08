"""Pixiv 小说接口（``/api/novel/*``、``/api/watchlist/*``）。

注意与附属项目 ``novel/``（小说**阅读器**）区分：本模块属于接口层，挂在
纯接口应用上；``novel/`` 那一层是阅读历史、下载、收藏台账与页面，靠扩展机制
挂载。文件名带 ``pixiv_`` 前缀就是为了避免和 ``novel/`` 撞名。
"""

from fastapi import APIRouter, Request
from pydantic import BaseModel

from . import get_api

router = APIRouter(prefix="/api", tags=["novel"])


class NovelBookmarkAddBody(BaseModel):
    novel_id: int
    restrict: str = "public"


class NovelBookmarkDeleteBody(BaseModel):
    novel_id: int


# 注意：字面量路由必须声明在同前缀的路径参数路由之前，
# 否则 /novel/ranking 等会被 /novel/{novel_id} 抢先匹配并因 int 解析失败返回 422。
@router.get("/novel/ranking")
async def get_novel_ranking(
    request: Request,
    mode: str = "day",
    date: str | None = None,
    next_url: str | None = None,
):
    return await get_api(request).novel_ranking(mode, date, next_url=next_url)


@router.get("/novel/recommended")
async def get_novel_recommended(request: Request, next_url: str | None = None):
    return await get_api(request).novel_recommended(next_url=next_url)


@router.get("/novel/follow")
async def get_novel_follow(
    request: Request, restrict: str = "public", next_url: str | None = None
):
    return await get_api(request).novel_follow(restrict, next_url=next_url)


@router.get("/novel/series")
async def get_novel_series(
    request: Request, series_id: int, next_url: str | None = None
):
    return await get_api(request).novel_series(series_id, next_url=next_url)


@router.get("/novel/{novel_id}")
async def get_novel_detail(novel_id: int, request: Request):
    return await get_api(request).novel_detail(novel_id)


@router.get("/novel/{novel_id}/text")
async def get_novel_text(novel_id: int, request: Request):
    # Pixiv 已下掉 /v1/novel/text（2024-02 起固定 404 "Specified end-point doesn't exist"），
    # 正文现在由 api.novel_text() 走 /webview/v2/novel 取；本路由与返回结构保持不变。
    return await get_api(request).novel_text(novel_id)


@router.get("/novel/{novel_id}/comments")
async def get_novel_comments(
    novel_id: int, request: Request, next_url: str | None = None
):
    return await get_api(request).novel_comments(novel_id, next_url=next_url)


@router.post("/novel/bookmark/add")
async def novel_bookmark_add(body: NovelBookmarkAddBody, request: Request):
    return await get_api(request).novel_bookmark_add(body.novel_id, body.restrict)


@router.post("/novel/bookmark/delete")
async def novel_bookmark_delete(
    body: NovelBookmarkDeleteBody, request: Request
):
    return await get_api(request).novel_bookmark_delete(body.novel_id)


@router.get("/watchlist/novel")
async def get_watchlist_novel(request: Request):
    return await get_api(request).watchlist_novel()


@router.post("/watchlist/novel/add")
async def watchlist_novel_add(request: Request, series_id: str):
    return await get_api(request).watchlist_novel_add(series_id)


@router.post("/watchlist/novel/delete")
async def watchlist_novel_delete(request: Request, series_id: str):
    return await get_api(request).watchlist_novel_delete(series_id)


@router.get("/watchlist/manga")
async def get_watchlist_manga(request: Request):
    return await get_api(request).watchlist_manga()


@router.post("/watchlist/manga/add")
async def watchlist_manga_add(request: Request, series_id: int):
    return await get_api(request).watchlist_manga_add(series_id)


@router.post("/watchlist/manga/delete")
async def watchlist_manga_delete(request: Request, series_id: int):
    return await get_api(request).watchlist_manga_delete(series_id)
