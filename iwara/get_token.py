"""交互式获取 iwara 的 refresh token（可选）。

    python iwara/get_token.py            # 输入邮箱 + 密码
    python iwara/get_token.py --status   # 只看当前登录态
    python iwara/get_token.py --logout   # 删掉已存的 token

为什么需要它：iwara 的**绝大部分内容匿名就能看**（含 R18），登录只为两件事 ——
「订阅作者的内容」和「自己账号下的私密视频」。所以这是可选步骤，不做也能正常跑。

token 存在项目目录的 ``data/iwara_tokens.json``（pwsh：也可以改用环境变量
``IWARA_REFRESH_TOKEN`` 临时覆盖，不改文件）。

注意登录接口只接受邮箱 + 密码，没有浏览器授权那条路；iwara 的登录**不带
Turnstile 验证码**（只有注册接口要 ``X-Captcha``）。密码被拒时上游返回
``400 {"message":"errors.invalidLogin"}``。
"""

import argparse
import asyncio
import getpass
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from site_bridge.deps import ensure_packages  # noqa: E402

from iwara import tokens  # noqa: E402

IWARA_EXTRA_MODULES = ("curl_cffi",)
IWARA_REQUIREMENTS = Path(__file__).resolve().parent / "requirements.txt"


def _ask(prompt, default=""):
    try:
        return input(prompt).strip() or default
    except (EOFError, KeyboardInterrupt):
        print()
        return ""


async def _login(email, password, token_path):
    from iwara.client import IwaraAuthError, IwaraClient, IwaraError

    client = IwaraClient()
    try:
        await client.login(email, password)
        if not client.refresh_token:
            print("登录失败：上游没有返回 token。", file=sys.stderr)
            return 1
        saved = tokens.save_token(client.refresh_token, token_path)
        print()
        print(f"登录成功，refresh token 已保存到: {saved}")
        if client.access_token:
            from iwara.client import jwt_payload

            payload = jwt_payload(client.access_token)
            import time

            exp = payload.get("exp")
            if exp:
                left = int(exp - time.time())
                print(f"access token 有效期剩余约 {left // 60} 分钟（会自动续期）。")
        print("现在可以启动服务了：python iwara/run.py")
        print("公开内容本来就不需要登录；登录后能额外看到订阅作者和私密视频。")
        return 0
    except IwaraAuthError as exc:
        print(f"\n登录失败: {exc.message}", file=sys.stderr)
        if "invalidLogin" in exc.message:
            print("邮箱或密码不对。", file=sys.stderr)
        return 1
    except IwaraError as exc:
        print(f"\n登录失败: {exc.message}", file=sys.stderr)
        if exc.reason == "cloudflare_challenge":
            print(
                "站点把本机 IP 或 TLS 指纹拦了：换个网络/出口 IP 再试。",
                file=sys.stderr,
            )
        return 1
    finally:
        await client.aclose()


async def _status(token_path):
    from iwara.client import IwaraError, IwaraClient, jwt_payload

    refresh = tokens.load_token(token_path)
    if not refresh:
        print(f"未登录（没有 {token_path}）。")
        print("匿名模式可用：公开视频（含 R18）都能看、能解析直链。")
        return 0
    print(f"已找到 token 文件: {token_path}")
    client = IwaraClient(refresh_token=refresh)
    try:
        await client.ensure_access_token()
        payload = jwt_payload(client.access_token or "")
        import time

        exp = payload.get("exp")
        if exp:
            left = int(exp - time.time())
            state = "有效" if left > 0 else "已过期"
            print(f"access token {state}，剩余约 {left // 60} 分钟。")
        else:
            print("access token 已换出（无法读取有效期）。")
        try:
            info = await client.me()
            user = (info or {}).get("user") or {}
            name = user.get("username") or user.get("name")
            print(f"当前账号: {name or '(未返回用户名)'}")
        except IwaraError as exc:
            print(f"取账号信息失败: {exc.message}")
        return 0
    except IwaraError as exc:
        print(f"token 不可用: {exc.message}", file=sys.stderr)
        print("重新登录：python iwara/get_token.py", file=sys.stderr)
        return 1
    finally:
        await client.aclose()


def main():
    parser = argparse.ArgumentParser(description="获取 / 检查 iwara 登录态（可选）")
    parser.add_argument("--status", action="store_true", help="只看当前登录态")
    parser.add_argument("--logout", action="store_true", help="删掉已保存的 token")
    parser.add_argument("--email", help="iwara 账号邮箱（不给就交互输入）")
    parser.add_argument(
        "--token-file", help="token 存放路径（默认项目目录 data/iwara_tokens.json）"
    )
    args = parser.parse_args()
    token_path = Path(args.token_file) if args.token_file else tokens.default_path()

    try:
        ensure_packages(IWARA_EXTRA_MODULES, IWARA_REQUIREMENTS)
    except RuntimeError as exc:
        print(f"[deps] {exc}", file=sys.stderr)
        return 1

    try:
        if args.logout:
            removed = tokens.clear_token(token_path)
            print("已删除 token 文件。" if removed else "本来就没有 token 文件。")
            return 0
        if args.status:
            return asyncio.run(_status(token_path))

        print("iwara 登录（可选，只为订阅内容和私密视频；公开内容匿名就能看）")
        print("提示：登录接口只收邮箱 + 密码，没有浏览器授权。")
        email = args.email or _ask("邮箱: ")
        if not email:
            print("邮箱为空，已取消。")
            return 1
        password = getpass.getpass("密码（输入时不显示）: ")
        if not password:
            print("密码为空，已取消。")
            return 1
        return asyncio.run(_login(email, password, token_path))
    except KeyboardInterrupt:
        print("\n已取消。")
        return 1


if __name__ == "__main__":
    sys.exit(main())
