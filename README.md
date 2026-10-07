# Pixiv Bridge — Pixiv 直连接口服务

基于开源项目 [PixEz (pixez-flutter)](https://github.com/Notsfsssf/pixez-flutter) 逆向公开的
Pixiv 官方 App API（`app-api.pixiv.net`）调用方式（请求签名、OAuth 认证、全部端点），用 Python 重新实现的一个
**自托管 HTTP 接口服务**。启动后，任何语言的代码通过普通 HTTP 请求即可调用 Pixiv 数据能力，无需处理签名、token 刷新、防盗链等细节。

> 项目分两块：**`pixiv_bridge`**（接口本体，只要接口就够用）与 **`novel`**（小说阅读子项目，依赖前者，多带一个网页阅读器与阅读/下载/收藏台账）。两个入口：`run.py` 纯接口、`novel/run.py` 接口 + 阅读器，详见「快速开始」。

## 特性

- **免代理直连（双模式）**：默认标准 HTTPS 直连；可开启 SNI 绕过直连（复刻 PixEz：关闭 TLS SNI + 硬编码官方 IP 池），适配 DNS 污染 / SNI 阻断型网络
- **自动认证**：自动刷新 access token（Pixiv 的 refresh token 会轮换，刷新后自动持久化到 `tokens.json`）
- **完整接口**：插画详情/排行/推荐/搜索/相关推荐、用户/收藏/关注、小说、动图元数据、评论、Spotlight 等 55 个端点
- **图片代理**：`i.pximg.net` 有 Referer 防盗链，服务内置图片代理，前端/脚本可直接引用直链
- **统一分页**：所有列表接口返回 `next_url`，任意接口可传 `next_url` 继续翻页
- **内置限流**：令牌桶限流，避免触发 Pixiv 风控（默认 100 请求/分钟，可配置）
- **启动前自动检查环境**：检查 Python 版本 / 依赖 / 网络连通性 / 端口，依赖缺失时自动安装
- **可选的小说阅读器**（子项目 `novel/`，用 `python novel/run.py` 启动）：浏览器打开 `http://127.0.0.1:8765/` 即可搜作品/搜作者、看榜单/推荐/关注/追更/收藏、进作者主页关注或取关、连续阅读、正文插图内联显示（多来源兜底取图）、导出 EPUB（一本书一个文件，含插图与目录；零依赖单文件页面，已做移动端适配，手机浏览器可用）。接口本体不需要它，用 `run.py` 启动时这个地址是 404
- **阅读与下载都有账**：阅读历史（`data/history.jsonl`）、下载历史（`data/downloads.jsonl`，**按章节记**）、收藏台账（`data/bookmarks.jsonl`）都落在项目里；书架分「云端记录 / 下载记录 / 本地文件」三类，同名系列的章节可在书架里**一键合并导出**成一个 EPUB
- **数据都在 `data/`**：token、阅读历史、下载记录、收藏台账、页面配置统一放在项目下的 `data/` 目录（老版本的 `tokens.json` / `history.jsonl` 首次启动自动搬过去），换机只需拷这一个目录
- **OpenAPI 文档**：启动后访问 `http://127.0.0.1:8765/docs` 在线调试

## 原理

Pixiv 官方 App 通过 `app-api.pixiv.net` 提供数据，请求需要：

1. **签名头**：`X-Client-Time` + `X-Client-Hash`（`md5(时间 + 固定盐)`）
2. **OAuth 令牌**：`Authorization: Bearer <access_token>`，access token 约 1 小时过期，用 refresh token 刷新
3. **移动端请求头**：`User-Agent`、`App-OS`、`App-Version` 等伪装为官方 Android 客户端

这些细节与端点清单均来自 PixEz 源码（`lib/network/api_client.dart`、`oauth_client.dart`），本项目在 Python 中重新实现，方便被任意代码调用。

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
2. **调用 OpenSSL 3.5+ 的 `SSL_set1_ech_config_list`**：Python 自带的 `ssl` 模块尚未暴露 ECH，故用 `ctypes` 直接调用系统 libssl（`ech_client.py`）
3. **只提供 TLS 1.3**：Cloudflare 对含 ECH 且允许 TLS 1.2 的 ClientHello 直接回 `illegal_parameter`，因此必须把 `SSL_CTX` 的 min/max 版本都钉在 TLS1.3
4. **外层用 `cloudflare-ech.com`、内层用真实域名**：certificate 校验按真实域名做（`SSL_set1_host`）
5. **加密 SNI 里的目标域名仍会被中间设备看到吗**——不会；但外层名字仍需能通过，故入口 IP 用 Cloudflare 的 anycast 地址（内置池 `104.18.10.118,104.18.11.118,104.18.42.239,104.16.132.229`）

开启方式：

```
PIXIV_BRIDGE_ECH=1
PIXIV_BRIDGE_BYPASS_SNI=0     # 两者只需开一个，ECH 优先
```

前提：系统里存在 **OpenSSL 3.5 及以上**的 libssl（Windows 装 OpenSSL 3.5 后默认在 `C:\Program Files\OpenSSL-Win64\bin`；Linux 需 `libssl.so.3`，Debian/Ubuntu 24.04 之后自带；macOS 可用 `brew install openssl@3`）。程序按 `PIXIV_BRIDGE_OPENSSL_DIR` → PATH → 常见安装路径 的顺序查找。启动检查里的 `ech` 一项显示 `ECH 传输可用` 即表示可用；若显示未找到 libssl，请改装 OpenSSL 3.5+ 或改用无 SNI 模式 / 代理。

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

# 只要接口（不带阅读器页面）
.venv/bin/python run.py                                 # Windows: .venv\Scripts\python run.py

# 接口 + 小说阅读器（多了 `/` 页面与阅读历史 / 下载 / 收藏 / 书架那几个接口）
.venv/bin/python novel/run.py
```

项目分成两块，**子项目依赖主项目**、单向依赖不反过来：

| 入口 | 提供什么 |
| --- | --- |
| `run.py` | 接口主项目（`pixiv_bridge`）：认证、插画、搜索、动态、用户、`/api/novel/*`、图片代理、健康检查。不带阅读器页面 |
| `novel/run.py` | 接口之上再挂小说阅读子项目（`novel`）：阅读器页面 `/` 与 `/api/history`、`/api/downloads`、`/api/bookmarks`、`/api/config`、`/api/library` |

两个入口共用同一套命令行（`--host` / `--port` / `--reload` / `--check` / `--env`）与环境检查。只要接口的话，拷 `pixiv_bridge/` 一个包即可；要阅读器就连 `novel/` 一起拷（`novel` 通过 `import pixiv_bridge.*` 用主项目，主项目不认识 `novel`）。

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

每次启动前自动执行 5 项检查：

| 检查项 | 失败影响 | 说明 |
| --- | --- | --- |
| `python`（版本 ≥ 3.10） | **中止启动** | 代码使用了 `list[str] \| None` 语法 |
| `deps`（fastapi/uvicorn/httpx） | **自动安装后复查**，仍失败则中止 | 用 `pip install -r requirements.txt` 自动下载；有本地 `wheels/` 时优先离线安装 |
| `files`（`run.py` / `requirements.txt`） | 仅警告，不中止 | 拷贝项目时漏文件的自检（阅读器页面属于 `novel/`，不在接口项目的清单里） |
| `port`（监听端口） | **中止启动** | 提示换端口（`--port` 或 `PIXIV_BRIDGE_PORT`） |
| `network` / `ech` / `proxy`（Pixiv 域名连通性） | 仅警告，不中止 | 三选一：设置 `PIXIV_PROXY` 时真发一次经代理的请求（`proxy` 项）；开启 `PIXIV_BRIDGE_ECH=1` 时真发一次 ECH 请求（`ech` 项）；否则用内置 IP 池 + 无 SNI TLS 探测（`network` 项）。不可达时按当前模式给出改 ECH / 改无 SNI / 改代理的提示，ECH 被 RST 时会直接点明“当前网络在阻断 ECH 扩展” |

跳过端口检查（仅校验登录）：`python run.py --check`（阅读器入口是 `python novel/run.py --check`）。

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

# 接口 + 小说阅读器（同一个 --host / --port 用法）
.venv/bin/python novel/run.py
```

> Windows 的 `start.bat` / Termux 的 `start.sh` 对应的是第一行那个**纯接口**入口。

验证：

```bash
curl http://127.0.0.1:8765/api/health
curl http://127.0.0.1:8765/api/auth/status
```

接口就到此可用了。上面这个入口**不带网页**：打开 <http://127.0.0.1:8765/> 会是 404（正常）。要小说阅读器页面，改用另一个入口 `python novel/run.py` 启动，再打开同一个地址（详见「Web 页面」）。

## 项目结构

```
pixiv-bridge/
├── run.py / pixiv_bridge/__main__.py   纯接口入口（先自动装依赖，再启动）
├── start.bat / start.sh                一键启动脚本（跑的是上面的纯接口入口）
├── get_token.py                        交互式获取 refresh_token（浏览器授权 / 账号密码）
├── requirements.txt                    依赖清单（Termux 自动改用 requirements-termux.txt）
├── requirements-termux.txt             Termux 专用：纯 Python 的 pydantic 1.x（无需 Rust 编译）
├── INSTALL.md                          傻瓜版安装配置说明（Windows / 安卓 Termux）
├── novel/                              小说阅读子项目（依赖 pixiv_bridge，入口 novel/run.py）
│   ├── __init__.py  说明本子项目依赖主项目；对外入口是 app.create_app
│   ├── run.py          完整服务入口（接口 + 阅读器；主项目的命令行之上挂自己的路由）
│   ├── app.py          组装：台账/配置/书架挂到 app.state，再挂本子项目路由
│   ├── history.py      阅读历史（JsonlStore 子类）
│   ├── downloads.py    下载历史（按「书号#章号」记账，可按系列汇总进度）
│   ├── bookmarks.py    收藏台账（删除写墓碑行，收藏状态可回溯）
│   ├── web/index.html  内置 Web 页面：个人向小说阅读器（搜作品/搜作者 / 榜单 / 推荐 / 关注 / 追更 / 收藏 / 书架三段 / 阅读器含插图 / EPUB 导出）
│   └── routes/         本子项目的路由（history / downloads / bookmarks / config / web）
├── data/                               你的全部个人数据（换机只拷这一个目录；可用 PIXIV_DATA_DIR 换位置）
│   ├── tokens.json                     登录凭据（首次启动自动从项目根搬进来）
│   ├── settings.json                   页面配置（本地书架目录、合并/跳过开关、扫描层数）
│   ├── history.jsonl                   阅读历史（一行一条 JSON）
│   ├── downloads.jsonl                 下载历史（一行一条 JSON，按章节记）
│   └── bookmarks.jsonl                 收藏台账（一行一条 JSON，删一条写墓碑行）
├── pixiv_bridge/                       接口主项目（纯接口，不带阅读器页面）
│   ├── deps.py         依赖检查与自动下载安装（支持本地 wheels/ 离线包）
│   ├── envcheck.py     运行前环境检查（Python / 依赖 / 项目文件 / 网络 / 端口）
│   ├── config.py       配置加载（.env / 环境变量 / data 目录与旧数据迁移）
│   ├── compat.py       pydantic 大版本兼容（v2 的 model_dump / v1 的 dict）
│   ├── oauth.py        OAuth 认证与 token 自动刷新
│   ├── api.py          App API 客户端（签名 + 全部端点）
│   ├── jsonl.py        通用 JSON Lines 存取（追加写 + 读时折叠 + 超限压缩，各台账共用）
│   ├── userdata.py     data 目录 / 旧数据迁移 / 用户配置 / 本地书架文件扫描
│   ├── hoster.py       Pixiv 官方 IP 池 + Cloudflare ECH 入口 IP（可配置覆盖）
│   ├── sni_client.py   无 SNI TLS 直连客户端（对应 PixEz sni:false）
│   ├── ech_client.py   ECH（加密 SNI）直连客户端（ctypes 调用 OpenSSL 3.5+）
│   ├── rate_limit.py   令牌桶限流
│   ├── image_proxy.py  图片防盗链代理
│   ├── cli.py          命令行入口（两个入口共用：--host / --port / --reload / --check / --env）
│   ├── server.py       FastAPI 应用组装（lifespan + 异常处理 + 挂载纯接口路由）
│   └── routes/         接口路由模块（按领域拆分）
│       ├── auth.py     认证接口
│       ├── illust.py   插画 / 收藏 / 评论
│       ├── search.py   搜索
│       ├── feed.py     排行 / 推荐 / 关注流 / 热门标签
│       ├── user.py     用户 / 关注
│       ├── novel.py    小说 / 追更（/api/novel/* 属于接口层，仍在主项目）
│       └── misc.py     健康检查 / 通用翻页 / 图片代理
├── tests/              单元测试（218 项）
├── examples/           Python / curl 调用示例
└── scripts/            自检脚本
    └── check_boundary.py  拆分边界自检（主项目不得 import novel + 两个入口的路由差集）
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
| `PIXIV_BRIDGE_OPENSSL_DIR` | 空 | 含 OpenSSL 3.5+ `libssl`/`libcrypto` 的目录（**仅 ECH 模式需要**；留空按 PATH → 常见安装路径查找） |
| `PIXIV_PROXY`（别名 `PIXIV_BRIDGE_PROXY`） | 空 | 如 `http://127.0.0.1:7890`；设置后所有 Pixiv 请求（API / OAuth / 图片 / 自检）都走代理，**优先于 ECH 与 SNI 绕过**，见「代理通道」 |

## Web 页面（小说阅读器）

小说阅读子项目（`novel/`）内置单文件页面（`novel/web/index.html`，零依赖、无构建步骤）。用 `python novel/run.py` 启动后，浏览器打开 <http://127.0.0.1:8765/> 即可使用。它是**个人向的 Pixiv 小说阅读器**：只做「找小说 → 读小说 → 存/导出」这一条链路，插画相关的界面已移除。

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
- 手机上访问用 `http://<电脑局域网 IP>:8765/`（后端需以 `HOST=0.0.0.0` 启动，见「局域网访问」）。

打开小说报错时怎么看（页面已内置提示）：

- **「接口不存在（404）」**：当前运行的服务进程里没有 `/api/novel/<id>` 这个接口——即进程是旧版本（页面每次请求都读盘、会自动更新，但**路由不会**）。停掉服务、用最新代码重启即可。
- **「Pixiv 返回 404」**：上游真的找不到这个 id，常见原因：这个 id 其实是**系列 id**（页面会自动尝试按系列打开一次）、作品已删除 / 转私密、或者 R-18 等需要先在 Pixiv 侧确认年龄的作品。
- **「正文接口返回 404」**：书名 / 作者 / 字数能显示，只有正文取不到。Pixiv 已在 2024 年 2 月的 App 更新里**删掉了旧正文接口 `/v1/novel/text`**（只会回一句 `Specified end-point doesn't exist`），本服务改从官方 iOS 客户端的 `/webview/v2/novel` 页面里取正文；若这里仍报 404，多半是这本作品本身对当前账号不可读（R-18 未确认年龄 / 本文非公开 / 已删除）。
- 两种情况都会附一段「诊断信息」（时间 / 页面地址 / 接口 / HTTP 状态 / 上游 message 与 body），点「复制诊断信息」即可整段复制反馈。

小说封面图与正文插图都通过服务内置的图片代理加载（自动附加 Referer），无需额外配置；封面加载失败时用书名首字兜底，插图加载失败时在原位显示提示文字。

> 说明：正文与导出内容仅供个人阅读使用；页面不写入任何后端数据，前端状态都停留在本机浏览器（`localStorage`）。

## 接口清单

所有接口返回 Pixiv 原始 JSON（透传），错误时返回 `{"error": {"message": "...", "reason": "..."}}`。

> 下面「认证」到「其他（接口主项目）」这些，`run.py` 与 `novel/run.py` 两个入口都有；最后一节「台账与书架」只有 `novel/run.py` 才有。

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

### 台账与书架（**仅 `novel/run.py` 提供**）

下面这些不是接口的一部分，而由小说阅读子项目 `novel/` 挂载；用 `run.py`（或 `start.bat` / `start.sh`）启动时它们都返回 **404**，要看下面的说明请改用 `novel/run.py`：

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

还有一个**只在 Termux 上才踩得到**的原因：那里装的是 **pydantic 1.x**（`requirements-termux.txt` 故意钉的，因为 pydantic 2 的 `pydantic-core` 要 Rust 编译，普通用户装不动）。pydantic 2 取模型字段用 `model.model_dump()`，1.x 只有 `model.dict()`；代码里直接调 `model_dump()` 时，Termux 上「保存配置」会稳定报 `500`，`detail` 是 `AttributeError: 'ConfigPatch' object has no attribute 'model_dump'`（`POST /api/history`、`/api/downloads`、`/api/bookmarks` 也一样）。现在所有取字段的地方都统一走 `pixiv_bridge/compat.py` 的 `dump_model()`（2.x 用 `model_dump()`，1.x 退回 `dict()`），两个大版本都能跑。要自己复核可以装一套 v1 依赖后跑测试：`pip install -r requirements-termux.txt` 再 `python -m unittest discover -s tests`，218 项应全绿。

> **安卓 / Termux**：请把项目放在 Termux 家目录里（例如 `~/pixiv-bridge`），不要把数据目录指到 `/sdcard` 下的路径。Termux 上依赖会自动换成纯 Python 的 `requirements-termux.txt`（见 INSTALL.md，pydantic 1.x 与 2.x 都支持），与这条路径限制无关。

## 开发自检（测试与边界检查）

```bash
python -m unittest discover -s tests       # 或 python -m pytest tests -q，218 项应全绿
python scripts/check_boundary.py           # 拆分边界自检
```

`scripts/check_boundary.py` 用 AST 扫两件事，再实际建一次应用对比路由：

1. `pixiv_bridge/` 里没有任何模块 `import novel` —— 依赖是单向的，主项目不认识子项目；
2. `novel/` 只用主项目 `pixiv_bridge`，不依赖本仓库其它本地包；
3. `novel` 的应用恰好比纯接口多出那几条子项目路由（`/`、`/api/history`、`/api/downloads*`、`/api/bookmarks*`、`/api/config`、`/api/library*`）。

改了 imports 或挪了路由后跑一下它，比等到运行时才 404 省事。测试给 `novel` 那一层的用例统一走 helper `app_with_novel(settings)`（`tests/test_bridge.py`），因为三台账 / 配置 / 书架 / 阅读器页面现在都由子项目挂载，用主项目的 `create_app` 打这些路径会 404。

## 打包与移植（换机 / 离线部署）

项目是自包含的：拷走文件夹 → 在目标机器上 `python run.py`，缺失的 Python 依赖会自动下载安装。

### 该拷什么、不该拷什么

| 内容 | 建议 | 原因 |
| --- | --- | --- |
| `pixiv_bridge/`、`run.py`、`requirements.txt`、`README.md` | **必须拷** | 接口本体（只要接口、不要阅读器时，拷这些就够） |
| `novel/`、`novel/web/index.html` | **要阅读器就必须拷** | 小说阅读子项目；漏拷时 `/` 直接 404、书架那几个接口不可用 |
| `start.bat`、`start.sh`、`get_token.py`、`INSTALL.md`、`requirements-termux.txt` | **建议拷** | 一键启动脚本、登录助手、傻瓜版说明、Termux 专用依赖清单 |
| `.env.example` | **必须拷** | 配置模板 |
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

打包后建议确认一眼包内没有 `.env`、`tokens.json`、`history.jsonl`、`data/`、`.venv/`、`__pycache__/`，并且含 `novel/web/index.html`：

```bash
tar -tzf ../pixiv-bridge.tar.gz | grep -E '\.env$|tokens\.json$|history\.jsonl$|downloads\.jsonl$|bookmarks\.jsonl$|settings\.json$|^\./data/|\.venv/|__pycache__/'   # 无输出最好
tar -tzf ../pixiv-bridge.tar.gz | grep -E 'novel/web/index\.html'                                                                                    # 应该有
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
| **OpenSSL 3.5+**（仅 ECH 模式需要） | ❌ **无法 pip 安装** | ECH 需要系统 `libssl`/`libcrypto` 提供 `SSL_set1_ech_config_list`（Python 自带的 ssl 模块没有 ECH）；不开 ECH 时完全不需要 |
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

### OpenSSL 3.5+ 安装（仅 ECH 模式）

`pip install` 拿不到 ECH 能力，必须让系统里有 OpenSSL 3.5+：

- **Windows**：安装 [Win64 OpenSSL v3.5+](https://slproweb.com/products/Win32OpenSSL.html)（默认 `C:\Program Files\OpenSSL-Win64`，程序会自动找到）；便携版或自定义目录用 `PIXIV_BRIDGE_OPENSSL_DIR` 指向其 `bin` 目录，或把该目录加进 `PATH`
- **Ubuntu / Debian**：`sudo apt install libssl3`（发行版自带 ≥ 3.5 即可；较老的发行版需升级系统、加官方源或自行编译）
- **macOS**：`brew install openssl@3`（版本不足 3.5 需先 `brew upgrade`），必要时 `export PIXIV_BRIDGE_OPENSSL_DIR=$(brew --prefix openssl@3)/lib`
- **Docker**：`python:3.12-slim` 这类 Debian bookworm 基础镜像自带 OpenSSL 3.0，**不含 ECH**；容器内要用 ECH 需换自带 OpenSSL 3.5+ 的基础镜像，或改用 `PIXIV_BRIDGE_BYPASS_SNI=1` / `PIXIV_PROXY`

装好后 `python run.py --check` 的 `ech` 项应为 `OK`；找不到时会显示 `WARN` 并在提示里指回本节。

### 目标机器上首次运行

```bash
cd pixiv-bridge
cp .env.example .env      # Windows: copy .env.example .env
# 编辑 .env：填入 PIXIV_REFRESH_TOKEN（以及需要的 PIXIV_BRIDGE_ECH 等）
python run.py --check     # 期望：python / deps / files / ech(或 network) 全 OK + 登录成功
python run.py             # 纯接口启动
python novel/run.py       # 或：接口 + 小说阅读器，浏览器打开 http://127.0.0.1:8765/
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

> Docker 镜像里跑的是**纯接口**入口（`CMD python run.py`）。想在容器里用阅读器，`docker run` 时把命令换成 `python novel/run.py`（镜像已 `COPY novel novel`，不用改镜像）。

### 局域网访问

服务持有你的 Pixiv 账号令牌，暴露到局域网有安全风险，默认只监听 `127.0.0.1`。
确需局域网调用时：

```bash
PIXIV_BRIDGE_HOST=0.0.0.0 .venv/bin/python run.py          # 纯接口
PIXIV_BRIDGE_HOST=0.0.0.0 .venv/bin/python novel/run.py     # 接口 + 阅读器（手机浏览器可用）
```

建议放在反向代理（Nginx/Caddy）后面并加访问密钥。

## 合规与免责

- 本项目仅供个人学习与自动化研究使用，请遵守 [Pixiv 服务条款](https://policies.pixiv.net/) 与所在地区法律
- 控制请求频率（默认已限流），不要用于批量抓取、镜像站或商业用途
- R-18 内容的可见性取决于账号设置与登录状态

## 致谢与许可

- 接口签名算法、OAuth 流程与端点清单参考 [PixEz (Notsfsssf/pixez-flutter)](https://github.com/Notsfsssf/pixez-flutter)（GPL-3.0）及社区公开资料
- 本项目为独立实现的 Python 服务，不包含 PixEz 源码
