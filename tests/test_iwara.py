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
import contextlib
import io
import itertools
import json
import os
import shutil
import sys
import time
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch
from urllib.parse import urlparse

import httpx
from fastapi.testclient import TestClient

from pixiv_bridge.config import Settings
from pixiv_bridge.extensions import load_extension_specs
from pixiv_bridge.server import create_bridge_app as _create_bridge_app

# 与 test_bridge.py 同一约定：临时的东西放工作区 _tmp_tests/，别用系统 TEMP。
PROJECT_ROOT = Path(__file__).resolve().parent.parent
TEST_TMP_ROOT = PROJECT_ROOT / "_tmp_tests"
_TEMP_COUNTER = itertools.count(1)

try:
    from curl_cffi.const import CurlOpt

    from iwara import ech as iwara_ech
    from iwara import tokens
    from iwara.app import SITE, create_app as create_iwara_app
    from iwara.app import iwara_state_factory
    from iwara.client import (
        ECH_HOSTS,
        SEED_HOSTS,
        SEED_IPS,
        THUMBNAIL_HOST,
        IwaraAuthError,
        IwaraChallengeError,
        IwaraClient,
        IwaraError,
        Resolved,
        Source,
        decorate_video,
        is_cdn_host,
        is_ech_host,
        jwt_payload,
        thumbnail_url,
        x_version,
    )
    from iwara.config import (
        IwaraSettings,
        as_iwara_settings,
        split_ips,
    )
    from iwara.envcheck import extra_checks

    HAVE_IWARA = True
    IWARA_IMPORT_ERROR = ""
except ImportError as exc:  # pragma: no cover - 只在没装 curl_cffi 时走到
    HAVE_IWARA = False
    IWARA_IMPORT_ERROR = str(exc)


def _mock_handler(request: httpx.Request) -> httpx.Response:
    """pixiv 侧的最小桩：这些用例不关心 pixiv 接口。"""
    return httpx.Response(200, json={"ok": True})


# 一个有意义的 ECH 配置：前两字节是长度，所以是「合法」的 ECHConfigList。
GOOD_ECH_B64 = base64.b64encode(b"\x00\x06hello!").decode("ascii")


def make_settings():
    # iwara 站点用框架的 Settings（Pixiv 那堆字段在 PixivSettings 里，
    # 站点之间互不共享配置）；refresh_token 之类的概念这里根本不存在。
    return Settings(rate_limit_rpm=10000)


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
        app = create_iwara_app(make_settings())
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
        app = create_iwara_app(make_settings())
        with TestClient(app):
            fake = _FakeIwaraClient()
            app.state.iwara_client = fake
        self.assertTrue(fake.closed)

    def test_framework_routes_are_present(self):
        # `/api/health` 由框架（pixiv_bridge/routes/health.py）提供，
        # 每个入口都有；站点自己不重复定义。
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

    def test_iwara_adds_exactly_its_own_routes_to_the_shell(self):
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

        # 基准是框架外壳（`create_bridge_app`）：各接口项目同级，
        # iwara 的入口只往外壳上挂自己的那 10 条路由，不挂 Pixiv 的任何路由。
        shell = paths(_create_bridge_app(make_settings()))
        full = paths(create_iwara_app(make_settings()))
        extra = full - shell
        self.assertTrue(shell <= full, "iwara 应用必须包含框架外壳的全部路由")
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

    def test_shell_and_iwara_app_do_not_expose_pixiv_routes(self):
        # 各接口项目同级：框架外壳里没有任何站点路由；
        # iwara 入口只挂 `/api/iwara/*`，Pixiv 的路由一条都不在。
        pixiv_paths = ("/api/ranking", "/api/trending-tags", "/api/search/illust")
        shell = TestClient(_create_bridge_app(make_settings()))
        with shell:
            self.assertEqual(shell.get("/api/iwara/videos").status_code, 404)
            self.assertEqual(shell.get("/api/ranking").status_code, 404)
        client = TestClient(create_iwara_app(make_settings()))
        with client:
            for path in pixiv_paths:
                self.assertEqual(
                    client.get(path).status_code,
                    404,
                    f"iwara 入口不该挂 Pixiv 的 {path}",
                )


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

    def test_entry_hands_the_site_to_the_framework_cli(self):
        text = self._source()
        # 入口本身不再拼参数：站点的检查/登录钩子都挂在 `iwara.app.SITE` 上，
        # 由框架基座 `pixiv_bridge.cli.main(site=...)` 统一取值。
        self.assertIn("from iwara.app import SITE", text)
        self.assertIn("main(site=SITE)", text)
        self.assertIn("from pixiv_bridge.cli import main", text)

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


@unittest.skipUnless(HAVE_IWARA, "需要 curl_cffi：%s" % IWARA_IMPORT_ERROR)
class IwaraWebExtensionTestCase(unittest.TestCase):
    """iwara_web 页面挂在 iwara 宿主上时能不能真的打开。

    `IwaraRouteTestCase.test_video_page_is_not_part_of_the_api_project` 只钉住
    「裸接口 app 上没有 /iwara」；这里补另一半：挂载扩展后页面确实是 HTML。
    """

    @staticmethod
    def _paths(app) -> set[str]:
        paths = set()
        for route in app.routes:
            inner = getattr(route, "original_router", None)
            for sub in getattr(inner, "routes", None) or (route,):
                paths.add(getattr(sub, "path", ""))
        return paths

    def _app(self, extensions=()):
        return create_iwara_app(make_settings(), extensions=extensions)

    def test_the_mounted_page_is_served_as_html(self):
        app = self._app(load_extension_specs(["iwara_web"], PROJECT_ROOT))
        with TestClient(app) as client:
            page = client.get("/iwara")

        self.assertEqual(page.status_code, 200)
        self.assertIn("text/html", page.headers["content-type"])
        self.assertTrue(page.text.lstrip().lower().startswith("<!doctype html"))
        self.assertIn("<title>iwara 视频</title>", page.text)
        self.assertIn('"/api/iwara"', page.text)

    def test_the_page_adds_exactly_one_route(self):
        pure = self._paths(self._app())
        full = self._paths(self._app(load_extension_specs(["iwara_web"], PROJECT_ROOT)))

        self.assertEqual(full - pure, {"/iwara"})
        self.assertNotIn("/novel", full)

    def test_the_page_shows_up_in_the_navigation(self):
        app = self._app(load_extension_specs(["iwara_web"], PROJECT_ROOT))
        with TestClient(app) as client:
            home = client.get("/")

        self.assertIn("/iwara", home.text)
        self.assertIn("iwara 视频（本入口独有）", home.text)

    def test_the_api_keeps_working_with_the_page_mounted(self):
        fake = _FakeIwaraClient()
        app = self._app(load_extension_specs(["iwara_web"], PROJECT_ROOT))
        with TestClient(app) as client:
            client.app.state.iwara_client = fake
            videos = client.get("/api/iwara/videos")

        self.assertEqual(videos.status_code, 200)
        self.assertEqual(videos.json()["count"], 1)


