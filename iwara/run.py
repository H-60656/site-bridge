"""iwara 子项目的启动入口。

包了一层主项目的命令行（同样的 --host/--port/--reload/--check/--env），
区别只在最后建应用那一步：多挂上 iwara 的视频路由。

    python iwara/run.py            # 接口 + iwara（默认 127.0.0.1:8765）
    python iwara/run.py --check    # 启动前自检（依赖 / 端口 / 网络）
    python iwara/get_token.py      # 可选：登录一次，解锁订阅内容
"""

import sys
from functools import partial
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from pixiv_bridge.cli import main as _bridge_main  # noqa: E402
from pixiv_bridge.deps import bootstrap, ensure_packages  # noqa: E402

from iwara.app import create_app  # noqa: E402

# iwara 额外需要的顶层模块 → 装在哪个清单里。curl_cffi 是编译包，
# 不放进主项目 REQUIRED，免得只跑 pixiv 的人也被要求装。
IWARA_EXTRA_MODULES = ("curl_cffi",)
IWARA_REQUIREMENTS = Path(__file__).resolve().parent / "requirements.txt"

# 自举复用主项目那一段（同样的 [deps] 报错与退出码），只是安装函数换成
# 子项目自己的清单。跟主项目入口一样放在模块级，进主程序前先补齐依赖。
bootstrap(partial(ensure_packages, IWARA_EXTRA_MODULES, IWARA_REQUIREMENTS))


def main(argv=None):
    _bridge_main(argv, app_factory=create_app)


if __name__ == "__main__":
    main()
