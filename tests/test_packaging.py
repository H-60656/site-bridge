"""分享包脚本（``scripts/make_share_packages.py``）的单元测试。

这个脚本不在任何包里（``scripts/`` 没有 ``__init__.py``），所以用
``importlib`` 按文件路径加载。它把结果写到 ``DIST_DIR``，测试必须把它替换成
``_tmp_tests/`` 下的临时目录，否则会往真实的 ``dist/`` 里写东西。
"""

import contextlib
import dataclasses
import importlib.util
import io
import itertools
import os
import shutil
import sys
import unittest
import zipfile
from pathlib import Path
from unittest.mock import patch

PROJECT_ROOT = Path(__file__).resolve().parent.parent
PACKAGING_PATH = PROJECT_ROOT / "scripts" / "make_share_packages.py"
TEST_TMP_ROOT = PROJECT_ROOT / "_tmp_tests"     # 与 tests/test_bridge.py 一致的临时目录根
_TEMP_COUNTER = itertools.count(1)


def make_temp_dir(tag="pack"):
    """在 ``_tmp_tests/`` 下建一个临时目录（系统 TEMP 在这台机器上不可写）。"""
    path = TEST_TMP_ROOT / ("%s_%d_%d" % (tag, os.getpid(), next(_TEMP_COUNTER)))
    path.mkdir(parents=True, exist_ok=True)
    return path


def drop_temp_dir(path):
    shutil.rmtree(path, ignore_errors=True)
    try:
        TEST_TMP_ROOT.rmdir()
    except OSError:
        pass


def load_packaging_module():
    spec = importlib.util.spec_from_file_location("make_share_packages", PACKAGING_PATH)
    module = importlib.util.module_from_spec(spec)
    # 必须先登记进 sys.modules：脚本里有 ``from __future__ import annotations``，
    # dataclasses 解析字符串注解时会回头查 sys.modules[cls.__module__]。
    sys.modules[spec.name] = module
    try:
        spec.loader.exec_module(module)
    except BaseException:
        sys.modules.pop(spec.name, None)
        raise
    return module


packaging = load_packaging_module()


class PackageSpecTestCase(unittest.TestCase):
    """包清单本身要自洽：key 唯一、内容存在、扩展包只带自己。"""

    def test_package_count_follows_the_site_registry(self):
        # 每加一个站点就多一个 api 包，另有固定 2 个扩展包 + 2 个组合包。
        self.assertEqual(len(packaging.PACKAGES), len(packaging.SITES) + 4)
        keys = [spec.key for spec in packaging.PACKAGES]
        zips = [spec.zip_name for spec in packaging.PACKAGES]
        self.assertEqual(len(set(keys)), len(keys))
        self.assertEqual(len(set(zips)), len(zips))

    def test_every_package_declares_title_summary_and_a_known_kind(self):
        for spec in packaging.PACKAGES:
            with self.subTest(key=spec.key):
                self.assertTrue(spec.title.strip())
                self.assertTrue(spec.summary.strip())
                self.assertIn(spec.kind, ("api", "extension", "bundle"))

    def test_every_declared_entry_exists_in_the_project(self):
        """回归：目录或根文件被改名/搬走时，打包脚本必须在测试里就先失败。"""
        for spec in packaging.PACKAGES:
            for name in spec.entries():
                with self.subTest(key=spec.key, entry=name):
                    self.assertTrue(
                        (packaging.PROJECT_ROOT / name).exists(),
                        "分享包 %s 声明了 %s，但项目里没有" % (spec.key, name),
                    )

    def test_documentation_files_exist(self):
        for name in packaging.DOC_FILES:
            with self.subTest(name=name):
                self.assertTrue((packaging.PROJECT_ROOT / name).is_file())

    def test_packages_by_key_matches_the_tuple(self):
        expected = {spec.key for spec in packaging.PACKAGES}
        self.assertEqual(set(packaging.PACKAGES_BY_KEY), expected)
        for key, spec in packaging.PACKAGES_BY_KEY.items():
            self.assertEqual(spec.key, key)

    def test_api_and_bundle_packages_carry_the_api_root_files(self):
        for spec in packaging.PACKAGES:
            if spec.kind == "extension":
                continue
            with self.subTest(key=spec.key):
                self.assertEqual(tuple(spec.files), packaging.API_ROOT_FILES)
                self.assertIn("pixiv_bridge", spec.dirs)

    def test_extension_packages_ship_only_their_own_directory(self):
        extensions = [spec for spec in packaging.PACKAGES if spec.kind == "extension"]
        self.assertEqual(len(extensions), 2)
        for spec in extensions:
            with self.subTest(key=spec.key):
                self.assertEqual(len(spec.dirs), 1)
                self.assertEqual(spec.dirs[0], spec.key.split("-")[0])
                self.assertEqual(tuple(spec.files), ())
                self.assertIn("PIXIV_BRIDGE_EXTENSIONS=", "\n".join(spec.start_commands))

    def test_entries_concatenates_files_extra_files_and_dirs(self):
        spec = packaging.PackageSpec(
            key="k", zip_name="k", title="t", kind="api", summary="s",
            dirs=("d",), files=("f",), extra_files=("e",),
        )
        self.assertEqual(spec.entries(), ("f", "e", "d"))

    def test_package_specs_are_frozen(self):
        spec = packaging.PACKAGES[0]
        with self.assertRaises(dataclasses.FrozenInstanceError):
            spec.key = "other"