@unittest.skipUnless(HAVE_IWARA, "需要 curl_cffi：%s" % IWARA_IMPORT_ERROR)
class IwaraClientCoverageTestCase(unittest.TestCase):
    """补上 `get_json` / `login` / `probe_range`：三个出入边界都没钉住过。"""

    def _client(self, **kwargs):
        return IwaraClient(**kwargs)

    def _patched(self, responses, **kwargs):
        """把 `request` 换成按队列吐响应，返回 (client, seen)。"""
        client = self._client(**kwargs)
        queue = list(responses)
        seen = []

        async def fake_request(method, path, **kw):
            seen.append({"method": method, "path": path, **kw})
            return queue.pop(0)

        client.request = fake_request  # type: ignore[assignment]
        return client, seen

    def test_get_json_returns_the_parsed_body(self):
        client, seen = self._patched([_FakeResp(200, {"ok": True, "n": 1})])

        self.assertEqual(asyncio.run(client.get_json("/anything")), {"ok": True, "n": 1})
        self.assertEqual(seen[0]["method"], "GET")

    def test_get_json_raises_with_the_status_and_a_body_snippet(self):
        client, _ = self._patched([_FakeResp(418, "I am a teapot")])

        with self.assertRaises(IwaraError) as caught:
            asyncio.run(client.get_json("/teapot"))

        self.assertEqual(caught.exception.status, 418)
        self.assertIn("418", str(caught.exception))
        self.assertIn("I am a teapot", str(caught.exception))

    def test_login_returns_both_tokens_and_refreshes(self):
        client = self._client()
        calls = []

        async def fake_request(method, path, **kw):
            calls.append((method, path, kw.get("json_body")))
            if path == "/user/login":
                return _FakeResp(200, {"token": "refresh-from-login"})
            return _FakeResp(200, {"accessToken": "access-fresh"})

        async def fake_refresh():
            client.access_token = "access-fresh"
            return "access-fresh"

        client.request = fake_request  # type: ignore[assignment]
        client.refresh_access = fake_refresh  # type: ignore[assignment]

        result = asyncio.run(client.login("me@example.com", "secret"))

        self.assertEqual(
            result, {"refresh_token": "refresh-from-login", "access_token": "access-fresh"}
        )
        self.assertEqual(calls[0][0], "POST")
        self.assertEqual(calls[0][1], "/user/login")
        self.assertEqual(calls[0][2], {"email": "me@example.com", "password": "secret"})
        self.assertEqual(client.refresh_token, "refresh-from-login")

    def test_login_reports_rate_limiting_with_429(self):
        client, _ = self._patched([_FakeResp(429, {"message": "tooManyRequests"})])

        with self.assertRaises(IwaraError) as caught:
            asyncio.run(client.login("me@example.com", "secret"))

        self.assertEqual(caught.exception.status, 429)
        self.assertEqual(caught.exception.reason, "too_many_requests")
        self.assertNotIsInstance(caught.exception, IwaraAuthError)

    def test_login_rejects_a_bad_password(self):
        client, _ = self._patched([_FakeResp(400, {"message": "errors.invalidLogin"})])

        with self.assertRaises(IwaraAuthError) as caught:
            asyncio.run(client.login("me@example.com", "wrong"))

        self.assertIn("invalidLogin", str(caught.exception))

    def test_login_rejects_a_response_without_a_token(self):
        client, _ = self._patched([_FakeResp(200, {"user": {}})])

        with self.assertRaises(IwaraAuthError) as caught:
            asyncio.run(client.login("me@example.com", "secret"))

        self.assertIn("登录未返回 token", str(caught.exception))

    def _probe(self, body: bytes, headers=None, status=206):
        """把重试层换成「直接跑一次」，返回 (probe, seen)。"""
        client = self._client()
        seen: dict = {}

        class _Resp:
            status_code = status
            content = body

        _Resp.headers = headers or {}

        class _Session:
            async def get(self, url, **kw):
                seen["url"] = url
                seen["headers"] = kw.get("headers")
                return _Resp()

        async def fake_call(host, call, *, retry_statuses=()):
            seen["host"] = host
            return await call(_Session())

        client._call_with_ip_retry = fake_call  # type: ignore[assignment]
        return client, seen

    def test_probe_range_reports_the_headers_and_mp4_magic(self):
        client, seen = self._probe(
            MP4_HEAD, {"content-type": "video/mp4", "content-range": "bytes 0-1023/9999"}
        )

        probe = asyncio.run(client.probe_range(VIDEO_URL))

        self.assertEqual(probe["status"], 206)
        self.assertTrue(probe["is_mp4"])
        self.assertEqual(probe["bytes"], len(MP4_HEAD))
        self.assertEqual(probe["content_type"], "video/mp4")
        self.assertEqual(probe["content_range"], "bytes 0-1023/9999")
        self.assertEqual(seen["host"], "firefly.iwara.tv")
        self.assertEqual(seen["headers"]["range"], "bytes=0-1023")

    def test_probe_range_marks_a_non_mp4_body(self):
        client, seen = self._probe(b"<html>nope</html>", {"content-type": "text/html"}, status=200)

        probe = asyncio.run(client.probe_range("https://files.iwara.tv/x.mp4"))

        self.assertFalse(probe["is_mp4"])
        self.assertIsNone(probe["content_range"])
        self.assertEqual(probe["bytes"], len(b"<html>nope</html>"))


@unittest.skipUnless(HAVE_IWARA, "需要 curl_cffi：%s" % IWARA_IMPORT_ERROR)
class IwaraChallengeTestCase(unittest.TestCase):
    """Cloudflare 质询识别：cf-mitigated 头 与 403 + text/html 两条路径。"""

    def _raise(self, resp, **client_kwargs):
        """只替换重试层（挑战识别在 request 里，位于重试层之上）。"""
        client = IwaraClient(**client_kwargs)

        class _Session:
            async def request(self, method, url, **kw):
                return resp

        async def fake_call(host, call, *, retry_statuses=()):
            # 真实实现在这里才 session_for(host)；替换后仍要塞个 session 进去。
            return await call(_Session())

        client._call_with_ip_retry = fake_call  # type: ignore[assignment]
        return asyncio.run(client.request("GET", "/videos"))

    def test_cf_mitigated_header_is_a_challenge(self):
        resp = _FakeResp(200, {"results": []})
        resp.headers["cf-mitigated"] = "challenge"

        with self.assertRaises(IwaraChallengeError) as caught:
            self._raise(resp)

        self.assertEqual(caught.exception.reason, "cloudflare_challenge")
        self.assertEqual(caught.exception.status, 502)
        self.assertIsInstance(caught.exception, IwaraError)
        self.assertIn("impersonate=", str(caught.exception))

    def test_html_403_is_a_challenge(self):
        resp = _FakeResp(403, "<html>challenge</html>")
        resp.headers["content-type"] = "text/html; charset=utf-8"

        with self.assertRaises(IwaraChallengeError) as caught:
            self._raise(resp)

        self.assertEqual(caught.exception.reason, "cloudflare_challenge")

    def test_json_403_is_not_a_challenge(self):
        resp = _FakeResp(403, {"message": "errors.forbidden"})
        resp.headers["content-type"] = "application/json"

        result = self._raise(resp)

        self.assertEqual(result.status_code, 403)

    def test_challenge_is_raised_before_the_token_replay(self):
        """挑战必须先于「403 = token 过期」的强制重放被识别出来。"""
        resp = _FakeResp(403, "<html>challenge</html>")
        resp.headers["content-type"] = "text/html"

        with self.assertRaises(IwaraChallengeError):
            # 带 refresh token 时 403 本来会触发重放；挑战要抢在前面。
            self._raise(resp, refresh_token="refresh-good")


