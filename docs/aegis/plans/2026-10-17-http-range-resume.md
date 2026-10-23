# 断点续传（HTTP Range）全覆盖

## Goal

让本仓库**每一个会往客户端吐字节的接口**都支持 HTTP Range 语义，浏览器/下载器因此可以
断点续传、可以拖进度条、可以只取图片头部探测尺寸：

- 上游本身支持 Range 的通道 → **原样透传**（入站 `Range` / `If-Range` 转给上游，上游的
  `206` + `Content-Range` / `Accept-Ranges` / `ETag` / `Last-Modified` / `Content-Length`
  原样转回）。
- 只能整包取回的通道（图片客户端是缓冲式的）→ **本地切片**，按入站 `Range` 自己切出
  `200` / `206` / `416` 三种应答。

收敛目标是「一件事只有一个 owner」：挑选转发头、解析区间、拼区间应答这三件逻辑目前在四个
站点里各写一份（或不写），本次收进框架模块 `site_bridge/ranges.py`。

## Aegis Visibility

这次改动同时动三样东西：一个新框架模块（`site_bridge/ranges.py`，新的公共 owner）、一条对外
HTTP 契约（`/img` 从「永远 200」变成「200/206/304/416」，新增 `Accept-Ranges` /
`Content-Range`）、以及四个站点模块的响应构造。契约是浏览器直接消费的，回归面横跨框架与三个站点，
所以值得一份 durable plan；不做计划容易漏掉「哪些站点该切、哪些该留」。

## Architecture

框架 `site_bridge/` 不认识任何站点；站点各有 `run.py` + `app.py`，只能单向 import 框架
（`scripts/check_boundary.py` 强制）。`site_bridge/ranges.py` 是**纯 HTTP 工具**：不出现任何
站点名、不 import fastapi 之外的东西（用 `starlette.responses.Response`，FastAPI 已重导出）。

字节出口一共 8 个，现状矩阵：

| 接口 | 上游是否支持 Range | 现状 | 本次 |
| --- | --- | --- | --- |
| `GET /img`、`/img/{path}`（pixiv） | 是 | 固定 200、无 `Content-Length` | 透传 |
| `GET /api/iwara/stream` | 是 | 已透传 | 改用共享 helper |
| `GET /api/iwara/image` | 否（缓冲式 `fetch_image`） | 固定 200 | 本地切片 |
| `GET /api/iwara/thumbnail/{id}` | 否 | 固定 200 | 本地切片 |
| `GET /api/asmr/stream` | 是 | 已透传 | 改用共享 helper |
| `GET /api/asmr/cover/{id}` | 否（`fetch_cover`） | 固定 200 | 本地切片 |
| `GET /api/eh/proxy` | 是 | 已透传（4 个头） | 改用共享 helper |
| `GET /api/library/file` | 是（Starlette `FileResponse` 内建） | 已支持 | 只补测试与文档 |

## Tech Stack

Python 3.13 + FastAPI 0.136.1 / **Starlette 1.7.0**（`.venv` 实测）、httpx。前端 `novel/web/index.html`
是无构建单文件，本次不动前端。

## Baseline / Authority Refs

- 用户指令（m00001）："加入断点续传"；并裁定"能够支持的接口都要支持"。→ 范围 = 上表全部 8 个出口。
- `README.md:714` / `:760` / `:784` 已有「透传 Range」的现成文案句式，新增文档沿用同一句式。
- `asmr_one/client.py:569 probe_range` 是仓库内既有的「Range 探测」先例。
- vanila 站点模板：`ehviewer/routes/galleries.py:28-37`（`_FORWARD_HEADERS` + `_pick_forward_headers`）。

**Requirement Ready Check**：范围（8 个出口）、场景（浏览器拖动/续传、下载器重试、探测头）、验收
证据（206 + `Content-Range` + 416 边界 + 全站测试绿）均已明确，无需回退。

## Compatibility Boundary

- **只增不减**：不带 `Range` 的请求行为完全不变（仍是 200 + 全量字节 + `Cache-Control`）。
- `/img` 新增行为：`206`、`416`、以及上游 `304` 原样转回（条件请求本来就被上游支持，只是以前
  被当成错误）。
