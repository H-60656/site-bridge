"""启动前的通用环境自检：Python 版本、依赖、必需文件、端口。

站点专属的探测（网络 / ECH / 代理 / 站点依赖 / 登录态）不在框架里 ——
站点通过 :class:`pixiv_bridge.site.Site` 的 ``checks`` 钩子把自己的检查项
追加到同一张表里（见 ``iwara/envcheck.py``、``pixiv/envcheck.py``）。
"""

import socket
import sys
from dataclasses import dataclass
from pathlib import Path

from . import deps

MIN_PYTHON = (3, 10)
# 框架自己运行必需的文件（相对项目根目录）。
REQUIRED_FILES = ("requirements.txt",)


@dataclass
class CheckResult:
    name: str
    ok: bool
    detail: str


def python_version():
    ok = sys.version_info >= MIN_PYTHON
    return CheckResult(
        "python", ok, f"{sys.version.split()[0]} (需要 3.10+)"
    )


def dependencies():
    missing = deps.missing_dependencies()
    detail = ", ".join(missing) if missing else "依赖完整"
    return CheckResult("deps", not missing, detail)


def _as_pairs(targets):
    """把 project_files 的参数规整成 ``[(目录, 文件名元组), ...]``。"""
    if not targets:
        return [(Path(__file__).resolve().parent.parent, REQUIRED_FILES)]
    pairs = []
    for target in targets:
        if isinstance(target, (tuple, list)):
            directory, names = target
        else:
            directory, names = target, REQUIRED_FILES
        pairs.append((Path(directory), tuple(names)))
    return pairs


def project_files(targets=None):
    """检查运行必需的文件是否随文件夹一起拷齐（移植自检）。

    `targets` 是 ``(目录, 文件名序列)`` 的列表（也可以只给目录，用框架的
    ``REQUIRED_FILES``）；缺省检查框架自己的项目根。多个目录时缺的文件名
    带上目录名，免得只看文件名分不清是谁缺。
    """
    pairs = _as_pairs(targets)
    missing = []
    for directory, names in pairs:
        for name in names:
            if not (directory / name).is_file():
                missing.append(f"{directory.name}/{name}" if len(pairs) > 1 else name)
    if missing:
        return CheckResult(
            "files",
            False,
            f"缺少 {', '.join(missing)}（接口仍可运行，但 Web 页面或依赖自动安装会受影响）",
        )
    return CheckResult("files", True, "项目文件完整")


def port_available(port):
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        try:
            sock.bind(("127.0.0.1", port))
            return CheckResult("port", True, f"{port} 可用")
        except OSError:
            return CheckResult("port", False, f"{port} 已被占用")


def run_checks(port=None, files=()):
    """通用自检项：Python / 依赖 / 必需文件 / 端口。

    `files` 是 ``(目录, 文件名序列)`` 的列表（框架项目根 + 站点目录）。
    站点专属项由入口追加，见 :meth:`pixiv_bridge.site.Site.environment_checks`。
    """
    results = [python_version(), dependencies(), project_files(files)]
    if port is not None:
        results.append(port_available(port))
    return results
