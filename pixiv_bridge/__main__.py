"""``python -m pixiv_bridge``：整个项目的导航 —— 列出接口项目与扩展，选一个再启动。

三种用法：

    python -m pixiv_bridge                      列出导航，选一个启动（回车＝默认）
    python -m pixiv_bridge <接口项目> [入口参数...]  直接启动某一个
    python -m pixiv_bridge --list               只打印导航，不启动

框架仍然不认识任何站点：这里只做目录发现（有 ``run.py`` 的目录＝接口项目，
只有 ``extension.py`` 的目录＝扩展），导航里的名字、简介、顺序都用 :mod:`ast`
从站点与扩展**自己的文件**里读字面量（不 import、也不执行它们的代码），选完才
把入口交给 :mod:`runpy` —— 真正的启动逻辑在各站点的 ``run.py`` 里
（它自己 bootstrap 站点额外依赖，例如 iwara 的 ``curl_cffi``）。

给站点/扩展看的信息来源（都能缺，缺了就退回目录名）：

- 接口项目的顺序与标题：``<站点>/app.py`` 里 ``SITE = Site(title=..., nav_order=...)``；
- 接口项目的简介：``<站点>/__init__.py`` 的 docstring 第一段；
- 扩展的标题与简介：``<扩展>/extension.py`` 的 ``EXTENSION_NAV``（退回它的 docstring）。

所以新增接口项目不用改本文件：目录按契约放好 ``app.py`` + ``run.py`` 就会出现在
导航里（扩展同理，放 ``extension.py``）。

没有终端可以询问时（stdin 不是 TTY：Docker、计划任务、管道）不挂住也不报错：
直接用导航里的第一个接口项目启动，并打印一行说明。``BRIDGE_SITE`` 最优先
（环境变量，其次项目根 ``.env`` 里的同名行）；``start.bat`` / ``start.sh``
也只转发到这里，站点的挑选逻辑只此一份。
"""

import ast
import os
import runpy
import sys
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

#: 站点没写 ``nav_order`` 时的排序（``Site.nav_order`` 的默认值）
DEFAULT_NAV_ORDER = 100

USAGE = """用法:
  python -m pixiv_bridge                          列出导航，选一个接口项目启动
  python -m pixiv_bridge <接口项目> [入口参数...]    直接启动它
  python -m pixiv_bridge --list                   只打印导航，不启动

入口参数原样传给站点入口：--host / --port / --reload / --check / --env。"""


@dataclass(frozen=True)
class NavItem:
    """导航里的一项：接口项目（``kind="site"``）或扩展（``kind="extension"``）。"""

    name: str
    kind: str
    #: 站点是 ``<目录>/run.py``，扩展是 ``<目录>/extension.py``
    entry: Path
    title: str
    description: str
    order: int


# --------------------------------------------------------------------------
# 读字面量：只解析、不 import，站点代码一行都不执行
# --------------------------------------------------------------------------

def _parse(path):
    """解析一个 Python 文件；读不到或语法错就返回 ``None``（导航照常出）。"""
    try:
        return ast.parse(path.read_text(encoding="utf-8"))
    except (OSError, SyntaxError, UnicodeDecodeError):
        return None


def _literal(node, constants=None):
    """把 AST 节点还原成字面量；是名字就去 ``constants`` 里找，找不到返回 None。"""
    if isinstance(node, ast.Constant) and isinstance(node.value, (str, int)):
        return node.value
    if isinstance(node, ast.Name) and constants:
        return constants.get(node.id)
    return None


def _module_constants(tree):
    """模块顶层的 ``NAME = <字面量>``，用来把 ``title=TITLE`` 解析回字符串。"""
    constants = {}
    for node in tree.body:
        if not isinstance(node, ast.Assign) or len(node.targets) != 1:
            continue
        target = node.targets[0]
        if isinstance(target, ast.Name):
            value = _literal(node.value)
            if value is not None:
                constants[target.id] = value
    return constants


def _docstring_paragraph(path):
    """模块 docstring 的第一段（折成一行），当简介用。"""
    tree = _parse(path)
    if tree is None:
        return ""
    doc = ast.get_docstring(tree) or ""
    lines = []
    for line in doc.splitlines():
        if not line.strip():
            if lines:
                break
            continue
        lines.append(line.strip())
    return " ".join(lines)


