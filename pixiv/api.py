"""Pixiv App API 客户端：拼签名头、发请求、把上游错误转成统一异常。

顺带承载小说正文的解析：2024-02 起官方下掉了 ``/v1/novel/text``，
正文只能从 ``/webview/v2/novel`` 的 HTML 里抠出来。
"""

import hashlib
import json
import time
from urllib.parse import urlparse

import httpx

from pixiv_bridge.rate_limit import RateLimiter

from .config import Settings
from .hoster import resolve, resolve_ech
from .oauth import OAuthManager
from . import ech_client, sni_client


class PixivApiError(RuntimeError):
    def __init__(self, message, status=400, reason=""):
        super().__init__(message)
        self.message = message
        self.status = status
        self.reason = reason


class PixivRateLimitError(PixivApiError):
    pass


# Pixiv 在 2024-02 的 App 更新里**下掉了 /v1/novel/text**：调用它只会得到
# {"error": {"user_message": "Specified end-point doesn't exist", "message": "", "reason": ""}} 的 404。
# 官方 iOS 客户端改用 app-api 上的 /webview/v2/novel：返回 HTML，正文在页面内联脚本的
# novel: {...}.text 里（参考 upbit/pixivpy#337，v3.7.5 起同理）。
# 参数只有 id 必需，其余按 pixiv 默认阅读参数给出。
NOVEL_VIEWER_PARAMS = {
    "font": "mincho",
    "font_size": "1.0em",
    "line_height": "1.8",
    "color": "#1F1F1F",
    "background_color": "#FFFFFF",
    "mode": "horizontal",
    "theme": "light",
    "margin_top": "60px",
    "margin_bottom": "50px",
    "viewer_version": "20221031_ai",
}


def _balanced_json(text, start):
    """从 text[start]（必须是 '{' 或 '['）开始按括号配对取一个完整 JSON 值；字符串内的括号跳过。"""
    if start >= len(text) or text[start] not in "{[":
        return None
    opener = text[start]
    closer = "}" if opener == "{" else "]"
    depth = 0
    in_string = False
    escaped = False
    for index in range(start, len(text)):
        char = text[index]
        if in_string:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
            continue
        if char == '"':
            in_string = True
        elif char == opener:
            depth += 1
        elif char == closer:
            depth -= 1
            if depth == 0:
                try:
                    return json.loads(text[start : index + 1])
                except ValueError:
                    return None
    return None


def extract_novel_from_webview(html):
    """取出 /webview/v2/novel 返回的 HTML 里内联的 novel 对象（含正文 text）。"""
    if not isinstance(html, str):
        return None
    marker = html.find("novel:")
    while marker != -1:
        brace = html.find("{", marker)
        if brace == -1:
            return None
        payload = _balanced_json(html, brace)
        if isinstance(payload, dict) and isinstance(payload.get("text"), str):
            return payload
        marker = html.find("novel:", marker + 1)
    return None


# 小说里 [uploadedimage:ID] 引用的插图，各尺寸 URL 的常见键名。
# 官方 App 与网页版用 128x128 / 480mw / 1200x1200 / original，这里统一成四档。
# 另有一批 web / 旧版接口形状的键名（original_image_url、square_medium_image_url…），
# 一律收进来：宁可多认几种键名，也不要让插图因为键名不同而取不到地址。
IMAGE_SIZE_ALIASES = (
    (
        "thumb",
        (
            "128x128",
            "thumbnail",
            "thumb",
            "small",
            "square_medium",
            "square_medium_image_url",
            "thumbnail_image_url",
        ),
    ),
    (
        "medium",
        (
            "480mw",
            "medium",
            "540x540",
            "480x480",
            "medium_image_url",
        ),
    ),
    (
        "large",
        (
            "1200x1200",
            "large",
            "1200mw",
            "large_image_url",
            "1200x1200_image_url",
        ),
    ),
    ("original", ("original", "original_image_url", "originalImageUrl")),
)

