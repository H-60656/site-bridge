"""pornhub 子项目的单元测试。

与 ``tests/test_iwara.py`` / ``tests/test_asmr_one.py`` 同一套约定，全部离线：
纯函数直接断言，传输层把 ``PornhubClient`` 的会话层打桩，路由用一个假客户端顶替
``app.state.ph_client``，不打任何真实请求。

pornhub 依赖 ``curl_cffi``（Cloudflare 只认浏览器 TLS 指纹，而且这里的坎是
「换边缘 IP 才通」，靠 ``CURLOPT_RESOLVE`` + ``CURLOPT_ECH``），而它只写在
``pornhub/requirements.txt`` 里 —— 新克隆的仓库还没跑过 ``pornhub/run.py`` 时会缺
这个包。所以整个模块在缺包时**整片跳过**，而不是让测试红掉：装齐依赖
（``pip install -r pornhub/requirements.txt``）后才会真正执行。

与 asmr_one 最大的不同在数据面：Pornhub **没有公开 JSON API**，接口的全部数据都
来自页面里嵌的那段 ``window.initialState``，所以这里的用例重心是「抠 JSON + 归一化
字段」以及「解不出来就 502，而不是崩进程」。
"""

import asyncio
import base64
import itertools
import json
import os
import shutil
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
from fastapi.testclient import TestClient

from pixiv_bridge.config import Settings
from pixiv_bridge.server import create_bridge_app as _create_bridge_app

# 与 test_bridge.py 同一约定：临时的东西放工作区 _tmp_tests/，别用系统 TEMP。
PROJECT_ROOT = Path(__file__).resolve().parent.parent
TEST_TMP_ROOT = PROJECT_ROOT / "_tmp_tests"
_TEMP_COUNTER = itertools.count(1)

try:
    from curl_cffi.const import CurlOpt

    from pornhub import ech as ph_ech
    from pornhub.app import PH_NAV, PH_ROUTERS, PH_STATE_KEYS, SITE
    from pornhub.app import create_app as create_pornhub_app
    from pornhub.app import ph_state_factory
    from pornhub.client import (
        ECH_HOSTS,
        MEDIA_HOSTS,
        MEDIA_HOSTS_SET,
        SEED_HOSTS,
        SEED_IPS,
        PornhubChallengeError,
        PornhubClient,
        PornhubError,
        PornhubParseError,
        _extract_initial_state,
        _video_from_state,
        _videos_from_search_state,
        is_ech_host,
        is_media_host,
    )
    from pornhub.client import SITE as SITE_DOMAIN
    from pornhub.client import SITE_HOST
    from pornhub.config import PornhubSettings, as_pornhub_settings, split_ips
    from pornhub.envcheck import ECH_NAME, NET_NAME

    HAVE_PORNHUB = True
    PORNHUB_IMPORT_ERROR = ""
except ImportError as exc:  # pragma: no cover - 只在没装 curl_cffi 时走到
    HAVE_PORNHUB = False
    PORNHUB_IMPORT_ERROR = str(exc)

SKIP_REASON = "需要 curl_cffi：pip install -r pornhub/requirements.txt"

# 一个有意义的 ECH 配置：前两字节是长度，所以是「合法」的 ECHConfigList。
GOOD_ECH_B64 = base64.b64encode(b"\x00\x06hello!").decode("ascii")

#: 白名单里的媒体直链（``/api/pornhub/stream`` 只认这几个域）。
MEDIA_URL = "https://mph.pornhub.com/video/abc/720p.mp4"
#: 8 字节，和假响应里声明的 content-length 对齐。
MP4_HEAD = b"MP4HEAD!"


def make_settings():
    # pornhub 站点只在框架 Settings 上加了 ECH 三项；这里给一个纯框架 Settings，
    # 让 as_pornhub_settings 去补默认值（正常入口走 PornhubSettings.load）。
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


def state_html(payload):
    """包成「视频页 HTML」：`request()` 只认页面里这段 initialState。"""
    return (
        "<html><head><title>x</title></head><body><script>"
        "window.initialState = " + json.dumps(payload) + ";"
        "</script></body></html>"
    )


class _FakeResp:
    """``request()`` 只会用到这几个属性。"""

    def __init__(self, status_code=200, payload=None, headers=None, text="", content=b""):
        self.status_code = status_code
        self._payload = payload if payload is not None else {}
        self.headers = headers or {}
        self.text = text or json.dumps(self._payload)
        self.content = content

    def json(self):
        return self._payload


class _FakeStreamResp:
    """``open_stream()`` 用的响应：只要状态、头和一个异步字节流。"""

    def __init__(self, status_code=206, headers=None, chunks=(MP4_HEAD,)):
        self.status_code = status_code
        self.headers = headers or {
            "content-type": "video/mp4",
            "content-length": str(len(MP4_HEAD)),
            "accept-ranges": "bytes",
            "etag": '"abc"',
            # 不在白名单里的头，客户端必须丢掉。
            "x-amz-request-id": "should-be-dropped",
        }
        self.chunks = tuple(chunks)

    def iter_content(self, chunk_size=0):
        async def gen():
            for chunk in self.chunks:
                yield chunk

        return gen()


class _FakeSession:
    """顶替 curl_cffi 的 AsyncSession：按顺序吐出预定的响应/异常。

    注意：这里**不能**再造一个 ``self.curl`` —— ``AsyncSession`` 根本没有
    ``.curl``（只有 ``.acurl``）。真实的钉 IP 走会话级 ``curl_options``，
    所以「这次试哪个 IP」由 ``_FakeSessions`` 记录 ``_session(host, ip)`` 的
    入参来断言，而不是去偷看一个并不存在的属性。
    """

    def __init__(self, outcomes=()):
        self.outcomes = list(outcomes)
        self.calls = []
        self._last = None

    async def request(self, method, url, **kwargs):
        self.calls.append((method, url, kwargs))
        if self.outcomes:
            # 队列耗尽后重复最后一条：`request()` 会按 ip_attempts 反复重试，
            # 只给一条失败时后面几次不该凭空变成成功。
            self._last = outcome = self.outcomes.pop(0)
        else:
            outcome = self._last or _FakeResp(200, text="<html>ok</html>")
        if isinstance(outcome, Exception):
            raise outcome
        return outcome

    async def close(self):
        return None


# 真实的视频页 initialState 形状：字段名与线上一致。
VIDEO_STATE = {
    "video": {
        "id": "ph60abc123",
        "viewkey": "ph60abc123",
        "title": "测试视频",
        "duration": "12:34",
        "views": 12345,
        "rating": "88",
        "ratings": 77,
        "thumbs": [
            {"url": "https://ei.phncdn.com/a.jpg"},
            {"url": "https://ei.phncdn.com/b.jpg"},
            "not-a-mapping",  # 脏数据必须被跳过
        ],
        "tags": [{"title": "tag1"}, {"title": "tag2"}, "not-a-mapping"],
        "categories": [{"title": "cat1"}, "not-a-mapping"],
        "mediaDefinitions": [
            {"format": "mp4", "quality": "480", "videoUrl": "https://mph.pornhub.com/480.mp4"},
            # 低质量在前、高质量在后：客户端取「最后一个非空的 mp4」。
            {"format": "mp4", "quality": "720", "videoUrl": "https://mph.pornhub.com/720.mp4"},
            {"format": "hls", "quality": "auto", "videoUrl": "https://mph.pornhub.com/master.m3u8"},
            {"format": "mp4", "quality": "1080", "videoUrl": ""},
        ],
        "downloadUrls": [
            {"url": "https://mpthc.phncdn.com/low.mp4", "quality": "480"},
            {"url": "https://mpthc.phncdn.com/high.mp4", "quality": "720"},
        ],
    }
}

LIST_STATE = {
    "page": {
        "videos": [
            {
                "id": "ph1",
                "title": "第一条",
                "duration": "1:00",
                "views": 10,
                "rating": "90",
                "thumbs": [{"url": "https://ei.phncdn.com/1.jpg"}],
                "tags": [{"title": "t"}],
            },
            "not-a-mapping",  # 脏数据必须被跳过
        ]
    }
}


