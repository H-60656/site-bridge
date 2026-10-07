import asyncio
import socket
import ssl
import sys
import types
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlparse

from . import deps, ech_client
from .hoster import resolve, resolve_ech

MIN_PYTHON = (3, 10)
CHECK_HOSTS = ("app-api.pixiv.net", "oauth.secure.pixiv.net", "i.pximg.net")
API_BASE = "https://app-api.pixiv.net"
CONNECT_TIMEOUT = 5.0
# 接口项目自身的文件（阅读器页面属于 novel/ 子项目，不在接口项目的必需清单里）
REQUIRED_FILES = ("run.py", "requirements.txt")
ECH_HINT = "；ECH 模式需要系统安装 OpenSSL 3.5+ 的 libssl（pip 装不了），见 README「ECH 模式」"
# 链路阻断 ECH 时的典型特征：握手在 ClientHello 之后被 RST（SSL_ERROR_SYSCALL=5）。
ECH_BLOCKED_HINT = (
    "；握手被重置通常说明本机到 Cloudflare 这一段在阻断 ECH 扩展"
    "（只与本地网络/运营商有关，与配置、IP 和服务端无关；Pixiv/Cloudflare 自身的 ECH 支持正常），"
    "同一台机器也可能时通时断；可重试、换网络/手机热点，或设置 "
    "PIXIV_PROXY=http://127.0.0.1:7890 走代理"
)


@dataclass
class CheckResult:
    name: str
    ok: bool
    detail: str


def python_version():
    ok = sys.version_info >= MIN_PYTHON
    return CheckResult(
        "python", ok, f"{sys.version.split()[0]} (需要 3.10+)"
    )


def dependencies():
    missing = deps.missing_dependencies()
    detail = ", ".join(missing) if missing else "依赖完整"
    return CheckResult("deps", not missing, detail)


def project_files(project_root=None):
    """检查运行必需的项目文件是否随文件夹一起拷齐（移植自检）。"""
    root = Path(project_root) if project_root else Path(__file__).resolve().parent.parent
    missing = [name for name in REQUIRED_FILES if not (root / name).is_file()]
    if missing:
        return CheckResult(
            "files",
            False,
            f"缺少 {', '.join(missing)}（接口仍可运行，但 Web 页面或依赖自动安装会受影响）",
        )
    return CheckResult("files", True, "项目文件完整")


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


def port_available(port):
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        try:
            sock.bind(("127.0.0.1", port))
            return CheckResult("port", True, f"{port} 可用")
        except OSError:
            return CheckResult("port", False, f"{port} 已被占用")


def ech_transport(ech_config="", ech_ips="", api_base=API_BASE):
    """真正做一次 ECH 请求：ECH 模式下这才是「能不能用」的判据。

    明文 SNI 被链路阻断时 TCP/TLS 层的探测（network）必然失败，
    但 ECH 走的是一次完整握手 + 一个真实 API 请求，任意 HTTP 状态码
    都说明加密 SNI 的通道成立（400 = Pixiv 在应答未认证的请求）。
    """
    try:
        if not ech_client.available():
            return CheckResult(
                "ech", False, "未找到支持 ECH 的 OpenSSL（需要 3.5+ 的 libssl）" + ECH_HINT
            )
    except ech_client.EchUnavailable as exc:
        return CheckResult("ech", False, str(exc) + ECH_HINT)
    host = urlparse(api_base).hostname
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
    return CheckResult("ech", True, f"ECH 传输可用（{host} 返回 HTTP {resp.status_code}）")


def proxy_transport(proxy, timeout=10.0, api_base=API_BASE):
    """配了代理时，用一次真实请求判断代理是否通（ECH 被链路阻断时的常用出路）。"""
    import httpx

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


def run_checks(
    port=None,
    api_ips="",
    oauth_ips="",
    image_ips="",
    ech=False,
    ech_config="",
    ech_ips="",
    proxy="",
    api_base=API_BASE,
    project_root=None,
):
    results = [python_version(), dependencies(), project_files(project_root)]
    if port is not None:
        results.append(port_available(port))
    if proxy:
        results.append(proxy_transport(proxy, api_base=api_base))
    elif ech:
        results.append(ech_transport(ech_config, ech_ips, api_base))
    else:
        results.append(network(api_ips, oauth_ips, image_ips))
    return results
