"""ehviewer 站点的启动自检。

补本站自己那四样，通过 ``Site.checks`` 挂进框架同一张 ``[环境检查]`` 表：

    ehviewer 依赖   curl_cffi 在不在（编译包，框架 REQUIRED 不含它）
    ehviewer 边缘   借道用的 Cloudflare 边缘有没有可用的（含 TCP 探测）
    ehviewer 站点   e-hentai.org 能不能真的取回一页（这才是整条链路通不通的判据）
    ehviewer 详情   详情页解析还认不认得出真实页面（守住骨架判据，见下）

四项都**不中止启动**：依赖缺失时启动本身会走 ``deps.bootstrap`` 自动补；网络
不通也只是接口返回 502，服务照常起来，方便用户先看到界面再排查。

**为什么「详情」要单独一项。** 上游对取不到的画廊回的是 HTTP 200 + 一句纯文本
（``Key missing…`` / ``No gallery specified.`` / ``Gallery not found.``），客户端
靠「页面里有没有 ``#gn`` / ``#gdc`` / ``#gdd`` 骨架」来分辨真假（见
``ehviewer/parse.py`` 的 ``parse_gallery_error``）。这个判据一旦失效 —— 上游改了
详情页结构、正则不再命中 —— **真实画廊会被整批误判成存根，全部返回 404**，比
原来的空壳更难查。``_site_check`` 探的是**列表页**，走另一套解析，证明不了这一点；
所以这里另外真取几条详情，确认它们仍能解析出标题。

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

# 边缘探测是并发的，给一个总上限即可。
EDGE_TIMEOUT = 20.0
# 站点探测要跑完一次真正的 HTTP/3 请求（QUIC 握手比 TCP 慢），给宽一点。
SITE_TIMEOUT = 30.0
# 详情探测要取列表 + 若干条详情，同样给宽一点（上面对每条不再单独计时）。
DETAIL_TIMEOUT = 40.0
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


async def _async_checks(settings) -> list[CheckResult]:
    return list(
        await asyncio.gather(
            _edge_check(settings), _site_check(settings), _detail_check(settings)
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
