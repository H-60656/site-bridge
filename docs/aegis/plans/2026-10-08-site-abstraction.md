# 站点抽象：框架与接口站点分层（pixiv / iwara 同级）

- 日期：2026-10-08
- 触发需求（用户原话，m00001）："项目中存在错误布局，两个接口项目应该是同级的，错误修正后更新分享包；现在想抽象两个接口项目，方便后续实现其他接口项目"
- 用户已确认的方案（m00066）：`depth=A`（彻底分层）+ `iwara_stack=只提供 iwara 接口`
- 状态：待执行

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

- **全量测试**：`.venv\Scripts\python.exe -m unittest discover -s tests` → `Ran 457 tests in 11.241s` / `OK`（`test_bridge` 314 + `test_iwara` 98 + `test_packaging` 45）；`discover -t . -s tests` 同样 457 项 OK。
  - 偏差：本机 `.venv` 里**没有 pytest**，`python -m pytest` 不可用；`tests/` 原先没有 `__init__.py`，`unittest discover -s tests` 会报 `ImportError: Start directory is not importable`，已补 `tests/__init__.py`。
- **边界检查**：`.venv\Scripts\python.exe scripts\check_boundary.py` → `框架外壳 6 条路由` / `pixiv 60 条路由（其中站点自己的 54 条）` / `iwara 16 条路由（其中站点自己的 10 条）` / `novel（扩展，宿于 pixiv）: 比宿主多 10 条路由` / `iwara_web（扩展，宿于 iwara）: 比宿主多 1 条路由` / **边界检查通过**。
- **打包自检**：`.venv\Scripts\python.exe scripts\make_share_packages.py --check` → 六个包内容就绪：`pixiv-api` 59、`iwara-api` 43、`novel-extension` 17、`iwara_web-extension` 8、`pixiv-novel` 72、`iwara-iwara_web` 47 个文件 → `分享包处理完成`。
- **重新生成 `dist/`**：`.venv\Scripts\python.exe scripts\make_share_packages.py` → `pixiv-api.zip` 59 文件 15.97 MB（压缩后 5.81 MB）、`iwara-api.zip` 43 文件 0.31 MB（0.13 MB）、`novel-extension.zip` 17 文件、`iwara_web-extension.zip` 8 文件、`pixiv-novel.zip` 72 文件 16.15 MB（5.87 MB）、`iwara-iwara_web.zip` 47 文件。旧 key 名的 `pixiv_bridge-api.zip` / `pixiv_bridge-novel.zip` 与 `dist/.stage/` 已删除（key 改名后不会被覆盖，脚本本身不清理异名 zip）。
- **入口烟测**（全部 `--check`）：
  - `.venv\Scripts\python.exe pixiv\run.py --check` → 表里 `python/deps/files/port/ech` 五项 OK（ECH：`app-api.pixiv.net 返回 HTTP 400，OpenSSL：项目自带`）→ `登录成功: huangee (user_mcys7857)` → exit 0。
  - `.venv\Scripts\python.exe iwara\run.py --check` → 前三项 + `iwara 依赖 OK curl_cffi 0.16.3` / `iwara 网络 OK api.iwara.tv 可达（共 2 个视频）` / `iwara 登录 OK token 有效：huanghe19512` → exit 0；`IWARA_REFRESH_TOKEN=definitely-not-a-real-token` 时该行变 `WARN token 不可用：刷新 token 失败（403）` 且 exit 1。
  - `.venv\Scripts\python.exe -m pixiv_bridge` → `用法: python -m pixiv_bridge <站点> [入口参数...]` / `可选站点: iwara, pixiv` → exit 2；`python -m pixiv_bridge iwara --check` 与 `iwara\run.py --check` 输出一致（同一 `SITE`）。
  - 站点隔离：`Get-ChildItem -Recurse -Filter run.py -Depth 2` 只列出 `iwara\run.py`、`pixiv\run.py`（根 `run.py`、`novel\run.py`、`iwara_web\run.py` 都不存在）。