@unittest.skipUnless(HAVE_IWARA, "需要 curl_cffi：%s" % IWARA_IMPORT_ERROR)
class IwaraTokenPathTestCase(unittest.TestCase):
    """tokens.default_path / _chmod_private：文件落点与权限。"""

    def setUp(self):
        self.tmp = make_temp_dir("iwara_token_path")
        self.addCleanup(drop_temp_dir, self.tmp)

    def test_default_path_uses_the_data_dir(self):
        self.assertEqual(tokens.default_path(self.tmp), self.tmp / "iwara_tokens.json")

    def test_default_path_falls_back_to_the_project_data_dir(self):
        expected = Path(tokens.__file__).resolve().parent.parent / "data" / "iwara_tokens.json"

        self.assertEqual(tokens.default_path(None), expected)

    def test_saved_token_file_is_private(self):
        if os.name != "posix":
            self.skipTest("Windows 没有 0600 这回事（chmod 只动只读位），只在 POSIX 上断言权限位")

        path = tokens.save_token("secret", self.tmp / "t.json")

        mode = os.stat(path).st_mode & 0o777
        self.assertEqual(mode, 0o600)

    def test_chmod_private_swallows_os_errors(self):
        tokens._chmod_private(self.tmp / "missing.json")  # 不该抛


@unittest.skipUnless(HAVE_IWARA, "需要 curl_cffi：%s" % IWARA_IMPORT_ERROR)
class IwaraResolveSaltTestCase(unittest.TestCase):
    """resolve 的双盐回退：新盐 404 时老盐要顶上。"""

    def _client(self, responses):
        client = IwaraClient()
        seen = []
        queue = list(responses)

        async def fake_video(video_id):
            return dict(VIDEO)

        async def fake_request(method, path, **kw):
            seen.append(kw.get("headers", {}).get("x-version"))
            return queue.pop(0)

        client.request = fake_request  # type: ignore[assignment]
        client.video = fake_video  # type: ignore[assignment]
        return client, seen

    def test_the_legacy_salt_is_tried_after_the_current_one(self):
        from iwara.client import SALT_CURRENT, SALT_LEGACY

        resp = _FakeResp(200, RAW_SOURCES)
        client, seen = self._client([_FakeResp(404, "nope"), resp])

        resolved = asyncio.run(client.resolve("b0pRCNfo8Igu3p"))

        self.assertEqual(resolved.best().name, "Source")
        self.assertEqual(len(seen), 2)
        self.assertEqual(seen[0], x_version(VIDEO["fileUrl"], SALT_CURRENT))
        self.assertEqual(seen[1], x_version(VIDEO["fileUrl"], SALT_LEGACY))
        self.assertEqual(resolved.salt_used, SALT_LEGACY)

    def test_every_failed_salt_is_reported(self):
        client, _ = self._client([_FakeResp(404, "a"), _FakeResp(500, "b")])

        with self.assertRaises(IwaraError) as caught:
            asyncio.run(client.resolve("b0pRCNfo8Igu3p"))

        self.assertEqual(caught.exception.status, 502)
        self.assertIn("404", str(caught.exception))
        self.assertIn("500", str(caught.exception))

    def test_a_non_json_body_falls_through_to_the_next_salt(self):
        """X-Version 不对时 CDN 回空体：这时要报「非 JSON」并换盐，而不是炸掉。"""

        class _HtmlResp(_FakeResp):
            content = b"<html>denied</html>"

            def json(self):
                raise ValueError("not json")

        client, seen = self._client([_HtmlResp(200), _FakeResp(200, RAW_SOURCES)])

        resolved = asyncio.run(client.resolve("b0pRCNfo8Igu3p"))

        self.assertEqual(resolved.best().name, "Source")
        self.assertEqual(len(seen), 2)


@unittest.skipUnless(HAVE_IWARA, "需要 curl_cffi：%s" % IWARA_IMPORT_ERROR)
class IwaraFetchImageTestCase(unittest.TestCase):
    """fetch_image 的跨主机回退。"""

    def _client(self, outcomes):
        client = IwaraClient()
        tried = []

        async def fake_once(url):
            tried.append(urlparse(url).netloc)
            result = outcomes.get(urlparse(url).netloc)
            if result is None:
                raise IwaraError("down", status=502)
            return result

        client._fetch_image_once = fake_once  # type: ignore[assignment]
        return client, tried

    def test_the_first_host_wins(self):
        client, tried = self._client({"files.iwara.tv": (b"jpeg", "image/jpeg")})

        body, ctype = asyncio.run(client.fetch_image("https://files.iwara.tv/image/a.jpg"))

        self.assertEqual(body, b"jpeg")
        self.assertEqual(tried, ["files.iwara.tv"])

    def test_other_image_hosts_are_tried_in_order(self):
        client, tried = self._client({"i.iwara.tv": (b"jpeg", "image/jpeg")})

        body, ctype = asyncio.run(client.fetch_image("https://files.iwara.tv/image/a.jpg"))

        self.assertEqual(tried, ["files.iwara.tv", "filesq.iwara.tv", "i.iwara.tv"])
        self.assertEqual(ctype, "image/jpeg")

    def test_every_host_failing_is_reported(self):
        client, tried = self._client({})

        with self.assertRaises(IwaraError) as caught:
            asyncio.run(client.fetch_image("https://files.iwara.tv/image/a.jpg"))

        self.assertIn("已尝试", str(caught.exception))
        self.assertIn("i.iwara.tv", str(caught.exception))


@unittest.skipUnless(HAVE_IWARA, "需要 curl_cffi：%s" % IWARA_IMPORT_ERROR)
class IwaraDohClientTestCase(unittest.TestCase):
    """doh_client 懒建 + aclose 关掉它。"""

    def test_the_client_is_built_lazily_and_closed(self):
        client = IwaraClient()

        self.assertIsNone(client._doh_client)

        doh = client.doh_client
        self.assertIsNotNone(doh)
        self.assertIs(client.doh_client, doh)

        asyncio.run(client.aclose())
        self.assertIsNone(client._doh_client)

    def test_aclose_swallows_a_broken_session(self):
        class _Broken:
            async def aclose(self):
                raise RuntimeError("boom")

        client = IwaraClient()
        client._sessions["api.iwara.tv"] = _Broken()

        asyncio.run(client.aclose())
        self.assertEqual(client._sessions, {})


