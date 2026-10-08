"""接口项目的命令行入口：先补齐依赖，再交给 ``pixiv_bridge.cli.main``。

与 ``pixiv_bridge/__main__.py`` 等价：``python run.py`` 与
``python -m pixiv_bridge`` 都到这里。两边的自举都走 ``deps.bootstrap()``，
所以缺依赖时只会在 stderr 打一行 ``[deps] ...`` 然后退出，不会甩出堆栈。
"""

from pixiv_bridge.deps import bootstrap

bootstrap()

from pixiv_bridge.cli import main  # noqa: E402  （依赖补齐后才能安全 import）

if __name__ == "__main__":
    main()
