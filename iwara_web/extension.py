"""iwara 视频播放页作为「扩展」挂到接口项目上。

固定文件名约定：扩展目录里必须有 `extension.py`，里面定义
`EXTENSION_ROUTERS`（要挂的路由元组），可选的还有
`extension_state_factory`（往 `app.state` 上放东西）与 `EXTENSION_NAV`
（在导航页 `/` 上声明自己的页面）。

这个扩展只有路由、没有状态，所以不定义 state factory；也正因为没有
需要释放的资源，不需要 `extension_shutdown`。

挂载前提：`iwara` 接口包也在（页面调用的全是 `/api/iwara/...`）。
"""

from iwara_web.web import WEB_PATH, router

EXTENSION_ROUTERS = (router,)

EXTENSION_NAV = (
    {
        "title": "iwara 视频页",
        "path": WEB_PATH,
        "description": "浏览 / 搜索 / 播放，带详情页与相关视频",
    },
)
