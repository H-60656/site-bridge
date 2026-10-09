"""Pornhub 接口站点：作品浏览、搜索、视频直链解析与播放代转（自带抓取客户端）。

依赖方向与 ``iwara`` / ``asmr_one`` 一致：本包只 import 框架 ``pixiv_bridge`` 与
自己的模块，不认识 ``pixiv`` / ``iwara`` / ``asmr_one`` —— 各接口项目同级、互不挂载。

与三个站点都不同的地方在传输层与数据面：

* Pornhub **没有公开 JSON API**（旧版 ``api.php`` 早已下线），能用的只有网页本身
  和页面里嵌入的一份 ``window.initialState`` JSON。所以这里是一个**抓取器**，
  不是 API 客户端：拿 HTML → 抠出那段 JSON → 归一化成接口返回。
* Pornhub 同样在 Cloudflare 后面、同样只认浏览器 TLS 指纹，所以同样自带一个
  ``curl_cffi`` 客户端（种子 IP 池固定 + 可选 ECH），机制与 asmr_one 同源。

抓取器天然比真实 API 脆：Pornhub 改版就会让字段消失，接口会回 502。本站在这一层
只承诺「匿名可见内容」（推荐 / 热门 / 搜索 / 视频页 / 直链代转），不碰登录态。

详见 `pornhub/client.py` 的模块 docstring。
"""

from typing import TYPE_CHECKING

if TYPE_CHECKING:  # 只给类型检查看，运行时不导入
    from .app import PH_ROUTERS, create_app, ph_state_factory

__all__ = ["PH_ROUTERS", "create_app", "ph_state_factory"]


def __getattr__(name):
    """按需导入 `pornhub.app` 里的三个名字。

    与 `asmr_one/__init__.py` 同因：不能写成模块级 `from .app import ...` ——
    `pornhub.app` 会一路 import 到 `pornhub.client`，而后者顶部就要
    `curl_cffi`。它是编译包、只写在 `pornhub/requirements.txt` 里，新克隆的
    仓库还没装，一旦在包的 `__init__` 里就拖进来，自举（先装依赖再建应用）
    根本跑不到。应用入口照旧显式 `from pornhub.app import SITE`。
    """
    if name in __all__:
        from . import app

        return getattr(app, name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
