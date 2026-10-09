"""ehviewer 站点的启动自检。

补本站自己那六样，通过 ``Site.checks`` 挂进框架同一张 ``[环境检查]`` 表：

    ehviewer 依赖   curl_cffi 在不在（编译包，框架 REQUIRED 不含它）
    ehviewer 边缘   借道用的 Cloudflare 边缘有没有可用的（含 TCP 探测）
    ehviewer 站点   e-hentai.org 能不能真的取回一页（这才是整条链路通不通的判据）
    ehviewer 详情   详情页解析还认不认得出真实页面（守住骨架判据，见下）
    ehviewer 榜单   /popular 那页还在不在、还能不能解析出画廊
    ehviewer 大图   大图页解析还认不认得出真实页面（守住图片直链判据，见下）

六项都**不中止启动**：依赖缺失时启动本身会走 ``deps.bootstrap`` 自动补；网络
不通也只是接口返回 502，服务照常起来，方便用户先看到界面再排查。

**为什么「详情」要单独一项。** 上游对取不到的画廊回的是 HTTP 200 + 一句纯文本
（``Key missing…`` / ``No gallery specified.`` / ``Gallery not found.``），客户端
靠「页面里有没有 ``#gn`` / ``#gdc`` / ``#gdd`` 骨架」来分辨真假（见
``ehviewer/parse.py`` 的 ``parse_gallery_error``）。这个判据一旦失效 —— 上游改了
详情页结构、正则不再命中 —— **真实画廊会被整批误判成存根，全部返回 404**，比
原来的空壳更难查。``_site_check`` 探的是**列表页**，走另一套解析，证明不了这一点；
所以这里另外真取几条详情，确认它们仍能解析出标题。

**「榜单」和「大图」同理，各守一个别处证明不了的判据。** 榜单走的是 ``/popular``
这个独立入口，首页好着不代表它还在；大图守的是 ``parse_s_page`` 的图片直链判据
（``_IMG_SRC_RE`` + ``_S_STUB_PATTERNS``），失效时 ``/api/eh/proxy`` 对真实大图页
整批 502，而列表与详情都还是绿的。

这里**没有 ECH 自检** —— 该站点不用 ECH（封锁是 SNI 字符串 + DNS 投毒，
ECH 的外层 SNI 恰好是被封的那个，叠上去只会更糟），所以也就没有 ``ech*`` 配置。
"""

from __future__ import annotations

import asyncio

from pixiv_bridge.envcheck import CheckResult

# 与主项目那几项并列，前缀统一成 ehviewer，一眼能看出是哪来的。
DEPS_NAME = "ehviewer 依赖"
EDGE_NAME = "ehviewer 边缘"
SITE_NAME = "ehviewer 站点"
DETAIL_NAME = "ehviewer 详情"
POPULAR_NAME = "ehviewer 榜单"
PAGE_NAME = "ehviewer 大图"

# 边缘探测是并发的，给一个总上限即可。
EDGE_TIMEOUT = 20.0
# 站点探测要跑完一次真正的 HTTP/3 请求（QUIC 握手比 TCP 慢），给宽一点。
SITE_TIMEOUT = 30.0
# 详情探测要取列表 + 若干条详情，同样给宽一点（上面对每条不再单独计时）。
DETAIL_TIMEOUT = 40.0
# 榜单探测只是一次 GET，跟站点探测同量级。
POPULAR_TIMEOUT = 30.0
# 大图探测要串三次请求（列表 → 详情 → /s/ 页），比详情再宽一点。
PAGE_TIMEOUT = 60.0
# 取几条详情来验判据。一条就够看出「判据还认不认得真实页面」，取两条是为了
# 万一第一条恰好是刚被删除/改版的上传，不至于就报故障。
DETAIL_SAMPLES = 2

_HINT_DEPS = "启动时会自动安装；手动：pip install -r ehviewer/requirements.txt"
_HINT_EDGE = (
    "HTTP/3（UDP:443）必须能出站；本机若被禁 UDP，可配 .env 里的代理或换网络"
)
_HINT_DETAIL = (
    "多半是上游改了详情页结构，ehviewer/parse.py 的骨架判据"
    "（#gn / #gdc / #gdd）不再命中 —— 真实画廊会被当成存根"
)
_HINT_POPULAR = (
    "/popular 页面的结构可能变了（list_galleries 那套解析不再命中），"
    "或者上游把这个入口下线了"
)
_HINT_PAGE = (
    "多半是上游改了大图页结构，ehviewer/parse.py 的 _IMG_SRC_RE"
    "（id=\"img\" 的 src）不再命中，或存根判据（invalid / key）不再命中"
    " —— /api/eh/proxy 会对真实大图页整批 404"
)


