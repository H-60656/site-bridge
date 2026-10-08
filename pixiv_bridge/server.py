"""组装 FastAPI 应用：lifespan 建资源、挂内置路由与扩展路由、统一错误映射。
"""

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
    nav,
    pixiv_novel,
    search,
    user,
)


def create_app(
    settings=None,
    transport=None,
    state_factory=None,
    extra_routers=(),
    extensions=(),
):
    """建接口应用。

    只挂「纯接口」路由（登录 / 插画 / 搜索 / 动态 / 用户 / 小说 / 图片代理）
    加一个导航页（`/`，见 `pixiv_bridge/routes/nav.py`）。
    阅读器那一层（阅读历史、下载记录、收藏台账、本地书架、页面）属于附属项目
    `novel`，它通过两种方式之一挂进来：

    - **扩展机制**（配置驱动，推荐）：`extensions` 传 `pixiv_bridge.extensions`
      加载好的 `Extension` 列表，由 `Settings.extensions`（`PIXIV_BRIDGE_EXTENSIONS`）
      决定装哪些。命令行入口 `python run.py` 走的就是这条路；
    - **显式组装**（代码驱动）：直接传

      - `state_factory(settings)`：返回要挂到 `app.state` 上的额外对象字典；
      - `extra_routers`：要追加挂载的路由（在主项目路由之后）。

    本函数自己**不读配置去加载扩展**（保持纯组装语义，也免得附属项目之间互相
    把对方挂两遍）；要按配置加载请用 `pixiv_bridge.extensions.load_extensions(settings)`。
    """
    settings = settings or Settings.load()
    extensions = tuple(extensions or ())

    # 额外的 app.state 对象：显式传进来的 state_factory + 各扩展自己的工厂。
    state_factories = []
    if state_factory is not None:
        state_factories.append(state_factory)
    state_factories.extend(
        ext.state_factory for ext in extensions if ext.state_factory is not None
    )
    # 要追加挂载的路由：显式传进来的 + 各扩展自己的（顺序即挂载顺序）。
    appended_routers = list(extra_routers or ())
    for ext in extensions:
        appended_routers.extend(ext.routers)

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
        app.state.extensions = extensions
        for factory in state_factories:
            for key, value in factory(settings).items():
                setattr(app.state, key, value)
        try:
            yield
        finally:
            try:
                # 扩展自己的资源由扩展自己收（接口项目只关自己的 httpx 客户端）。
                # 倒序释放，与挂载顺序相反；某一项抛错也要保证后面的都跑到。
                for ext in reversed(extensions):
                    if ext.shutdown is not None:
                        await ext.shutdown(app)
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
    app.include_router(pixiv_novel.router)
    app.include_router(misc.router)
    # 导航页占 `/`，挂在扩展路由**之前**：Starlette 先匹配先赢，所以即使某个
    # 扩展也声明了 `/`，这一页也不会被顶掉（阅读器因此改挂 `/novel`）。
    app.include_router(nav.router)
    for router in appended_routers:
        app.include_router(router)
    return app
