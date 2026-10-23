"""github 站点的上游客户端：只认 ``api.github.com`` 这一份 JSON 契约。

与 iwara / asmr_one / ehviewer 相比，这个站点**没有**任何穿透/借道手段 ——
``api.github.com`` 在本机直连就通（实测 200），所以这里既不读 ``settings.proxy``，
也不做 ECH 与边缘 IP 借道。它真正的敌人只剩一个：**配额**。

未认证只有 60 次/小时，search 更狠（10 次/小时），而 GitHub 的 403 是**软失败**
—— 状态码 403，正文 ``{"message": "API rate limit exceeded for ..."}``，如果只看
状态码，会误判成「没权限」。所以本模块做三件事：

1. **把配额当成一等公民**：每次响应都把 ``x-ratelimit-*`` 三件套记下来，下一次
   请求前先问「还剩几次、什么时候重置」，配额见底就**直接本地拒绝**（带上还要
   等多少秒），不去撞上游；
2. **按预算配速**：用框架的 :class:`~site_bridge.rate_limit.RateLimiter` 把节奏
   压到「用满一小时也不超」—— 未认证 1 次/分钟，带 token 才放开；
3. **出口处把语义定死**：403 究竟是限流还是真没权限，用 ``x-ratelimit-remaining``
   与正文 ``message`` 横向判定，翻成不同的异常类（``GithubRateLimitError`` /
   ``GithubForbiddenError``），而不是一律 403 丢给调用方猜。

字段归一化同样在出口做：上游给什么单位、叫什么名字，到这里统一成一套扁平键
（``stars`` / ``forks`` / ``open_issues`` / ``size_kb`` …），路由层因此不需要认识
``stargazers_count`` 这种上游拼写。
"""

from __future__ import annotations

import base64
import re
import time
from typing import Any
from urllib.parse import parse_qs, urlparse

import httpx

from site_bridge.rate_limit import RateLimiter

from .config import DEFAULT_API_URL, GithubSettings, as_github_settings

#: 上游单页上限；超了 GitHub 会 422。
MAX_PER_PAGE = 100

#: 未认证 / 带 token 两种预算（次/小时）。
ANONYMOUS_BUDGET = 60
AUTHENTICATED_BUDGET = 5000

#: 缺 ``x-ratelimit-resource`` 头时的归属 —— 绝大多数端点都在 core 这本账上。
_DEFAULT_RESOURCE = "core"

_DEFAULT_HEADERS = {
    "accept": "application/vnd.github+json",
    "x-github-api-version": "2022-11-28",
    "user-agent": "site-bridge-github/1.0 (+https://github.com/)",
}

_LINK_RE = re.compile(r'<([^>]+)>\s*;\s*rel="([^"]+)"')


class GithubError(Exception):
    """站点异常基类：三个属性就是统一错误体里的三个值。"""

    status = 502
    reason = "github_error"
    message = ""

    def __init__(self, message: str = "", status: int | None = None, reason: str | None = None):
        super().__init__(message or self.__class__.message)
        self.message = message or self.__class__.message
        if status is not None:
            self.status = status
        if reason is not None:
            self.reason = reason


class GithubNotFoundError(GithubError):
    """404：仓库 / 用户 / 路径不存在（也可能是不存在但被私有化）。"""

    status = 404
    reason = "not_found"
    message = "GitHub 上找不到这个资源"


class GithubUnauthorizedError(GithubError):
    """401：token 无效或已过期。"""

    status = 401
    reason = "auth_required"
    message = "GitHub token 无效或已过期"


class GithubForbiddenError(GithubError):
    """403 但不是限流：真没权限（私有仓库、被组织策略挡住等）。"""

    status = 403
    reason = "forbidden"
    message = "GitHub 拒绝了这个请求（权限不足）"


class GithubRateLimitError(GithubError):
    """配额用尽 —— 上游给 403，本端翻成 429，语义更准。"""

    status = 429
    reason = "rate_limited"
    message = "GitHub 配额已用尽"


def _text(value, default=""):
    """上游字段可能是 None；统一成字符串，省得调用方到处 ``or ""``。"""
    if value is None:
        return default
    return str(value)


def _int(value, default=0):
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _clamp_per_page(value) -> int:
    return max(1, min(MAX_PER_PAGE, _int(value, 30) or 30))


