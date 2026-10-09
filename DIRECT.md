# 直连思路 —— 不依赖代理穿透链路封锁

这份文档记录本项目的核心做法：**在目标站点被 DNS 污染 / SNI 阻断 / IP 封锁 / 反爬识别时，怎么让程序直接拿到数据。** 它不是 API 手册（那是 README），而是「遇到连不上时该怎么想、怎么定位、怎么选手段」的方法论。

适用对象：任何被链路中间盒干扰的站点。项目里已经用同一套思路落地了四个站点 —— Pixiv（`pixiv_bridge/`）、iwara.tv（`iwara/`）、asmr.one（`asmr_one/`）与 E-Hentai（`ehviewer/`），本文最后一节对照它们说明「同方法、不同手段」。

---

## 0. 一句话结论

**别把「连不上」当成一个问题，它至少是四层里的一层出了问题。** 分层定位、逐层对症，比一上来就全局挂代理更省事，也更容易判断「到底是本地网络的问题，还是服务端的问题」。

代理永远是最后的退路：它一层解决全部，但也意味着你放弃了直连能力，且依赖一个外部进程。

---

## 1. 先把链路拆成四道关

任何一次 HTTPS 请求要依次过四关，**每一关的失败现象完全不同**，这就是定位的依据：

| 层 | 干什么 | 典型失败现象 | 本项目的对策 | 落地位置 |
| --- | --- | --- | --- | --- |
| ① DNS 解析 | 域名 → IP | `getaddrinfo failed`；解析到明显无关的网段（Facebook / Twitter 段） | 硬编码 IP 池；DoH（**必须验真**） | `pixiv/hoster.py`、`iwara/client.py` 的 `SEED_HOSTS` / `SEED_IPS` |
| ② TCP 可达 | 能不能建立连接 | `Timeout`、`TcpTestSucceeded=False` | IP 池逐个回退 + 先探测再固定 | `hoster.resolve()`、`iwara/client.py` 的 `pick_ip` |
| ③ TLS 握手 | 握手内容与形状 | 连接被 RST；403 + `cf-mitigated: challenge` | 无 SNI 握手 / ECH / 浏览器指纹 | `pixiv/sni_client.py`、`ech_client.py`、`iwara/client.py` 的 `impersonate` |
| ④ 应用层 | HTTP 语义 | 200 但 body 空；403；业务错误码 | 补齐请求头 / 签名 / 令牌 | `pixiv/api.py` 的 `_signed_headers`、`iwara/client.py` 的 `headers` |

**关键纪律：不要跨层乱猜。** 每一层都有独立的探针，先定位再动手，否则会浪费大量时间在错误的方向上（本项目在 iwara 上就先后走过「以为是 HTTP 库问题」和「换个 DoH 就好」两条死路）。

---

## 2. 诊断决策树

遇到「请求不通」，按顺序问下去：

```
请求失败
├─ 报 getaddrinfo failed，或解析结果是无关网段？
│     → ① DNS 层。跳第 3.1 节。
├─ 解析没问题，但 TCP connect 超时？
│     → ② IP 层（IP 被丢包/封锁，或解析到的 IP 本身是死的）。跳第 3.2 节。
├─ TCP 通，但 TLS 握手被 RST / 报 ConnectionResetError？
│     → ③ TLS 层，**封锁型**。跳 3.3 a/b。
├─ 握手完成，但返回 403 + `cf-mitigated: challenge`（或 HTML 挑战页）？
│     → ③ TLS 层，**反爬型**（不是封锁）。跳 3.3 c。
├─ HTTP 200 但内容是空的 / 缺字段？
│     → ④ 应用层（少了必需的请求头或签名）。跳 3.4。
└─ 以上都正常，只是某个接口 404/5xx？
      → 不是链路问题，是业务状态。别重试、别换 IP。
```

判据的写法：**「换网络就消失的，都是本地链路问题；换到哪都一样、且只影响特定资源的，才是服务端问题。」**

---

## 3. 四层各自的手段

### 3.1 DNS 层：不要相信解析

**手段一：硬编码 IP 池。** 最可靠。`pixiv/hoster.py` 里按域名分组维护官方源站 IP：

```python
HOST_IP_POOLS = {
    "app-api.pixiv.net": PIXIV_API_IPS,        # 首位 210.140.139.155
    "oauth.secure.pixiv.net": PIXIV_OAUTH_IPS,
    "i.pximg.net": PIXIV_IMAGE_IPS,
}
```

iwara 更进一步，用一张**实测可用的边缘 IP 表**（`iwara/client.py:63-79`），因为那里的系统 DNS 和 DoH **全都不可信**。

**手段二：DoH，但必须验真。** DoH（DNS over HTTPS）能绕开本地解析器的劫持，这是它常见的用途。但本项目在 iwara 上得到一个反直觉的结论：

> **在部分网络里，能连上的 DoH 同样会返回污染答案。** `doh.pub` / `dns.alidns.com` / `doh.360.cn` 对 `*.iwara.tv` 一致地回 Facebook / Twitter 网段，而对 `www.baidu.com` / `example.com` / `pypi.org` 却答得完全正确 —— 同一台机器、同一个解析器，**只对特定域名撒谎**。同时 `cloudflare-dns.com` / `dns.google` / `dns.quad9.net` 根本连不上（固定 IP 也超时）。

所以「换一个 DoH 就好了」是个陷阱。**验证方法**：拿同一个 DoH 同时查目标域名和一个已知正常的域名，如果前者返回明显无关的网段而后者正确，就是**域相关污染**，换解析器无解，直接上硬编码 IP 池。

（iwara 仍然保留了 DoH 作补充候选，但结果**排在种子 IP 之后**，见 `iwara/client.py:314-349` 的 `resolve_host`。污染答案排在后面不会造成伤害，排前面则必然先超时一次。）

### 3.2 IP 层：逐个回退，但注意两个陷阱

**基本做法**：按序尝试 IP 池，单 IP 失败自动换下一个（`sni_client.py:140-161` 的候选循环）。

**陷阱一：`CURLOPT_RESOLVE` 不是候选列表。** 这是本项目实测出来的、文档里通常不会提的坑：

> libcurl 对同一个 `host:port` **只认最后一条** `RESOLVE` 记录。传 `[BOGUS, GOOD]` 它会用 `GOOD`（200）；传 `[GOOD, BOGUS]` 它会用 `BOGUS`（超时），**而且连接超时后不会自动回退到另一个**。

