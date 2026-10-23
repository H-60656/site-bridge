"""github 接口项目的测试：解析、客户端、路由、自检、入口契约。

全离线：客户端的 ``http`` 参数接一个 ``httpx.MockTransport``，路由测试直接把
``app.state.github_client`` 换成假客户端。**不发任何真实请求** —— 这个站点的
配额只有 60 次/小时，测试打真实接口会把它吃掉。

只有 ``httpx`` 一个依赖，而且它在根 ``requirements.txt`` 里，所以这个模块不会像
``test_asmr_one`` / ``test_ehviewer`` 那样整模块 skip。
"""

import asyncio
import json
import sys
import time
import unittest
from pathlib import Path

import httpx
from fastapi.testclient import TestClient

from github import client as github_client
from github.app import (
    GITHUB_ERROR_TYPES,
    GITHUB_ROUTERS,
    GITHUB_STATE_KEYS,
    SITE,
    create_app,
    github_state_factory,
)
from github.client import (
    GithubClient,
    GithubError,
    GithubForbiddenError,
    GithubNotFoundError,
    GithubRateLimitError,
    GithubUnauthorizedError,
    decode_content,
    normalize_commit,
    normalize_entry,
    normalize_issue,
    normalize_repo,
    normalize_release,
    normalize_user,
)
from github.config import (
    DEFAULT_API_URL,
    DEFAULT_PER_PAGE,
    GithubSettings,
    as_github_settings,
)
from github.routes import get_client
from pixiv_bridge.config import Settings
from pixiv_bridge.server import create_bridge_app

PROJECT_ROOT = Path(__file__).resolve().parent.parent

#: 与 ``github/app.py`` 里的 ``create_app`` 同名导入，避免遮蔽框架的。
create_github_app = create_app


def make_settings(**kw):
    """配额和配速都放开 —— 测试不该因为限流变慢或失败。"""
    kw.setdefault("rate_limit_rpm", 100000)
    return GithubSettings(**kw)


def _json_response(payload, status=200, headers=None, url="https://api.github.com/"):
    merged = {"content-type": "application/json"}
    merged.update(headers or {})
    return httpx.Response(
        status, json=payload, headers=merged, request=httpx.Request("GET", url)
    )


def _mock_http(handler):
    """把 ``handler(request)`` 包成 ``httpx.AsyncClient``（完全离线）。"""
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


def _client(handler, **kw):
    """建一个走假 transport 的客户端；测试结束时自动关掉。"""
    ctx = _mock_http(handler)
    return GithubClient(make_settings(**kw), http=ctx)


# ---------------------------------------------------------------- 样例载荷

REPO_PAYLOAD = {
    "full_name": "psf/requests",
    "name": "requests",
    "owner": {
        "login": "psf",
        "html_url": "https://github.com/psf",
        "avatar_url": "https://avatars.githubusercontent.com/u/28275?v=4",
    },
    "description": "A simple, yet elegant, HTTP library.",
    "html_url": "https://github.com/psf/requests",
    "homepage": "https://requests.readthedocs.io",
    "language": "Python",
    "license": {"spdx_id": "Apache-2.0", "name": "Apache License 2.0"},
    "topics": ["http", "python", "requests"],
    "stargazers_count": 54615,
    "forks_count": 12916,
    "subscribers_count": 1213,
    "watchers_count": 54615,
    "open_issues_count": 260,
    "default_branch": "main",
    "archived": False,
    "fork": False,
    "private": False,
    "created_at": "2011-02-13T18:38:17Z",
    "updated_at": "2026-10-20T10:00:00Z",
    "pushed_at": "2026-10-19T23:00:00Z",
    "size": 5911,
}

USER_PAYLOAD = {
    "login": "psf",
    "id": 28275,
    "name": "Python Software Foundation",
    "type": "Organization",
    "html_url": "https://github.com/psf",
    "avatar_url": "https://avatars.githubusercontent.com/u/28275?v=4",
    "bio": None,
    "company": None,
    "blog": "https://www.python.org/psf/",
    "location": None,
    "public_repos": 44,
    "public_gists": 0,
    "followers": 2562,
    "following": 0,
    "created_at": "2008-10-02T14:20:31Z",
    "updated_at": "2026-10-01T00:00:00Z",
}

RELEASE_PAYLOAD = [
    {
        "id": 123,
        "tag_name": "v2.34.2",
        "name": "v2.34.2",
        "draft": False,
        "prerelease": False,
        "created_at": "2026-09-01T00:00:00Z",
        "published_at": "2026-09-02T00:00:00Z",
        "html_url": "https://github.com/psf/requests/releases/tag/v2.34.2",
        "tarball_url": "https://api.github.com/repos/psf/requests/tarball/v2.34.2",
        "zipball_url": "https://api.github.com/repos/psf/requests/zipball/v2.34.2",
        "body": "Release notes",
        "author": {"login": "psf", "html_url": "", "avatar_url": ""},
        "assets": [
            {
                "name": "requests.tar.gz",
                "size": 1024,
                "download_count": 7,
                "content_type": "application/gzip",
                "browser_download_url": "https://example.invalid/requests.tar.gz",
            }
        ],
    }
]

ISSUE_PAYLOAD = [
    {
        "number": 6543,
        "title": "A bug",
        "state": "open",
        "comments": 3,
        "html_url": "https://github.com/psf/requests/issues/6543",
        "created_at": "2026-08-01T00:00:00Z",
        "updated_at": "2026-08-02T00:00:00Z",
        "closed_at": None,
        "user": {"login": "someone", "html_url": "", "avatar_url": ""},
        "labels": [{"name": "bug"}, {"name": "needs-triage"}],
    },
    {
        "number": 6544,
        "title": "A pull request",
        "state": "open",
        "comments": 0,
        "html_url": "https://github.com/psf/requests/pull/6544",
        "created_at": "2026-08-03T00:00:00Z",
        "updated_at": "2026-08-03T00:00:00Z",
        "closed_at": None,
        "user": {"login": "someone", "html_url": "", "avatar_url": ""},
        "labels": [],
        "pull_request": {"url": "https://api.github.com/repos/psf/requests/pulls/6544"},
    },
]

COMMIT_PAYLOAD = [
    {
        "sha": "abc123",
        "commit": {
            "message": "Fix a thing\n\nA longer body.",
            "author": {"name": "Someone", "email": "s@example.invalid", "date": "2026-07-01T00:00:00Z"},
        },
        "html_url": "https://github.com/psf/requests/commit/abc123",
    }
]

CONTENTS_DIR_PAYLOAD = [
    {
        "name": ".github",
        "path": ".github",
        "type": "dir",
        "size": 0,
        "sha": "aaa",
        "html_url": "https://github.com/psf/requests/tree/main/.github",
        "download_url": None,
    },
    {
        "name": "README.md",
        "path": "README.md",
        "type": "file",
        "size": 2894,
        "sha": "bbb",
        "html_url": "https://github.com/psf/requests/blob/main/README.md",
        "download_url": "https://raw.githubusercontent.com/psf/requests/main/README.md",
    },
]

README_PAYLOAD = {
    "name": "README.md",
    "path": "README.md",
    "type": "file",
    "size": 2894,
    "sha": "ccc",
    "html_url": "https://github.com/psf/requests/blob/main/README.md",
    "download_url": "https://raw.githubusercontent.com/psf/requests/main/README.md",
    "encoding": "base64",
    "content": "IyBSZXF1ZXN0cwoKQSBzaW1wbGUgSFRUUCBsaWJyYXJ5Lgo=",
}

