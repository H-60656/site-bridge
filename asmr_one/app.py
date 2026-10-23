"""asmr 接口站点：作品接口、运行时对象装配、应用工厂与站点描述符 ``SITE``。

依赖方向：本模块只 import 框架（``site_bridge``）与本站点自己的模块，框架不
认识 ``asmr_one``，单向依赖不会倒过来。

与 ``pixiv/`` / ``iwara/`` 站点的区别同样在传输层：asmr.one 的边缘 IP 在本机
是 IP 级被阻断的，必须借道别的 Cloudflare anycast IP 再叠 ECH，所以这里自己
建一个 ``AsmrClient``，由 ``asmr_state_factory`` 挂到 ``app.state.asmr_client``
上。它持有 curl_cffi 会话，退出时由 ``asmr_shutdown`` 释放。

本入口**只提供 asmr 接口**（``/api/asmr/*``）：Pixiv 的路由属于 ``pixiv/``、
iwara 的属于 ``iwara/``，三个接口项目同级、互不挂载。
"""

from pathlib import Path

from site_bridge.nav import NavItem, NavSection
from site_bridge.server import create_bridge_app
from site_bridge.site import Site

from .client import AsmrClient
from .config import AsmrSettings, as_asmr_settings, split_ips
from .envcheck import extra_checks
from .routes import works

TITLE = "ASMR Bridge"
VERSION = "1.0.0"
DIRECTORY = Path(__file__).resolve().parent

ASMR_ROUTERS = (works.router,)

ASMR_STATE_KEYS = ("asmr_client", "nav_sections")

# 导航页上露出的 asmr 接口。地址与 `asmr_one/routes/works.py` 里的前缀一致。
ASMR_NAV = NavSection(
    title="asmr 音频（本入口独有）",
    items=(
        NavItem("作品列表", "/api/asmr/works?page=1", "默认序，一页 20 条"),
        NavItem("搜索", "/api/asmr/search?query=催眠", "按关键词搜作品"),
        NavItem("音轨列表", "/api/asmr/tracks/1669340", "作品里的音频文件（树已展平）"),
        NavItem("封面", "/api/asmr/cover/1669340?type=main", "main / sam / 240x240"),
        NavItem("登录状态", "/api/asmr/auth", "本站匿名可用"),
    ),
    note="这些是 JSON 接口，用浏览器打开即可查看返回内容；音频字节走 /api/asmr/stream 代转。",
)


def asmr_state_factory(settings):
    """建出要挂到 app.state 上的 asmr 对象。

    asmr 不需要 token：公开浏览与播放匿名就够（要登录的只有标签 / 用户收藏
    那几个端点，本站不暴露）。所以这里只按配置建客户端。
    """
    client_settings = as_asmr_settings(settings)
    return {
        "asmr_client": AsmrClient(
            ech=client_settings.ech,
            ech_config=client_settings.ech_config,
            # ech_ips 是逗号/空白分隔的字符串，与 pixiv / iwara 那几个 *_ips 一致。
            ech_ips=split_ips(client_settings.ech_ips),
        ),
        # 框架的导航页会读这个属性，把 asmr 的接口列进去。
        "nav_sections": (ASMR_NAV,),
    }


async def asmr_shutdown(app):
    """退出时释放 asmr 的 curl 会话（框架的 lifespan 会 await 它）。"""
    client = getattr(app.state, "asmr_client", None)
    if client is not None:
        await client.aclose()


def create_app(settings=None, extensions=()):
    """建 asmr 接口应用（``extensions`` 由入口按配置加载好传进来）。"""
    return create_bridge_app(
        settings,
        title=TITLE,
        version=VERSION,
        routers=ASMR_ROUTERS,
        state_factory=asmr_state_factory,
        shutdown=asmr_shutdown,
        extensions=extensions,
    )


#: 站点描述符：入口脚本只需要它。
SITE = Site(
    name="asmr_one",
    directory=DIRECTORY,
    title=TITLE,
    create_app=create_app,
    version=VERSION,
    nav_order=3,
    # asmr 只有 ECH 三个开关是自己的，其余（data_dir 等路径）都在框架 Settings 里。
    settings_class=AsmrSettings,
    required_files=("run.py", "requirements.txt"),
    checks=extra_checks,
)
