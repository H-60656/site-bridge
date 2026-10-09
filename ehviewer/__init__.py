"""ehviewer 接口站点：E-Hentai 画廊浏览、搜索、详情、图片页与图片代转。

**绝不能在模块级 ``from .app import ...``**：那条链会 import ``app`` →
``routes`` → ``client`` → ``curl_cffi``，而 ``run.py`` 的自举必须排在
``import curl_cffi`` 之前（首次运行时依赖还没装上）。所以这里用
``__getattr__`` 惰性导出，和 ``asmr_one`` 一样。
"""

from typing import TYPE_CHECKING

if TYPE_CHECKING:  # pragma: no cover - 只给类型检查器看
    from .app import EH_ROUTERS, create_app, eh_state_factory

__all__ = ["EH_ROUTERS", "create_app", "eh_state_factory"]


def __getattr__(name):
    if name in __all__:
        from . import app

        return getattr(app, name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
