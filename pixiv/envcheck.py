"""Pixiv 站点的专属自检、登录校验与失败提示。

通用自检（Python / 依赖 / 必需文件 / 端口）在框架 ``pixiv_bridge.envcheck`` 里；
这里只有 Pixiv 才需要的部分：网络可达性、代理、ECH 直连探测、``--check`` 的
真实 OAuth 刷新，以及失败时该给用户什么提示。

自检项的名字（``network`` / ``ech`` / ``proxy``）与重构前一致 —— 它们都不在
``FATAL_CHECKS`` 里，只提示不中止。
"""

import asyncio
import socket
import ssl
import types
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from urllib.parse import urlparse

import httpx

from pixiv_bridge.envcheck import CheckResult

from . import ech_client
from .hoster import resolve, resolve_ech
from .oauth import OAuthManager

CHECK_HOSTS = ("app-api.pixiv.net", "oauth.secure.pixiv.net", "i.pximg.net")
API_BASE = "https://app-api.pixiv.net"
CONNECT_TIMEOUT = 5.0
ECH_HINT = (
    "；项目自带 openssl-libs/（随包附带的 OpenSSL 4.x，Windows 与 Termux 都已带对应子目录），"
    "若该平台没有对应子目录、或自带库载入失败，再安装系统 OpenSSL 4.0+ 的 libssl"
    "（pip 装不了；注意 3.x 全系都没有 ECH），见 README「ECH 模式」"
)
# 链路阻断 ECH 时的典型特征：握手在 ClientHello 之后被 RST（SSL_ERROR_SYSCALL=5）。
ECH_BLOCKED_HINT = (
    "；握手被重置通常说明本机到 Cloudflare 这一段在阻断 ECH 扩展"
    "（只与本地网络/运营商有关，与配置、IP 和服务端无关；Pixiv/Cloudflare 自身的 ECH 支持正常），"
    "同一台机器也可能时通时断；可重试、换网络/手机热点，或设置 "
    "PIXIV_PROXY=http://127.0.0.1:7890 走代理"
)

MISSING_TOKEN_HINT = (
    "  提示: 没有 refresh_token 也能用接口（公开内容照常返回），但登录态、"
    "关注流这些要用到它 —— 在项目目录跑一次 `python pixiv/get_token.py` 即可"
    "（浏览器授权，结果写进 data/tokens.json），或在 .env 里直接填 PIXIV_REFRESH_TOKEN"
)


def _probe(host, extra_ips=""):
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    for ip in resolve(host, extra_ips):
        try:
            with socket.create_connection((ip, 443), CONNECT_TIMEOUT) as sock:
                with ctx.wrap_socket(sock):
                    return host
        except (OSError, ssl.SSLError):
            continue
    return None


def network(api_ips="", oauth_ips="", image_ips=""):
    # 自检必须使用与真实请求相同的 IP 池，否则自定义 *_IPS 时探测结论会与实际行为矛盾。
    overrides = {
        "app-api.pixiv.net": api_ips,
        "oauth.secure.pixiv.net": oauth_ips,
        "i.pximg.net": image_ips,
    }
    with ThreadPoolExecutor(max_workers=len(CHECK_HOSTS)) as pool:
        reachable = [
            host
            for host in pool.map(
                lambda host: _probe(host, overrides.get(host, "")), CHECK_HOSTS
            )
            if host
        ]
    detail = (
        f"可达 {len(reachable)}/{len(CHECK_HOSTS)}: "
        f"{', '.join(reachable) if reachable else '全部不可达'}"
    )
    if "app-api.pixiv.net" not in reachable:
        detail += "（API 域名不可达，接口将无法使用）"
    return CheckResult("network", "app-api.pixiv.net" in reachable, detail)