所以想「把候选全塞进去让 libcurl 自己试」是行不通的。正确做法：**自己先探 TCP 可达性，再固定到单个 IP，失败时换人**（`iwara/client.py:366-389` 的 `pick_ip` + `iwara/client.py:398-413` 的 `session_for` + `iwara/client.py:431-463` 的 `_call_with_ip_retry`）。

**陷阱二：404 的语义按服务的不同而不同。**

- **CDN 边缘主机**的 `404 page not found` 意思是「**这个边缘不服务这份内容**」→ 应当视为该 IP 不可用，换个 IP 就有（`iwara/client.py:697-700` 就靠 `retry_statuses=(404,)` 做这件事）。
- **API 主机**的 404 是**真实业务状态**（视频不存在、清单为空）→ 换 IP 没有任何意义，重试只是浪费。

这两件事必须分开配置，不能统一重试。

**陷阱二之补：还有第三种「找不到」—— HTTP 200 配一句纯文本。** 状态码本身也可能是谎话。E-Hentai 对取不到的画廊和无效大图页都回 **200**，正文只有一句话：`Key missing, or incorrect key provided.`（39 字节）/ `No gallery specified.`（21 字节）/ `Gallery not found. …`（172 字节）/ `Invalid page.`（13 字节）。只看状态码就会把它当成功页面，解析出一具**空壳**（标题空、图片链空、`showing.total` 为 `null`）——调用方看到的是「这个画廊没有图」，比 404 难查得多。判据不能靠文案（文案会变），要靠**页面骨架**：正常详情页一定有 `#gn` / `#gdc` / `#gdd`，正常大图页一定有 `<img id="img" …>`；有骨架就是正常页，没有才去看那几句话。见 `ehviewer/parse.py` 的 `parse_gallery_error` / `parse_s_page_error`，两者都在 `ehviewer/client.py:gallery` / `image_page` 里转成 404。**这条对任何上游都值得先问一句：它报「不存在」时，到底换没换状态码。**

**陷阱二之补二：官方 JSON API 上还有第四种形态 —— 200 + 条目级 `error` 字段。** 换个协议不等于换了语义。E-Hentai 的 `api.php`（`gdata`）对失效 token / 不存在的 gid 同样回 **200**，只是把错误藏在**每一条目自己的 `error`** 里：`{"gmetadata":[{"gid":1,"error":"Key missing, or incorrect key provided."}]}`；gid 不存在时上游还会先把 gid 夹到 `2147483647`（`Gallery not found. …` 那段 172 字节文案）。HTML 那套「看骨架」在这里完全用不上——没有页面可看。批量接口里好坏条目**混在同一个 list** 逐条独立，所以这一层的判据要按调用方的语义分叉：批量（`gdata()`）忠实返回整批，让调用方自己逐条看 `error`；单条（`/api/eh/metadata`）必须兑现「要么给我、要么报错」，用 `ehviewer/parse.py` 的 `parse_gdata_error()` 判这一条并转成 404。注意兜底方向别搞反：认得的文案分类（`key` / `removed`），**认不得的 `error` 也一律按错误处理**（分类记 `error`），因为「上游换了句话」不等于「这条成功了」。

**陷阱二之补三：第五种形态最阴 —— 200 + 形状完全正确，但结果是「恒定的一批」。** 前三种至少还看得出「空壳」或「有条目级 error」；这一种连解析都是对的，只有**内容不对**。asmr.one 的 `/api/search/works` 就是**根本不存在的端点**：上游（Express）把路径里那段 `works` 当成了搜索关键词，于是 `/api/asmr/search?query=癒し`、`?query=DLsite`、`?query=随便什么` 一律回 HTTP 200、形状标准的 20 条、`totalCount` **恒为 117** —— 那 117 条其实是在「搜 works 这个词」。解析不出任何异常，自检也全绿，只有用户会发现「搜索不搜索」，而这类缺陷能悄悄活到上线之后。
判据只能是**横向对照**（单看一次响应永远看不出来）：① 换一个**必定查不到**的词（`zzzzqqxxnotexist`），如果结果和正常词一模一样，这个接口就没在按关键词过滤；② 同一关键词取两页，`totalCount` 若纹丝不动而 ids 在变，说明分页是真的、过滤是假的；③ 别只试参数名——真契约可能是**路径**参数（asmr 就是 `GET /api/search/{关键词}`，前端 bundle 里写的是 `/api/search/ + encodeURIComponent(kw)`），拿不到前端 JS 就永远猜不到。**教训：一个端点的存在与否，本身也是需要验证的假设；「200 + 看得懂」不等于「上游听懂了」。** 见 `asmr_one/client.py:search`。
 iwara 的每个视频源都挂在一个花名子域上（`firefly` / `zani` / `swan` / `bailu` / `yukong` / `welt` / `pela` …），但实测发现**同一份内容钉到池中任一可用 IP 都能取到**（少数转码源例外）。这带来两个好处：不必为每个花名维护解析，而且**某个花名整体不可达时可以整体绕过**。这也是「未登记的主机直接用整池去试」（`iwara/client.py:334-337`）的依据。

**陷阱三：前面五条都在问「上游怎么回错」，还有第六条 ——「上游错了之后，本端会不会把它改写得面目全非」。** 前五条的判据在上游；这一条的判据在**你自己的路由与错误处理**里，量起来也最简单：拿一个**必定失败**的路径打一次自己的接口，看返回的状态码和响应体。iwara 与 asmr 就栽在这上面 —— `/api/iwara/video/{id}` 与 `/api/asmr/work/{id}` 上游明明回 **404**，对外却是 **500 `Internal Server Error`**（`text/plain`），**状态码被改写、上游原文整段丢失**。

根因是两层叠加，缺一不可：① **路由层没兜** —— `iwara/routes/videos.py` 的 `/videos` `/search` `/video/{video_id}`、`asmr_one/routes/works.py` 的 `/works` `/search` `/work/{work_id}` 没有 `try/except`；② **站点没声明 `error_types`** —— `pixiv_bridge/errors.py` 的 `install_error_handlers()` 只给**传入的类型**（四个站点里只有 `pixiv/app.py` 传了）与 `httpx.HTTPError` 注册处理器，所以没有第二道网，异常一路逃逸到 FastAPI 默认处理器被改写成 500。

这条陷阱有两点值得单独记住：

