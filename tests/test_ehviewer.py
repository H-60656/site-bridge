"""ehviewer 子项目的单元测试。

与 ``tests/test_asmr_one.py`` / ``tests/test_iwara.py`` 同一套约定，**全部离线**：
解析函数直接喂真实页面片段，传输层把会话打桩，路由用假客户端顶替
``app.state.eh_client``，不打任何真实请求。

ehviewer 依赖 ``curl_cffi``（libcurl + BoringSSL，且必须带 HTTP/3）而它只写在
``ehviewer/requirements.txt`` 里 —— 新克隆的仓库还没跑过 ``ehviewer/run.py`` 时
会缺这个包。所以整个模块在缺包时**整片跳过**，而不是让测试红掉：装齐依赖
（``pip install -r ehviewer/requirements.txt``）后才会真正执行。

页面片段全部按 2026-10 的真实 markup 抄写（见 ``ehviewer/parse.py`` 的模块说明）：
这里刻意保留 ``&nbsp;``、``&#039;``、``gtl`` / ``gtw`` 分类、``id="prev"`` 这种
容易写错的细节，因为它们正是踩过的坑。
"""

import asyncio
import json
import os
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

from fastapi.testclient import TestClient

from pixiv_bridge.config import Settings
from pixiv_bridge.server import create_bridge_app as _create_bridge_app

PROJECT_ROOT = Path(__file__).resolve().parent.parent

try:
    from curl_cffi.const import CurlHttpVersion, CurlOpt

    from ehviewer import parse as eh_parse
    from ehviewer.app import EH_NAV, EH_ROUTERS, EH_STATE_KEYS, SITE
    from ehviewer.app import create_app as create_eh_app
    from ehviewer.app import eh_state_factory
    from ehviewer.client import (
        API_URL,
        BASE_URL,
        EDGE_IPS,
        MEDIA_HOST,
        PAGE_SIZE,
        EhAuthError,
        EhClient,
        EhError,
        EhNotFoundError,
        EhUnavailableError,
        is_allowed_proxy,
        is_eh_host,
        is_image_page,
        is_media_host,
        is_media_url,
        parse_gallery_path,
        split_ips,
    )
    from ehviewer.config import EhSettings, as_eh_settings
    from ehviewer.envcheck import DEPS_NAME, EDGE_NAME, SITE_NAME

    HAVE_EH = True
    EH_IMPORT_ERROR = ""
except ImportError as exc:  # pragma: no cover - 只在没装 curl_cffi 时走到
    HAVE_EH = False
    EH_IMPORT_ERROR = str(exc)

SKIP_REASON = "需要 curl_cffi：pip install -r ehviewer/requirements.txt"


def make_settings():
    # ehviewer 站点只在框架 Settings 上加了 http3 / 边缘池 / 重试次数；
    # 这里给一个纯框架 Settings，让 as_eh_settings 去补默认值。
    return Settings(rate_limit_rpm=10000)


# ---------------------------------------------------------------- 真实页面片段

# 列表页一行：分类列是 gl1c、缩略图列是 gl2c、画廊行才是 gl3c glname。
LIST_ROW = (
    "<tr>"
    '<td class="gl1c glcat"><div class="cn ct1" '
    "onclick=\"document.location='https://e-hentai.org/misc'\">Misc</div></td>"
    '<td class="gl2c"><div class="glthumb" id="it4242037"><div>'
    '<img style="height:250px;width:250px" src="https://ehgt.org/w/02/703/78918-n6q03g9i.webp" />'
    "</div></div></td>"
    '<td class="gl3c glname" onmouseover="show_image_pane(4242037)">'
    '<a href="https://e-hentai.org/g/4242037/8241dd44b0/">'
    '<div class="glink">Pinterest</div>'
    '<div><div class="gt" title="language:japanese">japanese</div>'
    '<div class="gt" title="parody:galaxy express 999">galaxy express 999</div></div>'
    "</a></td>"
    '<td class="gl4c"><a href="https://e-hentai.org/uploader/Dorttyy">Dorttyy</a></td>'
    '<td class="gl3c"><div id="posted_4242037">2026-10-09 12:04</div>'
    "<div>500 pages</div></td>"
    "</tr>"
)

# 第二行故意不含缩略图与上传者：真页面上这两列是可能缺的，不该把整行丢掉。
LIST_ROW_SPARSE = (
    "<tr>"
    '<td class="gl1c glcat"><div class="cn ct1">Doujinshi</div></td>'
    '<td class="gl3c glname"><a href="https://e-hentai.org/g/4242038/AAAA1111bb/">'
    '<div class="glink">Sparse Row</div></a></td>'
    "</tr>"
)

LIST_BODY = (
    "<html><body><table>"
    + LIST_ROW
    + LIST_ROW_SPARSE
    + "</table>"
    '<div class="searchtext"><p>Found 1,658,228 results. </p></div>'
    '<a id="dnext" href="https://e-hentai.org/?next=4242034">Next</a>'
    "</body></html>"
)

# 第二页：dprev 与 dnext 同时出现。
LIST_BODY_PAGE2 = (
    "<html><body><table>" + LIST_ROW + "</table>"
    '<a id="dprev" href="https://e-hentai.org/?prev=4242010">Prev</a>'
    '<a id="dnext" href="https://e-hentai.org/?next=4241985">Next</a>'
    "</body></html>"
)

# 搜索结果页：数字前多一个可选的 about，且末页的 dnext 是 <span> 不是 <a>。
SEARCH_BODY = (
    "<html><body><table>" + LIST_ROW + "</table>"
    '<p>Found about 3,203 results. </p>'
    '<span id="dnext">Next</span>'
    "</body></html>"
)

DETAIL_BODY = (
    "<html><body>"
    '<h1 id="gn">[Valytria] Under Courtney&#039;s Feet [Pok&#233;mon]</h1>'
    '<h1 id="gj"></h1>'
    '<div id="gdc"><div class="cs ct1" '
    "onclick=\"document.location='https://e-hentai.org/misc'\">Misc</div></div>"
    '<div id="gdn"><a href="https://e-hentai.org/uploader/human123123123">'
    "human123123123</a>&nbsp; <a href=\"https://forums.e-hentai.org/index.php?"
    'showuser=5941774">PM</a></div>'
    '<div id="gdd"><table>'
    '<tr><td class="gdt1">Posted:</td><td class="gdt2">2026-10-08 17:40</td></tr>'
    '<tr><td class="gdt1">Parent:</td><td class="gdt2">None</td></tr>'
    '<tr><td class="gdt1">Visible:</td><td class="gdt2">Yes</td></tr>'
    '<tr><td class="gdt1">Language:</td><td class="gdt2">Japanese &nbsp;</td></tr>'
    '<tr><td class="gdt1">File Size:</td><td class="gdt2">153.3 MiB</td></tr>'
    '<tr><td class="gdt1">Length:</td><td class="gdt2">40 pages</td></tr>'
    "</table></div>"
    '<div id="taglist"><table>'
    "<tr><td><div id=\"td_parody:pokemon\" class=\"gt\" style=\"opacity:1.0\">"
    '<a id="ta_parody:pokemon" href="https://e-hentai.org/tag/parody:pokemon">'
    "pokemon | pocket monsters</a></div></td></tr>"
    "<tr><td><div id=\"td_female:footjob\" class=\"gtl\" style=\"opacity:1.0\">"
    '<a id="ta_female:footjob" href="https://e-hentai.org/tag/female:footjob">'
    "footjob</a></div>"
    "<div id=\"td_female:sole_female\" class=\"gtw\" style=\"opacity:1.0\">"
    '<a id="ta_female:sole_female" href="https://e-hentai.org/tag/female:sole+female">'
    "sole female</a></div></td></tr>"
    "</table></div>"
    '<span id="rating_count">17</span>'
    '<td id="rating_label" colspan="3">Average: 3.86</td>'
    '<p class="gpc">Showing 1 - 20 of 40 images</p>'
    '<div style="background:url('
    "https://zoycbewnml.hath.network/c2/18u5edu0b3926119ce/4240495-0.webp)\"></div>"
    '<div style="background:url('
    "https://zoycbewnml.hath.network/c2/18u5edu0b3926119ce/4240495-0.webp)\"></div>"
    '<a href="https://e-hentai.org/s/b73be0a953/4240495-1">1</a>'
    '<a href="https://e-hentai.org/s/b6062cc00f/4240495-2">2</a>'
    '<a href="https://e-hentai.org/g/4240495/8bcad20a1a/">Canonical</a>'
    "</body></html>"
)

# /s/ 大图页：翻页锚是**短 id**（prev / next），不是列表页那套 dprev / dnext。
S_PAGE_BODY = (
    "<html><body>"
    '<div id="i1"><h1>AdminCourP_3</h1></div>'
    '<div id="i2">'
    '<a id="prev" onclick="return load_image(2, \'d2e31c9ac9\')" '
    'href="https://e-hentai.org/s/d2e31c9ac9/4240495-2">Prev</a>'
    '<a id="next" onclick="return load_image(4, \'b6062cc00f\')" '
    'href="https://e-hentai.org/s/b6062cc00f/4240495-4">Next</a>'
    "</div>"
    '<div id="i3"><a href="https://e-hentai.org/s/b6062cc00f/4240495-4">'
    '<img id="img" src="https://nptmsbi.aelkzxnsorsu.hath.network:52881/h/'
    '3aca166a/keystamp=1791555300-197fbe7561/AdminCourP_3.webp" '
    'style="width:1280px;height:1920px" /></a></div>'
    '<div id="i4"><div>AdminCourP_3.png :: 1280 x 1920 :: 104.4 KiB</div></div>'
    "<div><span>3</span> / <span>40</span></div>"
    "<script>var gid = 4240495;</script>"
    "</body></html>"
)

