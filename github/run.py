"""github 站点的入口脚本：``python github/run.py``。

与 pixiv / iwara / asmr_one / ehviewer 的 ``run.py`` 同构（三段式）：

1. 把项目根放进 ``sys.path``，否则 ``pixiv_bridge`` 找不到；
2. 先跑 ``bootstrap`` 装依赖 —— 必须在 import 站点模块**之前**，因为
   ``github/app.py`` 会拖进 ``httpx``；
3. 最后才 import ``SITE`` 并交给框架的 ``main``。

这个站点没有额外依赖（``httpx`` 在根 ``requirements.txt`` 里），所以
``bootstrap`` 不带 ``install=`` 回调。
"""

import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))

from pixiv_bridge.deps import bootstrap  # noqa: E402

bootstrap()

from github.app import SITE  # noqa: E402
from pixiv_bridge.cli import main  # noqa: E402

if __name__ == "__main__":
    main(site=SITE)
