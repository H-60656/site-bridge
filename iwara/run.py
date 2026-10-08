"""iwara 子项目的启动入口。

包了一层主项目的命令行（同样的 --host/--port/--reload/--check/--env），
区别有三处：多挂上 iwara 的视频接口路由、按配置挂扩展（例如页面扩展
`iwara_web`）、以及 --check 时多查 iwara 自己那几项
（curl_cffi / api.iwara.tv / iwara 登录态）。

    python iwara/run.py            # 接口 + iwara（默认 127.0.0.1:8765）
    python iwara/run.py --check    # 启动前自检（公共五项 + iwara 三项）
    python iwara/get_token.py      # 可选：登录一次，解锁订阅内容

想让这个入口同时带上视频页面，在 `.env` 里写：

    PIXIV_BRIDGE_EXTENSIONS=iwara_web
"""

import sys
from functools import partial
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from pixiv_bridge.cli import load_extensions_or_exit  # noqa: E402
from pixiv_bridge.cli import main as _bridge_main  # noqa: E402
from pixiv_bridge.deps import bootstrap, ensure_packages  # noqa: E402

# iwara 额外需要的顶层模块 → 装在哪个清单里。curl_cffi 是编译包，
# 不放进主项目 REQUIRED，免得只跑 pixiv 的人也被要求装。
IWARA_EXTRA_MODULES = ("curl_cffi",)
IWARA_REQUIREMENTS = Path(__file__).resolve().parent / "requirements.txt"

# 自举复用主项目那一段（同样的 [deps] 报错与退出码），只是安装函数换成
# 子项目自己的清单。必须排在下面 `from iwara.app import ...` 之前 ——
# 那条链会一路 import 到 iwara.client，而它开头就是 `import curl_cffi`，
# 包没装好时会让自举根本没机会跑。
bootstrap(partial(ensure_packages, IWARA_EXTRA_MODULES, IWARA_REQUIREMENTS))

from iwara.app import create_app  # noqa: E402  （依赖补齐后才能安全 import）
from iwara.envcheck import extra_checks  # noqa: E402


def app_factory(settings):
    """按配置挂扩展后组装应用。

    `iwara/run.py` 是独立接口项目，不是主项目的扩展，所以主项目那条
    「默认入口按配置挂扩展」的路走不到这里 —— 得自己加载（同一个
    `load_extensions_or_exit`，报错格式与退出码也一致）。典型用法是挂
    `iwara_web` 拿到人看的视频页面；不配扩展就是纯 iwara 接口。
    """
    extensions = load_extensions_or_exit(settings)
    return create_app(settings, extensions=extensions)


def main(argv=None):
    _bridge_main(argv, app_factory=app_factory, extra_checks=extra_checks)


if __name__ == "__main__":
    main()