class PackagingHelpersTestCase(unittest.TestCase):

    def setUp(self):
        self.dirs = []

    def tearDown(self):
        for path in self.dirs:
            drop_temp_dir(path)

    def make_dir(self, tag="pkg"):
        path = make_temp_dir(tag)
        self.dirs.append(path)
        return path

    def test_should_ignore_skips_private_and_bytecode(self):
        for name in ("__pycache__", ".venv", "data", "dist", "_scratch", "_tmp_tests"):
            with self.subTest(name=name):
                self.assertTrue(packaging.should_ignore(name))
        for name in ("x.pyc", "x.pyo"):
            with self.subTest(name=name):
                self.assertTrue(packaging.should_ignore(name))
        for name in ("pixiv_bridge", "README.md", "requirements.txt", ".env.example"):
            with self.subTest(name=name):
                self.assertFalse(packaging.should_ignore(name))

    def test_copy_tree_copies_files_and_reports_count(self):
        root = self.make_dir()
        src = root / "src"
        src.mkdir()
        (src / "a.py").write_text("a", encoding="utf-8")
        (src / "b.txt").write_text("b", encoding="utf-8")
        dst = root / "out"
        count = packaging._copy_tree(src, dst)
        self.assertEqual(count, 2)
        self.assertTrue((dst / "a.py").is_file())
        self.assertTrue((dst / "b.txt").is_file())

    def test_copy_tree_recurses_into_subdirectories(self):
        root = self.make_dir()
        src = root / "src"
        (src / "inner" / "deeper").mkdir(parents=True)
        (src / "inner" / "x.py").write_text("x", encoding="utf-8")
        (src / "inner" / "deeper" / "y.py").write_text("y", encoding="utf-8")
        dst = root / "out"
        count = packaging._copy_tree(src, dst)
        self.assertEqual(count, 2)
        self.assertTrue((dst / "inner" / "deeper" / "y.py").is_file())

    def test_copy_tree_skips_ignored_names(self):
        root = self.make_dir()
        src = root / "src"
        src.mkdir()
        (src / "keep.py").write_text("keep", encoding="utf-8")
        (src / "drop.pyc").write_text("bytecode", encoding="utf-8")
        (src / "data").mkdir()
        (src / "data" / "tokens.json").write_text("{}", encoding="utf-8")
        (src / "__pycache__").mkdir()
        (src / "__pycache__" / "keep.cpython-313.pyc").write_text("x", encoding="utf-8")
        dst = root / "out"
        count = packaging._copy_tree(src, dst)
        self.assertEqual(count, 1)
        self.assertEqual(sorted(p.name for p in dst.iterdir()), ["keep.py"])

    def test_copy_tree_creates_the_destination(self):
        root = self.make_dir()
        src = root / "src"
        src.mkdir()
        (src / "f").write_text("f", encoding="utf-8")
        dst = root / "nested" / "deeper"
        packaging._copy_tree(src, dst)
        self.assertTrue(dst.is_dir())


