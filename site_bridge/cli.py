"""命令行入口：解析参数、跑环境自检、按配置挂扩展、交给 uvicorn 启动。

入口不认识任何站点：站点通过 :class:`site_bridge.site.Site`（``<site>/app.py`` 里的
``SITE``）把自己的应用工厂、专属自检、登录校验与网络提示传进来，入口只负责
「参数 → 配置 → 自检 → 应用 → uvicorn」这条流水线。
"""

import argparse
import sys

import uvicorn

from .deps import ensure_dependencies
from .envcheck import REQUIRED_FILES, run_checks
from .extensions import ExtensionError, load_extensions

FATAL_CHECKS = ("python", "deps", "port")

NO_SITE_HINT = (
    "请用具体接口项目的入口启动：python pixiv/run.py 或 python iwara/run.py"
    "（也可以 python -m site_bridge <站点>）"
)


def _parse_args(argv=None, prog="bridge", description="接口服务"):
    parser = argparse.ArgumentParser(prog=prog, description=description)
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


def _print_extensions(extensions):
    """报告按配置挂上的附属项目（扩展）。一个都没有就不打印。"""
    if not extensions:
        return
    print(f"[扩展] 已挂载 {len(extensions)} 个：", flush=True)
    for ext in extensions:
        print(f"  {ext.name:<8} {ext.entry}（{len(ext.routers)} 个路由）", flush=True)


def load_extensions_or_exit(settings):
    """按配置加载扩展，打印清单；配置写错就退出。

    任何入口都用它 —— 对「扩展加载失败」的处理必须一致：给一行可读报错并
    ``sys.exit(1)``，而不是把栈甩给用户（配置写错目录 / 目录里没有 extension.py 等）。
    """
    try:
        extensions = load_extensions(settings)
    except ExtensionError as exc:
        print(f"[扩展] {exc}", file=sys.stderr, flush=True)
        sys.exit(1)
    _print_extensions(extensions)
    return extensions


def main(argv=None, site=None):
    """启动一个接口项目。

    `site` 是 :class:`site_bridge.site.Site`：站点入口脚本（``<site>/run.py``）先
    bootstrap 站点额外依赖，再把 `SITE` 传进来。框架本身不是服务，所以 `site=None`
    直接报错退出。

    这条流水线对每个站点都一样：读配置（站点可以用自己的 Settings 子类）→ 通用自检
    + ``site.environment_checks`` → 按 ``SITE_BRIDGE_EXTENSIONS`` 挂扩展 →
    ``site.check_login``（``--check``）或 ``site.build_app`` + uvicorn。
    """
    if site is None:
        print(NO_SITE_HINT, file=sys.stderr, flush=True)
        sys.exit(2)
    args = _parse_args(argv, prog=site.name, description=f"{site.title} 直连接口服务")
    settings = site.load_settings(args.env)
    if args.host:
        settings.host = args.host
    if args.port:
        settings.port = args.port
    ensure_dependencies()
    results = run_checks(
        port=settings.port,
        files=(
            (settings.project_root, REQUIRED_FILES),
            (site.directory, site.required_files),
        ),
    )
    # 站点专属的自检（pixiv：网络 / ECH / 代理；iwara：curl_cffi / api.iwara.tv /
    # 登录态）跟通用检查打在同一张表里 —— 它们都不在 FATAL_CHECKS 里，只提示不中止。
    results.extend(site.environment_checks(settings))

    _print_checks(results)
    _print_warnings(settings)
    # --check 只校验，不监听端口：端口被占只提示，最终启动时才中止。
    _fail_on(results, skip=("port",) if args.check else ())
    site.warn_network(results, settings)

    # 扩展按配置加载（`SITE_BRIDGE_EXTENSIONS`）；加载失败给一行可读报错。
    extensions = load_extensions_or_exit(settings)

    if args.check:
        sys.exit(site.check_login(settings))
    app = site.build_app(settings, extensions)
    uvicorn.run(app, host=settings.host, port=settings.port, reload=args.reload)


if __name__ == "__main__":
    main()