@unittest.skipUnless(HAVE_PORNHUB, SKIP_REASON)
class PornhubEchConfigTestCase(unittest.TestCase):
    """``pornhub/ech.py``：ECH 配置的解析、缓存与取值。

    它是 pornhub 版的自足实现（站点之间不能互相 import，见
    ``scripts/check_boundary.py``），路线与 iwara / asmr_one 一致：只把配置交给
    libcurl 的 ``CURLOPT_ECH``，不需要任何动态库。所以这里的用例全是纯逻辑。
    """

    def setUp(self):
        # 缓存是模块级的，谁先跑都会污染别人；每个用例都从干净状态开始。
        ph_ech.reset_config_cache()
        self.addCleanup(ph_ech.reset_config_cache)

    def test_the_bundled_config_is_a_usable_echconfiglist(self):
        raw = ph_ech._decode_config(ph_ech.DEFAULT_ECH_CONFIG)

        self.assertGreaterEqual(len(raw), 4)
        # ECHConfigList 的前两个字节是大端长度，必须覆盖整段，否则 libcurl
        # 会当成畸形配置丢掉。
        self.assertEqual(int.from_bytes(raw[:2], "big"), len(raw) - 2)
        # 内置配置里的 public_name 就是外层 SNI 用的那个名字。
        self.assertIn(b"cloudflare-ech.com", raw)

    def test_a_too_short_config_is_rejected(self):
        with self.assertRaises(ph_ech.EchUnavailable) as ctx:
            ph_ech._decode_config(base64.b64encode(b"\x01\x02").decode("ascii"))

        self.assertIn("过短", str(ctx.exception))

    def test_garbage_is_rejected_with_a_readable_reason(self):
        with self.assertRaises(ph_ech.EchUnavailable) as ctx:
            ph_ech._decode_config("not base64 at all!!")

        self.assertIn("base64", str(ctx.exception))

    def test_pem_wrapping_and_whitespace_are_tolerated(self):
        wrapped = "-----BEGIN ECH-----\n" + ph_ech.DEFAULT_ECH_CONFIG + "\n-----END ECH-----"

        self.assertEqual(
            ph_ech._decode_config(wrapped),
            ph_ech._decode_config(ph_ech.DEFAULT_ECH_CONFIG),
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

        self.assertEqual(ph_ech._ech_from_doh(payload), ph_ech._decode_config(good))

    def test_the_doh_regex_handles_the_escaped_quote(self):
        # DoH 的 JSON 里引号被转义成 \"：正则不许它，就永远退回内置配置。
        good = base64.b64encode(b"\x00\x04abcd").decode("ascii")
        for data in (f'ech=\\"{good}\\"', f'ech="{good}"', f"ech={good}"):
            self.assertRegex(data, ph_ech._ECH_VALUE_RE, data)

    def test_a_payload_without_any_ech_record_yields_nothing(self):
        self.assertIsNone(ph_ech._ech_from_doh({"Answer": [{"type": 65, "data": 'alpn="h2"'}]}))
        self.assertIsNone(ph_ech._ech_from_doh({}))
        self.assertIsNone(ph_ech._ech_from_doh(None))

    def test_a_broken_record_falls_through_to_the_next_answer(self):
        good = base64.b64encode(b"\x00\x04abcd").decode("ascii")
        payload = {
            "Answer": [
                {"type": 65, "data": 'ech="!!!!"'},  # 非法 base64，跳过
                {"type": 65, "data": f'ech="{good}"'},
            ]
        }

        self.assertEqual(ph_ech._ech_from_doh(payload), ph_ech._decode_config(good))

    def test_fetch_tries_every_endpoint_before_giving_up(self):
        seen = []

        def boom(url, **_kwargs):
            seen.append(url)
            raise httpx.ConnectError("no route")

        with patch.object(ph_ech.httpx, "get", boom):
            with self.assertRaises(ph_ech.EchUnavailable) as ctx:
                ph_ech.fetch_ech_config(endpoints=("https://a/resolve", "https://b/resolve"))

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

        with patch.object(ph_ech.httpx, "get", fake_get):
            found = ph_ech.fetch_ech_config(endpoints=("https://a/resolve",))

        self.assertEqual(found, ph_ech._decode_config(GOOD_ECH_B64))
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
                json=lambda: {"Answer": []},
                raise_for_status=lambda: None,
            )

        with patch.object(ph_ech.httpx, "get", fake_get):
            ph_ech.fetch_ech_config()

        self.assertEqual(seen, list(ph_ech.DOH_ENDPOINTS))

    def test_the_cache_ttl_is_bounded(self):
        # Cloudflare 轮换密钥是小时级：缓存不能太长（拿到旧配置就是握手失败），
        # 也不能太短（每次都打 DoH）。
        self.assertGreater(ph_ech.ECH_CONFIG_TTL, 0)
        self.assertLessEqual(ph_ech.ECH_CONFIG_TTL, 6 * 3600)

    def test_doh_failure_falls_back_to_the_bundled_config(self):
        def boom(url, **_kwargs):
            raise httpx.ConnectError("no route")

        with patch.object(ph_ech.httpx, "get", boom):
            config = ph_ech.ech_config(refresh=True)

        self.assertEqual(config, ph_ech._decode_config(ph_ech.DEFAULT_ECH_CONFIG))

    def test_the_second_call_uses_the_cache(self):
        calls = []

        def boom(url, **_kwargs):
            calls.append(url)
            raise httpx.ConnectError("no route")

        with patch.object(ph_ech.httpx, "get", boom):
            first = ph_ech.ech_config(refresh=True)
            before = len(calls)
            second = ph_ech.ech_config()

        self.assertEqual(first, second)
        self.assertEqual(len(calls), before, "缓存命中时不该再打 DoH")

    def test_an_override_short_circuits_doh(self):
        def boom(url, **_kwargs):
            raise AssertionError("有 override 时不该查 DoH")

        with patch.object(ph_ech.httpx, "get", boom):
            config = ph_ech.ech_config(GOOD_ECH_B64)

        self.assertEqual(config, ph_ech._decode_config(GOOD_ECH_B64))

    def test_the_option_is_prefixed_with_ecl(self):
        with patch.object(ph_ech, "available", lambda: True):
            option = ph_ech.ech_option(override=GOOD_ECH_B64)

        self.assertEqual(option, ph_ech.ECH_OPTION_PREFIX + GOOD_ECH_B64)
        self.assertEqual(ph_ech.ECH_OPTION_PREFIX, "ecl:")

    def test_a_raw_config_is_encoded_as_is(self):
        raw = b"\x00\x04abcd"

        with patch.object(ph_ech, "available", lambda: True):
            option = ph_ech.ech_option(raw=raw)

        self.assertEqual(option, "ecl:" + base64.b64encode(raw).decode("ascii"))

    def test_the_option_is_off_when_libcurl_lacks_ech(self):
        with patch.object(ph_ech, "available", lambda: False):
            self.assertIsNone(ph_ech.ech_option(raw=b"\x00\x04abcd"))

    def test_the_option_is_off_for_an_invalid_override(self):
        with patch.object(ph_ech, "available", lambda: True):
            self.assertIsNone(ph_ech.ech_option(override="not base64!!"))

    def test_the_option_is_off_for_an_empty_config(self):
        with patch.object(ph_ech, "available", lambda: True):
            self.assertIsNone(ph_ech.ech_option(raw=b""))

    def test_available_reports_the_libcurl_capability(self):
        self.assertIsInstance(ph_ech.available(), bool)
        # 这台机器上 curl_cffi 确实带 CURLOPT_ECH，否则本模块本来就被整片跳过。
        self.assertTrue(ph_ech.available())


