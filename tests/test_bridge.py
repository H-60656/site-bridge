"""主项目与扩展机制的单元测试；iwara 的用例在 ``tests/test_iwara.py``。
"""

import asyncio
import base64
import contextlib
import errno
import hashlib
import io
import itertools
import os
import platform
import shutil
import socket
import stat
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
from fastapi.testclient import TestClient

from pixiv_bridge import deps as deps_module
from pixiv.api import (
    PixivApi,
    PixivApiError,
    PixivRateLimitError,
    extract_images_from_html,
    extract_novel_images,
    normalize_image_urls,
    novel_text_has_embeds,
)
from pixiv_bridge.compat import dump_model
from pixiv_bridge.config import Settings
from pixiv_bridge.envcheck import (
    CheckResult,
    dependencies,
    port_available,
    project_files,
    python_version,
    run_checks,
)
from pixiv_bridge.extensions import (
    EXTENSION_FILENAME,
    EXTENSION_ROUTERS_ATTR,
    ExtensionError,
    load_extension_specs,
    load_extensions,
    parse_extension_specs,
)
from novel.app import create_app as create_novel_app
from novel.bookmarks import BookmarkStore, normalize_bookmark
from novel.downloads import DownloadStore, normalize_download
from novel.history import HistoryStore, normalize_entry
from pixiv.image_proxy import ImageProxy
from pixiv.oauth import OAuthManager
from pixiv.app import create_app
from pixiv.envcheck import ech_transport, network
from pixiv.config import PixivSettings

from pixiv_bridge import nav as nav_module


def app_with_novel(settings, **kwargs):
    """主项目的接口应用 + 小说子项目那一层（原来的完整服务）。"""
    return create_novel_app(settings, **kwargs)

# 用系统临时目录（Windows 是 %TEMP%，Linux/Termux 是 /tmp 或 $TMPDIR）。
# 不能写死 "/tmp"：Windows 上它会被解析成「当前盘符的 \tmp」，换一个盘跑测试就会静默失败。
TEST_TOKEN_FILE = Path(tempfile.gettempdir()) / "pixiv_bridge_test_tokens.json"
SERVER_TOKEN_FILE = Path(tempfile.gettempdir()) / "pixiv_bridge_server_test_tokens.json"
PROJECT_ROOT = Path(__file__).resolve().parent.parent
TEST_TMP_ROOT = PROJECT_ROOT / "_tmp_tests"     # 测试临时目录的根（跑完自动收掉）
_TEMP_COUNTER = itertools.count(1)


def make_settings(token="test-refresh"):
    return PixivSettings(
        refresh_token=token,
        token_file=TEST_TOKEN_FILE,
        rate_limit_rpm=10000,
    )


def make_temp_dir(tag="tmp"):
    """给测试建一个临时目录，返回 Path。

    放在工作区的 _tmp_tests/ 下，而不是系统 TEMP：这台机器的沙箱不允许往
    TEMP 下新建的子目录里写文件（mkdtemp 出来的目录也一样，写入会
    PermissionError），tempfile.TemporaryDirectory 连清理都会因为 chmod 被拒
    而报错。工作区里读写都正常，收尾用 rmtree(ignore_errors=True)。
    """
    path = TEST_TMP_ROOT / ("%s_%d_%d" % (tag, os.getpid(), next(_TEMP_COUNTER)))
    path.mkdir(parents=True, exist_ok=True)
    return path


def drop_temp_dir(path):
    shutil.rmtree(path, ignore_errors=True)
    try:                       # 空的根目录也顺手收掉
        TEST_TMP_ROOT.rmdir()
    except OSError:
        pass


class OAuthTestCase(unittest.IsolatedAsyncioTestCase):
    async def test_signed_headers(self):
        settings = make_settings()
        http = httpx.AsyncClient(
            transport=httpx.MockTransport(lambda req: httpx.Response(200, json={}))
        )
        oauth = OAuthManager(settings, http)
        headers = oauth._signed_headers()
        client_time = headers["X-Client-Time"]
        expected = hashlib.md5(
            (client_time + settings.hash_salt).encode("utf-8")
        ).hexdigest()
        self.assertEqual(headers["X-Client-Hash"], expected)
        self.assertEqual(len(headers["X-Client-Hash"]), 32)
        self.assertEqual(headers["User-Agent"], settings.user_agent)
        await http.aclose()

    async def test_token_persist_and_load(self):
        settings = make_settings()
        http = httpx.AsyncClient(
            transport=httpx.MockTransport(lambda req: httpx.Response(200, json={}))
        )
        oauth = OAuthManager(settings, http)
        oauth._access_token = "at"
        oauth._refresh_token = "rt"
        oauth._expires_at = 123.0
        oauth._user = {"id": 1}
        await oauth._persist()
        other = OAuthManager(make_settings(token=""), http)
        await other.load()
        self.assertEqual(other._access_token, "at")
        self.assertEqual(other._refresh_token, "rt")
        await http.aclose()

    async def test_code_challenge(self):
        verifier = OAuthManager._code_verifier()
        self.assertGreaterEqual(len(verifier), 43)
        challenge = OAuthManager._code_challenge(verifier)
        expected = base64.urlsafe_b64encode(
            hashlib.sha256(verifier.encode("utf-8")).digest()
        ).rstrip(b"=")
        self.assertEqual(challenge.encode("ascii"), expected)


class _AsyncBytes(httpx.AsyncByteStream):
    def __init__(self, content):
        self._content = content

    async def __aiter__(self):
        yield self._content


def _mock_handler(request):
    if request.url.host == "i.pximg.net":
        return httpx.Response(
            200,
            stream=_AsyncBytes(b"fake-image"),
            headers={"content-type": "image/jpeg"},
        )
    return httpx.Response(200, json={})


class EnvCheckTestCase(unittest.TestCase):
    def test_python_version_ok(self):
        result = python_version()
        self.assertTrue(result.ok)
        self.assertIn("3.10", result.detail)

    def test_dependencies_ok(self):
        result = dependencies()
        self.assertTrue(result.ok)

    def test_dependencies_missing(self):
        with patch(
            "pixiv_bridge.deps.missing_dependencies", return_value=["fastapi"]
        ):
            result = dependencies()
        self.assertFalse(result.ok)
        self.assertIn("fastapi", result.detail)

    def test_port_free(self):
        result = port_available(0)
        self.assertTrue(result.ok)

    def test_port_in_use(self):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            sock.bind(("127.0.0.1", 0))
            sock.listen(1)
            port = sock.getsockname()[1]
            result = port_available(port)
        self.assertFalse(result.ok)

    def test_network_reachable(self):
        with patch("pixiv.envcheck._probe", return_value="app-api.pixiv.net"):
            result = network()
        self.assertTrue(result.ok)

    def test_network_unreachable(self):
        with patch("pixiv.envcheck._probe", return_value=None):
            result = network()
        self.assertFalse(result.ok)

    def test_run_checks_with_port(self):
        # 通用自检只列框架自己的项；站点专属探测由 SITE.checks 追加到同一张表。
        results = run_checks(port=0)
        self.assertEqual([r.name for r in results], ["python", "deps", "files", "port"])

    def test_site_network_check_is_a_separate_hook(self):
        from pixiv.envcheck import extra_checks

        with patch("pixiv.envcheck._probe", return_value=None):
            results = extra_checks(PixivSettings())
        self.assertEqual([r.name for r in results], ["network"])
        self.assertFalse(results[0].ok)


class DepsTestCase(unittest.TestCase):
    def test_missing_dependencies_empty(self):
        with patch("pixiv_bridge.deps._importable", return_value=True):
            self.assertEqual(deps_module.missing_dependencies(), [])

    def test_missing_dependencies_detected(self):
        def fake_importable(name):
            return name != "httpx"

        with patch("pixiv_bridge.deps._importable", side_effect=fake_importable):
            self.assertEqual(deps_module.missing_dependencies(), ["httpx"])

    def test_ensure_dependencies_noop(self):
        with patch("pixiv_bridge.deps.missing_dependencies", return_value=[]), patch(
            "subprocess.check_call"
        ) as mock_call:
            result = deps_module.ensure_dependencies()
        self.assertEqual(result, [])
        mock_call.assert_not_called()

    def test_ensure_dependencies_installs(self):
        calls = {"n": 0}

        def fake_missing():
            calls["n"] += 1
            return ["fastapi"] if calls["n"] == 1 else []

        with patch(
            "pixiv_bridge.deps.missing_dependencies", side_effect=fake_missing
        ), patch("subprocess.check_call") as mock_call:
            result = deps_module.ensure_dependencies()
        self.assertEqual(result, ["fastapi"])
        mock_call.assert_called_once()

    def test_ensure_dependencies_failure(self):
        with patch(
            "pixiv_bridge.deps.missing_dependencies", return_value=["fastapi"]
        ), patch("subprocess.check_call"):
            with self.assertRaises(RuntimeError):
                deps_module.ensure_dependencies()

    def test_pip_indexes_default(self):
        with patch.dict(os.environ, {}, clear=True):
            self.assertEqual(deps_module._pip_indexes(), deps_module.DEFAULT_PIP_INDEXES)

    def test_pip_indexes_custom(self):
        with patch.dict(os.environ, {"PIXIV_BRIDGE_PIP_INDEX": "https://example.com/simple"}):
            self.assertEqual(
                deps_module._pip_indexes(), ("https://example.com/simple",)
            )

    def test_install_uses_mirror(self):
        calls = {"n": 0}

        def fake_missing():
            calls["n"] += 1
            return ["fastapi"] if calls["n"] == 1 else []

        with patch(
            "pixiv_bridge.deps.missing_dependencies", side_effect=fake_missing
        ), patch("subprocess.check_call") as mock_call:
            deps_module.ensure_dependencies()
        args = mock_call.call_args[0][0]
        self.assertIn("-i", args)
        self.assertEqual(args[args.index("-i") + 1], deps_module.DEFAULT_PIP_INDEXES[0])

    def test_install_falls_back_to_next_mirror(self):
        calls = {"n": 0}

        def fake_missing():
            calls["n"] += 1
            return ["fastapi"] if calls["n"] == 1 else []

        with patch(
            "pixiv_bridge.deps.missing_dependencies", side_effect=fake_missing
        ), patch(
            "subprocess.check_call",
            side_effect=[subprocess.CalledProcessError(1, "pip"), None],
        ) as mock_call:
            deps_module.ensure_dependencies()
        self.assertEqual(mock_call.call_count, 2)


class ServerTestCase(unittest.TestCase):
    def _client(self):
        settings = make_settings(token="")
        settings.token_file = SERVER_TOKEN_FILE
        app = create_app(settings, transport=httpx.MockTransport(_mock_handler))
        return TestClient(app)

    def test_health(self):
        with self._client() as client:
            resp = client.get("/api/health")
            self.assertEqual(resp.status_code, 200)
            self.assertTrue(resp.json()["ok"])

    def test_auth_status(self):
        with self._client() as client:
            resp = client.get("/api/auth/status")
            self.assertEqual(resp.status_code, 200)
            body = resp.json()
            self.assertFalse(body["authenticated"])
            self.assertFalse(body["refresh_token_set"])

    def test_unauthorized_api_call(self):
        with self._client() as client:
            resp = client.get("/api/illust/1")
            self.assertEqual(resp.status_code, 401)
            self.assertIn("error", resp.json())

    def test_openapi_docs(self):
        with self._client() as client:
            self.assertEqual(client.get("/docs").status_code, 200)
            self.assertEqual(client.get("/openapi.json").status_code, 200)

    def test_pure_api_app_index_is_the_navigation_page(self):
        """接口项目的 `/` 是导航页（不是阅读器页面）。"""
        with self._client() as client:
            resp = client.get("/")
            self.assertEqual(resp.status_code, 200)
            self.assertIn("text/html", resp.headers.get("content-type", ""))
            # 纯接口状态下也列出自带入口，并说明没有挂扩展
            self.assertIn("/api/health", resp.text)
            self.assertIn("没有挂载扩展", resp.text)

    def test_pure_api_app_does_not_serve_the_reader(self):
        """阅读器页面属于 novel 扩展：不挂扩展时 /novel 不存在。"""
        with self._client() as client:
            self.assertEqual(client.get("/novel").status_code, 404)

    def test_image_proxy_routes(self):
        with self._client() as client:
            resp = client.get("/img", params={"url": "https://evil.com/x.jpg"})
            self.assertEqual(resp.status_code, 400)
            resp = client.get(
                "/img",
                params={
                    "url": "https://i.pximg.net/c/250x250_80_a2/img-master/"
                    "img/2023/01/01/00/00/00/1_p0_master1200.jpg"
                },
            )
            self.assertEqual(resp.status_code, 200)
            self.assertIn("image", resp.headers.get("content-type", ""))


class NovelWebIndexTestCase(unittest.TestCase):
    """小说阅读器页面由子项目挂在 `/novel` 上（`/` 是接口项目的导航页）。"""

    def test_web_index(self):
        settings = make_settings(token="")
        settings.token_file = SERVER_TOKEN_FILE
        app = app_with_novel(settings, transport=httpx.MockTransport(_mock_handler))
        with TestClient(app) as client:
            resp = client.get("/novel")
            self.assertEqual(resp.status_code, 200)
            self.assertIn("text/html", resp.headers.get("content-type", ""))
            self.assertIn("Pixiv Bridge", resp.text)
            # 首页是小说阅读器：正文读取与导出都在前端完成（单文件、零依赖）
            self.assertIn('path = "/novel/ranking"', resp.text)
            self.assertIn('callApi("/novel/" + novelId + "/text")', resp.text)
            self.assertIn('id="view-reader"', resp.text)
            self.assertIn('id="reader-text"', resp.text)
            self.assertIn("downloadSeries", resp.text)
            # 移动端适配：固定底部标签栏 + 安全区留白
            self.assertIn("@media (max-width:640px)", resp.text)
            self.assertIn("env(safe-area-inset-bottom)", resp.text)
            # 打开失败的诊断面板：区分「桥自身 404」与「上游 404」
            self.assertIn("function renderOpenError(", resp.text)
            self.assertIn("function tryOpenAsSeries(", resp.text)
            # 详情 200 但正文取不到时，不能再报「整本书打不开」（Pixiv 已下架 /v1/novel/text）
            self.assertIn("function renderTextError(", resp.text)
            self.assertIn('diagLines(id, err, "/text")', resp.text)
            # 启动自检：服务进程是旧版本时提示重启（页面会读 /openapi.json 比对路由）
            self.assertIn('id="ver-banner"', resp.text)
            self.assertIn("async function checkRoutes()", resp.text)
            self.assertIn('"/api/novel/{novel_id}"', resp.text)
            # 正文里的插图：标记解析 + 内联渲染
            self.assertIn("function parseNovelText(", resp.text)
            self.assertIn("function novelPlainText(", resp.text)
            self.assertIn("function extractImages(", resp.text)
            self.assertIn("function novelFigure(", resp.text)
            self.assertIn("function renderNovelText(", resp.text)
            self.assertIn("[[imgurl:", resp.text)
            self.assertIn('"/img?url=" + encodeURIComponent(url)', resp.text)
            # 下载：一本（或一个系列）只出一个 EPUB，插图作为图片文件打进去
            self.assertIn("function zipStore(files)", resp.text)
            self.assertIn("function buildBookEpub(", resp.text)
            self.assertIn("async function xhtmlBody(", resp.text)
            self.assertIn('utf8("application/epub+zip")', resp.text)
            self.assertIn("META-INF/container.xml", resp.text)
            self.assertIn("OEBPS/content.opf", resp.text)
            self.assertIn("OEBPS/toc.ncx", resp.text)
            self.assertIn('safeFileName(novel.title, "")', resp.text)
            self.assertIn('"（全" + chapters.length + "章）"', resp.text)
            # 旧的 TXT / 含插图 HTML 双文件导出已经拆掉，不要再回来
            self.assertNotIn("buildNovelTxt", resp.text)
            self.assertNotIn("（含插图）", resp.text)
            self.assertNotIn('".html"', resp.text)
            # 插图取不到时的可诊断文案：带上标记里的 id 与本次解析到的张数
            self.assertIn("function pickImageUrl(", resp.text)
            self.assertIn("textEmbeddedImages", resp.text)
            self.assertIn("original_image_url", resp.text)
            self.assertIn("没有取到图片地址（正文里的 uploadedimage ", resp.text)
            self.assertIn("没有取到图片地址（插画 ", resp.text)
            # 阅读历史落在项目目录下的 history.jsonl（个人使用，不区分用户）
            self.assertIn("function historyEntry(novel, extra)", resp.text)
            self.assertIn("function postHistory(entry, force)", resp.text)
            self.assertIn("function beaconHistory(entry)", resp.text)
            self.assertIn("function historyMetaText(count)", resp.text)
            self.assertIn("async function hydrateHistory()", resp.text)
            self.assertIn('"/api/history"', resp.text)
            self.assertIn('"/api/history?limit=200"', resp.text)
            self.assertIn("阅读历史写入项目里的 ", resp.text)
            # 按作者搜索 + 作者主页 + 关注 / 我的关注
            self.assertIn('data-source="bookmarks"', resp.text)
            self.assertIn('id="q-kind"', resp.text)
            self.assertIn('path = "/search/user"', resp.text)
            self.assertIn('id="view-user"', resp.text)
            self.assertIn('id="user-grid"', resp.text)
            self.assertIn('id="user-more"', resp.text)
            self.assertIn("function userCardHtml(", resp.text)
            self.assertIn("async function openUser(", resp.text)
            self.assertIn("async function openFollowing(", resp.text)
            self.assertIn("function toggleFollow(", resp.text)
            self.assertIn('"/user/follow/add"', resp.text)
            self.assertIn('"#/user/"', resp.text)
            # 收藏（上游 add/delete + 本地台账）与追更
            self.assertIn('id="btn-fav"', resp.text)
            self.assertIn('id="btn-watch"', resp.text)
            self.assertIn('id="series-watch"', resp.text)
            self.assertIn("function callApiPost(", resp.text)
            self.assertIn("function toggleFav(", resp.text)
            self.assertIn("function renderFavs(", resp.text)
            self.assertIn("function toggleWatch(", resp.text)
            self.assertIn('"/bookmarks"', resp.text)
            self.assertIn('"/bookmarks/delete"', resp.text)
            self.assertIn('"/watchlist/novel/add"', resp.text)
            self.assertIn('"/watchlist/novel/delete"', resp.text)
            # 下载记录：章节粒度 + 按系列名归组合并（同系列两个不同「第一章」互不覆盖）
            self.assertIn("function refreshDownloadMarks(", resp.text)
            self.assertIn("function groupDownloads(", resp.text)
            self.assertIn("function downloadMergeGroup(", resp.text)
            self.assertIn("function seriesFullyDownloaded(", resp.text)
            self.assertIn("function mergeKeyOf(", resp.text)
            self.assertIn("function recordDownload(", resp.text)
            self.assertIn("function chapterLabelOf(", resp.text)
            self.assertIn('"/downloads"', resp.text)
            self.assertIn('"/downloads/clear"', resp.text)
            self.assertIn('data-merge="', resp.text)
            self.assertIn('data-dl-clear="1"', resp.text)
            # 书架分三段：云端记录 / 下载记录（可合并导出）/ 本地文件目录
            self.assertIn("async function renderShelf()", resp.text)
            self.assertIn("function headHtml(", resp.text)
            self.assertIn("function shelfCardHtml(", resp.text)
            self.assertIn("function localCardHtml(", resp.text)
            self.assertIn("function dlSectionHtml(", resp.text)
            self.assertIn("function libSectionHtml(", resp.text)
            self.assertIn("async function openLocal(", resp.text)
            self.assertIn('"/api/library/file?path="', resp.text)
            # Web 页面里能改项目配置（data/settings.json）与本地目录
            self.assertIn('id="set-libdir"', resp.text)
            self.assertIn('id="set-lib-list"', resp.text)
            self.assertIn('id="set-merge"', resp.text)
            self.assertIn('id="set-skip"', resp.text)
            self.assertIn('id="set-depth"', resp.text)
            self.assertIn('id="set-save"', resp.text)
            self.assertIn("function loadServerConfig(", resp.text)
            self.assertIn("function saveServerConfig(", resp.text)
            self.assertIn("function applyServerConfig(", resp.text)
            self.assertIn('"/config"', resp.text)
            self.assertIn('"/library"', resp.text)
            self.assertIn("loadServerConfig();", resp.text)
            self.assertIn("loadFavs();", resp.text)
            # 旧版服务自检要把新接口加进去（否则页面会去调不存在的路由）
            self.assertIn('"/api/bookmarks", "/api/downloads", "/api/config", "/api/library"]', resp.text)
            # E2E 里修掉的三个坑：作者卡点击委托、关注状态同步改概况行、下载后刷新系列页标记
            self.assertIn('closest("[data-user]")', resp.text)
            self.assertIn("function setFollowUi(", resp.text)
            self.assertIn('dataset.followChip = "1"', resp.text)
            self.assertIn("function renderSeriesDlState(", resp.text)
            # 「合并同名系列」开关真的参与归组：关掉后同名不同系列各自成组
            self.assertIn("groupDownloads(entries, mergeByName)", resp.text)
            self.assertIn("groupDownloads(state.dlAll, mergeSeriesOn())", resp.text)
            self.assertIn('"series:" + (e.series_id || ("book:" + e.book_id))', resp.text)
            # 卡片里的按钮不能被 flex 拉长（否则「合并导出」会变成 86x206）
            self.assertIn(".novel-card.shelf-card > .ghost", resp.text)
            # 保存配置失败不再是光秃秃的 500：设置里显示服务端原因 + 数据目录能不能写
            self.assertIn('id="set-warn"', resp.text)
            self.assertIn(".hint.warn{", resp.text)
            self.assertIn("data.writable", resp.text)
            self.assertIn("数据目录写不进去：", resp.text)
            self.assertIn("loadServerConfig(true);", resp.text)


