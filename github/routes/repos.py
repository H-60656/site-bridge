"""github 的只读 JSON 路由。

所有端点都在 ``/api/github`` 前缀下，只做两件事：把路径/查询参数校验干净，把
:class:`~github.client.GithubError` 翻成统一的 ``{"error": {...}}`` 响应。上游
字段的归一化在 :mod:`github.client` 里做完，这里不再碰 ``stargazers_count`` 这类
拼写 —— 路由层只认识本端定好的键。

没有写操作（不建 issue、不 star、不发 PR），因此也不需要 CSRF / 二次确认那一套。
"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Path, Query, Request

from . import get_client
from ..client import GithubError, MAX_PER_PAGE

router = APIRouter(prefix="/api/github", tags=["github"])

#: ``owner`` / ``repo`` 的合法字符。GitHub 允许 ``.`` ``_`` ``-``，不允许 ``/``。
#: 提前挡住畸形名字，省一次上游往返（顺便把配额省下来）。
_NAME = r"^[A-Za-z0-9][A-Za-z0-9._-]{0,99}$"


def _raise(exc: GithubError):
    raise HTTPException(status_code=exc.status, detail=exc.message) from exc


@router.get("/repo/{owner}/{repo}")
async def repo(
    request: Request,
    owner: str = Path(..., pattern=_NAME, description="用户或组织"),
    repo: str = Path(..., pattern=_NAME, description="仓库名"),
):
    """仓库主档（star / fork / 语言 / 许可证 / 默认分支）。"""
    client = get_client(request)
    try:
        return await client.repo(owner, repo)
    except GithubError as exc:
        _raise(exc)


@router.get("/readme/{owner}/{repo}")
async def readme(
    request: Request,
    owner: str = Path(..., pattern=_NAME, description="用户或组织"),
    repo: str = Path(..., pattern=_NAME, description="仓库名"),
    ref: str = Query("", description="分支 / tag / commit，留空用默认分支"),
):
    """README 正文（已 base64 解码成文本）。"""
    client = get_client(request)
    try:
        return await client.readme(owner, repo, ref=ref)
    except GithubError as exc:
        _raise(exc)


@router.get("/contents/{owner}/{repo}")
@router.get("/contents/{owner}/{repo}/{path:path}")
async def contents(
    request: Request,
    owner: str = Path(..., pattern=_NAME, description="用户或组织"),
    repo: str = Path(..., pattern=_NAME, description="仓库名"),
    path: str = "",
    ref: str = Query("", description="分支 / tag / commit，留空用默认分支"),
):
    """目录列表或单文件条目。上游同一个接口返两种形状，客户端已统一。

    ``path`` 的穿越/注入校验在 ``GithubClient.contents`` 里（``clean_repo_path``）：
    这里用 ``{path:path}`` 收整段，路由层校验不了含斜杠的内容，越界会被翻成 400。
    """
    client = get_client(request)
    try:
        return await client.contents(owner, repo, path=path, ref=ref)
    except GithubError as exc:
        _raise(exc)


@router.get("/releases/{owner}/{repo}")
async def releases(
    request: Request,
    owner: str = Path(..., pattern=_NAME, description="用户或组织"),
    repo: str = Path(..., pattern=_NAME, description="仓库名"),
    per_page: int | None = Query(None, ge=1, le=MAX_PER_PAGE, description="留空用 GITHUB_PER_PAGE"),
    page: int = Query(1, ge=1),
):
    """发行版列表（含资产清单）。"""
    client = get_client(request)
    try:
        return await client.releases(owner, repo, per_page=per_page, page=page)
    except GithubError as exc:
        _raise(exc)


@router.get("/issues/{owner}/{repo}")
async def issues(
    request: Request,
    owner: str = Path(..., pattern=_NAME, description="用户或组织"),
    repo: str = Path(..., pattern=_NAME, description="仓库名"),
    state: str = Query("open", pattern="^(open|closed|all)$"),
    per_page: int | None = Query(None, ge=1, le=MAX_PER_PAGE, description="留空用 GITHUB_PER_PAGE"),
    page: int = Query(1, ge=1),
):
    """issue 列表；``is_pull_request`` 用来区分 PR（上游把它们混在一个端点里）。"""
    client = get_client(request)
    try:
        return await client.issues(owner, repo, state=state, per_page=per_page, page=page)
    except GithubError as exc:
        _raise(exc)


@router.get("/commits/{owner}/{repo}")
async def commits(
    request: Request,
    owner: str = Path(..., pattern=_NAME, description="用户或组织"),
    repo: str = Path(..., pattern=_NAME, description="仓库名"),
    sha: str = Query("", description="分支 / tag / commit，留空用默认分支"),
    per_page: int | None = Query(None, ge=1, le=MAX_PER_PAGE, description="留空用 GITHUB_PER_PAGE"),
    page: int = Query(1, ge=1),
):
    """提交列表（首行摘要 + 作者 + 日期）。"""
    client = get_client(request)
    try:
        return await client.commits(owner, repo, sha=sha, per_page=per_page, page=page)
    except GithubError as exc:
        _raise(exc)


@router.get("/search/repositories")
async def search_repositories(
    request: Request,
    q: str = Query(..., min_length=1, description="GitHub 搜索语法，如 `language:python stars:>1000`"),
    per_page: int | None = Query(None, ge=1, le=MAX_PER_PAGE, description="留空用 GITHUB_PER_PAGE"),
    page: int = Query(1, ge=1),
    sort: str = Query("", pattern="^(|stars|forks|help-wanted-issues|updated)$"),
    order: str = Query("desc", pattern="^(asc|desc)$"),
):
    """仓库搜索。注意 search 配额是**独立**的，未认证只有 10 次/小时。"""
    client = get_client(request)
    try:
        return await client.search_repositories(
            q, per_page=per_page, page=page, sort=sort, order=order
        )
    except GithubError as exc:
        _raise(exc)


@router.get("/user/{login}")
async def user(
    request: Request,
    login: str = Path(..., pattern=_NAME, description="用户名或组织名"),
):
    """用户 / 组织主档。"""
    client = get_client(request)
    try:
        return await client.user(login)
    except GithubError as exc:
        _raise(exc)


@router.get("/rate_limit")
async def rate_limit(request: Request):
    """官方配额账本（core / search / graphql 各自一组）。

    这个端点是**排障入口**，所以刻意做得很宽容：上游那个 ``/rate_limit`` 不计费、
    因此客户端不会拦它；但如果上游本身就连不上（断网 / 改错了 ``GITHUB_API_URL``），
    这里也不抛错，而是照旧返回本进程自己记的账，外加一条 ``upstream_error`` 说明
    ——「查不到官方账本」不该连「本地还剩多少」一起赔进去。
    """
    client = get_client(request)
    payload: dict = {}
    upstream_error = ""
    try:
        body = await client.rate_limit()
        if isinstance(body, dict):
            payload = body
    except GithubError as exc:
        upstream_error = exc.message
    payload["client"] = client.budget()
    if upstream_error:
        payload["upstream_error"] = upstream_error
    return payload


@router.get("/auth")
async def auth(request: Request):
    """当前配置的认证状态与预算 —— **不发起任何上游请求**。"""
    client = get_client(request)
    return {
        "authenticated": client.authenticated,
        "api_url": client.base_url,
        "budget": client.budget(),
        "note": (
            "已配置 GITHUB_TOKEN，配额 5000 次/小时"
            if client.authenticated
            else "未配置 GITHUB_TOKEN，配额 60 次/小时（search 10 次/小时）"
        ),
    }
