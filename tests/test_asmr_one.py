"""asmr_one 子项目的单元测试。

与 ``tests/test_iwara.py`` 同一套约定，全部离线：纯函数直接断言，传输层把
``AsmrClient`` 的会话层打桩，路由用一个假客户端顶替
``app.state.asmr_client``，不打任何真实请求。

asmr_one 依赖 ``curl_cffi``（Cloudflare 只认浏览器 TLS 指纹，而且 asmr.one
自己的边缘 IP 在本机是 IP 级被 RST 的，必须借道别的 CF IP 再叠 ECH），而它只
写在 ``asmr_one/requirements.txt`` 里 —— 新克隆的仓库还没跑过
``asmr_one/run.py`` 时会缺这个包。所以整个模块在缺包时**整片跳过**，而不是让
测试红掉：装齐依赖（``pip install -r asmr_one/requirements.txt``）后才会真正
执行。
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

    from asmr_one import ech as asmr_ech
    from asmr_one.app import ASMR_NAV, ASMR_ROUTERS, ASMR_STATE_KEYS, SITE
    from asmr_one.app import asmr_state_factory
    from asmr_one.app import create_app as create_asmr_app
    from asmr_one.client import (
        API_HOST,
        COVER_TYPES,
        ECH_HOSTS,
        MEDIA_HOST,
        MEDIA_HOSTS,
        SEED_HOSTS,
        SEED_IPS,
        AsmrAuthError,
        AsmrChallengeError,
        AsmrClient,
        AsmrError,
        Track,
        cover_url,
        decorate_work,
        flatten_tracks,
        is_ech_host,
        is_media_host,
    )
    from asmr_one.config import AsmrSettings, as_asmr_settings, split_ips
    from asmr_one.envcheck import ECH_NAME, NET_NAME, SEARCH_NAME

    HAVE_ASMR = True
    ASMR_IMPORT_ERROR = ""
except ImportError as exc:  # pragma: no cover - 只在没装 curl_cffi 时走到
    HAVE_ASMR = False
    ASMR_IMPORT_ERROR = str(exc)

SKIP_REASON = "需要 curl_cffi：pip install -r asmr_one/requirements.txt"

# 一个有意义的 ECH 配置：前两字节是长度，所以是「合法」的 ECHConfigList。
GOOD_ECH_B64 = base64.b64encode(b"\x00\x06hello!").decode("ascii")


def make_settings():
    # asmr 站点只在框架 Settings 上加了 ECH 三项；这里给一个纯框架 Settings，
    # 让 as_asmr_settings 去补默认值（正常入口走 AsmrSettings.load）。
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


class _FakeResp:
    """``request()`` / ``get_json()`` 只会用到这几个属性。"""

    def __init__(self, status_code=200, payload=None, headers=None, text="", content=b""):
        self.status_code = status_code
        self._payload = payload if payload is not None else {}
        self.headers = headers or {}
        self.text = text or json.dumps(self._payload)
        self.content = content

    def json(self):
        return self._payload

    async def aiter_content(self):
        yield self.content


# 真实形状（字段名与线上一致）：顶层就是 list，folder 套 folder，叶子是 audio。
TRACK_TREE = [
    {
        "type": "folder",
        "title": "04：omake",
        "children": [
            {
                "type": "folder",
                "title": "03：撮り下ろし",
                "children": [
                    {
                        "type": "audio",
                        "hash": "aaaaaaaaaaaaaaaa",
                        "title": "track_01.mp3",
                        "work": {"id": 1669340},
                        "workTitle": "测试作品",
                        "mediaStreamUrl": "https://raw.kiko-play-niptan.one/media/a.mp3",
                        "mediaDownloadUrl": "https://raw.kiko-play-niptan.one/media/a_dl.mp3",
                        "duration": 123.5,
                        "size": 4096,
                    }
                ],
            }
        ],
    },
    {
        "type": "audio",
        "hash": "bbbbbbbbbbbbbbbb",
        "title": "track_02.mp3",
        "work": {"id": 1669340},
        "workTitle": "测试作品",
        "mediaStreamUrl": "https://raw.kiko-play-niptan.one/media/b.mp3",
        "mediaDownloadUrl": "",
        "duration": 60,
        "size": 2048,
    },
]

WORK = {
    "id": 1669340,
    "title": "测试作品",
    "mainCoverUrl": "https://api.asmr.one/api/cover/1669340.jpg?type=main",
    "samCoverUrl": "https://api.asmr.one/api/cover/1669340.jpg?type=sam",
    "thumbnailCoverUrl": "https://api.asmr.one/api/cover/1669340.jpg?type=240x240",
}

PAGE = {
    "pagination": {"currentPage": 1, "pageSize": 20, "totalCount": 62611},
    "works": [dict(WORK)],
}

MEDIA_URL = "https://raw.kiko-play-niptan.one/media/a.mp3"
# 12 字节里含 ftyp，被 probe_range 判成媒体。
MP4_HEAD = b"\x00\x00\x00 ftypisom"


class _FakeAsmrClient:
    """顶替真实客户端：记录调用，返回固定数据，从不联网。"""

    def __init__(self):
        self.calls = []
        self.closed = False
        self.ech = True
        self.error = None
        self.probe_error = None

    def _maybe_fail(self):
        if self.error is not None:
            raise self.error

    async def list_works(self, page=1):
        self.calls.append(("list_works", page))
        self._maybe_fail()
        return json.loads(json.dumps(PAGE))

    async def search(self, keyword, page=1):
        self.calls.append(("search", keyword, page))
        self._maybe_fail()
        return json.loads(json.dumps(PAGE))

    async def work(self, work_id):
        self.calls.append(("work", work_id))
        self._maybe_fail()
        # 上游不管路径里传什么，回来的一定是它自己的数字 id + 三个封面地址。
        return dict(WORK)

    async def tracks(self, work_id):
        self.calls.append(("tracks", work_id))
        self._maybe_fail()
        return flatten_tracks(TRACK_TREE)

    async def fetch_cover(self, work_id, cover_type="main"):
        self.calls.append(("fetch_cover", work_id, cover_type))
        self._maybe_fail()
        return b"RIFFWEBP", "image/webp"

    async def open_stream(self, url, headers=None):
        self.calls.append(("open_stream", url, dict(headers or {})))
        self._maybe_fail()
        if self.probe_error:
            raise self.probe_error

        async def body():
            yield MP4_HEAD

        return (
            206,
            {
                "content-type": "audio/mp4",
                "content-range": "bytes 0-4095/53874359",
                "content-length": "4096",
            },
            body(),
        )

    async def auth_me(self):
        self.calls.append(("auth_me",))
        self._maybe_fail()
        return {"user": {"loggedIn": False}, "auth": True, "reg": True}

    async def aclose(self):
        self.closed = True


@unittest.skipUnless(HAVE_ASMR, SKIP_REASON)
class AsmrEchConfigTestCase(unittest.TestCase):
    """``asmr_one/ech.py``：ECH 配置的解析、缓存与取值。

    它是 asmr 版的自足实现（站点之间不能互相 import，见
    ``scripts/check_boundary.py``），走的路线和 iwara 一样：只把配置交给
    libcurl 的 ``CURLOPT_ECH``，不需要任何动态库。所以这里的用例全是纯逻辑。
    """

    def setUp(self):
        # 缓存是模块级的，谁先跑都会污染别人；每个用例都从干净状态开始。
        self.addCleanup(asmr_ech.reset_config_cache)

    def test_the_bundled_config_is_a_usable_echconfiglist(self):
        raw = asmr_ech._decode_config(asmr_ech.DEFAULT_ECH_CONFIG)

        self.assertGreaterEqual(len(raw), 4)
        # ECHConfigList 的前两个字节是大端长度，必须覆盖整段，否则 libcurl
        # 会当成畸形配置丢掉。
        self.assertEqual(int.from_bytes(raw[:2], "big"), len(raw) - 2)
        # 内置配置里的 public_name 就是外层 SNI 用的那个名字。
        self.assertIn(b"cloudflare-ech.com", raw)

    def test_a_too_short_config_is_rejected(self):
        with self.assertRaises(asmr_ech.EchUnavailable) as ctx:
            asmr_ech._decode_config(base64.b64encode(b"\x01\x02").decode("ascii"))

        self.assertIn("过短", str(ctx.exception))

    def test_garbage_is_rejected_with_a_readable_reason(self):
        with self.assertRaises(asmr_ech.EchUnavailable) as ctx:
            asmr_ech._decode_config("not base64 at all!!")

        self.assertIn("base64", str(ctx.exception))

    def test_pem_wrapping_and_whitespace_are_tolerated(self):
        wrapped = "-----BEGIN ECH-----\n" + asmr_ech.DEFAULT_ECH_CONFIG + "\n-----END ECH-----"

        self.assertEqual(
            asmr_ech._decode_config(wrapped),
            asmr_ech._decode_config(asmr_ech.DEFAULT_ECH_CONFIG),
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
            asmr_ech._ech_from_doh(payload), asmr_ech._decode_config(good)
        )

    def test_the_doh_regex_handles_the_escaped_quote(self):
        # DoH 的 JSON 里引号被转义成 \"：正则不许它，就永远退回内置配置。
        good = base64.b64encode(b"\x00\x04abcd").decode("ascii")
        for data in (f'ech=\\"{good}\\"', f'ech="{good}"', f"ech={good}"):
            self.assertRegex(data, asmr_ech._ECH_VALUE_RE, data)

    def test_a_payload_without_any_ech_record_yields_nothing(self):
        self.assertIsNone(asmr_ech._ech_from_doh({"Answer": [{"type": 65, "data": 'alpn="h2"'}]}))
        self.assertIsNone(asmr_ech._ech_from_doh({}))
        self.assertIsNone(asmr_ech._ech_from_doh(None))

    def test_a_broken_record_falls_through_to_the_next_answer(self):
        good = base64.b64encode(b"\x00\x04abcd").decode("ascii")
        payload = {
            "Answer": [
                {"type": 65, "data": 'ech="!!!!"'},  # 非法 base64，跳过
                {"type": 65, "data": f'ech="{good}"'},
            ]
        }

        self.assertEqual(
            asmr_ech._ech_from_doh(payload), asmr_ech._decode_config(good)
        )

    def test_fetch_tries_every_endpoint_before_giving_up(self):
        seen = []

        def boom(url, **_kwargs):
            seen.append(url)
            raise httpx.ConnectError("no route")

        with patch.object(asmr_ech.httpx, "get", boom):
            with self.assertRaises(asmr_ech.EchUnavailable) as ctx:
                asmr_ech.fetch_ech_config(endpoints=("https://a/resolve", "https://b/resolve"))

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

        with patch.object(asmr_ech.httpx, "get", fake_get):
            found = asmr_ech.fetch_ech_config(endpoints=("https://a/resolve",))

        self.assertEqual(found, asmr_ech._decode_config(GOOD_ECH_B64))
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

        with patch.object(asmr_ech.httpx, "get", fake_get):
            asmr_ech.fetch_ech_config()

        self.assertEqual(seen, [asmr_ech.DOH_ENDPOINTS[0]])
        self.assertIn("alidns", asmr_ech.DOH_ENDPOINTS[0])
        self.assertIn("223.5.5.5", asmr_ech.DOH_ENDPOINTS[1])

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

        with patch.object(asmr_ech.httpx, "get", fake_get):
            found = asmr_ech.fetch_ech_config(endpoints=("https://a/resolve", "https://b/resolve"))

        self.assertEqual(found, asmr_ech._decode_config(GOOD_ECH_B64))

    def test_an_explicit_override_beats_everything(self):
        self.assertEqual(
            asmr_ech.ech_config(override=GOOD_ECH_B64),
            asmr_ech._decode_config(GOOD_ECH_B64),
        )

    def test_a_bad_override_is_reported_not_silently_ignored(self):
        with self.assertRaises(asmr_ech.EchUnavailable):
            asmr_ech.ech_config(override="!!!")

    def test_the_config_is_cached_until_it_is_reset(self):
        calls = []

        def fake_get(url, **_kwargs):
            calls.append(url)
            return MagicMock(
                json=lambda: {"Answer": [{"type": 65, "data": 'ech="%s"' % GOOD_ECH_B64}]},
                raise_for_status=lambda: None,
            )

        with patch.object(asmr_ech.httpx, "get", fake_get):
            first = asmr_ech.ech_config()
            second = asmr_ech.ech_config()

        self.assertEqual(first, second)
        self.assertEqual(len(calls), 1, "缓存期内不该再打一次 DoH")

    def test_the_cache_ttl_is_bounded(self):
        # Cloudflare 轮换 ECH 密钥是小时级，缓存半小时足够。
        self.assertGreater(asmr_ech.ECH_CONFIG_TTL, 0)
        self.assertLessEqual(asmr_ech.ECH_CONFIG_TTL, 6 * 3600)

    def test_everything_falls_back_to_the_bundled_config(self):
        # DoH 全挂时必须用内置常量兜底，而不是让整个站点起不来。
        def boom(url, **_kwargs):
            raise httpx.ConnectError("no route")

        with patch.object(asmr_ech.httpx, "get", boom):
            config = asmr_ech.ech_config(refresh=True)

        self.assertEqual(config, asmr_ech._decode_config(asmr_ech.DEFAULT_ECH_CONFIG))

    def test_the_option_is_the_ecl_prefix_plus_base64(self):
        # 只有 ecl: 才是「带上这份配置」；true/grease 只是开关或填充。
        with patch.object(asmr_ech, "ech_config", lambda *_a, **_k: b"\x00\x04abcd"):
            option = asmr_ech.ech_option()

        self.assertEqual(option, "ecl:" + base64.b64encode(b"\x00\x04abcd").decode("ascii"))
        self.assertTrue(option.startswith(asmr_ech.ECH_OPTION_PREFIX))

    def test_the_option_is_none_when_libcurl_has_no_ech(self):
        with patch.object(asmr_ech, "available", lambda: False):
            self.assertIsNone(asmr_ech.ech_option(raw=b"\x00\x04abcd"))

    def test_the_option_is_none_for_an_unusable_config(self):
        with patch.object(asmr_ech, "ech_config", side_effect=asmr_ech.EchUnavailable("挂了")):
            self.assertIsNone(asmr_ech.ech_option(override="!!!"))

    def test_the_option_is_none_for_an_empty_config(self):
        self.assertIsNone(asmr_ech.ech_option(raw=b""))


@unittest.skipUnless(HAVE_ASMR, SKIP_REASON)
class AsmrSettingsTestCase(unittest.TestCase):
    """``asmr_one/config.py``：IP 串切分与 ECH 三个开关的默认值。

    ``ASMR_BRIDGE_ECH`` 与 iwara 的 ``IWARA_BRIDGE_ECH`` 不一样：**不写也是
    开着的**（asmr.one 离开 ECH 就通不了），要关必须显式写 0。
    """

    def setUp(self):
        # ``Settings.load`` 会把 .env 灌进 os.environ，也会去碰数据目录；
        # 这里一律指到一个空目录，并断言式地清掉站点开关。
        self.directory = make_temp_dir("asmr_settings")
        self.addCleanup(drop_temp_dir, self.directory)
        env = patch.dict(
            os.environ,
            {
                "PIXIV_DATA_DIR": str(self.directory),
                "ASMR_BRIDGE_ECH": "",
                "ASMR_BRIDGE_ECH_CONFIG": "",
                "ASMR_BRIDGE_ECH_IPS": "",
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
        settings = AsmrSettings.load(migrate=False)

        self.assertTrue(settings.ech, "asmr.one 离开 ECH 就通不了，默认必须开")
        self.assertEqual(settings.ech_config, "")
        self.assertEqual(settings.ech_ips, "")

    def test_an_explicit_zero_turns_ech_off(self):
        os.environ["ASMR_BRIDGE_ECH"] = "0"

        self.assertFalse(AsmrSettings.load(migrate=False).ech)

    def test_the_other_falsy_spellings_also_turn_it_off(self):
        for raw in ("0", "false", "no", "off", "FALSE"):
            os.environ["ASMR_BRIDGE_ECH"] = raw
            self.assertFalse(AsmrSettings.load(migrate=False).ech, raw)

    def test_the_truthy_spellings_keep_it_on(self):
        for raw in ("1", "true", "yes", "on"):
            os.environ["ASMR_BRIDGE_ECH"] = raw
            self.assertTrue(AsmrSettings.load(migrate=False).ech, raw)

    def test_the_override_and_ip_pool_are_read(self):
        os.environ["ASMR_BRIDGE_ECH_CONFIG"] = GOOD_ECH_B64
        os.environ["ASMR_BRIDGE_ECH_IPS"] = "1.1.1.1 2.2.2.2"

        settings = AsmrSettings.load(migrate=False)

        self.assertEqual(settings.ech_config, GOOD_ECH_B64)
        self.assertEqual(settings.ech_ips, "1.1.1.1 2.2.2.2")
        self.assertEqual(split_ips(settings.ech_ips), ("1.1.1.1", "2.2.2.2"))

    def test_the_site_variables_do_not_answer_to_the_pixiv_names(self):
        # 三个站点同级，各自的开关互不影响：ASMR_ 前缀之外的变量不该被读。
        os.environ["PIXIV_BRIDGE_ECH"] = "0"

        self.assertTrue(AsmrSettings.load(migrate=False).ech)

    def test_a_plain_framework_settings_is_upgraded_with_defaults(self):
        upgraded = as_asmr_settings(make_settings())

        self.assertIsInstance(upgraded, AsmrSettings)
        self.assertTrue(upgraded.ech)
        self.assertEqual(upgraded.ech_config, "")
        self.assertEqual(upgraded.rate_limit_rpm, 10000)

    def test_an_asmr_settings_passes_through_unchanged(self):
        original = AsmrSettings(rate_limit_rpm=10000, ech=False)

        self.assertIs(as_asmr_settings(original), original)
        self.assertFalse(as_asmr_settings(original).ech)


@unittest.skipUnless(HAVE_ASMR, SKIP_REASON)
class AsmrClientLogicTestCase(unittest.TestCase):
    """不联网的纯逻辑：音轨树的展平、封面地址、主机白名单。"""

    def test_the_tree_is_flattened_in_order_with_folder_paths(self):
        tracks = flatten_tracks(TRACK_TREE)

        self.assertEqual([t.title for t in tracks], ["track_01.mp3", "track_02.mp3"])
        self.assertEqual(tracks[0].folders, ("04：omake", "03：撮り下ろし"))
        self.assertEqual(tracks[1].folders, ())
        self.assertEqual(tracks[0].hash, "aaaaaaaaaaaaaaaa")
        self.assertEqual(tracks[0].work_id, "1669340")
        self.assertEqual(tracks[0].work_title, "测试作品")
        self.assertEqual(tracks[0].duration, 123.5)
        self.assertEqual(tracks[0].size, 4096)

    def test_non_mapping_nodes_are_skipped(self):
        self.assertEqual(flatten_tracks([None, "x", 3, {"type": "audio", "hash": "h"}])[0].hash, "h")

    def test_an_unknown_node_type_is_not_a_leaf(self):
        self.assertEqual(flatten_tracks([{"type": "video", "hash": "h"}]), [])
        self.assertEqual(flatten_tracks(None), [])

    def test_a_folder_without_children_is_empty(self):
        self.assertEqual(flatten_tracks([{"type": "folder", "title": "空"}]), [])

    def test_track_from_node_tolerates_missing_fields(self):
        track = Track.from_node({"hash": None})

        self.assertEqual(track.hash, "")
        self.assertEqual(track.title, "")
        self.assertEqual(track.work_id, "")
        self.assertEqual(track.duration, 0.0)
        self.assertEqual(track.size, 0)
        self.assertEqual(track.folders, ())

    def test_track_as_dict_is_the_nine_wire_fields(self):
        payload = flatten_tracks(TRACK_TREE)[0].as_dict()

        self.assertEqual(
            sorted(payload),
            [
                "download_url",
                "duration",
                "folders",
                "hash",
                "size",
                "stream_url",
                "title",
                "work_id",
                "work_title",
            ],
        )
        self.assertEqual(payload["folders"], ["04：omake", "03：撮り下ろし"])
        self.assertNotIn("raw", payload)

    def test_cover_url_points_at_the_upstream_api(self):
        self.assertEqual(
            cover_url("1669340"),
            "https://api.asmr.one/api/cover/1669340.jpg?type=main",
        )
        self.assertEqual(cover_url("1669340", "240x240").endswith("type=240x240"), True)

    def test_the_cover_types_match_what_the_upstream_accepts(self):
        # 实测 type=thumb 是 400，缩略图必须用 240x240。
        self.assertEqual(COVER_TYPES, ("main", "sam", "240x240"))

    def test_decorate_work_adds_the_local_proxy_urls(self):
        decorated = decorate_work(dict(WORK))

        self.assertEqual(decorated["cover_url"], "/api/asmr/cover/1669340?type=main")
        self.assertEqual(decorated["thumbnail_url"], "/api/asmr/cover/1669340?type=240x240")
        # 上游字段原样保留（列表页还要用 title 等）。
        self.assertEqual(decorated["title"], "测试作品")

    def test_decorate_work_passes_through_non_works(self):
        self.assertIsNone(decorate_work(None))
        self.assertEqual(decorate_work({"no_id": 1}), {"no_id": 1})
        self.assertEqual(decorate_work("x"), "x")

    def test_is_ech_host_covers_every_cloudflare_host(self):
        for host in ECH_HOSTS:
            self.assertTrue(is_ech_host(host), host)
        self.assertFalse(is_ech_host("example.com"))

    def test_is_media_host_is_only_the_media_domain(self):
        self.assertTrue(is_media_host(MEDIA_HOST))
        self.assertEqual(MEDIA_HOSTS, (MEDIA_HOST,))
        self.assertFalse(is_media_host("evil.example.com"))
        self.assertFalse(is_media_host(API_HOST))


@unittest.skipUnless(HAVE_ASMR, SKIP_REASON)
class AsmrTransportTestCase(unittest.TestCase):
    """传输层：借道 IP 池、单个 IP 固定、ECH 开关与换 IP 重试。

    关键结论都来自实测：asmr.one 自己的边缘 IP 在本机被 RST，能通的是借道
    别的 Cloudflare anycast IP 再叠 ECH。所以这里不打任何真实请求，只验证
    这条链路的形状。
    """

    def _client(self, **kw) -> "AsmrClient":
        client = AsmrClient(**kw)
        self.addCleanup(lambda: asyncio.run(client.aclose()))
        return client

    def _patch_doh(self, client, mapping=None):
        async def fake(endpoint, host):
            return list((mapping or {}).get(host, ()))

        client._query_doh = fake  # type: ignore[assignment]
        return client

    def test_the_seed_pool_covers_every_service_host(self):
        for host in ECH_HOSTS:
            self.assertIn(host, SEED_HOSTS, host)
            self.assertTrue(SEED_HOSTS[host])

    def test_the_seed_pool_does_not_start_with_asrms_own_dead_ips(self):
        # asmr.one 自己解析到的 104.26.4.171 / 172.67.70.244 在本机一律 RST，
        # 绝不能排在候选最前面（这里只检查它们没有被「优先」使用）。
        preferred = SEED_HOSTS[API_HOST]
        self.assertNotIn("104.26.4.171", preferred)
        self.assertNotIn("172.67.70.244", preferred)

    def test_resolve_host_orders_seeds_before_configured_and_doh_ips(self):
        client = self._patch_doh(self._client(), {API_HOST: ["9.9.9.9"]})
        client.ech_ips = ("8.8.8.8",)

        ips = asyncio.run(client.resolve_host(API_HOST))

        self.assertEqual(ips[0], SEED_HOSTS[API_HOST][0])
        self.assertIn("8.8.8.8", ips)
        self.assertIn("9.9.9.9", ips)
        self.assertLess(ips.index("8.8.8.8"), ips.index("9.9.9.9"))

    def test_configured_ech_ips_are_ignored_for_a_non_ech_host(self):
        client = self._patch_doh(self._client(), {})
        client.ech_ips = ("8.8.8.8",)

        ips = asyncio.run(client.resolve_host("example.com"))

        self.assertNotIn("8.8.8.8", ips)
        # 未登记的主机拿整个借道池。
        self.assertEqual(ips[: len(SEED_IPS)], list(SEED_IPS))

    def test_resolve_host_caches_its_answer(self):
        client = self._patch_doh(self._client(), {API_HOST: ["9.9.9.9"]})

        first = asyncio.run(client.resolve_host(API_HOST))
        second = asyncio.run(client.resolve_host(API_HOST))

        self.assertIs(first, second)

    def test_resolve_host_without_any_candidate_is_a_502(self):
        client = self._client(doh_endpoints=())
        client.ech_ips = ()
        # 未登记的主机会退到整个借道池，所以池子也得清空才是「一个候选都没有」。
        with patch.dict(SEED_HOSTS, {}, clear=True), patch("asmr_one.client.SEED_IPS", ()):
            with self.assertRaises(AsmrError) as ctx:
                asyncio.run(client.resolve_host("nowhere.example"))

        self.assertEqual(ctx.exception.status, 502)
        self.assertIn("候选地址", ctx.exception.message)

    def test_pick_ip_skips_unreachable_candidates(self):
        client = self._patch_doh(self._client(), {})
        seen = []

        async def fake_reachable(ip, port=443):
            seen.append(ip)
            return ip == SEED_IPS[1]

        client._tcp_reachable = fake_reachable  # type: ignore[assignment]

        picked = asyncio.run(client.pick_ip("api.example.com"))

        self.assertEqual(picked, SEED_IPS[1])
        self.assertEqual(seen[:2], [SEED_IPS[0], SEED_IPS[1]])

    def test_pick_ip_binds_to_a_single_ip(self):
        # CurlOpt.RESOLVE 不是候选列表：多个 IP 只有最后一条生效，
        # 所以必须自己探测完再固定一个。
        client = self._patch_doh(self._client(), {})

        async def always(ip, port=443):
            return True

        client._tcp_reachable = always  # type: ignore[assignment]

        picked = asyncio.run(client.pick_ip("api.example.com"))

        self.assertIsInstance(picked, str)
        self.assertEqual(asyncio.run(client.pick_ip("api.example.com")), picked)

    def test_pick_ip_raises_when_nothing_is_reachable(self):
        client = self._patch_doh(self._client(), {})

        async def never(ip, port=443):
            return False

        client._tcp_reachable = never  # type: ignore[assignment]

        with self.assertRaises(AsmrError) as ctx:
            asyncio.run(client.pick_ip("api.example.com"))

        self.assertEqual(ctx.exception.status, 502)
        self.assertIn("都连不上", ctx.exception.message)

    def test_session_for_pins_exactly_one_ip(self):
        client = self._patch_doh(self._client(), {})

        async def always(ip, port=443):
            return True

        client._tcp_reachable = always  # type: ignore[assignment]
        captured = {}

        def fake_session(**kwargs):
            captured.update(kwargs)
            return MagicMock()

        with patch("asmr_one.client.curl_requests.AsyncSession", fake_session):
            asyncio.run(client.session_for(API_HOST))

        options = captured["curl_options"]
        pinned = options[CurlOpt.RESOLVE]
        self.assertEqual(len(pinned), 1, "RESOLVE 只能是单个 IP 的映射")
        host, _port, ip = pinned[0].split(":")
        self.assertEqual((host, _port), (API_HOST, "443"))
        self.assertEqual(ip, client.pinned_ip(API_HOST))
        # 只固定 IP，不碰 Host/SNI：SNI 仍按真实域名走。
        self.assertEqual(client.pinned_ip(API_HOST), pinned[0].split(":")[2])

    def test_session_for_reuses_the_same_session(self):
        client = self._patch_doh(self._client(), {})

        async def always(ip, port=443):
            return True

        client._tcp_reachable = always  # type: ignore[assignment]
        sessions = []

        def fake_session(**kwargs):
            sessions.append(kwargs)
            return MagicMock()

        with patch("asmr_one.client.curl_requests.AsyncSession", fake_session):
            first = asyncio.run(client.session_for(API_HOST))
            second = asyncio.run(client.session_for(API_HOST))

        self.assertIs(first, second)
        self.assertEqual(len(sessions), 1)

    def test_mark_ip_dead_clears_the_pin_and_the_session(self):
        client = self._patch_doh(self._client(), {})

        async def always(ip, port=443):
            return True

        client._tcp_reachable = always  # type: ignore[assignment]
        with patch("asmr_one.client.curl_requests.AsyncSession", lambda **kw: MagicMock()):
            asyncio.run(client.session_for(API_HOST))
        ip = client.pinned_ip(API_HOST)
        self.assertIsNotNone(ip)

        asyncio.run(client.mark_ip_dead(API_HOST, ip))

        self.assertIsNone(client.pinned_ip(API_HOST))
        self.assertNotIn(API_HOST, client._sessions)
        self.assertIn(ip, client._dead_ips[API_HOST])

    def test_ech_option_is_off_when_the_switch_is_off(self):
        client = self._client(ech=False)

        self.assertIsNone(client.ech_option(API_HOST))

    def test_ech_option_is_off_for_a_non_cloudflare_host(self):
        client = self._client(ech=True)

        with patch.object(asmr_ech, "available", lambda: True):
            self.assertIsNone(client.ech_option("example.com"))

    def test_ech_option_is_off_when_libcurl_lacks_ech(self):
        client = self._client(ech=True)

        with patch.object(asmr_ech, "available", lambda: False):
            self.assertIsNone(client.ech_option(API_HOST))

    def test_ech_option_passes_the_configured_override_through(self):
        client = self._client(ech=True, ech_config=GOOD_ECH_B64)

        with patch.object(asmr_ech, "available", lambda: True):
            option = client.ech_option(API_HOST)

        self.assertEqual(option, "ecl:" + GOOD_ECH_B64)

    def test_headers_carry_browser_identity_and_the_site_origin(self):
        client = self._client()
        headers = client.headers({"x-extra": "1"})

        self.assertIn("Mozilla/5.0", headers["user-agent"])
        self.assertEqual(headers["origin"], "https://asmr.one")
        self.assertEqual(headers["referer"], "https://asmr.one/")
        self.assertEqual(headers["x-extra"], "1")

    def test_call_with_ip_retry_swaps_the_ip_after_a_connect_failure(self):
        client = self._client(ip_attempts=3)
        dead = []

        async def fake_mark(host, ip):
            dead.append(ip)

        async def fake_session(host):
            return MagicMock(ip=host)

        client.mark_ip_dead = fake_mark  # type: ignore[assignment]
        client.session_for = fake_session  # type: ignore[assignment]
        client._live_ips[API_HOST] = "1.1.1.1"

        attempts = []

        async def call(session):
            attempts.append(1)
            if len(attempts) < 2:
                raise httpx.ConnectError("RST")
            return _FakeResp(payload={"ok": True})

        resp = asyncio.run(client._call_with_ip_retry(API_HOST, call))

        self.assertEqual(resp.status_code, 200)
        self.assertEqual(len(attempts), 2)
        self.assertEqual(dead, ["1.1.1.1"])

    def test_call_with_ip_retry_retries_a_404_only_when_told_to(self):
        client = self._client(ip_attempts=2)
        swapped = []

        async def fake_mark(host, ip):
            swapped.append(ip)

        async def fake_session(host):
            return MagicMock()

        client.mark_ip_dead = fake_mark  # type: ignore[assignment]
        client.session_for = fake_session  # type: ignore[assignment]
        client._live_ips[API_HOST] = "1.1.1.1"

        attempts = []

        async def call(session):
            attempts.append(1)
            return _FakeResp(404, text="missing")

        # 内容主机的 404 表示「这个边缘没有这份内容」，换一个可能有。
        resp = asyncio.run(client._call_with_ip_retry(API_HOST, call, retry_statuses=(404,)))

        self.assertEqual(resp.status_code, 404)
        self.assertEqual(len(attempts), 2)
        self.assertEqual(swapped, ["1.1.1.1"])

    def test_call_with_ip_retry_does_not_retry_a_404_by_default(self):
        client = self._client(ip_attempts=3)
        client._live_ips[API_HOST] = "1.1.1.1"

        async def fake_session(host):
            return MagicMock()

        client.session_for = fake_session  # type: ignore[assignment]
        attempts = []

        async def call(session):
            attempts.append(1)
            return _FakeResp(404, text="missing")

        resp = asyncio.run(client._call_with_ip_retry(API_HOST, call))

        self.assertEqual(resp.status_code, 404)
        self.assertEqual(len(attempts), 1, "API 的 404 是真实业务状态，换 IP 也没用")

    def test_request_wraps_transport_failures_into_asmr_error(self):
        client = self._client()

        async def boom(host, call, **_kw):
            raise httpx.ConnectError("RST")

        client._call_with_ip_retry = boom  # type: ignore[assignment]

        with self.assertRaises(AsmrError) as ctx:
            asyncio.run(client.request("GET", "/api/works"))

        self.assertEqual(ctx.exception.status, 502)
        self.assertIn("失败", ctx.exception.message)

    def test_request_flags_a_cloudflare_challenge(self):
        client = self._client()

        async def challenge(host, call, **_kw):
            return _FakeResp(403, headers={"cf-mitigated": "challenge"}, text="nope")

        client._call_with_ip_retry = challenge  # type: ignore[assignment]

        with self.assertRaises(AsmrChallengeError) as ctx:
            asyncio.run(client.request("GET", "/api/works"))

        self.assertEqual(ctx.exception.reason, "cloudflare_challenge")

    def test_request_flags_a_403_html_page_as_a_challenge(self):
        client = self._client()

        async def challenge(host, call, **_kw):
            return _FakeResp(
                403, headers={"content-type": "text/html; charset=utf-8"}, text="<html>"
            )

        client._call_with_ip_retry = challenge  # type: ignore[assignment]

        with self.assertRaises(AsmrChallengeError):
            asyncio.run(client.request("GET", "/api/works"))

    def test_request_leaves_a_plain_403_alone(self):
        # JSON 的 403 是上游的业务回答，不是 Cloudflare 挑战。
        client = self._client()

        async def plain(host, call, **_kw):
            return _FakeResp(403, headers={"content-type": "application/json"}, text="{}")

        client._call_with_ip_retry = plain  # type: ignore[assignment]

        resp = asyncio.run(client.request("GET", "/api/works"))

        self.assertEqual(resp.status_code, 403)

    def test_get_json_maps_401_to_auth_error(self):
        client = self._client()

        async def fake(*_a, **_kw):
            return _FakeResp(401, text="unauthorized")

        client.request = fake  # type: ignore[assignment]

        with self.assertRaises(AsmrAuthError) as ctx:
            asyncio.run(client.get_json("/api/auth/me"))

        self.assertEqual(ctx.exception.status, 401)
        self.assertEqual(ctx.exception.reason, "auth_required")

    def test_get_json_maps_other_errors_to_the_upstream_status(self):
        client = self._client()

        async def fake(*_a, **_kw):
            return _FakeResp(500, text="boom")

        client.request = fake  # type: ignore[assignment]

        with self.assertRaises(AsmrError) as ctx:
            asyncio.run(client.get_json("/api/works"))

        self.assertEqual(ctx.exception.status, 500)
        self.assertIn("500", ctx.exception.message)


@unittest.skipUnless(HAVE_ASMR, SKIP_REASON)
class AsmrClientApiTestCase(unittest.TestCase):
    """业务方法：各自的路径/参数形状，以及 404 换 IP 只在取字节时才用。"""

    def _client(self):
        client = AsmrClient()
        self.addCleanup(lambda: asyncio.run(client.aclose()))
        return client

    def _patch_json(self, client, payload=None, exc=None):
        seen = []

        async def fake_get_json(path, **kw):
            seen.append((path, kw))
            if exc is not None:
                raise exc
            return payload if payload is not None else {}

        client.get_json = fake_get_json  # type: ignore[assignment]
        return seen

    def test_list_works_hits_the_paginated_endpoint(self):
        client = self._client()
        seen = self._patch_json(client, PAGE)

        asyncio.run(client.list_works(page=3))

        self.assertEqual(seen, [("/api/works", {"params": {"page": 3}})])

    def test_list_works_clamps_the_page_to_at_least_one(self):
        client = self._client()
        seen = self._patch_json(client, PAGE)

        asyncio.run(client.list_works(page=0))
        asyncio.run(client.list_works(page=-5))

        self.assertEqual([kw["params"]["page"] for _path, kw in seen], [1, 1])

    def test_search_puts_the_keyword_in_the_path(self):
        # 真契约是 GET /api/search/{关键词}：关键词在路径里，不是查询参数。
        client = self._client()
        seen = self._patch_json(client, PAGE)

        asyncio.run(client.search("催眠", page=2))

        self.assertEqual(seen, [("/api/search/%E5%82%AC%E7%9C%A0", {"params": {"page": 2}})])

    def test_search_url_encodes_the_keyword_into_one_path_segment(self):
        # 带斜杠的关键词若保留 "/"，上游会当成多级路径回 404，必须编成 %2F。
        client = self._client()
        seen = self._patch_json(client, PAGE)

        asyncio.run(client.search("a/b"))
        asyncio.run(client.search("癒し 耳かき"))

        self.assertEqual(
            [path for path, _kw in seen],
            ["/api/search/a%2Fb", "/api/search/%E7%99%92%E3%81%97%20%E8%80%B3%E3%81%8B%E3%81%8D"],
        )

    def test_search_rejects_an_empty_keyword_without_sending_anything(self):
        client = self._client()
        seen = self._patch_json(client, PAGE)

        for keyword in ("", "   ", None):
            with self.assertRaises(AsmrError) as ctx:
                asyncio.run(client.search(keyword))
            self.assertEqual(ctx.exception.status, 400)

        self.assertEqual(seen, [], "空关键词不该发请求")

    def test_work_and_tracks_use_the_id_in_the_path(self):
        client = self._client()
        seen = self._patch_json(client, TRACK_TREE)

        asyncio.run(client.work("1669340"))
        tracks = asyncio.run(client.tracks("1669340"))

        self.assertEqual([path for path, _kw in seen], ["/api/work/1669340", "/api/tracks/1669340"])
        self.assertEqual(len(tracks), 2)
        self.assertIsInstance(tracks[0], Track)

    def test_auth_me_hits_the_diagnostic_endpoint(self):
        client = self._client()
        seen = self._patch_json(client, {"user": {"loggedIn": False}})

        asyncio.run(client.auth_me())

        self.assertEqual(seen, [("/api/auth/me", {})])

    def test_fetch_cover_rejects_an_unknown_type_without_a_request(self):
        client = self._client()

        with self.assertRaises(AsmrError) as ctx:
            asyncio.run(client.fetch_cover("1669340", "thumb"))

        self.assertEqual(ctx.exception.status, 400)
        self.assertIn("thumb", ctx.exception.message)

    def test_fetch_cover_returns_the_bytes_and_content_type(self):
        client = self._client()
        calls = []

        async def fake_retry(host, call, *, retry_statuses=()):
            calls.append((host, tuple(retry_statuses)))
            return _FakeResp(200, content=b"RIFF", headers={"content-type": "image/webp"})

        client._call_with_ip_retry = fake_retry  # type: ignore[assignment]

        content, content_type = asyncio.run(client.fetch_cover("1669340", "sam"))

        self.assertEqual(content, b"RIFF")
        self.assertEqual(content_type, "image/webp")
        # 只有内容主机的 404 值得换 IP。
        self.assertEqual(calls, [(API_HOST, (404,))])

    def test_fetch_cover_defaults_to_webp_when_the_header_is_missing(self):
        client = self._client()

        async def fake_retry(host, call, *, retry_statuses=()):
            return _FakeResp(200, content=b"RIFF", headers={})

        client._call_with_ip_retry = fake_retry  # type: ignore[assignment]

        self.assertEqual(asyncio.run(client.fetch_cover("1669340"))[1], "image/webp")

    def test_fetch_cover_surfaces_a_404_with_its_status(self):
        client = self._client()

        async def fake_retry(host, call, *, retry_statuses=()):
            return _FakeResp(404, text="nope")

        client._call_with_ip_retry = fake_retry  # type: ignore[assignment]

        with self.assertRaises(AsmrError) as ctx:
            asyncio.run(client.fetch_cover("1669340"))

        self.assertEqual(ctx.exception.status, 404)

    def test_probe_range_reports_media_bytes(self):
        client = self._client()

        async def fake_retry(host, call, *, retry_statuses=()):
            return _FakeResp(
                206,
                content=MP4_HEAD,
                headers={
                    "content-type": "audio/mp4",
                    "content-range": "bytes 0-1023/2048",
                },
            )

        client._call_with_ip_retry = fake_retry  # type: ignore[assignment]

        result = asyncio.run(client.probe_range(MEDIA_URL))

        self.assertEqual(result["status"], 206)
        self.assertEqual(result["bytes"], len(MP4_HEAD))
        self.assertEqual(result["content_range"], "bytes 0-1023/2048")
        self.assertTrue(result["is_media"])

    def test_probe_range_marks_a_non_media_body(self):
        client = self._client()

        async def fake_retry(host, call, *, retry_statuses=()):
            return _FakeResp(200, content=b"<html>not media", headers={"content-type": "text/html"})

        client._call_with_ip_retry = fake_retry  # type: ignore[assignment]

        self.assertFalse(asyncio.run(client.probe_range(MEDIA_URL))["is_media"])

    def test_open_stream_returns_the_status_and_the_passthrough_headers(self):
        client = self._client()

        async def fake_retry(host, call, *, retry_statuses=()):
            return _FakeResp(
                206,
                content=MP4_HEAD,
                headers={
                    "content-type": "audio/mp4",
                    "content-range": "bytes 0-4095/53874359",
                    "content-length": "4096",
                    "accept-ranges": "bytes",
                    "etag": '"abc"',
                    "last-modified": "Wed, 01 Jan 2025 00:00:00 GMT",
                    # 不该被透传的头部：
                    "set-cookie": "session=1",
                },
            )

        client._call_with_ip_retry = fake_retry  # type: ignore[assignment]

        async def consume():
            status, headers, body = await client.open_stream(MEDIA_URL, {"range": "bytes=0-4095"})
            chunks = [chunk async for chunk in body]
            return status, headers, chunks

        status, headers, chunks = asyncio.run(consume())

        self.assertEqual(status, 206)
        self.assertEqual(headers["content-range"], "bytes 0-4095/53874359")
        self.assertNotIn("set-cookie", headers)
        self.assertEqual(b"".join(chunks), MP4_HEAD)

    def test_open_stream_uses_streaming_so_the_body_is_not_buffered(self):
        # 音频动辄几十上百 MB，默认读取会把整个响应体塞进内存。
        client = self._client()
        captured = []

        async def fake_retry(host, call, *, retry_statuses=()):
            session = MagicMock()

            async def session_get(url, **kwargs):
                captured.append(kwargs)
                return _FakeResp(200, content=b"x", headers={})

            session.get = session_get
            await call(session)
            return _FakeResp(200, content=b"x", headers={})

        client._call_with_ip_retry = fake_retry  # type: ignore[assignment]

        asyncio.run(client.open_stream(MEDIA_URL))

        self.assertTrue(captured[0]["stream"])

    def test_aclose_closes_sessions_and_the_doh_client(self):
        client = self._client()
        session = MagicMock()
        session.close = AsyncMock()
        client._sessions[API_HOST] = session
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
        client._sessions[API_HOST] = session

        asyncio.run(client.aclose())  # 不该抛

        self.assertEqual(client._sessions, {})


@unittest.skipUnless(HAVE_ASMR, SKIP_REASON)
class AsmrRouteTestCase(unittest.TestCase):
    """路由层：真实 app + 假客户端，验证形状、装饰与主机白名单。"""

    def _client(self):
        app = create_asmr_app(make_settings())
        client = TestClient(app)
        client.__enter__()
        self.addCleanup(client.__exit__, None, None, None)
        self.fake = _FakeAsmrClient()
        client.app.state.asmr_client = self.fake
        return client

    # ---------- 列表与搜索 ----------

    def test_works_returns_the_page_with_local_cover_urls(self):
        client = self._client()

        body = client.get("/api/asmr/works?page=1").json()

        self.assertEqual(body["pagination"]["totalCount"], 62611)
        item = body["works"][0]
        self.assertEqual(item["cover_url"], "/api/asmr/cover/1669340?type=main")
        self.assertEqual(item["thumbnail_url"], "/api/asmr/cover/1669340?type=240x240")
        self.assertEqual(item["id"], 1669340)
        self.assertEqual(self.fake.calls[0], ("list_works", 1))

    def test_works_page_must_be_positive(self):
        client = self._client()

        self.assertEqual(client.get("/api/asmr/works?page=0").status_code, 422)
        self.assertEqual(client.get("/api/asmr/works?page=-1").status_code, 422)

    def test_works_survives_a_page_without_a_works_list(self):
        client = self._client()

        async def no_works(page=1):
            return {"pagination": {"totalCount": 0}}

        self.fake.list_works = no_works
        response = client.get("/api/asmr/works")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {"pagination": {"totalCount": 0}})

    def test_search_requires_a_query(self):
        client = self._client()

        self.assertEqual(client.get("/api/asmr/search").status_code, 422)
        self.assertEqual(client.get("/api/asmr/search?query=").status_code, 422)

    def test_search_decorates_the_results(self):
        client = self._client()

        body = client.get("/api/asmr/search?query=催眠&page=2").json()

        self.assertIn("cover_url", body["works"][0])
        self.assertEqual(self.fake.calls[0], ("search", "催眠", 2))

    # ---------- 详情与音轨 ----------

    def test_work_detail_is_decorated(self):
        client = self._client()

        body = client.get("/api/asmr/work/1669340").json()

        self.assertEqual(body["id"], 1669340)
        self.assertEqual(body["cover_url"], "/api/asmr/cover/1669340?type=main")
        self.assertEqual(self.fake.calls[0], ("work", "1669340"))

    def test_tracks_is_flattened_by_default(self):
        client = self._client()

        body = client.get("/api/asmr/tracks/1669340").json()

        self.assertEqual(body["work_id"], "1669340")
        self.assertEqual(body["count"], 2)
        self.assertEqual([t["title"] for t in body["tracks"]], ["track_01.mp3", "track_02.mp3"])
        self.assertEqual(body["tracks"][0]["folders"], ["04：omake", "03：撮り下ろし"])
        self.assertNotIn("raw", body["tracks"][0])

    def test_tracks_can_keep_the_folder_path_only(self):
        client = self._client()

        body = client.get("/api/asmr/tracks/1669340?flat=false").json()

        self.assertEqual(body["count"], 2)
        self.assertEqual(sorted(body["tracks"][0]), ["folders", "hash"])
        self.assertEqual(body["tracks"][1]["folders"], [])

    def test_tracks_surfaces_upstream_failure_without_500(self):
        client = self._client()
        self.fake.error = AsmrError("上游炸了", status=502)

        response = client.get("/api/asmr/tracks/1669340")

        self.assertEqual(response.status_code, 502)
        self.assertEqual(response.json()["detail"], "上游炸了")

    def test_tracks_keeps_a_401_as_a_401(self):
        client = self._client()
        self.fake.error = AsmrAuthError("要登录")

        self.assertEqual(client.get("/api/asmr/tracks/1669340").status_code, 401)

    # ---------- 封面 ----------

    def test_cover_streams_the_bytes_with_a_long_cache(self):
        client = self._client()

        response = client.get("/api/asmr/cover/1669340?type=sam")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.content, b"RIFFWEBP")
        self.assertEqual(response.headers["content-type"], "image/webp")
        self.assertIn("public", response.headers["cache-control"])
        self.assertEqual(self.fake.calls[0], ("fetch_cover", "1669340", "sam"))

    def test_cover_defaults_to_the_main_type(self):
        client = self._client()

        client.get("/api/asmr/cover/1669340")

        self.assertEqual(self.fake.calls[0], ("fetch_cover", "1669340", "main"))

    def test_cover_rejects_an_unknown_type_at_the_edge(self):
        client = self._client()

        self.assertEqual(client.get("/api/asmr/cover/1669340?type=thumb").status_code, 422)
        self.assertEqual(self.fake.calls, [], "非法 type 不该打到客户端")

    def test_cover_surfaces_upstream_failure(self):
        client = self._client()
        self.fake.error = AsmrError("取封面失败", status=404)

        response = client.get("/api/asmr/cover/1669340")

        self.assertEqual(response.status_code, 404)
        self.assertEqual(response.json()["detail"], "取封面失败")

    # ---------- 字节代转 ----------

    def test_stream_forwards_range_and_marks_private(self):
        client = self._client()

        response = client.get(
            "/api/asmr/stream", params={"url": MEDIA_URL}, headers={"Range": "bytes=0-4095"}
        )

        self.assertEqual(response.status_code, 206)
        self.assertEqual(response.content, MP4_HEAD)
        self.assertEqual(response.headers["content-range"], "bytes 0-4095/53874359")
        self.assertIn("private", response.headers["cache-control"])
        self.assertEqual(self.fake.calls[-1][2]["range"], "bytes=0-4095")

    def test_stream_forwards_if_range_too(self):
        client = self._client()

        client.get("/api/asmr/stream", params={"url": MEDIA_URL}, headers={"If-Range": '"abc"'})

        self.assertEqual(self.fake.calls[-1][2]["if-range"], '"abc"')

    def test_stream_requires_a_url(self):
        client = self._client()

        self.assertEqual(client.get("/api/asmr/stream").status_code, 422)

    def test_stream_rejects_foreign_hosts(self):
        client = self._client()

        for url in (
            "https://evil.example.com/x.mp3",
            "https://api.asmr.one/x.mp3",
            "https://raw.kiko-play-niptan.one.evil.com/x.mp3",
            "not a url",
        ):
            response = client.get("/api/asmr/stream", params={"url": url})
            self.assertEqual(response.status_code, 400, url)

        self.assertEqual(self.fake.calls, [], "外来主机不该打到客户端")

    def test_stream_surfaces_an_upstream_failure(self):
        client = self._client()
        self.fake.probe_error = AsmrError("上游挂了", status=502)

        response = client.get("/api/asmr/stream", params={"url": MEDIA_URL})

        self.assertEqual(response.status_code, 502)
        self.assertEqual(response.json()["detail"], "上游挂了")

    # ---------- 登录态诊断 ----------

    def test_auth_reports_the_anonymous_state(self):
        client = self._client()

        body = client.get("/api/asmr/auth").json()

        self.assertFalse(body["configured"])
        self.assertFalse(body["authenticated"])
        self.assertTrue(body["anonymous"])
        self.assertTrue(body["ech"])
        self.assertIsNone(body["error"])

    def test_auth_reports_a_failure_instead_of_a_500(self):
        client = self._client()
        self.fake.error = AsmrError("探不到")

        response = client.get("/api/asmr/auth")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["error"], "探不到")

    def test_auth_mirrors_the_ech_switch(self):
        client = self._client()
        self.fake.ech = False

        self.assertFalse(client.get("/api/asmr/auth").json()["ech"])

    def test_a_logged_in_user_is_reflected(self):
        client = self._client()

        async def logged_in():
            return {"user": {"loggedIn": True}}

        self.fake.auth_me = logged_in
        body = client.get("/api/asmr/auth").json()

        self.assertTrue(body["authenticated"])
        self.assertFalse(body["anonymous"])

    def test_the_asmr_page_is_not_part_of_the_api_project(self):
        # asmr 接口项目只提供 JSON 接口，没有自己的页面扩展。
        client = self._client()

        self.assertEqual(client.get("/asmr").status_code, 404)


@unittest.skipUnless(HAVE_ASMR, SKIP_REASON)
class AsmrEntryPointTestCase(unittest.TestCase):
    """入口与打包：路由集合、路由超集、依赖清单。"""

    def test_asmr_routers_cover_the_works_api(self):
        self.assertEqual(len(ASMR_ROUTERS), 1)
        # nav_sections 是给外壳导航页（`/`）用的：asmr 不改路由，而是把自己的
        # 接口加进那一页（见 asmr_one/app.py 的 ASMR_NAV）。
        self.assertEqual(ASMR_STATE_KEYS, ("asmr_client", "nav_sections"))
        self.assertEqual(
            sorted(asmr_state_factory(make_settings())),
            ["asmr_client", "nav_sections"],
        )

    def test_the_state_factory_builds_a_configured_client(self):
        state = asmr_state_factory(AsmrSettings(rate_limit_rpm=10000, ech=False))

        self.assertIsInstance(state["asmr_client"], AsmrClient)
        self.assertFalse(state["asmr_client"].ech)

    def test_the_state_factory_hands_the_ip_pool_to_the_client(self):
        settings = AsmrSettings(
            rate_limit_rpm=10000, ech=True, ech_config=GOOD_ECH_B64, ech_ips="1.1.1.1,2.2.2.2"
        )

        client = asmr_state_factory(settings)["asmr_client"]

        self.assertEqual(client.ech_ips, ("1.1.1.1", "2.2.2.2"))
        self.assertEqual(client.ech_config, GOOD_ECH_B64)

    def test_asmr_nav_lists_only_json_apis(self):
        paths = [item.path for item in ASMR_NAV.items]
        self.assertTrue(all(p.startswith("/api/asmr/") for p in paths), paths)

    def test_the_site_descriptor_is_shaped_like_the_others(self):
        self.assertEqual(SITE.name, "asmr_one")
        self.assertEqual(SITE.title, "ASMR Bridge")
        self.assertEqual(SITE.version, "1.0.0")
        self.assertEqual(SITE.nav_order, 3)
        self.assertEqual(SITE.settings_class, AsmrSettings)
        self.assertEqual(SITE.required_files, ("run.py", "requirements.txt"))
        # asmr 匿名就够用：没有登录自检钩子。
        self.assertIsNone(SITE.login_check)

    def test_asmr_adds_exactly_its_own_routes_to_the_shell(self):
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
        full = paths(create_asmr_app(make_settings()))
        extra = full - shell

        self.assertTrue(shell <= full, "asmr 应用必须包含框架外壳的全部路由")
        self.assertEqual(
            extra,
            {
                "/api/asmr/works",
                "/api/asmr/search",
                "/api/asmr/work/{work_id}",
                "/api/asmr/tracks/{work_id}",
                "/api/asmr/cover/{work_id}",
                "/api/asmr/stream",
                "/api/asmr/auth",
            },
        )

    def test_shell_and_asmr_app_do_not_expose_other_sites_routes(self):
        # 三个接口项目同级：外壳里没有任何站点路由，asmr 入口也只挂
        # `/api/asmr/*`，Pixiv 与 iwara 的路由一条都不在。
        client = TestClient(create_asmr_app(make_settings()))
        with client:
            for path in ("/api/ranking", "/api/iwara/videos", "/api/ranking/illust"):
                self.assertEqual(client.get(path).status_code, 404, path)

    def test_asmr_requirements_list_curl_cffi(self):
        requirements = PROJECT_ROOT / "asmr_one" / "requirements.txt"
        text = requirements.read_text(encoding="utf-8")
        self.assertIn("curl_cffi", text)
        self.assertIn("cffi", text)

    def test_curl_cffi_is_not_a_hard_dependency_of_the_main_project(self):
        # 只跑 pixiv 的人不该被要求装这个编译包。
        root_requirements = PROJECT_ROOT / "requirements.txt"
        self.assertNotIn("curl_cffi", root_requirements.read_text(encoding="utf-8"))

    def test_the_package_init_lazily_exposes_the_app(self):
        # asmr_one/__init__.py 不能模块级 import .app：那条链顶部就要
        # curl_cffi，会把「先装依赖再建应用」的自举堵死。
        text = (PROJECT_ROOT / "asmr_one" / "__init__.py").read_text(encoding="utf-8")
        self.assertIn("def __getattr__", text)
        # 只允许它待在 `if TYPE_CHECKING:` 缩进块里；模块顶层出现就说明
        # import 链会在自举（先装依赖再建应用）之前被拉起。
        top_level = [ln for ln in text.splitlines() if ln and not ln[:1].isspace()]
        self.assertFalse(
            any(ln.startswith("from .app import") for ln in top_level),
            "asmr_one/__init__.py 顶层不能 import .app",
        )

    def test_the_sites_registry_knows_about_asmr_one(self):
        # 包 / 边界 / 测试三处共用这一份注册表，加站点不该漏在这里。
        import sys

        sys.path.insert(0, str(PROJECT_ROOT / "scripts"))
        try:
            import sites  # type: ignore[import-not-found]
        finally:
            sys.path.pop(0)

        info = sites.SITES_BY_NAME["asmr_one"]
        self.assertEqual(info.directory, "asmr_one")
        self.assertEqual(info.title, "ASMR Bridge")
        self.assertIn("curl_cffi", info.extra_modules)
        self.assertEqual(info.requirements, "asmr_one/requirements.txt")
        self.assertIn("asmr_one", sites.SITE_ROUTE_PATHS)


@unittest.skipUnless(HAVE_ASMR, SKIP_REASON)
class AsmrEnvCheckTestCase(unittest.TestCase):
    """入口 ``--check`` 时多出来的那四项。

    它们跟主项目那五项打在同一张 ``[环境检查]`` 表里，但名字都不在
    ``FATAL_CHECKS`` 里 —— 装坏了/没网只该提示，永远不该拦下启动。
    """

    def setUp(self):
        self.directory = make_temp_dir("asmr_envcheck")
        self.addCleanup(drop_temp_dir, self.directory)
        env = patch.dict(
            os.environ,
            {"PIXIV_DATA_DIR": str(self.directory), "ASMR_BRIDGE_ECH": ""},
            clear=False,
        )
        env.start()
        self.addCleanup(env.stop)

    def _settings(self, **kw):
        base = {"rate_limit_rpm": 10000, "data_dir": str(self.directory)}
        base.update(kw)
        return AsmrSettings(**base)

    def _fake_client(self, *, payload=None, error=None, search=None, search_error=None):
        """顶掉 ``asmr_one.client.AsmrClient``，返回 (patch, instance)。

        ``search`` 默认按关键词给「诚实」的答复：必定查不到的词回 0 条，
        其余回非 0 —— 也就是修复之后的行为。要模拟缺陷（关键词被忽略）就
        传一个不区分关键词的 ``search``。
        """
        cls = MagicMock()
        instance = cls.return_value
        instance.ech = True

        async def _list_works(page=1):
            if error is not None:
                raise error
            return payload if payload is not None else PAGE

        async def _search(keyword, page=1):
            if search_error is not None:
                raise search_error
            if search is not None:
                return search(keyword)
            total = 0 if keyword == "zzzzqqxxnotexist" else 9743
            return {
                "pagination": {"currentPage": 1, "pageSize": 20, "totalCount": total},
                "works": [] if total == 0 else [dict(WORK)],
            }

        async def _request(method, path, **kw):
            if error is not None:
                raise error
            return _FakeResp(200, text="ip=1.2.3.4\nsni=encrypted\nwarp=off\n")

        instance.list_works = AsyncMock(side_effect=_list_works)
        instance.search = AsyncMock(side_effect=_search)
        instance.request = AsyncMock(side_effect=_request)
        instance.pinned_ip = MagicMock(return_value="104.18.10.118")
        instance.aclose = AsyncMock()
        return patch("asmr_one.client.AsmrClient", cls), instance

    def test_extra_check_names_are_named_and_never_fatal(self):
        from pixiv_bridge.cli import FATAL_CHECKS

        from asmr_one.envcheck import DEPS_NAME
        from asmr_one.envcheck import ECH_NAME as ECH
        from asmr_one.envcheck import NET_NAME as NET
        from asmr_one.envcheck import SEARCH_NAME as SEARCH

        self.assertEqual(
            sorted((DEPS_NAME, NET, SEARCH, ECH)),
            ["asmr ECH", "asmr 依赖", "asmr 搜索", "asmr 网络"],
        )
        for name in (DEPS_NAME, NET, SEARCH, ECH):
            self.assertNotIn(name, FATAL_CHECKS)

    def test_deps_check_really_imports_curl_cffi(self):
        from asmr_one.envcheck import deps_check

        result = deps_check()

        # 这台机器上确实装了 curl_cffi，否则本模块本来就被整片跳过。
        self.assertTrue(result.ok)
        self.assertIn("curl_cffi", result.detail)

    def test_extra_checks_returns_four_results_in_order(self):
        from asmr_one.envcheck import DEPS_NAME, extra_checks

        patcher, _ = self._fake_client()
        with patcher, patch.object(asmr_ech, "available", lambda: True):
            results = extra_checks(self._settings())

        self.assertEqual(
            [r.name for r in results], [DEPS_NAME, NET_NAME, SEARCH_NAME, ECH_NAME]
        )
        self.assertTrue(results[0].ok)

    def test_the_network_check_reports_the_page_size_and_total(self):
        from asmr_one.envcheck import extra_checks

        patcher, _ = self._fake_client()
        with patcher, patch.object(asmr_ech, "available", lambda: True):
            results = extra_checks(self._settings())

        net = [r for r in results if r.name == NET_NAME][0]
        self.assertTrue(net.ok)
        self.assertIn("本页 1 个作品", net.detail)
        self.assertIn("62611", net.detail)

    def test_the_network_failure_is_reported_not_raised(self):
        from asmr_one.envcheck import extra_checks

        patcher, _ = self._fake_client(error=AsmrError("都连不上"))
        with patcher, patch.object(asmr_ech, "available", lambda: True):
            results = extra_checks(self._settings())

        net = [r for r in results if r.name == NET_NAME][0]
        self.assertFalse(net.ok)
        self.assertIn("api.asmr.one 不可达", net.detail)
        self.assertIn("都连不上", net.detail)

    def test_a_timeout_is_reported_with_the_seconds(self):
        from asmr_one.envcheck import NETWORK_TIMEOUT, extra_checks

        async def never(page=1):
            await asyncio.sleep(3600)

        patcher, instance = self._fake_client()
        instance.list_works = AsyncMock(side_effect=never)
        with patcher, patch.object(asmr_ech, "available", lambda: True), patch(
            "asmr_one.envcheck.NETWORK_TIMEOUT", 0.01
        ):
            results = extra_checks(self._settings())

        net = [r for r in results if r.name == NET_NAME][0]
        self.assertFalse(net.ok)
        self.assertIn("无响应", net.detail)
        self.assertGreater(NETWORK_TIMEOUT, 0)

    def test_the_search_check_sees_the_keyword_actually_filtering(self):
        from asmr_one.envcheck import extra_checks

        patcher, instance = self._fake_client()
        with patcher, patch.object(asmr_ech, "available", lambda: True):
            results = extra_checks(self._settings())

        search = [r for r in results if r.name == SEARCH_NAME][0]
        self.assertTrue(search.ok)
        self.assertIn("关键词确实在过滤", search.detail)
        self.assertIn("9743", search.detail)
        # 两侧都要问：一个必定查不到的词 + 一个已知有结果的词。
        seen = [call.args[0] for call in instance.search.await_args_list]
        self.assertEqual(seen, ["zzzzqqxxnotexist", "癒し"])

    def test_the_search_check_catches_an_unfiltered_upstream(self):
        # 这就是本站曾经的缺陷：任何关键词都回同一批 117 条。
        from asmr_one.envcheck import extra_checks

        def unfiltered(keyword):
            return {
                "pagination": {"currentPage": 1, "pageSize": 20, "totalCount": 117},
                "works": [dict(WORK)],
            }

        patcher, _ = self._fake_client(search=unfiltered)
        with patcher, patch.object(asmr_ech, "available", lambda: True):
            results = extra_checks(self._settings())

        search = [r for r in results if r.name == SEARCH_NAME][0]
        self.assertFalse(search.ok)
        self.assertIn("关键词没有被过滤", search.detail)
        self.assertIn("zzzzqqxxnotexist", search.detail)
        self.assertIn("/api/search/", search.detail)

    def test_the_search_check_catches_a_search_that_is_all_empty(self):
        # 反向故障：接口整体坏掉、什么都返回空 —— 只卡「查不到的词回 0」
        # 会让它假通过，所以还得拿一个已知有结果的词兜住。
        from asmr_one.envcheck import extra_checks

        def always_empty(keyword):
            return {
                "pagination": {"currentPage": 1, "pageSize": 20, "totalCount": 0},
                "works": [],
            }

        patcher, _ = self._fake_client(search=always_empty)
        with patcher, patch.object(asmr_ech, "available", lambda: True):
            results = extra_checks(self._settings())

        search = [r for r in results if r.name == SEARCH_NAME][0]
        self.assertFalse(search.ok)
        self.assertIn("搜索整体不可用", search.detail)

    def test_the_search_check_reports_a_failure_not_raised(self):
        from asmr_one.envcheck import extra_checks

        patcher, _ = self._fake_client(
            search_error=AsmrError("搜索炸了", status=502)
        )
        with patcher, patch.object(asmr_ech, "available", lambda: True):
            results = extra_checks(self._settings())

        search = [r for r in results if r.name == SEARCH_NAME][0]
        self.assertFalse(search.ok)
        self.assertIn("搜索不可用", search.detail)
        self.assertIn("搜索炸了", search.detail)

    def test_the_search_check_catches_a_response_without_a_total(self):
        from asmr_one.envcheck import extra_checks

        patcher, _ = self._fake_client(search=lambda keyword: {"works": []})
        with patcher, patch.object(asmr_ech, "available", lambda: True):
            results = extra_checks(self._settings())

        search = [r for r in results if r.name == SEARCH_NAME][0]
        self.assertFalse(search.ok)
        self.assertIn("pagination.totalCount", search.detail)

    def test_the_search_check_times_out_instead_of_hanging(self):
        from asmr_one.envcheck import extra_checks

        async def never(keyword, page=1):
            await asyncio.sleep(3600)

        patcher, instance = self._fake_client()
        instance.search = AsyncMock(side_effect=never)
        with patcher, patch.object(asmr_ech, "available", lambda: True), patch(
            "asmr_one.envcheck.SEARCH_TIMEOUT", 0.01
        ):
            results = extra_checks(self._settings())

        search = [r for r in results if r.name == SEARCH_NAME][0]
        self.assertFalse(search.ok)
        self.assertIn("搜索探测超过", search.detail)
        instance.aclose.assert_awaited()

    def test_the_ech_check_is_skipped_when_the_switch_is_off(self):
        from asmr_one.envcheck import extra_checks

        patcher, _ = self._fake_client()
        with patcher, patch.object(asmr_ech, "available", lambda: True):
            results = extra_checks(self._settings(ech=False))

        self.assertEqual([r.name for r in results], ["asmr 依赖", NET_NAME, SEARCH_NAME])

    def test_an_encrypted_sni_is_reported_as_ok(self):
        from asmr_one.envcheck import extra_checks

        patcher, _ = self._fake_client()
        with patcher, patch.object(asmr_ech, "available", lambda: True):
            results = extra_checks(self._settings())

        ech = [r for r in results if r.name == ECH_NAME][0]
        self.assertTrue(ech.ok)
        self.assertIn("SNI 已加密", ech.detail)
        self.assertIn("104.18.10.118", ech.detail)

    def test_a_plaintext_sni_is_reported_as_ech_not_working(self):
        from asmr_one.envcheck import extra_checks

        async def plain(method, path, **kw):
            return _FakeResp(200, text="ip=1.2.3.4\nsni=plaintext\n")

        patcher, instance = self._fake_client()
        instance.request = AsyncMock(side_effect=plain)
        with patcher, patch.object(asmr_ech, "available", lambda: True):
            results = extra_checks(self._settings())

        ech = [r for r in results if r.name == ECH_NAME][0]
        self.assertFalse(ech.ok)
        self.assertIn("明文", ech.detail)

    def test_a_libcurl_without_ech_says_how_to_turn_it_off(self):
        from asmr_one.envcheck import extra_checks

        patcher, _ = self._fake_client()
        with patcher, patch.object(asmr_ech, "available", lambda: False):
            results = extra_checks(self._settings())

        ech = [r for r in results if r.name == ECH_NAME][0]
        self.assertFalse(ech.ok)
        self.assertIn("不支持 ECH", ech.detail)
        self.assertIn("ASMR_BRIDGE_ECH=0", ech.detail)

    def test_a_handshake_failure_is_reported_not_raised(self):
        from asmr_one.envcheck import extra_checks

        patcher, _ = self._fake_client(error=AsmrError("握手炸了"))
        with patcher, patch.object(asmr_ech, "available", lambda: True):
            results = extra_checks(self._settings())

        ech = [r for r in results if r.name == ECH_NAME][0]
        self.assertFalse(ech.ok)
        self.assertIn("握手失败", ech.detail)
        self.assertIn("ASMR_BRIDGE_ECH=0", ech.detail)


@unittest.skipUnless(HAVE_ASMR, SKIP_REASON)
class AsmrEntryWiringTestCase(unittest.TestCase):
    """入口脚本本身：自举要跑在 import asmr_one.app 之前，并交给框架基座。"""

    def _source(self) -> str:
        return (PROJECT_ROOT / "asmr_one" / "run.py").read_text(encoding="utf-8")

    def test_bootstrap_runs_before_importing_the_app(self):
        # asmr_one.app → asmr_one.client → import curl_cffi；这条链没装包时会
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
            if line.startswith("from asmr_one.app import")
        )
        self.assertLess(bootstrap, app_import)

    def test_entry_hands_the_site_to_the_framework_cli(self):
        text = self._source()
        # 入口本身不再拼参数：站点的检查钩子都挂在 `asmr_one.app.SITE` 上，
        # 由框架基座 `pixiv_bridge.cli.main(site=...)` 统一取值。
        self.assertIn("from asmr_one.app import SITE", text)
        self.assertIn("main(site=SITE)", text)
        self.assertIn("from pixiv_bridge.cli import main", text)

    def test_the_entry_puts_the_project_root_on_sys_path(self):
        # `python asmr_one/run.py` 时 sys.path[0] 是 asmr_one/，框架包在上一级。
        text = self._source()
        self.assertIn("sys.path.insert(0, str(HERE.parent))", text)

    def test_the_entry_bootstraps_curl_cffi(self):
        text = self._source()
        self.assertIn("curl_cffi", text)
        self.assertIn("requirements.txt", text)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
