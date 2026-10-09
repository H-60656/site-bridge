#!/usr/bin/env python3
"""生成分享包：把项目按「接口包 / 扩展包 / 接口+扩展组合包」打成若干 zip。

包的构成（几个接口包、几个扩展包）跟着 ``scripts/sites.py`` 注册表走，
当前清单与数量以 ``--list`` 的输出为准。

用法::

    python scripts/make_share_packages.py            # 生成全部包到 dist/
    python scripts/make_share_packages.py --check     # 只做内容与结构检查，不写 zip
    python scripts/make_share_packages.py pixiv-api novel-extension

每个包解压后是一个同名文件夹，根目录里有一份**该包自己的** ``README.md``
（说明这东西是什么、怎么启动、包里有什么），完整文档放在 ``手册/`` 里。

布局约定（重构后）：接口项目 ``pixiv/``、``iwara/``、``asmr_one/`` 是**同级**目录，
各自有 ``run.py`` 入口，都只依赖框架 ``pixiv_bridge/``；根目录不再有 ``run.py``。
所以每个接口包 = ``pixiv_bridge/`` + 一个（或两个）站点目录 + 根目录的启动文件。

约定：扩展包（``novel`` / ``iwara_web``）只是可挂载的目录，不能单独启动 ——
它们要放进一个接口项目的**项目根**，再在 ``.env`` 里写扩展名。

站点清单来自 ``scripts/sites.py``（唯一事实来源），本脚本只负责内容与文案。
"""

from __future__ import annotations

import argparse
import shutil
import sys
import zipfile
from dataclasses import dataclass
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT / "scripts"))

from sites import (  # noqa: E402  （脚本目录要在 sys.path 上）
    EXTENSIONS_BY_NAME,
    FRAMEWORK_PACKAGE,
    SITE_ASSETS,
    SITES,
    SITES_BY_NAME,
)

DIST_DIR = PROJECT_ROOT / "dist"

# 共享的根目录文件：启动脚本与依赖清单。站点自己的入口在 <站点>/run.py 里，
# 不在这一份清单里 —— 它就是「根目录不再有 run.py」这条约定的落点。
API_ROOT_FILES = (
    "requirements.txt",
    "requirements-termux.txt",
    "start.bat",
    "start.sh",
    "Dockerfile",
    ".env.example",
    ".gitattributes",
    ".dockerignore",
)

# 每个包都附的完整文档（放 手册/，避免和包自己的 README.md 打架）。
DOC_FILES = ("README.md", "INSTALL.md", "DIRECT.md")

# 打包时永远不要带上的东西。
IGNORED_DIRS = {"__pycache__", ".venv", "venv", "dist", "_tmp_tests", "_scratch", "data"}
IGNORED_SUFFIXES = (".pyc", ".pyo")

# 所有接口包都带的公共目录。
SHARED_DIRS = ("examples",)


@dataclass(frozen=True)
class PackageSpec:
    """一个分享包的定义。"""

    key: str                      # 命令行里用来选包的短名
    zip_name: str                 # 输出文件名（不含 .zip）与 zip 内的根文件夹名
    title: str                    # 包自己的 README 标题
    kind: str                     # api / extension / bundle
    summary: str                  # 一句话说明
    dirs: tuple = ()              # 从项目根拷的目录
    files: tuple = ()             # 从项目根拷的文件
    extra_files: tuple = ()       # 除了 API_ROOT_FILES 之外还要的根文件
    start_commands: tuple = ()    # 「怎么启动」的代码块行
    setup_lines: tuple = ()       # 「还要做什么」的条目
    notes: tuple = ()             # 额外说明

    def entries(self) -> tuple:
        return tuple(self.files) + tuple(self.extra_files) + tuple(self.dirs)

    @property
    def site_names(self) -> tuple:
        """这个包里的站点目录名（api 包一个、组合包一个、扩展包没有）。"""
        return tuple(name for name in self.dirs if name in SITES_BY_NAME)

    @property
    def extension_names(self) -> tuple:
        return tuple(name for name in self.dirs if name in EXTENSIONS_BY_NAME)


