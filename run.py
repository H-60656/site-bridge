"""接口项目的命令行入口：先补齐依赖，再交给 ``pixiv_bridge.cli.main``。

与 ``pixiv_bridge/__main__.py`` 等价：``python run.py`` 与
``python -m pixiv_bridge`` 都到这里。
"""

import sys

from pixiv_bridge.deps import ensure_dependencies

try:
    ensure_dependencies()
except RuntimeError as exc:
    print(f"[deps] {exc}", file=sys.stderr)
    sys.exit(1)

from pixiv_bridge.cli import main

if __name__ == "__main__":
    main()
