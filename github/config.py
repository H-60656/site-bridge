"""github 站点的配置。

这个站点在四个老站点里最省事：``api.github.com`` 在本机**直连**就通（不需要
ECH、不需要借道 Cloudflare、不需要 HTTP/3），所以没有 ``ech*`` / ``edge*`` /
``http3`` 那一组开关，也没有单独的 ``requirements.txt``。

它真正需要配的只有两件事：

- ``token``：GitHub 对未认证请求只给 **60 次/小时**（search 更狠，10 次/小时），
  写进 ``GITHUB_TOKEN`` 才能用 5000 次/小时。留空也能跑，只是自检与翻页很快就
  会撞到 403；
- ``api_url`` / ``per_page``：留默认即可，只有在 GitHub Enterprise 或想调小单页
  条数时才需要改。

沿用 ehviewer / asmr 的写法：站点配置继承框架 ``Settings``，用**站点自己的环境
变量前缀**（``GITHUB_*``），不混用 ``PIXIV_``。
"""

from __future__ import annotations

import os
from dataclasses import dataclass, fields

from site_bridge.config import Settings  # noqa: F401  （re-export：站点内沿用同一写法）

#: 官方公有 API 的地址；GitHub Enterprise 才需要改。
DEFAULT_API_URL = "https://api.github.com"

#: 官方单页上限 100，默认按 30 给（与未认证时的推荐节奏一致）。
DEFAULT_PER_PAGE = 30


def _int_or_default(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if raw is None or not raw.strip():
        return default
    try:
        return int(raw.strip())
    except ValueError:
        return default


def _bounded(value: int, low: int, high: int) -> int:
    """把外部配置夹进合法区间，别让一个手滑的 0 传到上游。"""
    return max(low, min(high, value))


@dataclass
class GithubSettings(Settings):
    """站点专属配置（另有 ``GITHUB_*`` 前缀的环境变量）。"""

    #: 可选 access token；留空走未认证（60 次/小时）。
    token: str = ""
    #: API 根地址，末尾不带斜杠。
    api_url: str = DEFAULT_API_URL
    #: 列表接口默认单页条数，夹在 1..100。
    per_page: int = DEFAULT_PER_PAGE

    @property
    def authenticated(self) -> bool:
        """有没有配 token —— 自检和导航都靠它给出不同的提示。"""
        return bool(self.token.strip())

    @classmethod
    def load(cls, env_path=None, migrate: bool = True) -> "GithubSettings":
        base = Settings.load(env_path, migrate=migrate)
        return cls(
            **vars(base),
            token=os.environ.get("GITHUB_TOKEN", "").strip(),
            api_url=(os.environ.get("GITHUB_API_URL", "") or DEFAULT_API_URL).strip().rstrip("/"),
            per_page=_bounded(_int_or_default("GITHUB_PER_PAGE", DEFAULT_PER_PAGE), 1, 100),
        )


def as_github_settings(settings) -> GithubSettings:
    """把框架的 ``Settings`` 升级成本站点的 ``Settings``。

    ``run.py`` 走框架时拿到的是框架 ``Settings``（它不认识 ``token`` 这些字段），
    所以各处都要过一道这个函数。已是本类就原样返回。
    """
    if settings is None:
        return GithubSettings.load()
    if isinstance(settings, GithubSettings):
        return settings
    base = {field.name: getattr(settings, field.name) for field in fields(Settings)}
    return GithubSettings(
        **base,
        token=os.environ.get("GITHUB_TOKEN", "").strip(),
        api_url=(os.environ.get("GITHUB_API_URL", "") or DEFAULT_API_URL).strip().rstrip("/"),
        per_page=_bounded(_int_or_default("GITHUB_PER_PAGE", DEFAULT_PER_PAGE), 1, 100),
    )
