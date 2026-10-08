"""iwara 子项目的单元测试。

这些用例全部离线：纯函数直接断言，路由用一个假客户端顶替
``app.state.iwara_client``，不打任何真实请求。

iwara 依赖 ``curl_cffi``（Cloudflare 只认浏览器 TLS 指纹），而它只写在
``iwara/requirements.txt`` 里 —— 新克隆的仓库还没跑过 ``iwara/run.py`` 时
会缺这个包。所以整个模块在缺包时**整片跳过**，而不是让测试红掉：
装齐依赖（``pip install -r iwara/requirements.txt``）后才会真正执行。
"""

import asyncio
import base64
import itertools
import json
import os
import shutil
import sys
import time
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
from fastapi.testclient import TestClient

from pixiv_bridge.config import Settings
from pixiv_bridge.server import create_app as _create_bridge_app

# 与 test_bridge.py 同一约定：临时的东西放工作区 _tmp_tests/，别用系统 TEMP。
TEST_TMP_ROOT = Path(__file__).resolve().parent.parent / "_tmp_tests"
_TEMP_COUNTER = itertools.count(1)

try:
    from curl_cffi.const import CurlOpt

    from iwara import tokens
    from iwara.app import create_app as create_iwara_app
    from iwara.client import (
        SEED_HOSTS,
        SEED_IPS,
        THUMBNAIL_HOST,
        IwaraAuthError,
        IwaraClient,
        IwaraError,
        Resolved,
        Source,
        decorate_video,
        is_cdn_host,
        jwt_payload,
        thumbnail_url,
        x_version,
    )

    HAVE_IWARA = True
    IWARA_IMPORT_ERROR = ""
except ImportError as exc:  # pragma: no cover - 只在没装 curl_cffi 时走到
    HAVE_IWARA = False
    IWARA_IMPORT_ERROR = str(exc)


def _mock_handler(request: httpx.Request) -> httpx.Response:
    """pixiv 侧的最小桩：这些用例不关心 pixiv 接口。"""
    return httpx.Response(200, json={"ok": True})


def make_settings():
    return Settings(refresh_token="", rate_limit_rpm=10000)


def make_temp_dir(tag="tmp"):
    """与 test_bridge.py 同一份实现：临时的东西放工作区 _tmp_tests/。"""
    path = TEST_TMP_ROOT / ("%s_%d_%d" % (tag, os.getpid(), next(_TEMP_COUNTER)))
    path.mkdir(parents=True, exist_ok=True)
    return path


def drop_temp_dir(path):
    shutil.rmtree(path, ignore_errors=True)
    try:
        TEST_TMP_ROOT.rmdir()
    except OSError:
        pass


# 真实结构（字段名与线上一致）：thumbnail 是整数帧号，真地址要用 file.id 拼。
VIDEO = {
    "id": "b0pRCNfo8Igu3p",
    "title": "测试视频",
    "rating": "ecchi",
    "thumbnail": 2,
    "customThumbnail": None,
    "embedUrl": None,
    "file": {
        "id": "11111111-2222-3333-4444-555555555555",
        "name": "11111111-2222-3333-4444-555555555555.mp4",
        "size": 53874359,
        "mime": "video/mp4",
    },
    "fileUrl": (
        "https://filesq.iwara.tv/file/11111111-2222-3333-4444-555555555555"
        "?expires=1791392561333&hash=af09207d"
    ),
}

RAW_SOURCES = [
    {
        "name": "Source",
        "type": "video/mp4",
        "src": {
            "view": "//firefly.iwara.tv/view?hash=a&filename=x_Source.mp4",
            "download": "//firefly.iwara.tv/view?hash=a&filename=x_Source.mp4&expires=1",
        },
    },
    {
        "name": "540",
        "type": "video/mp4",
        "src": {
            "view": "//bailu.iwara.tv/view?hash=b&filename=x_540.mp4",
            "download": "//bailu.iwara.tv/view?hash=b&filename=x_540.mp4&expires=1",
        },
    },
    {
        "name": "preview",
        "type": "video/mp4",
        "src": {
            "view": "//zani.iwara.tv/view?hash=c&filename=x_preview.mp4",
            "download": "//zani.iwara.tv/view?hash=c&filename=x_preview.mp4&expires=1",
        },
    },
]

VIDEO_URL = "https://firefly.iwara.tv/view?hash=a&filename=x_Source.mp4&expires=1"
MP4_HEAD = b"\x00\x00\x00 ftypisom" + b"\x00" * 4088


class _FakeIwaraClient:
    """顶替真实客户端：记录调用，返回固定数据，从不联网。"""

    def __init__(self):
        self.calls = []
        self.closed = False
        self.probe_error = None
        self.related_error = None
        # 登录态相关（/api/iwara/auth 会读这几个字段）。
        self.refresh_token = None
        self.access_token = None
        self.auth_error = ""
        self.refresh_error = None

    async def list_videos(self, **kwargs):
        self.calls.append(("list_videos", kwargs))
        return {"count": 1, "limit": kwargs.get("limit", 20), "page": 0, "results": [VIDEO]}

    async def search(self, query, **kwargs):
        self.calls.append(("search", query, kwargs))
        return {"count": 1, "limit": 20, "page": 0, "results": [VIDEO]}

    async def video(self, video_id):
        self.calls.append(("video", video_id))
        return dict(VIDEO, id=video_id)

    async def related(self, video_id):
        self.calls.append(("related", video_id))
        if self.related_error:
            raise IwaraError(self.related_error, status=502)
        return {
            "count": 2,
            "limit": 12,
            "page": 0,
            "results": [dict(VIDEO, id="related-1"), dict(VIDEO, id="related-2")],
        }

    async def resolve(self, video_id):
        self.calls.append(("resolve", video_id))
        return Resolved(
            video_id=video_id,
            title=VIDEO["title"],
            rating=VIDEO["rating"],
            file_url=VIDEO["fileUrl"],
            file_name=VIDEO["file"]["name"],
            file_size=VIDEO["file"]["size"],
            sources=[Source.from_api(item) for item in RAW_SOURCES],
            salt_used="test-salt",
            raw=VIDEO,
        )

    async def probe_any(self, resolved, *, nbytes=1024):
        self.calls.append(("probe_any", resolved.video_id, nbytes))
        if self.probe_error:
            raise IwaraError(self.probe_error, status=502)
        return resolved.sources[0], {
            "status": 206,
            "content_type": "video/mp4",
            "content_range": "bytes 0-1023/53874359",
            "bytes": 1024,
            "is_mp4": True,
        }

    async def open_stream(self, url, headers=None):
        self.calls.append(("open_stream", url, dict(headers or {})))

        async def body():
            yield MP4_HEAD

        return (
            206,
            {
                "content-type": "video/mp4",
                "content-range": "bytes 0-4095/53874359",
                "content-length": "4096",
            },
            body(),
        )

    async def fetch_image(self, url):
        self.calls.append(("fetch_image", url))
        return b"\xff\xd8\xff\xdbJPEGDATA", "image/jpeg"

    async def me(self):
        self.calls.append(("me",))
        # 匿名（没有 access token）时 /user 就是 401，和真实 API 一致；
        # 认证成功后返回账号信息，好让 /auth 的 username_hint 有来源。
        if not self.access_token:
            raise IwaraError("匿名访问", status=401, reason="auth_required")
        return {"user": {"username": "huanghe19512", "name": "huanghe"}}

    async def ensure_access_token(self):
        self.calls.append(("ensure_access_token",))
        if self.refresh_error:
            raise IwaraError(self.refresh_error, status=401, reason="auth_required")
        self.access_token = self.access_token or "fake-access-token"
        return self.access_token

    async def aclose(self):
        self.closed = True