- **同站内不一致比全站一致更危险。** 如果 `videos.py` 一条都没兜，读代码的人会警觉；正因为 `/related` `/resolve` `/stream` 都兜得很漂亮，那三条裸路由反而像是「本来就返回不了错误」。**样式统一会掩盖遗漏** —— 任何一条自带 `try/except` 的文件，都值得问一句「是不是每条都有」。
- **状态码是契约的一部分，丢它比丢形状严重。** 丢形状只影响人类的读法；丢状态码会让程序走错分支（404 变 500 就是「不存在」被说成「服务器炸了」）。

**顺带一条「数字也要横着看」：** 自检里报出的数字，先问它到底是不是它声称的那个东西。`iwara/envcheck.py` 曾把上游 `/videos` 顶层的 `count` 当总数报出去，而那个 `count` 实测**恒等于 `page*limit+n+1`**（20 组 `page × limit` 组合全部成立），于是自检煞有介事地报「共 2 个视频」。**同仓横向对照一眼就能看出**：同样是「报条数」，`asmr_one/envcheck.py` 分得清本页（`len(works)`）与真总数（`pagination.totalCount`），`ehviewer/envcheck.py` 只报条数 —— 四个站点里只有 iwara 混了。这类缺陷不会让任何测试变红，**一个看起来精确的数字，比一句「可达」更容易骗过自检。**

**陷阱四：前面六条都在问「200 里的错误长什么样」，还有第七条 ——「200 里甚至没有一个能解析的 body」。** 这是比「空壳」更彻底的一层：网关/Cloudflare 拦截页、上游改版、反代回静态页，都会给出 **HTTP 200 + `text/html`**。这时问题不再是「判据认不认得出错误」，而是**连解析都做不到**。

具体机制：`resp.json()` 在 body 不是 JSON 时抛 `json.JSONDecodeError`，而它是 `ValueError` 的子类 —— **不属于任何站点的错误类型**（`PixivApiError` / `IwaraError` / `AsmrError` / `EhError`），也躲过了 `pixiv_bridge/errors.py` 的 `install_error_handlers()`（它只认传入的 `error_types` 与 `httpx.HTTPError`）。于是异常一路逃到 FastAPI 默认处理器，对外变成 **裸 500 `text/plain Internal Server Error`** —— 形状没了，状态码也被改写成「服务器炸了」，而真相是「上游被拦了」。

四个站点里，**只有 ehviewer 一开始就兜住了**（`ehviewer/client.py:gdata` 把解析包在 `try/except Exception` 里，翻成 `EhError 502`），原因很偶然：它的 `gdata` 本来就是手工搭 JSON 请求的一段代码，顺手包了个 try。这也正是这条陷阱最容易漏的地方 —— **它和「有没有写 try」无关，和「写在哪一层」有关**：路由层兜异常（陷阱三修的那层）永远兜不住解析层抛出的 `ValueError`。

**修法与清单：** 一律在**解析点**把 `ValueError` 翻成站点异常，保留上游原文片段（口径同「选项 1」，保状态码、保语义）：

```python
try:
    return resp.json()
except ValueError as exc:
    raise IwaraError(
        f"GET {path} 返回的不是 JSON（可能被拦了或改版了）：{resp.text[:200]}",
        status=502, reason="bad_upstream_body",
    ) from exc
```

**按类别清点，别按现场清点** —— 这是本条最值钱的部分。发现缺陷时是探针在三条路由上炸，但把 `git grep -n "\.json()"` 全仓过一遍（排除 `tests/` `examples/`），**16 处里揪出 6 处**，其中 `iwara/client.py:632` 的 login 成功分支是探针**从没打到过**的：

| 位置 | 说明 |
| --- | --- |
| `pixiv/api.py` `get_json` 成功分支 | `return resp.json() if expect_json else resp.text`，两件事挤在一行，得先拆开 text 分支 |
| `pixiv/oauth.py` `_token_request` | 换 token 本来就是「上游一抽风就彻底登不上」的路径，最该报人话 |
| `iwara/client.py` `get_json` | 常规读取路径，影响面最大 |
| `iwara/client.py` `login` 成功分支 | **探针从未覆盖** —— 靠清点才发现 |
| `iwara/client.py` `refresh_access` | 同 `_token_request`，且它还要 `.get("accessToken")`，非 JSON 时连字典都拿不到 |
| `asmr_one/client.py` `get_json` | 同 iwara |

同一轮清点还确认了**已经正确的那些**（不必再改）：三个站点的 `ech.py` / `ech_client.py` 里的 DoH 解析（本来就包在 `try/except Exception` 里）、`pixiv/oauth.py:_extract_error`（有 `except ValueError: return resp.text`）、`pixiv/api.py:_handle_error`（有 `except ValueError: body = None`）、`iwara/client.py` 的 login 失败分支与 `resolve()` 盐循环（因为 `X-Version` 不对时 CDN 本来就回空 body，历史上被迫兜过）。**「哪里兜了」和「哪里该兜」是两件事，只有逐处过一遍才分得清。**

**为什么它比前六条更难自证：** 前六条的判据都建立在一个**能读懂的 body** 上（骨架、条目级 error、恒定结果），本条连 body 都读不了；而且它**只在被拦时发生** —— 平时开发环境直连上游一切正常，裸 500 只在真实对抗环境里出现。所以验证手法很朴素：**用 MockTransport 把所有上游请求都改成 `200 + text/html`，然后把自己所有路由打一遍**，看有没有 `text/plain Internal Server Error`。

**陷阱五：前面七条问的都是「上游给的能不能读懂」，还有第八条 ——「读懂了的那部分，**出口通不通**」。** 这一条最容易被忽视，因为**解析层是对的**：单测覆盖了、注释还专门写了「为什么不能写死这个键名」、字段在返回体里也照实回显。缺陷不在解析，在**从解析到对外契约之间那段没人负责的空白**。

E-Hentai 就栽在这上面。上游的列表页翻页有**两个**光标：向后是 `?next=<gid>`，向前是 `?prev=<gid>`。`ehviewer/parse.py` 的 `parse_list_nav()` 两个都解析、`_cursor_of(url, key)` 还为此专门加了 `key` 参数（注释记着「写死 `next` 会让 `prev` 永远解析成空串」）。**解析层完全正确**，返回体里 `next` / `prev` 都如实带出。

问题出在下一个环节：`ehviewer/client.py` 的 `list_galleries(cursor=…)` 只有一个 `cursor` 入口，`if cursor: params["next"] = cursor` —— **永远发 `?next=`**。于是 `prev` 变成了一个**没有出口的死字段**：解析出来、回显出去、调用方照着用 —— 而路由层 docstring 还明确承诺「返回里带 `next` / `prev` 两个光标，下一屏把它们回填到 `cursor` 上」。