def _openssl_source_label():
    """说明这次 ECH 用的是哪份 OpenSSL：项目自带还是系统装的。

    载入失败时 `library_path()` 返回空串，此时退回「找不到」的提示。
    """
    path = ech_client.library_path()
    if not path:
        return ""
    try:
        inside_bundle = ech_client.bundle_root() in Path(path).resolve().parents
    except OSError:  # pragma: no cover - 路径解析失败不该影响自检结论
        inside_bundle = False
    return "项目自带" if inside_bundle else f"系统安装 {Path(path).parent}"


def ech_transport(ech_config="", ech_ips="", api_base=API_BASE):
    """真正做一次 ECH 请求：ECH 模式下这才是「能不能用」的判据。

    明文 SNI 被链路阻断时 TCP/TLS 层的探测（network）必然失败，
    但 ECH 走的是一次完整握手 + 一个真实 API 请求，任意 HTTP 状态码
    都说明加密 SNI 的通道成立（400 = Pixiv 在应答未认证的请求）。
    """
    try:
        if not ech_client.available():
            return CheckResult(
                "ech", False, "未找到支持 ECH 的 OpenSSL（需要 4.0+ 的 libssl）" + ECH_HINT
            )
    except ech_client.EchUnavailable as exc:
        return CheckResult("ech", False, str(exc) + ECH_HINT)
    host = urlparse(api_base).hostname
    source = _openssl_source_label()
    probe = types.SimpleNamespace(ech_config=ech_config)
    try:
        resp = asyncio.run(
            ech_client.request(
                "GET",
                f"{api_base}/v1/illust/detail?illust_id=1",
                headers={"Accept-Language": "zh-CN"},
                timeout=CONNECT_TIMEOUT * 3,
                ips=resolve_ech(host, ech_ips),
                settings=probe,
            )
        )
    except ech_client.EchUnavailable as exc:
        return CheckResult("ech", False, str(exc))
    except Exception as exc:
        detail = f"ECH 直连失败: {exc.__class__.__name__}: {exc}"
        if "SSL_get_error=5" in detail or "SyscallError" in detail:
            detail += ECH_BLOCKED_HINT
        return CheckResult("ech", False, detail[:400])
    suffix = f"，OpenSSL：{source}" if source else ""
    return CheckResult(
        "ech", True, f"ECH 传输可用（{host} 返回 HTTP {resp.status_code}{suffix}）"
    )


def proxy_transport(proxy, timeout=10.0, api_base=API_BASE):
    """配了代理时，用一次真实请求判断代理是否通（ECH 被链路阻断时的常用出路）。"""
    host = urlparse(api_base).hostname
    try:
        resp = httpx.get(
            f"{api_base}/v1/illust/detail",
            params={"illust_id": "1"},
            headers={"Accept-Language": "zh-CN"},
            timeout=timeout,
            proxy=proxy,
        )
    except Exception as exc:  # noqa: BLE001
        return CheckResult("proxy", False, f"代理不可用: {exc.__class__.__name__}")
    return CheckResult(
        "proxy", True, f"代理可用（{host} 返回 HTTP {resp.status_code}）"
    )


def extra_checks(settings):
    """站点专属自检项：代理 / ECH / 网络探测（选哪条与重构前一致）。

    它们都不在 ``FATAL_CHECKS`` 里，所以只提示、不中止启动。
    """
    api_base = getattr(settings, "api_base", API_BASE)
    if settings.proxy:
        return [proxy_transport(settings.proxy, api_base=api_base)]
    if settings.ech:
        return [ech_transport(settings.ech_config, settings.ech_ips, api_base)]
    return [network(settings.api_ips, settings.oauth_ips, settings.image_ips)]


