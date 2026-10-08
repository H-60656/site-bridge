# 直连思路 —— 不依赖代理穿透链路封锁

这份文档记录本项目的核心做法：**在目标站点被 DNS 污染 / SNI 阻断 / IP 封锁 / 反爬识别时，怎么让程序直接拿到数据。** 它不是 API 手册（那是 README），而是「遇到连不上时该怎么想、怎么定位、怎么选手段」的方法论。

适用对象：任何被链路中间盒干扰的站点。项目里已经用同一套思路落地了两个站点 —— Pixiv（`pixiv_bridge/`）和 iwara.tv（`iwara/`），本文最后一节对照两者说明「同方法、不同手段」。

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

**边缘按内容路由、花名子域互通。** iwara 的每个视频源都挂在一个花名子域上（`firefly` / `zani` / `swan` / `bailu` / `yukong` / `welt` / `pela` …），但实测发现**同一份内容钉到池中任一可用 IP 都能取到**（少数转码源例外）。这带来两个好处：不必为每个花名维护解析，而且**某个花名整体不可达时可以整体绕过**。这也是「未登记的主机直接用整池去试」（`iwara/client.py:334-337`）的依据。

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
3. **别信静态源码报告，实测优先。** 对 iwara 的静态分析报告在至少三处与实测不符：`thumbnail` 字段是**整数帧号**而不是路径；`subscribed=true` 匿名也能用；R18 内容**不需要登录**。凡是能实测的都实测。
4. **交叉验证才能升级为「方法」。** 同一个坑在两个结构完全不同的站点上都复现，才有资格写进文档当通例。
5. **区分本地链路与服务端。** 见第 2 节的判据，并把它写进用户可见的排查表。
6. **自测代码本身也会骗人。** 本项目出现过「断言 `content[:11] == b"\x00\x00\x00 ftypisom"`（12 字节字面量）导致恒为假」和「假响应返回裸字符串导致 `AttributeError`」两类自伤 —— 断言写错比被测代码出错更难发现。

---

## 6. 工程取舍

直连能力是**可选增强**，不能反过来成为负担：

- **站点隔离，单向依赖。** `pixiv_bridge/` 是纯框架，永远不认识 `pixiv/`、`iwara/`、`novel/`、`iwara_web/`；站点与扩展只能 `import pixiv_bridge.*`（两个站点之间也互不依赖）。`scripts/check_boundary.py` 用 AST + 子进程探路由来强制这条边界，`scripts/sites.py` 是构建侧的登记表。
- **重依赖不进根清单。** `curl_cffi` 是编译包，只有 iwara 需要，所以写在 `iwara/requirements.txt` 里，由 `pixiv_bridge/deps.py` 的通用 `ensure_packages(names, requirements)` 按需安装，站点在自己 `run.py` 里先 `bootstrap(partial(ensure_packages, ...))` 再用它。只跑 Pixiv 的用户完全不必碰它。
- **可选能力要有回退，且失败要安静。** 登录态失效不该让整站不可用 —— iwara 的 `_prepare_auth`（`iwara/client.py:465-485`）换不出 access token 时只把原因记进 `auth_error` 并退回匿名，因为「iwara 的绝大部分内容匿名就能看，为了一个失效的 token 让整站不可用是本末倒置」。
- **能力自检要暴露给用户。** `/api/iwara/auth` 匿名时刻意不去打 `/user`（那返回 401，看着像报错），所以它永远 200；只有真拿到 access token 才问一次 `/user` 填 `username_hint` —— 顺带一条教训：token 载荷里**没有**用户名字段（只有 `exp`/`id`/`role`），早期直接读载荷的 `username` 于是恒为 `None`，而当时的测试替身自己往载荷里塞了 `username`，正好把这个 bug 盖住了。

---

## 7. 同一套方法、两个不同站点

| | Pixiv 站点（`pixiv/`） | iwara.tv 站点（`iwara/`） |
| --- | --- | --- |
| 阻塞形态 | SNI 阻断 + DNS 污染 | DNS 污染 + Cloudflare 反爬挑战 |
| ① DNS | 默认解析可用；直连模式下走内置 IP 池 | **解析完全不可信**，DoH 也返回污染答案 → 种子 IP 表 |
| ② IP | 官方源站 IP 池，逐 IP 回退 | 种子池 + 可达性探测 + 单 IP 固定 + 404 换 IP |
| ③ TLS | 无 SNI 握手 **或** ECH（OpenSSL 4.0+） | `curl_cffi` `impersonate="chrome"` |
| ④ 应用 | `X-Client-Time` / `X-Client-Hash` 签名 + OAuth 令牌 | 浏览器头 + `X-Version` 签名（双盐）+ 可选登录 |
| 传输层实现 | 自写 `sni_client.py` / `ech_client.py`（复用同一套报文构造） | `curl_cffi.AsyncSession`（因为必须拿浏览器指纹） |
| 额外依赖 | 无（ctypes 驱动自带 libssl） | `curl_cffi`（编译包，单独 requirements） |
| 登录 | 必需（OAuth） | 可选（公开内容匿名可看） |

**结论**：面对新站点时，先按第 2 节的决策树定位，再到第 3 节挑对应层的手段。两个站点的差异全部集中在**具体手段的选择**上 —— 方法论是一样的。

---

## 8. 遇到新站点时的实操清单

1. **分层探测**：`getaddrinfo` → TCP 443 连通性 → TLS 握手（带/不带 SNI）→ 裸 HTTP 请求。每步都记录结果，不要跳步。
2. **判断 DNS 是否可信**：拿同一个解析器同时查目标域名和一个已知正常的域名。对得上就是解析器坏，对不上就是域相关污染 → 上硬编码 IP 池。
3. **确认目标的 CDN 拓扑**：是否在 Cloudflare 后面（`cf-mitigated` 头、`server: cloudflare`）、边缘是否按内容路由（同一内容换 IP 试试）。
4. **确认封锁类型**：握手被 RST = 封锁（试无 SNI / ECH）；403 挑战页 = 反爬（试浏览器指纹）；两者都有就都要治。
5. **确认 IP 语义**：CDN 边缘的 404 要换 IP，API 的 404 不要。
6. **把可用 IP / 盐 / 指纹配置记成常量并注释来源**，附上实测日期 —— 这些东西会过期，注释是下次维护的入口。
7. **为每一层写一个离线单测**（用假传输、假响应），这样网络变了以后能区分「代码坏了」和「链路变了」。

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
| `IWARA_REFRESH_TOKEN` | iwara 可选登录态（也可跑 `python iwara/get_token.py` 存到 `data/iwara_tokens.json`） |

> 起手先跑 `python pixiv/run.py --check`：自检会逐项报告 python / deps / 项目文件 / 端口 / 网络 / ECH 的状态，把「四道关」的结论直接摊开（`--check` 不监听端口，所以 `port` 只警告不中止）。加了站点之后自检也要跟着长：iwara 入口跑 `python iwara/run.py --check`，会在这张表后面追加 `iwara 依赖`（真 import `curl_cffi`）、`iwara 网络`（真发一次 `GET /videos?limit=1`）、`iwara 登录` 三项——**新站点照这个套路做**：把「本层的判据」写成一条 `CheckResult`，挂在 `Site` 契约的 `checks`（以及 `login_check` / `network_hint`）上（见 `pixiv_bridge/site.py`），框架的 `pixiv_bridge.cli.main(site=SITE)` 就会把它与通用检查打在同一张表里。另外别忘了在构建侧登记：`scripts/sites.py` 的 `SITES` 加一条，`check_boundary.py` / `make_share_packages.py` 才认这个新站点。
