#!/usr/bin/env python3
"""核对整理是否成立：框架不依赖任何接口项目、各接口项目同级且互不依赖、出入口清爽。

用法：python scripts/check_boundary.py

检查四件事：

1. 依赖方向：``site_bridge/`` 里不出现任何站点/扩展的名字；各接口项目互不
   依赖；扩展只允许依赖框架、自己与它的宿主站点；
2. 出入口：每个接口项目一个 ``<site>/run.py``，根目录不再有 ``run.py``，
   扩展包只有 ``<ext>/extension.py``（没有专属入口）；
3. 注册表与运行时一致：``scripts/sites.py`` 里记的站点名/目录/标题/必需文件，
   与 ``<site>/app.py`` 里那份 ``SITE`` 对得上；
4. 路由隔离：每个站点各自只比框架外壳多出自己那部分（没有互相串台），
   每个扩展挂上去后**恰好**多出它声明的那几条路由。

站点清单不写死在这里，全部来自 ``scripts/sites.py``（唯一事实来源）。
"""

import ast
import json
import subprocess
import sys
from pathlib import Path

from sites import (
    ENTRY_POINTS,
    EXTENSION_FILENAME,
    EXTENSIONS,
    FORBIDDEN_ENTRIES,
    FRAMEWORK_PACKAGE,
    PROJECT_ROOT,
    SITE_ROUTE_PATHS,
    SITES,
    SITES_BY_NAME,
    SUB_PACKAGES,
)

# 本仓库顶层可见的包/模块名，其余顶层 import 一律当作第三方库放过。
LOCAL_NAMES = {p.stem for p in PROJECT_ROOT.glob("*.py")} | {
    p.name for p in PROJECT_ROOT.iterdir() if p.is_dir() and (p / "__init__.py").exists()
}


def iter_py(root):
    for path in sorted((PROJECT_ROOT / root).rglob("*.py")):
        yield path


def imports_of(path):
    """返回该文件所有绝对 import 的顶层模块名（相对导入不参与跨项目判断）。"""
    names = set()
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                names.add(alias.name.split(".")[0])
        elif isinstance(node, ast.ImportFrom) and not node.level:
            names.add((node.module or "").split(".")[0])
    return names


def check_imports(errors):
    """依赖方向：框架 → 不认识站点；站点 → 只认框架与自己；扩展 → 再加宿主。"""
    for sub in SUB_PACKAGES:
        bad = [p for p in iter_py(FRAMEWORK_PACKAGE) if sub in imports_of(p)]
        if bad:
            errors.append(
                f"框架不得 import 接口项目/扩展 {sub}，违规文件："
                + ", ".join(str(p.relative_to(PROJECT_ROOT)) for p in bad)
            )

    # 各接口项目同级：谁都不许 import 对方（扩展由各自的宿主负责挂）。
    for site in SITES:
        allowed = {FRAMEWORK_PACKAGE, site.directory}
        for path in iter_py(site.directory):
            for name in imports_of(path) & LOCAL_NAMES - allowed:
                errors.append(
                    f"接口项目 {site.name} 不应依赖本地包 {name}："
                    f"{path.relative_to(PROJECT_ROOT)}"
                )

    # 扩展可以依赖框架、自己与**它的**宿主站点（页面要调接口）；跨宿主不许。
    for ext in EXTENSIONS:
        allowed = {FRAMEWORK_PACKAGE, ext.name, ext.host}
        for path in iter_py(ext.name):
            for name in imports_of(path) & LOCAL_NAMES - allowed:
                errors.append(
                    f"扩展 {ext.name} 不应依赖本地包 {name}："
                    f"{path.relative_to(PROJECT_ROOT)}"
                )


def check_layout(errors):
    """出入口：接口项目各留一个入口，扩展包只有 extension.py。"""
    for rel in ENTRY_POINTS:
        if not (PROJECT_ROOT / rel).is_file():
            errors.append(f"缺少接口项目入口：{rel}")

    for site in SITES:
        if not (site.path / "app.py").is_file():
            errors.append(f"接口项目缺少站点描述符：{site.directory}/app.py")

    for rel in FORBIDDEN_ENTRIES:
        if not (PROJECT_ROOT / rel).exists():
            continue
        if rel == "run.py":
            errors.append(
                "根目录不应再有 run.py —— 接口项目的入口是："
                + " / ".join(ENTRY_POINTS)
            )
        else:
            errors.append(
                f"扩展包不应再有专属入口 {rel}：它由宿主站点按配置挂载"
            )

    for ext in EXTENSIONS:
        entry = ext.path / EXTENSION_FILENAME
        if not entry.is_file():
            errors.append(f"扩展包缺少扩展入口：{ext.entry}")


