"""E-Hentai 页面解析：只用正则，不引入 selectolax。

与 iwara 的选择一致（``iwara/ech.py`` 也只是 ``import re``）：这个站点要解析的
是**四张固定结构的页面**，正则够用，不必为此多带一个 HTML 解析库 —— 而且本机
``selectolax`` 装不上（PyPI 的 DNS 被污染，镜像装的编译轮子也被沙箱拒），
带上它就等于这个站点在这里跑不起来。

解析的目标页面（2026-10 实测，全部 HTTP/3 取回）：

    ``/`` ``/?next=…`` ``/?f_search=…``   列表页：每行一个画廊
    ``/g/<gid>/<token>/``                 详情页：元数据 + 标签 + 页面条带
    ``/s/<key>/<gid>-<n>``                大图页：真实图片直链 + 前后页

**这里只做「HTML → dict」，不发请求、不碰网络**，所以可以脱离 curl_cffi 单测
（``tests/test_ehviewer.py`` 就是直接喂页面片段给这些函数）。
"""

from __future__ import annotations

import html as _html
import re
from collections.abc import Mapping
from urllib.parse import unquote

# ---------------------------------------------------------------- 小工具

_TAG_RE = re.compile(r"<[^>]+>")
_WS_RE = re.compile(r"\s+")


def strip_tags(value: str) -> str:
    """去掉 HTML 标签，只剩文字。"""
    return _TAG_RE.sub("", value or "")


def text(value: str) -> str:
    """标签 → 去实体 → 压空白 → 去首尾。

    ``&nbsp;`` 会被 ``html.unescape`` 变成 ``\\xa0``，它算 ``\\s``，所以能一起
    压掉（详情页的 ``Language`` 字段值就带这个）。
    """
    return _WS_RE.sub(" ", _html.unescape(strip_tags(value or ""))).strip()


def attr_text(value: str) -> str:
    """属性值里的文本：只需要去实体，不能去空白（URL 里有 ``+``）。"""
    return _html.unescape(value or "").strip()


def to_int(value, default=None):
    """``"40 pages"`` / ``"12"`` → ``40`` / ``12``；取不到就 ``default``。"""
    if value is None:
        return default
    match = re.search(r"-?\d+", str(value))
    return int(match.group()) if match else default


def to_float(value, default=None):
    if value is None:
        return default
    match = re.search(r"-?\d+(?:\.\d+)?", str(value))
    return float(match.group()) if match else default


# ---------------------------------------------------------------- 列表页

#: 列表页行级切分的锚：只有画廊行有 ``gl3c glname``（分类列是 ``gl1c``、
#: 缩略图列是 ``gl2c``、上传者列是 ``gl4c``，别拿那些当锚，它们可能被省掉）。
_LIST_ROW_MARK = "gl3c glname"

_LINK_RE = re.compile(r'href="(https://e-hentai\.org/g/(\d+)/([0-9a-fA-F]+)/?)"')
_GLINK_RE = re.compile(r'<div class="glink">(.*?)</div>', re.S)
_CATEGORY_RE = re.compile(r'<div class="cn[^"]*"[^>]*>(.*?)</div>', re.S)
_ROW_TAG_RE = re.compile(r'<div class="gt[^"]*"[^>]*title="([^"]*)"')
_UPLOADER_RE = re.compile(r'href="[^"]*/uploader/([^"]+)"')
_PAGES_RE = re.compile(r">\s*(\d+)\s*pages?\s*<")
_POSTED_RE = re.compile(r"(\d{4}-\d{2}-\d{2}[ T]\d{2}:\d{2})")
#: 缩略图：第一行是 ``src="https://ehgt.org/…"``，第二行起是 base64 占位 +
#: ``data-src`` 懒加载。``https?://`` 这个前缀天然把 ``data:image/gif;base64,…``
#: 挡掉了，所以两个属性可以一起试。
_THUMB_RE = re.compile(r'(?:data-src|src)="(https?://[^"]+)"')