@unittest.skipUnless(HAVE_IWARA, "需要 curl_cffi：%s" % IWARA_IMPORT_ERROR)
class IwaraRouteGapTestCase(unittest.TestCase):
    """路由层没钉住的分支：上游 >=400、related 越界、image/thumbnail 的异常映射。"""

    def setUp(self):
        self.fake = _FakeIwaraClient()

    def _client(self):
        app = create_iwara_app(make_settings())
        client = TestClient(app)
        client.__enter__()
        self.addCleanup(client.__exit__, None, None, None)
        client.app.state.iwara_client = self.fake
        return client

    def test_stream_surfaces_an_upstream_failure(self):
        """上游给了个 >=400 的状态码（不是异常）：要原样报出来，别装成 200。

        `VIDEO_URL` 的 `firefly.iwara.tv` 在白名单内，所以能走到上游状态检查。
        """

        async def failing(url, headers=None):
            async def body():  # pragma: no cover - 不会轮到它
                yield b""

            return 502, {"content-type": "text/plain"}, body()

        self.fake.open_stream = failing  # type: ignore[assignment]

        client = self._client()
        resp = client.get("/api/iwara/stream", params={"url": VIDEO_URL})

        self.assertEqual(resp.status_code, 502)
        self.assertIn("上游视频源不可用", resp.json()["detail"])

    def test_related_trims_locally_and_can_run_off_the_end(self):
        """limit 在路由层本地裁剪，翻到末尾要给空页而不是 500。"""
        client = self._client()
        full = client.get("/api/iwara/related/b0pRCNfo8Igu3p").json()
        trimmed = client.get("/api/iwara/related/b0pRCNfo8Igu3p", params={"limit": 1}).json()
        beyond = client.get("/api/iwara/related/b0pRCNfo8Igu3p", params={"page": 50}).json()

        self.assertEqual(len(full["results"]), 2)
        self.assertEqual(len(trimmed["results"]), 1)
        self.assertEqual(beyond["results"], [])
        self.assertEqual(beyond["count"], full["count"])
        # 上游只被调用了一次，page/limit 都没往上传。
        self.assertEqual(self.fake.calls[0], ("related", "b0pRCNfo8Igu3p"))

    def test_image_maps_a_client_error_to_that_status(self):
        async def failing(url):
            raise IwaraError("图源挂了", status=502)

        self.fake.fetch_image = failing  # type: ignore[assignment]

        client = self._client()
        resp = client.get("/api/iwara/image", params={"url": "https://files.iwara.tv/a.jpg"})

        self.assertEqual(resp.status_code, 502)
        self.assertEqual(resp.json()["detail"], "图源挂了")

    def test_thumbnail_maps_a_client_error_to_that_status(self):
        async def failing(url):
            raise IwaraError("图源挂了", status=502)

        self.fake.fetch_image = failing  # type: ignore[assignment]

        client = self._client()
        resp = client.get("/api/iwara/thumbnail/b0pRCNfo8Igu3p")

        self.assertEqual(resp.status_code, 502)
        self.assertEqual(resp.json()["detail"], "图源挂了")

    def test_auth_survives_a_failing_username_lookup(self):
        """已认证但 /user 挂了：诊断照出，只是没有用户名。"""
        self.fake.refresh_token = "refresh-good"
        self.fake.access_token = _make_jwt(int(time.time()) + 3600)

        async def failing_me():
            raise IwaraError("上游挂了", status=502)

        self.fake.me = failing_me  # type: ignore[assignment]

        client = self._client()
        resp = client.get("/api/iwara/auth")

        payload = resp.json()
        self.assertEqual(resp.status_code, 200)
        self.assertTrue(payload["configured"])
        self.assertTrue(payload["authenticated"])
        self.assertIsNone(payload["username_hint"])
        self.assertIsNone(payload["error"])


@unittest.skipUnless(HAVE_IWARA, "需要 curl_cffi：%s" % IWARA_IMPORT_ERROR)
class IwaraGetTokenCliTestCase(unittest.TestCase):
    """`python iwara/get_token.py` 这个登录脚本：唯一的入口，此前零覆盖。

    它自己有 argparse / input / getpass，所以全部用 patch 顶掉，只验证
    「打印了什么、返回码是多少」—— 也就是用户在终端真正看到的东西。
    """

    def setUp(self):
        import importlib.util

        spec = importlib.util.spec_from_file_location(
            "iwara_get_token_under_test", PROJECT_ROOT / "iwara" / "get_token.py"
        )
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        self.module = module
        self.directory = make_temp_dir("iwara_get_token")
        self.addCleanup(drop_temp_dir, self.directory)
        self.token_file = self.directory / "tokens.json"
        # 每次调用都可能按默认路径（项目 data/）落盘，这里一律重定向到临时目录。
        patcher = patch.object(
            self.module.tokens, "default_path", lambda *_a, **_k: self.token_file
        )
        patcher.start()
        self.addCleanup(patcher.stop)

    def _run(self, argv, *, email=None, password=None, client=None, extra=()):
        """跑 main()，抓住它打印到 stdout/stderr 的一切。

        `_login` / `_status` 内部才 `from iwara.client import IwaraClient`，
        所以顶 `iwara.client.IwaraClient` 就能让它们离线跑完。
        """
        out, err = io.StringIO(), io.StringIO()
        patched = [
            patch.object(sys, "argv", ["get_token.py", *argv]),
            patch("builtins.input", return_value=email or ""),
            patch("getpass.getpass", return_value=password or ""),
        ]
        if client is not None:
            patched.append(patch("iwara.client.IwaraClient", client))
        patched.extend(extra)
        with contextlib.ExitStack() as stack:
            for item in patched:
                stack.enter_context(item)
            stack.enter_context(contextlib.redirect_stdout(out))
            stack.enter_context(contextlib.redirect_stderr(err))
            code = self.module.main()  # 依赖已装好，ensure_packages 会直接放行

        return code, out.getvalue(), err.getvalue()

    @staticmethod
    def _login_client(*, raise_error=None, refresh="refresh-abc", access=None):
        """造一个顶替 IwaraClient 的类：记录 login 参数，按设定给 token。"""
        seen = {}

        class _Client:
            def __init__(self, *_a, **_kw):
                self.refresh_token = None
                self.access_token = None

            async def login(self, email, password):
                seen["login"] = (email, password)
                if raise_error is not None:
                    raise raise_error
                self.refresh_token = refresh
                self.access_token = access

            async def aclose(self):
                seen["closed"] = True

        return _Client, seen

    @staticmethod
    def _status_client(*, access=None, ensure_error=None, me_error=None, user=None):
        seen = {}

        class _Client:
            def __init__(self, *_a, **kw):
                seen["refresh"] = kw.get("refresh_token")
                self.access_token = None

            async def ensure_access_token(self):
                if ensure_error is not None:
                    raise ensure_error
                self.access_token = access
                return access

            async def me(self):
                if me_error is not None:
                    raise me_error
                return user if user is not None else {"user": {"username": "huanghe19512"}}

            async def aclose(self):
                seen["closed"] = True

        return _Client, seen

    def test_ask_returns_the_typed_value(self):
        with patch("builtins.input", return_value="typed@example.com"):
            self.assertEqual(self.module._ask("邮箱: "), "typed@example.com")
        # 直接回车就用默认值。
        with patch("builtins.input", return_value="  "):
            self.assertEqual(self.module._ask("邮箱: ", default="x@example.com"), "x@example.com")

    def test_ask_treats_a_cancelled_prompt_as_empty(self):
        with patch("builtins.input", side_effect=EOFError):
            self.assertEqual(self.module._ask("邮箱: "), "")
        with patch("builtins.input", side_effect=KeyboardInterrupt):
            self.assertEqual(self.module._ask("邮箱: "), "")

    def test_login_saves_the_token_and_says_where(self):
        cls, seen = self._login_client(access=_make_jwt(int(time.time()) + 3600))

        code, out, _err = self._run([], email="me@example.com", password="secret", client=cls)

        self.assertEqual(code, 0)
        self.assertEqual(seen["login"], ("me@example.com", "secret"))
        self.assertTrue(seen["closed"])
        self.assertIn("登录成功", out)
        self.assertIn(str(self.token_file), out)
        self.assertIn("python iwara/run.py", out)
        self.assertIn("分钟", out)  # access token 剩余有效期
        self.assertEqual(tokens.load_token(self.token_file), "refresh-abc")

    def test_login_without_an_access_token_still_saves(self):
        cls, _seen = self._login_client(access=None)

        code, out, _err = self._run([], email="me@example.com", password="secret", client=cls)

        self.assertEqual(code, 0)
        self.assertNotIn("分钟", out)
        self.assertEqual(tokens.load_token(self.token_file), "refresh-abc")

    def test_login_without_a_refresh_token_is_a_failure(self):
        cls, _seen = self._login_client(refresh="")

        code, _out, err = self._run([], email="me@example.com", password="secret", client=cls)

        self.assertEqual(code, 1)
        self.assertIn("上游没有返回 token", err)

    def test_login_reports_a_bad_password(self):
        cls, _seen = self._login_client(
            raise_error=IwaraAuthError("登录失败（400）: errors.invalidLogin")
        )

        code, _out, err = self._run([], email="me@example.com", password="wrong", client=cls)

        self.assertEqual(code, 1)
        self.assertIn("invalidLogin", err)
        self.assertIn("邮箱或密码不对", err)

    def test_login_explains_a_cloudflare_block(self):
        cls, _seen = self._login_client(
            raise_error=IwaraError("被拦了", status=502, reason="cloudflare_challenge")
        )

        code, _out, err = self._run([], email="me@example.com", password="secret", client=cls)

        self.assertEqual(code, 1)
        self.assertIn("被拦了", err)
        self.assertIn("换个网络", err)

    def test_an_empty_email_cancels(self):
        code, out, _err = self._run([], email="", password="secret")

        self.assertEqual(code, 1)
        self.assertIn("邮箱为空", out)

    def test_an_empty_password_cancels(self):
        code, out, _err = self._run([], email="me@example.com", password="")

        self.assertEqual(code, 1)
        self.assertIn("密码为空", out)

    def test_status_says_anonymous_without_a_token(self):
        code, out, _err = self._run(["--status"])

        self.assertEqual(code, 0)
        self.assertIn("未登录", out)
        self.assertIn("匿名模式可用", out)

    def test_status_prints_the_account_and_the_remaining_minutes(self):
        tokens.save_token("refresh-abc", self.token_file)
        cls, seen = self._status_client(access=_make_jwt(int(time.time()) + 3600))

        code, out, _err = self._run(["--status"], client=cls)

        self.assertEqual(code, 0)
        self.assertEqual(seen["refresh"], "refresh-abc")
        self.assertTrue(seen["closed"])
        self.assertIn("已找到 token 文件", out)
        self.assertIn("有效", out)
        self.assertIn("huanghe19512", out)

    def test_status_reports_an_expired_token_without_the_account(self):
        tokens.save_token("refresh-abc", self.token_file)
        cls, _seen = self._status_client(access=_make_jwt(int(time.time()) - 60))

        code, out, _err = self._run(["--status"], client=cls)

        self.assertEqual(code, 0)
        self.assertIn("已过期", out)

    def test_status_survives_a_failing_username_lookup(self):
        tokens.save_token("refresh-abc", self.token_file)
        cls, _seen = self._status_client(
            access=_make_jwt(int(time.time()) + 3600), me_error=IwaraError("上游挂了")
        )

        code, out, _err = self._run(["--status"], client=cls)

        self.assertEqual(code, 0)  # 用户名取不到只是少一行，不算失败
        self.assertIn("取账号信息失败", out)
        self.assertNotIn("当前账号", out)

    def test_status_reports_a_dead_token_with_the_relogin_hint(self):
        tokens.save_token("stale", self.token_file)
        cls, _seen = self._status_client(ensure_error=IwaraError("刷新 token 失败（403）"))

        code, _out, err = self._run(["--status"], client=cls)

        self.assertEqual(code, 1)
        self.assertIn("token 不可用", err)
        self.assertIn("python iwara/get_token.py", err)

    def test_logout_removes_the_token_file(self):
        tokens.save_token("refresh-abc", self.token_file)

        code, out, _err = self._run(["--logout"])

        self.assertEqual(code, 0)
        self.assertIn("已删除 token 文件", out)
        self.assertFalse(self.token_file.exists())
        self.assertEqual(tokens.load_token(self.token_file), "")

    def test_logout_without_a_file_is_still_a_success(self):
        code, out, _err = self._run(["--logout"])

        self.assertEqual(code, 0)
        self.assertIn("本来就没有 token 文件", out)

    def test_a_missing_dependency_is_reported_not_raised(self):
        extra = [patch.object(self.module, "ensure_packages", side_effect=RuntimeError("装不上"))]
        code, _out, err = self._run(["--status"], extra=extra)

        self.assertEqual(code, 1)
        self.assertIn("[deps]", err)
        self.assertIn("装不上", err)

    def test_ctrl_c_is_reported_as_a_cancellation(self):
        extra = [patch("builtins.input", side_effect=KeyboardInterrupt)]
        code, out, _err = self._run([], extra=extra)

        self.assertEqual(code, 1)
        self.assertIn("已取消", out)


