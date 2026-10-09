"""asmr 站点的启动自检。

框架的通用检查查的是公共环境（Python / 依赖 / 项目文件 / 端口）。这一份补
asmr 自己那几样，通过 ``Site.checks`` 挂进同一张 ``[环境检查]`` 表：

    asmr 依赖   curl_cffi 在不在（编译包，框架 REQUIRED 不含它）
    asmr 网络   api.asmr.one 通不通（本机到它自己的边缘 IP 是 RST，要走借道池）
    asmr 搜索   关键词到底有没有在过滤（见下）
    asmr ECH    开了才跑：加密 SNI 能不能握手

四项都**不中止启动**：没装 curl_cffi 时启动本身会走 ``deps.bootstrap`` 自动补，
网络不通也只是让接口返回 502（服务照常起来，方便用户先看到界面再排查）。

「搜索」为什么要单独一项：**「能连通」不等于「参数生效」**。本站曾经把
``GET /api/search/works`` 当成搜索端点 —— 那个端点根本不存在，上游把路径里的
``works`` 当成了关键词，于是任何查询都恒定返回「搜 works」的同一批。它每次都是
HTTP 200、形状标准、解析毫无异常，``asmr 网络`` 那项探的是 ``/api/works``，照样
全绿。**只有拿一个必定查不到的词去问，才看得出来。**

与 iwara 不同的是这里**没有登录自检**：asmr 的公开浏览与播放匿名就够，要登录
的那几个端点（标签 / 用户收藏）本站不暴露，所以也没有 ``login_check``。
"""

from __future__ import annotations

import asyncio

from pixiv_bridge.envcheck import CheckResult

# 与主项目那几项并列，前缀统一成 asmr，一眼能看出是哪来的。
DEPS_NAME = "asmr 依赖"
NET_NAME = "asmr 网络"
SEARCH_NAME = "asmr 搜索"
ECH_NAME = "asmr ECH"

# 整个网络检查的上限：里面还含借道 IP 的 TCP 探测，别卡太久。
NETWORK_TIMEOUT = 25.0
# 搜索要发两次请求（一个真词 + 一个必定查不到的词），比列表稍宽。
SEARCH_TIMEOUT = 30.0
# ECH 只判握手，不需要把列表请求跑完，给短一点。
ECH_TIMEOUT = 15.0

# 必定查不到的词：只要它还能返回结果，就说明上游没有在按关键词过滤。
SEARCH_PROBE_MISS = "zzzzqqxxnotexist"
# 已知一定有结果的词，用来挡住「接口坏了、什么都返回空」这种反向故障。
SEARCH_PROBE_HIT = "癒し"

_HINT_DEPS = "启动时会自动安装；手动：pip install -r asmr_one/requirements.txt"
_HINT_ECH = "关掉：.env 里 ASMR_BRIDGE_ECH=0（关了大概率整个站不通）；或配代理"
_HINT_SEARCH = (
    "多半是上游换了搜索端点或改成了别的调用方式 —— 真契约是 "
    "GET /api/search/{关键词}（关键词在**路径**里，不是查询参数）。"
    "查 asmr.one 的前端 bundle（/js/app.*.js）里 /api/search/ 那段就能确认"
)


def deps_check() -> CheckResult:
    """curl_cffi 是否真的能用。

    这里用真 import 而不是 ``find_spec``：它是编译包，曾经出现过
    ``curl_cffi`` 装上了但 ``_cffi_backend`` 缺失（ABI 不匹配）的情况 ——
    只有 import 才暴露得出来。
    """
    try:
        import curl_cffi  # noqa: F401
    except Exception as exc:  # noqa: BLE001 - 装坏/没装都只提示
        return CheckResult(
            DEPS_NAME, False, f"不可用（{exc.__class__.__name__}: {exc}）—— {_HINT_DEPS}"
        )
    version = getattr(curl_cffi, "__version__", "")
    return CheckResult(DEPS_NAME, True, f"curl_cffi {version}".strip() + " 已就绪")