RATE_LIMIT_PAYLOAD = {
    "rate": {"limit": 60, "remaining": 41, "reset": 1792757111, "used": 19},
    "resources": {
        "core": {"limit": 60, "remaining": 41, "reset": 1792757111, "used": 19},
        "search": {"limit": 10, "remaining": 4, "reset": 1792757111, "used": 6},
        "graphql": {"limit": 0, "remaining": 0, "reset": 1792757111, "used": 0},
    },
}


# ---------------------------------------------------------------- 归一化


class GithubNormalizeTestCase(unittest.TestCase):
    """上游字段 → 本端扁平键；缺失/None 一律不抛。"""

    def test_repo_renames_the_counters(self):
        repo = normalize_repo(REPO_PAYLOAD)

        self.assertEqual(repo["full_name"], "psf/requests")
        # 上游叫 stargazers_count / forks_count / open_issues_count …
        self.assertEqual(repo["stars"], 54615)
        self.assertEqual(repo["forks"], 12916)
        self.assertEqual(repo["open_issues"], 260)
        self.assertEqual(repo["watchers"], 1213)
        self.assertEqual(repo["size_kb"], 5911)
        self.assertEqual(repo["license"], {"spdx_id": "Apache-2.0", "name": "Apache License 2.0"})
        self.assertEqual(repo["owner"]["login"], "psf")
        self.assertEqual(repo["topics"], ["http", "python", "requests"])

    def test_repo_falls_back_to_watchers_count_for_watchers(self):
        # 有些端点只给 watchers_count（等于 star 数），没有 subscribers_count。
        repo = normalize_repo({"watchers_count": 99})

        self.assertEqual(repo["watchers"], 99)

    def test_repo_tolerates_a_completely_empty_payload(self):
        repo = normalize_repo({})

        self.assertEqual(repo["full_name"], "")
        self.assertEqual(repo["stars"], 0)
        self.assertEqual(repo["topics"], [])
        self.assertEqual(repo["license"], {"spdx_id": "", "name": ""})

    def test_user_normalizes_nulls_to_empty_strings(self):
        user = normalize_user(USER_PAYLOAD)

        self.assertEqual(user["login"], "psf")
        self.assertEqual(user["type"], "Organization")
        self.assertEqual(user["bio"], "")
        self.assertEqual(user["location"], "")
        self.assertEqual(user["followers"], 2562)

    def test_release_keeps_assets_and_counts_downloads(self):
        release = normalize_release(RELEASE_PAYLOAD[0])

        self.assertEqual(release["tag_name"], "v2.34.2")
        self.assertFalse(release["prerelease"])
        self.assertEqual(len(release["assets"]), 1)
        self.assertEqual(release["assets"][0]["download_count"], 7)

    def test_issue_distinguishes_pull_requests(self):
        bug, pr = [normalize_issue(item) for item in ISSUE_PAYLOAD]

        self.assertFalse(bug["is_pull_request"])
        self.assertTrue(pr["is_pull_request"])
        self.assertEqual(bug["labels"], ["bug", "needs-triage"])

    def test_commit_summary_is_the_first_line_only(self):
        commit = normalize_commit(COMMIT_PAYLOAD[0])

        self.assertEqual(commit["summary"], "Fix a thing")
        self.assertIn("A longer body.", commit["message"])
        self.assertEqual(commit["author"]["name"], "Someone")

    def test_entry_null_download_url_becomes_empty(self):
        entry = normalize_entry(CONTENTS_DIR_PAYLOAD[0])

        self.assertEqual(entry["type"], "dir")
        self.assertEqual(entry["download_url"], "")

    def test_decode_content_returns_text(self):
        self.assertEqual(
            decode_content(README_PAYLOAD["content"]),
            "# Requests\n\nA simple HTTP library.\n",
        )

    def test_decode_content_never_raises(self):
        self.assertEqual(decode_content("不是 base64"), "")
        self.assertEqual(decode_content(None), "")


# ---------------------------------------------------------------- 客户端