@unittest.skipUnless(HAVE_IWARA, "需要 curl_cffi：pip install -r iwara/requirements.txt")
class IwaraClientLogicTestCase(unittest.TestCase):
    """不联网的纯逻辑：签名、缩略图拼接、源排序。"""

    def test_x_version_matches_known_vector(self):
        # 一次真实请求里抓到的 fileUrl，钉住算法：X-Version = sha1("{uuid}_{expires}_{salt}")
        real_url = (
            "https://filesq.iwara.tv/file/672c0d7a-8bb7-422e-a9c3-9c523469e185"
            "?expires=1791392561333&hash=af09207d"
        )
        self.assertEqual(
            x_version(real_url, "mSvL05GfEmeEmsEYfGCnVpEjYgTJraJN"),
            "5d77e760c0838bb1319ba845bf2fbe1df700af16",
        )

    def test_x_version_uses_default_salt(self):
        self.assertEqual(
            x_version(VIDEO["fileUrl"]),
            x_version(VIDEO["fileUrl"], "mSvL05GfEmeEmsEYfGCnVpEjYgTJraJN"),
        )

    def test_x_version_rejects_unparseable_url(self):
        with self.assertRaises(IwaraError):
            x_version("https://filesq.iwara.tv/file/nothing-here")

    def test_thumbnail_url_is_built_from_file_id(self):
        # thumbnail 字段本身是整数（帧号），绝不是路径 —— 别用错。
        self.assertEqual(
            thumbnail_url(VIDEO),
            f"https://{THUMBNAIL_HOST}/image/thumbnail/"
            "11111111-2222-3333-4444-555555555555/thumbnail-00.jpg",
        )

    def test_thumbnail_url_variants(self):
        big = thumbnail_url(VIDEO, index=1, original=True)
        self.assertIn("/image/original/", big)
        self.assertTrue(big.endswith("thumbnail-01.jpg"))

    def test_thumbnail_url_without_file(self):
        self.assertEqual(thumbnail_url({"id": "x"}), "")
        self.assertEqual(thumbnail_url({"file": {}}), "")

    def test_decorate_video_adds_both_urls(self):
        decorated = decorate_video(VIDEO)
        self.assertIn("/image/thumbnail/", decorated["thumbnailUrl"])
        self.assertIn("/image/original/", decorated["posterUrl"])
        # 原对象不被改动
        self.assertNotIn("thumbnailUrl", VIDEO)

    def test_decorate_video_passes_through_non_video(self):
        # 收藏列表里可能混着非视频条目，不能炸。
        self.assertEqual(decorate_video({"id": "x"}), {"id": "x"})

    def test_is_cdn_host(self):
        for host in (
            "iwara.tv",
            "files.iwara.tv",
            "filesq.iwara.tv",
            "firefly.iwara.tv",
            "bailu.iwara.tv",
        ):
            self.assertTrue(is_cdn_host(host), host)
        for host in ("", "notiwara.com", "evil-iwara.tv", "iwara.tv.evil.com"):
            self.assertFalse(is_cdn_host(host), host)

    def test_source_from_api_makes_protocol_relative_absolute(self):
        source = Source.from_api(RAW_SOURCES[0])
        self.assertEqual(source.name, "Source")
        self.assertEqual(source.mime, "video/mp4")
        self.assertTrue(source.download.startswith("https://firefly.iwara.tv/"))
        self.assertEqual(source.to_dict()["host"], "firefly.iwara.tv")

    def test_resolved_ordering_follows_preference(self):
        resolved = Resolved(
            video_id="v",
            title="t",
            rating="ecchi",
            file_url="",
            file_name="",
            file_size=0,
            sources=[Source.from_api(item) for item in RAW_SOURCES],
        )
        self.assertEqual([s.name for s in resolved.ordered()], ["Source", "540", "preview"])
        self.assertEqual(resolved.best().name, "Source")

    def test_resolved_ordering_puts_unknown_names_last(self):
        sources = [Source("weird", "video/mp4", "", "https://x.iwara.tv/a")]
        sources += [Source.from_api(item) for item in RAW_SOURCES]
        resolved = Resolved("v", "t", "all", "", "", 0, sources)
        self.assertEqual([s.name for s in resolved.ordered()][-1], "weird")

    def test_resolved_best_without_sources_raises(self):
        with self.assertRaises(IwaraError):
            Resolved("v", "t", "all", "", "", 0, []).best()

    def test_jwt_payload_decodes_without_verifying(self):
        # {"exp": 4102444800, "type": "access"}，不验签只看内容。
        token = (
            "eyJhbGciOiJIUzI1NiJ9."
            "eyJleHAiOjQxMDI0NDQ4MDAsInR5cGUiOiJhY2Nlc3MifQ."
            "signature-not-checked"
        )
        payload = jwt_payload(token)
        self.assertEqual(payload["type"], "access")
        self.assertEqual(payload["exp"], 4102444800)

    def test_jwt_payload_tolerates_garbage(self):
        self.assertEqual(jwt_payload("not-a-jwt"), {})
        self.assertEqual(jwt_payload(""), {})

    def test_resolve_reports_embedded_only_videos_distinctly(self):
        # 有些条目是外站嵌入（embedUrl 指向 YouTube 等，file/fileUrl 为 null）。
        # 这不该报成「私有视频，或需要登录」—— 那会让人白查登录状态。
        client = IwaraClient()

        async def fake_video(video_id):
            return {
                "id": video_id,
                "title": "mirror",
                "rating": "general",
                "fileUrl": None,
                "file": None,
                "embedUrl": "https://youtu.be/q8Z5yVhadWg",
            }

        client.video = fake_video  # type: ignore[assignment]
        with self.assertRaises(IwaraError) as ctx:
            asyncio.run(client.resolve("B2jhPDt5TP9S19"))
        self.assertEqual(ctx.exception.status, 404)
        self.assertEqual(ctx.exception.reason, "embedded_only")
        self.assertIn("youtu.be", ctx.exception.message)

    def test_resolve_still_flags_missing_fileurl_as_auth(self):
        client = IwaraClient()

        async def fake_video(video_id):
            return {"id": video_id, "fileUrl": None, "file": None, "embedUrl": None}

        client.video = fake_video  # type: ignore[assignment]
        with self.assertRaises(IwaraError) as ctx:
            asyncio.run(client.resolve("private-id"))
        self.assertEqual(ctx.exception.status, 403)
        self.assertEqual(ctx.exception.reason, "auth_required")