**这个缺陷比前面几条都难发现，因为它不报错。** 实测把第 2 页的 `prev` 值回填到 `cursor`：上游 `?next=4242366` 照常回 200，拿到的是**第 2 页的下一页**（与第 2 页重叠 24/25），和真正想要的上一页（`?prev=4242366`，与第 1 页 25/25 全同）完全不是一页。**静默错页 —— 调用方永远看不出自己翻错方向了。** 对比一下就很清楚：`?prev=` 上游完全支持（这一条是实测确认的，别凭直觉以为「上游只认 next」），缺的只是本端发不出去。

判据只能靠**端到端闭环**，单测和自检都证明不了：① 先确认上游那个键**真的能用**（`?prev=<值>` 取回的页与来路页逐条比对，要 25/25 全同，不能只看状态码 200）；② 再确认本端**真发得出去**（打一次自己的接口，把上游收到的 query 键名打印出来，看到底是 `next` 还是 `prev`）；③ 最后做**对照**：把 `prev` 的值故意当 `next` 发一次，如果这一发与「正确回退页」结果不同、且与「当前页」高度重叠，就坐实了「回填能跑通、但跑错了方向」。

**教训：解析正确 ≠ 能力可用。** 一个被解析出来、写进文档、甚至专门修过解析键名的字段，仍然可能在整个代码库里**没有一条路径能把它用出去**。这条和陷阱三同属「本端把语义弄丢了」，但陷阱三是**把上游的错改写**，这条是**把上游的对浪费掉** —— 前者看错误路径，后者看正常路径，检查方法也不同：**每新增一个「从上游解析出来的可选项」时，回头确认它有一个对应的出口参数**（这里是 `direction`），否则就是给调用方挖了个安静的坑。

顺带一条：这类缺陷的修法要**保留回显**。修好之后返回体里同时给出 `direction`（本次实际用的方向）与 `prev_url`，让调用方能自证「我这次是往哪边翻的」，而不是又要靠猜。

**陷阱六：陷阱五问「出口通不通」，这一条问「通了之后**送出去的东西是不是真的**」。** 前面的陷阱都假定：只要字段名对得上、形状对得上，值就是可信的。实际上**上游完全可以在一个语义正确的字段名里塞一个语义错误的数**，而本端最省事的做法——原样透传——会把这个谎言一路传给调用方，还顺手写进手册。

iwara 的 `/videos` 就栽在这上面。它的顶层有个 `count` 字段，看着就是「总数」。实测（20 组 `page × limit` 组合全部成立）它的值恒等于 **`page*limit + len(results) + 1`** —— 翻到越界空页时退化成 `page*limit`。这既不是总数也不是本页条数，只是「请求过的条数 + 1」：**同一份内容，`?page=0` 说 `共 9 个`，`?page=100` 就说 `共 809 个`**。真总数只出现在另一个端点 `/search` 上（跨页恒定 `15106`）。

**本端当时的做法正是最坏的那一种：`{**page, "results": ...}` 原样透传**，于是 §25 修过的那个坑（自检把伪 count 当总数报出「共 2 个视频」）在对外出口上原封不动地留着 —— 而且 `README.md` 还把它当契约写进了表格，`iwara_web/index.html` 直接读它渲染「共 N 个」。**自检是对的、手册是错的、前端照做的**，三处口径各自为政。

同一份清扫还挖出两个同族问题：`/related` 本地裁剪了 `results` 却不重算 `count`/`limit`/`page`，于是 `?page=1` 会给出「`count` 说 12 条、`results` 里 0 条」这种自相矛盾的响应；`subscribed` 参数在客户端存在、在路由层**没有出口**（陷阱五的镜像：这次是「有能力没出口」）。

**判据：拿真实响应逐页比对同一个字段，看它是不是常数。** 三种对照：① **翻页比同一字段** —— 总数这类字段**跨页必须恒定**，若它随 `page` 单调增长，那就不是总数（这一条比读文档可靠得多）；② **换参数比同一字段** —— 固定 `page` 只改 `limit`，若它跟着 `limit` 一起变，说明它只是「请求过的条数」；③ **找反例端点** —— 同一个上游往往另有一个端点给出真值（这里是 `/search`），拿它当参照物。**「字段名像总数」不是判据，「跨页恒定」才是。**

**修法：在出口处把语义定死，宁可说「未知」也不编一个数。** 本端统一成 `count`=**本页**条数（恒等于 `len(results)`）、`total`=总数，没有就写 `null` —— 这与 `asmr_one` 那边早就是正确口径（`count`=本页、`total`=分页里的 `totalCount`）完全一致，**横向对照一下就能看出谁对谁错**（见第 5 节第 7 条）。裁剪类接口还要**重算它自己那些描述性字段**（`limit`/`page`/`count` 都要跟着裁剪后的实际结果走），否则同一个响应体自相矛盾。

**教训：字段名是上游的命名习惯，不是契约；原样透传等于把上游的实现细节写进自己的文档。** 每透传一个数值字段前，先问一句「它是常数吗、它描述的是哪一层（请求量/本页量/总量）」。凡是把这个数直接展示给用户的地方（手册表格、前端「共 N 个」），都要跟着改成正确来源。


### 3.3 TLS 层：分「封锁」和「反爬」两种

这是最容易混淆的一层。**封锁是链路中间盒看你的握手内容**；**反爬是服务端看你的握手形状**。对策完全不同。

#### a) 无 SNI 握手 —— 对付「SNI 明文触发阻断」

TLS ClientHello 里不明文携带域名，链路中间盒就无从下手。`pixiv/sni_client.py` 用 `asyncio.open_connection(ip, port, ssl=ctx, server_hostname=None)` 实现，配套三个细节：

- 证书不校验（`ctx.check_hostname = False; ctx.verify_mode = ssl.CERT_NONE`）—— 直连源站 IP 时没有可校验的名字；
- **必须手工补 `Host` 头**：TLS 里没有 SNI 后，Host 是服务端识别域名的唯一依据（`sni_client.py:135-138`）；
- HTTP/1.1 报文必须自己拼（`_build_request`），包括那个容易漏掉的头部结束空行 —— 少了它服务端会一直等头部结束，请求永远没有响应（`sni_client.py:71-73` 有注释记着这个坑）。