class GithubClientRequestTestCase(unittest.TestCase):
    """请求层：URL 拼接、请求头、状态码翻译、配额账本。"""

    def test_a_request_goes_to_the_official_base_url(self):
        seen = {}

        def handler(request):
            seen["url"] = str(request.url)
            seen["headers"] = dict(request.headers)
            return _json_response(REPO_PAYLOAD)

        async def run():
            client = _client(handler)
            try:
                await client.repo("psf", "requests")
            finally:
                await client.aclose()

        asyncio.run(run())

        self.assertEqual(seen["url"], "https://api.github.com/repos/psf/requests")
        # 版本头 + accept 是官方要求的；没有 token 时必须**不**带 authorization。
        self.assertEqual(seen["headers"]["accept"], "application/vnd.github+json")
        self.assertEqual(seen["headers"]["x-github-api-version"], "2022-11-28")
        self.assertNotIn("authorization", seen["headers"])

    def test_the_token_becomes_a_bearer_header(self):
        seen = {}

        def handler(request):
            seen["authorization"] = request.headers.get("authorization")
            return _json_response(REPO_PAYLOAD)

        async def run():
            client = _client(handler, token="ghp_secret")
            try:
                await client.repo("psf", "requests")
            finally:
                await client.aclose()

        asyncio.run(run())

        self.assertEqual(seen["authorization"], "Bearer ghp_secret")

    def test_the_client_never_reads_a_proxy_setting(self):
        """用户明确要求：这个站点不使用代理。读源码是最直接的守门方式。"""
        source = (PROJECT_ROOT / "github" / "client.py").read_text(encoding="utf-8")
        # 模块文档里会说「不读 settings.proxy」（这是**刻意**的说明），
        # 所以先切掉开头的 docstring，只看真正会被执行的代码。
        doctail = source.split('"""', 2)[-1]

        self.assertNotIn("proxy", doctail)
        # 但**必须**显式关掉 httpx 对环境的继承：这台机器的系统代理
        # 127.0.0.1:7897 走得通，却把配额烧在别人的出口 IP 上。
        self.assertIn("trust_env=False", doctail)


    def test_404_becomes_not_found(self):
        def handler(request):
            return _json_response({"message": "Not Found"}, status=404)

        async def run():
            client = _client(handler)
            try:
                await client.repo("psf", "nope")
            finally:
                await client.aclose()

        with self.assertRaises(GithubNotFoundError) as caught:
            asyncio.run(run())
        self.assertEqual(caught.exception.status, 404)
        self.assertEqual(caught.exception.reason, "not_found")

    def test_401_becomes_unauthorized(self):
        def handler(request):
            return _json_response({"message": "Bad credentials"}, status=401)

        async def run():
            client = _client(handler, token="bad")
            try:
                await client.user("psf")
            finally:
                await client.aclose()

        with self.assertRaises(GithubUnauthorizedError) as caught:
            asyncio.run(run())
        self.assertEqual(caught.exception.status, 401)
        self.assertEqual(caught.exception.reason, "auth_required")

    def test_a_403_rate_limit_is_told_apart_from_a_real_forbidden(self):
        """403 是**软失败**：靠 x-ratelimit-remaining 与正文判定，不能只看状态码。"""

        def limited(request):
            return _json_response(
                {"message": "API rate limit exceeded for 1.2.3.4."},
                status=403,
                headers={"x-ratelimit-remaining": "0", "x-ratelimit-limit": "60"},
            )

        def forbidden(request):
            return _json_response(
                {"message": "Resource not accessible by integration"},
                status=403,
                headers={"x-ratelimit-remaining": "42", "x-ratelimit-limit": "60"},
            )

        async def call(handler):
            client = _client(handler)
            try:
                await client.repo("psf", "requests")
            finally:
                await client.aclose()

        with self.assertRaises(GithubRateLimitError) as exhausted:
            asyncio.run(call(limited))
        # 上游给 403，本端翻成 429 —— 语义更准（这是等一等就能好的事）。
        self.assertEqual(exhausted.exception.status, 429)
        self.assertEqual(exhausted.exception.reason, "rate_limited")

        with self.assertRaises(GithubForbiddenError) as denied:
            asyncio.run(call(forbidden))
        self.assertEqual(denied.exception.status, 403)
        self.assertEqual(denied.exception.reason, "forbidden")

    def test_an_exhausted_budget_refuses_locally_without_a_request(self):
        """配额见底且未到重置时刻时，不该再去撞上游（省下来的就是配额）。"""
        calls = []

        def handler(request):
            calls.append(str(request.url))
            return _json_response(REPO_PAYLOAD)

        async def run():
            client = _client(handler)
            try:
                # 第一次：真的发出去，把账本记得「剩 0」。
                await client.get_json("/rate_limit")
                client.remaining = 0
                client.reset_at = time.time() + 600
                # 第二次：本地拒绝，不该产生新请求。
                await client.repo("psf", "requests")
            finally:
                await client.aclose()

        with self.assertRaises(GithubRateLimitError) as caught:
            asyncio.run(run())

        self.assertEqual(len(calls), 1, calls)
        self.assertIn("秒后重置", caught.exception.message)

    def test_an_expired_budget_is_allowed_through_again(self):
        def handler(request):
            return _json_response(REPO_PAYLOAD)

        async def run():
            client = _client(handler)
            try:
                client.remaining = 0
                client.reset_at = time.time() - 1  # 已经重置过了
                return await client.repo("psf", "requests")
            finally:
                await client.aclose()

        self.assertEqual(asyncio.run(run())["full_name"], "psf/requests")

    def test_the_budget_ledger_reads_the_ratelimit_headers(self):
        def handler(request):
            return _json_response(
                REPO_PAYLOAD,
                headers={
                    "x-ratelimit-limit": "60",
                    "x-ratelimit-remaining": "41",
                    "x-ratelimit-used": "19",
                    "x-ratelimit-reset": "1792757111",
                },
            )

        async def run():
            client = _client(handler)
            try:
                await client.repo("psf", "requests")
                return client.budget()
            finally:
                await client.aclose()

        budget = asyncio.run(run())

        self.assertEqual(budget["limit"], 60)
        self.assertEqual(budget["remaining"], 41)
        self.assertEqual(budget["used"], 19)
        self.assertTrue(budget["reset_at"].endswith("Z"))

    def test_the_budget_ledger_keys_on_the_resource_header(self):
        """core 与 search 是**两本**账；``x-ratelimit-resource`` 才是归属的权威。"""

        def handler(request):
            if request.url.path.startswith("/search"):
                return _json_response(
                    {"total_count": 0, "items": []},
                    headers={
                        "x-ratelimit-resource": "search",
                        "x-ratelimit-limit": "10",
                        "x-ratelimit-remaining": "7",
                        "x-ratelimit-used": "3",
                        "x-ratelimit-reset": "1792757111",
                    },
                )
            return _json_response(
                REPO_PAYLOAD,
                headers={
                    "x-ratelimit-resource": "core",
                    "x-ratelimit-limit": "60",
                    "x-ratelimit-remaining": "41",
                    "x-ratelimit-used": "19",
                    "x-ratelimit-reset": "1792757111",
                },
            )

        async def run():
            client = _client(handler)
            try:
                await client.repo("psf", "requests")
                await client.search_repositories("requests")
                return client.budget()
            finally:
                await client.aclose()

        budget = asyncio.run(run())

        self.assertEqual(budget["resources"]["core"]["remaining"], 41)
        self.assertEqual(budget["resources"]["core"]["limit"], 60)
        self.assertEqual(budget["resources"]["search"]["remaining"], 7)
        self.assertEqual(budget["resources"]["search"]["limit"], 10)
        # 外层扁平键是 core 的兼容视图 —— 不能被 search 的数覆盖。
        self.assertEqual(budget["remaining"], 41)

    def test_an_exhausted_search_does_not_block_core(self):
        """曾经的真 BUG：search 用完后，**所有** core 请求被本地拒 429。"""
        calls = []

        def handler(request):
            calls.append(request.url.path)
            if request.url.path.startswith("/search"):
                return _json_response(
                    {"total_count": 0, "items": []},
                    headers={
                        "x-ratelimit-resource": "search",
                        "x-ratelimit-limit": "10",
                        "x-ratelimit-remaining": "0",
                        "x-ratelimit-reset": "1792757111",
                    },
                )
            return _json_response(
                REPO_PAYLOAD,
                headers={
                    "x-ratelimit-resource": "core",
                    "x-ratelimit-limit": "60",
                    "x-ratelimit-remaining": "55",
                    "x-ratelimit-reset": "1792757111",
                },
            )

        async def run():
            client = _client(handler)
            try:
                await client.search_repositories("requests")  # search 见底
                # core 还有 55 次，必须放行。
                return await client.repo("psf", "requests")
            finally:
                await client.aclose()

        payload = asyncio.run(run())

        self.assertEqual(payload["full_name"], "psf/requests")
        self.assertEqual(len(calls), 2, calls)

    def test_an_exhausted_core_does_not_block_search(self):
        calls = []

        def handler(request):
            calls.append(request.url.path)
            if request.url.path.startswith("/search"):
                return _json_response(
                    {"total_count": 1, "items": []},
                    headers={
                        "x-ratelimit-resource": "search",
                        "x-ratelimit-limit": "10",
                        "x-ratelimit-remaining": "6",
                        "x-ratelimit-reset": "1792757111",
                    },
                )
            return _json_response(
                REPO_PAYLOAD,
                headers={
                    "x-ratelimit-resource": "core",
                    "x-ratelimit-limit": "60",
                    "x-ratelimit-remaining": "0",
                    "x-ratelimit-reset": "1792757111",
                },
            )

        async def run():
            client = _client(handler)
            try:
                await client.repo("psf", "requests")  # core 见底
                return await client.search_repositories("requests")  # 该放行
            finally:
                await client.aclose()

        page = asyncio.run(run())

        self.assertEqual(page["total_count"], 1)
        self.assertEqual(len(calls), 2, calls)

    def test_the_refusal_message_names_the_exhausted_resource(self):
        """报错要说清是 core 还是 search 用完了 —— 否则用户会去等错的重置。"""

        def handler(request):
            return _json_response(
                {"total_count": 0, "items": []},
                headers={
                    "x-ratelimit-resource": "search",
                    "x-ratelimit-limit": "10",
                    "x-ratelimit-remaining": "0",
                    "x-ratelimit-reset": str(int(time.time()) + 600),
                },
            )

        async def run():
            client = _client(handler)
            try:
                await client.search_repositories("x")
                await client.search_repositories("y")  # 本地拒绝
            finally:
                await client.aclose()

        with self.assertRaises(GithubRateLimitError) as caught:
            asyncio.run(run())

        self.assertIn("search", caught.exception.message)
        self.assertIn("秒后重置", caught.exception.message)

    def test_an_exhausted_budget_never_blocks_the_rate_limit_endpoint(self):
        """排障入口不该被自己关掉：配额见底时正是最需要查它的时候。

        上游的 ``/rate_limit`` **不计费**（实测连打两次 ``used`` 不变），所以它在
        本地硬闸之外。否则用户看到 429、去查重置时间、又拿到同一个 429。
        """
        seen = []

        def handler(request):
            seen.append(request.url.path)
            return _json_response(
                RATE_LIMIT_PAYLOAD,
                headers={
                    "x-ratelimit-resource": "core",
                    "x-ratelimit-limit": "60",
                    "x-ratelimit-remaining": "0",
                    "x-ratelimit-reset": str(int(time.time()) + 600),
                },
            )

        async def run():
            client = _client(handler)
            try:
                # 先让账本见底。
                await client.rate_limit()
                budget = client.budget()
                # 普通请求必须被本地拒绝。
                with self.assertRaises(GithubRateLimitError):
                    await client.repo("psf", "requests")
                # 查账必须仍然放行。
                await client.rate_limit()
                return budget, client.budget()
            finally:
                await client.aclose()

        before, after = asyncio.run(run())

        self.assertEqual(before["resources"]["core"]["remaining"], 0)
        self.assertEqual(after["resources"]["core"]["remaining"], 0)
        # 两次查账都真的发出去了（第二次没有被本地硬闸拦下）。
        self.assertEqual(seen, ["/rate_limit", "/rate_limit"], seen)

    def test_a_non_json_body_is_not_silently_swallowed(self):
        def handler(request):
            return httpx.Response(
                200,
                text="<html>not json</html>",
                headers={"content-type": "text/html"},
                request=httpx.Request("GET", "https://api.github.com/repos/a/b"),
            )

        async def run():
            client = _client(handler)
            try:
                await client.repo("a", "b")
            finally:
                await client.aclose()

        with self.assertRaises(GithubError) as caught:
            asyncio.run(run())
        self.assertEqual(caught.exception.reason, "github_upstream")