@unittest.skipUnless(HAVE_IWARA, "需要 curl_cffi：pip install -r iwara/requirements.txt")
class IwaraTransportTestCase(unittest.TestCase):
    """传输层：种子 IP 池、单 IP 固定、失败换 IP。

    背景：本机（及大量国内线路）对 ``*.iwara.tv`` 的 DNS 被污染，连 DoH 都
    返回 Facebook/Twitter 段地址。而 libcurl 的 ``CURLOPT_RESOLVE`` **不是**
    候选列表 —— 同一 host:port 只认最后一条，所以必须自己探测可达性、固定到
    单个 IP，并在失败时换人。这些用例把真实的 socket/curl 调用打桩，验证这套
    逻辑本身。
    """

    def test_seed_hosts_cover_the_service_hosts(self):
        for host in ("api.iwara.tv", "files.iwara.tv", "filesq.iwara.tv"):
            self.assertIn(host, SEED_HOSTS, f"{host} 缺种子 IP，DNS 被污染时会全线挂掉")
            self.assertTrue(SEED_HOSTS[host], f"{host} 的种子列表不能为空")
        self.assertTrue(SEED_IPS, "视频花名主机需要一张通用边缘池")

    def test_resolve_host_puts_seeds_before_poisoned_doh_answers(self):
        # DoH 在本机返回的是 Facebook 段污染地址；种子必须排前面，
        # 否则 pick_ip 会先去试那个必然超时的地址。
        client = IwaraClient(doh_endpoints=("https://doh.invalid/dns-query",))

        async def fake_query(endpoint, host):
            return ["157.240.3.8"]  # 典型的污染答案

        client._query_doh = fake_query  # type: ignore[assignment]
        ips = asyncio.run(client.resolve_host("filesq.iwara.tv"))
        self.assertEqual(ips[0], SEED_HOSTS["filesq.iwara.tv"][0])
        self.assertIn("157.240.3.8", ips)
        self.assertLess(ips.index(SEED_HOSTS["filesq.iwara.tv"][0]), ips.index("157.240.3.8"))

    def test_unregistered_cdn_host_gets_the_whole_edge_pool(self):
        client = IwaraClient(doh_endpoints=("https://doh.invalid/dns-query",))

        async def fake_query(endpoint, host):
            return []

        client._query_doh = fake_query  # type: ignore[assignment]
        ips = asyncio.run(client.resolve_host("firefly.iwara.tv"))
        for seed in SEED_IPS:
            self.assertIn(seed, ips)
        self.assertNotIn("firefly.iwara.tv", SEED_HOSTS)

    def test_pick_ip_skips_unreachable_candidates(self):
        client = IwaraClient()
        client._ip_cache["filesq.iwara.tv"] = ["10.255.255.1", "5.61.209.181"]

        async def fake_reachable(ip, port=443):
            return ip == "5.61.209.181"

        client._tcp_reachable = fake_reachable  # type: ignore[assignment]
        self.assertEqual(asyncio.run(client.pick_ip("filesq.iwara.tv")), "5.61.209.181")

    def test_pick_ip_raises_when_nothing_is_reachable(self):
        client = IwaraClient(ip_attempts=2)
        client._ip_cache["filesq.iwara.tv"] = ["10.0.0.1", "10.0.0.2"]

        async def fake_reachable(ip, port=443):
            return False

        client._tcp_reachable = fake_reachable  # type: ignore[assignment]
        with self.assertRaises(IwaraError):
            asyncio.run(client.pick_ip("filesq.iwara.tv"))

    def test_session_pins_exactly_one_ip(self):
        # CurlOpt.RESOLVE 只认最后一条记录，传多条等于随机丢弃候选。
        client = IwaraClient()
        client._live_ips["filesq.iwara.tv"] = "5.61.209.181"
        session = asyncio.run(client.session_for("filesq.iwara.tv"))
        self.addCleanup(lambda: asyncio.run(session.close()))
        self.assertEqual(
            session.curl_options[CurlOpt.RESOLVE],
            ["filesq.iwara.tv:443:5.61.209.181"],
        )
        self.assertIsInstance(session.curl_options[CurlOpt.CONNECTTIMEOUT], int)

    def test_mark_ip_dead_clears_the_pin_and_session(self):
        client = IwaraClient()
        client._live_ips["filesq.iwara.tv"] = "5.61.209.181"
        client._sessions["filesq.iwara.tv"] = object()
        asyncio.run(client.mark_ip_dead("filesq.iwara.tv", "5.61.209.181"))
        self.assertNotIn("filesq.iwara.tv", client._live_ips)
        self.assertNotIn("filesq.iwara.tv", client._sessions)
        self.assertIn("5.61.209.181", client._dead_ips["filesq.iwara.tv"])

    def test_call_with_ip_retry_swaps_ip_after_connect_failure(self):
        client = IwaraClient(ip_attempts=3)
        tried = []

        class FakeResp:
            status_code = 200

        async def fake_session_for(host):
            ip = client._live_ips.get(host)
            if ip is None:
                # 第一次给坏 IP，之后给好 IP
                ip = "10.0.0.1" if not tried else "5.61.209.181"
                client._live_ips[host] = ip
            return object()

        async def call(session):
            ip = client._live_ips["filesq.iwara.tv"]
            tried.append(ip)
            if ip == "10.0.0.1":
                raise OSError("connect timed out")
            return FakeResp()

        client.session_for = fake_session_for  # type: ignore[assignment]
        resp = asyncio.run(client._call_with_ip_retry("filesq.iwara.tv", call))
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(tried, ["10.0.0.1", "5.61.209.181"])

    def test_call_with_ip_retry_retries_a_404_when_told_to(self):
        # 边缘按内容路由：池里某些 IP 会对同一 URL 回 404，换一个就有。
        client = IwaraClient(ip_attempts=3)
        seen = []
        ips = ["151.115.90.3", "151.115.89.180"]

        async def fake_session_for(host):
            client._live_ips.setdefault(host, ips[len(seen) - 1] if seen else ips[0])
            return object()

        class FakeResp:
            def __init__(self, status):
                self.status_code = status

        async def call(session):
            ip = client._live_ips["firefly.iwara.tv"]
            seen.append(ip)
            return FakeResp(404 if ip == ips[0] else 206)

        original_mark = client.mark_ip_dead

        async def fake_mark(host, ip):
            await original_mark(host, ip)
            # 拉黑后换下一个候选
            remaining = [i for i in ips if i not in client._dead_ips.get(host, set())]
            if remaining:
                client._live_ips[host] = remaining[0]

        client.session_for = fake_session_for  # type: ignore[assignment]
        client.mark_ip_dead = fake_mark  # type: ignore[assignment]
        resp = asyncio.run(
            client._call_with_ip_retry("firefly.iwara.tv", call, retry_statuses=(404,))
        )
        self.assertEqual(resp.status_code, 206)
        self.assertEqual(seen, [ips[0], ips[1]])

    def test_call_with_ip_retry_does_not_retry_404_by_default(self):
        client = IwaraClient(ip_attempts=3)
        calls = []

        class FakeResp:
            status_code = 404

        async def fake_session_for(host):
            client._live_ips.setdefault(host, "104.20.28.232")
            return object()

        async def call(session):
            calls.append(1)
            return FakeResp()

        client.session_for = fake_session_for  # type: ignore[assignment]
        resp = asyncio.run(client._call_with_ip_retry("api.iwara.tv", call))
        self.assertEqual(resp.status_code, 404)
        self.assertEqual(len(calls), 1, "API 的 404 是真实业务状态，不该换 IP 重试")