**失效条件**：服务端本身要求 SNI（回 403/421）；或链路是 IP 层封锁（TCP 都建不起来，无 SNI 也没用）。本项目的 README 里有一条对照表专门帮用户区分这两种。

#### b) ECH —— 对付「SNI 无论怎么发都被阻断」

ECH（Encrypted Client Hello，RFC 9180 的 TLS 扩展）把**真正的 SNI 加密**后放进 ClientHello，中间盒只能看到外层名字 `cloudflare-ech.com`。`pixiv/ech_client.py` 的实现要点（全部来自实测约束）：

1. **Cloudflare 的 ECH 公钥与站点无关**（按 anycast 集群下发），所以可以拿 `cloudflare-ech.com` 的配置去连**任意** Cloudflare 前置的域名 —— 这是整套方案成立的前提。
2. **需要 OpenSSL 4.0+**：ECH 是 OpenSSL 4.0.0 才加入的，3.x 全系（含 3.5/3.6）都没有。Python 自带的 `ssl` 模块没有暴露 ECH，所以用 `ctypes` 直接调 libssl。项目**自带** `openssl-libs/`，按 `平台-架构` 分目录（`win32-amd64` 4.0.2 / `android-arm64` 4.0.3），查找顺序是「用户指定 → 项目自带 → 系统安装」，逐个候选尝试。
3. **只提供 TLS 1.3**：同时提供 TLS 1.2 时 Cloudflare 会以 `illegal_parameter` 中止握手，所以要把 min/max 版本都钉在 TLS1.3。
4. **fd 必须保持阻塞**：`socket.create_connection` 的 timeout 会把 fd 设成非阻塞，导致 `SSL_connect` 返回 `SSL_ERROR_WANT_READ`。
5. **外层 `cloudflare-ech.com`、内层真实域名**，证书按真实域名校验（`SSL_set1_host`）。
6. ECH 配置会轮换（缓存 30 分钟），DoH 端点按 `cloudflare-dns.com` → `dns.alidns.com` → `223.5.5.5` 顺序尝试取 `type=65` 记录。

**失效条件（本机实测）**：部分运营商**无差别阻断 ECH 扩展本身** —— 只要 ClientHello 里带一个*结构合法*的 ECH 扩展，就在约 0.2 秒内被 RST（`SSL_get_error=5`）；而同一 IP、同一外层 SNI、**不带** ECH 扩展的握手能正常走完。特征是「换 IP、换外层 SNI、换配置都无效」，因为中间盒只看扩展在不在。**客户端无法绕过这种阻断**，只能换网络或改用代理。日志里要能把「配置过期」（`SSL_get_error=1`，会自动刷新重试）和「被阻断」（`SSL_get_error=5`）分开，否则用户无从判断。

#### c) 浏览器 TLS 指纹 —— 对付反爬（不是封锁）

这一层与前两者**正交**：前两者改的是握手**内容**，这一层改的是握手的**形状**（JA3/JA4 指纹）。

站点在 Cloudflare 后面时，普通的 Python TLS 指纹会被直接判定为机器人，返回 `403` + `cf-mitigated: challenge` + `<title>Just a moment...</title>`，**这与 SNI 是否明文无关**。iwara 就是这种情况：只修 DNS 仍被挑战，只冒充 TLS 也解析不到，**两个手段必须同时用**（这也是 `iwara/client.py` 顶部 docstring 记录的第一件事）。

落地方案是 `curl_cffi`（libcurl + 浏览器指纹）：

```python
session = curl_requests.AsyncSession(curl_options={
    CurlOpt.RESOLVE: [f"{host}:443:{ip}"],   # DNS 层：固定到实测可用的 IP
    CurlOpt.CONNECTTIMEOUT: int(timeout),    # 必须是 int，传 float 会报 "an integer is required"
})
resp = await session.request("GET", url, headers=..., impersonate="chrome")
```

实测可用的 `impersonate` 配置：`chrome`、`chrome131`、`chrome136`、`safari184`、`edge101`；**`impersonate=None` 必然拿到 403 挑战页**。

两个 API 坑：

- `Session.request()` **不接受** per-call 的 `curl_options=` 参数（`TypeError`），必须设在 session 对象上；
- `session.get(..., stream=True)` 才真正惰性；不加 `stream=True` 会把整个文件读进内存（对视频直链尤其致命）。

### 3.4 应用层：补齐请求头与语义

链路全通了，还可能因为「请求长得不像客户端」而失败：

- **必需的伪装头**：`User-Agent`、`App-OS` / `App-Version`（Pixiv 移动端），或 `origin` / `referer` / `x-site`（iwara，缺 `x-site` 会回 `301 {"message":"errors.differentSite"}`）；
- **签名头**：Pixiv 的 `X-Client-Time` + `X-Client-Hash`（`md5(时间 + 固定盐)`）；iwara 的 `X-Version`（`sha1("{file_uuid}_{expires}_{salt}")`，盐会**换版本** —— 本项目同时保留新旧两版盐并依次尝试）；
- **令牌刷新**：注意错误码语义。iwara 用 **403 `errors.forbidden`** 而不是 401 表示 token 过期，所以刷新逻辑要同时挂在这两个状态码上；
- **区域参数**：某些内容要显式请求才拿得到（iwara 的 R18 内容匿名可看，但必须显式传 `rating=ecchi`）。

---

## 4. 代理放在什么位置

代理是「一层解决全部问题」的退路，因此优先级**高于**所有直连手段 —— 配了代理就不该再叠加 ECH / 无 SNI。

```python
if settings.proxy:      # 代理已解决连通性，直接用
elif settings.ech:      # ECH 优先于无 SNI：加密 SNI 且照常校验证书
elif settings.bypass_sni:
else:                   # 普通路径
```

（`pixiv/api.py:298-338` 就是这个分支结构。）

**什么时候必须用代理**：① 目标是 IP 层封锁（TCP 都建不起来）；② 链路无差别阻断 ECH 扩展；③ 服务端策略升级到直连手段都跟不上。

---

## 5. 验证方法论（比手段更重要）

本项目的直连能力是靠试错积累的，几条经验值得单独记：