class StagePackageTestCase(unittest.TestCase):

    def setUp(self):
        self.dist = make_temp_dir("stage")
        self.dirs = [self.dist]
        self.patch = patch.object(packaging, "DIST_DIR", self.dist)
        self.patch.start()
        self.addCleanup(self.patch.stop)

    def tearDown(self):
        for path in self.dirs:
            drop_temp_dir(path)

    def test_stage_package_lays_out_an_extension(self):
        spec = packaging.PACKAGES_BY_KEY["novel-extension"]
        target = packaging.stage_package(spec)
        self.assertEqual(target, self.dist / ".stage" / spec.zip_name)
        self.assertTrue((target / "novel" / "extension.py").is_file())
        self.assertTrue((target / "README.md").is_file())
        for name in packaging.DOC_FILES:
            self.assertTrue((target / "手册" / name).is_file(), name)

    def test_stage_package_does_not_include_bytecode_or_data(self):
        spec = packaging.PACKAGES_BY_KEY["iwara_web-extension"]
        target = packaging.stage_package(spec)
        for path in target.rglob("*"):
            with self.subTest(path=str(path)):
                self.assertNotIn("__pycache__", path.parts)
                self.assertFalse(path.name.endswith((".pyc", ".pyo")), path)
                self.assertNotIn(path.name, ("tokens.json", "history.jsonl"))


class StagePackageFailureTestCase(unittest.TestCase):

    def setUp(self):
        self.dist = make_temp_dir("stagefail")
        self.dirs = [self.dist]
        self.patch = patch.object(packaging, "DIST_DIR", self.dist)
        self.patch.start()
        self.addCleanup(self.patch.stop)

    def tearDown(self):
        for path in self.dirs:
            drop_temp_dir(path)

    def test_stage_package_reports_missing_directories(self):
        spec = packaging.PackageSpec(
            key="ghost", zip_name="ghost", title="t", kind="api", summary="s",
            dirs=("没有这个目录",),
        )
        with self.assertRaises(SystemExit) as ctx:
            packaging.stage_package(spec)
        message = str(ctx.exception)
        self.assertIn("ghost", message)
        self.assertIn("没有这个目录", message)

    def test_stage_package_reports_missing_root_files(self):
        spec = packaging.PackageSpec(
            key="ghost2", zip_name="ghost2", title="t", kind="api", summary="s",
            files=("no_such_file.txt",),
        )
        with self.assertRaises(SystemExit) as ctx:
            packaging.stage_package(spec)
        self.assertIn("no_such_file.txt", str(ctx.exception))

    def test_stage_package_replaces_a_previous_stage(self):
        spec = packaging.PACKAGES_BY_KEY["iwara_web-extension"]
        target = packaging.stage_package(spec)
        stale = target / "stale.txt"
        stale.write_text("old", encoding="utf-8")
        packaging.stage_package(spec)
        self.assertFalse(stale.exists())