@unittest.skipUnless(HAVE_IWARA, "需要 curl_cffi：%s" % IWARA_IMPORT_ERROR)
class IwaraEchConfigTestCase(unittest.TestCase):
    """``iwara/ech.py``：ECH 配置的解析、缓存与取值。

    它是 iwara 版的自足实现（站点之间不能互相 import，见
    ``scripts/check_boundary.py``），走的路线也和 pixiv 那套不同：pixiv 用
    ctypes 调 OpenSSL 4.0+ 自己握手，这里只把配置交给 libcurl 的
    ``CURLOPT_ECH``，所以不需要任何动态库 —— 这里的用例全是纯逻辑。
    """

    def setUp(self):
        # 缓存是模块级的，谁先跑都会污染别人；每个用例都从干净状态开始。
        self.addCleanup(iwara_ech.reset_config_cache)

    def test_the_bundled_config_is_a_usable_echconfiglist(self):
        raw = iwara_ech._decode_config(iwara_ech.DEFAULT_ECH_CONFIG)

        self.assertGreaterEqual(len(raw), 4)
        # ECHConfigList 的前两个字节是大端长度，必须覆盖整段，否则 libcurl
        # 会当成畸形配置丢掉。
        self.assertEqual(int.from_bytes(raw[:2], "big"), len(raw) - 2)
        # 内置配置里的 public_name 就是外层 SNI 用的那个名字。
        self.assertIn(b"cloudflare-ech.com", raw)

    def test_a_too_short_config_is_rejected(self):
        with self.assertRaises(iwara_ech.EchUnavailable) as ctx:
            iwara_ech._decode_config(base64.b64encode(b"\x01\x02").decode("ascii"))

        self.assertIn("过短", str(ctx.exception))

    def test_garbage_is_rejected_with_a_readable_reason(self):
        with self.assertRaises(iwara_ech.EchUnavailable) as ctx:
            iwara_ech._decode_config("not base64 at all!!")

        self.assertIn("base64", str(ctx.exception))

    def test_pem_wrapping_and_whitespace_are_tolerated(self):
        wrapped = "-----BEGIN ECH-----\n" + iwara_ech.DEFAULT_ECH_CONFIG + "\n-----END ECH-----"

        self.assertEqual(
            iwara_ech._decode_config(wrapped),
            iwara_ech._decode_config(iwara_ech.DEFAULT_ECH_CONFIG),
        )

    def test_doh_payload_is_only_read_from_type_65(self):
        good = base64.b64encode(b"\x00\x04abcd").decode("ascii")
        payload = {
            "Answer": [
                # A 记录里就算有 ech= 也不能信（那不是 HTTPS 记录）。
                {"type": 1, "data": f"1.2.3.4 ech={good}"},
                {"type": 65, "data": f'alpn="h2" ech="{good}"'},
            ]
        }

        self.assertEqual(
            iwara_ech._ech_from_doh(payload), iwara_ech._decode_config(good)
        )

    def test_the_doh_regex_handles_the_escaped_quote(self):
        # DoH 的 JSON 里引号被转义成 \"：正则不许它，就永远退回内置配置。
        good = base64.b64encode(b"\x00\x04abcd").decode("ascii")
        for data in (f'ech=\\"{good}\\"', f'ech="{good}"', f"ech={good}"):
            self.assertRegex(data, iwara_ech._ECH_VALUE_RE, data)

    def test_a_payload_without_any_ech_record_yields_nothing(self):
        self.assertIsNone(iwara_ech._ech_from_doh({"Answer": [{"type": 65, "data": 'alpn="h2"'}]}))
        self.assertIsNone(iwara_ech._ech_from_doh({}))
        self.assertIsNone(iwara_ech._ech_from_doh(None))

    def test_a_broken_record_falls_through_to_the_next_answer(self):
        good = base64.b64encode(b"\x00\x04abcd").decode("ascii")
        payload = {
            "Answer": [
                {"type": 65, "data": 'ech="!!!!"'},  # 非法 base64，跳过
                {"type": 65, "data": f'ech="{good}"'},
            ]
        }

        self.assertEqual(
            iwara_ech._ech_from_doh(payload), iwara_ech._decode_config(good)
        )

    def test_fetch_tries_every_endpoint_before_giving_up(self):
        seen = []

        def boom(url, **_kwargs):
            seen.append(url)
            raise httpx.ConnectError("no route")

        with patch.object(iwara_ech.httpx, "get", boom):
            with self.assertRaises(iwara_ech.EchUnavailable) as ctx:
                iwara_ech.fetch_ech_config(endpoints=("https://a/resolve", "https://b/resolve"))

        self.assertEqual(seen, ["https://a/resolve", "https://b/resolve"])
        self.assertIn("DoH 查询失败", str(ctx.exception))
        self.assertIn("ConnectError", str(ctx.exception))

    def test_fetch_asks_for_https_records_in_dns_json(self):
        calls = []

        def fake_get(url, **kwargs):
            calls.append((url, kwargs))
            return MagicMock(
                json=lambda: {"Answer": [{"type": 65, "data": 'ech="%s"' % GOOD_ECH_B64}]},
                raise_for_status=lambda: None,
            )

        with patch.object(iwara_ech.httpx, "get", fake_get):
            found = iwara_ech.fetch_ech_config(endpoints=("https://a/resolve",))

        self.assertEqual(found, iwara_ech._decode_config(GOOD_ECH_B64))
        url, kwargs = calls[0]
        self.assertEqual(url, "https://a/resolve")
        self.assertEqual(kwargs["params"], {"name": "cloudflare-ech.com", "type": "65"})
        self.assertEqual(kwargs["headers"], {"accept": "application/dns-json"})

    def test_fetch_falls_back_to_the_bundled_endpoints(self):
        # 没给 endpoints 就用模块里那两个（实测只有它们能查 type 65）。
        seen = []

        def fake_get(url, **_kwargs):
            seen.append(url)
            return MagicMock(
                json=lambda: {"Answer": [{"type": 65, "data": 'ech="%s"' % GOOD_ECH_B64}]},
                raise_for_status=lambda: None,
            )

        with patch.object(iwara_ech.httpx, "get", fake_get):
            iwara_ech.fetch_ech_config()

        self.assertEqual(seen, [iwara_ech.DOH_ENDPOINTS[0]])
        self.assertIn("alidns", iwara_ech.DOH_ENDPOINTS[0])
        self.assertIn("223.5.5.5", iwara_ech.DOH_ENDPOINTS[1])

    def test_fetch_stops_at_the_first_endpoint_that_answers(self):
        answers = iter(
            [
                httpx.ConnectError("no route"),
                MagicMock(
                    json=lambda: {
                        "Answer": [{"type": 65, "data": 'ech="%s"' % GOOD_ECH_B64}]
                    },
                    raise_for_status=lambda: None,
                ),
            ]
        )

        def fake_get(url, **_kwargs):
            value = next(answers)
            if isinstance(value, Exception):
                raise value
            return value

        with patch.object(iwara_ech.httpx, "get", fake_get):
            found = iwara_ech.fetch_ech_config(endpoints=("https://a/resolve", "https://b/resolve"))

        self.assertEqual(found, iwara_ech._decode_config(GOOD_ECH_B64))

    def test_an_explicit_override_beats_everything(self):
        self.assertEqual(
            iwara_ech.ech_config(override=GOOD_ECH_B64),
            iwara_ech._decode_config(GOOD_ECH_B64),
        )

    def test_a_bad_override_is_reported_not_silently_ignored(self):
        with self.assertRaises(iwara_ech.EchUnavailable):
            iwara_ech.ech_config(override="!!!")

    def test_the_config_is_cached_until_it_is_reset(self):
        calls = []

        def fake_get(url, **_kwargs):
            calls.append(url)
            return MagicMock(
                json=lambda: {"Answer": [{"type": 65, "data": 'ech="%s"' % GOOD_ECH_B64}]},
                raise_for_status=lambda: None,
            )

        with patch.object(iwara_ech.httpx, "get", fake_get):
            first = iwara_ech.ech_config()
            second = iwara_ech.ech_config()
            iwara_ech.reset_config_cache()
            third = iwara_ech.ech_config()

        self.assertEqual(first, second)
        self.assertEqual(second, third)
        self.assertEqual(len(calls), 2)  # 两次取用之间只打了一次 DoH

    def test_refresh_forces_a_new_lookup(self):
        calls = []

        def fake_get(url, **_kwargs):
            calls.append(url)
            return MagicMock(
                json=lambda: {"Answer": [{"type": 65, "data": 'ech="%s"' % GOOD_ECH_B64}]},
                raise_for_status=lambda: None,
            )

        with patch.object(iwara_ech.httpx, "get", fake_get):
            iwara_ech.ech_config()
            iwara_ech.ech_config(refresh=True)

        self.assertEqual(len(calls), 2)

    def test_the_bundled_config_is_the_last_resort(self):
        with patch.object(iwara_ech.httpx, "get", side_effect=httpx.ConnectError("offline")):
            config = iwara_ech.ech_config()

        self.assertEqual(config, iwara_ech._decode_config(iwara_ech.DEFAULT_ECH_CONFIG))

    def test_the_option_is_the_ecl_prefixed_base64(self):
        raw = iwara_ech._decode_config(GOOD_ECH_B64)

        self.assertEqual(iwara_ech.ech_option(raw=raw), "ecl:" + GOOD_ECH_B64)

    def test_the_option_is_none_when_libcurl_cannot_do_ech(self):
        with patch.object(iwara_ech, "available", lambda: False):
            self.assertIsNone(iwara_ech.ech_option(raw=b"\x00\x04abcd"))

    def test_an_unusable_config_yields_no_option(self):
        with patch.object(iwara_ech, "ech_config", side_effect=iwara_ech.EchUnavailable("没了")):
            self.assertIsNone(iwara_ech.ech_option())

    def test_available_reflects_whether_libcurl_has_the_option(self):
        # 本机 curl_cffi 0.15.0 有 CURLOPT_ECH；换一个没有的 CurlOpt 就该是假。
        self.assertTrue(iwara_ech.available())
        with patch.object(iwara_ech, "CurlOpt", object()):
            self.assertFalse(iwara_ech.available())


