"""小说阅读子项目的启动入口。

包了一层主项目的命令行（同样的 --host/--port/--reload/--check/--env），
区别只在最后建应用那一步：挂上本子项目的阅读器路由。

    python novel/run.py            # 接口 + 阅读器（默认 127.0.0.1:8765）
    python novel/run.py --check    # 只校验登录状态
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from pixiv_bridge.cli import main as _bridge_main  # noqa: E402
from pixiv_bridge.deps import ensure_dependencies  # noqa: E402
from pixiv_bridge.server import create_app as _create_bridge_app  # noqa: E402

from novel.app import NOVEL_ROUTERS, novel_state_factory  # noqa: E402


def main(argv=None):
    _bridge_main(
        argv,
        app_factory=lambda settings: _create_bridge_app(
            settings,
            state_factory=novel_state_factory,
            extra_routers=NOVEL_ROUTERS,
        ),
    )


if __name__ == "__main__":
    try:
        ensure_dependencies()
    except RuntimeError as exc:
        print(f"[deps] {exc}", file=sys.stderr)
        sys.exit(1)
    main()
