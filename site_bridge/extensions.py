"""扩展机制：让「附属项目」以目录的形式挂到接口项目上。

接口项目（`site_bridge`）本身只做 Pixiv 接口；「小说阅读器」这类附属功能
默认完全不参与。要让接口项目带上它，只需两步：

1. 把扩展目录放进项目目录里（例如 `novel/`）；
2. 在配置里写上它的相对路径：`SITE_BRIDGE_EXTENSIONS=novel`。

程序会去该目录下找**固定文件名** `extension.py`，从里面读出四样东西：

- `EXTENSION_ROUTERS`（必需）：要挂到接口应用上的路由元组；
- `extension_state_factory(settings)`（可选）：返回要挂到 `app.state` 上的
  对象字典，和 `site_bridge.server.create_app(state_factory=...)` 同形；
- `extension_shutdown(app)`（可选，async）：自己的资源在停机时释放
  （接口项目只关自己的 httpx 客户端，管不到扩展自己开的东西）；
- `EXTENSION_NAV`（可选）：要在导航页（`/`）上露出的页面清单，每项
  `{"title": ..., "path": ..., "description": ...}`。不写也照样出现在导航页上
  （只列扩展名），写了就有链接可点。

依赖方向仍然是单向的：扩展可以 import `site_bridge`，接口项目永远不认识
任何扩展的名字 —— 这里只是按配置里的路径去执行一个文件。
"""

import importlib.util
import sys
from dataclasses import dataclass
from pathlib import Path

# nav.py 只依赖标准库，不会反向 import 本模块，所以这里没有循环依赖。
from .nav import normalize_nav_items

# 扩展目录里固定要找的入口文件名。配置里只写目录，不写文件名 ——
# 这样「把目录拷过去 + 写一行配置」就是全部动作，换机器不用改配置。
EXTENSION_FILENAME = "extension.py"

EXTENSION_ROUTERS_ATTR = "EXTENSION_ROUTERS"
EXTENSION_STATE_ATTR = "extension_state_factory"
EXTENSION_SHUTDOWN_ATTR = "extension_shutdown"
EXTENSION_NAV_ATTR = "EXTENSION_NAV"


class ExtensionError(Exception):
    """扩展配置或加载出错（路径写错、目录里没有入口文件、入口文件抛异常）。"""


@dataclass
class Extension:
    """一个已经加载好的扩展。"""

    name: str            # 配置里写的那一项，例如 "novel"
    directory: Path      # 扩展目录
    entry: Path          # 入口文件（<目录>/extension.py）
    module: object       # 加载出来的模块对象
    routers: tuple = ()
    state_factory: object = None
    shutdown: object = None
    # 导航页上要露出的页面（`EXTENSION_NAV` 收拾成 `NavItem` 后的元组）。
    # 为空表示这个扩展没声明 —— 导航页仍然会列出它的名字，只是没有链接。
    nav: tuple = ()


def parse_extension_specs(value):
    """把 `SITE_BRIDGE_EXTENSIONS` 的原始字符串切成一项一项。

    逗号 / 分号 / 空白都算分隔符，所以 `novel, reader` 与 `novel;reader`
    与 `novel reader` 写法等价；空项自动丢掉。
    """
    items = []
    for chunk in (value or "").replace(";", ",").replace(",", " ").split():
        if chunk:
            items.append(chunk)
    return items


def resolve_extension_dir(spec, project_root):
    """把配置里的一项解析成绝对目录：相对路径按项目目录算。"""
    path = Path(spec).expanduser()
    if not path.is_absolute():
        path = Path(project_root) / path
    return path


def _module_name(spec):
    """给扩展模块起一个不会撞车的名字（要注册进 sys.modules）。"""
    cleaned = "".join(ch if ch.isalnum() else "_" for ch in spec)
    return f"site_bridge_extension_{cleaned}"


def load_extension(spec, project_root):
    """加载配置里的一项扩展，返回 `Extension`；出错抛 `ExtensionError`。"""
    directory = resolve_extension_dir(spec, project_root)
    entry = directory / EXTENSION_FILENAME
    if not entry.is_file():
        raise ExtensionError(
            f"扩展 {spec}：找不到入口文件 {entry}"
            f"（扩展目录里必须有固定的 {EXTENSION_FILENAME}）"
        )

    module = _exec_module(entry, _module_name(spec), spec)
    if not hasattr(module, EXTENSION_ROUTERS_ATTR):
        raise ExtensionError(
            f"扩展 {spec}：{entry} 里没有 {EXTENSION_ROUTERS_ATTR}"
            "（必须定义要挂载的路由元组）"
        )
    routers = getattr(module, EXTENSION_ROUTERS_ATTR)
    if routers is None:
        raise ExtensionError(f"扩展 {spec}：{EXTENSION_ROUTERS_ATTR} 不能是 None")
    if not isinstance(routers, (list, tuple)):
        routers = (routers,)

    state_factory = getattr(module, EXTENSION_STATE_ATTR, None)
    shutdown = getattr(module, EXTENSION_SHUTDOWN_ATTR, None)
    # 导航声明是可选的：不写就是空元组，导航页会退化成「只列这个扩展的名字」。
    nav = normalize_nav_items(getattr(module, EXTENSION_NAV_ATTR, ()))
    return Extension(
        name=spec,
        directory=directory,
        entry=entry,
        module=module,
        routers=tuple(routers),
        state_factory=state_factory,
        shutdown=shutdown,
        nav=nav,
    )


def _exec_module(entry, module_name, spec):
    loader_spec = importlib.util.spec_from_file_location(module_name, entry)
    if loader_spec is None or loader_spec.loader is None:
        raise ExtensionError(f"扩展 {spec}：{entry} 不是可导入的 Python 文件")
    module = importlib.util.module_from_spec(loader_spec)
    sys.modules[module_name] = module
    try:
        loader_spec.loader.exec_module(module)
    except Exception as exc:  # noqa: BLE001 - 如实转述扩展自己的报错
        sys.modules.pop(module_name, None)
        raise ExtensionError(
            f"扩展 {spec}：加载 {entry} 出错：{exc.__class__.__name__}: {exc}"
        ) from exc
    return module


def load_extension_specs(specs, project_root):
    """按顺序加载配置里的每一项扩展，返回 `Extension` 列表。"""
    specs = [str(item).strip() for item in (specs or ())]
    specs = [item for item in specs if item]
    if not specs:
        return []

    root = Path(project_root)
    # 扩展入口文件里通常写的是 `from novel.app import ...` 这种绝对导入，
    # 所以项目目录必须在 sys.path 上（`python run.py` 时本来就已自动带上，
    # 但 `create_app()` 也可能被别的方式调用，这里补一道保险）。
    root_text = str(root)
    if root_text not in sys.path:
        sys.path.insert(0, root_text)

    loaded = []
    seen = set()
    for spec in specs:
        if spec in seen:
            continue
        seen.add(spec)
        loaded.append(load_extension(spec, root))
    return loaded


def load_extensions(settings):
    """按 `settings.extensions` 加载扩展（`load_extension_specs` 的便捷包装）。"""
    return load_extension_specs(
        getattr(settings, "extensions", ()) or (),
        getattr(settings, "project_root", None) or Path.cwd(),
    )
