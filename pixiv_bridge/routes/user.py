from fastapi import APIRouter, Request
from pydantic import BaseModel

from . import get_api

router = APIRouter(prefix="/api", tags=["user"])


class FollowAddBody(BaseModel):
    user_id: int
    restrict: str = "public"


class FollowDeleteBody(BaseModel):
    user_id: int


# 注意：字面量路由必须声明在同前缀的路径参数路由之前，
# 否则 /user/recommended 会被 /user/{user_id} 抢先匹配并因 int 解析失败返回 422。
@router.get("/user/recommended")
async def get_user_recommended(request: Request, next_url: str | None = None):
    return await get_api(request).user_recommended(next_url=next_url)


@router.get("/user/{user_id}")
async def get_user_detail(user_id: int, request: Request):
    return await get_api(request).user_detail(user_id)


@router.get("/user/{user_id}/illusts")
async def get_user_illusts(
    user_id: int,
    request: Request,
    type: str = "illust",
    next_url: str | None = None,
):
    return await get_api(request).user_illusts(user_id, type, next_url=next_url)


@router.get("/user/{user_id}/novels")
async def get_user_novels(
    user_id: int, request: Request, next_url: str | None = None
):
    return await get_api(request).user_novels(user_id, next_url=next_url)


@router.get("/user/{user_id}/bookmarks")
async def get_user_bookmarks(
    user_id: int,
    request: Request,
    restrict: str = "public",
    tag: str | None = None,
    next_url: str | None = None,
):
    return await get_api(request).user_bookmarks_illust(
        user_id, restrict, tag, next_url=next_url
    )


@router.get("/user/{user_id}/bookmark-tags")
async def get_user_bookmark_tags(
    user_id: int, request: Request, restrict: str = "public"
):
    return await get_api(request).user_bookmark_tags_illust(user_id, restrict)


@router.get("/user/{user_id}/following")
async def get_user_following(
    user_id: int,
    request: Request,
    restrict: str = "public",
    next_url: str | None = None,
):
    return await get_api(request).user_following(user_id, restrict, next_url=next_url)


@router.get("/user/{user_id}/followers")
async def get_user_followers(
    user_id: int,
    request: Request,
    restrict: str = "public",
    next_url: str | None = None,
):
    return await get_api(request).user_follower(user_id, restrict, next_url=next_url)


@router.get("/user/{user_id}/follow-detail")
async def get_user_follow_detail(user_id: int, request: Request):
    return await get_api(request).user_follow_detail(user_id)


@router.post("/user/follow/add")
async def follow_add(body: FollowAddBody, request: Request):
    return await get_api(request).user_follow_add(body.user_id, body.restrict)


@router.post("/user/follow/delete")
async def follow_delete(body: FollowDeleteBody, request: Request):
    return await get_api(request).user_follow_delete(body.user_id)
