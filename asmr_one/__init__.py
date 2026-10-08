"""asmr.one 接口站点：作品浏览、搜索、音轨树与音频/封面代转（自带客户端）。

依赖方向与 ``iwara`` 一致：本包只 import 框架 ``pixiv_bridge`` 与自己的模块，
不认识 ``pixiv`` / ``iwara`` —— 三个接口项目同级、互不挂载。

传输层的坎和 iwara 同源（都在 Cloudflare 后面、本机 DNS 与 TLS 指纹都会被拦），
所以这里同样自带一个 ``curl_cffi`` 客户端：种子 IP 池固定 + 可选 ECH。
差别是 asmr 的接口全是 JSON（没有 HTML 要解析），所以不需要 selectolax。
详见 `asmr_one/client.py` 的模块 docstring。
"""

from typing import TYPE_CHECKING

if TYPE_CHECKING:  # 只给类型检查看，运行时不导入
    from .app import ASMR_ROUTERS, create_app, asmr_state_factory

__all__ = ["ASMR_ROUTERS", "create_app", "asmr_state_factory"]


def __getattr__(name):
    """按需导入 `asmr_one.app` 里的三个名字。

    与 `iwara/__init__.py` 同因：不能写成模块级 `from .app import ...` ——
    `asmr_one.app` 会一路 import 到 `asmr_one.client`，而后者顶部就要
    `curl_cffi`。它是编译包、只写在 `asmr_one/requirements.txt` 里，新克隆的
    仓库还没装，一旦在包的 `__init__` 里就拖进来，自举（先装依赖再建应用）
    根本跑不到。应用入口照旧显式 `from asmr_one.app import SITE`。
    """
    if name in __all__:
        from . import app

        return getattr(app, name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
