# 站点抽象：框架与接口站点分层（pixiv / iwara 同级）

- 日期：2026-10-08
- 触发需求（用户原话，m00001）："项目中存在错误布局，两个接口项目应该是同级的，错误修正后更新分享包；现在想抽象两个接口项目，方便后续实现其他接口项目"
- 用户已确认的方案（m00066）：`depth=A`（彻底分层）+ `iwara_stack=只提供 iwara 接口`
- 状态：**已完成**（2026-10-08 收尾，35 / 35，见第 7 节尾注与第 8 节实测）。第 12 节是同日追加的「根目录启动＝项目导航」；第 13 节是后续用本抽象落地的第三个站点 `asmr_one/`；第 14 节是第四个站点 `pornhub/`（抓取器）—— 两次加站点都验证了「实现侧零硬编码、只有壳文件与文档要手改」这条抽象承诺；第 15 节是 pornhub 线上探活揪出的传输层致命缺陷及其修复（提交 `28a3c76`），含一条工程教训与一条已知未验证边界；第 16 节是「通过 cloudflare 连接」路径穷举（结论：pornhub 不在 CF 上，本机无法连通，代码零改动）。**第 17 节已按用户决定整体退役 `pornhub/` 站点**（站数回到三个：pixiv / iwara / asmr_one），故第 14～16 节记录的是**已删除**的站点，读时以第 17 节为准。

## 1. Goal

1. **修正布局**：`pixiv/` 与 `iwara/` 成为**同级**的接口项目，各自有同构入口 `pixiv/run.py`、`iwara/run.py`；仓库根的 `run.py` 删除。
2. **抽象**：`pixiv_bridge/` 收缩为**不含任何站点知识**的框架（CLI 基座、站点描述符、扩展机制、导航页、数据目录/JSONL、环境自检基座、依赖自举）。Pixiv 站点代码（路由、api、oauth、image_proxy、ech/sni、hoster、Pixiv 配置与环境检查、get_token）搬进 `pixiv/`。
3. **新增站点成本**：写一个目录（`<site>/app.py` 导出 `SITE`、`<site>/run.py` 约 20 行、可选 `requirements.txt`）+ 在构建侧注册表加一条记录，就能得到一个独立的接口服务；框架不改动。
4. **重新生成分享包**：`dist/` 下六个 zip 按新布局重建（含 iwara 包不再自带 Pixiv 站点代码与 openssl-libs）。

## 2. Baseline（改造前的实况）

- 两个接口项目**不同级**：Pixiv 的入口是仓库根 `run.py` + 根目录下的 `pixiv_bridge/` 包；iwara 的入口是 `iwara/run.py`。
- **框架层长在站点实现里**：iwara 为了用 CLI/自检/扩展/导航，必须 `import pixiv_bridge`，而 `pixiv_bridge` 同时就是 Pixiv 服务本体（`api.py`/`oauth.py`/`routes/*` 都在里面）。
- `pixiv_bridge/cli.py:main()` 硬编码 Pixiv：`run_checks(api_ips/oauth_ips/image_ips/ech/ech_config/ech_ips/proxy/api_base)`、`_warn_network()`（PIXIV_PROXY / PIXIV_BRIDGE_ECH / PIXIV_BRIDGE_BYPASS_SNI 提示）、`_refresh_hint()`、`_check()`（真刷 Pixiv OAuth token）、`MISSING_TOKEN_HINT`。后果：`python iwara/run.py --check` 会去刷 **Pixiv** token 并以其成败决定退出码。
- `pixiv_bridge/server.py:create_app()` lifespan 里直接构造 `httpx.AsyncClient` + `OAuthManager` + `PixivApi` + `ImageProxy`，并注册 Pixiv 异常处理器；`iwara/app.py` 只能靠 monkeypatch `app.router.lifespan_context` 关自己的 client。
- `pixiv_bridge/envcheck.py:REQUIRED_FILES = ("run.py", "requirements.txt")` 与 `project_files()` 默认根 `Path(__file__).parent.parent` 都假定"入口在根"。
- `pixiv_bridge/config.py:Settings` 里混着 Pixiv 常量与字段（client_id/client_secret/hash_salt/api_base/image_referer/redirect_uri/user_agent/app_os*/bypass_sni/ech*/ip 池/refresh_token）与通用字段（host/port/data_dir/台账路径/proxy/extensions/nav_links）。
- 两个入口共用同一个 `cli.main`，靠 `app_factory` / `extra_checks` 两个回调参数注入站点；iwara 因此也继承了 Pixiv 的 `--check`。

## 3. 目标架构

三层，依赖方向严格单向：**站点 → 框架**，**扩展 → 站点 → 框架**。

```
pixiv_bridge/            框架：零站点知识
  site.py      Site 描述符（站点契约的唯一形状）
  cli.py       main(argv=None, site=None)：通用自检 + 站点钩子 + uvicorn
  config.py    Settings 基座（通用字段 + .env 解析 + 数据目录约束/迁移）
  envcheck.py  CheckResult / python_version / dependencies / project_files / port_available / run_checks
  server.py    create_bridge_app(...)：lifespan + 路由挂载 + 错误处理装配
  errors.py    BridgeError + install_error_handlers
  extensions.py / nav.py / jsonl.py / userdata.py / deps.py / compat.py / rate_limit.py
  routes/nav.py  导航页（标题取 app.title）
  __main__.py    python -m pixiv_bridge <site> [...] 通用分发器（目录发现，不 import 站点）

pixiv/                   接口站点：Pixiv
  app.py       SITE + PIXIV_ROUTERS + pixiv_state_factory + create_app
  config.py    PixivSettings(Settings)
  envcheck.py  network / ech_transport / proxy_transport / extra_checks / login_check / network_hint
  api.py oauth.py image_proxy.py ech_client.py sni_client.py hoster.py
  routes/{__init__,auth,illust,search,feed,user,pixiv_novel,misc}.py
  run.py get_token.py requirements.txt(可选)

iwara/                   接口站点：iwara（同级，只提供 /api/iwara/*）
  app.py       SITE + IWARA_ROUTERS + iwara_state_factory + create_app
  envcheck.py  deps_check / network / auth + extra_checks / login_check
  run.py client.py tokens.py get_token.py requirements.txt routes/videos.py

novel/  iwara_web/       扩展（不改层次，只改 import 与文档措辞）
```

### 3.1 站点契约 `pixiv_bridge/site.py`

```python
@dataclass(frozen=True)
class Site:
    name: str                       # "pixiv" / "iwara"
    directory: Path                 # 站点目录（入口所在）
    title: str                      # FastAPI title，也用于导航页标题
    create_app: Callable[..., Any]  # create_app(settings, transport=None, extensions=())
    version: str = "1.0.0"
    settings_class: type[Settings] = Settings
    required_files: tuple[str, ...] = ("run.py",)
    checks: Callable[[Settings], list[CheckResult]] | None = None      # 站点专属环境检查
    login_check: Callable[[Settings], int] | None = None               # --check 的登录校验，返回退出码
    network_hint: Callable[[list[CheckResult], Settings], None] | None = None
```

- `cli.main(argv=None, site=None)`：`site is None` → 明确报错"请用 `<site>/run.py` 启动，或用 `python -m pixiv_bridge <site>`"，退出码 2。
- 通用自检固定为 `[python_version(), dependencies(), project_files(site.directory, site.required_files), port_available(port)]`；站点检查通过 `site.checks(settings)` 追加。
- `--check` 走 `site.login_check`；站点未提供则跳过登录校验（只做环境检查）。
- 提示文案（`network_hint`）由站点提供，框架不出现 "Pixiv" 字样。

### 3.2 框架应用基座 `pixiv_bridge/server.py`

```python
def create_bridge_app(
    settings=None, *, title, version="1.0.0", routers=(), state_factory=None,
    shutdown=None, error_types=(), extensions=(), transport=None,
) -> FastAPI
```

- lifespan：`app.state.settings/extensions` → 依次跑"显式 state_factory + 各扩展 state_factory"（**支持 async**：结果 awaitable 就 await），键值 `setattr(app.state, k, v)` → yield → 倒序 `await ext.shutdown(app)` → `await site_shutdown(app)`。
- `error_types` 由框架用 `install_error_handlers` 装配成统一的 `{"error": {"message": ..., "reason": ...}}`（取 `exc.status` / `exc.reason` / `exc.message`），并**始终**注册 `httpx.HTTPError → 502`（`reason="upstream_error"`）。
- 路由：`routers` 顺序挂载 → `routes/nav.py`（占 `/`）→ 各扩展 routers。先匹配先赢的既有顺序保持。
- 框架**不再**构造 `httpx.AsyncClient`/`OAuthManager`/`PixivApi`/`ImageProxy`（搬到 `pixiv/app.py:pixiv_state_factory`），`transport` 参数保留为站点 create_app 的注入点。

### 3.3 入口同构

```python
# pixiv/run.py
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from pixiv_bridge.deps import bootstrap, ensure_dependencies
bootstrap()
from pixiv.app import SITE
from pixiv_bridge.cli import main
if __name__ == "__main__":
    main(site=SITE)
```

```python
# iwara/run.py
import sys
from functools import partial
from pathlib import Path
HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))
from pixiv_bridge.deps import bootstrap, ensure_packages
bootstrap(partial(ensure_packages, ("curl_cffi",), HERE / "requirements.txt"))
from iwara.app import SITE
from pixiv_bridge.cli import main
if __name__ == "__main__":
    main(site=SITE)
```

扩展仍由 `PIXIV_BRIDGE_EXTENSIONS` 配置驱动，`site.create_app` 负责 `load_extensions_or_exit` 后传入。

### 3.4 配置拆分

- 框架 `Settings` 保留：host、port、data_dir、token_file、history_file、downloads_file、bookmarks_file、config_file、migrated_files、warnings、rate_limit_rpm、accept_language、request_timeout、proxy、project_root、extensions、nav_links、`load()`。
- `pixiv/config.py:PixivSettings(Settings)` 增补：refresh_token、bypass_sni、ech、ech_config、ech_ips、api_ips、oauth_ips、image_ips、client_id、client_secret、hash_salt、api_base、oauth_base、image_base、image_referer、redirect_uri、user_agent、app_os、app_os_version、app_version，以及 Pixiv 默认常量；`PixivSettings.load()` = `Settings.load()` + 这些字段的 env 读取。
- **环境变量名一律不改**（PIXIV_/PIXIV_BRIDGE_ 前缀继续沿用），避免破坏既有 `.env`；文档里说明这些是"框架/站点共用的既有前缀"。
- 有意保留项：四个台账路径（history/downloads/bookmarks/config_file）留在框架 `Settings`（多站点共享的数据目录布局约定），移出属于后续可选项，不在本次范围。

### 3.5 构建侧站点注册表

新增 `scripts/sites.py`（或 `scripts/site_registry.py`）作为**唯一**的站点元数据来源，供：

- `scripts/check_boundary.py`（站点目录、入口、期望路由差集、允许的依赖方向）
- `scripts/make_share_packages.py`（包的 dirs / files / 启动命令 / 标题 / 摘要）
- `tests/test_packaging.py`（断言集合）

新增站点的完整成本 = 写站点目录 + 注册表加一条 +（可选）文档一节。

## 4. Compatibility Boundary（破坏性变更，需在文档明示）

| 变更 | 影响 | 处理 |
| --- | --- | --- |
| 根 `run.py` 删除，改用 `pixiv/run.py` | 所有文档/脚本/启动器/Dockerfile 里的 `python run.py` | 全量替换为 `python pixiv/run.py` |
| `iwara/run.py` 不再挂 Pixiv 路由 | `python iwara/run.py` 之前能访问 `/api/illust/*` 等；现在只有 `/api/iwara/*` + 扩展 | README/INSTALL/DIRECT/分享包说明明示；iwara 分享包不再需要 Pixiv 站点代码 |
| `iwara/run.py --check` 不再刷 Pixiv token | 退出码语义变化 | 改为 iwara 自己的登录检查 |
| 模块路径迁移 | `pixiv_bridge.api/oauth/image_proxy/ech_client/sni_client/hoster/routes/*` → `pixiv.*`；`pixiv_bridge.config.Settings` 的 Pixiv 字段 → `pixiv.config.PixivSettings` | 仓库内测试、脚本、文档同步；对外属框架 DSH 内部 API，不做兼容垫片 |
| 分享包内容变化 | pixiv 系列包含 `pixiv/`；iwara 系列包不再含 Pixiv 站点代码与 openssl-libs | 重新生成 `dist/` 六个 zip，更新包内说明 |
| `pixiv_bridge/__main__.py` 语义变化 | 由"等价于根 run.py"变为"`python -m pixiv_bridge <site>` 分发器" | 文档说明 |

## 5. Change Necessity

- 布局不对称是用户直接指出的缺陷（m00001）——必须修。
- 抽象是为"后续实现其他接口项目"服务：当前新增站点必须复制 `iwara/run.py` 的 `app_factory`/`extra_checks` 技巧，并且无法摆脱 Pixiv 的 `--check`/网络检查/异常处理器；不做分层就无法真正复用。
- 因此本计划不接受"只摆平入口"的最小改动（用户已在 m00066 明确选择彻底分层）。

## 6. TDD Route

`TDD Route: skipped`（Aegis TDD mode = off；用户未要求 strict）。验证以既有 3 个测试文件 + `scripts/check_boundary.py --check` + 打包脚本 `--check` 为准。

## 7. Tasks（分阶段）