@unittest.skipUnless(HAVE_IWARA, "需要 curl_cffi：pip install -r iwara/requirements.txt")
class IwaraAuthTestCase(unittest.TestCase):
    """登录态：token 落盘 + 请求前的自动续期 + 失败退回匿名。"""

    def setUp(self):
        # 和 test_bridge.py 一样：临时目录放在工作区的 _tmp_tests/ 下，不用系统
        # TEMP —— 这台机器的沙箱不允许往 TEMP 的新建子目录里写文件，
        # tempfile.TemporaryDirectory 连清理都会因 chmod 被拒而报错。
        self.tmp = TEST_TMP_ROOT / f"iwara_auth_{os.getpid()}_{next(_TEMP_COUNTER)}"
        self.tmp.mkdir(parents=True, exist_ok=True)
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.path = self.tmp / "iwara_tokens.json"

    def test_save_then_load_round_trips(self):
        saved = tokens.save_token("refresh-abc", self.path)
        self.assertEqual(saved, self.path)
        self.assertEqual(tokens.load_token(self.path), "refresh-abc")

    def test_load_returns_empty_for_missing_file(self):
        self.assertEqual(tokens.load_token(self.path), "")

    def test_load_returns_empty_for_corrupt_file(self):
        self.path.write_text("{not json", encoding="utf-8")
        self.assertEqual(tokens.load_token(self.path), "")

    def test_load_returns_empty_for_wrong_shape(self):
        self.path.write_text(json.dumps(["nope"]), encoding="utf-8")
        self.assertEqual(tokens.load_token(self.path), "")

    def test_load_tolerates_non_string_token(self):
        self.path.write_text(json.dumps({"refresh_token": 123}), encoding="utf-8")
        self.assertEqual(tokens.load_token(self.path), "")

    def test_clear_token(self):
        tokens.save_token("x", self.path)
        self.assertTrue(tokens.clear_token(self.path))
        self.assertFalse(tokens.clear_token(self.path), "再删一次应返回 False")
        self.assertFalse(self.path.exists())

    def test_env_token_wins_over_file(self):
        tokens.save_token("from-file", self.path)
        self.assertEqual(tokens.resolve(None, env_token="from-env"), "from-env")

    def test_resolve_falls_back_to_file(self):
        tokens.save_token("from-file", self.path)
        # data_dir 指到临时目录，避免读到真实项目里的 data/。
        self.assertEqual(tokens.resolve(self.path.parent), "from-file")

    def test_env_token_blank_is_ignored(self):
        tokens.save_token("from-file", self.path)
        self.assertEqual(tokens.resolve(self.path.parent, env_token="  "), "from-file")

    def test_state_factory_passes_the_token_to_the_client(self):
        from iwara.app import iwara_state_factory

        # 用环境变量注入，避免依赖真实文件；data_dir 指到空的临时目录。
        os.environ["IWARA_REFRESH_TOKEN"] = "env-refresh"
        self.addCleanup(os.environ.pop, "IWARA_REFRESH_TOKEN", None)
        settings = make_settings()
        settings.data_dir = self.tmp
        client = iwara_state_factory(settings)["iwara_client"]
        self.addCleanup(lambda: asyncio.run(client.aclose()))
        self.assertEqual(client.refresh_token, "env-refresh")

    def test_state_factory_is_anonymous_without_a_token(self):
        from iwara.app import iwara_state_factory

        os.environ.pop("IWARA_REFRESH_TOKEN", None)
        settings = make_settings()
        settings.data_dir = self.tmp
        client = iwara_state_factory(settings)["iwara_client"]
        self.addCleanup(lambda: asyncio.run(client.aclose()))
        self.assertIsNone(client.refresh_token)

    def test_prepare_auth_is_a_noop_without_tokens(self):
        client = IwaraClient()
        asyncio.run(client._prepare_auth("/videos"))
        self.assertEqual(client.auth_error, "")
        self.assertFalse(client._auth_failed)

    def test_prepare_auth_skips_the_auth_endpoints_themselves(self):
        # 认证接口不能被自动续期拦，否则 refresh 里再触发 refresh 就递归了。
        client = IwaraClient(refresh_token="r")
        called = []

        async def boom():
            called.append(1)
            raise AssertionError("认证接口不该触发续期")

        client.ensure_access_token = boom  # type: ignore[assignment]
        asyncio.run(client._prepare_auth("/user/token"))
        asyncio.run(client._prepare_auth("/user/login"))
        self.assertEqual(called, [])

    def test_prepare_auth_records_failure_and_degrades_to_anonymous(self):
        client = IwaraClient(refresh_token="expired")

        async def boom():
            raise IwaraError("刷新 token 失败（403）", status=401)

        client.ensure_access_token = boom  # type: ignore[assignment]
        asyncio.run(client._prepare_auth("/videos"))
        self.assertTrue(client._auth_failed)
        self.assertIn("403", client.auth_error)
        self.assertIsNone(client.access_token)

    def test_prepare_auth_does_not_retry_after_failure(self):
        client = IwaraClient(refresh_token="expired")
        calls = []

        async def boom():
            calls.append(1)
            raise IwaraError("坏了", status=401)

        client.ensure_access_token = boom  # type: ignore[assignment]
        asyncio.run(client._prepare_auth("/videos"))
        asyncio.run(client._prepare_auth("/videos"))
        asyncio.run(client._prepare_auth("/videos"))
        self.assertEqual(len(calls), 1, "失败后不该在每个请求上重复撞墙")

    def test_prepare_auth_refreshes_and_sets_the_header(self):
        client = IwaraClient(refresh_token="good")

        async def ok():
            client.access_token = "access-xyz"
            return "access-xyz"

        client.ensure_access_token = ok  # type: ignore[assignment]
        asyncio.run(client._prepare_auth("/videos"))
        self.assertEqual(client.auth_error, "")
        self.assertEqual(client.headers()["authorization"], "Bearer access-xyz")


def _make_jwt(exp: int, username: str | None = None) -> str:
    """造一个形状正确的 JWT（不验签，客户端也只 base64 解载荷）。

    **刻意默认不带 ``username``**：真实 access token 的载荷是
    ``['exp','iat','id','iss','premium','role','type']``，没有用户名。以前这里
    塞了 ``username``，正好把「从载荷取用户名」那个 bug 掩盖住了。
    """

    def seg(payload: dict) -> str:
        raw = json.dumps(payload).encode("utf-8")
        return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")

    claims = {"exp": exp, "iat": exp - 3600, "id": "user-uuid", "iss": "iwara", "type": "access_token"}
    if username is not None:  # 只有明确要求时才加，模拟不存在的场景
        claims["username"] = username
    return f"{seg({'alg': 'HS256', 'typ': 'JWT'})}.{seg(claims)}.sig"


class _FakeResp:
    """`request()` 只会用到 status_code / headers / json() / text。"""

    def __init__(self, status_code=200, payload=None, headers=None, text=""):
        self.status_code = status_code
        self._payload = payload if payload is not None else {}
        self.headers = headers or {}
        self.text = text or json.dumps(self._payload)

    def json(self):
        return self._payload


