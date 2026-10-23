"""组装 FastAPI 基座应用：lifespan 建站点资源、挂站点与扩展路由、统一错误映射。

框架只提供「壳」：导航页（``/``）+ 健康检查（``/api/health``）+ 扩展机制 + 错误响应
形状；具体接口由站点通过 ``routers`` / ``state_factory`` / ``shutdown`` / ``error_types``
传进来，站点自己的工厂（``pixiv/app.py``、``iwara/app.py``）负责把这些参数填好。
"""

from contextlib import asynccontextmanager
from inspect import isawaitable

from fastapi import FastAPI

from .config import Settings
from .errors import install_error_handlers
from .routes import health, nav


def create_bridge_app(
    settings=None,
    *,
    title="Bridge",
    version="1.0.0",
    routers=(),
    state_factory=None,
    shutdown=None,
    error_types=(),
    extensions=(),
):
    """建接口应用基座。

    - ``routers``：站点的接口路由，按给定顺序挂载；
    - ``state_factory(settings)``：建站点的运行时对象，返回要挂到 ``app.state`` 上的
      字典。可以是 ``async`` 函数（Pixiv 要先 ``await oauth.load()`` 才能开始服务）；
    - ``shutdown(app)``：站点自己的资源释放（``httpx`` 客户端等），在扩展之后跑；
    - ``error_types``：站点的异常类型，统一映射成 ``{"error": {...}}``；
    - ``extensions``：调用方用 ``site_bridge.extensions.load_extensions(settings)``
      加载好的扩展。本函数自己**不读配置去加载扩展**（保持纯组装语义，也免得附属
      项目之间互相把对方挂两遍）。

    扩展的 ``state_factory`` / ``shutdown`` 与站点的按同一套规矩处理：先建站点 state、
    再建各扩展 state；释放时倒序（扩展 → 站点），某一项抛错也保证后面的都跑到。
    """
    settings = settings or Settings.load()
    extensions = tuple(extensions or ())

    state_factories = []
    if state_factory is not None:
        state_factories.append(state_factory)
    state_factories.extend(
        ext.state_factory for ext in extensions if ext.state_factory is not None
    )
    extension_routers = []
    for ext in extensions:
        extension_routers.extend(ext.routers)

    @asynccontextmanager
    async def lifespan(app):
        app.state.settings = settings
        app.state.extensions = extensions
        for factory in state_factories:
            state = factory(settings)
            if isawaitable(state):
                state = await state
            for key, value in state.items():
                setattr(app.state, key, value)
        try:
            yield
        finally:
            try:
                for ext in reversed(extensions):
                    if ext.shutdown is not None:
                        await ext.shutdown(app)
            finally:
                if shutdown is not None:
                    await shutdown(app)

    app = FastAPI(title=title, version=version, lifespan=lifespan)
    install_error_handlers(app, error_types)
    app.include_router(health.router)
    # 导航页占 `/`，挂在站点与扩展路由**之前**：Starlette 先匹配先赢，所以即使某个
    # 扩展也声明了 `/`，这一页也不会被顶掉（阅读器因此改挂 `/novel`）。
    app.include_router(nav.router)
    for router in routers or ():
        app.include_router(router)
    for router in extension_routers:
        app.include_router(router)
    return app
