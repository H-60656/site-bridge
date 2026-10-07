# Pixiv Bridge 安装与配置（傻瓜版）

从零到能用，只需要 5 步：**装 Python → 解压 → 运行启动脚本 → 登录一次 → 打开网页**。

- Windows 走 [第 1 节](#1-windows1011最简单)
- 安卓 / Termux（以及其它 Linux）走 [第 2 节](#2-安卓--termux以及-其它-linux)
- 只想给别的程序调用接口：装好、登录好之后，接口地址是 `http://127.0.0.1:8765/api/...`

> **两个入口，别搞混**：一键脚本（`start.bat` / `start.sh`）跑的是**纯接口**版，浏览器打开 `http://127.0.0.1:8765/` 会是 **404**——它压根不带网页；要下面第 5 步那个小说阅读器页面，请用 `python novel\run.py`（Termux：`python3 novel/run.py`）启动。接口本身两个入口完全一样。

---

## 0. 先看清楚：什么要自己弄，什么会自动搞定

| 需要的东西 | 谁负责 | 说明 |
| --- | --- | --- |
| **Python 3.10 或更高** | ❌ **必须自己装** | 唯一必须手动安装的东西，见第 1/2 节 |
| fastapi / uvicorn / httpx / pydantic | ✅ 自动下载 | 第一次运行时自动用 pip 安装；装不上会自动换国内镜像重试 |
| **refresh_token**（登录凭证） | ✅ 自动 | 运行 `python get_token.py` 登录一次，自动保存到 `data\tokens.json` |
| 能连上 Pixiv 的网络 | ⚠️ 看环境 | 被墙时见第 4 节（ECH / SNI 绕过 / 代理 三选一） |
| OpenSSL 3.5+（**只有 ECH 模式需要**） | ⚠️ 部分系统自带 | Termux 自带 3.6.5 ✅；Windows 需另装（见第 4 节）；Ubuntu 24.04+ 自带 |
| 完全没网的机器 | ❌ 需要提前准备 | 见 `README.md` 的「完全离线部署」（提前下载 `wheels/` 目录） |

> 除了 Python 解释器，其它 Python 依赖都会自动安装。项目文件夹本身是自包含的。

---

## 1. Windows（10/11，最简单）

**第 1 步：装 Python**

1. 打开 <https://www.python.org/downloads/>，下载 Python 3.12 或更新版本。
2. 安装时**务必勾选** `Add python.exe to PATH`，然后一路下一步。
3. 验证：按 `Win+R` 输入 `cmd` 回车，在里面输入：

   ```bat
   python --version
   ```

   能显示 `Python 3.12.x` 就成功了。
   - 如果提示「不是内部或外部命令」：Python 没装好或没勾 PATH，重装并勾选 PATH。
   - 如果提示「Python 未找到；请在没有参数的情况下运行以从 Microsoft Store 安装」：这是 Windows 的「应用执行别名」在捣乱。打开 **设置 → 应用 → 高级应用设置 → 应用执行别名**，把 `python.exe` / `python3.exe` 两项**关掉**。

**第 2 步：解压**

把 `pixiv-bridge.zip` 解压到任意目录，例如 `C:\pixiv-bridge`（路径别太深，**不要**放进 OneDrive 同步文件夹）。

**第 3 步：启动**

双击目录里的 **`start.bat`**（就是这个黑色窗口，别关它）——这是**纯接口**那一版。

> 想直接看小说阅读器页面（本文第 5 步要开的那个网页），请**改用**下面这条命令启动；`--port` 等参数用法一样：
>
> ```bat
> cd /d C:\pixiv-bridge
> python novel\run.py
> ```
>
> 两个入口的接口部分完全一样，差别只在有没有那个网页：用 `start.bat` 时 `http://127.0.0.1:8765/` 是 **404**（正常，它不带页面），只有 `novel\run.py` 才有 `/` 页面和阅读历史 / 下载 / 收藏 / 书架那几个接口。

- 第一次运行会：自动生成 `.env` 配置文件 → 自动下载依赖（**1~3 分钟，属正常**）→ 打印 5 行检查结果 → 开始监听。
- 看到类似下面的内容就成功了：

  ```
  python   OK   3.12.7 (需要 3.10+)
  deps     OK   依赖完整
  files    OK   项目文件完整
  port     OK   8765 可用
  ech      OK   ECH 传输可用
  [server] http://127.0.0.1:8765
  ```

> `start.bat` 里的提示文字是英文的：Windows 批处理里写中文在各种系统编码下都会乱码甚至报错，所以英文提示 + 后面的中文说明（由 Python 输出，不会乱码）。
> 如果 Windows 防火墙弹窗询问，选「允许访问」即可（只监听本机时选「取消」也不影响使用）。

**第 4 步：登录一次**

再开一个新的 `cmd` 窗口（`Win+R` → `cmd`），切到项目目录并运行：

```bat
cd /d C:\pixiv-bridge
python get_token.py
```

按提示选 `2`（浏览器授权）：把打印出来的网址复制到浏览器打开 → 登录 Pixiv → 点「授权」→ 浏览器会跳到 `app-api.pixiv.net` 的一个空白/报错页面（**这是正常的**）→ **复制地址栏里的整条网址**，粘回窗口回车。
成功后会显示 `登录成功: 你的昵称`，并写入 `data\tokens.json`。

**第 5 步：打开网页**

浏览器访问 <http://127.0.0.1:8765/>，能看到小说榜单、点开一本能读正文即全部完成。页面顶部可以直接搜小说，右上角 `Aa` 调字号/主题，正文里的插图会直接显示，「下载 EPUB」「下载全系列（EPUB）」导出的是一本 `.epub`（**一本书只有一个文件**：单本 `<书名>.epub`，系列 `<系列名>（全N章）.epub`），插图作为图片打包进书里、带内置目录，离线打开也能看图。读过的书、下过的章节、收藏了哪些都记在**项目目录下的 `data\` 里**（`data\history.jsonl` 阅读历史、`data\downloads.jsonl` 下载历史、`data\bookmarks.jsonl` 收藏台账，都是一行一条 JSON，个人使用、不区分用户；想清空直接删对应文件即可，也可以在 `.env` 里用 `PIXIV_HISTORY_FILE` / `PIXIV_DOWNLOADS_FILE` / `PIXIV_BOOKMARKS_FILE` 换位置）。老版本的 `tokens.json` / `history.jsonl` 首次启动会自动搬进 `data\`。接口文档在 <http://127.0.0.1:8765/docs>。

**换端口**（默认 8765 被占用时）：`start.bat --port 9000`，然后访问 `http://127.0.0.1:9000/`。阅读器入口换端口一样是 `python novel\run.py --port 9000`。

---

## 2. 安卓 / Termux（以及其它 Linux）

Termux 是一个安卓上的 Linux 终端（**从 F-Droid 安装**：<https://f-droid.org/packages/com.termux/>；Google Play 上的版本早已停止更新，不要用）。

**第 1 步：装 Python 和 OpenSSL**

```sh
pkg update -y && pkg install -y python openssl
python --version    # 应为 3.10 以上
```

**第 2 步：把压缩包放进手机并解压**

```sh
termux-setup-storage          # 弹出权限请求，点允许
pkg install -y unzip
cd ~ && cp ~/storage/downloads/pixiv-bridge.zip . && unzip -o pixiv-bridge.zip
cd pixiv-bridge
```

（也可以直接在 Termux 里下载：`curl -LO <压缩包地址>`。）

**第 3 步：启动**

```sh
sh start.sh
```

> 这条命令跑的是**纯接口**那一版（Windows 的 `start.bat` 同此），要看**小说阅读器页面**（第 5 步要开的那个网页）请用：
>
> ```sh
> python3 novel/run.py
> ```
>
> 两边端口之类参数一样（`sh start.sh --port 9000` / `python3 novel/run.py --port 9000`）。用 `start.sh` 时打开 `http://127.0.0.1:8765/` 是 404，属于正常——它不带网页，只看/调接口的话用这版就够了。

- 第一次会：生成 `.env` → 自动安装依赖。Termux 上会**自动改用 `requirements-termux.txt`**（纯 Python 的 pydantic 1.x），因此**不需要** Rust/编译器，也不会卡在 `pydantic-core` 编译失败上。程序里取配置字段走的是 pydantic 1.x / 2.x 都认的写法，不用管装到的是哪一代（早先的版本在 Termux 上「保存配置」必报 500，就是这里不兼容）。
- 安装期间屏幕滚动几分钟是正常的；看到 5 行检查结果和监听地址就好了。

**第 4 步：登录一次**

从 Termux 左侧边缘向右滑 → `NEW SESSION`（新开一个会话），然后：

```sh
cd ~/pixiv-bridge && python get_token.py
```

选 `2`，把网址复制到手机浏览器打开授权，再把回调网址粘回来。

**第 5 步：打开网页**

手机浏览器访问 <http://127.0.0.1:8765/>，看到小说榜单即为成功；点开一本即可阅读，右上角 `Aa` 里把字号调大一点更适合手机。

页面就是按手机设计的：底部有「榜单 / 推荐 / 关注 / 追更 / 收藏 / 书架」六个标签，顶部常驻搜索框（排序、榜单日期这类不常用的参数收在「筛选」里，首屏直接显示小说列表），阅读页工具条可以左右滑，目录和设置是全宽浮层。点开小说如果报错，页面会直接告诉你原因——「接口不存在（404）」说明后台服务还是旧版本，重启一下服务即可；「Pixiv 返回 404」说明上游找不到这本书（多半是 R-18 或已删除）；「正文接口返回 404」说明书名/作者能取到、只有正文取不到（Pixiv 2024 年 2 月起下架了旧正文接口，本服务已改走官方 iOS 客户端的正文页面；若仍报错多半是这本对当前账号不可读）。三种情况都可以点「复制诊断信息」把详细内容发出来。

**小贴士**

- 关掉 Termux 或手机清理后台会停掉服务。想让它一直活着：在启动前执行 `termux-wake-lock`（获取唤醒锁）。
- **项目就放在 Termux 家目录里**（`~/pixiv-bridge`）。不要把项目、也不要把数据目录指到 `/sdcard`（`~/storage/...`）那边：那是安卓的 FUSE 目录，经常「读得到、写不进」，表现就是**设置里保存配置报 500**。真指过去了也不会坏：服务会把数据路径自动收回项目里的 `data/`，启动日志和设置页都会逐条提示（详见 README 的「保存配置报『写入失败』」）。
- 电脑上访问手机的服务：`sh start.sh --host 0.0.0.0`（要看阅读器就 `python3 novel/run.py --host 0.0.0.0`），然后在电脑浏览器打开 `http://手机IP:8765/`（仅限同一局域网；注意这样别人也能访问，必要时加防火墙）。
- 其它 Linux（Ubuntu / Debian / 树莓派）和 macOS 用法一样：`sh start.sh`。

> 手机上直接看也一样：用 `python3 novel/run.py` 启动后，手机浏览器开 <http://127.0.0.1:8765/>。用 `start.sh` 的话这个地址是 404（纯接口不带页面）。

---

## 3. 登录一次就够了

`python get_token.py` 提供两种方式：

| 方式 | 怎么用 | 什么时候用 |
| --- | --- | --- |
| **2) 浏览器授权**（推荐） | 脚本给你一个网址，浏览器登录并授权，把回调网址粘回来 | 绝大多数情况，最稳 |
| 1) 账号密码 | 直接输入 Pixiv 账号密码 | 图省事；Pixiv 有风控，可能提示验证码而失败 |