def network_hint(results, settings):
    """网络类自检失败时，按当前配置给出下一步该怎么做。"""
    network = next(
        (result for result in results if result.name in ("network", "ech", "proxy")),
        None,
    )
    if not (network and not network.ok):
        return
    if settings.proxy:
        print(
            f"  提示: 代理 {settings.proxy} 不可用，检查代理软件是否在运行、"
            "端口是否写对；socks5:// 需要先 pip install \"httpx[socks]\"",
            flush=True,
        )
    elif settings.ech:
        print(
            "  提示: ECH 模式仍无法直连。若错误里是 SSL_get_error=5（握手被重置），"
            "说明本机到 Cloudflare 这一段在阻断 ECH 扩展（只与本地网络/运营商有关，"
            "与服务端无关；同一台机器也可能时通时断），可重试、换网络/手机热点，"
            "或设置 PIXIV_PROXY=http://127.0.0.1:7890 走代理；"
            "也可检查 PIXIV_BRIDGE_ECH_CONFIG / PIXIV_BRIDGE_ECH_IPS，"
            "或安装带 ECH 支持的 OpenSSL（4.0+）",
            flush=True,
        )
    elif settings.bypass_sni:
        print(
            "  提示: SNI 绕过已开启仍无法直连，可改用 PIXIV_BRIDGE_ECH=1（加密 SNI），"
            "或设置 PIXIV_PROXY 走代理",
            flush=True,
        )
    else:
        print(
            "  提示: 无法直连 Pixiv，可设置 PIXIV_BRIDGE_ECH=1 走加密 SNI，"
            "或设置 PIXIV_BRIDGE_BYPASS_SNI=1 开启 SNI 绕过，或设置 PIXIV_PROXY",
            flush=True,
        )


def _refresh_hint(settings, exc):
    """刷新失败时，指出是「本地链路」还是「服务端」的问题。"""
    text = f"{exc.__class__.__name__}: {exc}"
    if "getaddrinfo" in text or "NameResolution" in text or "No address" in text:
        return (
            "  提示: 域名解析失败（getaddrinfo）是**本地 DNS 问题**，不是服务端问题——"
            "系统解析被污染时普通模式必然失败；请设置 PIXIV_BRIDGE_ECH=1（ECH 用 DoH 解析，"
            "不走系统 DNS）或 PIXIV_PROXY 走代理"
        )
    if "SSL_get_error=5" in text or "SyscallError" in text or "EchConnectError" in text:
        return (
            "  提示: 握手被重置说明**本机到 Cloudflare 这一段**在阻断 ECH 扩展"
            "（Pixiv/Cloudflare 服务端正常，同一台机器也可能时通时断），可换网络/手机热点，"
            "或设置 PIXIV_PROXY=http://127.0.0.1:7890 走代理"
        )
    if isinstance(exc, (httpx.ConnectTimeout, httpx.ConnectError, httpx.ReadTimeout)):
        return (
            "  提示: 连不上 Pixiv（连接失败/超时）属于**本地链路问题**，"
            "不是服务端问题；可重试、换网络，或设置 PIXIV_PROXY 走代理"
        )
    status = getattr(exc, "status", None)
    if status in (401, 403):
        return (
            f"  提示: 服务端返回 HTTP {status}，属于**账号/权限问题**（token 失效、"
            "作品不可见或接口变更），与本地网络无关；可重新获取 refresh_token 后再试"
        )
    if status:
        return (
            f"  提示: 服务端返回 HTTP {status}，请求已经到达 Pixiv（说明本地链路是通的），"
            "属于**服务端问题**"
        )
    return ""


async def _check(settings):
    async with httpx.AsyncClient(proxy=settings.proxy or None) as http:
        oauth = OAuthManager(settings, http)
        await oauth.load()
        if not oauth.refresh_token:
            print(
                "未配置 refresh_token（PIXIV_REFRESH_TOKEN 或 tokens.json）",
                flush=True,
            )
            print(MISSING_TOKEN_HINT, flush=True)
            return 1
        try:
            await oauth.refresh()
        except Exception as exc:
            print(f"刷新失败: {exc}", flush=True)
            print(_refresh_hint(settings, exc), flush=True)
            return 1
        user = oauth.user or {}
        print(f"登录成功: {user.get('name')} ({user.get('account')})", flush=True)
        return 0


def login_check(settings):
    """``--check``：真刷一次 Pixiv OAuth token，返回进程退出码。"""
    return asyncio.run(_check(settings))
