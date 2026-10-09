"""Pornhub 接口站点：视频接口、运行时对象装配、应用工厂与站点描述符 ``SITE``。

依赖方向：本模块只 import 框架（``pixiv_bridge``）与本站点自己的模块，框架不
认识 ``pornhub``，单向依赖不会倒过来。

与 ``pixiv/`` / ``iwara/`` / ``asmr_one/`` 站点的区别同样在传输层与数据面：
Pornhub 没有公开 API，必须借道 curl_cffi 爬网页（详见 `pornhub/client.py`），
所以这里自己建一个 ``PornhubClient``，由 ``ph_state_factory`` 挂到
``app.state.ph_client`` 上。它持有 curl_cffi 会话，退出时由 ``ph_shutdown`` 释放。

本入口**只提供 pornhub 接口**（``/api/pornhub/*``）：Pixiv 的路由属于 ``pixiv/``、
iwara 的属于 ``iwara/``、asmr 的属于 ``asmr_one/``，各接口项目同级、互不挂载。
"""

from pathlib import Path

from pixiv_bridge.nav import NavItem, NavSection
from pixiv_bridge.server import create_bridge_app
from pixiv_bridge.site import Site

from .client import PornhubClient
from .config import PornhubSettings, as_pornhub_settings, split_ips
from .envcheck import extra_checks
from .routes import videos

TITLE = "Pornhub Bridge"
VERSION = "1.0.0"
DIRECTORY = Path(__file__).resolve().parent

PH_ROUTERS = (videos.router,)

PH_STATE_KEYS = ("ph_client", "nav_sections")

#: 导航页上露出的 pornhub 接口。地址与 `pornhub/routes/videos.py` 里的前缀一致。
PH_NAV = NavSection(
    title="Pornhub 视频（本入口独有，匿名）",
    items=(
        NavItem("视频列表", "/api/pornhub/videos?page=1", "推荐 / 热门，一页若干条"),
        NavItem("搜索", "/api/pornhub/search?query=teen", "按关键词搜视频"),
        NavItem("视频详情", "/api/pornhub/video/ph60abc123", "含直链 mp4_url / download_url"),
        NavItem("探活", "/api/pornhub/health", "确认链路通不通"),
    ),
    note="这些是 JSON 接口，用浏览器打开即可查看返回内容；视频字节走 /api/pornhub/stream 代转。",
)


def ph_state_factory(settings):
    """建出要挂到 app.state 上的 pornhub 对象。

    pornhub 不需要 token：公开浏览与播放匿名就够（要登录的只有收藏 / 订阅
    那几个端点，本站不暴露）。所以这里只按配置建客户端。
    """
    client_settings = as_pornhub_settings(settings)
    return {
        "ph_client": PornhubClient(
            ech=client_settings.ech,
            ech_config=client_settings.ech_config,
            # ech_ips 是逗号/空白分隔的字符串，与 pixiv / iwara / asmr_one 同形。
            ech_ips=split_ips(client_settings.ech_ips),
        ),
        # 框架的导航页会读这个属性，把 pornhub 的接口列进去。
        "nav_sections": (PH_NAV,),
    }


async def ph_shutdown(app):
    """退出时释放 pornhub 的 curl 会话（框架的 lifespan 会 await 它）。"""
    client = getattr(app.state, "ph_client", None)
    if client is not None:
        await client.aclose()


def create_app(settings=None, extensions=()):
    """建 pornhub 接口应用（``extensions`` 由入口按配置加载好传进来）。"""
    return create_bridge_app(
        settings,
        title=TITLE,
        version=VERSION,
        routers=PH_ROUTERS,
        state_factory=ph_state_factory,
        shutdown=ph_shutdown,
        extensions=extensions,
    )


#: 站点描述符：入口脚本只需要它。
SITE = Site(
    name="pornhub",
    directory=DIRECTORY,
    title=TITLE,
    create_app=create_app,
    version=VERSION,
    nav_order=4,
    # pornhub 只有 ECH 三个开关是自己的，其余（data_dir 等路径）都在框架 Settings 里。
    settings_class=PornhubSettings,
    required_files=("run.py", "requirements.txt"),
    checks=extra_checks,
)
