"""iwara.tv 接口站点：视频浏览、搜索、直链解析与代转播放（自带 curl_cffi 客户端）。

依赖方向与 `novel` 一致：本包只 import 主项目 `pixiv_bridge`，
主项目不认识 `iwara`，单向依赖不会倒过来。

和 `novel` 不同的是，iwara 站点不能用主项目那套 httpx 客户端：
本机的系统 DNS 把 `*.iwara.tv` 解析到无关网段，且 `api.iwara.tv` 在
Cloudflare 后面会拦截普通 Python TLS 指纹。因此这里自带一个基于
`curl_cffi` 的异步客户端（TLS 冒充 + 种子 IP 池 + 边缘 IP 固定）。
详见 `iwara/client.py` 的模块 docstring。
"""

from typing import TYPE_CHECKING

if TYPE_CHECKING:  # 只给类型检查看，运行时不导入
    from .app import IWARA_ROUTERS, create_app, iwara_state_factory

__all__ = ["IWARA_ROUTERS", "create_app", "iwara_state_factory"]


def __getattr__(name):
    """按需导入 `iwara.app` 里的三个名字。

    这里**不能**写成模块级 `from .app import ...`：`iwara.app` 链式导入
    `iwara.client`，而后者顶部就要 `curl_cffi`。可 `curl_cffi` 是编译包、
    只写在 `iwara/requirements.txt` 里，新克隆的仓库还没装 —— 一旦在包的
    `__init__` 里就把它拖进来，自举（先装依赖再建应用）根本跑不到：
    `from iwara import tokens` 会先炸 ImportError。
    应用入口照旧显式 `from iwara.app import create_app`。
    """
    if name in __all__:
        from . import app

        return getattr(app, name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