# 路由取法要兼容两种 FastAPI 形态：新版本 include_router() 不再把子路由摊平进
# app.routes，而是塞一个包装对象（真正的路由挂在 .original_router.routes 上）；
# 旧版本本来就是摊平的。只认 .path 会在新版本上把整棵子路由当空气。
#
# 探针在子进程里跑：它要 import 站点（iwara 会连带 import curl_cffi），
# 不能把这份依赖带进检查脚本自己。
ROUTE_PROBE = """
import json, sys
sys.path.insert(0, r'{root}')
from site_bridge.server import create_bridge_app
from site_bridge.config import Settings
from site_bridge.extensions import load_extension_specs
{imports}

#: 站点名 → 站点模块（由上面的 import 逐条拼出来，不写死某几个站点名）。
SITE_MODULES = {{{module_map}}}

def key(app):
    out = set()
    for r in app.routes:
        holder = getattr(r, 'original_router', None)
        items = holder.routes if holder is not None else [r]
        for item in items:
            path = getattr(item, 'path', None)
            if path is not None:
                out.add(path)
    return out

settings = Settings.load(r'{root}')
shell = key(create_bridge_app())
apps = {{}}
for site in {sites!r}:
    apps[site] = key(SITE_MODULES[site].create_app(settings))

result = {{
    'shell': len(shell),
    'apps': {{name: len(paths) for name, paths in apps.items()}},
    'routes': {{name: sorted(paths - shell) for name, paths in apps.items()}},
    'sites': {{}},
    'extensions': {{}},
    'shared': [],
}}
names = list(apps)
for left in range(len(names)):
    for right in range(left + 1, len(names)):
        shared = (apps[names[left]] - shell) & (apps[names[right]] - shell)
        result['shared'].extend(sorted(shared))

for site in {sites!r}:
    descriptor = SITE_MODULES[site].SITE
    result['sites'][site] = {{
        'name': descriptor.name,
        'title': descriptor.title,
        'version': descriptor.version,
        'directory': str(descriptor.directory),
        'required_files': list(descriptor.required_files),
        'has_checks': descriptor.checks is not None,
        'has_login_check': descriptor.login_check is not None,
    }}

for ext, host in {extensions!r}.items():
    exts = load_extension_specs([ext], r'{root}')
    host_app = SITE_MODULES[host].create_app(settings, extensions=exts)
    result['extensions'][ext] = sorted(key(host_app) - apps[host])

print(json.dumps(result))
"""


def _probe_source():
    site_names = [site.name for site in SITES]
    imports = "\n".join(
        f"import {site.directory}.app as {site.name}_app" for site in SITES
    )
    module_map = ", ".join(f"{site.name!r}: {site.name}_app" for site in SITES)
    extensions = {ext.name: ext.host for ext in EXTENSIONS}
    return ROUTE_PROBE.format(
        root=PROJECT_ROOT,
        imports=imports,
        module_map=module_map,
        sites=site_names,
        extensions=extensions,
    )


def check_apps(errors):
    out = subprocess.run(
        [sys.executable, "-c", _probe_source()], capture_output=True, text=True
    )
    if out.returncode != 0:
        last = (out.stderr.strip().splitlines() or [""])[-1]
        hint = ""
        # 站点的 client 可能在模块顶层 import 站点专属依赖（比如 iwara / asmr_one
        # 都要 curl_cffi：Cloudflare 只认浏览器 TLS 指纹），而这些只写在站点自己的
        # requirements.txt 里。新克隆的仓库若还没跑过那个站点的 run.py，这里会缺包
        # —— 点明怎么补，别让人以为路由写错了。
        needs = [site for site in SITES if site.requirements]
        for site in needs:
            if site.extra_modules and any(m in last for m in site.extra_modules):
                hint = (
                    f"；先装 {site.name} 的额外依赖："
                    f"pip install -r {site.requirements}"
                )
                break
        errors.append(f"站点无法同时导入：{last}{hint}")
        return
    try:
        data = json.loads(out.stdout)
    except json.JSONDecodeError:
        errors.append(f"路由探针输出无法解析：{out.stdout[:200]}")
        return

    print(f"框架外壳 {data['shell']} 条路由")
    for site in SITES:
        info = data["apps"][site.name]
        print(f"  {site.name:<8} {info} 条路由（其中站点自己的 {len(data['routes'][site.name])} 条）")

    for path in data["shared"]:
        errors.append(f"站点之间的路由串台了：{path}")

    for name, expected_paths in SITE_ROUTE_PATHS.items():
        if name not in data["routes"]:
            continue
        extra = set(data["routes"][name])
        if extra != set(expected_paths):
            errors.append(
                f"{name} 站点路由与预期不符：多出 "
                f"{sorted(extra - set(expected_paths))}，缺少 "
                f"{sorted(set(expected_paths) - extra)}"
            )

    for site in SITES:
        runtime = data["sites"][site.name]
        expected = {
            "name": site.name,
            "title": site.title,
            "version": site.version,
            "directory": str(site.path),
            "required_files": list(site.files),
        }
        for field, value in expected.items():
            if runtime.get(field) != value:
                errors.append(
                    f"{site.name}/app.py 的 SITE.{field} 是 {runtime.get(field)!r}，"
                    f"与 scripts/sites.py 记的 {value!r} 不一致"
                )
        if not runtime.get("has_checks"):
            errors.append(f"{site.name}/app.py 的 SITE 没有 checks 钩子（自检项）")

    for ext in EXTENSIONS:
        extra = set(data["extensions"].get(ext.name, ()))
        label = f"{ext.name}（扩展，宿于 {ext.host}）"
        print(f"  {label}: 比宿主多 {len(extra)} 条路由")
        if extra != set(ext.extra_paths):
            errors.append(
                f"{ext.name} 路由与预期不符：多出 "
                f"{sorted(extra - ext.extra_paths)}，缺少 "
                f"{sorted(set(ext.extra_paths) - extra)}"
            )


def main():
    errors = []
    check_imports(errors)
    check_layout(errors)
    check_apps(errors)
    for line in errors:
        print(f"[FAIL] {line}")
    print("边界检查通过" if not errors else f"边界检查失败：{len(errors)} 项")
    return 1 if errors else 0


if __name__ == "__main__":
    sys.exit(main())