IMAGE_URL = (
    "https://nptmsbi.aelkzxnsorsu.hath.network:52881/h/3aca166a/"
    "keystamp=1791555300-197fbe7561/AdminCourP_3.webp"
)
IMAGE_PAGE_URL = "https://e-hentai.org/s/b73be0a953/4240495-3"
GALLERY_TOKEN = "8bcad20a1a"


class _FakeResp:
    """``request()`` 用得到的那几个属性。"""

    def __init__(self, status_code=200, payload=None, headers=None, text="", content=b""):
        self.status_code = status_code
        self._payload = payload if payload is not None else {}
        self.headers = headers or {}
        self.text = text or json.dumps(self._payload)
        self.content = content
        self.closed = False

    def json(self):
        return self._payload

    async def aiter_content(self):
        yield self.content

    async def aclose(self):
        self.closed = True


# ---------------------------------------------------------------- 解析


@unittest.skipUnless(HAVE_EH, SKIP_REASON)
class EhParseTestCase(unittest.TestCase):
    """``ehviewer/parse.py``：纯正则解析，不发请求。"""

    def test_strip_and_text_normalise_entities_and_whitespace(self):
        self.assertEqual(eh_parse.strip_tags("<b>hi</b>"), "hi")
        # &#039; 变单引号，&nbsp; 变 \xa0 后被空白压缩吃掉。
        self.assertEqual(eh_parse.text("<div>a&nbsp; &#039;b&#039;</div>"), "a 'b'")
        self.assertEqual(eh_parse.text("  <i>x</i>\n  y "), "x y")

    def test_attr_text_keeps_plus_signs(self):
        # URL 里的 + 是标签地址的一部分，去空白会把 sole+female 弄坏。
        self.assertEqual(
            eh_parse.attr_text("https://e-hentai.org/tag/female:sole+female"),
            "https://e-hentai.org/tag/female:sole+female",
        )

    def test_to_int_and_to_float_pick_the_first_number(self):
        self.assertEqual(eh_parse.to_int("40 pages"), 40)
        self.assertEqual(eh_parse.to_int("12"), 12)
        self.assertIsNone(eh_parse.to_int("no digits"))
        self.assertIsNone(eh_parse.to_int(None, None))
        self.assertEqual(eh_parse.to_int(None, 7), 7)
        self.assertEqual(eh_parse.to_float("Average: 3.86"), 3.86)
        self.assertIsNone(eh_parse.to_float(""))

    # ---------- 列表页 ----------

    def test_galleries_are_split_on_glname_rows(self):
        items = eh_parse.parse_galleries(LIST_BODY)

        self.assertEqual(len(items), 2)
        first = items[0]
        self.assertEqual(first["gid"], 4242037)
        self.assertEqual(first["token"], "8241dd44b0")
        self.assertEqual(first["url"], "https://e-hentai.org/g/4242037/8241dd44b0/")
        self.assertEqual(first["title"], "Pinterest")
        self.assertEqual(first["category"], "Misc")
        self.assertEqual(first["uploader"], "Dorttyy")
        self.assertEqual(first["pages"], 500)
        self.assertEqual(first["posted"], "2026-10-09 12:04")
        self.assertEqual(first["thumbnail"], "https://ehgt.org/w/02/703/78918-n6q03g9i.webp")

    def test_row_tags_carry_the_full_namespace(self):
        tags = eh_parse.parse_galleries(LIST_BODY)[0]["tags"]

        self.assertEqual(
            [(t["namespace"], t["tag"]) for t in tags],
            [("language", "japanese"), ("parody", "galaxy express 999")],
        )
        self.assertEqual(tags[0]["key"], "language:japanese")

    def test_a_sparse_row_survives_without_thumbnail_or_uploader(self):
        item = eh_parse.parse_galleries(LIST_BODY)[1]

        self.assertEqual(item["gid"], 4242038)
        self.assertEqual(item["title"], "Sparse Row")
        self.assertEqual(item["category"], "Doujinshi")
        self.assertEqual(item["uploader"], "")
        self.assertEqual(item["thumbnail"], "")
        self.assertIsNone(item["pages"])
        self.assertEqual(item["tags"], [])

    def test_thumbnails_are_flagged_unusable(self):
        # ehgt.org 本机是 IP 级被封的，缩略图取不到 —— 解析沿用真实字段，
        # 但必须显式告诉调用方别拿它当代转源（封面走详情页 hath 条带）。
        for item in eh_parse.parse_galleries(LIST_BODY):
            self.assertFalse(item["thumbnail_usable"])

    def test_a_body_without_rows_yields_nothing(self):
        self.assertEqual(eh_parse.parse_galleries(""), [])
        self.assertEqual(eh_parse.parse_galleries("<table><tr><td>x</td></tr></table>"), [])

    def test_result_count_tolerates_the_optional_about(self):
        self.assertEqual(eh_parse.parse_result_count(LIST_BODY), "1,658,228")
        self.assertEqual(eh_parse.parse_result_count(SEARCH_BODY), "3,203")
        self.assertEqual(eh_parse.parse_result_count("nothing here"), "")

    def test_list_nav_reads_the_next_cursor(self):
        nav = eh_parse.parse_list_nav(LIST_BODY)

        self.assertEqual(nav["next"], "4242034")
        self.assertEqual(nav["next_url"], "https://e-hentai.org/?next=4242034")
        self.assertEqual(nav["prev"], "")
        self.assertEqual(nav["found"], "1,658,228")

    def test_list_nav_reads_both_directions_on_a_middle_page(self):
        nav = eh_parse.parse_list_nav(LIST_BODY_PAGE2)

        self.assertEqual(nav["next"], "4241985")
        self.assertEqual(nav["prev"], "4242010")

    def test_a_span_instead_of_an_anchor_means_last_page(self):
        # 末页那个位置是 <span id="dnext">，只匹配 <a> 才会正确地给出空串。
        self.assertEqual(eh_parse.parse_list_nav(SEARCH_BODY)["next"], "")

    def test_nav_href_accepts_either_attribute_order(self):
        swapped = '<a href="https://e-hentai.org/?next=99" id="dnext">N</a>'

        self.assertEqual(eh_parse.parse_list_nav(swapped)["next"], "99")

    # ---------- 详情页 ----------

    def test_detail_metadata_and_tags(self):
        data = eh_parse.parse_detail(DETAIL_BODY)

        self.assertEqual(data["gid"], 4240495)
        self.assertEqual(data["token"], GALLERY_TOKEN)
        self.assertEqual(data["title"], "[Valytria] Under Courtney's Feet [Pokémon]")
        self.assertEqual(data["title_jpn"], "")
        self.assertEqual(data["category"], "Misc")
        self.assertEqual(data["uploader"], "human123123123")
        self.assertEqual(data["posted"], "2026-10-08 17:40")
        self.assertEqual(data["parent"], "None")
        self.assertEqual(data["visible"], "Yes")
        self.assertEqual(data["language"], "Japanese")
        self.assertEqual(data["file_size"], "153.3 MiB")
        self.assertEqual(data["length"], "40 pages")
        self.assertEqual(data["pages"], 40)
        self.assertEqual(data["rating"], 3.86)
        self.assertEqual(data["rating_count"], 17)
        self.assertEqual(data["url"], f"https://e-hentai.org/g/4240495/{GALLERY_TOKEN}/")

    def test_detail_gdd_rows_are_keyed_by_name_not_position(self):
        info = eh_parse.parse_detail(DETAIL_BODY)["info"]

        self.assertEqual(info["Posted"], "2026-10-08 17:40")
        self.assertEqual(info["File Size"], "153.3 MiB")
        # 键名不带冒号，行序不固定也不影响取值。
        self.assertNotIn("Posted:", info)

    def test_detail_tags_map_gtl_and_gtw_to_kinds(self):
        tags = eh_parse.parse_detail_tags(DETAIL_BODY)
        by_key = {t["key"]: t for t in tags}

        self.assertEqual(by_key["parody:pokemon"]["kind"], "normal")
        self.assertEqual(by_key["female:footjob"]["kind"], "language")
        self.assertEqual(by_key["female:sole_female"]["kind"], "weight")
        self.assertEqual(by_key["parody:pokemon"]["name"], "pokemon | pocket monsters")
        self.assertEqual(by_key["female:sole_female"]["namespace"], "female")
        self.assertEqual(
            by_key["female:sole_female"]["url"],
            "https://e-hentai.org/tag/female:sole+female",
        )

    def test_detail_tags_are_deduplicated(self):
        doubled = DETAIL_BODY.replace(
            '<div id="td_parody:pokemon"', '<div id="td_parody:pokemon"', 1
        )
        keys = [t["key"] for t in eh_parse.parse_detail_tags(doubled)]

        self.assertEqual(len(keys), len(set(keys)))

    def test_cover_comes_from_the_first_hath_strip_image(self):
        # 条带里同一张 sprite 重复 20 次，只取第一张就够了。
        self.assertEqual(
            eh_parse.parse_detail(DETAIL_BODY)["cover"],
            "https://zoycbewnml.hath.network/c2/18u5edu0b3926119ce/4240495-0.webp",
        )

    def test_showing_parses_the_range(self):
        showing = eh_parse.parse_showing(DETAIL_BODY)

        self.assertEqual(showing["from"], 1)
        self.assertEqual(showing["to"], 20)
        self.assertEqual(showing["total"], 40)
        self.assertEqual(showing["text"], "Showing 1 - 20 of 40 images")

    def test_showing_degrades_gracefully(self):
        self.assertEqual(
            eh_parse.parse_showing("<html></html>"),
            {"text": "", "from": None, "to": None, "total": None},
        )
        odd = eh_parse.parse_showing('<p class="gpc">nothing to see</p>')
        self.assertIsNone(odd["total"])
        self.assertEqual(odd["text"], "nothing to see")

    def test_page_links_are_the_slash_s_urls(self):
        links = eh_parse.parse_page_links(DETAIL_BODY)

        self.assertEqual(
            links,
            [
                "https://e-hentai.org/s/b73be0a953/4240495-1",
                "https://e-hentai.org/s/b6062cc00f/4240495-2",
            ],
        )

    def test_detail_of_an_unrelated_body_is_empty_not_an_error(self):
        data = eh_parse.parse_detail("<html><body>nothing</body></html>")

        self.assertIsNone(data["gid"])
        self.assertEqual(data["title"], "")
        self.assertEqual(data["cover"], "")
        self.assertEqual(data["url"], "")
        self.assertEqual(data["tags"], [])

    # ---------- 大图页 ----------

    def test_s_page_reads_the_image_and_the_pager(self):
        page = eh_parse.parse_s_page(S_PAGE_BODY)

        self.assertEqual(page["image_url"], IMAGE_URL)
        self.assertEqual(page["title"], "AdminCourP_3")
        self.assertEqual(page["filename"], "AdminCourP_3.png")
        self.assertEqual(page["width"], 1280)
        self.assertEqual(page["height"], 1920)
        self.assertEqual(page["size"], "104.4 KiB")
        self.assertEqual(page["current"], 3)
        self.assertEqual(page["total"], 40)
        self.assertEqual(page["gid"], "4240495")

    def test_s_page_pager_uses_the_short_ids(self):
        # 这里用的是 id="prev" / id="next"；如果只认 dprev / dnext，
        # 大图页的翻页会永远是空（实测踩过这个坑）。
        page = eh_parse.parse_s_page(S_PAGE_BODY)

        self.assertEqual(page["prev"], "https://e-hentai.org/s/d2e31c9ac9/4240495-2")
        self.assertEqual(page["next"], "https://e-hentai.org/s/b6062cc00f/4240495-4")

    def test_s_page_without_an_image_returns_empty_url(self):
        page = eh_parse.parse_s_page("<div id='i3'></div>")

        self.assertEqual(page["image_url"], "")
        self.assertIsNone(page["current"])
        self.assertEqual(page["prev"], "")

    def test_strip_cover_helper_ignores_non_hath_urls(self):
        self.assertEqual(eh_parse.parse_strip_cover("url(https://ehgt.org/a.webp)"), "")
        self.assertEqual(
            eh_parse.parse_strip_cover(DETAIL_BODY),
            "https://zoycbewnml.hath.network/c2/18u5edu0b3926119ce/4240495-0.webp",
        )