class PixivApiTestCase(unittest.IsolatedAsyncioTestCase):
    async def _make_api(self, handler, token="test-token"):
        transport = httpx.MockTransport(handler)
        http = httpx.AsyncClient(transport=transport)
        settings = make_settings()
        oauth = OAuthManager(settings, http)
        oauth._access_token = token
        oauth._expires_at = time.time() + 3600
        api = PixivApi(settings, oauth, http)
        return api, oauth, http

    async def test_illust_detail_headers(self):
        captured = {}

        def handler(request):
            captured["path"] = request.url.path
            captured["params"] = dict(request.url.params)
            captured["headers"] = request.headers
            return httpx.Response(200, json={"illust": {"id": 1}})

        api, _, http = await self._make_api(handler)
        result = await api.illust_detail(123)
        self.assertEqual(result["illust"]["id"], 1)
        self.assertEqual(captured["path"], "/v1/illust/detail")
        self.assertEqual(captured["params"]["illust_id"], "123")
        self.assertEqual(captured["headers"]["authorization"], "Bearer test-token")
        self.assertIn("x-client-time", captured["headers"])
        self.assertIn("x-client-hash", captured["headers"])
        self.assertIn("app-os", captured["headers"])
        await http.aclose()

    async def test_auto_refresh_on_oauth_error(self):
        calls = {"n": 0}

        def handler(request):
            calls["n"] += 1
            if calls["n"] == 1:
                return httpx.Response(
                    400,
                    json={
                        "error": {
                            "message": "Your OAuth token is expired.",
                            "reason": "expired",
                        }
                    },
                )
            return httpx.Response(200, json={"ok": True})

        api, oauth, http = await self._make_api(handler)
        with patch.object(oauth, "refresh", new=AsyncMock()) as mocked:
            result = await api.get("/v1/walkthrough/illusts")
        self.assertEqual(result, {"ok": True})
        mocked.assert_awaited_once()
        await http.aclose()

    async def test_refresh_once_then_fail(self):
        def handler(request):
            return httpx.Response(
                400,
                json={
                    "error": {
                        "message": "Your OAuth token is expired.",
                        "reason": "expired",
                    }
                },
            )

        api, oauth, http = await self._make_api(handler)
        with patch.object(oauth, "refresh", new=AsyncMock()):
            with self.assertRaises(PixivApiError):
                await api.get("/v1/walkthrough/illusts")
        await http.aclose()

    async def test_rate_limit_error(self):
        def handler(request):
            return httpx.Response(
                400, json={"error": {"message": "Rate Limit", "reason": "rate_limit"}}
            )

        api, _, http = await self._make_api(handler)
        with self.assertRaises(PixivRateLimitError):
            await api.get("/v1/illust/detail", {"illust_id": 1})
        await http.aclose()

    async def test_upstream_error_passthrough(self):
        def handler(request):
            return httpx.Response(403, json={"error": {"message": "Forbidden"}})

        api, _, http = await self._make_api(handler)
        with self.assertRaises(PixivApiError) as ctx:
            await api.get("/v1/illust/detail", {"illust_id": 1})
        self.assertEqual(ctx.exception.status, 403)
        await http.aclose()

    async def test_error_body_with_bool_marker_is_passed_through(self):
        """{"error": true, "message": ...} 曾让 _handle_error 抛 AttributeError → 500。"""

        def handler(request):
            return httpx.Response(
                404, json={"error": True, "message": "unmocked: /v2/novel/detail"}
            )

        api, _, http = await self._make_api(handler)
        with self.assertRaises(PixivApiError) as ctx:
            await api.novel_detail(999)
        self.assertEqual(ctx.exception.status, 404)
        self.assertEqual(ctx.exception.message, "unmocked: /v2/novel/detail")
        await http.aclose()

    async def test_novel_text_reads_webview_html(self):
        """/v1/novel/text 已被 Pixiv 下掉；正文改从 /webview/v2/novel 的 HTML 里取。"""
        captured = {}
        novel_json = (
            '{"id":"21630272","title":"\\u4ffa\\u306e\\u4e8b",'
            '"seriesId":null,"tags":["R-18"],'
            '"text":"第一段。\\n[uploadedimage:123]\\n第二段 } 里有括号和 \\"引号\\"。",'
            '"textEmbeddedImages":{'
            '"123":{"urls":{"128x128":"https://i.pximg.net/t.png",'
            '"480mw":"https://i.pximg.net/m.png",'
            '"1200x1200":"https://i.pximg.net/l.png",'
            '"original":"https://i.pximg.net/o.png"}},'
            '"456":{"image_urls":{"large":"https://i.pximg.net/456.jpg"}}}}'
        )
        html = (
            "<!DOCTYPE html><html lang=\"zh\"><head><title>pixiv</title><script>\n"
            "Object.defineProperty(window, 'pixiv', {\n"
            "  value: {\n"
            "    viewerVersion: \"20221031_ai\",\n"
            "    isV2: true,\n"
            "    novel: \n"
            + novel_json
            + ",\n    isOwnWork: false\n  }\n});\n</script></head></html>"
        )

        def handler(request):
            captured["path"] = request.url.path
            captured["params"] = dict(request.url.params)
            return httpx.Response(
                200, text=html, headers={"content-type": "text/html; charset=UTF-8"}
            )

        api, _, http = await self._make_api(handler)
        result = await api.novel_text(21630272)
        self.assertEqual(captured["path"], "/webview/v2/novel")
        self.assertEqual(captured["params"]["id"], "21630272")
        self.assertEqual(captured["params"]["viewer_version"], "20221031_ai")
        self.assertEqual(
            result["novel_text"],
            '第一段。\n[uploadedimage:123]\n第二段 } 里有括号和 "引号"。',
        )
        # 正文里的 [uploadedimage:ID] 要靠这份映射找图；对外多加一个字段，旧调用方不受影响
        self.assertEqual(
            result["novel_images"]["123"],
            {
                "thumb": "https://i.pximg.net/t.png",
                "medium": "https://i.pximg.net/m.png",
                "large": "https://i.pximg.net/l.png",
                "original": "https://i.pximg.net/o.png",
            },
        )
        self.assertEqual(result["novel_images"]["456"], {"large": "https://i.pximg.net/456.jpg"})
        await http.aclose()

    async def test_novel_text_without_images_returns_empty_map(self):
        """没有插图的正文不再多出奇怪字段：novel_images 固定是（空）字典。"""
        html = (
            "<script>Object.defineProperty(window,'pixiv',{value:{novel:"
            '{"id":"1","text":"只有正文。"}'
            "}});</script>"
        )

        async def handler(request):
            return httpx.Response(200, text=html)

        api, _, http = await self._make_api(handler)
        result = await api.novel_text(1)
        self.assertEqual(result["novel_text"], "只有正文。")
        self.assertEqual(result["novel_images"], {})
        await http.aclose()

    def test_normalize_image_urls_accepts_common_shapes(self):
        """网页版/App 两套命名都要能吃：urls / image_urls / 直接给 URL / 缺档位。"""
        self.assertEqual(normalize_image_urls("https://i.pximg.net/x.png"), {"original": "https://i.pximg.net/x.png"})
        self.assertEqual(
            normalize_image_urls({"urls": {"1200x1200": "L", "480mw": "M", "128x128": "T"}}),
            {"large": "L", "medium": "M", "thumb": "T"},
        )
        self.assertEqual(normalize_image_urls({"image_urls": {"original": "O", "large": "L"}}), {"original": "O", "large": "L"})
        self.assertEqual(normalize_image_urls({"medium": "M"}), {"medium": "M"})
        self.assertEqual(normalize_image_urls({"urls": {"unknown": "x"}}), {})
        self.assertEqual(normalize_image_urls(None), {})
        self.assertEqual(normalize_image_urls("   "), {})

    def test_extract_novel_images_handles_naming_and_empty(self):
        novel = {
            "text_embedded_images": {
                "1": {"urls": {"original": "O1"}},
                "2": "https://i.pximg.net/2.png",
                "3": {"urls": {"unknown": "drop-me"}},
            }
        }
        self.assertEqual(
            extract_novel_images(novel), {"1": {"original": "O1"}, "2": {"original": "https://i.pximg.net/2.png"}}
        )
        self.assertEqual(extract_novel_images({"text": "没有插图"}), {})
        self.assertEqual(extract_novel_images({"textEmbeddedImages": {}}), {})
        self.assertEqual(extract_novel_images(None), {})

    async def test_novel_text_reports_missing_webview_payload(self):
        """HTML 里没有 novel 对象时给一句能看懂的 502，而不是把整页 HTML 当错误。"""

        def handler(request):
            return httpx.Response(200, text="<html><body>Service Unavailable</body></html>")

        api, _, http = await self._make_api(handler)
        with self.assertRaises(PixivApiError) as ctx:
            await api.novel_text(21630272)
        self.assertEqual(ctx.exception.status, 502)
        self.assertIn("novel", ctx.exception.message)
        await http.aclose()

    async def test_error_body_truncates_huge_non_json_body(self):
        """Cloudflare 之类的 HTML 拦截页曾是整篇文档塞进 message。"""

        def handler(request):
            return httpx.Response(503, text="<!DOCTYPE html>" + "x" * 5000)

        api, _, http = await self._make_api(handler)
        with self.assertRaises(PixivApiError) as ctx:
            await api.get("/v1/illust/detail", {"illust_id": 1})
        self.assertEqual(ctx.exception.status, 503)
        self.assertLessEqual(len(ctx.exception.message), 301)
        self.assertTrue(ctx.exception.message.endswith("…"))
        await http.aclose()

    async def test_error_body_with_string_shape_is_parsed(self):
        """OAuth 风格 {"error": "invalid_request", "error_description": ...}。"""

        def handler(request):
            return httpx.Response(
                400,
                json={
                    "error": "invalid_request",
                    "error_description": "参数不合法",
                },
            )

        api, _, http = await self._make_api(handler)
        with self.assertRaises(PixivApiError) as ctx:
            await api.get("/v1/illust/detail", {"illust_id": 1})
        self.assertEqual(ctx.exception.status, 400)
        self.assertEqual(ctx.exception.message, "参数不合法")
        self.assertEqual(ctx.exception.reason, "invalid_request")
        await http.aclose()

    async def test_error_body_without_json_uses_raw_text(self):
        def handler(request):
            return httpx.Response(503, text="Service Unavailable")

        api, _, http = await self._make_api(handler)
        with self.assertRaises(PixivApiError) as ctx:
            await api.get("/v1/illust/detail", {"illust_id": 1})
        self.assertEqual(ctx.exception.status, 503)
        self.assertEqual(ctx.exception.message, "Service Unavailable")
        self.assertEqual(ctx.exception.reason, "")
        await http.aclose()

    async def test_empty_error_body_gets_readable_message(self):
        """Pixiv 找不到作品时回空 message 的错误体，不能把 JSON 原文或空串当 message。"""

        def handler(request):
            return httpx.Response(
                404,
                json={
                    "error": {
                        "user_message": "",
                        "message": "",
                        "reason": "",
                        "user_message_lang": "",
                    }
                },
            )

        api, _, http = await self._make_api(handler)
        with self.assertRaises(PixivApiError) as ctx:
            await api.novel_detail(29311438)
        self.assertEqual(ctx.exception.status, 404)
        self.assertEqual(ctx.exception.message, "上游返回 HTTP 404（上游未给出错误说明）")
        self.assertEqual(ctx.exception.reason, "")
        await http.aclose()

    async def test_next_url_normalize(self):
        def handler(request):
            return httpx.Response(
                200,
                json={"path": request.url.path, "query": dict(request.url.params)},
            )

        api, _, http = await self._make_api(handler)
        result = await api.next(
            "https://app-api.pixiv.net/v1/illust/ranking?mode=day&offset=30"
        )
        self.assertEqual(result["path"], "/v1/illust/ranking")
        self.assertEqual(result["query"]["offset"], "30")
        with self.assertRaises(PixivApiError):
            await api.next("https://evil.example.com/v1/illust/ranking")
        await http.aclose()

    async def test_network_error_maps_to_502(self):
        async def handler(request):
            raise httpx.ConnectError("boom", request=request)

        api, _, http = await self._make_api(handler)
        with self.assertRaises(PixivApiError) as ctx:
            await api.get("/v1/illust/detail", {"illust_id": 1})
        self.assertEqual(ctx.exception.status, 502)
        await http.aclose()

    async def test_bookmark_add_form(self):
        captured = {}

        def handler(request):
            captured["content_type"] = request.headers["content-type"]
            captured["body"] = request.content.decode()
            return httpx.Response(200, json={"ok": True})

        api, _, http = await self._make_api(handler)
        await api.illust_bookmark_add(1, "public", ["a", " b "])
        self.assertIn("application/x-www-form-urlencoded", captured["content_type"])
        self.assertIn("illust_id=1", captured["body"])
        self.assertIn("tags%5B%5D=a+b", captured["body"])
        await http.aclose()


class ImageProxyTestCase(unittest.IsolatedAsyncioTestCase):
    async def test_resolve(self):
        settings = make_settings()
        http = httpx.AsyncClient(
            transport=httpx.MockTransport(lambda req: httpx.Response(200))
        )
        proxy = ImageProxy(settings, http)
        url = (
            "https://i.pximg.net/c/250x250_80_a2/img-master/img/2023/01/01/"
            "00/00/00/1_p0_master1200.jpg?x=1"
        )
        resolved = proxy.resolve("", url)
        self.assertEqual(
            resolved,
            "c/250x250_80_a2/img-master/img/2023/01/01/00/00/00/"
            "1_p0_master1200.jpg?x=1",
        )
        with self.assertRaises(ValueError):
            proxy.resolve("", "https://evil.example.com/x.jpg")
        with self.assertRaises(ValueError):
            proxy.resolve("../secret", None)
        with self.assertRaises(ValueError):
            proxy.resolve("//evil.com/x.jpg", None)
        await http.aclose()


class HosterTestCase(unittest.TestCase):
    def test_resolve_default_pool(self):
        from pixiv import hoster

        self.assertIn("210.140.92.136", hoster.resolve("app-api.pixiv.net"))
        self.assertIn(
            "210.140.139.133", hoster.resolve("i.pximg.net")
        )
        self.assertEqual(hoster.resolve("unknown.example.com"), ("unknown.example.com",))

    def test_resolve_custom_ips(self):
        from pixiv import hoster

        self.assertEqual(
            hoster.resolve("app-api.pixiv.net", "1.2.3.4, 5.6.7.8"),
            ("1.2.3.4", "5.6.7.8"),
        )


class SniClientTestCase(unittest.IsolatedAsyncioTestCase):
    async def test_build_request_form(self):
        from pixiv.sni_client import _build_request

        raw = _build_request(
            "POST",
            "/auth/token",
            None,
            {"a": "1", "b": "2"},
            None,
            {"Host": "oauth.secure.pixiv.net", "X-Test": "yes"},
        ).decode("latin-1")
        self.assertIn("POST /auth/token HTTP/1.1", raw)
        self.assertIn("Host: oauth.secure.pixiv.net", raw)
        self.assertIn("X-Test: yes", raw)
        self.assertIn("Content-Type: application/x-www-form-urlencoded", raw)
        self.assertIn("Content-Length: 7", raw)
        self.assertTrue(raw.endswith("a=1&b=2"))

    async def test_build_request_json(self):
        from pixiv.sni_client import _build_request

        raw = _build_request(
            "GET",
            "/v1/illust/detail",
            {"illust_id": 1},
            None,
            {"q": 1},
            {"Host": "app-api.pixiv.net"},
        ).decode("latin-1")
        self.assertIn("GET /v1/illust/detail?illust_id=1 HTTP/1.1", raw)
        self.assertIn("Content-Type: application/json", raw)

    async def test_request_succeeds_on_first_ip(self):
        from pixiv import sni_client

        calls = []

        class FakeWriter:
            def __init__(self):
                self.data = b""

            def write(self, chunk):
                self.data += chunk

            async def drain(self):
                pass

            def close(self):
                pass

            async def wait_closed(self):
                pass

        class FakeReader:
            def __init__(self):
                self.lines = iter(
                    [
                        b"HTTP/1.1 200 OK\r\n",
                        b"Content-Type: application/json\r\n",
                        b"\r\n",
                    ]
                )

            async def readline(self):
                return next(self.lines)

            async def read(self):
                return b'{"ok": true}'

        async def async_fake_open(ip, port, ssl=None, server_hostname=None):
            calls.append(ip)
            return (FakeReader(), FakeWriter())

        with patch.object(
            sni_client, "_open_connection", side_effect=async_fake_open
        ):
            resp = await sni_client.request(
                "GET",
                "https://app-api.pixiv.net/v1/illust/detail?illust_id=1",
                headers={"Host": "app-api.pixiv.net"},
                ips=("10.0.0.1", "10.0.0.2"),
            )
        self.assertEqual(calls, ["10.0.0.1"])
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json(), {"ok": True})

    async def test_request_raises_on_all_failures(self):
        from pixiv import sni_client

        async def fail_open(ip, port, ssl=None, server_hostname=None):
            raise OSError("no route")

        with patch.object(sni_client, "_open_connection", side_effect=fail_open):
            with self.assertRaises(sni_client.SniConnectError):
                await sni_client.request(
                    "GET",
                    "https://app-api.pixiv.net/v1/illust/detail",
                    ips=("10.0.0.1", "10.0.0.2"),
                )

    async def test_request_retries_next_ip_after_protocol_error(self):
        from pixiv import sni_client

        class FakeWriter:
            def write(self, chunk):
                pass

            async def drain(self):
                pass

            def close(self):
                pass

            async def wait_closed(self):
                pass

        class BadReader:
            async def readline(self):
                raise ValueError("bad status line")

            async def read(self):
                return b""

        attempts = []

        async def async_fake_open(ip, port, ssl=None, server_hostname=None):
            attempts.append(ip)
            if ip == "10.0.0.1":
                return (BadReader(), FakeWriter())
            return (FakeReader(), FakeWriter())

        class FakeReader:
            def __init__(self):
                self.lines = iter([b"HTTP/1.1 204 No Content\r\n", b"\r\n"])

            async def readline(self):
                return next(self.lines)

            async def read(self):
                return b""

        with patch.object(
            sni_client, "_open_connection", side_effect=async_fake_open
        ):
            resp = await sni_client.request(
                "GET",
                "https://app-api.pixiv.net/v1/illust/detail",
                ips=("10.0.0.1", "10.0.0.2"),
            )
        self.assertEqual(attempts, ["10.0.0.1", "10.0.0.2"])
        self.assertEqual(resp.status_code, 204)


class BypassApiTestCase(unittest.IsolatedAsyncioTestCase):
    async def test_api_request_uses_bypass_when_enabled(self):
        from pixiv import hoster
        from pixiv import sni_client

        settings = make_settings()
        settings.bypass_sni = True
        http = httpx.AsyncClient(
            transport=httpx.MockTransport(lambda req: httpx.Response(500))
        )
        oauth = MagicMock()
        oauth.access_token = AsyncMock(return_value="token")
        api = PixivApi(settings, oauth, http)

        async def fake_sni_request(
            method,
            url,
            *,
            params=None,
            data=None,
            json=None,
            headers=None,
            timeout=30.0,
            ips=None,
        ):
            self.assertEqual(method, "GET")
            self.assertIn("/v1/illust/detail", url)
            self.assertEqual(ips, hoster.resolve("app-api.pixiv.net", ""))
            self.assertIn("Authorization", headers)
            return httpx.Response(200, json={"illust": {"id": 1}})

        with patch.object(
            sni_client, "request", AsyncMock(side_effect=fake_sni_request)
        ):
            result = await api.get("/v1/illust/detail", {"illust_id": 1})
        self.assertEqual(result["illust"]["id"], 1)
        await http.aclose()

    async def test_api_bypass_connect_error_maps_to_502(self):
        from pixiv import sni_client

        settings = make_settings()
        settings.bypass_sni = True
        http = httpx.AsyncClient(
            transport=httpx.MockTransport(lambda req: httpx.Response(500))
        )
        oauth = MagicMock()
        oauth.access_token = AsyncMock(return_value="token")
        api = PixivApi(settings, oauth, http)

        with patch.object(
            sni_client,
            "request",
            AsyncMock(side_effect=sni_client.SniConnectError("all failed")),
        ):
            with self.assertRaises(PixivApiError) as ctx:
                await api.get("/v1/illust/detail", {"illust_id": 1})
        self.assertEqual(ctx.exception.status, 502)
        await http.aclose()


class LiteralRouteOrderTestCase(unittest.TestCase):
    """回归：字面量路由被同前缀的 {path_param} 路由遮蔽，曾返回 422。"""

    def _client(self):
        settings = make_settings(token="")
        settings.token_file = SERVER_TOKEN_FILE
        app = create_app(settings, transport=httpx.MockTransport(_mock_handler))
        return TestClient(app)

    def test_literal_routes_reach_their_handlers(self):
        cases = [
            ("/api/user/recommended", {}),
            ("/api/novel/ranking", {}),
            ("/api/novel/recommended", {}),
            ("/api/novel/follow", {}),
            ("/api/novel/series", {"series_id": 1}),
        ]
        with self._client() as client:
            for path, params in cases:
                with self.subTest(path=path):
                    resp = client.get(path, params=params)
                    # 未登录时应由业务处理函数返回 401；
                    # 422 说明被 /user/{user_id} 或 /novel/{novel_id} 抢先匹配。
                    self.assertNotEqual(resp.status_code, 422, resp.text)
                    self.assertEqual(resp.status_code, 401, resp.text)

    def test_path_param_routes_still_work(self):
        with self._client() as client:
            for path in ("/api/user/11", "/api/novel/1"):
                with self.subTest(path=path):
                    self.assertEqual(client.get(path).status_code, 401)


class SettingsEnvPathTestCase(unittest.TestCase):
    """回归：--env 传文件时曾被当成目录，导致 .env 不加载、token 落到错误路径。"""

    def test_env_file_path_is_loaded(self):
        fd, raw_path = tempfile.mkstemp(dir=PROJECT_ROOT, suffix=".env")
        os.close(fd)
        self.addCleanup(os.remove, raw_path)
        Path(raw_path).write_text(
            "PIXIV_REFRESH_TOKEN=from-file\nPIXIV_BRIDGE_PORT=9999\n",
            encoding="utf-8",
        )
        with patch.dict(os.environ, {}, clear=True):
            settings = PixivSettings.load(raw_path, migrate=False)
        self.assertEqual(settings.refresh_token, "from-file")
        self.assertEqual(settings.port, 9999)
        # 项目根取 .env 所在目录，token 文件仍落在项目内（个人数据统一在 data/ 下）
        self.assertEqual(settings.project_root, PROJECT_ROOT)
        self.assertEqual(settings.token_file, PROJECT_ROOT / "data" / "tokens.json")

    def test_env_directory_path_still_supported(self):
        target = PROJECT_ROOT / "pixiv_bridge"
        with patch.dict(os.environ, {}, clear=True):
            settings = PixivSettings.load(str(target), migrate=False)
        self.assertEqual(settings.project_root, target)
        self.assertEqual(settings.token_file, target / "data" / "tokens.json")
        self.assertEqual(settings.history_file, target / "data" / "history.jsonl")
        self.assertEqual(settings.port, 8765)

    def test_blank_env_paths_fall_back_to_defaults(self):
        """回归：`.env` 里写了 `PIXIV_HISTORY_FILE=`（空值）时，曾被解析成当前目录。"""
        fd, raw_path = tempfile.mkstemp(dir=PROJECT_ROOT, suffix=".env")
        os.close(fd)
        self.addCleanup(os.remove, raw_path)
        Path(raw_path).write_text(
            "PIXIV_TOKEN_FILE=\nPIXIV_HISTORY_FILE=\n",
            encoding="utf-8",
        )
        with patch.dict(os.environ, {}, clear=True):
            settings = PixivSettings.load(raw_path, migrate=False)
        self.assertEqual(settings.token_file, PROJECT_ROOT / "data" / "tokens.json")
        self.assertEqual(settings.history_file, PROJECT_ROOT / "data" / "history.jsonl")
        self.assertFalse(settings.history_file.is_dir())


class EnvCheckIpPoolTestCase(unittest.TestCase):
    """回归：envcheck 曾忽略 PIXIV_BRIDGE_*_IPS，自检结论与真实请求行为矛盾。"""

    def test_network_forwards_custom_pools_to_probe(self):
        seen = {}

        def fake_probe(host, extra_ips=""):
            seen[host] = extra_ips
            return host if host == "app-api.pixiv.net" else None

        with patch("pixiv.envcheck._probe", fake_probe):
            result = network(
                api_ips="1.1.1.1", oauth_ips="2.2.2.2", image_ips="3.3.3.3"
            )
        self.assertEqual(seen["app-api.pixiv.net"], "1.1.1.1")
        self.assertEqual(seen["oauth.secure.pixiv.net"], "2.2.2.2")
        self.assertEqual(seen["i.pximg.net"], "3.3.3.3")
        self.assertTrue(result.ok)

    def test_site_extra_checks_forwards_custom_pools(self):
        from pixiv.envcheck import extra_checks

        with patch("pixiv.envcheck.network") as fake_network:
            fake_network.return_value = CheckResult("network", True, "ok")
            results = extra_checks(
                PixivSettings(
                    api_ips="1.1.1.1",
                    oauth_ips="2.2.2.2",
                    image_ips="3.3.3.3",
                )
            )
        fake_network.assert_called_once_with("1.1.1.1", "2.2.2.2", "3.3.3.3")
        self.assertEqual([r.name for r in results], ["network"])

    def test_network_default_pools_unchanged(self):
        seen = {}

        def fake_probe(host, extra_ips=""):
            seen[host] = extra_ips
            return None

        with patch("pixiv.envcheck._probe", fake_probe):
            network()
        self.assertEqual(set(seen.values()), {""})


class SniHostHeaderTestCase(unittest.IsolatedAsyncioTestCase):
    """回归：无 SNI 直连缺 Host 头（HTTP/1.1 强制要求，缺失时服务端必须回 400）。"""

    class _FakeWriter:
        def __init__(self, sink):
            self._sink = sink

        def write(self, chunk):
            self._sink.append(chunk)

        async def drain(self):
            pass

        def close(self):
            pass

        async def wait_closed(self):
            pass

    async def _run(self, reader):
        from pixiv import sni_client

        sink = []

        async def fake_open(host, port, ssl=None, server_hostname=None):
            return reader, self._FakeWriter(sink)

        with patch.object(sni_client, "_open_connection", fake_open):
            response = await sni_client.request(
                "GET",
                "https://app-api.pixiv.net/v1/illust/detail?illust_id=1",
                headers={"Authorization": "Bearer x"},
                ips=["127.0.0.1"],
            )
        return response, b"".join(sink).decode("latin-1")

    async def test_host_header_is_added_when_absent(self):
        class Reader:
            def __init__(self):
                self._lines = [
                    b"HTTP/1.1 200 OK\r\n",
                    b"Content-Length: 2\r\n",
                    b"\r\n",
                ]

            async def readline(self):
                return self._lines.pop(0) if self._lines else b""

            async def read(self):
                return b"{}"

        response, raw = await self._run(Reader())
        self.assertEqual(response.status_code, 200)
        self.assertIn("Host: app-api.pixiv.net\r\n", raw)
        self.assertEqual(raw.count("Host:"), 1)

    async def test_caller_supplied_host_is_not_duplicated(self):
        class Reader:
            def __init__(self):
                self._lines = [b"HTTP/1.1 200 OK\r\n", b"\r\n"]

            async def readline(self):
                return self._lines.pop(0) if self._lines else b""

            async def read(self):
                return b""

        # 复用 _run 的调用路径，但由调用方显式提供 Host
        from pixiv import sni_client

        sink = []

        async def fake_open(host, port, ssl=None, server_hostname=None):
            return Reader(), self._FakeWriter(sink)

        with patch.object(sni_client, "_open_connection", fake_open):
            await sni_client.request(
                "GET",
                "https://app-api.pixiv.net/x",
                headers={"host": "custom.example"},
                ips=["127.0.0.1"],
            )
        raw = b"".join(sink).decode("latin-1")
        self.assertEqual(raw.lower().count("host:"), 1)
        self.assertIn("custom.example", raw)

    async def test_malformed_status_line_maps_to_connect_error(self):
        from pixiv import sni_client

        class Reader:
            async def readline(self):
                return b"garbage\r\n"

        sink = []

        async def fake_open(host, port, ssl=None, server_hostname=None):
            return Reader(), self._FakeWriter(sink)

        with patch.object(sni_client, "_open_connection", fake_open):
            with self.assertRaises(sni_client.SniConnectError):
                await sni_client.request(
                    "GET", "https://app-api.pixiv.net/x", ips=["127.0.0.1"]
                )


class HeaderTerminatorTestCase(unittest.IsolatedAsyncioTestCase):
    """回归：头部块缺少终止空行时，服务端会一直等头部，请求永远没有响应。

    该缺陷曾让 ECH 与无 SNI 直连的所有请求（含图片）全部超时。
    """

    def _raw(self, *args):
        from pixiv.sni_client import _build_request

        return _build_request(*args).decode("latin-1")

    async def test_get_without_body_ends_with_blank_line(self):
        raw = self._raw(
            "GET",
            "/v1/illust/detail",
            {"illust_id": 1},
            None,
            None,
            {"Host": "app-api.pixiv.net", "Authorization": "Bearer t"},
        )
        self.assertTrue(raw.startswith("GET /v1/illust/detail?illust_id=1 HTTP/1.1\r\n"))
        self.assertTrue(raw.endswith("\r\n\r\n"), repr(raw))
        self.assertEqual(raw.count("\r\n\r\n"), 1)
        self.assertIn("Host: app-api.pixiv.net\r\n", raw)

    async def test_post_header_block_ends_before_body(self):
        raw = self._raw(
            "POST",
            "/auth/token",
            None,
            {"grant_type": "refresh_token"},
            None,
            {"Host": "oauth.secure.pixiv.net"},
        )
        head, sep, body = raw.partition("\r\n\r\n")
        self.assertEqual(sep, "\r\n\r\n")
        self.assertEqual(body, "grant_type=refresh_token")
        self.assertTrue(head.endswith(f"Content-Length: {len(body)}"), repr(head))


class EchSettingsTestCase(unittest.TestCase):
    def test_ech_env_flags_parsed(self):
        env = {
            "PIXIV_BRIDGE_ECH": "yes",
            "PIXIV_BRIDGE_ECH_CONFIG": "AAAA",
            "PIXIV_BRIDGE_ECH_IPS": "1.2.3.4,5.6.7.8",
        }
        with patch.dict(os.environ, env, clear=True):
            settings = PixivSettings.load(str(PROJECT_ROOT / "_no_such_env_file"))
        self.assertTrue(settings.ech)
        self.assertEqual(settings.ech_config, "AAAA")
        self.assertEqual(settings.ech_ips, "1.2.3.4,5.6.7.8")

    def test_ech_defaults_off(self):
        with patch.dict(os.environ, {}, clear=True):
            settings = PixivSettings.load(str(PROJECT_ROOT / "_no_such_env_file"))
        self.assertFalse(settings.ech)
        self.assertEqual(settings.ech_config, "")
        self.assertEqual(settings.ech_ips, "")
        self.assertEqual(settings.proxy, "")

    def test_proxy_from_env(self):
        with patch.dict(os.environ, {"PIXIV_PROXY": "http://127.0.0.1:7890"}, clear=True):
            settings = PixivSettings.load(str(PROJECT_ROOT / "_no_such_env_file"))
        self.assertEqual(settings.proxy, "http://127.0.0.1:7890")
        with patch.dict(os.environ, {"PIXIV_BRIDGE_PROXY": "socks5://127.0.0.1:1080"}, clear=True):
            settings = PixivSettings.load(str(PROJECT_ROOT / "_no_such_env_file"))
        self.assertEqual(settings.proxy, "socks5://127.0.0.1:1080")


