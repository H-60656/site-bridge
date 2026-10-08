#!/usr/bin/env python3
"""核对整理是否成立：主项目不依赖附属项目、附属项目单向依赖主项目、出入口清爽。

用法：python scripts/check_boundary.py

检查四件事：
1. pixiv_bridge/ 里没有任何模块 import 附属项目（依赖不能倒过来）；
2. 各附属项目只用主项目 pixiv_bridge，不使用本仓库其它本地包；
3. 各附属项目都能建出应用，且各自比纯接口多出预期的那几条路由 ——
   `novel` 走的是**扩展机制**（配置驱动的真实路径，即 `extension.py`），
   `iwara` 走的是自己独立接口项目的工厂；
4. 出入口清爽：两个接口项目各留一个 `run.py`，阅读项目不再有专属入口，
   改为固定文件名 `novel/extension.py`。
"""

import ast
import subprocess
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent

# 本仓库顶层可见的包/模块名，其余顶层 import 一律当作第三方库放过。
LOCAL_NAMES = {
    p.stem for p in PROJECT_ROOT.glob("*.py")
} | {
    p.name for p in PROJECT_ROOT.iterdir()
    if p.is_dir() and (p / "__init__.py").exists()
}
MAIN_PACKAGE = "pixiv_bridge"

# 附属项目：主项目永远不认识它们，由运行方式决定怎么挂上来。
SUB_PACKAGES = ("iwara", "novel")

# 扩展机制约定的固定入口文件名（与 pixiv_bridge/extensions.py 保持一致）。
EXTENSION_FILENAME = "extension.py"

# 出入口：两个接口项目各留一个入口脚本；阅读项目改由配置启用，不该再有入口。
ENTRY_POINTS = ("run.py", "iwara/run.py")
REMOVED_ENTRY_POINTS = ("novel/run.py",)

# 各附属项目相对「纯接口」多出来的路由（同一路径的 GET/POST 各算一条）。
EXPECTED_EXTRA = {
    "iwara": {
        "/api/iwara/auth",
        "/api/iwara/image",
        "/api/iwara/me",
        "/api/iwara/resolve/{video_id}",
        "/api/iwara/search",
        "/api/iwara/stream",
        "/api/iwara/thumbnail/{video_id}",
        "/api/iwara/video/{video_id}",
        "/api/iwara/videos",
    },
    "novel": {
        "/api/bookmarks",
        "/api/bookmarks/delete",
        "/api/config",
        "/api/downloads",
        "/api/downloads/clear",
        "/api/history",
        "/api/library",
        "/api/library/file",
        "/api/library/preview",
        # 阅读器页面：以前在 `/`，加了导航页之后让位到 `/novel`。
        "/novel",
    },
}


def iter_py(root):
    for path in sorted((PROJECT_ROOT / root).rglob("*.py")):
        yield path


def imports_of(path):
    """返回该文件所有绝对 import 的顶层模块名（相对导入不参与跨项目判断）。"""
    names = set()
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                names.add(alias.name.split(".")[0])
        elif isinstance(node, ast.ImportFrom) and not node.level:
            names.add((node.module or "").split(".")[0])
    return names


def check_imports(errors):
    for sub in SUB_PACKAGES:
        bad = [p for p in iter_py(MAIN_PACKAGE) if sub in imports_of(p)]
        if bad:
            errors.append(
                f"主项目不得 import 子项目 {sub}，违规文件："
                + ", ".join(str(p.relative_to(PROJECT_ROOT)) for p in bad)
            )

        allowed = {MAIN_PACKAGE, sub}
        for path in iter_py(sub):
            for name in imports_of(path) & LOCAL_NAMES - allowed:
                errors.append(
                    f"子项目 {sub} 不应依赖本地包 {name}：{path.relative_to(PROJECT_ROOT)}"
                )


def check_layout(errors):
    """出入口：接口项目各留一个入口，阅读项目改由扩展机制启用。"""
    for rel in ENTRY_POINTS:
        if not (PROJECT_ROOT / rel).is_file():
            errors.append(f"缺少接口项目入口：{rel}")

    for rel in REMOVED_ENTRY_POINTS:
        if (PROJECT_ROOT / rel).exists():
            errors.append(
                f"附属项目不应再有专属入口 {rel} —— 它现在作为扩展启用："
                f"在配置里写 PIXIV_BRIDGE_EXTENSIONS=novel"
            )

    entry = PROJECT_ROOT / "novel" / EXTENSION_FILENAME
    if not entry.is_file():
        errors.append(f"阅读项目缺少扩展入口：novel/{EXTENSION_FILENAME}")