class GithubClientPagingTestCase(unittest.TestCase):
    """列表语义：``count`` 是本页条数、``total`` 是 None、翻页看 ``Link``。"""

    @staticmethod
    def _link(next_url=None):
        parts = []
        if next_url:
            parts.append(f'<{next_url}>; rel="next"')
        return {"link": ", ".join(parts)}

    def test_releases_report_this_pages_count_and_unknown_total(self):
        def handler(request):
            self.assertEqual(request.url.params["per_page"], "30")
            self.assertEqual(request.url.params["page"], "1")
            return _json_response(RELEASE_PAYLOAD, headers=self._link())

        async def run():
            client = _client(handler)
            try:
                return await client.releases("psf", "requests")
            finally:
                await client.aclose()

        page = asyncio.run(run())

        self.assertEqual(page["count"], 1)
        # 上游不提供总数 —— 编一个数比说「不知道」更坏。
        self.assertIsNone(page["total"])
        self.assertFalse(page["has_next"])
        self.assertIsNone(page["next_page"])

    def test_has_next_and_next_page_come_from_the_link_header(self):
        def handler(request):
            return _json_response(
                RELEASE_PAYLOAD,
                headers=self._link("https://api.github.com/repos/psf/requests/releases?page=3"),
            )

        async def run():
            client = _client(handler)
            try:
                return await client.releases("psf", "requests", page=2)
            finally:
                await client.aclose()

        page = asyncio.run(run())

        self.assertTrue(page["has_next"])
        self.assertEqual(page["next_page"], 3)

    def test_per_page_is_clamped_to_the_upstream_limit(self):
        seen = {}

        def handler(request):
            seen["per_page"] = request.url.params["per_page"]
            return _json_response([])

        async def run():
            client = _client(handler)
            try:
                await client.commits("psf", "requests", per_page=5000)
            finally:
                await client.aclose()

        asyncio.run(run())

        self.assertEqual(seen["per_page"], "100")

    def test_issues_pass_the_state_through(self):
        seen = {}

        def handler(request):
            seen["state"] = request.url.params.get("state")
            return _json_response(ISSUE_PAYLOAD)

        async def run():
            client = _client(handler)
            try:
                return await client.issues("psf", "requests", state="all")
            finally:
                await client.aclose()

        page = asyncio.run(run())

        self.assertEqual(seen["state"], "all")
        self.assertEqual(page["count"], 2)
        self.assertEqual(page["items"][1]["number"], 6544)

    def test_commits_send_sha_only_when_given(self):
        seen = []

        def handler(request):
            seen.append(request.url.params.get("sha"))
            return _json_response(COMMIT_PAYLOAD)

        async def run():
            client = _client(handler)
            try:
                await client.commits("psf", "requests")
                await client.commits("psf", "requests", sha="dev")
            finally:
                await client.aclose()

        asyncio.run(run())

        self.assertIsNone(seen[0])
        self.assertEqual(seen[1], "dev")


