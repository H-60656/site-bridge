"""导航页路由：接口项目把 `/` 让给这一页。

页面内容全部由 `pixiv_bridge/nav.py` 从当前应用状态渲染（挂了哪些扩展、
各扩展的 `EXTENSION_NAV` 声明了什么、配置里补了哪些别的入口链接），
所以这里只是薄薄一层 HTTP 包装。

注意 `/` 不再属于任何扩展：小说阅读器已经挪到 `/novel`（见 `novel/extension.py`）。
这个路由在扩展路由之前挂载，因此即使某个扩展也声明了 `/`，导航页也不会被顶掉。
"""

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse

from ..nav import render_page

router = APIRouter(tags=["nav"])


@router.get("/", include_in_schema=False)
async def index(request: Request):
    """导航页：列出接口项目自带的入口与各扩展声明的页面。"""
    return HTMLResponse(render_page(request.app))