1. **每层一个独立探针，不要用端到端请求去猜。** 「端到端失败」几乎不含信息量；「TCP 通但握手 RST」才直接指向对策。
2. **明确「什么算成功」。** 例：ECH 自检里**任意 HTTP 状态码都算通道成立**（400 表示 Pixiv 在应答未认证的请求，说明加密 SNI 的通道已经建起来了）。判据定错会把成功当失败。
3. **别信静态源码报告，实测优先。** 对 iwara 的静态分析报告在至少三处与实测不符：`thumbnail` 字段是**整数帧号**而不是路径；`subscribed=true` 匿名下**被静默忽略**（三种取值实测返回完全相同的 8/20 条，配 `sort=trending` 也不变 —— 它只在登录态有效）；R18 内容**不需要登录**。凡是能实测的都实测。
4. **交叉验证才能升级为「方法」。** 同一个坑在两个结构完全不同的站点上都复现，才有资格写进文档当通例。
5. **区分本地链路与服务端。** 见第 2 节的判据，并把它写进用户可见的排查表。
6. **自测代码本身也会骗人。** 本项目出现过「断言 `content[:11] == b"\x00\x00\x00 ftypisom"`（12 字节字面量）导致恒为假」和「假响应返回裸字符串导致 `AttributeError`」两类自伤 —— 断言写错比被测代码出错更难发现。
7. **横向对照是唯一能证明「参数/判据真的生效」的手法。** 单次响应永远证明不了「它听懂了我的话」：上游可以回 HTTP 200、形状完全正确，内容却与参数毫无关系（见 3.2 陷阱二之补三）。三种最省力的对照，按代价从低到高：① 换一个**必定查不到**的输入（结果不变 ⇒ 根本没在过滤）；② 同一输入翻页（总数纹丝不动而条目在变 ⇒ 分页真、过滤假）；③ 拿一个「常量本身」当输入反查（输入 `works` 得到那个恒定值 ⇒ 铁证）。**同一份对照也适用于自己写的代码**：把「同类代码的另一份」拿来并排看（哪个站点报本页条数、哪个报总数；哪条路由兜了异常、哪条没兜），遗漏会自己浮出来 —— 见 3.2 陷阱三。

---

## 6. 工程取舍

直连能力是**可选增强**，不能反过来成为负担：

- **站点隔离，单向依赖。** `pixiv_bridge/` 是纯框架，永远不认识 `pixiv/`、`iwara/`、`asmr_one/`、`ehviewer/`、`novel/`、`iwara_web/`；站点与扩展只能 `import pixiv_bridge.*`（各站点之间也互不依赖）。`scripts/check_boundary.py` 用 AST + 子进程探路由来强制这条边界，`scripts/sites.py` 是构建侧的登记表。
- **挑选站点也只写一次。** 根目录的启动就是**项目导航**：`pixiv_bridge/__main__.py` 发现「有 `run.py` 的目录＝接口项目、只有 `extension.py` 的目录＝扩展」，用 `ast` 从站点自己的文件里读导航用的字面量（标题、简介、`Site.nav_order`），**不 import 也不执行站点代码**，选完才 `runpy.run_path` 交给它的 `run.py`。`start.bat` / `start.sh` 因此只是转发器（在 sh/bat 里各抄一套发现＋排序＋询问必然与 Python 侧漂移，而且 shell 读不到 `nav_order`）。
- **重依赖不进根清单。** `curl_cffi` 是编译包，只有 iwara、asmr.one 与 E-Hentai 需要，所以分别写在 `iwara/requirements.txt`、`asmr_one/requirements.txt`、`ehviewer/requirements.txt` 里，由 `pixiv_bridge/deps.py` 的通用 `ensure_packages(names, requirements)` 按需安装，站点在自己 `run.py` 里先 `bootstrap(partial(ensure_packages, ...))` 再用它。只跑 Pixiv 的用户完全不必碰它。
- **可选能力要有回退，且失败要安静。** 登录态失效不该让整站不可用 —— iwara 的 `_prepare_auth`（`iwara/client.py:465-485`）换不出 access token 时只把原因记进 `auth_error` 并退回匿名，因为「iwara 的绝大部分内容匿名就能看，为了一个失效的 token 让整站不可用是本末倒置」。
- **能力自检要暴露给用户。** `/api/iwara/auth` 匿名时刻意不去打 `/user`（那返回 401，看着像报错），所以它永远 200；只有真拿到 access token 才问一次 `/user` 填 `username_hint` —— 顺带一条教训：token 载荷里**没有**用户名字段（只有 `exp`/`id`/`role`），早期直接读载荷的 `username` 于是恒为 `None`，而当时的测试替身自己往载荷里塞了 `username`，正好把这个 bug 盖住了。

---

## 7. 同一套方法、四个不同站点

| | Pixiv 站点（`pixiv/`） | iwara.tv 站点（`iwara/`） | asmr.one 站点（`asmr_one/`） | E-Hentai 站点（`ehviewer/`） |
| --- | --- | --- | --- | --- |
| 阻塞形态 | SNI 阻断 + DNS 污染 | DNS 污染 + Cloudflare 反爬挑战 | Cloudflare 边缘 IP 级阻断（同一批边缘对 Google 也返回 404，本机整段不通） | 上游自己解析出的那几个边缘 IP 被 IP 级阻断（整段不通，连 TCP 都建不起来） |
| ① DNS | 默认解析可用；直连模式下走内置 IP 池 | **解析完全不可信**，DoH 也返回污染答案 → 种子 IP 表 | 同样不可信 → 种子 IP 表 + DoH 只作补充候选（排种子之后） | 不可信，且自己解析出的 IP 恰好是被封的那些 → 内置 16 个别的 Cloudflare anycast IP 种子池 |
| ② IP | 官方源站 IP 池，逐 IP 回退 | 种子池 + 可达性探测 + 单 IP 固定 + 404 换 IP | 种子池 + 可达性探测 + 单 IP 固定 + 换 IP 重试（媒体主机换 IP 有效） | 种子池 + 裸 TCP 探可达（`probe_edge` 只建连不发 TLS，所以不被 SNI 判据拦）+ **轮转游标**换边缘重试 |
| ③ TLS | 无 SNI 握手 **或** ECH（OpenSSL 4.0+） | `curl_cffi` `impersonate="chrome"`，可选再叠 ECH（`IWARA_BRIDGE_ECH=1`） | `curl_cffi` `impersonate="chrome"` + **ECH 默认开**（`ASMR_BRIDGE_ECH` 不写就是开） | **强制 HTTP/3**（`EH_BRIDGE_HTTP3` 默认开）：HTTPS/1.1 与 HTTP/2 会因 SNI 不符被拒，只有 QUIC（UDP:443）不受这个判据限制 |
| ④ 应用 | `X-Client-Time` / `X-Client-Hash` 签名 + OAuth 令牌 | 浏览器头 + `X-Version` 签名（双盐）+ 可选登录 | 只要浏览器头：无签名、无令牌（站点只暴露匿名端点） | 只要浏览器头：无签名、无令牌（同样只暴露匿名端点）；元数据走官方 `api.php` 的 `gdata`，那里的「取不到」是 200 + 条目级 `error`，单条接口转 404 |
| 传输层实现 | 自写 `sni_client.py` / `ech_client.py`（复用同一套报文构造） | `curl_cffi.AsyncSession`（因为必须拿浏览器指纹） | `curl_cffi.AsyncSession`（同上） | `curl_cffi.AsyncSession` + 会话级 `CurlOpt.RESOLVE` 把域名钉到选中的边缘 IP（一条 RESOLVE 只认一个 IP，所以一 host 一 IP 一个会话） |
| ECH 走法 | ctypes 调 OpenSSL 4.0+ 的 `SSL_set1_ech_config_list`（要自带 `openssl-libs/`） | `curl_cffi` 的 `CurlOpt.ECH`（libcurl 的 `CURLOPT_ECH`），**不需要动态库** | 同 iwara（`CurlOpt.ECH`，不需要动态库），且**默认就开** | 不走 ECH —— 这条链路的解法是 HTTP/3，用 `CurlOpt.HTTP_VERSION = CurlHttpVersion.V3` 打开 |
| 额外依赖 | 无（ctypes 驱动自带 libssl） | `curl_cffi`（编译包，单独 requirements） | `curl_cffi`（编译包，单独的 `asmr_one/requirements.txt`） | `curl_cffi`（编译包，单独的 `ehviewer/requirements.txt`；换边缘 IP 与 HTTP/3 都由它的 libcurl 提供） |
| 登录 | 必需（OAuth） | 可选（公开内容匿名可看） | **不需要**（纯匿名，连 `login_check` 都没有） | **不需要**（纯匿名，同样没有 `login_check`；exhentai 里站内容会直接返回 401，本项目不碰它） |

