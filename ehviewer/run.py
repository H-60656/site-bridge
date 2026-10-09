"""ehviewer 接口项目的入口：先补齐依赖（含 curl_cffi），再把 ``SITE`` 交给框架基座。

用法（两个等价入口）：

    python ehviewer/run.py [--host ...] [--port ...] [--check] [--env ...]
    python -m pixiv_bridge ehviewer [同上]

与 ``asmr_one/run.py`` 同构，入口只做三件事：把项目根放进 ``sys.path``、补齐
依赖、把站点的 ``SITE`` 交给 ``pixiv_bridge.cli.main``。

自举那一段必须排在 ``from ehviewer.app import SITE`` **之前** —— 那条链会一路
import 到 ``ehviewer.client``，而它开头就是 ``import curl_cffi``，包没装好时
会让自举根本没机会跑。
"""

import sys
from functools import partial
from pathlib import Path

HERE = Path(__file__).resolve().parent

# 直接 `python ehviewer/run.py` 时 sys.path[0] 是 ehviewer/，框架包在它的上一级。
sys.path.insert(0, str(HERE.parent))

from pixiv_bridge.deps import bootstrap, ensure_packages  # noqa: E402

# ehviewer 额外需要的顶层模块 → 装在哪个清单里。curl_cffi 是编译包，
# 不放进框架的 REQUIRED，免得只跑 pixiv 的人也被要求装。
EH_EXTRA_MODULES = ("curl_cffi",)
EH_REQUIREMENTS = HERE / "requirements.txt"

bootstrap(partial(ensure_packages, EH_EXTRA_MODULES, EH_REQUIREMENTS))

from ehviewer.app import SITE  # noqa: E402  （依赖补齐后才能安全 import）
from pixiv_bridge.cli import main  # noqa: E402

if __name__ == "__main__":
    main(site=SITE)
