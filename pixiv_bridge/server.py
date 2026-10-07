from contextlib import asynccontextmanager

import httpx
from fastapi import FastAPI
from fastapi.responses import JSONResponse

from .api import PixivApi, PixivApiError, PixivRateLimitError
from .config import Settings
from .image_proxy import ImageProxy
from .oauth import AuthError, OAuthManager
from .routes import (
    auth,
    feed,
    illust,
    misc,
    novel,
    search,
    user,
)


def create_app(settings=None, transport=None, state_factory=None, extra_routers=()):
    """建接口应用。

    只挂「纯接口」路由（登录 / 插画 / 搜索 / 动态 / 用户 / 小说 / 图片代理）。
    阅读器那一层（阅读历史、下载记录、收藏台账、本地书架、页面）属于子项目
    `novel`，由它调用本函数并传入：

    - `state_factory(settings)`：返回要挂到 `app.state` 上的额外对象字典；
    - `extra_routers`：要追加挂载的路由（在主项目路由之后）。
    """
    settings = settings or Settings.load()

    @asynccontextmanager
    async def lifespan(app):
        http = httpx.AsyncClient(transport=transport, proxy=settings.proxy or None)
        oauth = OAuthManager(settings, http)
        await oauth.load()
        api = PixivApi(settings, oauth, http)
        image_proxy = ImageProxy(settings, http)
        app.state.oauth = oauth
        app.state.api = api
        app.state.image_proxy = image_proxy
        app.state.settings = settings
        if state_factory is not None:
            for key, value in state_factory(settings).items():
                setattr(app.state, key, value)
        try:
            yield
        finally:
            await http.aclose()

    app = FastAPI(title="Pixiv Bridge", version="1.0.0", lifespan=lifespan)

    @app.exception_handler(PixivRateLimitError)
    async def _rate_limit_handler(request, exc):
        return JSONResponse(
            status_code=429,
            content={"error": {"message": exc.message, "reason": exc.reason}},
        )

    @app.exception_handler(PixivApiError)
    async def _api_error_handler(request, exc):
        return JSONResponse(
            status_code=exc.status or 400,
            content={"error": {"message": exc.message, "reason": exc.reason}},
        )

    @app.exception_handler(AuthError)
    async def _auth_error_handler(request, exc):
        return JSONResponse(
            status_code=401,
            content={"error": {"message": exc.message, "reason": exc.reason}},
        )

    @app.exception_handler(httpx.HTTPError)
    async def _upstream_error_handler(request, exc):
        return JSONResponse(
            status_code=502,
            content={
                "error": {
                    "message": f"上游请求失败: {exc.__class__.__name__}",
                    "reason": "upstream_error",
                }
            },
        )

    app.include_router(auth.router)
    app.include_router(illust.router)
    app.include_router(search.router)
    app.include_router(feed.router)
    app.include_router(user.router)
    app.include_router(novel.router)
    app.include_router(misc.router)
    for router in extra_routers:
        app.include_router(router)
    return app