- 多区间 `Range`（`bytes=0-1,5-6`）**不支持**：按 RFC 9110 服务端可以选择忽略，本实现忽略并回
  整包 200（不做 `multipart/byteranges`）。与 Starlette `FileResponse` 的多区间能力不对齐，但
  浏览器实际只用单区间续传，刻意留白并在 README 写明。
- 不带 `Range` 的 200 响应新增 `Accept-Ranges: bytes` 头——这是让浏览器知道"可以续传"的信号，
  也是本需求的核心可见效果。

## TDD Route

- mode: `off`（本次会话 Aegis TDD mode = off，用户未要求 strict）
- decision: `skipped`
- authority: 无 strict 授权
- test posture: 先改实现，随后为每一处改动补回归用例（task 6），并用现有 929 项测试做基线对比
- verification: `python -m unittest discover -s tests` 全绿；新增用例覆盖 206 / 416 / 200 三条路径

## Change Necessity

纯文档不够：`/img` 当前把上游 `206`/`304` 当错误抛 500/304 之外的状态码，且不回 `Content-Length`，
浏览器**无法**续传；三处整包图片路由返回 `iter([content])` 且无 `Accept-Ranges`，同样无法续传。
最小代码边界 = 一个框架工具模块 + 四处调用的替换，不引入新路由、不改 URL、不改数据结构。

## Ripple Signal Triage

命中「public API / contract」与「duplicate owner」两项：

- canonical owner（本次确立）：`site_bridge/ranges.py` 独占「该转哪些续传头」与「区间应答怎么拼」。
- 下游消费者：四个站点的字节路由 + 浏览器 + 可能的第三方下载器。
- 重复 owner 的退休：`ehviewer/routes/galleries.py:28-37` 的 `_FORWARD_HEADERS` /
  `_pick_forward_headers`，以及 `iwara/routes/videos.py:210-214`、`asmr_one/routes/works.py:136-140`
  里各一份的 `for name in ("range", "if-range")` 循环，全部改调 `ranges.forwarded_headers()`。
- 无 schema / 持久化 / 迁移影响；无 fallback 分支新增。

## Plan Pressure Test（compact）

- owner fit：新的纯 HTTP owner 放在框架层，与「框架不认识站点」的既有边界一致 ✅
- 更高层路径：是否该统一成「所有字节出口都走一个 `stream_bytes(...)`」？否——上游能自己 Range 的
  三条已经工作，重写只增加回归面；本次只收敛「头挑选」这一层。
- verification 范围：单测覆盖框架逻辑 + 四处路由；全站 929 项做回归基线。
- 任务可执行性：每条任务精确到文件与断言，无占位符。

## Tasks

### Task 1 — 新增 `site_bridge/ranges.py`

新文件。公开 API：

```python
REQUEST_HEADERS = ("range", "if-range")
RESPONSE_HEADERS = ("content-range", "accept-ranges", "content-length", "etag", "last-modified")

class RangeNotSatisfiable(Exception):   # .size
def forwarded_headers(request, extra=()) -> dict
def response_headers(headers) -> dict
def parse_range(value, size) -> tuple[int, int] | None
def bytes_response(request, content, media_type, cache_control="public, max-age=86400") -> Response
```

要点（写进 docstring，不留隐性知识）：

- `parse_range` 是**闭区间**；`bytes=-N` 后缀区间取最后 N 字节；`bytes=N-` 取到末尾；
  单位不是 `bytes`、语法不可解析、多区间 → 返回 `None`（忽略 Range，回整包）；
  起点 `>= size` 或 `bytes=-0` → 抛 `RangeNotSatisfiable(size)`。
- `bytes_response` 收到 `If-Range` 一律回整包 200（本函数不发 `ETag`/`Last-Modified`，没有稳定
  校验器就无法判断"这半截还能不能用"，RFC 9110 要求退回整包）。
- `bytes_response` 的 206 带 `Content-Range: bytes {start}-{end}/{size}`，416 带
  `Content-Range: bytes */{size}`、空 body、`Content-Type` 不写。

验收：`python -c "import site_bridge.ranges"` 通过；`python scripts/check_boundary.py` 通过
（模块内无站点名）。

### Task 2 — pixiv `/img` 透传 Range