class EchConfigTestCase(unittest.TestCase):
    def test_builtin_config_decodes_and_hides_pixiv_name(self):
        from pixiv import ech_client

        raw = ech_client._decode_config(ech_client.DEFAULT_ECH_CONFIG)
        # ECHConfigList = uint16 总长 + ECHConfig；版本号 0xfe0d 在总长之后。
        self.assertEqual(int.from_bytes(raw[:2], "big"), len(raw) - 2)
        self.assertEqual(raw[2:4], b"\xfe\x0d")
        self.assertIn(ech_client.ECH_PUBLIC_NAME.encode(), raw)
        self.assertNotIn(b"pixiv", raw)

    def test_override_wins_over_constant(self):
        from pixiv import ech_client

        settings = PixivSettings(ech_config=ech_client.DEFAULT_ECH_CONFIG)
        self.assertEqual(
            ech_client.ech_config(settings),
            ech_client._decode_config(ech_client.DEFAULT_ECH_CONFIG),
        )

    def test_pem_wrapped_config_is_accepted(self):
        from pixiv import ech_client

        pem = (
            "-----BEGIN ECHCONFIG-----\n"
            f"{ech_client.DEFAULT_ECH_CONFIG}\n"
            "-----END ECHCONFIG-----\n"
        )
        self.assertEqual(
            ech_client._decode_config(pem),
            ech_client._decode_config(ech_client.DEFAULT_ECH_CONFIG),
        )

    def test_invalid_config_raises_unavailable(self):
        from pixiv import ech_client

        with self.assertRaises(ech_client.EchUnavailable):
            ech_client._decode_config("not base64 !!!")

    def test_doh_answer_with_quoted_ech_value_is_parsed(self):
        """真实形状 data='1 . alpn="h3,h2" ... ech="AEX+..." ...'：旧实现按空格分词后取空串。"""
        from pixiv import ech_client

        payload = {
            "Status": 0,
            "Answer": [
                {
                    "name": "cloudflare-ech.com.",
                    "TTL": 85,
                    "type": 65,
                    "data": (
                        '1 . alpn="h3,h2" ipv4hint="104.18.10.118,104.18.11.118" '
                        f'ech="{ech_client.DEFAULT_ECH_CONFIG}" '
                        'ipv6hint="2606:4700::6812:a76"'
                    ),
                }
            ],
        }
        self.assertEqual(
            ech_client._ech_from_doh(payload),
            ech_client._decode_config(ech_client.DEFAULT_ECH_CONFIG),
        )
        # 没有 ech= 字段、或只有 A/AAAA 记录时都不应误判
        self.assertIsNone(ech_client._ech_from_doh({"Answer": [{"type": 1, "data": "1.2.3.4"}]}))
        self.assertIsNone(ech_client._ech_from_doh({"Answer": [{"type": 65, "data": "1 . alpn=h2"}]}))

    def test_fetch_ech_config_falls_back_to_reachable_doh(self):
        from pixiv import ech_client

        calls = []

        def fake_get(endpoint, **kwargs):
            calls.append(endpoint)
            if endpoint == ech_client.DOH_ENDPOINTS[0]:
                raise httpx.ConnectError("blocked")
            return httpx.Response(
                200,
                json={
                    "Answer": [
                        {
                            "type": 65,
                            "data": f'ech="{ech_client.DEFAULT_ECH_CONFIG}"',
                        }
                    ]
                },
                request=httpx.Request("GET", endpoint),
            )

        with patch.object(ech_client.httpx, "get", side_effect=fake_get):
            found = ech_client.fetch_ech_config("cloudflare-ech.com")
        self.assertEqual(found, ech_client._decode_config(ech_client.DEFAULT_ECH_CONFIG))
        self.assertEqual(calls, list(ech_client.DOH_ENDPOINTS[:2]))

    def test_fetch_ech_config_reports_all_endpoint_failures(self):
        from pixiv import ech_client

        with patch.object(
            ech_client.httpx, "get", side_effect=httpx.ConnectError("blocked")
        ):
            with self.assertRaises(ech_client.EchUnavailable) as ctx:
                ech_client.fetch_ech_config("cloudflare-ech.com")
        self.assertIn("DoH 查询失败", str(ctx.exception))
        self.assertIn(ech_client.DOH_ENDPOINTS[1], str(ctx.exception))

    def test_cached_config_is_refreshed_after_ttl_or_on_demand(self):
        from pixiv import ech_client

        fresh = ech_client._decode_config(ech_client.DEFAULT_ECH_CONFIG)
        calls = []

        def fake_fetch(name):
            calls.append(name)
            return fresh

        ech_client.reset_config_cache()
        with patch.object(ech_client, "fetch_ech_config", side_effect=fake_fetch), patch.object(
            ech_client, "ECH_CONFIG_TTL", 0.0
        ):
            first = ech_client.ech_config(PixivSettings(ech_config=""))
            second = ech_client.ech_config(PixivSettings(ech_config=""))
        self.assertEqual(first, fresh)
        self.assertEqual(second, fresh)
        # TTL=0 时每次都视为过期 → 重新取（而不是进程内永久缓存）
        self.assertEqual(len(calls), 2)

        ech_client.reset_config_cache()
        with patch.object(ech_client, "fetch_ech_config", side_effect=fake_fetch):
            ech_client.ech_config(PixivSettings(ech_config=""))
        self.assertEqual(len(calls), 3)
        ech_client.reset_config_cache()

    def test_transport_refreshes_config_when_tls_fails(self):
        """TLS 层失败（SSL_get_error=1，常见于配置过期）刷新一次配置再试；被 RST 则不刷。"""
        from pixiv import ech_client

        class FakeConn:
            def __init__(self, ip, port, timeout):
                self.ip = ip

            def handshake(self, hostname, config):
                raise ech_client.EchConnectError("ECH 握手失败（SSL_get_error=1）")

            def close(self):
                pass

        configs = [b"\xfe\x0d\x00\x00", b"\xfe\x0d\x00\x01"]
        seen = []

        def fake_config(settings=None, refresh=False):
            seen.append(refresh)
            return configs[1] if refresh else configs[0]

        with patch.object(ech_client, "_Connection", FakeConn), patch.object(
            ech_client, "ech_config", side_effect=fake_config
        ):
            with self.assertRaises(ech_client.EchConnectError) as ctx:
                ech_client._request_sync(
                    "GET", "https://app-api.pixiv.net/v1/x", None, None, None, {}, 1.0, ["1.1.1.1"], PixivSettings()
                )
        self.assertIn("刷新 ECH 配置后重试", str(ctx.exception))
        self.assertEqual(seen, [False, True])
        # 子错误文案（SSL_get_error）在聚合错误里，便于用户判断是配置过期还是被阻断
        self.assertIn("SSL_get_error=1", str(ctx.exception))


class EchHosterTestCase(unittest.TestCase):
    def test_resolve_ech_default_pool(self):
        from pixiv import hoster

        pool = hoster.resolve_ech("app-api.pixiv.net")
        self.assertIn("104.18.10.118", pool)
        self.assertIn("104.16.132.229", pool)
        self.assertNotIn("172.64.145.17", pool)  # 实测该 IP 连 TCP 都建不起来
        self.assertIn("104.18.10.118", hoster.resolve_ech("i.pximg.net"))

    def test_resolve_ech_custom_ips(self):
        from pixiv import hoster

        self.assertEqual(
            hoster.resolve_ech("app-api.pixiv.net", "1.1.1.1, 2.2.2.2"),
            ("1.1.1.1", "2.2.2.2"),
        )


class EchTransportTestCase(unittest.IsolatedAsyncioTestCase):
    """ECH 打开时 API / OAuth 必须走 ech_client，且优先于 BYPASS_SNI。"""

    def _parts(self, **flags):
        from pixiv.api import PixivApi

        settings = make_settings()
        for key, value in flags.items():
            setattr(settings, key, value)
        http = httpx.AsyncClient(
            transport=httpx.MockTransport(lambda req: httpx.Response(500))
        )
        oauth = MagicMock()
        oauth.access_token = AsyncMock(return_value="token")
        return settings, http, PixivApi(settings, oauth, http)

    async def test_api_prefers_ech_over_bypass_sni(self):
        from pixiv import ech_client, hoster, sni_client

        settings, http, api = self._parts(ech=True, bypass_sni=True)
        seen = {}

        async def fake_ech(
            method,
            url,
            *,
            params=None,
            data=None,
            json=None,
            headers=None,
            timeout=30.0,
            ips=None,
            settings=None,
        ):
            seen.update(method=method, url=url, ips=ips, headers=headers, settings=settings)
            return httpx.Response(200, json={"illust": {"id": 7}})

        with patch.object(ech_client, "request", AsyncMock(side_effect=fake_ech)), patch.object(
            sni_client, "request", AsyncMock(side_effect=AssertionError("不应走无 SNI 直连"))
        ):
            result = await api.get("/v1/illust/detail", {"illust_id": 7})
        self.assertEqual(result["illust"]["id"], 7)
        self.assertEqual(seen["method"], "GET")
        self.assertIn("/v1/illust/detail", seen["url"])
        self.assertEqual(seen["ips"], hoster.resolve_ech("app-api.pixiv.net", ""))
        self.assertIs(seen["settings"], settings)
        self.assertIn("Authorization", seen["headers"])
        await http.aclose()

    async def test_proxy_transport_skips_ech_and_sni(self):
        from pixiv import ech_client, sni_client
        from pixiv.api import PixivApi

        settings = make_settings()
        settings.ech = True
        settings.bypass_sni = True
        settings.proxy = "http://127.0.0.1:7890"
        seen = {}

        def handler(request):
            seen["url"] = str(request.url)
            return httpx.Response(200, json={"illust": {"id": 9}})

        http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        oauth = MagicMock()
        oauth.access_token = AsyncMock(return_value="token")
        api = PixivApi(settings, oauth, http)
        with patch.object(
            ech_client, "request", AsyncMock(side_effect=AssertionError("配了代理不应走 ECH"))
        ), patch.object(
            sni_client,
            "request",
            AsyncMock(side_effect=AssertionError("配了代理不应走 SNI 绕过")),
        ):
            result = await api.get("/v1/illust/detail", {"illust_id": 9})
        self.assertEqual(result["illust"]["id"], 9)
        self.assertIn("/v1/illust/detail", seen["url"])
        await http.aclose()

    async def test_api_ech_errors_map_to_502(self):
        from pixiv import ech_client

        for error in (
            ech_client.EchConnectError("握手失败"),
            ech_client.EchUnavailable("缺少 OpenSSL"),
        ):
            with self.subTest(error=type(error).__name__):
                settings, http, api = self._parts(ech=True)
                with patch.object(ech_client, "request", AsyncMock(side_effect=error)):
                    with self.assertRaises(PixivApiError) as ctx:
                        await api.get("/v1/illust/detail", {"illust_id": 1})
                self.assertEqual(ctx.exception.status, 502)
                await http.aclose()

    async def test_ech_custom_ips_are_forwarded(self):
        from pixiv import ech_client

        settings, http, api = self._parts(ech=True, ech_ips="9.9.9.9")
        seen = {}

        async def fake_ech(method, url, **kwargs):
            seen.update(kwargs)
            return httpx.Response(200, json={})

        with patch.object(ech_client, "request", AsyncMock(side_effect=fake_ech)):
            await api.get("/v1/illust/detail", {"illust_id": 1})
        self.assertEqual(seen["ips"], ("9.9.9.9",))
        await http.aclose()

    async def test_oauth_token_request_uses_ech(self):
        from pixiv import ech_client, hoster

        settings = make_settings()
        settings.ech = True
        http = httpx.AsyncClient(
            transport=httpx.MockTransport(lambda req: httpx.Response(500))
        )
        oauth = OAuthManager(settings, http)
        seen = {}

        async def fake_ech(
            method,
            url,
            *,
            params=None,
            data=None,
            json=None,
            headers=None,
            timeout=30.0,
            ips=None,
            settings=None,
        ):
            seen.update(method=method, url=url, ips=ips, data=data, headers=headers)
            return httpx.Response(
                200,
                json={
                    "response": {
                        "access_token": "at",
                        "refresh_token": "rt",
                        "expires_in": 3600,
                        "user": {"id": 1},
                    }
                },
            )

        with patch.object(ech_client, "request", AsyncMock(side_effect=fake_ech)):
            response = await oauth._token_request({"grant_type": "refresh_token"})
        self.assertEqual(seen["method"], "POST")
        self.assertIn("/auth/token", seen["url"])
        self.assertEqual(seen["ips"], hoster.resolve_ech("oauth.secure.pixiv.net", ""))
        self.assertEqual(seen["data"], {"grant_type": "refresh_token"})
        self.assertEqual(response["access_token"], "at")
        await http.aclose()


class ImageProxyTransportTestCase(unittest.IsolatedAsyncioTestCase):
    """图片代理必须遵守 ECH / BYPASS_SNI / 普通三档传输（此前完全不理会 IP 池）。"""

    def _parts(self, **flags):
        settings = make_settings()
        for key, value in flags.items():
            setattr(settings, key, value)
        http = httpx.AsyncClient(transport=httpx.MockTransport(_mock_handler))
        return settings, http, ImageProxy(settings, http)

    @staticmethod
    async def _read(image):
        return b"".join([chunk async for chunk in image.aiter_raw()])

    async def test_stream_uses_ech_when_enabled(self):
        from pixiv import ech_client, hoster, sni_client

        settings, http, proxy = self._parts(ech=True, bypass_sni=True)
        seen = {}

        async def fake_ech(
            method,
            url,
            *,
            params=None,
            data=None,
            json=None,
            headers=None,
            timeout=30.0,
            ips=None,
            settings=None,
        ):
            seen.update(method=method, url=url, headers=headers, ips=ips, settings=settings)
            return httpx.Response(
                200, content=b"image-bytes", headers={"content-type": "image/jpeg"}
            )

        with patch.object(ech_client, "request", AsyncMock(side_effect=fake_ech)), patch.object(
            sni_client, "request", AsyncMock(side_effect=AssertionError("不应走无 SNI 直连"))
        ):
            image = await proxy.stream("img/1.jpg")
            self.assertEqual(image.status_code, 200)
            self.assertEqual(await self._read(image), b"image-bytes")
        self.assertEqual(seen["url"], "https://i.pximg.net/img/1.jpg")
        self.assertEqual(seen["ips"], hoster.resolve_ech("i.pximg.net", ""))
        self.assertEqual(seen["headers"]["Referer"], settings.image_referer)
        await http.aclose()

    async def test_stream_uses_bypass_sni_with_custom_image_ips(self):
        from pixiv import ech_client, hoster, sni_client

        settings, http, proxy = self._parts(bypass_sni=True, image_ips="5.5.5.5")
        seen = {}

        async def fake_sni(
            method,
            url,
            *,
            params=None,
            data=None,
            json=None,
            headers=None,
            timeout=30.0,
            ips=None,
        ):
            seen.update(method=method, url=url, ips=ips, headers=headers)
            return httpx.Response(404, content=b"nope", headers={"content-type": "text/html"})

        with patch.object(sni_client, "request", AsyncMock(side_effect=fake_sni)), patch.object(
            ech_client, "request", AsyncMock(side_effect=AssertionError("ECH 未开启"))
        ):
            image = await proxy.stream("img/1.jpg")
            self.assertEqual(image.status_code, 404)
            self.assertEqual(await self._read(image), b"nope")
        self.assertEqual(seen["ips"], hoster.resolve("i.pximg.net", "5.5.5.5"))
        await http.aclose()

    async def test_stream_errors_map_to_connect_error(self):
        from pixiv import ech_client, sni_client

        settings, http, proxy = self._parts(ech=True)
        with patch.object(
            ech_client, "request", AsyncMock(side_effect=ech_client.EchConnectError("全部失败"))
        ):
            with self.assertRaises(httpx.ConnectError):
                await proxy.stream("img/1.jpg")

        settings.bypass_sni = True
        settings.ech = False
        with patch.object(
            sni_client, "request", AsyncMock(side_effect=sni_client.SniConnectError("全部失败"))
        ):
            with self.assertRaises(httpx.ConnectError):
                await proxy.stream("img/1.jpg")
        await http.aclose()

    async def test_stream_uses_httpx_when_both_modes_off(self):
        settings, http, proxy = self._parts()
        image = await proxy.stream("img/1.jpg")
        self.assertEqual(image.status_code, 200)
        self.assertEqual(await self._read(image), b"fake-image")
        await image.aclose()
        await http.aclose()


class EchEnvCheckTestCase(unittest.TestCase):
    """--check 在 ECH 模式下做真实握手，而不是探测明文 SNI。"""

    def test_ech_transport_ok(self):
        from pixiv import ech_client

        with patch.object(ech_client, "available", return_value=True), patch.object(
            ech_client, "request", AsyncMock(return_value=httpx.Response(400, json={}))
        ):
            result = ech_transport()
        self.assertTrue(result.ok)
        self.assertEqual(result.name, "ech")
        self.assertIn("400", result.detail)

    def test_ech_transport_reports_bundled_library(self):
        """--check 要说清用的是项目自带那份还是系统装的，便于排错。"""
        from pixiv import ech_client

        bundled = ech_client.bundle_root() / "win32-amd64" / "libssl-4-x64.dll"
        with patch.object(ech_client, "available", return_value=True), patch.object(
            ech_client, "library_path", return_value=str(bundled)
        ), patch.object(
            ech_client, "request", AsyncMock(return_value=httpx.Response(400, json={}))
        ):
            result = ech_transport()
        self.assertTrue(result.ok)
        self.assertIn("项目自带", result.detail)

    def test_ech_transport_reports_system_library(self):
        from pixiv import ech_client

        system = Path(r"C:\Program Files\OpenSSL-Win64\bin\libssl-4-x64.dll")
        with patch.object(ech_client, "available", return_value=True), patch.object(
            ech_client, "library_path", return_value=str(system)
        ), patch.object(
            ech_client, "request", AsyncMock(return_value=httpx.Response(400, json={}))
        ):
            result = ech_transport()
        self.assertTrue(result.ok)
        self.assertIn("系统安装", result.detail)

    def test_ech_transport_ok_without_library_path(self):
        """拿不到库路径（极端情况）不该影响结论，也不该冒出空标签。"""
        from pixiv import ech_client

        with patch.object(ech_client, "available", return_value=True), patch.object(
            ech_client, "library_path", return_value=""
        ), patch.object(
            ech_client, "request", AsyncMock(return_value=httpx.Response(400, json={}))
        ):
            result = ech_transport()
        self.assertTrue(result.ok)
        self.assertNotIn("OpenSSL：", result.detail)

    def test_ech_transport_without_openssl(self):
        from pixiv import ech_client

        with patch.object(ech_client, "available", return_value=False):
            result = ech_transport()
        self.assertFalse(result.ok)
        self.assertIn("OpenSSL", result.detail)

    def test_ech_transport_failure_detail(self):
        from pixiv import ech_client

        with patch.object(ech_client, "available", return_value=True), patch.object(
            ech_client, "request", AsyncMock(side_effect=ech_client.EchConnectError("x"))
        ):
            result = ech_transport()
        self.assertFalse(result.ok)
        self.assertIn("EchConnectError", result.detail)

    def test_ech_transport_hints_when_reset(self):
        """SSL_get_error=5（握手被中间盒重置）时给出代理这条出路。"""
        from pixiv import ech_client

        error = ech_client.EchConnectError(
            "ECH 直连失败: 104.18.10.118: EchConnectError(ECH 握手失败（SSL_get_error=5）)"
        )
        with patch.object(ech_client, "available", return_value=True), patch.object(
            ech_client, "request", AsyncMock(side_effect=error)
        ):
            result = ech_transport()
        self.assertFalse(result.ok)
        self.assertIn("PIXIV_PROXY", result.detail)
        self.assertIn("阻断", result.detail)

    def test_proxy_check_ok_and_failure(self):
        from pixiv.envcheck import proxy_transport

        with patch.object(
            httpx,
            "get",
            return_value=httpx.Response(
                400, json={}, request=httpx.Request("GET", "http://127.0.0.1:7890/x")
            ),
        ) as get:
            result = proxy_transport("http://127.0.0.1:7890")
        self.assertTrue(result.ok)
        self.assertEqual(result.name, "proxy")
        self.assertIn("400", result.detail)
        self.assertEqual(get.call_args.kwargs["proxy"], "http://127.0.0.1:7890")

        with patch.object(httpx, "get", side_effect=httpx.ConnectError("refused")):
            result = proxy_transport("http://127.0.0.1:7890")
        self.assertFalse(result.ok)
        self.assertIn("代理不可用", result.detail)

    def test_ech_transport_forwards_config_and_ips(self):
        from pixiv import ech_client

        seen = {}

        async def fake_ech(
            method,
            url,
            *,
            params=None,
            data=None,
            json=None,
            headers=None,
            timeout=30.0,
            ips=None,
            settings=None,
        ):
            seen.update(method=method, url=url, ips=ips, settings=settings)
            return httpx.Response(200, json={})

        with patch.object(ech_client, "available", return_value=True), patch.object(
            ech_client, "request", AsyncMock(side_effect=fake_ech)
        ):
            result = ech_transport("CONFIG-B64", "9.9.9.9")
        self.assertTrue(result.ok)
        self.assertEqual(seen["ips"], ("9.9.9.9",))
        self.assertEqual(seen["settings"].ech_config, "CONFIG-B64")
        self.assertEqual(
            seen["url"], "https://app-api.pixiv.net/v1/illust/detail?illust_id=1"
        )

    def test_site_extra_checks_with_ech_replaces_network_probe(self):
        from pixiv.envcheck import extra_checks

        with patch("pixiv.envcheck.network") as fake_network, patch(
            "pixiv.envcheck.ech_transport"
        ) as fake_ech:
            fake_ech.return_value = CheckResult("ech", True, "ok")
            results = extra_checks(
                PixivSettings(
                    ech=True,
                    ech_config="c",
                    ech_ips="1.1.1.1",
                    api_base="https://x.example",
                )
            )
        fake_network.assert_not_called()
        fake_ech.assert_called_once_with("c", "1.1.1.1", "https://x.example")
        # 站点钩子只回自己的那几项；python/deps/files 由框架的 run_checks 负责。
        self.assertEqual([r.name for r in results], ["ech"])

    def test_site_extra_checks_without_ech_probes_the_network(self):
        from pixiv.envcheck import extra_checks

        with patch("pixiv.envcheck.network") as fake_network, patch(
            "pixiv.envcheck.ech_transport"
        ) as fake_ech:
            fake_network.return_value = CheckResult("network", True, "ok")
            results = extra_checks(
                PixivSettings(
                    api_ips="1.1.1.1", oauth_ips="2.2.2.2", image_ips="3.3.3.3"
                )
            )
        fake_ech.assert_not_called()
        fake_network.assert_called_once_with("1.1.1.1", "2.2.2.2", "3.3.3.3")
        self.assertEqual([r.name for r in results], ["network"])


class RefreshHintTestCase(unittest.TestCase):
    """--check 刷新失败时要说清是本地链路问题还是服务端问题。"""

    def test_dns_failure_is_local(self):
        from pixiv.envcheck import _refresh_hint
        from pixiv.config import PixivSettings

        hint = _refresh_hint(PixivSettings(), RuntimeError("[Errno 11001] getaddrinfo failed"))
        self.assertIn("本地 DNS", hint)
        self.assertIn("不是服务端问题", hint)

    def test_ech_reset_is_local(self):
        from pixiv.envcheck import _refresh_hint
        from pixiv.config import PixivSettings

        hint = _refresh_hint(
            PixivSettings(), RuntimeError("ECH 握手失败（SSL_get_error=5）")
        )
        self.assertIn("本机到 Cloudflare", hint)
        self.assertIn("PIXIV_PROXY", hint)

    def test_timeout_is_local(self):
        from pixiv.envcheck import _refresh_hint
        from pixiv.config import PixivSettings

        hint = _refresh_hint(PixivSettings(), httpx.ConnectTimeout("timed out"))
        self.assertIn("本地链路问题", hint)

    def test_upstream_status_is_server_side(self):
        from pixiv.api import PixivApiError
        from pixiv.envcheck import _refresh_hint
        from pixiv.config import PixivSettings

        hint = _refresh_hint(PixivSettings(), PixivApiError("specified not found", status=404))
        self.assertIn("服务端问题", hint)
        self.assertIn("404", hint)

        hint = _refresh_hint(PixivSettings(), PixivApiError("invalid token", status=401))
        self.assertIn("账号/权限问题", hint)

    def test_unknown_error_has_no_hint(self):
        from pixiv.envcheck import _refresh_hint
        from pixiv.config import PixivSettings

        self.assertEqual(_refresh_hint(PixivSettings(), RuntimeError("weird")), "")


class QueryEncodingTestCase(unittest.TestCase):
    """直连/ECH 路径的参数编码必须和 httpx 一致：布尔小写、None 省略。

    回归背景：urllib 会把 True 编成 "True"、None 编成 "None"，Pixiv 对这两个
    字面量一律返回 400（message 为空），于是 /v1/search/illust、
    /v1/novel/recommended、/v1/illust/ranking 在 ECH/BYPASS_SNI 模式下全挂，
    而 httpx 模式下正常——同一端点两套行为。
    """

    def _raw(self, **kwargs):
        from pixiv.sni_client import _build_request

        return _build_request(**kwargs).decode("latin-1")

    def test_bool_params_are_lowercased(self):
        raw = self._raw(
            method="GET",
            path="/v1/search/illust",
            params={
                "word": "x",
                "merge_plain_keyword_results": True,
                "include_translated_tag_results": False,
            },
            data=None,
            json_body=None,
            headers={"Host": "app-api.pixiv.net"},
        )
        self.assertIn("merge_plain_keyword_results=true", raw)
        self.assertIn("include_translated_tag_results=false", raw)
        self.assertNotIn("True", raw)
        self.assertNotIn("False", raw)

    def test_none_params_are_dropped(self):
        raw = self._raw(
            method="GET",
            path="/v1/illust/ranking",
            params={"mode": "day", "date": None, "filter": "for_android"},
            data=None,
            json_body=None,
            headers={"Host": "app-api.pixiv.net"},
        )
        self.assertIn("GET /v1/illust/ranking?mode=day&filter=for_android HTTP/1.1", raw)
        self.assertNotIn("date", raw)

    def test_sequence_values_are_expanded(self):
        raw = self._raw(
            method="GET",
            path="/v1/search/novel",
            params={"tags[]": ["a", "b"], "empty": None},
            data=None,
            json_body=None,
            headers={"Host": "app-api.pixiv.net"},
        )
        self.assertIn("tags%5B%5D=a&tags%5B%5D=b", raw)
        self.assertNotIn("empty", raw)

    def test_form_body_shares_the_same_encoding(self):
        raw = self._raw(
            method="POST",
            path="/v2/illust/bookmark/add",
            params=None,
            data={"illust_id": 1, "restrict": "public", "is_private": True, "x": None},
            json_body=None,
            headers={"Host": "app-api.pixiv.net"},
        )
        head, _, body = raw.partition("\r\n\r\n")
        self.assertIn("Content-Length: %d" % len(body.encode("utf-8")), head)
        self.assertEqual(body, "illust_id=1&restrict=public&is_private=true")

    def test_json_body_keeps_native_booleans(self):
        raw = self._raw(
            method="POST",
            path="/v1/x",
            params=None,
            data=None,
            json_body={"flag": True, "none": None},
            headers={"Host": "app-api.pixiv.net"},
        )
        _, _, body = raw.partition("\r\n\r\n")
        self.assertEqual(body, '{"flag": true, "none": null}')


