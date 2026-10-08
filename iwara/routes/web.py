"""iwara 视频播放页的页面路由。

路径是 `/iwara` 而不是 `/`：根地址留给接口项目的**导航页**
（`pixiv_bridge/routes/nav.py`），导航页再把这里列成一条链接
（见 `iwara/app.py` 的 `IWARA_NAV`）。

页面内部不需要跟着改 —— 它的接口调用全是绝对路径（`/api/iwara/...`），
前端路由走 `location.hash`，跟挂在哪个路径下无关。
"""

from pathlib import Path

from fastapi import APIRouter
from fastapi.responses import FileResponse

router = APIRouter(tags=["web"])

# iwara/routes/web.py → 上两级是 iwara/，页面在 iwara/web/index.html
WEB_INDEX = Path(__file__).resolve().parent.parent / "web" / "index.html"

# 页面地址（导航页链接与文档都以它为准）。
WEB_PATH = "/iwara"


@router.get(WEB_PATH, include_in_schema=False)
async def index():
    return FileResponse(WEB_INDEX)