### 阶段 1：框架底座（先把基座做成"不认识站点"，此时站点仍走老路）
1. 新增 `pixiv_bridge/site.py`：`Site` 描述符（见 3.1）。
2. 改 `pixiv_bridge/cli.py`：`main(argv=None, site=None)`；通用检查固定为 python/deps/files/port，站点检查走 `site.checks(settings)`；`--check` 走 `site.login_check`；失败提示走 `site.network_hint`；`site.required_files`/`site.directory` 传给 files 检查；`site is None` → 打印"请用 `<site>/run.py` 启动，或 `python -m pixiv_bridge <site>`"并 `sys.exit(2)`。删掉 `_warn_network`/`_refresh_hint`/`_check`/`MISSING_TOKEN_HINT` 与 Pixiv 版 `run_checks` 参数（搬去 `pixiv/envcheck.py`）。
3. 改 `pixiv_bridge/envcheck.py`：保留 `CheckResult`/`python_version`/`dependencies`/`port_available`；`project_files(directory, required_files)` 参数化（默认根改为"框架包所在项目根"，站点文件由站点目录判）；`run_checks(port=None, directory=None, required_files=..., project_root=None)` 只做通用项；移除 `network`/`ech_transport`/`proxy_transport`/`_openssl_source_label`/`CHECK_HOSTS`/`API_BASE`/`ECH_HINT`/`ECH_BLOCKED_HINT` 与 `hoster` 依赖（搬去 `pixiv/envcheck.py`）。
4. 改 `pixiv_bridge/server.py` → `create_bridge_app(settings=None, *, title, version="1.0.0", routers=(), state_factory=None, shutdown=None, error_types=(), extensions=(), transport=None)`：支持 awaitable 的 state_factory/shutdown；`app.state.settings`/`app.state.extensions`；路由 = `routers` → 框架 `nav` 路由（占 `/`）→ 扩展路由；不再构造 httpx/OAuthManager/PixivApi/ImageProxy。
5. 新增 `pixiv_bridge/errors.py`：`BridgeError`（`status`/`reason`/`message`）+ `install_error_handlers(app, error_types)`，统一 `{"error": {...}}`，并始终注册 `httpx.HTTPError → 502`。
6. 新增框架路由 `pixiv_bridge/routes/health.py`（`/api/health`，从 `pixiv_bridge/routes/misc.py` 拆出，保证每个站点天生有健康检查、导航页 BUILTIN_ITEMS 的链接不作废）；`pixiv_bridge/routes/__init__.py` 改为纯 docstring 空壳。
7. 改 `pixiv_bridge/nav.py`：`DEFAULT_TITLE` 中立化；`render_page(app)` 用 `getattr(app, "title", DEFAULT_TITLE)`；docstring 去 Pixiv 专属措辞（`page_sections` 的 `nav_sections` 机制保留）。
8. 改 `pixiv_bridge/routes/nav.py`：标题取 `app.title`。
9. 改 `pixiv_bridge/__main__.py`：`python -m pixiv_bridge <site> [args...]` —— 在框架包所在项目根扫描含 `run.py` 的目录，用 `runpy.run_path(..., run_name="__main__")` 执行并传递 `sys.argv`；无参/未知站点 → 列出可用站点并 `sys.exit(2)`（纯目录发现，不 import 站点）。
10. `deps.py`/`extensions.py`/`jsonl.py`/`userdata.py`/`compat.py`/`rate_limit.py`：只改 docstring 里的路径措辞（`run.py` 的指代）。

### 阶段 2：Pixiv 站点成立
11. `git mv` 搬迁：`pixiv_bridge/{api,oauth,image_proxy,ech_client,sni_client,hoster}.py` → `pixiv/`；`pixiv_bridge/routes/{auth,illust,search,feed,user,pixiv_novel,misc}.py` → `pixiv/routes/`；`pixiv_bridge/routes/__init__.py`（`get_api`/`get_oauth`/`get_image_proxy`）→ `pixiv/routes/__init__.py`；根 `get_token.py` → `pixiv/get_token.py`（`PROJECT_ROOT` 由 `.parent` 改 `.parent.parent`，`--env` 帮助文本同步）；根 `run.py` 删除。
12. 新增 `pixiv/config.py:PixivSettings(Settings)`：从 `pixiv_bridge/config.py` 拆出 Pixiv 常量与字段 + 这些字段的 env 读取；`PixivSettings.load()` = `Settings.load()` + Pixiv 部分。框架 `Settings` 只留通用字段（保留四个台账路径）。
13. 新增 `pixiv/envcheck.py`：搬入 `network`/`ech_transport`/`proxy_transport`/`_openssl_source_label`/常量与提示；新增 `extra_checks(settings)`（保持 `cli` 原调用语义）、`login_check(settings)`（= 原 `cli._check`，返回退出码）、`network_hint(results, settings)`（= 原 `cli._warn_network`）、`_refresh_hint`、`MISSING_TOKEN_HINT`。
14. 新增 `pixiv/app.py`：`TITLE`/`VERSION`/`PIXIV_ROUTERS`/`PIXIV_ERROR_TYPES`/`async def pixiv_state_factory(settings, transport=None)`（建 http → `oauth.load()` → api → image_proxy）/`async def pixiv_shutdown(app)`（`await app.state.http.aclose()`）/`create_app(settings=None, transport=None, extensions=(), extra_routers=(), extra_state_factory=None)`/`SITE = Site(...)`。
15. 新增 `pixiv/run.py`（同构入口，见 3.3）。
16. 站点内 import 改写：框架模块引用改 `pixiv.…`/相对导入；`pixiv_bridge.routes.health` 由框架提供；异常基类改挂 `pixiv_bridge.errors.BridgeError`（`PixivApiError`/`PixivRateLimitError`/`AuthError` 补齐 `status`/`reason`/`message` 属性以适配统一处理器）。

### 阶段 3：iwara 站点收口（只提供 iwara 接口）
17. 改 `iwara/app.py`：`TITLE = "Iwara Bridge"`；`create_app(settings=None, transport=None, extensions=())` 改用 `create_bridge_app`（`routers=IWARA_ROUTERS`、`state_factory=iwara_state_factory`、`shutdown=iwara_shutdown`、`error_types=()`）；删掉 `_close_client_on_shutdown` monkeypatch；`IWARA_NAV` 标题/说明改写（不再自称"本入口独有"、不再提 Pixiv 路由）；导出 `SITE`。
18. 改 `iwara/run.py`：同构入口（保留 `curl_cffi` 自举，仍在 import `iwara.app` 之前）。
19. 改 `iwara/envcheck.py`：`extra_checks` 保持语义；新增 `login_check(settings)`（复用现有 `_auth_check`，无 token 视为通过并提示匿名模式）；import 路径更新。

### 阶段 4：扩展与构建脚本
20. 改 `novel/app.py`：`create_app` 组合 pixiv 站点（`pixiv.app.create_app(..., extra_routers=NOVEL_ROUTERS, extra_state_factory=novel_state_factory)`），顺带修好"独立 novel 应用缺 `app.state.api`"的既有缺口；`novel/extension.py` 文档措辞去 `python run.py`。
21. 新增 `scripts/sites.py`：站点注册表（站点名/目录/入口/标题/依赖包/扩展宿主/期望路由差集/分享包元数据）作为构建与校验的单一来源。
22. 重写 `scripts/check_boundary.py` 基于注册表：①框架不得 import 任何站点；②站点只依赖框架（+自身），扩展只依赖框架 + 宿主站点 + 自身；③入口 = 各站点 `run.py`，且根 `run.py`、扩展 `run.py` 必须不存在；④路由：框架基线（仅 `/` + docs 内建）→ 各站点差集，扩展相对**宿主站点**的差集，并断言站点之间互不串路由（pixiv 无 `/api/iwara/*`，iwara 无 `/api/illust|search|feed|user|…`）。
23. 改 `scripts/make_share_packages.py`：`API_ROOT_FILES` 去掉 `run.py`（站点入口随站点目录进包）；六个包 dirs 调整（pixiv 系列加 `pixiv/`；iwara 系列去掉 `openssl-libs`）；包内 README/启动命令/说明全部改 `python pixiv/run.py`、`python iwara/run.py`；`get_token.py` 条目改 `pixiv/get_token.py`。
24. 改 `tests/test_packaging.py`：沿用注册表断言（仍是 6 个包、key/zip 名唯一、声明条目存在、非扩展包内容、扩展包只带自己目录、start_commands 含 `PIXIV_BRIDGE_EXTENSIONS=`、stage/zip 往返、`--check` 不写 zip）。

### 阶段 5：测试、入口脚本与镜像
25. 改 `tests/test_bridge.py`：import 与 patch 目标（`from pixiv.config import PixivSettings as Settings`、`from pixiv.app import create_app`、`pixiv.api`/`pixiv.oauth`/`pixiv.image_proxy`/`pixiv.ech_client`/`pixiv.sni_client`/`pixiv.hoster`、`pixiv.envcheck` 的 network/ech/proxy vs `pixiv_bridge.envcheck` 的通用项，`pixiv_bridge.routes.health`）；`ProjectFilesTestCase`（L2714）、`CliCheckTestCase`（L4915）、`NavigationPageTestCase`（L4727）、`ExtensionLayoutTestCase`（L4675）、`EchEnvCheckTestCase`（L1846）、`RefreshHintTestCase`（L2017）的断言按新契约更新。
26. 改 `tests/test_iwara.py`：import（`pixiv_bridge.config.Settings` 仍适用；框架基座改 `create_bridge_app`）与入口断言（`IwaraEntryPointTestCase` L1166、`IwaraEnvCheckTestCase` L1246、`IwaraEntryWiringTestCase` L1376、L1380 读 `iwara/run.py` 内容）。
27. 改 `start.bat` / `start.sh`：支持可选站点参数（默认 `pixiv`），实际启动命令改 `python <site>\run.py`（`<site>/run.py`），用法块与注释同步；保持 start.bat 的 ASCII-only 约束。
28. 改 `Dockerfile`：新增 `COPY pixiv pixiv`，删 `COPY run.py .`，`CMD ["python", "pixiv/run.py"]`，注释里的"默认跑纯接口/改跑 iwara"语义更新。
29. 改 `requirements-termux.txt` 注释措辞（自动使用本文件的是 `pixiv/run.py`）。

### 阶段 6：文档
30. `README.md` 55 处：入口路径 22（L76/82/103/127/163/177/194/200/234/235/244/401/404/558/719/843/856/865/866/882/890/891）、iwara 范围 10（L7/21/133/145/146/148/241/397/471/578/709/735/867）、包结构 4（L7/139/143/148/547）、分享包 7（L715/723/734/735/737/738/739）、必需文件 4（L173/708/749/753）、数量 2（L139/365，数字不变、身份描述要改）、目录树整块重写（L257–332）、其他措辞（L311/344/346/349/359/368）。
31. `INSTALL.md` 20 处：入口路径 9（L19/83/143/167/185/307/308/309/328）、iwara 范围 7（L9/60/189/255/256/258/268）、数量 1（L260）、分享包 1（L284）、必需文件 1（L317）、整块重写（L253–258、L266–273、L282–291）、L122–133/L253–258 补"站点参数"。
32. `DIRECT.md` 14 处：`pixiv_bridge/{hoster,sni_client,ech_client,api}.py` → `pixiv/…`（L23/25/26/60/82/105/108/109/115/171/210）、四道关表 L21–26、两站点对照表 L203–213、L243 起步命令。
33. `.env.example`：**无需修改**（审计确认：L22–29、L54 的 iwara_web↔`/api/iwara/*` 依赖关系在重构后仍成立）。
34. `pixiv_bridge/__init__.py` 版本号提升（框架语义变化），并在 README 的项目结构里点明"框架 vs 站点"。

### 阶段 7：验证与交付
35. 全量测试 + 边界检查 + 打包 `--check`，重新生成 `dist/` 六个 zip；把结果与实测命令记回本计划（第 8 节）。

> **状态：35 / 35 完成**（2026-10-08 收尾）。第 8 节记的是真正跑过的命令与实测输出；与计划文字有出入的只有两处，已就地注明：本机 `.venv` 没装 pytest（改用 `unittest`，并补了 `tests/__init__.py` 让 `discover` 能跑）、`dist/` 里两个旧 key 名的 zip（`pixiv_bridge-api.zip` / `pixiv_bridge-novel.zip`）被新 key 名的包取代而不是同名覆盖。

## 8. Verification

实测记录（2026-10-08，Windows / Python 3.13.15 / cwd = 项目根）：

- **全量测试**：`.venv\Scripts\python.exe -m unittest discover -s tests` → `Ran 457 tests in 11.241s` / `OK`（`test_bridge` 314 + `test_iwara` 98 + `test_packaging` 45）；`discover -t . -s tests` 同样 457 项 OK。（**后续追加**：加完第 12 节的 `NavigationTestCase` 20 项后为 477 项 —— 框架与站点 334 + iwara 98 + 打包 45，复跑仍全绿。）
  - 偏差：本机 `.venv` 里**没有 pytest**，`python -m pytest` 不可用；`tests/` 原先没有 `__init__.py`，`unittest discover -s tests` 会报 `ImportError: Start directory is not importable`，已补 `tests/__init__.py`。
- **边界检查**：`.venv\Scripts\python.exe scripts\check_boundary.py` → `框架外壳 6 条路由` / `pixiv 60 条路由（其中站点自己的 54 条）` / `iwara 16 条路由（其中站点自己的 10 条）` / `novel（扩展，宿于 pixiv）: 比宿主多 10 条路由` / `iwara_web（扩展，宿于 iwara）: 比宿主多 1 条路由` / **边界检查通过**。
- **打包自检**：`.venv\Scripts\python.exe scripts\make_share_packages.py --check` → 六个包内容就绪：`pixiv-api` 59、`iwara-api` 43、`novel-extension` 17、`iwara_web-extension` 8、`pixiv-novel` 72、`iwara-iwara_web` 47 个文件 → `分享包处理完成`。
- **重新生成 `dist/`**：`.venv\Scripts\python.exe scripts\make_share_packages.py` → `pixiv-api.zip` 59 文件 15.97 MB（压缩后 5.81 MB）、`iwara-api.zip` 43 文件 0.31 MB（0.13 MB）、`novel-extension.zip` 17 文件、`iwara_web-extension.zip` 8 文件、`pixiv-novel.zip` 72 文件 16.15 MB（5.87 MB）、`iwara-iwara_web.zip` 47 文件。旧 key 名的 `pixiv_bridge-api.zip` / `pixiv_bridge-novel.zip` 与 `dist/.stage/` 已删除（key 改名后不会被覆盖，脚本本身不清理异名 zip）。
- **入口烟测**（全部 `--check`）：
  - `.venv\Scripts\python.exe pixiv\run.py --check` → 表里 `python/deps/files/port/ech` 五项 OK（ECH：`app-api.pixiv.net 返回 HTTP 400，OpenSSL：项目自带`）→ `登录成功: huangee (user_mcys7857)` → exit 0。
  - `.venv\Scripts\python.exe iwara\run.py --check` → 前三项 + `iwara 依赖 OK curl_cffi 0.16.3` / `iwara 网络 OK api.iwara.tv 可达（共 2 个视频）` / `iwara 登录 OK token 有效：huanghe19512` → exit 0；`IWARA_REFRESH_TOKEN=definitely-not-a-real-token` 时该行变 `WARN token 不可用：刷新 token 失败（403）` 且 exit 1。
  - `.venv\Scripts\python.exe -m pixiv_bridge` → `用法: python -m pixiv_bridge <站点> [入口参数...]` / `可选站点: iwara, pixiv` → exit 2；`python -m pixiv_bridge iwara --check` 与 `iwara\run.py --check` 输出一致（同一 `SITE`）。**（已被第 12 节取代**：无参现在是项目导航 —— 有终端就列菜单、没有终端就打一行提示并直接启动默认接口项目，不再打印用法后 exit 2；`--list` / `--help` 只看导航。**）**
  - 站点隔离：`Get-ChildItem -Recurse -Filter run.py -Depth 2` 只列出 `iwara\run.py`、`pixiv\run.py`（根 `run.py`、`novel\run.py`、`iwara_web\run.py` 都不存在）。
