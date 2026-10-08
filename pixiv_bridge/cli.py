"""命令行入口：解析参数、跑环境自检、按配置挂扩展、交给 uvicorn 启动。

默认入口（调用方不传 ``app_factory``）在这里读 ``PIXIV_BRIDGE_EXTENSIONS``
并挂扩展；传了 ``app_factory`` 的调用方自己负责组装（``iwara/run.py`` 走这条）。
"""

import argparse
import asyncio
import sys

import httpx
import uvicorn

from .config import Settings
from .deps import ensure_dependencies
from .envcheck import run_checks
from .extensions import ExtensionError, load_extensions
from .oauth import OAuthManager
from .server import create_app

FATAL_CHECKS = ("python", "deps", "port")

MISSING_TOKEN_HINT = (
    "  提示: 没有 refresh_token 也能用接口（公开内容照常返回），但登录态、"
    "关注流这些要用到它 —— 在项目目录跑一次 `python get_token.py` 即可"
    "（浏览器授权，结果写进 data/tokens.json），或在 .env 里直接填 PIXIV_REFRESH_TOKEN"
)


def _parse_args(argv=None):
    parser = argparse.ArgumentParser(
        prog="pixiv-bridge", description="Pixiv App API 直连接口服务"
    )
    parser.add_argument(
        "--host", default=None, help="监听地址，默认取环境变量或 127.0.0.1"
    )
    parser.add_argument(
        "--port", type=int, default=None, help="监听端口，默认取环境变量或 8765"
    )
    parser.add_argument("--reload", action="store_true", help="开发模式自动重载")
    parser.add_argument("--check", action="store_true", help="校验登录状态后退出")
    parser.add_argument(
        "--env",
        default=None,
        help=".env 文件路径，或包含 .env 的目录路径，默认使用当前目录下的 .env",
    )
    return parser.parse_args(argv)


def _print_checks(results):
    print("[环境检查]", flush=True)
    for result in results:
        mark = "OK  " if result.ok else "WARN"
        print(f"  {result.name:<8} {mark} {result.detail}", flush=True)


def _print_warnings(settings):
    """启动时把 Settings 里的提醒打出来（例如项目外的数据目录被收回 data/）。"""
    for note in getattr(settings, "warnings", ()) or ():
        print(f"  ! {note}", flush=True)


def _fail_on(results, skip=()):
    """有致命检查不通过就中止启动。

    `skip` 用于 ``--check``：那条路上不监听端口，端口被占只该提示，
    不该拦住「只校验登录」的用法（真正启动时才中止）。
    """
    failed = [
        result
        for result in results
        if not result.ok and result.name in FATAL_CHECKS and result.name not in skip
    ]
    if not failed:
        return
    for result in failed:
        print(f"  x {result.name}: {result.detail}", flush=True)
    print("环境不满足要求，启动中止", flush=True)
    sys.exit(1)


def _warn_network(results, settings):
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


def _print_extensions(extensions):
    """报告按配置挂上的附属项目（扩展）。一个都没有就不打印。"""
    if not extensions:
        return
    print(f"[扩展] 已挂载 {len(extensions)} 个：", flush=True)
    for ext in extensions:
        print(f"  {ext.name:<8} {ext.entry}（{len(ext.routers)} 个路由）", flush=True)


def load_extensions_or_exit(settings):
    """按配置加载扩展，打印清单；配置写错就退出。

    默认入口用它，自己组装应用的独立入口（`iwara/run.py`）也用它 ——
    两边对「扩展加载失败」的处理必须一致：给一行可读报错并 `sys.exit(1)`，
    而不是把栈甩给用户（配置写错目录 / 目录里没有 extension.py 等）。
    """
    try:
        extensions = load_extensions(settings)
    except ExtensionError as exc:
        print(f"[扩展] {exc}", file=sys.stderr, flush=True)
        sys.exit(1)
    _print_extensions(extensions)
    return extensions


def main(argv=None, app_factory=None, extra_checks=None):
    """启动服务。

    默认 `app_factory` 就是主项目的 `create_app`，并按配置
    （`PIXIV_BRIDGE_EXTENSIONS`）把附属项目（扩展，例如 `novel`）挂上去 ——
    也就是说「把扩展目录放进项目目录 + 在配置里写它的相对路径」就够了，
    不需要专门的入口脚本。

    传了 `app_factory(settings)` 的调用方自己负责组装（`iwara/run.py` 走这条，
    它不是 pixiv 接口项目的扩展，而是自己独立的接口项目）；这类入口要挂扩展的话，
    在自己的 factory 里调 `load_extensions_or_exit(settings)` 再传给 `create_app`
    （`iwara/run.py` 就是这么挂 `iwara_web` 页面扩展的）。这类入口还可以传
    `extra_checks(settings) -> list[CheckResult]`，把自己专属的自检项追加到
    `[环境检查]` 里（`iwara/run.py` 用它报告 curl_cffi、api.iwara.tv 与 iwara 登录态）。
    """
    args = _parse_args(argv)
    settings = Settings.load(args.env)
    if args.host:
        settings.host = args.host
    if args.port:
        settings.port = args.port
    ensure_dependencies()
    results = run_checks(
        port=settings.port,
        api_ips=settings.api_ips,
        oauth_ips=settings.oauth_ips,
        image_ips=settings.image_ips,
        ech=settings.ech,
        ech_config=settings.ech_config,
        ech_ips=settings.ech_ips,
        proxy=settings.proxy,
        api_base=settings.api_base,
        project_root=settings.project_root,
    )
    if extra_checks is not None:
        # 入口专属的自检（iwara：curl_cffi / api.iwara.tv / iwara 登录态）跟通用检查
        # 打在同一张表里 —— 它们都不在 FATAL_CHECKS 里，所以只提示、不中止。
        results.extend(extra_checks(settings))

    _print_checks(results)
    _print_warnings(settings)
    # --check 只校验，不监听端口：端口被占只提示，最终启动时才中止。
    _fail_on(results, skip=("port",) if args.check else ())
    _warn_network(results, settings)

    if app_factory is None:
        # 扩展只在默认入口这一条路上按配置加载；加载失败给一行可读报错，
        # 而不是把栈甩给用户（配置写错目录 / 目录里没有 extension.py 等）。
        extensions = load_extensions_or_exit(settings)
        app_factory = lambda settings: create_app(  # noqa: E731 - 就地绑定已加载的扩展
            settings, extensions=extensions
        )

    if args.check:
        sys.exit(asyncio.run(_check(settings)))
    app = app_factory(settings)
    uvicorn.run(app, host=settings.host, port=settings.port, reload=args.reload)


if __name__ == "__main__":
    main()