class RenderReadmeTestCase(unittest.TestCase):

    def render(self, **overrides):
        fields = {
            "key": "demo", "zip_name": "demo", "title": "演示包", "kind": "api",
            "summary": "一句话说明。",
            "dirs": ("pixiv",), "files": ("requirements.txt",),
            "start_commands": ("# 启动", "python pixiv/run.py"),
            "setup_lines": ("第一步",),
            "notes": ("注意点",),
        }
        fields.update(overrides)
        return packaging.render_readme(packaging.PackageSpec(**fields))

    def test_api_package_says_it_carries_one_api_project(self):
        text = self.render(kind="api")
        self.assertIn("# 演示包", text)
        self.assertIn("一句话说明。", text)
        # 接口包 = 框架 + 恰好一个接口项目；各接口项目互不依赖。
        self.assertIn("一个接口项目", text)
        self.assertIn("互不依赖", text)

    def test_extension_package_says_it_cannot_start_alone(self):
        text = self.render(kind="extension")
        self.assertIn("不能单独启动", text)
        self.assertIn("扩展包", text)

    def test_bundle_package_explains_the_layout(self):
        text = self.render(kind="bundle")
        self.assertIn("接口项目", text)
        self.assertIn("扩展目录", text)

    def test_includes_start_commands_setup_and_notes(self):
        text = self.render()
        for piece in (
            "## 怎么启动", "python pixiv/run.py", "## 还要做什么", "1. 第一步",
            "## 说明", "- 注意点",
        ):
            with self.subTest(piece=piece):
                self.assertIn(piece, text)

    def test_includes_the_documentation_pointer_and_tree(self):
        text = self.render()
        self.assertIn("## 包里的目录", text)
        self.assertIn("## 完整文档", text)
        self.assertIn("## 不要拷的东西", text)
        for name in packaging.DOC_FILES:
            self.assertIn("手册/" + name, text)

    def test_tree_preview_lists_the_root_folder_and_readme(self):
        spec = packaging.PACKAGES_BY_KEY["pixiv-api"]
        tree = packaging._tree_preview(spec)
        self.assertTrue(tree.startswith(spec.zip_name + "/"))
        self.assertIn("README.md", tree)
        self.assertIn("手册/", tree)
        self.assertIn("pixiv_bridge/", tree)
        self.assertIn("pixiv/", tree)
        for name in spec.files:
            self.assertIn(name, tree)

    def test_omits_empty_sections(self):
        spec = packaging.PackageSpec(
            key="bare", zip_name="bare", title="t", kind="api", summary="s",
        )
        text = packaging.render_readme(spec)
        self.assertNotIn("## 怎么启动", text)
        self.assertNotIn("## 还要做什么", text)
        self.assertNotIn("## 说明", text)


class ValidateSpecsTestCase(unittest.TestCase):
    """``validate_specs()`` 是「各接口项目同级」在打包这一层的守门人。"""

    def test_the_real_manifest_is_valid(self):
        self.assertEqual(packaging.validate_specs(), [])

    def test_rejects_a_package_that_mixes_two_api_projects(self):
        bogus = packaging.PackageSpec(
            key="pixiv-api", zip_name="pixiv-api", title="t", kind="api", summary="s",
            dirs=("pixiv_bridge", "pixiv", "iwara"),
        )
        with patch.object(packaging, "PACKAGES", (bogus,)):
            problems = packaging.validate_specs()
        self.assertTrue(
            any("只能带一个接口项目" in problem for problem in problems), problems
        )

    def test_accepts_an_extension_package_without_its_host_directory(self):
        # 回归：扩展包本来就不带站点目录，不该被当成「宿主缺失」。
        bogus = packaging.PackageSpec(
            key="novel-extension", zip_name="novel-extension", title="t",
            kind="extension", summary="s", dirs=("novel",),
        )
        with patch.object(packaging, "PACKAGES", (bogus,)):
            problems = packaging.validate_specs()
        self.assertFalse(
            [problem for problem in problems if "挂在" in problem], problems
        )

    def test_rejects_a_bundle_whose_host_is_another_api_project(self):
        # iwara_web 挂在 iwara 上，放进 pixiv 的组合包里必须报出来。
        bogus = packaging.PackageSpec(
            key="pixiv-novel", zip_name="pixiv-novel", title="t", kind="bundle",
            summary="s", dirs=("pixiv_bridge", "pixiv", "iwara_web"),
        )
        with patch.object(packaging, "PACKAGES", (bogus,)):
            problems = packaging.validate_specs()
        self.assertTrue(
            any("挂在 iwara 上" in problem for problem in problems), problems
        )