@unittest.skipUnless(HAVE_PORNHUB, SKIP_REASON)
class PornhubSettingsTestCase(unittest.TestCase):
    """``pornhub/config.py``：IP 串切分与 ECH 三个开关的默认值。

    ``PORNHUB_BRIDGE_ECH`` 与 asmr_one 一样：**不写也是开着的**（Pornhub 在
    Cloudflare 后面，离开 ECH 大概率抓取全灭），要关必须显式写 0。
    """

    def setUp(self):
        # ``Settings.load`` 会把 .env 灌进 os.environ，也会去碰数据目录；
        # 这里一律指到一个空目录，并断言式地清掉站点开关。
        self.directory = make_temp_dir("pornhub_settings")
        self.addCleanup(drop_temp_dir, self.directory)
        env = patch.dict(
            os.environ,
            {
                "PIXIV_DATA_DIR": str(self.directory),
                "PORNHUB_BRIDGE_ECH": "",
                "PORNHUB_BRIDGE_ECH_CONFIG": "",
                "PORNHUB_BRIDGE_ECH_IPS": "",
            },
            clear=False,
        )
        env.start()
        self.addCleanup(env.stop)

    def test_split_ips_accepts_commas_semicolons_and_spaces(self):
        self.assertEqual(split_ips("1.2.3.4,5.6.7.8"), ("1.2.3.4", "5.6.7.8"))
        self.assertEqual(split_ips("1.2.3.4;5.6.7.8"), ("1.2.3.4", "5.6.7.8"))
        self.assertEqual(split_ips("1.2.3.4 5.6.7.8"), ("1.2.3.4", "5.6.7.8"))
        self.assertEqual(split_ips(""), ())
        self.assertEqual(split_ips("  , 1.2.3.4 , "), ("1.2.3.4",))

    def test_a_missing_switch_means_ech_is_on(self):
        settings = PornhubSettings.load(migrate=False)

        self.assertTrue(settings.ech, "Pornhub 离开 ECH 大概率抓不动，默认必须开")
        self.assertEqual(settings.ech_config, "")
        self.assertEqual(settings.ech_ips, "")

    def test_an_explicit_zero_turns_ech_off(self):
        os.environ["PORNHUB_BRIDGE_ECH"] = "0"

        self.assertFalse(PornhubSettings.load(migrate=False).ech)

    def test_the_other_falsy_spellings_also_turn_it_off(self):
        for raw in ("0", "false", "no", "off", "FALSE"):
            os.environ["PORNHUB_BRIDGE_ECH"] = raw
            self.assertFalse(PornhubSettings.load(migrate=False).ech, raw)

    def test_the_truthy_spellings_keep_it_on(self):
        for raw in ("1", "true", "yes", "on"):
            os.environ["PORNHUB_BRIDGE_ECH"] = raw
            self.assertTrue(PornhubSettings.load(migrate=False).ech, raw)

    def test_a_whitespace_only_switch_still_means_the_default(self):
        # 「写了但只有空格」和「没写」同义：别把空白当成显式关闭。
        os.environ["PORNHUB_BRIDGE_ECH"] = "   "

        self.assertTrue(PornhubSettings.load(migrate=False).ech)

    def test_the_override_and_ip_pool_are_read(self):
        os.environ["PORNHUB_BRIDGE_ECH_CONFIG"] = GOOD_ECH_B64
        os.environ["PORNHUB_BRIDGE_ECH_IPS"] = "1.1.1.1 2.2.2.2"

        settings = PornhubSettings.load(migrate=False)

        self.assertEqual(settings.ech_config, GOOD_ECH_B64)
        self.assertEqual(settings.ech_ips, "1.1.1.1 2.2.2.2")
        self.assertEqual(split_ips(settings.ech_ips), ("1.1.1.1", "2.2.2.2"))

    def test_the_site_variables_do_not_answer_to_other_sites_names(self):
        # 四个站点同级，各自的开关互不影响：别的站点前缀不该被读。
        os.environ["PIXIV_BRIDGE_ECH"] = "0"
        os.environ["ASMR_BRIDGE_ECH"] = "0"
        os.environ["IWARA_BRIDGE_ECH"] = "0"

        self.assertTrue(PornhubSettings.load(migrate=False).ech)

    def test_a_plain_framework_settings_is_upgraded_with_defaults(self):
        upgraded = as_pornhub_settings(make_settings())

        self.assertIsInstance(upgraded, PornhubSettings)
        self.assertTrue(upgraded.ech)
        self.assertEqual(upgraded.ech_config, "")
        self.assertEqual(upgraded.rate_limit_rpm, 10000)

    def test_a_pornhub_settings_passes_through_unchanged(self):
        original = PornhubSettings(rate_limit_rpm=10000, ech=False)

        self.assertIs(as_pornhub_settings(original), original)
        self.assertFalse(as_pornhub_settings(original).ech)

    def test_none_loads_the_real_settings(self):
        upgraded = as_pornhub_settings(None)

        self.assertIsInstance(upgraded, PornhubSettings)
        self.assertEqual(Path(upgraded.data_dir), Path(self.directory))


@unittest.skipUnless(HAVE_PORNHUB, SKIP_REASON)
class PornhubClientLogicTestCase(unittest.TestCase):
    """不联网的纯逻辑：initialState 的抠取、归一化与主机白名单。"""

    # ---------- 抠 JSON ----------

    def test_the_state_is_pulled_out_of_the_page(self):
        state = _extract_initial_state(state_html({"video": {"viewkey": "abc"}}))

        self.assertEqual(state, {"video": {"viewkey": "abc"}})

    def test_a_page_without_the_state_is_a_parse_error(self):
        with self.assertRaises(PornhubParseError) as ctx:
            _extract_initial_state("<html>没有那段 JSON</html>")

        self.assertEqual(ctx.exception.status, 502)
        self.assertEqual(ctx.exception.reason, "parse_failed")
        self.assertIn("找不到", ctx.exception.message)

    def test_malformed_json_is_a_parse_error(self):
        html = "<html><script>window.initialState = {oops};</script></html>"

        with self.assertRaises(PornhubParseError) as ctx:
            _extract_initial_state(html)

        self.assertIn("不是合法 JSON", ctx.exception.message)

    def test_the_challenge_page_hint_is_surfaced(self):
        # Cloudflare 挑战页里也不会有 initialState：这条错误信息就是给用户看的
        # 排查线索。
        with self.assertRaises(PornhubParseError) as ctx:
            _extract_initial_state("<html><title>Just a moment...</title></html>")

        self.assertIn("Cloudflare 挑战", ctx.exception.message)

    # ---------- 视频详情归一化 ----------

    def test_the_video_is_normalised(self):
        video = _video_from_state(VIDEO_STATE)

        self.assertEqual(video["id"], "ph60abc123")
        self.assertEqual(video["viewkey"], "ph60abc123")
        self.assertEqual(video["title"], "测试视频")
        self.assertEqual(video["duration"], "12:34")
        self.assertEqual(video["views"], 12345)
        self.assertEqual(video["rating"], "88")
        self.assertEqual(video["ratings"], 77)

    def test_the_highest_mp4_is_picked_and_the_last_download(self):
        video = _video_from_state(VIDEO_STATE)

        # 媒体列表里最后一个「非空 videoUrl 的 mp4」才是最高质量。
        self.assertEqual(video["mp4_url"], "https://mph.pornhub.com/720.mp4")
        self.assertEqual(video["download_url"], "https://mpthc.phncdn.com/high.mp4")

    def test_only_mapping_entries_survive_the_normalisation(self):
        video = _video_from_state(VIDEO_STATE)

        self.assertEqual(
            video["thumbnails"],
            ["https://ei.phncdn.com/a.jpg", "https://ei.phncdn.com/b.jpg"],
        )
        self.assertEqual(video["tags"], ["tag1", "tag2"])
        self.assertEqual(video["categories"], ["cat1"])

    def test_a_state_without_a_video_is_a_parse_error(self):
        for state in ({}, {"page": {}}, {"video": None}):
            with self.assertRaises(PornhubParseError) as ctx:
                _video_from_state(state)
            self.assertIn("缺失", ctx.exception.message, state)

    def test_a_non_mapping_state_is_a_parse_error(self):
        with self.assertRaises(PornhubParseError):
            _video_from_state("不是字典")

    def test_the_video_falls_back_to_the_alternate_field_names(self):
        video = _video_from_state(
            {
                "video": {
                    "viewkey": "vk",
                    "video_title": "备用标题",
                    "video_duration": "0:30",
                }
            }
        )

        self.assertEqual(video["id"], "vk")
        self.assertEqual(video["title"], "备用标题")
        self.assertEqual(video["duration"], "0:30")
        self.assertEqual(video["views"], 0)
        self.assertEqual(video["rating"], "")
        self.assertIsNone(video["mp4_url"])
        self.assertIsNone(video["download_url"])

    # ---------- 列表归一化 ----------

    def test_the_list_is_read_from_the_page_container(self):
        videos = _videos_from_search_state(LIST_STATE)

        self.assertEqual(len(videos), 1, "脏条目必须被跳过")
        item = videos[0]
        self.assertEqual(item["id"], "ph1")
        self.assertEqual(item["title"], "第一条")
        self.assertEqual(item["duration"], "1:00")
        self.assertEqual(item["views"], 10)
        self.assertEqual(item["thumbnails"], ["https://ei.phncdn.com/1.jpg"])
        self.assertEqual(item["tags"], ["t"])
        # 列表条目不带直链：那要再点进详情页。
        self.assertNotIn("mp4_url", item)

    def test_the_container_keys_are_tried_in_order(self):
        videos = _videos_from_search_state({"search": {"items": [{"id": "s1"}]}})

        self.assertEqual([v["id"] for v in videos], ["s1"])

    def test_a_page_without_any_list_is_an_empty_list(self):
        for state in ({}, {"page": {}}, {"page": {"videos": None}}, {"page": []}):
            self.assertEqual(_videos_from_search_state(state), [], state)

    # ---------- 主机白名单 ----------

    def test_is_ech_host_covers_every_cloudflare_host(self):
        for host in ECH_HOSTS:
            self.assertTrue(is_ech_host(host), host)
        self.assertFalse(is_ech_host("example.com"))

    def test_is_media_host_is_only_the_whitelisted_cdn_domains(self):
        for host in MEDIA_HOSTS:
            self.assertTrue(is_media_host(host), host)
        self.assertEqual(MEDIA_HOSTS_SET, frozenset(MEDIA_HOSTS))
        self.assertFalse(is_media_host("evil.example.com"))
        self.assertFalse(is_media_host("mph.pornhub.com.evil.com"))

    def test_the_site_domain_and_host_are_the_expected_ones(self):
        self.assertEqual(SITE_DOMAIN, "pornhub.com")
        self.assertEqual(SITE_HOST, "www.pornhub.com")
        self.assertIn(SITE_HOST, ECH_HOSTS)