#: ``contents`` 的路径里不该出现的字符：``?`` ``#`` 会在拼 URL 时被 httpx 当成查询
#: 串/片段的分隔符，``\`` 在部分实现里等同于 ``/``。
_REJECTED_PATH_CHARS = ("?", "#", "\\")


def clean_repo_path(path: str) -> str:
    """把仓库内路径规范成「相对、无穿越」的形状，或直接拒绝。

    ``/contents/{owner}/{repo}/{path:path}`` 使用 ``path`` 转换器，斜杠会被整段吞进
    参数里；如果不校验，``..`` 就能一路爬出 ``/repos/{owner}/{repo}/contents`` 的
    前缀，打到 ``/rate_limit``、``/user/emails`` 这类**别的**上游端点上去。路由层的
    ``_NAME`` 只能管住 ``owner`` / ``repo``，管不到这里，所以必须在拼 URL 之前挡。

    返回去掉了首尾多余的 ``/`` 的路径；不合法就抛 :class:`GithubError` 400。
    """
    raw = _text(path)
    if any(char in raw for char in _REJECTED_PATH_CHARS):
        raise GithubError(
            "路径里不能出现 ? / # / \\ —— 目录内的路径不需要它们", status=400, reason="invalid_path"
        )
    parts: list[str] = []
    for part in raw.split("/"):
        if part in ("", "."):
            continue
        if part == "..":
            raise GithubError(
                "路径里不能出现 .. —— 这会让请求爬出仓库范围", status=400, reason="invalid_path"
            )
        parts.append(part)
    return "/".join(parts)


def _parse_link(value) -> dict[str, str]:
    """解析 ``Link: <url>; rel="next", <url>; rel="last"``。"""
    out: dict[str, str] = {}
    for match in _LINK_RE.finditer(_text(value)):
        out.setdefault(match.group(2), match.group(1))
    return out


def _page_of(url) -> int | None:
    """从 ``?page=2`` 里取页码；取不到就返回 None（不是猜 1）。"""
    values = parse_qs(urlparse(_text(url)).query).get("page")
    if not values:
        return None
    return _int(values[0], 0) or None


def _license_of(raw) -> dict[str, str]:
    if not isinstance(raw, dict):
        return {"spdx_id": "", "name": ""}
    return {"spdx_id": _text(raw.get("spdx_id")), "name": _text(raw.get("name"))}


def _owner_of(raw) -> dict[str, str]:
    if not isinstance(raw, dict):
        return {"login": "", "html_url": "", "avatar_url": ""}
    return {
        "login": _text(raw.get("login")),
        "html_url": _text(raw.get("html_url")),
        "avatar_url": _text(raw.get("avatar_url")),
    }


def normalize_repo(raw) -> dict[str, Any]:
    """仓库对象 → 扁平键。列表与搜索里复用同一份，保证两处口径一致。"""
    if not isinstance(raw, dict):
        raw = {}
    topics = raw.get("topics") or []
    return {
        "full_name": _text(raw.get("full_name")),
        "name": _text(raw.get("name")),
        "owner": _owner_of(raw.get("owner")),
        "description": _text(raw.get("description")),
        "html_url": _text(raw.get("html_url")),
        "homepage": _text(raw.get("homepage")),
        "language": _text(raw.get("language")),
        "license": _license_of(raw.get("license")),
        "topics": [_text(item) for item in topics if _text(item)],
        "stars": _int(raw.get("stargazers_count")),
        "forks": _int(raw.get("forks_count")),
        "watchers": _int(raw.get("subscribers_count", raw.get("watchers_count"))),
        "open_issues": _int(raw.get("open_issues_count")),
        "default_branch": _text(raw.get("default_branch")),
        "archived": bool(raw.get("archived")),
        "fork": bool(raw.get("fork")),
        "private": bool(raw.get("private")),
        "created_at": _text(raw.get("created_at")),
        "updated_at": _text(raw.get("updated_at")),
        "pushed_at": _text(raw.get("pushed_at")),
        "size_kb": _int(raw.get("size")),
    }