@unittest.skipUnless(HAVE_IWARA, "需要 curl_cffi：pip install -r iwara/requirements.txt")
class IwaraTokenHttpTestCase(unittest.TestCase):
    """换 token 的真实 HTTP 形状，以及「403 表示过期」的强制重放。

    既有的用例都把 ``ensure_access_token`` 整个打桩掉，所以从没验证过它实际
    发出去的是什么（方法、路径、空 body、refresh token 放哪个头），也没验证过
    服务端用 403 而不是 401 表示过期时那次重放。这里补上这两块。
    """

    def _client_with_a_live_token(self) -> "IwaraClient":
        # access token 离过期还很远，好让 _prepare_auth 保持不动。
        client = IwaraClient(refresh_token="refresh-good", access_token=_make_jwt(int(time.time()) + 3600))
        return client

    def _patch_request(self, client: "IwaraClient"):
        """记录 request() 收到的参数，并按序返回假响应。"""
        seen: list[dict] = []
        queue: list[_FakeResp] = []

        async def fake_request(method, path, **kw):
            seen.append({"method": method, "path": path, **kw})
            if not queue:
                raise AssertionError(f"没有排队的响应给 {method} {path}")
            return queue.pop(0)

        client.request = fake_request  # type: ignore[assignment]
        return seen, queue

    # ---------- refresh_access 的请求形状 ----------

    def test_refresh_access_posts_an_empty_body_with_the_refresh_token_in_a_header(self):
        client = IwaraClient(refresh_token="refresh-good")
        seen, queue = self._patch_request(client)
        queue.append(_FakeResp(payload={"accessToken": "access-new"}))

        token = asyncio.run(client.refresh_access())

        self.assertEqual(token, "access-new")
        self.assertEqual(client.access_token, "access-new")
        self.assertEqual(len(seen), 1)
        call = seen[0]
        self.assertEqual(call["method"], "POST")
        self.assertEqual(call["path"], "/user/token")
        # 空 body：这个端点靠 authorization 头认人，不是 JSON 体。
        self.assertIsNone(call.get("json_body"))
        self.assertEqual(call["headers"]["authorization"], "Bearer refresh-good")

    def test_refresh_access_treats_403_as_a_dead_token(self):
        # 关键：这个 API 过期 token 回 403，不是 401，两者都必须当失败。
        client = IwaraClient(refresh_token="refresh-expired")
        seen, queue = self._patch_request(client)
        queue.append(_FakeResp(403, text='{"message":"errors.forbidden"}'))

        with self.assertRaises(IwaraAuthError) as ctx:
            asyncio.run(client.refresh_access())
        self.assertIn("403", str(ctx.exception))
        self.assertIsNone(client.access_token)

    def test_refresh_access_rejects_a_response_without_access_token(self):
        client = IwaraClient(refresh_token="refresh-good")
        seen, queue = self._patch_request(client)
        queue.append(_FakeResp(payload={"nope": True}))

        with self.assertRaises(IwaraAuthError):
            asyncio.run(client.refresh_access())

    def test_refresh_access_without_a_refresh_token_does_not_send_anything(self):
        client = IwaraClient()
        seen, queue = self._patch_request(client)

        with self.assertRaises(IwaraAuthError):
            asyncio.run(client.refresh_access())
        self.assertEqual(seen, [], "连 refresh token 都没有就不该发请求")

    # ---------- ensure_access_token 的续期时机 ----------

    def test_ensure_access_token_refreshes_when_there_is_no_access_token(self):
        client = IwaraClient(refresh_token="refresh-good")
        calls = []

        async def fake_refresh():
            calls.append(1)
            client.access_token = "access-new"
            return "access-new"

        client.refresh_access = fake_refresh  # type: ignore[assignment]
        self.assertEqual(asyncio.run(client.ensure_access_token()), "access-new")
        self.assertEqual(len(calls), 1)

    def test_ensure_access_token_keeps_a_token_that_is_still_fresh(self):
        client = self._client_with_a_live_token()
        calls = []

        async def fake_refresh():  # pragma: no cover - 不该被调到
            calls.append(1)
            return "should-not-happen"

        client.refresh_access = fake_refresh  # type: ignore[assignment]
        asyncio.run(client.ensure_access_token())
        self.assertEqual(calls, [], "离过期还有一小时，不该白白换一次")

    def test_ensure_access_token_renews_inside_the_five_minute_window(self):
        # 提前 300 秒续期，免得请求正好卡在过期那一瞬。
        client = IwaraClient(refresh_token="refresh-good", access_token=_make_jwt(int(time.time()) + 120))
        calls = []

        async def fake_refresh():
            calls.append(1)
            client.access_token = "access-renewed"
            return "access-renewed"

        client.refresh_access = fake_refresh  # type: ignore[assignment]
        self.assertEqual(asyncio.run(client.ensure_access_token()), "access-renewed")
        self.assertEqual(len(calls), 1)

    def test_ensure_access_token_renews_a_token_without_an_exp_claim(self):
        # 载荷解不出来（畸形 token）时，宁愿换新的也不赌它还有效。
        client = IwaraClient(refresh_token="refresh-good", access_token="not-a-jwt")
        calls = []

        async def fake_refresh():
            calls.append(1)
            client.access_token = "access-renewed"
            return "access-renewed"

        client.refresh_access = fake_refresh  # type: ignore[assignment]
        self.assertEqual(asyncio.run(client.ensure_access_token()), "access-renewed")
        self.assertEqual(len(calls), 1)

    # ---------- request() 的 403 强制重放 ----------

    def test_request_replays_once_after_a_403(self):
        # 服务端用 403 表示「token 过期」。带 token 收到 403 时应换一次 token
        # 再重放同一请求，而且只放一次。
        client = self._client_with_a_live_token()
        client._call_with_ip_retry = self._queue_retries(client)
        responses = client._call_with_ip_retry.queue  # type: ignore[attr-defined]
        responses.append(_FakeResp(403, text='{"message":"expired"}'))
        responses.append(_FakeResp(200, payload={"ok": True}))

        refreshed = []

        async def fake_refresh():
            refreshed.append(1)
            client.access_token = _make_jwt(int(time.time()) + 3600)
            return client.access_token

        client.refresh_access = fake_refresh  # type: ignore[assignment]
        resp = asyncio.run(client.request("GET", "/videos"))

        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json(), {"ok": True})
        self.assertEqual(len(refreshed), 1, "403 应触发一次换 token")
        self.assertTrue(client._auth_retry_used)

    def test_request_does_not_replay_a_second_403_forever(self):
        # token 真失效时不能死循环：重放一次又回 403 就把响应交出去。
        client = self._client_with_a_live_token()
        client._call_with_ip_retry = self._queue_retries(client)
        responses = client._call_with_ip_retry.queue  # type: ignore[attr-defined]
        responses.append(_FakeResp(403, text='{"message":"expired"}'))
        responses.append(_FakeResp(403, text='{"message":"still expired"}'))

        refreshed = []

        async def fake_refresh():  # pragma: no cover - 只该调一次
            refreshed.append(1)
            client.access_token = _make_jwt(int(time.time()) + 3600)
            return client.access_token

        client.refresh_access = fake_refresh  # type: ignore[assignment]
        resp = asyncio.run(client.request("GET", "/videos"))

        self.assertEqual(resp.status_code, 403)
        self.assertEqual(len(refreshed), 1, "只放一次，不能无限循环")

    def test_request_surfaces_a_failed_refresh_without_losing_the_403(self):
        client = self._client_with_a_live_token()
        client._call_with_ip_retry = self._queue_retries(client)
        client._call_with_ip_retry.queue.append(  # type: ignore[attr-defined]
            _FakeResp(403, text='{"message":"expired"}')
        )

        async def boom():
            raise IwaraAuthError("刷新 token 失败（403）: errors.forbidden")

        client.refresh_access = boom  # type: ignore[assignment]
        resp = asyncio.run(client.request("GET", "/videos"))

        # 换不出 token 也要把原始 403 交回去，并记下原因退化成匿名。
        self.assertEqual(resp.status_code, 403)
        self.assertTrue(client._auth_failed)
        self.assertIn("403", client.auth_error)

    def test_request_does_not_replay_without_a_refresh_token(self):
        # 匿名访问收到 403 就是真没权限，不是「token 过期」，不该去换 token。
        client = IwaraClient()
        client._call_with_ip_retry = self._queue_retries(client)
        client._call_with_ip_retry.queue.append(  # type: ignore[attr-defined]
            _FakeResp(403, text='{"message":"nope"}')
        )

        async def fake_refresh():  # pragma: no cover - 不该被调到
            raise AssertionError("匿名不该尝试续期")

        client.refresh_access = fake_refresh  # type: ignore[assignment]
        resp = asyncio.run(client.request("GET", "/videos"))
        self.assertEqual(resp.status_code, 403)

    @staticmethod
    def _queue_retries(client: "IwaraClient"):
        """把真正发请求的那层换成「按队列吐响应」，并挂在函数上便于取用。"""

        async def fake_call(host, call, *, retry_statuses=()):
            return fake_call.queue.pop(0)  # type: ignore[attr-defined]

        fake_call.queue = []  # type: ignore[attr-defined]
        return fake_call