@unittest.skipUnless(HAVE_PORNHUB, SKIP_REASON)
class PornhubTransportTestCase(unittest.TestCase):
    """传输层：DoH 解析、借道 IP 池、单 IP 固定、ECH 开关与换 IP 重试。

    全部打桩：这里验证的是形状（每次尝试只固定一个 IP、挑战会重试、ECH 只对
    Cloudflare 主机开），不是真连通性。
    """

    def _client(self, **kw):
        client = PornhubClient(**kw)
        self.addCleanup(lambda: asyncio.run(client.aclose()))
        # TCP 预探测一律打桩成「通」：否则用例会真去连候选 IP（借道池里
        # 大部分是不通的），测试既慢又依赖网络。要验证探测本身时再单独覆盖。
        client._tcp_reachable = AsyncMock(return_value=True)  # type: ignore[assignment]
        # 默认把 DoH 也掐掉：这几个用例关心的是 IP 选择与会话参数，不是解析。
        # 忘了打桩就会真去查阿里/Cloudflare 的 DoH，测试也就不可复现了。
        return self._patch_doh(client, error=httpx.ConnectError("DoH 已打桩"))

    def _patch_doh(self, client, mapping=None, error=None):
        async def fake_get(endpoint, params=None):
            if error is not None:
                raise error
            host = (params or {}).get("name")
            return _FakeResp(200, payload={"Answer": (mapping or {}).get(host, [])})

        client._doh_client = MagicMock()
        client._doh_client.get = AsyncMock(side_effect=fake_get)
        return client

    def _patch_session(self, client, session):
        """顶掉 ``_session`` 并记下每次尝试钉的 IP。

        真实实现把 IP 写进**会话级** ``curl_options``（``AsyncSession`` 没有
        ``.curl``，建会话之后没法再 setopt），所以「这次试哪个 IP」只能从
        ``_session(host, ip)`` 的入参观察。
        """
        client.pinned = []

        def fake_session(host, ip=None):
            client.pinned.append((host, ip))
            return session

        client._session = fake_session  # type: ignore[assignment]
        return session

    # ---------- DNS ----------

    def test_the_seed_hosts_cover_the_two_cloudflare_names(self):
        for host in (SITE_HOST, "pornhub.com"):
            self.assertIn(host, SEED_HOSTS, host)
            self.assertTrue(SEED_HOSTS[host], host)

    def test_resolve_only_trusts_a_records(self):
        client = self._patch_doh(
            self._client(doh_endpoints=("https://doh/a",)),
            {
                SITE_HOST: [
                    {"type": 5, "data": "cname.example.com"},  # CNAME 不是地址
                    {"type": 1, "data": "9.9.9.9"},
                    {"type": 1, "data": "10.10.10.10"},
                ]
            },
        )

        self.assertEqual(asyncio.run(client._resolve(SITE_HOST)), ["9.9.9.9", "10.10.10.10"])

    def test_resolve_falls_back_to_the_seed_ips_when_doh_dies(self):
        client = self._patch_doh(
            self._client(doh_endpoints=("https://doh/a",)), error=httpx.ConnectError("RST")
        )

        self.assertEqual(asyncio.run(client._resolve(SITE_HOST)), list(SEED_HOSTS[SITE_HOST]))

    def test_resolve_hands_unknown_hosts_the_borrowed_pool(self):
        client = self._patch_doh(
            self._client(doh_endpoints=("https://doh/a",)), error=httpx.ConnectError("RST")
        )

        self.assertEqual(asyncio.run(client._resolve("nowhere.example")), list(SEED_IPS))

    def test_configured_ips_win_for_an_unregistered_host(self):
        client = self._patch_doh(
            self._client(doh_endpoints=("https://doh/a",), ech_ips=("8.8.8.8",)),
            error=httpx.ConnectError("RST"),
        )

        self.assertEqual(asyncio.run(client._resolve("nowhere.example")), ["8.8.8.8"])

    # ---------- IP 选择 ----------

    def test_pick_ip_puts_the_configured_ips_first(self):
        client = self._patch_doh(
            self._client(ech_ips=("8.8.8.8",)), error=httpx.ConnectError("RST")
        )

        ip, rest = asyncio.run(client._pick_ip(SITE_HOST))

        self.assertEqual(ip, "8.8.8.8")
        self.assertIn(SEED_HOSTS[SITE_HOST][0], rest)
        self.assertNotIn("8.8.8.8", rest)
        self.assertEqual(client._live_ips[SITE_HOST], "8.8.8.8")

    def test_pick_ip_caches_the_chosen_ip(self):
        client = self._patch_doh(
            self._client(doh_endpoints=("https://doh/a",)), error=httpx.ConnectError("RST")
        )

        first, _rest = asyncio.run(client._pick_ip(SITE_HOST))
        again = asyncio.run(client._pick_ip(SITE_HOST))

        self.assertEqual(again, (first, []), "已经定下来的 IP 不该再重算一遍候选")

    # ---------- 会话与 ECH ----------

    def test_session_pins_one_ip_and_keeps_the_real_sni(self):
        client = self._patch_doh(
            self._client(ech_ips=("8.8.8.8",)), error=httpx.ConnectError("RST")
        )
        captured = {}

        def fake_session(**kwargs):
            captured.update(kwargs)
            return MagicMock()

        with patch("pornhub.client.curl_requests.AsyncSession", fake_session):
            asyncio.run(client._pick_ip(SITE_HOST))
            client._session(SITE_HOST)

        # RESOLVE 只是「把域名解到哪个 IP」，不改 Host/SNI：TLS 里仍是真实域名。
        self.assertEqual(captured["impersonate"], "chrome")
        self.assertTrue(captured["timeout"] > 0)
        options = captured["curl_options"]
        self.assertEqual(options[CurlOpt.RESOLVE], [f"{SITE_HOST}:443:8.8.8.8"])
        self.assertGreater(options[CurlOpt.CONNECTTIMEOUT], 0)

    def test_session_rebuilds_when_the_ip_changes(self):
        """换 IP 必须重建会话：RESOLVE 是会话级的，旧会话改不了。"""
        client = self._patch_doh(
            self._client(doh_endpoints=("https://doh/a",)),
            {SITE_HOST: [{"type": 1, "data": "1.1.1.1"}]},
        )
        built = []

        def fake_session(**kwargs):
            built.append(kwargs)
            return MagicMock()

        with patch("pornhub.client.curl_requests.AsyncSession", fake_session):
            first = client._session(SITE_HOST, "1.1.1.1")
            again = client._session(SITE_HOST, "1.1.1.1")
            second = client._session(SITE_HOST, "2.2.2.2")

        self.assertIs(first, again, "同一个 IP 该复用会话")
        self.assertIsNot(first, second, "换了 IP 必须换会话")
        self.assertEqual(len(built), 2)
        self.assertEqual(built[1]["curl_options"][CurlOpt.RESOLVE], [f"{SITE_HOST}:443:2.2.2.2"])

    def test_session_reuses_the_same_object_per_host(self):
        client = self._client()
        built = []

        def fake_session(**kwargs):
            built.append(kwargs)
            return MagicMock()

        with patch("pornhub.client.curl_requests.AsyncSession", fake_session):
            first = client._session(SITE_HOST, "1.1.1.1")
            second = client._session(SITE_HOST, "1.1.1.1")

        self.assertIs(first, second)
        self.assertEqual(len(built), 1)

    def test_session_turns_on_ech_for_a_cloudflare_host(self):
        client = self._client(ech=True, ech_config=GOOD_ECH_B64)
        captured = {}

        def fake_session(**kwargs):
            captured.update(kwargs)
            return MagicMock()

        with patch("pornhub.client.curl_requests.AsyncSession", fake_session), patch.object(
            ph_ech, "available", lambda: True
        ):
            client._session(SITE_HOST, "1.1.1.1")

        option = captured["curl_options"][CurlOpt.ECH]
        self.assertEqual(option, ("ecl:" + GOOD_ECH_B64).encode())

    def test_session_leaves_a_non_cloudflare_host_without_ech(self):
        client = self._client(ech=True, ech_config=GOOD_ECH_B64)
        captured = {}

        def fake_session(**kwargs):
            captured.update(kwargs)
            return MagicMock()

        with patch("pornhub.client.curl_requests.AsyncSession", fake_session):
            client._session("example.com", "1.1.1.1")

        self.assertNotIn(CurlOpt.ECH, captured["curl_options"])

    def test_session_leaves_ech_off_when_the_switch_is_off(self):
        client = self._client(ech=False, ech_config=GOOD_ECH_B64)
        captured = {}

        def fake_session(**kwargs):
            captured.update(kwargs)
            return MagicMock()

        with patch("pornhub.client.curl_requests.AsyncSession", fake_session), patch.object(
            ph_ech, "available", lambda: True
        ):
            client._session(SITE_HOST, "1.1.1.1")

        self.assertNotIn(CurlOpt.ECH, captured["curl_options"])

    def test_session_leaves_ech_off_when_libcurl_lacks_it(self):
        client = self._client(ech=True, ech_config=GOOD_ECH_B64)
        captured = {}

        def fake_session(**kwargs):
            captured.update(kwargs)
            return MagicMock()

        with patch("pornhub.client.curl_requests.AsyncSession", fake_session), patch.object(
            ph_ech, "available", lambda: False
        ):
            client._session(SITE_HOST, "1.1.1.1")

        self.assertNotIn(CurlOpt.ECH, captured["curl_options"])

    # ---------- 请求与重试 ----------

    def test_request_pins_exactly_one_ip_per_attempt(self):
        client = self._client(ip_attempts=1)
        session = self._patch_session(client, _FakeSession([_FakeResp(200)]))
        client._live_ips[SITE_HOST] = "1.1.1.1"

        asyncio.run(client.request("GET", "/video"))

        self.assertEqual(client.pinned, [(SITE_HOST, "1.1.1.1")])
        self.assertEqual(session.calls[0][1], f"https://{SITE_HOST}/video")

    def test_request_passes_the_params_through(self):
        client = self._client(ip_attempts=1)
        session = self._patch_session(client, _FakeSession([_FakeResp(200)]))
        client._live_ips[SITE_HOST] = "1.1.1.1"

        asyncio.run(client.request("GET", "/video/search", params={"search": "x", "page": 2}))

        self.assertEqual(session.calls[0][2]["params"], {"search": "x", "page": 2})

    def test_request_swaps_the_ip_after_a_connect_failure(self):
        client = self._client(ip_attempts=3)
        session = self._patch_session(
            client, _FakeSession([httpx.ConnectError("RST"), _FakeResp(200)])
        )
        client._live_ips[SITE_HOST] = "1.1.1.1"

        resp = asyncio.run(client.request("GET", "/video"))

        self.assertEqual(resp.status_code, 200)
        self.assertEqual(len(session.calls), 2)
        # 两次尝试固定的是两个不同的 IP，失败的第一个要被拉黑。
        self.assertEqual(len(client.pinned), 2)
        self.assertNotEqual(client.pinned[0][1], client.pinned[1][1])
        self.assertIn("1.1.1.1", client._dead_ips[SITE_HOST])
        self.assertEqual(client._live_ips[SITE_HOST], client.pinned[1][1])

    def test_a_cloudflare_challenge_moves_on_to_the_next_ip(self):
        client = self._client(ip_attempts=2)
        session = self._patch_session(
            client,
            _FakeSession([_FakeResp(403, text="<html>cf-chl</html>"), _FakeResp(200)]),
        )
        client._live_ips[SITE_HOST] = "1.1.1.1"

        resp = asyncio.run(client.request("GET", "/video"))

        self.assertEqual(resp.status_code, 200)
        self.assertEqual(len(session.calls), 2)

    def test_a_plain_403_is_not_a_challenge(self):
        # JSON 的 403 是上游的业务回答，不该被当成 Cloudflare 挑战换 IP 重试。
        client = self._client(ip_attempts=3)
        session = self._patch_session(
            client, _FakeSession([_FakeResp(403, headers={"content-type": "application/json"}, text="{}")])
        )
        client._live_ips[SITE_HOST] = "1.1.1.1"

        resp = asyncio.run(client.request("GET", "/video"))

        self.assertEqual(resp.status_code, 403)
        self.assertEqual(len(session.calls), 1)

    def test_request_raises_a_challenge_error_when_every_ip_fails(self):
        client = self._client(ip_attempts=2)
        self._patch_session(client, _FakeSession([httpx.ConnectError("RST")]))

        with self.assertRaises(PornhubChallengeError) as ctx:
            asyncio.run(client.request("GET", "/video"))

        self.assertEqual(ctx.exception.status, 502)
        self.assertEqual(ctx.exception.reason, "cloudflare_challenge")
        self.assertIn("全部 IP 失败", ctx.exception.message)

    def test_request_says_so_when_there_is_no_exception_to_report(self):
        # 全是挑战页时 last_exc 是 None：文案不能写成「失败：None」。
        client = self._client(ip_attempts=1)
        self._patch_session(client, _FakeSession([_FakeResp(403, text="cf-chl")]))

        with self.assertRaises(PornhubChallengeError) as ctx:
            asyncio.run(client.request("GET", "/video"))

        self.assertIn("无异常信息", ctx.exception.message)

    def test_request_honours_the_attempt_cap(self):
        client = self._client(ip_attempts=2)
        session = self._patch_session(client, _FakeSession([httpx.ConnectError("RST")]))

        with self.assertRaises(PornhubChallengeError):
            asyncio.run(client.request("GET", "/video"))

        self.assertEqual(len(session.calls), 2)

    def test_request_switches_the_host_when_asked(self):
        client = self._client(ip_attempts=1)
        session = self._patch_session(client, _FakeSession([_FakeResp(200)]))
        client._live_ips["pornhub.com"] = "1.1.1.1"

        asyncio.run(client.request("GET", "/cdn-cgi/trace", host="pornhub.com"))

        self.assertEqual(session.calls[0][1], "https://pornhub.com/cdn-cgi/trace")
        self.assertEqual(client.pinned, [("pornhub.com", "1.1.1.1")])


