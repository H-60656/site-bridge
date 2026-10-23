"""Pixiv 接口站点：路由清单、运行时对象装配、应用工厂与站点描述符 ``SITE``。

框架（``site_bridge``）只认识 :class:`site_bridge.site.Site` 这一份契约；
本模块把 Pixiv 的细节填进去：

- ``PIXIV_ROUTERS``：``/api/*`` 路由（``/api/health`` 与导航页由框架提供）；
- ``pixiv_state_factory``：建 ``httpx`` 客户端、``OAuthManager``、``PixivApi``、
  ``ImageProxy``，挂到 ``app.state`` 上（``oauth.load()`` 是 async，所以这个
  工厂也是 async）；
- ``PIXIV_ERROR_TYPES``：站点的异常类型，交给框架统一映射成
  ``{"error": {"message": ..., "reason": ...}}``；
- ``create_app``：签名与重构前的 ``site_bridge.server.create_app`` 一致，
  方便 ``novel`` 这类扩展继续在它上面追加路由与状态；
- ``SITE``：入口脚本（``pixiv/run.py``）交给 ``site_bridge.cli.main`` 的东西。
"""

import inspect
from pathlib import Path

import httpx

from site_bridge.server import create_bridge_app
from site_bridge.site import Site

from .api import PixivApi, PixivApiError, PixivRateLimitError
from .config import PixivSettings, as_pixiv_settings
from .envcheck import extra_checks, login_check, network_hint
from .image_proxy import ImageProxy
from .oauth import AuthError, OAuthManager
from .routes import auth, feed, illust, misc, pixiv_novel, search, user

TITLE = "Pixiv Bridge"
VERSION = "1.0.0"
DIRECTORY = Path(__file__).resolve().parent

#: 纯接口路由；导航页（`/`）与健康检查（`/api/health`）由框架挂载。
PIXIV_ROUTERS = (
    auth.router,
    illust.router,
    search.router,
    feed.router,
    user.router,
    pixiv_novel.router,
    misc.router,
)

#: 需要映射成统一错误响应形状的站点异常（``httpx`` 的 502 由框架兜底）。
PIXIV_ERROR_TYPES = (PixivApiError, PixivRateLimitError, AuthError)


async def pixiv_state_factory(settings, transport=None):
    """建 Pixiv 的运行时对象（要 ``setattr`` 到 ``app.state`` 上的字典）。

    ``transport`` 是测试用的 ``httpx`` transport 注入点；``oauth.load()`` 会把
    ``data/tokens.json`` 里的 refresh_token 读进来，所以这个工厂是 async 的。
    """
    http = httpx.AsyncClient(transport=transport, proxy=settings.proxy or None)
    oauth = OAuthManager(settings, http)
    await oauth.load()
    return {
        "http": http,
        "oauth": oauth,
        "api": PixivApi(settings, oauth, http),
        "image_proxy": ImageProxy(settings, http),
    }


async def pixiv_shutdown(app):
    """站点自己的资源：关掉那个 httpx 客户端。"""
    await app.state.http.aclose()


def create_app(
    settings=None,
    transport=None,
    state_factory=None,
    extra_routers=(),
    extensions=(),
):
    """建 Pixiv 接口应用。

    只挂「纯接口」路由（登录 / 插画 / 搜索 / 动态 / 用户 / 小说 / 图片代理）加框架的
    导航页（`/`）与健康检查。阅读器那一层（阅读历史、下载记录、收藏台账、本地书架、
    页面）属于附属项目 ``novel``，它通过两种方式之一挂进来：

    - **扩展机制**（配置驱动，推荐）：``extensions`` 传
      ``site_bridge.extensions.load_extensions(settings)`` 加载好的扩展列表，由
      ``SITE_BRIDGE_EXTENSIONS`` 决定装哪些；命令行入口 ``python pixiv/run.py``
      走的就是这条路；
    - **显式组装**（代码驱动）：直接传 ``state_factory(settings)``（返回要挂到
      ``app.state`` 上的额外对象字典，可以是 async）与 ``extra_routers``。

    本函数自己**不读配置去加载扩展**（保持纯组装语义，也免得附属项目之间互相把
    对方挂两遍）。
    """
    settings = as_pixiv_settings(settings)

    async def combined_state(settings):
        state = await pixiv_state_factory(settings, transport)
        if state_factory is not None:
            extra = state_factory(settings)
            if inspect.isawaitable(extra):
                extra = await extra
            state.update(extra)
        return state

    return create_bridge_app(
        settings,
        title=TITLE,
        version=VERSION,
        routers=tuple(PIXIV_ROUTERS) + tuple(extra_routers or ()),
        state_factory=combined_state,
        shutdown=pixiv_shutdown,
        error_types=PIXIV_ERROR_TYPES,
        extensions=extensions,
    )


#: 站点描述符：入口脚本只需要它。
SITE = Site(
    name="pixiv",
    directory=DIRECTORY,
    title=TITLE,
    create_app=create_app,
    version=VERSION,
    nav_order=1,
    settings_class=PixivSettings,
    required_files=("run.py",),
    checks=extra_checks,
    login_check=login_check,
    network_hint=network_hint,
)
