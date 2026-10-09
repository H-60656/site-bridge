"""ehviewer 站点的路由共用件。"""

from __future__ import annotations


def get_client(request):
    """从应用状态里取本站的客户端（``app.py`` 的 state factory 放进去的）。"""
    return request.app.state.eh_client