class NextUrlQueryForwardingTestCase(unittest.IsolatedAsyncioTestCase):
    """回归：直连/ECH 传输曾把 next_url 的查询串丢掉，导致「下一页」还是第一页。

    /api/next 与各列表接口的 next_url 分支交给传输层的是一条**完整 URL**
    （形如 https://app-api.pixiv.net/v1/illust/ranking?offset=30）。两个手写
    HTTP 传输都只取了 urlparse(url).path，parsed.query 没接回去，于是 offset
    丢失、上游原样重发第一页。httpx 那条传输自己解析 URL，所以只有
    ECH / BYPASS_SNI 模式会踩到，且单元测试当时只覆盖了 httpx 分支。
    """

    class _FakeWriter:
        def __init__(self, sink):
            self._sink = sink

        def write(self, chunk):
            self._sink.append(chunk)

        async def drain(self):
            pass

        def close(self):
            pass

        async def wait_closed(self):
            pass

    class _Reader:
        def __init__(self, lines):
            self._lines = list(lines)

        async def readline(self):
            return self._lines.pop(0) if self._lines else b""

        async def read(self):
            return b"{}"

    def _ok_reader(self):
        return self._Reader(
            [b"HTTP/1.1 200 OK\r\n", b"Content-Length: 2\r\n", b"\r\n"]
        )

    async def test_sni_transport_keeps_next_url_query(self):
        from pixiv import sni_client

        sink = []

        async def fake_open(host, port, ssl=None, server_hostname=None):
            return self._ok_reader(), self._FakeWriter(sink)

        with patch.object(sni_client, "_open_connection", fake_open):
            await sni_client.request(
                "GET",
                "https://app-api.pixiv.net/v1/illust/ranking"
                "?mode=day&filter=for_android&offset=30",
                headers={"Authorization": "Bearer x"},
                ips=["127.0.0.1"],
            )
        request_line = b"".join(sink).decode("latin-1").split("\r\n")[0]
        self.assertIn("offset=30", request_line)
        self.assertIn("mode=day", request_line)

    async def test_transport_merges_next_url_query_with_params(self):
        """显式 params 与 URL 里已有的查询串要能并存（用 & 连接，不能吃掉任一方）。"""
        from pixiv import sni_client

        sink = []

        async def fake_open(host, port, ssl=None, server_hostname=None):
            return self._ok_reader(), self._FakeWriter(sink)

        with patch.object(sni_client, "_open_connection", fake_open):
            await sni_client.request(
                "GET",
                "https://app-api.pixiv.net/v1/illust/ranking?offset=30",
                params={"mode": "day"},
                headers={"Host": "app-api.pixiv.net"},
                ips=["127.0.0.1"],
            )
        request_line = b"".join(sink).decode("latin-1").split("\r\n")[0]
        self.assertIn("offset=30", request_line)
        self.assertIn("mode=day", request_line)


class EchNextUrlQueryTestCase(unittest.TestCase):
    """ECH 传输同样要保留 next_url 的查询串。

    单独一个同步用例：_request_sync 内部会调 asyncio.run()，放在
    IsolatedAsyncioTestCase 里会撞上「cannot be called from a running event loop」。
    """

    def test_ech_transport_keeps_next_url_query(self):
        from pixiv import ech_client

        sent = {}

        class FakeConn:
            def __init__(self, ip, port, timeout):
                pass

            def handshake(self, hostname, config):
                return True

            def send(self, raw, deadline):
                sent["raw"] = raw
                return None

            def close(self):
                pass

        def fake_read_raw(conn, deadline):
            return b"HTTP/1.1 200 OK\r\nContent-Length: 2\r\n\r\n{}"

        with patch.object(ech_client, "_Connection", FakeConn), patch.object(
            ech_client, "_read_raw", fake_read_raw
        ), patch.object(ech_client, "ech_config", lambda *a, **k: b"\xfe\x0d\x00\x00"):
            ech_client._request_sync(
                "GET",
                "https://app-api.pixiv.net/v1/illust/ranking"
                "?mode=day&filter=for_android&offset=30",
                None,
                None,
                None,
                {"Authorization": "Bearer x"},
                5.0,
                ["1.1.1.1"],
                PixivSettings(),
            )
        request_line = sent["raw"].decode("latin-1").split("\r\n")[0]
        self.assertIn("offset=30", request_line)
        self.assertIn("mode=day", request_line)


class BundledOpenSslTestCase(unittest.TestCase):
    """项目自带的 OpenSSL（openssl-libs/）优先于系统安装，载入失败才回退。"""

    def test_bundle_root_is_sibling_of_package(self):
        from pixiv import ech_client

        self.assertEqual(
            ech_client.bundle_root(),
            PROJECT_ROOT / ech_client.BUNDLE_DIR_NAME,
        )
        self.assertEqual(ech_client.BUNDLE_DIR_NAME, "openssl-libs")

    def test_platform_tags_prefer_specific_then_generic(self):
        from pixiv import ech_client

        tags = ech_client._platform_tags()
        # 具体平台-架构 在最前，之后才是松松的兜底名；顺序即查找优先级。
        self.assertTrue(tags[0].startswith(("win32-", "linux-", "darwin-")))
        self.assertEqual(tags[-1], tags[0].split("-")[0])
        self.assertEqual(len(set(tags)), len(tags))

    def test_bundled_dir_precedes_system_dirs(self):
        from pixiv import ech_client

        with patch.dict(os.environ, {"PIXIV_BRIDGE_OPENSSL_DIR": ""}):
            dirs = ech_client._candidate_dirs()
        bundle = ech_client.bundle_root()
        bundled = [d for d in dirs if bundle in d.parents or d == bundle]
        self.assertTrue(bundled, "项目自带目录没有进入候选列表")
        first_bundled = min(dirs.index(d) for d in bundled)
        # 系统安装位置（Windows 的 Program Files / 解释器目录）必须排在后面。
        system_markers = (
            Path(r"C:\Program Files\OpenSSL-Win64\bin"),
            Path(sys.prefix) / "bin",
        )
        for marker in system_markers:
            if marker in dirs:
                self.assertLess(first_bundled, dirs.index(marker))

    def test_explicit_env_dir_still_wins(self):
        from pixiv import ech_client

        with patch.dict(os.environ, {"PIXIV_BRIDGE_OPENSSL_DIR": str(PROJECT_ROOT)}):
            dirs = ech_client._candidate_dirs()
        self.assertEqual(dirs[0], PROJECT_ROOT)
        # 显式指定只是排在最前，内置那份仍留在候选里兜底。
        self.assertIn(ech_client.bundle_root(), dirs)

    def test_library_pairs_yields_every_candidate(self):
        """候选是逐个产出的，不是只认第一个——载入失败才有下一个可试。"""
        from pixiv import ech_client

        dirs = [Path("/dir-a"), Path("/dir-b")]
        with patch.object(ech_client, "_candidate_dirs", return_value=dirs), patch.object(
            Path, "is_file", lambda self: True
        ):
            pairs = list(ech_client._library_pairs())
        self.assertEqual(len(pairs), 2)
        self.assertEqual([ssl.parent for _, ssl in pairs], dirs)

    def test_library_pairs_skips_dirs_without_libcrypto(self):
        from pixiv import ech_client

        with patch.object(
            ech_client, "_candidate_dirs", return_value=[Path("/empty")]
        ), patch.object(Path, "is_file", lambda self: False):
            self.assertEqual(list(ech_client._library_pairs()), [])

    def test_find_pair_error_points_at_bundle_dir(self):
        from pixiv import ech_client

        with patch.object(ech_client, "_library_pairs", lambda: iter(())), patch.object(
            ech_client, "_candidate_dirs", return_value=[]
        ):
            with self.assertRaises(ech_client.EchUnavailable) as ctx:
                ech_client.find_library_pair()
        self.assertIn(ech_client.BUNDLE_DIR_NAME, str(ctx.exception))
        self.assertIn("PIXIV_BRIDGE_OPENSSL_DIR", str(ctx.exception))

    def test_lib_falls_through_and_reports_reasons(self):
        """内置库载入失败 → 用系统那份；两份都不行时错误里带上失败原因。"""
        from pixiv import ech_client

        class FakeLib:
            def __init__(self, crypto_path, ssl_path):
                if "bundle" not in str(ssl_path):
                    raise OSError("不是有效的 Win32 应用程序")
                self.ssl_path = str(ssl_path)

        pairs = [
            (Path("system/libcrypto"), Path("system/libssl")),
            (Path("bundle/libcrypto"), Path("bundle/libssl")),
        ]
        saved = ech_client._lib
        try:
            with patch.object(ech_client, "_library_pairs", lambda: iter(pairs)), patch.object(
                ech_client, "_LibSsl", FakeLib
            ):
                ech_client._lib = None
                lib = ech_client.lib()
                self.assertEqual(lib.ssl_path, str(Path("bundle/libssl")))
                # 单例：第二次调用不再重新遍历候选。
                self.assertIs(ech_client.lib(), lib)

            with patch.object(
                ech_client,
                "_library_pairs",
                lambda: iter([(Path("a/libcrypto"), Path("a/libssl"))]),
            ), patch.object(ech_client, "_LibSsl", FakeLib):
                ech_client._lib = None
                with self.assertRaises(ech_client.EchUnavailable) as ctx:
                    ech_client.lib()
            self.assertIn(str(Path("a") / "libssl"), str(ctx.exception))
            self.assertIn("不是有效的 Win32 应用程序", str(ctx.exception))
        finally:
            ech_client._lib = saved

    def test_repo_bundle_layout_if_present(self):
        """仓库里若带了内置库，目录布局必须符合约定，且能真的载入。"""
        from pixiv import ech_client

        bundle = ech_client.bundle_root()
        if not bundle.is_dir():
            self.skipTest("仓库没有内置 OpenSSL 目录")
        for tag in ech_client._platform_tags():
            directory = bundle / tag
            if not directory.is_dir():
                continue
            names = {item.name for item in directory.iterdir()}
            self.assertTrue(
                names & set(ech_client._LIBSSL_NAMES),
                f"{directory} 里没有 libssl: {sorted(names)}",
            )
            self.assertTrue(
                names & set(ech_client._LIBCRYPTO_NAMES),
                f"{directory} 里没有 libcrypto: {sorted(names)}",
            )
            self.assertTrue(ech_client.available())
            self.assertIn(str(bundle), ech_client.library_path())


class BundledAndroidBundleTestCase(unittest.TestCase):
    """android-arm64 那份库只能在 Termux 上 dlopen，本机测不了「载入」。

    但它恰恰是 Termux 能开 ECH 的**唯一**来源（Termux 官方源的 OpenSSL 3.6.5
    实测导出的 ECH 符号数为 0，回退系统库那条路走不通），而开发机通常是
    Windows/Linux，`test_repo_bundle_layout_if_present` 只会校验当前平台的
    那个子目录。所以这里对 android-arm64 单独做结构校验：必须是 aarch64 的
    ELF64，且真的导出 ECH 符号、SONAME 与依赖名能被 Android 链接器解析。
    一旦有人拿错架构（比如把 x86_64 的库）或错版本（3.x 没有 ECH）替换掉它，
    只有这个用例能拦住。
    """

    ANDROID_TAG = "android-arm64"
    EM_AARCH64 = 183

    def _bundle_dir(self):
        from pixiv import ech_client

        directory = ech_client.bundle_root() / self.ANDROID_TAG
        if not directory.is_dir():
            self.skipTest(f"仓库没有 {self.ANDROID_TAG} 内置库")
        return directory

    def _elf(self, path):
        """极简 ELF64 解析：返回 (e_machine, 导出符号集合, {DT_NEEDED/DT_SONAME: [名字]})。

        section header 字段顺序（<IIQQQQIIQQ）：
        sh_name, sh_type, sh_flags, sh_addr, sh_offset, sh_size, sh_link, sh_info, ...
        """
        import struct

        data = Path(path).read_bytes()
        self.assertEqual(data[:4], b"\x7fELF", f"{path} 不是 ELF 文件")
        self.assertEqual(data[4], 2, f"{path} 不是 ELF64")
        self.assertEqual(data[5], 1, f"{path} 不是小端序")

        e_machine = int.from_bytes(data[18:20], "little")
        e_shoff = int.from_bytes(data[0x28:0x30], "little")
        e_shentsize = int.from_bytes(data[0x3A:0x3C], "little")
        e_shnum = int.from_bytes(data[0x3C:0x3E], "little")
        e_shstrndx = int.from_bytes(data[0x3E:0x40], "little")

        sections = [
            struct.unpack_from("<IIQQQQIIQQ", data, e_shoff + i * e_shentsize)
            for i in range(e_shnum)
        ]
        name_tab = self._section_bytes(data, sections[e_shstrndx])

        def section_name(sh):
            end = name_tab.find(b"\x00", sh[0])
            return name_tab[sh[0] : end].decode("utf-8", "replace")

        by_name = {section_name(sh): sh for sh in sections}

        def strings_of(name, entry_size):
            """取某个节引用的字符串表里的全部名字。"""
            sh = by_name[name]
            table = self._section_bytes(data, sections[sh[6]])  # sh_link -> 字符串表
            body = self._section_bytes(data, sh)
            names = set()
            for offset in range(0, len(body) - entry_size + 1, entry_size):
                start = int.from_bytes(body[offset : offset + 4], "little")
                end = table.find(b"\x00", start)
                names.add(table[start:end].decode("utf-8", "replace"))
            return names

        dynsym_names = strings_of(".dynsym", 24) if ".dynsym" in by_name else set()

        dynamic = {}
        if ".dynamic" in by_name:
            sh = by_name[".dynamic"]
            body = self._section_bytes(data, sh)
            dynstr = self._section_bytes(data, sections[sh[6]])
            for offset in range(0, len(body) - 15, 16):
                tag = int.from_bytes(body[offset : offset + 8], "little")
                value = int.from_bytes(body[offset + 8 : offset + 16], "little")
                if tag in (1, 14):  # DT_NEEDED / DT_SONAME
                    end = dynstr.find(b"\x00", value)
                    dynamic.setdefault(tag, []).append(
                        dynstr[value:end].decode("utf-8", "replace")
                    )
        return e_machine, dynsym_names, dynamic

    @staticmethod
    def _section_bytes(data, sh):
        return data[sh[4] : sh[4] + sh[5]]

    def test_android_libs_are_aarch64_elf(self):
        directory = self._bundle_dir()
        for name in ("libssl.so", "libcrypto.so"):
            machine, _, _ = self._elf(directory / name)
            self.assertEqual(
                machine, self.EM_AARCH64, f"{name} 不是 aarch64（e_machine={machine}）"
            )

    def test_android_libssl_exports_ech_symbols(self):
        directory = self._bundle_dir()
        _, names, _ = self._elf(directory / "libssl.so")
        # 项目代码唯一硬依赖的符号，缺了 ECH 模式就完全不可用。
        self.assertIn("SSL_set1_ech_config_list", names)
        self.assertIn("SSL_CTX_new", names)
        # 对照组：3.x 的库这些不存在，能再挡一次「版本退回去」。
        self.assertIn("SSL_ech_set1_server_names", names)

    def test_android_soname_matches_lookup_names(self):
        from pixiv import ech_client

        directory = self._bundle_dir()
        _, _, dynamic = self._elf(directory / "libssl.so")
        # SONAME 必须落在 _LIBSSL_NAMES 里，Termux 上才按名字找得到。
        for soname in dynamic.get(14, []):
            self.assertIn(soname, ech_client._LIBSSL_NAMES)

        _, _, crypto_dynamic = self._elf(directory / "libcrypto.so")
        for soname in crypto_dynamic.get(14, []):
            self.assertIn(soname, ech_client._LIBCRYPTO_NAMES)
        # libssl 依赖的 libcrypto 用的也是同一个名字，链接时才能解析到自带那份。
        self.assertIn("libcrypto.so", dynamic.get(1, []))


class PlatformDirsTestCase(unittest.TestCase):
    """别的系统也要能找到系统 OpenSSL，不能只按 x86_64 Linux 写死。"""

    def _dirs_for(self, platform_str, machine, prefix=None, env=None):
        from pixiv import ech_client

        real_sp = sys.platform
        real_machine = platform.machine
        real_prefix = sys.prefix
        real_path = os.environ.get("PATH", "")
        saved_env = {
            key: os.environ.get(key) for key in ("PREFIX", "TERMUX_VERSION")
        }
        try:
            sys.platform = platform_str
            platform.machine = lambda: machine
            os.environ["PATH"] = "/usr/local/bin:/usr/bin:/bin"
            if prefix is not None:
                sys.prefix = prefix
            for key in ("PREFIX", "TERMUX_VERSION"):
                os.environ.pop(key, None)
            if env:
                os.environ.update(env)
            with patch.object(Path, "is_dir", lambda self: True):
                dirs = [str(d) for d in ech_client._candidate_dirs()]
        finally:
            sys.platform = real_sp
            platform.machine = real_machine
            sys.prefix = real_prefix
            os.environ["PATH"] = real_path
            for key, value in saved_env.items():
                if value is None:
                    os.environ.pop(key, None)
                else:
                    os.environ[key] = value
        # Windows 上 Path("/usr/lib64") 会渲染成 "\usr\lib64"，比较前统一成正斜杠。
        return " ".join(dirs).replace("\\", "/")

    def test_system_name_does_not_merge_bsd_into_linux(self):
        from pixiv import ech_client

        real = sys.platform
        try:
            for value, expected in (
                ("win32", "win32"),
                ("cygwin", "win32"),
                ("linux", "linux"),
                ("darwin", "darwin"),
                ("freebsd13", "freebsd"),
                ("openbsd7", "openbsd"),
            ):
                sys.platform = value
                self.assertEqual(ech_client._system_name(), expected, value)
        finally:
            sys.platform = real

    def test_platform_tags_use_real_system_name(self):
        from pixiv import ech_client

        real = sys.platform
        real_machine = platform.machine
        try:
            sys.platform = "freebsd13"
            platform.machine = lambda: "amd64"
            self.assertEqual(ech_client._platform_tags(), ("freebsd-amd64", "freebsd"))
        finally:
            sys.platform = real
            platform.machine = real_machine

    def test_linux_covers_non_x86_64_multiarch_dirs(self):
        """ARM 板子上的 libssl 在 /usr/lib/aarch64-linux-gnu，不能只认 x86_64。"""
        dirs = self._dirs_for("linux", "aarch64")
        self.assertIn("aarch64-linux-gnu", dirs)
        self.assertIn("/usr/lib64", dirs)
        self.assertIn("arm-linux-gnueabihf", self._dirs_for("linux", "armv7l"))

    def test_linux_includes_x86_64_multiarch_and_lib64(self):
        dirs = self._dirs_for("linux", "x86_64")
        self.assertIn("x86_64-linux-gnu", dirs)
        self.assertIn("/usr/lib64", dirs)

    def test_macos_finds_homebrew_keg_only_openssl(self):
        """Homebrew 的 openssl@3 是 keg-only，库在 opt/ 下而不是 lib/。"""
        dirs = self._dirs_for("darwin", "arm64")
        self.assertIn("/opt/homebrew/opt/openssl@3/lib", dirs)
        self.assertIn("/opt/homebrew/lib", dirs)

    def test_bsd_does_not_get_linux_multiarch_dirs(self):
        dirs = self._dirs_for("freebsd13", "amd64")
        self.assertNotIn("linux-gnu", dirs)
        self.assertIn("/usr/local/lib", dirs)

    def test_windows_dirs_unchanged(self):
        dirs = self._dirs_for("win32", "AMD64")
        self.assertIn("OpenSSL-Win64", dirs)
        self.assertNotIn("linux-gnu", dirs)


class TermuxTestCase(unittest.TestCase):
    """Termux（Android）单列成 android 目标：bionic 库与 glibc 的 Linux 版不通用。"""

    TERMUX = "/data/data/com.termux/files/usr"

    def _env(self, **extra):
        env = {"PREFIX": self.TERMUX}
        env.update(extra)
        return env

    def test_detects_termux_by_prefix(self):
        from pixiv import ech_client

        with patch.dict(os.environ, {"PREFIX": self.TERMUX}, clear=False):
            os.environ.pop("TERMUX_VERSION", None)
            self.assertTrue(ech_client.is_termux())
            self.assertEqual(ech_client.termux_prefix(), self.TERMUX)

    def test_detects_termux_by_version_marker(self):
        from pixiv import ech_client

        env = dict(os.environ)
        env.pop("PREFIX", None)
        env["TERMUX_VERSION"] = "0.118.0"
        with patch.dict(os.environ, env, clear=True):
            self.assertTrue(ech_client.is_termux())

    def test_not_termux_on_plain_linux(self):
        from pixiv import ech_client

        env = {"PATH": "/usr/bin:/bin", "PREFIX": "/usr"}
        with patch.dict(os.environ, env, clear=True):
            self.assertFalse(ech_client.is_termux())
            self.assertEqual(ech_client.termux_prefix(), "")

    def test_system_name_is_android_not_linux(self):
        from pixiv import ech_client

        with patch.dict(os.environ, self._env(), clear=False):
            self.assertEqual(ech_client._system_name(), "android")
            self.assertIn("android", ech_client._platform_tags()[0])

    def test_bundle_tag_targets_android_dir(self):
        from pixiv import ech_client

        real_machine = platform.machine
        platform.machine = lambda: "aarch64"  # Termux 上通常是 aarch64
        try:
            with patch.dict(os.environ, self._env(), clear=False):
                tags = ech_client._platform_tags()
        finally:
            platform.machine = real_machine
        self.assertEqual(tags[0], "android-arm64")
        self.assertIn("android", tags)

    def test_looks_in_termux_prefix_lib(self):
        from pixiv import ech_client

        with patch.dict(os.environ, self._env(), clear=False):
            with patch.object(Path, "is_dir", lambda self: True):
                dirs = [str(d).replace("\\", "/") for d in ech_client._candidate_dirs()]
        joined = " ".join(dirs)
        self.assertIn(self.TERMUX + "/lib", joined)
        # 安卓上不存在 glibc 的多架构目录，不该混进清单。
        self.assertNotIn("linux-gnu", joined)

    def test_does_not_use_glibc_linux_dirs_on_termux(self):
        from pixiv import ech_client

        with patch.dict(os.environ, self._env(), clear=False):
            with patch.object(Path, "is_dir", lambda self: True):
                dirs = [str(d).replace("\\", "/") for d in ech_client._candidate_dirs()]
        joined = " ".join(dirs)
        self.assertNotIn("/usr/lib64", joined)
        self.assertNotIn("/usr/lib/x86_64", joined)


class ProjectFilesTestCase(unittest.TestCase):
    """移植自检：运行必需的文件是否随项目文件夹一起拷全。"""

    def test_real_project_has_required_files(self):
        result = project_files([PROJECT_ROOT])
        self.assertTrue(result.ok, result.detail)
        self.assertEqual(result.detail, "项目文件完整")

    def test_missing_files_are_reported(self):
        # 拿站点目录当「项目根」：它没有框架的 requirements.txt，必然报缺。
        result = project_files([(PROJECT_ROOT / "pixiv", ("run.py", "requirements.txt"))])
        self.assertFalse(result.ok)
        self.assertIn("requirements.txt", result.detail)

    def test_site_directory_has_its_entry_point(self):
        # 每个接口项目的必需文件是它自己的 run.py —— 由站点的 SITE 声明。
        for site in ("pixiv", "iwara"):
            result = project_files([(PROJECT_ROOT / site, ("run.py",))])
            self.assertTrue(result.ok, f"{site}: {result.detail}")

    def test_multiple_directories_prefix_the_missing_name(self):
        # 要查多个目录时，缺的文件名必须带目录名，否则看不出是谁缺。
        result = project_files(
            [
                (PROJECT_ROOT / "pixiv", ("run.py",)),
                (PROJECT_ROOT / "iwara", ("run.py", "nope.txt")),
            ]
        )
        self.assertFalse(result.ok)
        self.assertIn("iwara/nope.txt", result.detail)

    def test_run_checks_includes_files_check(self):
        results = run_checks(files=((PROJECT_ROOT / "pixiv", ("run.py",)),))
        self.assertEqual([r.name for r in results], ["python", "deps", "files"])
        files_result = next(r for r in results if r.name == "files")
        self.assertTrue(files_result.ok, files_result.detail)

    def test_run_checks_adds_the_port_check_only_when_asked(self):
        results = run_checks(port=0, files=((PROJECT_ROOT / "pixiv", ("run.py",)),))
        self.assertEqual([r.name for r in results], ["python", "deps", "files", "port"])


class OfflineWheelTestCase(unittest.TestCase):
    """离线移植：项目根下存在 wheels/ 时必须优先 --no-index --find-links。"""

    def test_no_wheel_dir_in_repo(self):
        self.assertEqual(deps_module.wheel_dirs(PROJECT_ROOT), ())

    def test_wheel_dir_names_are_detected(self):
        with patch.object(Path, "is_dir", lambda self: self.name == "wheels"):
            dirs = deps_module.wheel_dirs("/fake/root")
        self.assertEqual(dirs, (Path("/fake/root/wheels"),))

    def test_wheel_dir_takes_priority_over_mirrors(self):
        fake = PROJECT_ROOT / "wheels"
        with patch("pixiv_bridge.deps.wheel_dirs", return_value=(fake,)):
            candidates = deps_module._install_candidates(
                PROJECT_ROOT / "requirements.txt", PROJECT_ROOT
            )
        label, cmd = candidates[0]
        self.assertIn("--no-index", cmd)
        self.assertEqual(cmd[cmd.index("--find-links") + 1], str(fake))
        self.assertIn(str(fake), label)
        # 镜像源仍留在后面兜底（本地 wheel 不全时还能联网补）
        self.assertTrue(any("-i" in item for _, item in candidates))

    def test_index_candidates_use_mirrors_and_timeout(self):
        candidates = deps_module._install_candidates(
            PROJECT_ROOT / "requirements.txt", PROJECT_ROOT
        )
        indexes = tuple(
            cmd[cmd.index("-i") + 1] for _, cmd in candidates if "-i" in cmd
        )
        self.assertEqual(indexes, deps_module.DEFAULT_PIP_INDEXES)
        for _, cmd in candidates:
            self.assertIn("--timeout", cmd)
            self.assertIn(str(PROJECT_ROOT / "requirements.txt"), cmd)


class PipBootstrapTestCase(unittest.TestCase):
    """有些系统自带 Python 没有 pip（Debian/Ubuntu），要能自动补上。"""

    def test_ensure_pip_noop_when_present(self):
        with patch("pixiv_bridge.deps._pip_available", return_value=True), patch(
            "subprocess.check_call"
        ) as mock_call:
            self.assertTrue(deps_module._ensure_pip())
        mock_call.assert_not_called()

    def test_ensure_pip_bootstraps_via_ensurepip(self):
        calls = {"n": 0}

        def fake_available():
            calls["n"] += 1
            return calls["n"] > 1

        with patch(
            "pixiv_bridge.deps._pip_available", side_effect=fake_available
        ), patch("subprocess.check_call") as mock_call:
            self.assertTrue(deps_module._ensure_pip())
        self.assertIn("ensurepip", mock_call.call_args[0][0])

    def test_install_requirements_raises_without_pip(self):
        with patch("pixiv_bridge.deps._ensure_pip", return_value=False):
            with self.assertRaises(RuntimeError) as ctx:
                deps_module._install_requirements(
                    PROJECT_ROOT / "requirements.txt", PROJECT_ROOT
                )
        self.assertIn("pip", str(ctx.exception))

    def test_install_failure_mentions_manual_command(self):
        with patch(
            "pixiv_bridge.deps.missing_dependencies", return_value=["fastapi"]
        ), patch("subprocess.check_call"):
            with self.assertRaises(RuntimeError) as ctx:
                deps_module.ensure_dependencies(PROJECT_ROOT)
        self.assertIn("README", str(ctx.exception))
        self.assertIn("pip download", str(ctx.exception))

    def test_entry_points_share_the_same_bootstrap(self):
        """各站点入口的自举都走 ``deps.bootstrap()``，不再各抄一遍 try/except。"""
        from pixiv_bridge import __main__ as navigation

        for item in navigation.site_items():
            name = f"{item.name}/run.py"
            source = (PROJECT_ROOT / name).read_text(encoding="utf-8")
            with self.subTest(name=name):
                self.assertIn("bootstrap(", source)
                self.assertNotIn("except RuntimeError", source)

    def test_module_entry_dispatches_to_a_site_entry(self):
        """``python -m pixiv_bridge <站点>`` 只负责找到并运行 ``<站点>/run.py``。"""
        source = (PROJECT_ROOT / "pixiv_bridge/__main__.py").read_text(encoding="utf-8")
        self.assertIn("runpy.run_path", source)
        self.assertIn('"run.py"', source)

    def test_bootstrap_reports_failure_without_traceback(self):
        """``bootstrap()`` 抓到 RuntimeError 时只打一行 ``[deps]``，然后 exit(1)。"""
        def boom():
            raise RuntimeError("依赖自动安装失败")

        stderr = io.StringIO()
        with patch("sys.stderr", stderr):
            with self.assertRaises(SystemExit) as ctx:
                deps_module.bootstrap(boom)
        self.assertEqual(ctx.exception.code, 1)
        self.assertIn("[deps] 依赖自动安装失败", stderr.getvalue())
        self.assertNotIn("Traceback", stderr.getvalue())

    def test_bootstrap_passes_the_install_result_through(self):
        self.assertEqual(deps_module.bootstrap(lambda: ["fastapi"]), ["fastapi"])