class GithubClientEndpointsTestCase(unittest.TestCase):
    """各端点的路径与形状（读 README、目录 vs 单文件、搜索、配额）。"""

    def test_readme_is_decoded(self):
        seen = {}

        def handler(request):
            seen["path"] = request.url.path
            return _json_response(README_PAYLOAD)

        async def run():
            client = _client(handler)
            try:
                return await client.readme("psf", "requests")
            finally:
                await client.aclose()

        entry = asyncio.run(run())

        self.assertEqual(seen["path"], "/repos/psf/requests/readme")
        self.assertEqual(entry["encoding"], "base64")
        self.assertIn("simple HTTP library", entry["content"])

    def test_readme_passes_ref_only_when_given(self):
        seen = []

        def handler(request):
            seen.append(request.url.params.get("ref"))
            return _json_response(README_PAYLOAD)

        async def run():
            client = _client(handler)
            try:
                await client.readme("psf", "requests")
                await client.readme("psf", "requests", ref="v2.0.0")
            finally:
                await client.aclose()

        asyncio.run(run())

        self.assertIsNone(seen[0])
        self.assertEqual(seen[1], "v2.0.0")

    def test_contents_returns_a_list_shape_for_a_directory(self):
        seen = {}

        def handler(request):
            seen["path"] = request.url.path
            return _json_response(CONTENTS_DIR_PAYLOAD)

        async def run():
            client = _client(handler)
            try:
                return await client.contents("psf", "requests")
            finally:
                await client.aclose()

        result = asyncio.run(run())

        # 上游目录项走 /contents/ 带尾斜杠；不带 path 时也要带上。
        self.assertEqual(seen["path"], "/repos/psf/requests/contents/")
        self.assertEqual(result["type"], "dir")
        self.assertEqual(result["count"], 2)
        self.assertEqual([item["name"] for item in result["items"]], [".github", "README.md"])

    def test_contents_returns_a_single_entry_for_a_file(self):
        file_payload = dict(CONTENTS_DIR_PAYLOAD[1])

        def handler(request):
            return _json_response(file_payload)

        async def run():
            client = _client(handler)
            try:
                return await client.contents("psf", "requests", "README.md")
            finally:
                await client.aclose()

        entry = asyncio.run(run())

        self.assertEqual(entry["type"], "file")
        self.assertEqual(entry["count"], 1)
        self.assertEqual(entry["name"], "README.md")
        self.assertNotIn("items", entry)

    def test_contents_strips_leading_and_trailing_slashes(self):
        seen = {}

        def handler(request):
            seen["path"] = request.url.path
            return _json_response(CONTENTS_DIR_PAYLOAD)

        async def run():
            client = _client(handler)
            try:
                await client.contents("psf", "requests", "/docs/api/")
            finally:
                await client.aclose()

        asyncio.run(run())

        self.assertEqual(seen["path"], "/repos/psf/requests/contents/docs/api")

    def test_contents_refuses_a_traversal_path_without_a_request(self):
        """``..`` 会爬出 ``/contents`` 前缀，打到 ``/rate_limit`` 这类别的端点上去。"""
        calls = []

        def handler(request):
            calls.append(request.url.path)
            return _json_response({})

        async def run():
            client = _client(handler)
            try:
                for bad in ("..", "../..", "a/../../../../user", "../../../../../rate_limit"):
                    with self.subTest(path=bad):
                        with self.assertRaises(GithubError) as caught:
                            await client.contents("psf", "requests", bad)
                        self.assertEqual(caught.exception.status, 400)
                        self.assertEqual(caught.exception.reason, "invalid_path")
                return client
            finally:
                await client.aclose()

        asyncio.run(run())

        self.assertEqual(calls, [], "越界路径不该产生任何上游请求")

    def test_contents_refuses_query_and_fragment_injection(self):
        """``?`` / ``#`` 在拼 URL 时会被当当查询串/片段的分隔符。"""
        calls = []

        def handler(request):
            calls.append(request.url.path)
            return _json_response({})

        async def run():
            client = _client(handler)
            try:
                for bad in ("x?q=injected&per_page=1", "x#frag", "a\\b"):
                    with self.subTest(path=bad):
                        with self.assertRaises(GithubError) as caught:
                            await client.contents("psf", "requests", bad)
                        self.assertEqual(caught.exception.status, 400)
                        self.assertEqual(caught.exception.reason, "invalid_path")
            finally:
                await client.aclose()

        asyncio.run(run())

        self.assertEqual(calls, [])

    def test_contents_still_accepts_an_ordinary_nested_path(self):
        """挡穿越不能连带把正常的深路径也挡了。"""
        seen = {}

        def handler(request):
            seen["path"] = request.url.path
            return _json_response(CONTENTS_DIR_PAYLOAD)

        async def run():
            client = _client(handler)
            try:
                await client.contents("psf", "requests", "src/requests/sessions.py")
            finally:
                await client.aclose()

        asyncio.run(run())

        self.assertEqual(seen["path"], "/repos/psf/requests/contents/src/requests/sessions.py")

    def test_the_page_size_setting_is_actually_used(self):
        """``GITHUB_PER_PAGE`` 曾经是个死配置：改了没有任何效果。"""
        seen = []

        def handler(request):
            seen.append(request.url.params["per_page"])
            return _json_response([])

        async def run():
            client = _client(handler, per_page=7)
            try:
                await client.releases("psf", "requests")
                await client.issues("psf", "requests")
                await client.commits("psf", "requests")
            finally:
                await client.aclose()

        asyncio.run(run())

        self.assertEqual(seen, ["7", "7", "7"])

    def test_an_explicit_per_page_beats_the_setting(self):
        seen = {}

        def handler(request):
            seen["per_page"] = request.url.params["per_page"]
            return _json_response([])

        async def run():
            client = _client(handler, per_page=7)
            try:
                await client.releases("psf", "requests", per_page=50)
            finally:
                await client.aclose()

        asyncio.run(run())

        self.assertEqual(seen["per_page"], "50")

    def test_the_page_size_setting_is_clamped_before_it_reaches_the_upstream(self):
        seen = {}

        def handler(request):
            seen["per_page"] = request.url.params["per_page"]
            return _json_response([])

        async def run():
            client = _client(handler, per_page=5000)  # 手滑写大了
            try:
                await client.releases("psf", "requests")
            finally:
                await client.aclose()

        asyncio.run(run())

        self.assertEqual(seen["per_page"], "100")

    def test_search_reports_total_count_and_incomplete_flag(self):
        seen = {}

        def handler(request):
            seen["q"] = request.url.params.get("q")
            seen["sort"] = request.url.params.get("sort")
            seen["order"] = request.url.params.get("order")
            return _json_response(
                {
                    "total_count": 356262,
                    "incomplete_results": False,
                    "items": [REPO_PAYLOAD],
                }
            )

        async def run():
            client = _client(handler)
            try:
                return await client.search_repositories(
                    "requests language:python", per_page=5, sort="stars", order="asc"
                )
            finally:
                await client.aclose()

        result = asyncio.run(run())

        self.assertEqual(seen["q"], "requests language:python")
        self.assertEqual(seen["sort"], "stars")
        self.assertEqual(seen["order"], "asc")
        self.assertEqual(result["total_count"], 356262)
        self.assertFalse(result["incomplete_results"])
        self.assertEqual(result["count"], 1)

    def test_search_does_not_send_sort_when_blank(self):
        seen = {}

        def handler(request):
            seen["has_sort"] = "sort" in request.url.params
            return _json_response({"total_count": 0, "items": []})

        async def run():
            client = _client(handler)
            try:
                await client.search_repositories("requests")
            finally:
                await client.aclose()

        asyncio.run(run())

        self.assertFalse(seen["has_sort"])

    def test_rate_limit_normalizes_every_resource(self):
        def handler(request):
            self.assertEqual(request.url.path, "/rate_limit")
            return _json_response(RATE_LIMIT_PAYLOAD)

        async def run():
            client = _client(handler)
            try:
                return await client.rate_limit()
            finally:
                await client.aclose()

        payload = asyncio.run(run())

        self.assertFalse(payload["authenticated"])
        self.assertEqual(payload["resources"]["core"]["remaining"], 41)
        self.assertEqual(payload["resources"]["search"]["limit"], 10)
        self.assertEqual(payload["resources"]["graphql"]["remaining"], 0)


# ---------------------------------------------------------------- 路由


class _FakeGithubClient:
    """顶替 ``app.state.github_client``：只提供路由用到的几个方法。"""

    def __init__(self, **kw):
        self.authenticated = kw.get("authenticated", False)
        self.base_url = kw.get("base_url", DEFAULT_API_URL)
        #: 假客户端也照真客户端的样子，把路由传下来的 ``per_page=None`` 解成配置
        #: 默认值 —— 这样断言里的数字才是用户真正看到的条数。
        self.default_per_page = kw.get("default_per_page", 30)
        self.calls = []
        self.error = kw.get("error")
        self.payload = kw.get("payload", {"ok": True})

    def _page_size(self, per_page):
        return self.default_per_page if per_page is None else per_page

    def budget(self):
        return {
            "authenticated": self.authenticated,
            "resources": {
                "core": {
                    "limit": 60,
                    "remaining": 41,
                    "used": 19,
                    "reset": 1792757111,
                    "reset_at": "2026-10-23T12:05:11Z",
                    "seconds_until_reset": 120,
                }
            },
            "limit": 60,
            "remaining": 41,
            "used": 19,
            "reset": 1792757111,
            "reset_at": "2026-10-23T12:05:11Z",
            "seconds_until_reset": 120,
        }

    async def aclose(self):
        """框架外壳在 lifespan 结束时会对 state 里的客户端调这个方法。"""

    def _maybe_raise(self):
        if self.error is not None:
            raise self.error

    async def repo(self, owner, repo):
        self.calls.append(("repo", owner, repo))
        self._maybe_raise()
        return {"full_name": f"{owner}/{repo}"}

    async def readme(self, owner, repo, ref=""):
        self.calls.append(("readme", owner, repo, ref))
        self._maybe_raise()
        return {"name": "README.md", "content": "hi"}

    async def contents(self, owner, repo, path="", ref=""):
        self.calls.append(("contents", owner, repo, path, ref))
        self._maybe_raise()
        return {"type": "dir", "count": 0, "items": []}

    async def releases(self, owner, repo, per_page=None, page=1):
        self.calls.append(("releases", owner, repo, self._page_size(per_page), page))
        self._maybe_raise()
        return {"count": 0, "total": None, "has_next": False, "next_page": None, "items": []}

    async def issues(self, owner, repo, state="open", per_page=None, page=1):
        self.calls.append(("issues", owner, repo, state, self._page_size(per_page), page))
        self._maybe_raise()
        return {"count": 0, "items": []}

    async def commits(self, owner, repo, sha="", per_page=None, page=1):
        self.calls.append(("commits", owner, repo, sha, self._page_size(per_page), page))
        self._maybe_raise()
        return {"count": 0, "items": []}

    async def search_repositories(self, query, per_page=None, page=1, sort="", order="desc"):
        self.calls.append(
            ("search", query, self._page_size(per_page), page, sort, order)
        )
        self._maybe_raise()
        return {"query": query, "total_count": 0, "count": 0, "items": []}

    async def user(self, login):
        self.calls.append(("user", login))
        self._maybe_raise()
        return {"login": login}

    async def rate_limit(self):
        self.calls.append(("rate_limit",))
        self._maybe_raise()
        return {"authenticated": self.authenticated, "resources": {"core": {"remaining": 41}}}


