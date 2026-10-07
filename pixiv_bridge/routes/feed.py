from fastapi import APIRouter, Request

from . import get_api

router = APIRouter(prefix="/api", tags=["feed"])


@router.get("/ranking")
async def get_ranking(
    request: Request,
    mode: str = "day",
    date: str | None = None,
    next_url: str | None = None,
):
    return await get_api(request).illust_ranking(mode, date, next_url=next_url)


@router.get("/recommended")
async def get_recommended(
    request: Request, type: str = "illust", next_url: str | None = None
):
    api = get_api(request)
    if type == "manga":
        return await api.manga_recommended(next_url=next_url)
    return await api.illust_recommended(next_url=next_url)


@router.get("/follow/illusts")
async def get_follow_illusts(
    request: Request, restrict: str = "public", next_url: str | None = None
):
    return await get_api(request).illust_follow(restrict, next_url=next_url)


@router.get("/trending-tags")
async def get_trending_tags(request: Request, type: str = "illust"):
    api = get_api(request)
    if type == "novel":
        return await api.trending_tags_novel()
    return await api.trending_tags_illust()


@router.get("/spotlight")
async def get_spotlight(
    request: Request, category: str = "all", next_url: str | None = None
):
    return await get_api(request).spotlight(category, next_url=next_url)


@router.get("/walkthrough/illusts")
async def get_walkthrough_illusts(request: Request):
    return await get_api(request).walkthrough_illusts()