# ---------------------------------------------------------------- 地址判定


@unittest.skipUnless(HAVE_EH, SKIP_REASON)
class EhClientAddressTestCase(unittest.TestCase):
    """主机/路径白名单：代转接口的安全边界就靠这几个纯函数。"""

    def test_is_eh_host(self):
        self.assertTrue(is_eh_host("e-hentai.org"))
        self.assertTrue(is_eh_host("api.e-hentai.org"))
        self.assertTrue(is_eh_host("E-HENTAI.ORG"))
        self.assertFalse(is_eh_host("e-hentai.org.evil.com"))
        self.assertFalse(is_eh_host("not-e-hentai.org"))
        self.assertFalse(is_eh_host(""))

    def test_is_media_host(self):
        self.assertTrue(is_media_host("hath.network"))
        self.assertTrue(is_media_host("nptmsbi.aelkzxnsorsu.hath.network"))
        self.assertFalse(is_media_host("ehgt.org"))
        self.assertFalse(is_media_host("hath.network.evil.com"))

    def test_parse_gallery_path(self):
        self.assertEqual(
            parse_gallery_path("https://e-hentai.org/g/1234/abcd/"), ("1234", "abcd")
        )
        self.assertEqual(
            parse_gallery_path("https://e-hentai.org/g/1234/ABCD"), ("1234", "ABCD")
        )
        self.assertIsNone(parse_gallery_path("https://e-hentai.org/g/1234/"))
        self.assertIsNone(parse_gallery_path("https://evil.com/g/1234/abcd/"))
        self.assertIsNone(parse_gallery_path(""))

    def test_is_image_page(self):
        self.assertTrue(is_image_page(IMAGE_PAGE_URL))
        self.assertTrue(is_image_page("https://e-hentai.org/s/abc123/42-1"))
        self.assertFalse(is_image_page("https://e-hentai.org/s/abc123/42"))  # 缺页码
        self.assertFalse(is_image_page("https://e-hentai.org/g/42/abc/"))
        self.assertFalse(is_image_page("ftp://e-hentai.org/s/abc/42-1"))

    def test_is_media_url(self):
        self.assertTrue(is_media_url(IMAGE_URL))
        self.assertFalse(is_media_url("https://ehgt.org/a.webp"))
        self.assertFalse(is_media_url("file:///c:/a.png"))

    def test_allowed_proxy_is_only_the_gallery_pages_and_hah(self):
        self.assertTrue(is_allowed_proxy(IMAGE_PAGE_URL))
        self.assertTrue(is_allowed_proxy(IMAGE_URL))
        # 任意代理是这里最要紧的不变量。
        self.assertFalse(is_allowed_proxy("https://e-hentai.org/"))
        self.assertFalse(is_allowed_proxy("https://example.com/secret"))
        self.assertFalse(is_allowed_proxy("http://127.0.0.1:8080/admin"))

    def test_split_ips_accepts_comma_semicolon_and_space(self):
        self.assertEqual(split_ips("1.1.1.1, 2.2.2.2;3.3.3.3"), ("1.1.1.1", "2.2.2.2", "3.3.3.3"))
        self.assertEqual(split_ips("  1.1.1.1 \t 2.2.2.2 "), ("1.1.1.1", "2.2.2.2"))
        self.assertEqual(split_ips(""), ())

    def test_the_default_edge_pool_excludes_the_sites_own_ips(self):
        # e-hentai.org 自己解析出的那两个 CF IP 在本机被 IP 级 RST，借道池
        # 里放它们等于把「换边缘重试」变成必然失败。
        self.assertNotIn("172.66.132.196", EDGE_IPS)
        self.assertNotIn("172.66.140.62", EDGE_IPS)
        self.assertGreaterEqual(len(EDGE_IPS), 8)


# ---------------------------------------------------------------- 传输层


