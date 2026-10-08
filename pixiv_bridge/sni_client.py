"""绕过 SNI 的 TLS 客户端：手工握手后直接发 HTTP 请求，Host 头由调用方补。
"""

import asyncio
import json
import ssl
from urllib.parse import urlencode, urlparse

import httpx

_open_connection = asyncio.open_connection


class SniConnectError(RuntimeError):
    pass


def tls_context():
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    return ctx


def _parse_headers(lines):
    headers = httpx.Headers()
    for line in lines:
        name, _, value = line.partition(":")
        headers[name.strip().lower()] = value.strip()
    return headers


def _encode_pairs(mapping):
    """按 httpx 的语义编码查询/表单参数：布尔转小写 true/false，None 直接省略。

    urllib 默认把 True 编成 "True"、把 None 编成 "None"，而 Pixiv 对这两个字面量
    都拒绝（400 且 message 为空）：`merge_plain_keyword_results=True` 会让
    /v1/search/illust、/v1/novel/recommended 直接失败，`date=None` 会让
    /v1/illust/ranking、/v1/novel/ranking 失败。httpx 那条路径本来就没这个问题，
    补齐后直连/ECH 与 httpx 三种传输的行为才一致。
    """
    pairs = []
    for key, value in mapping.items():
        items = value if isinstance(value, (list, tuple)) else (value,)
        for item in items:
            if item is None:
                continue
            if isinstance(item, bool):
                item = "true" if item else "false"
            pairs.append((key, item))
    return pairs


def _build_request(method, path, params, data, json_body, headers):
    query = urlencode(_encode_pairs(params)) if params else ""
    if query:
        path = f"{path}?{query}" if "?" not in path else f"{path}&{query}"
    body = b""
    if json_body is not None:
        body = json.dumps(json_body).encode("utf-8")
        headers = {**headers, "Content-Type": "application/json"}
    elif data is not None:
        if isinstance(data, (bytes, str)):
            body = data.encode("utf-8") if isinstance(data, str) else data
        else:
            body = urlencode(_encode_pairs(data)).encode("utf-8")
        headers = {**headers, "Content-Type": "application/x-www-form-urlencoded"}
    headers = dict(headers)
    headers.setdefault("Connection", "close")
    if body and "content-length" not in {k.lower(): v for k, v in headers.items()}:
        headers["Content-Length"] = str(len(body))
    head = f"{method} {path} HTTP/1.1\r\n"
    head += "".join(f"{key}: {value}\r\n" for key, value in headers.items())
    # 头部必须以空行结束（RFC 9112 §2.1）：少这个 CRLF，服务端会一直等头部结束，
    # 请求永远不会有响应（POST 的正文还会被当成头部继续读）。
    head += "\r\n"
    return head.encode("latin-1") + body


async def _read_response(reader, timeout):
    status_line = (await asyncio.wait_for(reader.readline(), timeout)).decode(
        "latin-1"
    ).strip()
    parts = status_line.split(" ", 2)
    if len(parts) < 2 or not parts[1].isdigit():
        raise ValueError(f"非法状态行: {status_line!r}")
    status = int(parts[1])
    lines = []
    while True:
        line = (await asyncio.wait_for(reader.readline(), timeout)).decode(
            "latin-1"
        ).strip()
        if not line:
            break
        lines.append(line)
    resp_headers = _parse_headers(lines)
    if resp_headers.get("transfer-encoding", "").lower() == "chunked":
        body = b""
        while True:
            size_line = (await asyncio.wait_for(reader.readline(), timeout)).decode(
                "latin-1"
            ).strip()
            size = int(size_line.split(";", 1)[0], 16)
            if size == 0:
                await asyncio.wait_for(reader.readline(), timeout)
                break
            body += await asyncio.wait_for(reader.readexactly(size), timeout)
            await asyncio.wait_for(reader.readline(), timeout)
    else:
        body = await asyncio.wait_for(reader.read(), timeout)
    return httpx.Response(status, headers=resp_headers, content=body)


async def request(
    method,
    url,
    *,
    params=None,
    data=None,
    json=None,
    headers=None,
    timeout=30.0,
    ips=None,
):
    parsed = urlparse(url)
    hostname = parsed.hostname
    port = parsed.port or 443
    path = parsed.path or "/"
    if parsed.query:
        # 翻页时传进来的是一条完整 URL（/api/next 与各列表接口的 next_url 分支），
        # 查询串落在 parsed.query 里：只取 parsed.path 会把它整条丢掉，offset 一没，
        # 上游就把第一页原样再发一遍——表现为「第二页和第一页一模一样」。
        # httpx 那条传输不走这里（它自己解析 URL），所以只有直连/ECH 会踩到。
        path = f"{path}?{parsed.query}"
    candidates = ips or (hostname,)
    ctx = tls_context()
    headers = dict(headers or {})
    # HTTP/1.1 要求每个请求都带 Host（RFC 7230 §5.4），缺失时服务端必须返回 400。
    # 无 SNI 直连时 TLS 握手里没有 SNI，Host 是服务端识别域名的唯一依据，必须兜底补上。
    if not any(key.lower() == "host" for key in headers):
        headers["Host"] = hostname
    errors = []
    for ip in candidates:
        try:
            reader, writer = await asyncio.wait_for(
                _open_connection(ip, port, ssl=ctx, server_hostname=None),
                timeout=timeout,
            )
        except (OSError, ssl.SSLError, asyncio.TimeoutError) as exc:
            errors.append(f"{ip}: {exc.__class__.__name__}")
            continue
        try:
            raw = _build_request(method, path, params, data, json, headers)
            writer.write(raw)
            await asyncio.wait_for(writer.drain(), timeout)
            return await _read_response(reader, timeout)
        except (OSError, ssl.SSLError, asyncio.TimeoutError, ValueError) as exc:
            errors.append(f"{ip}: {exc.__class__.__name__}")
        finally:
            writer.close()
            try:
                await writer.wait_closed()
            except (OSError, ssl.SSLError):
                pass
    raise SniConnectError(f"无 SNI 直连失败: {'; '.join(errors)}")