class SiteRegistryTestCase(unittest.TestCase):
    """分享包覆盖面对齐 ``scripts/sites.py``（构建侧唯一事实来源）。"""

    def test_every_registered_site_ships_in_an_api_package(self):
        covered = {name for spec in packaging.PACKAGES for name in spec.site_names}
        expected = {site.directory for site in packaging.SITES}
        self.assertEqual(covered, expected)

    def test_every_registered_extension_ships_in_an_extension_package(self):
        covered = {
            name for spec in packaging.PACKAGES for name in spec.extension_names
        }
        self.assertEqual(covered, set(packaging.EXTENSIONS_BY_NAME))

    def test_api_packages_use_the_registry_entry_command(self):
        for spec in packaging.PACKAGES:
            if spec.kind == "extension":
                continue
            with self.subTest(key=spec.key):
                site = packaging.SITES_BY_NAME[spec.site_names[0]]
                self.assertIn(site.command(), "\n".join(spec.start_commands))

    def test_iwara_package_does_not_ship_pixiv_openssl_assets(self):
        # openssl-libs 是 Pixiv 的 ECH 直连用的，iwara 包不该带。
        self.assertNotIn("openssl-libs", packaging.PACKAGES_BY_KEY["iwara-api"].dirs)
        self.assertIn("openssl-libs", packaging.PACKAGES_BY_KEY["pixiv-api"].dirs)


class ZipRoundTripTestCase(unittest.TestCase):

    def setUp(self):
        self.dist = make_temp_dir("zip")
        self.dirs = [self.dist]
        self.patch = patch.object(packaging, "DIST_DIR", self.dist)
        self.patch.start()
        self.addCleanup(self.patch.stop)

    def tearDown(self):
        for path in self.dirs:
            drop_temp_dir(path)

    def test_write_zip_produces_one_root_folder_and_a_readme(self):
        spec = packaging.PACKAGES_BY_KEY["iwara_web-extension"]
        staged = packaging.stage_package(spec)
        target = packaging.write_zip(spec, staged)
        self.assertEqual(target, self.dist / (spec.zip_name + ".zip"))
        with zipfile.ZipFile(target) as archive:
            names = archive.namelist()
        self.assertTrue(all(name.startswith(spec.zip_name + "/") for name in names))
        self.assertIn(spec.zip_name + "/README.md", names)

    def test_inspect_zip_is_clean_for_a_real_package(self):
        spec = packaging.PACKAGES_BY_KEY["iwara_web-extension"]
        target = packaging.write_zip(spec, packaging.stage_package(spec))
        count, size, problems = packaging.inspect_zip(target)
        self.assertEqual(problems, [])
        self.assertGreater(count, 0)
        self.assertGreater(size, 0)

    def test_write_zip_overwrites_an_existing_archive(self):
        spec = packaging.PACKAGES_BY_KEY["iwara_web-extension"]
        staged = packaging.stage_package(spec)
        first = packaging.write_zip(spec, staged)
        (staged / "extra.txt").write_text("x", encoding="utf-8")
        second = packaging.write_zip(spec, staged)
        self.assertEqual(first, second)
        with zipfile.ZipFile(second) as archive:
            self.assertIn(spec.zip_name + "/extra.txt", archive.namelist())

    def make_zip(self, name, entries):
        target = self.dist / name
        with zipfile.ZipFile(target, "w", zipfile.ZIP_DEFLATED) as archive:
            for entry in entries:
                archive.writestr(entry, b"x")
        return target

    def test_inspect_zip_flags_bytecode_and_private_files(self):
        target = self.make_zip(
            "bad.zip",
            ["bad/README.md", "bad/mod.py", "bad/__pycache__/mod.pyc", "bad/.env", "bad/data/tokens.json"],
        )
        _, _, problems = packaging.inspect_zip(target)
        joined = "\n".join(problems)
        self.assertIn("字节码", joined)
        self.assertIn("私密数据", joined)
        self.assertIn("不该带的内容", joined)

    def test_inspect_zip_flags_a_missing_root_folder_and_readme(self):
        target = self.make_zip("flat.zip", ["mod.py"])
        _, _, problems = packaging.inspect_zip(target)
        joined = "\n".join(problems)
        self.assertIn("README.md", joined)
        self.assertIn("同名根文件夹", joined)