class _FakeTty(io.StringIO):
    """假的终端：``choose_site`` 只问 ``isatty()``。"""

    def isatty(self):
        return True


class NavigationTestCase(unittest.TestCase):
    """``python -m pixiv_bridge`` 就是整个项目的导航：列站点与扩展、挑一个、再执行入口。

    导航信息必须**只靠读字面量**（:mod:`ast`）拿到，不能 import 站点代码 ——
    框架不认识任何站点，那几个假项目树里连 ``Site`` 都没定义，解析失败时
    也必须退回目录名而不是崩掉。
    """

    @classmethod
    def setUpClass(cls):
        import pixiv_bridge.__main__ as module

        cls.nav = module

    def _fake_root(self):
        # 放在工作区的 _tmp_tests/ 下：系统 TEMP 里新建子目录会被沙箱拒绝
        directory = make_temp_dir("nav")
        self.addCleanup(drop_temp_dir, directory)
        return directory

    def _keep_argv(self):
        saved = list(sys.argv)
        self.addCleanup(lambda: sys.argv.__setitem__(slice(None), saved))

    # ---------------- 列出真实项目里的接口项目与扩展 ----------------

    def test_the_real_project_lists_every_api_project_in_nav_order(self):
        sites = self.nav.site_items()
        self.assertEqual(
            [item.name for item in sites], ["pixiv", "iwara", "asmr_one"]
        )
        self.assertEqual([item.order for item in sites], [1, 2, 3])
        self.assertEqual([item.kind for item in sites], ["site"] * 3)
        self.assertEqual(sites[0].title, "Pixiv Bridge")
        self.assertEqual(sites[0].entry, PROJECT_ROOT / "pixiv" / "run.py")
        self.assertTrue(sites[0].description.startswith("Pixiv 接口站点"))
        self.assertTrue(sites[1].description.startswith("iwara.tv 接口站点"))
        self.assertTrue(sites[2].description.startswith("asmr.one 接口站点"))

    def test_the_real_project_lists_extensions_that_are_not_entries(self):
        extensions = self.nav.extension_items()
        self.assertEqual([item.name for item in extensions], ["iwara_web", "novel"])
        for item in extensions:
            with self.subTest(extension=item.name):
                self.assertEqual(item.kind, "extension")
                self.assertEqual(item.entry, PROJECT_ROOT / item.name / "extension.py")
                self.assertTrue(item.title)
        # 站点列表跟着 package 的注册表走，不再写死两个。
        self.assertEqual(
            self.nav.site_names(), ["pixiv", "iwara", "asmr_one"]
        )

    def test_render_navigation_shows_numbers_extensions_and_hints(self):
        text = self.nav.render_navigation()
        self.assertIn("项目导航", text)
        self.assertIn("1) pixiv", text)
        self.assertIn("2) iwara", text)
        self.assertIn("PIXIV_BRIDGE_EXTENSIONS", text)
        self.assertIn("--list", text)
        self.assertIn("BRIDGE_SITE=pixiv", text)
        self.assertIn("iwara_web", text)
        self.assertIn("novel", text)

    # ---------------- 只读字面量，不 import 站点 ----------------

    def test_site_metadata_comes_from_literals_in_app_py(self):
        root = self._fake_root()
        demo = root / "demo"
        demo.mkdir()
        (demo / "run.py").write_text("", encoding="utf-8")
        (demo / "__init__.py").write_text(
            '"""演示项目简介。\n\n第二段不该出现。"""\n', encoding="utf-8"
        )
        # 没有 import Site，也没有 create_app：导航只看字面量，不会执行这里
        (demo / "app.py").write_text(
            'TITLE = "演示站点"\n'
            'SITE = Site(name="demo", title=TITLE, nav_order=3, create_app=create_app)\n',
            encoding="utf-8",
        )

        sites = self.nav.site_items(root)
        self.assertEqual([item.name for item in sites], ["demo"])
        self.assertEqual(sites[0].title, "演示站点")
        self.assertEqual(sites[0].order, 3)
        self.assertEqual(sites[0].description, "演示项目简介。")
        self.assertEqual(sites[0].entry, demo / "run.py")
        self.assertEqual(sites[0].kind, "site")

    def test_extension_metadata_comes_from_nav_literals(self):
        root = self._fake_root()
        demoext = root / "demoext"
        demoext.mkdir()
        (demoext / "extension.py").write_text(
            'EXTENSION_NAV = (\n'
            '    {"title": "演示扩展", "description": "扩展说明", "path": "/demo"},\n'
            ")\n",
            encoding="utf-8",
        )
        doc_only = root / "doconly"
        doc_only.mkdir()
        (doc_only / "extension.py").write_text(
            '"""文档里写的扩展。\n\n第二段。"""\n', encoding="utf-8"
        )
        both = root / "both"
        both.mkdir()
        (both / "run.py").write_text("", encoding="utf-8")
        (both / "extension.py").write_text("EXTENSION_NAV = ()\n", encoding="utf-8")

        items = {item.name: item for item in self.nav.extension_items(root)}
        self.assertEqual(sorted(items), ["demoext", "doconly"])
        self.assertEqual(items["demoext"].title, "演示扩展")
        self.assertEqual(items["demoext"].description, "扩展说明")
        self.assertEqual(items["demoext"].entry, demoext / "extension.py")
        self.assertEqual(items["doconly"].title, "文档里写的扩展。")
        self.assertEqual(items["doconly"].description, "")
        # 目录里同时有 run.py 时算接口项目，不当扩展
        self.assertEqual(self.nav.site_names(root), ["both"])

    def test_broken_or_missing_metadata_falls_back_to_the_directory_name(self):
        root = self._fake_root()
        broken = root / "broken"
        broken.mkdir()
        (broken / "run.py").write_text("", encoding="utf-8")
        (broken / "app.py").write_text("SITE = Site(\n", encoding="utf-8")
        (broken / "__init__.py").write_text("", encoding="utf-8")

        plain = root / "plain"
        plain.mkdir()
        (plain / "run.py").write_text("", encoding="utf-8")

        sites = self.nav.site_items(root)
        self.assertEqual([item.name for item in sites], ["broken", "plain"])
        self.assertEqual([item.title for item in sites], ["broken", "plain"])
        self.assertEqual([item.order for item in sites], [100, 100])

        empty_root = self._fake_root()
        (empty_root / "empty").mkdir()
        text = self.nav.render_navigation(empty_root)
        self.assertIn("（没有找到：本目录下需要有 <接口项目>/run.py）", text)
        self.assertIn("（没有找到：扩展目录里需要有 extension.py）", text)
        self.assertNotIn("BRIDGE_SITE=", text)

    # ---------------- 挑站点：BRIDGE_SITE → 单个 → 问 → 默认 ----------------

    def test_non_interactive_stream_starts_the_first_site(self):
        printed = []
        name = self.nav.choose_site(
            self.nav.site_items(), stream=io.StringIO(), out=printed.append
        )
        self.assertEqual(name, "pixiv")
        self.assertTrue(any("没有可交互的终端" in line for line in printed))

    def test_interactive_choice_by_number_name_and_default(self):
        sites = self.nav.site_items()
        for answer, expected in (("2", "iwara"), ("iwara", "iwara"), ("1", "pixiv"), ("", "pixiv")):
            with self.subTest(answer=answer):
                self.assertEqual(
                    self.nav.choose_site(
                        sites,
                        stream=_FakeTty(),
                        ask=lambda _prompt, given=answer: given,
                        out=lambda *_: None,
                    ),
                    expected,
                )

    def test_interactive_choice_asks_again_after_bad_input(self):
        answers = iter(["???", "0", "iwara"])
        printed = []
        name = self.nav.choose_site(
            self.nav.site_items(),
            stream=_FakeTty(),
            ask=lambda _prompt: next(answers),
            out=printed.append,
        )
        self.assertEqual(name, "iwara")
        self.assertEqual(sum("没听懂" in line for line in printed), 2)

    def test_interactive_choice_prints_the_navigation_before_asking(self):
        printed = []
        self.nav.choose_site(
            self.nav.site_items(), stream=_FakeTty(), ask=lambda _p: "2", out=printed.append
        )
        self.assertTrue(any("项目导航" in line for line in printed))

    def test_single_api_project_is_never_asked_about(self):
        asked = []

        def ask(prompt):
            asked.append(prompt)
            return "x"

        name = self.nav.choose_site(
            self.nav.site_items()[:1], stream=_FakeTty(), ask=ask, out=lambda *_: None
        )
        self.assertEqual(name, "pixiv")
        self.assertEqual(asked, [])

    def test_bridge_site_wins_and_a_bad_value_is_reported(self):
        sites = self.nav.site_items()
        asked = []

        def ask(prompt):
            asked.append(prompt)
            return "pixiv"

        with patch.object(self.nav, "_explicit_site", return_value="iwara"):
            self.assertEqual(
                self.nav.choose_site(sites, stream=_FakeTty(), ask=ask, out=lambda *_: None),
                "iwara",
            )

        errors = []
        with patch.object(self.nav, "_explicit_site", return_value="nope"):
            self.assertIsNone(
                self.nav.choose_site(
                    sites,
                    stream=_FakeTty(),
                    ask=ask,
                    out=lambda *_: None,
                    err=errors.append,
                )
            )
        self.assertEqual(asked, [])
        self.assertEqual(len(errors), 1)
        self.assertIn("BRIDGE_SITE=nope", errors[0])
        self.assertIn("pixiv, iwara", errors[0])

    def test_explicit_site_prefers_the_environment_variable(self):
        with patch.dict(os.environ, {"BRIDGE_SITE": "iwara"}):
            self.assertEqual(self.nav._explicit_site(), "iwara")

    # ---------------- 启动：把入口交给 runpy ----------------

    def test_launch_runs_the_entry_as_main_with_the_given_arguments(self):
        self._keep_argv()
        entry = str(PROJECT_ROOT / "pixiv" / "run.py")
        with patch.object(self.nav.runpy, "run_path") as run_path:
            self.assertEqual(self.nav._launch("pixiv", ["--check"]), 0)
        run_path.assert_called_once_with(entry, run_name="__main__")
        self.assertEqual(sys.argv, [entry, "--check"])

    def test_main_dispatches_a_named_site_to_its_entry(self):
        root = self._fake_root()
        (root / "demo").mkdir()
        (root / "demo" / "run.py").write_text("", encoding="utf-8")
        self._keep_argv()
        with patch.object(self.nav, "ROOT", root), patch.object(self.nav, "runpy") as runpy_module:
            self.assertEqual(self.nav.main(["demo", "--check"]), 0)
        runpy_module.run_path.assert_called_once_with(
            str(root / "demo" / "run.py"), run_name="__main__"
        )
        self.assertEqual(sys.argv, [str(root / "demo" / "run.py"), "--check"])

    def test_main_without_arguments_starts_the_default_site(self):
        """没有终端可以问（管道 / Docker）时也必须真的启动，而不是打印用法后退出。"""
        root = self._fake_root()
        for name in ("beta", "alpha"):
            (root / name).mkdir()
            (root / name / "run.py").write_text("", encoding="utf-8")
        self._keep_argv()
        with patch.object(self.nav, "ROOT", root), patch.object(
            self.nav, "_explicit_site", return_value=""
        ), patch.object(sys, "stdin", io.StringIO()), patch.object(
            self.nav, "runpy"
        ) as runpy_module:
            self.assertEqual(self.nav.main([]), 0)
        runpy_module.run_path.assert_called_once_with(
            str(root / "alpha" / "run.py"), run_name="__main__"
        )

    def test_main_reports_an_unknown_site_and_an_empty_project(self):
        errors = io.StringIO()
        with patch.object(sys, "stderr", errors), patch.object(self.nav, "runpy") as runpy_module:
            self.assertEqual(self.nav.main(["nosuch"]), 2)
        runpy_module.run_path.assert_not_called()
        self.assertIn("没有这个接口项目: nosuch", errors.getvalue())
        self.assertIn("pixiv", errors.getvalue())

        root = self._fake_root()
        (root / "empty").mkdir()
        errors = io.StringIO()
        with patch.object(self.nav, "ROOT", root), patch.object(
            sys, "stderr", errors
        ), patch.object(sys, "stdin", io.StringIO()), patch.object(
            self.nav, "runpy"
        ) as runpy_module:
            self.assertEqual(self.nav.main([]), 2)
        runpy_module.run_path.assert_not_called()
        self.assertIn("没有找到任何接口项目", errors.getvalue())

    def test_help_prints_the_usage_before_the_navigation(self):
        printed = io.StringIO()
        with patch.object(sys, "stdout", printed):
            self.assertEqual(self.nav.main(["--help"]), 0)
        text = printed.getvalue()
        self.assertIn("用法:", text)
        self.assertIn("项目导航", text)
        self.assertLess(text.index("用法:"), text.index("项目导航"))

    def test_list_only_prints_the_navigation(self):
        printed = io.StringIO()
        with patch.object(sys, "stdout", printed):
            self.assertEqual(self.nav.main(["--list"]), 0)
        self.assertIn("项目导航", printed.getvalue())

    # ---------------- 两个启动脚本都只转发给导航 ----------------

    def test_start_scripts_delegate_to_the_module_navigation(self):
        bat = (PROJECT_ROOT / "start.bat").read_text(encoding="utf-8")
        sh = (PROJECT_ROOT / "start.sh").read_text(encoding="utf-8")
        for name, text in (("start.bat", bat), ("start.sh", sh)):
            with self.subTest(script=name):
                self.assertIn("-m pixiv_bridge", text)
                self.assertNotIn("$SITE/run.py", text)
        # .bat 必须是纯 ASCII：中文会被 cmd 的 936 代码页错解
        self.assertTrue(bat.isascii(), "start.bat 必须保持纯 ASCII")
        self.assertNotIn('set "SITE=', bat)
        self.assertIn("BRIDGE_SITE", bat)
        self.assertIn("BRIDGE_SITE", sh)


class BodyValidationTestCase(unittest.TestCase):
    """POST 请求体校验（原测试集里所有请求都是 GET，这块完全没覆盖）。

    未登录时请求体通过校验后由认证层拒绝 → 401；请求体不合法 → 422。
    """

    def _client(self):
        settings = make_settings(token="")
        settings.token_file = SERVER_TOKEN_FILE
        return TestClient(
            create_app(settings, transport=httpx.MockTransport(_mock_handler))
        )

    def test_valid_body_reaches_auth_layer(self):
        cases = (
            ("/api/bookmark/add", {"illust_id": 1}, None),
            ("/api/bookmark/delete", {"illust_id": 1}, None),
            ("/api/comment/add", {"illust_id": 1, "comment": "hi"}, None),
            ("/api/user/follow/add", {"user_id": 2}, None),
            ("/api/novel/bookmark/add", {"novel_id": 3}, None),
            ("/api/novel/bookmark/delete", {"novel_id": 3}, None),
            # watchlist 的 series_id 是查询参数
            # （见 pixiv_bridge/routes/pixiv_novel.py 的 watchlist_novel_add）
            ("/api/watchlist/novel/add", None, {"series_id": "1"}),
            ("/api/watchlist/manga/add", None, {"series_id": 1}),
        )
        for path, body, params in cases:
            with self.subTest(path=path), self._client() as client:
                resp = client.post(path, json=body, params=params)
                self.assertEqual(resp.status_code, 401)

    def test_invalid_body_is_rejected_with_422(self):
        cases = (
            ("/api/bookmark/add", {"illust_id": "not-a-number"}),
            ("/api/bookmark/add", {}),
            ("/api/comment/add", {"illust_id": 1}),
            ("/api/user/follow/add", {"user_id": "x"}),
            ("/api/novel/bookmark/add", {}),
        )
        for path, body in cases:
            with self.subTest(path=path, body=body), self._client() as client:
                resp = client.post(path, json=body)
                self.assertEqual(resp.status_code, 422)

    def test_missing_required_query_param_is_rejected(self):
        with self._client() as client:
            resp = client.post("/api/watchlist/novel/add")
        self.assertEqual(resp.status_code, 422)

    def test_body_defaults(self):
        from pixiv.routes.illust import BookmarkAddBody, CommentAddBody
        from pixiv.routes.pixiv_novel import NovelBookmarkAddBody
        from pixiv.routes.user import FollowAddBody

        body = BookmarkAddBody(illust_id=1)
        self.assertEqual(body.restrict, "public")
        self.assertIsNone(body.tags)
        self.assertEqual(BookmarkAddBody(illust_id=1, tags=["a", "b"]).tags, ["a", "b"])
        self.assertIsNone(CommentAddBody(illust_id=1, comment="x").parent_comment_id)
        self.assertEqual(NovelBookmarkAddBody(novel_id=1).restrict, "public")
        self.assertEqual(FollowAddBody(user_id=1).restrict, "public")


class TermuxRequirementsTestCase(unittest.TestCase):
    """Termux 上要改用纯 Python 依赖集（pydantic 2 的 pydantic-core 装不了）。"""

    def test_is_termux_detects_prefix_and_executable(self):
        with patch.dict(os.environ, {"PREFIX": "/data/data/com.termux/files/usr"}):
            self.assertTrue(deps_module.is_termux())
        termux_python = "/data/data/com.termux/files/usr/bin/python3"
        with patch.dict(os.environ, {"PREFIX": ""}), patch.object(
            deps_module.sys, "executable", termux_python
        ):
            self.assertTrue(deps_module.is_termux())
        with patch.dict(os.environ, {"PREFIX": ""}), patch.object(
            deps_module.sys, "executable", "/usr/bin/python3"
        ), patch.object(deps_module.sys, "platform", "linux"):
            self.assertFalse(deps_module.is_termux())

    def test_requirements_path_switches_on_termux(self):
        self.assertEqual(
            deps_module.requirements_path(PROJECT_ROOT),
            PROJECT_ROOT / "requirements.txt",
        )
        self.assertEqual(
            deps_module.requirements_path(PROJECT_ROOT, termux=True),
            PROJECT_ROOT / "requirements-termux.txt",
        )
        self.assertEqual(
            deps_module.requirements_path(PROJECT_ROOT, termux=False),
            PROJECT_ROOT / "requirements.txt",
        )

    def test_termux_requirements_pins_pure_python_pydantic(self):
        text = (PROJECT_ROOT / "requirements-termux.txt").read_text(encoding="utf-8")
        pins = [
            line.strip()
            for line in text.splitlines()
            if line.strip() and not line.strip().startswith("#")
        ]
        self.assertIn("pydantic<2", pins)
        self.assertIn("fastapi<0.116", pins)
        self.assertIn("uvicorn", pins)
        self.assertIn("httpx", pins)

    def test_ensure_dependencies_announces_termux(self):
        with patch.dict(
            os.environ, {"PREFIX": "/data/data/com.termux/files/usr"}
        ), patch(
            "pixiv_bridge.deps.missing_dependencies", side_effect=[["fastapi"], []]
        ), patch(
            "subprocess.check_call"
        ), patch("builtins.print") as mock_print:
            deps_module.ensure_dependencies(PROJECT_ROOT)
        printed = " ".join(str(call.args[0]) for call in mock_print.call_args_list)
        self.assertIn("Termux", printed)
        self.assertIn("requirements-termux.txt", printed)


class PydanticCompatTestCase(unittest.TestCase):
    """pydantic 1.x / 2.x 通用取字段：Termux 上装的是 1.x（没有 model_dump）。"""

    def test_prefers_model_dump_when_available(self):
        calls = []

        class V2:
            def model_dump(self, **kwargs):
                calls.append(kwargs)
                return {"a": 1}

        self.assertEqual(dump_model(V2(), exclude_unset=True), {"a": 1})
        self.assertEqual(calls, [{"exclude_unset": True}])

    def test_falls_back_to_dict_on_pydantic_v1(self):
        calls = []

        class V1:
            def dict(self, **kwargs):          # pydantic 1.x 的取字段方式
                calls.append(kwargs)
                return {"merge_series": False}

        self.assertEqual(dump_model(V1(), exclude_unset=True), {"merge_series": False})
        self.assertEqual(calls, [{"exclude_unset": True}])

    def test_without_either_method_raises_type_error(self):
        class Neither:
            pass

        with self.assertRaises(TypeError) as ctx:
            dump_model(Neither())
        self.assertIn("model_dump", str(ctx.exception))
        self.assertIn("dict", str(ctx.exception))

    def test_routes_never_call_model_dump_directly(self):
        """路由里必须走 dump_model()：直接 .model_dump() 在 Termux 上必 500。"""
        offenders = []
        for routes_dir in (
            PROJECT_ROOT / "pixiv_bridge" / "routes",
            PROJECT_ROOT / "novel" / "routes",
        ):
            for path in sorted(routes_dir.glob("*.py")):
                for number, line in enumerate(
                    path.read_text(encoding="utf-8").splitlines(), start=1
                ):
                    if ".model_dump(" in line and "def model_dump" not in line:
                        offenders.append("%s:%d" % (path.name, number))
        self.assertEqual(offenders, [])

    def test_real_pydantic_model_dumps_through_helper(self):
        from novel.routes.config import ConfigPatch

        self.assertEqual(dump_model(ConfigPatch(), exclude_unset=True), {})
        self.assertEqual(
            dump_model(ConfigPatch(merge_series=False), exclude_unset=True),
            {"merge_series": False},
        )


class NovelImageExtractionTestCase(unittest.TestCase):
    """插图 URL 的多来源解析：app 接口、网页 AJAX 形状、各种键名都要认。"""

    def test_embedded_images_recognized_in_app_and_web_shapes(self):
        # app-api /v2/novel/detail 的形状：urls 里是 128x128 / 480mw / 1200x1200 / original
        self.assertEqual(
            extract_novel_images(
                {
                    "textEmbeddedImages": {
                        "111": {
                            "urls": {
                                "128x128": "https://i.pximg.net/111-thumb.png",
                                "480mw": "https://i.pximg.net/111-mid.png",
                                "1200x1200": "https://i.pximg.net/111-large.png",
                                "original": "https://i.pximg.net/111-orig.png",
                            }
                        }
                    }
                }
            ),
            {
                "111": {
                    "thumb": "https://i.pximg.net/111-thumb.png",
                    "medium": "https://i.pximg.net/111-mid.png",
                    "large": "https://i.pximg.net/111-large.png",
                    "original": "https://i.pximg.net/111-orig.png",
                }
            },
        )
        # 网页版 AJAX（/ajax/novel/<id>）的 body 形状：直接给单张 URL，键名带 image_url 后缀
        self.assertEqual(
            extract_novel_images(
                {
                    "textEmbeddedImages": {
                        "222": {"original_image_url": "https://i.pximg.net/222.png"},
                        "333": {"large_image_url": "https://i.pximg.net/333.png"},
                        "444": "https://i.pximg.net/444.png",
                    }
                }
            ),
            {
                "222": {"original": "https://i.pximg.net/222.png"},
                "333": {"large": "https://i.pximg.net/333.png"},
                "444": {"original": "https://i.pximg.net/444.png"},
            },
        )
        # 键带前缀（"1:"）时按冒号后的 id 归位
        self.assertEqual(
            extract_novel_images(
                {"textEmbeddedImages": {"1:555": {"url": "https://i.pximg.net/555.png"}}}
            ),
            {"555": {"original": "https://i.pximg.net/555.png"}},
        )
        # 没有任何图片映射 → 空 dict（调用方据此判断「真的没有图」）
        self.assertEqual(extract_novel_images({"text": "没有图"}), {})

    def test_extract_images_from_html_reads_viewer_payload(self):
        html = (
            '<script>window.__DATA__ = {"novel": {"id": 2001}};</script>'
            'textEmbeddedImages: {"111": {"urls": {"1200x1200": "https://i.pximg.net/a.png"}}},'
            '"text": "正文"'
        )
        self.assertEqual(
            extract_images_from_html(html),
            {"111": {"large": "https://i.pximg.net/a.png"}},
        )
        # 网页里没有这段映射时不能瞎猜
        self.assertEqual(extract_images_from_html("<html>没有图</html>"), {})

    def test_novel_text_has_embeds_detects_uploaded_marker(self):
        self.assertTrue(novel_text_has_embeds("前文\n[uploadedimage:111]\n后文"))
        self.assertFalse(novel_text_has_embeds("只有 [pixivimage:222] 的正文"))


class HistoryStoreTestCase(unittest.TestCase):
    """项目目录下的阅读历史文件：追加写、按书折叠、坏行跳过、超限压缩。"""

    def setUp(self):
        self.tmp = make_temp_dir("history")
        self.path = self.tmp / "history.jsonl"
        self.store = HistoryStore(self.path)

    def tearDown(self):
        drop_temp_dir(self.tmp)

    def test_normalize_entry_fixes_fields(self):
        with self.assertRaises(ValueError):
            normalize_entry({"title": "没有 id"})
        with self.assertRaises(ValueError):
            normalize_entry("不是一个对象")  # type: ignore[arg-type]
        entry = normalize_entry(
            {
                "id": "2001",
                "title": "书" * 300,
                "user": "作者" * 200,
                "series_id": "999",
                "chapter_index": 2,
                "chapters": "3",
                "progress": 1.7,
                "read_at": " 2026-01-02T03:04:05+00:00 ",
                "看不出这个字段": True,
            }
        )
        self.assertEqual(entry["id"], 2001)
        self.assertEqual(entry["series_id"], 999)
        self.assertEqual(entry["chapter_index"], 2)
        self.assertEqual(entry["chapters"], 3)
        self.assertEqual(entry["progress"], 1.0)          # 夹到 0..1
        self.assertEqual(entry["read_at"], "2026-01-02T03:04:05+00:00")
        self.assertEqual(len(entry["title"]), 200)
        self.assertEqual(len(entry["user"]), 120)
        self.assertNotIn("看不出这个字段", entry)          # 多余字段丢掉
        self.assertEqual(normalize_entry({"id": 5, "progress": -3})["progress"], 0.0)
        self.assertIsNone(normalize_entry({"id": 5, "progress": "快读完了"})["progress"])
        self.assertEqual(normalize_entry({"id": 5})["read_at"][:2], "20")  # 自动补时间

    def test_append_and_fold_keeps_latest_per_novel(self):
        self.store.record({"id": 2001, "title": "书甲", "progress": 0.1, "read_at": "2026-01-01T00:00:00+00:00"})
        self.store.record({"id": 2002, "title": "书乙", "progress": 0.2, "read_at": "2026-01-02T00:00:00+00:00"})
        self.store.record({"id": 2001, "title": "书甲", "progress": 0.9, "read_at": "2026-01-03T00:00:00+00:00"})
        entries = self.store.entries()
        self.assertEqual([it["id"] for it in entries], [2001, 2002])   # 按时间倒序
        self.assertEqual(entries[0]["progress"], 0.9)                   # 同一本只留最新
        status = self.store.status()
        self.assertEqual(status["count"], 2)      # 去重后的本数
        self.assertEqual(status["lines"], 3)      # 文件里的合法行数
        self.assertEqual(status["bad_lines"], 0)
        self.assertEqual(status["file"], str(self.path))
        # 每条一行，能直接 tail / grep
        self.assertEqual(len(self.path.read_text(encoding="utf-8").strip().splitlines()), 3)
        self.assertIn('"progress": 0.9', self.path.read_text(encoding="utf-8"))

    def test_bad_lines_are_skipped(self):
        self.path.write_text(
            '{"id": 1, "title": "好行", "read_at": "2026-01-01T00:00:00+00:00"}\n'
            "这不是 JSON\n"
            "\n"
            '{"title": "没有 id"}\n'
            '{"id": 2, "title": "好行二", "read_at": "2026-01-02T00:00:00+00:00"}\n',
            encoding="utf-8",
        )
        status = self.store.status()
        self.assertEqual(status["bad_lines"], 2)
        self.assertEqual([it["id"] for it in status["entries"]], [2, 1])

    def test_compact_rewrites_one_line_per_novel(self):
        for i in range(30):
            self.store.record({"id": 1, "title": "书甲", "progress": i / 100})
            self.store.record({"id": 2, "title": "书乙", "progress": i / 100})
        self.assertEqual(len(self.path.read_text(encoding="utf-8").strip().splitlines()), 60)
        removed = self.store.compact()
        self.assertEqual(removed, 58)
        lines = self.path.read_text(encoding="utf-8").strip().splitlines()
        self.assertEqual(len(lines), 2)
        entries = self.store.entries()
        self.assertEqual(entries[0]["progress"], 0.29)     # 压缩保留每本最新一条

    def test_store_rejects_a_directory_path(self):
        """配置把路径写成了目录时要当场报错，而不是读文件时报 PermissionError。"""
        with self.assertRaises(ValueError):
            HistoryStore(self.tmp)

    def test_append_creates_missing_parent_directory(self):
        nested = self.tmp / "deep" / "dir" / "history.jsonl"
        store = HistoryStore(nested)
        store.record({"id": 7, "title": "新目录"})
        self.assertTrue(nested.exists())
        self.assertEqual(store.entries()[0]["id"], 7)