class GithubRouteTestCase(unittest.TestCase):
    """九个只读端点的绑定、参数校验与错误形状。"""

    def _client(self, **kw):
        app = create_github_app(make_settings())
        client = TestClient(app)
        client.__enter__()
        self.addCleanup(client.__exit__, None, None, None)
        self.fake = _FakeGithubClient(**kw)
        client.app.state.github_client = self.fake
        return client

    def test_repo_route_passes_owner_and_repo(self):
        client = self._client()

        response = client.get("/api/github/repo/psf/requests")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["full_name"], "psf/requests")
        self.assertIn(("repo", "psf", "requests"), self.fake.calls)

    def test_readme_route_passes_ref(self):
        client = self._client()

        response = client.get("/api/github/readme/psf/requests?ref=v2.0.0")

        self.assertEqual(response.status_code, 200)
        self.assertIn(("readme", "psf", "requests", "v2.0.0"), self.fake.calls)

    def test_contents_route_accepts_a_nested_path(self):
        client = self._client()

        response = client.get("/api/github/contents/psf/requests/docs/api")

        self.assertEqual(response.status_code, 200)
        self.assertIn(("contents", "psf", "requests", "docs/api", ""), self.fake.calls)

    def test_contents_route_works_without_a_path(self):
        client = self._client()

        response = client.get("/api/github/contents/psf/requests")

        self.assertEqual(response.status_code, 200)
        self.assertIn(("contents", "psf", "requests", "", ""), self.fake.calls)

    def test_contents_route_hands_the_traversal_to_the_client_to_refuse(self):
        """HTTP 层的 percent-encoding 会被 Starlette 解码，路由挡不住 —— 得靠客户端。

        这里用的假客户端不校验，所以断言的是「路由**没有**把它挡在前面、原样交给了
        客户端」；真正的拒绝在 ``test_contents_refuses_a_traversal_path_without_a_request``
        与下面的端到端用例里。
        """
        client = self._client()

        client.get("/api/github/contents/psf/requests/%2e%2e/%2e%2e/user%2femails")

        self.assertIn(("contents", "psf", "requests", "../../user/emails", ""), self.fake.calls)

    def test_the_real_client_refuses_a_traversal_at_the_http_boundary(self):
        """端到端：``..`` 无论怎么编码，都必须 400 且不产生上游请求。"""
        seen = []

        def handler(request):
            seen.append(request.url.path)
            return _json_response({})

        app = create_github_app(make_settings())
        client = TestClient(app)
        with client:
            real = GithubClient(make_settings(), http=_mock_http(handler), pace=False)
            client.app.state.github_client = real
            for bad in (
                "/api/github/contents/psf/requests/%2e%2e/%2e%2e/%2e%2e/%2e%2e/%2e%2e/user/emails",
                "/api/github/contents/psf/requests/..%2f..%2f..%2f..%2f..%2fuser%2femails",
                "/api/github/contents/psf/requests/x%3Fper_page%3D1",
            ):
                with self.subTest(url=bad):
                    response = client.get(bad)
                    self.assertEqual(response.status_code, 400)
                    self.assertIn("路径里不能出现", response.json()["detail"])

        self.assertEqual(seen, [], f"越界路径不该打到上游：{seen}")

    def test_the_page_size_setting_reaches_the_route_default(self):
        """``GITHUB_PER_PAGE`` 必须真的成为路由的默认条数。"""
        app = create_github_app(make_settings(per_page=7))
        client = TestClient(app)
        with client:
            self.fake = _FakeGithubClient(default_per_page=7)
            client.app.state.github_client = self.fake
            client.get("/api/github/releases/psf/requests")

        self.assertIn(("releases", "psf", "requests", 7, 1), self.fake.calls)

    def test_releases_route_passes_paging(self):
        client = self._client()

        response = client.get("/api/github/releases/psf/requests?per_page=10&page=2")

        self.assertEqual(response.status_code, 200)
        self.assertIn(("releases", "psf", "requests", 10, 2), self.fake.calls)

    def test_issues_route_defaults_to_open(self):
        client = self._client()

        client.get("/api/github/issues/psf/requests")

        self.assertIn(("issues", "psf", "requests", "open", 30, 1), self.fake.calls)

    def test_issues_route_rejects_a_bogus_state(self):
        client = self._client()

        response = client.get("/api/github/issues/psf/requests?state=bogus")

        self.assertEqual(response.status_code, 422)
        self.assertEqual(self.fake.calls, [])

    def test_commits_route_passes_sha(self):
        client = self._client()

        client.get("/api/github/commits/psf/requests?sha=dev")

        self.assertIn(("commits", "psf", "requests", "dev", 30, 1), self.fake.calls)

    def test_search_route_requires_q(self):
        client = self._client()

        response = client.get("/api/github/search/repositories")

        self.assertEqual(response.status_code, 422)
        self.assertEqual(self.fake.calls, [])

    def test_search_route_rejects_a_bogus_sort(self):
        client = self._client()

        response = client.get("/api/github/search/repositories?q=x&sort=bogus")

        self.assertEqual(response.status_code, 422)

    def test_search_route_passes_everything_through(self):
        client = self._client()

        client.get("/api/github/search/repositories?q=requests&per_page=5&sort=stars&order=asc")

        self.assertIn(("search", "requests", 5, 1, "stars", "asc"), self.fake.calls)

    def test_user_route_passes_login(self):
        client = self._client()

        response = client.get("/api/github/user/psf")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["login"], "psf")

    def test_per_page_above_the_limit_is_rejected(self):
        client = self._client()

        response = client.get("/api/github/releases/psf/requests?per_page=101")

        self.assertEqual(response.status_code, 422)

    def test_a_bad_owner_name_is_rejected_before_the_upstream(self):
        """畸形名字省一次上游往返 —— 配额只有 60 次/小时，不能白花。"""
        client = self._client()

        response = client.get("/api/github/repo/psf%20x/requests")

        self.assertIn(response.status_code, (404, 422))
        self.assertEqual(self.fake.calls, [])

    def test_rate_limit_route_reports_the_client_view(self):
        client = self._client()

        payload = client.get("/api/github/rate_limit").json()

        self.assertEqual(payload["resources"]["core"]["remaining"], 41)
        # 路由在官方账本上补一段「本进程视角」。
        self.assertEqual(payload["client"]["remaining"], 41)

    def test_rate_limit_route_still_answers_when_the_upstream_fails(self):
        """排障入口不能跟着上游一起坏 —— 本地那本账要照旧交出去。"""
        client = self._client(error=GithubError("上游连不上", status=502, reason="github_upstream"))

        response = client.get("/api/github/rate_limit")

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertIn("upstream_error", payload)
        self.assertEqual(payload["client"]["remaining"], 41)

    def test_auth_route_does_not_touch_the_upstream(self):
        client = self._client()

        payload = client.get("/api/github/auth").json()

        self.assertFalse(payload["authenticated"])
        self.assertIn("60", payload["note"])
        self.assertEqual(self.fake.calls, [])

    def test_auth_route_says_so_when_authenticated(self):
        client = self._client(authenticated=True)

        payload = client.get("/api/github/auth").json()

        self.assertTrue(payload["authenticated"])
        self.assertIn("5000", payload["note"])

    def test_site_errors_become_http_status_and_detail(self):
        cases = (
            (GithubNotFoundError("找不到"), 404, "找不到"),
            (GithubUnauthorizedError("token 坏了"), 401, "token 坏了"),
            (GithubForbiddenError("没权限"), 403, "没权限"),
            (GithubRateLimitError("配额用尽"), 429, "配额用尽"),
        )

        for error, status, detail in cases:
            with self.subTest(error=error.__class__.__name__):
                client = self._client(error=error)
                response = client.get("/api/github/repo/psf/requests")
                self.assertEqual(response.status_code, status)
                self.assertEqual(response.json()["detail"], detail)

    def test_get_client_reads_the_state_key(self):
        client = self._client()

        # 路由用的取值器就是往 app.state 上摸一次；这里直接照着调用。
        fake_request = type("R", (), {"app": client.app})()

        self.assertIs(get_client(fake_request), self.fake)


