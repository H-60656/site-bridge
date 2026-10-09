"""iwara 站点的启动自检与登录校验。

框架的通用检查查的是公共环境（Python / 依赖 / 项目文件 / 端口）。这一份补
iwara 自己那几样，通过 ``Site.checks`` 挂进同一张 ``[环境检查]`` 表：

    iwara 依赖   curl_cffi 在不在（编译包，框架 REQUIRED 不含它）
    iwara 网络   api.iwara.tv 通不通（本机 DNS 对 *.iwara.tv 有污染，走种子 IP 池）
    iwara ECH    开了 IWARA_BRIDGE_ECH 才跑：加密 SNI 能不能握手
    iwara 登录   有 token 就验一次；没有就说明匿名模式够用

四个自检项以及 ``login_check`` 都**不中止启动**：没装 curl_cffi 时启动本身会走
``deps.bootstrap`` 自动补，登录态失效也只是退回匿名（iwara 公开内容含 R18 本来就
不需要登录）。``login_check`` 只在 ``--check`` 里用：没配 token 也算通过。
"""

from __future__ import annotations

import asyncio
import os

from pixiv_bridge.envcheck import CheckResult

# 与主项目那五项并列，前缀统一成 iwara，一眼能看出是哪来的。
DEPS_NAME = "iwara 依赖"
NET_NAME = "iwara 网络"
ECH_NAME = "iwara ECH"
AUTH_NAME = "iwara 登录"

# 整个网络检查的上限：里面还含 DoH 汇总与边缘 IP 探测，别卡太久。
NETWORK_TIMEOUT = 25.0
# ECH 只判握手，不需要把整个列表请求跑完，给短一点。
ECH_TIMEOUT = 15.0

_HINT_DEPS = "启动时会自动安装；手动：pip install -r iwara/requirements.txt"
_HINT_ECH = "关掉：.env 里 IWARA_BRIDGE_ECH=0；或配代理 PIXIV_PROXY=..."


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
    """向 api.iwara.tv 真发一个列表请求。

    只探 TCP 不够：这里真正的坎是 Cloudflare 挑战（普通 Python TLS 一律
    403 ``cf-mitigated: challenge``），而能拿到 JSON 恰恰证明 curl_cffi 的
    浏览器指纹 + 种子 IP 固定这条链路是通的。
    """
    try:
        from .client import IwaraClient, IwaraError
    except Exception as exc:  # noqa: BLE001 - curl_cffi 缺失时退化
        return CheckResult(NET_NAME, False, f"跳过（{exc.__class__.__name__}: {exc}）")

    client = IwaraClient()
    try:
        page = await asyncio.wait_for(
            client.list_videos(limit=1, sort="trending"), timeout=NETWORK_TIMEOUT
        )
    except asyncio.TimeoutError:
        return CheckResult(
            NET_NAME, False, f"api.iwara.tv 超过 {NETWORK_TIMEOUT:.0f} 秒无响应"
        )
    except IwaraError as exc:
        return CheckResult(NET_NAME, False, f"api.iwara.tv 不可达：{exc}")
    except Exception as exc:  # noqa: BLE001 - 未知异常也让自检给出结论
        return CheckResult(
            NET_NAME, False, f"api.iwara.tv 探测失败（{exc.__class__.__name__}: {exc}）"
        )
    finally:
        await client.aclose()

    results = page.get("results") if isinstance(page, dict) else None
    count = len(results) if isinstance(results, list) else None
    # 不要把顶层 count 当总数报出去：上游 /videos 的 count 恒等于
    # page*limit+n+1（实测 20 组组合全部成立），根本不是总数。
    # 真正给总数的是 /search 那个端点。这里只报本页条数。
    detail = "api.iwara.tv 可达" + (f"（本页 {count} 个视频）" if count is not None else "")
    return CheckResult(NET_NAME, True, detail)


async def _ech_check(settings) -> CheckResult:
    """开了 ECH 才跑：拿一次 ECH 配置，跟 www.iwara.tv 握手看 SNI 是否真加密。

    判据用 ``/cdn-cgi/trace``：Cloudflare 会在里面回显它看到的 ``sni=``，
    明文是 ``plaintext``，加密成功才是 ``encrypted``。这比「能拿到 JSON」更
    直接 —— 后者在 ECH 没生效、只靠 TLS 冒充时也成立。
    """
    try:
        from . import ech as ech_module
        from .client import IwaraClient, IwaraError
        from .config import as_iwara_settings, split_ips
    except Exception as exc:  # noqa: BLE001 - curl_cffi 缺失时退化
        return CheckResult(ECH_NAME, False, f"跳过（{exc.__class__.__name__}: {exc}）")

    if not ech_module.available():
        return CheckResult(
            ECH_NAME,
            False,
            "当前 curl_cffi 的 libcurl 不支持 ECH（需要 CURLOPT_ECH）；" + _HINT_ECH,
        )

    options = as_iwara_settings(settings)
    client = IwaraClient(
        ech=True, ech_config=options.ech_config, ech_ips=split_ips(options.ech_ips)
    )
    try:
        if not client.ech:
            return CheckResult(ECH_NAME, False, "ECH 未启用；" + _HINT_ECH)
        resp = await asyncio.wait_for(
            client.request("GET", "/cdn-cgi/trace", host=client.site),
            timeout=ECH_TIMEOUT,
        )
    except asyncio.TimeoutError:
        return CheckResult(ECH_NAME, False, f"ECH 握手超过 {ECH_TIMEOUT:.0f} 秒；" + _HINT_ECH)
    except IwaraError as exc:
        return CheckResult(ECH_NAME, False, f"ECH 不可用：{exc}；" + _HINT_ECH)
    except Exception as exc:  # noqa: BLE001
        return CheckResult(
            ECH_NAME, False, f"ECH 探测失败（{exc.__class__.__name__}: {exc}）；" + _HINT_ECH
        )
    finally:
        await client.aclose()

    body = getattr(resp, "text", "") or ""
    encrypted = "sni=encrypted" in body
    if encrypted:
        return CheckResult(ECH_NAME, True, "ECH 可用（www.iwara.tv 回显 sni=encrypted）")
    if getattr(resp, "status_code", 0) >= 400:
        return CheckResult(
            ECH_NAME, False, f"ECH 请求返回 HTTP {resp.status_code}；" + _HINT_ECH
        )
    return CheckResult(ECH_NAME, False, "ECH 未生效（SNI 仍是明文）；" + _HINT_ECH)