- **旧引用残留检查**：`pixiv_bridge.(api|oauth|image_proxy|ech_client|sni_client|hoster)` / `from pixiv_bridge.routes.(auth|illust|search|feed|user|pixiv_novel|misc)` / `pixiv_bridge.server import create_app` 在 `*.py,*.md,*.txt,*.bat,*.sh,scripts/,pixiv/,iwara/,novel/,iwara_web/,pixiv_bridge/,tests/` 全库扫描 **0 命中**；`scripts/*.py` 里残留的 `"run.py"` 都是站点目录内的（`sites.py:41` 的 `files=("run.py",)`、`sites.py:173` 注释「根目录不再有入口」、`make_share_packages.py:410` 的 `target/name/"run.py"`、`check_boundary.py:107` 的 `rel == "run.py"`）。
- **分享包抽检**（解压到临时目录，**cwd 设为包根**再跑，否则会读到工作区的 `.env`）：
  - `iwara-api.zip` → 包内 `iwara\run.py --check`：`[环境检查]` 七项全 OK、无扩展挂载（包内没有 `.env`）、`iwara 登录 OK 匿名模式…` → exit 0；包内不含 `pixiv/`。
  - `pixiv-api.zip` → 包内 `pixiv\run.py --check`：五项 + `network OK 可达 3/3: app-api.pixiv.net, oauth.secure.pixiv.net, i.pximg.net`，随后是新装的提示（`缺少 refresh_token…` + 让跑 `python pixiv/get_token.py`）→ exit 1（预期：尚未登录）；包内没有根 `run.py`，`pixiv_bridge\api.py` 不存在。
- **顺带修掉的一个重复**：`iwara --check` 原先会打印两次 `iwara 登录 OK …`（`Site.checks` 一次、`Site.login_check` 一次）并真的刷两次 token；现在 `iwara/envcheck.py` 用进程内 `_AUTH_MEMO`（元素 `(token, CheckResult)`）缓存，`extra_checks()` 每次开头清空缓存、`login_check()` 只复用不重跑也不再重复打印。`tests.test_iwara` 98 项在此改动后复跑仍 OK。
- **顺带修掉的一个打包缺陷（Dockerfile）**：旧 Dockerfile 无条件 `COPY pixiv pixiv` / `COPY novel novel` / `COPY iwara/requirements.txt …`，而 `iwara-api.zip` 里根本没有 `pixiv/`、`novel/`，`pixiv-api.zip` 里没有 `iwara/` —— 两份分享包里的 Dockerfile 都构建不起来。现在改成「`COPY requirements.txt .` → 装框架依赖 → `COPY . .`（框架 + 镜像里**实际存在**的站点与扩展）→ 有 `iwara/requirements.txt` 才装 `curl_cffi`」，`CMD` 改成 `["sh", "start.sh"]`（启动脚本现在只是转发给框架的项目导航：容器里没有可交互终端，所以它不声不响直接启动默认接口项目 —— 无 `pixiv/` 的包里默认就是 iwara，`-e BRIDGE_SITE=iwara` 可显式指定；见第 12 节）。实测：四个 API/组合包里的 Dockerfile 末行都是 `CMD ["sh", "start.sh"]`，扩展包（`novel-extension` / `iwara_web-extension`）**不含** Dockerfile，只带 `README.md` + 扩展目录 + `手册/`。**未做**：本机 Docker Desktop 守护进程没运行（`docker --version` → 29.7.2，但 `docker images` 报 `failed to connect to the docker API at npipe:////./pipe/dockerDesktopLinuxEngine`），所以没有真跑 `docker build` 验证。
- README 的 Docker 段同步改为「镜像入口是 `CMD ["sh", "start.sh"]`，它转发给框架的项目导航；容器里没有可交互终端，所以直接启动默认接口项目」，并把 ECH 那段「再 COPY 进镜像」改成「放进 `openssl-libs/linux-x86_64/`，`COPY . .` 会一起带进去」。

## 9. Risks

- **测试改写量大**（`tests/test_bridge.py` 217 KB、`tests/test_iwara.py` 60 KB）：漏改 import/patch 目标会大面积报错。缓解：先 `grep` 出全部 `pixiv_bridge.(api|oauth|image_proxy|ech_client|sni_client|hoster|envcheck|config|server)` 引用点，逐条映射，再跑测试收口。
- **`Settings` 拆分漏搬 env**：`PIXIV_REFRESH_TOKEN`/`PIXIV_BRIDGE_ECH*`/ip 池若没跟着搬进 `PixivSettings.load`，Pixiv 站点会静默丢配置。缓解：`PixivSettings.load` 只做"基座 load + Pixiv 字段"，并用既有 `EchSettingsTestCase`（L1372）、`SettingsEnvPathTestCase`（L1141）、`EnvCheckIpPoolTestCase`（L1185）兜底。
- **循环导入**：`pixiv/app.py` → `pixiv/config.py`/`pixiv/routes/*` 单向；`pixiv` 不得反过来被框架 import（`check_boundary` 守住）。`novel` 允许 import `pixiv`（扩展→宿主站点）。
- **破坏性行为变化**：`python iwara/run.py` 不再提供 Pixiv 接口（用户已确认，m00066），旧用户脚本可能依赖 `/api/illust/*`；必须在 README/INSTALL 与分享包说明顶部显著提示。
- **根入口消失**：习惯 `python run.py` 的用户会报错。缓解：README 快速开始改为 `python pixiv/run.py`；`python -m pixiv_bridge <site>` 提供等价入口；start.bat/start.sh 默认仍是最常用的 Pixiv 站点。
- **`openssl-libs` 从 iwara 包移除**：只有 Pixiv 的 ECH/SNI 需要（确认 `ech_client.bundle_root()` 与 `sni_client` 的使用面）；若 iwara 直接或间接依赖，移除会破坏其 ECH 能力 —— 以 `check_boundary` 的依赖检查与 iwara 测试为准。
- **框架包名仍叫 `pixiv_bridge`**：观感上仍以 Pixiv 命名，属用户明确选择（m00066 未选改名 `bridge_kit`），记为已知残留。

## 10. Retirement

- 删除：根 `run.py`；`pixiv_bridge/{api,oauth,image_proxy,ech_client,sni_client,hoster}.py`（→ `pixiv/`）；`pixiv_bridge/routes/{auth,illust,search,feed,user,pixiv_novel,misc}.py`（→ `pixiv/routes/`）；根 `get_token.py`（→ `pixiv/get_token.py`）；`pixiv_bridge/envcheck.py` 的 Pixiv 检查与提示；`pixiv_bridge/server.py` 的 Pixiv 对象装配与异常处理器；`iwara/app.py:_close_client_on_shutdown` monkeypatch；`scripts/check_boundary.py` 的 `MAIN_PACKAGE`/`SUB_PACKAGES`/`ENTRY_POINTS` 旧常量（由 `scripts/sites.py` 取代）；`pixiv_bridge/config.py` 的 Pixiv 常量（→ `pixiv/config.py`）。
- 覆盖：`dist/` 六个旧 zip 由新生成的同名 zip 取代（`dist/` 已 gitignore）。
- 保留（有意）：框架包名 `pixiv_bridge`；`PIXIV_`/`PIXIV_BRIDGE_` 环境变量前缀；四个台账路径字段留在框架 `Settings`。

## 11. 影响面清单（只读审计结果）

### 文档与脚本（106 处，已确认）

| 文件 | 入口路径 | iwara 范围 | 包结构 | 分享包 | 必需文件 | 目录树 | 数量 | 其他 | 合计 |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| README.md | 22 | 10 | 4 | 7 | 4 | 1 | 2 | 5 | 55 |
| INSTALL.md | 9 | 7 | 0 | 1 | 1 | 0 | 1 | 1 | 20 |
| DIRECT.md | 1 | 0 | 13 | 0 | 0 | 0 | 0 | 0 | 14 |
| start.bat | 2 | 0 | 0 | 0 | 0 | 0 | 0 | 1 | 3 |
| start.sh | 2 | 0 | 0 | 0 | 0 | 0 | 0 | 1 | 3 |
| Dockerfile | 3 | 1 | 0 | 0 | 0 | 0 | 0 | 1 | 5 |
| requirements-termux.txt | 0 | 0 | 0 | 0 | 1 | 0 | 0 | 0 | 1 |
| get_token.py | 3 | 0 | 0 | 0 | 2 | 0 | 0 | 0 | 5 |
| .env.example | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 |

关键补充结论：

- `get_token.py:30` 的 `PROJECT_ROOT = Path(__file__).resolve().parent` 下沉一层后必须改 `.parent.parent`，否则 `.env`（在仓库根）找不到。
- 数字 2（接口项目/入口）/4（项目块）/6（分享包）本身仍成立，**要改的是身份与范围**，不要机械改数。
- README 未受影响的计数：L13「55 个端点」、L324「443 项：345 主项目 + 98 iwara」、L713「98 项」、L715「37 项」——按新布局复核，必要时随路由变化更新。
- 整块重写（不是换词）：README L141–148、L257–332、L732–739、L747–760；INSTALL L253–258、L266–273、L282–291；DIRECT L21–26、L203–213；start.bat L4–14/L63–65；start.sh L4–15/L57–59；Dockerfile L16–20/L22–26/L33–39；pixiv/get_token.py L3–8/L116/L120–127。

### 测试（待测试影响面审计补齐）

已知锚点：`tests/test_bridge.py:27-65`（import 块）、`:3258`/`:3266`（jsonl/userdata import）、`:2714` `ProjectFilesTestCase`、`:4675` `ExtensionLayoutTestCase`、`:4727` `NavigationPageTestCase`、`:4915` `CliCheckTestCase`、`:1846` `EchEnvCheckTestCase`、`:2017` `RefreshHintTestCase`、`:3955` `SettingsDataDirTestCase`；`tests/test_iwara.py:27-28`（import 块）、`:1166`/`:1246`/`:1376` 三个入口/自检用例、`:1380` 读 `iwara/run.py`；`tests/test_packaging.py` 全量期望。

---

## 12. 后续追加：根目录启动＝项目导航（用户 m01107）

需求原文（m01107）："提交git，根目录启动是整个项目的导航"。做法由用户选定（m01121）：**启动器 / 菜单** —— 根目录启动时列出全部接口项目（+扩展），选一个再启动。被否掉的两种：门户服务（一个进程挂全部站点 + `/` 导航页）、只打印导航说明后退出。

### 12.1 设计（四条，都是取舍）

1. **挑选逻辑只写一份**，放在 `pixiv_bridge/__main__.py`：`start.bat` / `start.sh` 退化成转发器（`exec "$PY" -m pixiv_bridge "$@"` / `%PY% -m pixiv_bridge %*`）。理由：在 sh/bat 里各抄一套「发现＋排序＋询问」必然与 Python 侧漂移，而且 shell 读不到 `nav_order`。
2. **框架仍然不认识任何站点**：导航要的元数据全部用 `ast` 从站点/扩展**自己的文件**里读**字面量**（`<站点>/app.py` 的 `SITE = Site(...)` 取 `title` / `nav_order`；`<扩展>/extension.py` 的 `EXTENSION_NAV` 取标题与简介，读不到就用模块 docstring 第一段），**不 import、不执行站点代码**；选完才 `runpy.run_path(<站点>/run.py, run_name="__main__")`。站点顺序因此由站点自己声明：`Site` 契约新增 `nav_order: int = 100`（`pixiv/app.py` 写 `nav_order=1`、`iwara/app.py` 写 `nav_order=2`），框架里没有一个站点名。
3. **发现规则**：有 `run.py` 的目录＝接口项目；只有 `extension.py`（没有 `run.py`）的目录＝扩展。两者都按 `(nav_order, 目录名)` / 目录名排序。
4. **没有可交互终端时不挂住、也不退出**：`sys.stdin.isatty()` 为假时打一行 `[i] 没有可交互的终端：启动默认接口项目 <第一个>（--list 看全部，BRIDGE_SITE 可指定）` 然后直接启动第一项；只有一个接口项目时直接启动、连问都不问。理由：`Dockerfile` 的 `CMD ["sh", "start.sh"]` 没有 TTY，报错退出会让容器起不来。老行为（无参打印用法后 exit 2）由这条取代。
5. `BRIDGE_SITE` 最优先：先环境变量，再读项目根 `.env` 的同名行（复用框架自己的 `pixiv_bridge/config.py:_load_dotenv`，它用 `os.environ.setdefault`，所以环境变量赢）；写错就打 stderr 一行并以 exit 2 退出，而不是默默启动别的。