def deps_check() -> CheckResult:
    """curl_cffi 是否真的能用。

    用真 import 而不是 ``find_spec``：它是编译包（libcurl + BoringSSL），出现过
    「装上了但 ``_cffi_backend`` 缺失」的情况，只有 import 才暴露得出来。
    顺便确认这份 libcurl **编译了 HTTP/3** —— 没有 ngtcp2/nghttp3 的话，
    这个站点在本机是连不上的，值得单独说出来。
    """
    try:
        import curl_cffi
    except Exception as exc:  # noqa: BLE001 - 装坏/没装都只提示
        return CheckResult(
            DEPS_NAME, False, f"不可用（{exc.__class__.__name__}: {exc}）—— {_HINT_DEPS}"
        )

    version = getattr(curl_cffi, "__version__", "")
    detail = f"curl_cffi {version}".strip() + " 已就绪"
    # curl_cffi 暴露了底层 libcurl 的能力位；HTTP3 位就是 ngtcp2/nghttp3 的判据。
    try:
        from curl_cffi.const import CurlHttpVersion  # noqa: F401

        detail += "（含 HTTP/3 / QUIC）"
    except Exception:  # noqa: BLE001 - 老版本没有这个常量
        detail += "；但缺 CurlHttpVersion（HTTP/3 不可用，站点会连不上）"
    return CheckResult(DEPS_NAME, True, detail)


async def _edge_check(settings) -> CheckResult:
    """借道边缘还有没有活的。

    只建 TCP（不发 TLS），所以不会被 SNI 判据拦下 —— 能连上就算这个边缘可用。
    """
    try:
        from .client import EhClient
        from .config import as_eh_settings, split_ips
    except Exception as exc:  # noqa: BLE001 - curl_cffi 缺失时退化
        return CheckResult(EDGE_NAME, False, f"跳过（{exc.__class__.__name__}: {exc}）")

    client_settings = as_eh_settings(settings)
    client = EhClient(
        edge_ips=split_ips(client_settings.edge_ips),
        edge_attempts=client_settings.edge_attempts,
        image_attempts=client_settings.image_attempts,
    )
    try:
        edges = await asyncio.wait_for(client.edges(), timeout=EDGE_TIMEOUT)
    except asyncio.TimeoutError:
        return CheckResult(
            EDGE_NAME, False, f"探测超过 {EDGE_TIMEOUT:.0f} 秒；{_HINT_EDGE}"
        )
    except Exception as exc:  # noqa: BLE001 - 没有可用边缘也是结论
        return CheckResult(
            EDGE_NAME, False, f"没有可用边缘（{exc.__class__.__name__}: {exc}）；{_HINT_EDGE}"
        )
    finally:
        await client.aclose()

    return CheckResult(
        EDGE_NAME,
        True,
        f"{len(edges)}/{len(client.edge_ips)} 个候选边缘可建连（示例 {edges[0]}）",
    )


async def _site_check(settings) -> CheckResult:
    """真的去 e-hentai.org 取一页。

    只探 TCP / 边缘还不够：这一站真正的坎是「SNI 明文被封」，而唯一的出路是
    QUIC —— 所以**能取回一页 HTML 才证明整条链路是通的**。
    """
    try:
        from .client import EhClient, EhError
        from .config import as_eh_settings, split_ips
    except Exception as exc:  # noqa: BLE001 - curl_cffi 缺失时退化
        return CheckResult(SITE_NAME, False, f"跳过（{exc.__class__.__name__}: {exc}）")

    client_settings = as_eh_settings(settings)
    client = EhClient(
        http3=client_settings.http3,
        edge_ips=split_ips(client_settings.edge_ips),
        edge_attempts=client_settings.edge_attempts,
        image_attempts=client_settings.image_attempts,
    )
    try:
        page = await asyncio.wait_for(
            client.list_galleries(), timeout=SITE_TIMEOUT
        )
    except asyncio.TimeoutError:
        return CheckResult(
            SITE_NAME, False, f"e-hentai.org 超过 {SITE_TIMEOUT:.0f} 秒无响应；{_HINT_EDGE}"
        )
    except EhError as exc:
        return CheckResult(SITE_NAME, False, f"e-hentai.org 不可达：{exc.message}")
    except Exception as exc:  # noqa: BLE001 - 未知异常也让自检给出结论
        return CheckResult(
            SITE_NAME,
            False,
            f"e-hentai.org 探测失败（{exc.__class__.__name__}: {exc}）",
        )
    finally:
        await client.aclose()

    count = page.get("count") if isinstance(page, dict) else None
    detail = "e-hentai.org 可达（HTTP/3 + 借道边缘）"
    if isinstance(count, int):
        detail += f"，首页取回 {count} 条"
    if not client.http3:
        detail += "；注意 HTTP3 已关，取到页面属于侥幸"
    return CheckResult(SITE_NAME, True, detail)


