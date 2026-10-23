"""统一的错误响应形状：``{"error": {"message": ..., "reason": ...}}``。

框架只认异常对象上的三个属性（``status`` / ``reason`` / ``message``），所以站点可以
直接沿用自己原有的异常类，只要它们带着这三个属性；想让框架顺手给默认值，就继承
:class:`BridgeError`。上游 ``httpx.HTTPError`` 一律映射成 502，所有站点共用。
"""

import httpx
from fastapi import FastAPI
from fastapi.responses import JSONResponse


class BridgeError(Exception):
    """站点异常基类：三个类属性就是响应体里的三个值。"""

    status = 400
    reason = ""
    message = ""

    def __init__(self, message="", status=None, reason=None):
        super().__init__(message or self.__class__.message)
        self.message = message or self.__class__.message
        if status is not None:
            self.status = status
        if reason is not None:
            self.reason = reason


def error_body(exc):
    """把异常里的 message / reason 取成响应体。"""
    return {
        "error": {
            "message": getattr(exc, "message", ""),
            "reason": getattr(exc, "reason", ""),
        }
    }


async def handle_site_error(request, exc):
    return JSONResponse(
        status_code=getattr(exc, "status", None) or 400,
        content=error_body(exc),
    )


async def handle_upstream_error(request, exc):
    return JSONResponse(
        status_code=502,
        content={
            "error": {
                "message": f"上游请求失败: {exc.__class__.__name__}",
                "reason": "upstream_error",
            }
        },
    )


def install_error_handlers(app: FastAPI, error_types=()):
    """注册站点异常的统一处理器，外加 ``httpx.HTTPError`` → 502。"""
    for exc_type in error_types:
        app.add_exception_handler(exc_type, handle_site_error)
    app.add_exception_handler(httpx.HTTPError, handle_upstream_error)
    return app