接口：`python -m pixiv_bridge`（导航）、`python -m pixiv_bridge <接口项目> [入口参数...]`（直启）、`python -m pixiv_bridge --list`（只打印）、`-h` / `--help`（用法 + 导航）。

改动文件：`pixiv_bridge/__main__.py`（重写）、`pixiv_bridge/site.py`（+`nav_order`）、`pixiv/app.py`、`iwara/app.py`、`iwara/__init__.py`（docstring 第一段就是导航简介）、`start.bat`、`start.sh`、`tests/test_bridge.py`（+`NavigationTestCase` 20 项，含 `_FakeTty`）、`README.md`、`INSTALL.md`、`DIRECT.md` §6、`scripts/make_share_packages.py`（`BASE_NOTES` 加一条「双击 `start.bat` 走的是项目导航，本包只有一个接口项目所以直接启动」）。

### 12.2 实测（2026-10-08，Windows / Python 3.13.15 / cwd = 项目根）

- **全量测试**：`.venv\Scripts\python.exe -m unittest discover -s tests` → `Ran 477 tests in 11.431s` / `OK`（第 8 节的 457 + 本节的 20）。**边界检查**：`scripts\check_boundary.py` → 外壳 6 / pixiv 60（自己 54）/ iwara 16（自己 10）/ novel +10 / iwara_web +1 → `边界检查通过`。**打包自检**：`scripts\make_share_packages.py --check` → 59 / 43 / 17 / 8 / 72 / 47 → `分享包处理完成`。
- **`--list`**：exit 0，输出 `pixiv-bridge —— 项目导航（E:\github\pixiv-bridge）` → `1) pixiv     Pixiv Bridge —— Pixiv 接口站点：…`、`2) iwara     Iwara Bridge —— iwara.tv 接口站点：…` → 扩展一段 `iwara_web iwara 视频页 —— …`、`novel     小说阅读器 —— …` → 用法 → `提示: BRIDGE_SITE=pixiv 可让 start.bat / start.sh 跳过询问、直接启动它`。
- **选择逻辑 10 条**（`choose_site` 注入 `ask` / 非 TTY 流）：非 TTY → `[i] 没有可交互的终端：启动默认接口项目 pixiv（--list 看全部，BRIDGE_SITE 可指定）` 并返回第一项；答 `2` → iwara、答 `iwara` → iwara、答空 → pixiv、答 `1` → pixiv；答 `xxx` / `9` → `没听懂「…」：输入编号 1-2，或接口项目名（pixiv, iwara）` 并重问（连错两次后答对仍能起）；`BRIDGE_SITE=iwara` → iwara，`BRIDGE_SITE=nope` → stderr `BRIDGE_SITE=nope 不是本目录里的接口项目（可选：pixiv, iwara）` + 返回 None；`main(["nosuch"])` → exit 2 + stderr `没有这个接口项目: nosuch（可选：pixiv, iwara）`；单站点列表直接返回、不询问；`--help` 先用法后导航、`--list` 只打印。
- **脚本**：`start.bat` 3781 字符、仍**纯 ASCII**（无 `[^\x00-\x7F]` 命中）、含 `python -m pixiv_bridge`、不再有 `set "SITE=`；`start.sh` 末尾 `exec "$PY" -m pixiv_bridge "$@"`（`bash -n start.sh` 在本机沙箱被拒 `E_ACCESSDENIED`，改人工逐行核对 + 测试断言两个脚本都含 `-m pixiv_bridge`）。踩过的坑：PowerShell 里 `Get-Content -Encoding Byte` 在 PS7 报 `'Byte' is not a supported encoding name`，会让 ASCII 检查假通过；要用 `Get-Content -Raw` + `-match '[^\x00-\x7F]'`。
- **分享包抽检**（解压到临时目录、**cwd 设为包根**，包内顶层是 `<包名>/`）：`iwara-api` 里 `python -m pixiv_bridge --check` → 无参非 TTY 直接选 iwara（唯一接口项目），七项全 OK（`iwara 网络 OK api.iwara.tv 可达（共 2 个视频）`、`iwara 登录 OK 匿名模式…`）→ exit 0；`pixiv-api` 里 `--list` → 只有 `1) pixiv`、扩展那行是 `（没有找到：扩展目录里需要有 extension.py）`、提示 `BRIDGE_SITE=pixiv` → exit 0；包内 `start.bat` 纯 ASCII 且含 `python -m pixiv_bridge`，包内没有根 `run.py`。
- **`dist/` 重建**：`.venv\Scripts\python.exe scripts\make_share_packages.py`（无参）→ `pixiv-api` 59 文件 15.99 MB（压缩 5.82 MB）、`iwara-api` 43 / 0.32 MB（0.14）、`novel-extension` 17 / 0.32 MB、`iwara_web-extension` 8 / 0.17 MB、`pixiv-novel` 72 / 16.16 MB（5.88）、`iwara-iwara_web` 47 / 0.35 MB；`dist/` 下无旧 key 名 zip、无 `.stage/` 残留。
- **仍未验证**：Docker 只做静态核对（本机守护进程没运行，`docker build` 未跑），与第 8 节同一限制。

## 13. 后续追加：第三个站点 `asmr_one/`（抽象本身的验收）

需求原文（m01648）："尝试做asmr-one的接口项目"；m01776 用户批准按 `Site` 契约搭骨架，m02454「继续」推进到收尾。

这一节的价值不在于新增功能，而在于**用一次真实落地检验第 1 节的「新增站点成本」承诺**：写一个目录 + 在构建侧注册表加一条记录，框架零改动。实测结论：框架 `pixiv_bridge/` **没有为 asmr 改过一行**，改动全部落在新站点目录与三处构建/测试侧的写死假设上。

### 13.1 新增的站点目录

`asmr_one/` 共 10 个文件：`__init__.py`（docstring 第一段就是导航简介，与 iwara 同构；`__getattr__` 惰性导出）、`ech.py`、`config.py`、`client.py`、`routes/__init__.py`、`routes/works.py`、`app.py`、`envcheck.py`、`run.py`（三段式）、`requirements.txt`（`curl_cffi>=0.7` + `cffi>=1.16`）。

- `app.py`：`SITE = Site(name="asmr_one", title="ASMR Bridge", nav_order=3, settings_class=AsmrSettings, checks=extra_checks, ...)`，**没有 `login_check`**（asmr.one 纯匿名可用）。
- `routes/works.py` 七条 `/api/asmr/*`：`/works`、`/search`、`/work/{id}`、`/tracks/{id}`、`/cover/{id}`、`/stream`、`/auth`。
- `config.py`：`ASMR_BRIDGE_ECH` 用 `_flag_or_default("ASMR_BRIDGE_ECH", True)` —— 与 iwara 相反，**不写这个变量就是开**（asmr 的 Cloudflare 边缘不带 ECH 会被拦），要关必须显式写 `=0`。
- `envcheck.py`：三项 `asmr 依赖` / `asmr 网络` / `asmr ECH`；`ASMR_BRIDGE_ECH=0` 关掉后没有 ECH 那一项。
- 与 iwara 同骨架：种子 IP 池 + `curl_cffi impersonate="chrome"` + DoH，全池轮询换 IP 重试。
- 站点间禁止互相 import（`check_boundary.py` 会拦），所以 `asmr_one/ech.py` 是 `iwara/ech.py` 的**自足副本**。

### 13.2 抽象承诺暴露出的四处写死假设（都已动态化）

搭第三个站点时撞到的都是「两个」写死在代码里，而不是框架设计缺陷 —— 这正是这次落地的收获（前三处是搭站点时就发现的，第四处是收尾复查时才发现的）：

1. `scripts/check_boundary.py`：`ROUTE_PROBE` 里写死的 `{'pixiv': pixiv_app, 'iwara': iwara_app}[...]` 与 `check_apps` 里只认 iwara 的路由特判 → 换成由 `_probe_source()` 生成的 `module_map` 与遍历 `SITE_ROUTE_PATHS`。
2. `scripts/make_share_packages.py`：`validate_specs()` 里 `== 2` / `== 6` 两条硬编码 → 改成 `len(SITES)` / `len(SITES) + 4`；并新增 `asmr-api` 包。
3. `tests/`：`test_bridge.py` 的 `NavigationTestCase` 与入口同构两处、`test_packaging.py` 的包数断言、`test_iwara.py` 的措辞断言，写死「两个接口项目 / 两个站点」→ 改为遍历 `pixiv_bridge.__main__` 的 `site_items()` / `extension_items()` 与 `scripts/sites.py` 的注册表。
4. 首尾两道壳里的站点专属假设（**第二次排查才补上**）：`Dockerfile` 只写了 `if [ -f iwara/requirements.txt ]` 就装 `curl_cffi`，`asmr-api.zip` 的镜像里因此少装一份站点的额外依赖（`deps.py` 运行时会自己补，所以不是致命错，但镜像该预装好）；`scripts/check_boundary.py` 的导入失败提示只认 `curl_cffi` → `pip install -r iwara/requirements.txt`。两处都改成遍历 `SITES`：Dockerfile 用 `for req in iwara/requirements.txt asmr_one/requirements.txt`，检查脚本按 `site.extra_modules` 命中哪个站点就提示哪份 requirements。教训：`scripts/sites.py` 是唯一事实来源，但**壳文件（Dockerfile / 启动脚本 / 检查脚本的提示文案）不会因为加了注册表记录就自动跟上**，加站点时要顺手 grep 一遍。

### 13.3 实测（2026-10-08 之后，Windows / Python 3.13）

- **边界检查**：`python scripts/check_boundary.py` → pixiv 60（自带 54）、iwara 16（自带 10）、**asmr_one 13（自带 7）**、novel ↗pixiv +10、iwara_web ↗iwara +1 → `边界通过`。
- **全量测试**：`python -m unittest discover -s tests` → **Ran 712 tests … OK (skipped=1)**，约 16 s。构成为 bridge 334 + iwara 199 + **asmr_one 134**（`tests/test_asmr_one.py`）+ packaging 45。
- **打包自检**：`python scripts/make_share_packages.py --check` → **七个包**：`pixiv-api` 59、`iwara-api` 45、**`asmr-api` 43**、`novel-extension` 17、`iwara_web-extension` 8、`pixiv-novel` 72、`iwara-iwara_web` 49 → `分享包处理完成`。
- **文档**：`README.md` / `INSTALL.md` / `DIRECT.md` / `.env.example` 四份同步为三站点（含新增 `ASMR_BRIDGE_ECH` / `ASMR_BRIDGE_ECH_CONFIG` / `ASMR_BRIDGE_ECH_IPS`）。
- **仍未验证**：asmr.one 的真实取流只做过探活（`/api/tracks/{id}`、`/api/work/{id}`、`/api/works`、`/api/search/works`、`/api/auth/me` 的响应形状已核对并写进 `asmr_one/` 注释），未做长时间播放验证。

## 14. 后续追加：第四个站点 `pornhub/`（抓取器，无公开 API）

需求原文（m03971）："尝试做pornhub接口项目"；m03971 经选择「先搭可运行骨架 + 仅匿名」批准落地。

可行性（web 检索结论）：Pornhub **没有公开 JSON API**（旧 `api.php` 已废）；社区库 `pornhub.js` / `pornhub-api` 都是**抓网页**（HTML + 页面内 `window.initialState`）；站点在 Cloudflare 后只认浏览器 TLS 指纹，抓取器靠 `curl_cffi` 的 `impersonate="chrome"` 清掉，与 iwara / asmr_one 同机制；官方 Hubtraffic / Webmaster API 是联盟流量 API，不能浏览 / 搜索 / 播放——所以本站点是一个**纯匿名抓取器**，无 `login_check`，站点改版会让 `initialState` 字段名变掉、接口回 502（已知脆弱性，改 `pornhub/client.py` 的解析即可）。

### 14.1 新增的站点目录

`pornhub/` 共 10 个文件，结构完全镜像 `asmr_one/`（站点间禁止互相 import，所以 `ech.py` / `client.py` 全都是自足副本）：`__init__.py`、`ech.py`、`config.py`、`client.py`（抓取器，非 API 客户端）、`routes/__init__.py`、`routes/videos.py`、`app.py`、`envcheck.py`、`run.py`（三段式）、`requirements.txt`（`curl_cffi>=0.7` + `cffi>=1.16`）。

- `app.py`：`SITE = Site(name="pornhub", title="Pornhub Bridge", nav_order=4, settings_class=PornhubSettings, checks=extra_checks, ...)`，**没有 `login_check`**（纯匿名抓取）。
- `routes/videos.py` 五条 `/api/pornhub/*`：`/health`、`/videos`、`/search`、`/video/{viewkey}`、`/stream`。
- `config.py`：`PORNHUB_BRIDGE_ECH` 用 `_flag_or_default("PORNHUB_BRIDGE_ECH", True)` —— **不写这个变量就是开**（与 asmr 同策略），要关必须显式写 `=0`；另有 `PORNHUB_BRIDGE_ECH_CONFIG` / `PORNHUB_BRIDGE_ECH_IPS`。
- `client.py`（`PornhubClient`）：`SITE_HOST="www.pornhub.com"`、`MEDIA_HOSTS` 含 `mph.pornhub.com` / `mpthc.phncdn.com` / `phncdn.com` / `pornhub.com`；用 `_STATE_RE` 抠 `window.initialState`，`_video_from_state` / `_videos_from_search_state` 归一化；`request()` 换 IP 重试；`health` / `list_videos` / `search` / `video` / `open_stream` / `aclose`；抛 `PornhubError` / `PornhubChallengeError`（Cloudflare 拦截） / `PornhubParseError`（改版）；`is_ech_host` / `is_media_host`。
- `envcheck.py`：三项 `pornhub 依赖` / `pornhub 网络` / `pornhub ECH`；`PORNHUB_BRIDGE_ECH=0` 关掉后没有 ECH 那一项。
- 与 asmr 同骨架：种子 IP 池 + `curl_cffi impersonate="chrome"` + DoH（自带 `ech.py` 的 `resolve_with_doh`），全池轮询换 IP 重试。

### 14.2 抽象承诺在第四站点上的验证

