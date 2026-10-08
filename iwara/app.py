"""把 iwara 子项目挂到主项目的接口应用上。

依赖方向：本模块只 import 主项目（`pixiv_bridge`）与本子项目的模块，
主项目不认识 `iwara`，单向依赖不会倒过来。

与 `novel` 的区别在传输层：iwara 站点用不了主项目那个 httpx 客户端
（系统 DNS 污染 + Cloudflare 拦截 Python TLS 指纹），所以这里自己建一个
`IwaraClient`，由 `iwara_state_factory` 挂到 `app.state.iwara_client` 上。
它持有 curl_cffi 会话，主项目的 lifespan 不管这些会话，所以这里把主项目
的 lifespan 包一层，退出时顺手释放。

与 `novel` 的另一处区别：iwara 不参与扩展机制，它是**自己独立的入口**
（`iwara/run.py`），因为它要多装编译依赖 `curl_cffi`。所以导航页那一层
它不改路由（根地址 `/` 仍是主项目的导航页），而是通过
`app.state.nav_sections` 把自己的接口**加进那一页**，效果就是
`iwara/run.py` 启动后 `/` 这个导航页会连 iwara 的接口一起列出。

人看的视频页面（`/iwara`）**不在这个包里**：它是独立的扩展包 `iwara_web`，
按配置挂上去（`PIXIV_BRIDGE_EXTENSIONS=iwara_web`）。这样「接口」与「页面」
可以分开分发，装了接口包也不必带上页面。这里仍然在导航页里列出那个地址，
方便挂上页面之后一眼能找到（没挂就是 404，导航页的无障碍降级）。
"""

from contextlib import asynccontextmanager
import os

from pixiv_bridge.nav import NavItem, NavSection
from pixiv_bridge.server import create_app as _create_bridge_app

from . import tokens
from .client import IwaraClient
from .routes import videos

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
        # 主项目的导航页会读这个属性，把 iwara 的页面列进去。
        "nav_sections": (IWARA_NAV,),
    }


def create_app(settings=None, transport=None, extensions=()):
    """接口 + iwara 的完整服务（`iwara/run.py` 的入口）。

    `extensions` 是「接口 + 对应扩展」的组合形态：`iwara/run.py` 读配置
    （`PIXIV_BRIDGE_EXTENSIONS`）加载扩展后传进来，典型的是 `iwara_web`
    （人看的视频页面）。不传就是纯接口。
    """
    app = _create_bridge_app(
        settings,
        transport=transport,
        state_factory=iwara_state_factory,
        extra_routers=IWARA_ROUTERS,
        extensions=extensions,
    )
    _close_client_on_shutdown(app)
    return app


def _close_client_on_shutdown(app):
    """包装路由上的 lifespan，让应用关闭时释放 iwara 的 curl 会话。"""
    inner = app.router.lifespan_context

    @asynccontextmanager
    async def lifespan(app):
        async with inner(app):
            try:
                yield
            finally:
                client = getattr(app.state, "iwara_client", None)
                if client is not None:
                    await client.aclose()

    app.router.lifespan_context = lifespan
