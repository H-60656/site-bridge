#!/usr/bin/env python3
"""生成分享包：把项目按「接口包 / 扩展包 / 接口+扩展组合包」打成 6 个 zip。

用法::

    python scripts/make_share_packages.py            # 生成全部 6 个包到 dist/
    python scripts/make_share_packages.py --check     # 只做内容检查，不写 zip
    python scripts/make_share_packages.py pixiv_bridge-api novel-extension

每个包解压后是一个同名文件夹，根目录里有一份**该包自己的** ``README.md``
（说明这东西是什么、怎么启动、包里有什么），完整文档放在 ``手册/`` 里。

约定：扩展包（``novel`` / ``iwara_web``）只是可挂载的目录，不能单独启动 ——
它们要放进一个接口项目的**项目根**，再在 ``.env`` 里写扩展名。
"""

from __future__ import annotations

import argparse
import shutil
import sys
import zipfile
from dataclasses import dataclass, field
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DIST_DIR = PROJECT_ROOT / "dist"

# 共享的根目录文件：接口项目的启动与配置那一套。
API_ROOT_FILES = (
    "run.py",
    "requirements.txt",
    "requirements-termux.txt",
    "start.bat",
    "start.sh",
    "get_token.py",
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


COMMON_SETUP = (
    "安装 Python **3.10+**（这是唯一必须自己准备的东西；其余依赖首次启动会自动下载）。",
    "把 `.env.example` 复制成 `.env`，填上自己的 `PIXIV_REFRESH_TOKEN`"
    "（怎么拿见 `手册/INSTALL.md`，或跑一次 `python get_token.py`）。",
)

COMMON_NOTES = (
    "第一次启动会打印一份 `[环境检查]`，其中 `deps` 一项会自动装依赖，慢一点属正常。",
    "个人数据都在 `data/` 里（登录凭据 / 阅读历史 / 下载记录 / 收藏台账 / 页面配置）；"
    "换机只拷这一个目录。",
    "连不上 Pixiv 时先看 `手册/DIRECT.md`（直连思路：怎么分层定位是 DNS、IP、TLS 还是应用层的问题）。",
    "`openssl-libs/` 是项目自带的 OpenSSL 4.x 动态库（Windows 与安卓两套），"
    "ECH 模式要用；不用 ECH 的话删掉它也不影响启动。",
)


PACKAGES = (
    PackageSpec(
        key="pixiv_bridge-api",
        zip_name="pixiv_bridge-api",
        title="pixiv_bridge 接口包",
        kind="api",
        summary="纯 Pixiv 接口服务：认证 / 插画 / 搜索 / 动态 / 用户 / 小说接口 / 图片代理 + `/` 导航页。",
        dirs=("pixiv_bridge", "openssl-libs", "examples"),
        files=API_ROOT_FILES,
        start_commands=(
            "# Windows",
            "start.bat",
            "",
            "# 其它平台",
            "python run.py",
        ),
        setup_lines=COMMON_SETUP,
        notes=(
            "启动后浏览器打开 `http://127.0.0.1:8765/` 是**导航页**（列出 `/docs`、"
            "`/api/health` 等入口）。",
        )
        + COMMON_NOTES,
    ),
    PackageSpec(
        key="iwara-api",
        zip_name="iwara-api",
        title="iwara 接口包",
        kind="api",
        summary="Pixiv 接口 + iwara.tv 视频接口（`/api/iwara/*`）。",
        dirs=("pixiv_bridge", "iwara", "openssl-libs", "examples"),
        files=API_ROOT_FILES,
        start_commands=(
            "# Windows",
            "python iwara\\run.py",
            "",
            "# 其它平台",
            "python iwara/run.py",
        ),
        setup_lines=COMMON_SETUP
        + (
            "iwara 的登录是可选的（公开内容含 R18 匿名就能看）：想登录订阅内容再跑一次 "
            "`python iwara/get_token.py`。",
        ),
        notes=(
            "入口是 `iwara/run.py`，它在接口之上多挂 iwara 的接口；`run.py` 那版入口"
            "不认识 iwara，`/api/iwara/*` 会 404。",
            "会多装一个 `curl_cffi`（Cloudflare 只认浏览器 TLS 指纹），比主依赖慢一点。",
            "iwara 的**视频页面**不在本包里，它是单独的扩展包 `iwara_web-extension`。",
        )
        + COMMON_NOTES,
    ),
    PackageSpec(
        key="novel-extension",
        zip_name="novel-extension",
        title="novel 扩展包（小说阅读器）",
        kind="extension",
        summary="小说阅读器页面与阅读历史 / 下载 / 收藏 / 书架接口，作为**扩展**挂到接口项目上。",
        dirs=("novel",),
        start_commands=(
            "# 这是一个扩展，本身不能单独启动。用法：",
            "# 1) 把 novel/ 整个目录拷到接口项目的项目根（与 run.py 同级）",
            "# 2) 在接口项目的 .env 里加一行：",
            "PIXIV_BRIDGE_EXTENSIONS=novel",
            "# 3) 用接口项目原来的方式启动（start.bat / sh start.sh / python run.py）",
        ),
        setup_lines=(
            "先有一个能跑的接口包（`pixiv_bridge-api` 或任意含 `pixiv_bridge/` 的项目根）。",
            "把 `novel/` 拷到那个项目根里，保持目录名不变。",
        ),
        notes=(
            "启用后阅读器页面在 `http://127.0.0.1:8765/novel`；接口项目自己的 `/` 仍是导航页，"
            "挂上扩展后它上面会多一段指向阅读器的链接。",
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
            "# 1) 把 iwara_web/ 整个目录拷到 iwara 接口项目的项目根（与 run.py 同级）",
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
            "它必须配合 `iwara/run.py` 入口：用 `run.py` 启动时 iwara 的接口本身就不在，"
            "页面会打开但列表是空的。",
            "封面图走 `/api/iwara/image` 代转、播放走 `/api/iwara/stream`，两者都由接口项目提供。",
        ),
    ),
    PackageSpec(
        key="pixiv_bridge-novel",
        zip_name="pixiv_bridge-novel",
        title="pixiv_bridge + novel 组合包",
        kind="bundle",
        summary="纯 Pixiv 接口 + 小说阅读器（已把 `novel` 扩展放进包里，配好一行配置即可用）。",
        dirs=("pixiv_bridge", "novel", "openssl-libs", "examples"),
        files=API_ROOT_FILES,
        start_commands=(
            "# 先在 .env 里加一行：PIXIV_BRIDGE_EXTENSIONS=novel",
            "",
            "# Windows",
            "start.bat",
            "",
            "# 其它平台",
            "python run.py",
        ),
        setup_lines=COMMON_SETUP,
        notes=(
            "启动后阅读器在 `http://127.0.0.1:8765/novel`；`/` 是导航页，"
            "上面也会多出一段指向阅读器的链接。",
            "`.env` 里那一行是开关：删掉它就退回纯接口（那样 `/novel` 是 404）。",
        )
        + COMMON_NOTES,
    ),
    PackageSpec(
        key="iwara-iwara_web",
        zip_name="iwara-iwara_web",
        title="iwara + iwara_web 组合包",
        kind="bundle",
        summary="Pixiv 接口 + iwara 视频接口 + iwara 视频页面（`iwara_web` 已在包里）。",
        dirs=("pixiv_bridge", "iwara", "iwara_web", "openssl-libs", "examples"),
        files=API_ROOT_FILES,
        start_commands=(
            "# 先在 .env 里加一行：PIXIV_BRIDGE_EXTENSIONS=iwara_web",
            "",
            "# Windows",
            "python iwara\\run.py",
            "",
            "# 其它平台",
            "python iwara/run.py",
        ),
        setup_lines=COMMON_SETUP
        + (
            "iwara 的登录是可选的：公开内容（含 R18）匿名就能看，登录只为订阅内容与私密视频 —— "
            "`python iwara/get_token.py`。",
        ),
        notes=(
            "启动后视频页面在 `http://127.0.0.1:8765/iwara`，视频接口在 `/api/iwara/*`。",
            "必须用 `iwara/run.py` 这个入口：`run.py` 不挂 iwara，页面会打开但拿不到数据。",
            "会多装一个 `curl_cffi`（Cloudflare 只认浏览器 TLS 指纹），比主依赖慢一点。",
        )
        + COMMON_NOTES,
    ),
)

PACKAGES_BY_KEY = {spec.key: spec for spec in PACKAGES}


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
            "接口项目（`pixiv_bridge/`）+ 一个扩展目录，已经放在一起了。"
            "接口项目启动时会在项目根找扩展目录，配置里写名字即可。",
            "",
        ]
    else:
        lines += [
            "## 包里是什么",
            "",
            "接口项目（`pixiv_bridge/`）本体。它不依赖任何子项目，单独就能跑。",
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


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        prog="make_share_packages",
        description="把项目打成 6 个分享包（接口包两个、扩展包两个、接口+扩展两个）。",
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