class MainCommandTestCase(unittest.TestCase):

    SMALL = ("novel-extension", "iwara_web-extension")

    def setUp(self):
        self.dist = make_temp_dir("main")
        self.dirs = [self.dist]
        self.patch = patch.object(packaging, "DIST_DIR", self.dist)
        self.patch.start()
        self.addCleanup(self.patch.stop)

    def tearDown(self):
        for path in self.dirs:
            drop_temp_dir(path)

    def run_main(self, argv):
        buffer = io.StringIO()
        with contextlib.redirect_stdout(buffer):
            code = packaging.main(argv)
        return code, buffer.getvalue()

    def test_list_returns_zero_and_names_every_key(self):
        code, output = self.run_main(["--list"])
        self.assertEqual(code, 0)
        for spec in packaging.PACKAGES:
            self.assertIn(spec.key, output)
            self.assertIn(spec.zip_name + ".zip", output)

    def test_unknown_key_returns_two(self):
        code, _ = self.run_main(["没有这个包"])
        self.assertEqual(code, 2)

    def test_check_does_not_write_a_zip(self):
        code, output = self.run_main([*self.SMALL, "--check"])
        self.assertEqual(code, 0)
        self.assertIn("内容就绪", output)
        self.assertEqual(list(self.dist.glob("*.zip")), [])

    def test_writes_the_selected_zips_and_cleans_the_stage(self):
        code, output = self.run_main(list(self.SMALL))
        self.assertEqual(code, 0)
        self.assertIn("分享包处理完成", output)
        for key in self.SMALL:
            spec = packaging.PACKAGES_BY_KEY[key]
            self.assertTrue((self.dist / (spec.zip_name + ".zip")).is_file(), key)
        self.assertFalse((self.dist / ".stage").exists())

    def test_reports_failures_from_a_dirty_package(self):
        """内容检查一旦报问题，退出码要是 1，且问题要打出来。"""
        with patch.object(packaging, "inspect_zip", return_value=(1, 1, ["带了字节码：bad/x.pyc"])):
            code, output = self.run_main(["iwara_web-extension"])
        self.assertEqual(code, 1)
        self.assertIn("内容检查未通过", output)
        self.assertIn("带了字节码：bad/x.pyc", output)


class PackageContentTestCase(unittest.TestCase):
    """对真实项目跑一遍内容检查：每个包的每个条目都要真的在盘上。"""

    def test_every_package_can_be_staged(self):
        dist = make_temp_dir("real")
        try:
            with patch.object(packaging, "DIST_DIR", dist):
                for spec in packaging.PACKAGES:
                    with self.subTest(key=spec.key):
                        target = packaging.stage_package(spec)
                        self.assertTrue((target / "README.md").is_file())
                        for name in packaging.DOC_FILES:
                            self.assertTrue((target / "手册" / name).is_file(), name)
                        shutil.rmtree(target, ignore_errors=True)
        finally:
            drop_temp_dir(dist)


if __name__ == "__main__":
    unittest.main()