def normalize_user(raw) -> dict[str, Any]:
    if not isinstance(raw, dict):
        raw = {}
    return {
        "login": _text(raw.get("login")),
        "id": _int(raw.get("id")),
        "name": _text(raw.get("name")),
        "type": _text(raw.get("type")),
        "html_url": _text(raw.get("html_url")),
        "avatar_url": _text(raw.get("avatar_url")),
        "bio": _text(raw.get("bio")),
        "company": _text(raw.get("company")),
        "blog": _text(raw.get("blog")),
        "location": _text(raw.get("location")),
        "public_repos": _int(raw.get("public_repos")),
        "public_gists": _int(raw.get("public_gists")),
        "followers": _int(raw.get("followers")),
        "following": _int(raw.get("following")),
        "created_at": _text(raw.get("created_at")),
        "updated_at": _text(raw.get("updated_at")),
    }


def normalize_release(raw) -> dict[str, Any]:
    if not isinstance(raw, dict):
        raw = {}
    assets = raw.get("assets") or []
    return {
        "id": _int(raw.get("id")),
        "tag_name": _text(raw.get("tag_name")),
        "name": _text(raw.get("name")),
        "draft": bool(raw.get("draft")),
        "prerelease": bool(raw.get("prerelease")),
        "created_at": _text(raw.get("created_at")),
        "published_at": _text(raw.get("published_at")),
        "html_url": _text(raw.get("html_url")),
        "tarball_url": _text(raw.get("tarball_url")),
        "zipball_url": _text(raw.get("zipball_url")),
        "body": _text(raw.get("body")),
        "author": _owner_of(raw.get("author")),
        "assets": [
            {
                "name": _text(item.get("name")),
                "size": _int(item.get("size")),
                "download_count": _int(item.get("download_count")),
                "content_type": _text(item.get("content_type")),
                "browser_download_url": _text(item.get("browser_download_url")),
            }
            for item in assets
            if isinstance(item, dict)
        ],
    }


def normalize_issue(raw) -> dict[str, Any]:
    """issue 与 pull request 共用这份形状（``is_pull_request`` 区分）。"""
    if not isinstance(raw, dict):
        raw = {}
    labels = raw.get("labels") or []
    return {
        "number": _int(raw.get("number")),
        "title": _text(raw.get("title")),
        "state": _text(raw.get("state")),
        "comments": _int(raw.get("comments")),
        "html_url": _text(raw.get("html_url")),
        "created_at": _text(raw.get("created_at")),
        "updated_at": _text(raw.get("updated_at")),
        "closed_at": _text(raw.get("closed_at")),
        "user": _owner_of(raw.get("user")),
        "labels": [_text(item.get("name")) for item in labels if isinstance(item, dict)],
        "is_pull_request": isinstance(raw.get("pull_request"), dict),
    }


def normalize_commit(raw) -> dict[str, Any]:
    if not isinstance(raw, dict):
        raw = {}
    commit = raw.get("commit") if isinstance(raw.get("commit"), dict) else {}
    author = commit.get("author") if isinstance(commit.get("author"), dict) else {}
    message = _text(commit.get("message"))
    return {
        "sha": _text(raw.get("sha")),
        "message": message,
        "summary": message.splitlines()[0] if message else "",
        "author": {
            "name": _text(author.get("name")),
            "email": _text(author.get("email")),
            "date": _text(author.get("date")),
        },
        "html_url": _text(raw.get("html_url")),
    }


def normalize_entry(raw) -> dict[str, Any]:
    """``/contents`` 的目录项 / 单文件项。"""
    if not isinstance(raw, dict):
        raw = {}
    return {
        "name": _text(raw.get("name")),
        "path": _text(raw.get("path")),
        "type": _text(raw.get("type")),
        "size": _int(raw.get("size")),
        "sha": _text(raw.get("sha")),
        "html_url": _text(raw.get("html_url")),
        "download_url": _text(raw.get("download_url")),
    }


def decode_content(payload) -> str:
    """base64 正文 → 文本。解不出来就返回空串，绝不抛出。"""
    if not isinstance(payload, str) or not payload:
        return ""
    try:
        return base64.b64decode(payload).decode("utf-8", "replace")
    except Exception:  # noqa: BLE001 —— 坏 base64 不该让整个接口 500
        return ""


