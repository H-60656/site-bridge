"""交互式获取 Pixiv refresh_token（傻瓜式）。

用法：
    python pixiv/get_token.py              # 交互式选择登录方式
    python pixiv/get_token.py --password   # 直接用账号密码登录

成功后 refresh_token 会写入项目数据目录 data/tokens.json（项目启动时优先读取它），
不需要手工编辑 .env。
"""

import argparse
import asyncio
import getpass
import sys
from pathlib import Path

# 直接 `python pixiv/get_token.py` 时 sys.path[0] 是 pixiv/，框架包在它的上一级。
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from site_bridge.deps import ensure_dependencies

try:
    ensure_dependencies()
except RuntimeError as exc:
    print(f"[deps] {exc}", file=sys.stderr)
    sys.exit(1)

import httpx  # noqa: E402

from pixiv.config import PixivSettings as Settings  # noqa: E402
from pixiv.oauth import AuthError, OAuthManager  # noqa: E402

# 项目根（`.env` 与数据目录都在这里）；本文件在 pixiv/ 下，所以取上一级。
PROJECT_ROOT = Path(__file__).resolve().parent.parent


def _ask(prompt, default=""):
    try:
        return input(prompt).strip() or default
    except (EOFError, KeyboardInterrupt):
        print()
        return ""


def _extract_code(raw):
    """从粘贴内容里取出授权码：支持整条回调网址或纯 code。"""
    raw = raw.strip().strip('"').strip("'")
    if "code=" in raw:
        raw = raw.split("code=", 1)[1]
    return raw.split("&", 1)[0].split("#", 1)[0].strip()


async def _login_with_password(oauth):
    print()
    print("输入 Pixiv 账号（邮箱 / pixiv ID）和密码。")
    print("提示：Pixiv 对密码登录有风控，若提示验证码或错误码，请改用浏览器授权。")
    username = _ask("账号: ")
    password = getpass.getpass("密码（输入时不显示）: ")
    if not username or not password:
        print("账号或密码为空，已取消。")
        return None
    return await oauth.login_password(username, password)


async def _login_with_browser(oauth):
    url, verifier = oauth.login_url()
    print()
    print("第 1 步：把下面这个网址复制到浏览器打开，登录并点「授权」：")
    print()
    print(url)
    print()
    print("第 2 步：授权后浏览器会跳到 app-api.pixiv.net 的回调页（页面空白或报错都正常）。")
    print("        复制地址栏里的完整网址（含 code=...）粘贴到下面，也可以只粘 code 的值。")
    print()
    raw = _ask("粘贴: ")
    code = _extract_code(raw)
    if not code:
        print("没有识别到 code，已取消。")
        return None
    return await oauth.exchange_code(code, verifier)


async def _run(args):
    root = Path(args.env).expanduser() if args.env else PROJECT_ROOT
    settings = Settings.load(root)
    mode = (
        f"代理（{settings.proxy}）"
        if settings.proxy
        else "ECH（加密 SNI）"
        if settings.ech
        else "SNI 绕过"
        if settings.bypass_sni
        else "标准 HTTPS（系统 DNS 直连）"
    )
    print(f"项目目录: {root}")
    print(f"token 文件: {settings.token_file}")
    print(f"网络模式: {mode}")
    if args.password:
        choice = "1"
    else:
        print()
        print("请选择登录方式：")
        print("  1) 账号密码（最快，但 Pixiv 可能要求验证码）")
        print("  2) 浏览器授权（推荐，最稳）")
        choice = _ask("输入 1 或 2 [2]: ", "2")
    async with httpx.AsyncClient(proxy=settings.proxy or None) as client:
        oauth = OAuthManager(settings, client)
        if choice == "1":
            response = await _login_with_password(oauth)
        else:
            response = await _login_with_browser(oauth)
    if not response:
        return 1
    user = response.get("user") or {}
    name = user.get("name") or "(未知)"
    account = user.get("account") or ""
    print()
    print(f"登录功功: {name} {account}".rstrip())
    print(f"refresh_token 已保存到: {settings.token_file}")
    print("现在可以启动服务了：python pixiv/run.py")
    return 0


def main():
    parser = argparse.ArgumentParser(description="交互式获取 Pixiv refresh_token")
    parser.add_argument(
        "--env", help=".env 所在目录或 .env 文件路径（默认脚本所在目录）"
    )
    parser.add_argument(
        "--password", action="store_true", help="跳过选择，直接用账号密码登录"
    )
    args = parser.parse_args()
    try:
        code = asyncio.run(_run(args))
    except AuthError as exc:
        print(f"\n登录失败: {exc.message}", file=sys.stderr)
        if exc.reason == "connect_failed":
            print(
                "本机连不上 Pixiv（网络被阻断）。请先按 INSTALL.md 开启 ECH 或设置代理，再重试。",
                file=sys.stderr,
            )
        code = 1
    except KeyboardInterrupt:
        print("\n已取消。")
        code = 1
    sys.exit(code)


if __name__ == "__main__":
    main()