登录结果保存在项目目录的 `data\tokens.json`（`refresh_token` 会长期有效，脚本也会自动续期）。**不要**把这个文件发给别人。

也可以完全不用 `get_token.py`：把已有的 `PIXIV_REFRESH_TOKEN=...` 填进 `.env` 即可（三种方式详见 `README.md`）。

---

## 4. 连不上 Pixiv？（被墙时的三条通道）

先自检：

```sh
python run.py --check        # 纯接口入口；看阅读器用 python3 novel/run.py --check
```

（两个入口的 `--check` 查的是同一套环境：Python / 依赖 / 项目文件 / 端口 / 网络 / ECH。）

看输出里的 `network` / `ech` 两行：

| 你的情况 | 建议 |
| --- | --- |
| `ech OK` | 什么都不用改，已经在用加密 SNI 直连 |
| `network OK 可达 3/3` | 网络本身没问题，正常用即可 |
| `network WARN` / `ech WARN`，且本机有代理软件 | 用代理（见下）：在 `.env` 里写 `PIXIV_PROXY=http://127.0.0.1:7890` |
| `ech WARN` 且错误里有 `SSL_get_error=5` | 本机到 Cloudflare 这一段在阻断 ECH 扩展本身（与服务端无关），换网络或走代理，见第 4 节 |
| 有 Termux/路由器级代理、手机上别的 Pixiv 客户端能用 | 用代理 |