def _site_metadata(directory):
    """``<站点>/app.py`` 里 ``SITE = Site(...)`` 的字面量：``(title, nav_order)``。"""
    tree = _parse(directory / "app.py")
    if tree is not None:
        constants = _module_constants(tree)
        for node in tree.body:
            if not isinstance(node, ast.Assign) or not isinstance(node.value, ast.Call):
                continue
            call = node.value
            if not isinstance(call.func, ast.Name) or call.func.id != "Site":
                continue
            keywords = {kw.arg: _literal(kw.value, constants) for kw in call.keywords}
            title = keywords.get("title")
            order = keywords.get("nav_order")
            return (
                title if isinstance(title, str) and title else directory.name,
                order if isinstance(order, int) else DEFAULT_NAV_ORDER,
            )
    return directory.name, DEFAULT_NAV_ORDER


def _extension_metadata(directory):
    """``<扩展>/extension.py`` 的 ``EXTENSION_NAV`` 里第一项的 ``(title, description)``。"""
    source = directory / "extension.py"
    tree = _parse(source)
    if tree is not None:
        for node in tree.body:
            if not isinstance(node, ast.Assign) or not isinstance(node.value, (ast.Tuple, ast.List)):
                continue
            if not any(isinstance(target, ast.Name) and target.id == "EXTENSION_NAV" for target in node.targets):
                continue
            for element in node.value.elts:
                if not isinstance(element, ast.Dict):
                    continue
                pairs = {
                    key.value: _literal(value)
                    for key, value in zip(element.keys, element.values)
                    if isinstance(key, ast.Constant) and isinstance(key.value, str)
                }
                title = pairs.get("title")
                if isinstance(title, str) and title:
                    description = pairs.get("description")
                    return title, description if isinstance(description, str) else ""
    return _docstring_paragraph(source) or directory.name, ""


# --------------------------------------------------------------------------
# 目录发现
# --------------------------------------------------------------------------

def _directories(root):
    try:
        return [path for path in sorted(root.iterdir()) if path.is_dir()]
    except OSError:  # pragma: no cover - 项目根不可读时导航没什么可列的
        return []


def site_items(root=None):
    """接口项目＝目录里有 ``run.py``；按 ``nav_order``、再按目录名排序。"""
    root = ROOT if root is None else root
    items = []
    for directory in _directories(root):
        if not (directory / "run.py").is_file():
            continue
        title, order = _site_metadata(directory)
        items.append(
            NavItem(
                name=directory.name,
                kind="site",
                entry=directory / "run.py",
                title=title,
                description=_docstring_paragraph(directory / "__init__.py")
                or _docstring_paragraph(directory / "run.py"),
                order=order,
            )
        )
    return sorted(items, key=lambda item: (item.order, item.name))


def extension_items(root=None):
    """扩展＝目录里有 ``extension.py`` 而没有 ``run.py``（它不是入口）。"""
    root = ROOT if root is None else root
    items = []
    for directory in _directories(root):
        if (directory / "run.py").is_file() or not (directory / "extension.py").is_file():
            continue
        title, description = _extension_metadata(directory)
        items.append(
            NavItem(
                name=directory.name,
                kind="extension",
                entry=directory / "extension.py",
                title=title,
                description=description,
                order=DEFAULT_NAV_ORDER,
            )
        )
    return sorted(items, key=lambda item: item.name)


def site_names(root=None):
    """所有接口项目的目录名（按导航顺序）。"""
    return [item.name for item in site_items(root)]


def _line(item, prefix):
    text = item.title or item.name
    if item.description:
        text = f"{text} —— {item.description}"
    return f"{prefix}{item.name:<9} {text}"