BASE_SETUP = (
    "安装 Python **3.10+**（这是唯一必须自己准备的东西；其余依赖首次启动会自动下载）。",
)

PIXIV_SETUP = (
    "把 `.env.example` 复制成 `.env`，填上自己的 `PIXIV_REFRESH_TOKEN`"
    "（怎么拿见 `手册/INSTALL.md`，或跑一次 `python pixiv/get_token.py`）。",
)

IWARA_SETUP = (
    "Pixiv 的登录对 iwara 接口没有影响；iwara 的登录也是可选的"
    "（公开内容含 R18 匿名就能看）：想登录订阅内容再跑一次 `python iwara/get_token.py`。",
)

BASE_NOTES = (
    "第一次启动会打印一份 `[环境检查]`，其中 `deps` 一项会自动装依赖，慢一点属正常。",
    "双击 `start.bat`（或 `sh start.sh`）走的是框架的**项目导航**：本包只有一个接口项目，"
    "所以它直接启动；想显式指定就在 `.env` 里写 `BRIDGE_SITE=<站点目录名>`。",
    "个人数据都在 `data/` 里（登录凭据 / 阅读历史 / 下载记录 / 收藏台账 / 页面配置）；"
    "换机只拷这一个目录。",
    "各接口项目 `pixiv/` / `iwara/` / `asmr_one/` 是**同级**的：各自一个 `run.py`，"
    "互相不挂载，都只用框架 `pixiv_bridge/`。",
)

PIXIV_NOTES = (
    "连不上 Pixiv 时先看 `手册/DIRECT.md`（直连思路：怎么分层定位是 DNS、IP、TLS 还是应用层的问题）。",
    "`openssl-libs/` 是项目自带的 OpenSSL 4.x 动态库（Windows 与安卓两套），"
    "ECH 模式要用；不用 ECH 的话删掉它也不影响启动。",
)

ASMR_SETUP = (
    "asmr 不需要登录：公开浏览、搜索与播放匿名就够。"
    "（要登录的只有标签 / 用户收藏那几个端点，本站不暴露它们。）",
)

IWARA_NOTES = (
    "iwara 的接口走 `curl_cffi`（Cloudflare 只认浏览器 TLS 指纹），"
    "首次启动会多装一个编译包，比 pixiv 那边慢一点。",
    "本包不含 `openssl-libs/`：那是 Pixiv 的 ECH 直连用的（ctypes 调 OpenSSL 4.0+）；"
    "iwara 的 ECH 走 `curl_cffi` 的 libcurl（`IWARA_BRIDGE_ECH=1`），不需要那套动态库。",
)

ASMR_NOTES = (
    "asmr 的 ECH **默认开**（`ASMR_BRIDGE_ECH`）：它自己那几个边缘 IP 在本机是"
    "IP 级被阻断的，必须借道 Cloudflare 别的 anycast IP 再叠 ECH 才通；关掉大概率整个站不通。",
    "换 IP 与叠 ECH 都要 libcurl，所以本包同样带 `curl_cffi`（首次启动会多装一个编译包）。",
    "音频字节在 `raw.kiko-play-niptan.one` 上，浏览器同样到不了 —— 播放器要走本站的"
    "`/api/asmr/stream?url=...` 代转（`/api/asmr/tracks/{id}` 里给的直链是给本站用的）。",
)

def _site_dirs(*names, with_assets=True) -> tuple:
    """组装包里要带的目录：框架 + 指定站点（+ 站点资产 + 公共目录）。"""
    dirs = [FRAMEWORK_PACKAGE]
    for name in names:
        dirs.append(name)
        if with_assets:
            dirs.extend(SITE_ASSETS.get(name, ()))
    dirs.extend(SHARED_DIRS)
    return tuple(dirs)


PIXIV = SITES_BY_NAME["pixiv"]
IWARA = SITES_BY_NAME["iwara"]
ASMR = SITES_BY_NAME["asmr_one"]