`scripts/sites.py` 是唯一事实来源，加一条 `SiteInfo(name="pornhub", ...)` + `SITE_ROUTE_PATHS["pornhub"]` 即可；`make_share_packages.py` 加 `PH_SETUP` / `PH_NOTES` / `PH` 与 `pornhub-api` 包（`validate_specs()` 的 `len(SITES)` / `len(SITES)+4` 断言自动跟上，无需改数）；`check_boundary.py` 已经遍历 `SITE_ROUTE_PATHS` 与 `site.extra_modules`，不用再动。壳文件（Dockerfile / start.bat / start.sh）里第三站点时已经遍历 `SITES`，但 BRIDGE_SITE 示例与文案要补 pornhub；文档（README / INSTALL / DIRECT / .env.example）同步为四站点 + `PORNHUB_BRIDGE_ECH*` 三个变量。第 13.2 节「四处写死假设」在第四站点上已被验证为**已全部动态化**——唯一仍需手改的是壳文件与文档的措辞/示例（与当初的教训一致：注册表记录不会让壳和文档自动跟上）。

### 14.3 实测（本次落地，Windows / Python 3.13）

- **边界检查**：`python scripts/check_boundary.py` → pixiv 60（自带 54）、iwara 16（自带 10）、asmr_one 13（自带 7）、**pornhub 11（自带 5）**、novel ↗pixiv +10、iwara_web ↗iwara +1 → `边界通过`。
- **全量测试**：`python -m unittest discover -s tests` → **Ran 843 tests … OK (skipped=1)**，约 16 s。构成为 bridge 334 + iwara 199 + asmr_one 134 + **pornhub 131**（`tests/test_pornhub.py`）+ packaging 45。注意 `tests/test_bridge.py` 的 `NavigationTestCase` 里有两处写死了站点名单，加站点时必须一起改（本次已改为四站点 + `description` 前缀断言）。
- **打包自检**：`python scripts/make_share_packages.py --check` → **八个包**：`pixiv-api` 59、`iwara-api` 45、`asmr-api` 43、**`pornhub-api` 43**、`novel-extension` 17、`iwara_web-extension` 8、`pixiv-novel` 72、`iwara-iwara_web` 49 → `分享包处理完成`。`pornhub-api.zip` 不含其它站点目录与 `openssl-libs/`。
- **文档**：`README.md` / `INSTALL.md` / `DIRECT.md` / `.env.example` 四份同步为四站点（含新增 `PORNHUB_BRIDGE_ECH` / `PORNHUB_BRIDGE_ECH_CONFIG` / `PORNHUB_BRIDGE_ECH_IPS`）。
- **仍未验证**：pornhub 的真实抓取只做过 `client.py` 的解析路径核对与自检（`/api/pornhub/health` 等 5 个接口的响应形状基于 `window.initialState` 结构推断，未连真站跑通）——站点改版会让 `initialState` 字段名变掉、接口回 502，这是已知脆弱性。

## 15. 后续追加：pornhub 线上探活揪出的传输层缺陷（修复提交 `28a3c76`）

第 14.3 节把 pornhub 记为「只有离线单测」，本次补做**线上活体验证**（`pornhub/envcheck.py` 的 `extra_checks()` 直接对着真站跑），当场揪出四个站点里唯一一个「测试全绿但站点实际完全不可用」的缺陷。

### 15.1 缺陷与根因

线上探活输出：

```
pornhub 依赖 | True  | curl_cffi 0.15.0 已就绪
pornhub 网络 | False | www.pornhub.com 不可达：www.pornhub.com 全部 IP 失败：'AsyncSession' object has no attribute 'curl'
pornhub ECH  | False | pornhub.com 握手失败：pornhub.com 全部 IP 失败：'AsyncSession' object has no attribute 'curl'
```

根因：`pornhub/client.py` 的 `request()` 里写的是 `session.curl.setopt(CurlOpt.RESOLVE, [f"{host}:443:{ip}"])`，但 curl_cffi 0.15.0 的 **`AsyncSession` 只有 `.acurl`（`AsyncCurl`），没有 `.curl`** —— `.curl` 仅存在于同步 `Session`（实测 `hasattr(AsyncSession, "curl") is False`、`hasattr(AsyncSession, "acurl") is True`）。每次尝试都在这一行抛 `AttributeError`，被 `except Exception` 吞掉后换 IP 重试，`ip_attempts` 轮全败 → 抛 `PornhubChallengeError(... 'AsyncSession' object has no attribute 'curl')`。**所有出站请求 100% 必败，站点从未真正可用过。**

修复（提交 `28a3c76`，`pornhub/client.py` +142/−47）：照 `iwara/client.py` 与 `asmr_one/client.py` 早就用对的模式重写——钉 IP 不再逐请求 `setopt`，而是**建会话时把 `CurlOpt.RESOLVE`（单个 IP）/ `CurlOpt.CONNECTTIMEOUT` / `CurlOpt.ECH` 一起放进会话级 `curl_options`**。这一步可行的前提已核实到 curl_cffi 源码：`AsyncSession.__init__` 保存 `self.curl_options`，`_request_once()` 每次请求都把它传给 `set_curl_options()`，所以会话级只需设一次。配套加了 `_tcp_reachable` TCP 预探测、`_dead_ips` + `mark_ip_dead`、`_session(host, ip=None)`、`_session_ips` 跟踪，失败即拉黑该 IP 并重建会话换 IP。

另外确认 `SEED_IPS` 那 14 个 CF anycast 是从 `asmr_one/` 抄来的、**并不承载 Pornhub 边缘**（逐个探测是 6 秒超时或 `SSLError: Recv failure`，带 ECH 只是 `SSLV3_ALERT_HANDSHAKE_FAILURE`），注释已改写成实测结论，它只作为 DoH 无答案时的兜底。

### 15.2 教训：离线桩照抄源码的错误 API，会让测试全绿却掩盖真缺陷

这才是这次缺陷能藏住的原因：`tests/test_pornhub.py` 的传输层桩自己造了 `self.curl = _FakeCurl()`，而 `_FakeCurl.setopt()` 只记录 `CurlOpt.RESOLVE` —— **桩模仿的正是源码里那个错误的 API 写法**。于是 131 项测试全绿，却与真实 curl_cffi 完全脱节。

修复时同步做了三件事，避免同类问题复发：
1. **删掉 `_FakeCurl` 整个类**，桩不再假装 `AsyncSession` 有 `.curl`；
2. 会话类断言改看 `AsyncSession` 构造入参的 `curl_options`（`CurlOpt.RESOLVE` 是单条 `"{host}:443:{ip}"`、`CONNECTTIMEOUT`、`CurlOpt.ECH`）；
3. 新增 `test_session_rebuilds_when_the_ip_changes`：换 IP 必须重建会话，而不是复用旧会话偷偷改配置。

**通用规则**：桩允不允许某个属性/方法，必须与实际依赖版本的行为对齐；当桩是自己「补」出来的 API（真实对象上并不存在）时，测试就在自证一个谎言。凡涉及第三方库调用约定的代码，离线测试通过不能替代一次真实调用（本次的机制实证是在本机可达的 `dns.alidns.com` 上做的：`AsyncSession(curl_options={CurlOpt.RESOLVE: ["dns.alidns.com:443:223.5.5.5"], CurlOpt.CONNECTTIMEOUT: 8})` 请求返回 200，同一会话 `hasattr(session, "curl") is False`）。

修复后复验：`python -m unittest tests.test_pornhub` → **Ran 132 tests OK**（第 14.3 节的 131 项 + 新增的 `test_session_rebuilds_when_the_ip_changes`）；全量 `python -m unittest discover -s tests -t .` → **Ran 844 tests OK (skipped=1)**；`python scripts/check_boundary.py` → 边界通过（pornhub 11 / 自带 5，与第 14.3 节一致）。

### 15.3 已知未验证边界：本机对外 TLS 被整体阻断

修复后再跑线上探活，`AttributeError` 已消失，暴露出真实错误是 TLS 握手层：

```
www.pornhub.com 全部 IP 失败：Failed to perform, curl: (35) TLS connect error:
error:10000410:SSL routines:OPENSSL_internal:SSLV3_ALERT_HANDSHAKE_FAILURE
```

进一步用裸 `ssl` 逐个换 SNI 对照（`www.pornhub.com` / `pornhub.com` / `asmr.one` / `example.com`），**全部 `ConnectionResetError: [WinError 10054] 远程主机强迫关闭了一个现有的连接`**；连对照请求 `httpx.get("https://api.asmr.one/api/works?page=1")` 也是 `ConnectError [WinError 10054]`。

**结论：与本项目代码无关，是本机网络环境（需代理）导致。** 因此 pornhub 的端到端验证在本机**只能到「不再抛 AttributeError、DoH 解析与 TCP 预探测链路正常选中 IP」这一层**；真实抓取（`/api/pornhub/health` 之外四个接口拿到真数据）在有代理的环境里仍需补做。此边界不改变第 14.3 节的「已知脆弱性」判断。

> 注（第 16 节修正）：本节「本机对外 TLS 被**整体**阻断」的表述**不准确**。实测阻断是**按域名/SNI 分级**的，`example.com`、`www.cloudflare.com`、`api.github.com`、`dns.alidns.com` 等在本机均可正常握手。且阻断的直接原因**不是「缺代理」**，而是对 pornhub 域的**定点 SNI 封锁**——见第 16 节。

## 16. 后续追加：pornhub 线上连通性彻底排查（「通过 cloudflare 连接」路径穷举，代码零改动）

第 15.3 节把 pornhub 不可达笼统归因为「本机网络环境（需代理）」，但没说清阻断在哪一层、也没验证「是不是换条路就能通」。本次针对用户提出的口径「**通过 cloudflare 连接**」把可行路径逐条穷举干净，结论如下（**本节目的是固化结论、封存已排除的死路，避免后来者重复踩坑；未做任何代码改动**）。

所有探针都写在仓库外的临时目录 `_scratch/`（`.gitignore:27` 已忽略），全程 `git status --porcelain` 为空、HEAD 停在第 15 节修复后的 `28a3c76` 之后的 `a574d4a`。

### 16.1 决定性前提：pornhub 根本不在 Cloudflare 上

这是整轮排查里最重要的一条，它直接解释了此前所有「打 CF IP」的尝试为何全灭——**pornhub 没有 CF 边缘可打**。

| 证据 | 实测结果 |
| --- | --- |
| 可信 DoH 解析链（ECH+钉 CF IP 打 `cloudflare-dns.com`，该通道对已知域名返回正确地址故可信） | `www.pornhub.com` → CNAME `pornhub.com.` → A **`66.254.114.41`**（24 轮重试命中 18 次，**答案零方差**） |
| 是否落在 CF 官方段内（段表取自 `https://www.cloudflare.com/ips-v4`，HTTP 200，15 个 ipv4 段，逐段用 `ipaddress` 判定） | **不在任何一段内** |
| RDAP 归属 | `66.254.114.41` → `REFLECTED-1` / `NET-66-254-96-0-1`，**AS29789**（Reflected Networks） |
| 外部三方互证 | Shodan `https://www.shodan.io/host/66.254.114.41` 主机名 = `pornhub.com`、`reflectededge.reflected.net`；BeaverCheck `https://beavercheck.com/sites/pornhub.com/infrastructure` = "hosted on REFLECTED - Reflected Networks, Inc., US, **AS29789**"；URLert `https://www.urlert.com/domains/rncdn7.com` = REFLECTED AS29789 |
| `pornhub.com` 的 NS（经可信 DoH 查 NS 记录） | `ns-92.awsdns-11.com` / `ns-531.awsdns-02.net` / `ns-1499.awsdns-59.org` / `ns-1567.awsdns-03.co.uk` / `sdns3.ultradns.{biz,com,net,org}` ⇒ **AWS Route53 + UltraDNS，不是 Cloudflare NS** |
| 内容 CDN | `ei.phncdn.com` → CNAME `ei.phncdn.com.sds.rncdn7.com.` → `64.210.156.{18,19,22}`；`ev-h.phncdn.com` → `*.lds.rncdn7.com.` → `64.210.156.{3,7,2}`；RDAP = `VIKINGHOST-BV-CDN` / Viking Host BV，**同样不在 CF** |

⇒ **pornhub 主站与内容 CDN 都托管在自有 CDN「Reflected Edge」（AS29789）上**，「用 Cloudflare 的 IP / ECH 绕过从而连上 pornhub」在**物理前提上就不成立**。

> 顺带纠正两条早期误判：(a) `SEED_IPS` 里那 14 个 CF anycast **确实不承载 Pornhub 边缘**（第 15.1 节已记为兜底），本次给出了原因；(b) 早期一度以为 `104.18.10.118` 上 `curl.exe` 得到过 200，**该结论作废**——复测全部 `code=000`，是受 IP 限速/抖动污染的假象。对照组可复现的结论是：**同一个已知可达 CF IP（如 `104.16.124.96`），带 SNI `www.cloudflare.com` → 200，带 SNI `www.pornhub.com` → 000**，即阻断按 SNI 生效。

### 16.2 路径一：CF IP / ECH 绕过 —— 已证伪

- **ECH 机制在本机是真实可用的**（这点必须记住，别误以为 ECH 环境不具备）：`_scratch/_probe_ech_matrix2.py`（钉 IP `104.18.27.97`，ECL 取 `ech.ech_config()`）对照结果——

  | inner SNI | 无 ECH | 有 ECH |
  | --- | --- | --- |
  | `www.cloudflare.com` | HTTP 200 (1334670B) | HTTP 200 (1334670B) |
  | `example.com` | HTTP 200 (577B) | HTTP 200 (577B) |
  | `www.iwara.tv` | HTTP 200 (2309B) | HTTP 200 (2309B) |
  | `api.asmr.one` | RST（中间盒） | **HTTP 404（ECH 成功绕开）** |
  | `www.pornhub.com` | RST（中间盒） | **ALERT（对端拒绝）** |