@unittest.skipUnless(HAVE_IWARA, "需要 curl_cffi：pip install -r iwara/requirements.txt")
class IwaraRouteTestCase(unittest.TestCase):
    """路由层：真实 app + 假客户端，验证形状、装饰与主机白名单。"""

    def _client(self):
        app = create_iwara_app(make_settings(), transport=httpx.MockTransport(_mock_handler))
        client = TestClient(app)
        client.__enter__()
        self.addCleanup(client.__exit__, None, None, None)
        self.fake = _FakeIwaraClient()
        client.app.state.iwara_client = self.fake
        return client

    def test_videos_returns_envelope_with_thumbnails(self):
        client = self._client()
        body = client.get("/api/iwara/videos?limit=5").json()
        self.assertEqual(body["count"], 1)
        item = body["results"][0]
        self.assertIn("/image/thumbnail/", item["thumbnailUrl"])
        self.assertIn("/image/original/", item["posterUrl"])
        self.assertEqual(self.fake.calls[0][0], "list_videos")
        self.assertEqual(self.fake.calls[0][1]["limit"], 5)

    def test_videos_passes_filters_through(self):
        client = self._client()
        client.get("/api/iwara/videos?rating=ecchi&sort=trending&tags=dance&user=someone")
        kwargs = self.fake.calls[0][1]
        self.assertEqual(kwargs["rating"], "ecchi")
        self.assertEqual(kwargs["sort"], "trending")
        self.assertEqual(kwargs["tags"], "dance")
        self.assertEqual(kwargs["user_id"], "someone")

    def test_videos_limit_is_bounded(self):
        client = self._client()
        self.assertEqual(client.get("/api/iwara/videos?limit=999").status_code, 422)
        self.assertEqual(client.get("/api/iwara/videos?limit=0").status_code, 422)

    def test_search_requires_query(self):
        client = self._client()
        self.assertEqual(client.get("/api/iwara/search").status_code, 422)
        body = client.get("/api/iwara/search?query=dance&type=videos").json()
        self.assertIn("/image/thumbnail/", body["results"][0]["thumbnailUrl"])

    def test_video_detail_is_decorated(self):
        client = self._client()
        body = client.get("/api/iwara/video/b0pRCNfo8Igu3p").json()
        self.assertEqual(body["id"], "b0pRCNfo8Igu3p")
        self.assertIn("/image/thumbnail/", body["thumbnailUrl"])

    def test_related_returns_envelope_with_thumbnails(self):
        client = self._client()
        body = client.get("/api/iwara/related/b0pRCNfo8Igu3p").json()
        self.assertEqual(body["count"], 2)
        self.assertEqual([item["id"] for item in body["results"]], ["related-1", "related-2"])
        self.assertIn("/image/thumbnail/", body["results"][0]["thumbnailUrl"])
        self.assertEqual(self.fake.calls[0], ("related", "b0pRCNfo8Igu3p"))

    def test_related_pages_locally(self):
        # 上游 /related 不接受分页参数，固定给 12 条；分页在本地裁剪。
        client = self._client()
        first = client.get("/api/iwara/related/b0pRCNfo8Igu3p?limit=1").json()
        self.assertEqual([item["id"] for item in first["results"]], ["related-1"])
        second = client.get("/api/iwara/related/b0pRCNfo8Igu3p?page=1&limit=1").json()
        self.assertEqual([item["id"] for item in second["results"]], ["related-2"])

    def test_related_limit_is_bounded(self):
        client = self._client()
        self.assertEqual(client.get("/api/iwara/related/b0pRCNfo8Igu3p?limit=999").status_code, 422)
        self.assertEqual(client.get("/api/iwara/related/b0pRCNfo8Igu3p?limit=0").status_code, 422)

    def test_related_surfaces_upstream_failure_without_500(self):
        client = self._client()
        self.fake.related_error = "上游炸了"
        response = client.get("/api/iwara/related/b0pRCNfo8Igu3p")
        self.assertEqual(response.status_code, 502)
        self.assertEqual(response.json()["detail"], "上游炸了")

    def test_video_page_is_not_part_of_the_api_project(self):
        # 视频页面已经拆到独立的扩展包 iwara_web：接口项目本身不提供它。
        # （页面能不能打开由 IwaraWebExtensionTestCase 验证。）
        client = self._client()
        self.assertEqual(client.get("/iwara").status_code, 404)

    def test_resolve_reports_chosen_source(self):
        client = self._client()
        body = client.get("/api/iwara/resolve/b0pRCNfo8Igu3p").json()
        self.assertEqual(body["salt_used"], "test-salt")
        self.assertEqual(body["file_size"], 53874359)
        self.assertEqual([s["name"] for s in body["sources"]], ["Source", "540", "preview"])
        self.assertTrue(body["chosen"]["verified"])
        self.assertEqual(body["chosen"]["host"], "firefly.iwara.tv")
        self.assertEqual(body["chosen"]["content_range"], "bytes 0-1023/53874359")
        self.assertIsNone(body["probe_error"])

    def test_resolve_can_skip_probing(self):
        client = self._client()
        body = client.get("/api/iwara/resolve/b0pRCNfo8Igu3p?probe=false").json()
        self.assertIsNone(body["chosen"])
        self.assertNotIn("probe_any", [call[0] for call in self.fake.calls])

    def test_resolve_surfaces_probe_failure_without_500(self):
        client = self._client()
        self.fake.probe_error = "所有视频源都不可用"
        response = client.get("/api/iwara/resolve/b0pRCNfo8Igu3p")
        self.assertEqual(response.status_code, 200)
        self.assertIsNone(response.json()["chosen"])
        self.assertEqual(response.json()["probe_error"], "所有视频源都不可用")

    def test_stream_forwards_range_and_marks_private(self):
        client = self._client()
        response = client.get("/api/iwara/stream", params={"url": VIDEO_URL}, headers={"Range": "bytes=0-4095"})
        self.assertEqual(response.status_code, 206)
        self.assertEqual(response.headers["content-range"], "bytes 0-4095/53874359")
        self.assertEqual(response.content[:12], b"\x00\x00\x00 ftypisom")
        self.assertIn("private", response.headers["cache-control"])
        forwarded = self.fake.calls[-1][2]
        self.assertEqual(forwarded["range"], "bytes=0-4095")

    def test_stream_rejects_foreign_hosts(self):
        client = self._client()
        for url in ("https://evil.example.com/x.mp4", "https://notiwara.com/x.mp4", "https://iwara.tv.evil.com/x.mp4"):
            response = client.get("/api/iwara/stream", params={"url": url})
            self.assertEqual(response.status_code, 400, url)

    def test_image_rejects_foreign_hosts(self):
        client = self._client()
        self.assertEqual(
            client.get("/api/iwara/image", params={"url": "https://evil.example.com/a.jpg"}).status_code,
            400,
        )
        ok = client.get("/api/iwara/image", params={"url": "https://files.iwara.tv/image/thumbnail/x/thumbnail-00.jpg"})
        self.assertEqual(ok.status_code, 200)
        self.assertEqual(ok.headers["content-type"], "image/jpeg")

    def test_thumbnail_builds_url_from_video_id(self):
        client = self._client()
        response = client.get("/api/iwara/thumbnail/b0pRCNfo8Igu3p")
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.content.startswith(b"\xff\xd8\xff"))
        url = self.fake.calls[-1][1]
        self.assertEqual(
            url,
            f"https://{THUMBNAIL_HOST}/image/thumbnail/"
            "11111111-2222-3333-4444-555555555555/thumbnail-00.jpg",
        )

    def test_thumbnail_original_index(self):
        client = self._client()
        client.get("/api/iwara/thumbnail/b0pRCNfo8Igu3p?index=1&original=true")
        self.assertIn("/image/original/", self.fake.calls[-1][1])
        self.assertTrue(self.fake.calls[-1][1].endswith("thumbnail-01.jpg"))

    def test_thumbnail_404_when_video_has_no_file(self):
        client = self._client()

        async def no_file(video_id):
            return {"id": video_id, "title": "没有文件"}

        self.fake.video = no_file
        self.assertEqual(client.get("/api/iwara/thumbnail/whatever").status_code, 404)

    def test_me_surfaces_upstream_401(self):
        client = self._client()
        response = client.get("/api/iwara/me")
        self.assertEqual(response.status_code, 401)

    def test_shutdown_releases_the_client(self):
        app = create_iwara_app(make_settings(), transport=httpx.MockTransport(_mock_handler))
        with TestClient(app):
            fake = _FakeIwaraClient()
            app.state.iwara_client = fake
        self.assertTrue(fake.closed)

    def test_pixiv_routes_still_present(self):
        client = self._client()
        self.assertEqual(client.get("/api/health").status_code, 200)

    def test_auth_reports_anonymous_when_no_token(self):
        client = self._client()
        body = client.get("/api/iwara/auth").json()
        self.assertFalse(body["configured"])
        self.assertTrue(body["anonymous"])
        self.assertFalse(body["authenticated"])
        self.assertIsNone(body["error"])
        self.assertIn("iwara_tokens.json", body["token_file"])

    def test_auth_probes_a_configured_token(self):
        client = self._client()
        self.fake.refresh_token = "fake-refresh"
        body = client.get("/api/iwara/auth").json()
        self.assertTrue(body["configured"])
        self.assertTrue(body["authenticated"])
        self.assertFalse(body["anonymous"])
        self.assertIn(("ensure_access_token",), self.fake.calls)
        # access token 的载荷里没有用户名，所以只能问一次 /user —— 这条路径必须
        # 真的去问了，否则 username_hint 永远是 None（曾经就是这个 bug）。
        self.assertIn(("me",), self.fake.calls)
        self.assertEqual(body["username_hint"], "huanghe19512")

    def test_auth_does_not_ask_for_the_user_while_anonymous(self):
        client = self._client()
        body = client.get("/api/iwara/auth").json()
        self.assertTrue(body["anonymous"])
        self.assertNotIn("username_hint", body)
        # 匿名时 /user 会返回 401，看着像报错 —— 不许去打。
        self.assertNotIn(("me",), self.fake.calls)

    def test_auth_surfaces_a_dead_token_without_500(self):
        client = self._client()
        self.fake.refresh_token = "expired"
        self.fake.refresh_error = "刷新 token 失败（403）"
        response = client.get("/api/iwara/auth")
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertFalse(body["authenticated"])
        self.assertIn("403", body["error"])

    def test_auth_keeps_a_previously_recorded_error(self):
        client = self._client()
        self.fake.refresh_token = "bad"
        self.fake.auth_error = "刷新未返回 accessToken"
        body = client.get("/api/iwara/auth").json()
        self.assertFalse(body["authenticated"])
        self.assertIn("accessToken", body["error"])
        # 客户端已经记过错，就别再打一次网络。
        self.assertNotIn(("ensure_access_token",), self.fake.calls)