**方式一：ECH（加密 SNI，免代理首选）**

在 `.env` 里：

```ini
PIXIV_BRIDGE_ECH=1
PIXIV_BRIDGE_BYPASS_SNI=0
```

需要系统里有 **OpenSSL 3.5+**：Termux 自带（`pkg install openssl` 后是 3.6.5）✅；Windows 需要从 <https://slproweb.com/products/Win32OpenSSL.html> 安装（选完整版），装完在 `.env` 里指向它的 `bin` 目录：

```ini
PIXIV_BRIDGE_OPENSSL_DIR=C:\Program Files\OpenSSL-Win64\bin
```

Ubuntu 24.04+ / Debian 13+ 自带；macOS 用 `brew install openssl@3`。

**方式二：SNI 绕过（不需要 OpenSSL，但可能被服务器拒绝）**

```ini
PIXIV_BRIDGE_ECH=0
PIXIV_BRIDGE_BYPASS_SNI=1
```

**方式三：代理（最可靠，需要你已有可用的代理）**

```ini
PIXIV_PROXY=http://127.0.0.1:7890
```

端口换成你代理软件的 HTTP 端口（Clash 常见 7890、v2ray 常见 10809）；设置后**所有** Pixiv 请求都走代理，ECH / SNI 绕过自动停用，不用管它们原来的开关。`socks5://` 需要先 `pip install "httpx[socks]"`。

