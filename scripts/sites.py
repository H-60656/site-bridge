#!/usr/bin/env python3
"""构建侧的站点注册表：三处工具共用同一份「有哪些项目」的事实。

运行时的站点元数据在各自的 ``<site>/app.py`` 里（那里的 ``SITE`` 是框架认的契约），
但构建侧工具不该 import 站点模块 —— 那会连带拖上 ``curl_cffi`` 这类运行时依赖，
打包测试与边界检查就都得先装好整个运行环境才能跑。所以「有哪些接口项目、在哪个
目录、用哪个入口、比框架外壳多出哪些路由、哪个扩展挂在谁身上」这份清单放在这里：

    scripts/check_boundary.py       边界、布局与路由隔离检查
    scripts/make_share_packages.py  分享包内容
    tests/test_packaging.py         打包清单

新增一个接口项目 = 写 ``<site>/app.py`` + ``<site>/run.py``，再在这里补一条
``SiteInfo``（新增扩展同理补 ``ExtensionInfo``）—— 上面三处工具就都跟上了。
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent

#: 框架包名（所有接口项目都依赖它，它不认识任何接口项目）。
FRAMEWORK_PACKAGE = "pixiv_bridge"

#: 扩展机制约定的固定入口文件名（与 ``pixiv_bridge/extensions.py`` 一致）。
EXTENSION_FILENAME = "extension.py"


@dataclass(frozen=True)
class SiteInfo:
    """一个接口项目（站点）。"""

    name: str                      # 站点名，与 ``<site>/app.py`` 里的 ``SITE.name`` 一致
    directory: str                 # 项目根下的目录名
    title: str                     # 人类可读的名字（与 ``SITE.title`` 一致）
    version: str = "1.0.0"
    extra_modules: tuple = ()      # 入口自举时要补装的顶层模块（编译包）
    requirements: str = ""         # 站点自己的依赖清单（相对项目根，空表示没有）
    files: tuple = ("run.py",)     # 站点目录下必须随包拷齐的文件
    token_script: str = ""         # 可选登录脚本（相对项目根，用于分享包说明）

    @property
    def path(self) -> Path:
        return PROJECT_ROOT / self.directory

    @property
    def entry(self) -> str:
        """入口脚本相对项目根的路径（命令行与分享包说明都用它）。"""
        return f"{self.directory}/run.py"

    def command(self, windows: bool = False) -> str:
        """启动命令（分享包 README 里照抄这行）。"""
        separator = "\\" if windows else "/"
        return f"python {self.directory}{separator}run.py"


#: 接口项目：同级、互不挂载，各自只依赖框架包。
SITES = (
    SiteInfo(
        name="pixiv",
        directory="pixiv",
        title="Pixiv Bridge",
        token_script="pixiv/get_token.py",
    ),
    SiteInfo(
        name="iwara",
        directory="iwara",
        title="Iwara Bridge",
        # iwara 的传输层要 curl_cffi（Cloudflare 只认浏览器 TLS 指纹），
        # 它是编译包，不放进框架 REQUIRED，免得只跑 pixiv 的人也被要求装。
        extra_modules=("curl_cffi",),
        requirements="iwara/requirements.txt",
        files=("run.py", "requirements.txt"),
        token_script="iwara/get_token.py",
    ),
    SiteInfo(
        name="asmr_one",
        directory="asmr_one",
        title="ASMR Bridge",
        # 与 iwara 同因：换边缘 IP（CURLOPT_RESOLVE）与叠 ECH 都要 libcurl，
        # 纯 httpx 做不到。asmr 没有自己的登录脚本（匿名就能浏览与播放）。
        extra_modules=("curl_cffi",),
        requirements="asmr_one/requirements.txt",
        files=("run.py", "requirements.txt"),
    ),
    SiteInfo(
        name="ehviewer",
        directory="ehviewer",
        title="EHViewer Bridge",
        # 同样要 libcurl，而且**必须带 HTTP/3**：e-hentai.org 在本机是按 SNI
        # 字符串封锁的（本地 DNS 答案也被投毒），TCP+TLS 钉哪个 IP 都是 RST，
        # 只有 QUIC 能过。匿名可用，所以没有登录脚本。
        extra_modules=("curl_cffi",),
        requirements="ehviewer/requirements.txt",
        files=("run.py", "requirements.txt"),
    ),
)

SITES_BY_NAME = {site.name: site for site in SITES}

#: 分享包里「站点 → 额外要带的目录」：站点的专有资产（Pixiv 的 ECH 动态库等）。
SITE_ASSETS = {
    "pixiv": ("openssl-libs",),
    "iwara": (),
    "asmr_one": (),
    "ehviewer": (),
}


@dataclass(frozen=True)
class ExtensionInfo:
    """一个扩展包（挂在某个接口项目上的附属目录）。"""

    name: str                          # 目录名，也是配置里 ``PIXIV_BRIDGE_EXTENSIONS`` 写的值
    host: str                          # 宿主站点名（``SITES_BY_NAME`` 的键）
    title: str = ""
    extra_paths: frozenset = frozenset()  # 挂上后比宿主多出来的路由路径

    @property
    def path(self) -> Path:
        return PROJECT_ROOT / self.name

    @property
    def entry(self) -> str:
        return f"{self.name}/{EXTENSION_FILENAME}"

    @property
    def host_site(self) -> SiteInfo:
        return SITES_BY_NAME[self.host]


#: 两个扩展包：只有路由，由宿主站点按配置挂载。
EXTENSIONS = (
    ExtensionInfo(
        name="novel",
        host="pixiv",
        title="小说阅读器",
        extra_paths=frozenset(
            {
                "/api/bookmarks",
                "/api/bookmarks/delete",
                "/api/config",
                "/api/downloads",
                "/api/downloads/clear",
                "/api/history",
                "/api/library",
                "/api/library/file",
                "/api/library/preview",
                # 阅读器页面：以前在 `/`，有了导航页之后让位到 `/novel`。
                "/novel",
            }
        ),
    ),
    ExtensionInfo(
        name="iwara_web",
        host="iwara",
        title="iwara 视频页面",
        extra_paths=frozenset(
            {
                # 视频页面。根地址 `/` 留给框架的导航页。
                "/iwara",
            }
        ),
    ),
)

EXTENSIONS_BY_NAME = {ext.name: ext for ext in EXTENSIONS}

#: 站点自己的接口（相对框架外壳多出来的部分），用于路由隔离检查。
IWARA_ROUTE_PATHS = frozenset(
    {
        "/api/iwara/auth",
        "/api/iwara/image",
        "/api/iwara/me",
        "/api/iwara/related/{video_id}",
        "/api/iwara/resolve/{video_id}",
        "/api/iwara/search",
        "/api/iwara/stream",
        "/api/iwara/thumbnail/{video_id}",
        "/api/iwara/video/{video_id}",
        "/api/iwara/videos",
    }
)

ASM_ROUTE_PATHS = frozenset(
    {
        "/api/asmr/auth",
        "/api/asmr/cover/{work_id}",
        "/api/asmr/search",
        "/api/asmr/stream",
        "/api/asmr/tracks/{work_id}",
        "/api/asmr/work/{work_id}",
        "/api/asmr/works",
    }
)

EH_ROUTE_PATHS = frozenset(
    {
        "/api/eh/auth",
        "/api/eh/gallery/{gid}/{token}",
        "/api/eh/images/{gid}/{token}",
        "/api/eh/list",
        "/api/eh/metadata",
        "/api/eh/page",
        "/api/eh/popular",
        "/api/eh/proxy",
        "/api/eh/search",
    }
)

#: 「站点名 → 该站点自己的路由」：路由隔离检查按这张表逐个站点比对，
#: 加站点时只要在这里补一条（不写死某个站点名）。
SITE_ROUTE_PATHS = {
    "iwara": IWARA_ROUTE_PATHS,
    "asmr_one": ASM_ROUTE_PATHS,
    "ehviewer": EH_ROUTE_PATHS,
}

#: 顶层包名：项目根下所有「有 __init__.py 的目录」+ 各站点目录。
SUB_PACKAGES = tuple(site.directory for site in SITES) + tuple(
    ext.name for ext in EXTENSIONS
)

#: 出入口：每个接口项目一个 ``run.py``，扩展包只有 ``extension.py``。
ENTRY_POINTS = tuple(site.entry for site in SITES)
FORBIDDEN_ENTRIES = tuple(
    f"{ext.name}/run.py" for ext in EXTENSIONS
) + ("run.py",)  # 根目录不再有入口
