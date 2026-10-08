# Pixiv Bridge — Pixiv 直连接口服务

基于开源项目 [PixEz (pixez-flutter)](https://github.com/Notsfsssf/pixez-flutter) 逆向公开的
Pixiv 官方 App API（`app-api.pixiv.net`）调用方式（请求签名、OAuth 认证、全部端点），用 Python 重新实现的一个
**自托管 HTTP 接口服务**。启动后，任何语言的代码通过普通 HTTP 请求即可调用 Pixiv 数据能力，无需处理签名、token 刷新、防盗链等细节。

> 项目分四块：**`pixiv_bridge`**（接口本体，只要接口就够用）、**`novel`**（小说阅读子项目，依赖前者，多带一个网页阅读器与阅读/下载/收藏台账）、**`iwara`**（iwara.tv 子项目，把 iwara 的视频接口并到同一套服务上）与 **`iwara_web`**（iwara 的视频页面，纯页面扩展）。**两个接口项目各有一个入口**：`run.py` 启动 `pixiv_bridge`、`iwara/run.py` 启动 `pixiv_bridge` + iwara；`novel` 与 `iwara_web` 都没有自己的入口，它们在 `pixiv_bridge` 里以**扩展**方式启用——把扩展目录名写进配置即可，详见「附属项目（扩展）」与「快速开始」。

## 特性

- **免代理直连（双模式）**：默认标准 HTTPS 直连；可开启 SNI 绕过直连（复刻 PixEz：关闭 TLS SNI + 硬编码官方 IP 池），适配 DNS 污染 / SNI 阻断型网络
- **自动认证**：自动刷新 access token（Pixiv 的 refresh token 会轮换，刷新后自动持久化到 `tokens.json`）
- **完整接口**：插画详情/排行/推荐/搜索/相关推荐、用户/收藏/关注、小说、动图元数据、评论、Spotlight 等 55 个端点
- **图片代理**：`i.pximg.net` 有 Referer 防盗链，服务内置图片代理，前端/脚本可直接引用直链
- **统一分页**：所有列表接口返回 `next_url`，任意接口可传 `next_url` 继续翻页
- **内置限流**：令牌桶限流，避免触发 Pixiv 风控（默认 100 请求/分钟，可配置）
- **启动前自动检查环境**：检查 Python 版本 / 依赖 / 网络连通性 / 端口，依赖缺失时自动安装
- **可选的小说阅读器**（子项目 `novel/`，在配置里写 `PIXIV_BRIDGE_EXTENSIONS=novel` 启用）：浏览器打开 `http://127.0.0.1:8765/novel` 即可搜作品/搜作者、看榜单/推荐/关注/追更/收藏、进作者主页关注或取关、连续阅读、正文插图内联显示（多来源兜底取图）、导出 EPUB（一本书一个文件，含插图与目录；零依赖单文件页面，已做移动端适配，手机浏览器可用）。接口本体不需要它，不启用扩展时这个地址是 404
- **阅读与下载都有账**：阅读历史（`data/history.jsonl`）、下载历史（`data/downloads.jsonl`，**按章节记**）、收藏台账（`data/bookmarks.jsonl`）都落在项目里；书架分「云端记录 / 下载记录 / 本地文件」三类，同名系列的章节可在书架里**一键合并导出**成一个 EPUB
- **数据都在 `data/`**：token、阅读历史、下载记录、收藏台账、页面配置统一放在项目下的 `data/` 目录（老版本的 `tokens.json` / `history.jsonl` 首次启动自动搬过去），换机只需拷这一个目录
- **可选的 iwara.tv 站点**（子项目 `iwara/`，用 `python iwara/run.py` 启动）：把 iwara 的视频浏览 / 搜索 / 详情 / 直链解析 / 缩略图并到同一套服务上（`/api/iwara/*`），还自带一个**视频页面**（`http://127.0.0.1:8766/iwara`）：列表 / 搜索 / 筛选、点开详情看简介与标签、看相关视频、`<video>` 直接播（服务端代转字节并透传 `Range`，清晰度可切换）。直链是带签名的 MP4，浏览器里能直接播。用 `run.py` 启动时这些地址是 404
- **OpenAPI 文档**：启动后访问 `http://127.0.0.1:8765/docs` 在线调试

## 原理

Pixiv 官方 App 通过 `app-api.pixiv.net` 提供数据，请求需要：

1. **签名头**：`X-Client-Time` + `X-Client-Hash`（`md5(时间 + 固定盐)`）
2. **OAuth 令牌**：`Authorization: Bearer <access_token>`，access token 约 1 小时过期，用 refresh token 刷新
3. **移动端请求头**：`User-Agent`、`App-OS`、`App-Version` 等伪装为官方 Android 客户端

这些细节与端点清单均来自 PixEz 源码（`lib/network/api_client.dart`、`oauth_client.dart`），本项目在 Python 中重新实现，方便被任意代码调用。

> **连不上时怎么办**：下面的「直连模式」「ECH 模式」「代理通道」三节讲的是 Pixiv 这一站的具体开关。若想了解**为什么这么设计**、以及面对一个新站点时怎么分层定位（DNS / TCP / TLS / 应用层各自的现象与手段），见 **[DIRECT.md](DIRECT.md)**（直连思路）；iwara 子项目也用的是同一套方法。

### 直连模式（SNI 绕过，复刻 PixEz）

部分网络对 Pixiv 的封锁手段是 **SNI 阻断 / DNS 污染**（域名被解析成错误 IP，或 TLS 握手因 SNI 字段被识别而中断）。PixEz 的解法（本项目已移植）：

1. **硬编码 IP 池**（`hoster.py`，对应 PixEz `Hoster`）：内置 Pixiv 官方源站 IP，不再依赖本地 DNS 解析
2. **无 SNI TLS 握手**（`sni_client.py`，对应 PixEz `TlsSettings(sni: false)`）：TLS ClientHello 不携带 SNI 字段，绕过基于 SNI 的域名过滤
3. **证书不校验**（对应 PixEz `verifyCertificates: false`）：直连源站 IP 时跳过证书校验
4. **逐 IP 回退**：按序尝试 IP 池，单 IP 连接失败自动换下一个

开启方式（环境变量，默认关闭）：

```
PIXIV_BRIDGE_BYPASS_SNI=1
```

> 说明：若你的网络是 **IP 层封锁**（对所有 Pixiv 官方 IP 直接丢弃 TCP 连接，无 SNI 也无法连通），本模式同样无效，请改用代理。可通过启动时的网络检查判断：`network` 一项显示 API 域名不可达即属于此类。

### ECH 模式（SNI 被阻断时的推荐方案）

当网络能连上 Pixiv 的 IP，但**只要 ClientHello 里出现 `app-api.pixiv.net` 就被 RST** 时，无 SNI 模式（上面）会拿到 403/连接重置。ECH（Encrypted Client Hello，RFC 9180 的 TLS 扩展）把真正的 SNI 加密后放进 ClientHello，中间设备只能看到外层名字 `cloudflare-ech.com`。

Pixiv 的 API 与图片 CDN 都在 Cloudflare 后面，而 **Cloudflare 的 ECH 公钥是按 anycast 集群下发、与站点无关**，因此本项目可以用 Cloudflare 的 ECH 配置连任意 Cloudflare 前置的 Pixiv 域名：

1. **取 ECH 配置**：依次向 `cloudflare-dns.com` → `dns.alidns.com` → `223.5.5.5` 发 DoH 查询（`type=65`），取 `cloudflare-ech.com` 的 `ech=` 字段（国内一般只有后两个可达）；全部失败才回退到内置配置（`ech_client.py` 的 `DEFAULT_ECH_CONFIG`，**会过期**）。配置在进程内缓存 30 分钟，并在“TLS 层握手失败”时强制刷新一次重试
2. **调用 OpenSSL 4.0+ 的 `SSL_set1_ech_config_list`**：Python 自带的 `ssl` 模块尚未暴露 ECH，故用 `ctypes` 直接调用 libssl（`ech_client.py`）。库优先取**项目自带的 `openssl-libs/`**（见下），没有或载入失败才去找系统安装的 OpenSSL。注意 ECH 是 **OpenSSL 4.0.0** 才加入的，3.x 全系（含 3.5/3.6）都没有——所以系统里装的是 3.x 时回退必然失败
3. **只提供 TLS 1.3**：Cloudflare 对含 ECH 且允许 TLS 1.2 的 ClientHello 直接回 `illegal_parameter`，因此必须把 `SSL_CTX` 的 min/max 版本都钉在 TLS1.3
4. **外层用 `cloudflare-ech.com`、内层用真实域名**：certificate 校验按真实域名做（`SSL_set1_host`）
5. **加密 SNI 里的目标域名仍会被中间设备看到吗**——不会；但外层名字仍需能通过，故入口 IP 用 Cloudflare 的 anycast 地址（内置池 `104.18.10.118,104.18.11.118,104.18.42.239,104.16.132.229`）

开启方式：

```
PIXIV_BRIDGE_ECH=1
PIXIV_BRIDGE_BYPASS_SNI=0     # 两者只需开一个，ECH 优先
```

前提：有一份带 ECH 的 libssl（**OpenSSL 4.0 及以上**；3.x 全系没有 ECH）。**项目已经自带**：`openssl-libs/` 目录里随包附带 OpenSSL 4.x 的 `libssl` / `libcrypto`，Windows（`win32-amd64`，4.0.2）与安卓 Termux（`android-arm64`，4.0.3）都已带对应子目录，所以拷到一台完全没装 OpenSSL 的机器上也能直接开 ECH，不需要先装任何东西。程序查找顺序是 `PIXIV_BRIDGE_OPENSSL_DIR`（你明确指定的）→ **项目自带的 `openssl-libs/`** → `openssl` 可执行文件旁边 → PATH → 常见安装路径（Windows `C:\Program Files\OpenSSL-Win64\bin`、Linux `/usr/lib/x86_64-linux-gnu` 等）；候选逐个尝试，前一个载入失败（架构不符、缺运行库、版本太老）自动换下一个，全都不行才报错并列出每个候选的失败原因。

启动检查里的 `ech` 一项显示 `ECH 传输可用` 即表示可用。若显示未找到或载入失败，再按系统装的思路补救：Windows 装 OpenSSL **4.x** 后默认在 `C:\Program Files\OpenSSL-Win64\bin`；Linux/macOS 的发行版与 Homebrew 目前多为 3.x，**装了也没有 ECH**，需要自备 4.0+ 的 `libssl`。也可以直接改用无 SNI 模式 / 代理。

> ECH 模式经本机实测（中国大陆家宽 + DNS 污染 + SNI 阻断）：`run.py --check` 能完成真实 refresh token 刷新，40 余个读接口与 `/img` 图片流全部 200。

#### 某些网络会阻断 ECH 扩展（本地链路问题，与服务端无关）

部分运营商/网络会**无差别阻断 ECH 扩展本身**：只要 ClientHello 里带一个*结构合法*的 ECH 扩展，就在 0.2 秒内被 RST（`SSL_get_error=5`）；而同一 IP、同一外层 SNI、**不带** ECH 扩展的握手能正常走完（`SSL_get_error=1`，只是证书与 SNI 不匹配）。特征：

- `run.py --check` 的 `ech` 一项报 `ECH 握手失败（SSL_get_error=5）`（每个 IP 分别带上具体原因），提示里会点明「本机到 Cloudflare 这一段在阻断 ECH 扩展」；
- 换 IP、换外层 SNI、换 ECH 配置都无效——中间设备只看扩展在不在；
- **同一台机器也会时通时断**：同一个 IP 可能几分钟前正常返回 200，几分钟后全部被 RST。

这**只是你这一侧到 Cloudflare 的链路行为，不是服务端问题**：Pixiv / Cloudflare 的 ECH 支持一直正常，同一份程序在别的网络或服务器上可以直连。客户端无法绕过这种阻断，可行的办法是换网络（手机热点试一下）或改用下面的代理通道。程序仍会照常刷新 DoH 配置并把详细子错误写进日志，用来区分「配置过期」（`SSL_get_error=1`，会自动刷新重试）与「被阻断」（`SSL_get_error=5`）。

#### 怎么区分「本地网络问题」和「服务端问题」

| 现象 | 属于哪一段 | 怎么判断 |
| --- | --- | --- |
| `ech` 报 `SSL_get_error=5`（握手被重置） | 本机 → Pixiv（本地链路） | 换网络 / 手机热点 / 代理后立刻恢复 |
| `getaddrinfo failed`、解析超时 | 本机 → Pixiv（本地 DNS 被污染） | 同上；ECH 模式下不走系统解析，所以 ECH 能通、普通模式不通 |
| `network` 一项失败、`TimeoutError` | 本机 → Pixiv（本地链路） | 同一时刻别的网络正常 |
| 页面打不开、连接被拒（`ERR_CONNECTION_REFUSED`） | 浏览器 → 本服务（桥） | 与 ECH 无关：查 `HOST`/`PORT`、防火墙、是否用了正确的局域网 IP |
| 接口 404（`{"detail":"Not Found"}`） | 浏览器 → 本服务（桥） | 服务进程是旧版本，重启即可，见「打开小说报错时怎么看」 |
| 上游 404 / 5xx，换网络也一样 | 服务端（Pixiv） | 只有这一本书、这一个接口失败，换网络能复现 |

一句话：**换网络或换机器就能消失的，都是本地链路问题；换到哪都一样、且只影响特定资源的，才是服务端问题。**

### 代理通道（`PIXIV_PROXY`）

设置后**所有**对 Pixiv 的请求（API、OAuth 换 token、图片代理、`run.py --check`）都走这个代理，不再使用 ECH / SNI 绕过——因为代理已经替你解决了连通性：

```
PIXIV_PROXY=http://127.0.0.1:7890      # Clash/v2ray 常见的混合端口
```

- 支持 `http://`；`socks5://` 需要先 `pip install "httpx[socks]"`（项目默认不带，保持依赖最小）
- 出现 `proxy WARN 代理不可用` 时，检查代理软件是否在运行、端口是否写对
- 与 `PIXIV_BRIDGE_ECH` / `PIXIV_BRIDGE_BYPASS_SNI` 同时设置时**代理优先**


## 快速开始

要求 Python 3.10+。**一条命令即可启动**：运行时会先做环境检查，依赖缺失则自动用 pip 安装，无需手动装依赖。

> **第一次安装、不知道从哪下手？看 [INSTALL.md](INSTALL.md)（傻瓜版）**：Windows 双击 `start.bat`，安卓 / Termux 运行 `sh start.sh`，含登录一次、连不上 Pixiv 的三条通道和排错表。

整个项目文件夹是自包含的：直接拷贝到另一台机器即可运行（详见 [打包与移植](#打包与移植换机--离线部署)）。

```bash
cd pixiv-bridge
python3 -m venv .venv                                   # 可选，但推荐（Windows: python -m venv .venv）

# 只要接口
.venv/bin/python run.py                                 # Windows: .venv\Scripts\python run.py

# 接口 + 小说阅读器：在 .env 里加一行 PIXIV_BRIDGE_EXTENSIONS=novel 后，用同一条命令启动
# （多了 `/novel` 阅读器页面与阅读历史 / 下载 / 收藏 / 书架那几个接口）

# 接口 + iwara.tv（多了 `/api/iwara/*` 那几个接口）
.venv/bin/python iwara/run.py

# iwara + 视频页面：在 .env 里加一行 PIXIV_BRIDGE_EXTENSIONS=iwara_web
# （`/iwara` 上多出视频页面；它调的是 iwara 的接口，所以要跟上面那条一起用）
```

项目分成四块，**子项目依赖主项目**、单向依赖不反过来。**两个接口项目各有一个入口脚本**；小说阅读器与 iwara 视频页面都没有专属入口，它们作为扩展挂上去：

| 启动方式 | 提供什么 |
| --- | --- |
| `run.py` | 接口主项目（`pixiv_bridge`）：认证、插画、搜索、动态、用户、`/api/novel/*`、图片代理、健康检查，以及 `/` 上的**导航页** |
| `run.py` + `PIXIV_BRIDGE_EXTENSIONS=novel` | 上面那些，再挂上小说阅读子项目（`novel`）：阅读器页面 `/novel` 与 `/api/history`、`/api/downloads`、`/api/bookmarks`、`/api/config`、`/api/library`（导航页上也会多出一段指向阅读器） |
| `iwara/run.py` | 接口之上再挂 iwara 子项目（`iwara`）：`/api/iwara/videos`、`/api/iwara/search`、`/api/iwara/video/{id}`、`/api/iwara/related/{id}`、`/api/iwara/resolve/{id}`、`/api/iwara/stream`、`/api/iwara/image`、`/api/iwara/thumbnail/{id}`、`/api/iwara/me`、`/api/iwara/auth` |
| `iwara/run.py` + `PIXIV_BRIDGE_EXTENSIONS=iwara_web` | 上面那些，再挂上 iwara 视频页面扩展（`iwara_web`）：`/iwara` 上的播放页（浏览 / 搜索 / 播放 / 相关视频） |

两个入口脚本共用同一套命令行（`--host` / `--port` / `--reload` / `--check` / `--env`）与环境检查——`iwara/run.py` 会在这套检查后面追加 3 项 iwara 专属自检（见「环境检查说明」）。只要接口的话，拷 `pixiv_bridge/` 一个包即可；要阅读器就连 `novel/` 一起拷（并在配置里启用扩展），要 iwara 就连 `iwara/` 一起拷，要 iwara 的视频页面就再加 `iwara_web/`（子项目都通过 `import pixiv_bridge.*` 用主项目，主项目不认识它们）。

首次运行输出示例：

```
[环境检查]
  python   OK   3.12.11 (需要 3.10+)
  deps     OK   依赖完整
  files    OK   项目文件完整
  port     OK   8765 可用
  network  WARN 可达 0/3: 全部不可达（API 域名不可达，接口将无法使用）
  提示: 无法直连 Pixiv，可设置 PIXIV_BRIDGE_ECH=1 走加密 SNI，或设置 PIXIV_BRIDGE_BYPASS_SNI=1 开启 SNI 绕过，或设置 PIXIV_PROXY=http://127.0.0.1:7890 走代理
INFO:     Uvicorn running on http://127.0.0.1:8765
```

> 不用 venv 也可以直接 `python run.py`，依赖缺失时同样会自动安装（装到当前 Python 环境）。

### 环境检查说明

每次启动前自动执行 5 项检查（`iwara/run.py` 入口还会多打 3 项 iwara 专属检查，见下）：

| 检查项 | 失败影响 | 说明 |
| --- | --- | --- |
| `python`（版本 ≥ 3.10） | **中止启动** | 代码使用了 `list[str] \| None` 语法 |
| `deps`（fastapi/uvicorn/httpx） | **自动安装后复查**，仍失败则中止 | 用 `pip install -r requirements.txt` 自动下载；有本地 `wheels/` 时优先离线安装 |
| `files`（`run.py` / `requirements.txt`） | 仅警告，不中止 | 拷贝项目时漏文件的自检（阅读器页面属于 `novel/`，不在接口项目的清单里） |
| `port`（监听端口） | **中止启动**（`--check` 下只警告） | 提示换端口（`--port` 或 `PIXIV_BRIDGE_PORT`）。`--check` 只校验、不监听端口，所以端口被占时照常给出结论，真正启动时才中止 |
| `network` / `ech` / `proxy`（Pixiv 域名连通性） | 仅警告，不中止 | 三选一：设置 `PIXIV_PROXY` 时真发一次经代理的请求（`proxy` 项）；开启 `PIXIV_BRIDGE_ECH=1` 时真发一次 ECH 请求（`ech` 项）；否则用内置 IP 池 + 无 SNI TLS 探测（`network` 项）。不可达时按当前模式给出改 ECH / 改无 SNI / 改代理的提示，ECH 被 RST 时会直接点明“当前网络在阻断 ECH 扩展” |

校验登录态并退出（不启动服务，但**同样做上面全部 5 项**，其中 `port` 只警告）：`python run.py --check`。`iwara` 入口是 `python iwara/run.py --check`，它在同一张表里追加 3 项：

| iwara 检查项 | 失败影响 | 说明 |
| --- | --- | --- |
| `iwara 依赖` | 仅警告，不中止 | 真 `import curl_cffi`（不看包在不在，看能不能用——ABI 不匹配也照样失败），附版本号 |
| `iwara 网络` | 仅警告，不中止 | 真发一次 `GET /videos?limit=1`，确认 `api.iwara.tv` 可达。只探 TCP 不够——真正的坎是 Cloudflare 挑战，必须真发一次请求 |
| `iwara 登录` | 仅警告，不中止 | 没配 token 就报「匿名模式」（公开视频含 R18 照常看），配了就真刷一次 access token 并读用户名；失败提示重新 `python iwara/get_token.py` |

这三项**故意不在致命清单里**：iwara 依赖缺失或 API 不可达时入口仍能启动（图片/其它接口照用），所以只提示不中止。

### 获取 refresh_token（三种方式任选）

> 推荐方式 A。Pixiv 已限制部分账号的密码登录，网页授权（PKCE）最稳。

**方式 0：一条命令（最省事，推荐新手）**

```bash
python get_token.py     # 交互式选「2) 浏览器授权」，登录成功后自动写入 tokens.json
```

**方式 A：网页授权（推荐）**

```bash
.venv/bin/python run.py
# 1) 打开浏览器访问 GET /api/auth/login-url 返回的 url，登录并授权
curl -s http://127.0.0.1:8765/api/auth/login-url
# 2) 授权后浏览器地址栏会跳转到 .../callback?code=xxxx，复制 code
# 3) 用 code 换 token（code_verifier 用上一步返回的）
curl -s -X POST http://127.0.0.1:8765/api/auth/callback \
  -H "Content-Type: application/json" \
  -d '{"code":"上一步的code","code_verifier":"login-url返回的code_verifier"}'
```

成功后 token 自动写入 `tokens.json`，之后重启服务无需再登录。

**方式 B：密码登录**

```bash
curl -s -X POST http://127.0.0.1:8765/api/auth/login \
  -H "Content-Type: application/json" \
  -d '{"username":"你的pixiv账号","password":"你的密码"}'
```

**方式 C：已有 refresh token**

把 token 写入项目根目录 `.env`（参考 `.env.example`）：

```
PIXIV_REFRESH_TOKEN=你的refresh_token
```

或直接放入 `data/tokens.json`（`{"refresh_token": "..."}`）。

### 启动

```bash
# 纯接口（不带网页）
.venv/bin/python run.py                    # 默认 127.0.0.1:8765
.venv/bin/python run.py --host 0.0.0.0 --port 9000

# 接口 + 小说阅读器：先给 .env 加一行 PIXIV_BRIDGE_EXTENSIONS=novel，再用上面同一条命令
# （阅读器在 /novel；同一个 --host / --port 用法）

# 接口 + iwara.tv（同一个 --host / --port 用法）
.venv/bin/python iwara/run.py
```

> Windows 的 `start.bat` / Termux 的 `start.sh` 跑的都是第一行那个入口；默认不挂任何扩展。

验证：

```bash
curl http://127.0.0.1:8765/api/health
curl http://127.0.0.1:8765/api/auth/status
```

接口就到此可用了。打开 <http://127.0.0.1:8765/> 是**导航页**：不挂扩展时它会列出接口项目自带的 `/docs`、`/redoc`、`/openapi.json`、`/api/health`；挂了扩展则会多出「扩展：X」一段（扩展用可选的 `EXTENSION_NAV` 声明自己的页面，详见「附属项目（扩展）」）。要小说阅读器页面，在配置里启用 `novel` 扩展再用同一条命令启动，然后打开 <http://127.0.0.1:8765/novel>。

## 项目结构

```
pixiv-bridge/
├── run.py / pixiv_bridge/__main__.py   接口入口（两者等价：都走 deps.bootstrap 先自动装依赖，再启动；按配置挂扩展）
├── start.bat / start.sh                一键启动脚本（跑的就是 run.py）
├── get_token.py                        交互式获取 refresh_token（浏览器授权 / 账号密码）
├── requirements.txt                    依赖清单（Termux 自动改用 requirements-termux.txt）
├── requirements-termux.txt             Termux 专用：纯 Python 的 pydantic 1.x（无需 Rust 编译）
├── INSTALL.md                          傻瓜版安装配置说明（Windows / 安卓 Termux）
├── DIRECT.md                           直连思路（分层定位方法与各层手段，换了网络/加了站点时先看这份）
├── novel/                              小说阅读子项目（依赖 pixiv_bridge，作为扩展启用）
│   ├── __init__.py  说明本子项目依赖主项目；对外入口是 app.create_app
│   ├── extension.py    扩展入口（固定文件名）：导出本子项目的路由与状态工厂
│   ├── app.py          组装：台账/配置/书架挂到 app.state，再挂本子项目路由
│   ├── history.py      阅读历史（JsonlStore 子类）
│   ├── downloads.py    下载历史（按「书号#章号」记账，可按系列汇总进度）
│   ├── bookmarks.py    收藏台账（删除写墓碑行，收藏状态可回溯）
│   ├── web/index.html  内置 Web 页面：个人向小说阅读器（搜作品/搜作者 / 榜单 / 推荐 / 关注 / 追更 / 收藏 / 书架三段 / 阅读器含插图 / EPUB 导出），挂在 `/novel` 上
│   └── routes/         本子项目的路由（history / downloads / bookmarks / config / web）
├── iwara/                              iwara.tv 子项目（依赖 pixiv_bridge，入口 iwara/run.py）
│   ├── __init__.py  说明本子项目依赖主项目；对外入口是 app.create_app
│   ├── run.py          完整服务入口（接口 + iwara；多装一个 curl_cffi 依赖）
│   ├── client.py       iwara 客户端：种子 IP 池 + 浏览器指纹 TLS + 直链解析 + Range 探测
│   ├── tokens.py        登录态落盘（data/iwara_tokens.json，与 pixiv 的 tokens.json 分开）
│   ├── get_token.py     可选：交互式登录一次（邮箱 + 密码），解锁订阅内容
│   ├── envcheck.py      iwara 专属自检（依赖 / api.iwara.tv / 登录态），经 extra_checks 追加到入口检查表
│   ├── app.py          组装：把 IwaraClient 挂到 app.state，再挂本子项目路由
│   ├── requirements.txt 本子项目额外依赖（curl_cffi，刻意不放进根清单）
│   └── routes/         本子项目的路由（videos 接口）
├── iwara_web/                          iwara 视频页面（扩展包：只放页面，接口仍在 iwara/）
│   ├── __init__.py     说明本扩展只做页面层，没有自己的状态与接口
│   ├── extension.py    扩展入口（固定文件名）：导出页面路由与 EXTENSION_NAV
│   ├── web.py          页面路由（`/iwara`），靠绝对路径调 /api/iwara/*
│   └── index.html      内置 Web 页面：视频浏览 / 搜索 / 播放（详情页 + 相关视频 + 清晰度切换）
├── data/                               你的全部个人数据（换机只拷这一个目录；可用 PIXIV_DATA_DIR 换位置）
│   ├── tokens.json                     登录凭据（首次启动自动从项目根搬进来）
│   ├── iwara_tokens.json               iwara 登录态（只有跑过 iwara/get_token.py 才有）
│   ├── settings.json                   页面配置（本地书架目录、合并/跳过开关、扫描层数）
│   ├── history.jsonl                   阅读历史（一行一条 JSON）
│   ├── downloads.jsonl                 下载历史（一行一条 JSON，按章节记）
│   └── bookmarks.jsonl                 收藏台账（一行一条 JSON，删一条写墓碑行）
├── pixiv_bridge/                       接口主项目（纯接口，不带阅读器页面）
│   ├── deps.py         依赖检查与自动下载安装（支持本地 wheels/ 离线包；`bootstrap()` 是各入口共用的自举段）
│   ├── envcheck.py     运行前环境检查（Python / 依赖 / 项目文件 / 网络 / 端口）
│   ├── config.py       配置加载（.env / 环境变量 / data 目录与旧数据迁移）
│   ├── compat.py       pydantic 大版本兼容（v2 的 model_dump / v1 的 dict）
│   ├── oauth.py        OAuth 认证与 token 自动刷新
│   ├── api.py          App API 客户端（签名 + 全部端点）
│   ├── jsonl.py        通用 JSON Lines 存取（追加写 + 读时折叠 + 超限压缩，各台账共用）
│   ├── userdata.py     data 目录 / 旧数据迁移 / 用户配置 / 本地书架文件扫描
│   ├── hoster.py       Pixiv 官方 IP 池 + Cloudflare ECH 入口 IP（可配置覆盖）
│   ├── sni_client.py   无 SNI TLS 直连客户端（对应 PixEz sni:false）
│   ├── ech_client.py   ECH（加密 SNI）直连客户端（ctypes 调用 OpenSSL 4.0+，优先用自带库）
│   ├── rate_limit.py   令牌桶限流
│   ├── image_proxy.py  图片防盗链代理
│   ├── cli.py          命令行入口（两个入口共用：--host / --port / --reload / --check / --env；默认入口在这里按配置挂扩展）
│   ├── nav.py          导航页渲染（自带入口 + 扩展声明的页面 + 配置里的其它入口）
│   ├── routes/nav.py   `/` 导航页路由（在扩展路由之前挂载，保证扩展抢不走 `/`）
│   ├── server.py       FastAPI 应用组装（lifespan + 异常处理 + 挂载纯接口路由）
│   ├── extensions.py   扩展加载（配置里写目录名，程序找其中的 extension.py）
│   └── routes/         接口路由模块（按领域拆分）
│       ├── auth.py     认证接口
│       ├── illust.py   插画 / 收藏 / 评论
│       ├── search.py   搜索
│       ├── feed.py     排行 / 推荐 / 关注流 / 热门标签
│       ├── user.py     用户 / 关注
│       ├── pixiv_novel.py  小说 / 追更（/api/novel/* 属于接口层，仍在主项目；名字里的 pixiv_ 是为了和 novel/ 阅读器区分）
│       └── misc.py     健康检查 / 通用翻页 / 图片代理
├── tests/              单元测试（443 项：345 主项目 + 98 iwara）
├── openssl-libs/       项目自带的 OpenSSL 4.x 动态库（按 平台-架构 分目录，供 ECH 模式优先加载）
│   ├── win32-amd64/    Windows 版（4.0.2）
│   └── android-arm64/  安卓 / Termux 版（4.0.3）
├── examples/           Python / curl 调用示例
└── scripts/            自检与打包脚本
    ├── check_boundary.py  整理自检（依赖方向 + 出入口布局 + 各子项目的路由差集）
    └── make_share_packages.py  一键生成六个分享包（接口包 / 扩展包 / 组合包）
```

## 配置（环境变量 / .env）

| 变量 | 默认值 | 说明 |
| --- | --- | --- |
| `PIXIV_REFRESH_TOKEN` | 空 | refresh token（`.env` 或 `tokens.json`） |
| `PIXIV_BRIDGE_HOST` | `127.0.0.1` | 监听地址，局域网调用改 `0.0.0.0` |
| `PIXIV_BRIDGE_PORT` | `8765` | 监听端口 |
| `PIXIV_RATE_LIMIT_RPM` | `100` | 每分钟请求上限，被限流就调低 |
| `PIXIV_ACCEPT_LANGUAGE` | `zh-CN` | 返回内容的语言偏好 |
| `PIXIV_REQUEST_TIMEOUT` | `30` | 单次上游请求超时（秒） |
| `PIXIV_DATA_DIR` | `data` | 个人数据目录（token / 历史 / 下载 / 收藏 / 页面配置都在里面）；相对路径按项目根算，换机拷这一个目录即可。**只支持项目目录内**：写成项目外的路径（例如安卓的 `/sdcard/...`）会被自动改回项目里的 `data/`，启动日志与设置页都会提示 |
| `PIXIV_TOKEN_FILE` | `data/tokens.json` | token 持久化文件路径（老版本放在项目根的 `tokens.json` 会在首次启动时自动搬进来；写成项目外会自动收回 `data/` 并尽量把原文件搬进来） |
| `PIXIV_HISTORY_FILE` | `data/history.jsonl` | 阅读历史文件路径（个人使用，一行一条 JSON；相对路径按项目根目录算，见「阅读历史落在项目里」） |
| `PIXIV_DOWNLOADS_FILE` | `data/downloads.jsonl` | 下载历史文件路径（按章节记，见「下载历史按章节记」） |
| `PIXIV_BOOKMARKS_FILE` | `data/bookmarks.jsonl` | 收藏台账文件路径（删除写墓碑行，见「收藏台账」） |
| `PIXIV_CONFIG_FILE` | `data/settings.json` | 页面配置（本地书架目录、合并/跳过开关、扫描层数），由页面里的「Aa → 书架与下载」写入；**写成项目外会被强制改回项目内**，否则「保存配置」很可能一直 500 |
| `PIXIV_BRIDGE_PIP_INDEX` | 空 | 依赖安装时的 pip 镜像源；默认自动尝试清华 / 阿里云 / 官方，网络受限时可自定义 |
| `PIXIV_BRIDGE_BYPASS_SNI` | 关 | `1/true/yes/on` 开启 SNI 绕过直连（无 SNI TLS + 硬编码 IP 池，见「直连模式」） |
| `PIXIV_BRIDGE_API_IPS` | 空 | 逗号分隔，覆盖 `app-api.pixiv.net` 的内置 IP 池 |
| `PIXIV_BRIDGE_OAUTH_IPS` | 空 | 逗号分隔，覆盖 `oauth.secure.pixiv.net` 的内置 IP 池 |
| `PIXIV_BRIDGE_IMAGE_IPS` | 空 | 逗号分隔，覆盖 `i.pximg.net` 的内置 IP 池（ECH / SNI 绕过模式下的图片代理会用到） |
| `PIXIV_BRIDGE_ECH` | 关 | `1/true/yes/on` 开启 ECH（加密 SNI）直连，见「ECH 模式」 |
| `PIXIV_BRIDGE_ECH_CONFIG` | 空 | 自定义 ECHConfigList（base64，可带 PEM 包裹）；留空则自动经 DoH 获取（Cloudflare → 阿里 DNS），失败时用内置默认值（会过期） |
| `PIXIV_BRIDGE_ECH_IPS` | 空 | 逗号分隔，覆盖 ECH 用的 Cloudflare 入口 IP（默认 `104.18.10.118,104.18.11.118,104.18.42.239,104.16.132.229`，按实测可用率排序） |
| `PIXIV_BRIDGE_OPENSSL_DIR` | 空 | 含 OpenSSL **4.0+** `libssl`/`libcrypto` 的目录（**仅 ECH 模式需要**）。留空时用项目自带的 `openssl-libs/`，再退回系统安装位置；填了就**优先**用它 |
| `PIXIV_BRIDGE_EXTENSIONS` | 空 | 要挂载的**附属项目（扩展）**目录，逗号分隔、按顺序挂载；相对路径按项目根算。留空＝纯接口。写 `novel` 就会去找 `novel/extension.py` 并把小说阅读器挂上，写 `iwara_web` 就会挂上 iwara 的视频页面，见「附属项目（扩展）」 |
| `PIXIV_BRIDGE_NAV_LINKS` | 空 | 额外列进导航页「其它入口」段的链接，写成 `标题=地址`，多项用逗号 / 分号 / 换行分隔（如 `iwara 视频=http://127.0.0.1:8766/`）。只写地址时拿地址当标题 |
| `PIXIV_PROXY`（别名 `PIXIV_BRIDGE_PROXY`） | 空 | 如 `http://127.0.0.1:7890`；设置后所有 Pixiv 请求（API / OAuth / 图片 / 自检）都走代理，**优先于 ECH 与 SNI 绕过**，见「代理通道」 |

## 附属项目（扩展）

项目分四块，但**入口只有两个**（`run.py` 与 `iwara/run.py`）。小说阅读器（`novel`）与 iwara 视频页面（`iwara_web`）都不带专属入口脚本，它们作为**扩展**挂在接口主项目上：把扩展目录名写进配置，主项目启动时自己去该目录找固定名字的入口文件 `extension.py`。

```
PIXIV_BRIDGE_EXTENSIONS=novel        # 相对路径按项目根算
```

启用后启动日志会多一段：

```
[扩展] 已挂载 1 个：
  novel    /path/to/pixiv-bridge/novel/extension.py（5 个路由）
```

约定（写自己的扩展时照着做）：

| 名字 | 必需 | 作用 |
| --- | --- | --- |
| `extension.py` | 是 | 扩展目录里的固定入口文件名 |
| `EXTENSION_ROUTERS` | 是 | 要挂到应用上的路由（元组；写单个 router 也能被自动包成元组） |
| `extension_state_factory(settings)` | 否 | 返回 `{属性名: 对象}`，启动时逐个挂到 `app.state` 上（与主项目自己的状态并存；也用它往导航页里加自己的段落，见下） |
| `extension_shutdown(app)` | 否 | 异步函数；应用退出时被 `await`（按扩展的**倒序**释放，用来关掉扩展自己的连接池等资源） |
| `EXTENSION_NAV` | 否 | 导航页上要展示的自己这一摊页面：`({"title": ..., "path": ..., "description": ...}, ...)`。没写就只在导航页上列一个扩展名 |

### `/` 上的导航页

`/` 恒定是接口项目的**导航页**（`pixiv_bridge/nav.py` + `pixiv_bridge/routes/nav.py`），任何人启动都会看到它：

- 不挂扩展时列出接口项目自带的 `/docs`、`/redoc`、`/openapi.json`、`/api/health`；
- 挂了扩展就多出「扩展：目录名」一段，内容来自该扩展的 `EXTENSION_NAV`（没声明就只列名字 + 一句说明）；
- 配置里的 `PIXIV_BRIDGE_NAV_LINKS` 会渲染成「其它入口」一段，用来把**另一个入口**（例如 `iwara/run.py`）挂到手边；
- 导航页路由挂在扩展路由**之前**（Starlette 先匹配先赢），所以扩展即使也声明 `/` 也顶不掉它 —— 小说阅读器就是这么让位到 `/novel` 的。

`iwara/run.py` 那个入口的 `/` 是同一个导航页，只是多一段「iwara 视频（本入口独有）」（列的是那几个 JSON 接口）：iwara 不动路由，而是在 `iwara/app.py` 里通过 `iwara_state_factory` 往 `app.state.nav_sections` 塞一个 `NavSection`。**视频页面不在这一段里** —— 它归 `iwara_web` 扩展，挂上后由该扩展自己的 `EXTENSION_NAV` 在导航页上声明。

### 要点

- **主项目不认识任何具体扩展**：`pixiv_bridge/server.py` 的 `create_app()` 只按传入的扩展对象组装，不读配置（保证纯接口的行为不随 `.env` 漂移）；读配置加载扩展发生在 `pixiv_bridge/cli.py` 里，即 `run.py` 走的那条路。
- **扩展目录名写错不会静默降级**：找不到 `extension.py`、或入口里没定义 `EXTENSION_ROUTERS`，都会在启动前打印一条可读的错误并**以退出码 1 中止**，不会半启动。
- `iwara` 不参与这套机制，它保留自己的入口 `iwara/run.py`（原因是它需要多装一个编译依赖 `curl_cffi`，见「iwara.tv」一节）。
- 命令行上用 `--env` 指定别的目录（`python run.py --env /path/to/other`）时，扩展相对路径就按那个目录算。

## Web 页面（小说阅读器）

小说阅读子项目（`novel/`）内置单文件页面（`novel/web/index.html`，零依赖、无构建步骤）。在配置里启用 `novel` 扩展（`PIXIV_BRIDGE_EXTENSIONS=novel`）后启动，浏览器打开 <http://127.0.0.1:8765/novel> 即可使用（`/` 是接口项目的导航页，上面有指向阅读器的链接）。它是**个人向的 Pixiv 小说阅读器**：只做「找小说 → 读小说 → 存/导出」这一条链路，插画相关的界面已移除。

导航有六项，页面顶部常驻搜索框（搜索框左侧可选「作品 / 作者」）：

| 入口 | 说明 |
| --- | --- |
| **榜单** | 日/周/月、男性向/女性向、新人、R18（共 8 种）可选模式；指定日期在「筛选」里。列表上方会显示当前「模式 · 日期 · 条数」，支持「加载更多」翻页 |
| **推荐** | `/api/novel/recommended` 推荐小说 |
| **关注** | 关注用户的新作，可切换 公开 / 私密（`restrict`）；旁边的「我的关注」直接看自己关注了谁（可在这里进作者页取关） |
| **追更** | 追更的系列列表（点进系列看目录，可整系列下载）；小说阅读页与系列页都有「追更 / 已追更」按钮，点一下即追更/取消（走 `/api/watchlist/novel/add|delete`） |
| **收藏** | 小说收藏台账（`data/bookmarks.jsonl`）：阅读页的「收藏」会**同时**写本地台账并尽力同步到 Pixiv，上游连不上时提示「pixiv 那边没同步上，本地已记下」，本地这条照样生效 |
| **书架** | 三类分开显示：**云端记录**（启动时把 `data/history.jsonl` 并进来，再叠加本机 `localStorage`，卡片显示「已读 xx%」「读到第 N 章」与最后阅读时间，右侧「移除」只清本机那份）、**下载记录**（`data/downloads.jsonl`，按同名系列归组，见下）、**本地文件**（配置的本地目录里的 `.txt` / `.epub`，点开直接读） |
| **搜索** | 关键词 + 排序（最新/最旧/热门）+ 起止日期（排序与日期在「筛选」里展开），回车或点「搜索」触发；把左侧选成「作者」就搜用户，结果进**作者主页** |

作者主页（`#/user/<id>`）：

- 头像、昵称、`@账号`、作品/插画/关注数、个人简介；「关注 / 已关注」一键关注或取关（顺带更新概况行），「Pixiv 主页」跳到原站；下面是这位作者的**小说列表**与「加载更多」。
- 小说卡片上的作者名也是链接，点一下直接进作者主页。

阅读与导出：

- **正文阅读器**：HTML 正文自动归一化（`<ruby>` 拉平注音、`<br>` 转换行、实体解码）；**正文里的插图会内联显示**（见下）；章节内 ←/→ 或 `Esc` 返回、`#/read/<id>` 可直接分享/收藏链接。
- **正文插图**：Pixiv 小说正文里的 `[uploadedimage:<id>]`（作者随小说上传的图）与 `[pixivimage:<id>]`（引用的插画，含 `[pixivimage:<id>-<页>]` 多页形态）都会渲染成图片，走服务内置的 `/img?url=` 代理加载（自动附加 Referer），默认取 `large` 一档；`[chapter:标题]` 显示成小标题，`[newpage]` 画成分页线，`[[rb:汉字 > 假名]]` 显示成注音，`[[jumpuri:标题 > url]]` 显示成链接，`[jump:N]` 忽略。某张图取不到时只在原位留一句说明（不影响其它正文），不会留白框。
- **插图地址从哪来（为什么以前会有「没有取到图片地址」）**：`[uploadedimage:<id>]` 的图片地址**不在正文接口里**——上游小说详情与正文页面都不保证带图，网页版是打开阅读器时**另外**发一次 AJAX（`/ajax/novel/<id>`）才拿到 `textEmbeddedImages`。本服务只有 App 端 OAuth、没有网页版 cookie，所以改成**多来源兜底**：正文接口里带就以它为准；没有就依次尝试「正文页面 HTML 里内嵌的 `textEmbeddedImages`」→「小说详情接口里的同类字段（`/api/novel/<id>` 现在也附带加性 `novel_images`）」；键名兼容 `textEmbeddedImages` / `text_embedded_images` / `embeddedImages` / `novel_images` 等，URL 兼容 `urls` 里的 `1200x1200`/`large` 与网页版的 `large_image_url`/`original_image_url` 等写法，键带 `1:` 前缀也会归位。真的一张都没解析到时，原位会写清**是哪个 id、本次解析到几张**（并在控制台打出已解析的键），方便直接定位。
- **阅读历史落在项目里**：打开小说、滚动、读系列章节时，页面会把「在读什么、读到哪」写进**项目数据目录下的 `data/history.jsonl`**（可通过 `PIXIV_HISTORY_FILE` 改名/换位置）。格式是 **JSON Lines：一行一条 JSON**，追加写不重写整份文件，断电最多丢最后一行，可以直接 `tail` / `grep` 看，也能手工改；字段固定为 `id` / `title` / `user` / `series_id` / `series_title` / `chapter_index` / `chapters` / `progress` / `read_at`（多余的字段会被丢掉，不带任何账号信息）。同一本书只保留最新一条、按时间倒序返回，文件超过 2000 行时自动压成「每本一条」。写入很克制：同一本书最快 15 秒一条，离开页面时再用 `sendBeacon` 补一条；本机 `localStorage` 仍然照写，所以服务没开或浏览器换了都不影响阅读本身。
- **下载历史按章节记**：每次导出 EPUB 都会在 **`data/downloads.jsonl`** 里记一条（一行一条 JSON），粒度是**章节**而不是整本：`book_id`（这一章的作品号）、`chapter_index`（第几章，从 0 起）、`chapter_id` / `chapter_title`、`title` / `user` / `series_id` / `series_title`、`merge_key`（归一化后的系列名，用来归组）、`format` / `file` / `bytes` / `chapters` / `at`。记账主键是 **`book_id#chapter_index`**，所以「两个不同的系列里各有一个第一章」会存成两条互不覆盖的记录（用户明确要过这个场景）；系列页的章节行会显示「已下载」，入口处写着「这个系列已经下载过 N 章」，整本下过再点下载会直接提示并跳过（可在「Aa → 书架与下载」里关掉「跳过已下载」）。
- **书架里一键合并同名系列**：书架「下载记录」按 `merge_key`（同名系列）归组，卡片上列出这一组的每一章（章号重复的那些会额外标出 `（#作品号）`，并提示「同名不同『第 N 章』N 个」），点「合并导出」就按章号顺序逐章抓取、合成一本 `<系列名>（合并N章）.epub`——这就是「自动合并相同系列名称的不同章节」。在「Aa → 书架与下载」里关掉「合并同名系列」后，同名但不同系列的作品各自成组，不再混在一起。旁边的「清空下载记录」只清台账，不动你已经导出的文件。
- **本地书架（读你硬盘上的 txt / epub）**：在「Aa → 书架与下载」里填一个本地目录（可加多个，最多 20 个，用**绝对路径**），页面会调 `/api/library` 扫描该目录下的 `.txt` 与 `.epub`（默认 3 层、最多 1000 个文件，可调），书架第三段就按修改时间列出这些文件：`.txt` 点开直接进阅读器读（读的是文件内容，不写阅读记录），`.epub` 点「下载 / 打开」交给系统/浏览器打开。扫描只读、只列白名单扩展名，目录必须在你配置的清单里（越界路径一律 400）。
- **项目配置写在 `data/settings.json`**：本地书架目录、合并同名系列、跳过已下载、扫描层数、文件数上限都存在这个文件里（`indent=2` 的 JSON，原子写、无 `.tmp` 残留），页面「保存配置」即 `PUT /api/config`；非法值会被拒绝（400）而不是悄悄写坏，文件缺失/损坏时静默回落到默认值，不影响启动。写盘失败不会只回一句 `Internal Server Error`：能自动处理的（只读位、短暂占用、跨文件系统）自动处理，处理不了的在页面提示里写清路径与系统原因（见「保存配置报『写入失败』」）。
- **你的数据都在 `data/` 一个目录里**：`data/tokens.json`（凭据）、`data/history.jsonl`、`data/downloads.jsonl`、`data/bookmarks.jsonl`、`data/settings.json` 统一放在项目下的 `data/`（可用 `PIXIV_DATA_DIR` 换位置，相对路径按项目根算）。老版本直接把 `tokens.json` / `history.jsonl` 放在项目根，**首次启动会自动搬进 `data/`**（目标已存在时不动它，可反复启动）；`.env` 里如果还写着老路径，也会自动改回 `data/` 下的新默认值，不会出现「文件搬走了、配置还指着空路径」。**只支持项目目录内**：任何一条数据路径写到项目外（安卓上最常见的是 `/sdcard/...`，那种目录常常读得到写不进）都会被收回项目里的 `data/`，启动日志与页面设置里逐条提示，能搬的文件顺手搬进来。换机器只要拷 `data/` 这一个目录。
- **阅读设置**（右上角 `Aa`）：字号 / 行距 / 页宽 / 主题（浅色 / 深色）/ 字体，存 `localStorage`。
- **进度记忆**：滚动位置按小说记录（`localStorage`），下次打开自动回到上次位置；书架卡片显示「已读 xx%」。
- **导出 EPUB（一本书就一个文件）**：入口是阅读页的「下载 EPUB」、系列页的「下载全系列（EPUB）」、阅读器目录抽屉里的「下载全系列（EPUB）」。**任何一次导出都只产生一个 `.epub`**：单本导出 `<书名>.epub`；系列导出 `<系列名>（全N章）.epub`——在阅读器里读系列某一章时点「下载 EPUB」也会自动改导整本，并提示「这本属于系列，按整本导出 EPUB」。
- **EPUB 里有什么**：标题 / 作者 / 来源链接 / 字数 / 收藏 / 标签等信息头放在首章开头；正文按 `[chapter:标题]` 分成小标题并进入内置目录（EPUB 的 nav + ncx 双目录，阅读器里可跳转），`[newpage]` 画成分页线，`[[rb:汉字 > 假名]]` 变成真正的 `<ruby>` 注音，`[[jumpuri:标题 > url]]` 变成外链，`[jump:N]` 忽略（分页交给阅读器）；插图作为**真正的图片文件**打进书里（不是 base64 内联），所以离线打开照样有图，图片按 `large` 一档取、整体上限 40 MB，超限或取不到的插图只在原位留一句说明。系列导出逐章抓取、每章一个文档，缺章的说明写在书末，不会整本作废，**文件名也不带缺章标记**（同一本书重复下载就是同一个文件）。
- **导出细节**：EPUB 由页面自己按 OCF 规范打包（手写最小 ZIP，`mimetype` 是第一个且不压缩），零依赖、不需要后端参与；导出有 1.2 秒防连点（系列逐章之间有停顿，避免触发限流）。页面内「复制全文」仍是纯文本，插图位置显示为 `［插图 N］` 占位符。
- **加载更多**：只把按钮变成「加载中…」（不弹全屏遮罩），新条目追加在已有列表后面，不会重建已经渲染的卡片、也不会丢掉滚动位置。
- **诊断**：正文结构未适配或正文接口报错时，阅读器会显示诊断块并附原始 JSON / 接口路径，方便排查。
- **认证状态**：右上角显示登录状态与用户名（首次请求后自动刷新）。

移动端适配（页面按手机优先设计）：

- `≤640px` 时导航改为**贴底标签栏**（六项均分、每项 ≥44px 高），顶部只留标题栏，拇指可及。
- 搜索框常驻一行（关键词 + 搜索 + 筛选），**排序 / 榜单日期默认收在「筛选」里**，榜单首屏直接就能看到小说列表，不再被参数区占满。
- 输入框在窄屏为 `16px` 字号（iOS 不会因聚焦而放大页面）、常用按钮整行铺满；卡片单列、封面放大到 64×88；按钮 / 可点区域按 ≥44px 设计。
- 顶栏 / 标签栏 / 抽屉 / 提示条都带 `env(safe-area-inset-*)`，刘海屏与手势条不遮挡内容；阅读工具条在窄屏可横向滑动。
- 阅读设置（`Aa`）与目录抽屉在手机上都是全宽浮层，浮在标签栏之上。
- 手机上访问用 `http://<电脑局域网 IP>:8765/novel`（后端需以 `HOST=0.0.0.0` 启动，见「局域网访问」）。

打开小说报错时怎么看（页面已内置提示）：

- **「接口不存在（404）」**：当前运行的服务进程里没有 `/api/novel/<id>` 这个接口——即进程是旧版本（页面每次请求都读盘、会自动更新，但**路由不会**）。停掉服务、用最新代码重启即可。
- **「Pixiv 返回 404」**：上游真的找不到这个 id，常见原因：这个 id 其实是**系列 id**（页面会自动尝试按系列打开一次）、作品已删除 / 转私密、或者 R-18 等需要先在 Pixiv 侧确认年龄的作品。
- **「正文接口返回 404」**：书名 / 作者 / 字数能显示，只有正文取不到。Pixiv 已在 2024 年 2 月的 App 更新里**删掉了旧正文接口 `/v1/novel/text`**（只会回一句 `Specified end-point doesn't exist`），本服务改从官方 iOS 客户端的 `/webview/v2/novel` 页面里取正文；若这里仍报 404，多半是这本作品本身对当前账号不可读（R-18 未确认年龄 / 本文非公开 / 已删除）。
- 两种情况都会附一段「诊断信息」（时间 / 页面地址 / 接口 / HTTP 状态 / 上游 message 与 body），点「复制诊断信息」即可整段复制反馈。

小说封面图与正文插图都通过服务内置的图片代理加载（自动附加 Referer），无需额外配置；封面加载失败时用书名首字兜底，插图加载失败时在原位显示提示文字。

> 说明：正文与导出内容仅供个人阅读使用；页面不写入任何后端数据，前端状态都停留在本机浏览器（`localStorage`）。

## 接口清单

所有接口返回 Pixiv 原始 JSON（透传），错误时返回 `{"error": {"message": "...", "reason": "..."}}`。

> 下面「认证」到「其他（接口主项目）」这些，两个入口都有；「台账与书架」只有启用 `novel` 扩展后才有，「iwara.tv」只有 `iwara/run.py` 才有。

### 认证

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET | `/api/health` | 健康检查 |
| GET | `/api/auth/status` | 认证状态、用户信息、token 过期时间 |
| GET | `/api/auth/login-url` | 生成网页授权 URL + code_verifier |
| POST | `/api/auth/callback` | 用授权 code 换 token |
| POST | `/api/auth/login` | 密码登录 |
| POST | `/api/auth/refresh` | 强制刷新 token |

### 插画

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET | `/api/illust/{illust_id}` | 插画详情 |
| GET | `/api/illust/{illust_id}/related?next_url=` | 相关插画 |
| GET | `/api/illust/{illust_id}/comments?next_url=` | 评论 |
| GET | `/api/illust/{illust_id}/ugoira` | 动图帧元数据 |
| GET | `/api/illust/{illust_id}/bookmark-detail` | 收藏详情 |
| GET | `/api/illust/{illust_id}/series` | 系列内作品 |
| POST | `/api/bookmark/add` | 收藏 `{"illust_id":1,"restrict":"public","tags":["a"]}` |
| POST | `/api/bookmark/delete` | 取消收藏 `{"illust_id":1}` |
| POST | `/api/comment/add` | 发评论 |

### 排行 / 推荐 / 流

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET | `/api/ranking?mode=day&date=2026-10-06&next_url=` | 排行；mode: day/week/month/day_male/day_female/week_original/week_rookie/day_r18/... |
| GET | `/api/recommended?type=illust\|manga&next_url=` | 推荐流 |
| GET | `/api/follow/illusts?restrict=public&next_url=` | 关注动态 |
| GET | `/api/trending-tags?type=illust\|novel` | 热门标签 |
| GET | `/api/spotlight?category=all&next_url=` | Spotlight 专题 |
| GET | `/api/walkthrough/illusts` | 引导推荐 |

### 搜索

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET | `/api/search/illust?word=&sort=&search_target=&start_date=&end_date=&bookmark_num_min=&bookmark_num_max=&search_ai_type=&next_url=` | 搜插画；sort: date_desc/date_asc/popular_desc；search_target: partial_match_for_tags/exact_match_for_tags/title_and_caption/...；search_ai_type: 0 全部 / 1 仅非AI / 2 仅AI |
| GET | `/api/search/novel?word=&next_url=` | 搜小说 |
| GET | `/api/search/user?word=&next_url=` | 搜用户 |
| GET | `/api/search/autocomplete?word=` | 联想词 |
| GET | `/api/search/popular?word=&next_url=` | 热门预览 |

### 用户

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET | `/api/user/{user_id}` | 用户详情 |
| GET | `/api/user/{user_id}/illusts?type=illust\|manga&next_url=` | 用户作品 |
| GET | `/api/user/{user_id}/novels?next_url=` | 用户小说 |
| GET | `/api/user/{user_id}/bookmarks?restrict=public&tag=&next_url=` | 用户收藏 |
| GET | `/api/user/{user_id}/bookmark-tags?restrict=` | 收藏标签分组 |
| GET | `/api/user/{user_id}/following?restrict=&next_url=` | 关注列表 |
| GET | `/api/user/{user_id}/followers?restrict=&next_url=` | 粉丝列表 |
| GET | `/api/user/{user_id}/follow-detail` | 关注关系 |
| GET | `/api/user/recommended?next_url=` | 推荐用户 |
| POST | `/api/user/follow/add` / `/api/user/follow/delete` | 关注 / 取关 `{"user_id":1,"restrict":"public"}` |

### 小说

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET | `/api/novel/{novel_id}` | 小说详情（详情里已带 `textEmbeddedImages` 等图片字段时，响应会附上加性 `novel_images`，形状同下；没有图时响应与旧版一致） |
| GET | `/api/novel/{novel_id}/text` | 小说正文（上游 `/v1/novel/text` 已被 Pixiv 下架，改从 `/webview/v2/novel` 页面提取；返回 `{"novel_text": …}`，另附**加性**字段 `novel_images`：`{"<插图id>": {"original","large","medium","thumb"}}`，供页面渲染正文插图，旧调用方忽略即可。正文含 `[uploadedimage:]` 但上游没给图时，会再尝试正文页面 HTML 与详情接口兜底一次） |
| GET | `/api/novel/{novel_id}/comments?next_url=` | 小说评论 |
| GET | `/api/novel/ranking?mode=&date=&next_url=` | 小说排行 |
| GET | `/api/novel/recommended?next_url=` | 小说推荐 |
| GET | `/api/novel/follow?restrict=&next_url=` | 关注小说动态 |
| GET | `/api/novel/series?series_id=&next_url=` | 小说系列 |
| POST | `/api/novel/bookmark/add` / `/api/novel/bookmark/delete` | 小说收藏/取消 |

### 其他（接口主项目）

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET | `/api/watchlist/novel` / `/api/watchlist/manga` | 追更列表 |
| POST | `/api/watchlist/novel\|manga/add\|delete?series_id=` | 追更/取消 |
| GET | `/api/next?url=` | 通用翻页（传任意列表接口返回的 next_url） |
| GET | `/img?url=完整图片URL` 或 `/img/{path}` | 图片代理 |

### 台账与书架（**仅启用 `novel` 扩展后提供**）

下面这些不是接口的一部分，而由小说阅读子项目 `novel/` 挂载；不启用扩展时（`python run.py` / `start.bat` / `start.sh` 的默认状态）它们都返回 **404**（阅读器页面在 `/novel`，不在 `/`——`/` 是导航页），要看下面的说明请先在配置里写 `PIXIV_BRIDGE_EXTENSIONS=novel`：

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET | `/api/history?limit=` | 阅读历史（每本小说一条最新记录，按阅读时间倒序；同时返回 `file` 告诉前端文件落在哪里） |
| POST | `/api/history` | 记一条阅读历史（`{"id": 2001, "title": …, "progress": 0.42, "chapter_index": 1, …}`；字段见「阅读历史落在项目里」，缺 `id` 会被拒绝） |
| GET | `/api/downloads?limit=&series_id=&book_id=` | 下载历史（一行一条、按章节记；带 `series_id` 时额外返回 `series`：`{"<章节序号>": {at,file,bytes,format}}` 方便判断「这个系列下过哪几章」） |
| POST | `/api/downloads` | 记一条下载记录（`{"book_id": 3001, "chapter_index": 0, "title": …, "series_title": "雨夜系列", "merge_key": "雨夜系列", "format": "epub", "file": "雨夜系列（全2章）.epub", "bytes": 5638}`；主键是 `book_id#chapter_index`，同系列两个不同作品的第一章互不覆盖） |
| POST | `/api/downloads/clear` | 清空下载台账（文件保留，只是清记录） |
| GET | `/api/bookmarks?limit=` | 收藏台账（`data/bookmarks.jsonl`，已删的不再返回） |
| POST | `/api/bookmarks` | 收藏一本小说（`{"id": 2001, "title": …, "restrict": "public"}`）：先写本地台账，再尽力同步上游；**上游失败也会返回 200**，用 `synced: false` + `sync_error` 说明原因，本地这条照样生效 |
| POST | `/api/bookmarks/delete` | 取消收藏（`{"id": 2001}`；写一条墓碑行，同样尽力同步上游） |
| GET | `/api/config` | 读项目配置：`{ok,file,data_dir,defaults,config,migrated_files,warnings,library:{dirs,missing},writable:{ok,dir,reason},files:{tokens,history,downloads,bookmarks}}`（`warnings` 是「项目外路径被收回项目内」的逐条说明，`writable` 是数据目录的真写探测） |
| PUT | `/api/config` | 改项目配置（`{"library_dirs": ["D:\\books"], "merge_series": true, "skip_downloaded": true, "scan_depth": 3, "max_files": 1000}`，字段都可选；非法值 400，原子写；写不动时 500 并带上文件路径与系统原因——只读位、短暂占用、跨文件系统都会先自动处理） |
| GET | `/api/library` | 扫描本地书架目录：`{dirs,missing,files:[{name,path,dir,rel,ext,kind,size,mtime,at}],count,truncated,errors,scanned_at}` |
| GET | `/api/library/preview?path=` | 读本地 `.txt` 的文本（最多 2 MB，超长 `truncated: true`；只允许配置目录里的 `.txt`） |
| GET | `/api/library/file?path=` | 下载/打开本地 `.txt` / `.epub`（`Content-Disposition` 带原文件名） |

### iwara.tv（**仅 `iwara/run.py` 提供**）

iwara 是另一个站点，与 Pixiv 无关，由子项目 `iwara/` 挂载；用 `run.py`（不论是否启用 `novel` 扩展）启动时这些地址都返回 **404**。其中的**视频页面**（最后一行）不属 `iwara/` 包，而在独立的扩展包 `iwara_web/` 里——它默认不挂，需要在配置里写 `PIXIV_BRIDGE_EXTENSIONS=iwara_web`；没挂时那一条是 404，其余接口照常可用。

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET | `/iwara` | **视频页面**（给人看的，需挂 `iwara_web` 扩展）：列表 / 搜索 / 筛选（排序、分级）、点开详情、`<video>` 走 `/api/iwara/stream` 代转播放、清晰度切换、相关视频、标签跳转、深浅色主题。`iwara/run.py` 的导航页（`/`）上也有入口 |
| GET | `/api/iwara/videos?page=&limit=&sort=&rating=&tags=&user=` | 视频列表。`sort`: `date`/`trending`/`popularity`/`likes`/`views`；`rating`: `all`/`general`/`ecchi`（R18 不需要登录，显式传 `ecchi` 即可取到）；返回上游的 `{count,limit,page,results}`，并给每条补上 `thumbnailUrl` / `posterUrl` |
| GET | `/api/iwara/search?query=&type=&page=&limit=` | 搜索，`type` 默认 `videos` |
| GET | `/api/iwara/video/{video_id}` | 视频详情（同样补 `thumbnailUrl` / `posterUrl`） |
| GET | `/api/iwara/related/{video_id}?page=&limit=` | 相关视频；形状同 `/videos`（同样补缩略图）。上游固定只给 12 条且不接受分页参数，`page`/`limit` 是本地裁剪 |
| GET | `/api/iwara/resolve/{video_id}?probe=true` | 解析可播放的直链：返回各清晰度 `sources`，`probe=true` 时真发一次 Range 请求，把结果放在 `chosen`（`{name,mime,url,host,content_range,verified}`）。`salt_used` 说明用了哪版签名盐 |
| GET | `/api/iwara/stream?url=` | 代转视频字节并透传 `Range`（原样转发客户端的 `Range` / `If-Range`，返回上游的 206 与 `Content-Range`）。只允许 iwara CDN 主机 |
| GET | `/api/iwara/thumbnail/{video_id}?index=&original=` | 按视频 id 直接取缩略图，省得前端自己拼 `file.id` |
| GET | `/api/iwara/image?url=` | 代转任意 iwara 图片地址（只允许 iwara CDN 主机） |
| GET | `/api/iwara/me` | 当前登录用户；未登录返回 401 |
| GET | `/api/iwara/auth` | 只看登录态：`{configured, authenticated, anonymous, error, token_file}`，登录成功后再补 `expires_at` 与 `username_hint`。**匿名时刻意不去打 `/user`**（那返回 401，看着像报错），所以它永远 200；一旦拿到 access token 就会问一次 `/user` —— 因为 token 的载荷里只有 `exp`/`id`/`role`，没有用户名 |

> **登录是可选的**。iwara 的公开内容（含 R18）匿名就能看，登录只为「订阅作者的内容」和「自己账号下的私密视频」。想登录就跑一次 `python iwara/get_token.py`（邮箱 + 密码，没有浏览器授权那条路），token 存到项目里的 `data/iwara_tokens.json`，也可以用环境变量 `IWARA_REFRESH_TOKEN` 临时覆盖。**token 失效不会拖垮整站**：续期失败只记在 `auth_error` 里并退回匿名，`/api/iwara/videos` 之类的接口照常可用。

关于 iwara 的三点实现说明（都来自实测，不是猜的）：

1. **直链不是「文件地址」，要换一次。** `/video/{id}` 给的 `fileUrl` 指向 `/file/<uuid>?expires=…&hash=…`，直接 GET 只会拿到空 JSON；必须以 `X-Version` 头再请求一次，值是该 `fileUrl` 的 `sha1("{uuid}_{expires}_{盐}")`，返回的才是真正的清晰度列表，且每个清晰度在不同 CDN 主机上。
2. **系统 DNS 解析不了 iwara，DoH 也不行。** 本机拿到的污染结果落在 Facebook / Twitter 等网段；而**能连上的 DoH（`doh.pub` / 阿里 / 360）对 `*.iwara.tv` 同样回污染答案**，`cloudflare-dns.com` / `dns.google` / `dns.quad9.net` 在这类网络里根本连不上。所以本子项目不依赖解析：内置一张实测可用的**种子 IP 表**（`SEED_HOSTS` / `SEED_IPS`），DoH 结果只作补充候选且排在种子之后。另一个实测结论让这套做法成立：视频 CDN 的边缘**按内容路由、各花名子域互通** —— 同一份内容钉到池中任一可用 IP 都能取到（个别转码源例外，所以要全池轮询）。
   还有两个坑：libcurl 的 `CURLOPT_RESOLVE` **不是候选列表** —— 同一 host:port 传多条时只认最后一条，因此必须自己探测可达性后固定到**单个** IP；以及 CDN 边缘对不服务的内容回 `404 page not found`，这属于「换个 IP 就有」，会触发换 IP 重试（API 的 404 是真实业务状态，不重试）。
3. **必须用浏览器 TLS 指纹。** iwara 在 Cloudflare 后面，纯 Python 的 TLS 指纹会被判定为机器人（403 + `cf-mitigated: challenge` + `Just a moment...`）。因此额外依赖 `curl_cffi`（`impersonate="chrome"`），它只装在 `iwara/requirements.txt` 里，不影响只跑 Pixiv 接口的用户。另外并非所有 CDN 花名主机都可达，解析直链时会在全部 `sources` 之间逐个探测，取第一个真正能播的。

> 这三点属于**链路层**问题，不是 iwara API 本身的特性。它们和 Pixiv 那边对付 DNS 污染 / SNI 阻断 / ECH 阻断用的是同一套思路（分层定位 → 逐层选手段），完整方法论见 **[DIRECT.md](DIRECT.md)**。

## 调用示例

### curl

```bash
# 日榜前几条
curl -s "http://127.0.0.1:8765/api/ranking?mode=day" | jq '.illusts[:3] | .[].title'

# 搜索
curl -s -G "http://127.0.0.1:8765/api/search/illust" \
  --data-urlencode "word=初音ミク" --data-urlencode "sort=popular_desc"

# 翻页：取响应的 next_url
curl -s -G "http://127.0.0.1:8765/api/next" \
  --data-urlencode "url=https://app-api.pixiv.net/v1/illust/ranking?mode=day&offset=30&filter=for_android"
```

### Python（同步客户端，见 `examples/client.py`）

```python
from examples.client import PixivBridge

bridge = PixivBridge("http://127.0.0.1:8765")
illust = bridge.illust(97682620)          # 插画详情
ranking = bridge.ranking("day")           # 日榜
search = bridge.search_illust("初音ミク", sort="popular_desc")
page2 = bridge.next(search["next_url"])   # 翻页
img_url = bridge.image_url(illust["illust"]["image_urls"]["original"])  # 直链（走代理）
```

### 任意语言

接口是纯 HTTP + JSON，任何语言（JS/Go/Java/C# 等）用标准 HTTP 库即可调用，无需引入 Pixiv 相关 SDK。

## 分页说明

Pixiv 列表接口的分页基于 `next_url`（不是页码）。服务的行为：

1. 列表接口的响应里透传 Pixiv 返回的 `next_url` 字段
2. 需要下一页时，把 `next_url` 原样传给**同一个列表接口的 `next_url` 参数**，或统一用 `GET /api/next?url=...`

`next_url` 只接受 `app-api.pixiv.net` 主机，其他主机一律拒绝（防 SSRF）。

## 图片代理

Pixiv 图片域名 `i.pximg.net` 有防盗链，直接 <img> 引用会 403。两种用法：

```bash
# 方式1：传完整 URL（来自响应里的 image_urls.original / thumb 等）
curl -s -o sample.jpg "http://127.0.0.1:8765/img?url=https://i.pximg.net/c/.../xxx_p0_master1200.jpg"

# 方式2：直接传路径
curl -s -o sample.jpg "http://127.0.0.1:8765/img/c/250x250_80_a2/img-master/img/.../xxx_p0_master1200.jpg"
```

代理会自动附加 `Referer: https://app-api.pixiv.net/`，并做 24 小时缓存。只允许代理 `i.pximg.net`。

## 错误处理

| HTTP 状态码 | 含义 |
| --- | --- |
| `200` | 成功，返回 Pixiv 原始 JSON |
| `400` | 参数错误 / next_url 主机不合法 |
| `401` | 未认证或 refresh token 失效（需重新获取 token） |
| `404` | 上游找不到资源（作品已删除 / id 不是小说而是系列 / R-18 未确认年龄等），`message` 为上游原文 |
| `429` | 触发 Pixiv 限流（调低 `PIXIV_RATE_LIMIT_RPM` 或稍后重试） |
| `502` | 上游网络异常，或正文页结构已改版（`/webview/v2/novel` 的返回里找不到 `novel` 对象） |
| `500` | 本地写盘/读盘失败（保存配置、扫描本地书架、读取设置）。`detail` 会写明是哪个文件、系统给的什么原因、该怎么办；没预料到的异常也会带上异常类型与原文，不会只回 `Internal Server Error`（Termux 上若出现 `AttributeError: … has no attribute 'model_dump'`，说明跑的是旧版代码，见下面「安卓 / Termux 的 pydantic 1.x」） |

响应体统一为 `{"error": {"message": "...", "reason": "..."}}`；上游错误体有多种形状（`{"error": {"message": ...}}`、OAuth 风格 `{"error": "invalid_grant", ...}`、`{"error": true, "message": ...}`、纯文本），服务都会解析出可读的 `message` 并把**上游状态码原样透传**；上游回空错误体时（Pixiv 对「作品不存在 / 对当前账号不可见」的 404 就是这样）`message` 会退化为 `上游返回 HTTP 404（上游未给出错误说明）`，不会留空。过长（>300 字符）的上游报文会被截断，避免整页 HTML 塞进错误信息。

### 保存配置报「写入失败」（`data/settings.json`）

页面「Aa → 书架与下载」保存时提示写盘失败，通常是 `data/settings.json` 被别的程序占着或带了只读属性（编辑器打开着、同步盘/杀毒软件正在扫描、或从压缩包里解出来带了只读位）。服务端已经做了三层兜底，一般不用手动处理：

1. 目标带只读属性 → 自动摘掉只读位再写；
2. 目标被短暂占用（杀软/同步盘）→ 退避重试后再写；
3. 还是换不动 → 退回「直接覆盖写」，并保证不留下 `settings.json.tmp`。

三层都失败时才会返回 `500`，`detail` 里会写明路径和系统原因（例如 `写入失败：Permission denied。配置文件 … 可能正被其它程序打开…`）。这时关掉占用它的程序（编辑器、同步盘、杀软的实时扫描）后重试即可；实在不行直接删掉 `data/settings.json`，服务会退回默认值。

还有一个容易被忽略的原因：**数据目录在项目外**（安卓上把 `PIXIV_DATA_DIR` 指到 `/sdcard/...` 就属于这种）。项目外的目录经常「读得到、写不进」，保存配置就会 500。现在这类路径会被**自动收回项目里的 `data/`**：

- 启动时日志与**页面「Aa → 书架与下载」里的黄色提示块**都会写明哪一条被改了（`PIXIV_DATA_DIR`、`PIXIV_TOKEN_FILE` 等逐条列出）；
- 能搬的文件会自动搬进项目（例如项目外的 `tokens.json`），搬不动的就改用项目内的新文件，原位置的文件不动（不丢数据）；
- `GET /api/config` 多返回两个字段：`warnings`（上面那些提示）与 `writable`（真写一个探针文件判断数据目录能不能写，`{"ok": bool, "dir": "...", "reason": "..."}`），页面在保存前就能提示；
- 跨文件系统的 `os.replace`（安卓 `/sdcard` 的 FUSE 就会这样，报 `EXDEV`）不再白等重试，直接退回「原地覆盖写」。

还有一个**只在 Termux 上才踩得到**的原因：那里装的是 **pydantic 1.x**（`requirements-termux.txt` 故意钉的，因为 pydantic 2 的 `pydantic-core` 要 Rust 编译，普通用户装不动）。pydantic 2 取模型字段用 `model.model_dump()`，1.x 只有 `model.dict()`；代码里直接调 `model_dump()` 时，Termux 上「保存配置」会稳定报 `500`，`detail` 是 `AttributeError: 'ConfigPatch' object has no attribute 'model_dump'`（`POST /api/history`、`/api/downloads`、`/api/bookmarks` 也一样）。现在所有取字段的地方都统一走 `pixiv_bridge/compat.py` 的 `dump_model()`（2.x 用 `model_dump()`，1.x 退回 `dict()`），两个大版本都能跑。要自己复核可以装一套 v1 依赖后跑测试：`pip install -r requirements-termux.txt` 再 `python -m unittest discover -s tests`，主项目的用例应全绿（iwara 的 98 项会跳过，见下）。
> **安卓 / Termux**：请把项目放在 Termux 家目录里（例如 `~/pixiv-bridge`），不要把数据目录指到 `/sdcard` 下的路径。Termux 上依赖会自动换成纯 Python 的 `requirements-termux.txt`（见 INSTALL.md，pydantic 1.x 与 2.x 都支持），与这条路径限制无关。

## 开发自检（测试与边界检查）

```bash
python -m unittest discover -s tests       # 或 python -m pytest tests -q，443 项应全绿
python scripts/check_boundary.py           # 拆分边界自检
python scripts/make_share_packages.py --check   # 分享包内容自检（不写 zip）
```

`scripts/check_boundary.py` 做三件事：用 AST 扫依赖方向与出入口布局，再实际建一次应用对比路由。

1. `pixiv_bridge/` 里没有任何模块 `import novel` / `import iwara` —— 依赖是单向的，主项目不认识子项目；
2. 各子项目只用主项目 `pixiv_bridge`，不依赖本仓库其它本地包；
3. 出入口布局成立：`run.py` 与 `iwara/run.py` 两个接口入口都在，`novel/run.py` 这个专属入口**不存在**（阅读项目改由扩展机制启用），且 `novel/extension.py` 这个扩展入口存在；
4. 各子项目的应用恰好比纯接口多出那几条子项目路由（`novel`（扩展）：`/novel`、`/api/history`、`/api/downloads*`、`/api/bookmarks*`、`/api/config`、`/api/library*`；`iwara_web`（扩展）：`/iwara`；`iwara`：`/api/iwara/*`）。两个扩展那一侧都是通过**扩展机制**建的：`create_app(settings, extensions=load_extension_specs(["novel"], root))`。路由差集里没有 `/`，因为导航页两版都有；`/iwara` 也不算 iwara 包的差集，它归 `iwara_web` 扩展。

改了 imports、挪了路由或动了入口脚本后跑一下它，比等到运行时才 404 省事。测试给 `novel` 那一层的用例统一走 helper（`tests/test_bridge.py`），因为三台账 / 配置 / 书架 / 阅读器页面现在都由子项目挂载，用主项目的 `create_app` 打这些路径会 404。

`iwara` 的用例在 `tests/test_iwara.py`（98 项），全部离线：纯函数直接断言，路由用一个假客户端顶替 `app.state.iwara_client`，不打真实请求（`--check` 那三项也是把 `IwaraClient` 整个换掉再断言）。换 token 那一层也不例外：把真正发请求的 `_call_with_ip_retry` 换成「按队列吐假响应」，就能让 `refresh_access()` 的请求形状（POST `/user/token`、空 body、refresh token 放 `authorization` 头）和 `request()` 收到 403 后的那次强制重放真实跑一遍，不需要账号。因为 `iwara/client.py` 顶层 `import curl_cffi` 而它只写在 `iwara/requirements.txt` 里，新克隆的仓库还没装那个包时，这 98 项会**整片跳过**（显示 `OK (skipped=98)`）而不是报错；装上 `pip install -r iwara/requirements.txt` 后就会真正跑起来。

分享包脚本的用例在 `tests/test_packaging.py`（37 项），同样全部离线、且**不写真实 `dist/`**（用 `patch.object(packaging, "DIST_DIR", 临时目录)` 顶掉）。它盯三类回归：包清单本身（六个包、key 与 zip 名唯一、**每个包里声明的每条目录/文件都真的在盘上**——目录被改名时测试先炸，而不是等打出来的包缺东西）、铺包逻辑（扩展包只带自己、自动跳过 `__pycache__` / `.pyc` / `data/`、缺内容时报 `SystemExit` 并指名道姓）、以及 zip 回读（条目都在同名根文件夹下、根有 `README.md`、`inspect_zip` 能认出字节码与私密数据）。脚本不在任何包里（`scripts/` 没有 `__init__.py`），所以测试用 `importlib` 按路径加载；注意脚本头有 `from __future__ import annotations`，加载时必须**先**把模块登记进 `sys.modules` 再 `exec_module`，否则 dataclasses 解析字符串注解时会去 `sys.modules` 里找一个还不存在的模块，报 `AttributeError: 'NoneType' object has no attribute '__dict__'`。

## 打包与移植（换机 / 离线部署）

项目是自包含的：拷走文件夹 → 在目标机器上 `python run.py`，缺失的 Python 依赖会自动下载安装。

### 一键生成分享包（推荐）

项目现在是四块（两个接口项目 + 两个扩展包），按用途拆成六个分享包：

```bash
python scripts/make_share_packages.py            # 六个包全做，写进 dist/
python scripts/make_share_packages.py --list     # 只列有哪些包
python scripts/make_share_packages.py iwara-api  # 只做一个（按 key 或 zip 名）
python scripts/make_share_packages.py --check    # 只自检内容、不写 zip
```

| key | 压缩包 | 里面是什么 | 怎么用 |
| --- | --- | --- | --- |
| `pixiv_bridge-api` | `pixiv_bridge-api.zip` | 纯 Pixiv 接口项目（含 `openssl-libs/`、`examples/`、启动脚本） | 解压后在目录里 `python run.py` |
| `iwara-api` | `iwara-api.zip` | Pixiv 接口 + `iwara/` 子项目（`/api/iwara/*`） | 解压后 `python iwara/run.py` |
| `novel-extension` | `novel-extension.zip` | 小说阅读器扩展（`novel/`） | **不能单独启动**：把包内 `novel/` 拷进接口项目根，`.env` 写 `PIXIV_BRIDGE_EXTENSIONS=novel` |
| `iwara_web-extension` | `iwara_web-extension.zip` | iwara 视频页面扩展（`iwara_web/`） | **不能单独启动**：把包内 `iwara_web/` 拷进 `iwara-api` 项目根，`.env` 写 `PIXIV_BRIDGE_EXTENSIONS=iwara_web`（要配 `iwara/run.py` 入口） |
| `pixiv_bridge-novel` | `pixiv_bridge-novel.zip` | 接口 + 阅读器，已配好 | 解压后在目录里 `python run.py`，`.env` 写 `PIXIV_BRIDGE_EXTENSIONS=novel` |
| `iwara-iwara_web` | `iwara-iwara_web.zip` | iwara 接口 + 视频页面，已配好 | 解压后 `python iwara/run.py`，`.env` 写 `PIXIV_BRIDGE_EXTENSIONS=iwara_web` |

每个包内都有**自己的一份 `README.md`**（写清这是接口包还是扩展包、怎么启动、要不要写 `PIXIV_BRIDGE_EXTENSIONS`），手册（`README.md`、`INSTALL.md`、`DIRECT.md`）统一放在包内 `手册/` 子目录里，不会和包自己的 README 打架。包内不含 `.env`、`data/`、`tokens.json`、`.venv/`、`__pycache__/`、`_scratch/`、`_tmp_tests/`、`dist/`。

> `dist/` 已在 `.gitignore` / `.dockerignore` 里，不入库——需要分享包时现做一份。**扩展包不能单独启动**：它没有 `run.py`，只提供路由与页面，必须拷进对应接口项目、由 `PIXIV_BRIDGE_EXTENSIONS` 启用。

### 该拷什么、不该拷什么

| 内容 | 建议 | 原因 |
| --- | --- | --- |
| `pixiv_bridge/`、`run.py`、`requirements.txt`、`README.md` | **必须拷** | 接口本体（只要接口、不要阅读器时，拷这些就够） |
| `novel/`（含 `novel/extension.py`）、`novel/web/index.html` | **要阅读器就必须拷** | 小说阅读子项目；漏拷时 `/novel` 直接 404、书架那几个接口不可用。拷过去后在 `.env` 里写 `PIXIV_BRIDGE_EXTENSIONS=novel` 启用 |
| `iwara/`、`iwara/requirements.txt` | **要 iwara 就必须拷** | iwara 子项目（视频接口）；漏拷时 `/api/iwara/*` 全部 404。首次启动会自动补装 `curl_cffi` |
| `iwara_web/` | **要 iwara 视频页面就必须拷** | iwara 的播放页扩展（含 `iwara_web/index.html`）；漏拷时 `/iwara` 是 404（`/api/iwara/*` 不受影响）。拷过去后在 `.env` 里写 `PIXIV_BRIDGE_EXTENSIONS=iwara_web` 启用（它调 iwara 的接口，所以 `iwara/` 也得在） |
| `start.bat`、`start.sh`、`get_token.py`、`INSTALL.md`、`DIRECT.md`、`requirements-termux.txt` | **建议拷** | 一键启动脚本、登录助手、傻瓜版说明、直连思路文档、Termux 专用依赖清单 |
| `.env.example` | **必须拷** | 配置模板 |
| `openssl-libs/` | **要 ECH 就必须拷** | 项目自带的 OpenSSL 4.x 动态库（`win32-amd64` + `android-arm64`，按 `平台-架构` 分子目录，约 16 MB）；拷过去即可用 ECH，无需在目标机器安装 OpenSSL。不用 ECH 时可以不拷 |
| `.env`、`data/`（含 `data/tokens.json`） | **不要拷** | 里面是 refresh token / access token 与你的阅读、下载、收藏记录，属私密数据且与账号绑定；目标机器上重新填 `.env` 即可 |
| 项目根的 `tokens.json`、`history.jsonl` | **不要拷** | 老版本的存放位置；确实想把进度带过去时，改拷 `data/` 目录（或单独拷 `data/history.jsonl`，一行一条 JSON，删掉即清空） |
| `.venv/`、`venv/` | **不要拷** | 虚拟环境里写死了源机器的绝对路径，拷过去必坏；目标机器上重新 `python -m venv .venv`（或干脆不用 venv） |
| `__pycache__/`、`*.pyc` | **不要拷** | 与 Python 版本绑定，没有意义 |
| `tests/`、`scripts/`、`examples/` | 可选 | 只是测试、自检脚本与调用示例 |

一键打包（排除不该带的东西）：

Windows（PowerShell，先 `robocopy` 暂存一份干净的副本再压缩，这样任意深度的 `__pycache__/` 都会被排除）：

```powershell
$stage = "$env:TEMP\pixiv-bridge"   # 目录名会成为压缩包里的根文件夹，保持 pixiv-bridge
if (Test-Path $stage) { Remove-Item $stage -Recurse -Force }
robocopy . $stage /E /XD .venv venv __pycache__ dist _tmp_tests data /XF .env tokens.json history.jsonl *.pyc /NFL /NDL /NJH /NJS /NP
Compress-Archive -Path $stage -DestinationPath "dist\pixiv-bridge.zip" -Force
Remove-Item $stage -Recurse -Force
```

> `robocopy` 退出码 1 表示「已成功复制文件」，是正常结果。
>
> 不要用 `Compress-Archive -Path (Get-ChildItem -Exclude ...)`：`Get-ChildItem -Exclude` 只作用于**顶层**条目，会把 `pixiv_bridge\__pycache__\*.pyc` 一起打进包；而 `Compress-Archive -Path <完整文件路径列表>` 还会把目录结构压平（所有文件挤到压缩包根目录）。

Linux / macOS（Windows 10 1803+ 自带的 `tar` 也可以直接用这条命令）：

```bash
tar --exclude=.venv --exclude=venv --exclude=__pycache__ --exclude=.env \
    --exclude=tokens.json --exclude=history.jsonl --exclude=data --exclude=dist --exclude=_tmp_tests \
    --exclude=.git --exclude='*.pyc' -czf dist/pixiv-bridge.tar.gz .
```

> 产物也可以像上面那样写进 `dist/`（`dist` 已在排除列表里，不会被 tar 打进包）；写成 `../pixiv-bridge.tar.gz` 也一样，关键是**别写到会被扫到的路径**，否则 tar 会把这个正在写入的压缩包自己也算进去。
>
> `dist/` 已在 `.gitignore` 里，不入库——需要分享包时用上面这两条命令现做一份。**直接从版本库的源码改动生成**，别用旧的包：拆分之前打的包里还是 `web/index.html`、`pixiv_bridge/history.py` 那一套旧结构，拷给别人会跑不起来。

打包后建议确认一眼包内没有 `.env`、`tokens.json`、`history.jsonl`、`data/`、`.venv/`、`__pycache__/`，并且含 `novel/web/index.html`、`iwara_web/index.html` 与 `openssl-libs/`：

```bash
tar -tzf ../pixiv-bridge.tar.gz | grep -E '\.env$|tokens\.json$|history\.jsonl$|downloads\.jsonl$|bookmarks\.jsonl$|settings\.json$|^\./data/|\.venv/|__pycache__/'   # 无输出最好
tar -tzf ../pixiv-bridge.tar.gz | grep -E 'novel/web/index\.html|iwara_web/index\.html'                                                               # 应该有
tar -tzf ../pixiv-bridge.tar.gz | grep -E 'openssl-libs/.*(libssl|libcrypto)'                                                                        # 要用 ECH 就该有
```

（Windows PowerShell 里把 `grep -E` 换成 `Select-String -Pattern`。）

> `.env`、`tokens.json`、`data/`、`.venv/`、`__pycache__/` 已在 `.gitignore` / `.dockerignore` 中排除。

### 目标机器需要什么（能自动下载 / 不能自动下载）

| 需求 | 能否自动获取 | 说明 |
| --- | --- | --- |
| Python **3.10+** | ❌ **必须自行安装** | 解释器本身无法由本项目安装；版本不符时启动即中止（`python` 检查项） |
| `pip` | ✅ 自动尝试 | 系统 Python 缺 pip 时会自动执行 `python -m ensurepip --upgrade`；仍失败则提示手动装（如 `apt install python3-pip`） |
| fastapi / uvicorn / httpx | ✅ **自动下载** | 首次运行自动 `pip install -r requirements.txt`，按 清华 → 阿里云 → PyPI 依次尝试；`PIXIV_BRIDGE_PIP_INDEX` 可指定内网镜像 |
| 本地 wheel 离线包 | ✅ 离线可用 | 见下节「完全离线部署」 |
| Pixiv refresh_token | ❌ **必须自行提供** | 用户凭据，见 [获取 refresh_token](#获取-refreshtoken三种方式任选) |
| 能访问 Pixiv | ❌ 需环境配合 | 直连 / SNI 绕过 / ECH / 代理四选一，见 [原理](#原理) |
| **OpenSSL 4.0+**（仅 ECH 模式需要） | ✅ **项目自带** | ECH 需要 libssl 提供 `SSL_set1_ech_config_list`（Python 自带的 ssl 模块没有 ECH；该 API 是 **4.0.0** 才加入的，3.x 全系没有）。`openssl-libs/` 里已经带了 Windows 与安卓 Termux 两个平台的 4.x 库，拷包即用，不必在目标机器安装；也可另装系统 OpenSSL 4.0+ 作为后备。不开 ECH 时完全不需要 |
| Docker（仅容器部署需要） | ❌ 需自行安装 | 见 [Docker](#docker) |

### 完全离线部署

在有网的机器上把依赖下载成 wheel 包，和项目一起拷过去：

```bash
python -m pip download -r requirements.txt -d wheels
```

把 `wheels/` 目录放在项目根目录（与 `requirements.txt` 同级；也可命名为 `vendor/` 或 `pip-wheels/`），目标机器启动时会**优先**执行
`pip install --no-index --find-links wheels -r requirements.txt` 离线安装，完全不联网；本地包不全时再自动回退到各镜像源。

> ⚠️ wheel 包与 **操作系统 + CPU 架构 + Python 版本**绑定（例如 `pydantic_core-…-cp313-cp313-win_amd64.whl` 只能装在 64 位 Windows 的 CPython 3.13 上）。
> 要装到别的平台，请带上目标平台参数下载，例如给 Linux x86_64 / Python 3.12 准备：
>
> ```bash
> python -m pip download -r requirements.txt -d wheels \
>   --platform manylinux2014_x86_64 --python-version 312 --implementation cp --abi cp312 --only-binary=:all:
> ```
>
> 最省事的做法是**直接在目标机器上**执行一次 `pip download`（先手动装好 pip），或让目标机器联网自动安装。

### OpenSSL 4.0+（仅 ECH 模式，通常不用管）

**项目自带，一般不需要做任何事。** `openssl-libs/` 里按 `平台-架构` 存放了 OpenSSL 4.x 的 `libssl` / `libcrypto`（Windows 是 `openssl-libs/win32-amd64/`，安卓/Termux 是 `openssl-libs/android-arm64/`），`ech_client.py` 会优先加载它。真正需要动手的只有两种情况：

- **目标平台没有对应的子目录**（例如包里只有 `win32-amd64`，却要跑在 Linux 上）：要么在该机器上装 OpenSSL **4.0+** 让程序自动找到，要么自己补一份——把目标平台的 `libssl` / `libcrypto` 放进 `openssl-libs/<系统>-<架构>/` 即可，命名沿用 `libssl-4-x64.dll` / `libssl.so.4` / `libssl.4.dylib` 这类常见名（完整认名列表见 `ech_client.py` 的 `_LIBSSL_NAMES` / `_LIBCRYPTO_NAMES`）。
  - 目录名就是 `<系统>-<架构>`：`sys.platform` 的字母前缀（`win32`、`linux`、`darwin`、`android`、`freebsd`…）配 `platform.machine()`，例如 `linux-arm64`、`darwin-arm64`；架构别名会一并尝试（`aarch64` ↔ `arm64`、`x86_64` ↔ `amd64`），也可以只写系统名（`linux/`）当作该系统的通用兜底。
  - **Termux 特例**：`sys.platform` 在 Termux 上是 `linux`，但程序识别 `$PREFIX` 后会按 **`android`** 找目录，因此用的是 `android-arm64/`。**不要**指望 `pkg install openssl`——官方源是 3.6.5，实测导出的 ECH 符号数为 0。
- **自带的这份载入失败**（架构不符、缺 VC 运行库等）：程序会自动往系统安装的位置继续找，不会直接判死；`python run.py --check` 失败时会列出每个候选各自的失败原因。

系统安装（作为后备）的常规做法——**注意发行版仓库里目前基本都是 3.x，装了也没有 ECH**：

- **Windows**：安装 [Win64 OpenSSL **v4.x**](https://slproweb.com/products/Win32OpenSSL.html)（默认 `C:\Program Files\OpenSSL-Win64`，程序会自动找到）；便携版或自定义目录用 `PIXIV_BRIDGE_OPENSSL_DIR` 指向其 `bin` 目录，或把该目录加进 `PATH`
- **Ubuntu / Debian / Fedora / RHEL**：仓库里的 `libssl3` / `openssl-libs` 都是 3.x，**不含 ECH**；要用 ECH 只能自备 4.0+ 的 `libssl` 并放进 `openssl-libs/linux-<架构>/`（程序会按当前架构找 `/usr/lib/x86_64-linux-gnu`、`/usr/lib/aarch64-linux-gnu`、`/usr/lib64` 等位置，若要装进系统目录则需自行编译）
- **macOS**：`brew install openssl@4`，必要时 `export PIXIV_BRIDGE_OPENSSL_DIR=$(brew --prefix openssl@4)/lib`；Homebrew 的 `openssl@4` 是 keg-only、不会链进 `lib/`，但程序会主动去 `$(brew --prefix)/opt/openssl@4/lib` 找
- **BSD**：`pkg install openssl`（若有 4.0+ 版本），库在 `/usr/local/lib`
- **Arch / 其它**：库在 `/usr/lib`、`/lib`、`/usr/local/lib` 等常规位置，但要确认是 4.0+
- **Docker**：`python:3.12-slim` 这类 Debian bookworm 基础镜像自带 OpenSSL 3.0，**不含 ECH**；容器里要用 ECH 要么把 Linux 版的库放进 `openssl-libs/linux-x86_64/` 再 COPY 进镜像（当前仓库带的是 `win32-amd64` 与 `android-arm64`），要么换自带 OpenSSL 4.0+ 的基础镜像，或改用 `PIXIV_BRIDGE_BYPASS_SNI=1` / `PIXIV_PROXY`

> `openssl-libs/` 里的库来自 [OpenSSL 官方发行版](https://slproweb.com/products/Win32OpenSSL.html) 与 [TaurusTLS OpenSSL-Distribution](https://github.com/TaurusTLS-Developers/OpenSSL-Distribution)，许可证是 **Apache License 2.0**，各子目录随包附带了 `LICENSE.txt`。

装好后 `python run.py --check` 的 `ech` 项应为 `OK`；找不到时会显示 `WARN` 并在提示里指回本节。

### 目标机器上首次运行

```bash
cd pixiv-bridge
cp .env.example .env      # Windows: copy .env.example .env
# 编辑 .env：填入 PIXIV_REFRESH_TOKEN（以及需要的 PIXIV_BRIDGE_ECH 等）
# 要小说阅读器的话，再加一行 PIXIV_BRIDGE_EXTENSIONS=novel
python run.py --check     # 期望：python / deps / files / ech(或 network) 全 OK + 登录成功（port 只警告）
python run.py             # 启动（配了扩展就一起挂上；浏览器打开 http://127.0.0.1:8765/ 是导航页）
# 想连 iwara 一起用就换入口：python iwara/run.py --check / python iwara/run.py
```

## 部署

### Docker

```bash
docker build -t pixiv-bridge .
docker run -d -p 8765:8765 \
  -v $(pwd)/data:/app/data \
  -e PIXIV_REFRESH_TOKEN=你的token \
  pixiv-bridge
```

> Docker 镜像里默认跑的是**纯接口**入口（`CMD python run.py`）。想在容器里用阅读器，`docker run` 时加一个环境变量 `-e PIXIV_BRIDGE_EXTENSIONS=novel`（镜像已 `COPY novel novel`，不用改镜像）；阅读器在 `/novel`。

### 局域网访问

服务持有你的 Pixiv 账号令牌，暴露到局域网有安全风险，默认只监听 `127.0.0.1`。
确需局域网调用时：

```bash
PIXIV_BRIDGE_HOST=0.0.0.0 .venv/bin/python run.py          # 纯接口
PIXIV_BRIDGE_HOST=0.0.0.0 PIXIV_BRIDGE_EXTENSIONS=novel .venv/bin/python run.py   # 接口 + 阅读器（手机浏览器可用）
```

建议放在反向代理（Nginx/Caddy）后面并加访问密钥。

## 合规与免责

- 本项目仅供个人学习与自动化研究使用，请遵守 [Pixiv 服务条款](https://policies.pixiv.net/) 与所在地区法律
- 控制请求频率（默认已限流），不要用于批量抓取、镜像站或商业用途
- R-18 内容的可见性取决于账号设置与登录状态

## 致谢与许可

- 接口签名算法、OAuth 流程与端点清单参考 [PixEz (Notsfsssf/pixez-flutter)](https://github.com/Notsfsssf/pixez-flutter)（GPL-3.0）及社区公开资料
- 本项目为独立实现的 Python 服务，不包含 PixEz 源码