**结论**：面对新站点时，先按第 2 节的决策树定位，再到第 3 节挑对应层的手段。四个站点的差异全部集中在**具体手段的选择**上 —— 方法论是一样的。iwara 与 asmr.one 两者尤其像：同一副「种子 IP 池 + 浏览器指纹 + 换 IP 重试」的骨架，差别只在 ECH 的默认开关与登录态；E-Hentai 与它们共用同一副骨架，但把「指纹 + ECH」换成了「浏览器头都不伪造 + 强制 HTTP/3」，理由在第三节：它的墙是按 IP 与 SNI 判的，不是按指纹判的。要注意的是**站点之间不许互相 import**（`check_boundary.py` 会拦），所以 `asmr_one/ech.py` 是 `iwara/ech.py` 的**自足副本**，而不是引用。

---

## 8. 遇到新站点时的实操清单

1. **分层探测**：`getaddrinfo` → TCP 443 连通性 → TLS 握手（带/不带 SNI）→ 裸 HTTP 请求。每步都记录结果，不要跳步。
2. **判断 DNS 是否可信**：拿同一个解析器同时查目标域名和一个已知正常的域名。对得上就是解析器坏，对不上就是域相关污染 → 上硬编码 IP 池。
3. **确认目标的 CDN 拓扑**：是否在 Cloudflare 后面（`cf-mitigated` 头、`server: cloudflare`）、边缘是否按内容路由（同一内容换 IP 试试）。
4. **确认封锁类型**：握手被 RST = 封锁（试无 SNI / ECH）；403 挑战页 = 反爬（试浏览器指纹）；两者都有就都要治。
5. **确认 IP 语义**：CDN 边缘的 404 要换 IP，API 的 404 不要。
6. **把可用 IP / 盐 / 指纹配置记成常量并注释来源**，附上实测日期 —— 这些东西会过期，注释是下次维护的入口。
7. **为每一层写一个离线单测**（用假传输、假响应），这样网络变了以后能区分「代码坏了」和「链路变了」。
8. **兜异常要逐条检查，别按印象。** 路由层把站点异常映射成 `HTTPException` 这件事，容易只做一半（见 3.2 陷阱三）：拿一个**必定失败**的路径把每条路由都打一遍，确认状态码与上游原文都还在。**文件里有一条兜了，不等于每条都兜了。**
9. **把所有上游请求改成 `200 + text/html` 再打一遍，看有没有裸 500。** 第 8 条查的是「错误有没有被改写」，这条查的是「连 body 都解析不了时会发生什么」（见 3.2 陷阱四）：网关拦截页、上游改版、反代回静态页都会给出 `200` + 不可解析的 body，而 `resp.json()` 抛的 `JSONDecodeError` 不属于任何站点错误类型，会直接逃成裸 500。**别按现场清点，按类别清点**：`git grep -n "\.json()"` 把全仓过一遍，逐处确认它在 `try` 里 —— 探针打过的那几处只是恰好撞上的，真正漏掉的那处（`iwara/client.py` 的 login 成功分支）从来没人打过。
10. **把上游解析出来的每个「可选项」都反向走一遍，看它有没有出口。** 第 8、9 条查错误路径，这条查**正常路径**（见 3.2 陷阱五）：解析层正确解析出的字段（如上游的 `?prev=` 光标），可能在客户端层根本没有参数能把它发出去，而文档却承诺「回填后即可翻页」—— 回填能跑通、不报错，只是**静默拿到错的一页**。两个动作：`grep` 一遍返回体里带回上游语义的字段，逐个问「调用方拿它做什么、有没有对应的入参」；再拿一个真实响应，把该字段**故意用错**发一次，对比结果是否与「正确用法」不同。**解析正确不等于能力可用。**
11. **每透传一个数值字段，先确认它是「常数」还是「本页量」。** 第 8–10 条查错误、查出口，这条查**送出去的值得不可信**（见 3.2 陷阱六）：上游可以把一个语义错误的数塞进一个名字很像总数的字段（iwara `/videos` 的 `count` 实测恒等于 `page*limit+len+1`）。三个动作：① **翻页比同一字段** —— 总数跨页必须恒定，跟着页码涨的就不是总数；② **固定页码只改 `limit`** —— 跟着变的只是「请求过的条数」；③ **找出真值端点**（同一上游往往另有一个字段/端点给真值，如 `/search` 的 `count`）当参照物。**「名字像总数」不是判据，「跨页恒定」才是。** 出口统一成 `count`=本页、`total`=总数（没有就写 `null`），并回头检查**所有展示这个数的地方**（手册表格、前端「共 N 个」）是否也跟着改 —— 裁剪类接口还要把 `limit`/`page`/`count` 一起重算，别让一个响应体自相矛盾。