async def _detail_check(settings) -> CheckResult:
    """详情页还能不能解析出内容 —— 守住「200 + 存根」的骨架判据。

    取一页列表，再挑头几条去取详情，确认 ``gallery()`` 认得它们（能拿到标题
    与图链）。**这里刻意不吞掉 ``EhNotFoundError``**：真实画廊被判成存根时
    抛的正是这个，而那种故障必须报出来，不能跟「这条恰好没了」混为一谈。
    """
    try:
        from .client import EhClient, EhError
        from .config import as_eh_settings, split_ips
    except Exception as exc:  # noqa: BLE001 - curl_cffi 缺失时退化
        return CheckResult(DETAIL_NAME, False, f"跳过（{exc.__class__.__name__}: {exc}）")

    client_settings = as_eh_settings(settings)
    client = EhClient(
        http3=client_settings.http3,
        edge_ips=split_ips(client_settings.edge_ips),
        edge_attempts=client_settings.edge_attempts,
        image_attempts=client_settings.image_attempts,
    )

    async def probe() -> CheckResult:
        page = await client.list_galleries()
        items = page.get("items") if isinstance(page, dict) else None
        if not items:
            return CheckResult(
                DETAIL_NAME, False, "列表页没给出画廊，无法验详情解析"
            )
        samples = items[:DETAIL_SAMPLES]
        empty, checked = [], 0
        for item in samples:
            detail = await client.gallery(item["gid"], item["token"])
            checked += 1
            if not detail.get("title") and not detail.get("page_links"):
                empty.append(str(item.get("gid", "?")))
        if empty:
            return CheckResult(
                DETAIL_NAME,
                False,
                f"{len(empty)}/{checked} 条真实详情解析成空（gid={'、'.join(empty)}）；"
                f"{_HINT_DETAIL}",
            )
        return CheckResult(
            DETAIL_NAME,
            True,
            f"{checked} 条真实详情解析正常（判据仍认得上游页面）",
        )

    try:
        return await asyncio.wait_for(probe(), timeout=DETAIL_TIMEOUT)
    except asyncio.TimeoutError:
        return CheckResult(
            DETAIL_NAME, False, f"详情探测超过 {DETAIL_TIMEOUT:.0f} 秒；{_HINT_EDGE}"
        )
    except EhError as exc:
        return CheckResult(
            DETAIL_NAME,
            False,
            f"详情不可用：{exc.message}；{_HINT_DETAIL}",
        )
    except Exception as exc:  # noqa: BLE001 - 未知异常也让自检给出结论
        return CheckResult(
            DETAIL_NAME,
            False,
            f"详情探测失败（{exc.__class__.__name__}: {exc}）",
        )
    finally:
        await client.aclose()


async def _popular_check(settings) -> CheckResult:
    """``/popular`` 还取不取得到、还解析不解析得出画廊。

    这条**必须单独探**：热门榜单虽然复用列表页那套解析，但走的是另一个 URL
    （``/popular``），上游完全可能只动这一个入口（改版、下线、重定向到登录页）。
    ``_site_check`` 探的是首页，它绿着也不代表榜单还在。
    """
    try:
        from .client import EhClient, EhError
        from .config import as_eh_settings, split_ips
    except Exception as exc:  # noqa: BLE001 - curl_cffi 缺失时退化
        return CheckResult(POPULAR_NAME, False, f"跳过（{exc.__class__.__name__}: {exc}）")

    client_settings = as_eh_settings(settings)
    client = EhClient(
        http3=client_settings.http3,
        edge_ips=split_ips(client_settings.edge_ips),
        edge_attempts=client_settings.edge_attempts,
        image_attempts=client_settings.image_attempts,
    )
    try:
        page = await asyncio.wait_for(client.popular(), timeout=POPULAR_TIMEOUT)
    except asyncio.TimeoutError:
        return CheckResult(
            POPULAR_NAME, False, f"榜单探测超过 {POPULAR_TIMEOUT:.0f} 秒；{_HINT_EDGE}"
        )
    except EhError as exc:
        return CheckResult(
            POPULAR_NAME, False, f"榜单不可用：{exc.message}；{_HINT_POPULAR}"
        )
    except Exception as exc:  # noqa: BLE001 - 未知异常也让自检给出结论
        return CheckResult(
            POPULAR_NAME,
            False,
            f"榜单探测失败（{exc.__class__.__name__}: {exc}）",
        )
    finally:
        await client.aclose()

    items = page.get("items") if isinstance(page, dict) else None
    if not items:
        return CheckResult(
            POPULAR_NAME, False, f"榜单页没解析出画廊；{_HINT_POPULAR}"
        )
    return CheckResult(
        POPULAR_NAME,
        True,
        f"榜单页解析正常（{len(items)} 条画廊）",
    )


