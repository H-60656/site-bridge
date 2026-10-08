"""路由模块共用的依赖访问器：从 ``app.state`` 取主项目注入的对象。
"""

def get_api(request):
    return request.app.state.api


def get_oauth(request):
    return request.app.state.oauth


def get_image_proxy(request):
    return request.app.state.image_proxy