async def _network_check() -> CheckResult:
    """向 api.asmr.one 真发一个列表请求。

    只探 TCP 不够：这里的坎是「IP 被 RST + 不带 ECH 也会被拦」，能拿到 JSON
    才证明借道 IP 池 + impersonate + ECH 这条链路整体是通的。
    """
    try:
        from .client import AsmrClient, AsmrError
    except Exception as exc:  # noqa: BLE001 - curl_cffi 缺失时退化
        return CheckResult(NET_NAME, False, f"跳过（{exc.__class__.__name__}: {exc}）")

    client = AsmrClient()
    try:
        page = await asyncio.wait_for(client.list_works(page=1), timeout=NETWORK_TIMEOUT)
    except asyncio.TimeoutError:
        return CheckResult(
            NET_NAME, False, f"api.asmr.one 超过 {NETWORK_TIMEOUT:.0f} 秒无响应"
        )
    except AsmrError as exc:
        return CheckResult(NET_NAME, False, f"api.asmr.one 不可达：{exc.message}")
    except Exception as exc:  # noqa: BLE001 - 未知异常也让自检给出结论
        return CheckResult(
            NET_NAME, False, f"api.asmr.one 探测失败（{exc.__class__.__name__}: {exc}）"
        )
    finally:
        await client.aclose()

    works = page.get("works") if isinstance(page, dict) else None
    count = len(works) if isinstance(works, list) else None
    total = ((page or {}).get("pagination") or {}).get("totalCount")
    detail = "api.asmr.one 可达"
    if count is not None:
        detail += f"（本页 {count} 个作品，共 {total}）"
    return CheckResult(NET_NAME, True, detail)


async def _search_check(settings) -> CheckResult:
    """关键词到底有没有在过滤。

    判据是**横向对照**，不是单次响应 —— 单看一次永远看不出问题：本站曾经
    用不存在的 ``/api/search/works`` 当搜索端点，上游把路径里的 ``works``
    当关键词，于是任何查询都回 200、形状标准、totalCount 恒为 117。

    两侧都要卡：
      ① 拿必定查不到的词去问，**必须回 0 条** —— 有条目就说明没在过滤。
      ② 拿已知有结果的词去问，**必须不是 0 条** —— 挡住「接口整体坏掉、
         什么都返回空」这种反向故障（那会让 ① 假通过）。
    """
    try:
        from .client import AsmrClient, AsmrError
        from .config import as_asmr_settings, split_ips
    except Exception as exc:  # noqa: BLE001 - curl_cffi 缺失时退化
        return CheckResult(SEARCH_NAME, False, f"跳过（{exc.__class__.__name__}: {exc}）")

    site_settings = as_asmr_settings(settings)
    client = AsmrClient(
        ech=site_settings.ech,
        ech_config=site_settings.ech_config,
        ech_ips=split_ips(site_settings.ech_ips),
    )

    def _total(page) -> int | None:
        if not isinstance(page, dict):
            return None
        pagination = page.get("pagination") or {}
        total = pagination.get("totalCount")
        return total if isinstance(total, int) else None

    async def probe() -> CheckResult:
        miss = await client.search(SEARCH_PROBE_MISS)
        miss_total = _total(miss)
        if miss_total is None:
            return CheckResult(SEARCH_NAME, False, "搜索响应里没有 pagination.totalCount")
        if miss_total != 0:
            return CheckResult(
                SEARCH_NAME,
                False,
                f"搜一个必定查不到的词「{SEARCH_PROBE_MISS}」居然回了 {miss_total} 条"
                f" —— 关键词没有被过滤（任何查询都返回同一批）；{_HINT_SEARCH}",
            )
        hit = await client.search(SEARCH_PROBE_HIT)
        hit_total = _total(hit)
        if hit_total is None:
            return CheckResult(SEARCH_NAME, False, "搜索响应里没有 pagination.totalCount")
        if hit_total == 0:
            return CheckResult(
                SEARCH_NAME,
                False,
                f"连已知有结果的「{SEARCH_PROBE_HIT}」都回 0 条 —— 搜索整体不可用（上游改版或端点下线）；{_HINT_SEARCH}",
            )
        return CheckResult(
            SEARCH_NAME,
            True,
            f"关键词确实在过滤（「{SEARCH_PROBE_HIT}」{hit_total} 条，「{SEARCH_PROBE_MISS}」0 条）",
        )

    try:
        return await asyncio.wait_for(probe(), timeout=SEARCH_TIMEOUT)
    except asyncio.TimeoutError:
        return CheckResult(SEARCH_NAME, False, f"搜索探测超过 {SEARCH_TIMEOUT:.0f} 秒")
    except AsmrError as exc:
        return CheckResult(SEARCH_NAME, False, f"搜索不可用：{exc.message}；{_HINT_SEARCH}")
    except Exception as exc:  # noqa: BLE001 - 未知异常也让自检给出结论
        return CheckResult(
            SEARCH_NAME, False, f"搜索探测失败（{exc.__class__.__name__}: {exc}）"
        )
    finally:
        await client.aclose()


