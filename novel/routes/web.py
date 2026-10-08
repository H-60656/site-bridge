"""小说阅读器的页面路由。

路径是 `/novel` 而不是 `/`：根地址留给接口项目的**导航页**
（`pixiv_bridge/routes/nav.py`），导航页再把这里列成一条链接。

页面内部不需要跟着改 —— 它的接口调用全是绝对路径（`/api/...`），
前端路由走 `location.hash`，跟挂在哪个路径下无关。
"""

from pathlib import Path

from fastapi import APIRouter
from fastapi.responses import FileResponse

router = APIRouter(tags=["web"])

# novel/routes/web.py → 上两级是 novel/，阅读器页面在 novel/web/index.html
WEB_INDEX = Path(__file__).resolve().parent.parent / "web" / "index.html"

# 阅读器页面的地址（导航页链接与文档都以它为准）。
WEB_PATH = "/novel"


@router.get(WEB_PATH, include_in_schema=False)
async def index():
    return FileResponse(WEB_INDEX)