class HistoryRouteTestCase(unittest.TestCase):
    """GET/POST /api/history：写进项目里的文件，读回来是每本一条最新。"""

    def setUp(self):
        self.tmp = make_temp_dir("history_route")
        self.history_file = self.tmp / "history.jsonl"

    def tearDown(self):
        drop_temp_dir(self.tmp)

    def _client(self):
        settings = make_settings(token="")
        settings.token_file = SERVER_TOKEN_FILE
        settings.history_file = self.history_file
        return TestClient(app_with_novel(settings, transport=httpx.MockTransport(_mock_handler)))

    def test_history_round_trip(self):
        with self._client() as client:
            empty = client.get("/api/history")
            self.assertEqual(empty.status_code, 200)
            self.assertEqual(empty.json()["count"], 0)
            self.assertEqual(empty.json()["entries"], [])
            self.assertEqual(empty.json()["file"], str(self.history_file))

            posted = client.post(
                "/api/history",
                json={
                    "id": 2001,
                    "title": "插图试验作",
                    "user": "作者甲",
                    "series_id": 999,
                    "series_title": "长篇测试系列",
                    "chapter_index": 1,
                    "chapters": 2,
                    "progress": 0.42,
                },
            )
            self.assertEqual(posted.status_code, 200)
            body = posted.json()
            self.assertTrue(body["ok"])
            self.assertEqual(body["entry"]["id"], 2001)
            self.assertEqual(body["entry"]["progress"], 0.42)
            self.assertTrue(body["entry"]["read_at"])

            again = client.get("/api/history").json()
            self.assertEqual(again["count"], 1)
            item = again["entries"][0]
            self.assertEqual(item["title"], "插图试验作")
            self.assertEqual(item["series_id"], 999)
            self.assertEqual(item["chapter_index"], 1)
            self.assertTrue(self.history_file.exists())

            # 缺 id 直接被模型挡掉，不会写坏文件
            bad = client.post("/api/history", json={"title": "没有 id"})
            self.assertEqual(bad.status_code, 422)
            self.assertEqual(len(self.history_file.read_text(encoding="utf-8").strip().splitlines()), 1)

    def test_history_limit_can_be_zero(self):
        with self._client() as client:
            client.post("/api/history", json={"id": 1, "title": "甲"})
            client.post("/api/history", json={"id": 2, "title": "乙"})
            self.assertEqual(len(client.get("/api/history?limit=1").json()["entries"]), 1)
            self.assertEqual(len(client.get("/api/history?limit=0").json()["entries"]), 2)


# ---------------------------------------------------------------------------
# 以下为新增覆盖：通用 JSONL 存取、下载/收藏台账、用户数据与本地书架、配置路由。
# 只追加测试，不改动已有测试与任何源码。
# ---------------------------------------------------------------------------

from pixiv_bridge.jsonl import (
    MAX_LINE_BYTES,
    JsonlStore,
    as_int,
    as_ratio,
    as_text,
    iso_now,
)
from pixiv_bridge.userdata import (
    DEFAULT_CONFIG,
    LocalLibrary,
    UserConfig,
    is_inside,
    migrate_legacy_data,
)


class _MiniStore(JsonlStore):
    """测试用的最小 JsonlStore：主键是 id，time_field 是 at，墓碑记录折叠后消失。"""

    label = "小记录"
    time_field = "at"

    def normalize(self, raw, now=None):
        if not isinstance(raw, dict):
            raise ValueError("记录必须是一个 JSON 对象")
        key = as_int(raw.get("id"))
        if key is None:
            raise ValueError("记录缺少 id")
        entry = {
            "id": key,
            "text": as_text(raw.get("text"), 20),
            "at": as_text(raw.get("at"), 40).strip() or (now or iso_now()),
        }
        if raw.get("deleted"):
            entry["deleted"] = True
        return entry

    def identity(self, entry):
        key = as_int(entry.get("id"))
        if key is None:
            raise ValueError("记录缺少 id")
        return str(key)

    def keep(self, entry):
        return not entry.get("deleted")


class JsonlHelpersTestCase(unittest.TestCase):
    """jsonl.py 的通用小工具：时间戳、整数/比例/文本归一化（边界夹取与截断）。"""

    def test_iso_now_is_utc_seconds(self):
        text = iso_now()
        self.assertTrue(text.endswith("+00:00"))
        self.assertIn("T", text)
        self.assertEqual(len(text), 25)  # 2026-01-02T03:04:05+00:00

    def test_as_int(self):
        self.assertEqual(as_int("12"), 12)
        self.assertEqual(as_int(3.9), 3)
        self.assertEqual(as_int(-5), -5)
        self.assertIsNone(as_int("十二"))
        self.assertIsNone(as_int(None))
        self.assertIsNone(as_int({}))

    def test_as_ratio_clamps_and_rounds(self):
        self.assertEqual(as_ratio(1.7), 1.0)          # 上夹到 1
        self.assertEqual(as_ratio(-2), 0.0)           # 下夹到 0
        self.assertEqual(as_ratio("0.5"), 0.5)
        self.assertEqual(as_ratio(0.123456), 0.1235)  # round(...,4)
        self.assertEqual(as_ratio(0), 0.0)
        self.assertIsNone(as_ratio("快读完了"))
        self.assertIsNone(as_ratio(None))

    def test_as_text(self):
        self.assertEqual(as_text(None), "")
        self.assertEqual(as_text(123, 10), "123")
        self.assertEqual(as_text("abcdef", 3), "abc")
        self.assertEqual(as_text("默认长度", 200), "默认长度")
        self.assertEqual(len(as_text("x" * 300)), 200)


class JsonlStoreTestCase(unittest.TestCase):
    """JsonlStore：追加写一行一条、读取折叠、墓碑过滤、坏行跳过、压缩与清空。"""

    def setUp(self):
        self.tmp = make_temp_dir("jsonl")
        self.addCleanup(drop_temp_dir, self.tmp)
        self.path = self.tmp / "mini.jsonl"
        self.store = _MiniStore(self.path)

    def test_append_creates_parent_and_writes_one_line_per_entry(self):
        nested = self.tmp / "deep" / "dir" / "mini.jsonl"
        store = _MiniStore(nested)
        store.append({"id": 1, "text": "甲", "at": "2026-01-01T00:00:00+00:00"})
        store.append({"id": 2, "text": "乙", "at": "2026-01-02T00:00:00+00:00"})
        self.assertTrue(nested.exists())
        lines = nested.read_text(encoding="utf-8").strip().splitlines()
        self.assertEqual(len(lines), 2)
        self.assertEqual(lines[0], '{"at": "2026-01-01T00:00:00+00:00", "id": 1, "text": "甲"}')

    def test_record_normalizes_and_read_all_returns_entries(self):
        entry = self.store.record({"id": "7", "text": "很长" * 30})
        self.assertEqual(entry["id"], 7)
        self.assertEqual(len(entry["text"]), 20)   # as_text 截断
        self.assertTrue(entry["at"])              # 自动补时间
        entries, bad = self.store.read_all()
        self.assertEqual(bad, 0)
        self.assertEqual(entries[0]["id"], 7)

    def test_read_all_skips_bad_lines_and_foreign_records(self):
        self.path.write_text(
            '{"id": 1, "text": "好", "at": "2026-01-01T00:00:00+00:00"}\n'
            "这不是 JSON\n"
            "\n"
            '{"text": "没有 id"}\n'
            '{"id": 1, "text": "' + "x" * (MAX_LINE_BYTES + 10) + '"}\n',
            encoding="utf-8",
        )
        entries, bad = self.store.read_all()
        self.assertEqual(len(entries), 1)
        self.assertEqual(bad, 3)  # 非 JSON / 缺主键 / 超长行
        status = self.store.status()
        self.assertEqual(status["bad_lines"], 3)
        self.assertEqual(status["lines"], 1)
        self.assertEqual(status["count"], 1)
        self.assertEqual(status["file"], str(self.path))

    def test_fold_keeps_latest_per_key_sorted_desc(self):
        self.store.record({"id": 1, "text": "旧", "at": "2026-01-01T00:00:00+00:00"})
        self.store.record({"id": 2, "text": "乙", "at": "2026-01-03T00:00:00+00:00"})
        self.store.record({"id": 1, "text": "新", "at": "2026-01-02T00:00:00+00:00"})
        entries = self.store.entries()
        self.assertEqual([it["id"] for it in entries], [2, 1])   # 时间倒序
        self.assertEqual(entries[1]["text"], "新")               # 同主键留最新

    def test_keep_filters_out_tombstones(self):
        self.store.record({"id": 1, "text": "在架", "at": "2026-01-01T00:00:00+00:00"})
        self.store.append({"id": 1, "deleted": True, "at": "2026-01-02T00:00:00+00:00"})
        self.assertEqual(self.store.entries(), [])
        self.assertEqual(self.store.status()["count"], 0)
        self.assertEqual(self.store.status()["lines"], 2)   # 历史仍留痕

    def test_entries_limit(self):
        for i in (1, 2, 3):
            self.store.record({"id": i, "at": "2026-01-0%dT00:00:00+00:00" % i})
        self.assertEqual(len(self.store.entries(0)), 3)   # 0 = 不限
        self.assertEqual(len(self.store.entries(2)), 2)
        self.assertEqual(self.store.entries(1)[0]["id"], 3)

    def test_sort_key_uses_time_field(self):
        self.assertEqual(self.store.sort_key({"at": "2026-01-01"}), "2026-01-01")
        self.assertEqual(self.store.sort_key({}), "")

    def test_compact_rewrites_one_line_per_key(self):
        for i in range(30):
            stamp = "2026-01-01T00:00:%02d+00:00" % i
            self.store.record({"id": 1, "text": "甲%d" % i, "at": stamp})
            self.store.record({"id": 2, "text": "乙%d" % i, "at": stamp})
        self.assertEqual(len(self.path.read_text(encoding="utf-8").strip().splitlines()), 60)
        removed = self.store.compact()
        self.assertEqual(removed, 58)
        lines = self.path.read_text(encoding="utf-8").strip().splitlines()
        self.assertEqual(len(lines), 2)
        self.assertEqual(self.store.entries()[0]["text"], "甲29")   # 压缩保留最新一条

    def test_clear_keeps_the_file_but_empties_it(self):
        self.store.record({"id": 1})
        self.store.clear()
        self.assertTrue(self.path.exists())
        self.assertEqual(self.path.read_text(encoding="utf-8"), "")
        self.assertEqual(self.store.read_all(), ([], 0))
        self.assertEqual(self.store.status()["count"], 0)

    def test_store_rejects_a_directory_path(self):
        with self.assertRaises(ValueError):
            _MiniStore(self.tmp)


class DownloadNormalizeTestCase(unittest.TestCase):
    """normalize_download：三种 id 别名、缺 id/非 dict 报错、字段归一化与多余字段丢弃。"""

    def test_id_aliases(self):
        cases = (({"book_id": 1}, 1), ({"id": 2}, 2), ({"novel_id": 3}, 3), ({"book_id": "4"}, 4))
        for raw, expected in cases:
            with self.subTest(raw=raw):
                self.assertEqual(normalize_download(raw)["book_id"], expected)

    def test_missing_id_and_non_dict_raise(self):
        with self.assertRaises(ValueError):
            normalize_download({"title": "没有 id"})
        with self.assertRaises(ValueError):
            normalize_download(None)
        with self.assertRaises(ValueError):
            normalize_download([1, 2])

    def test_fields_are_normalized_and_extras_dropped(self):
        entry = normalize_download(
            {
                "book_id": "7",
                "chapter_index": None,
                "format": "EPUB",
                "bytes": "abc",
                "title": "题" * 300,
                "chapters": "3",
                "看不出的字段": True,
            }
        )
        self.assertEqual(entry["book_id"], 7)
        self.assertEqual(entry["chapter_index"], 0)      # 缺省 0
        self.assertEqual(entry["chapter_id"], 7)         # 缺 chapter_id 回落到 book_id
        self.assertEqual(entry["format"], "epub")
        self.assertEqual(entry["bytes"], 0)              # 非数字 → 0
        self.assertEqual(entry["chapters"], 3)
        self.assertEqual(len(entry["title"]), 200)
        self.assertTrue(entry["at"])                     # 自动补时间
        self.assertNotIn("看不出的字段", entry)

    def test_format_only_epub_else_txt(self):
        # format 只做 lower()、不 strip 空白：带空格的 " EPUB " 会被当成未知格式
        self.assertEqual(normalize_download({"book_id": 1, "format": "EPUB"})["format"], "epub")
        self.assertEqual(normalize_download({"book_id": 1, "format": " EPUB "})["format"], "txt")
        self.assertEqual(normalize_download({"book_id": 1, "format": "PDF"})["format"], "txt")
        self.assertEqual(normalize_download({"book_id": 1})["format"], "txt")
        self.assertEqual(normalize_download({"book_id": 1, "bytes": "12"})["bytes"], 12)


class DownloadStoreTestCase(unittest.TestCase):
    """DownloadStore：主键 book_id#chapter_index、同系列两个「第一章」互不覆盖、过滤与状态。"""

    def setUp(self):
        self.tmp = make_temp_dir("downloads")
        self.addCleanup(drop_temp_dir, self.tmp)
        self.path = self.tmp / "downloads.jsonl"
        self.store = DownloadStore(self.path)

    def test_identity_is_book_and_chapter(self):
        self.store.record({"book_id": 1, "chapter_index": 1, "title": "旧", "at": "2026-01-01T00:00:00+00:00"})
        self.store.record({"book_id": 1, "chapter_index": 1, "title": "新", "at": "2026-01-02T00:00:00+00:00"})
        entries = self.store.entries()
        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0]["title"], "新")
        self.assertEqual(self.store.identity(entries[0]), "1#1")

    def test_two_first_chapters_of_one_series_are_independent(self):
        self.store.record({"book_id": 100, "chapter_index": 1, "series_id": 5, "title": "甲的第一章", "at": "2026-01-01T00:00:00+00:00"})
        self.store.record({"book_id": 200, "chapter_index": 1, "series_id": 5, "title": "乙的第一章", "at": "2026-01-02T00:00:00+00:00"})
        entries = self.store.entries(0)
        self.assertEqual(len(entries), 2)
        self.assertEqual(sorted(it["book_id"] for it in entries), [100, 200])
        self.assertEqual(self.store.status()["count"], 2)

    def test_filter_by_series_and_book(self):
        self.store.record({"book_id": 1, "chapter_index": 1, "series_id": 10, "at": "2026-01-01T00:00:00+00:00"})
        self.store.record({"book_id": 2, "chapter_index": 1, "series_id": 20, "at": "2026-01-02T00:00:00+00:00"})
        self.store.record({"book_id": 1, "chapter_index": 2, "series_id": 10, "at": "2026-01-03T00:00:00+00:00"})
        self.assertEqual([it["book_id"] for it in self.store.filter(series_id=10)], [1, 1])
        self.assertEqual(len(self.store.filter(book_id=2)), 1)
        self.assertEqual(len(self.store.filter(series_id=999)), 0)
        self.assertEqual(len(self.store.filter()), 3)

    def test_series_progress_maps_chapter_index(self):
        self.store.record({"book_id": 1, "chapter_index": 1, "series_id": 7, "file": "a.epub", "bytes": 10, "format": "epub", "at": "2026-01-01T00:00:00+00:00"})
        self.store.record({"book_id": 1, "chapter_index": 2, "series_id": 7, "file": "b.epub", "bytes": 20, "format": "epub", "at": "2026-01-02T00:00:00+00:00"})
        progress = self.store.series_progress(7)
        self.assertEqual(set(progress), {"1", "2"})
        self.assertEqual(
            progress["1"],
            {"at": "2026-01-01T00:00:00+00:00", "file": "a.epub", "bytes": 10, "format": "epub"},
        )

    def test_status_count_held_and_entries(self):
        # limit 先作用于「折叠后的最新记录」再由 series_id 过滤，所以让目标记录最新
        self.store.record({"book_id": 2, "series_id": 8, "at": "2026-01-01T00:00:00+00:00"})
        self.store.record({"book_id": 1, "series_id": 7, "at": "2026-01-02T00:00:00+00:00"})
        data = self.store.status(limit=1, series_id=7)
        self.assertEqual(data["count"], 2)          # 全部主键数（不受过滤影响）
        self.assertEqual(data["held"], 1)           # 过滤后条数
        self.assertEqual(len(data["entries"]), 1)
        self.assertEqual(data["entries"][0]["book_id"], 1)
        self.assertEqual(data["lines"], 2)
        self.assertEqual(data["file"], str(self.path))

    def test_clear_empties_but_keeps_file(self):
        self.store.record({"book_id": 1})
        self.store.clear()
        self.assertTrue(self.path.exists())
        self.assertEqual(self.path.read_text(encoding="utf-8"), "")
        self.assertEqual(self.store.entries(0), [])
        self.assertEqual(self.store.status()["count"], 0)


class BookmarkNormalizeTestCase(unittest.TestCase):
    """normalize_bookmark：restrict 默认/归一化、墓碑标记只在删除时出现、缺 id 报错。"""

    def test_defaults_and_restrict(self):
        entry = normalize_bookmark({"id": 1})
        self.assertEqual(entry["id"], 1)
        self.assertEqual(entry["restrict"], "public")
        self.assertNotIn("deleted", entry)
        self.assertTrue(entry["at"])
        self.assertEqual(normalize_bookmark({"novel_id": "2", "restrict": "Private"})["restrict"], "private")
        self.assertEqual(normalize_bookmark({"id": 3, "restrict": "私密"})["restrict"], "public")

    def test_deleted_flag_only_for_tombstone(self):
        self.assertTrue(normalize_bookmark({"id": 4, "deleted": True})["deleted"])
        self.assertNotIn("deleted", normalize_bookmark({"id": 5, "deleted": False}))

    def test_missing_id_and_non_dict_raise(self):
        with self.assertRaises(ValueError):
            normalize_bookmark({"title": "没有 id"})
        with self.assertRaises(ValueError):
            normalize_bookmark("不是一个对象")


class BookmarkStoreTestCase(unittest.TestCase):
    """BookmarkStore：has/remove 墓碑语义，取消后再收藏同一本 has 又为 True。"""

    def setUp(self):
        self.tmp = make_temp_dir("bookmarks")
        self.addCleanup(drop_temp_dir, self.tmp)
        self.path = self.tmp / "bookmarks.jsonl"
        self.store = BookmarkStore(self.path)

    def test_has_and_remove_writes_tombstone(self):
        self.store.record({"id": 1, "title": "收藏甲", "at": "2026-01-01T00:00:00+00:00"})
        self.assertTrue(self.store.has(1))
        self.assertFalse(self.store.has(2))
        self.assertEqual(self.store.status()["count"], 1)
        self.store.remove(1, now="2026-01-02T00:00:00+00:00")
        self.assertFalse(self.store.has(1))
        self.assertEqual(self.store.entries(), [])
        self.assertEqual(self.store.status()["count"], 0)
        self.assertEqual(self.store.status()["lines"], 2)   # 历史留痕

    def test_readd_after_remove_makes_has_true_again(self):
        self.store.record({"id": 1, "at": "2026-01-01T00:00:00+00:00"})
        self.store.remove(1, now="2026-01-02T00:00:00+00:00")
        self.assertFalse(self.store.has(1))
        self.store.record({"id": 1, "title": "再收藏", "at": "2026-01-03T00:00:00+00:00"})
        self.assertTrue(self.store.has(1))
        self.assertEqual(self.store.entries()[0]["title"], "再收藏")

    def test_remove_requires_an_id(self):
        with self.assertRaises(ValueError):
            self.store.remove(None)


class MigrateLegacyDataTestCase(unittest.TestCase):
    """migrate_legacy_data：把项目根的旧数据搬进 data/，不覆盖、幂等、自动建目录。"""

    def setUp(self):
        self.tmp = make_temp_dir("migrate")
        self.addCleanup(drop_temp_dir, self.tmp)

    def test_moves_legacy_files_into_data_dir(self):
        root = self.tmp / "root"
        root.mkdir()
        (root / "tokens.json").write_text('{"refresh_token":"rt"}', encoding="utf-8")
        (root / "history.jsonl").write_text('{"id":1}\n', encoding="utf-8")
        data_dir = root / "data"
        moved = migrate_legacy_data(root, data_dir)
        self.assertEqual(moved, ("tokens.json", "history.jsonl"))
        self.assertFalse((root / "tokens.json").exists())
        self.assertEqual(
            (data_dir / "tokens.json").read_text(encoding="utf-8"),
            '{"refresh_token":"rt"}',
        )
        self.assertTrue((data_dir / "history.jsonl").is_file())

    def test_existing_target_is_not_overwritten_and_call_is_idempotent(self):
        root = self.tmp / "root"
        root.mkdir()
        data_dir = root / "data"
        data_dir.mkdir()
        (data_dir / "tokens.json").write_text("new", encoding="utf-8")
        (root / "tokens.json").write_text("old", encoding="utf-8")
        self.assertEqual(migrate_legacy_data(root, data_dir), ())   # 目标已存在 → 不搬
        self.assertEqual((data_dir / "tokens.json").read_text(encoding="utf-8"), "new")
        self.assertTrue((root / "tokens.json").exists())
        (root / "history.jsonl").write_text("h", encoding="utf-8")
        self.assertEqual(migrate_legacy_data(root, data_dir), ("history.jsonl",))
        self.assertEqual(migrate_legacy_data(root, data_dir), ())   # 幂等

    def test_missing_source_is_a_noop(self):
        root = self.tmp / "empty"
        root.mkdir()
        self.assertEqual(migrate_legacy_data(root, root / "data"), ())
        self.assertFalse((root / "data").exists())

    def test_creates_missing_data_dir(self):
        root = self.tmp / "root"
        root.mkdir()
        (root / "tokens.json").write_text("t", encoding="utf-8")
        self.assertEqual(migrate_legacy_data(root, root / "data"), ("tokens.json",))
        self.assertTrue((root / "data").is_dir())


class IsInsideTestCase(unittest.TestCase):
    """is_inside：同一路径/目录内为 True、目录外为 False、Windows 下大小写不敏感。"""

    def setUp(self):
        self.tmp = make_temp_dir("inside")
        self.addCleanup(drop_temp_dir, self.tmp)

    def test_same_path_and_inside(self):
        (self.tmp / "sub").mkdir()
        self.assertTrue(is_inside(self.tmp / "sub" / "f.txt", self.tmp))
        self.assertTrue(is_inside(self.tmp / "sub", self.tmp))
        self.assertTrue(is_inside(self.tmp, self.tmp))
        self.assertFalse(is_inside(self.tmp / "other" / "f.txt", self.tmp / "sub"))

    def test_windows_case_insensitive(self):
        if os.name != "nt":
            self.skipTest("大小写不敏感只在 Windows 上成立")
        (self.tmp / "Sub").mkdir()
        self.assertTrue(is_inside(self.tmp / "Sub" / "f.txt", self.tmp / "sub"))