@unittest.skipUnless(HAVE_IWARA, "需要 curl_cffi：%s" % IWARA_IMPORT_ERROR)
class IwaraSettingsEchTestCase(unittest.TestCase):
    """``iwara/config.py``：``IwaraSettings`` 只比框架多 ECH 三项。

    环境变量用 ``IWARA_`` 前缀而不是 ``PIXIV_``：各站点是同级入口，混用
    会让人以为改一个开关两个站都变。
    """

    def setUp(self):
        self.directory = make_temp_dir("iwara_ech_settings")
        self.addCleanup(drop_temp_dir, self.directory)

    def _load(self, **env):
        merged = {
            "IWARA_BRIDGE_ECH": "",
            "IWARA_BRIDGE_ECH_CONFIG": "",
            "IWARA_BRIDGE_ECH_IPS": "",
        }
        merged.update(env)
        with patch.dict(os.environ, merged, clear=False):
            return IwaraSettings.load(migrate=False)

    def test_split_ips_accepts_commas_spaces_and_semicolons(self):
        self.assertEqual(split_ips(""), ())
        self.assertEqual(split_ips("1.2.3.4"), ("1.2.3.4",))
        self.assertEqual(
            split_ips("1.2.3.4, 5.6.7.8;9.9.9.9"),
            ("1.2.3.4", "5.6.7.8", "9.9.9.9"),
        )
        self.assertEqual(split_ips("  "), ())

    def test_ech_is_off_until_the_env_says_otherwise(self):
        for value in ("", "0", "false", "off", "no"):
            self.assertFalse(self._load(IWARA_BRIDGE_ECH=value).ech, value)

        for value in ("1", "true", "YES", "on"):
            self.assertTrue(self._load(IWARA_BRIDGE_ECH=value).ech, value)

    def test_the_two_other_keys_are_taken_verbatim(self):
        settings = self._load(
            IWARA_BRIDGE_ECH="1",
            IWARA_BRIDGE_ECH_CONFIG="cfg-base64",
            IWARA_BRIDGE_ECH_IPS="1.2.3.4,5.6.7.8",
        )

        self.assertTrue(settings.ech)
        self.assertEqual(settings.ech_config, "cfg-base64")
        self.assertEqual(settings.ech_ips, "1.2.3.4,5.6.7.8")

    def test_the_framework_fields_survive_the_round_trip(self):
        settings = self._load()

        # 框架负责的那部分（data_dir / 端口 / 限流）不能因为加了站点字段就丢。
        self.assertEqual(settings.rate_limit_rpm, Settings.load(migrate=False).rate_limit_rpm)
        self.assertEqual(settings.host, Settings.load(migrate=False).host)
        self.assertTrue(settings.data_dir)

    def test_as_iwara_settings_keeps_an_iwara_settings_as_is(self):
        settings = self._load(IWARA_BRIDGE_ECH="1")

        self.assertIs(as_iwara_settings(settings), settings)

    def test_as_iwara_settings_upgrades_a_plain_framework_settings(self):
        base = Settings(rate_limit_rpm=10000, data_dir=str(self.directory))

        upgraded = as_iwara_settings(base)

        self.assertIsInstance(upgraded, IwaraSettings)
        self.assertFalse(upgraded.ech)  # 没开就是默认关
        self.assertEqual(upgraded.rate_limit_rpm, 10000)
        self.assertEqual(upgraded.data_dir, str(self.directory))

    def test_as_iwara_settings_reloads_when_given_nothing(self):
        with patch.dict(os.environ, {"IWARA_BRIDGE_ECH": "1"}, clear=False):
            self.assertTrue(as_iwara_settings(None).ech)

    def test_the_site_declares_its_own_settings_class(self):
        # 框架 cli 走 site.load_settings()，用的就是这个 settings_class。
        self.assertIs(SITE.settings_class, IwaraSettings)
        self.assertIsInstance(SITE.load_settings(), IwaraSettings)