def render_navigation(root=None):
    """导航全文：接口项目（带编号）与扩展（不带编号，它们不是入口）。"""
    root = ROOT if root is None else root
    sites = site_items(root)
    extensions = extension_items(root)
    lines = [f"{root.name} —— 项目导航（{root}）", ""]
    lines.append("接口项目（各自有 run.py、互不依赖；选一个启动）")
    if sites:
        lines += [_line(item, f"  {index}) ") for index, item in enumerate(sites, start=1)]
    else:
        lines.append("  （没有找到：本目录下需要有 <接口项目>/run.py）")
    lines.append("")
    lines.append("扩展（不是入口、没有 run.py；在 .env 里写 PIXIV_BRIDGE_EXTENSIONS=<名字> 挂到接口项目上）")
    if extensions:
        lines += [_line(item, "      ") for item in extensions]
    else:
        lines.append("  （没有找到：扩展目录里需要有 extension.py）")
    lines.append("")
    lines.append(USAGE)
    if sites:
        lines.append("")
        lines.append(f"提示: BRIDGE_SITE={sites[0].name} 可让 start.bat / start.sh 跳过询问、直接启动它")
    return "\n".join(lines)


# --------------------------------------------------------------------------
# 选站点 + 启动
# --------------------------------------------------------------------------

def _explicit_site():
    """``BRIDGE_SITE``：环境变量最优先，其次是项目根 ``.env`` 里的同名行。"""
    value = (os.environ.get("BRIDGE_SITE") or "").strip()
    if value:
        return value
    from .config import _load_dotenv  # 延后导入：--list / --help 用不到配置

    _load_dotenv(ROOT / ".env")
    return (os.environ.get("BRIDGE_SITE") or "").strip()


def choose_site(sites, stream=None, ask=input, out=print, err=None):
    """挑一个接口项目：``BRIDGE_SITE`` → 只有一个就直接用 → 问终端 → 默认第一个。

    无法决定（``BRIDGE_SITE`` 写错、询问时按了 Ctrl-C）返回 ``None``。
    ``out`` / ``err`` 是两个输出口（测试可以注入别的实现），``err`` 缺省写标准错误。
    """
    if err is None:
        def err(message):
            print(message, file=sys.stderr)

    names = ", ".join(item.name for item in sites)
    wanted = _explicit_site()
    if wanted:
        if any(item.name == wanted for item in sites):
            return wanted
        err(f"BRIDGE_SITE={wanted} 不是本目录里的接口项目（可选：{names}）")
        return None
    if len(sites) == 1:
        return sites[0].name
    stream = sys.stdin if stream is None else stream
    if not stream.isatty():
        out(f"[i] 没有可交互的终端：启动默认接口项目 {sites[0].name}（--list 看全部，BRIDGE_SITE 可指定）")
        return sites[0].name
    out(render_navigation())
    out("")
    while True:
        try:
            answer = ask("请选择要启动的接口项目 [1]: ").strip()
        except (EOFError, KeyboardInterrupt):
            out("")
            return None
        if not answer:
            return sites[0].name
        if answer.isdigit() and 1 <= int(answer) <= len(sites):
            return sites[int(answer) - 1].name
        if answer in {item.name for item in sites}:
            return answer
        out(f"没听懂「{answer}」：输入编号 1-{len(sites)}，或接口项目名（{names}）")


def _launch(name, rest):
    """把入口交给 :mod:`runpy`：bootstrap 与启动都在站点自己的 ``run.py`` 里。"""
    entry = ROOT / name / "run.py"
    sys.argv = [str(entry), *rest]
    runpy.run_path(str(entry), run_name="__main__")
    return 0


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    if argv and argv[0] in ("-h", "--help"):
        print(USAGE)
        print()
        print(render_navigation())
        return 0
    if argv and argv[0] in ("-l", "--list"):
        print(render_navigation())
        return 0

    sites = site_items()
    names = ", ".join(item.name for item in sites)
    first = argv[0] if argv else None
    if first is not None and not first.startswith("-"):
        if not (ROOT / first / "run.py").is_file():
            print(
                f"没有这个接口项目: {first}（可选：{names or '没有找到任何接口项目'}）",
                file=sys.stderr,
            )
            return 2
        return _launch(first, argv[1:])
    if not sites:
        print("没有找到任何接口项目：本目录下需要有 <接口项目>/run.py。", file=sys.stderr)
        return 2
    name = choose_site(sites)
    if name is None:
        return 2
    return _launch(name, argv)


if __name__ == "__main__":
    sys.exit(main())
