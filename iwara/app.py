"""iwara 接口站点：视频接口、运行时对象装配、应用工厂与站点描述符 ``SITE``。

依赖方向：本模块只 import 框架（``pixiv_bridge``）与本站点自己的模块，框架不
认识 ``iwara``，单向依赖不会倒过来。

与 ``pixiv/`` 站点的区别在传输层：iwara 用不了框架默认那个 httpx 客户端
（系统 DNS 污染 + Cloudflare 拦 Python TLS 指纹），所以这里自己建一个
``IwaraClient``，由 ``iwara_state_factory`` 挂到 ``app.state.iwara_client`` 上。
它持有 curl_cffi 会话，退出时由 ``iwara_shutdown`` 释放（框架的 lifespan 会
await 这个钩子）。

本入口**只提供 iwara 接口**（``/api/iwara/*``）：Pixiv 的路由属于 ``pixiv/``
站点，两个接口项目同级、互不挂载。人看的视频页面（``/iwara``）是独立的扩展包
``iwara_web``，按配置挂上去（``PIXIV_BRIDGE_EXTENSIONS=iwara_web``）—— 这样
「接口」与「页面」可以分开分发，装了接口包也不必带上页面。
"""

import inspect
import os
from pathlib import Path

from pixiv_bridge.nav import NavItem, NavSection
from pixiv_bridge.server import create_bridge_app
from pixiv_bridge.site import Site

from . import tokens
from .client import IwaraClient
from .envcheck import extra_checks, login_check
from .routes import videos

TITLE = "Iwara Bridge"
VERSION = "1.0.0"
DIRECTORY = Path(__file__).resolve().parent

IWARA_ROUTERS = (videos.router,)

IWARA_STATE_KEYS = ("iwara_client", "nav_sections")

# 导航页上露出的 iwara 接口。地址与 `iwara/routes/videos.py` 里的前缀一致，
# 改成 `/api/iwara/...` 的路径时这里也要跟着改。
# 视频**页面**（`/iwara`）不在这里列：它由独立的扩展包 `iwara_web` 提供，
# 没挂那个包时这个地址是 404，列出来反而误导；挂了之后扩展自己会在导航页上
# 声明那条链接（见 `iwara_web/extension.py` 的 `EXTENSION_NAV`）。
IWARA_NAV = NavSection(
    title="iwara 视频（本入口独有）",
    items=(
        NavItem("视频列表", "/api/iwara/videos", "trending / 最新，支持 rating 过滤"),
        NavItem("搜索", "/api/iwara/search?query=dance", "按关键词搜视频"),
        NavItem("解析直链", "/api/iwara/resolve/{video_id}?probe=true", "拿到可播放的 mp4 直链并探活"),
        NavItem("登录状态", "/api/iwara/auth", "是否配了 token；不配也能匿名看公开内容"),
    ),
    note="这些是 JSON 接口，用浏览器打开即可查看返回内容；人看的视频页面在 iwara_web 扩展包里。",
)


def iwara_state_factory(settings):
    """建出要挂到 app.state 上的 iwara 对象。

    登录态是「可选增强」：有 token 就以登录身份请求（能看到私密 / 订阅内容），
    没有就照常匿名跑。token 来源优先环境变量 ``IWARA_REFRESH_TOKEN``，
    其次是 ``data/iwara_tokens.json``（`iwara/get_token.py` 写入）。
    """
    data_dir = getattr(settings, "data_dir", None)
    refresh_token = tokens.resolve(
        data_dir, env_token=os.environ.get(tokens.ENV_VAR, "")
    )
    return {
        "iwara_client": IwaraClient(refresh_token=refresh_token or None),
        # 框架的导航页会读这个属性，把 iwara 的接口列进去。
        "nav_sections": (IWARA_NAV,),
    }


async def iwara_shutdown(app):
    """退出时释放 iwara 的 curl 会话（框架的 lifespan 会 await 它）。"""
    client = getattr(app.state, "iwara_client", None)
    if client is not None:
        await client.aclose()


def create_app(settings=None, extensions=(), state_factory=None, extra_routers=()):
    """建 iwara 接口应用。

    - ``extensions``：按 ``PIXIV_BRIDGE_EXTENSIONS`` 加载好的扩展（典型是
      ``iwara_web``，人看的视频页面）；入口脚本走这条路。
    - ``state_factory`` / ``extra_routers``：代码驱动的显式组装（与
      ``pixiv.create_app`` 同形，便于脚本与测试再加一层）。
    """
    async def combined_state(settings):
        state = iwara_state_factory(settings)
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
        routers=tuple(IWARA_ROUTERS) + tuple(extra_routers or ()),
        state_factory=combined_state,
        shutdown=iwara_shutdown,
        extensions=extensions,
    )


#: 站点描述符：入口脚本只需要它。
SITE = Site(
    name="iwara",
    directory=DIRECTORY,
    title=TITLE,
    create_app=create_app,
    version=VERSION,
    # iwara 没有额外配置字段，用框架的 Settings 就够（data_dir 等路径都在里面）。
    required_files=("run.py", "requirements.txt"),
    checks=extra_checks,
    login_check=login_check,
)