@unittest.skipUnless(HAVE_IWARA, "需要 curl_cffi：%s" % IWARA_IMPORT_ERROR)
class IwaraClientEchTestCase(unittest.TestCase):
    """``iwara/client.py`` 里的 ECH 接线：谁能用、怎么传给 libcurl。

    核心事实：ECH 只对**在 Cloudflare 后面**的主机生效（api / www / 裸域）。
    视频与图片的 CDN 主机是 Let's Encrypt 证书，给它们开 ECH 只会握手失败
    （``60: SSL certificate problem: Hostname mismatch``）。
    """

    def test_only_the_cloudflare_hosts_are_eligible(self):
        for host in ("api.iwara.tv", "www.iwara.tv", "iwara.tv"):
            self.assertTrue(is_ech_host(host), host)

        for host in ("files.iwara.tv", "filesq.iwara.tv", "i.iwara.tv", "firefly.iwara.tv"):
            self.assertFalse(is_ech_host(host), host)

    def test_the_whitelist_is_exactly_the_documented_three(self):
        self.assertEqual(ECH_HOSTS, ("api.iwara.tv", "www.iwara.tv", "iwara.tv"))

    def test_ech_is_off_by_default(self):
        client = IwaraClient()

        self.assertFalse(client.ech)
        self.assertIsNone(client.ech_option("www.iwara.tv"))

    def test_the_option_never_appears_for_a_cdn_host(self):
        client = IwaraClient(ech=True)

        self.assertIsNotNone(client.ech_option("www.iwara.tv"))
        self.assertIsNone(client.ech_option("files.iwara.tv"))

    def test_the_option_is_none_when_libcurl_cannot_do_ech(self):
        client = IwaraClient(ech=True)

        with patch.object(iwara_ech, "available", lambda: False):
            self.assertIsNone(client.ech_option("www.iwara.tv"))

    def test_the_explicit_config_is_handed_to_the_module(self):
        seen = {}

        def fake(override="", refresh=False):
            seen["override"] = override
            return "ecl:" + GOOD_ECH_B64

        client = IwaraClient(ech=True, ech_config="my-config")
        with patch.object(iwara_ech, "ech_option", fake):
            self.assertEqual(client.ech_option("www.iwara.tv"), "ecl:" + GOOD_ECH_B64)

        self.assertEqual(seen["override"], "my-config")

    def test_the_session_carries_the_option_for_an_ech_host(self):
        client = IwaraClient(ech=True)
        client._live_ips["www.iwara.tv"] = "104.20.28.232"

        with patch.object(iwara_ech, "ech_option", lambda **_kwargs: "ecl:" + GOOD_ECH_B64):
            session = asyncio.run(client.session_for("www.iwara.tv"))

        self.addCleanup(lambda: asyncio.run(session.close()))
        self.assertEqual(session.curl_options[CurlOpt.ECH], "ecl:" + GOOD_ECH_B64)
        # 钉 IP 与浏览器指纹那套不变：ECH 只是多了一个选项。
        self.assertEqual(
            session.curl_options[CurlOpt.RESOLVE], ["www.iwara.tv:443:104.20.28.232"]
        )

    def test_the_session_has_no_ech_option_for_a_cdn_host(self):
        client = IwaraClient(ech=True)
        client._live_ips["files.iwara.tv"] = "5.61.209.181"

        session = asyncio.run(client.session_for("files.iwara.tv"))

        self.addCleanup(lambda: asyncio.run(session.close()))
        self.assertNotIn(CurlOpt.ECH, session.curl_options)

    def test_the_session_has_no_ech_option_when_ech_is_off(self):
        client = IwaraClient()
        client._live_ips["www.iwara.tv"] = "104.20.28.232"

        session = asyncio.run(client.session_for("www.iwara.tv"))

        self.addCleanup(lambda: asyncio.run(session.close()))
        self.assertNotIn(CurlOpt.ECH, session.curl_options)

    def test_configured_ech_ips_land_behind_the_seeds(self):
        # 种子 IP 是实测可用的，用户配的只是补充：必须排在种子之后，
        # 否则 pick_ip 会先去试一个可能不通的地址。
        client = IwaraClient(
            ech=True,
            ech_ips=("203.0.113.7",),
            doh_endpoints=("https://doh.invalid/dns-query",),
        )

        async def fake_query(endpoint, host):
            return ["198.51.100.9"]  # DoH 补充的候选（本机常被污染）

        client._query_doh = fake_query  # type: ignore[assignment]
        ips = asyncio.run(client.resolve_host("www.iwara.tv"))

        seed = SEED_HOSTS["www.iwara.tv"][0]
        self.assertEqual(ips[0], seed)
        self.assertLess(ips.index("203.0.113.7"), ips.index("198.51.100.9"))

    def test_ech_ips_are_ignored_for_a_cdn_host(self):
        client = IwaraClient(ech=True, ech_ips=("203.0.113.7",))

        self.assertEqual(client.ech_ips, ("203.0.113.7",))
        self.assertFalse(is_ech_host("files.iwara.tv"))


