"""HTTP Range：把「能断点续传」这件事在一个地方说清楚。

本仓库有八个会往客户端吐字节的接口，它们分两类：

- **透传**：上游（CDN / 图床）自己就支持 Range，我们要做的只是把入站的
  ``Range`` / ``If-Range`` 转给上游，再把上游的 ``206`` 与续传相关响应头原样转回。
- **本地切片**：上游客户端是缓冲式的（一次性读完整个图片），手上已经捏着完整字节，
  只能自己按入站 ``Range`` 切 200 / 206 / 416 三种应答。

于是这里收三件事，各站点不再各写一份：

- :func:`forwarded_headers` —— 从入站请求里挑出该转给上游的续传相关头；
- :func:`response_headers` —— 从上游响应里挑出该转回客户端的续传相关头；
- :func:`parse_range` / :func:`bytes_response` —— 有完整字节时按 ``Range`` 拼应答。

刻意的取舍：

- **只支持单区间**。``bytes=0-1,5-6`` 这类多区间请求按 RFC 9110 允许服务端忽略，
  这里忽略并回整包 200，不做 ``multipart/byteranges``——浏览器续传只用单区间，
  为它实现一套 multipart 边界是没人付账的复杂度。
- **不生成校验器**。:func:`bytes_response` 不发 ``ETag`` / ``Last-Modified``，
  所以也就无法判断 ``If-Range`` 里的校验器是否还有效：收到 ``If-Range`` 一律回整包，
  这正是 RFC 9110 在服务端无法判定时必须走的路。
"""

from starlette.responses import Response

#: 应该转给上游的续传相关请求头（``If-Range`` 不带的话，上游会忽略 Range 回整包）。
REQUEST_HEADERS = ("range", "if-range")

#: 应该从上游响应转回客户端的续传相关头。``content-length`` 也在这里——206 的
#: Content-Length 是那一段的长度，少了它下载器算不出进度。
RESPONSE_HEADERS = (
    "content-range",
    "accept-ranges",
    "content-length",
    "etag",
    "last-modified",
)


class RangeNotSatisfiable(Exception):
    """请求的区间落在实体之外：调用方应回 416 + ``Content-Range: bytes */<size>``。"""

    def __init__(self, size):
        self.size = size
        super().__init__(f"区间超出 {size} 字节")


def forwarded_headers(request, extra=()):
    """挑出入站请求里该转给上游的续传头。

    ``extra`` 给调用方补自己那份额外的头（例如 ehviewer 的 ``if-none-match`` /
    ``if-modified-since``：它靠这两个头把上游的 304 原样转回浏览器）。
    返回的键保持小写，直接就是 httpx 接受的形状。
    """
    headers = {}
    for name in (*REQUEST_HEADERS, *extra):
        value = request.headers.get(name)
        if value:
            headers[name] = value
    return headers


def response_headers(headers):
    """从上游响应里挑出续传相关的头，原样转回客户端。

    ``headers`` 可以是 ``httpx.Headers``（大小写不敏感），也可以是普通 dict。
    """
    picked = {}
    for name in RESPONSE_HEADERS:
        value = headers.get(name)
        if value:
            picked[name] = value
    return picked


def parse_range(value, size):
    """把单区间 ``Range`` 解析成闭区间 ``(start, end)``。

    - ``bytes=0-99`` → ``(0, 99)``
    - ``bytes=100-`` → ``(100, size - 1)``
    - ``bytes=-100`` → 最后 100 字节
    - 末尾越界会被截到 ``size - 1``（RFC 允许）

    返回 ``None`` 表示「这个头我不打算理」：没写、单位不是 ``bytes``、语法不可解析、
    或写了多区间——调用方回整包 200 就好。
    区间起点越界、或 ``bytes=-0`` 抛 :class:`RangeNotSatisfiable`，调用方回 416。
    """
    if not value or size <= 0:
        return None
    unit, _, spec = value.partition("=")
    if unit.strip().lower() != "bytes":
        return None
    spec = spec.strip()
    if not spec or "," in spec:  # 多区间：刻意忽略
        return None
    first, _, last = spec.partition("-")
    first, last = first.strip(), last.strip()
    try:
        if not first:
            suffix = int(last)
            if suffix <= 0:
                raise RangeNotSatisfiable(size)
            return max(0, size - suffix), size - 1
        start = int(first)
        if start < 0:
            return None
        if start >= size:
            raise RangeNotSatisfiable(size)
        if not last:
            return start, size - 1
        end = int(last)
        if end < start:
            return None
        return start, min(end, size - 1)
    except ValueError:
        return None


def bytes_response(request, content, media_type, cache_control="public, max-age=86400"):
    """手上已有完整字节时，按入站 ``Range`` 回 200 / 206 / 416。

    不带 Range 的请求行为与以前完全一致（200 + 整包），只多一个 ``Accept-Ranges``
    ——那个头就是让浏览器知道「这个地址可以续传」的信号。
    """
    headers = {"Cache-Control": cache_control, "Accept-Ranges": "bytes"}
    if request.headers.get("if-range"):
        return Response(content, media_type=media_type, headers=headers)
    try:
        span = parse_range(request.headers.get("range"), len(content))
    except RangeNotSatisfiable as exc:
        headers["Content-Range"] = f"bytes */{exc.size}"
        return Response(status_code=416, headers=headers)
    if span is None:
        return Response(content, media_type=media_type, headers=headers)
    start, end = span
    headers["Content-Range"] = f"bytes {start}-{end}/{len(content)}"
    return Response(
        content[start : end + 1],
        status_code=206,
        media_type=media_type,
        headers=headers,
    )
