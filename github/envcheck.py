"""github 站点的环境自检。

只有三项 —— 这个站点没有穿透层要验：

- **依赖**：``httpx`` 能不能真 import（用真实 import 而不是 ``find_spec``：编译包
  装上了却 ABI 不匹配时，``find_spec`` 会说「有」）；
- **接口**：真的打一次 ``/rate_limit``，detail 里报出配额还剩多少 —— 这是唯一
  能证明「能用」的判据，顺便把 60 次/小时这个现实摆在用户面前；
- **搜索**：横向对照。``search`` 有**独立的**配额（未认证 10 次/小时），而且它的
  参数很容易静默失效 —— 所以拿一个**必定查不到**的词问一次，必须 ``total_count``
  为 0；再拿一个**必定有结果**的词问一次，必须非 0。两次都对，才说明 ``q`` 真的
  传到了上游、绑定没被吃掉（DIRECT.md 记过这个坑：只看「有结果」是看不出没过滤的）。

三项都**不吞异常**：失败就是把 ``ok=False`` 摆出来，不假装通过。
"""

from __future__ import annotations

import asyncio

from pixiv_bridge.envcheck import CheckResult

from .client import GithubClient, GithubError
from .config import as_github_settings

DEPS_NAME = "github 依赖"
API_NAME = "github 接口"
SEARCH_NAME = "github 搜索"

API_TIMEOUT = 20.0
SEARCH_TIMEOUT = 30.0

#: 必定查不到的词 —— 它的存在只为证明「过滤真的生效了」。
SEARCH_PROBE_MISS = "zzzzqqxxnotexist9999"
#: 必定有结果的词 —— 证明绑定没被吃掉、请求真的发出去了。
SEARCH_PROBE_HIT = "requests"

_HINT_DEPS = "pip install -r requirements.txt（httpx 是根清单里的依赖，不需要单独装）"
_HINT_API = (
    "确认 api.github.com 能直连；若是 403，先看 /api/github/auth 报的配额是否已用尽，"
    "未认证只有 60 次/小时，配 GITHUB_TOKEN 可提到 5000"
)
_HINT_SEARCH = (
    "search 配额是独立的（未认证 10 次/小时），耗尽后要等重置；"
    "若两次都返回 0，说明 q 参数没生效"
)


def deps_check() -> CheckResult:
    """真 import 一次 ``httpx``。"""
    try:
        import httpx  # noqa: F401
    except Exception as exc:  # noqa: BLE001 —— 任何 import 失败都算依赖不满足
        return CheckResult(DEPS_NAME, False, f"{exc.__class__.__name__}: {exc}｜{_HINT_DEPS}")
    return CheckResult(DEPS_NAME, True, "httpx 可用")


async def _api_check(settings) -> CheckResult:
    """打一次 ``/rate_limit``：能拿到配额账本才算通。"""
    client = GithubClient(settings, pace=False)
    try:
        payload = await asyncio.wait_for(client.rate_limit(), API_TIMEOUT)
    except asyncio.TimeoutError:
        return CheckResult(API_NAME, False, f"超时（>{API_TIMEOUT:.0f}s）｜{_HINT_API}")
    except GithubError as exc:
        return CheckResult(API_NAME, False, f"{exc.message}｜{_HINT_API}")
    except Exception as exc:  # noqa: BLE001
        return CheckResult(API_NAME, False, f"{exc.__class__.__name__}: {exc}｜{_HINT_API}")
    finally:
        await client.aclose()

    core = (payload.get("resources") or {}).get("core") or {}
    remaining = core.get("remaining")
    limit = core.get("limit")
    who = "已认证" if payload.get("authenticated") else "未认证"
    detail = f"api.github.com 通，{who}，core 配额 {remaining}/{limit}"
    if not payload.get("authenticated"):
        detail += "（配 GITHUB_TOKEN 可提到 5000）"
    return CheckResult(API_NAME, True, detail)


async def _search_check(settings) -> CheckResult:
    """横向对照：miss 必须 0 条、hit 必须非 0 条。"""
    client = GithubClient(settings, pace=False)
    try:
        miss = await asyncio.wait_for(
            client.search_repositories(SEARCH_PROBE_MISS, per_page=1), SEARCH_TIMEOUT
        )
        hit = await asyncio.wait_for(
            client.search_repositories(SEARCH_PROBE_HIT, per_page=1), SEARCH_TIMEOUT
        )
    except asyncio.TimeoutError:
        return CheckResult(SEARCH_NAME, False, f"超时（>{SEARCH_TIMEOUT:.0f}s）｜{_HINT_SEARCH}")
    except GithubError as exc:
        return CheckResult(SEARCH_NAME, False, f"{exc.message}｜{_HINT_SEARCH}")
    except Exception as exc:  # noqa: BLE001
        return CheckResult(SEARCH_NAME, False, f"{exc.__class__.__name__}: {exc}｜{_HINT_SEARCH}")
    finally:
        await client.aclose()

    miss_total = miss.get("total_count")
    hit_total = hit.get("total_count")
    if miss_total != 0:
        return CheckResult(
            SEARCH_NAME,
            False,
            f"无关词 {SEARCH_PROBE_MISS!r} 返回 {miss_total} 条（本该 0 条，q 可能没生效）｜{_HINT_SEARCH}",
        )
    if not hit_total:
        return CheckResult(
            SEARCH_NAME,
            False,
            f"已知有结果的词 {SEARCH_PROBE_HIT!r} 返回 {hit_total} 条（本该非 0）｜{_HINT_SEARCH}",
        )
    return CheckResult(
        SEARCH_NAME,
        True,
        f"搜索在按关键词过滤：无关词 0 条，{SEARCH_PROBE_HIT!r} {hit_total} 条",
    )


async def _async_checks(settings):
    return await asyncio.gather(
        _api_check(settings),
        _search_check(settings),
    )


def extra_checks(settings) -> list[CheckResult]:
    """``--check`` 时被 ``pixiv_bridge.cli`` 调用的站点自检。"""
    resolved = as_github_settings(settings)
    return [deps_check()] + list(asyncio.run(_async_checks(resolved)))
