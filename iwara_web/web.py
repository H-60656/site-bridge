"""iwara 视频播放页 —— 独立扩展包。

它跟 `iwara/` 接口包是分开的两份：接口包只出 JSON 接口（`/api/iwara/...`），
页面这层（HTML + 路由）单独作为扩展挂上去。想让接口带页面，就把它挂成扩展
（配置里写 `iwara_web`，见项目根的 `.env.example`）。

挂载方式与 `novel` 扩展一样：目录里必须有固定的 `extension.py`。
它**依赖 `iwara` 接口项目**（页面里的接口调用全是绝对路径 `/api/iwara/...`），
所以挂它之前得先有 iwara 接口包。

路径是 `/iwara` 而不是 `/`：根地址留给接口项目的**导航页**
（`site_bridge/routes/nav.py`），导航页再把这里列成一条链接
（本包的 `extension.py` 用 `EXTENSION_NAV` 声明）。

页面内部不需要跟着改 —— 它的接口调用全是绝对路径，前端路由走
`location.hash`，跟挂在哪个路径下无关。
"""

from pathlib import Path

from fastapi import APIRouter
from fastapi.responses import FileResponse

router = APIRouter(tags=["web"])

# 页面与 web.py 同级，都在本包目录里。
WEB_INDEX = Path(__file__).resolve().parent / "index.html"

# 页面地址（导航页链接与文档都以它为准）。
WEB_PATH = "/iwara"


@router.get(WEB_PATH, include_in_schema=False)
async def index():
    return FileResponse(WEB_INDEX)