@unittest.skipUnless(HAVE_PORNHUB, SKIP_REASON)
class PornhubClientApiTestCase(unittest.TestCase):
    """业务方法：各自的路径/参数形状，以及解析失败怎么变成 PornhubError。"""

    def _client(self):
        client = PornhubClient()
        self.addCleanup(lambda: asyncio.run(client.aclose()))
        return client

    def _patch_request(self, client, responses):
        """顶掉 ``request()``，返回每次调用的记录。"""
        seen = []
        queue = list(responses)

        async def fake_request(method, path, **kw):
            seen.append((method, path, kw))
            item = queue.pop(0) if queue else _FakeResp(200, text="<html>ok</html>")
            if isinstance(item, Exception):
                raise item
            return item

        client.request = fake_request  # type: ignore[assignment]
        return seen

    # ---------- 探活 ----------

    def test_health_accepts_a_html_page_without_the_state(self):
        client = self._client()
        seen = self._patch_request(client, [_FakeResp(200, text="<html>首页</html>")])

        info = asyncio.run(client.health())

        self.assertEqual(info, {"status": "ok", "status_code": 200})
        self.assertEqual(seen, [("GET", "/", {})])

    def test_health_accepts_a_page_with_the_state(self):
        client = self._client()
        self._patch_request(client, [_FakeResp(200, text=state_html(VIDEO_STATE))])

        self.assertEqual(asyncio.run(client.health())["status_code"], 200)

    def test_health_re_raises_when_the_page_is_not_html(self):
        client = self._client()
        self._patch_request(client, [_FakeResp(403, text="nope")])

        with self.assertRaises(PornhubParseError):
            asyncio.run(client.health())

    # ---------- 列表与搜索 ----------

    def test_list_videos_returns_a_normalised_page(self):
        client = self._client()
        seen = self._patch_request(client, [_FakeResp(200, text=state_html(LIST_STATE))])

        body = asyncio.run(client.list_videos(page=3))

        self.assertEqual(body["segment"], "video")
        self.assertEqual(body["page"], 3)
        self.assertEqual(body["count"], 1)
        self.assertEqual(body["videos"][0]["id"], "ph1")
        self.assertEqual(seen, [("GET", "/video", {"params": {"page": 3}})])

    def test_list_videos_reports_a_changed_page_structure(self):
        client = self._client()
        self._patch_request(client, [_FakeResp(200, text="<html>改版了</html>")])

        with self.assertRaises(PornhubParseError) as ctx:
            asyncio.run(client.list_videos())

        self.assertEqual(ctx.exception.status, 502)
        self.assertIn("找不到", ctx.exception.message)

    def test_search_uses_the_dedicated_search_path(self):
        client = self._client()
        seen = self._patch_request(client, [_FakeResp(200, text=state_html(LIST_STATE))])

        body = asyncio.run(client.search("催眠", page=2))

        self.assertEqual(body["query"], "催眠")
        self.assertEqual(body["page"], 2)
        self.assertEqual(
            seen, [("GET", "/video/search", {"params": {"search": "催眠", "page": 2}})]
        )

    # ---------- 详情 ----------

    def test_video_uses_the_viewkey_query(self):
        client = self._client()
        seen = self._patch_request(client, [_FakeResp(200, text=state_html(VIDEO_STATE))])

        body = asyncio.run(client.video("ph60abc123"))

        self.assertEqual(body["viewkey"], "ph60abc123")
        self.assertEqual(
            seen, [("GET", "/view_video.php", {"params": {"viewkey": "ph60abc123"}})]
        )

    def test_a_video_page_without_the_video_block_is_a_parse_error(self):
        client = self._client()
        self._patch_request(client, [_FakeResp(200, text=state_html({"page": {}}))])

        with self.assertRaises(PornhubParseError) as ctx:
            asyncio.run(client.video("ph60abc123"))

        self.assertIn("缺失", ctx.exception.message)

    # ---------- 字节代转 ----------

    def _patch_stream_session(self, client, resp=None, error=None):
        session = MagicMock()
        session.get = AsyncMock(side_effect=error, return_value=resp)
        client._session = lambda host: session  # type: ignore[assignment]
        return session

    def test_open_stream_rejects_a_foreign_host_without_a_request(self):
        client = self._client()
        session = self._patch_stream_session(client, resp=_FakeStreamResp())

        for url in (
            "https://evil.example.com/x.mp4",
            "https://www.pornhub.com/x.mp4",  # 站点主机不是媒体主机
            "https://mph.pornhub.com.evil.com/x.mp4",
            "not a url",
        ):
            with self.assertRaises(PornhubError) as ctx:
                asyncio.run(client.open_stream(url))
            self.assertEqual(ctx.exception.status, 400, url)
            self.assertIn("只允许 Pornhub 媒体主机", ctx.exception.message)

        session.get.assert_not_awaited()

    def test_open_stream_returns_the_status_body_and_whitelisted_headers(self):
        client = self._client()
        self._patch_stream_session(client, resp=_FakeStreamResp())

        status, headers, body = asyncio.run(client.open_stream(MEDIA_URL))

        self.assertEqual(status, 206)
        self.assertEqual(headers["content-type"], "video/mp4")
        self.assertEqual(headers["content-length"], str(len(MP4_HEAD)))
        self.assertEqual(headers["accept-ranges"], "bytes")
        self.assertIn("etag", headers)
        # 白名单之外的头必须丢掉，别把上游的内部头透出去。
        self.assertNotIn("x-amz-request-id", headers)

        async def collect():
            return b"".join([chunk async for chunk in body])

        self.assertEqual(asyncio.run(collect()), MP4_HEAD)

    def test_open_stream_forwards_range_and_adds_a_referer(self):
        client = self._client()
        session = self._patch_stream_session(client, resp=_FakeStreamResp())

        asyncio.run(client.open_stream(MEDIA_URL, {"range": "bytes=0-7"}))

        _url, kwargs = session.get.await_args
        self.assertEqual(kwargs["headers"]["range"], "bytes=0-7")
        # 上游对 Referer 有要求：没传就补上站点首页。
        self.assertEqual(kwargs["headers"]["referer"], f"https://{SITE_HOST}/")
        self.assertIn("Mozilla", kwargs["headers"]["user-agent"])
        self.assertTrue(kwargs["stream"])

    def test_open_stream_surfaces_a_transport_failure(self):
        client = self._client()
        self._patch_stream_session(client, error=httpx.ConnectError("RST"))

        with self.assertRaises(PornhubError) as ctx:
            asyncio.run(client.open_stream(MEDIA_URL))

        self.assertEqual(ctx.exception.status, 502)
        self.assertIn("直链代转请求失败", ctx.exception.message)

    # ---------- 释放 ----------

    def test_aclose_closes_sessions_and_the_doh_client(self):
        client = self._client()
        session = MagicMock()
        session.close = AsyncMock()
        client._sessions[SITE_HOST] = session
        doh = MagicMock()
        doh.aclose = AsyncMock()
        client._doh_client = doh

        asyncio.run(client.aclose())

        session.close.assert_awaited()
        doh.aclose.assert_awaited()
        self.assertEqual(client._sessions, {})
        self.assertIsNone(client._doh_client)

    def test_aclose_swallows_a_broken_session(self):
        client = self._client()
        session = MagicMock()
        session.close = AsyncMock(side_effect=RuntimeError("already gone"))
        client._sessions[SITE_HOST] = session

        asyncio.run(client.aclose())  # 不该抛

        self.assertEqual(client._sessions, {})

    def test_aclose_is_idempotent(self):
        client = self._client()

        asyncio.run(client.aclose())
        asyncio.run(client.aclose())

        self.assertIsNone(client._doh_client)

    def test_errors_default_to_a_502_and_carry_a_reason(self):
        error = PornhubError("炸了")

        self.assertEqual(error.status, 502)
        self.assertEqual(error.reason, "pornhub_error")
        self.assertEqual(str(error), "炸了")


