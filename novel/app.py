"""把小说阅读子项目挂到主项目的接口应用上。

依赖关系：本模块只 import 主项目（`pixiv_bridge`）与本子项目的模块，
主项目不认识 `novel`，单向依赖不会倒过来。

`create_app()` 的构造过程：

1. `novel_state_factory(settings)`：按主项目 Settings 里的四个文件路径建好
   三本台账 + 用户配置 + 本地书架；
2. `pixiv_bridge.server.create_app(..., state_factory=...)`：主项目建 FastAPI
   应用、登录/OAuth/图片代理，再把这些对象挂到 `app.state` 上；
3. 最后把本子项目的路由挂上去（历史 / 下载 / 收藏 / 配置书架 / 阅读器页面）。
"""

from pixiv_bridge.server import create_app as _create_bridge_app
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
    """按主项目的 Settings 建出本子项目要挂到 app.state 上的那几个对象。"""
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
    """接口 + 小说阅读的完整服务（`novel/run.py` 的入口）。"""
    return _create_bridge_app(
        settings,
        transport=transport,
        state_factory=novel_state_factory,
        extra_routers=NOVEL_ROUTERS,
    )