如果代理软件只监听了 SOCKS 端口，用 HTTP 端口更省事；实在只有 SOCKS，就按上面那条装 `socksio` 再把地址写成 `socks5://127.0.0.1:1080`。

> **现实中会遇到的情况**：如果 `ech` 报 `ECH 直连失败: EchConnectError`，同时 `network` 也 `WARN`，说明当前网络对 Pixiv 的封锁同时覆盖了「加密 SNI（ECH 外层域名）」「普通 SNI」和「无 SNI 直连」三条路，此时**只有代理能通**。这是网络环境问题，不是软件坏了。
>
> 实测样例（某校园网）：`cloudflare-ech.com`（ECH 外层名）与 `cloudflare-dns.com` 的 TLS 握手被直接重置，Pixiv 的三个域名带 SNI 也一律被重置，无 SNI 直连 API 返回 `403 Forbidden` —— 三条免代理通道全断，只能走代理。
>
> **2026-10 新增实测（家宽）**：ECH 也可能被**定向阻断**——`ech` 报 `ECH 直连失败: ... SSL_get_error=5`，而同一时间不带 ECH 扩展的握手一切正常。这种封锁只看 ClientHello 里有没有 ECH 扩展，换 IP、换外层域名、换 ECH 配置都没用，客户端无法绕过，只能换网络或走代理。自检会直接把这个结论打在 `ech` 那一行后面。
>
> **⚠️ 分清是哪一段的问题**：上面这些 `ech` / `network` / `SSL_get_error=5` / `getaddrinfo failed` 报错，都只说明**你这台机器到 Pixiv 这条链路（本地网络 / 运营商 / DNS）**不通，**不代表服务端有问题**——Pixiv 与 Cloudflare 的 ECH 支持一直是正常的，同一份程序换个网络或换台服务器就能直连，同一台机器也可能时通时断（同一个 IP 几分钟前 200、几分钟后全部被重置）。判断口诀：**换网络/换机器就能消失的是本地问题；换到哪都一样、且只影响某本书或某个接口的，才是服务端问题**（例如上游 404 = 作品被隐藏/下架）。页面打不开、连接被拒一类才是「浏览器 → 本服务」这一段，与 ECH 无关，查 `HOST`/`PORT`/防火墙。详见 README「怎么区分本地网络问题和服务端问题」。

---

## 5. 每天怎么用

| 平台 | 启动（小说阅读器 + 接口） |
| --- | --- |
| Windows | 只需接口用 `start.bat`；要看小说阅读器用 `python novel\run.py` |
| Termux / Linux | 只需接口用 `cd pixiv-bridge && sh start.sh`；要看小说阅读器用 `python3 novel/run.py` |

> 这两个入口的用法差别只在页面：`run.py`（含 `start.bat` / `start.sh`）是纯接口，浏览器打开 `/` 是 404；`novel/run.py` 在上面多挂了小说阅读器页面（`http://127.0.0.1:8765/`）与阅读历史 / 下载 / 收藏 / 书架那几个接口。命令行参数（`--host` / `--port` / `--reload` / `--check`）两边一样。

启动后：

- 网页（小说阅读器，**需 `novel/run.py` 入口**）：<http://127.0.0.1:8765/>；用 `start.bat` / `start.sh` 时这里就是 404，属正常
- 自带的交互式接口文档（两个入口都有）：<http://127.0.0.1:8765/docs>
- 健康检查：<http://127.0.0.1:8765/api/health>
- 给别的程序调用，例如：

  ```sh
  curl "http://127.0.0.1:8765/api/illust/detail?illust_id=123456"
  ```

