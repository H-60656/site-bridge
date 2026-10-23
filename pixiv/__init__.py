"""Pixiv 接口站点：路由、客户端、协议实现、自检与入口都在这个包里。

它和 `iwara/` 是**同级**的接口项目：两者都只依赖框架包 `site_bridge`，
互不依赖；框架也不认识它们中的任何一个。

- `app.py`：站点描述符 `SITE` + 应用工厂 + 运行时对象装配
- `config.py`：`PixivSettings`（框架 Settings 基座 + Pixiv 凭据与协议常量）
- `envcheck.py`：站点专属自检（网络 / ECH / 代理）、登录校验、提示文案
- `api.py` / `oauth.py` / `image_proxy.py` / `ech_client.py` / `sni_client.py` / `hoster.py`
- `routes/`：`/api/*` 接口
- `run.py`：入口（`python pixiv/run.py`）
- `get_token.py`：交互式获取 refresh_token
"""

from site_bridge import __version__

__version__ = __version__