文件：`pixiv/image_proxy.py`、`pixiv/routes/misc.py`。

- `ImageProxy.stream(self, path)` → `stream(self, path, extra_headers=None)`：`extra_headers`
  合进原有的 `Referer`/`User-Agent` 里，三档传输（ECH / bypass SNI / httpx）都接受 headers，无需
  改传输层。
- `_stream_image`：`proxy.stream(safe_path, ranges.forwarded_headers(request))`；上游状态
  `(200, 206, 304, 416)` 放行，其余仍走 `HTTPException`；响应头用
  `ranges.response_headers(upstream.headers)` 再叠加 `Cache-Control: public, max-age=86400`；
  状态码原样回传。
- 416 分支：先 `await upstream.aclose()`，回 `Response(status_code=416)`。
- 304 分支：同样 `aclose()` 后回状态码 304、空 body（把上游条件命中的语义原样转回，而不是像
  以前那样被当成错误）。

验收：`/img` 带 `Range: bytes=0-3` 回 206 且 body 是前 4 字节；不带 Range 回 200 且带
`Accept-Ranges: bytes`；已存在 `tests/test_bridge.py:379-391` 仍绿。

### Task 3 — iwara `/image` 与 `/thumbnail` 本地切片

文件：`iwara/routes/videos.py`。

- `:233-247 image`、`:249-277 thumbnail` 的 `StreamingResponse(iter([content]), ...)` 换成
  `return ranges.bytes_response(request, content, content_type)`（`Cache-Control` 默认值保持
  `public, max-age=86400`）。
- `:210-214 stream` 的 `pass_through` 手写循环换成 `ranges.forwarded_headers(request)`。
- `:221-222` 的 `if status >= 400` 保留（上游错误仍要变成本站 4xx/5xx）。

验收：`/api/iwara/image` 带 Range 回 206 + `Content-Range`；`bytes=999999-` 回 416；
`/api/iwara/stream` 的既有两条 Range 测试仍绿。

### Task 4 — asmr `/cover` 本地切片

文件：`asmr_one/routes/works.py`。

- `:103-122 cover` 换成 `ranges.bytes_response(request, content, content_type)`。
- `:136-140 stream` 的手写循环换成 `ranges.forwarded_headers(request)`。

验收：`/api/asmr/cover/main` 带 Range 回 206；既有 `test_stream_forwards_range_and_marks_private`
与 `test_stream_forwards_if_range_too` 仍绿。

### Task 5 — ehviewer `/proxy` 改用共享 helper

文件：`ehviewer/routes/galleries.py`。

- 删掉 `_FORWARD_HEADERS` 与 `_pick_forward_headers`（`:27-37`），`/proxy` 改调
  `ranges.forwarded_headers(request, extra=("if-none-match", "if-modified-since"))`。
- 保留 `:227-231` 的上游 4xx/5xx 先消费 body 再抛异常的写法，以及 `:233` 的 `cache-control`
  默认值。304 空 body 小疣本次不动，在 README 明确记录为已知限制。

验收：`tests/test_ehviewer.py:1737 test_proxy_forwards_the_range_header` 与
`:1101-1119` 的 client 层 206 断言仍绿。

### Task 6 — 补回归用例

- `tests/test_bridge.py`：新增 `RangeHelperTestCase`（`parse_range` / `bytes_response` 的
  `200/206/416/后缀区间/多区间忽略/If-Range 忽略` 纯单测）；`/img` 的 206 与 416 端到端（
  `_mock_handler` 扩一条「上游按 Range 回 206」的分支，或直接对 mock request 断言转发头）；
  `LibraryRouteTestCase.test_library_preview_and_file` 追加 `/api/library/file` 带 Range → 206
  + `Content-Range` 的断言（锁住 Starlette 行为，防将来换响应类时静默丢失）。
- `tests/test_iwara.py`：`/api/iwara/image` Range → 206、超界 → 416。
- `tests/test_asmr_one.py`：`/api/asmr/cover/main` Range → 206。

验收：新用例全绿，且总数从 929 增长；**同步更新 `README.md:390` 与 `:894` 的计数文案**。

### Task 7 — 文档

`README.md`：

