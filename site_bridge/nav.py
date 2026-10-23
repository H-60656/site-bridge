"""导航页：把「接口项目自带的入口」和「各扩展声明的页面」列成一页。

页面是零依赖的静态 HTML（和 `novel/web/index.html` 一样单文件、无构建步骤），
由 `site_bridge/routes/nav.py` 挂在 `/` 上。

扩展出现在导航页上有两种程度：

- 在 `extension.py` 里写可选导出 `EXTENSION_NAV`，逐条声明自己要露出的页面：

      EXTENSION_NAV = (
          {"title": "小说阅读器", "path": "/novel", "description": "读 Pixiv 小说"},
      )

- 什么都不写：导航页仍然会列出这个扩展（只给名字，不给链接），
  这样「现在挂了哪些东西」一眼可见，写没写声明都不影响它是否出现。

框架不认识任何具体扩展，也不认识具体接口站点 —— 页面标题取自应用自己的
`app.title`（站点在建应用时声明），别的入口（例如另一个站点 `iwara` 的运行实例）
在配置里用 `SITE_BRIDGE_NAV_LINKS` 手工补链接即可，例如
`SITE_BRIDGE_NAV_LINKS=iwara 视频=http://127.0.0.1:8766/`。

依赖方向：本模块只依赖标准库，扩展与接口项目都可以用它渲染自己的那一页。
"""

from dataclasses import dataclass
from html import escape

# 配置项：导航页上「其它入口」那一段的额外链接（`标题=地址`，逗号分隔）。
NAV_LINKS_ENV = "SITE_BRIDGE_NAV_LINKS"

# 接口项目自带、永远列在最上面的那几个入口。
BUILTIN_ITEMS = (
    {"title": "接口文档（Swagger UI）", "path": "/docs", "description": "在线调试所有接口"},
    {"title": "接口文档（ReDoc）", "path": "/redoc", "description": "同一份 OpenAPI 的另一种排版"},
    {"title": "OpenAPI 定义", "path": "/openapi.json", "description": "给程序读的路由清单"},
    {
        "title": "健康检查",
        "path": "/api/health",
        "description": '返回 {"ok": true}，用来确认服务活着',
    },
)

# 页面标题的兜底值（正常取自应用的 `app.title`）；副标题按当前挂了什么动态拼。
DEFAULT_TITLE = "Bridge"

# `href` 只接受这几类地址，别的（例如 javascript:）只当文本显示。
_SAFE_PREFIXES = ("/", "http://", "https://")


@dataclass(frozen=True)
class NavItem:
    """导航页上的一条链接。"""

    title: str
    path: str
    description: str = ""


@dataclass(frozen=True)
class NavSection:
    """导航页上的一小段（自带入口 / 某个扩展 / 别的入口补的页面）。"""

    title: str
    items: tuple = ()
    note: str = ""


def normalize_nav_items(raw):
    """把扩展声明的 `EXTENSION_NAV` 收拾成 `NavItem` 元组。

    容错：接受 dict / `NavItem` / 裸字符串（当路径用）；缺 `path` 的丢掉；
    缺 `title` 时用路径兜底。扩展写错一点不至于让整页报错。
    """
    items = []
    for entry in raw or ():
        if isinstance(entry, NavItem):
            items.append(entry)
            continue
        if isinstance(entry, str):
            entry = {"path": entry}
        if not isinstance(entry, dict):
            continue
        path = str(entry.get("path") or "").strip()
        if not path:
            continue
        title = str(entry.get("title") or "").strip() or path
        description = str(entry.get("description") or "").strip()
        items.append(NavItem(title=title, path=path, description=description))
    return tuple(items)


def parse_nav_links(value):
    """把 `SITE_BRIDGE_NAV_LINKS` 解析成额外链接。

    写法：`标题=地址`，多项用逗号 / 分号 / 换行分隔；只写地址时用地址当标题。
    例：`iwara 视频=http://127.0.0.1:8766/, 接口文档=/docs`。
    """
    items = []
    text = (value or "").replace(";", ",").replace(",", "\n")
    for chunk in text.split("\n"):
        chunk = chunk.strip()
        if not chunk:
            continue
        title, sep, path = chunk.partition("=")
        if not sep:
            title, path = "", chunk
        path = path.strip()
        if not path:
            continue
        items.append(NavItem(title=title.strip() or path, path=path))
    return tuple(items)


def extension_sections(extensions):
    """每个扩展一小段：声明了 `EXTENSION_NAV` 就列出来，没声明就只列名字。"""
    sections = []
    for ext in extensions or ():
        items = normalize_nav_items(getattr(ext, "nav", ()))
        note = ""
        if not items:
            note = (
                "这个扩展没有声明 EXTENSION_NAV，导航页上只列出它的名字；"
                "它挂的接口在接口文档里。"
            )
        sections.append(
            NavSection(
                title=f"扩展：{getattr(ext, 'name', '?')}",
                items=items,
                note=note,
            )
        )
    return tuple(sections)