PACKAGES = (
    PackageSpec(
        key="pixiv-api",
        zip_name="pixiv-api",
        title="Pixiv 接口包",
        kind="api",
        summary="纯 Pixiv 接口服务：认证 / 插画 / 搜索 / 动态 / 用户 / 小说接口 / 图片代理 + `/` 导航页。",
        dirs=_site_dirs(PIXIV.directory),
        files=API_ROOT_FILES,
        start_commands=(
            "# Windows",
            "start.bat",
            "",
            "# 其它平台",
            PIXIV.command(),
        ),
        setup_lines=BASE_SETUP + PIXIV_SETUP,
        notes=(
            "启动后浏览器打开 `http://127.0.0.1:8765/` 是**导航页**（列出 `/docs`、"
            "`/api/health` 等入口）。",
            "入口是 `pixiv/run.py`；它只提供 Pixiv 接口，`/api/iwara/*` 不在本包里。",
        )
        + PIXIV_NOTES
        + BASE_NOTES,
    ),
    PackageSpec(
        key="iwara-api",
        zip_name="iwara-api",
        title="iwara 接口包",
        kind="api",
        summary="iwara.tv 视频接口（`/api/iwara/videos`、搜索、直链解析、代理播放等）+ `/` 导航页。",
        dirs=_site_dirs(IWARA.directory, with_assets=False),
        files=API_ROOT_FILES,
        start_commands=(
            "# Windows",
            "start.bat",
            "",
            "# 其它平台",
            IWARA.command(),
        ),
        setup_lines=BASE_SETUP + IWARA_SETUP,
        notes=(
            "入口是 `iwara/run.py`，它**只挂 iwara 的接口** —— Pixiv 的路由不在本包里"
            "（各接口项目同级，互不挂载），要用 Pixiv 接口请用 `pixiv-api` 那个包。",
            "iwara 的**视频页面**不在本包里，它是单独的扩展包 `iwara_web-extension`。",
        )
        + IWARA_NOTES
        + BASE_NOTES,
    ),
    PackageSpec(
        key="asmr-api",
        zip_name="asmr-api",
        title="asmr 接口包",
        kind="api",
        summary="asmr.one 音频接口（`/api/asmr/works`、搜索、音轨列表、封面与音频代转）+ `/` 导航页。",
        dirs=_site_dirs(ASMR.directory, with_assets=False),
        files=API_ROOT_FILES,
        start_commands=(
            "# Windows",
            "start.bat",
            "",
            "# 其它平台",
            ASMR.command(),
        ),
        setup_lines=BASE_SETUP + ASMR_SETUP,
        notes=(
            "入口是 `asmr_one/run.py`，它**只挂 asmr 的接口** —— Pixiv / iwara 的路由"
            "都不在本包里（三个接口项目同级，互不挂载）。",
            "启动后浏览器打开 `http://127.0.0.1:8765/` 是**导航页**（列出 `/docs`、"
            "`/api/health` 等入口）。",
        )
        + ASMR_NOTES
        + BASE_NOTES,
    ),
    PackageSpec(
        key="novel-extension",
        zip_name="novel-extension",
        title="novel 扩展包（小说阅读器）",
        kind="extension",
        summary="小说阅读器页面与阅读历史 / 下载 / 收藏 / 书架接口，作为**扩展**挂到 Pixiv 接口项目上。",
        dirs=("novel",),
        start_commands=(
            "# 这是一个扩展，本身不能单独启动。用法：",
            "# 1) 把 novel/ 整个目录拷到 Pixiv 接口包的项目根（与 pixiv/ 同级）",
            "# 2) 在接口项目的 .env 里加一行：",
            "PIXIV_BRIDGE_EXTENSIONS=novel",
            "# 3) 用接口项目原来的方式启动：",
            "start.bat",
            "# 或：python pixiv/run.py",
        ),
        setup_lines=(
            "先有一个能跑的 Pixiv 接口包（`pixiv-api`，或任意含 `pixiv_bridge/` 与 `pixiv/` 的项目根）。",
            "把 `novel/` 拷到那个项目根里，保持目录名不变。",
        ),
        notes=(
            "启用后阅读器页面在 `http://127.0.0.1:8765/novel`；接口项目自己的 `/` 仍是导航页，"
            "挂上扩展后它上面会多一段指向阅读器的链接。",
            "阅读器要用 Pixiv 的接口取数据，所以它是挂在 `pixiv/` 站点上的；"
            "用 `iwara/run.py` 启动时不要挂它。",
            "程序按固定文件名 `novel/extension.py` 加载扩展；目录名写错会在启动时报错并退出，"
            "不会静默降级。",
            "目录里必须保留 `.py` 文件与 `web/index.html`（阅读器页面本体）。",
        ),
    ),
    PackageSpec(
        key="iwara_web-extension",
        zip_name="iwara_web-extension",
        title="iwara_web 扩展包（iwara 视频页面）",
        kind="extension",
        summary="iwara 的视频页面（浏览 / 搜索 / 播放 / 相关视频），作为**扩展**挂到 iwara 接口项目上。",
        dirs=("iwara_web",),
        start_commands=(
            "# 这是一个扩展，本身不能单独启动。用法：",
            "# 1) 把 iwara_web/ 整个目录拷到 iwara 接口包的项目根（与 iwara/ 同级）",
            "# 2) 在 .env 里加一行：",
            "PIXIV_BRIDGE_EXTENSIONS=iwara_web",
            "# 3) 用 iwara 的入口启动：",
            "python iwara/run.py",
        ),
        setup_lines=(
            "先有 `iwara-api` 那个接口包（页面调的全是 `/api/iwara/*`，没有它页面拿不到数据）。",
            "把 `iwara_web/` 拷到那个项目根里，保持目录名不变。",
        ),
        notes=(
            "启用后视频页面在 `http://127.0.0.1:8765/iwara`。",
            "它挂在 `iwara/` 站点上：必须用 `python iwara/run.py`（或 `start.bat`）启动，"
            "用 `pixiv/run.py` 启动时那个站点上没有 iwara 接口，页面会打开但列表是空的。",
            "封面图走 `/api/iwara/image` 代转、播放走 `/api/iwara/stream`，两者都由接口项目提供。",
        ),
    ),
    PackageSpec(
        key="pixiv-novel",
        zip_name="pixiv-novel",
        title="Pixiv 接口 + novel 组合包",
        kind="bundle",
        summary="纯 Pixiv 接口 + 小说阅读器（已把 `novel` 扩展放进包里，配好一行配置即可用）。",
        dirs=_site_dirs(PIXIV.directory, "novel"),
        files=API_ROOT_FILES,
        start_commands=(
            "# 先在 .env 里加一行：PIXIV_BRIDGE_EXTENSIONS=novel",
            "",
            "# Windows",
            "start.bat",
            "",
            "# 其它平台",
            PIXIV.command(),
        ),
        setup_lines=BASE_SETUP + PIXIV_SETUP,
        notes=(
            "启动后阅读器在 `http://127.0.0.1:8765/novel`；`/` 是导航页，"
            "上面也会多出一段指向阅读器的链接。",
            "`.env` 里那一行是开关：删掉它就退回纯接口（那样 `/novel` 是 404）。",
            "入口是 `pixiv/run.py`；`/api/iwara/*` 不在本包里。",
        )
        + PIXIV_NOTES
        + BASE_NOTES,
    ),
    PackageSpec(
        key="iwara-iwara_web",
        zip_name="iwara-iwara_web",
        title="iwara 接口 + iwara_web 组合包",
        kind="bundle",
        summary="iwara 视频接口 + iwara 视频页面（`iwara_web` 已在包里）。",
        dirs=_site_dirs(IWARA.directory, "iwara_web", with_assets=False),
        files=API_ROOT_FILES,
        start_commands=(
            "# 先在 .env 里加一行：PIXIV_BRIDGE_EXTENSIONS=iwara_web",
            "",
            "# Windows",
            "start.bat",
            "",
            "# 其它平台",
            IWARA.command(),
        ),
        setup_lines=BASE_SETUP + IWARA_SETUP,
        notes=(
            "启动后视频页面在 `http://127.0.0.1:8765/iwara`，视频接口在 `/api/iwara/*`。",
            "入口是 `iwara/run.py`：本包只提供 iwara 接口（没有 Pixiv 路由）。",
        )
        + IWARA_NOTES
        + BASE_NOTES,
    ),
)

