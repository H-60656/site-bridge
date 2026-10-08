"""搜索路由（``/api/search/*``）：插画、小说、用户、联想词与热门词。
"""

from fastapi import APIRouter, Query, Request

from . import get_api

router = APIRouter(prefix="/api/search", tags=["search"])


@router.get("/illust")
async def search_illust(
    request: Request,
    word: str = Query(...),
    sort: str | None = None,
    search_target: str | None = None,
    start_date: str | None = None,
    end_date: str | None = None,
    bookmark_num_min: int | None = None,
    bookmark_num_max: int | None = None,
    search_ai_type: int | None = None,
    next_url: str | None = None,
):
    return await get_api(request).search_illust(
        word,
        sort=sort,
        search_target=search_target,
        start_date=start_date,
        end_date=end_date,
        bookmark_num_min=bookmark_num_min,
        bookmark_num_max=bookmark_num_max,
        search_ai_type=search_ai_type,
        next_url=next_url,
    )


@router.get("/novel")
async def search_novel(
    request: Request,
    word: str = Query(...),
    sort: str | None = None,
    search_target: str | None = None,
    start_date: str | None = None,
    end_date: str | None = None,
    next_url: str | None = None,
):
    return await get_api(request).search_novel(
        word,
        sort=sort,
        search_target=search_target,
        start_date=start_date,
        end_date=end_date,
        next_url=next_url,
    )


@router.get("/user")
async def search_user(
    request: Request, word: str = Query(...), next_url: str | None = None
):
    return await get_api(request).search_user(word, next_url=next_url)


@router.get("/autocomplete")
async def search_autocomplete(request: Request, word: str = Query(...)):
    return await get_api(request).search_autocomplete(word)


@router.get("/popular")
async def search_popular(
    request: Request, word: str = Query(...), next_url: str | None = None
):
    return await get_api(request).search_popular_preview(word, next_url=next_url)