async def _page_check(settings) -> CheckResult:
    """大图页解析还认不认得出真实页面 —— 守住 ``/api/eh/proxy`` 的图片直链判据。

    跟「详情」是同一个道理，但守的是另一套正则：``parse_s_page`` 靠
    ``_IMG_SRC_RE``（``id="img"`` 的 ``src``）取图片直链，靠 ``_S_STUB_PATTERNS``
    分辨存根。这条一旦失效，``/api/eh/proxy`` 拿不到直链，**真实大图页会整批
    502**，而列表页与详情页都还是好的。

    串起整条链才作数：列表 → 取一条详情的图列表 → 拿第一个 ``/s/`` 页面真解一次。
    """
    try:
        from .client import EhClient, EhError
        from .config import as_eh_settings, split_ips
    except Exception as exc:  # noqa: BLE001 - curl_cffi 缺失时退化
        return CheckResult(PAGE_NAME, False, f"跳过（{exc.__class__.__name__}: {exc}）")

    client_settings = as_eh_settings(settings)
    client = EhClient(
        http3=client_settings.http3,
        edge_ips=split_ips(client_settings.edge_ips),
        edge_attempts=client_settings.edge_attempts,
        image_attempts=client_settings.image_attempts,
    )

    async def probe() -> CheckResult:
        page = await client.list_galleries()
        items = page.get("items") if isinstance(page, dict) else None
        if not items:
            return CheckResult(PAGE_NAME, False, "列表页没给出画廊，无法验大图解析")
        item = items[0]
        detail = await client.gallery(item["gid"], item["token"])
        links = detail.get("page_links") if isinstance(detail, dict) else None
        if not links:
            return CheckResult(
                PAGE_NAME,
                False,
                f"详情页没给出图片页链接（gid={item.get('gid', '?')}），无法验大图解析",
            )
        shot = await client.image_page(links[0])
        if not shot.get("image_url"):
            return CheckResult(
                PAGE_NAME, False, f"大图页里没有图片直链：{links[0]}；{_HINT_PAGE}"
            )
        return CheckResult(
            PAGE_NAME,
            True,
            f"大图页解析正常（{shot.get('filename') or '首张'}，来自 {links[0]}）",
        )

    try:
        return await asyncio.wait_for(probe(), timeout=PAGE_TIMEOUT)
    except asyncio.TimeoutError:
        return CheckResult(
            PAGE_NAME, False, f"大图探测超过 {PAGE_TIMEOUT:.0f} 秒；{_HINT_EDGE}"
        )
    except EhError as exc:
        return CheckResult(
            PAGE_NAME, False, f"大图不可用：{exc.message}；{_HINT_PAGE}"
        )
    except Exception as exc:  # noqa: BLE001 - 未知异常也让自检给出结论
        return CheckResult(
            PAGE_NAME,
            False,
            f"大图探测失败（{exc.__class__.__name__}: {exc}）",
        )
    finally:
        await client.aclose()


async def _async_checks(settings) -> list[CheckResult]:
    return list(
        await asyncio.gather(
            _edge_check(settings),
            _site_check(settings),
            _detail_check(settings),
            _popular_check(settings),
            _page_check(settings),
        )
    )


def extra_checks(settings) -> list[CheckResult]:
    """``Site.checks`` 要的钩子：站点的自检项。

    环境变量在 ``ehviewer/config.py`` 里读（``EhSettings``），这里只读
    ``settings`` 上的结果，不重复解析。
    """
    results = [deps_check()]
    results.extend(asyncio.run(_async_checks(settings)))
    return results