PACKAGES_BY_KEY = {spec.key: spec for spec in PACKAGES}


def validate_specs():
    """结构自检：包清单必须与站点注册表一致（同级布局 / 一个站点只配自己的扩展）。

    返回问题列表；空列表表示清单成立。这些规则就是「各接口项目同级」在打包
    这一层的落点，写错了会在 ``--check`` 里直接报出来。
    """
    problems = []
    kinds = {}
    for spec in PACKAGES:
        kinds[spec.kind] = kinds.get(spec.kind, 0) + 1
        if spec.key != spec.zip_name:
            problems.append(f"{spec.key}: key 与 zip_name 不一致")
        if FRAMEWORK_PACKAGE not in spec.dirs and spec.kind != "extension":
            problems.append(f"{spec.key}: 接口包必须带框架 {FRAMEWORK_PACKAGE}/")
        if len(spec.site_names) > 1:
            problems.append(
                f"{spec.key}: 一个包只能带一个接口项目，现在是 {spec.site_names}"
                "（各站点同级，不组合分发）"
            )
        for name in spec.extension_names:
            ext = EXTENSIONS_BY_NAME[name]
            # 扩展包本身不带站点目录；只有组合包才要求宿主站点也在包里。
            if spec.site_names and ext.host not in spec.site_names:
                problems.append(
                    f"{spec.key}: 扩展 {name} 挂在 {ext.host} 上，包里却没有 {ext.host}/"
                )
        if spec.kind == "api" and len(spec.site_names) != 1:
            problems.append(f"{spec.key}: 接口包要恰好带一个接口项目")
        if spec.kind == "extension" and spec.site_names:
            problems.append(f"{spec.key}: 扩展包不该带接口项目目录")
        if spec.kind == "bundle" and (
            len(spec.site_names) != 1 or len(spec.extension_names) != 1
        ):
            problems.append(f"{spec.key}: 组合包要恰好一个接口项目 + 一个扩展")
        for name in spec.entries():
            source = PROJECT_ROOT / name
            if not source.exists():
                problems.append(f"{spec.key}: 清单里的 {name} 在磁盘上不存在")
    if kinds.get("api") != len(SITES) or kinds.get("extension") != 2 or kinds.get("bundle") != 2:
        problems.append(
            f"包的构成变了：{kinds}（约定是 {len(SITES)} 接口（每个站点一个）"
            " + 2 扩展 + 2 组合）"
        )
    if len(PACKAGES) != len(SITES) + 4:
        problems.append(f"分享包数量是 {len(PACKAGES)}，约定是 {len(SITES) + 4} 个")
    covered = {
        name
        for spec in PACKAGES
        for name in spec.site_names + spec.extension_names
    }
    missing = ({site.directory for site in SITES} | set(EXTENSIONS_BY_NAME)) - covered
    if missing:
        problems.append(f"这些站点/扩展没有被任何分享包覆盖：{sorted(missing)}")
    return problems


