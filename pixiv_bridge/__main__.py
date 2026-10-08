"""``python -m pixiv_bridge`` 的入口：先补齐依赖，再交给命令行主函数。

与仓库根的 ``run.py`` 等价，自举同样走 ``deps.bootstrap()``。
"""

from .deps import bootstrap

bootstrap()

from .cli import main  # noqa: E402  （依赖补齐后才能安全 import）

if __name__ == "__main__":
    main()