_DNEXT_RE = re.compile(r'<a\b[^>]*id="dnext"[^>]*href="([^"]+)"')
_DNEXT_RE2 = re.compile(r'<a\b[^>]*href="([^"]+)"[^>]*id="dnext"')
_DPREV_RE = re.compile(r'<a\b[^>]*id="dprev"[^>]*href="([^"]+)"')
_DPREV_RE2 = re.compile(r'<a\b[^>]*href="([^"]+)"[^>]*id="dprev"')
#: ``/s/`` 大图页的翻页锚用的是**短 id**（``id="prev"`` / ``id="next"``），
#: 与列表页的 ``dprev`` / ``dnext`` 不是一套；少了这一组，详情页能解析而
#: 大图页的 prev/next 永远是空（实测踩过）。
_SNEXT_RE = re.compile(r'<a\b[^>]*id="next"[^>]*href="([^"]+)"')
_SNEXT_RE2 = re.compile(r'<a\b[^>]*href="([^"]+)"[^>]*id="next"')
_SPREV_RE = re.compile(r'<a\b[^>]*id="prev"[^>]*href="([^"]+)"')
_SPREV_RE2 = re.compile(r'<a\b[^>]*href="([^"]+)"[^>]*id="prev"')
_COUNT_RE = re.compile(r"Found\s+(?:about\s+)?([^<]*?)\s*results", re.I)

#: ``which`` → 依次尝试的正则（id 与 href 谁在前都认；短 id 与 ``d`` 前缀 id
#: 两套都列上，因为列表页与大图页各用一套）。
_NAV_PATTERNS = {
    "dnext": (_DNEXT_RE, _DNEXT_RE2),
    "dprev": (_DPREV_RE, _DPREV_RE2),
    "next": (_SNEXT_RE, _SNEXT_RE2),
    "prev": (_SPREV_RE, _SPREV_RE2),
}


def _nav_href(fragment: str, which: str) -> str:
    """取翻页锚上的 href（id 与 href 谁在前都认）。

    没有下一页时那个位置是 ``<span id="dnext">`` 而不是 ``<a>``，所以这里
    只匹配 ``<a>``：拿不到就返回空串（末页的信号）。
    """
    for pattern in _NAV_PATTERNS.get(which, ()):
        match = pattern.search(fragment)
        if match:
            return attr_text(match.group(1))
    return ""


def _cursor_of(url: str, key: str = "next") -> str:
    """从 ``…&next=4216410`` 里取出光标值。

    上一页用的是 ``?prev=``，键名必须跟着翻页方向走 —— 写死 ``next`` 会让
    ``prev`` 永远解析成空串（实测踩过）。
    """
    match = re.search(rf"[?&]{re.escape(key)}=(\d+)", url or "")
    return match.group(1) if match else ""


def parse_row_tags(fragment: str) -> list:
    """列表行的标签：``<div class="gt" title="parody:pokemon">pokemon</div>``。

    行内标签的 ``title`` 带完整命名空间（``parody:pokemon``），所以列表页不用
    再请求详情页就能给出可用的标签。
    """
    tags = []
    for raw in _ROW_TAG_RE.findall(fragment):
        key = attr_text(raw)
        if not key:
            continue
        namespace, _, slug = key.partition(":")
        tags.append(
            {
                "namespace": namespace or "other",
                "tag": slug or namespace,
                "name": slug or namespace,
                # 列表页给不了显示名（那是语言相关的），留空由调用方决定。
                "key": key,
            }
        )
    return tags