def should_ignore(name: str) -> bool:
    """打包时这个条目要不要跳过。"""
    if name in IGNORED_DIRS:
        return True
    return name.endswith(IGNORED_SUFFIXES)


def _copy_tree(src: Path, dst: Path) -> int:
    dst.mkdir(parents=True, exist_ok=True)
    count = 0
    for entry in sorted(src.iterdir()):
        if should_ignore(entry.name):
            continue
        target = dst / entry.name
        if entry.is_dir():
            count += _copy_tree(entry, target)
        elif entry.is_file():
            shutil.copy2(entry, target)
            count += 1
    return count


def stage_package(spec: PackageSpec) -> Path:
    """把包的内容铺到 ``dist/.stage/<zip_name>/``，返回该目录。"""
    stage_root = DIST_DIR / ".stage"
    target = stage_root / spec.zip_name
    if target.exists():
        shutil.rmtree(target)
    target.mkdir(parents=True)

    missing = []
    for name in tuple(spec.files) + tuple(spec.extra_files):
        source = PROJECT_ROOT / name
        if not source.is_file():
            missing.append(name)
            continue
        shutil.copy2(source, target / name)
    for name in spec.dirs:
        source = PROJECT_ROOT / name
        if not source.is_dir():
            missing.append(name + "/")
            continue
        _copy_tree(source, target / name)
    if missing:
        raise SystemExit(f"分享包 {spec.key} 缺这些内容：{', '.join(missing)}")

    # 接口包里的站点目录必须带着自己的入口，否则解压后没法启动。
    for name in spec.site_names:
        entry = target / name / "run.py"
        if not entry.is_file():
            raise SystemExit(f"分享包 {spec.key} 里 {name}/run.py 没拷进来")

    docs = target / "手册"
    docs.mkdir()
    for name in DOC_FILES:
        source = PROJECT_ROOT / name
        if source.is_file():
            shutil.copy2(source, docs / name)

    (target / "README.md").write_text(render_readme(spec), encoding="utf-8")
    return target