- `:373-375` 模块表插入 `ranges.py  HTTP Range：续传头挑选 / 单区间解析 / 200·206·416 应答`。
- `:680` `/img` 行补「透传 Range（206/Content-Range），支持断点续传」。
- `:700` `/api/library/file` 行补「支持 Range（Starlette FileResponse 内建，可续传）」。
- `:715-716` iwara thumbnail / image、`:759` asmr cover 各补「支持 Range（本地切片）」。
- 新增一小节「断点续传」放在接口表之后：说明哪些是透传、哪些是本地切片、多区间不支持、
  eh 304 空 body 是已知限制。
- `:78` 附近若写死了「`/img` 图片流全部 200」这类描述，一并改。

验收：`git diff README.md` 只含上述行；文档里的断言与实现一致。

## Verification

1. `python -m unittest discover -s tests` —— 全绿，计数与 README 一致。
2. `python scripts/check_boundary.py` —— 依赖方向/出入口/注册表/路由隔离四项通过。
3. `python scripts/make_share_packages.py --check` —— 打包清单不需要为新模块改动（`dirs = [FRAMEWORK_PACKAGE]`
   整目录拷贝），此命令验证没有意外差异。
4. 手工冒烟（可选，需要真实网络）：`curl -r 0-3 http://127.0.0.1:PORT/img/<path>` 应得
   `206` + `Content-Range: bytes 0-3/...`。

## Risks

| 风险 | 缓解 |
| --- | --- |
| 上游 `Content-Length` 与实际 body 不一致（如 `Content-Encoding: gzip` 已被 httpx 解码） | `response_headers` 只挑续传相关头，与 iwara/asmr/eh 现有做法一致；图片是二进制不会被 gzip。风险与既有一致，不新增。 |
| ECH / bypass-SNI 分支是整包缓冲，上游 206 的 body 就是那一段 | `_BufferedImage` 原样携带上游 status/headers/body，无需切；已由 Task 2 的用例锁住。 |
| 浏览器对 `Accept-Ranges` 的反应变化（开始发 Range 而不是全量） | 这是本需求要的效果；`Cache-Control` 与 `ETag` 透传让缓存仍能命中。 |
| 多区间请求被忽略后回整包，可能浪费带宽 | RFC 允许；浏览器续传只用单区间。README 明写。 |

## Retirement Track

- **删**：`ehviewer/routes/galleries.py:27-37` 的 `_FORWARD_HEADERS` / `_pick_forward_headers`
  （owner 移交 `ranges.forwarded_headers`）。
- **删**：`iwara/routes/videos.py:210-214`、`asmr_one/routes/works.py:136-140` 两份手写
  `for name in ("range", "if-range")` 循环。
- **不删、明确保留**：`iwara` / `asmr` / `eh` 三条 stream 路由的「上游状态码原样回传 +
  `status >= 400` 判定」逻辑——那是站点对自己的错误语义负责，不属于 Range 共性。
- **不删、明确保留**：`pixiv/routes/misc.py` 的 `proxy.resolve` 校验（安全边界，与 Range 无关）。

## Execution Route

- route: `inline`（四处改动顺序依赖同一模块，且工作树必须在同一提交里保持一致）
- reason: 任务间共享 `site_bridge/ranges.py`，切给 subagent 会重复读同一批基线文件
- fallback: 无
- `User confirmation required: no`

## Implementation Record（2026-10-17，已完成）

### 落地形态

- **新增** `site_bridge/ranges.py`（144 行）：`REQUEST_HEADERS = ("range", "if-range")`、
  `RESPONSE_HEADERS = ("content-range", "accept-ranges", "content-length", "etag", "last-modified")`、
  `class RangeNotSatisfiable(Exception)`（带 `.size`）、`forwarded_headers(request, extra=())`、
  `response_headers(headers)`、`parse_range(value, size)`、`bytes_response(request, content, media_type, cache_control="public, max-age=86400")`。