@unittest.skipUnless(HAVE_EH, SKIP_REASON)
class EhClientEdgeTestCase(unittest.TestCase):
    """边缘探测、轮转、摘除与会话钉 IP。"""

    def _client(self, edges=("1.1.1.1", "2.2.2.2"), **kw):
        return EhClient(edge_ips=edges, **kw)

    def test_the_client_falls_back_to_the_default_pool(self):
        self.assertEqual(EhClient().edge_ips, EDGE_IPS)

    def test_probe_edge_is_a_plain_tcp_connect(self):
        writer = MagicMock()
        writer.wait_closed = AsyncMock()
        conn = AsyncMock(return_value=(MagicMock(), writer))
        client = self._client()

        with patch("ehviewer.client.asyncio.open_connection", conn):
            ok = asyncio.run(client.probe_edge("1.1.1.1"))

        self.assertTrue(ok)
        conn.assert_awaited_once_with("1.1.1.1", 443)
        writer.close.assert_called_once()

    def test_probe_edge_reports_a_refused_connection(self):
        conn = AsyncMock(side_effect=OSError("refused"))
        client = self._client()

        with patch("ehviewer.client.asyncio.open_connection", conn):
            self.assertFalse(asyncio.run(client.probe_edge("1.1.1.1")))

    def test_edges_probes_once_then_caches(self):
        client = self._client(edges=("1.1.1.1", "2.2.2.2", "3.3.3.3"))
        client.probe_edge = AsyncMock(side_effect=[True, False, True])

        first = asyncio.run(client.edges())
        second = asyncio.run(client.edges())

        self.assertEqual(first, ["1.1.1.1", "3.3.3.3"])
        self.assertEqual(second, first)
        self.assertEqual(client.probe_edge.await_count, 3, "第二次不该再探")

    def test_edges_raises_when_the_pool_is_all_dead(self):
        client = self._client(edges=("1.1.1.1",))
        client.probe_edge = AsyncMock(return_value=False)

        with self.assertRaises(EhUnavailableError):
            asyncio.run(client.edges())

    def test_next_edge_rotates_instead_of_repeating(self):
        # 换边缘是「拿新图床节点」的唯一杠杆，所以必须轮转。
        client = self._client(edges=("1.1.1.1", "2.2.2.2"))

        self.assertEqual(
            [client.next_edge(["1.1.1.1", "2.2.2.2"]) for _ in range(4)],
            ["1.1.1.1", "2.2.2.2", "1.1.1.1", "2.2.2.2"],
        )

    def test_mark_edge_dead_removes_it_but_keeps_the_last_one(self):
        client = self._client(edges=("1.1.1.1", "2.2.2.2"))
        client._good_edges = ["1.1.1.1", "2.2.2.2"]

        client.mark_edge_dead("1.1.1.1")
        self.assertEqual(client._good_edges, ["2.2.2.2"])

        client.mark_edge_dead("2.2.2.2")
        self.assertEqual(client._good_edges, ["2.2.2.2"], "只剩一个时不能摘空")

    def test_mark_edge_dead_drops_the_sessions_pinned_to_it(self):
        client = self._client(edges=("1.1.1.1", "2.2.2.2"))
        client._good_edges = ["1.1.1.1", "2.2.2.2"]
        client._sessions[("e-hentai.org", "1.1.1.1")] = MagicMock()
        client._sessions[("e-hentai.org", "2.2.2.2")] = MagicMock()

        client.mark_edge_dead("1.1.1.1")

        self.assertEqual(list(client._sessions), [("e-hentai.org", "2.2.2.2")])

    def test_session_for_pins_the_ip_and_enables_http3(self):
        maker = MagicMock(return_value=MagicMock())
        client = self._client(edges=("9.9.9.9",), http3=True)

        with patch("ehviewer.client.curl_requests.AsyncSession", maker):
            first = asyncio.run(client.session_for("e-hentai.org", "9.9.9.9"))
            second = asyncio.run(client.session_for("e-hentai.org", "9.9.9.9"))

        self.assertIs(first, second, "同一 (host, ip) 复用会话")
        self.assertEqual(maker.call_count, 1)
        options = maker.call_args.kwargs["curl_options"]
        # RESOLVE 只认一条记录，所以是「钉死单个 IP」而不是候选列表。
        self.assertEqual(options[CurlOpt.RESOLVE], ["e-hentai.org:443:9.9.9.9"])
        self.assertEqual(options[CurlOpt.HTTP_VERSION], CurlHttpVersion.V3)

    def test_session_for_can_skip_http3(self):
        maker = MagicMock(return_value=MagicMock())
        client = self._client(edges=("9.9.9.9",), http3=False)

        with patch("ehviewer.client.curl_requests.AsyncSession", maker):
            asyncio.run(client.session_for("e-hentai.org", "9.9.9.9"))

        options = maker.call_args.kwargs["curl_options"]
        self.assertNotIn(CurlOpt.HTTP_VERSION, options)
        self.assertIn(CurlOpt.CONNECTTIMEOUT, options)

    def test_the_plain_session_is_not_pinned(self):
        maker = MagicMock(return_value=MagicMock())
        client = self._client()

        with patch("ehviewer.client.curl_requests.AsyncSession", maker):
            session = client.plain_session
            again = client.plain_session

        self.assertIs(session, again)
        self.assertEqual(maker.call_count, 1)
        options = maker.call_args.kwargs["curl_options"]
        self.assertNotIn(CurlOpt.RESOLVE, options)
        self.assertNotIn(CurlOpt.HTTP_VERSION, options)


@unittest.skipUnless(HAVE_EH, SKIP_REASON)
class EhClientRequestTestCase(unittest.TestCase):
    """``request`` 的换边缘重试、``_get_html`` 的状态映射与各 API 的取数。"""

    def _client(self, edges=("1.1.1.1", "2.2.2.2"), **kw):
        client = EhClient(edge_ips=tuple(edges), **kw)
        client._good_edges = list(edges)
        return client

    def _pinned(self, client, behaviours):
        """把 ``session_for`` 换成按 IP 决定行为的假会话。

        ``behaviours`` 是 ``{ip: 结果}``；结果是 ``_FakeResp``、异常实例或
        ``"dead"``（表示这条会话直接抛异常）。返回被访问过的 IP 列表。
        """
        seen = []

        class _Session:
            def __init__(self, ip):
                self.ip = ip

            async def request(self, method, url, **kw):
                seen.append(self.ip)
                outcome = behaviours[self.ip]
                if isinstance(outcome, Exception):
                    raise outcome
                return outcome

        async def session_for(host, ip):
            return _Session(ip)

        client.session_for = session_for
        return seen

    def test_request_tries_the_next_edge_after_a_transport_failure(self):
        client = self._client()
        seen = self._pinned(client, {"1.1.1.1": OSError("rst"), "2.2.2.2": _FakeResp(200, text="ok")})

        resp = asyncio.run(client.request("GET", "https://e-hentai.org/"))

        self.assertEqual(resp.text, "ok")
        self.assertEqual(seen, ["1.1.1.1", "2.2.2.2"])
        self.assertNotIn("1.1.1.1", client._good_edges)

    def test_request_marks_an_edge_dead_on_a_retry_status(self):
        client = self._client()
        seen = self._pinned(client, {"1.1.1.1": _FakeResp(403), "2.2.2.2": _FakeResp(200, text="ok")})

        resp = asyncio.run(
            client.request("GET", "https://e-hentai.org/", retry_statuses=(403,))
        )

        self.assertEqual(resp.status_code, 200)
        self.assertEqual(seen, ["1.1.1.1", "2.2.2.2"])

    def test_request_gives_up_after_the_configured_attempts(self):
        client = self._client(edges=("1.1.1.1",), edge_attempts=3)
        seen = self._pinned(client, {"1.1.1.1": OSError("rst")})

        with self.assertRaises(EhUnavailableError) as ctx:
            asyncio.run(client.request("GET", "https://e-hentai.org/"))

        self.assertEqual(len(seen), 3)
        self.assertIn("3", str(ctx.exception))

    def test_non_eh_hosts_go_straight_through_the_plain_session(self):
        # H@H 图片节点不在 Cloudflare 上，借道与钉 IP 都没有意义。
        client = self._client()
        client._good_edges = []
        plain = MagicMock()
        plain.request = AsyncMock(return_value=_FakeResp(200, content=b"img"))
        client._plain = plain

        resp = asyncio.run(client.request("GET", IMAGE_URL))

        self.assertEqual(resp.status_code, 200)
        plain.request.assert_awaited_once()
        self.assertEqual(plain.request.await_args.args[1], IMAGE_URL)

    # ---------- _get_html 的状态映射 ----------

    def _html_client(self, resp):
        client = self._client(edges=())
        client.request = AsyncMock(return_value=resp)
        return client

    def test_get_html_maps_401_to_auth_error(self):
        client = self._html_client(_FakeResp(401))

        with self.assertRaises(EhAuthError) as ctx:
            asyncio.run(client._get_html("https://e-hentai.org/"))

        self.assertEqual(ctx.exception.status, 401)

    def test_get_html_maps_404_to_not_found(self):
        client = self._html_client(_FakeResp(404))

        with self.assertRaises(EhNotFoundError):
            asyncio.run(client._get_html("https://e-hentai.org/x"))

    def test_get_html_keeps_other_statuses(self):
        client = self._html_client(_FakeResp(503))

        with self.assertRaises(EhError) as ctx:
            asyncio.run(client._get_html("https://e-hentai.org/"))

        self.assertEqual(ctx.exception.status, 503)

    # ---------- 列表 / 搜索 ----------

    def test_list_galleries_uses_a_cursor_not_a_page_number(self):
        client = self._html_client(_FakeResp(200, text=LIST_BODY))

        body = asyncio.run(client.list_galleries())

        self.assertEqual(body["count"], 2)
        self.assertEqual(body["found"], "1,658,228")
        self.assertEqual(body["next"], "4242034")
        self.assertEqual(body["cursor"], "")
        # 首页不带任何查询参数。
        self.assertIsNone(client.request.await_args.kwargs["params"])

    def test_list_galleries_sends_next_and_f_search(self):
        client = self._html_client(_FakeResp(200, text=LIST_BODY))

        asyncio.run(client.list_galleries(cursor="4242034", query="lolicon"))

        params = client.request.await_args.kwargs["params"]
        self.assertEqual(params, {"f_search": "lolicon", "next": "4242034"})

    def test_search_rejects_an_empty_query_before_any_request(self):
        client = self._html_client(_FakeResp(200, text=LIST_BODY))

        with self.assertRaises(EhError) as ctx:
            asyncio.run(client.search("   "))

        self.assertEqual(ctx.exception.status, 400)
        client.request.assert_not_awaited()

    # ---------- 详情 / 大图页 ----------

    def test_gallery_builds_the_url_and_decorates_it(self):
        client = self._html_client(_FakeResp(200, text=DETAIL_BODY))

        data = asyncio.run(client.gallery(4240495, GALLERY_TOKEN))

        url = client.request.await_args.args[1]
        self.assertEqual(url, f"https://e-hentai.org/g/{4240495}/{GALLERY_TOKEN}/")
        self.assertEqual(data["gid"], 4240495)
        self.assertEqual(data["page"], 1)
        self.assertEqual(data["page_size"], PAGE_SIZE)
        self.assertEqual(data["showing"]["total"], 40)
        self.assertEqual(len(data["page_links"]), 2)

    def test_gallery_page_two_uses_the_zero_based_p(self):
        client = self._html_client(_FakeResp(200, text=DETAIL_BODY))

        asyncio.run(client.gallery(4240495, GALLERY_TOKEN, page=2))

        # 上游详情页用 0 基的 p，所以第 2 屏是 ?p=1。
        self.assertEqual(
            client.request.await_args.args[1],
            f"https://e-hentai.org/g/{4240495}/{GALLERY_TOKEN}/?p=1",
        )

    def test_gallery_validates_its_inputs(self):
        client = self._html_client(_FakeResp(200, text=DETAIL_BODY))

        with self.assertRaises(EhError):
            asyncio.run(client.gallery("not-a-number", GALLERY_TOKEN))
        with self.assertRaises(EhError):
            asyncio.run(client.gallery(1, " "))
        client.request.assert_not_awaited()

    def test_gallery_falls_back_to_the_requested_ids(self):
        # 详情页里没有 canonical 锚时，用调用方给的 gid/token 兜底。
        client = self._html_client(_FakeResp(200, text="<html></html>"))

        data = asyncio.run(client.gallery(99, "aabbccdd"))

        self.assertEqual(data["gid"], 99)
        self.assertEqual(data["token"], "aabbccdd")

    def test_image_page_refuses_a_non_image_url(self):
        client = self._html_client(_FakeResp(200, text=S_PAGE_BODY))

        with self.assertRaises(EhError) as ctx:
            asyncio.run(client.image_page("https://e-hentai.org/g/1/aa/"))

        self.assertEqual(ctx.exception.status, 400)
        client.request.assert_not_awaited()

    def test_image_page_returns_the_direct_link(self):
        client = self._html_client(_FakeResp(200, text=S_PAGE_BODY))

        page = asyncio.run(client.image_page(IMAGE_PAGE_URL))

        self.assertEqual(page["image_url"], IMAGE_URL)
        self.assertEqual(page["url"], IMAGE_PAGE_URL)
        self.assertEqual(page["current"], 3)

    def test_image_page_without_a_link_is_a_502(self):
        client = self._html_client(_FakeResp(200, text="<div id='i3'></div>"))

        with self.assertRaises(EhError) as ctx:
            asyncio.run(client.image_page(IMAGE_PAGE_URL))

        self.assertEqual(ctx.exception.status, 502)