async def _ech_check(settings) -> CheckResult:
    """开了 ECH 才跑：跟 asmr.one 握手，看 Cloudflare 看到的 SNI 是否真加密。

    判据用 ``/cdn-cgi/trace``：Cloudflare 会在里面回显 ``sni=``，明文是
    ``plaintext``，加密成功才是 ``encrypted``。
    """
    try:
        from . import ech as ech_module
        from .client import AsmrClient, AsmrError
        from .config import as_asmr_settings, split_ips
    except Exception as exc:  # noqa: BLE001 - curl_cffi 缺失时退化
        return CheckResult(ECH_NAME, False, f"跳过（{exc.__class__.__name__}: {exc}）")

    if not ech_module.available():
        return CheckResult(
            ECH_NAME,
            False,
            "当前 curl_cffi 的 libcurl 不支持 ECH（需要 CURLOPT_ECH）；" + _HINT_ECH,
        )

    site_settings = as_asmr_settings(settings)
    client = AsmrClient(
        ech=site_settings.ech,
        ech_config=site_settings.ech_config,
        ech_ips=split_ips(site_settings.ech_ips),
    )
    try:
        if not client.ech:
            return CheckResult(ECH_NAME, False, "未开启（ASMR_BRIDGE_ECH=0）")
        resp = await asyncio.wait_for(
            client.request("GET", "/cdn-cgi/trace", host="asmr.one"),
            timeout=ECH_TIMEOUT,
        )
    except asyncio.TimeoutError:
        return CheckResult(
            ECH_NAME, False, f"asmr.one 握手超过 {ECH_TIMEOUT:.0f} 秒；" + _HINT_ECH
        )
    except AsmrError as exc:
        return CheckResult(ECH_NAME, False, f"asmr.one 握手失败：{exc.message}；" + _HINT_ECH)
    except Exception as exc:  # noqa: BLE001
        return CheckResult(
            ECH_NAME,
            False,
            f"asmr.one 握手失败（{exc.__class__.__name__}: {exc}）；" + _HINT_ECH,
        )
    finally:
        await client.aclose()

    text = resp.text if hasattr(resp, "text") else ""
    sni = ""
    for line in text.splitlines():
        if line.startswith("sni="):
            sni = line.split("=", 1)[1].strip()
            break
    if sni == "encrypted":
        ip = client.pinned_ip("asmr.one") or "?"
        return CheckResult(ECH_NAME, True, f"SNI 已加密（sni=encrypted，IP {ip}）")
    if sni:
        return CheckResult(ECH_NAME, False, f"SNI 仍是明文（sni={sni}）；" + _HINT_ECH)
    return CheckResult(ECH_NAME, False, "取不到 /cdn-cgi/trace 的 sni 行；" + _HINT_ECH)


async def _async_checks(settings) -> list[CheckResult]:
    checks = [_network_check(), _search_check(settings)]
    if getattr(settings, "ech", True):
        checks.append(_ech_check(settings))
    return list(await asyncio.gather(*checks))


def extra_checks(settings) -> list[CheckResult]:
    """``Site.checks`` 要的钩子：站点的自检项。

    环境变量在 ``asmr_one/config.py`` 里读（``AsmrSettings``），这里只读
    ``settings`` 上的结果，不重复解析。
    """
    results = [deps_check()]
    results.extend(asyncio.run(_async_checks(settings)))
    return results
