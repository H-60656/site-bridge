# Pixiv Bridge 安装与配置（傻瓜版）

从零到能用，只需要 5 步：**装 Python → 解压 → 运行启动脚本 → 登录一次 → 打开网页**。

- Windows 走 [第 1 节](#1-windows1011最简单)
- 安卓 / Termux（以及其它 Linux）走 [第 2 节](#2-安卓--termux以及-其它-linux)
- 只想给别的程序调用接口：装好、登录好之后，接口地址是 `http://127.0.0.1:8765/api/...`

> **入口有四个，别搞混**：一键脚本（`start.bat` / `start.sh`）和 `python pixiv/run.py` 跑的是**同一个接口项目**（Pixiv），浏览器打开 `http://127.0.0.1:8765/` 会看到**导航页**（不挂扩展时列出接口文档等自带入口）；要下面第 5 步那个小说阅读器页面，请在 `.env` 里加一行 `PIXIV_BRIDGE_EXTENSIONS=novel` 后**还是用原来那个启动方式**（阅读器没有自己的入口脚本了，见「附属项目（扩展）」），阅读器在 `/novel`。另外三个入口 `python iwara\run.py`、`python asmr_one\run.py` 与 `python ehviewer\run.py` 是**与 `pixiv/` 同级的另外三个接口项目**：iwara.tv 只提供 `/api/iwara/*`（视频页面 `iwara_web` 是扩展、默认不挂，见第 5 节），asmr.one 只提供 `/api/asmr/*`，E-Hentai 只提供 `/api/eh/*`；它们都不挂任何 Pixiv 路由，也互不挂对方的路由。**一键脚本第一次会先列出项目导航**（接口项目 + 扩展）让你挑：直接回车＝默认的 Pixiv，输入 `2` 或 `iwara` 就起 iwara，输入 `3` 或 `asmr_one` 就起 asmr.one，输入 `4` 或 `ehviewer` 就起 E-Hentai；设了环境变量 `BRIDGE_SITE=iwara` 就跳过询问直接起 iwara。

---

## 0. 先看清楚：什么要自己弄，什么会自动搞定

| 需要的东西 | 谁负责 | 说明 |
| --- | --- | --- |
| **Python 3.10 或更高** | ❌ **必须自己装** | 唯一必须手动安装的东西，见第 1/2 节 |
| fastapi / uvicorn / httpx / pydantic | ✅ 自动下载 | 第一次运行时自动用 pip 安装；装不上会自动换国内镜像重试 |
| **refresh_token**（登录凭证） | ✅ 自动 | 运行 `python pixiv/get_token.py` 登录一次，自动保存到 `data\tokens.json` |
| 能连上 Pixiv 的网络 | ⚠️ 看环境 | 被墙时见第 4 节（ECH / SNI 绕过 / 代理 三选一） |
| OpenSSL 4.0+（**只有 ECH 模式需要**） | ✅ **项目自带** | `openssl-libs/` 里已带 OpenSSL 4.x 的库，**Windows 与安卓 Termux 都开箱即用**；其它平台没带对应子目录时才需自己装（注意 3.x 全系都没有 ECH，装了也没用） |
| curl_cffi（**iwara / asmr.one / E-Hentai 三个站点需要**） | ✅ 自动下载 | 想用 `iwara/run.py`、`asmr_one/run.py` 或 `ehviewer/run.py` 时第一次启动自动装；只跑 Pixiv 的话完全不需要它。Termux 上要 Python 3.13+ 才是现成轮子（见第 8 节） |
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

双击目录里的 **`start.bat`**（就是这个黑色窗口，别关它）——这会打开**项目导航**（列出接口项目与扩展），**直接回车**起的就是 Pixiv 接口那一版（不挂阅读器）；想跑 iwara 就输入 `2` 或 `iwara`，想跑 asmr.one 就输入 `3` 或 `asmr_one`，想跑 E-Hentai 就输入 `4` 或 `ehviewer`（asmr 与 E-Hentai 都匿名可用，不需要登录）。

> 想直接看小说阅读器页面（本文第 5 步要开的那个网页），**不需要换启动方式**，只要在 `.env` 里加一行再照常用 `start.bat` 启动：
>
> ```bat
> rem 编辑 C:\pixiv-bridge\.env，加一行（没有 .env 就先跑一次 start.bat 让它生成）
> PIXIV_BRIDGE_EXTENSIONS=novel
> ```
>
> `--port` 等参数用法完全不变，各接口项目共用同一套检查表。默认（不写扩展，走 `pixiv/run.py`）时 `http://127.0.0.1:8765/` 是**导航页**（列出 `/docs`、`/api/health` 等自带入口，此时不会有阅读器链接），加了 `PIXIV_BRIDGE_EXTENSIONS=novel` 就多出 `/novel` 阅读器页面（导航页上也会多出指向它的链接）和阅读历史 / 下载 / 收藏 / 书架那几个接口；`iwara\run.py` 是**另一个站点**，只提供 `/api/iwara/*`（不挂任何 Pixiv 接口），要 iwara 的视频页面，就在 `.env` 里再加一项 `iwara_web`（`PIXIV_BRIDGE_EXTENSIONS=iwara_web`，多项用逗号分隔；它调 `/api/iwara/*`，所以要配合 `iwara\run.py` 用）；`asmr_one\run.py` 是**第三个站点**，只提供 `/api/asmr/*`，纯匿名不需要登录；`ehviewer\run.py` 是**第四个站点**，只提供 `/api/eh/*`，同样匿名可用（E-Hentai 公开画廊不需要登录）。它们都不挂任何 Pixiv 或 iwara 路由，也互不挂对方的路由。

- 第一次运行会：自动生成 `.env` 配置文件 → 自动下载依赖（**1~3 分钟，属正常**）→ 打印环境检查结果（框架 4 项 + 本站点自己的检查项）→ 开始监听。
- 看到类似下面的内容就成功了：

  ```
  python   OK   3.12.7 (需要 3.10+)
  deps     OK   依赖完整
  files    OK   项目文件完整
  port     OK   8765 可用
  ech      OK   ECH 传输可用
  [server] http://127.0.0.1:8765
  ```

> `start.bat` 自己的提示文字是英文的：Windows 批处理里写中文在各种系统编码下都会乱码甚至报错，所以英文提示 + 后面的中文说明（**项目导航、环境检查这些都由 Python 输出，中文正常**）。
> 如果 Windows 防火墙弹窗询问，选「允许访问」即可（只监听本机时选「取消」也不影响使用）。

**第 4 步：登录一次**

再开一个新的 `cmd` 窗口（`Win+R` → `cmd`），切到项目目录并运行：

```bat
cd /d C:\pixiv-bridge
python pixiv/get_token.py
```

按提示选 `2`（浏览器授权）：把打印出来的网址复制到浏览器打开 → 登录 Pixiv → 点「授权」→ 浏览器会跳到 `app-api.pixiv.net` 的一个空白/报错页面（**这是正常的**）→ **复制地址栏里的整条网址**，粘回窗口回车。
成功后会显示 `登录成功: 你的昵称`，并写入 `data\tokens.json`。

**第 5 步：打开网页**

浏览器访问 <http://127.0.0.1:8765/novel>，能看到小说榜单、点开一本能读正文即全部完成（`http://127.0.0.1:8765/` 是导航页，上面也有指向阅读器的链接）。页面顶部可以直接搜小说，右上角 `Aa` 调字号/主题，正文里的插图会直接显示，「下载 EPUB」「下载全系列（EPUB）」导出的是一本 `.epub`（**一本书只有一个文件**：单本 `<书名>.epub`，系列 `<系列名>（全N章）.epub`），插图作为图片打包进书里、带内置目录，离线打开也能看图。读过的书、下过的章节、收藏了哪些都记在**项目目录下的 `data\` 里**（`data\history.jsonl` 阅读历史、`data\downloads.jsonl` 下载历史、`data\bookmarks.jsonl` 收藏台账，都是一行一条 JSON，个人使用、不区分用户；想清空直接删对应文件即可，也可以在 `.env` 里用 `PIXIV_HISTORY_FILE` / `PIXIV_DOWNLOADS_FILE` / `PIXIV_BOOKMARKS_FILE` 换位置）。老版本的 `tokens.json` / `history.jsonl` 首次启动会自动搬进 `data\`。接口文档在 <http://127.0.0.1:8765/docs>。

**换端口**（默认 8765 被占用时）：`start.bat --port 9000`，然后访问 `http://127.0.0.1:9000/`。挂了阅读器扩展时用法完全一样，还是 `start.bat --port 9000`。

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

> 这条命令先打开**项目导航**（列出接口项目与扩展）：直接回车跑的是默认那一版（只跑 Pixiv 接口、不挂扩展；Windows 的 `start.bat` 同此），输入 `2` 或 `iwara` 就换成 iwara。要看**小说阅读器页面**（第 5 步要开的那个网页）**不用换命令**，在 `.env` 里加一行再照常启动即可：
>
> ```sh
> nano .env            # 没有就先跑一次 sh start.sh 让它生成
> # 加一行：PIXIV_BRIDGE_EXTENSIONS=novel
> sh start.sh
> ```
>
> 端口之类参数完全一样（`sh start.sh --port 9000`）。不加扩展时打开 `http://127.0.0.1:8765/` 是导航页（只列接口自带的入口，没有阅读器链接）——只看/调接口的话用默认这版就够了。

- 第一次会：生成 `.env` → 自动安装依赖。Termux 上会**自动改用 `requirements-termux.txt`**（纯 Python 的 pydantic 1.x），因此**不需要** Rust/编译器，也不会卡在 `pydantic-core` 编译失败上。程序里取配置字段走的是 pydantic 1.x / 2.x 都认的写法，不用管装到的是哪一代（早先的版本在 Termux 上「保存配置」必报 500，就是这里不兼容）。
- 安装期间屏幕滚动几分钟是正常的；看到环境检查结果和监听地址就好了。

**第 4 步：登录一次**

从 Termux 左侧边缘向右滑 → `NEW SESSION`（新开一个会话），然后：

```sh
cd ~/pixiv-bridge && python pixiv/get_token.py
```

选 `2`，把网址复制到手机浏览器打开授权，再把回调网址粘回来。

**第 5 步：打开网页**

手机浏览器访问 <http://127.0.0.1:8765/novel>，看到小说榜单即为成功；点开一本即可阅读，右上角 `Aa` 里把字号调大一点更适合手机。

页面就是按手机设计的：底部有「榜单 / 推荐 / 关注 / 追更 / 收藏 / 书架」六个标签，顶部常驻搜索框（排序、榜单日期这类不常用的参数收在「筛选」里，首屏直接显示小说列表），阅读页工具条可以左右滑，目录和设置是全宽浮层。点开小说如果报错，页面会直接告诉你原因——「接口不存在（404）」说明后台服务还是旧版本，重启一下服务即可；「Pixiv 返回 404」说明上游找不到这本书（多半是 R-18 或已删除）；「正文接口返回 404」说明书名/作者能取到、只有正文取不到（Pixiv 2024 年 2 月起下架了旧正文接口，本服务已改走官方 iOS 客户端的正文页面；若仍报错多半是这本对当前账号不可读）。三种情况都可以点「复制诊断信息」把详细内容发出来。

**小贴士**

- 关掉 Termux 或手机清理后台会停掉服务。想让它一直活着：在启动前执行 `termux-wake-lock`（获取唤醒锁）。
- **项目就放在 Termux 家目录里**（`~/pixiv-bridge`）。不要把项目、也不要把数据目录指到 `/sdcard`（`~/storage/...`）那边：那是安卓的 FUSE 目录，经常「读得到、写不进」，表现就是**设置里保存配置报 500**。真指过去了也不会坏：服务会把数据路径自动收回项目里的 `data/`，启动日志和设置页都会逐条提示（详见 README 的「保存配置报『写入失败』」）。
- 电脑上访问手机的服务：`sh start.sh --host 0.0.0.0`（要看阅读器就先在 `.env` 里写 `PIXIV_BRIDGE_EXTENSIONS=novel`，命令不变），然后在电脑浏览器打开 `http://手机IP:8765/novel`（仅限同一局域网；注意这样别人也能访问，必要时加防火墙）。
- 其它 Linux（Ubuntu / Debian / 树莓派）和 macOS 用法一样：`sh start.sh`。

> 手机上直接看也一样：在 `.env` 里写 `PIXIV_BRIDGE_EXTENSIONS=novel` 并用 `sh start.sh` 启动后，手机浏览器开 <http://127.0.0.1:8765/novel>。不加扩展时这个地址是 404。

---

## 3. 登录一次就够了

`python pixiv/get_token.py` 提供两种方式：

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
python pixiv/run.py --check        # 查的是同一套环境，跟挂没挂扩展无关
python iwara/run.py --check  # 同一套 + 3 项 iwara 专属（依赖 / api.iwara.tv / 登录态）
python asmr_one/run.py --check  # 同一套 + 4 项 asmr 专属（依赖 / api.asmr.one / 搜索是否真在过滤 / ECH，默认就有 ECH）
python ehviewer/run.py --check  # 同一套 + 6 项 ehviewer 专属（依赖 / Cloudflare 边缘 / e-hentai.org 站点 / 详情页解析 / 热门榜单 / 大图页解析）
```

（各入口的 `--check` 查的是同一套环境：Python / 依赖 / 项目文件 / 端口 / 网络 / ECH，其中 `port` 在 `--check` 下只警告不中止——毕竟这条路不监听端口。iwara 入口再追加 3 项：`iwara 依赖`（真 import `curl_cffi`）、`iwara 网络`（真发一次 `GET /videos?limit=1`）、`iwara 登录`（没配 token 就报匿名模式，配了就真刷一次并读用户名）。`asmr_one` 入口追加 4 项：`asmr 依赖`（真 import `curl_cffi`）、`asmr 网络`（真发一次 `GET /api/works?page=1`，要整条「借道 IP 池 + impersonate + ECH」链路通才算过）、`asmr 搜索`（拿两个词横向对照：必定查不到的 `zzzzqqxxnotexist` 必须回 0 条，已知有结果的 `癒し` 必须不是 0 条。守的是「参数到底有没有生效」——本站曾经把不存在的 `/api/search/works` 当搜索端点，任何查询都恒定回同一批 117 条，HTTP 200 形状标准、上一项照样全绿）、`asmr ECH`（真发一次 `/cdn-cgi/trace` 看回显是不是 `sni=encrypted`；`ASMR_BRIDGE_ECH` **默认就开**，所以默认有这一项，写成 `ASMR_BRIDGE_ECH=0` 关掉后才剩 3 项）。`ehviewer` 入口追加 6 项：`ehviewer 依赖`（真 import `curl_cffi` 并检查有没有 `CurlHttpVersion`，HTTP/3 就靠它）、`ehviewer 边缘`（并发探内置的 16 个候选 Cloudflare anycast IP，只建 TCP 不发 TLS，不算站点是否通）、`ehviewer 站点`（真发一次首页 `list_galleries()`；`EH_BRIDGE_HTTP3` 关掉时会提示「取到页面属于侥幸」）、`ehviewer 详情`（从刚取回的列表里挑头两条真取一次详情，确认「页面骨架」判据 `#gn` / `#gdc` / `#gdd` 还认得上游页面——上游对取不到的画廊回的是 HTTP 200 + 一句纯文本，判据一旦失效，真实画廊会被整批误判成存根、全部 404，而列表页走的是另一套解析，证明不了这一点）、`ehviewer 榜单`（真发一次 `GET /popular`，确认热门榜单这个**独立入口**还在、还能解析出画廊——首页好着不代表它还在，上游可能只动这一个入口）、`ehviewer 大图`（串完整条链：列表 → 取一条详情的图片页链接 → 真解一次那个 `/s/` 页面，确认图片直链判据与存根判据仍认得上游页面——判据一旦失效，`/api/eh/proxy` 拿不到直链，真实大图页会整批 502，而列表与详情都还是绿的）。这些都只警告、不中止，因为它们失败只影响该站点自己的接口，入口照样能启动；asmr.one 与 E-Hentai 都没有登录自检，都是纯匿名的。）

> **为什么有这些通道、各自对付什么**，以及换到别的站点时怎么从头定位，见 **[DIRECT.md](DIRECT.md)**（直连思路）。这里只讲「按现象选哪一条」。

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

需要 **OpenSSL 4.0+**（ECH 是 4.0.0 才加入的，3.x 全系没有）——**项目自带的 `openssl-libs/` 里已经有了，Windows 与安卓 Termux 上都不用装任何东西**，程序会自动优先用这一份。

只有两种情况才需要自己动手：包里没有当前平台的子目录（比如把 Windows 版拷到 Linux 上），或自带的那份载入失败。这时程序会自动退去找系统安装的 OpenSSL（Windows 的 `C:\Program Files\OpenSSL*`、Debian 系的多架构目录 `/usr/lib/<架构>-linux-gnu`、Fedora 的 `/usr/lib64`、macOS Homebrew 的 `opt/openssl@4/lib`、BSD 的 `/usr/local/lib` 等，按当前系统与架构挑）；也可以自己装一份 4.0+，并在 `.env` 里指过去：

```ini
PIXIV_BRIDGE_OPENSSL_DIR=C:\Program Files\OpenSSL-Win64\bin
```

想在包里补一份本平台的库，放到 `openssl-libs/<系统>-<架构>/` 即可（如 `linux-arm64/`、`darwin-arm64/`），命名沿用 `libssl.so.4`、`libssl.4.dylib` 这类常见名。

系统安装来源：Windows 从 <https://slproweb.com/products/Win32OpenSSL.html> 装 **4.x** 完整版；Ubuntu/Debian 需要自行编译或取预编译的 4.0+（**发行版自带的都是 3.x，没有 ECH**）；Fedora/RHEL 同理；macOS 用 `brew install openssl@4`。**Termux 上不要用 `pkg install openssl`**——官方源是 3.6.5，实测导出的 ECH 符号数为 0，项目自带的 `openssl-libs/android-arm64/`（4.0.3）才是可用的那份。

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

| 平台 | 启动 |
| --- | --- |
| Windows | 只需 Pixiv 接口：`start.bat` 然后回车（导航里的默认项就是它）；要小说阅读器：先在 `.env` 加 `PIXIV_BRIDGE_EXTENSIONS=novel`，还是 `start.bat`；要用 iwara：`start.bat iwara` 或 `python iwara\run.py`（要 iwara 视频页面再加 `iwara_web`）；要用 asmr.one：`start.bat asmr_one` 或 `python asmr_one\run.py`；要用 E-Hentai：`start.bat ehviewer` 或 `python ehviewer\run.py` |
| Termux / Linux | 只需接口：`cd pixiv-bridge && sh start.sh` 然后回车；要小说阅读器：先在 `.env` 加 `PIXIV_BRIDGE_EXTENSIONS=novel`，还是 `sh start.sh`；要用 iwara 视频：`sh start.sh iwara` 或 `python3 iwara/run.py`（要 iwara 视频页面再加 `iwara_web`）；要用 asmr.one：`sh start.sh asmr_one` 或 `python3 asmr_one/run.py`；要用 E-Hentai：`sh start.sh ehviewer` 或 `python3 ehviewer/run.py` |

> 差别只在多挂了什么：一键脚本先给一个**项目导航**（直接回车＝默认的 `pixiv/run.py`）——默认只跑 Pixiv 接口、不挂扩展，浏览器打开 `/` 是**导航页**（列出接口文档、健康检查等自带入口）；在 `.env` 里写 `PIXIV_BRIDGE_EXTENSIONS=novel` 就在同一个入口上多挂了小说阅读器页面（`http://127.0.0.1:8765/novel`，导航页上也会多一段链接）与阅读历史 / 下载 / 收藏 / 书架那几个接口；`iwara/run.py` 提供的是 `/api/iwara/*`（视频浏览、解析直链、代转播放），它的 `/` 也是导航页、额外多一段 iwara 自己的入口；`asmr_one/run.py` 提供的是 `/api/asmr/*`（作品浏览、搜索、详情、音轨树、封面与音频代转），同样是导航页 + 自己那一段，且**匿名即可用**；`ehviewer/run.py` 提供的是 `/api/eh/*`（画廊列表、热门榜单、搜索、详情、图片页、大图页解析、元数据、图片代转），也是导航页 + 自己那一段，同样**不需要登录**。命令行参数（`--host` / `--port` / `--reload` / `--check`）都一样，`python -m pixiv_bridge --list` 可以只看导航不启动。

> **阅读器为什么没有自己的启动命令？** 它现在按「扩展」的方式挂到接口项目上：配置里写目录名（`PIXIV_BRIDGE_EXTENSIONS=novel`），程序自己去找 `novel/extension.py`。这样每个接口项目各留一个入口，阅读器不再需要单独的脚本。写错目录名不会静默降级——启动前会打印一条错误并退出。

> iwara、asmr.one 与 E-Hentai 那三条入口第一次启动会额外装一个 `curl_cffi`（比框架依赖慢一点，属正常）。Windows / Linux / macOS 与 **Python 3.13+ 的 Termux** 装的都是官方现成轮子；Termux 上 Python 是 3.12 及以下时没有对应轮子，会退回源码编译（要 Rust + C 工具链），见第 8 节排错表。
>
> **iwara 登录是可选的**：公开内容（含 R18）匿名就能看、能解析直链、能播。登录只为两件事——「订阅作者的内容」和「自己账号下的私密视频」。想登录就跑一次 `python iwara/get_token.py`（Termux：`python3 iwara/get_token.py`，只收邮箱 + 密码，没有浏览器授权），token 存到 `data/iwara_tokens.json`；`--status` 看当前登录态，`--logout` 删掉。也可以用环境变量 `IWARA_REFRESH_TOKEN` 临时覆盖。token 失效不会拖垮整站：自动退回匿名，接口照常可用。
>
> **asmr.one 不需要登录**：浏览、搜索、播放全部匿名可用，所以它没有 `get_token.py`，也没有 `login_check`。它与 iwara 一样走 `curl_cffi`（种子 IP 池 + 浏览器指纹 + DoH 换 IP 重试），但 **ECH 默认是开的**（`ASMR_BRIDGE_ECH` 不写就等于开）；要关掉就显式写 `ASMR_BRIDGE_ECH=0`。
>
> **E-Hentai 也不需要登录**：公开画廊匿名可读，所以 `ehviewer/` 同样没有 `get_token.py` 和 `login_check`。它另有一条规矩——**必须强制 HTTP/3**（`EH_BRIDGE_HTTP3` 默认开）：`e-hentai.org` 自己解析出来的那两个边缘 IP 在本机是 IP 级被阻断的，所以要借道别的 Cloudflare anycast IP 并用 HTTP/3 打过去；关掉这个开关大概率连不上。图片字节在 `*.hath.network` 的 H@H 节点上，必须走 `/api/eh/proxy?url=...` 代转。

启动后：

- 导航页（各入口都有，`/`）：<http://127.0.0.1:8765/>；不挂扩展时列出接口自带的入口，挂了扩展会多出「扩展：X」一段
- 网页（小说阅读器，**需在 `.env` 里写 `PIXIV_BRIDGE_EXTENSIONS=novel`**，在 `/novel`）：<http://127.0.0.1:8765/novel>；没启用扩展时这里是 404，属正常
- 自带的交互式接口文档（各入口都有）：<http://127.0.0.1:8765/docs>
- 健康检查：<http://127.0.0.1:8765/api/health>
- iwara 视频页面（**需 `iwara/run.py` 入口 + `.env` 里写 `PIXIV_BRIDGE_EXTENSIONS=iwara_web`**）：<http://127.0.0.1:8765/iwara>；用 `pixiv/run.py` 启动、或没挂 `iwara_web` 时是 404，属正常
- iwara 视频接口（**需 `iwara/run.py` 入口**）：<http://127.0.0.1:8765/api/iwara/videos>；用 `pixiv/run.py` 启动时是 404，属正常
- asmr.one 接口（**需 `asmr_one/run.py` 入口**）：<http://127.0.0.1:8765/api/asmr/works>（搜索 `/api/asmr/search?query=...`、作品详情 `/api/asmr/work/{id}`、音轨 `/api/asmr/tracks/{id}`、封面 `/api/asmr/cover/{id}`、音频 `/api/asmr/stream?url=...`）；用 `pixiv/run.py` 或 `iwara/run.py` 启动时是 404，属正常
- E-Hentai 接口（**需 `ehviewer/run.py` 入口**）：<http://127.0.0.1:8765/api/eh/list>（热门榜单 `/api/eh/popular`、搜索 `/api/eh/search?query=...`、画廊详情 `/api/eh/gallery/{gid}/{token}`、图片页清单 `/api/eh/images/{gid}/{token}`、大图页解析 `/api/eh/page?url=...`、元数据 `/api/eh/metadata?gid=&token=`、图片代转 `/api/eh/proxy?url=...`）；用别的入口启动时是 404，属正常
- 给别的程序调用，例如：

  ```sh
  curl "http://127.0.0.1:8765/api/illust/detail?illust_id=123456"
  ```

- 完整接口清单见 `README.md`。

**搬机器 / 分享给别人**：整个项目文件夹拷过去即可，但**不要拷** `.env`、`data\` 目录（里面有你的账号凭证 `data\tokens.json`，以及阅读历史、下载记录、收藏台账和页面配置）、`.venv`、`__pycache__`。反过来，如果只是自己换台电脑、想把进度带走，**只拷 `data\` 目录**就够了。详见 `README.md` 的「打包与移植」。

**只分享一部分**：项目分三层（框架 + 四个同级接口项目 + 两个扩展），可以按用途单独打包。跑一条命令就能生成八个现成的分享包（Pixiv 接口 / 只有 iwara 的接口 / 只有 asmr.one 的接口 / 只有 E-Hentai 的接口 / 两个扩展包 / 两个配好的组合包）：

```bash
python scripts/make_share_packages.py          # 八个包全做，产物在 dist/
python scripts/make_share_packages.py --list   # 看看有哪几个
```

每个包内都有自己的一份 `README.md`，写清这是接口包还是扩展包、怎么启动。**扩展包（`novel` / `iwara_web`）不能单独启动**：把包里的同名目录拷进接口项目根，再在 `.env` 里用 `PIXIV_BRIDGE_EXTENSIONS` 启用即可。详见 `README.md` 的「一键生成分享包」。

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
| `未配置 refresh_token` / 网页显示未登录 | 先跑 `python pixiv/get_token.py` 登录一次 |
| 登录时报验证码/错误 | 改用 `get_token.py` 的「2) 浏览器授权」 |
| 网页（小说阅读器）打不开 / `404` | 阅读器现在在 `/novel`，不在 `/`（`/` 是导航页，一定打得开）。没启用扩展时 `/novel` 返回 404 属正常，要阅读器就在 `.env` 里加一行 `PIXIV_BRIDGE_EXTENSIONS=novel`，然后**用原来的方式重启**（`start.bat` / `sh start.sh` / `python pixiv/run.py`） |
| 启动时打印 `[扩展] …找不到入口文件…` 后退出 | `.env` 里 `PIXIV_BRIDGE_EXTENSIONS` 写的目录名不对（相对路径按项目根算）。要小说阅读器就写 `novel`，并确认 `novel/extension.py` 确实拷过来了 |
| `/iwara` 打不开 / 404 | 视频页面现在归 **`iwara_web` 扩展包**：要 `python iwara\run.py` 入口 + `.env` 里写 `PIXIV_BRIDGE_EXTENSIONS=iwara_web`，并且 `iwara_web/` 目录要拷过来 |
| `/api/iwara/...` 全部 404 | 用错入口了：iwara 接口只在 `python iwara\run.py` 下挂载，`pixiv/run.py` 那个站点没有（各站点互不挂对方的路由） |
| `/api/asmr/...` 全部 404 | 用错入口了：asmr 接口只在 `python asmr_one\run.py` 下挂载，`pixiv/run.py` 与 `iwara/run.py` 都返回 404（各站点互不挂对方的路由；asmr 也不需要登录，404 一定是入口不对） |
| `/api/eh/...` 全部 404 | 用错入口了：E-Hentai 接口只在 `python ehviewer\run.py` 下挂载，别的入口都返回 404（互不挂对方的路由；ehviewer 也不需要登录，404 一定是入口不对） |
| ehviewer 入口报缺 `curl_cffi` | 依赖没装上。手动补一次：`pip install -r ehviewer/requirements.txt`（它同时负责 HTTP/3 与换边缘 IP，必需） |
| ehviewer 全部接口都说「N 个候选边缘全部连不上」 | 内置的 16 个 Cloudflare anycast IP 在这个网络里全不通（`EH_BRIDGE_HTTP3=0` 时更容易出现）。换网络，或把实测可用的 anycast IP 写进 `EH_BRIDGE_EDGE_IPS`（逗号分隔，会**覆盖**内置表） |
| ehviewer 取到页面但图片代转失败 | 大图在 `*.hath.network` 的 H@H 节点上，节点会失效且端口随机。默认会重新解析 `/s/` 页换节点重试 `EH_BRIDGE_IMAGE_ATTEMPTS`（默认 4）次；老是失败就调大这个值再试 |
| iwara 入口报缺 `curl_cffi` | 依赖没装上。手动补一次：`pip install -r iwara/requirements.txt`。Termux 上先看 `python3 -V`：**3.13 及以上**能拿到官方安卓轮子（`android_24_arm64_v8a`），装的是现成轮子；**3.12 及以下**没有对应轮子，会退回源码编译（要 Rust + C 工具链），那才是真的装不动——先把 Python 升到 3.13+ 再装，比补底层库管用 |
| iwara 接口返回 `cloudflare_challenge` | 站点把本机 IP 或 TLS 指纹拦了：换个网络/出口 IP 重试，或确认装的是真正的 `curl_cffi`（不要用别的 HTTP 库顶替） |
| iwara 视频能解析出直链但播不动 | 该视频某个 CDN 花名主机（或种子 IP）在本机不通。`/api/iwara/resolve` 默认会逐个源探测并返回能用的那个（`chosen`）；`chosen` 为 `null` 时看 `probe_error` 里已尝试过哪些主机 |
| iwara 全部接口都说「没有可用的候选地址」 | 内置的种子 IP（`iwara/client.py` 的 `SEED_HOSTS` / `SEED_IPS`）在这个网络里全不通，而 DoH 又只给了污染答案。换网络，或把实测可用的 iwara 边缘 IP 补进那张表 |
| `files` 检查项说缺文件 | 这个检查分两处：框架根要 `requirements.txt`，站点目录要自己的 `run.py`（`pixiv/run.py` 或 `iwara/run.py`）。阅读器页面在 `novel/web/index.html`，由扩展提供，不在清单里 |
| 网页打得开但列表空白 | 看启动窗口里的报错；多半是网络被阻断（见第 4 节）。iwara 视频页面（`/iwara`）列表空白时同理，另外它的封面图要走 `/api/iwara/image` 代转，连不上 iwara 图片主机时也会是空白 |
| 设置里保存配置提示「写入失败」 | 先看提示里写的**文件路径和系统原因**：`data\settings.json` 被别的程序占着或带了只读属性（编辑器/同步盘/杀软）最常见，服务会自动摘只读位、重试、再退回直接覆盖写，一般会自动好；仍失败就关掉占用它的程序，或直接删掉该文件（会退回默认值）。**安卓上还要看数据目录是不是在项目外**：`PIXIV_DATA_DIR`/`PIXIV_*_FILE` 指到 `/sdcard/...` 会被自动收回项目里的 `data/`（启动日志和设置页会逐条提示）；项目本身也别放在 `/sdcard` 下，放到 Termux 家目录（`~/pixiv-bridge`）最稳 |
| 保存配置报 `500`，提示里带 `AttributeError: … has no attribute 'model_dump'` | 用的是旧版代码（Termux 装的是 pydantic 1.x，没有 `model_dump`）。换成最新版的 `pixiv_bridge/`（里面有 `compat.py`，两个 pydantic 大版本都认）后重跑 `sh start.sh` |

---

## 7. 需要人工准备的东西（无法自动下载）

1. **Python 3.10+**：Windows 从 python.org 装；Termux `pkg install python`。
2. **能访问 Pixiv 的网络**：被墙时需要代理，或依赖 ECH/SNI 绕过（不一定成功）。
3. **你自己的 Pixiv 登录**：`python pixiv/get_token.py`（需要能连上 Pixiv 才能完成）。
4. **OpenSSL 4.0+**：只有用 ECH 模式才需要；**项目自带 `openssl-libs/`，Windows 与安卓 Termux 版都直接可用**，其它平台缺对应子目录时再自行准备 4.0+（注意 3.x 全系没有 ECH，Termux 官方源也是 3.x，不能用 pip 装）。
5. 完全离线的机器：提前在能上网的同类机器上执行
   `pip download -r requirements.txt -d wheels`，把 `wheels/` 一起拷过去。