- **旧引用残留检查**：`pixiv_bridge.(api|oauth|image_proxy|ech_client|sni_client|hoster)` / `from pixiv_bridge.routes.(auth|illust|search|feed|user|pixiv_novel|misc)` / `pixiv_bridge.server import create_app` 在 `*.py,*.md,*.txt,*.bat,*.sh,scripts/,pixiv/,iwara/,novel/,iwara_web/,pixiv_bridge/,tests/` 全库扫描 **0 命中**；`scripts/*.py` 里残留的 `"run.py"` 都是站点目录内的（`sites.py:41` 的 `files=("run.py",)`、`sites.py:173` 注释「根目录不再有入口」、`make_share_packages.py:410` 的 `target/name/"run.py"`、`check_boundary.py:107` 的 `rel == "run.py"`）。
- **分享包抽检**（解压到临时目录，**cwd 设为包根**再跑，否则会读到工作区的 `.env`）：
  - `iwara-api.zip` → 包内 `iwara\run.py --check`：`[环境检查]` 七项全 OK、无扩展挂载（包内没有 `.env`）、`iwara 登录 OK 匿名模式…` → exit 0；包内不含 `pixiv/`。
  - `pixiv-api.zip` → 包内 `pixiv\run.py --check`：五项 + `network OK 可达 3/3: app-api.pixiv.net, oauth.secure.pixiv.net, i.pximg.net`，随后是新装的提示（`缺少 refresh_token…` + 让跑 `python pixiv/get_token.py`）→ exit 1（预期：尚未登录）；包内没有根 `run.py`，`pixiv_bridge\api.py` 不存在。
- **顺带修掉的一个重复**：`iwara --check` 原先会打印两次 `iwara 登录 OK …`（`Site.checks` 一次、`Site.login_check` 一次）并真的刷两次 token；现在 `iwara/envcheck.py` 用进程内 `_AUTH_MEMO`（元素 `(token, CheckResult)`）缓存，`extra_checks()` 每次开头清空缓存、`login_check()` 只复用不重跑也不再重复打印。`tests.test_iwara` 98 项在此改动后复跑仍 OK。
- **顺带修掉的一个打包缺陷（Dockerfile）**：旧 Dockerfile 无条件 `COPY pixiv pixiv` / `COPY novel novel` / `COPY iwara/requirements.txt …`，而 `iwara-api.zip` 里根本没有 `pixiv/`、`novel/`，`pixiv-api.zip` 里没有 `iwara/` —— 两份分享包里的 Dockerfile 都构建不起来。现在改成「`COPY requirements.txt .` → 装框架依赖 → `COPY . .`（框架 + 镜像里**实际存在**的站点与扩展）→ 有 `iwara/requirements.txt` 才装 `curl_cffi`」，`CMD` 改成 `["sh", "start.sh"]`（与一键脚本同一套站点选择逻辑：有 `pixiv/` 跑 pixiv，否则 iwara；`-e BRIDGE_SITE=iwara` 可显式指定）。实测：四个 API/组合包里的 Dockerfile 末行都是 `CMD ["sh", "start.sh"]`，扩展包（`novel-extension` / `iwara_web-extension`）**不含** Dockerfile，只带 `README.md` + 扩展目录 + `手册/`。**未做**：本机 Docker Desktop 守护进程没运行（`docker --version` → 29.7.2，但 `docker images` 报 `failed to connect to the docker API at npipe:////./pipe/dockerDesktopLinuxEngine`），所以没有真跑 `docker build` 验证。
- README 的 Docker 段同步改为「镜像入口是 `CMD ["sh", "start.sh"]`，由它挑站点」，并把 ECH 那段「再 COPY 进镜像」改成「放进 `openssl-libs/linux-x86_64/`，`COPY . .` 会一起带进去」。

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
