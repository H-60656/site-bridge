"""pornhub 站点的启动自检。

框架的通用检查查的是公共环境（Python / 依赖 / 项目文件 / 端口）。这一份补
pornhub 自己那几样，通过 ``Site.checks`` 挂进同一张 ``[环境检查]`` 表：

    pornhub 依赖   curl_cffi 在不在（编译包，框架 REQUIRED 不含它）
    pornhub 网络   www.pornhub.com 通不通（Cloudflare 后，本机指纹会被拦）
    pornhub ECH    开了才跑：加密 SNI 能不能握手

三项都**不中止启动**：没装 curl_cffi 时启动本身会走 ``deps.bootstrap`` 自动补，
网络不通也只是让接口返回 502（服务照常起来，方便用户先看到界面再排查）。

与 iwara / asmr_one 相同，这里**没有登录自检**：pornhub 的公开浏览匿名就够，
要登录的那几个端点本站不暴露，所以也没有 ``login_check``。
"""

from __future__ import annotations

import asyncio

from pixiv_bridge.envcheck import CheckResult

# 与主项目那几项并列，前缀统一成 pornhub，一眼能看出是哪来的。
DEPS_NAME = "pornhub 依赖"
NET_NAME = "pornhub 网络"
ECH_NAME = "pornhub ECH"

# 整个网络检查的上限：里面还含借道 IP 的 TCP 探测，别卡太久。
NETWORK_TIMEOUT = 25.0
# ECH 只判握手，不需要把列表请求跑完，给短一点。
ECH_TIMEOUT = 15.0

_HINT_DEPS = "启动时会自动安装；手动：pip install -r pornhub/requirements.txt"
_HINT_ECH = "关掉：.env 里 PORNHUB_BRIDGE_ECH=0（关了大概率整个站不通）；或配代理"


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
    """向 www.pornhub.com 真发一个首页请求。

    只探 TCP 不够：这里的坎是「Cloudflare 指纹被拦 / 换 IP 才通」，能拿到 HTML
    才证明 curl_cffi + impersonate + 借道 IP 池 + ECH 这条链路整体是通的。
    """
    try:
        from .client import PornhubClient, PornhubError
    except Exception as exc:  # noqa: BLE001 - curl_cffi 缺失时退化
        return CheckResult(NET_NAME, False, f"跳过（{exc.__class__.__name__}: {exc}）")

    client = PornhubClient()
    try:
        info = await asyncio.wait_for(client.health(), timeout=NETWORK_TIMEOUT)
    except asyncio.TimeoutError:
        return CheckResult(
            NET_NAME, False, f"www.pornhub.com 超过 {NETWORK_TIMEOUT:.0f} 秒无响应"
        )
    except PornhubError as exc:
        return CheckResult(NET_NAME, False, f"www.pornhub.com 不可达：{exc.message}")
    except Exception as exc:  # noqa: BLE001 - 未知异常也让自检给出结论
        return CheckResult(
            NET_NAME, False, f"www.pornhub.com 探测失败（{exc.__class__.__name__}: {exc}）"
        )
    finally:
        await client.aclose()

    return CheckResult(NET_NAME, True, f"www.pornhub.com 可达（HTTP {info.get('status_code')}）")


async def _ech_check(settings) -> CheckResult:
    """开了 ECH 才跑：跟 pornhub.com 握手，看 Cloudflare 看到的 SNI 是否真加密。

    判据用 ``/cdn-cgi/trace``：Cloudflare 会在里面回显 ``sni=``，明文是
    ``plaintext``，加密成功才是 ``encrypted``。
    """
    try:
        from . import ech as ech_module
        from .client import PornhubClient, PornhubError
        from .config import as_pornhub_settings, split_ips
    except Exception as exc:  # noqa: BLE001 - curl_cffi 缺失时退化
        return CheckResult(ECH_NAME, False, f"跳过（{exc.__class__.__name__}: {exc}）")

    if not ech_module.available():
        return CheckResult(
            ECH_NAME,
            False,
            "当前 curl_cffi 的 libcurl 不支持 ECH（需要 CURLOPT_ECH）；" + _HINT_ECH,
        )

    site_settings = as_pornhub_settings(settings)
    client = PornhubClient(
        ech=site_settings.ech,
        ech_config=site_settings.ech_config,
        ech_ips=split_ips(site_settings.ech_ips),
    )
    try:
        if not client.ech:
            return CheckResult(ECH_NAME, False, "未开启（PORNHUB_BRIDGE_ECH=0）")
        resp = await asyncio.wait_for(
            client.request("GET", "/cdn-cgi/trace", host="pornhub.com"),
            timeout=ECH_TIMEOUT,
        )
    except asyncio.TimeoutError:
        return CheckResult(
            ECH_NAME, False, f"pornhub.com 握手超过 {ECH_TIMEOUT:.0f} 秒；" + _HINT_ECH
        )
    except PornhubError as exc:
        return CheckResult(ECH_NAME, False, f"pornhub.com 握手失败：{exc.message}；" + _HINT_ECH)
    except Exception as exc:  # noqa: BLE001
        return CheckResult(
            ECH_NAME,
            False,
            f"pornhub.com 握手失败（{exc.__class__.__name__}: {exc}）；" + _HINT_ECH,
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
        ip = client._live_ips.get("pornhub.com") or "?"
        return CheckResult(ECH_NAME, True, f"SNI 已加密（sni=encrypted，IP {ip}）")
    if sni:
        return CheckResult(ECH_NAME, False, f"SNI 仍是明文（sni={sni}）；" + _HINT_ECH)
    return CheckResult(ECH_NAME, False, "取不到 /cdn-cgi/trace 的 sni 行；" + _HINT_ECH)


async def _async_checks(settings) -> list[CheckResult]:
    checks = [_network_check()]
    if getattr(settings, "ech", True):
        checks.append(_ech_check(settings))
    return list(await asyncio.gather(*checks))


def extra_checks(settings) -> list[CheckResult]:
    """``Site.checks`` 要的钩子：站点的自检项。

    环境变量在 ``pornhub/config.py`` 里读（``PornhubSettings``），这里只读
    ``settings`` 上的结果，不重复解析。
    """
    results = [deps_check()]
    results.extend(asyncio.run(_async_checks(settings)))
    return results