# 路由取法要兼容两种 FastAPI 形态：0.14x 起 include_router() 不再把子路由摊平进
# app.routes，而是塞一个 _IncludedRouter 包装对象（没有 .path / .methods，真正的路由
# 挂在 .original_router.routes 上）；旧版本则本来就是摊平的。只认 .path 会在新版本上
# 直接把整棵子路由当空气，差集算出来永远是空集。
#
# 注意：纯接口那一侧显式用 `create_app()` 的默认参数（extensions=()），不读配置 ——
# create_app 本身不按配置加载扩展，所以这里的结果不随 .env 变化。
ROUTE_PROBE = (
    "import sys;"
    f"sys.path.insert(0, r'{PROJECT_ROOT}');"
    "from pixiv_bridge.server import create_app\n"
    "from pixiv_bridge.config import Settings\n"
    "from pixiv_bridge.extensions import load_extension_specs\n"
    "def key(app):\n"
    "    out = set()\n"
    "    for r in app.routes:\n"
    "        holder = getattr(r, 'original_router', None)\n"
    "        items = holder.routes if holder is not None else [r]\n"
    "        for item in items:\n"
    "            path = getattr(item, 'path', None)\n"
    "            if path is not None:\n"
    "                out.add((path, frozenset(getattr(item, 'methods', ()) or ())))\n"
    "    return out\n"
    "pure = key(create_app());\n"
    "print(len(pure));\n"
    # iwara：自己独立的接口项目，用自己 app.py 的工厂。
    "from iwara.app import create_app as create_iwara_app\n"
    "iwara = key(create_iwara_app());\n"
    "print(len(iwara));\n"
    "print(','.join(sorted(p for p, _ in iwara - pure)));\n"
    # novel：走扩展机制（用户实际走的路），顺带验证 extension.py 存在且导出了路由。
    f"settings = Settings.load(r'{PROJECT_ROOT}');\n"
    f"extensions = load_extension_specs(['novel'], r'{PROJECT_ROOT}');\n"
    "novel = key(create_app(settings, extensions=extensions));\n"
    "print(len(novel));\n"
    "print(','.join(sorted(p for p, _ in novel - pure)));\n"
)


def check_apps(errors):
    out = subprocess.run(
        [sys.executable, "-c", ROUTE_PROBE], capture_output=True, text=True
    )
    if out.returncode != 0:
        last = (out.stderr.strip().splitlines() or [""])[-1]
        hint = ""
        # iwara 的 client 在模块顶层 import curl_cffi（Cloudflare 要求浏览器
        # TLS 指纹），而它只写在 iwara/requirements.txt 里。新克隆的仓库若还
        # 没跑过 iwara/run.py，这里会缺包——点明怎么补，别让人以为路由写错了。
        if "curl_cffi" in last:
            hint = "；先装 iwara 的额外依赖：pip install -r iwara/requirements.txt"
        errors.append(f"各入口无法同时导入：{last}{hint}")
        return
    lines = out.stdout.splitlines()
    pure_count = int(lines[0])
    print(f"纯接口 {pure_count} 条路由条目（同一路径的 GET/POST 各算一条）")
    for index, sub in enumerate(SUB_PACKAGES):
        sub_count = int(lines[1 + index * 2])
        extra_paths = set(filter(None, lines[2 + index * 2].split(",")))
        label = f"{sub}（扩展）" if sub == "novel" else sub
        print(f"  {label}: {sub_count} 条路由条目，比纯接口多 {len(extra_paths)} 条")
        expected = EXPECTED_EXTRA[sub]
        if extra_paths != expected:
            errors.append(
                f"{sub} 路由与预期不符：多出 {sorted(extra_paths - expected)}，"
                f"缺少 {sorted(expected - extra_paths)}"
            )


def main():
    errors = []
    check_imports(errors)
    check_layout(errors)
    check_apps(errors)
    for line in errors:
        print(f"[FAIL] {line}")
    print("边界检查通过" if not errors else f"边界检查失败：{len(errors)} 项")
    return 1 if errors else 0


if __name__ == "__main__":
    sys.exit(main())