- **但 ECH 救不了 pornhub**，因为 pornhub 域**不发布 type-65 ECH 配置**：`www.pornhub.com`（仅 CNAME `pornhub.com.`）、`pornhub.com`、`ei.phncdn.com`、`ev-h.phncdn.com`、`mph.pornhub.com`、`mpthc.phncdn.com` 的 HTTPS(65) 记录**全部没有 `ech=` 字段**；对 CDN 强行叠 ECH 直接报 `Failed to perform, curl: (101) ECH required: error:1000013f:`。
- 只能「借」公共 ECL：`cloudflare-ech.com` 与 `crypto.cloudflare.com` 的 type65 里**有** `ech="AEX+DQBB8gAgACCIZ8uQXidaAaC48rHXa9U…`（与 `pornhub/ech.py` 的 `DEFAULT_ECH_CONFIG` 同族）。但拿这个借来的键去打 pornhub zone，Cloudflare 在握手层直接**拒绝**（RST → `SSLV3_ALERT_HANDSHAKE_FAILURE`）。
- 一个反直觉的旁证：`www.cloudflare.com` 的 type65 **没有** `ech=` 字段，叠 ECH 却稳定 200。⇒ **「是否发布 type65」与「能否 ECH」是两回事**，真正决定因素是 **CF 侧该 zone 是否启用了 ECH**；pornhub 不在 CF，无从启用。
- 环境注记：本机 `curl.exe --ech` **不可用**（`curl: option --ech: the installed libcurl version does not support ECH`），ECH 全部实验是通过 **curl_cffi 的 `CurlOpt.ECH`** 完成的。

### 16.3 路径二：起用本机已装的 Clash Verge 订阅 —— 节点可达但凭证已被服务端注销

现场：`C:\Program Files\Clash Verge\{clash-verge.exe, verge-mihomo.exe, verge-mihomo-alpha.exe}`（核心 `Mihomo Meta v1.19.21 windows amd64`）；配置目录 `C:\Users\Administrator\AppData\Roaming\io.github.clash-verge-rev.clash-verge-rev\`（`mixed-port 7897`、`external-controller 127.0.0.1:9097`、`tun.enable: false`、`mode: rule`；`verge.yaml` 里 `enable_system_proxy: false`、`enable_tun_mode: false`；当前 profile `LUGgwWog4DcU`）。排查时 **Clash Verge 完全没在运行**，**无任何代理端口监听**；订阅里**没有 WARP/wireguard 类型节点**。

- 订阅节点**不是 CF 中继**：`*.ccgfw.top` → `172.234.26.134 / 172.236.114.191 / 172.238.172.46`（Linode 网段），`*.okgg.top` → `45.62.109.93`，**均不在 CF 段内**。
- 链路层其实没被封：裸 Python `ssl` 对 `s246/s678/s644/ipv6s116.*` 等可完成 **TLSv1.3 握手**。
- 但**协议层全废**：自建最小 mihomo 配置（`mixed-port 17897` / `ctrl 19097`）轮测 6 个 vless 节点，**全部** `URLError: [SSL: UNEXPECTED_EOF_WHILE_READING]`，连 `https://www.cloudflare.com/cdn-cgi/trace` 都取不到。
- 根因（mihomo 日志原文）：`level=error msg="XTLS Vision server responded unknown UUID: 12040000-0000-0000-0300-000080000400"`。⇒ **该免费订阅的统一 UUID `8f8ccdc5-fe99-38d7-a8a6-422f8c88ef0a` 已被服务端注销**（约 2200 条节点全废）。**此路不通，勿再试。**

### 16.4 路径三：Cloudflare WARP 隧道 —— 注册成功，但数据面被定点阻断

控制面在本机可达：`api.cloudflareclient.com` A = `104.16.24.84 / 104.16.192.82`，`GET /` → 404（协议级响应，非网络阻断）。

- 早先 `POST /v0a2158/reg`（body `{}`）返回 `400 Invalid registration request`。**补全 payload 后注册成功**（脚本 `_scratch/_warp_reg.js`；Node 25.2.1 用 `crypto.generateKeyPairSync('x25519')` 生成密钥对；body 含 `key`(b64 raw pub) / `install_id:''` / `fcm_token:''` / `tos:<ISO time>` / `model:'PC'` / `serial_number:''` / `locale:'en_US'`；headers `CF-Client-Version: a-6.30-2158` / `User-Agent: okhttp/3.12.1`）。账号信息（临时凭据）留在 `_scratch/warp_account.json`：device `a2237f75-a85a-47d4-8576-bccaf7155d56`、account `b3f53f33-3130-487d-a65d-0e6394ae12de`、`account_type: free`、`warp_plus: true`、`ttl 2027-01-07`；分配 `v4 172.16.0.2`；peer 公钥 `bmXOC+F1FxEMF9dyiK2H5/1SUtzH0JuVo51h2wPfgyo=`；endpoint `engage.cloudflareclient.com:2408`（`162.159.192.8`），候选端口 `[2408, 500, 1701, 4500]`。
- **隧道建不起来**：用 mihomo `type: wireguard` outbound 轮测四个端口，**全部失败**——`https://www.cloudflare.com/cdn-cgi/trace` 虽返回 200，但 `ip=111.55.147.168`、**`warp=off`**（= 流量根本没进隧道，回退成了直连）。mihomo 日志：`peer(bmXO…fgyo) - Sending handshake initiation` → `Handshake did not complete after 5 seconds, retrying (try 2)`。
- 分层定位（`_scratch/_probe_udp.py`）：**UDP 出站本身完全正常**——UDP:53 查询对 `10.209.132.24`、`223.5.5.5`、`1.1.1.1`、`8.8.8.8`、`114.114.114.114` **全部有回包（68B）**。但**到 WARP 数据面的 UDP 全部零回包**：`162.159.192.8` / `162.159.192.1` 的 `2408/500/1701/4500/894/8095/443` 精细扫描 **命中 0**；`162.159.193.1` / `162.159.195.1` / `188.114.98.224` / `188.114.99.224` 的 2408 亦全部超时；连 `162.159.192.8:443/UDP` 与 `104.16.123.96:443/UDP` 也超时。

⇒ **UDP 未被全局封锁，是到 CF WARP 端点（`162.159.192.0/24` 等）的 UDP 被定点丢弃**。**WARP 此路不通。**

### 16.5 路径四（唯一物理上仍成立）：用 CF Worker / Pages 做反代 —— 卡在本机无 CF 账号

CF 的**控制面与 Pages 托管面在本机可达**（`_scratch/_probe_cf_relay_feasible.py`）：

| 端点 | HTTP | 裸 TLS 握手 |
| --- | --- | --- |
| `api.cloudflare.com` | 301 | `104.19.192.174/175` → OK TLSv1.3 |
| `dash.cloudflare.com` | 200 (11756B) | `104.17.110.184/111.184` → OK |
| `pages.dev` | 301 (167B) | `104.18.20.135` → OK（`104.18.21.135` → Timeout） |
| `cdnjs.cloudflare.com` | 200 (8891B) | `104.17.24.14/25.14` → OK |
| `cloudflare-ech.com` | 301 | `104.18.10.118/11.118` → OK |
| **`workers.dev`** | **`(28) Connection timed out`** | `128.242.245.180` → **Timeout** |

思路：CF 边缘在墙外、能到 pornhub，本机又能到 CF 控制面 —— 部署一个 Worker/Pages 反代取 pornhub 再回吐，是**本机唯一可能成立的「通过 cloudflare 连接」**。注意 `workers.dev` 被单独阻断，故 Worker **必须挂 Custom Domain 或 `*.pages.dev`**，不能只靠 `*.workers.dev`。

**当前阻碍：本机没有任何 CF 账号凭证。** 环境变量无 `CLOUDFLARE*` / `CF_*` / `WRANGLER*`；`~/.wrangler`、`~/.cloudflared`、`~/.cf`、`AppData/Roaming|Local/wrangler` **均不存在**；`wrangler` / `cloudflared` / `wgcf` **未安装**（`npx` / `npm` / `node` v25.2.1 / `git` 可用，足以 `npx wrangler` 但需登录）。

### 16.6 结论与未决项

1. **pornhub 不在 Cloudflare 上**（AS29789 Reflected Networks，NS = AWS Route53 + UltraDNS）。「通过 Cloudflare 连接 pornhub 主站」若指**绕过 CF 反代**，前提不成立。
2. 已穷举的四条路**全部证伪或不可行**：CF IP/ECH 绕过（前提不成立）、Clash 订阅（UUID 被服务端注销）、WARP（数据面 UDP 被定点丢弃）、本机代理端口（无进程监听、无 `HTTP(S)_PROXY`）。
3. 唯一剩路是 **CF Worker/Pages 反代**，但**需用户提供 CF 账号/Token** 才能部署；且必须避开被阻断的 `*.workers.dev`。
4. 本次**未做任何代码改动**。第 15.3 节「真实抓取在有代理的环境里仍需补做」的边界**仍然成立且未变**；本节的增量是：把「需要代理」这个笼统归因，落实为**可按域名/SNI 复现的定点封锁 + pornhub 不在 CF** 这两条硬事实。
5. 若将来要真正打通，有两条后续路线（均需用户投入/授权，不在本次范围）：(a) 由用户提供可用出口（商业代理/VPN）或 CF 账号走 Worker 反代；(b) 为 pornhub 补 `PORNHUB_BRIDGE_PROXY` 支持以对齐 pixiv 的 `PIXIV_PROXY`（`pixiv_bridge/config.py:64` 定义 `proxy: str = ""`、`pixiv_bridge/config.py:208-210` 读 `PIXIV_PROXY`/`PIXIV_BRIDGE_PROXY`）。

---

## 17. 退役 pornhub 站点（2026-10-08）

用户决定（原话）：「不能用就清理对应项目」。既然第 16 节已把「本机无法连通 pornhub」证成硬结论，且没有任何无需用户投入就能打通的路径，pornhub 接口项目作为「永远连不上、永远回 502」的死代码保留在仓库里只会：
- 让每次改框架都要多维护一个站点的自检、打包、路由差集；
- 让文档里「四个同级接口项目」这类表述持续失真；
- 给后来者留下「这个项目是能用的」的错误预期。

故**整体移除**，站数从四个回到三个（pixiv / iwara / asmr_one）。

### 17.1 移除清单（逐项）

**删除的文件**（`git rm`）：
- `pornhub/` 全目录 10 个文件：`__init__.py`、`app.py`、`client.py`、`config.py`、`ech.py`、`envcheck.py`、`requirements.txt`、`run.py`、`routes/__init__.py`、`routes/videos.py`
- `tests/test_pornhub.py`
- `dist/pornhub-api.zip`（构建产物；`dist/` 本就在 `.gitignore:24` 内，未入版本库）

**构建侧登记表** `scripts/sites.py`：
- 删 `SiteInfo(name="pornhub", ...)` 整块（原 L88-99）
- 删 `SITE_ASSETS` 里的 `"pornhub": ()`
- 删 `PH_ROUTE_PATHS` frozenset（原 L200-208）及 `SITE_ROUTE_PATHS` 里的 `"pornhub": PH_ROUTE_PATHS`
- 复验：`SITES = ['pixiv','iwara','asmr_one']`、`SITE_ROUTE_PATHS = ['asmr_one','iwara']`、`SITE_ASSETS = ['asmr_one','iwara','pixiv']`、`ENTRY_POINTS = ('pixiv/run.py','iwara/run.py','asmr_one/run.py')`

**打包脚本** `scripts/make_share_packages.py`：
- 删 `PH_SETUP` / `PH_NOTES` 定义、`PORNHUB = SITES_BY_NAME["pornhub"]`、`PackageSpec(key="pornhub-api", ...)` 整块（原 L257-283）、`BASE_NOTES` 里的 `` `pornhub/` ``（原 L121）
- `--check` 现输出 **7 个包**：pixiv-api 59 / iwara-api 45 / asmr-api 43 / novel-extension 17 / iwara_web-extension 8 / pixiv-novel 72 / iwara-iwara_web 49

**测试** `tests/test_bridge.py`：
- `test_the_real_project_lists_every_api_project_in_nav_order` 现断言 `["pixiv","iwara","asmr_one"]` / `[1,2,3]` / `["site"]*3`，删掉 `sites[3].description.startswith("Pornhub 接口站点")`
- `test_the_real_project_lists_extensions_that_are_not_entries` 的 `self.nav.site_names()` 现为 `["pixiv","iwara","asmr_one"]`

**配置样例** `.env.example`：删掉 `PORNHUB_BRIDGE_ECH` / `PORNHUB_BRIDGE_ECH_CONFIG` / `PORNHUB_BRIDGE_ECH_IPS` 三行及其注释（原 L76-85）。

**文档**（四处口径全部从「四个」改回「三个」）：
- `README.md`：L7 四层总述、特性表「可选的 pornhub 站点」整条、L143 入口列表、L153 启动方式表行、L155 段落里的 pornhub 自检与「要 pornhub 就拷…」、目录树 pornhub 块（原 L333-342）、测试计数（844 → **712** 项：框架与站点 334 + iwara 199 + asmr_one 134 + 打包 45）、「入口只有四个」→ 三个、分享包「八个」→ **七个** 及 `pornhub-api` 表行
- `INSTALL.md`：入口数、`curl_cffi` 需用站点（去掉 pornhub）、入口段落、`--check` 自检说明、启动表两行、抓取器说明段、接口地址列表、分享包段落、排错表 `/api/pornhub/...` 行
- `DIRECT.md`：适用对象站点数、站点隔离清单、重依赖说明、§7 标题「同一套方法、四个不同站点」→ 三个 并删掉整列 pornhub 列、§7 结论段、§8 环境变量表里的三行 `PORNHUB_BRIDGE_ECH*`、自检说明段
- `Dockerfile`：`RUN for req in ...` 循环去掉 `pornhub/requirements.txt`、`BRIDGE_SITE=pornhub` 示例行、注释里的站点枚举
- `start.bat` / `start.sh`：标题、用法示例里的 `start.bat pornhub` / `sh start.sh pornhub`、同级站点列表、扩展说明括号、提示语里的站点枚举

**无需改动**（已确认是动态的，自动随站点数收敛）：
- `scripts/check_boundary.py` 从 `SITE_ROUTE_PATHS` 动态遍历（L243-252），无 pornhub 硬编码
- `tests/test_packaging.py:64` 期望 `len(packaging.PACKAGES) == len(packaging.SITES) + 4`（动态）
- 全文无 pornhub 字面量的其它测试

