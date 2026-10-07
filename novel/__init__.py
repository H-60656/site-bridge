"""小说阅读子项目。

依赖主项目 `pixiv_bridge`（接口层）提供的 PixivApi / OAuth / ImageProxy，
自己负责「阅读」这一层：阅读历史、下载记录、收藏台账、本地书架与阅读器页面。

对外入口是 `novel.app.create_app()`：在主项目的 FastAPI 应用上挂上本子项目的路由。
"""

__version__ = "1.0.0"