def parse_galleries(body: str) -> list:
    """列表页 → 画廊行列表。

    按 ``<tr`` 切分再筛 ``gl3c glname``，比「整个页面找链接」稳：页面里还有
    导航、上传者、标签链接，全都长得像 ``/g/<gid>/…`` 的只有画廊行。
    """
    items = []
    for fragment in re.split(r"<tr\b", body or "")[1:]:
        if _LIST_ROW_MARK not in fragment:
            continue
        link = _LINK_RE.search(fragment)
        if not link:
            continue
        url, gid, token = link.group(1), link.group(2), link.group(3)
        title = ""
        glink = _GLINK_RE.search(fragment)
        if glink:
            title = text(glink.group(1))
        if not title:
            # 兜底：``glink`` 结构变了也要能给出个标题（取锚点里的第一段文字）。
            anchor = re.search(r'<a\b[^>]*href="[^"]*/g/\d+/[0-9a-fA-F]+/?"[^>]*>(.*?)</a>', fragment, re.S)
            if anchor:
                title = text(anchor.group(1))
        category = ""
        cat = _CATEGORY_RE.search(fragment)
        if cat:
            category = text(cat.group(1))
        uploader = ""
        up = _UPLOADER_RE.search(fragment)
        if up:
            uploader = unquote(attr_text(up.group(1)))
        thumb = ""
        th = _THUMB_RE.search(fragment)
        if th:
            thumb = attr_text(th.group(1))
        posted = ""
        post = _POSTED_RE.search(fragment)
        if post:
            posted = post.group(1)
        pages = None
        pg = _PAGES_RE.search(fragment)
        if pg:
            pages = to_int(pg.group(1))

        items.append(
            {
                "gid": int(gid),
                "token": token,
                "url": f"https://e-hentai.org/g/{gid}/{token}/",
                "title": title,
                "category": category,
                "uploader": uploader,
                "pages": pages,
                "posted": posted,
                # 缩略图挂在 ehgt.org 上 —— 那个域名本机是 IP 级被阻断的、
                # 也不在 Cloudflare 上（没有 QUIC），取不到。这里原样带出来
                # 只为信息完整，**不要**拿它做代转：封面走详情页的 hath 条带。
                "thumbnail": thumb,
                "thumbnail_usable": False,
                "tags": parse_row_tags(fragment),
            }
        )
    return items


def parse_result_count(body: str) -> str:
    """``Found about 3,203 results`` / ``Found 1,658,228 results`` → ``"3,203"``。

    首页不带 ``about``、搜索结果页带，所以那个词是可选的（不要让它混进结果数）。
    """
    match = _COUNT_RE.search(body or "")
    return match.group(1).strip() if match else ""


def parse_list_nav(body: str) -> dict:
    """列表页的翻页信息。

    E-Hentai **不用页码分页**：``?page=N`` 被完全忽略，唯一有效的是
    ``?next=<上一页最后一个 gid>`` 这种光标。所以这里给的是 URL 与光标值，
    不是页码。
    """
    next_url = _nav_href(body or "", "dnext")
    prev_url = _nav_href(body or "", "dprev")
    return {
        "next_url": next_url,
        "next": _cursor_of(next_url),
        "prev_url": prev_url,
        "prev": _cursor_of(prev_url, "prev"),
        "found": parse_result_count(body or ""),
    }


# ---------------------------------------------------------------- 详情页

_GN_RE = re.compile(r'<h1[^>]*id="gn"[^>]*>(.*?)</h1>', re.S)
_GJ_RE = re.compile(r'<h1[^>]*id="gj"[^>]*>(.*?)</h1>', re.S)
_GDC_RE = re.compile(r'<div[^>]*id="gdc"[^>]*>(.*?)</div>\s*</div>', re.S)
_GDC_INNER_RE = re.compile(r'class="cs[^"]*"[^>]*>(.*?)</div>', re.S)
_GDN_RE = re.compile(r'<div[^>]*id="gdn"[^>]*>(.*?)</div>', re.S)
_GDD_ROW_RE = re.compile(
    r'<td class="gdt1">(.*?)</td>\s*<td class="gdt2"[^>]*>(.*?)</td>', re.S
)
_RATING_LABEL_RE = re.compile(r'<td[^>]*id="rating_label"[^>]*>(.*?)</td>', re.S)
_RATING_COUNT_RE = re.compile(r'<span[^>]*id="rating_count"[^>]*>(.*?)</span>', re.S)
_GPC_RE = re.compile(r'<p class="gpc">(.*?)</p>', re.S)

#: 标签块：``<div id="td_<ns>:<slug>" class="gt|gtl|gtw" …><a …>显示名</a></div>``。
#: 命名空间与 slug 都在 ``id`` 里；``class`` 可能是 ``gt``（普通）、``gtl``
#: （语言类）、``gtw``（权重类）—— **后两者也是真标签**，别只认 ``gt``。
_TAG_DIV_RE = re.compile(r'<div\b([^>]*id="td_[^"]+"[^>]*)>(.*?)</div>', re.S)
_TAG_ID_RE = re.compile(r'id="td_([^"]+)"')
_TAG_CLASS_RE = re.compile(r'class="([^"]*)"')
_TAG_HREF_RE = re.compile(r'href="([^"]*)"')

