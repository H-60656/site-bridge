"""插画路由（``/api/illust/*``、``/api/bookmark/*``、``/api/comment/*``）。
"""

from fastapi import APIRouter, Request
from pydantic import BaseModel

from . import get_api

router = APIRouter(prefix="/api", tags=["illust"])


class BookmarkAddBody(BaseModel):
    illust_id: int
    restrict: str = "public"
    tags: list[str] | None = None


class BookmarkDeleteBody(BaseModel):
    illust_id: int


class CommentAddBody(BaseModel):
    illust_id: int
    comment: str
    parent_comment_id: int | None = None


@router.get("/illust/{illust_id}")
async def get_illust_detail(illust_id: int, request: Request):
    return await get_api(request).illust_detail(illust_id)


@router.get("/illust/{illust_id}/related")
async def get_illust_related(
    illust_id: int, request: Request, next_url: str | None = None
):
    return await get_api(request).illust_related(illust_id, next_url=next_url)


@router.get("/illust/{illust_id}/comments")
async def get_illust_comments(
    illust_id: int, request: Request, next_url: str | None = None
):
    return await get_api(request).illust_comments(illust_id, next_url=next_url)


@router.get("/illust/{illust_id}/ugoira")
async def get_ugoira_metadata(illust_id: int, request: Request):
    return await get_api(request).ugoira_metadata(illust_id)


@router.get("/illust/{illust_id}/bookmark-detail")
async def get_illust_bookmark_detail(illust_id: int, request: Request):
    return await get_api(request).illust_bookmark_detail(illust_id)


@router.get("/illust/{illust_id}/series")
async def get_illust_series_illust(illust_id: int, request: Request):
    return await get_api(request).illust_series_illust(illust_id)


@router.post("/bookmark/add")
async def bookmark_add(body: BookmarkAddBody, request: Request):
    return await get_api(request).illust_bookmark_add(
        body.illust_id, body.restrict, body.tags
    )


@router.post("/bookmark/delete")
async def bookmark_delete(body: BookmarkDeleteBody, request: Request):
    return await get_api(request).illust_bookmark_delete(body.illust_id)


@router.post("/comment/add")
async def comment_add(body: CommentAddBody, request: Request):
    return await get_api(request).illust_comment_add(
        body.illust_id, body.comment, body.parent_comment_id
    )