class _FakePornhubClient:
    """顶替真实客户端：记录调用，返回固定数据，从不联网。"""

    def __init__(self):
        self.calls = []
        self.closed = False
        self.ech = True
        self.error = None
        self.stream_error = None

    def _maybe_fail(self):
        if self.error is not None:
            raise self.error

    async def health(self):
        self.calls.append(("health",))
        self._maybe_fail()
        return {"status": "ok", "status_code": 200}

    async def list_videos(self, page=1, segment="video"):
        self.calls.append(("list_videos", page))
        self._maybe_fail()
        return {
            "segment": segment,
            "page": page,
            "count": 1,
            "videos": [{"id": "ph1", "title": "第一条"}],
        }

    async def search(self, query, page=1):
        self.calls.append(("search", query, page))
        self._maybe_fail()
        return {"query": query, "page": page, "count": 1, "videos": [{"id": "ph1"}]}

    async def video(self, viewkey):
        self.calls.append(("video", viewkey))
        self._maybe_fail()
        return {
            "id": viewkey,
            "viewkey": viewkey,
            "title": "测试视频",
            "mp4_url": "https://mph.pornhub.com/720.mp4",
            "download_url": "https://mpthc.phncdn.com/high.mp4",
        }

    async def open_stream(self, url, pass_through=None):
        self.calls.append(("open_stream", url, dict(pass_through or {})))
        if self.stream_error is not None:
            raise self.stream_error
        self._maybe_fail()

        async def body():
            yield MP4_HEAD

        return (
            206,
            {
                "content-type": "video/mp4",
                "content-length": str(len(MP4_HEAD)),
                "accept-ranges": "bytes",
                "etag": '"abc"',
            },
            body(),
        )

    async def aclose(self):
        self.closed = True


@unittest.skipUnless(HAVE_PORNHUB, SKIP_REASON)
class PornhubRouteTestCase(unittest.TestCase):
    """路由层：真实 app + 假客户端，验证形状与主机白名单。"""

    def _client(self):
        app = create_pornhub_app(make_settings())
        client = TestClient(app)
        client.__enter__()
        self.addCleanup(client.__exit__, None, None, None)
        self.fake = _FakePornhubClient()
        client.app.state.ph_client = self.fake
        return client

    # ---------- 探活 ----------

    def test_health_reports_ok(self):
        client = self._client()

        body = client.get("/api/pornhub/health").json()

        self.assertEqual(body["status"], "ok")
        self.assertEqual(self.fake.calls[0], ("health",))

    def test_health_surfaces_an_upstream_failure(self):
        client = self._client()
        self.fake.error = PornhubError("探不到", status=502)

        response = client.get("/api/pornhub/health")

        self.assertEqual(response.status_code, 502)
        self.assertEqual(response.json()["detail"], "探不到")

    # ---------- 列表与搜索 ----------

    def test_videos_returns_the_page(self):
        client = self._client()

        body = client.get("/api/pornhub/videos?page=1").json()

        self.assertEqual(body["count"], 1)
        self.assertEqual(body["videos"][0]["id"], "ph1")
        self.assertEqual(self.fake.calls[0], ("list_videos", 1))

    def test_videos_page_must_be_positive(self):
        client = self._client()

        self.assertEqual(client.get("/api/pornhub/videos?page=0").status_code, 422)
        self.assertEqual(client.get("/api/pornhub/videos?page=-1").status_code, 422)
        self.assertEqual(self.fake.calls, [], "参数不合法不该打到客户端")

    def test_search_requires_a_non_empty_query(self):
        client = self._client()

        self.assertEqual(client.get("/api/pornhub/search").status_code, 422)
        self.assertEqual(client.get("/api/pornhub/search?query=").status_code, 422)

    def test_search_passes_the_query_and_page_through(self):
        client = self._client()

        body = client.get("/api/pornhub/search?query=催眠&page=2").json()

        self.assertEqual(body["query"], "催眠")
        self.assertEqual(self.fake.calls[0], ("search", "催眠", 2))

    # ---------- 详情 ----------

    def test_video_returns_the_direct_links(self):
        client = self._client()

        body = client.get("/api/pornhub/video/ph60abc123").json()

        self.assertEqual(body["viewkey"], "ph60abc123")
        self.assertIn("mp4_url", body)
        self.assertEqual(self.fake.calls[0], ("video", "ph60abc123"))

    def test_an_upstream_failure_is_passed_through_with_its_status(self):
        client = self._client()
        self.fake.error = PornhubError("上游炸了", status=502)

        response = client.get("/api/pornhub/video/ph60abc123")

        self.assertEqual(response.status_code, 502)
        self.assertEqual(response.json()["detail"], "上游炸了")

    # ---------- 字节代转 ----------

    def test_stream_forwards_range_and_marks_private(self):
        client = self._client()

        response = client.get(
            "/api/pornhub/stream",
            params={"url": MEDIA_URL},
            headers={"Range": "bytes=0-7"},
        )

        self.assertEqual(response.status_code, 206)
        self.assertEqual(response.content, MP4_HEAD)
        self.assertEqual(response.headers["accept-ranges"], "bytes")
        self.assertIn("private", response.headers["cache-control"])
        self.assertEqual(self.fake.calls[-1][2]["range"], "bytes=0-7")

    def test_stream_forwards_if_range_too(self):
        client = self._client()

        client.get(
            "/api/pornhub/stream",
            params={"url": MEDIA_URL},
            headers={"If-Range": '"abc"'},
        )

        self.assertEqual(self.fake.calls[-1][2]["if-range"], '"abc"')

    def test_stream_requires_a_url(self):
        client = self._client()

        self.assertEqual(client.get("/api/pornhub/stream").status_code, 422)

    def test_stream_rejects_foreign_hosts(self):
        client = self._client()

        for url in (
            "https://evil.example.com/x.mp4",
            "https://www.pornhub.com/x.mp4",
            "https://mph.pornhub.com.evil.com/x.mp4",
            "not a url",
        ):
            response = client.get("/api/pornhub/stream", params={"url": url})
            self.assertEqual(response.status_code, 400, url)

        self.assertEqual(self.fake.calls, [], "外来主机不该打到客户端")

    def test_a_bad_upstream_status_becomes_an_error_not_a_body(self):
        client = self._client()

        async def gone(url, pass_through=None):
            async def body():
                yield b""

            return 404, {"content-type": "text/html"}, body()

        self.fake.open_stream = gone
        response = client.get("/api/pornhub/stream", params={"url": MEDIA_URL})

        self.assertEqual(response.status_code, 404)
        self.assertEqual(response.json()["detail"], "上游视频源不可用")

    def test_stream_surfaces_an_upstream_failure(self):
        client = self._client()
        self.fake.stream_error = PornhubError("上游挂了", status=502)

        response = client.get("/api/pornhub/stream", params={"url": MEDIA_URL})

        self.assertEqual(response.status_code, 502)
        self.assertEqual(response.json()["detail"], "上游挂了")

    def test_the_pornhub_page_is_not_part_of_the_api_project(self):
        # pornhub 接口项目只提供 JSON 接口，没有自己的页面扩展。
        client = self._client()

        self.assertEqual(client.get("/pornhub").status_code, 404)