#: 详情页的页面条带：缩略图挂在 ``*.hath.network`` 上（``ehgt.org`` 那块取不到），
#: 所以封面从条带里拿。**多张图共用同一张 sprite**，靠 ``-Npx 0`` 偏移切分，
#: 因此这里只取**第一张**（画廊封面）就够了。
_STRIP_RE = re.compile(r"url\((https://[^)\s\"']*hath\.network[^)\s\"']*)\)")
_LENGTH_RE = re.compile(r"(\d+)\s*pages?", re.I)


def parse_detail(body: str) -> dict:
    """详情页 → 元数据 + 标签 + 条带封面。

    ``#gdd`` 那张信息表的**行序不固定**（``Posted`` / ``Parent`` / ``Visible``
    / ``Language`` / ``File Size`` / ``Length`` / ``Favorited`` 会按有值的项
    增删），所以按键名收进 dict，不要按下标取。
    """
    body = body or ""
    info: dict = {}
    for row in _GDD_ROW_RE.finditer(body):
        key = text(row.group(1)).rstrip(":").strip()
        if key:
            info[key] = text(row.group(2))

    category = ""
    gdc = _GDC_RE.search(body)
    if gdc:
        inner = _GDC_INNER_RE.search(gdc.group(1))
        category = text(inner.group(1)) if inner else text(gdc.group(1))
    if not category:
        cat = _CATEGORY_RE.search(body)
        if cat:
            category = text(cat.group(1))

    uploader = ""
    gdn = _GDN_RE.search(body)
    if gdn:
        anchor = re.search(r'href="[^"]*/uploader/([^"]+)"', gdn.group(1))
        uploader = unquote(attr_text(anchor.group(1))) if anchor else text(gdn.group(1))

    rating = None
    rating_count = None
    label = _RATING_LABEL_RE.search(body)
    if label:
        rating = to_float(text(label.group(1).replace("Average:", "")))
    count = _RATING_COUNT_RE.search(body)
    if count:
        rating_count = to_int(text(count.group(1)))

    tags = parse_detail_tags(body)

    length = info.get("Length", "")
    pages = to_int(length) if length else None

    canonical = re.search(r'href="(https://e-hentai\.org/g/(\d+)/([0-9a-fA-F]+)/?)"', body)
    gid = int(canonical.group(2)) if canonical else None
    token = canonical.group(3) if canonical else ""

    return {
        "gid": gid,
        "token": token,
        "title": text(_GN_RE.search(body).group(1)) if _GN_RE.search(body) else "",
        "title_jpn": text(_GJ_RE.search(body).group(1)) if _GJ_RE.search(body) else "",
        "category": category,
        "uploader": uploader,
        "posted": info.get("Posted", ""),
        "parent": info.get("Parent", ""),
        "visible": info.get("Visible", ""),
        "language": info.get("Language", ""),
        "file_size": info.get("File Size", ""),
        "length": length,
        "favorited": info.get("Favorited", ""),
        "rating": rating,
        "rating_count": rating_count,
        "tags": tags,
        "pages": pages,
        "info": info,
        "cover": parse_strip_cover(body),
        "url": f"https://e-hentai.org/g/{gid}/{token}/" if gid else "",
    }


def parse_detail_tags(body: str) -> list:
    """详情页标签：命名空间在 ``<div id="td_<ns>:<slug>">`` 里。

    ``#taglist`` 按 ``<tr>`` 分行、每行一个命名空间，但 ``gtl``（语言）与
    ``gtw``（权重）跟 ``gt`` 一样都是真标签，所以直接在整页里找 ``td_`` 开头的
    div 更稳，不必先框出 ``#taglist``。
    """
    tags = []
    seen = set()
    for match in _TAG_DIV_RE.finditer(body or ""):
        attrs, inner = match.group(1), match.group(2)
        id_match = _TAG_ID_RE.search(attrs)
        if not id_match:
            continue
        key = attr_text(id_match.group(1))
        if not key or key in seen:
            continue
        seen.add(key)
        namespace, _, slug = key.partition(":")
        cls_match = _TAG_CLASS_RE.search(attrs)
        cls = (cls_match.group(1) if cls_match else "").strip()
        href_match = _TAG_HREF_RE.search(inner)
        tags.append(
            {
                "namespace": namespace or "other",
                "tag": slug or namespace,
                # 显示名（可能是「pokemon | pocket monsters」这种双语）。
                "name": text(inner),
                "key": key,
                "class": cls,
                "kind": {"gtl": "language", "gtw": "weight"}.get(cls, "normal"),
                "url": attr_text(href_match.group(1)) if href_match else "",
            }
        )
    return tags