IMAGE_MAP_KEYS = (
    "textEmbeddedImages",
    "text_embedded_images",
    "embeddedImages",
    "embedded_images",
    "novelImages",
    "novel_images",
    "images",
    "image",
    "novelImage",
    "novel_image",
)


def normalize_image_urls(raw):
    """把一只插图的多种 URL 形状归一化成 {original,large,medium,thumb}。

    认得的形状：字符串、{urls|image_urls|imageUrls: {...}}、直接一串档位键名、
    以及 [{...}]（有的接口会把一张图包成单元素列表）。
    """
    if isinstance(raw, str):
        return {"original": raw} if raw.strip() else {}
    if isinstance(raw, (list, tuple)):
        for item in raw:
            urls = normalize_image_urls(item)
            if urls:
                return urls
        return {}
    if not isinstance(raw, dict):
        return {}
    source = raw
    for key in ("urls", "image_urls", "imageUrls", "image_url"):
        value = raw.get(key)
        if isinstance(value, dict):
            source = value
            break
    if source is raw:
        # 少数形状把地址放在 url/src 里（没有尺寸分档）。
        for key in ("url", "src", "link"):
            value = raw.get(key)
            if isinstance(value, str) and value.strip():
                return {"original": value}
    urls = {}
    for name, aliases in IMAGE_SIZE_ALIASES:
        for alias in aliases:
            value = source.get(alias)
            if isinstance(value, str) and value.strip():
                urls[name] = value
                break
    return urls


def extract_novel_images(novel):
    """抽取 novel 正文里 [uploadedimage:ID] → 图片 URL 的映射。

    兼容 textEmbeddedImages（网页/App 的 camelCase 形状）与 text_embedded_images
    （snake_case）等命名；键里带 "uploadedimage:" 前缀也会去掉；返回
    {"<imageId>": {original,large,medium,thumb}}，没有任何插图时返回 {}。
    """
    if not isinstance(novel, dict):
        return {}
    raw = None
    for key in IMAGE_MAP_KEYS:
        value = novel.get(key)
        if isinstance(value, dict) and value:
            raw = value
            break
    if not raw:
        return {}
    images = {}
    for image_id, entry in raw.items():
        key = str(image_id).strip()
        if ":" in key:
            key = key.rsplit(":", 1)[-1].strip()
        urls = normalize_image_urls(entry)
        if key and urls:
            images[key] = urls
    return images


def novel_text_has_embeds(text):
    """正文里是否出现 [uploadedimage:ID]（有就说明需要图片地址）。"""
    return isinstance(text, str) and "[uploadedimage:" in text


# webview 页面里可能把插图映射单独放在 novel 对象之外的内联脚本里，只认这几个键。
HTML_IMAGE_KEYS = (
    "textEmbeddedImages",
    "text_embedded_images",
    "embeddedImages",
    "embedded_images",
)


def extract_images_from_html(html):
    """在 webview HTML 里直接找 textEmbeddedImages 之类的内联映射。

    正常情况图片映射就挂在 novel 对象里（extract_novel_images 已覆盖）；
    这里兜住「Pixiv 把映射挪到页面另一处内联脚本」的情况：
    键名后面跟的第一个 `{`/`[` 开始按括号配对解析，能解析出 URL 就收下。
    """
    if not isinstance(html, str):
        return {}
    images = {}
    for key in HTML_IMAGE_KEYS:
        marker = html.find(key + ":")
        while marker != -1:
            start = -1
            for char in ("{", "["):
                found = html.find(char, marker)
                if found != -1 and (start == -1 or found < start):
                    start = found
            if start == -1:
                break
            payload = _balanced_json(html, start)
            if payload:
                images.update(extract_novel_images({key: payload}))
            marker = html.find(key + ":", marker + 1)
    return images