def render_readme(spec: PackageSpec) -> str:
    """生成这个包自己的 README。"""
    lines = [
        f"# {spec.title}",
        "",
        spec.summary,
        "",
        "---",
        "",
    ]

    if spec.kind == "extension":
        lines += [
            "## 这是什么",
            "",
            "**一个扩展包，不能单独启动。** 它是一份可挂载的目录，要放进接口项目的项目根，"
            "由接口项目在启动时加载。",
            "",
        ]
    elif spec.kind == "bundle":
        lines += [
            "## 包里是什么",
            "",
            "框架（`pixiv_bridge/`）+ 一个接口项目 + 一个扩展目录，已经放在一起了。"
            "接口项目启动时会在项目根找扩展目录，配置里写名字即可。",
            "",
        ]
    else:
        lines += [
            "## 包里是什么",
            "",
            "框架（`pixiv_bridge/`）+ 一个接口项目。框架不认识任何接口项目，"
            "接口项目之间也互不依赖 —— 这里装的是其中一个。",
            "",
        ]

    if spec.start_commands:
        lines += ["## 怎么启动", "", "```", *spec.start_commands, "```", ""]

    if spec.setup_lines:
        lines += ["## 还要做什么", ""]
        lines += [f"{index}. {text}" for index, text in enumerate(spec.setup_lines, 1)]
        lines += [""]

    if spec.notes:
        lines += ["## 说明", ""]
        lines += [f"- {text}" for text in spec.notes]
        lines += [""]

    lines += [
        "## 包里的目录",
        "",
        "```",
        _tree_preview(spec),
        "```",
        "",
        "## 完整文档",
        "",
        "详细安装步骤、接口清单、直连排错都在 `手册/` 里：",
        "",
        "- `手册/INSTALL.md` —— 傻瓜版安装（Windows / 安卓 Termux），**从这一份开始**",
        "- `手册/README.md` —— 完整说明：配置项、全部接口、打包与部署",
        "- `手册/DIRECT.md` —— 连不上时怎么分层定位（DNS / IP / TLS / 应用层）",
        "",
        "## 不要拷的东西",
        "",
        "本包里不会有 `.env`、`tokens.json`、`data/`（都是你的私密数据）；"
        "换机时那些自己带，接口与页面拷这个包就行。",
        "",
    ]
    return "\n".join(lines)


def _tree_preview(spec: PackageSpec) -> str:
    root = spec.zip_name
    names = [name for name in tuple(spec.files) + tuple(spec.extra_files)]
    dirs = [name + "/" for name in spec.dirs]
    entries = names + dirs + ["手册/", "README.md"]
    return "\n".join([root + "/", *[f"├── {name}" for name in entries]])


