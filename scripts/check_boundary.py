#!/usr/bin/env python3
"""核对拆分是否成立：主项目不依赖子项目、子项目单向依赖主项目。

用法：python scripts/check_boundary.py

检查三件事：
1. pixiv_bridge/ 里没有任何模块 import novel（依赖不能倒过来）；
2. novel/ 只用主项目 pixiv_bridge，不使用本仓库其它本地包；
3. 两个入口都能建出应用，且 novel 比纯接口多出预期的那几条路由。
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
SUB_PACKAGE = "novel"


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
    bad = [p for p in iter_py(MAIN_PACKAGE) if SUB_PACKAGE in imports_of(p)]
    if bad:
        errors.append(
            f"主项目不得 import 子项目 {SUB_PACKAGE}，违规文件："
            + ", ".join(str(p.relative_to(PROJECT_ROOT)) for p in bad)
        )

    allowed = {MAIN_PACKAGE, SUB_PACKAGE}
    for path in iter_py(SUB_PACKAGE):
        for name in imports_of(path) & LOCAL_NAMES - allowed:
            errors.append(
                f"子项目不应依赖本地包 {name}：{path.relative_to(PROJECT_ROOT)}"
            )


ROUTE_PROBE = (
    "import sys;"
    f"sys.path.insert(0, r'{PROJECT_ROOT}');"
    "from pixiv_bridge.server import create_app;"
    "from novel.app import create_app as create_novel_app;"
    "key = lambda app: {(r.path, frozenset(getattr(r, 'methods', ()) or ()))"
    "                   for r in app.routes};"
    "pure, full = key(create_app()), key(create_novel_app());"
    "print(len(pure), len(full));"
    "print(','.join(sorted(p for p, _ in full - pure)))"
)

EXPECTED_EXTRA = {
    "/",
    "/api/bookmarks",
    "/api/bookmarks/delete",
    "/api/config",
    "/api/downloads",
    "/api/downloads/clear",
    "/api/history",
    "/api/library",
    "/api/library/file",
    "/api/library/preview",
}


def check_apps(errors):
    out = subprocess.run(
        [sys.executable, "-c", ROUTE_PROBE], capture_output=True, text=True
    )
    if out.returncode != 0:
        errors.append(
            f"两个入口无法同时导入：{(out.stderr.strip().splitlines() or [''])[-1]}"
        )
        return
    counts, extra = out.stdout.splitlines()
    pure_count, full_count = (int(x) for x in counts.split())
    extra_paths = set(filter(None, extra.split(",")))
    print(
        f"纯接口 {pure_count} 条路由条目 / novel 应用 {full_count} 条"
        "（同一路径的 GET/POST 各算一条）"
    )
    if extra_paths != EXPECTED_EXTRA:
        errors.append(
            f"子项目路由与预期不符：多出 {sorted(extra_paths - EXPECTED_EXTRA)}，"
            f"缺少 {sorted(EXPECTED_EXTRA - extra_paths)}"
        )


def main():
    errors = []
    check_imports(errors)
    check_apps(errors)
    for line in errors:
        print(f"[FAIL] {line}")
    print("边界检查通过" if not errors else f"边界检查失败：{len(errors)} 项")
    return 1 if errors else 0


if __name__ == "__main__":
    sys.exit(main())
