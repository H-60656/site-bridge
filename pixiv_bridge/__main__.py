"""``python -m pixiv_bridge`` 的入口：先补齐依赖，再交给命令行主函数。
"""

import sys

from .deps import ensure_dependencies

try:
    ensure_dependencies()
except RuntimeError as exc:
    print(f"[deps] {exc}", file=sys.stderr)
    sys.exit(1)

from .cli import main

if __name__ == "__main__":
    main()