### 17.2 验证结果

- 全量测试：`.venv\Scripts\python.exe -m unittest discover -s tests` → **Ran 712 tests … OK (skipped=1)**，失败数为 0（`test_bridge` 334 + `test_iwara` 199 + `test_asmr_one` 134 + `test_packaging` 45）。
- 边界自检：`python scripts/check_boundary.py` → 「边界通过」，路由差集为框架外壳 6 条、pixiv 60（其中站点自己的 54 条）、iwara 16（其中站点自己的 10 条）、asmr_one 13（其中站点自己的 7 条）、novel（扩展，宿于 pixiv）比宿主多 10 条、iwara_web（扩展，宿于 iwara）比宿主多 1 条。
- 打包自检：`python scripts/make_share_packages.py --check` → 7 个包全部「内容就绪」。
- `dist/` 重建：`dist/` 下现有 7 个 zip（`pixiv-api.zip` / `pixiv-novel.zip` / `iwara-api.zip` / `iwara-iwara_web.zip` / `asmr-api.zip` / `novel-extension.zip` / `iwara_web-extension.zip`），`pornhub-api.zip` 已不存在。**不记具体字节数**——三份手册（`README.md` / `INSTALL.md` / `DIRECT.md`，见 `scripts/make_share_packages.py:62` 的 `DOC_FILES`）本身会被打进每个包，改一次文档字节数就全变，写死只会立刻过期；要核对的是「7 个包、无 pornhub 残留」（`System.IO.Compression.ZipFile` 逐包扫描条目数与包内手册文本，`pornhub|PORNHUB|Pornhub` 零命中）。
- 仓库级 grep：除本计划文档（历史记录，刻意保留 §16/§17 的 pornhub 字样）外，全仓无 `pornhub` / `PORNHUB` / `Pornhub` 残留（排除 `.git` / `_scratch` / `dist` / `.venv` / `__pycache__`）。

### 17.3 保留的教训（不随代码删除）

- **站点能否落地，先判「目标站在谁的网络上」。** 第 16 节最大的产出不是「连不上」，而是「一开始就不该按 Cloudflare 反代去设计」。`pornhub/ech.py` 与整个种子 IP 池 + ECH 骨架都是照 asmr.one 抄的，前提（目标在 CF 后）从未被验证过，结果所有 CF-IP 探针全灭。
- **「缺代理」是结论，不是原因。** 本机 UDP:53 完全正常、`dash.cloudflare.com` / `api.cloudflare.com` / `*.pages.dev` 均可达，被阻断的是**按域名/SNI 定点**的几个主机（`*.workers.dev`、WARP 数据面 `162.159.192.x`）。诊断时先分层定位再归因。
- **能用 `curl` 拿到的结论，不要写成「环境不行」。** 可复现的硬证据（DoH CNAME 链、CF 官方段表不命中、RDAP AS29789、NS 记录、UDP 端口扫描零回包）能直接判定可行性；反之，「本机对外 TLS 整体被阻断」这种笼统表述会掩盖真实原因（第 15.3 节的原文已因此被订正）。

---

## 18. 后续追加：第四个站点 `ehviewer/`（E-Hentai，2026-10-09）

用户原话（m01084）：「尝试做ehviewer的接口项目」。这是第 13 节之后**第二次用抽象本身做验收**：站点数从三个回到四个，但抽象层（`Site` 契约 / `scripts/sites.py` 登记表 / `check_boundary.py`）**一行没改**——新增只动了「写站点」与「登记一条」两处。

### 18.1 阻塞形态与解题手段（与前三站都不同）

- 目标 `e-hentai.org` **在 Cloudflare 后面**（这次前提是对的），但**它自己解析出来的那几个边缘 IP 在本机被 IP 级阻断**（`172.66.132.196` / `172.66.140.62`：连 TCP 都建不起来，不是 TLS 层被拒）。
- 解法 = **内置 16 个别的 Cloudflare anycast IP 种子池** + 会话级 `CurlOpt.RESOLVE`（把域名钉到选中的边缘）+ **强制 HTTP/3**（`CurlOpt.HTTP_VERSION = CurlHttpVersion.V3`，`EH_BRIDGE_HTTP3` 默认开）。HTTPS/1.1 与 HTTP/2 会因 SNI 与边缘不符被拒，QUIC 走 UDP:443，不受那个判据限制。
- 种子池**故意排除** e-hentai 自己解析出的那两个被封地址；`probe_edge()` 用**裸 TCP 建连**探活（不发 TLS，因此不会被 SNI 判据误杀），`next_edge()` 走**轮转游标**而不是死磕同一个 IP。
- **不走 ECH**：本站的墙按 IP 与 SNI 判，不是按指纹判，所以也不 `impersonate`——只加普通浏览器头。
- 图片字节在 `*.hath.network` 的 H@H 节点上，**端口随机且会失效**，因此必须由服务端代转（`GET /api/eh/proxy?url=...`），且每次重试都**重新解析 `/s/` 页**拿新节点（`EH_BRIDGE_IMAGE_ATTEMPTS`，默认 4），而不是重试同一个死 URL。
- 元数据走官方 `api.php` 的 `gdata`（`POST`，body 必须是 `{"method":"gdata","gidlist":[[gid,token],…],"namespace":1}` 的 bytes，`content-type: application/json`）。
- 匿名可用：没有 `login_check`，也不碰 exhentai（里站内容上游直接 401）。

### 18.2 落盘清单

**新建 `ehviewer/`（10 个文件，与 asmr_one 同构）**：`__init__.py`（惰性 `__getattr__` 导出，不做模块级 `from .app import`，否则会把 curl_cffi 拖进自举）`app.py`（`nav_order=4`，`EH_NAV` 七个 `NavItem`）`client.py`（借道 + 强制 H3 + 裸 TCP 探边缘 + 会话级 RESOLVE + 轮转换边缘 + 图片代转重解析）`config.py`（`EH_BRIDGE_HTTP3` / `_EDGE_IPS` / `_EDGE_ATTEMPTS` / `_IMAGE_ATTEMPTS`，**无 ech 三项**）`envcheck.py`（依赖 / 边缘 / 站点三项，均不中止启动）`parse.py`（列表 / 详情 / 大图页 / 条带解析）`requirements.txt`（`curl_cffi` + `cffi`）`run.py`（`bootstrap` 排在 `from ehviewer.app import SITE` 之前）`routes/__init__.py` `routes/galleries.py`（`/api/eh` 七个端点）。

**改动**：`scripts/sites.py` 加一条 `SiteInfo(name="ehviewer", …)` + `EH_ROUTE_PATHS` frozenset + `SITE_ASSETS`；`scripts/make_share_packages.py` 加 `EH_SETUP` / `EH_NOTES` 与 `PackageSpec(key="ehviewer-api", kind="api", …)`（插在 `asmr-api` 与 `novel-extension` 之间）；`tests/test_bridge.py` 两处站点名单硬编码（全仓仅有的两处）加 `"ehviewer"`；新建 `tests/test_ehviewer.py`（128 项，离线，缺 curl_cffi 时整片跳过）。

**文档**：`README.md`（总述 / 特性表 / 入口列表 / 启动表 / 环境检查三项表 / 目录树 / 环境变量四行 / 扩展章节 / 计数 712→**840** / 分享包 七→**八个** 及 `ehviewer-api` 表行 / 新增「E-Hentai（仅 `ehviewer/run.py` 提供）」整节）、`INSTALL.md`（入口数 / curl_cffi 三站点 / `--check` 清单 / 启动表 / 接口地址 / 分享包 / 排错表四行）、`DIRECT.md`（站点数 / 隔离清单 / 重依赖 / §7 标题改「四个不同站点」并新增 E-Hentai 整列 / 环境变量表四行 / 自检说明段）、`Dockerfile`（`RUN for req in …` 加 `ehviewer/requirements.txt`、`BRIDGE_SITE=ehviewer` 示例）、`start.bat` / `start.sh`、`.env.example`（补 `EH_BRIDGE_*` 四项）。

### 18.3 验证结果

- 全量测试：`.venv\Scripts\python.exe -m unittest discover -s tests` → **Ran 840 tests … OK (skipped=1)**（`test_bridge` 334 + `test_iwara` 199 + `test_asmr_one` 134 + `test_ehviewer` 128 + `test_packaging` 45）。
- 边界自检：`python scripts/check_boundary.py` → 「边界检查通过」，ehviewer 13 条路由（其中站点自己的 7 条），与 `EH_ROUTE_PATHS` 逐字一致。
- 打包自检：`python scripts/make_share_packages.py --check` → **8 个包**全部「内容就绪」（pixiv-api 59 / iwara-api 45 / asmr-api 43 / **ehviewer-api 43** / novel-extension 17 / iwara_web-extension 8 / pixiv-novel 72 / iwara-iwara_web 49）。
- 真实链路：借道 `104.17.0.1` + HTTP/3 实连，`list_galleries()` / 详情 / `/s/` 大图页 / `gdata` 逐字段核对通过；真实 markup 已抄成 `tests/test_ehviewer.py` 的 fixture。

### 18.4 本段揪出并修掉的真缺陷（写测试与探针时暴露）

1. `ehviewer/parse.py` 的 `_nav_href` 只认 `dnext` / `dprev`（列表页详情导航用的 id），而 `/s/` 大图页的翻页锚是**短 id** `next` / `prev`（`<a id="next" onclick="return load_image(4, 'b6062cc00f')" …>`）⇒ 大图页翻页永远为空。修法：加 `_NAV_PATTERNS` 把四组正则按 `which` 分派。
2. `ehviewer/parse.py` 的 `_COUNT_RE` 只认 `Found about N results`，而首页是 `Found 1,658,228 results`（无 `about`）⇒ 改为 `r"Found\s+(?:about\s+)?([^<]*?)\s*results"`。
3. `ehviewer/parse.py` 的 `_cursor_of(url)` 把键名写死成 `next`，而上一页 URL 是 `?prev=` ⇒ prev 光标永远空串。修法：加 `key` 参数（`ehviewer/parse.py:118`），调用点 `"prev": _cursor_of(prev_url, "prev")`（L239）。
4. `ehviewer/client.py` 的 `gdata()` 把 `int(gid)` 放在 `try` **外面**，畸形条目会以 `ValueError` 冒出去变成 500 ⇒ `int()` 挪进 `try` 并把 `ValueError` 加进异常元组，语义改为「畸形项跳过」。
5. 测试自身两处 bug：`site_items()` 的字段是 `order` 而非 `nav_order`；`envcheck` 测试用 `MagicMock` 造假客户端导致 `client.http3` 恒为真值、走不到「HTTP3 已关」分支（改 `side_effect` 显式赋 `http3`）。

### 18.5 保留的教训

- **「在 Cloudflare 后」不等于「任何一个 CF 边缘都能用」。** 第 16 节的教训是「先判目标在谁的网络上」，本节补上后半句：**在 CF 后面时，还要判「它自己解析到的那个边缘是否可用」**——e-hentai 自己解析出的两个 IP 被封，但同一 anycast 段的别的 IP 完全可用，所以种子池 + 换边缘才是解。
- **纯 TCP 探活比 TLS 探活更可靠。** SNI 不符时 TLS 会失败，但那不代表 IP 不可达；`probe_edge()` 只建 TCP，避免把「SNI 判据」误当成「IP 不通」（这正是 pornhub 时代探针全灭时踩过的坑的反面）。
- **换节点要重新解析，不要重试死 URL。** H@H 直链带一次性 `keystamp` 且端口随机，重试同一 URL 必然持续失败；每次重新解析 `/s/` 页才是有效重试。

## 19. 后续追加：`ehviewer/` 的三处接口缺口（2026-10-09）

第 18 节把站点做通了，但复查时发现三处「能力有、接口没暴露」或「暴露得不对」的地方。本节记录这次补齐。

### 19.1 三处缺口与判定依据

1. **`/api/eh/images` 回了一个假的 `next_cursor`**。详情页是**页码**分页（上游 0 基 `?p=`），跟首页的 `?next=` 游标根本不是一回事，但 `/images` 照抄了光标形状的字段名，值恒为 `None` —— 客户端照字段语义去翻页就会走进死胡同。
2. **`image_page()` 在客户端里有、路由没暴露**。想看「这张 `/s/` 页对应哪个图片直链」只能走 `/proxy` 把字节也拖下来。
3. **上游 `/popular` 完全没接**。

第 3 条的形态必须先探再定（探针 `_scratch/_probe_eh_popular.py`，真实链路 HTTP/3 + 借道边缘，`EXIT=0`）：

| 请求 | bytes | rows | found | nav next |
| --- | --- | --- | --- | --- |
| `GET /popular` | 165242 | 74 | `''` | `''` |
| `GET /popular?next=4241792` | 165242 | 74 | `''` | `''` |
| `GET /?next=4241792`（对照） | 59316 | 25 | `1,658,289` | `4241767` |

第二行与第一行**字节数完全相同** ⇒ `/popular` 忽略 `?next=`，**确实不可翻页**，且该页不印 `Found N results`。所以没有把它硬塞进光标分页抽象，而是照实暴露成「一页给完」。

### 19.2 改动清单（逐项）