class PixivApi:
    def __init__(self, settings, oauth, http):
        self._settings = settings
        self._oauth = oauth
        self._http = http
        self._limiter = RateLimiter(settings.rate_limit_rpm)

    def _signed_headers(self):
        client_time = time.strftime("%Y-%m-%dT%H:%M:%S+00:00", time.gmtime())
        client_hash = hashlib.md5(
            (client_time + self._settings.hash_salt).encode("utf-8")
        ).hexdigest()
        s = self._settings
        return {
            "X-Client-Time": client_time,
            "X-Client-Hash": client_hash,
            "User-Agent": s.user_agent,
            "App-OS": s.app_os,
            "App-OS-Version": s.app_os_version,
            "App-Version": s.app_version,
            "Accept-Language": s.accept_language,
        }

    def _api_host(self):
        return urlparse(self._settings.api_base).hostname

    def _normalize_next_url(self, next_url):
        parsed = urlparse(next_url)
        if parsed.hostname != self._api_host():
            raise PixivApiError("next_url 主机不合法", status=400)
        path = parsed.path
        if parsed.query:
            path = f"{path}?{parsed.query}"
        return path

    async def _request(
        self, method, path, params=None, data=None, retried=False, expect_json=True
    ):
        await self._limiter.acquire()
        headers = self._signed_headers()
        headers["Authorization"] = f"Bearer {await self._oauth.access_token()}"
        try:
            if self._settings.proxy:
                # 配了代理就走代理转发：ECH/SNI 直连是绕过链路阻断的手段，代理本身已解决这件事。
                resp = await self._http.request(
                    method,
                    f"{self._settings.api_base}{path}",
                    params=params,
                    data=data,
                    headers=headers,
                    timeout=self._settings.request_timeout,
                )
            elif self._settings.ech:
                # ECH 优先：它加密 SNI、照常校验证书，比「无 SNI + 手工 Host」更可靠。
                resp = await ech_client.request(
                    method,
                    f"{self._settings.api_base}{path}",
                    params=params,
                    data=data,
                    headers=headers,
                    timeout=self._settings.request_timeout,
                    ips=resolve_ech(self._api_host(), self._settings.ech_ips),
                    settings=self._settings,
                )
            elif self._settings.bypass_sni:
                resp = await sni_client.request(
                    method,
                    f"{self._settings.api_base}{path}",
                    params=params,
                    data=data,
                    headers=headers,
                    timeout=self._settings.request_timeout,
                    ips=resolve(self._api_host(), self._settings.api_ips),
                )
            else:
                resp = await self._http.request(
                    method,
                    f"{self._settings.api_base}{path}",
                    params=params,
                    data=data,
                    headers=headers,
                    timeout=self._settings.request_timeout,
                )
        except (
            sni_client.SniConnectError,
            ech_client.EchConnectError,
            ech_client.EchUnavailable,
        ) as exc:
            raise PixivApiError(str(exc), status=502)
        except httpx.HTTPError as exc:
            raise PixivApiError(
                f"上游请求失败: {exc.__class__.__name__}", status=502
            )
        if resp.status_code == 200:
            # /webview/v2/novel 这类接口回的是 HTML，不能在成功分支里硬当 JSON 解析。
            if not expect_json:
                return resp.text
            # 200 也可能没有 JSON：Cloudflare 拦截页、上游改版、网关吐 HTML 都回 200。
            # 裸 resp.json() 抛的 JSONDecodeError 是 ValueError，没人兜 → 对外裸 500。
            try:
                return resp.json()
            except ValueError as exc:
                raise PixivApiError(
                    f"上游返回的不是 JSON（可能被拦了或改版了）：{resp.text[:200]}",
                    status=502,
                    reason="bad_upstream_body",
                ) from exc
        return await self._handle_error(
            method, path, params, data, resp, retried, expect_json
        )

    async def _handle_error(
        self, method, path, params, data, resp, retried, expect_json=True
    ):
        message, reason = resp.text, ""
        try:
            body = resp.json()
        except ValueError:
            body = None
        if isinstance(body, dict):
            # 上游错误体不止一种形状：
            #   {"error": {"message": ..., "reason": ...}}      —— app-api 常见形状
            #   {"error": "invalid_grant", "error_description": ...} —— OAuth 风格
            #   {"error": true, "message": ...}                 —— 布尔标记
            # 早期实现直接对 error 调 .get()，遇到字符串/布尔就 AttributeError → 500。
            error = body.get("error")
            if isinstance(error, dict):
                message = error.get("message") or body.get("message") or ""
                reason = error.get("reason") or body.get("reason") or ""
            elif isinstance(error, str) and error:
                message = body.get("error_description") or body.get("message") or error
                reason = body.get("reason") or error
            else:
                message = body.get("message") or ""
                reason = body.get("reason") or ""
            if not str(message).strip():
                # Pixiv 对「作品不存在 / 对当前账号不可见」会回一个空 message 的错误体
                # （{"error":{"user_message":"","message":"","reason":"","user_message_lang":""}}），
                # 这时不要把整段 JSON 当人话，也不要留一句空白。
                message = f"上游返回 HTTP {resp.status_code}（上游未给出错误说明）"
        message = str(message).strip() or f"上游返回 HTTP {resp.status_code}"
        reason = str(reason or "").strip()
        if len(message) > 300:
            # 非 JSON 的错误响应（HTML 拦截页等）会把整篇文档塞进 message，截断以免刷屏。
            message = message[:300].rstrip() + "…"
        lowered = message.lower()
        if resp.status_code == 400 and "oauth" in lowered:
            if retried:
                raise PixivApiError(message, status=400, reason=reason)
            await self._oauth.refresh()
            return await self._request(
                method, path, params, data, retried=True, expect_json=expect_json
            )
        if resp.status_code == 429 or (
            resp.status_code == 400 and "limit" in lowered
        ):
            raise PixivRateLimitError(message, status=resp.status_code, reason=reason)
        raise PixivApiError(message, status=resp.status_code, reason=reason)

    async def get(self, path, params=None):
        return await self._request("GET", path, params=params)

    async def post_form(self, path, data):
        return await self._request("POST", path, data=data)

    async def next(self, next_url):
        return await self._request("GET", self._normalize_next_url(next_url))

    async def get_text(self, path, params=None):
        """取回**非 JSON**（HTML）响应，例如 /webview/v2/novel。"""
        return await self._request("GET", path, params=params, expect_json=False)

    async def illust_detail(self, illust_id):
        return await self.get(
            "/v1/illust/detail",
            {"illust_id": illust_id, "filter": "for_android"},
        )

    async def illust_ranking(self, mode="day", date=None, next_url=None):
        if next_url:
            return await self.next(next_url)
        return await self.get(
            "/v1/illust/ranking",
            {"mode": mode, "date": date, "filter": "for_android"},
        )

    async def illust_recommended(self, next_url=None):
        if next_url:
            return await self.next(next_url)
        return await self.get(
            "/v1/illust/recommended",
            {"filter": "for_ios", "include_ranking_label": True},
        )

    async def manga_recommended(self, next_url=None):
        if next_url:
            return await self.next(next_url)
        return await self.get(
            "/v1/manga/recommended",
            {"filter": "for_ios", "include_ranking_label": True},
        )

    async def illust_related(self, illust_id, next_url=None):
        if next_url:
            return await self.next(next_url)
        return await self.get(
            "/v2/illust/related",
            {"illust_id": illust_id, "filter": "for_android"},
        )

    async def illust_follow(self, restrict="public", next_url=None):
        if next_url:
            return await self.next(next_url)
        return await self.get("/v2/illust/follow", {"restrict": restrict})

    async def illust_series(self, illust_series_id):
        return await self.get(
            "/v1/illust/series", {"illust_series_id": illust_series_id}
        )

    async def illust_series_illust(self, illust_id):
        return await self.get("/v1/illust-series/illust", {"illust_id": illust_id})

    async def illust_bookmark_detail(self, illust_id):
        return await self.get(
            "/v2/illust/bookmark/detail", {"illust_id": illust_id}
        )

    async def illust_comments(self, illust_id, next_url=None):
        if next_url:
            return await self.next(next_url)
        return await self.get("/v3/illust/comments", {"illust_id": illust_id})

    async def illust_comment_replies(self, comment_id):
        return await self.get(
            "/v2/illust/comment/replies", {"comment_id": comment_id}
        )

    async def illust_comment_add(self, illust_id, comment, parent_comment_id=None):
        data = {"illust_id": illust_id, "comment": comment}
        if parent_comment_id is not None:
            data["parent_comment_id"] = parent_comment_id
        return await self.post_form("/v1/illust/comment/add", data)

    async def ugoira_metadata(self, illust_id):
        return await self.get("/v1/ugoira/metadata", {"illust_id": illust_id})

    async def illust_bookmark_add(self, illust_id, restrict="public", tags=None):
        data = {"illust_id": illust_id, "restrict": restrict}
        if tags:
            data["tags[]"] = " ".join(tag.strip() for tag in tags if tag.strip())
        return await self.post_form("/v2/illust/bookmark/add", data)

    async def illust_bookmark_delete(self, illust_id):
        return await self.post_form(
            "/v1/illust/bookmark/delete", {"illust_id": illust_id}
        )

    async def search_illust(
        self,
        word,
        sort=None,
        search_target=None,
        start_date=None,
        end_date=None,
        bookmark_num_min=None,
        bookmark_num_max=None,
        search_ai_type=None,
        next_url=None,
    ):
        if next_url:
            return await self.next(next_url)
        params = {
            "word": word,
            "sort": sort,
            "search_target": search_target,
            "start_date": start_date,
            "end_date": end_date,
            "bookmark_num_min": bookmark_num_min,
            "bookmark_num_max": bookmark_num_max,
            "search_ai_type": search_ai_type,
            "filter": "for_android",
            "merge_plain_keyword_results": True,
        }
        return await self.get(
            "/v1/search/illust", {k: v for k, v in params.items() if v is not None}
        )

    async def search_novel(
        self,
        word,
        sort=None,
        search_target=None,
        start_date=None,
        end_date=None,
        next_url=None,
    ):
        if next_url:
            return await self.next(next_url)
        params = {
            "word": word,
            "sort": sort,
            "search_target": search_target,
            "start_date": start_date,
            "end_date": end_date,
            "filter": "for_android",
            "merge_plain_keyword_results": True,
        }
        return await self.get(
            "/v1/search/novel", {k: v for k, v in params.items() if v is not None}
        )

    async def search_user(self, word, next_url=None):
        if next_url:
            return await self.next(next_url)
        return await self.get(
            "/v1/search/user", {"word": word, "filter": "for_android"}
        )

    async def search_autocomplete(self, word):
        return await self.get(
            "/v2/search/autocomplete",
            {"word": word, "merge_plain_keyword_results": True},
        )

    async def search_popular_preview(
        self, word, search_target="partial_match_for_tags", next_url=None
    ):
        if next_url:
            return await self.next(next_url)
        params = {
            "word": word,
            "search_target": search_target,
            "filter": "for_android",
            "include_translated_tag_results": True,
            "merge_plain_keyword_results": True,
        }
        return await self.get("/v1/search/popular-preview/illust", params)

    async def trending_tags_illust(self):
        return await self.get("/v1/trending-tags/illust", {"filter": "for_android"})

    async def trending_tags_novel(self):
        return await self.get("/v1/trending-tags/novel", {"filter": "for_android"})

    async def user_detail(self, user_id):
        return await self.get(
            "/v1/user/detail", {"user_id": user_id, "filter": "for_android"}
        )

    async def user_illusts(self, user_id, type_="illust", next_url=None):
        if next_url:
            return await self.next(next_url)
        return await self.get(
            "/v1/user/illusts",
            {"user_id": user_id, "type": type_, "filter": "for_android"},
        )

    async def user_novels(self, user_id, next_url=None):
        if next_url:
            return await self.next(next_url)
        return await self.get(
            "/v1/user/novels", {"user_id": user_id, "filter": "for_android"}
        )

    async def user_bookmarks_illust(
        self, user_id, restrict="public", tag=None, next_url=None
    ):
        if next_url:
            return await self.next(next_url)
        params = {"user_id": user_id, "restrict": restrict}
        if tag is not None:
            params["tag"] = tag
        return await self.get("/v1/user/bookmarks/illust", params)

    async def user_bookmark_tags_illust(self, user_id, restrict="public"):
        return await self.get(
            "/v1/user/bookmark-tags/illust",
            {"user_id": user_id, "restrict": restrict},
        )

    async def user_following(self, user_id, restrict="public", next_url=None):
        if next_url:
            return await self.next(next_url)
        return await self.get(
            "/v1/user/following",
            {"user_id": user_id, "restrict": restrict, "filter": "for_android"},
        )

    async def user_follower(self, user_id, restrict="public", next_url=None):
        if next_url:
            return await self.next(next_url)
        return await self.get(
            "/v1/user/follower",
            {"user_id": user_id, "restrict": restrict, "filter": "for_android"},
        )

    async def user_follow_detail(self, user_id):
        return await self.get("/v1/user/follow/detail", {"user_id": user_id})

    async def user_follow_add(self, user_id, restrict="public"):
        return await self.post_form(
            "/v1/user/follow/add", {"user_id": user_id, "restrict": restrict}
        )

    async def user_follow_delete(self, user_id):
        return await self.post_form("/v1/user/follow/delete", {"user_id": user_id})

    async def user_recommended(self, next_url=None):
        if next_url:
            return await self.next(next_url)
        return await self.get(
            "/v1/user/recommended", {"filter": "for_android"}
        )

    async def user_ai_show_settings(self):
        return await self.get("/v1/user/ai-show-settings")

    async def user_ai_show_settings_edit(self, show_ai):
        return await self.post_form(
            "/v1/user/ai-show-settings/edit", {"show_ai": show_ai}
        )

    async def novel_detail(self, novel_id):
        data = await self.get("/v2/novel/detail", {"novel_id": novel_id})
        # 加性字段：详情里若带着正文插图映射，就顺手归一化挂到 novel 上，
        # 这样阅读器进阅读页时（还没取正文）就能拿到插图地址。
        # 没有任何插图时不加，保证无图小说的响应与以前逐字节一致。
        novel = data.get("novel") if isinstance(data, dict) else None
        if isinstance(novel, dict) and not isinstance(novel.get("novel_images"), dict):
            images = extract_novel_images(novel)
            if images:
                novel["novel_images"] = images
        return data

    async def novel_text(self, novel_id):
        """取小说正文。

        /v1/novel/text 已被 Pixiv 移除（2024-02 起固定 404 "Specified end-point doesn't exist"），
        改用官方 iOS 客户端的 /webview/v2/novel：它返回一页 HTML，正文在 novel:{...}.text 里。
        对外返回结构保持原样（{"novel_text": "..."}），并**加性**补一个 novel_images
        （正文里 [uploadedimage:ID] 引用的插图 URL 映射）——旧调用方只读 novel_text 不受影响。

        上传插图的地址不一定跟着 webview 走（网页版是单独一次 /ajax/novel/<id> 请求拿
        textEmbeddedImages），所以正文里出现 [uploadedimage:] 而这里没解析出图时，依次再试：
        webview 页面里其它位置的内联映射 → /v2/novel/detail。都失败就返回空映射，
        让页面把「没有取到图片地址（id=…）」显示出来，而不是整篇正文取不到。
        """
        html = await self.get_text(
            "/webview/v2/novel", dict(NOVEL_VIEWER_PARAMS, id=novel_id)
        )
        novel = extract_novel_from_webview(html)
        if novel is None:
            raise PixivApiError(
                "取不到正文：/webview/v2/novel 的返回里没有 novel 对象（Pixiv 可能改版）",
                status=502,
            )
        text = novel.get("text") or ""
        images = extract_novel_images(novel)
        if not images and novel_text_has_embeds(text):
            images = extract_images_from_html(html)
        if not images and novel_text_has_embeds(text):
            images = await self._detail_images(novel_id)
        return {"novel_text": text, "novel_images": images}

    async def _detail_images(self, novel_id):
        """兜底：从 /v2/novel/detail 里取正文插图映射；失败就当没有（不影响正文）。"""
        try:
            data = await self.get("/v2/novel/detail", {"novel_id": novel_id})
        except PixivApiError:
            return {}
        novel = data.get("novel") if isinstance(data, dict) else None
        return extract_novel_images(novel)

    async def novel_ranking(self, mode="day", date=None, next_url=None):
        if next_url:
            return await self.next(next_url)
        return await self.get(
            "/v1/novel/ranking",
            {"mode": mode, "date": date, "filter": "for_android"},
        )

    async def novel_recommended(self, next_url=None):
        if next_url:
            return await self.next(next_url)
        return await self.get(
            "/v1/novel/recommended",
            {
                "include_privacy_policy": True,
                "filter": "for_android",
                "include_ranking_novels": True,
            },
        )

    async def novel_follow(self, restrict="public", next_url=None):
        if next_url:
            return await self.next(next_url)
        return await self.get("/v1/novel/follow", {"restrict": restrict})

    async def novel_series(self, series_id, next_url=None):
        if next_url:
            return await self.next(next_url)
        return await self.get("/v2/novel/series", {"series_id": series_id})

    async def novel_bookmark_add(self, novel_id, restrict="public"):
        return await self.post_form(
            "/v2/novel/bookmark/add", {"novel_id": novel_id, "restrict": restrict}
        )

    async def novel_bookmark_delete(self, novel_id):
        return await self.post_form(
            "/v1/novel/bookmark/delete", {"novel_id": novel_id}
        )

    async def novel_comments(self, novel_id, next_url=None):
        if next_url:
            return await self.next(next_url)
        return await self.get("/v3/novel/comments", {"novel_id": novel_id})

    async def novel_comment_replies(self, comment_id):
        return await self.get(
            "/v2/novel/comment/replies", {"comment_id": comment_id}
        )

    async def novel_comment_add(self, novel_id, comment, parent_comment_id=None):
        data = {"novel_id": novel_id, "comment": comment}
        if parent_comment_id is not None:
            data["parent_comment_id"] = parent_comment_id
        return await self.post_form("/v1/novel/comment/add", data)

    async def spotlight(self, category="all", next_url=None):
        if next_url:
            return await self.next(next_url)
        return await self.get(
            "/v1/spotlight/articles",
            {"category": category, "filter": "for_android"},
        )

    async def walkthrough_illusts(self):
        return await self.get("/v1/walkthrough/illusts")

    async def watchlist_novel(self):
        return await self.get("/v1/watchlist/novel")

    async def watchlist_novel_add(self, series_id):
        return await self.post_form(
            "/v1/watchlist/novel/add", {"series_id": series_id}
        )

    async def watchlist_novel_delete(self, series_id):
        return await self.post_form(
            "/v1/watchlist/novel/delete", {"series_id": series_id}
        )

    async def watchlist_manga(self):
        return await self.get("/v1/watchlist/manga")

    async def watchlist_manga_add(self, series_id):
        return await self.post_form(
            "/v1/watchlist/manga/add", {"series_id": series_id}
        )

    async def watchlist_manga_delete(self, series_id):
        return await self.post_form(
            "/v1/watchlist/manga/delete", {"series_id": series_id}
        )