@unittest.skipUnless(HAVE_IWARA, "需要 curl_cffi：pip install -r iwara/requirements.txt")
class IwaraEntryPointTestCase(unittest.TestCase):
    """入口与打包：路由集合、路由超集、依赖清单。"""

    def test_iwara_routers_cover_the_video_api(self):
        from iwara.app import IWARA_ROUTERS, IWARA_STATE_KEYS, iwara_state_factory

        # 只剩视频接口路由 —— 视频页面拆给了独立的扩展包 iwara_web。
        self.assertEqual(len(IWARA_ROUTERS), 1)
        # nav_sections 是给主项目导航页（`/`）用的：iwara 不改路由，
        # 而是把自己的接口加进那一页（见 iwara/app.py 的 IWARA_NAV）。
        self.assertEqual(IWARA_STATE_KEYS, ("iwara_client", "nav_sections"))
        self.assertEqual(
            sorted(iwara_state_factory(make_settings())),
            ["iwara_client", "nav_sections"],
        )

    def test_iwara_nav_lists_only_json_apis(self):
        from iwara.app import IWARA_NAV

        paths = [item.path for item in IWARA_NAV.items]
        self.assertTrue(all(p.startswith("/api/iwara/") for p in paths), paths)
        # 页面由 iwara_web 扩展自己声明（EXTENSION_NAV），这里不列，
        # 否则没挂那个包时导航页会出现一条 404 链接。
        self.assertNotIn("/iwara", paths)

    def test_iwara_adds_routes_to_the_pure_app(self):
        def paths(app):
            # 新版 FastAPI 的 include_router() 会塞一个 _IncludedRouter 包装对象，
            # 真正的路由在 .original_router.routes 里；旧版本来就是摊平的。
            collected = set()
            for route in app.routes:
                holder = getattr(route, "original_router", None)
                for item in holder.routes if holder is not None else [route]:
                    path = getattr(item, "path", None)
                    if path is not None:
                        collected.add(path)
            return collected

        pure = paths(_create_bridge_app(make_settings(), transport=httpx.MockTransport(_mock_handler)))
        full = paths(create_iwara_app(make_settings(), transport=httpx.MockTransport(_mock_handler)))
        extra = full - pure
        self.assertTrue(pure <= full, "iwara 应用必须包含纯接口的全部路由")
        self.assertEqual(
            extra,
            {
                "/api/iwara/videos",
                "/api/iwara/search",
                "/api/iwara/video/{video_id}",
                "/api/iwara/related/{video_id}",
                "/api/iwara/resolve/{video_id}",
                "/api/iwara/stream",
                "/api/iwara/image",
                "/api/iwara/thumbnail/{video_id}",
                "/api/iwara/me",
                "/api/iwara/auth",
            },
        )

    def test_iwara_requirements_list_curl_cffi(self):
        from pathlib import Path

        requirements = Path(__file__).resolve().parent.parent / "iwara" / "requirements.txt"
        text = requirements.read_text(encoding="utf-8")
        self.assertIn("curl_cffi", text)
        self.assertIn("cffi", text)

    def test_curl_cffi_is_not_a_hard_dependency_of_the_main_project(self):
        # 只跑 pixiv 的人不该被要求装这个编译包。
        from pathlib import Path

        root_requirements = Path(__file__).resolve().parent.parent / "requirements.txt"
        self.assertNotIn("curl_cffi", root_requirements.read_text(encoding="utf-8"))

    def test_pure_bridge_does_not_expose_iwara_routes(self):
        client = TestClient(_create_bridge_app(make_settings(), transport=httpx.MockTransport(_mock_handler)))
        with client:
            self.assertEqual(client.get("/api/iwara/videos").status_code, 404)