@unittest.skipUnless(HAVE_PORNHUB, SKIP_REASON)
class PornhubEntryPointTestCase(unittest.TestCase):
    """入口与打包：路由集合、路由超集、依赖清单。"""

    def test_pornhub_routers_cover_the_video_api(self):
        self.assertEqual(len(PH_ROUTERS), 1)
        # nav_sections 是给外壳导航页（`/`）用的：pornhub 不改路由，而是把自己的
        # 接口加进那一页（见 pornhub/app.py 的 PH_NAV）。
        self.assertEqual(PH_STATE_KEYS, ("ph_client", "nav_sections"))
        self.assertEqual(
            sorted(ph_state_factory(make_settings())),
            ["nav_sections", "ph_client"],
        )

    def test_the_state_factory_builds_a_configured_client(self):
        state = ph_state_factory(PornhubSettings(rate_limit_rpm=10000, ech=False))
        client = state["ph_client"]
        self.addCleanup(lambda: asyncio.run(client.aclose()))

        self.assertIsInstance(client, PornhubClient)
        self.assertFalse(client.ech)

    def test_the_state_factory_hands_the_ip_pool_to_the_client(self):
        settings = PornhubSettings(
            rate_limit_rpm=10000, ech=True, ech_config=GOOD_ECH_B64, ech_ips="1.1.1.1,2.2.2.2"
        )

        client = ph_state_factory(settings)["ph_client"]
        self.addCleanup(lambda: asyncio.run(client.aclose()))

        self.assertEqual(client._ech_ips, ("1.1.1.1", "2.2.2.2"))
        self.assertEqual(client.ech_config, GOOD_ECH_B64)

    def test_pornhub_nav_lists_only_json_apis(self):
        for item in PH_NAV.items:
            self.assertTrue(item.path.startswith("/api/pornhub/"), item.path)

    def test_the_site_descriptor_is_shaped_like_the_others(self):
        self.assertEqual(SITE.name, "pornhub")
        self.assertEqual(SITE.title, "Pornhub Bridge")
        self.assertEqual(SITE.version, "1.0.0")
        self.assertEqual(SITE.nav_order, 4)
        self.assertEqual(SITE.directory, PROJECT_ROOT / "pornhub")
        self.assertEqual(SITE.settings_class, PornhubSettings)
        self.assertEqual(SITE.required_files, ("run.py", "requirements.txt"))
        # pornhub 匿名就够用：没有登录自检钩子。
        self.assertIsNone(SITE.login_check)

    def test_pornhub_adds_exactly_its_own_routes_to_the_shell(self):
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

        shell = paths(_create_bridge_app(make_settings()))
        full = paths(create_pornhub_app(make_settings()))
        extra = full - shell

        self.assertTrue(shell <= full, "pornhub 应用必须包含框架外壳的全部路由")
        self.assertEqual(
            extra,
            {
                "/api/pornhub/health",
                "/api/pornhub/videos",
                "/api/pornhub/search",
                "/api/pornhub/video/{viewkey}",
                "/api/pornhub/stream",
            },
        )

    def test_shell_and_pornhub_app_do_not_expose_other_sites_routes(self):
        # 四个接口项目同级：外壳里没有任何站点路由，pornhub 入口也只挂
        # `/api/pornhub/*`，Pixiv / iwara / asmr 的路由一条都不在。
        client = TestClient(create_pornhub_app(make_settings()))
        with client:
            for path in ("/api/ranking", "/api/iwara/videos", "/api/asmr/works"):
                self.assertEqual(client.get(path).status_code, 404, path)

    def test_pornhub_requirements_list_curl_cffi(self):
        requirements = PROJECT_ROOT / "pornhub" / "requirements.txt"
        text = requirements.read_text(encoding="utf-8")
        self.assertIn("curl_cffi", text)
        self.assertIn("cffi", text)

    def test_curl_cffi_is_not_a_hard_dependency_of_the_main_project(self):
        # 只跑 pixiv 的人不该被要求装这个编译包。
        root_requirements = PROJECT_ROOT / "requirements.txt"
        self.assertNotIn("curl_cffi", root_requirements.read_text(encoding="utf-8"))

    def test_the_package_init_lazily_exposes_the_app(self):
        # pornhub/__init__.py 不能模块级 import .app：那条链顶部就要 curl_cffi，
        # 会把「先装依赖再建应用」的自举堵死。
        text = (PROJECT_ROOT / "pornhub" / "__init__.py").read_text(encoding="utf-8")
        self.assertIn("def __getattr__", text)
        # 只允许它待在 `if TYPE_CHECKING:` 缩进块里；模块顶层出现就说明
        # import 链会在自举（先装依赖再建应用）之前被拉起。
        top_level = [ln for ln in text.splitlines() if ln and not ln[:1].isspace()]
        self.assertFalse(
            any(ln.startswith("from .app import") for ln in top_level),
            "pornhub/__init__.py 顶层不能 import .app",
        )

    def test_the_package_lazy_exports_the_documented_names(self):
        import pornhub

        self.assertEqual(
            sorted(pornhub.__all__), ["PH_ROUTERS", "create_app", "ph_state_factory"]
        )
        self.assertIs(pornhub.PH_ROUTERS, PH_ROUTERS)
        with self.assertRaises(AttributeError):
            pornhub.nope  # noqa: B018 - 访问未知名字必须抛 AttributeError

    def test_the_sites_registry_knows_about_pornhub(self):
        # 包 / 边界 / 测试三处共用这一份注册表，加站点不该漏在这里。
        import sys

        sys.path.insert(0, str(PROJECT_ROOT / "scripts"))
        try:
            import sites  # type: ignore[import-not-found]
        finally:
            sys.path.pop(0)

        info = sites.SITES_BY_NAME["pornhub"]
        self.assertEqual(info.directory, "pornhub")
        self.assertEqual(info.title, "Pornhub Bridge")
        self.assertIn("curl_cffi", info.extra_modules)
        self.assertEqual(info.requirements, "pornhub/requirements.txt")
        self.assertIn("pornhub", sites.SITE_ROUTE_PATHS)
        self.assertEqual(
            set(sites.SITE_ROUTE_PATHS["pornhub"]),
            {
                "/api/pornhub/health",
                "/api/pornhub/videos",
                "/api/pornhub/search",
                "/api/pornhub/video/{viewkey}",
                "/api/pornhub/stream",
            },
        )


