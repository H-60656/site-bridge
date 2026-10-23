"""github 接口站点：路由清单、运行时对象装配、应用工厂与站点描述符 ``SITE``。

框架（``pixiv_bridge``）只认识 :class:`pixiv_bridge.site.Site` 这一份契约；
本模块把 GitHub 的细节填进去：

- ``GITHUB_ROUTERS``：``/api/github/*`` 路由（``/api/health`` 与导航页由框架提供）；
- ``github_state_factory``：建 ``httpx`` 客户端包成 :class:`GithubClient`，挂到
  ``app.state.github_client`` 上。这个工厂是**同步**的 —— 与 pixiv/ehviewer 不同，
  这里没有需要 await 的加载步骤（不读 token 文件，token 是纯环境变量）；
- ``GITHUB_ERROR_TYPES``：站点异常，交给框架统一映射成
  ``{"error": {"message": ..., "reason": ...}}``；
- ``create_app``：站点应用工厂；
- ``SITE``：入口脚本（``github/run.py``）交给 ``pixiv_bridge.cli.main`` 的东西。

**没有代理**：``api.github.com`` 在本机直连即可（实测 200），所以这里刻意不读
``settings.proxy``，也没有 ECH / 边缘 IP 借道 —— 这一层在这个站点上是纯负担。
"""

from pathlib import Path

import httpx

from pixiv_bridge.nav import NavItem, NavSection
from pixiv_bridge.server import create_bridge_app
from pixiv_bridge.site import Site

from .client import (
    GithubClient,
    GithubError,
    GithubForbiddenError,
    GithubNotFoundError,
    GithubRateLimitError,
    GithubUnauthorizedError,
)
from .config import GithubSettings, as_github_settings
from .envcheck import extra_checks
from .routes import repos

TITLE = "GitHub Bridge"
VERSION = "1.0.0"
DIRECTORY = Path(__file__).resolve().parent

#: 纯接口路由；导航页（`/`）与健康检查（`/api/health`）由框架挂载。
GITHUB_ROUTERS = (repos.router,)

#: 需要映射成统一错误响应形状的站点异常（``httpx`` 的 502 由框架兜底）。
GITHUB_ERROR_TYPES = (
    GithubError,
    GithubNotFoundError,
    GithubUnauthorizedError,
    GithubForbiddenError,
    GithubRateLimitError,
)

#: 运行时对象：客户端 + 导航段。
GITHUB_STATE_KEYS = ("github_client", "nav_sections")

GITHUB_NAV = NavSection(
    title="GitHub（本入口独有）",
    items=(
        NavItem("仓库主档", "/api/github/repo/{owner}/{repo}", "star / fork / 语言 / 许可证 / 默认分支"),
        NavItem("README", "/api/github/readme/{owner}/{repo}", "已解码成文本的 README 正文"),
        NavItem("目录 / 文件", "/api/github/contents/{owner}/{repo}/{path}", "目录列表或单文件条目，path 可空"),
        NavItem("发行版", "/api/github/releases/{owner}/{repo}", "含资产清单与下载数"),
        NavItem("issues", "/api/github/issues/{owner}/{repo}", "state=open|closed|all，PR 用 is_pull_request 区分"),
        NavItem("提交", "/api/github/commits/{owner}/{repo}", "首行摘要 + 作者 + 日期"),
        NavItem("仓库搜索", "/api/github/search/repositories?q=", "search 配额独立，未认证 10 次/小时"),
        NavItem("用户 / 组织", "/api/github/user/{login}", "公开档案与粉丝数"),
        NavItem("配额账本", "/api/github/rate_limit", "core / search / graphql 各自一组"),
        NavItem("认证状态", "/api/github/auth", "当前是否带 token、预算多少（不打上游）"),
    ),
    note=(
        "只读 JSON 接口，全部挂在 `/api/github` 下。**未认证只有 60 次/小时**"
        "（search 单独 10 次/小时）：配 `GITHUB_TOKEN` 任何一次请求都会带上去，"
        "配额提到 5000 次/小时。这个站点不使用代理，直连 api.github.com。"
    ),
)


def github_state_factory(settings, http: httpx.AsyncClient | None = None):
    """建 GitHub 的运行时对象（要 ``setattr`` 到 ``app.state`` 上的字典）。

    ``http`` 是测试与自检用的注入点：传一个带 ``httpx.MockTransport`` 的
    ``AsyncClient`` 就能完全离线；留空由客户端自己建、自己关。
    """
    return {
        "github_client": GithubClient(settings, http=http),
        "nav_sections": (GITHUB_NAV,),
    }


async def github_shutdown(app):
    """站点自己的资源：关掉那个 httpx 客户端。"""
    client = getattr(app.state, "github_client", None)
    if client is not None:
        await client.aclose()


def create_app(settings=None, http: httpx.AsyncClient | None = None, extensions=()):
    """建 GitHub 接口应用。

    只挂只读的 ``/api/github/*`` 路由加框架的导航页（`/`）与健康检查。
    ``settings`` 可以是框架的 ``Settings``（``run.py`` 那条路拿到的就是它），
    这里过一道 :func:`as_github_settings` 升成本站点的配置。
    """
    resolved = as_github_settings(settings)

    return create_bridge_app(
        resolved,
        title=TITLE,
        version=VERSION,
        routers=GITHUB_ROUTERS,
        state_factory=lambda value: github_state_factory(value, http),
        shutdown=github_shutdown,
        error_types=GITHUB_ERROR_TYPES,
        extensions=extensions,
    )


#: 站点描述符：入口脚本只需要它。
SITE = Site(
    name="github",
    directory=DIRECTORY,
    title=TITLE,
    create_app=create_app,
    version=VERSION,
    nav_order=5,
    settings_class=GithubSettings,
    required_files=("run.py",),
    checks=extra_checks,
)