@unittest.skipUnless(HAVE_IWARA, "需要 curl_cffi：pip install -r iwara/requirements.txt")
class IwaraEnvCheckTestCase(unittest.TestCase):
    """iwara 入口 ``--check`` 时多出来的那三项。

    它们跟主项目那五项打在同一张 ``[环境检查]`` 表里，但名字都不在
    ``FATAL_CHECKS`` 里 —— 装坏了/没登录只该提示，永远不该拦下启动。
    """

    def setUp(self):
        # 真实环境里可能已经有 token 文件；这里一律换成一个空目录。
        self.directory = make_temp_dir("iwara_envcheck")
        self.addCleanup(drop_temp_dir, self.directory)
        env = patch.dict(os.environ, {"IWARA_REFRESH_TOKEN": ""}, clear=False)
        env.start()
        self.addCleanup(env.stop)

    def _settings(self):
        return Settings(
            refresh_token="",
            rate_limit_rpm=10000,
            data_dir=str(self.directory),
        )

    def _fake_client(self, *, videos=None, error=None, username="tester"):
        """顶掉 iwara.client.IwaraClient，返回 (patch, instance)。"""
        cls = MagicMock()
        instance = cls.return_value

        async def _list_videos(**_kwargs):
            if error is not None:
                raise error
            return videos if videos is not None else {"count": 3, "results": []}

        async def _ensure():
            if error is not None:
                raise error
            return "access-token"

        async def _me():
            if error is not None:
                raise error
            return {"user": {"username": username}}

        instance.list_videos = AsyncMock(side_effect=_list_videos)
        instance.ensure_access_token = AsyncMock(side_effect=_ensure)
        instance.me = AsyncMock(side_effect=_me)
        instance.aclose = AsyncMock()
        return patch("iwara.client.IwaraClient", cls), instance

    def test_extra_checks_are_named_and_never_fatal(self):
        from pixiv_bridge.cli import FATAL_CHECKS

        from iwara.envcheck import AUTH_NAME, DEPS_NAME, NET_NAME

        self.assertEqual(
            sorted((DEPS_NAME, NET_NAME, AUTH_NAME)),
            ["iwara 依赖", "iwara 登录", "iwara 网络"],
        )
        for name in (DEPS_NAME, NET_NAME, AUTH_NAME):
            self.assertNotIn(name, FATAL_CHECKS)

    def test_extra_checks_returns_three_results_in_order(self):
        from iwara.envcheck import AUTH_NAME, DEPS_NAME, NET_NAME, extra_checks

        patcher, _ = self._fake_client()
        with patcher:
            results = extra_checks(self._settings())

        self.assertEqual([r.name for r in results], [DEPS_NAME, NET_NAME, AUTH_NAME])
        # 这台机器上确实装了 curl_cffi，否则这些用例本来就被整片跳过。
        self.assertTrue(results[0].ok)
        self.assertIn("curl_cffi", results[0].detail)

    def test_anonymous_mode_is_ok_and_says_how_to_log_in(self):
        from iwara.envcheck import extra_checks

        patcher, instance = self._fake_client()
        with patcher:
            results = extra_checks(self._settings())

        auth = [r for r in results if r.name == "iwara 登录"][0]
        self.assertTrue(auth.ok)
        self.assertIn("匿名模式", auth.detail)
        self.assertIn("iwara/get_token.py", auth.detail)
        # 没有 token 就不该去验 token。
        instance.ensure_access_token.assert_not_called()

    def test_network_failure_is_reported_not_raised(self):
        from iwara.envcheck import extra_checks

        patcher, _ = self._fake_client(error=IwaraError("都连不上"))
        with patcher:
            results = extra_checks(self._settings())

        net = [r for r in results if r.name == "iwara 网络"][0]
        self.assertFalse(net.ok)
        self.assertIn("api.iwara.tv 不可达", net.detail)
        self.assertIn("都连不上", net.detail)

    def test_broken_token_is_a_warning_with_the_relogin_hint(self):
        from iwara.client import IwaraAuthError
        from iwara.envcheck import extra_checks

        os.environ["IWARA_REFRESH_TOKEN"] = "stale.token.value"
        patcher, _ = self._fake_client(
            error=IwaraAuthError("刷新 token 失败（403）: errors.forbidden")
        )
        with patcher:
            results = extra_checks(self._settings())

        auth = [r for r in results if r.name == "iwara 登录"][0]
        self.assertFalse(auth.ok)
        self.assertIn("token 不可用", auth.detail)
        self.assertIn("iwara/get_token.py", auth.detail)

    def test_saved_token_is_verified_and_named(self):
        from iwara import tokens
        from iwara.envcheck import extra_checks

        tokens.save_token("good.token.value", self.directory / tokens.DEFAULT_FILENAME)
        patcher, instance = self._fake_client(username="huanghe19512")
        with patcher:
            results = extra_checks(self._settings())

        auth = [r for r in results if r.name == "iwara 登录"][0]
        self.assertTrue(auth.ok)
        self.assertIn("huanghe19512", auth.detail)
        instance.ensure_access_token.assert_awaited()


@unittest.skipUnless(HAVE_IWARA, "需要 curl_cffi：pip install -r iwara/requirements.txt")
class IwaraEntryWiringTestCase(unittest.TestCase):
    """入口脚本本身：自举要跑在 import iwara.app 之前，并且要挂上专属检查。"""

    def _source(self) -> str:
        path = Path(__file__).resolve().parent.parent / "iwara" / "run.py"
        return path.read_text(encoding="utf-8")

    def test_bootstrap_runs_before_importing_the_app(self):
        # iwara.app → iwara.client → import curl_cffi；这条链没装包时会直接
        # ImportError，必须排在 bootstrap 之后，否则自举根本没机会跑。
        # 只看真正的语句行：注释里也可能提到这两个名字。
        lines = [line.strip() for line in self._source().splitlines()]
        statements = [line for line in lines if line and not line.startswith("#")]
        bootstrap = next(
            i for i, line in enumerate(statements) if line.startswith("bootstrap(")
        )
        app_import = next(
            i
            for i, line in enumerate(statements)
            if line.startswith("from iwara.app import")
        )
        self.assertLess(bootstrap, app_import)

    def test_entry_passes_the_iwara_extra_checks(self):
        text = self._source()
        self.assertIn("from iwara.envcheck import extra_checks", text)
        self.assertIn("extra_checks=extra_checks", text)

    def test_get_token_does_not_import_the_app_package(self):
        # `from iwara import tokens` 不能再顺带把 iwara.app（→ curl_cffi）拖进来，
        # 否则没装包时登录脚本一开头就炸。
        path = Path(__file__).resolve().parent.parent / "iwara" / "get_token.py"
        text = path.read_text(encoding="utf-8")
        self.assertNotIn("from iwara.app import", text)
        self.assertNotIn("from iwara.client import", text.split("def _login")[0])

    def test_init_lazy_exports_keep_tokens_importable(self):
        # `iwara/__init__.py` 用模块级 __getattr__ 惰性转发，所以导入子模块时
        # 不会连锁拖进 curl_cffi。
        import iwara

        self.assertIn("create_app", iwara.__all__)
        self.assertIs(iwara.tokens, sys.modules["iwara.tokens"])


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
