"""asmr_one 路由共用的取用函数。"""


def get_client(request):
    """本子项目挂在 app.state 上的 AsmrClient。"""
    return request.app.state.asmr_client