# 进程内缓存上一次登录校验的结果，元素是 (token, CheckResult)。
_AUTH_MEMO: tuple[str, CheckResult] | None = None


async def _auth_check(settings) -> CheckResult:
    """有 token 就验一次，没有就说明匿名模式够用。

    ``--check`` 会问两次同一件事：``Site.checks`` 先把它打进 ``[环境检查]`` 表，
    ``Site.login_check`` 再用它定退出码。token 刷新是有副作用的请求，所以按
    token 值在进程内缓存，第二次直接复用结果。
    """
    global _AUTH_MEMO

    from . import tokens

    data_dir = getattr(settings, "data_dir", None)
    token = tokens.resolve(data_dir, env_token=os.environ.get(tokens.ENV_VAR, ""))
    if _AUTH_MEMO is not None and _AUTH_MEMO[0] == token:
        return _AUTH_MEMO[1]

    result = await _auth_check_once(token)
    _AUTH_MEMO = (token, result)
    return result


async def _auth_check_once(token: str) -> CheckResult:
    """真正去验一次登录态（不带缓存，见 ``_auth_check``）。"""
    if not token:
        return CheckResult(
            AUTH_NAME,
            True,
            "匿名模式（公开视频含 R18 照常看）；要看订阅内容跑 python iwara/get_token.py",
        )

    try:
        from .client import IwaraClient, IwaraError
    except Exception as exc:  # noqa: BLE001 - curl_cffi 缺失时退化
        return CheckResult(AUTH_NAME, False, f"跳过（{exc.__class__.__name__}: {exc}）")

    client = IwaraClient(refresh_token=token)
    try:
        await asyncio.wait_for(client.ensure_access_token(), timeout=NETWORK_TIMEOUT)
        user = await asyncio.wait_for(client.me(), timeout=NETWORK_TIMEOUT)
    except asyncio.TimeoutError:
        return CheckResult(AUTH_NAME, False, "验证超时；重新登录：python iwara/get_token.py")
    except IwaraError as exc:
        # token 失效只该退回匿名，不该拦启动。
        return CheckResult(
            AUTH_NAME, False, f"token 不可用：{exc}；重新登录：python iwara/get_token.py"
        )
    except Exception as exc:  # noqa: BLE001
        return CheckResult(
            AUTH_NAME,
            False,
            f"验证失败（{exc.__class__.__name__}: {exc}）；重新登录：python iwara/get_token.py",
        )
    finally:
        await client.aclose()

    payload = user if isinstance(user, dict) else {}
    name = (payload.get("user") or {}).get("username") or "(未知)"
    return CheckResult(AUTH_NAME, True, f"token 有效：{name}")


def extra_checks(settings) -> list[CheckResult]:
    """``Site.checks`` 要的钩子：iwara 自己的自检（开了 ECH 是四项）。

    同步返回，内部自己起一次事件循环（``--check`` 路径本来就是同步的）。每次
    都清掉登录缓存，好让紧接着的 ``login_check``（只复用、不重跑）用上这次的
    结果，而重复调用本函数仍然各自真验一次。
    """
    global _AUTH_MEMO

    _AUTH_MEMO = None
    results = [deps_check()]
    results.extend(asyncio.run(_async_checks(settings)))
    return results


async def _async_checks(settings) -> list[CheckResult]:
    checks = [_network_check()]
    if getattr(settings, "ech", False):
        checks.append(_ech_check(settings))
    checks.append(_auth_check(settings))
    return list(await asyncio.gather(*checks))


def login_check(settings) -> int:
    """``Site.login_check`` 要的钩子：``--check`` 时验一次 iwara 登录态。

    没配 token 时 ``_auth_check`` 返回「匿名模式」且算通过 —— 匿名本来就能看
    公开内容（含 R18）。但如果配了 token 却验不过（失效 / 网络失败），就给非零
    退出码：用户专门跑 ``--check``，多半是想确认登录态真的可用。

    这里**不重新打印**那一行：``Site.checks`` 已经把它打进 ``[环境检查]`` 表了，
    而且 ``_auth_check`` 的进程内缓存会让这次调用直接复用上一次的结果。
    """
    result = asyncio.run(_auth_check(settings))
    return 0 if result.ok else 1
