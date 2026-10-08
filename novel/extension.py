"""小说阅读器的扩展入口（固定文件名 `extension.py`）。

接口项目 `pixiv_bridge` 不 import 本目录里的任何东西；它只是按配置
`PIXIV_BRIDGE_EXTENSIONS=novel` 找到这个文件并执行它，然后读走下面三个名字：

- `EXTENSION_ROUTERS`（必需）—— 要挂到接口应用上的路由；
- `extension_state_factory`（可选）—— 返回要挂到 `app.state` 上的对象字典；
- `extension_shutdown`（可选）—— 停机时释放自己的资源；
- `EXTENSION_NAV`（可选）—— 在导航页（`/`）上露出的页面清单。

也就是说「把 `novel/` 放进项目目录 + 配置里写一行 `PIXIV_BRIDGE_EXTENSIONS=novel`」
就是启用阅读器的全部动作，不再需要 `novel/run.py` 这样的专属入口。

注意根地址 `/` 是接口项目的**导航页**，阅读器挂在 `/novel`
（以前它自己占 `/`，加了导航页之后让位了）。

依赖方向依旧是单向的：这里可以 import `pixiv_bridge`，反过来不行。
`novel/app.py` 保留着 `create_app()` 这条显式组装路径（测试用），
两者共用同一个 `NOVEL_ROUTERS` / `novel_state_factory`。
"""

from novel.app import NOVEL_ROUTERS, novel_state_factory
from novel.routes.web import WEB_PATH

# 接口项目会把这些路由挂在纯接口路由之后。
EXTENSION_ROUTERS = NOVEL_ROUTERS

# 与 pixiv_bridge.server.create_app(state_factory=...) 同形：
# 返回 {app.state 上的属性名: 对象}。
extension_state_factory = novel_state_factory

# 导航页上露出的页面（每项 {title, path, description}）。
EXTENSION_NAV = (
    {
        "title": "小说阅读器",
        "path": WEB_PATH,
        "description": "搜作品 / 搜作者、榜单与关注、连续阅读、导出 EPUB",
    },
)