---

## 附：本项目直连相关的环境变量

| 变量 | 作用 |
| --- | --- |
| `PIXIV_BRIDGE_BYPASS_SNI=1` | 无 SNI 直连模式（复刻 PixEz） |
| `PIXIV_BRIDGE_ECH=1` | ECH 模式（与上面只需开一个，ECH 优先） |
| `PIXIV_BRIDGE_ECH_CONFIG` | 手动指定 ECH 配置；留空则自动 DoH 获取 |
| `PIXIV_BRIDGE_ECH_IPS` | ECH 入口 IP 池；留空用内置 Cloudflare anycast 池 |
| `PIXIV_BRIDGE_API_IPS` / `_OAUTH_IPS` / `_IMAGE_IPS` | 覆盖各域名的 IP 池（逗号分隔） |
| `PIXIV_BRIDGE_OPENSSL_DIR` | 指定带 ECH 的 OpenSSL 4.0+ 目录；留空优先用项目自带的 `openssl-libs/` |
| `PIXIV_PROXY` | 代理通道，**优先级高于以上所有直连手段** |
| `IWARA_BRIDGE_ECH=1` | iwara 的 ECH（走 `curl_cffi` 的 libcurl，不需要 OpenSSL 动态库；只对 Cloudflare 后的 `api` / `www` 生效） |
| `IWARA_BRIDGE_ECH_CONFIG` | 手动指定 iwara 的 ECH 配置；留空走 DoH（阿里 → `223.5.5.5`） |
| `IWARA_BRIDGE_ECH_IPS` | 给 ECH 主机额外追加的入口 IP（排在内置种子之后） |
| `IWARA_REFRESH_TOKEN` | iwara 可选登录态（也可跑 `python iwara/get_token.py` 存到 `data/iwara_tokens.json`） |
| `ASMR_BRIDGE_ECH` | asmr.one 的 ECH（走 `curl_cffi` 的 libcurl，不需要 OpenSSL 动态库）。**与 iwara 相反：不写就是开**，要关必须显式写 `ASMR_BRIDGE_ECH=0` |
| `ASMR_BRIDGE_ECH_CONFIG` | 手动指定 asmr 的 ECH 配置；留空走 DoH（阿里 → `223.5.5.5`），失败用内置默认值 |
| `ASMR_BRIDGE_ECH_IPS` | 给 asmr 的 ECH 主机额外追加的入口 IP（排在内置种子之后） |
| `EH_BRIDGE_HTTP3` | ehviewer（E-Hentai）的 HTTP/3。**默认开**（要关必须显式写 `0`）：它的墙按 IP 与 SNI 判，HTTP/1.1 与 HTTP/2 都会被拒，只有 QUIC 走得通 |
| `EH_BRIDGE_EDGE_IPS` | 覆盖内置的 16 个 Cloudflare anycast 候选 IP（逗号分隔）。内置表里**故意排除了** e-hentai 自己解析出的那两个被 IP 级阻断的地址 |
| `EH_BRIDGE_EDGE_ATTEMPTS` | 一次请求最多换几个边缘 IP 重试（默认 `5`） |
| `EH_BRIDGE_IMAGE_ATTEMPTS` | 图片代转时最多重新解析几次 `/s/` 页拿新 H@H 节点（默认 `4`） |

> 起手先跑 `python pixiv/run.py --check`：自检会逐项报告 python / deps / 项目文件 / 端口 / 网络 / ECH 的状态，把「四道关」的结论直接摊开（`--check` 不监听端口，所以 `port` 只警告不中止）。加了站点之后自检也要跟着长：iwara 入口跑 `python iwara/run.py --check`，会在这张表后面追加 `iwara 依赖`（真 import `curl_cffi`）、`iwara 网络`（真发一次 `GET /videos?limit=1`）、`iwara 登录` 三项，开了 `IWARA_BRIDGE_ECH=1` 再追加 `iwara ECH`（真发一次 `/cdn-cgi/trace` 看回显是不是 `sni=encrypted`）；asmr.one 入口跑 `python asmr_one/run.py --check`，追加 `asmr 依赖`、`asmr 网络`（真发一次 `GET /api/works?page=1`，要整条「借道 IP 池 + impersonate + ECH」链路通才算过）、`asmr 搜索`（拿必定查不到的 `zzzzqqxxnotexist` 与已知有结果的 `癒し` 横向对照——守的是「参数到底有没有生效」：本站曾经把不存在的 `/api/search/works` 当搜索端点，任何查询都恒回同一批 117 条，HTTP 200、形状标准、`asmr 网络` 照样全绿）以及 `asmr ECH`（**默认就有**，因为 ECH 默认开；写成 `ASMR_BRIDGE_ECH=0` 关掉后才是 3 项），它没有登录自检；ehviewer 入口跑 `python ehviewer/run.py --check`，追加 `ehviewer 依赖`（真 import `curl_cffi`，并检查有没有 `CurlHttpVersion`——没有就说明这个 curl_cffi 缺 HTTP/3 支持）、`ehviewer 边缘`（并发探 16 个候选 anycast IP，**只建 TCP 不发 TLS**，所以不会被 SNI 判据误杀）、`ehviewer 站点`（真发一次首页 `list_galleries()`，这是整条链路唯一的判据；`EH_BRIDGE_HTTP3` 关掉时会额外提示「取到页面属于侥幸」）、`ehviewer 详情`（挑头两条真取一次详情，确认「页面骨架」判据还认得上游页面——上游对取不到的画廊回 HTTP 200 + 一句纯文本，判据失效会让真实画廊整批 404）、`ehviewer 榜单`（真发一次 `GET /popular`，确认这个独立入口还在）、`ehviewer 大图`（列表 → 详情图片页链接 → 真解一次 `/s/` 页，确认图片直链判据仍命中）——同样没有登录自检。——**新站点照这个套路做**：把「本层的判据」写成一条 `CheckResult`，挂在 `Site` 契约的 `checks`（以及 `login_check` / `network_hint`）上（见 `pixiv_bridge/site.py`），框架的 `pixiv_bridge.cli.main(site=SITE)` 就会把它与通用检查打在同一张表里。另外别忘了在构建侧登记：`scripts/sites.py` 的 `SITES` 加一条，`check_boundary.py` / `make_share_packages.py` 才认这个新站点。