@unittest.skipUnless(HAVE_IWARA, "需要 curl_cffi：%s" % IWARA_IMPORT_ERROR)
class IwaraEchWiringTestCase(unittest.TestCase):
    """站点状态与自检里的 ECH 接线。"""

    def test_the_state_factory_hands_the_three_switches_to_the_client(self):
        settings = IwaraSettings.load(migrate=False)
        settings.ech = True
        settings.ech_config = "cfg-base64"
        settings.ech_ips = "1.2.3.4,5.6.7.8"

        state = iwara_state_factory(settings)

        client = state["iwara_client"]
        self.addCleanup(lambda: asyncio.run(client.aclose()))
        self.assertTrue(client.ech)
        self.assertEqual(client.ech_config, "cfg-base64")
        self.assertEqual(client.ech_ips, ("1.2.3.4", "5.6.7.8"))

    def test_a_plain_framework_settings_still_builds_a_client(self):
        # 测试与脚本常常只造框架 Settings：不该因为缺 ech 字段就炸。
        state = iwara_state_factory(Settings(rate_limit_rpm=10000))

        client = state["iwara_client"]
        self.addCleanup(lambda: asyncio.run(client.aclose()))
        self.assertFalse(client.ech)

    def test_the_check_stays_at_three_items_when_ech_is_off(self):
        from iwara.envcheck import AUTH_NAME, DEPS_NAME, NET_NAME

        patcher, _instance = self._fake_client()
        with patcher:
            results = extra_checks(Settings(rate_limit_rpm=10000, data_dir=str(self.directory)))

        self.assertEqual([r.name for r in results], [DEPS_NAME, NET_NAME, AUTH_NAME])

    def test_the_ech_check_is_appended_when_the_switch_is_on(self):
        from iwara.envcheck import AUTH_NAME, DEPS_NAME, ECH_NAME, NET_NAME

        patcher, _instance = self._fake_client()
        with patcher:
            results = extra_checks(self._ech_settings())

        self.assertEqual(
            [r.name for r in results], [DEPS_NAME, NET_NAME, ECH_NAME, AUTH_NAME]
        )

    def test_the_ech_check_reads_the_trace_echo(self):
        patcher, _instance = self._fake_client(
            trace="sni=encrypted\nvisit_scheme=https\n"
        )
        with patcher:
            results = extra_checks(self._ech_settings())

        ech = [r for r in results if r.name == "iwara ECH"][0]
        self.assertTrue(ech.ok)
        self.assertIn("sni=encrypted", ech.detail)

    def test_a_plaintext_sni_is_reported_as_ech_not_working(self):
        patcher, _instance = self._fake_client(trace="sni=plaintext\n")
        with patcher:
            results = extra_checks(self._ech_settings())

        ech = [r for r in results if r.name == "iwara ECH"][0]
        self.assertFalse(ech.ok)
        self.assertIn("未生效", ech.detail)

    def test_an_http_error_from_the_trace_endpoint_is_reported(self):
        patcher, _instance = self._fake_client(trace="<html>503</html>", status=503)
        with patcher:
            results = extra_checks(self._ech_settings())

        ech = [r for r in results if r.name == "iwara ECH"][0]
        self.assertFalse(ech.ok)
        self.assertIn("HTTP 503", ech.detail)

    def test_a_failing_handshake_is_reported_not_raised(self):
        patcher, _instance = self._fake_client(error=IwaraError("都连不上"))
        with patcher:
            results = extra_checks(self._ech_settings())

        ech = [r for r in results if r.name == "iwara ECH"][0]
        self.assertFalse(ech.ok)
        self.assertIn("都连不上", ech.detail)

    def test_a_libcurl_without_ech_says_how_to_turn_it_off(self):
        patcher, _instance = self._fake_client()
        with patcher, patch.object(iwara_ech, "available", lambda: False):
            results = extra_checks(self._ech_settings())

        ech = [r for r in results if r.name == "iwara ECH"][0]
        self.assertFalse(ech.ok)
        self.assertIn("CURLOPT_ECH", ech.detail)
        self.assertIn("IWARA_BRIDGE_ECH=0", ech.detail)

    def test_the_ech_check_never_kills_the_startup(self):
        from pixiv_bridge.cli import FATAL_CHECKS

        from iwara.envcheck import ECH_NAME

        self.assertNotIn(ECH_NAME, FATAL_CHECKS)

    # -- 脚手架 ----------------------------------------------------------

    def setUp(self):
        self.directory = make_temp_dir("iwara_ech_wiring")
        self.addCleanup(drop_temp_dir, self.directory)
        env = patch.dict(os.environ, {"IWARA_REFRESH_TOKEN": ""}, clear=False)
        env.start()
        self.addCleanup(env.stop)

    def _ech_settings(self):
        settings = IwaraSettings.load(migrate=False)
        settings.ech = True
        settings.data_dir = str(self.directory)
        return settings

    def _fake_client(self, *, videos=None, error=None, trace="", status=200, username="tester"):
        """顶掉 iwara.client.IwaraClient；``trace`` 是 /cdn-cgi/trace 的回显。"""
        cls = MagicMock()
        instance = cls.return_value
        instance.ech = True
        instance.site = "www.iwara.tv"

        async def _maybe_raise():
            if error is not None:
                raise error
            return None

        async def _list_videos(**_kwargs):
            await _maybe_raise()
            return videos if videos is not None else {"count": 3, "results": []}

        async def _request(*_args, **_kwargs):
            await _maybe_raise()
            return MagicMock(text=trace, status_code=status)

        async def _me():
            await _maybe_raise()
            return {"user": {"username": username}}

        instance.list_videos = AsyncMock(side_effect=_list_videos)
        instance.request = AsyncMock(side_effect=_request)
        instance.ensure_access_token = AsyncMock(side_effect=lambda: _maybe_raise())
        instance.me = AsyncMock(side_effect=_me)
        instance.aclose = AsyncMock()
        return patch("iwara.client.IwaraClient", cls), instance


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