def page_sections(app):
    """收集导航页要显示的各段内容。

    顺序：接口项目自带 → 别的入口补的（`app.state.nav_sections`）→ 各扩展。

    `app.state.nav_sections` 是留给「同一个导航页上还要露自己页面」的入口用的，
    典型是 `iwara`：它是独立的接口项目而不是 pixiv 的扩展，它在自己的
    `iwara_state_factory` 里把这一项挂上来，于是 `iwara/run.py` 启动后 `/`
    这个导航页会连 iwara 自己的页面一起列出。
    """
    sections = [
        NavSection(title="接口项目自带", items=normalize_nav_items(BUILTIN_ITEMS))
    ]
    for extra in getattr(app.state, "nav_sections", ()) or ():
        if isinstance(extra, NavSection):
            sections.append(extra)
        else:
            sections.append(NavSection(title="其它", items=normalize_nav_items(extra)))
    sections.extend(extension_sections(getattr(app.state, "extensions", ())))
    return tuple(sections)


def subtitle_for(app):
    """副标题：报一句当前挂载情况（没挂扩展就是纯接口）。"""
    extensions = tuple(getattr(app.state, "extensions", ()) or ())
    if not extensions:
        return "纯接口：当前没有挂载扩展。"
    names = "、".join(getattr(ext, "name", "?") for ext in extensions)
    return f"已挂载 {len(extensions)} 个扩展：{names}"


def render_page(app):
    """按当前应用状态渲染整个导航页（路由直接用它）。"""
    settings = getattr(app.state, "settings", None)
    return render_html(
        # 标题由接口站点自己声明（`create_bridge_app(title=...)` → FastAPI 的 app.title）；
        # 框架不写死任何一个站点的名字。
        title=getattr(app, "title", DEFAULT_TITLE) or DEFAULT_TITLE,
        subtitle=subtitle_for(app),
        sections=page_sections(app),
        links=parse_nav_links(getattr(settings, "nav_links", "")),
        footer="本页由接口项目渲染；扩展在 extension.py 里用 EXTENSION_NAV 声明页面。",
    )


def render_html(*, title, subtitle="", sections=(), links=(), footer=""):
    """生成导航页 HTML（纯字符串，无模板引擎、无外部资源）。"""
    out = [
        "<!DOCTYPE html>",
        '<html lang="zh-CN">',
        "<head>",
        '<meta charset="utf-8" />',
        '<meta name="viewport" content="width=device-width, initial-scale=1" />',
        f"<title>{escape(title)}</title>",
        f"<style>{_STYLE}</style>",
        "</head>",
        "<body>",
        '<div class="wrap">',
        f"<h1>{escape(title)}</h1>",
    ]
    if subtitle:
        out.append(f'<p class="sub">{escape(subtitle)}</p>')

    for section in sections:
        out.append(f"<h2>{escape(section.title)}</h2>")
        if section.items:
            out.append("<ul>")
            out.extend(f"<li>{_anchor(item)}</li>" for item in section.items)
            out.append("</ul>")
        if section.note:
            out.append(f'<div class="empty">{escape(section.note)}</div>')

    if links:
        out.append("<h2>其它入口</h2>")
        out.append("<ul>")
        out.extend(f"<li>{_anchor(item)}</li>" for item in links)
        out.append("</ul>")

    if footer:
        out.append(f"<footer>{escape(footer)}</footer>")
    out.extend(["</div>", "</body>", "</html>"])
    return "\n".join(out) + "\n"


def _anchor(item):
    """一条链接：只对可信地址（/ 或 http(s)）生成 <a>，其余只显示文本。"""
    head = (
        f'<div class="t">{escape(item.title)}</div>'
        f'<div class="p">{escape(item.path)}</div>'
    )
    if item.description:
        head += f'<div class="d">{escape(item.description)}</div>'
    if not item.path.startswith(_SAFE_PREFIXES):
        return f'<div class="card">{head}</div>'
    href = escape(item.path, quote=True)
    return f'<a class="card" href="{href}">{head}</a>'


_STYLE = """
:root{--bg:#f5f6f8;--panel:#fff;--text:#1d2129;--muted:#86909c;--line:#e5e6eb;
  --primary:#3b6ef0;--soft:#eef3ff}
@media (prefers-color-scheme:dark){:root{--bg:#16181d;--panel:#1f2229;--text:#dfe2e8;
  --muted:#8b93a1;--line:#2e323b;--primary:#6f9bff;--soft:#232a3a}}
*{box-sizing:border-box}
body{margin:0;padding:24px 16px 48px;background:var(--bg);color:var(--text);
  font:15px/1.6 -apple-system,BlinkMacSystemFont,"Segoe UI","Microsoft YaHei",sans-serif}
.wrap{max-width:720px;margin:0 auto}
h1{font-size:20px;margin:0 0 4px}
.sub{color:var(--muted);font-size:13px;margin:0 0 20px}
h2{font-size:13px;color:var(--muted);font-weight:600;margin:24px 0 8px}
ul{list-style:none;margin:0;padding:0}
li{margin:0 0 8px}
a.card,.card{display:block;padding:12px 14px;border:1px solid var(--line);
  border-radius:12px;background:var(--panel);color:inherit;text-decoration:none}
a.card:hover{border-color:var(--primary);background:var(--soft)}
.t{font-weight:600;color:var(--primary)}
.p{font-family:ui-monospace,Menlo,Consolas,monospace;font-size:12px;color:var(--muted);
  word-break:break-all}
.d{color:var(--muted);font-size:13px;margin-top:2px}
.empty{color:var(--muted);font-size:13px;padding:10px 14px;border:1px dashed var(--line);
  border-radius:12px}
footer{margin-top:28px;color:var(--muted);font-size:12px}
"""