@unittest.skipUnless(HAVE_EH, SKIP_REASON)
class EhClientProxyTestCase(unittest.TestCase):
    """字节代理、重解析重试与官方 gdata。"""

    def _client(self, edges=("1.1.1.1",), **kw):
        client = EhClient(edge_ips=tuple(edges), **kw)
        client._good_edges = list(edges)
        return client

    def test_open_stream_forwards_the_selected_headers_and_closes(self):
        client = self._client()
        resp = _FakeResp(
            206,
            headers={
                "content-type": "image/webp",
                "content-length": "4096",
                "content-range": "bytes 0-4095/8192",
                "accept-ranges": "bytes",
                "etag": "abc",
                "last-modified": "yesterday",
                "x-should-not-leak": "nope",
            },
            content=b"WEBP",
        )
        client.request = AsyncMock(return_value=resp)

        status, headers, body = asyncio.run(client.open_stream(IMAGE_URL))

        self.assertEqual(status, 206)
        self.assertEqual(headers["content-type"], "image/webp")
        self.assertEqual(headers["content-range"], "bytes 0-4095/8192")
        self.assertNotIn("x-should-not-leak", headers)
        self.assertTrue(client.request.await_args.kwargs["stream"])

        chunks = asyncio.run(_collect(body))
        self.assertEqual(b"".join(chunks), b"WEBP")
        self.assertTrue(resp.closed, "流读完后要关掉上游响应")

    def test_open_stream_refuses_a_non_hah_url(self):
        client = self._client()

        with self.assertRaises(EhError) as ctx:
            asyncio.run(client.open_stream("https://e-hentai.org/"))

        self.assertEqual(ctx.exception.status, 400)

    def test_open_proxy_passes_a_direct_hah_link_straight_through(self):
        client = self._client()
        client.open_stream = AsyncMock(return_value=(200, {"content-type": "image/webp"}, _empty()))

        status, headers, _ = asyncio.run(client.open_proxy(IMAGE_URL))

        self.assertEqual(status, 200)
        client.open_stream.assert_awaited_once()

    def test_open_proxy_re_resolves_the_s_page_instead_of_retrying_a_dead_url(self):
        # 死节点只能靠「换边缘重新取 /s/ 页」解决：H@H 会返回另一个节点。
        client = self._client(image_attempts=3)
        pages = [
            {"image_url": "https://ehgt.org/dead.webp"},  # 不在 H@H 上 -> 再来一次
            {"image_url": IMAGE_URL},
            {"image_url": IMAGE_URL},
        ]
        client.image_page = AsyncMock(side_effect=pages)
        client.open_stream = AsyncMock(
            side_effect=[OSError("node died"), (206, {"content-type": "image/webp"}, _empty())]
        )

        status, headers, _ = asyncio.run(client.open_proxy(IMAGE_PAGE_URL))

        self.assertEqual(status, 206)
        self.assertEqual(client.image_page.await_count, 3)
        self.assertEqual(client.open_stream.await_count, 2)
        self.assertEqual(headers["x-eh-page-url"], IMAGE_PAGE_URL)
        self.assertEqual(headers["x-eh-image-url"], IMAGE_URL)

    def test_open_proxy_gives_up_after_the_configured_attempts(self):
        client = self._client(image_attempts=2)
        client.image_page = AsyncMock(return_value={"image_url": IMAGE_URL})
        client.open_stream = AsyncMock(side_effect=OSError("always dead"))

        with self.assertRaises(EhUnavailableError) as ctx:
            asyncio.run(client.open_proxy(IMAGE_PAGE_URL))

        self.assertIn("2", str(ctx.exception))
        self.assertEqual(client.image_page.await_count, 2)

    def test_open_proxy_forwards_range_but_not_as_referer(self):
        client = self._client()
        client.image_page = AsyncMock(return_value={"image_url": IMAGE_URL})
        client.open_stream = AsyncMock(return_value=(206, {}, _empty()))

        asyncio.run(client.open_proxy(IMAGE_PAGE_URL, {"range": "bytes=0-99"}))

        forwarded = client.open_stream.await_args.args[1]
        self.assertEqual(forwarded["range"], "bytes=0-99")
        self.assertEqual(forwarded["referer"], IMAGE_PAGE_URL)

    def test_gdata_posts_a_json_body_as_bytes(self):
        client = self._client()
        client.request = AsyncMock(return_value=_FakeResp(200, payload={"gmetadata": [{"gid": 1}]}))

        body = asyncio.run(client.gdata([(4240495, GALLERY_TOKEN)]))

        self.assertEqual(body["gmetadata"][0]["gid"], 1)
        args = client.request.await_args
        self.assertEqual(args.args[0], "POST")
        self.assertEqual(args.args[1], API_URL)
        payload = args.kwargs["data"]
        self.assertIsInstance(payload, bytes, "CF 只认 bytes + application/json")
        decoded = json.loads(payload.decode())
        self.assertEqual(decoded["method"], "gdata")
        self.assertEqual(decoded["namespace"], 1)
        self.assertEqual(decoded["gidlist"], [[4240495, GALLERY_TOKEN]])
        self.assertEqual(args.kwargs["headers"]["content-type"], "application/json")

    def test_gdata_skips_malformed_entries_and_needs_at_least_one(self):
        client = self._client()
        client.request = AsyncMock(return_value=_FakeResp(200, payload={}))

        asyncio.run(client.gdata([(1, "aa"), None, "junk"]))

        decoded = json.loads(client.request.await_args.kwargs["data"].decode())
        self.assertEqual(decoded["gidlist"], [[1, "aa"]])

        client.request.reset_mock()
        with self.assertRaises(EhError) as ctx:
            asyncio.run(client.gdata([]))
        self.assertEqual(ctx.exception.status, 400)
        client.request.assert_not_awaited()

    def test_gdata_surfaces_an_http_error(self):
        client = self._client()
        client.request = AsyncMock(return_value=_FakeResp(503, text="busy"))

        with self.assertRaises(EhError) as ctx:
            asyncio.run(client.metadata(1, "aa"))

        self.assertEqual(ctx.exception.status, 503)

    def test_gdata_rejects_a_non_json_body(self):
        client = self._client()
        bad = _FakeResp(200, text="<html>nope</html>")
        bad.json = MagicMock(side_effect=ValueError("not json"))
        client.request = AsyncMock(return_value=bad)

        with self.assertRaises(EhError) as ctx:
            asyncio.run(client.metadata(1, "aa"))

        self.assertIn("JSON", str(ctx.exception))

    def test_auth_status_reports_the_pinned_edge(self):
        client = self._client(edges=("1.1.1.1", "2.2.2.2"))

        body = asyncio.run(client.auth_status())

        self.assertEqual(body["site"], "e-hentai.org")
        self.assertTrue(body["anonymous"])
        self.assertEqual(body["edge_count"], 2)
        self.assertIn(body["pinned_edge"], ("1.1.1.1", "2.2.2.2"))

    def test_aclose_closes_every_session_exactly_once(self):
        client = self._client()
        first, second, plain = MagicMock(), MagicMock(), MagicMock()
        for session in (first, second, plain):
            session.close = AsyncMock()
        client._sessions[("a", "b")] = first
        client._sessions[("c", "d")] = second
        client._plain = plain

        asyncio.run(client.aclose())

        first.close.assert_awaited_once()
        second.close.assert_awaited_once()
        plain.close.assert_awaited_once()
        self.assertEqual(client._sessions, {})
        self.assertIsNone(client._plain)

    def test_aclose_swallows_a_broken_session(self):
        client = self._client()
        broken = MagicMock()
        broken.close = AsyncMock(side_effect=RuntimeError("already gone"))
        client._sessions[("a", "b")] = broken

        asyncio.run(client.aclose())  # 不该抛

        self.assertEqual(client._sessions, {})


