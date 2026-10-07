def get_api(request):
    return request.app.state.api


def get_oauth(request):
    return request.app.state.oauth


def get_image_proxy(request):
    return request.app.state.image_proxy