class GithubClient:
    """``api.github.com`` 的薄客户端：一个 ``httpx.AsyncClient`` + 配额账本。

    ``http`` 是给测试用的注入点（换成 ``httpx.MockTransport`` 就完全离线）；
    ``pace=False`` 关掉配速，测试里不必等令牌。
    """

    def __init__(self, settings=None, http: httpx.AsyncClient | None = None, pace: bool = True):
        self.settings: GithubSettings = as_github_settings(settings)
        self.base_url = (self.settings.api_url or DEFAULT_API_URL).rstrip("/")
        self._owns_http = http is None
        self._http = http or httpx.AsyncClient(
            headers=_DEFAULT_HEADERS,
            timeout=self.settings.request_timeout,
            follow_redirects=True,
            # 明确不继承环境/系统代理设置。``api.github.com`` 直连就通，而走本机的
            # 代理出口反而**更容易**撞上别人的限额（实测同一个 /rate_limit：直连剩
            # 56 次，经代理剩 0 次）。注意 ``trust_env=False`` 也一并关掉了
            # ``SSL_CERT_FILE`` / ``.netrc`` 的自动读取 —— 这正是「不要中间人」。
            trust_env=False,
        )
        self._headers = dict(_DEFAULT_HEADERS)
        if self.settings.authenticated:
            self._headers["authorization"] = f"Bearer {self.settings.token.strip()}"
        #: 配速用**配置的**每分钟额度（默认 100），不按小时预算摊薄 —— 60/小时摊成
        #: 1 次/分钟会让一次页面加载都跑不完。真正的硬闸是下面的配额账本：它知道
        #: 还剩几次、什么时候重置，见底就本地拒绝，不必靠「慢慢发」来避免超限。
        self._limiter = RateLimiter(self.settings.rate_limit_rpm) if pace else None
        #: 配额账本 —— **按资源分桶**。GitHub 给 core / search / graphql 各发一本独立
        #: 的账（响应头 ``x-ratelimit-resource`` 就是账本名），未认证时 core 60 次/小时、
        #: search 只有 10 次/小时。曾经只用一组标量记「最近一次」的额度，结果是：搜索
        #: 跑掉几次把 search 的账记成 0 之后，**core 请求也被本地拒了** —— 明明仓库
        #: 详情还剩几十次。所以键必须分层，见 :meth:`_note_budget`。
        self.buckets: dict[str, dict[str, Any]] = {}

    # ---------------------------------------------------------------- 生命周期

    async def aclose(self):
        if self._owns_http:
            await self._http.aclose()

    @property
    def authenticated(self) -> bool:
        return self.settings.authenticated

    @property
    def default_per_page(self) -> int:
        """列表接口不指定 ``per_page`` 时用的默认值（来自 ``GITHUB_PER_PAGE``）。"""
        return _clamp_per_page(self.settings.per_page)

    # ---------------------------------------------------------------- 配额账本

    def _bucket(self, resource: str | None = None) -> dict[str, Any]:
        """取（必要时新建）某个资源的账本。"""
        return self.buckets.setdefault(resource or _DEFAULT_RESOURCE, {})

    def _view(self, resource: str | None = None) -> dict[str, Any]:
        """把一个桶摊成对外的扁平视图。"""
        bucket = self.buckets.get(resource or _DEFAULT_RESOURCE) or {}
        reset_at = bucket.get("reset_at")
        return {
            "limit": bucket.get("limit"),
            "remaining": bucket.get("remaining"),
            "used": bucket.get("used"),
            "reset": int(reset_at) if reset_at else None,
            "reset_at": (
                time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(reset_at)) if reset_at else ""
            ),
            "seconds_until_reset": (
                max(0, int(reset_at - time.time())) if reset_at else None
            ),
        }

    # ---- core 视角的兼容属性：老代码（与老测试）直接读写这几个名字。
    #      注意读写都落在 core 桶上，search 的额度不会从这里漏出来。

    @property
    def limit(self) -> int | None:
        return self._view(_DEFAULT_RESOURCE)["limit"]

    @limit.setter
    def limit(self, value: int | None) -> None:
        self._bucket(_DEFAULT_RESOURCE)["limit"] = value

    @property
    def remaining(self) -> int | None:
        return self._view(_DEFAULT_RESOURCE)["remaining"]

    @remaining.setter
    def remaining(self, value: int | None) -> None:
        self._bucket(_DEFAULT_RESOURCE)["remaining"] = value

    @property
    def used(self) -> int | None:
        return self._view(_DEFAULT_RESOURCE)["used"]

    @used.setter
    def used(self, value: int | None) -> None:
        self._bucket(_DEFAULT_RESOURCE)["used"] = value

    @property
    def reset_at(self) -> float | None:
        return self._bucket(_DEFAULT_RESOURCE).get("reset_at")

    @reset_at.setter
    def reset_at(self, value: float | None) -> None:
        self._bucket(_DEFAULT_RESOURCE)["reset_at"] = value

    def budget(self) -> dict[str, Any]:
        """把已知的配额状态摊开（``/rate_limit`` 之外的接口也能报）。

        同时给两份视图：``resources`` 是按资源分组的**真账**（core / search / …），
        外层的 ``limit`` / ``remaining`` 等扁平键是 core 的兼容视图 —— 老调用方读的
        就是这个，但它们不该被 search 的数字污染。
        """
        return {
            "authenticated": self.authenticated,
            "resources": {name: self._view(name) for name in self.buckets},
            **self._view(_DEFAULT_RESOURCE),
        }

    def _note_budget(self, headers, fallback_resource: str = _DEFAULT_RESOURCE) -> None:
        """把 ``x-ratelimit-*`` 记进**对应资源**的桶里。

        ``x-ratelimit-resource`` 是上游自报的账本名（``core`` / ``search`` /
        ``graphql`` / ``integration_manifest`` …）。缺失时才退回到调用方预期的那个。
        """
        resource = _text(headers.get("x-ratelimit-resource")).strip() or fallback_resource
        bucket = self._bucket(resource)
        limit = headers.get("x-ratelimit-limit")
        if limit is not None:
            bucket["limit"] = _int(limit, 0)
        remaining = headers.get("x-ratelimit-remaining")
        if remaining is not None:
            bucket["remaining"] = _int(remaining, 0)
        used = headers.get("x-ratelimit-used")
        if used is not None:
            bucket["used"] = _int(used, 0)
        reset = headers.get("x-ratelimit-reset")
        if reset is not None:
            bucket["reset_at"] = float(_int(reset, 0)) or bucket.get("reset_at")

    def _refuse_when_exhausted(self, resource: str = _DEFAULT_RESOURCE) -> None:
        """**这本账**已经见底、重置时刻又还没到，就别去撞上游了。

        只看 ``resource`` 对应的桶：search 用完不影响 core，反之亦然。
        """
        bucket = self.buckets.get(resource) or {}
        remaining = bucket.get("remaining")
        if remaining is None or remaining > 0:
            return
        reset_at = bucket.get("reset_at")
        if reset_at is None or reset_at <= time.time():
            return
        wait = max(1, int(reset_at - time.time()))
        limit = bucket.get("limit")
        raise GithubRateLimitError(
            f"GitHub {resource} 配额已用尽（{limit or '?'}/小时），约 {wait} 秒后重置"
            + ("" if self.authenticated else "；配 GITHUB_TOKEN 可提到 5000 次/小时"),
            status=429,
        )

    # ---------------------------------------------------------------- 请求

    async def _request(
        self,
        path: str,
        params: dict[str, Any] | None = None,
        resource: str = _DEFAULT_RESOURCE,
        free: bool = False,
    ) -> Any:
        """GET 一个 JSON 资源，并按状态码翻成站点异常。

        ``resource`` 只是**预期**归属：真正记账时以上游回的
        ``x-ratelimit-resource`` 为准，它才是权威。

        ``free=True`` 用于上游**不计费**的端点（目前只有 ``/rate_limit``）：
        它们不占配额，所以也绝不能被 :meth:`_refuse_when_exhausted` 拦下 ——
        否则「配额用完了，去查一下还剩多少/什么时候重置」这条唯一的出路
        会先被自己关掉，用户只能对着 429 干等一小时。
        """
        if not free:
            self._refuse_when_exhausted(resource)
        if self._limiter is not None:
            await self._limiter.acquire()
        url = f"{self.base_url}{path}"
        response = await self._http.get(url, params=params, headers=self._headers)
        self._note_budget(response.headers, resource)
        return self._unwrap(response, path, resource)

    def _unwrap(self, response: httpx.Response, path: str, resource: str = _DEFAULT_RESOURCE) -> Any:
        status = response.status_code
        if 200 <= status < 300:
            try:
                return response.json()
            except ValueError as exc:  # noqa: TRY301
                raise GithubError(
                    f"GitHub 返回了非 JSON 响应（{path}）", status=502, reason="github_upstream"
                ) from exc

        body: dict[str, Any] = {}
        try:
            parsed = response.json()
            if isinstance(parsed, dict):
                body = parsed
        except ValueError:
            pass
        message = _text(body.get("message")) or f"GitHub 返回 {status}"

        if status == 404:
            raise GithubNotFoundError(f"GitHub 上找不到这个资源：{path}")
        if status == 401:
            raise GithubUnauthorizedError(f"GitHub token 无效或已过期：{message}")
        if status == 403:
            # 403 有两种：限流（软失败）与真没权限。用**这本账**的余额 + 正文横向判定
            # —— 拿别的资源的余量来判断会两头都判错。
            bucket = self.buckets.get(resource) or {}
            remaining = bucket.get("remaining")
            reset_at = bucket.get("reset_at")
            exhausted = remaining == 0 or "rate limit" in message.lower()
            if exhausted:
                raise GithubRateLimitError(
                    f"{message}"
                    + (
                        f"（约 {max(0, int(reset_at - time.time()))} 秒后重置）"
                        if reset_at
                        else ""
                    )
                )
            raise GithubForbiddenError(message)
        if status == 429:
            raise GithubRateLimitError(message)
        if status == 422:
            raise GithubError(f"GitHub 认为参数不合法：{message}", status=422, reason="invalid_params")
        if status >= 500:
            raise GithubError(
                f"GitHub 服务端错误（{status}）：{message}", status=502, reason="github_upstream"
            )
        raise GithubError(message, status=502, reason="github_upstream")

    async def get_json(self, path: str, params: dict[str, Any] | None = None) -> Any:
        """给路由层与自检用的通用入口。"""
        return await self._request(path, params)

    # ---------------------------------------------------------------- 端点

    async def repo(self, owner: str, repo: str) -> dict[str, Any]:
        raw = await self._request(f"/repos/{owner}/{repo}")
        return normalize_repo(raw)

    async def readme(self, owner: str, repo: str, ref: str = "") -> dict[str, Any]:
        raw = await self._request(f"/repos/{owner}/{repo}/readme", {"ref": ref} if ref else None)
        if not isinstance(raw, dict):
            raw = {}
        entry = normalize_entry(raw)
        entry["encoding"] = _text(raw.get("encoding"))
        entry["content"] = decode_content(raw.get("content"))
        return entry

    async def contents(self, owner: str, repo: str, path: str = "", ref: str = "") -> dict[str, Any]:
        """目录 → 列表；单文件 → 一项。上游同一个接口返两种形状，这里统一。

        ``path`` 先过 :func:`clean_repo_path`：拒绝 ``..`` 穿越与 ``?`` / ``#`` 注入。
        """
        cleaned = clean_repo_path(path)
        suffix = f"/contents/{cleaned}" if cleaned else "/contents/"
        raw = await self._request(f"/repos/{owner}/{repo}{suffix}", {"ref": ref} if ref else None)
        if isinstance(raw, list):
            items = [normalize_entry(item) for item in raw if isinstance(item, dict)]
            return {"type": "dir", "path": cleaned, "count": len(items), "items": items}
        entry = normalize_entry(raw)
        entry["type"] = entry["type"] or "file"
        entry["count"] = 1
        return entry

    async def releases(
        self, owner: str, repo: str, per_page: int | None = None, page: int = 1
    ) -> dict[str, Any]:
        return await self._paged(
            f"/repos/{owner}/{repo}/releases", {}, per_page, page, normalize_release
        )

    async def issues(
        self,
        owner: str,
        repo: str,
        state: str = "open",
        per_page: int | None = None,
        page: int = 1,
    ) -> dict[str, Any]:
        return await self._paged(
            f"/repos/{owner}/{repo}/issues",
            {"state": state},
            per_page,
            page,
            normalize_issue,
        )

    async def commits(
        self,
        owner: str,
        repo: str,
        sha: str = "",
        per_page: int | None = None,
        page: int = 1,
    ) -> dict[str, Any]:
        params = {"sha": sha} if sha else {}
        return await self._paged(
            f"/repos/{owner}/{repo}/commits", params, per_page, page, normalize_commit
        )

    async def search_repositories(
        self,
        query: str,
        per_page: int | None = None,
        page: int = 1,
        sort: str = "",
        order: str = "desc",
    ) -> dict[str, Any]:
        per_page = self.default_per_page if per_page is None else _clamp_per_page(per_page)
        page = max(1, _int(page, 1) or 1)
        params: dict[str, Any] = {"q": query, "per_page": per_page, "page": page}
        if sort:
            params["sort"] = sort
            params["order"] = order
        # search 是**独立**的一本账（未认证 10 次/小时），记账与拒发都按这个资源走。
        response = await self._request_with_headers(
            "/search/repositories", params, resource="search"
        )
        payload = response["data"]
        if not isinstance(payload, dict):
            payload = {}
        items = [normalize_repo(item) for item in (payload.get("items") or []) if isinstance(item, dict)]
        link = _parse_link(response["headers"].get("link"))
        return {
            "query": query,
            "total_count": _int(payload.get("total_count")),
            "incomplete_results": bool(payload.get("incomplete_results")),
            "page": page,
            "per_page": per_page,
            "count": len(items),
            "has_next": "next" in link,
            "next_page": _page_of(link.get("next")),
            "items": items,
        }

    async def user(self, login: str) -> dict[str, Any]:
        return normalize_user(await self._request(f"/users/{login}"))

    async def rate_limit(self) -> dict[str, Any]:
        """官方配额账本。上游明确**不计费**，所以这里用 ``free=True`` 放行 ——
        它是配额见底时唯一还能回答「还剩多少 / 什么时候重置」的端点。"""
        payload = await self._request("/rate_limit", free=True)
        resources: dict[str, Any] = {}
        if isinstance(payload, dict):
            for name, raw in (payload.get("resources") or {}).items():
                if not isinstance(raw, dict):
                    continue
                reset = _int(raw.get("reset"), 0)
                resources[name] = {
                    "limit": _int(raw.get("limit")),
                    "remaining": _int(raw.get("remaining")),
                    "used": _int(raw.get("used")),
                    "reset": reset or None,
                    "reset_at": (
                        time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(reset)) if reset else ""
                    ),
                }
        return {"authenticated": self.authenticated, "resources": resources}

    # ---------------------------------------------------------------- 内部分页

    async def _request_with_headers(
        self,
        path: str,
        params: dict[str, Any] | None = None,
        resource: str = _DEFAULT_RESOURCE,
    ) -> dict[str, Any]:
        """需要读 ``Link`` 头的场景：把 data 与 headers 一起交出去。"""
        self._refuse_when_exhausted(resource)
        if self._limiter is not None:
            await self._limiter.acquire()
        response = await self._http.get(
            f"{self.base_url}{path}", params=params, headers=self._headers
        )
        self._note_budget(response.headers, resource)
        return {"data": self._unwrap(response, path, resource), "headers": response.headers}

    async def _paged(
        self,
        path: str,
        params: dict[str, Any],
        per_page: int | None,
        page: int,
        normalizer,
    ) -> dict[str, Any]:
        """列表接口统一出口：条数用 ``len(实际收到的)``，``has_next`` 看 ``Link``。

        ``total`` 一律给 ``None`` —— 这几个端点上游**不提供**总数，编一个数比说
        「不知道」更坏（DIRECT.md 里记过这个教训）。

        ``per_page=None`` 表示「调用方没指定」，此时用 ``GITHUB_PER_PAGE`` 的配置值 ——
        这个开关只有在**真的被读到**时才算数，否则就是文档里一句空话。
        """
        params = dict(params)
        params["per_page"] = (
            self.default_per_page if per_page is None else _clamp_per_page(per_page)
        )
        params["page"] = max(1, _int(page, 1) or 1)
        response = await self._request_with_headers(path, params)
        payload = response["data"]
        if not isinstance(payload, list):
            payload = []
        items = [normalizer(item) for item in payload if isinstance(item, dict)]
        link = _parse_link(response["headers"].get("link"))
        return {
            "page": params["page"],
            "per_page": params["per_page"],
            "count": len(items),
            "total": None,
            "has_next": "next" in link,
            "next_page": _page_of(link.get("next")),
            "items": items,
        }