def write_zip(spec: PackageSpec, staged: Path) -> Path:
    DIST_DIR.mkdir(parents=True, exist_ok=True)
    target = DIST_DIR / f"{spec.zip_name}.zip"
    if target.exists():
        target.unlink()
    with zipfile.ZipFile(target, "w", zipfile.ZIP_DEFLATED) as archive:
        for path in sorted(staged.rglob("*")):
            if path.is_dir():
                continue
            relative = path.relative_to(staged.parent)
            archive.write(path, relative.as_posix())
    return target


def inspect_zip(target: Path) -> tuple:
    """返回 ``(文件数, 解压后字节数, 问题列表)``。"""
    problems = []
    total = 0
    with zipfile.ZipFile(target) as archive:
        names = archive.namelist()
        for info in archive.infolist():
            total += info.file_size
        for name in names:
            parts = Path(name).parts
            if any(part in IGNORED_DIRS for part in parts):
                problems.append(f"带了不该带的内容：{name}")
            if name.endswith(".pyc"):
                problems.append(f"带了字节码：{name}")
            if Path(name).name in (".env", "tokens.json", "history.jsonl"):
                problems.append(f"带了私密数据：{name}")
        if not any(name.endswith("README.md") for name in names):
            problems.append("根目录没有 README.md")
        if not any(name.startswith(f"{target.stem}/") for name in names):
            problems.append("没有一个同名根文件夹")
    return len(names), total, problems


def _kind_counts() -> str:
    """把包清单按 kind 归纳成一句话，供 ``--help`` 使用（不再写死数量）。"""
    labels = {"api": "接口包", "extension": "扩展包", "bundle": "接口+扩展"}
    counts: dict[str, int] = {}
    for spec in PACKAGES:
        counts[spec.kind] = counts.get(spec.kind, 0) + 1
    parts = [f"{labels.get(kind, kind)} {counts[kind]} 个" for kind in labels if kind in counts]
    return "、".join(parts) or "无"


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        prog="make_share_packages",
        description=f"把项目打成 {len(PACKAGES)} 个分享包（{_kind_counts()}）。",
    )
    parser.add_argument(
        "keys",
        nargs="*",
        help=f"只做指定的包（默认全部）。可选：{', '.join(PACKAGES_BY_KEY)}",
    )
    parser.add_argument("--check", action="store_true", help="只检查内容与结构，不写 zip")
    parser.add_argument("--list", action="store_true", help="列出所有包就退出")
    args = parser.parse_args(argv)

    if args.list:
        for spec in PACKAGES:
            print(f"{spec.key:<24} {spec.zip_name + '.zip':<28} {spec.summary}")
        return 0

    problems = validate_specs()
    if problems:
        for problem in problems:
            print(f"[FAIL] {problem}", file=sys.stderr)
        print(f"分享包清单不成立：{len(problems)} 项", file=sys.stderr)
        return 1

    unknown = [key for key in args.keys if key not in PACKAGES_BY_KEY]
    if unknown:
        print(f"没有这些包：{', '.join(unknown)}", file=sys.stderr)
        print(f"可选：{', '.join(PACKAGES_BY_KEY)}", file=sys.stderr)
        return 2
    selected = [PACKAGES_BY_KEY[key] for key in args.keys] if args.keys else list(PACKAGES)

    failures = 0
    for spec in selected:
        staged = stage_package(spec)
        file_count = sum(1 for path in staged.rglob("*") if path.is_file())
        if args.check:
            print(f"[检查] {spec.key:<24} 内容就绪：{file_count} 个文件")
        else:
            target = write_zip(spec, staged)
            count, size, problems = inspect_zip(target)
            print(
                f"[打包] {target.name:<28} {count:>5} 个文件 "
                f"{size / 1024 / 1024:>6.2f} MB 压缩后 {target.stat().st_size / 1024 / 1024:.2f} MB"
            )
            for problem in problems:
                print(f"       ! {problem}")
                failures += 1

    if not args.check:
        stage_root = DIST_DIR / ".stage"
        if stage_root.exists():
            shutil.rmtree(stage_root, ignore_errors=True)

    print("内容检查未通过" if failures else "分享包处理完成")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