async def _collect(iterator) -> list:
    return [chunk async for chunk in iterator]


async def _empty():
    if False:  # pragma: no cover - 只为让它成为空异步生成器
        yield b""


# ---------------------------------------------------------------- 路由


class _FakeEhClient:
    """顶替真实客户端：记录调用、返回固定数据、从不联网。"""

    def __init__(self):
        self.calls = []
        self.closed = False
        self.error = None
        self.proxy_error = None
        self.proxy_status = 200

    def _maybe_fail(self):
        if self.error is not None:
            raise self.error

    async def list_galleries(self, cursor="", query=""):
        self.calls.append(("list", cursor, query))
        self._maybe_fail()
        return {
            "query": query,
            "cursor": cursor,
            "count": 2,
            "found": "1,658,228",
            "next": "4242034",
            "prev": "",
            "next_url": "https://e-hentai.org/?next=4242034",
            "items": [
                {
                    "gid": 4242037,
                    "token": "8241dd44b0",
                    "title": "Pinterest",
                    "thumbnail": "https://ehgt.org/w/02/703/78918-n6q03g9i.webp",
                    "thumbnail_usable": False,
                    "tags": [],
                }
            ],
        }

    async def search(self, query, cursor=""):
        self.calls.append(("search", query, cursor))
        self._maybe_fail()
        return await self.list_galleries(cursor=cursor, query=query)

    async def gallery(self, gid, token, page=1):
        self.calls.append(("gallery", str(gid), token, page))
        self._maybe_fail()
        return {
            "gid": int(gid),
            "token": token,
            "page": page,
            "page_size": 20,
            "showing": {"from": 1, "to": 20, "total": 40},
            "page_links": [
                "https://e-hentai.org/s/b73be0a953/4240495-1",
                "https://e-hentai.org/s/b6062cc00f/4240495-2",
            ],
        }

    async def metadata(self, gid, token):
        self.calls.append(("metadata", int(gid), token))
        self._maybe_fail()
        return {"gmetadata": [{"gid": int(gid), "token": token, "title": "Pinterest"}]}

    async def auth_status(self):
        self.calls.append(("auth",))
        self._maybe_fail()
        return {"site": "e-hentai.org", "anonymous": True, "edge_count": 16}

    async def open_proxy(self, url, headers=None):
        self.calls.append(("proxy", url, dict(headers or {})))
        self._maybe_fail()
        if self.proxy_error is not None:
            raise self.proxy_error

        async def body():
            yield b"WEBPBYTES"

        return self.proxy_status, {"content-type": "image/webp"}, body()

    async def aclose(self):
        self.closed = True


@unittest.skipUnless(HAVE_EH, SKIP_REASON)
class EhRouteTestCase(unittest.TestCase):
    """路由层：真实 app + 假客户端，只验证形状与错误映射。"""

    def _client(self):
        app = create_eh_app(make_settings())
        client = TestClient(app)
        client.__enter__()
        self.addCleanup(client.__exit__, None, None, None)
        self.fake = _FakeEhClient()
        client.app.state.eh_client = self.fake
        return client

    # ---------- 列表 / 搜索 ----------

    def test_list_returns_the_cursor_page(self):
        client = self._client()

        body = client.get("/api/eh/list").json()

        self.assertEqual(body["count"], 2)
        self.assertEqual(body["next"], "4242034")
        self.assertEqual(body["items"][0]["gid"], 4242037)
        self.assertEqual(self.fake.calls[0], ("list", "", ""))

    def test_list_passes_the_cursor_through_trimmed(self):
        client = self._client()

        client.get("/api/eh/list?cursor=%204242034%20&query=%20cat%20")

        self.assertEqual(self.fake.calls[0], ("list", "4242034", "cat"))

    def test_search_requires_a_query(self):
        client = self._client()

        self.assertEqual(client.get("/api/eh/search").status_code, 422)
        self.assertEqual(client.get("/api/eh/search?query=").status_code, 422)
        self.assertEqual(self.fake.calls, [], "非法参数不该打到客户端")

    def test_search_forwards_the_query(self):
        client = self._client()

        body = client.get("/api/eh/search?query=lolicon&cursor=4242034").json()

        self.assertEqual(body["query"], "lolicon")
        self.assertEqual(self.fake.calls[0], ("search", "lolicon", "4242034"))

    # ---------- 详情 / 图片 ----------

    def test_gallery_decorates_the_detail(self):
        client = self._client()

        body = client.get(f"/api/eh/gallery/4240495/{GALLERY_TOKEN}").json()

        self.assertEqual(body["gid"], 4240495)
        self.assertEqual(body["page"], 1)
        self.assertEqual(body["showing"]["total"], 40)
        self.assertEqual(self.fake.calls[0], ("gallery", "4240495", GALLERY_TOKEN, 1))

    def test_gallery_page_must_be_positive(self):
        client = self._client()

        self.assertEqual(client.get(f"/api/eh/gallery/1/aa?page=0").status_code, 422)
        self.assertEqual(client.get(f"/api/eh/gallery/1/aa?page=-3").status_code, 422)
        self.assertEqual(self.fake.calls, [])

    def test_images_indexes_the_page_links(self):
        client = self._client()

        body = client.get(f"/api/eh/images/4240495/{GALLERY_TOKEN}").json()

        self.assertEqual(body["count"], 2)
        self.assertEqual(body["total"], 40)
        self.assertEqual(body["items"][0], {
            "index": 1,
            "url": "https://e-hentai.org/s/b73be0a953/4240495-1",
        })
        self.assertEqual(body["items"][1]["index"], 2)

    def test_images_offsets_the_index_on_later_screens(self):
        client = self._client()

        body = client.get(f"/api/eh/images/4240495/{GALLERY_TOKEN}?page=3").json()

        # 第 3 屏的第一张是全画廊第 41 张。
        self.assertEqual(body["items"][0]["index"], 41)
        self.assertEqual(body["page"], 3)

    def test_metadata_needs_both_ids(self):
        client = self._client()

        self.assertEqual(client.get("/api/eh/metadata").status_code, 422)
        self.assertEqual(client.get("/api/eh/metadata?gid=0&token=aa").status_code, 422)
        self.assertEqual(client.get("/api/eh/metadata?gid=1").status_code, 422)

    def test_metadata_hits_gdata(self):
        client = self._client()

        body = client.get("/api/eh/metadata?gid=4240495&token=8bcad20a1a").json()

        self.assertEqual(body["gmetadata"][0]["gid"], 4240495)
        self.assertEqual(self.fake.calls[0], ("metadata", 4240495, "8bcad20a1a"))

    # ---------- 代转 ----------

    def test_proxy_rejects_anything_outside_the_whitelist(self):
        client = self._client()

        for url in ("https://e-hentai.org/", "https://example.com/x", "http://127.0.0.1:80/"):
            response = client.get("/api/eh/proxy", params={"url": url})
            self.assertEqual(response.status_code, 400, url)
        self.assertEqual(self.fake.calls, [], "白名单拦下的地址不该打到客户端")

    def test_proxy_streams_the_bytes_with_a_long_cache(self):
        client = self._client()

        response = client.get("/api/eh/proxy", params={"url": IMAGE_PAGE_URL})

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.content, b"WEBPBYTES")
        self.assertEqual(response.headers["content-type"], "image/webp")
        self.assertIn("public", response.headers["cache-control"])
        self.assertEqual(self.fake.calls[0][1], IMAGE_PAGE_URL)

    def test_proxy_forwards_the_range_header(self):
        client = self._client()

        client.get(
            "/api/eh/proxy",
            params={"url": IMAGE_URL},
            headers={"range": "bytes=0-99", "x-ignored": "1"},
        )

        forwarded = self.fake.calls[0][2]
        self.assertEqual(forwarded, {"range": "bytes=0-99"})

    def test_proxy_keeps_an_upstream_failure_as_that_status(self):
        client = self._client()
        self.fake.proxy_status = 404

        response = client.get("/api/eh/proxy", params={"url": IMAGE_PAGE_URL})

        self.assertEqual(response.status_code, 404)
        self.assertIn("404", response.json()["detail"])

    def test_proxy_surfaces_a_client_error(self):
        client = self._client()
        self.fake.proxy_error = EhUnavailableError("连续 4 次都落在死图片节点上")

        response = client.get("/api/eh/proxy", params={"url": IMAGE_PAGE_URL})

        self.assertEqual(response.status_code, 502)
        self.assertIn("死图片节点", response.json()["detail"])

    def test_auth_is_anonymous_and_shaped(self):
        client = self._client()

        body = client.get("/api/eh/auth").json()

        self.assertTrue(body["anonymous"])
        self.assertEqual(body["edge_count"], 16)

    # ---------- 错误映射 ----------

    def test_client_errors_keep_their_status_and_message(self):
        client = self._client()
        self.fake.error = EhError("上游炸了", status=502)

        response = client.get("/api/eh/list")

        self.assertEqual(response.status_code, 502)
        self.assertEqual(response.json()["detail"], "上游炸了")

    def test_a_401_stays_a_401(self):
        client = self._client()
        self.fake.error = EhAuthError("要登录")

        self.assertEqual(client.get("/api/eh/list").status_code, 401)

    def test_a_404_stays_a_404(self):
        client = self._client()
        self.fake.error = EhNotFoundError("没有这个画廊")

        self.assertEqual(client.get("/api/eh/list").status_code, 404)


