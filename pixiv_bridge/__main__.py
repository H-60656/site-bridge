"""``python -m pixiv_bridge <站点> [入口参数...]``：按名字找到站点入口并执行。

框架不认识任何站点：这里只做「在项目根目录下找 ``<name>/run.py``」的目录发现，
真正的启动逻辑在各站点的入口脚本里（它自己 bootstrap 站点额外依赖）。
"""

import runpy
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def site_names():
    """项目根下所有带 ``run.py`` 的目录名（接口项目 + 附属项目）。"""
    try:
        children = sorted(ROOT.iterdir())
    except OSError:  # pragma: no cover - 项目根不可读时没什么可提示的
        return []
    return [path.name for path in children if (path / "run.py").is_file()]


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    if not argv or argv[0] in ("-h", "--help"):
        print("用法: python -m pixiv_bridge <站点> [入口参数...]")
        print(f"可选站点: {', '.join(site_names()) or '（没有找到任何站点目录）'}")
        return 0 if argv else 2
    name, rest = argv[0], argv[1:]
    entry = ROOT / name / "run.py"
    if not entry.is_file():
        print(
            f"没有这个接口项目: {name}（可选：{', '.join(site_names())}）",
            file=sys.stderr,
        )
        return 2
    sys.argv = [str(entry), *rest]
    runpy.run_path(str(entry), run_name="__main__")
    return 0


if __name__ == "__main__":
    sys.exit(main())