# ---------------------------------------------------------------- 配置


class GithubSettingsTestCase(unittest.TestCase):
    """站点配置：``GITHUB_*`` 前缀、不借别人的前缀、区间夹紧。"""

    def setUp(self):
        self._saved = {
            key: __import__("os").environ.get(key)
            for key in ("GITHUB_TOKEN", "GITHUB_API_URL", "GITHUB_PER_PAGE")
        }
        self.addCleanup(self._restore)

    def _restore(self):
        import os

        for key, value in self._saved.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value

    def test_defaults_when_nothing_is_configured(self):
        import os

        for key in self._saved:
            os.environ.pop(key, None)

        settings = GithubSettings.load()

        self.assertEqual(settings.token, "")
        self.assertFalse(settings.authenticated)
        self.assertEqual(settings.api_url, DEFAULT_API_URL)
        self.assertEqual(settings.per_page, DEFAULT_PER_PAGE)

    def test_the_token_is_stripped(self):
        import os

        os.environ["GITHUB_TOKEN"] = "  ghp_x  "

        settings = GithubSettings.load()

        self.assertEqual(settings.token, "ghp_x")
        self.assertTrue(settings.authenticated)

    def test_a_trailing_slash_is_removed_from_the_api_url(self):
        import os

        os.environ["GITHUB_API_URL"] = "https://ghe.example.invalid/api/v3/"

        self.assertEqual(GithubSettings.load().api_url, "https://ghe.example.invalid/api/v3")

    def test_per_page_is_clamped_into_range(self):
        import os

        os.environ["GITHUB_PER_PAGE"] = "5000"
        self.assertEqual(GithubSettings.load().per_page, 100)

        os.environ["GITHUB_PER_PAGE"] = "0"
        self.assertEqual(GithubSettings.load().per_page, 1)

    def test_a_non_numeric_per_page_falls_back_to_the_default(self):
        import os

        os.environ["GITHUB_PER_PAGE"] = "abc"

        self.assertEqual(GithubSettings.load().per_page, DEFAULT_PER_PAGE)

    def test_the_prefix_is_not_borrowed_from_another_site(self):
        source = (PROJECT_ROOT / "github" / "config.py").read_text(encoding="utf-8")

        self.assertIn("GITHUB_", source)
        self.assertNotIn("PIXIV_BRIDGE_", source)
        self.assertNotIn("ech_ips", source)
        self.assertNotIn("edge_ips", source)

    def test_as_github_settings_upgrades_plain_framework_settings(self):
        base = Settings(rate_limit_rpm=7, port=9999)

        upgraded = as_github_settings(base)

        self.assertIsInstance(upgraded, GithubSettings)
        self.assertEqual(upgraded.port, 9999)
        self.assertEqual(upgraded.rate_limit_rpm, 7)

    def test_as_github_settings_passes_an_already_upgraded_instance_through(self):
        settings = GithubSettings(rate_limit_rpm=5)

        self.assertIs(as_github_settings(settings), settings)

    def test_as_github_settings_loads_when_given_none(self):
        self.assertIsInstance(as_github_settings(None), GithubSettings)


# ---------------------------------------------------------------- 自检


class GithubEnvCheckTestCase(unittest.TestCase):
    """三项自检：依赖真 import、接口真打通、搜索横向对照。"""

    def _settings(self, **kw):
        kw.setdefault("rate_limit_rpm", 100000)
        return GithubSettings(**kw)

    def test_deps_check_really_imports_httpx(self):
        from github.envcheck import deps_check

        result = deps_check()

        self.assertTrue(result.ok)
        self.assertEqual(result.name, "github 依赖")

    def test_api_check_reports_the_remaining_budget(self):
        from github import envcheck

        def handler(request):
            return _json_response(RATE_LIMIT_PAYLOAD)

        original = envcheck.GithubClient

        class Patched(original):
            def __init__(self, settings=None, http=None, pace=True):
                super().__init__(settings, http=_mock_http(handler), pace=False)

        envcheck.GithubClient = Patched
        self.addCleanup(setattr, envcheck, "GithubClient", original)

        result = asyncio.run(envcheck._api_check(self._settings()))

        self.assertTrue(result.ok, result.detail)
        self.assertIn("41/60", result.detail)
        self.assertIn("未认证", result.detail)

    def test_api_check_reports_a_rate_limit_as_a_fault(self):
        from github import envcheck

        def handler(request):
            return _json_response(
                {"message": "API rate limit exceeded"},
                status=403,
                headers={"x-ratelimit-remaining": "0", "x-ratelimit-limit": "60"},
            )

        original = envcheck.GithubClient

        class Patched(original):
            def __init__(self, settings=None, http=None, pace=True):
                super().__init__(settings, http=_mock_http(handler), pace=False)

        envcheck.GithubClient = Patched
        self.addCleanup(setattr, envcheck, "GithubClient", original)

        result = asyncio.run(envcheck._api_check(self._settings()))

        self.assertFalse(result.ok)
        self.assertIn("GITHUB_TOKEN", result.detail)

    def test_search_check_requires_zero_for_the_miss_probe(self):
        """横向对照的另一半：无关词必须 0 条，否则说明 q 没生效。"""
        from github import envcheck

        def handler(request):
            return _json_response({"total_count": 5, "items": []})

        original = envcheck.GithubClient

        class Patched(original):
            def __init__(self, settings=None, http=None, pace=True):
                super().__init__(settings, http=_mock_http(handler), pace=False)

        envcheck.GithubClient = Patched
        self.addCleanup(setattr, envcheck, "GithubClient", original)

        result = asyncio.run(envcheck._search_check(self._settings()))

        self.assertFalse(result.ok)
        self.assertIn("本该 0 条", result.detail)

    def test_search_check_passes_on_a_proper_contrast(self):
        from github import envcheck

        def handler(request):
            query = request.url.params.get("q")
            total = 0 if query == envcheck.SEARCH_PROBE_MISS else 356262
            return _json_response({"total_count": total, "items": []})

        original = envcheck.GithubClient

        class Patched(original):
            def __init__(self, settings=None, http=None, pace=True):
                super().__init__(settings, http=_mock_http(handler), pace=False)

        envcheck.GithubClient = Patched
        self.addCleanup(setattr, envcheck, "GithubClient", original)

        result = asyncio.run(envcheck._search_check(self._settings()))

        self.assertTrue(result.ok, result.detail)
        self.assertIn("0 条", result.detail)

    def test_extra_checks_runs_all_three(self):
        from github import envcheck

        def handler(request):
            if "/search/" in request.url.path:
                query = request.url.params.get("q")
                total = 0 if query == envcheck.SEARCH_PROBE_MISS else 356262
                return _json_response({"total_count": total, "items": []})
            return _json_response(RATE_LIMIT_PAYLOAD)

        original = envcheck.GithubClient

        class Patched(original):
            def __init__(self, settings=None, http=None, pace=True):
                super().__init__(settings, http=_mock_http(handler), pace=False)

        envcheck.GithubClient = Patched
        self.addCleanup(setattr, envcheck, "GithubClient", original)

        results = envcheck.extra_checks(self._settings())

        self.assertEqual(
            [r.name for r in results], ["github 依赖", "github 接口", "github 搜索"]
        )
        self.assertTrue(all(r.ok for r in results), [r.detail for r in results])


