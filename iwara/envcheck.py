"""iwara 入口专属的启动自检。

主项目的 ``--check`` 查的是公共环境（Python / 依赖 / 项目文件 / 端口 /
网络 / ECH）。这一份只补 iwara 自己那三样，通过
``pixiv_bridge.cli.main(extra_checks=...)`` 挂进同一张 ``[环境检查]`` 表：

    iwara 依赖   curl_cffi 在不在（编译包，主项目 REQUIRED 不含它）
    iwara 网络   api.iwara.tv 通不通（本机 DNS 对 *.iwara.tv 有污染，走种子 IP 池）
    iwara 登录   有 token 就验一次；没有就说明匿名模式够用

三项名字都**不在** ``FATAL_CHECKS`` 里，所以永远只提示、不中止启动：
没装 curl_cffi 时启动本身会走 ``deps.bootstrap`` 自动补，登录态失效也只是
退回匿名（iwara 公开内容含 R18 本来就不需要登录）。
"""

from __future__ import annotations

import asyncio
import os

from pixiv_bridge.envcheck import CheckResult

# 与主项目那五项并列，前缀统一成 iwara，一眼能看出是哪来的。
DEPS_NAME = "iwara 依赖"
NET_NAME = "iwara 网络"
AUTH_NAME = "iwara 登录"

# 整个网络检查的上限：里面还含 DoH 汇总与边缘 IP 探测，别卡太久。
NETWORK_TIMEOUT = 25.0

_HINT_DEPS = "启动时会自动安装；手动：pip install -r iwara/requirements.txt"


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

    count = page.get("count") if isinstance(page, dict) else None
    detail = "api.iwara.tv 可达" + (f"（共 {count} 个视频）" if count is not None else "")
    return CheckResult(NET_NAME, True, detail)


async def _auth_check(settings) -> CheckResult:
    """有 token 就验一次，没有就说明匿名模式够用。"""
    from . import tokens

    data_dir = getattr(settings, "data_dir", None)
    token = tokens.resolve(data_dir, env_token=os.environ.get(tokens.ENV_VAR, ""))
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
    """``pixiv_bridge.cli.main(extra_checks=...)`` 要的钩子。

    同步返回，内部自己起一次事件循环（``--check`` 路径本来就是同步的）。
    """
    results = [deps_check()]
    results.extend(asyncio.run(_async_checks(settings)))
    return results


async def _async_checks(settings) -> list[CheckResult]:
    return list(await asyncio.gather(_network_check(), _auth_check(settings)))