@unittest.skipUnless(HAVE_PORNHUB, SKIP_REASON)
class PornhubEnvCheckTestCase(unittest.TestCase):
    """入口 ``--check`` 时多出来的那三项。

    它们跟主项目那五项打在同一张 ``[环境检查]`` 表里，但名字都不在
    ``FATAL_CHECKS`` 里 —— 装坏了/没网只该提示，永远不该拦下启动。
    """

    def setUp(self):
        self.directory = make_temp_dir("pornhub_envcheck")
        self.addCleanup(drop_temp_dir, self.directory)
        env = patch.dict(
            os.environ,
            {"PIXIV_DATA_DIR": str(self.directory), "PORNHUB_BRIDGE_ECH": ""},
            clear=False,
        )
        env.start()
        self.addCleanup(env.stop)

    def _settings(self, **kw):
        base = {"rate_limit_rpm": 10000, "data_dir": str(self.directory)}
        base.update(kw)
        return PornhubSettings(**base)

    def _fake_client(self, *, payload=None, error=None):
        """顶掉 ``pornhub.client.PornhubClient``，返回 (patch, instance)。"""
        cls = MagicMock()
        instance = cls.return_value
        instance.ech = True
        instance._live_ips = {"pornhub.com": "104.18.10.118"}

        async def _health():
            if error is not None:
                raise error
            return {"status": "ok", "status_code": 200}

        async def _request(method, path, **kw):
            if error is not None:
                raise error
            text = "ip=1.2.3.4\nsni=encrypted\nwarp=off\n"
            if payload is not None:
                text = payload
            return _FakeResp(200, text=text)

        instance.health = AsyncMock(side_effect=_health)
        instance.request = AsyncMock(side_effect=_request)
        instance.aclose = AsyncMock()
        return patch("pornhub.client.PornhubClient", cls), instance

    def test_extra_check_names_are_named_and_never_fatal(self):
        from pixiv_bridge.cli import FATAL_CHECKS

        from pornhub.envcheck import DEPS_NAME
        from pornhub.envcheck import ECH_NAME as ECH
        from pornhub.envcheck import NET_NAME as NET

        self.assertEqual(
            sorted((DEPS_NAME, NET, ECH)),
            ["pornhub ECH", "pornhub 依赖", "pornhub 网络"],
        )
        for name in (DEPS_NAME, NET, ECH):
            self.assertNotIn(name, FATAL_CHECKS)

    def test_deps_check_really_imports_curl_cffi(self):
        from pornhub.envcheck import deps_check

        result = deps_check()

        # 这台机器上确实装了 curl_cffi，否则本模块本来就被整片跳过。
        self.assertTrue(result.ok)
        self.assertIn("curl_cffi", result.detail)

    def test_extra_checks_returns_three_results_in_order(self):
        from pornhub.envcheck import DEPS_NAME, extra_checks

        patcher, _ = self._fake_client()
        with patcher, patch.object(ph_ech, "available", lambda: True):
            results = extra_checks(self._settings())

        self.assertEqual([r.name for r in results], [DEPS_NAME, NET_NAME, ECH_NAME])
        self.assertTrue(results[0].ok)

    def test_the_network_check_reports_the_http_status(self):
        from pornhub.envcheck import extra_checks

        patcher, instance = self._fake_client()
        with patcher, patch.object(ph_ech, "available", lambda: True):
            results = extra_checks(self._settings())

        net = [r for r in results if r.name == NET_NAME][0]
        self.assertTrue(net.ok)
        self.assertIn("www.pornhub.com 可达", net.detail)
        self.assertIn("200", net.detail)
        instance.aclose.assert_awaited()

    def test_the_network_failure_is_reported_not_raised(self):
        from pornhub.envcheck import extra_checks

        patcher, _ = self._fake_client(error=PornhubError("都连不上"))
        with patcher, patch.object(ph_ech, "available", lambda: True):
            results = extra_checks(self._settings())

        net = [r for r in results if r.name == NET_NAME][0]
        self.assertFalse(net.ok)
        self.assertIn("www.pornhub.com 不可达", net.detail)
        self.assertIn("都连不上", net.detail)

    def test_a_timeout_is_reported_with_the_seconds(self):
        from pornhub.envcheck import NETWORK_TIMEOUT, extra_checks

        async def never():
            await asyncio.sleep(3600)

        patcher, instance = self._fake_client()
        instance.health = AsyncMock(side_effect=never)
        with patcher, patch.object(ph_ech, "available", lambda: True), patch(
            "pornhub.envcheck.NETWORK_TIMEOUT", 0.01
        ):
            results = extra_checks(self._settings())

        net = [r for r in results if r.name == NET_NAME][0]
        self.assertFalse(net.ok)
        self.assertIn("无响应", net.detail)
        self.assertGreater(NETWORK_TIMEOUT, 0)

    def test_the_ech_check_is_skipped_when_the_switch_is_off(self):
        from pornhub.envcheck import extra_checks

        patcher, _ = self._fake_client()
        with patcher, patch.object(ph_ech, "available", lambda: True):
            results = extra_checks(self._settings(ech=False))

        self.assertEqual([r.name for r in results], ["pornhub 依赖", NET_NAME])

    def test_an_encrypted_sni_is_reported_as_ok(self):
        from pornhub.envcheck import extra_checks

        patcher, _ = self._fake_client()
        with patcher, patch.object(ph_ech, "available", lambda: True):
            results = extra_checks(self._settings())

        ech = [r for r in results if r.name == ECH_NAME][0]
        self.assertTrue(ech.ok)
        self.assertIn("SNI 已加密", ech.detail)
        self.assertIn("104.18.10.118", ech.detail)

    def test_a_plaintext_sni_is_reported_as_ech_not_working(self):
        from pornhub.envcheck import extra_checks

        patcher, _ = self._fake_client(payload="ip=1.2.3.4\nsni=plaintext\n")
        with patcher, patch.object(ph_ech, "available", lambda: True):
            results = extra_checks(self._settings())

        ech = [r for r in results if r.name == ECH_NAME][0]
        self.assertFalse(ech.ok)
        self.assertIn("明文", ech.detail)

    def test_a_missing_sni_line_is_reported(self):
        from pornhub.envcheck import extra_checks

        patcher, _ = self._fake_client(payload="ip=1.2.3.4\n")
        with patcher, patch.object(ph_ech, "available", lambda: True):
            results = extra_checks(self._settings())

        ech = [r for r in results if r.name == ECH_NAME][0]
        self.assertFalse(ech.ok)
        self.assertIn("取不到", ech.detail)

    def test_a_libcurl_without_ech_says_how_to_turn_it_off(self):
        from pornhub.envcheck import extra_checks

        patcher, _ = self._fake_client()
        with patcher, patch.object(ph_ech, "available", lambda: False):
            results = extra_checks(self._settings())

        ech = [r for r in results if r.name == ECH_NAME][0]
        self.assertFalse(ech.ok)
        self.assertIn("不支持 ECH", ech.detail)
        self.assertIn("PORNHUB_BRIDGE_ECH=0", ech.detail)

    def test_a_handshake_failure_is_reported_not_raised(self):
        from pornhub.envcheck import extra_checks

        patcher, _ = self._fake_client(error=PornhubError("握手炸了"))
        with patcher, patch.object(ph_ech, "available", lambda: True):
            results = extra_checks(self._settings())

        ech = [r for r in results if r.name == ECH_NAME][0]
        self.assertFalse(ech.ok)
        self.assertIn("握手失败", ech.detail)
        self.assertIn("PORNHUB_BRIDGE_ECH=0", ech.detail)

    def test_a_handshake_timeout_is_reported(self):
        from pornhub.envcheck import ECH_TIMEOUT, extra_checks

        async def never(method, path, **kw):
            await asyncio.sleep(3600)

        patcher, instance = self._fake_client()
        instance.request = AsyncMock(side_effect=never)
        with patcher, patch.object(ph_ech, "available", lambda: True), patch(
            "pornhub.envcheck.ECH_TIMEOUT", 0.01
        ):
            results = extra_checks(self._settings())

        ech = [r for r in results if r.name == ECH_NAME][0]
        self.assertFalse(ech.ok)
        self.assertIn("握手超过", ech.detail)
        self.assertGreater(ECH_TIMEOUT, 0)

    def test_an_unknown_failure_is_still_reported(self):
        from pornhub.envcheck import extra_checks

        patcher, instance = self._fake_client()
        instance.request = AsyncMock(side_effect=RuntimeError("怪东西"))
        with patcher, patch.object(ph_ech, "available", lambda: True):
            results = extra_checks(self._settings())

        ech = [r for r in results if r.name == ECH_NAME][0]
        self.assertFalse(ech.ok)
        self.assertIn("握手失败", ech.detail)
        self.assertIn("RuntimeError", ech.detail)


@unittest.skipUnless(HAVE_PORNHUB, SKIP_REASON)
class PornhubEntryWiringTestCase(unittest.TestCase):
    """入口脚本本身：自举要跑在 import pornhub.app 之前，并交给框架基座。"""

    def _source(self) -> str:
        return (PROJECT_ROOT / "pornhub" / "run.py").read_text(encoding="utf-8")

    def test_bootstrap_runs_before_importing_the_app(self):
        # pornhub.app → pornhub.client → import curl_cffi；这条链没装包时会
        # 直接 ImportError，必须排在 bootstrap 之后，否则自举根本没机会跑。
        # 只看真正的语句行：注释里也可能提到这两个名字。
        lines = [line.strip() for line in self._source().splitlines()]
        statements = [line for line in lines if line and not line.startswith("#")]
        bootstrap = next(
            i for i, line in enumerate(statements) if line.startswith("bootstrap(")
        )
        app_import = next(
            i
            for i, line in enumerate(statements)
            if line.startswith("from pornhub.app import")
        )
        self.assertLess(bootstrap, app_import)

    def test_entry_hands_the_site_to_the_framework_cli(self):
        text = self._source()
        # 入口本身不再拼参数：站点的检查钩子都挂在 `pornhub.app.SITE` 上，
        # 由框架基座 `pixiv_bridge.cli.main(site=...)` 统一取值。
        self.assertIn("from pornhub.app import SITE", text)
        self.assertIn("main(site=SITE)", text)
        self.assertIn("from pixiv_bridge.cli import main", text)

    def test_the_entry_puts_the_project_root_on_sys_path(self):
        # `python pornhub/run.py` 时 sys.path[0] 是 pornhub/，框架包在上一级。
        text = self._source()
        self.assertIn("sys.path.insert(0, str(HERE.parent))", text)

    def test_the_entry_bootstraps_curl_cffi(self):
        text = self._source()
        self.assertIn("curl_cffi", text)
        self.assertIn("requirements.txt", text)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
