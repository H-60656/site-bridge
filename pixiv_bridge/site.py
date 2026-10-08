"""站点描述符：命令行入口只认识这一份契约，不认识任何具体接口项目。

新增一个接口项目 = 写一个目录，里面放两个文件：

- ``<site>/app.py``：导出 ``SITE``（本模块的 :class:`Site` 实例），把站点的
  ``create_app``、专属自检、登录校验、网络提示、错误类型声明清楚；
- ``<site>/run.py``：二十行的同构入口 —— 先 bootstrap（框架依赖 + 站点额外依赖），
  再把 ``SITE`` 交给 ``pixiv_bridge.cli.main``。

框架层不 import 任何站点模块（``pixiv_bridge/`` 里出现站点包名即架构违规，
``scripts/check_boundary.py`` 会拒绝），站点通过 ``SITE`` 反向挂到框架上。
"""

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Optional, Sequence

from .config import Settings
from .envcheck import CheckResult

# 站点钩子的签名（只作文档用，运行时不校验）：
#   create_app(settings, extensions=())  -> FastAPI
#   checks(settings)                     -> list[CheckResult]
#   login_check(settings)                -> int（进程退出码，0 = 通过）
#   network_hint(results, settings)      -> None
#
# 另外 ``nav_order`` 只影响导航里的排序（见 ``pixiv_bridge.__main__``），与运行无关。


@dataclass(frozen=True)
class Site:
    """一个接口项目的「元数据 + 钩子」集合。"""

    #: 站点名（命令行的 prog 名、错误提示里的名字）
    name: str
    #: 站点目录（入口脚本所在目录，必需文件检查以此为基准）
    directory: Path
    #: 人类可读的名字（FastAPI title、导航页标题）
    title: str
    #: 组装应用的工厂：create_app(settings, extensions=())
    create_app: Callable[..., Any]
    version: str = "1.0.0"
    #: ``python -m pixiv_bridge`` 导航里的排序（小的在前，也是默认启动的那个）；
    #: 不写就是 100，排在写了 ``nav_order`` 的站点后面、彼此按目录名排
    nav_order: int = 100
    #: 站点自己的配置类（默认框架的 Settings；Pixiv 用 PixivSettings）
    settings_class: type = Settings
    #: 站点目录下必须随包拷齐的文件
    required_files: tuple = ("run.py",)
    #: 站点专属自检项（网络 / ECH / 代理 / 站点依赖 / 登录态…）
    checks: Optional[Callable[[Any], Sequence[CheckResult]]] = None
    #: ``--check`` 的登录校验
    login_check: Optional[Callable[[Any], int]] = None
    #: 自检里网络类项目失败时的可读提示
    network_hint: Optional[Callable[[Sequence[CheckResult], Any], None]] = None

    def load_settings(self, env_path=None):
        """读配置：站点可以换成自己的 Settings 子类（例如 ``PixivSettings``）。"""
        return self.settings_class.load(env_path)

    def environment_checks(self, settings):
        """站点专属自检项，没有就返回空表。"""
        if self.checks is None:
            return []
        return list(self.checks(settings))

    def build_app(self, settings, extensions=()):
        """按站点工厂建应用（扩展由调用方加载好后传进来）。"""
        return self.create_app(settings, extensions=tuple(extensions or ()))

    def check_login(self, settings):
        """``--check`` 的登录校验：返回进程退出码（0 = 通过）。"""
        if self.login_check is None:
            return 0
        return self.login_check(settings)

    def warn_network(self, results, settings):
        """网络类自检失败时的提示，没有钩子就什么都不打。"""
        if self.network_hint is not None:
            self.network_hint(results, settings)
