"""把小说阅读子项目挂到 Pixiv 接口站点（`pixiv/`）的应用上。

依赖关系：本模块只 import 框架（`pixiv_bridge`）、Pixiv 站点（`pixiv`）与本
子项目的模块；框架不认识 `novel`，Pixiv 站点也不认识它，单向依赖不会倒过来。

命令行上的启用方式是**扩展机制**（配置驱动）：扩展目录里放一个固定的
`extension.py`（本目录已有），配置里写 `PIXIV_BRIDGE_EXTENSIONS=novel`，
Pixiv 站点就会执行它并把阅读器挂上去 —— 不需要专属入口脚本。

本模块提供两条等价的路：

- `novel/extension.py` 导出 `EXTENSION_ROUTERS` 与 `extension_state_factory`，
  供扩展机制使用（`python pixiv/run.py` 走这条）；
- `create_app()` 直接显式组装，供测试与脚本使用。

`create_app()` 的构造过程：

1. `novel_state_factory(settings)`：按框架 Settings 里的四个文件路径建好
   三本台账 + 用户配置 + 本地书架；
2. `pixiv.create_app(..., state_factory=..., extra_routers=...)`：Pixiv 站点建
   FastAPI 应用与登录/OAuth/图片代理（以及 iwara 之外的其余 Pixiv 接口），
   再把本子项目的对象挂到 `app.state` 上；
3. 最后把本子项目的路由挂上去（历史 / 下载 / 收藏 / 配置书架 / 阅读器页面）。

也就是说：**阅读器是挂在 Pixiv 站点上的** —— 它要用 Pixiv 的 `app.state.api`
等对象取数据；单独跑 `novel` 不是受支持的用法。
"""

from pixiv.app import create_app as _create_pixiv_app
from pixiv_bridge.userdata import LocalLibrary, UserConfig

from .bookmarks import BookmarkStore
from .downloads import DownloadStore
from .history import HistoryStore
from .routes import bookmarks, config, downloads, history, web

NOVEL_STATE_KEYS = (
    "history",
    "downloads",
    "bookmarks",
    "user_config",
    "library",
)

NOVEL_ROUTERS = (
    history.router,
    downloads.router,
    bookmarks.router,
    config.router,
    web.router,
)


def novel_state_factory(settings):
    """按框架 Settings 建出本子项目要挂到 app.state 上的那几个对象。"""
    user_config = UserConfig(settings.config_file)
    user_config.load()
    return {
        "history": HistoryStore(settings.history_file),
        "downloads": DownloadStore(settings.downloads_file),
        "bookmarks": BookmarkStore(settings.bookmarks_file),
        "user_config": user_config,
        "library": LocalLibrary(user_config),
    }


def create_app(settings=None, transport=None):
    """Pixiv 接口 + 小说阅读的完整服务（显式组装路径，主要给测试与脚本用）。

    命令行启动不走这里 —— 阅读器作为**扩展**挂在 Pixiv 站点上：把 `novel/`
    放进项目目录，配置里写 `PIXIV_BRIDGE_EXTENSIONS=novel`，由
    `pixiv_bridge.extensions` 执行 `novel/extension.py` 完成挂载（见该文件）。
    本函数保留原名与语义，与扩展共用同一套 `NOVEL_ROUTERS` /
    `novel_state_factory`，两条路挂出来的应用是同一个。
    """
    return _create_pixiv_app(
        settings,
        transport=transport,
        state_factory=novel_state_factory,
        extra_routers=NOVEL_ROUTERS,
    )