- 完整接口清单见 `README.md`。

**搬机器 / 分享给别人**：整个项目文件夹拷过去即可，但**不要拷** `.env`、`data\` 目录（里面有你的账号凭证 `data\tokens.json`，以及阅读历史、下载记录、收藏台账和页面配置）、`.venv`、`__pycache__`。反过来，如果只是自己换台电脑、想把进度带走，**只拷 `data\` 目录**就够了。详见 `README.md` 的「打包与移植」。

---

## 6. 一分钟排错表

| 症状 | 原因 / 处理 |
| --- | --- |
| `'python' 不是内部或外部命令` | Python 没装或安装时没勾 PATH；见第 1 节第 1 步 |
| 提示去 Microsoft Store 装 Python | 关闭「应用执行别名」里的 python.exe / python3.exe |
| `port WARN 8765 已被占用` | 换端口：`start.bat --port 9000` / `sh start.sh --port 9000`；或关掉之前那个窗口 |
| 依赖一直装不上 | 在 `.env` 里设 `PIXIV_BRIDGE_PIP_INDEX=https://pypi.tuna.tsinghua.edu.cn/simple`；或按 README 准备 `wheels/` 目录离线安装 |
| `network WARN 可达 0/3` | 连不上 Pixiv，见第 4 节 |
| `ech WARN 且含 SSL_get_error=5` | 本机到 Cloudflare 这一段在阻断 ECH 扩展（与配置、IP、服务端无关），换网络或设 `PIXIV_PROXY` 走代理，见第 4 节 |
| `ech WARN ECH 直连失败`（其他原因） | ECH 外层域名被封锁或配置过期，改用代理，见第 4 节 |
| Termux 里 `pydantic-core` 编译失败 | 不应该发生：Termux 会自动用 `requirements-termux.txt`（纯 Python）。若出现，删掉 `.venv`/重装并确认 `python -m pip install -r requirements-termux.txt` |
| `未配置 refresh_token` / 网页显示未登录 | 先跑 `python get_token.py` 登录一次 |
| 登录时报验证码/错误 | 改用 `get_token.py` 的「2) 浏览器授权」 |
| 网页（小说阅读器）打不开 / `404` | 用错入口了：`start.bat` / `start.sh` 是**纯接口**，不带网页，`/` 返回 404 属正常。要看阅读器请用 `python novel\run.py`（Termux：`python3 novel/run.py`） |
| `files` 检查项提到缺 `web/index.html` | 已拆成两个项目：阅读器页面现在在 `novel/web/index.html`，接口项目的必需文件清单只剩 `run.py` / `requirements.txt` |
| 网页打得开但列表空白 | 看启动窗口里的报错；多半是网络被阻断（见第 4 节） |
| 设置里保存配置提示「写入失败」 | 先看提示里写的**文件路径和系统原因**：`data\settings.json` 被别的程序占着或带了只读属性（编辑器/同步盘/杀软）最常见，服务会自动摘只读位、重试、再退回直接覆盖写，一般会自动好；仍失败就关掉占用它的程序，或直接删掉该文件（会退回默认值）。**安卓上还要看数据目录是不是在项目外**：`PIXIV_DATA_DIR`/`PIXIV_*_FILE` 指到 `/sdcard/...` 会被自动收回项目里的 `data/`（启动日志和设置页会逐条提示）；项目本身也别放在 `/sdcard` 下，放到 Termux 家目录（`~/pixiv-bridge`）最稳 |
| 保存配置报 `500`，提示里带 `AttributeError: … has no attribute 'model_dump'` | 用的是旧版代码（Termux 装的是 pydantic 1.x，没有 `model_dump`）。换成最新版的 `pixiv_bridge/`（里面有 `compat.py`，两个 pydantic 大版本都认）后重跑 `sh start.sh` |

---

## 7. 需要人工准备的东西（无法自动下载）

1. **Python 3.10+**：Windows 从 python.org 装；Termux `pkg install python`。
2. **能访问 Pixiv 的网络**：被墙时需要代理，或依赖 ECH/SNI 绕过（不一定成功）。
3. **你自己的 Pixiv 登录**：`python get_token.py`（需要能连上 Pixiv 才能完成）。
4. **OpenSSL 3.5+**：只有用 ECH 模式才需要，且**不能用 pip 安装**（Termux 自带，Windows 需另装）。
5. 完全离线的机器：提前在能上网的同类机器上执行
   `pip download -r requirements.txt -d wheels`，把 `wheels/` 一起拷过去。