# ---------------------------------------------------------------- 入口契约


def _app_paths(app) -> set:
    """把 ``app.routes`` 摊平成路径集合（兼容 ``original_router`` 包装）。"""
    paths = set()
    for route in app.routes:
        holder = getattr(route, "original_router", None)
        for item in holder.routes if holder is not None else [route]:
            path = getattr(item, "path", None)
            if path:
                paths.add(path)
    return paths


class GithubEntryPointTestCase(unittest.TestCase):
    """站点描述符、路由集合、注册表与依赖清单。"""

    def test_the_router_and_state_contract(self):
        self.assertEqual(len(GITHUB_ROUTERS), 1)
        self.assertEqual(GITHUB_STATE_KEYS, ("github_client", "nav_sections"))
        self.assertEqual(
            sorted(github_state_factory(make_settings())),
            ["github_client", "nav_sections"],
        )

    def test_the_state_factory_builds_a_configured_client(self):
        state = github_state_factory(make_settings(token="ghp_x"))

        client = state["github_client"]
        self.assertIsInstance(client, GithubClient)
        self.assertTrue(client.authenticated)

    def test_the_state_factory_builds_a_client_without_a_token(self):
        client = github_state_factory(make_settings())["github_client"]

        self.assertFalse(client.authenticated)

    def test_nav_lists_only_json_apis_under_the_site_prefix(self):
        from github.app import GITHUB_NAV

        paths = [item.path for item in GITHUB_NAV.items]

        self.assertEqual(len(paths), 10)
        self.assertTrue(all(p.startswith("/api/github/") for p in paths), paths)
        self.assertTrue(GITHUB_NAV.title)
        self.assertTrue(GITHUB_NAV.note)

    def test_the_site_descriptor_is_shaped_like_the_others(self):
        self.assertEqual(SITE.name, "github")
        self.assertEqual(SITE.title, "GitHub Bridge")
        self.assertEqual(SITE.version, "1.0.0")
        # 接在 pixiv=1 / iwara=2 / asmr_one=3 / ehviewer=4 之后，不留空洞。
        self.assertEqual(SITE.nav_order, 5)
        self.assertEqual(SITE.settings_class, GithubSettings)
        # 没有站点自己的 requirements.txt：httpx 已经在根清单里。
        self.assertEqual(SITE.required_files, ("run.py",))
        self.assertIsNotNone(SITE.checks)
        # token 是可选增强，不是登录门槛：没有登录自检钩子。
        self.assertIsNone(SITE.login_check)

    def test_github_adds_exactly_its_own_routes_to_the_shell(self):
        shell = _app_paths(create_bridge_app(make_settings()))
        full = _app_paths(create_github_app(make_settings()))
        extra = full - shell

        self.assertTrue(shell <= full, "github 应用必须包含框架外壳的全部路由")
        self.assertEqual(
            extra,
            {
                "/api/github/repo/{owner}/{repo}",
                "/api/github/readme/{owner}/{repo}",
                "/api/github/contents/{owner}/{repo}",
                "/api/github/contents/{owner}/{repo}/{path:path}",
                "/api/github/releases/{owner}/{repo}",
                "/api/github/issues/{owner}/{repo}",
                "/api/github/commits/{owner}/{repo}",
                "/api/github/search/repositories",
                "/api/github/user/{login}",
                "/api/github/rate_limit",
                "/api/github/auth",
            },
        )

    def test_the_route_table_matches_the_registry(self):
        """``scripts/sites.py`` 的 ``GITHUB_ROUTE_PATHS`` 必须与实际挂载一致。"""
        sys.path.insert(0, str(PROJECT_ROOT / "scripts"))
        self.addCleanup(sys.path.remove, str(PROJECT_ROOT / "scripts"))
        import sites

        shell = _app_paths(create_bridge_app(make_settings()))
        actual = _app_paths(create_github_app(make_settings())) - shell

        self.assertEqual(sites.GITHUB_ROUTE_PATHS, frozenset(actual))

    def test_shell_and_app_do_not_expose_other_sites_routes(self):
        client = TestClient(create_github_app(make_settings()))
        client.__enter__()
        self.addCleanup(client.__exit__, None, None, None)
        client.app.state.github_client = _FakeGithubClient()

        for path in ("/api/ranking", "/api/iwara/videos", "/api/asmr/works", "/api/eh/list"):
            with self.subTest(path=path):
                self.assertEqual(client.get(path).status_code, 404)

    def test_the_requirements_do_not_mention_curl_cffi(self):
        """这个站点只用 httpx；不该为了它把编译包拖进根清单。"""
        requirements = (PROJECT_ROOT / "requirements.txt").read_text(encoding="utf-8")

        self.assertIn("httpx", requirements)
        self.assertNotIn("curl_cffi", requirements)
        self.assertFalse((PROJECT_ROOT / "github" / "requirements.txt").exists())

    def test_the_sites_registry_knows_about_github(self):
        sys.path.insert(0, str(PROJECT_ROOT / "scripts"))
        self.addCleanup(sys.path.remove, str(PROJECT_ROOT / "scripts"))
        import sites

        site = sites.SITES_BY_NAME["github"]

        self.assertEqual(site.directory, "github")
        self.assertEqual(site.title, "GitHub Bridge")
        self.assertEqual(site.entry, "github/run.py")
        # 唯一一个不需要额外依赖的站点。
        self.assertEqual(site.extra_modules, ())
        self.assertEqual(site.requirements, "")
        self.assertEqual(site.files, ("run.py",))
        self.assertIn("github", sites.SITE_ROUTE_PATHS)
        self.assertIn("github", sites.SITE_ASSETS)

    def test_the_share_packages_cover_github(self):
        sys.path.insert(0, str(PROJECT_ROOT / "scripts"))
        self.addCleanup(sys.path.remove, str(PROJECT_ROOT / "scripts"))
        import make_share_packages as packaging

        spec = packaging.PACKAGES_BY_KEY["github-api"]

        self.assertEqual(spec.kind, "api")
        self.assertEqual(spec.title, "GitHub 接口包")
        site = packaging.SITES_BY_NAME["github"]
        self.assertIn(site.directory, spec.entries())

    def test_the_nav_order_has_no_gap(self):
        from pixiv_bridge.__main__ import site_items

        orders = sorted(item.order for item in site_items())

        self.assertEqual(orders, list(range(1, len(orders) + 1)))

    def test_the_nav_page_lists_github_last(self):
        from pixiv_bridge.__main__ import site_items

        items = site_items()

        self.assertEqual(items[-1].name, "github")
        self.assertEqual(items[-1].title, "GitHub Bridge")
        self.assertTrue(items[-1].description.startswith("github 接口站点"), items[-1].description)


# ---------------------------------------------------------------- 入口脚本


class GithubRunScriptTestCase(unittest.TestCase):
    """``github/run.py`` 的三段式装配顺序。"""

    def test_run_script_puts_the_project_root_on_the_path_first(self):
        source = (PROJECT_ROOT / "github" / "run.py").read_text(encoding="utf-8")

        self.assertLess(source.index("sys.path.insert"), source.index("bootstrap("))
        # 必须先装依赖、再 import 站点模块（app.py 会拖进 httpx）。
        self.assertLess(source.index("bootstrap("), source.index("from github.app import"))

    def test_the_entry_point_script_exists(self):
        self.assertTrue((PROJECT_ROOT / "github" / "run.py").exists())

    def test_the_package_init_does_not_import_the_app_eagerly(self):
        source = (PROJECT_ROOT / "github" / "__init__.py").read_text(encoding="utf-8")

        self.assertNotIn("from .app import", source)


if __name__ == "__main__":
    unittest.main()
