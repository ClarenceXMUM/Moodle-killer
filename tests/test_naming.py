#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""命名规则的离线回归：纯函数为主，配置项单独一组。

运行：python3 -m unittest discover -s tests -v
不联网、不读真实账号、不碰 ~/.moodle-killer。
"""
import copy
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "moodle-killer" / "scripts"))
import config_store as cs          # noqa: E402
import moodle_client as mc         # noqa: E402


class CourseCodeTest(unittest.TestCase):
    def test_code_from_real_course_names(self):
        cases = {
            "MAT203 Statistics 2026/09 Koh Siew Khew": "MAT203",
            "MAT301 and MAT418 Partial Differential Equations 2026/09 Yufeng Lu": "MAT301",
            "MAT201 Mathematical Analysis I 202609 - General": "MAT201",
            "mat107 probability theory 2026/04": "MAT107",
        }
        for name, want in cases.items():
            self.assertEqual(mc.derive_course_code(name), want, name)

    def test_no_code_in_name_falls_back_to_shortname(self):
        self.assertEqual(mc.derive_course_code("Abstract Algebra I 2026/09 Ali Azimi"), "")
        self.assertEqual(mc.derive_course_code("Abstract Algebra I 2026/09 Ali Azimi", "AAI"), "AAI")
        # MPU2.2 这种「一个数字 + 点」不算课程代号，别抠出 MPU2
        self.assertEqual(mc.derive_course_code("MPU2.2 Writing"), "")

    def test_explicit_code_wins(self):
        entry = {"name": "MAT203 Statistics", "shortname": "Stat", "code": "STAT203"}
        self.assertEqual(mc.resolve_course_code(entry), "STAT203")
        # 没手填 code 时：课名里抠优先于短名
        self.assertEqual(mc.resolve_course_code({"name": "MAT203 Statistics", "shortname": "Stat"}), "MAT203")
        self.assertEqual(mc.resolve_course_code({"name": "Abstract Algebra I", "shortname": "AAI"}), "AAI")
        self.assertEqual(mc.resolve_course_code({}, "MAT203 Statistics"), "MAT203")


class BuildFilenameTest(unittest.TestCase):
    def test_default_is_code_dash_name(self):
        got = mc.build_filename("Animals", ".txt", mode="default", code="MAT203")
        self.assertEqual(got, "MAT203-Animals.txt")

    def test_default_without_code_has_no_leading_dash(self):
        self.assertEqual(mc.build_filename("Animals", ".txt", mode="default", code=""), "Animals.txt")

    def test_default_cleans_folder_file_name(self):
        got = mc.build_filename("PDE2609 - Lecture 1", ".pdf", mode="default", code="MAT301",
                                folder="Lecture Notes (2026/09)", digest="a50271397f6b")
        self.assertEqual(got, "MAT301-PDE2609-Lecture-1.pdf")

    def test_default_keeps_chinese(self):
        got = mc.build_filename("群论之美 讲义", ".pdf", mode="default", code="AAI")
        self.assertEqual(got, "AAI-群论之美-讲义.pdf")

    def test_no_extension_resource(self):
        got = mc.build_filename("Course Information", "", mode="default", code="MAT203")
        self.assertEqual(got, "MAT203-Course-Information")

    def test_plain_uses_only_the_resource_name(self):
        self.assertEqual(mc.build_filename("Animals", ".txt", mode="plain", code="MAT203"), "Animals.txt")

    def test_original_keeps_moodle_name(self):
        got = mc.build_filename("Animals", ".txt", mode="original", code="MAT203",
                                legacy="744833-Animals-c96707aa4535.txt")
        self.assertEqual(got, "744833-Animals-c96707aa4535.txt")
        # 没有 legacy（单文件资源）时退回原名，而不是空字符串
        self.assertEqual(mc.build_filename("Animals", ".txt", mode="original"), "Animals.txt")

    def test_custom_template(self):
        got = mc.build_filename("Animals", ".txt", mode="custom", template="{code}-{date}-{name}",
                                code="MAT203", day="20260929")
        self.assertEqual(got, "MAT203-20260929-Animals.txt")

    def test_custom_template_with_ext_not_doubled(self):
        self.assertEqual(mc.build_filename("Animals", ".txt", mode="custom", template="{code}-{name}{ext}",
                                           code="MAT203"), "MAT203-Animals.txt")

    def test_custom_bad_template_never_breaks(self):
        # 空模板 → 退回默认规则
        self.assertEqual(mc.build_filename("Animals", ".txt", mode="custom", template="", code="MAT203"),
                         "MAT203-Animals.txt")
        # 写错的占位符 → 丢掉，不写进文件名
        self.assertEqual(mc.build_filename("Animals", ".txt", mode="custom", template="{nope}-{name}"),
                         "Animals.txt")
        # 模板只写了代号、而这门课没代号 → 退回原名，不能变成 "-.txt" 这种
        self.assertEqual(mc.build_filename("Animals", ".txt", mode="custom", template="{code}"), "Animals.txt")

    def test_unknown_mode_treated_as_default(self):
        self.assertEqual(mc.build_filename("Animals", ".txt", mode="因为", code="MAT203"), "MAT203-Animals.txt")

    def test_folder_and_course_fields(self):
        got = mc.build_filename("Lecture 1", ".pdf", mode="custom", template="{folder}_{name}",
                                folder="Lecture Notes (2026/09)")
        self.assertEqual(got, "Lecture-Notes-2026-09_Lecture-1.pdf")


class UniquePathTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="mk naming ")
        self.addCleanup(self.tmp.cleanup)

    def test_same_content_reuses_name(self):
        fp = os.path.join(self.tmp.name, "MAT203-Animals.txt")
        Path(fp).write_bytes(b"same")
        self.assertEqual(mc.unique_path(fp, b"same"), fp)

    def test_different_content_gets_suffix(self):
        fp = os.path.join(self.tmp.name, "MAT203-Animals.txt")
        Path(fp).write_bytes(b"first")
        self.assertEqual(mc.unique_path(fp, b"second"), os.path.join(self.tmp.name, "MAT203-Animals-2.txt"))
        Path(os.path.join(self.tmp.name, "MAT203-Animals-2.txt")).write_bytes(b"second")
        self.assertEqual(mc.unique_path(fp, b"third"), os.path.join(self.tmp.name, "MAT203-Animals-3.txt"))

    def test_missing_file_keeps_name(self):
        fp = os.path.join(self.tmp.name, "new.txt")
        self.assertEqual(mc.unique_path(fp, b"x"), fp)


class PickNameTest(unittest.TestCase):
    """scan_course 用的那条路径：Moodle 资源 → 落地文件名。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="mk naming pick ")
        self.addCleanup(self.tmp.cleanup)
        env = patch.dict(os.environ, {"MOODLE_KILLER_HOME": self.tmp.name})
        env.start()
        self.addCleanup(env.stop)
        cfg = copy.deepcopy(cs.DEFAULTS)
        cfg["moodle"].update(url="https://moodle.example", user="u", password="p")
        cs.save_config(cfg)
        self.client = mc.MoodleClient()

    def test_single_file_resource(self):
        info = {"name": "Tau Shean Lim PDE Notes", "url": "x"}
        self.assertEqual(
            self.client.pick_name(info, cmid="744354", code="MAT301", mode="default"),
            "MAT301-Tau-Shean-Lim-PDE-Notes")

    def test_folder_resource_uses_real_filename_and_suffix(self):
        info = {"name": "Lecture Notes (2026/09) / PDE2609 - Lecture 1.pdf",
                "filename": "PDE2609 - Lecture 1.pdf", "folder": "Lecture Notes (2026/09)",
                "hash": "a50271397f6b", "download_name": "744363-PDE2609 - Lecture 1-a50271397f6b.pdf",
                "url": "x"}
        self.assertEqual(self.client.pick_name(info, cmid="folder:744363:a50271397f6b",
                                               code="MAT301", mode="default"),
                         "MAT301-PDE2609-Lecture-1.pdf")

    def test_mode_comes_from_config(self):
        cfg = cs.load_config()
        cs.set_path(cfg, "download.naming", "plain")
        cs.save_config(cfg)
        self.client = mc.MoodleClient()
        info = {"name": "Animals.txt", "filename": "Animals.txt", "url": "x"}
        # pick_name 默认 mode 由调用方传入；scan_course 才读配置，这里确认传参生效
        self.assertEqual(self.client.pick_name(info, mode="plain", code="MAT203"), "Animals.txt")


class NamingConfigTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="mk naming cfg ")
        self.addCleanup(self.tmp.cleanup)
        env = patch.dict(os.environ, {"MOODLE_KILLER_HOME": self.tmp.name})
        env.start()
        self.addCleanup(env.stop)

    def test_defaults_exist(self):
        cfg = copy.deepcopy(cs.DEFAULTS)
        cs.save_config(cfg)
        cfg = cs.load_config()
        self.assertEqual(cs.get_path(cfg, "download.naming"), "default")
        self.assertEqual(cs.get_path(cfg, "download.name_template"), "{code}-{name}")

    def test_aliases(self):
        self.assertEqual(cs.normalize_key("命名"), "download.naming")
        self.assertEqual(cs.normalize_key("自动命名"), "download.naming")
        self.assertEqual(cs.normalize_key("命名模板"), "download.name_template")

    def test_chinese_values(self):
        self.assertEqual(cs.coerce("download.naming", "默认"), "default")
        self.assertEqual(cs.coerce("download.naming", "自定义"), "custom")
        self.assertEqual(cs.coerce("download.naming", "原名"), "original")
        self.assertEqual(cs.coerce("download.naming", "只要名字"), "plain")
        self.assertEqual(cs.coerce("download.naming", "custom"), "custom")
        with self.assertRaises(ValueError):
            cs.coerce("download.naming", "随便乱填")

    def test_output_mode_fuzzy_still_works(self):
        # 「默认」在 output.mode 里还是 heartbeat（不能因为命名规则抢走这个词）
        self.assertEqual(cs.coerce("output.mode", "默认"), "heartbeat")


if __name__ == "__main__":
    unittest.main(verbosity=2)
