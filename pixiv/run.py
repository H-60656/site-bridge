"""Pixiv 接口项目的入口：先补齐依赖，再把 ``SITE`` 交给框架的命令行基座。

用法（两个等价入口）：

    python pixiv/run.py [--host ...] [--port ...] [--check] [--env ...]
    python -m site_bridge pixiv [同上]

它和 ``iwara/run.py`` 是同构的：入口只做三件事 —— 把项目根放进 ``sys.path``、
补齐框架依赖、把站点的 ``SITE`` 交给 ``site_bridge.cli.main``。
"""

import sys
from pathlib import Path

# 直接 `python pixiv/run.py` 时 sys.path[0] 是 pixiv/，框架包在它的上一级。
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from site_bridge.deps import bootstrap  # noqa: E402

bootstrap()

from pixiv.app import SITE  # noqa: E402  （依赖补齐后才能安全 import）
from site_bridge.cli import main  # noqa: E402

if __name__ == "__main__":
    main(site=SITE)