# ---------------------------------------------------------------- 配置


@unittest.skipUnless(HAVE_EH, SKIP_REASON)
class EhSettingsTestCase(unittest.TestCase):
    """``ehviewer/config.py``：站点专属字段与环境变量前缀。"""

    def test_defaults(self):
        settings = EhSettings(rate_limit_rpm=10000)

        self.assertTrue(settings.http3, "HTTP/3 默认必须开，关掉等于关站")
        self.assertEqual(settings.edge_ips, "")
        self.assertEqual(settings.edge_attempts, 5)
        self.assertEqual(settings.image_attempts, 4)

    def test_settings_upgrade_fills_the_site_fields(self):
        upgraded = as_eh_settings(Settings(rate_limit_rpm=10000))

        self.assertIsInstance(upgraded, EhSettings)
        self.assertEqual(upgraded.rate_limit_rpm, 10000)
        self.assertTrue(upgraded.http3)

    def test_settings_upgrade_is_a_noop_for_our_own_class(self):
        settings = EhSettings(rate_limit_rpm=10000, http3=False)

        self.assertIs(as_eh_settings(settings), settings)

    def test_environment_overrides_are_read(self):
        env = {
            "EH_BRIDGE_HTTP3": "0",
            "EH_BRIDGE_EDGE_IPS": "1.1.1.1, 2.2.2.2",
            "EH_BRIDGE_EDGE_ATTEMPTS": "9",
            "EH_BRIDGE_IMAGE_ATTEMPTS": "2",
        }
        with patch.dict(os.environ, env, clear=False):
            upgraded = as_eh_settings(Settings(rate_limit_rpm=10000))

        self.assertFalse(upgraded.http3)
        self.assertEqual(split_ips(upgraded.edge_ips), ("1.1.1.1", "2.2.2.2"))
        self.assertEqual(upgraded.edge_attempts, 9)
        self.assertEqual(upgraded.image_attempts, 2)

    def test_a_blank_variable_keeps_the_default_but_a_zero_disables(self):
        with patch.dict(os.environ, {"EH_BRIDGE_HTTP3": "   "}, clear=False):
            self.assertTrue(as_eh_settings(Settings(rate_limit_rpm=10000)).http3)
        with patch.dict(os.environ, {"EH_BRIDGE_HTTP3": "false"}, clear=False):
            self.assertFalse(as_eh_settings(Settings(rate_limit_rpm=10000)).http3)

    def test_a_garbage_number_falls_back_to_the_default(self):
        with patch.dict(os.environ, {"EH_BRIDGE_EDGE_ATTEMPTS": "abc"}, clear=False):
            self.assertEqual(
                as_eh_settings(Settings(rate_limit_rpm=10000)).edge_attempts, 5
            )

    def test_the_prefix_is_not_borrowed_from_another_site(self):
        # 站点各用各的前缀（EH_BRIDGE_*），Pixiv 的那套不该影响这里。
        with patch.dict(os.environ, {"PIXIV_BRIDGE_HTTP3": "0"}, clear=False):
            self.assertTrue(as_eh_settings(Settings(rate_limit_rpm=10000)).http3)

        text = (PROJECT_ROOT / "ehviewer" / "config.py").read_text(encoding="utf-8")
        self.assertIn("EH_BRIDGE_", text)
        self.assertNotIn("PIXIV_BRIDGE_", text)
        # 这个站点不用 ECH，别把别站的三项抄过来。
        self.assertNotIn("ech_ips", text)


# ---------------------------------------------------------------- 入口与站点描述


def _app_paths(app) -> set:
    """摊平 app 的全部路由路径。

    新版 FastAPI 的 ``include_router()`` 会塞一个 ``_IncludedRouter`` 包装对象，
    真正的路由在 ``.original_router.routes`` 里；旧版本来就是摊平的。
    """
    collected = set()
    for route in app.routes:
        holder = getattr(route, "original_router", None)
        for item in holder.routes if holder is not None else [route]:
            path = getattr(item, "path", None)
            if path is not None:
                collected.add(path)
    return collected


@unittest.skipUnless(HAVE_EH, SKIP_REASON)
class EhEntryPointTestCase(unittest.TestCase):
    """站点描述符、路由集合、打包登记表与依赖清单。"""

    def test_the_router_and_state_contract(self):
        self.assertEqual(len(EH_ROUTERS), 1)
        self.assertEqual(EH_STATE_KEYS, ("eh_client", "nav_sections"))
        self.assertEqual(
            sorted(eh_state_factory(make_settings())), ["eh_client", "nav_sections"]
        )

    def test_the_state_factory_builds_a_configured_client(self):
        state = eh_state_factory(EhSettings(rate_limit_rpm=10000, http3=False))

        client = state["eh_client"]
        self.assertIsInstance(client, EhClient)
        self.assertFalse(client.http3)

    def test_the_state_factory_hands_the_edge_pool_to_the_client(self):
        settings = EhSettings(
            rate_limit_rpm=10000, edge_ips="1.1.1.1,2.2.2.2", edge_attempts=7, image_attempts=2
        )

        client = eh_state_factory(settings)["eh_client"]

        self.assertEqual(client.edge_ips, ("1.1.1.1", "2.2.2.2"))
        self.assertEqual(client.edge_attempts, 7)
        self.assertEqual(client.image_attempts, 2)

    def test_nav_lists_only_json_apis_under_the_site_prefix(self):
        paths = [item.path for item in EH_NAV.items]

        self.assertEqual(len(paths), 7)
        self.assertTrue(all(p.startswith("/api/eh/") for p in paths), paths)
        self.assertTrue(EH_NAV.title)
        self.assertTrue(EH_NAV.note)

    def test_the_site_descriptor_is_shaped_like_the_others(self):
        self.assertEqual(SITE.name, "ehviewer")
        self.assertEqual(SITE.title, "EHViewer Bridge")
        self.assertEqual(SITE.version, "1.0.0")
        # 接在 pixiv=1 / iwara=2 / asmr_one=3 之后，不留空洞。
        self.assertEqual(SITE.nav_order, 4)
        self.assertEqual(SITE.settings_class, EhSettings)
        self.assertEqual(SITE.required_files, ("run.py", "requirements.txt"))
        self.assertIsNotNone(SITE.checks)
        # 匿名就够用：没有登录自检钩子。
        self.assertIsNone(SITE.login_check)

    def test_ehviewer_adds_exactly_its_own_routes_to_the_shell(self):
        shell = _app_paths(_create_bridge_app(make_settings()))
        full = _app_paths(create_eh_app(make_settings()))
        extra = full - shell

        self.assertTrue(shell <= full, "ehviewer 应用必须包含框架外壳的全部路由")
        self.assertEqual(
            extra,
            {
                "/api/eh/list",
                "/api/eh/search",
                "/api/eh/gallery/{gid}/{token}",
                "/api/eh/images/{gid}/{token}",
                "/api/eh/metadata",
                "/api/eh/proxy",
                "/api/eh/auth",
            },
        )

    def test_shell_and_app_do_not_expose_other_sites_routes(self):
        client = TestClient(create_eh_app(make_settings()))
        with client:
            for path in ("/api/ranking", "/api/iwara/videos", "/api/asmr/works"):
                self.assertEqual(client.get(path).status_code, 404, path)

    def test_requirements_pin_curl_cffi_and_cffi(self):
        text = (PROJECT_ROOT / "ehviewer" / "requirements.txt").read_text(encoding="utf-8")

        self.assertIn("curl_cffi", text)
        self.assertIn("cffi", text)

    def test_curl_cffi_is_not_a_hard_dependency_of_the_main_project(self):
        root_requirements = PROJECT_ROOT / "requirements.txt"

        self.assertNotIn("curl_cffi", root_requirements.read_text(encoding="utf-8"))

    def test_the_package_init_lazily_exposes_the_app(self):
        # ehviewer/__init__.py 不能模块级 import .app：那条链顶部就要
        # curl_cffi，会把「先装依赖再建应用」的自举堵死。
        text = (PROJECT_ROOT / "ehviewer" / "__init__.py").read_text(encoding="utf-8")

        self.assertIn("def __getattr__", text)
        top_level = [ln for ln in text.splitlines() if ln and not ln[:1].isspace()]
        self.assertFalse(
            any(ln.startswith("from .app import") for ln in top_level),
            "ehviewer/__init__.py 顶层不能 import .app",
        )

    def test_the_sites_registry_knows_about_ehviewer(self):
        # 包 / 边界 / 测试三处共用这一份注册表，加站点不该漏在这里。
        import sys

        sys.path.insert(0, str(PROJECT_ROOT / "scripts"))
        try:
            import sites  # type: ignore[import-not-found]
        finally:
            sys.path.pop(0)

        info = sites.SITES_BY_NAME["ehviewer"]
        self.assertEqual(info.directory, "ehviewer")
        self.assertEqual(info.title, "EHViewer Bridge")
        self.assertIn("curl_cffi", info.extra_modules)
        self.assertEqual(sites.SITE_ROUTE_PATHS["ehviewer"], frozenset({
            "/api/eh/list",
            "/api/eh/search",
            "/api/eh/gallery/{gid}/{token}",
            "/api/eh/images/{gid}/{token}",
            "/api/eh/metadata",
            "/api/eh/proxy",
            "/api/eh/auth",
        }))

    def test_the_nav_order_has_no_gap(self):
        # nav_order 是用 AST 从各站点的 app.py 里读出来的（见
        # ``pixiv_bridge/__main__._site_metadata``），加站点时最容易出错的就是
        # 留个空洞（比如从 3 直接跳到 5）。导航项上暴露的字段叫 ``order``。
        from pixiv_bridge.__main__ import site_items

        orders = sorted(item.order for item in site_items())

        self.assertEqual(orders, list(range(1, len(orders) + 1)))

    def test_the_nav_page_lists_the_four_sites_in_order(self):
        from pixiv_bridge.__main__ import site_items

        items = site_items()

        self.assertEqual(
            [item.name for item in items], ["pixiv", "iwara", "asmr_one", "ehviewer"]
        )
        eh = items[-1]
        self.assertEqual(eh.title, "EHViewer Bridge")
        self.assertTrue(eh.description.startswith("ehviewer 接口站点"), eh.description)


