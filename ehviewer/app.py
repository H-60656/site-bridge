"""ehviewer 接口站点：E-Hentai 只读接口、运行时对象装配、应用工厂与站点描述符。

依赖方向与 ``asmr_one`` / ``iwara`` 一致：本模块只 import 框架与本站点自己的
模块，框架不认识 ``ehviewer``。

与其它三个站点的区别全在传输层（详见 ``client.py`` 的模块说明）：``e-hentai.org``
在本机是**按 SNI 字符串**被封的，且本地 DNS 答案被投毒，只有 HTTP/3 能通；
而它自己解析出的 Cloudflare IP 又被 IP 级 RST，所以要借道别的 CF anycast IP。
图片则挂在 ``*.hath.network`` 上（不在 Cloudflare），直连即可，但节点常死，
需要在 ``/api/eh/proxy`` 里换边缘重解析。

本入口**只提供 E-Hentai 接口**（``/api/eh/*``），不与其它站点互挂。
"""

from pathlib import Path

from pixiv_bridge.nav import NavItem, NavSection
from pixiv_bridge.server import create_bridge_app
from pixiv_bridge.site import Site

from .client import EhClient
from .config import EhSettings, as_eh_settings, split_ips
from .envcheck import extra_checks
from .routes import galleries

TITLE = "EHViewer Bridge"
VERSION = "1.0.0"
DIRECTORY = Path(__file__).resolve().parent

EH_ROUTERS = (galleries.router,)

EH_STATE_KEYS = ("eh_client", "nav_sections")

# 导航页上露出的 E-Hentai 接口。地址与 `ehviewer/routes/galleries.py` 的前缀一致。
EH_NAV = NavSection(
    title="E-Hentai（本入口独有）",
    items=(
        NavItem("画廊列表", "/api/eh/list", "首页默认序，一页 25 条；翻页用返回里的 next 光标"),
        NavItem("搜索", "/api/eh/search?query=lolicon", "支持站内搜索语法"),
        NavItem(
            "画廊详情",
            "/api/eh/gallery/4240495/8bcad20a1a",
            "元数据 + 标签 + 条带封面 + 本屏图片页地址",
        ),
        NavItem(
            "图片直链",
            "/api/eh/images/4240495/8bcad20a1a",
            "这一屏的 /s/ 大图页地址（喂给 proxy 即可看图）",
        ),
        NavItem("官方元数据", "/api/eh/metadata?gid=4240495&token=8bcad20a1a", "走 api.php 的 gdata"),
        NavItem("代转图片", "/api/eh/proxy?url=<上一步的地址>", "服务端取字节转发，自动换节点重试"),
        NavItem("运行状态", "/api/eh/auth", "匿名可用；顺带回报借道边缘情况"),
    ),
    note="图片必须走 /api/eh/proxy 代转：真实图床是随机的 H@H 家用节点，浏览器直连常失败。",
)


def eh_state_factory(settings):
    """建出要挂到 ``app.state`` 上的本站对象。

    E-Hentai 的只读接口匿名就能用，所以这里没有 token 一说，只按配置建客户端。
    """
    client_settings = as_eh_settings(settings)
    return {
        "eh_client": EhClient(
            # http3 关掉等于关掉整个站点（TCP 路径在本机是死的），默认开。
            http3=client_settings.http3,
            edge_ips=split_ips(client_settings.edge_ips),
            edge_attempts=client_settings.edge_attempts,
            image_attempts=client_settings.image_attempts,
        ),
        # 框架的导航页会读这个属性，把本站接口列进去。
        "nav_sections": (EH_NAV,),
    }


async def eh_shutdown(app):
    """退出时释放 curl 会话（框架的 lifespan 会 await 它）。"""
    client = getattr(app.state, "eh_client", None)
    if client is not None:
        await client.aclose()


def create_app(settings=None, extensions=()):
    """建 ehviewer 接口应用（``extensions`` 由入口按配置加载好传进来）。"""
    return create_bridge_app(
        settings,
        title=TITLE,
        version=VERSION,
        routers=EH_ROUTERS,
        state_factory=eh_state_factory,
        shutdown=eh_shutdown,
        extensions=extensions,
    )


#: 站点描述符：入口脚本只需要它。
SITE = Site(
    name="ehviewer",
    directory=DIRECTORY,
    title=TITLE,
    create_app=create_app,
    version=VERSION,
    nav_order=4,
    # 站点专属配置只有 http3 / 边缘池 / 重试次数，其余（data_dir 等）在框架 Settings 里。
    settings_class=EhSettings,
    required_files=("run.py", "requirements.txt"),
    checks=extra_checks,
)