def parse_showing(body: str) -> dict:
    """``Showing 1 - 20 of 40 images`` → ``{"from":1,"to":20,"total":40}``。"""
    match = _GPC_RE.search(body or "")
    if not match:
        return {"text": "", "from": None, "to": None, "total": None}
    label = text(match.group(1))
    span = re.search(r"(\d+)\s*-\s*(\d+)\s*of\s*(\d+)", label)
    if not span:
        return {"text": label, "from": None, "to": None, "total": None}
    return {
        "text": label,
        "from": int(span.group(1)),
        "to": int(span.group(2)),
        "total": int(span.group(3)),
    }


def parse_page_links(body: str) -> list:
    """详情页里的 ``/s/`` 大图页链接（按出现顺序，一页 20 个）。"""
    return re.findall(
        r'href="(https://e-hentai\.org/s/[0-9a-fA-F]+/\d+-\d+)"', body or ""
    )


# 上游对「取不到的画廊」**不返回 404**，而是 200 + 一段几十字节的纯文本存根。
# 实测三种（括号里是字节数）：
#   ``Key missing, or incorrect key provided.``                          (39)
#   ``No gallery specified.``                                           (21)
#   ``Gallery not found. If you just added this gallery, you may have …`` (172)
# 光看状态码会把它们当成功页面，于是解析出一具空壳（title 空、page_links 空、
# showing.total 为 None）。用「有没有 #gn / #gdc / #gdd 这些详情页骨架」来判：
# 实测 31 张真实详情页（含 ?p= 后续屏）全部命中、3 个存根全部落空。
_DETAIL_MARK_RE = re.compile(r'id="(?:gn|gdc|gdd|gdn)"')

_GALLERY_STUB_PATTERNS = (
    ("key", re.compile(r"Key missing, or incorrect key provided", re.I)),
    ("no_gallery", re.compile(r"No gallery specified", re.I)),
    ("removed", re.compile(r"Gallery not found", re.I)),
)


def parse_gallery_error(body: str) -> tuple[str, str] | None:
    """这段响应是不是「取不到画廊」的存根？是就返回 ``(code, 原始文本)``。

    返回 ``None`` 表示这是一张正常的详情页（有 ``#gn`` / ``#gdc`` / ``#gdd``
    之类的骨架）。判据优先看骨架，其次才认那几句存根文字——这样万一上游改了
    文案，正常页面仍不会被误判成错误。
    """
    body = body or ""
    if _DETAIL_MARK_RE.search(body):
        return None
    for code, pattern in _GALLERY_STUB_PATTERNS:
        if pattern.search(body):
            return code, text(body)
    return None


def parse_s_page_error(body: str) -> tuple[str, str] | None:
    """``/s/`` 大图页的同一套存根（200 + ``Invalid page.``）。

    返回 ``None`` 表示页里有 ``<img id="img" …>``，是正常的。定义在此处只为紧
    挨着 ``parse_gallery_error``，用到的 ``_IMG_SRC_RE`` 在下面大图页一节里。
    """
    body = body or ""
    if _IMG_SRC_RE.search(body) or _IMG_SRC_RE2.search(body):
        return None
    for code, pattern in _S_STUB_PATTERNS:
        if pattern.search(body):
            return code, text(body)
    return None


# ---------------------------------------------------------------- 官方 API