# ---------------------------------------------------------------- 自检


@unittest.skipUnless(HAVE_EH, SKIP_REASON)
class EhEnvCheckTestCase(unittest.TestCase):
    """``ehviewer/envcheck.py``：三项自检都只给结论，绝不中止启动。"""

    def _settings(self, **kw):
        return EhSettings(rate_limit_rpm=10000, **kw)

    def _fake_client(self, edges=("1.1.1.1", "2.2.2.2"), edges_error=None, page=None, page_error=None):
        """让 envcheck 内部的 ``from .client import EhClient`` 拿到假的。"""
        instance = MagicMock()
        instance.edge_ips = tuple(edges)
        instance.edges = AsyncMock(side_effect=edges_error) if edges_error else AsyncMock(
            return_value=list(edges)
        )
        if page_error is not None:
            instance.list_galleries = AsyncMock(side_effect=page_error)
        else:
            instance.list_galleries = AsyncMock(return_value=page or {"count": 25})
        instance.aclose = AsyncMock()

        def _make(*_args, **kwargs):
            # 真 EhClient 会把 http3 存成实例属性，envcheck 随后读它；
            # MagicMock 不建这个属性的话读到的会是真值，测不出分支。
            instance.http3 = kwargs.get("http3", True)
            return instance

        maker = MagicMock(side_effect=_make)
        patcher = patch("ehviewer.client.EhClient", maker)
        return patcher, instance

    def test_deps_check_reports_curl_cffi_and_http3(self):
        from ehviewer.envcheck import deps_check

        result = deps_check()

        self.assertTrue(result.ok)
        self.assertIn("curl_cffi", result.detail)
        self.assertIn("HTTP/3", result.detail)

    def test_deps_check_reports_a_missing_package(self):
        from ehviewer.envcheck import deps_check

        with patch.dict("sys.modules", {"curl_cffi": None}):
            result = deps_check()

        self.assertFalse(result.ok)
        self.assertIn("requirements.txt", result.detail)

    def test_edge_check_counts_the_live_edges(self):
        from ehviewer.envcheck import extra_checks

        patcher, _ = self._fake_client(edges=("1.1.1.1", "2.2.2.2"))
        with patcher:
            results = extra_checks(self._settings())

        edge = [r for r in results if r.name == EDGE_NAME][0]
        self.assertTrue(edge.ok)
        self.assertIn("2/2", edge.detail)

    def test_edge_check_reports_when_nothing_connects(self):
        from ehviewer.envcheck import _edge_check

        patcher, _ = self._fake_client(
            edges_error=EhUnavailableError("16 个候选边缘全部连不上")
        )
        with patcher:
            result = asyncio.run(_edge_check(self._settings()))

        self.assertFalse(result.ok)
        self.assertIn("没有可用边缘", result.detail)

    def test_edge_check_times_out_instead_of_hanging(self):
        from ehviewer.envcheck import _edge_check

        async def never():
            await asyncio.sleep(3600)

        patcher, instance = self._fake_client()
        instance.edges = AsyncMock(side_effect=never)
        with patcher, patch("ehviewer.envcheck.EDGE_TIMEOUT", 0.01):
            result = asyncio.run(_edge_check(self._settings()))

        self.assertFalse(result.ok)
        self.assertIn("探测超过", result.detail)
        instance.aclose.assert_awaited()

    def test_site_check_proves_the_whole_chain(self):
        from ehviewer.envcheck import _site_check

        patcher, _ = self._fake_client(page={"count": 25})
        with patcher:
            result = asyncio.run(_site_check(self._settings()))

        self.assertTrue(result.ok)
        self.assertIn("HTTP/3", result.detail)
        self.assertIn("25", result.detail)

    def test_site_check_reports_an_eh_error(self):
        from ehviewer.envcheck import _site_check

        patcher, _ = self._fake_client(page_error=EhUnavailableError("边缘全挂"))
        with patcher:
            result = asyncio.run(_site_check(self._settings()))

        self.assertFalse(result.ok)
        self.assertIn("e-hentai.org 不可达", result.detail)

    def test_site_check_warns_when_http3_was_turned_off(self):
        from ehviewer.envcheck import _site_check

        patcher, instance = self._fake_client(page={"count": 1})
        with patcher:
            result = asyncio.run(_site_check(self._settings(http3=False)))

        self.assertTrue(result.ok)
        self.assertIn("侥幸", result.detail)
        self.assertFalse(instance.http3)

    def test_extra_checks_runs_deps_first_then_the_network_trio(self):
        from ehviewer.envcheck import extra_checks

        patcher, _ = self._fake_client()
        with patcher:
            results = extra_checks(self._settings())

        self.assertEqual([r.name for r in results], [DEPS_NAME, EDGE_NAME, SITE_NAME])
        self.assertTrue(all(r.ok for r in results))
        # 三项都不中止启动：返回的是结论，不是异常。
        self.assertTrue(all(hasattr(r, "ok") for r in results))

    def test_envcheck_has_no_ech_check(self):
        text = (PROJECT_ROOT / "ehviewer" / "envcheck.py").read_text(encoding="utf-8")

        self.assertNotIn("ECH_NAME", text)
        self.assertNotIn("ech_config", text)


@unittest.skipUnless(HAVE_EH, SKIP_REASON)
class EhEntryWiringTestCase(unittest.TestCase):
    """入口脚本本身：自举要跑在 import ehviewer.app 之前，并交给框架基座。"""

    def _source(self) -> str:
        return (PROJECT_ROOT / "ehviewer" / "run.py").read_text(encoding="utf-8")

    def test_bootstrap_runs_before_importing_the_app(self):
        # ehviewer.app → ehviewer.client → import curl_cffi；这条链没装包时会
        # 直接 ImportError，必须排在 bootstrap 之后，否则自举根本没机会跑。
        # 只看真正的语句行：注释里也可能提到这两个名字。
        statements = [
            line.strip()
            for line in self._source().splitlines()
            if line.strip() and not line.strip().startswith("#")
        ]
        bootstrap = next(i for i, ln in enumerate(statements) if ln.startswith("bootstrap("))
        app_import = next(
            i for i, ln in enumerate(statements) if ln.startswith("from ehviewer.app import")
        )

        self.assertLess(bootstrap, app_import)

    def test_entry_hands_the_site_to_the_framework_cli(self):
        text = self._source()

        self.assertIn("from ehviewer.app import SITE", text)
        self.assertIn("main(site=SITE)", text)
        self.assertIn("from pixiv_bridge.cli import main", text)

    def test_the_entry_puts_the_project_root_on_sys_path(self):
        # `python ehviewer/run.py` 时 sys.path[0] 是 ehviewer/，框架包在上一级。
        self.assertIn("sys.path.insert(0, str(HERE.parent))", self._source())

    def test_the_entry_bootstraps_curl_cffi(self):
        text = self._source()

        self.assertIn("curl_cffi", text)
        self.assertIn("requirements.txt", text)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