- `ehviewer/client.py`：新增模块级 `pagination(page, showing, page_size=PAGE_SIZE)` —— 由详情页的 `showing.total` 算 `total_pages` / `next_page` / `has_next`；**`total` 缺失、非数字或 ≤ 0 时三者一律 `None`**（注释写明：数不出来时不要瞎猜，否则客户端翻页循环停不下来）。`gallery()` 尾部调它；新增 `popular()`。
- `ehviewer/routes/galleries.py`：新增 `GET /api/eh/popular` 与 `GET /api/eh/page?url=`（后者先过 `is_image_page()` 白名单，非 `/s/` 页一律 400）；`/images` 去掉误导性的光标语义、改为回 `total_pages` / `next_page` / `has_next`（保留 `next_cursor: None` 作历史字段）。
- `ehviewer/app.py`：`EH_NAV` 7 → 9 项。
- `ehviewer/parse.py`：`parse_detail()` 的 `cover` 改为复用 `parse_strip_cover()`，消掉重复的 `_STRIP_RE` 取值。
- `scripts/sites.py`：`EH_ROUTE_PATHS` 7 → 9 条。
- `tests/test_ehviewer.py`：新增 `EhPaginationTestCase`（8 例，覆盖中间屏 / 末屏 / 部分末屏 / 超界 / 缺 `total` 的 5 种输入 / `total=0` / 垃圾参数 / 单屏）与 8 个路由用例；**`_FakeEhClient.gallery()` 改为直接调真实 `pagination()`**，避免假客户端里另写一套使路由测试测的是假货。
- 文档：`README.md`（特性条目 / 入口表 / 端点表 7 → 9 行 / 新增实现说明第 5 条「首页游标 vs 详情页码」）、`INSTALL.md`（第 260 行端点列举、第 281 行接口地址）。

### 19.3 验证结果

- 单模块：`tests.test_ehviewer` **Ran 146 tests … OK**（原 128）。
- 全量：`.venv\Scripts\python.exe -m unittest discover -s tests -t .` → **Ran 858 tests … OK (skipped=1)**，约 21.8 s（`test_bridge` 334 + `test_iwara` 199 + `test_asmr_one` 134 + **`test_ehviewer` 146** + `test_packaging` 45）。
- `python scripts/check_boundary.py` → `ehviewer 15 条路由（其中站点自己的 9 条）`，其余站点与框架数字不变，「边界检查通过」。
- `python scripts/make_share_packages.py --check` → 8 个包全就绪（`ehviewer-api` 43 个文件）。

### 19.4 本段踩到的坑

- **硬编码的路由清单不止一处。** 改完 `scripts/sites.py` 与 `len(paths)` 断言后全量测试仍失败：`tests/test_ehviewer.py:1718 test_the_sites_registry_knows_about_ehviewer` 里还有一个 9 元素 frozenset 是独立写死的，报 `Items in the first set but not the second: '/api/eh/page' '/api/eh/popular'`。教训：加端点时不能只 grep 路由名字符串，**断言用的是集合字面量、grep 关键字未必命中**，跑一遍全量测试才是可靠手段。
- **假客户端会掩盖真缺陷。** 若 `_FakeEhClient` 自己实现一套分页算术，新增的 `/images` 断言测的就只是假货；改成调用真实 `pagination()` 后，路由测试才真正覆盖生产逻辑。

## 20. 提交后冒烟揪出的真缺陷：「不存在」可以是 HTTP 200（2026-10-09）

第 19 节以 `ea60f0f` 提交，**全部 858 项离线测试通过**。随后照例做一次**真实链路**冒烟（非单测），立刻发现所有离线测试都覆盖不到的问题。

### 20.1 症状

对一批真实画廊逐个取详情时，有几个返回「成功但空」：`title=''`、`page_links=0`、`showing={'from': None, 'to': None, 'total': None}`，`gid` 还被兜底逻辑填回去了。既不是 404，也不抛错。

### 20.2 根因

上游对「取不到的画廊」**不返回 404**，而是 **HTTP 200 + 一句纯文本**。直取正文实测（`c.request('GET', f'https://e-hentai.org/g/{gid}/{tok}/')`，未经解析）：

| 输入 | 状态码 | 字节数 | 正文 |
| --- | --- | --- | --- |
| `4242119/0de46151`、`1/aaaaaaaaaa` | 200 | 39 | `Key missing, or incorrect key provided.` |
| `0/0000000000` | 200 | 21 | `No gallery specified.` |
| `999999999/bbbbbbbbbb` | 200 | 172 | `Gallery not found. If you just added this gallery, you may have to wait a short while before it becomes available. It might also have been reverted to its previous version.` |

`_get_html` 只在 `status_code == 404` 时抛 `EhNotFoundError`，200 于是被放行，`parse_detail()` 在一段纯文本上什么也解析不出来 —— **空壳**。

`/s/` 大图页有同一套：`GET /s/aaaaaaaaaa/1-1` → **200 + 13 字节** `Invalid page.`。

### 20.3 判据与验证

不能用文案当判据（会变），要用**页面骨架**：

- 详情页正常则必有 `#gn` / `#gdc` / `#gdd` / `#gdn` → `_DETAIL_MARK_RE = re.compile(r'id="(?:gn|gdc|gdd|gdn)"')`；
- 大图页正常则必有 `<img id="img" …>` → 复用既有的 `_IMG_SRC_RE` / `_IMG_SRC_RE2`。

判定顺序：**先看骨架，有骨架直接放行**（哪怕文案恰好出现在正常页里）；没有骨架才去匹配那几句存根文字；两者都不中则放行（保持原有空壳行为，不乱报 404）。

实测分离度（探针 `_scratch/_probe_eh_stub.py`，`EXIT=0`）：

- 真实详情页 **31 张全部命中骨架、零误判** —— 含首页 25 个结果、同一画廊的 `?p=1`/`?p=2`/`?p=16`/`?p=17`/`?p=999` 各屏（`?p=16` 起是与首屏同字节数的 14814 B 尾屏）；3 个存根全部落空。
- 真实 `/s/` 页有 `<img id="img">`，`Invalid page.` 没有。

### 20.4 改动

- `ehviewer/parse.py`：新增 `_DETAIL_MARK_RE`、`_GALLERY_STUB_PATTERNS`（`key` / `no_gallery` / `removed`）、`parse_gallery_error(body) -> tuple[str, str] | None`；新增 `_S_STUB_PATTERNS`（`invalid` / `key`）与 `parse_s_page_error(body)`。两者都返回 `(code, 上游原文)` 或 `None`。
- `ehviewer/client.py`：`gallery()` 在 `_get_html` 之后、`parse_detail()` 之前拦一道，命中则抛 `EhNotFoundError`（404 / `reason: "not_found"`），消息含 code 与上游原文；`image_page()` 同样在 `parse_s_page()` 之前拦一道。
- `tests/test_ehviewer.py`：新增 4 个存根夹具（`STUB_KEY` / `STUB_NO_GALLERY` / `STUB_REMOVED` / `STUB_INVALID_PAGE`，原文照抄）+ 11 个用例（三种存根各识别一次、保留上游原文、真实详情页放行、**骨架优先于文案**、无关 HTML 不误判、`/s/` 三种情形、客户端层 200 存根转 404 且消息带原文）。
- 文档：`README.md`（测试计数 858 → 869、新增实现说明第 6 条）、`DIRECT.md` §3.2「陷阱二」补第三类「200 配纯文本」（第 90-95 行那节）。

### 20.5 验证结果

- `tests.test_ehviewer`：**Ran 157 tests … OK**（146 → 157）。
- 全量：**Ran 869 tests … OK (skipped=1)**，21.7 s（`test_bridge` 334 + `test_iwara` 199 + `test_asmr_one` 134 + **`test_ehviewer` 157** + `test_packaging` 45）。
- `check_boundary.py` → 路由数不变（`ehviewer 15 条，自有 9 条`），「边界检查通过」；`make_share_packages.py --check` → 8 个包全就绪。
- **真实链路回归**（探针 `_scratch/_probe_eh_stubfix.py`，`EXIT=0`）：三个画廊存根全部变成 `EhNotFoundError(404)` 且消息含原文与 code；真实画廊 `4241779/29578e0069` 仍 `OK total=322 links=20 pages=17`；`/s/aaaaaaaaaa/1-1` → 404，真实 `/s/bb925ff2a5/4241779-1` → 正常拿到直链。

### 20.6 教训

- **离线测试全绿不等于对。** 这一条缺陷的性质是「上游的行为与代码的假设不一致」，夹具全是我按假设写的，所以 869 项里没有任何一项可能发现它；只有打真实请求才会暴露。**每次加完取数逻辑，都要真跑一次链路。**
- **状态码是上游的实现细节，不是契约。** 报「不存在」时它可能用 404，也可能用 200 加一句话。凡是把状态码当唯一判据的地方（`_get_html` 的 `if resp.status_code == 404`）都值得回头问一句。
- **判据要选不变的东西。** 文案会改，骨架不会；所以先认骨架、文案只作兜底，这样上游换句话说同一件事时不会把正常页判死。


## 21. 把骨架判据纳入开机自检（2026-10-09）

### 21.1 动机

§20 修完之后，我按 §20.6 的第一条教训检查了一遍「这条判据现在有人守吗」，答案是**没有**。

`ehviewer/envcheck.py` 原有的三项只探到列表页：`_site_check` 真发一次 `list_galleries()`，而列表页（`parse_galleries`）与详情页（`parse_detail` + `parse_gallery_error`）**走的是两套完全不同的解析**。也就是说 §20 新加的那条「页面骨架」判据即使明天就失效，`--check` 也照样报绿。

失效的后果还不轻：`_DETAIL_MARK_RE` 一旦不再命中真实详情页，`parse_gallery_error` 会把**每一个真实画廊**都判成存根，`gallery()` 全部抛 404。这不是「少个字段」而是「整个详情接口全灭」，而且是静默翻车 —— 列表页还是好的，用户会以为只是详情坏了。

### 21.2 设计

新增第四项 `ehviewer 详情`（`DETAIL_NAME = "ehviewer 详情"`，`DETAIL_TIMEOUT = 40.0`，`DETAIL_SAMPLES = 2`），实现在 `ehviewer/envcheck.py` 的 `_detail_check(settings)`：

1. 先 `page = await client.list_galleries()` 拿真实列表（复用站点项已经证明通的链路）。
2. 列表为空 → 报「列表页没给出画廊，无法验详情解析」（而不是假装通过）。
3. 取**头两条**（`DETAIL_SAMPLES = 2`）逐条 `await client.gallery(gid, token)`：一条就够看出判据认不认得真实页面，取两条是防第一条恰好是被删/改版的上传。
4. `title` 与 `page_links` 同时为空 → 计入 `empty`，明细列出 gid。
5. 有 `empty` → 失败，消息附 `_HINT_DETAIL`（「多半是上游改了详情页结构，`ehviewer/parse.py` 的骨架判据（#gn / #gdc / #gdd）不再命中 —— 真实画廊会被当成存根」）。
6. 全部正常 → `f"{checked} 条真实详情解析正常（判据仍认得上游页面）"`。

三个刻意的决定：

- **不吞 `EhNotFoundError`。** 真实画廊被判成存根时抛的正是它；把 `EhNotFoundError` 当成「跳过」就等于把这项自检要抓的故障吞了。它走 `except EhError` 分支报失败。
- **`DETAIL_SAMPLES` 取两条而不是一条。** 上游某个具体上传被删/改版会误报，两条都不正常才更可信。
- **沿用「只警告、不中止」。** 与其余三项一致，`extra_checks` 的返回值不影响启动。

`_async_checks` 相应改为三项并发 `asyncio.gather(_edge_check, _site_check, _detail_check)`；`--check` 的顺序由 `extra_checks` 组装成 `[deps, edge, site, detail]`。

### 21.3 先改断言、再交东西

`tests/test_ehviewer.py` 的 `EhEnvCheckTestCase`（原先 `_fake_client(edges, edges_error, page, page_error)`）扩了 `detail` / `detail_error` 两个口子，并新增 `_page(**kw)` 造默认列表页（`{"count": 2, "items": [{"gid": 4241779, "token": "29578e0069"}, {"gid": 4241780, "token": "aabbccddee"}]}`）；`test_extra_checks_runs_deps_first_then_the_network_trio` 的断言改成四项。新增 4 个用例：

| 用例 | 输入 | 断言 |
| --- | --- | --- |
| `test_detail_check_accepts_a_real_looking_detail` | 默认 | ok；`instance.gallery.await_count == 2` |
| `test_detail_check_reports_when_parse_yields_empty` | `detail={"title": "", "page_links": []}` | 不 ok，含「解析成空」与 gid `4241779` |
| `test_detail_check_reports_a_stub_404_as_a_fault` | `detail_error=EhNotFoundError("取不到画廊（removed）：Gallery not found.")` | 不 ok，含「详情不可用」与「骨架判据」 |
| `test_detail_check_says_so_when_the_list_is_empty` | `page={"count": 0, "items": []}` | 不 ok，含「无法验详情解析」 |

### 21.4 验证结果

- `tests.test_ehviewer`：**Ran 161 tests … OK**（157 → 161）。
- 全量：**Ran 873 tests … OK (skipped=1)**，21.4 s（334 + 199 + 134 + **161** + 45）。
- `check_boundary.py` → 路由数不变（`ehviewer 15 条，自有 9 条`），「边界检查通过」。**自检不是路由**，所以边界检查与分享包清单都不受影响。
- **真实链路**（`python ehviewer/run.py --check`，`EXIT=0`）：`python OK` / `deps OK` / `files OK` / `port OK 8765 可用` / `ehviewer 依赖 OK curl_cffi 0.16.3 已就绪（含 HTTP/3 / QUIC）` / `ehviewer 边缘 OK 16/16 个候选边缘可建连（示例 104.17.0.1）` / `ehviewer 站点 OK e-hentai.org 可达（HTTP/3 + 借道边缘），首页取回 25 条` / **`ehviewer 详情 OK 2 条真实详情解析正常（判据仍认得上游页面）`** / `[扩展] 已挂载 2 个：novel（5 个路由）、iwara_web（1 个路由）`。

### 21.5 教训

- **自检探到的层，必须是你真正怕坏的那一层。** 原来那项叫「ehviewer 站点」，名字听起来是「整站通不通」，实际只到列表页。**名字比覆盖面宽，是最容易骗过自己的一种绿。**
- **修完一个缺陷，问的下一句是「同类缺陷还有谁会发现」。** §20 修的是代码，不是「代码错了没人知道」这件事；后者要靠自检补。离线测试按定义就发现不了这类「假设与上游不符」的错，所以**真跑链路的那一项才是最后一道网**。
- **一条守判据的自检比一条验功能的测试更值。** 验功能的测试在上游改版时会跟着一起错（夹具是我按假设写的）；守判据的自检直接打真实页面，是唯一不受我自己假设影响的一环。