# 上面三套是「HTML 存根」，这里是**第四条路径**：官方 ``api.php`` 的 gdata 把
# 错误放在条目里的 ``error`` 字段，HTTP 状态码仍然是 **200**。实测（2026-10）：
#   ``Key missing, or incorrect key provided.``   token 错或太短
#   ``Gallery not found. If you just added …``     gid 不存在（上游还把它夹到
#                                                  ``2147483647``）
# 只看状态码会把它当成功返回，调用方拿到一条什么字段都没有的空壳 —— 和当初
# HTML 路径那个坑一模一样（见 §20），只是换了传输方式。
_GDATA_STUB_PATTERNS = (
    ("key", re.compile(r"Key missing, or incorrect key provided", re.I)),
    ("removed", re.compile(r"Gallery not found", re.I)),
)


def parse_gdata_error(entry) -> tuple[str, str] | None:
    """这条 gdata 条目是不是「取不到」？是就返回 ``(code, 原始文本)``。

    ``error`` 缺字段 / 是 ``None`` / 是空串都算**正常条目**（返回 ``None``）。
    """
    if not isinstance(entry, Mapping):
        return None
    raw = entry.get("error")
    if not isinstance(raw, str) or not raw.strip():
        return None
    raw = raw.strip()
    for code, pattern in _GDATA_STUB_PATTERNS:
        if pattern.search(raw):
            return code, raw
    # 上游哪天换文案也别当成功：只要有非空的 error，就是取不到。
    return "error", raw


# ---------------------------------------------------------------- 大图页

# ``/s/`` 大图页也有同一套路：组合不对时 200 + ``Invalid page.``（13 字节）。
# 正常页一定有 ``<img id="img" …>``，所以判据就是那张图在不在。
_S_STUB_PATTERNS = (
    ("invalid", re.compile(r"Invalid page", re.I)),
    ("key", re.compile(r"Key missing, or incorrect key provided", re.I)),
)

_IMG_SRC_RE = re.compile(r'<img\b[^>]*id="img"[^>]*src="([^"]+)"', re.S)
_IMG_SRC_RE2 = re.compile(r'<img\b[^>]*src="([^"]+)"[^>]*id="img"', re.S)
_I1_TITLE_RE = re.compile(r'<div[^>]*id="i1"[^>]*>\s*<h1[^>]*>(.*?)</h1>', re.S)
_I2_NAV_RE = re.compile(r"<span>(\d+)</span>\s*/\s*<span>(\d+)</span>")
_I2_META_RE = re.compile(
    r"<div>\s*([^<]*?)\s*::\s*(\d+)\s*x\s*(\d+)\s*::\s*([^<]*?)\s*</div>", re.S
)
_GID_RE = re.compile(r"var\s+gid\s*=\s*(\d+)")


def parse_s_page(body: str) -> dict:
    """``/s/<key>/<gid>-<n>`` 大图页 → 图片直链与翻页信息。

    真实图片在 ``<img id="img" src="https://<rand>.<rand>.hath.network:<高端口>/h/…">``。
    **这个节点是随机的、而且常常是死的**（实测约 1/3 取不到），调用方要按
    「换一个 Cloudflare 边缘 IP 重新取这一页」来重试 —— 换边缘会让 H@H 返回
    另一个节点，见 ``client.EhClient``。
    """
    body = body or ""
    image = _IMG_SRC_RE.search(body) or _IMG_SRC_RE2.search(body)
    nav = _I2_NAV_RE.search(body)
    meta = _I2_META_RE.search(body)
    gid = _GID_RE.search(body)

    width = to_int(meta.group(2)) if meta else None
    height = to_int(meta.group(3)) if meta else None

    return {
        "image_url": attr_text(image.group(1)) if image else "",
        "title": text(_I1_TITLE_RE.search(body).group(1)) if _I1_TITLE_RE.search(body) else "",
        "filename": text(meta.group(1)) if meta else "",
        "width": width,
        "height": height,
        "size": text(meta.group(4)) if meta else "",
        "current": to_int(nav.group(1)) if nav else None,
        "total": to_int(nav.group(2)) if nav else None,
        "gid": gid.group(1) if gid else "",
        "prev": _nav_href(body, "prev"),
        "next": _nav_href(body, "next"),
    }


def parse_strip_cover(body: str) -> str:
    """详情页条带里的第一张图（= 画廊封面，挂在 hath.network 上，可代转）。"""
    match = _STRIP_RE.search(body or "")
    return attr_text(match.group(1)) if match else ""
