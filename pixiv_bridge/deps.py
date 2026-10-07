"""依赖检查与自动安装。

移植原则：整个项目文件夹可以直接拷到另一台机器运行——只要那台机器有
Python 3.10+，其余 Python 依赖一律在这里自动下载安装。为了支持无网环境，
若项目根目录下存在 ``wheels/``（或 ``vendor/``、``pip-wheels/``）目录，
会优先用 ``--no-index --find-links`` 从本地 wheel 离线安装。
在 Termux（Android）上会自动改用 ``requirements-termux.txt``：那里装不了
pydantic 2 的 pydantic-core（bionic libc + 需 Rust 编译），所以用纯 Python 的
pydantic 1.x 依赖集。
"""

import importlib
import importlib.util
import os
import subprocess
import sys
from pathlib import Path

REQUIRED = ("fastapi", "uvicorn", "httpx")

DEFAULT_PIP_INDEXES = (
    "https://pypi.tuna.tsinghua.edu.cn/simple",
    "https://mirrors.aliyun.com/pypi/simple",
    "https://pypi.org/simple",
)

WHEEL_DIR_NAMES = ("wheels", "vendor", "pip-wheels")

# Termux 上 pydantic 2 的 pydantic-core 无法安装（bionic libc + 需要 Rust 编译），
# 因此那里改用 requirements-termux.txt 钉住纯 Python 的 pydantic 1.x。
TERMUX_REQUIREMENTS = "requirements-termux.txt"

MANUAL_HINT = (
    "可手动安装：{python} -m pip install -r {req}；"
    "离线环境请先在能上网的机器上执行 {python} -m pip download -r {req} -d wheels，"
    "把整个 wheels 目录随项目一起拷贝过来（详见 README「打包与移植」）"
)


def _importable(name):
    try:
        importlib.import_module(name)
        return True
    except ImportError:
        return False


def missing_dependencies():
    return [name for name in REQUIRED if not _importable(name)]


def project_root_of(project_root=None):
    return Path(project_root or Path(__file__).resolve().parent.parent)


def is_termux():
    """是否运行在 Termux（Android）里。

    Termux 的特征：``sys.platform`` 为 ``android`` 或 ``linux``，解释器路径与环境
    变量 ``PREFIX`` 指向 ``/data/data/com.termux/files/usr``，并会设置
    ``TERMUX_VERSION``。
    """
    prefix = os.environ.get("PREFIX", "")
    markers = (prefix, sys.executable, os.environ.get("TERMUX_VERSION", ""))
    return sys.platform == "android" or any("com.termux" in item for item in markers)


def requirements_path(project_root=None, termux=None):
    """返回要安装的依赖清单路径。

    Termux 上优先用 ``requirements-termux.txt``（纯 Python 依赖集），该文件不存在
    时回退到通用的 ``requirements.txt``。
    """
    root = project_root_of(project_root)
    if termux is None:
        termux = is_termux()
    if termux:
        termux_requirements = root / TERMUX_REQUIREMENTS
        if termux_requirements.is_file():
            return termux_requirements
    return root / "requirements.txt"


def wheel_dirs(project_root=None):
    """返回存在的本地 wheel 离线包目录（按优先顺序）。"""
    root = project_root_of(project_root)
    return tuple(root / name for name in WHEEL_DIR_NAMES if (root / name).is_dir())


def _pip_indexes():
    custom = os.environ.get("PIXIV_BRIDGE_PIP_INDEX")
    if custom:
        return (custom,)
    return DEFAULT_PIP_INDEXES


def _pip_available():
    return importlib.util.find_spec("pip") is not None


def _ensure_pip():
    """部分系统自带 Python 不含 pip（Debian/Ubuntu 常见），先用 ensurepip 补上。"""
    if _pip_available():
        return True
    try:
        subprocess.check_call(
            [sys.executable, "-m", "ensurepip", "--upgrade"],
            stdout=subprocess.DEVNULL,
        )
    except (subprocess.CalledProcessError, OSError):
        return False
    return _pip_available()


def _install_candidates(requirements, project_root=None):
    """按顺序返回 (说明, 命令) 列表：先本地 wheel（离线），再各镜像源。"""
    candidates = []
    for directory in wheel_dirs(project_root):
        candidates.append(
            (
                f"本地离线包 {directory}",
                [
                    sys.executable,
                    "-m",
                    "pip",
                    "install",
                    "--disable-pip-version-check",
                    "--no-index",
                    "--find-links",
                    str(directory),
                    "-r",
                    str(requirements),
                ],
            )
        )
    for index in _pip_indexes():
        candidates.append(
            (
                f"镜像 {index}",
                [
                    sys.executable,
                    "-m",
                    "pip",
                    "install",
                    "--disable-pip-version-check",
                    "--timeout",
                    "30",
                    "--retries",
                    "2",
                    "-r",
                    str(requirements),
                    "-i",
                    index,
                ],
            )
        )
    return candidates


def _install_requirements(requirements, project_root=None):
    if not _ensure_pip():
        raise RuntimeError(
            "当前 Python 环境没有 pip，且自动执行 `python -m ensurepip --upgrade` 失败。"
            "请先安装 pip（Debian/Ubuntu: apt install python3-pip）后重试"
        )
    last_error = None
    for label, cmd in _install_candidates(requirements, project_root):
        try:
            subprocess.check_call(cmd)
            return label
        except subprocess.CalledProcessError as exc:
            last_error = exc
            print(f"[deps] {label} 安装失败，尝试下一种方式…", file=sys.stderr)
    raise last_error


def ensure_dependencies(project_root=None):
    missing = missing_dependencies()
    if not missing:
        return []
    requirements = requirements_path(project_root)
    if is_termux():
        print(
            "[deps] 检测到 Termux：改用纯 Python 依赖集 "
            f"{requirements.name}（pydantic 1.x，无需 Rust 编译）；首次安装需要几分钟…"
        )
    try:
        _install_requirements(requirements, project_root)
    except subprocess.CalledProcessError as exc:
        raise RuntimeError(
            "依赖自动安装失败，本地离线包与所有镜像源均不可用。"
            + MANUAL_HINT.format(python=sys.executable, req=requirements)
        ) from exc
    still_missing = missing_dependencies()
    if still_missing:
        raise RuntimeError(
            f"依赖安装后仍缺失: {', '.join(still_missing)}。"
            + MANUAL_HINT.format(python=sys.executable, req=requirements)
        )
    return missing