class UserConfigTestCase(unittest.TestCase):
    """UserConfig：坏文件退回默认值、保存只认已知键且原子、非法值抛 ValueError。"""

    def setUp(self):
        self.tmp = make_temp_dir("userconfig")
        self.addCleanup(drop_temp_dir, self.tmp)
        self.config_file = self.tmp / "settings.json"

    def test_missing_file_returns_defaults(self):
        config = UserConfig(self.config_file)
        self.assertEqual(config.load(), dict(DEFAULT_CONFIG))
        self.assertEqual(config.as_dict(), dict(DEFAULT_CONFIG))

    def test_broken_or_invalid_file_falls_back_to_defaults(self):
        for raw in ("不是 JSON", "[1, 2, 3]", "null", '{"scan_depth": 99}', '{"max_files": "x"}'):
            with self.subTest(raw=raw):
                self.config_file.write_text(raw, encoding="utf-8")
                self.assertEqual(UserConfig(self.config_file).load(), dict(DEFAULT_CONFIG))

    def test_save_ignores_unknown_keys_and_writes_atomically(self):
        config = UserConfig(self.config_file)
        saved = config.save({"merge_series": False, "看不出这个键": "drop"})
        self.assertIs(saved["merge_series"], False)
        self.assertNotIn("看不出这个键", saved)
        self.assertTrue(self.config_file.exists())
        self.assertFalse((self.tmp / "settings.json.tmp").exists())   # 没有 .tmp 残留
        text = self.config_file.read_text(encoding="utf-8")
        self.assertIn('\n  "library_dirs": []', text)                 # indent=2
        self.assertTrue(text.endswith("\n"))
        self.assertEqual(UserConfig(self.config_file).load(), saved)   # load 能读回

    def test_library_dirs_is_deduped_and_tilde_expanded(self):
        config = UserConfig(self.config_file)
        saved = config.save({"library_dirs": ["~/pixiv-books", "~/pixiv-books"]})
        self.assertEqual(saved["library_dirs"], [os.path.expanduser("~/pixiv-books")])

    def test_invalid_values_raise_value_error(self):
        config = UserConfig(self.config_file)
        cases = (
            {"library_dirs": "不是列表"},
            {"library_dirs": [str(i) for i in range(21)]},      # 超过 20 个
            {"library_dirs": ["x" * 501]},                      # 单路径超过 500
            {"scan_depth": 0},
            {"scan_depth": 7},
            {"scan_depth": 99},
            {"scan_depth": "abc"},
            {"max_files": 49},
            {"max_files": 5001},
            {"merge_series": "也许"},
        )
        for patch_body in cases:
            with self.subTest(patch=patch_body):
                with self.assertRaises(ValueError):
                    UserConfig(self.config_file).save(patch_body)

    def test_boundary_values_are_accepted(self):
        config = UserConfig(self.config_file)
        saved = config.save({"scan_depth": 1, "max_files": 50})
        self.assertEqual(saved["scan_depth"], 1)
        self.assertEqual(saved["max_files"], 50)
        saved = config.save({"scan_depth": 6, "max_files": 5000, "merge_series": "no"})
        self.assertEqual(saved["scan_depth"], 6)
        self.assertEqual(saved["max_files"], 5000)
        self.assertIs(saved["merge_series"], False)

    def test_directory_path_raises(self):
        with self.assertRaises(ValueError):
            UserConfig(self.tmp)

    def test_save_survives_readonly_target(self):
        """目标带只读属性时也要能存上（Windows 上 os.replace 会 WinError 5）。"""
        config = UserConfig(self.config_file)
        config.save({"merge_series": False})
        os.chmod(self.config_file, stat.S_IREAD)
        self.addCleanup(os.chmod, self.config_file, stat.S_IWRITE)
        saved = config.save({"scan_depth": 2})
        self.assertEqual(saved["scan_depth"], 2)
        self.assertEqual(UserConfig(self.config_file).load()["scan_depth"], 2)
        self.assertFalse((self.tmp / "settings.json.tmp").exists())

    def test_save_falls_back_when_replace_is_blocked(self):
        """os.replace 被占用挡住：先退避重试，再退回直接覆盖写，且不留 .tmp。"""
        config = UserConfig(self.config_file)
        config.save({"merge_series": True})
        real_replace = os.replace
        seen = []

        def blocked(src, dst, *args, **kwargs):
            seen.append(str(dst))
            raise PermissionError(13, "拒绝访问。")

        os.replace = blocked
        try:
            saved = config.save({"merge_series": False})
        finally:
            os.replace = real_replace
        self.assertGreaterEqual(len(seen), 3)              # 重试过，不是一次就放弃
        self.assertIs(saved["merge_series"], False)
        self.assertEqual(UserConfig(self.config_file).load()["merge_series"], False)
        self.assertFalse((self.tmp / "settings.json.tmp").exists())

    def test_save_falls_back_on_cross_device_replace(self):
        """安卓 /sdcard（FUSE）上 os.replace 会 EXDEV：不该白等重试，直接退回覆盖写。"""
        config = UserConfig(self.config_file)
        config.save({"merge_series": True})
        real_replace = os.replace
        seen = []

        def cross_device(src, dst, *args, **kwargs):
            seen.append(str(dst))
            raise OSError(errno.EXDEV, "Invalid cross-device link")

        os.replace = cross_device
        try:
            saved = config.save({"scan_depth": 2})
        finally:
            os.replace = real_replace
        self.assertEqual(len(seen), 1)                     # 不重试，立刻覆盖写
        self.assertEqual(saved["scan_depth"], 2)
        self.assertEqual(UserConfig(self.config_file).load()["scan_depth"], 2)
        self.assertFalse((self.tmp / "settings.json.tmp").exists())

    def test_check_writable_dir_reports_reason(self):
        from pixiv_bridge.userdata import check_writable_dir

        ok, why = check_writable_dir(self.tmp)
        self.assertTrue(ok, why)
        self.assertEqual(why, "")
        blocker = self.tmp / "blocked"
        blocker.mkdir()
        real_write_text = Path.write_text

        def blocked_write_text(path_self, *args, **kwargs):
            raise PermissionError(13, "拒绝访问。")

        Path.write_text = blocked_write_text
        try:
            ok, why = check_writable_dir(blocker)
        finally:
            Path.write_text = real_write_text
        self.assertFalse(ok)
        self.assertIn("写不进去", why)
        self.assertFalse((blocker / ".pixiv-bridge-write-test").exists())

    def test_save_error_still_cleans_tmp_and_raises(self):
        """连直接覆盖写也失败：抛 OSError（路由转成带说明的 500）且不留 .tmp。"""
        config = UserConfig(self.config_file)
        config.save({})
        real_replace = os.replace
        real_write_text = Path.write_text

        def blocked_replace(src, dst, *args, **kwargs):
            raise PermissionError(13, "拒绝访问。")

        def blocked_write_text(path_self, *args, **kwargs):
            if path_self.name == "settings.json":          # 只挡目标文件，.tmp 仍能写
                raise PermissionError(13, "拒绝访问。")
            return real_write_text(path_self, *args, **kwargs)

        os.replace = blocked_replace
        Path.write_text = blocked_write_text
        try:
            with self.assertRaises(PermissionError):
                config.save({"scan_depth": 5})
        finally:
            os.replace = real_replace
            Path.write_text = real_write_text
        self.assertFalse((self.tmp / "settings.json.tmp").exists())


class LocalLibraryTestCase(unittest.TestCase):
    """LocalLibrary：目录过滤、扫描（扩展名/隐藏目录/层数/mtime/截断）、resolve 与 preview。"""

    def setUp(self):
        self.tmp = make_temp_dir("library")
        self.addCleanup(drop_temp_dir, self.tmp)
        self.books = self.tmp / "books"
        self.books.mkdir(parents=True, exist_ok=True)
        self.config_file = self.tmp / "settings.json"
        self.config = UserConfig(self.config_file)
        self.config.save({"library_dirs": [str(self.books)]})
        self.library = LocalLibrary(self.config)

    def _write(self, path, text="内容"):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
        return path

    def test_dirs_ignores_missing_and_non_directory_entries(self):
        plain = self._write(self.tmp / "plain.txt", "x")
        missing = self.tmp / "gone"
        self.config.save({"library_dirs": [str(self.books), str(plain), str(missing)]})
        self.assertEqual(self.library.dirs(), [self.books])
        missing_reported = self.library.missing_dirs()
        self.assertIn(str(missing), missing_reported)
        self.assertTrue(any("不是目录" in item for item in missing_reported))

    def test_scan_collects_only_txt_and_epub_and_skips_hidden(self):
        self._write(self.books / "visible.txt")
        self._write(self.books / "book.epub")
        self._write(self.books / "ignore.pdf")
        self._write(self.books / "ignore.md")
        self._write(self.books / "UPPER.TXT")
        self._write(self.books / ".hidden" / "secret.txt")
        result = self.library.scan()
        self.assertEqual(
            sorted(item["name"] for item in result["files"]),
            ["UPPER.TXT", "book.epub", "visible.txt"],
        )
        self.assertEqual(result["count"], 3)
        self.assertIs(result["truncated"], False)
        self.assertEqual(result["dirs"], [str(self.books)])
        self.assertEqual(result["errors"], [])
        self.assertEqual(result["missing"], [])
        self.assertEqual(
            {item["name"]: item["kind"] for item in result["files"]},
            {"UPPER.TXT": "txt", "book.epub": "epub", "visible.txt": "txt"},
        )

    def test_scan_respects_scan_depth(self):
        self._write(self.books / "a" / "b" / "c" / "d.txt", "deep")
        self.config.save({"scan_depth": 1})
        self.assertEqual(self.library.scan()["count"], 0)   # 3 层以下收不到
        self.config.save({"scan_depth": 3})
        self.assertEqual(self.library.scan()["count"], 1)

    def test_scan_sorts_by_mtime_desc(self):
        old = self._write(self.books / "old.txt", "old")
        new = self._write(self.books / "new.txt", "new")
        os.utime(old, (1000, 1000))
        os.utime(new, (2000, 2000))
        names = [item["name"] for item in self.library.scan()["files"]]
        self.assertEqual(names, ["new.txt", "old.txt"])

    def test_scan_truncates_at_max_files(self):
        for i in range(51):
            self._write(self.books / ("f%02d.txt" % i))
        self.config.save({"max_files": 50})
        result = self.library.scan()
        self.assertEqual(result["count"], 50)
        self.assertIs(result["truncated"], True)

    def test_resolve_accepts_inside_and_rejects_everything_else(self):
        inside = self._write(self.books / "ok.txt", "ok")
        self.assertEqual(self.library.resolve(str(inside)), inside.resolve())
        bad = (
            "",
            str(self._write(self.books / "ok.pdf", "pdf")),
            str(self.books / "nope.txt"),
            str(self._write(self.tmp / "outside" / "x.txt", "x")),
        )
        for path in bad:
            with self.subTest(path=path):
                with self.assertRaises(ValueError):
                    self.library.resolve(path)
        # 没有配置目录时也拒绝
        empty = LocalLibrary(UserConfig(self.tmp / "other_settings.json"))
        with self.assertRaises(ValueError):
            empty.resolve(str(inside))

    def test_preview_txt_and_truncation(self):
        txt = self._write(self.books / "preview.txt", "0123456789")
        preview = self.library.preview(str(txt), max_bytes=8)
        self.assertEqual(preview["name"], "preview.txt")
        self.assertEqual(preview["path"], str(txt.resolve()))
        self.assertEqual(preview["size"], 10)
        self.assertIs(preview["truncated"], True)
        self.assertEqual(preview["text"], "01234567")
        full = self.library.preview(str(txt))
        self.assertIs(full["truncated"], False)
        self.assertEqual(full["text"], "0123456789")

    def test_preview_rejects_epub(self):
        epub = self._write(self.books / "b.epub", "epub")
        with self.assertRaises(ValueError):
            self.library.preview(str(epub))


class SettingsDataDirTestCase(unittest.TestCase):
    """PixivSettings 新默认值：个人数据都在 data/ 下；load(migrate=True) 自动搬旧数据。"""

    def setUp(self):
        self.root = make_temp_dir("settings_root")
        self.addCleanup(drop_temp_dir, self.root)

    def test_defaults_land_under_data_dir(self):
        with patch.dict(os.environ, {}, clear=True):
            settings = PixivSettings.load(str(self.root), migrate=False)
        self.assertEqual(settings.project_root, self.root)
        self.assertEqual(settings.data_dir, self.root / "data")
        self.assertEqual(settings.token_file, self.root / "data" / "tokens.json")
        self.assertEqual(settings.history_file, self.root / "data" / "history.jsonl")
        self.assertEqual(settings.downloads_file, self.root / "data" / "downloads.jsonl")
        self.assertEqual(settings.bookmarks_file, self.root / "data" / "bookmarks.jsonl")
        self.assertEqual(settings.config_file, self.root / "data" / "settings.json")

    def test_external_data_dir_is_pulled_back_into_project(self):
        """项目外的数据目录要收回项目内（安卓/Termux 项目外常常写不进 → 保存 500）。"""
        outside = make_temp_dir("settings_outside")
        self.addCleanup(drop_temp_dir, outside)
        (outside / "tokens.json").write_text('{"refresh_token":"rt"}', encoding="utf-8")
        with patch.dict(os.environ, {"PIXIV_DATA_DIR": str(outside)}, clear=True):
            settings = PixivSettings.load(str(self.root), migrate=True)
        # 路径全部落回项目里的 data/
        self.assertEqual(settings.data_dir, self.root / "data")
        self.assertEqual(settings.token_file, self.root / "data" / "tokens.json")
        self.assertEqual(settings.history_file, self.root / "data" / "history.jsonl")
        self.assertEqual(settings.downloads_file, self.root / "data" / "downloads.jsonl")
        self.assertEqual(settings.bookmarks_file, self.root / "data" / "bookmarks.jsonl")
        self.assertEqual(settings.config_file, self.root / "data" / "settings.json")
        # 项目外的 token 被搬进来，不至于「突然未登录」
        self.assertFalse((outside / "tokens.json").exists())
        self.assertEqual(
            (self.root / "data" / "tokens.json").read_text(encoding="utf-8"),
            '{"refresh_token":"rt"}',
        )
        notes = "\n".join(settings.warnings)
        self.assertIn("PIXIV_DATA_DIR", notes)
        self.assertIn("tokens.json", notes)

    def test_external_config_file_is_pulled_back_without_moving_when_migrate_off(self):
        outside = make_temp_dir("settings_outside_cfg")
        self.addCleanup(drop_temp_dir, outside)
        cfg = outside / "settings.json"
        cfg.write_text('{"scan_depth": 2}', encoding="utf-8")
        env = {"PIXIV_CONFIG_FILE": str(cfg)}
        with patch.dict(os.environ, env, clear=True):
            settings = PixivSettings.load(str(self.root), migrate=False)
        # 配置文件必须写在项目里（否则项目外写不动时又变成 500）
        self.assertEqual(settings.config_file, self.root / "data" / "settings.json")
        self.assertTrue(cfg.exists())          # migrate=False 不搬文件
        self.assertTrue(settings.warnings)

    def test_relative_data_dir_inside_project_is_kept(self):
        with patch.dict(os.environ, {"PIXIV_DATA_DIR": "mydata"}, clear=True):
            settings = PixivSettings.load(str(self.root), migrate=False)
        self.assertEqual(settings.data_dir, self.root / "mydata")
        self.assertEqual(settings.token_file, self.root / "mydata" / "tokens.json")
        self.assertEqual(settings.warnings, ())

    def test_load_migrates_legacy_files(self):
        (self.root / "tokens.json").write_text('{"refresh_token":"rt"}', encoding="utf-8")
        (self.root / "history.jsonl").write_text('{"id":1}\n', encoding="utf-8")
        with patch.dict(os.environ, {}, clear=True):
            settings = PixivSettings.load(str(self.root), migrate=True)
        self.assertEqual(settings.migrated_files, ("tokens.json", "history.jsonl"))
        self.assertFalse((self.root / "tokens.json").exists())
        self.assertFalse((self.root / "history.jsonl").exists())
        self.assertTrue((self.root / "data" / "tokens.json").is_file())
        self.assertTrue((self.root / "data" / "history.jsonl").is_file())
        self.assertEqual(settings.token_file, self.root / "data" / "tokens.json")

    def test_migrate_false_leaves_legacy_files_alone(self):
        (self.root / "tokens.json").write_text("t", encoding="utf-8")
        with patch.dict(os.environ, {}, clear=True):
            settings = PixivSettings.load(str(self.root), migrate=False)
        self.assertEqual(settings.migrated_files, ())
        self.assertTrue((self.root / "tokens.json").exists())
        self.assertFalse((self.root / "data" / "tokens.json").exists())


def _failing_bookmark_handler(status):
    """小说收藏接口返回指定错误码，其余（含 oauth 换 token）正常。"""

    def handler(request):
        if request.url.host == "i.pximg.net":
            return _mock_handler(request)
        if "novel/bookmark" in request.url.path:
            return httpx.Response(
                status, json={"error": {"message": "上游拒绝", "reason": "denied"}}
            )
        return httpx.Response(200, json={})

    return handler


class UserDataRouteBase(unittest.TestCase):
    """配置/书架/下载/收藏这批路由的公共脚手架：数据文件都指向临时目录。"""

    def setUp(self):
        self.tmp = make_temp_dir("userdata_route")
        self.addCleanup(drop_temp_dir, self.tmp)
        self.library_dir = self.tmp / "books"
        self.library_dir.mkdir(parents=True, exist_ok=True)
        self.config_file = self.tmp / "settings.json"
        self.downloads_file = self.tmp / "downloads.jsonl"
        self.bookmarks_file = self.tmp / "bookmarks.jsonl"
        self.history_file = self.tmp / "history.jsonl"

    def _settings(self, token=""):
        settings = make_settings(token=token)
        settings.token_file = self.tmp / "tokens.json"
        settings.data_dir = self.tmp
        settings.config_file = self.config_file
        settings.downloads_file = self.downloads_file
        settings.bookmarks_file = self.bookmarks_file
        settings.history_file = self.history_file
        return settings

    def _client(self, handler=None, token=""):
        transport = httpx.MockTransport(handler or _mock_handler)
        return TestClient(app_with_novel(self._settings(token=token), transport=transport))

    def _write(self, path, text="内容"):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
        return path

    def _set_library_dirs(self, client, dirs):
        resp = client.put("/api/config", json={"library_dirs": [str(d) for d in dirs]})
        self.assertEqual(resp.status_code, 200, resp.text)
        return resp


class ConfigRouteTestCase(UserDataRouteBase):
    """GET/PUT /api/config：默认值、写盘、library 字段与非法值 400。"""

    def test_get_config_defaults(self):
        with self._client() as client:
            resp = client.get("/api/config")
            self.assertEqual(resp.status_code, 200)
            body = resp.json()
            self.assertTrue(body["ok"])
            self.assertEqual(body["file"], str(self.config_file))
            self.assertEqual(body["data_dir"], str(self.tmp))
            self.assertEqual(body["config"], dict(DEFAULT_CONFIG))
            self.assertEqual(body["defaults"], dict(DEFAULT_CONFIG))
            self.assertEqual(body["migrated_files"], [])
            self.assertEqual(body["warnings"], [])
            self.assertTrue(body["writable"]["ok"], body["writable"])
            self.assertEqual(body["writable"]["dir"], str(self.tmp))
            self.assertEqual(body["library"], {"dirs": [], "missing": []})
            self.assertEqual(
                body["files"],
                {
                    "tokens": str(self.tmp / "tokens.json"),
                    "history": str(self.history_file),
                    "downloads": str(self.downloads_file),
                    "bookmarks": str(self.bookmarks_file),
                },
            )

    def test_put_config_updates_memory_and_file(self):
        with self._client() as client:
            resp = client.put(
                "/api/config",
                json={"library_dirs": [str(self.library_dir)], "merge_series": False},
            )
            self.assertEqual(resp.status_code, 200)
            body = resp.json()
            self.assertEqual(body["config"]["library_dirs"], [str(self.library_dir)])
            self.assertIs(body["config"]["merge_series"], False)
            self.assertEqual(body["library"]["dirs"], [str(self.library_dir)])
            # 已经写进文件，重新构造一个 UserConfig 也能读回同样的值
            self.assertEqual(UserConfig(self.config_file).load(), body["config"])

    def test_put_config_invalid_value_is_400(self):
        with self._client() as client:
            for patch_body in ({"scan_depth": 99}, {"max_files": 10}, {"scan_depth": 0}):
                with self.subTest(patch=patch_body):
                    resp = client.put("/api/config", json=patch_body)
                    self.assertEqual(resp.status_code, 400, resp.text)
                    self.assertIn("detail", resp.json())

    def test_put_config_write_error_is_500_with_hint(self):
        """写不动时不能是光秃秃的 500：要说清是哪个文件、为什么。"""
        with self._client() as client:
            real_replace = os.replace
            real_write_text = Path.write_text

            def blocked_replace(src, dst, *args, **kwargs):
                raise PermissionError(13, "拒绝访问。")

            def blocked_write_text(path_self, *args, **kwargs):
                if path_self.name == "settings.json":
                    raise PermissionError(13, "拒绝访问。")
                return real_write_text(path_self, *args, **kwargs)

            os.replace = blocked_replace
            Path.write_text = blocked_write_text
            try:
                resp = client.put("/api/config", json={"merge_series": False})
            finally:
                os.replace = real_replace
                Path.write_text = real_write_text
            self.assertEqual(resp.status_code, 500, resp.text)
            detail = resp.json()["detail"]
            self.assertIn("写入失败", detail)
            self.assertIn(str(self.config_file), detail)
            self.assertIn("只读", detail)
            self.assertIn("/sdcard", detail)          # 安卓 / Termux 的提示
            self.assertFalse((self.tmp / "settings.json.tmp").exists())

    def test_put_config_unexpected_error_is_500_with_reason(self):
        """连没预料到的异常也要带原因，不能只回 Internal Server Error。"""
        real_save = UserConfig.save

        def boom(self, *args, **kwargs):
            raise RuntimeError("磁盘满")

        UserConfig.save = boom
        try:
            with self._client() as client:
                resp = client.put("/api/config", json={"merge_series": False})
        finally:
            UserConfig.save = real_save
        self.assertEqual(resp.status_code, 500, resp.text)
        detail = resp.json()["detail"]
        self.assertIn("磁盘满", detail)
        self.assertIn("RuntimeError", detail)
        self.assertIn("/sdcard", detail)

    def test_put_config_reports_saved_even_if_state_read_fails(self):
        """设置已经写进去了，就不能因为「顺手读一下状态」失败而告诉用户保存失败。"""
        import novel.routes.config as config_route

        real_get_library = config_route.get_library

        def boom(request):
            raise RuntimeError("扫描炸了")

        config_route.get_library = boom
        try:
            with self._client() as client:
                resp = client.put("/api/config", json={"merge_series": False})
                self.assertEqual(resp.status_code, 200, resp.text)
                body = resp.json()
                self.assertTrue(body["saved"])
                self.assertIn("扫描炸了", body["payload_error"])
                # 顺手读状态的 GET 也要给原因，而不是光秃秃的 500
                get_resp = client.get("/api/config")
        finally:
            config_route.get_library = real_get_library
        self.assertFalse(UserConfig(self.config_file).load()["merge_series"])
        self.assertEqual(get_resp.status_code, 500, get_resp.text)
        self.assertIn("扫描炸了", get_resp.json()["detail"])


class LibraryRouteTestCase(UserDataRouteBase):
    """GET /api/library、/api/library/preview、/api/library/file 的受控读取。"""

    def test_library_list(self):
        self._write(self.library_dir / "甲.txt", "内容")
        self._write(self.library_dir / "乙.epub", "epub-bytes")
        with self._client() as client:
            self._set_library_dirs(client, [self.library_dir])
            resp = client.get("/api/library")
            self.assertEqual(resp.status_code, 200)
            body = resp.json()
            self.assertEqual(body["count"], 2)
            self.assertEqual(
                sorted(item["name"] for item in body["files"]), ["乙.epub", "甲.txt"]
            )
            self.assertEqual(
                {item["name"]: item["kind"] for item in body["files"]},
                {"甲.txt": "txt", "乙.epub": "epub"},
            )
            self.assertEqual(body["dirs"], [str(self.library_dir)])

    def test_library_preview_and_file(self):
        txt = self._write(self.library_dir / "note.txt", "0123456789")
        epub = self._write(self.library_dir / "book.epub", "epub-bytes")
        outside = self._write(self.tmp / "outside" / "secret.txt", "x")
        with self._client() as client:
            self._set_library_dirs(client, [self.library_dir])

            preview = client.get("/api/library/preview", params={"path": str(txt)})
            self.assertEqual(preview.status_code, 200)
            body = preview.json()
            self.assertEqual(body["name"], "note.txt")
            self.assertEqual(body["text"], "0123456789")
            self.assertEqual(body["size"], 10)
            self.assertIs(body["truncated"], False)

            self.assertEqual(
                client.get("/api/library/preview", params={"path": str(epub)}).status_code,
                400,
            )
            self.assertEqual(
                client.get("/api/library/preview", params={"path": str(outside)}).status_code,
                400,
            )

            opened = client.get("/api/library/file", params={"path": str(txt)})
            self.assertEqual(opened.status_code, 200)
            self.assertIn("note.txt", opened.headers["content-disposition"])
            self.assertEqual(opened.content, b"0123456789")

            epub_file = client.get("/api/library/file", params={"path": str(epub)})
            self.assertEqual(epub_file.status_code, 200)
            self.assertIn("book.epub", epub_file.headers["content-disposition"])

            self.assertEqual(
                client.get("/api/library/file", params={"path": str(outside)}).status_code,
                400,
            )


class DownloadsRouteTestCase(UserDataRouteBase):
    """GET/POST /api/downloads：章节归一化、series 映射、清空。"""

    def test_downloads_round_trip(self):
        with self._client() as client:
            empty = client.get("/api/downloads")
            self.assertEqual(empty.status_code, 200)
            body = empty.json()
            self.assertEqual(body["count"], 0)
            self.assertEqual(body["held"], 0)
            self.assertEqual(body["entries"], [])
            self.assertEqual(body["file"], str(self.downloads_file))

            posted = client.post(
                "/api/downloads",
                json={
                    "book_id": 100,
                    "series_id": 5,
                    "chapter_index": 1,
                    "title": "第一章",
                    "file": "a.epub",
                    "bytes": 123,
                    "format": "epub",
                },
            )
            self.assertEqual(posted.status_code, 200)
            entry = posted.json()["entry"]
            self.assertEqual(entry["book_id"], 100)
            self.assertEqual(entry["chapter_index"], 1)
            self.assertEqual(entry["format"], "epub")

            # 省略 chapter_index → 归一化成 0；非法 format → txt
            defaulted = client.post(
                "/api/downloads",
                json={"book_id": 101, "series_id": 5, "title": "单篇", "format": "PDF"},
            )
            self.assertEqual(defaulted.status_code, 200)
            self.assertEqual(defaulted.json()["entry"]["chapter_index"], 0)
            self.assertEqual(defaulted.json()["entry"]["format"], "txt")

            listed = client.get("/api/downloads").json()
            self.assertEqual(listed["count"], 2)
            self.assertEqual(listed["held"], 2)

            by_book = client.get("/api/downloads", params={"book_id": 101}).json()
            self.assertEqual(by_book["held"], 1)
            self.assertEqual(by_book["entries"][0]["book_id"], 101)

            by_series = client.get("/api/downloads", params={"series_id": 5}).json()
            self.assertEqual(set(by_series["series"]), {"0", "1"})
            self.assertEqual(by_series["series"]["1"]["file"], "a.epub")
            self.assertEqual(by_series["series"]["1"]["bytes"], 123)
            self.assertEqual(by_series["series"]["1"]["format"], "epub")
            self.assertTrue(by_series["series"]["1"]["at"])

            cleared = client.post("/api/downloads/clear")
            self.assertEqual(cleared.status_code, 200)
            self.assertTrue(cleared.json()["ok"])
            self.assertEqual(client.get("/api/downloads").json()["count"], 0)
            self.assertTrue(self.downloads_file.exists())
            self.assertEqual(self.downloads_file.read_text(encoding="utf-8"), "")


class BookmarksRouteTestCase(UserDataRouteBase):
    """GET/POST /api/bookmarks：本地台账 + 上游同步，上游失败也要本地记成功。"""

    def test_bookmarks_success_then_delete(self):
        with self._client(token="rt") as client:
            empty = client.get("/api/bookmarks").json()
            self.assertEqual(empty["count"], 0)
            self.assertEqual(empty["entries"], [])

            added = client.post(
                "/api/bookmarks",
                json={"id": 2001, "title": "收藏甲", "user": "作者", "restrict": "private"},
            )
            self.assertEqual(added.status_code, 200)
            body = added.json()
            self.assertTrue(body["ok"])
            self.assertTrue(body["synced"])              # mock 上游返回成功
            self.assertIsNone(body["sync_error"])
            self.assertEqual(body["entry"]["id"], 2001)
            self.assertEqual(body["entry"]["restrict"], "private")

            listed = client.get("/api/bookmarks").json()
            self.assertEqual(listed["count"], 1)
            self.assertEqual(listed["entries"][0]["id"], 2001)

            removed = client.post("/api/bookmarks/delete", json={"id": 2001})
            self.assertEqual(removed.status_code, 200)
            self.assertTrue(removed.json()["ok"])
            self.assertTrue(removed.json()["synced"])

            after = client.get("/api/bookmarks").json()
            self.assertEqual(after["count"], 0)
            self.assertEqual(after["entries"], [])

    def test_bookmark_is_recorded_locally_when_upstream_fails(self):
        for status in (401, 500):
            with self.subTest(status=status):
                with self._client(
                    handler=_failing_bookmark_handler(status), token="rt"
                ) as client:
                    resp = client.post(
                        "/api/bookmarks", json={"id": 3001, "title": "离线也记"}
                    )
                    self.assertEqual(resp.status_code, 200)
                    body = resp.json()
                    self.assertTrue(body["ok"])
                    self.assertFalse(body["synced"])
                    self.assertTrue(body["sync_error"])
                    self.assertIn("PixivApiError", body["sync_error"])
                    # 本地台账里仍然有这条记录
                    listed = client.get("/api/bookmarks").json()
                    self.assertEqual(listed["count"], 1)
                    self.assertEqual(listed["entries"][0]["id"], 3001)


