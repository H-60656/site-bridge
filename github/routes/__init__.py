"""github 站点的路由包。

只暴露一个取客户端的入口；路由模块按资源分文件，新增时在
``github/app.py`` 的 ``GITHUB_ROUTERS`` 里登记即可。
"""

from __future__ import annotations


def get_client(request):
    """从 ``app.state`` 取站点客户端（框架在 lifespan 里挂上去的）。"""
    return request.app.state.github_client