- **pixiv**：`pixiv/image_proxy.py:57` `async def stream(self, path, extra_headers=None)`，
  新增的两行把 `extra_headers` 合进 `headers`（三档传输都会带）；`pixiv/routes/misc.py`
  加 `_PASSTHROUGH_STATUSES = (200, 206, 304, 416)`，`_stream_image` 改为
  `proxy.stream(safe_path, ranges.forwarded_headers(request))` + `status not in _PASSTHROUGH_STATUSES`
  → 抛错；304/416 走 `Response(status_code=status, headers=headers)`（`aclose()` 后无实体）；
  200/206 走 `StreamingResponse(upstream.aiter_raw(), status_code=status, media_type=..., headers=headers)`。
- **iwara**：`videos.py` `/image` 与 `/thumbnail/{video_id}` 改用 `ranges.bytes_response(...)`；
  `/stream` 的手写循环换成 `ranges.forwarded_headers(request)`。
- **asmr_one**：`works.py` `/cover/{work_id}` 改用 `ranges.bytes_response(...)`；
  `/stream` 同上换成 `ranges.forwarded_headers(request)`。
- **ehviewer**：`galleries.py` 删掉 `_FORWARD_HEADERS` / `_pick_forward_headers`，改为
  `_EXTRA_FORWARD_HEADERS = ("if-none-match", "if-modified-since")` +
  `ranges.forwarded_headers(request, extra=_EXTRA_FORWARD_HEADERS)`。
- **novel `FileResponse` 未改**（Starlette 1.7.0 内建 Range），只补测试锁行为。

### 与计划的偏差

- 计划里 `bytes_response` 用 `StreamingResponse(iter([content]))`；实现改为 `Response(content)`——
  字节已在内存，`Response` 会自动给 `Content-Length`，比流式包装更直白。
- 计划未提 `/img` 的 `304`；实现把 304 也纳入透传状态（条件命中时上游只给头），避免把「没变」当成错误。
- 计划里「本地切片不发校验器，带 `If-Range` 一律回整包」在实现中落实为
  `bytes_response` 开头的 `if request.headers.get("if-range"): return Response(content, ...)`。

### 测试

新增 17 个用例（946 = 原 929 + 17）：

- `tests/test_bridge.py`：`RangeHelperTestCase`（9 项：转发头挑选 / 响应头挑选 / `parse_range` 六种形态 /
  多区间与非法单位被忽略 / 越界 416 / `bytes_response` 的 200·206·416·If-Range）；
  `ImageRangeRouteTestCase`（3 项：整包 200、206 透传、416 透传，用新增的 `_range_handler` 假上游）；
  `ImageProxyTransportTestCase.test_stream_forwards_extra_headers`（1 项：bypass SNI 分支把 `range` 送去上游）；
  `LibraryRouteTestCase.test_library_preview_and_file` 追加 Range → 206 断言。
- `tests/test_iwara.py`：`test_image_slices_a_range_locally`、`test_image_rejects_an_out_of_bounds_range`。
- `tests/test_asmr_one.py`：`test_cover_slices_a_range_locally`、`test_cover_rejects_an_out_of_bounds_range`。

全部通过：`python -m unittest discover -s tests` → `Ran 946 tests ... OK (skipped=1)`；
`python scripts/check_boundary.py` → 边界通过；`python scripts/make_share_packages.py --check` → 通过。

### 文档

`README.md`：模块树加 `ranges.py` 行（:375）；`/img` 行（:681）、`/api/library/file` 行（:701）、
iwara thumbnail/image 行、asmr cover 行各补 Range 说明；新增「## 断点续传（HTTP Range）」一节
（八个字节接口的两类做法表 + 三条约定）；错误处理表补 `206` / `416` 两行；
测试计数三处（`tests/` 目录说明、开发自检、iwara 用例段）改为 946 / 352 / 213 / 147 / 189 / 45。

### 刻意未做（记录在案）

- **小说 EPUB 导出的前端断点续跑**：`novel/web/index.html` 的 `downloadSeries` / `downloadMergeGroup`
  是「逐章抓取 → 内存里打包 → `downloadBlob` 一次性落地」，中断后已抓章节全丢。若要续跑需要把
  抓取结果落盘（服务端任务或 IndexedDB），属另一条需求线，不在「接口断点续传」范围内。
- **eh `/proxy` 的 304 空 body 小疣**：上游 304 时会带一个空 body 的 `StreamingResponse`，
  语义上没错（304 本来无实体），未改动。