class ExtensionSpecsTestCase(unittest.TestCase):
    """扩展配置项解析：`PIXIV_BRIDGE_EXTENSIONS` 的原始字符串怎么切。"""

    def test_single_spec(self):
        self.assertEqual(parse_extension_specs("novel"), ["novel"])

    def test_comma_semicolon_and_whitespace_all_separate(self):
        for raw in ("novel,reader", "novel;reader", "novel reader", "novel , ; reader"):
            with self.subTest(raw=raw):
                self.assertEqual(parse_extension_specs(raw), ["novel", "reader"])

    def test_empty_entries_are_dropped(self):
        self.assertEqual(parse_extension_specs(",,novel,,"), ["novel"])
        self.assertEqual(parse_extension_specs("   "), [])

    def test_empty_and_none_are_empty(self):
        self.assertEqual(parse_extension_specs(""), [])
        self.assertEqual(parse_extension_specs(None), [])

    def test_settings_default_is_empty(self):
        # 默认构造与 PixivSettings.load 在没配 PIXIV_BRIDGE_EXTENSIONS 时都得是空元组，
        # 否则「默认只跑纯接口」这条约定会被破坏。
        self.assertEqual(PixivSettings().extensions, ())
        self.assertIsInstance(PixivSettings().extensions, tuple)

    def test_settings_load_reads_the_env_var(self):
        with patch.dict(
            os.environ, {"PIXIV_BRIDGE_EXTENSIONS": "novel, extra"}, clear=False
        ):
            settings = PixivSettings.load(str(PROJECT_ROOT))
        self.assertEqual(settings.extensions, ("novel", "extra"))

    def test_blank_env_var_gives_empty_tuple(self):
        with patch.dict(os.environ, {"PIXIV_BRIDGE_EXTENSIONS": ""}, clear=False):
            settings = PixivSettings.load(str(PROJECT_ROOT))
        self.assertEqual(settings.extensions, ())


class ExtensionLoadingTestCase(unittest.TestCase):
    """扩展加载：固定入口文件名 extension.py，缺文件/缺路由要给可读报错。"""

    def setUp(self):
        self.dirs = []

    def tearDown(self):
        for path in reversed(self.dirs):
            drop_temp_dir(path)

    def make_package(self, body, name="ext"):
        """在 _tmp_tests/ 下建一个带 extension.py 的临时包，返回目录。"""
        directory = make_temp_dir(name)
        self.dirs.append(directory)
        (directory / "extension.py").write_text(body, encoding="utf-8")
        return directory

    def test_loads_the_repo_novel_extension(self):
        extensions = load_extension_specs(["novel"], PROJECT_ROOT)
        self.assertEqual(len(extensions), 1)
        ext = extensions[0]
        self.assertEqual(ext.name, "novel")
        self.assertEqual(ext.entry, PROJECT_ROOT / "novel" / EXTENSION_FILENAME)
        self.assertTrue(ext.routers)
        self.assertIsNotNone(ext.state_factory)
        # novel 没有自己的资源要释放，所以没有 shutdown。
        self.assertIsNone(ext.shutdown)

    def test_relative_spec_resolves_against_project_root(self):
        extensions = load_extension_specs(["novel"], PROJECT_ROOT)
        self.assertEqual(extensions[0].directory, PROJECT_ROOT / "novel")

    def test_absolute_spec_is_used_as_is(self):
        directory = self.make_package(
            "from fastapi import APIRouter\n"
            "router = APIRouter()\n"
            "EXTENSION_ROUTERS = (router,)\n"
        )
        extensions = load_extension_specs([str(directory)], PROJECT_ROOT)
        self.assertEqual(extensions[0].directory, directory)

    def test_missing_extension_file_is_a_readable_error(self):
        with self.assertRaises(ExtensionError) as ctx:
            load_extension_specs(["no-such-extension-dir"], PROJECT_ROOT)
        message = str(ctx.exception)
        self.assertIn(EXTENSION_FILENAME, message)
        self.assertIn("找不到入口文件", message)

    def test_missing_routers_attribute_is_a_readable_error(self):
        directory = self.make_package("X = 1\n")
        relative = directory.relative_to(PROJECT_ROOT)
        with self.assertRaises(ExtensionError) as ctx:
            load_extension_specs([str(relative)], PROJECT_ROOT)
        self.assertIn(EXTENSION_ROUTERS_ATTR, str(ctx.exception))

    def test_none_routers_is_rejected(self):
        directory = self.make_package(f"{EXTENSION_ROUTERS_ATTR} = None\n")
        relative = directory.relative_to(PROJECT_ROOT)
        with self.assertRaises(ExtensionError):
            load_extension_specs([str(relative)], PROJECT_ROOT)

    def test_a_single_router_is_wrapped_into_a_tuple(self):
        directory = self.make_package(
            "from fastapi import APIRouter\n"
            "router = APIRouter()\n"
            "EXTENSION_ROUTERS = router\n"
        )
        relative = directory.relative_to(PROJECT_ROOT)
        extensions = load_extension_specs([str(relative)], PROJECT_ROOT)
        self.assertEqual(len(extensions[0].routers), 1)

    def test_extension_import_error_is_wrapped_and_no_half_module_left(self):
        directory = self.make_package("raise RuntimeError('扩展内部炸了')\n")
        relative = directory.relative_to(PROJECT_ROOT)
        before = set(sys.modules)
        with self.assertRaises(ExtensionError) as ctx:
            load_extension_specs([str(relative)], PROJECT_ROOT)
        message = str(ctx.exception)
        self.assertIn("RuntimeError", message)
        self.assertIn("扩展内部炸了", message)
        # 加载失败不能把半个模块留在 sys.modules 里（同一个进程里前面
        # 已经加载成功的扩展会留在里面，所以只比对这次新增的部分）。
        self.assertEqual(set(sys.modules) - before, set())

    def test_empty_and_none_specs_load_nothing(self):
        self.assertEqual(load_extension_specs([], PROJECT_ROOT), [])
        self.assertEqual(load_extension_specs(None, PROJECT_ROOT), [])

    def test_duplicate_specs_load_once(self):
        extensions = load_extension_specs(["novel", "novel"], PROJECT_ROOT)
        self.assertEqual(len(extensions), 1)

    def test_load_extensions_reads_settings(self):
        settings = PixivSettings(project_root=PROJECT_ROOT, extensions=("novel",))
        extensions = load_extensions(settings)
        self.assertEqual([ext.name for ext in extensions], ["novel"])

    def test_load_extensions_without_settings_field_is_empty(self):
        class Bare:
            project_root = PROJECT_ROOT

        self.assertEqual(load_extensions(Bare()), [])


class ExtensionMountTestCase(unittest.TestCase):
    """扩展挂载进应用：路由、app.state、shutdown、纯接口不受影响。"""

    def setUp(self):
        self.dirs = []

    def tearDown(self):
        for path in reversed(self.dirs):
            drop_temp_dir(path)

    def make_extension_dir(self, body, name="ext_mount"):
        directory = make_temp_dir(name)
        self.dirs.append(directory)
        (directory / "extension.py").write_text(body, encoding="utf-8")
        return directory

    @staticmethod
    def route_paths(app):
        out = set()
        for route in app.routes:
            holder = getattr(route, "original_router", None)
            items = holder.routes if holder is not None else [route]
            for item in items:
                path = getattr(item, "path", None)
                if path is not None:
                    out.add(path)
        return out

    def test_novel_extension_adds_exactly_the_reader_routes(self):
        settings = make_settings()
        pure = self.route_paths(create_app(settings))
        full = self.route_paths(
            create_app(settings, extensions=load_extension_specs(["novel"], PROJECT_ROOT))
        )
        self.assertNotIn("/api/history", pure)
        extra = full - pure
        self.assertIn("/api/history", extra)
        # 阅读器页面在 /novel；`/` 两版都有（导航页），所以不在差集里。
        self.assertIn("/novel", extra)
        self.assertNotIn("/", extra)
        self.assertEqual(len(extra), 10)

    def test_iwara_web_extension_adds_exactly_the_video_page(self):
        # iwara 的视频页面拆成了独立扩展包：它只挂一个路由，接口仍归 iwara 包。
        settings = make_settings()
        pure = self.route_paths(create_app(settings))
        full = self.route_paths(
            create_app(settings, extensions=load_extension_specs(["iwara_web"], PROJECT_ROOT))
        )
        self.assertNotIn("/iwara", pure)
        self.assertEqual(full - pure, {"/iwara"})

    def test_pure_api_keeps_working_with_the_extension_mounted(self):
        settings = make_settings()
        app = create_app(
            settings, extensions=load_extension_specs(["novel"], PROJECT_ROOT)
        )
        with TestClient(app) as client:
            self.assertEqual(client.get("/api/health").status_code, 200)
            self.assertEqual(client.get("/api/history").status_code, 200)

    def test_both_extension_packages_can_be_mounted_together(self):
        settings = make_settings()
        extensions = load_extension_specs(["iwara_web", "novel"], PROJECT_ROOT)
        app = create_app(settings, extensions=extensions)
        with TestClient(app) as client:
            self.assertEqual(client.get("/api/health").status_code, 200)
            self.assertEqual(client.get("/novel").status_code, 200)
            page = client.get("/iwara")
            self.assertEqual(page.status_code, 200)
            # 页面靠绝对路径调 iwara 接口，所以挂哪个路径都行。
            self.assertIn('"/api/iwara"', page.text)

    def test_extension_state_factory_runs_inside_lifespan(self):
        directory = self.make_extension_dir(
            "from fastapi import APIRouter\n"
            "router = APIRouter()\n"
            "@router.get('/api/ext-probe')\n"
            "async def probe():\n"
            "    return {'ok': True}\n"
            "EXTENSION_ROUTERS = (router,)\n"
            "def extension_state_factory(settings):\n"
            "    return {'ext_probe': 'FROM-EXTENSION'}\n"
        )
        relative = directory.relative_to(PROJECT_ROOT)
        extensions = load_extension_specs([str(relative)], PROJECT_ROOT)
        app = create_app(make_settings(), extensions=extensions)
        with TestClient(app) as client:
            self.assertEqual(app.state.ext_probe, "FROM-EXTENSION")
            self.assertEqual(client.get("/api/ext-probe").status_code, 200)

    def test_app_state_exposes_loaded_extensions(self):
        extensions = load_extension_specs(["novel"], PROJECT_ROOT)
        app = create_app(make_settings(), extensions=extensions)
        with TestClient(app):
            self.assertEqual(app.state.extensions, tuple(extensions))

    def test_extension_shutdown_is_awaited(self):
        directory = self.make_extension_dir(
            "from fastapi import APIRouter\n"
            "router = APIRouter()\n"
            "EXTENSION_ROUTERS = (router,)\n"
            "async def extension_shutdown(app):\n"
            "    app.state.shutdown_ran = True\n"
        )
        relative = directory.relative_to(PROJECT_ROOT)
        extensions = load_extension_specs([str(relative)], PROJECT_ROOT)
        app = create_app(make_settings(), extensions=extensions)
        with TestClient(app):
            pass
        self.assertTrue(getattr(app.state, "shutdown_ran", False))

    def test_explicit_state_factory_and_extension_are_both_mounted(self):
        directory = self.make_extension_dir(
            "from fastapi import APIRouter\n"
            "router = APIRouter()\n"
            "EXTENSION_ROUTERS = (router,)\n"
            "def extension_state_factory(settings):\n"
            "    return {'from_extension': 1}\n"
        )
        relative = directory.relative_to(PROJECT_ROOT)
        extensions = load_extension_specs([str(relative)], PROJECT_ROOT)
        app = create_app(
            make_settings(),
            state_factory=lambda settings: {"from_explicit": 2},
            extensions=extensions,
        )
        with TestClient(app):
            self.assertEqual(app.state.from_extension, 1)
            self.assertEqual(app.state.from_explicit, 2)

    def test_create_app_does_not_read_configuration_by_itself(self):
        # create_app 是纯组装：不给 extensions 就不挂任何扩展，
        # 即使环境变量里写着 novel —— 否则边界检查与应用行为会随 .env 漂移。
        settings = make_settings()
        settings.extensions = ("novel",)
        pure = self.route_paths(create_app(settings))
        self.assertNotIn("/api/history", pure)


class ExtensionLayoutTestCase(unittest.TestCase):
    """出入口整理：接口项目各留一个入口，阅读项目改由扩展机制启用。"""

    def test_two_api_entry_points_are_peers(self):
        # 各接口项目同级：各自 <站点>/run.py，根目录不再有 run.py。
        # 站点/扩展清单跟着导航发现走（不再写死），加站点时这里自动覆盖。
        from pixiv_bridge import __main__ as navigation

        sites = navigation.site_items()
        self.assertGreaterEqual(len(sites), 2)
        for item in sites:
            with self.subTest(name=item.name):
                self.assertTrue((PROJECT_ROOT / item.name / "run.py").is_file())
        self.assertFalse((PROJECT_ROOT / "run.py").exists())
        # 框架包里不许混进任何站点/扩展知识。
        local = [item.name for item in sites] + [
            item.name for item in navigation.extension_items()
        ]
        for name in local:
            with self.subTest(framework=name):
                self.assertFalse((PROJECT_ROOT / "pixiv_bridge" / name).exists(), name)

    def test_extension_packages_have_no_dedicated_entry_point(self):
        from pixiv_bridge import __main__ as navigation

        for item in navigation.extension_items():
            with self.subTest(name=item.name):
                self.assertFalse((PROJECT_ROOT / item.name / "run.py").exists())

    def test_novel_exposes_the_fixed_extension_entry(self):
        entry = PROJECT_ROOT / "novel" / EXTENSION_FILENAME
        self.assertTrue(entry.is_file(), str(entry))
        source = entry.read_text(encoding="utf-8")
        self.assertIn("EXTENSION_ROUTERS", source)
        self.assertIn("extension_state_factory", source)

    def test_iwara_web_exposes_the_fixed_extension_entry(self):
        entry = PROJECT_ROOT / "iwara_web" / EXTENSION_FILENAME
        self.assertTrue(entry.is_file(), str(entry))
        source = entry.read_text(encoding="utf-8")
        self.assertIn("EXTENSION_ROUTERS", source)
        self.assertIn("EXTENSION_NAV", source)
        # 只挂路由、没有自己的状态，所以不该真的定义状态工厂/关闭钩子
        # （文档字符串里提到这两个名字是说明约定，不算）。
        extension = load_extension_specs(["iwara_web"], PROJECT_ROOT)[0]
        self.assertIsNone(extension.state_factory)
        self.assertIsNone(extension.shutdown)
        self.assertEqual(len(extension.routers), 1)

    def test_novel_app_still_offers_the_explicit_assembly_path(self):
        # 测试与脚本依赖 novel.app.create_app 这条老路，不能被整理掉。
        self.assertTrue(callable(create_novel_app))
        settings = make_settings()
        app = create_novel_app(settings)
        self.assertIn("/api/history", ExtensionMountTestCase.route_paths(app))

    def test_gitignore_does_not_drop_the_extension_entry(self):
        source = (PROJECT_ROOT / ".gitignore").read_text(encoding="utf-8")
        for line in source.splitlines():
            stripped = line.strip()
            if stripped and not stripped.startswith("#"):
                self.assertNotIn(
                    "extension.py",
                    stripped,
                    "扩展入口文件不能被 .gitignore 忽略",
                )


class NavigationPageTestCase(unittest.TestCase):
    """导航页：接口项目的 `/`，把自带入口与各扩展声明的页面列出来。"""

    def _html(self, settings=None, extensions=()):
        app = create_app(
            settings or make_settings(),
            transport=httpx.MockTransport(_mock_handler),
            extensions=extensions,
        )
        with TestClient(app) as client:
            resp = client.get("/")
        return resp

    def _load_temp_extension(self, body, name="ext_nav"):
        """在 _tmp_tests/ 下造一个临时扩展并加载它（用完自动清掉）。"""
        directory = make_temp_dir(name)
        self.addCleanup(drop_temp_dir, directory)
        (directory / "extension.py").write_text(body, encoding="utf-8")
        return load_extension_specs(
            [str(directory.relative_to(PROJECT_ROOT))], PROJECT_ROOT
        )

    # ---- 纯函数层 ----

    def test_normalize_nav_items_accepts_dicts_and_strings(self):
        items = nav_module.normalize_nav_items(
            [
                {"title": "阅读器", "path": "/novel", "description": "读小说"},
                {"path": "/docs"},
                "/api/health",
            ]
        )
        self.assertEqual([item.path for item in items], ["/novel", "/docs", "/api/health"])
        self.assertEqual(items[0].title, "阅读器")
        self.assertEqual(items[0].description, "读小说")
        # 缺 title 时用路径兜底
        self.assertEqual(items[1].title, "/docs")

    def test_normalize_nav_items_drops_junk(self):
        # 缺 path、非 dict、None 都丢掉；None 整体也能接受
        self.assertEqual(nav_module.normalize_nav_items(None), ())
        self.assertEqual(nav_module.normalize_nav_items([None, 1, {}, {"title": "x"}]), ())
        self.assertEqual(
            [i.path for i in nav_module.normalize_nav_items([{"path": "  /a  "}])],
            ["/a"],
        )

    def test_normalize_nav_items_passes_navitem_through(self):
        item = nav_module.NavItem("t", "/p", "d")
        self.assertEqual(nav_module.normalize_nav_items([item]), (item,))

    def test_parse_nav_links_forms(self):
        links = nav_module.parse_nav_links(
            "iwara 视频=http://127.0.0.1:8766/; /docs"
        )
        self.assertEqual(
            [(item.title, item.path) for item in links],
            [("iwara 视频", "http://127.0.0.1:8766/"), ("/docs", "/docs")],
        )
        self.assertEqual(nav_module.parse_nav_links(""), ())
        self.assertEqual(nav_module.parse_nav_links(None), ())

    def test_parse_nav_links_skips_empty_entries(self):
        # 只有地址的空项、只有 `=` 的空项都丢掉，正常项保留
        links = nav_module.parse_nav_links("a=/a, ,b=/b,=,")
        self.assertEqual([(i.title, i.path) for i in links], [("a", "/a"), ("b", "/b")])
        self.assertEqual(nav_module.parse_nav_links("=,"), ())

    def test_unsafe_href_is_not_linked(self):
        html = nav_module.render_html(
            title="T",
            sections=(nav_module.NavSection("S", (nav_module.NavItem("bad", "javascript:alert(1)"),)),),
        )
        self.assertNotIn("<a", html)
        self.assertIn("javascript:alert(1)", html)  # 仍以文本显示，但不生成链接

    def test_render_html_escapes_text(self):
        html = nav_module.render_html(
            title="<script>",
            sections=(
                nav_module.NavSection(
                    "S", (nav_module.NavItem("<b>t</b>", '/x"y'),)
                ),
            ),
        )
        self.assertNotIn("<script>", html)
        self.assertIn("&lt;script&gt;", html)
        self.assertNotIn('href="/x"y"', html)

    # ---- 配置层 ----

    def test_nav_links_settings_default_empty(self):
        self.assertEqual(make_settings().nav_links, "")

    def test_settings_loads_nav_links_from_env(self):
        with patch.dict(
            os.environ, {"PIXIV_BRIDGE_NAV_LINKS": "iwara=http://127.0.0.1:8766/"}, clear=False
        ):
            settings = PixivSettings.load(str(PROJECT_ROOT))
        self.assertEqual(settings.nav_links, "iwara=http://127.0.0.1:8766/")

    # ---- HTTP 层 ----

    def test_root_is_the_navigation_page(self):
        resp = self._html()
        self.assertEqual(resp.status_code, 200)
        self.assertIn("text/html", resp.headers.get("content-type", ""))
        for path in ("/docs", "/openapi.json", "/api/health"):
            self.assertIn(path, resp.text)
        self.assertIn("没有挂载扩展", resp.text)

    def test_navigation_page_lists_extension_with_nav(self):
        extensions = load_extension_specs(["novel"], PROJECT_ROOT)
        resp = self._html(extensions=extensions)
        self.assertEqual(resp.status_code, 200)
        self.assertIn("扩展：novel", resp.text)
        self.assertIn("小说阅读器", resp.text)
        self.assertIn('href="/novel"', resp.text)
        self.assertIn("已挂载 1 个扩展：novel", resp.text)

    def test_navigation_page_lists_extension_without_nav(self):
        raw = (
            "from fastapi import APIRouter\n"
            "router = APIRouter()\n"
            "EXTENSION_ROUTERS = (router,)\n"
        )
        extensions = self._load_temp_extension(raw)
        self.assertEqual(extensions[0].nav, ())
        resp = self._html(extensions=extensions)
        # 没声明也照样列出名字（名字就是配置里写的 spec），并说明它没声明
        heading = f"<h2>扩展：{extensions[0].name}</h2>"
        self.assertIn(heading, resp.text)
        self.assertIn("没有声明 EXTENSION_NAV", resp.text)
        # 这一段里只有说明文字，没有链接（自带入口那一段在上面，不受影响）
        self.assertNotIn("<a", resp.text.split(heading)[1])

    def test_navigation_page_renders_config_links(self):
        settings = make_settings()
        settings.nav_links = "iwara 视频=http://127.0.0.1:8766/"
        resp = self._html(settings=settings)
        self.assertIn("其它入口", resp.text)
        self.assertIn('href="http://127.0.0.1:8766/"', resp.text)

    def test_navigation_page_keeps_root_when_extension_declares_it(self):
        # 导航页挂在扩展路由之前：扩展就算也声明 `/`，导航页仍然是它。
        raw = (
            "from fastapi import APIRouter\n"
            "router = APIRouter()\n"
            "@router.get('/')\n"
            "async def hijack():\n"
            "    return {'hijacked': True}\n"
            "EXTENSION_ROUTERS = (router,)\n"
        )
        directory = make_temp_dir("ext_hijack")
        self.addCleanup(drop_temp_dir, directory)
        (directory / "extension.py").write_text(raw, encoding="utf-8")
        extensions = load_extension_specs(
            [str(directory.relative_to(PROJECT_ROOT))], PROJECT_ROOT
        )
        resp = self._html(extensions=extensions)
        self.assertIn("text/html", resp.headers.get("content-type", ""))
        self.assertNotIn("hijacked", resp.text)

    def test_novel_extension_declares_its_navigation_page(self):
        extensions = load_extension_specs(["novel"], PROJECT_ROOT)
        nav = extensions[0].nav
        self.assertEqual(len(nav), 1)
        self.assertEqual(nav[0].path, "/novel")
        self.assertTrue(nav[0].title)

    def test_reader_page_lives_at_novel_not_root(self):
        extensions = load_extension_specs(["novel"], PROJECT_ROOT)
        app = create_app(
            make_settings(),
            transport=httpx.MockTransport(_mock_handler),
            extensions=extensions,
        )
        with TestClient(app) as client:
            reader = client.get("/novel")
            self.assertEqual(reader.status_code, 200)
            self.assertIn("text/html", reader.headers.get("content-type", ""))
            self.assertIn('id="view-reader"', reader.text)
            # `/` 仍是导航页（HTML 里是导航页的标志，不是阅读器页面）
            root = client.get("/")
            self.assertEqual(root.status_code, 200)
            self.assertIn("接口项目自带", root.text)


class CliCheckTestCase(unittest.IsolatedAsyncioTestCase):
    """``--check`` 这条路的三处行为：端口只提示、缺 token 有提示、可加专属检查。"""

    def _fatal_results(self):
        return [CheckResult("python", True, "3.13"), CheckResult("port", False, "8765 已被占用")]

    def test_fail_on_stops_on_a_fatal_check(self):
        from pixiv_bridge.cli import _fail_on

        with self.assertRaises(SystemExit) as ctx:
            _fail_on(self._fatal_results())
        self.assertEqual(ctx.exception.code, 1)

    def test_fail_on_skips_the_port_check_when_asked(self):
        from pixiv_bridge.cli import _fail_on

        # --check 只校验、不监听端口，端口被占不该拦下自检。
        _fail_on(self._fatal_results(), skip=("port",))

    async def test_check_hints_get_token_when_no_refresh_token(self):
        from pixiv.envcheck import MISSING_TOKEN_HINT, _check

        directory = make_temp_dir("cli_notoken")
        self.addCleanup(drop_temp_dir, directory)
        settings = PixivSettings(refresh_token="", token_file=directory / "absent.json")

        buffer = io.StringIO()
        with contextlib.redirect_stdout(buffer):
            code = await _check(settings)

        self.assertEqual(code, 1)
        text = buffer.getvalue()
        self.assertIn("未配置 refresh_token", text)
        self.assertIn("python pixiv/get_token.py", text)
        self.assertIn(MISSING_TOKEN_HINT.strip(), text)

    def _demo_site(self, checks):
        """一个最小站点：只挂一个专属自检，应用工厂是空壳（uvicorn 被 mock）。"""
        from pixiv_bridge.site import Site

        return Site(
            name="demo",
            directory=PROJECT_ROOT / "pixiv",
            title="演示站点",
            create_app=lambda settings, extensions=(): None,
            settings_class=PixivSettings,
            checks=checks,
        )

    def test_main_calls_site_checks_in_the_same_table(self):
        from pixiv_bridge import cli

        seen = {}

        def extra(settings):
            seen["settings"] = settings
            return [CheckResult("站点网络", False, "不可达（测试）")]

        buffer = io.StringIO()
        with patch.object(cli, "ensure_dependencies"), patch.object(
            cli, "run_checks", return_value=[CheckResult("python", True, "3.13")]
        ), patch.object(cli, "load_extensions", return_value=[]), patch.object(
            cli.uvicorn, "run"
        ) as uvicorn_run, contextlib.redirect_stdout(buffer):
            cli.main([], site=self._demo_site(extra))

        text = buffer.getvalue()
        # 站点自己的检查与通用检查打在同一张 [环境检查] 表里，顺序排在它们后面。
        self.assertIn("[环境检查]", text)
        self.assertLess(text.index("python"), text.index("站点网络"))
        self.assertIn("不可达（测试）", text)
        # 站点用自己的 Settings 子类读配置，入口不认识任何站点字段。
        self.assertIsInstance(seen.get("settings"), PixivSettings)
        uvicorn_run.assert_called_once()

    def test_main_without_a_site_tells_the_user_where_to_start(self):
        from pixiv_bridge import cli

        buffer = io.StringIO()
        with contextlib.redirect_stderr(buffer), self.assertRaises(SystemExit) as ctx:
            cli.main([])

        # 框架本身不是服务：没有站点入口时退出码 2 并指出该用哪个入口。
        self.assertEqual(ctx.exception.code, 2)
        self.assertIn("pixiv/run.py", buffer.getvalue())
        self.assertIn("iwara/run.py", buffer.getvalue())

    def test_main_reports_site_checks_failure_without_stopping(self):
        from pixiv_bridge import cli

        def extra(settings):
            return [CheckResult("站点依赖", False, "不可用")]

        buffer = io.StringIO()
        with patch.object(cli, "ensure_dependencies"), patch.object(
            cli, "run_checks", return_value=[CheckResult("python", True, "3.13")]
        ), patch.object(cli, "load_extensions", return_value=[]), patch.object(
            cli.uvicorn, "run"
        ) as uvicorn_run, contextlib.redirect_stdout(buffer):
            cli.main([], site=self._demo_site(extra))

        # 站点专属检查不在 FATAL_CHECKS 里：WARN 之后照样进 uvicorn.run。
        self.assertIn("WARN", buffer.getvalue())
        uvicorn_run.assert_called_once()


if __name__ == "__main__":
    unittest.main()
