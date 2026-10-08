"""阅读器路由共用的依赖访问器：从 ``app.state`` 取主项目与扩展注入的对象。
"""

def get_api(request):
    """主项目注入的 PixivApi（app.state.api）。"""
    return request.app.state.api


def get_history(request):
    return request.app.state.history


def get_downloads(request):
    return request.app.state.downloads


def get_bookmarks(request):
    return request.app.state.bookmarks


def get_config(request):
    return request.app.state.user_config


def get_library(request):
    return request.app.state.library
