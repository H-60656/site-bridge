"""iwara.tv 子项目。

依赖方向与 `novel` 一致：本包只 import 主项目 `pixiv_bridge`，
主项目不认识 `iwara`，单向依赖不会倒过来。

和 `novel` 不同的是，iwara 站点不能用主项目那套 httpx 客户端：
本机的系统 DNS 把 `*.iwara.tv` 解析到无关网段，且 `api.iwara.tv` 在
Cloudflare 后面会拦截普通 Python TLS 指纹。因此这里自带一个基于
`curl_cffi` 的异步客户端（TLS 冒充 + 种子 IP 池 + 边缘 IP 固定）。
详见 `iwara/client.py` 的模块 docstring。
"""

from .app import IWARA_ROUTERS, create_app, iwara_state_factory

__all__ = ["IWARA_ROUTERS", "create_app", "iwara_state_factory"]
