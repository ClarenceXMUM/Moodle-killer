#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""离线回归：真实临时数据目录，HTTP / 系统通知 / 安装器外部命令用替身。

运行：python3 -m unittest discover -s tests -v
不读取真实账号，不安装定时器，不发送外部消息。
"""
import base64
import contextlib
import copy
import io
import json
import os
import shlex
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
import threading
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "moodle-killer" / "scripts"))
import config_store as cs
import mk
import moodle_client as mc
import moodle_prep as mp
import platform_support as ps
import sender
import verify


class IsolatedTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="mk regression ")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        env = patch.dict(os.environ, {"MOODLE_KILLER_HOME": str(self.root),
                                      "MOODLE_KILLER_SANDBOX": "1",
                                      "MOODLE_KILLER_CHANNEL": "",
                                      "MOODLE_KILLER_DELIVERED_BY_AGENT": ""})
        env.start()
        self.addCleanup(env.stop)
        self.cfg = copy.deepcopy(cs.DEFAULTS)
        self.cfg["moodle"].update(url="https://moodle.example", user="student", password="fixture")
        self.cfg["delivery"]["channel"] = "local"
        cs.save_config(self.cfg)
        cs.save_courses({"math": {"id": 7, "name": "数学", "path": str(self.root / "files")}})


class PipelineTests(IsolatedTest):
    def run_pipeline(self, argv=(), items=None, notes=None, missing=None, sent=True):
        cs.save_config(self.cfg)
        scanner = Mock()
        scanner.login.return_value = True
        scanner.state_dir = str(cs.state_dir())
        meta = {"scanned": 1, "notifications": 0, "notes": notes or [], "missing": missing or []}
        stdout, stderr = io.StringIO(), io.StringIO()
        with patch.object(mp, "MoodleClient", return_value=scanner), \
                patch.object(mp, "collect", return_value=(items or [], [], meta)) as collect, \
                patch.object(sender, "send_text", return_value=(sent, "已发送" if sent else "网络失败")) as send, \
                contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            code = mp.main(list(argv))
        return code, send, collect, stdout.getvalue(), stderr.getvalue()

    def test_heartbeat_is_sent_without_signals(self):
        code, send, _, _, _ = self.run_pipeline()
        self.assertEqual(code, 0)
        self.assertIn("无新内容", send.call_args.args[0])
        self.assertEqual(send.call_count, 1)

    def test_heartbeat_summarizes_hidden_files_and_keeps_full_local_list(self):
        items = [{"bucket": "DOWNLOAD", "text": "[新文件] 数学: %d.pdf 已下载" % i}
                 for i in range(8)]
        code, send, _, _, _ = self.run_pipeline(items=items)
        self.assertEqual(code, 0)
        self.assertIn("另4条见 %s" % (cs.out_dir() / "all_signals.txt"), send.call_args.args[0])
        self.assertEqual(len((cs.out_dir() / "all_signals.txt").read_text().splitlines()), 8)

    def test_silent_and_urgent_empty_are_not_sent(self):
        for mode in ("silent", "urgent", "full"):
            with self.subTest(mode=mode):
                self.cfg["output"]["mode"] = mode
                code, send, _, _, _ = self.run_pipeline()
                self.assertEqual(code, 0)
                send.assert_not_called()

    def test_silent_signal_is_sent(self):
        self.cfg["output"]["mode"] = "silent"
        item = {"bucket": "GRADE", "text": "[成绩] 数学: 85"}
        code, send, _, _, _ = self.run_pipeline(items=[item])
        self.assertEqual(code, 0)
        self.assertEqual(send.call_args.args[0], item["text"])

    def test_dry_run_preserves_state_and_last_run(self):
        state = cs.state_path(7)
        state.write_text('{"resources": {"1": "旧文件"}}', encoding="utf-8")
        # 真跑留下的产出记录也不能被试跑覆盖（否则一次测试就把上一次的结果抹了）
        artifacts = ["last_run.json", "signals.txt", "all_signals.txt",
                     "verify_report.txt", "unclassified_moodle.json"]
        for name in artifacts:
            (cs.out_dir() / name).write_text("previous " + name, encoding="utf-8")
        original = state.read_bytes()
        code, send, collect, _, _ = self.run_pipeline(["--dry-run"])
        self.assertEqual(code, 0)
        send.assert_not_called()
        self.assertFalse(collect.call_args.kwargs["do_download"])
        self.assertEqual(state.read_bytes(), original)
        for name in artifacts:
            self.assertEqual((cs.out_dir() / name).read_text(encoding="utf-8"), "previous " + name,
                             "试跑覆盖了正常扫描的 %s" % name)
        self.assertTrue((cs.out_dir() / ".dryrun" / "signals.txt").exists())

    def test_none_and_pause_suppress_delivery_but_still_scan(self):
        for channel, paused in (("none", False), ("local", True)):
            self.cfg["delivery"]["channel"] = channel
            self.cfg["advanced"]["paused"] = paused
            code, send, collect, _, _ = self.run_pipeline()
            self.assertEqual(code, 0)
            send.assert_not_called()
            collect.assert_called_once()

    def test_send_failure_is_visible_in_json_and_run_status(self):
        code, send, _, output, stderr = self.run_pipeline(["--json"], sent=False)
        self.assertEqual(code, 1)
        send.assert_called_once()
        self.assertEqual(json.loads(output)["status"], "send_failed")
        self.assertIn("❌", stderr)
        self.assertEqual(json.loads((cs.out_dir() / "last_run.json").read_text())["status"], "send_failed")

    def test_scan_failure_is_not_reported_as_no_updates(self):
        code, send, _, _, stderr = self.run_pipeline(missing=["数学"])
        self.assertEqual(code, 2)
        self.assertIn("❌", stderr)
        send.assert_not_called()

    def test_channel_test_does_not_change_config(self):
        before = cs.config_path().read_bytes()
        output = io.StringIO()
        with patch.object(sender, "send_text", return_value=(True, "已发送")) as send, \
                contextlib.redirect_stdout(output):
            self.assertEqual(mk.main(["channel", "test", "--json"]), 0)
        self.assertIn("通道测试", send.call_args.args[0])
        self.assertTrue(json.loads(output.getvalue())["ok"])
        self.assertEqual(cs.config_path().read_bytes(), before)

    def test_cli_send_failure_returns_nonzero(self):
        with patch.object(sender, "send_text", return_value=(False, "失败")), \
                contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(mk.main(["channel", "test"]), 1)

    def test_agent_channels_never_directly_send(self):
        with patch.object(sender, "_post_json") as post, patch.object(ps, "notify") as notify:
            for channel in ("hermes", "whatsapp"):
                self.assertTrue(sender.send_text("测试", channel=channel, cfg=self.cfg)[0])
            post.assert_not_called()
            notify.assert_not_called()

    def test_auto_only_hands_off_for_current_agent_run(self):
        self.cfg["delivery"]["channel"] = "auto"
        with patch("sender.os.path.exists", return_value=True):
            self.assertEqual(sender.detect_channel(self.cfg)[0], "local")
        with patch.dict(os.environ, {"MOODLE_KILLER_DELIVERED_BY_AGENT": "1"}):
            self.assertEqual(sender.detect_channel(self.cfg)[0], "hermes")

    def test_local_failure_retains_log_and_reports_error(self):
        with patch.object(ps, "notify", return_value=False):
            ok, message = sender.send_text("测试", cfg=self.cfg)
        self.assertFalse(ok)
        self.assertIn("通知失败", message)
        self.assertIn("测试", (cs.out_dir() / "sent.log").read_text())


def response(url, text="", content=None, content_type="text/html"):
    result = Mock()
    result.url, result.text = url, text
    result.content = text.encode() if content is None else content
    result.headers = {"Content-Type": content_type}
    return result


class CompletionTests(IsolatedTest):
    """勾 Done 的安全闸门：只勾不取消、试跑不写、上限拦、回读熔断、绝不发 false。"""

    def make_scanner(self, targets, states=None, state_existed=True,
                     write_result=(True, "已勾完成"), readback=None):
        s = Mock()
        s.check_notifications.return_value = []
        s.scan_course.return_value = {"name": "数学", "new_files": ["a.pdf"], "assignments": {},
                                      "notes": [], "completion_targets": targets,
                                      "state_existed": state_existed, "course_id": 7}
        s.set_activity_completion.return_value = write_result
        seq = [(states or {}, {})]
        if readback is not None:
            seq.append((readback, {}))
        seq.append((readback if readback is not None else (states or {}), {}))
        s.get_course_completion.side_effect = seq
        return s

    def run_collect(self, scanner, do_download=True, cap=15):
        cfg = cs.load_config()
        cfg["completion"] = {"mark_done": True, "max_marks": cap}
        cs.save_config(cfg)
        courses = {"math": {"id": 7, "name": "数学", "path": str(self.root / "files")}}
        with patch("time.sleep"), contextlib.redirect_stdout(io.StringIO()):
            _items, _unc, meta = mp.collect(scanner, courses, cfg, do_download=do_download)
        return meta["completion"]

    def test_dry_run_never_sends_any_write(self):
        s = self.make_scanner({1: "a.pdf"}, states={1: False})
        out = self.run_collect(s, do_download=False)
        s.set_activity_completion.assert_not_called()
        self.assertFalse(out["enabled"])

    def test_switch_off_sends_nothing(self):
        # 文档里把 completion.mark_done=false 当逃生口，得证明它真的什么都不做
        s = self.make_scanner({1: "a.pdf"}, states={1: False})
        cfg = cs.load_config()
        cfg["completion"] = {"mark_done": False, "max_marks": 15}
        courses = {"math": {"id": 7, "name": "数学", "path": str(self.root / "files")}}
        with contextlib.redirect_stdout(io.StringIO()):
            _i, _u, meta = mp.collect(s, courses, cfg, do_download=True)
        s.set_activity_completion.assert_not_called()
        self.assertFalse(meta["completion"]["enabled"])

    def test_already_done_is_skipped_without_request(self):
        s = self.make_scanner({1: "a.pdf"}, states={1: True})
        out = self.run_collect(s)
        s.set_activity_completion.assert_not_called()   # 幂等：本来就完成，不发请求
        self.assertEqual(out["skipped"], 1)
        self.assertEqual(out["marked"], [])

    def test_not_done_is_marked_then_read_back(self):
        s = self.make_scanner({1: "a.pdf"}, states={1: False}, readback={1: True})
        out = self.run_collect(s)
        s.set_activity_completion.assert_called_once_with(1)
        self.assertEqual([m["cmid"] for m in out["marked"]], [1])

    def test_status_false_is_skip_not_failure(self):
        # 活动不支持手动勾（status=False 但无 error）→ 跳过 + 提示，绝不当作写失败熔断
        s = self.make_scanner({1: "a.pdf"}, states={1: False},
                              write_result=(False, "not_toggleable（status=False）"))
        out = self.run_collect(s)
        self.assertEqual(out["failed"], [])
        self.assertEqual(out["skipped"], 1)

    def test_readback_mismatch_aborts_the_rest(self):
        s = self.make_scanner({1: "a.pdf", 2: "b.pdf"}, states={1: False, 2: False},
                              readback={1: False, 2: False})
        out = self.run_collect(s)
        self.assertTrue(out["aborted"])
        self.assertEqual(s.set_activity_completion.call_count, 1)   # 熔断：第二条没发
        self.assertEqual(out["failed"], ["a.pdf（回读不符）"])

    def test_missing_state_file_refuses_to_write(self):
        s = self.make_scanner({1: "a.pdf"}, states={1: False}, state_existed=False)
        out = self.run_collect(s)
        s.set_activity_completion.assert_not_called()

    def test_over_cap_writes_nothing_at_all(self):
        s = self.make_scanner({1: "a", 2: "b", 3: "c"}, states={1: False, 2: False, 3: False})
        out = self.run_collect(s, cap=1)
        s.set_activity_completion.assert_not_called()               # 全有或全无
        self.assertEqual(out["refused_over_cap"], 3)

    def test_client_refuses_to_unmark(self):
        with self.assertRaises(ValueError):
            mc.MoodleClient().set_activity_completion(1, completed=False)

    def test_negative_control_error_is_reported_as_failure(self):
        client = mc.MoodleClient()
        client.session = Mock()
        client.session.get.return_value = response("https://moodle.example/my/", '"sesskey":"k"')
        client.session.post.return_value = Mock(status_code=200, json=Mock(return_value=[
            {"error": True, "exception": {"errorcode": "invalidcoursemodule", "message": "课程模块 ID 无效"}}]))
        ok, detail = client.set_activity_completion(99999999)
        self.assertFalse(ok)
        self.assertIn("invalidcoursemodule", detail)

    def test_client_treats_status_false_as_not_toggleable(self):
        client = mc.MoodleClient()
        client.session = Mock()
        client.session.get.return_value = response("https://moodle.example/my/", '"sesskey":"k"')
        client.session.post.return_value = Mock(status_code=200, json=Mock(return_value=[
            {"error": False, "data": {"status": False, "warnings": []}}]))
        ok, detail = client.set_activity_completion(743561)
        self.assertFalse(ok)
        self.assertIn("not_toggleable", detail)

    def test_client_success_requires_status_and_no_warnings(self):
        client = mc.MoodleClient()
        client.session = Mock()
        client.session.get.return_value = response("https://moodle.example/my/", '"sesskey":"k"')
        client.session.post.return_value = Mock(status_code=200, json=Mock(return_value=[
            {"error": False, "data": {"status": True, "warnings": []}}]))
        ok, _ = client.set_activity_completion(743561)
        self.assertTrue(ok)

    # ── 报告渲染（评审指出：这层原先是零覆盖，所以「无事静默」才漏了出去）──
    def run_main_with_meta(self, completion, argv=()):
        cs.save_config(self.cfg)
        scanner = Mock()
        scanner.login.return_value = True
        scanner.state_dir = str(cs.state_dir())
        meta = {"scanned": 1, "notifications": 0, "notes": [], "missing": [],
                "completion": completion}
        with patch.object(mp, "MoodleClient", return_value=scanner), \
                patch.object(mp, "collect", return_value=([], [], meta)), \
                patch.object(sender, "send_text", return_value=(True, "已发送")), \
                contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            code = mp.main(list(argv))
        return code, (cs.out_dir() / "verify_report.txt").read_text(encoding="utf-8")

    def test_report_always_mentions_completion_even_with_nothing_to_mark(self):
        # 无事也必须显式出现，否则分不清「功能关了 / 坏了 / 真的没新文件」
        code, report = self.run_main_with_meta(
            {"enabled": True, "candidates": 0, "marked": [], "skipped": 0, "failed": []})
        self.assertEqual(code, 0)
        self.assertIn("完成度: 本轮没有新下的课件", report)

    def test_refused_completion_is_not_reported_as_no_new_files(self):
        """被拒绝勾选时不能说成「没有新课件」——那分不清「功能没生效」和「被拒」。"""
        _code, report = self.run_main_with_meta(
            {"enabled": True, "candidates": 0, "marked": [], "skipped": 0, "failed": [],
             "warnings": ["跳过 某课：该课扫描状态文件不存在，本轮文件全算新，拒绝批量勾"]})
        self.assertIn("完成度: 本轮未动完成度（已勾 0 ｜拒绝或跳过，原因见下）", report)
        self.assertNotIn("本轮没有新下的课件", report)

    def test_report_says_so_when_the_feature_is_switched_off(self):
        # 关了也要说一声，否则运维时分不清「关了」和「坏了」
        _code, report = self.run_main_with_meta({"enabled": False})
        self.assertIn("完成度: 功能已关闭", report)

    def test_report_uses_per_course_counts_not_the_global_one(self):
        comp = {"enabled": True, "candidates": 3, "marked": [], "skipped": 3, "failed": [],
                "snapshots": [
                    {"course": "A", "course_id": 1, "tracked": 2, "before_done": 1,
                     "after_done": 2, "skipped": 1, "marked": 1},
                    {"course": "B", "course_id": 2, "tracked": 2, "before_done": 2,
                     "after_done": 2, "skipped": 2, "marked": 0}]}
        _code, report = self.run_main_with_meta(comp)
        lines = [ln for ln in report.splitlines() if ln.startswith("完成度:")]
        self.assertEqual(len(lines), 2)
        self.assertIn("已勾 1 ｜跳过 1", lines[0])
        self.assertIn("已勾 0 ｜跳过 2", lines[1])      # 不是全局那个 3
        self.assertIn("50% → 100%", lines[0])


class MoodleTests(IsolatedTest):
    def setUp(self):
        super().setUp()
        self.client = mc.MoodleClient()
        self.client.session = Mock()

    def activities(self, extra=""):
        page = response("https://moodle.example/course/view.php?id=7", """
<a href='/mod/resource/view.php?id=8'><span>单文件</span></a>
<a href='/mod/folder/view.php?id=9&amp;redirect=1'><span>讲义</span></a>
<a href='/mod/assign/view.php?id=10'>作业</a>
""")
        folder = response("https://moodle.example/mod/folder/view.php?id=9", """
<a href='/pluginfile.php/55/mod_folder/content/0/week1/slides.pdf?forcedownload=1'><span>slides.pdf</span></a>
<a href='/pluginfile.php/55/mod_folder/content/0/week1/slides.pdf?forcedownload=0'>重复链接</a>
<a href='/pluginfile.php/55/mod_folder/content/0/week2/slides.pdf'>slides.pdf</a>
<a href='/pluginfile.php/55/user/icon/f1'>头像</a>
""" + extra)
        self.client.session.get.side_effect = [page, folder]
        return self.client.get_course_activities(7)

    def test_folder_nested_links_and_backward_compatible_resource_keys(self):
        acts = self.activities()
        self.assertEqual(len(acts["resources"]), 3)
        self.assertEqual(acts["resources"]["8"]["name"], "单文件")
        self.assertIn("10", acts["assignments"])
        files = [v for k, v in acts["resources"].items() if k.startswith("folder:")]
        self.assertNotEqual(files[0]["download_name"], files[1]["download_name"])

    def test_screen_reader_type_label_is_not_part_of_name(self):
        # Moodle 把类型标签放在 class="accesshide" 里（给读屏软件用），不能混进文件名。
        self.client.session.get.return_value = response(
            "https://moodle.example/course/view.php?id=7",
            '<a href="/mod/resource/view.php?id=8"><span class="instancename">Course Information '
            '<span class="accesshide "> 文件</span></span></a>')
        acts = self.client.get_course_activities(7)
        self.assertEqual(acts["resources"]["8"]["name"], "Course Information")

    def test_discover_courses_beats_the_ten_course_page_cap(self):
        # /my/ 只列 10 门；第 11 门必须靠 Moodle 接口补回来，否则新学期加课会漏。
        page = "".join('<a href="/course/view.php?id=%d">Course %d</a>' % (i, i) for i in range(1, 11))
        page += '"sesskey":"testkey"'
        self.client.session.get.return_value = response("https://moodle.example/my/", page)
        self.client.session.post.return_value = Mock(json=Mock(return_value=[{
            "error": False, "data": {"courses": [
                {"id": 99, "fullname": "MAT203 Statistics 2026/09"},
                {"id": 1, "fullname": "Course 1"},          # 与页面重复，不该出现两次
            ]}}]))
        got = self.client.discover_courses()
        ids = [c["id"] for c in got]
        self.assertEqual(sorted(ids), list(range(1, 11)) + [99])
        self.assertEqual(len(ids), len(set(ids)))
        self.assertIn("MAT203", [c["name"] for c in got if c["id"] == 99][0])

    def test_discover_courses_survives_missing_sesskey(self):
        self.client.session.get.return_value = response(
            "https://moodle.example/my/", '<a href="/course/view.php?id=7">数学</a>')
        self.client.session.post.side_effect = AssertionError("没有 sesskey 时不该发接口请求")
        self.assertEqual([c["id"] for c in self.client.discover_courses()], [7])

    def test_api_course_names_are_html_unescaped(self):
        # Moodle 的 timeline 接口把课名转义过（`&amp;`）——带着实体进 courses.json / 推送 /
        # 自动生成的下载目录名都很难看，也不好搜。
        self.client.session.get.return_value = response(
            "https://moodle.example/my/", '"sesskey":"testkey"')
        self.client.session.post.return_value = Mock(json=Mock(return_value=[{
            "error": False, "data": {"courses": [
                {"id": 13303, "fullname": "MPU1022/MPU3322 Integrity &amp; Anti-Corruption 2026/09 Dr Loh",
                 "shortname": "Corruption 2026/09"},
            ]}}]))
        got = self.client.discover_courses()
        self.assertEqual(got[0]["name"],
                         "MPU1022/MPU3322 Integrity & Anti-Corruption 2026/09 Dr Loh")

    def test_every_activity_type_is_parsed_not_just_the_five(self):
        # 只认 resource/folder/assign/url/quiz/forum 会整块丢掉老师加的网页/图书/链接
        # —— 那就是「Moodle 上明明有，它从没提过」的根因。
        self.client.session.get.return_value = response(
            "https://moodle.example/course/view.php?id=7",
            '<a href="/mod/page/view.php?id=21">Share Links</a>'
            '<a href="/mod/book/view.php?id=22">课程读本</a>'
            '<a href="/mod/label/view.php?id=23">本章说明</a>'
            '<a href="/mod/lti/view.php?id=24">在线课堂</a>'
            '<a href="/mod/resource/view.php?id=25">讲义</a>')
        acts = self.client.get_course_activities(7)
        got = {i: v["type"] for i, v in acts["activities"].items()}
        self.assertEqual(got, {"21": "page", "22": "book", "23": "label", "24": "lti", "25": "resource"})

    def test_first_scan_only_seeds_the_activity_baseline(self):
        # 升级前的老 state 没有 activities 键：第一次只写基线，绝不把历史活动翻出来报一遍。
        acts = self.activities()
        new_files, _assigns, new_acts = self.client.diff_new_files(7, acts)
        self.assertEqual(new_acts, {})
        state = json.loads((cs.state_dir() / "course_7.json").read_text())
        self.assertIn("activities", state)
        self.assertIn("10", state["activities"])          # 作业也进基线
        # 第二次带一个新增网页活动 → 只有它算「新」
        acts["activities"]["21"] = {"name": "Share Links", "type": "page"}
        _f, _a, new_acts = self.client.diff_new_files(7, acts)
        self.assertEqual(list(new_acts), ["21"])

    def test_new_activities_never_double_report_files_or_assignments(self):
        acts = self.activities()
        self.client.diff_new_files(7, acts)                     # 建基线
        acts["activities"]["30"] = {"name": "第 2 次作业", "type": "assign"}
        acts["activities"]["31"] = {"name": "新讲义.pdf", "type": "resource"}
        acts["activities"]["32"] = {"name": "资料夹二", "type": "folder"}
        acts["activities"]["33"] = {"name": "小测验", "type": "quiz"}
        _f, _a, new_acts = self.client.diff_new_files(7, acts)
        self.assertEqual(list(new_acts), ["33"])                # 文件/作业各有出口，别重复报

    def test_pipeline_reports_new_non_file_activity(self):
        # 端到端：课上新出现的「网页」必须出信号，标签是人话（不是 mod/page 这种）。
        self.client.session.get.return_value = response(
            "https://moodle.example/course/view.php?id=7", "")
        cs.save_courses({"math": {"id": 7, "name": "数学", "path": str(self.root / "files")}})
        state = cs.state_dir() / "course_7.json"
        state.parent.mkdir(parents=True, exist_ok=True)
        state.write_text(json.dumps({"last_scan": "2026-09-01 08:00", "resources": {},
                                     "urls": {}, "activities": {}}), encoding="utf-8")
        scanner = Mock()
        scanner.login.return_value = True
        scanner.state_dir = str(cs.state_dir())
        scanner.check_notifications.return_value = []
        scanner.scan_course.return_value = {"name": "数学", "new_files": [], "assignments": {},
                                            "new_activities": {"21": {"name": "Share Links", "type": "page"}},
                                            "notes": [], "completion_targets": {},
                                            "state_existed": True, "course_id": 7}
        cs.save_config(self.cfg)
        out = io.StringIO()
        with patch.object(mp, "MoodleClient", return_value=scanner), \
                patch.object(sender, "send_text", return_value=(True, "已发送")), \
                contextlib.redirect_stdout(out), contextlib.redirect_stderr(io.StringIO()):
            code = mp.main([])
        self.assertEqual(code, 0)
        self.assertIn("[新活动] 数学: 网页 Share Links", out.getvalue())

    def test_folder_diff_sees_later_additions(self):
        first = self.activities()
        self.assertEqual(len(self.client.diff_new_files(7, first)[0]), 3)
        self.assertFalse(self.client.diff_new_files(7, self.activities())[0])
        updated = self.activities("<a href='/pluginfile.php/55/mod_folder/content/0/new.txt'>new.txt</a>")
        new, _, _ = self.client.diff_new_files(7, updated)
        self.assertEqual(len(new), 1)
        self.assertIn("new.txt", next(iter(new.values()))["name"])

    def test_legacy_windows_state_keeps_seen_files(self):
        old = {"resources": {"8": "讲义"}}
        cs.state_path(7).write_bytes(json.dumps(old, ensure_ascii=False).encode("cp936"))
        acts = {"resources": {"8": {"name": "讲义", "url": "https://moodle.example/file"}}}
        with patch.object(mc.locale, "getpreferredencoding", return_value="cp936"):
            self.assertFalse(self.client.diff_new_files(7, acts)[0])
        self.assertEqual(json.loads(cs.state_path(7).read_text(encoding="utf-8"))["resources"], old["resources"])

    def test_small_text_and_office_files_keep_extensions(self):
        for name, content, kind in (("notes.txt", b"hi", "text/plain"),
                                    ("lecture.docx", b"PK\x03\x04test", "application/octet-stream"),
                                    ("slides.pdf", b"%PDF-test", "application/pdf")):
            url = "https://moodle.example/pluginfile.php/1/mod_folder/content/0/" + name
            self.client.session.get.return_value = response(url, content=content, content_type=kind)
            with contextlib.redirect_stdout(io.StringIO()):
                filename, ok = self.client.download_file(url, name, str(self.root / "files"))
            self.assertTrue(ok)
            self.assertEqual(filename, name)
            self.assertEqual((self.root / "files" / filename).read_bytes(), content)

    def test_login_page_is_not_saved_as_file(self):
        self.client.session.get.return_value = response("https://moodle.example/login/index.php", "<html>Login</html>")
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertFalse(self.client.download_file("https://moodle.example/file", "notes", str(self.root / "files"))[1])
        self.assertEqual(list((self.root / "files").iterdir()), [])

    def test_failed_download_is_retried_on_next_scan(self):
        acts = {"resources": {"8": {"name": "slides", "url": "https://moodle.example/file"}}}
        with patch.object(self.client, "get_course_activities", return_value=acts), \
                patch.object(self.client, "download_file", return_value=(None, False)), \
                contextlib.redirect_stdout(io.StringIO()):
            result = self.client.scan_course(7, "数学")
        self.assertTrue(result["notes"])
        self.assertIn("8", self.client.diff_new_files(7, acts)[0])

    def test_chinese_due_extracted_from_table(self):
        self.client.session.get.return_value = response("https://moodle.example/mod/assign/view.php?id=1",
            "<table><tr><th>截止日期</th><td>2026年 09月 28日, 23:59</td></tr></table>")
        details = self.client.check_assignment_details("https://moodle.example/mod/assign/view.php?id=1")
        self.assertEqual(mp.parse_due(details["due"]), datetime(2026, 9, 28, 23, 59))

    def test_due_formats_and_invalid_dates(self):
        for value in ("2026年9月28日 23:59", "2026年 09月 28日, 23:59",
                      "截止：2026/09/28 23:59", "2026年9月28日 星期一 下午11:59",
                      "Monday, 28 September 2026, 11:59 PM", "2026-09-28T23:59"):
            with self.subTest(value=value):
                self.assertEqual(mp.parse_due(value), datetime(2026, 9, 28, 23, 59))
        for value in ("2026年2月30日 23:59", "2026年9月28日 25:00", "未设置", "2026年9月28日"):
            self.assertIsNone(mp.parse_due(value))


class PlatformTests(IsolatedTest):
    @unittest.skipIf(os.name == "nt", "POSIX 定时命令在 macOS/Linux 验证")
    def test_posix_scheduled_command_keeps_space_and_special_characters(self):
        argv = [sys.executable, "-c", "print('正常运行')", str(self.root / "file $name's.py")]
        with patch.object(ps, "system", return_value="mac"):
            cmd = ps.command_line(argv)
            plist = ps._launchd_plist(["08:30"], cmd, self.root / "moodle log.txt")
        self.assertEqual(shlex.split(cmd), argv)
        result = subprocess.run(plist["ProgramArguments"], capture_output=True, text=True)
        self.assertEqual(result.stdout.strip(), "正常运行")
        self.assertEqual(result.returncode, 0)

    def test_windows_scheduled_redirection_uses_cmd_and_all_tasks_must_succeed(self):
        with patch.object(ps.subprocess, "run", side_effect=[Mock(returncode=0),
                                                            Mock(returncode=1, stderr="拒绝访问")]) as run:
            ok, _ = ps._install_schtasks(["08:30", "20:00"], '"C:\\Python Files\\python.exe" "C:\\Course Files\\moodle_prep.py"',
                                        "C:\\Course Files\\moodle.log")
        self.assertFalse(ok)
        args = run.call_args_list[0].args[0]
        command = args[args.index("/TR") + 1]
        self.assertTrue(command.startswith('cmd.exe /d /s /c "'))
        self.assertIn('>> "C:\\Course Files\\moodle.log"', command)

    def test_windows_toast_success_has_no_background_fallback(self):
        with patch.object(ps, "system", return_value="windows"), \
                patch.object(ps.subprocess, "run", return_value=Mock(returncode=0)) as run, \
                patch.object(ps.subprocess, "Popen") as popen:
            self.assertTrue(ps.notify("Moodle", "学生's <文件>"))
        script = base64.b64decode(run.call_args.args[0][-1]).decode("utf-16-le")
        self.assertIn("学生''s <文件>", script)
        self.assertIn("CreateTextNode", script)
        popen.assert_not_called()

    def test_windows_toast_failure_starts_nonblocking_balloon(self):
        with patch.object(ps, "system", return_value="windows"), \
                patch.object(ps.subprocess, "run", return_value=Mock(returncode=1)), \
                patch.object(ps.subprocess, "Popen") as popen:
            self.assertTrue(ps.notify("Moodle", "消息"))
        script = base64.b64decode(popen.call_args.args[0][-1]).decode("utf-16-le")
        self.assertIn("ShowBalloonTip", script)
        self.assertNotIn("MessageBox", script)
        popen.return_value.wait.assert_not_called()
        self.assertEqual(popen.call_args.kwargs["stdout"], subprocess.DEVNULL)

    def test_windows_console_reconfigures_both_streams(self):
        with patch.object(ps.sys, "platform", "win32"), \
                patch.object(ps.sys, "stdout") as stdout, patch.object(ps.sys, "stderr") as stderr:
            ps.configure_console()
        stdout.reconfigure.assert_called_once_with(encoding="utf-8")
        stderr.reconfigure.assert_called_once_with(encoding="utf-8")

    def test_existing_twenty_self_checks_in_configured_fixture(self):
        # 安装记录和 shim 是测试夹具，不执行全机安装，也不伪造登录结果。
        (cs.home() / "install.json").write_text('{"harnesses": ["fixture"]}')
        shim = self.root / "mk.cmd"
        shim.write_text("@echo off")
        with patch.object(ps, "shim_path", return_value=shim):
            checks = verify.check_self()
        self.assertEqual(len(checks), 20)   # -sandbox.py +course_code.py
        self.assertTrue(all(ok for ok, _ in checks), checks)


class EndToEndTests(IsolatedTest):
    def test_folder_download_to_webhook_and_second_run_is_silent(self):
        """本机 HTTP 夹具贯穿真实 requests、文件落地、差量状态和 webhook 发送。"""
        deliveries, downloads = [], []

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def reply(self, body, kind="text/html"):
                data = body.encode() if isinstance(body, str) else body
                self.send_response(200)
                self.send_header("Content-Type", kind + "; charset=utf-8")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def do_GET(self):
                if self.path == "/login/index.php":
                    self.reply('<input name="logintoken" value="test-token">')
                elif self.path == "/my/":
                    self.reply("student logout")
                elif self.path.startswith("/course/"):
                    self.reply('<a href="/mod/folder/view.php?id=9"><span>讲义</span></a>')
                elif self.path.startswith("/mod/folder/"):
                    self.reply('<a href="/pluginfile.php/55/mod_folder/content/0/notes.txt?forcedownload=1">notes.txt</a>')
                elif self.path.startswith("/pluginfile.php/"):
                    downloads.append(self.path)
                    self.reply("短小的课堂笔记", "text/plain")
                else:
                    self.reply("")

            def do_POST(self):
                body = self.rfile.read(int(self.headers.get("Content-Length", "0")))
                if self.path == "/webhook":
                    deliveries.append(json.loads(body))
                    self.reply("{}", "application/json")
                else:
                    self.reply("logout")

        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        worker = threading.Thread(target=server.serve_forever, daemon=True)
        worker.start()
        try:
            base = "http://127.0.0.1:%d" % server.server_port
            self.cfg["moodle"]["url"] = base
            self.cfg["delivery"].update(channel="webhook", webhook={"url": base + "/webhook"})
            self.cfg["output"]["mode"] = "silent"
            cs.save_config(self.cfg)
            teams_root = self.root / "OneDrive" / "02 General"
            teams_root.mkdir(parents=True)
            (teams_root / "Course Information.pdf").write_bytes(b"%PDF-fixture")
            (cs.home() / "teams_sources.json").write_text(json.dumps({
                "math_teams": {"name": "数学 Teams", "source": str(teams_root.parent),
                               "path": str(self.root / "teams-files")}
            }, ensure_ascii=False), encoding="utf-8")
            with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
                self.assertEqual(mp.main(["--dry-run"]), 0)
                self.assertFalse(deliveries)
                self.assertFalse(downloads)
                self.assertFalse(cs.state_path(7).exists())
                self.assertEqual(mp.main([]), 0)
                self.assertEqual(mp.main([]), 0)
            self.assertEqual(len(deliveries), 1)
            self.assertEqual(len(downloads), 1)
            self.assertIn("讲义 / notes.txt", deliveries[0]["text"])
            self.assertIn("Course Information.pdf", deliveries[0]["text"])
            self.assertTrue((self.root / "teams-files" / "Course Information" / "Course Information.pdf").exists())
            files = list((self.root / "files").glob("*.txt"))
            self.assertEqual(len(files), 1)
            self.assertEqual(files[0].read_text(), "短小的课堂笔记")
            self.assertTrue(all(ok for ok, _ in verify.check_run()))
        finally:
            server.shutdown()
            worker.join(timeout=5)
            server.server_close()


@unittest.skipIf(os.name == "nt", "Bash 安装器在 macOS/Linux 上验证")
class InstallerTests(IsolatedTest):
    def install(self, scenario, dry=False):
        # 替身 Python 只模拟命令退出码和依赖可用性，安装脚本本身真实运行。
        fake_bin = self.root / "bin"
        fake_bin.mkdir()
        wrapper = fake_bin / "python3"
        wrapper.write_text("#!%s\n" % sys.executable + r'''
import json, os, pathlib, sys
root = pathlib.Path(os.environ['MOODLE_KILLER_HOME'])
args = sys.argv[1:]
scenario = os.environ['MK_INSTALL_SCENARIO']
with (root / 'calls.jsonl').open('a') as f:
    f.write(json.dumps(args) + '\n')
if args == ['-']:
    code = sys.stdin.read()
    if 'missing = []' in code:
        sys.exit(0 if (root / 'deps_ready').exists() else 1)
    sys.exit(1 if scenario == 'old_python' else 0)
if args[:2] == ['-m', 'pip']:
    if scenario == 'success' or (scenario == 'pep668' and 'venv' in sys.argv[0]):
        (root / 'deps_ready').touch()
        sys.exit(0)
    sys.exit(1)
if args[:2] == ['-m', 'venv']:
    if scenario == 'failed':
        sys.exit(1)
    target = pathlib.Path(args[2]) / 'bin' / 'python'
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(pathlib.Path(sys.argv[0]).read_text())
    target.chmod(0o755)
    sys.exit(0)
if args[-1:] == ['install']:
    (root / 'installed').touch()
    sys.exit(0)
sys.exit(3)
''')
        wrapper.chmod(0o755)
        env = dict(os.environ, PATH=str(fake_bin) + os.pathsep + os.environ["PATH"],
                   MK_INSTALL_SCENARIO=scenario)
        result = subprocess.run(["bash", str(ROOT / "install.sh")] + (["--dry-run"] if dry else []),
                                env=env, capture_output=True, text=True, timeout=20)
        calls = [json.loads(line) for line in (self.root / "calls.jsonl").read_text().splitlines()]
        return result, calls

    def test_missing_dependencies_are_installed_before_mk(self):
        result, calls = self.install("success")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue((self.root / "installed").exists())
        self.assertTrue(any(args[:2] == ["-m", "pip"] for args in calls))

    def test_pep668_falls_back_to_persistent_venv(self):
        result, calls = self.install("pep668")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue((self.root / "venv/bin/python").exists())
        self.assertTrue((self.root / "installed").exists())
        self.assertEqual(sum(args[:2] == ["-m", "pip"] for args in calls), 3)

    def test_failed_dependencies_never_run_mk(self):
        result, _ = self.install("failed")
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse((self.root / "installed").exists())

    def test_dry_run_does_not_install_dependencies(self):
        result, calls = self.install("failed", dry=True)
        self.assertEqual(result.returncode, 0)
        self.assertFalse(any(args[:1] == ["-m"] for args in calls))
        self.assertFalse((self.root / "installed").exists())

    def test_old_python_aborts_before_dependencies(self):
        result, calls = self.install("old_python")
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(calls, [["-"]])


class FreshRunTests(IsolatedTest):
    """mk fresh：完整重跑一次真流程，但绝不碰调度器排期（这一条是它的存在理由）。"""

    def _fake_cron(self, next_run_at="2026-09-30T08:30:00+08:00"):
        p = self.root / ".hermes" / "cron" / "jobs.json"
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps({"jobs": [{"id": "9ec44bbe3658",
                                           "name": "moodle-all-courses-monitor",
                                           "next_run_at": next_run_at}]}), encoding="utf-8")
        return p

    def _patch_expanduser(self, real_path):
        """只把 ~/.hermes/cron/jobs.json 指到临时目录，别的一律照旧。

        注意比较的是 **展开后** 的路径：expanduser 拿到的是 `~/...` 原样字符串，
        拿它去比展开后的绝对路径永远不相等 —— 那样 patch 形同不存在，
        断言只是在拿真机的值碰运气（踩过：真值从 09-30 滚到 10-01 时才暴露）。
        """
        import os.path as osp
        real_expanduser = osp.expanduser            # 先抓住原件，否则 patch 后自我递归
        target = osp.normpath(real_expanduser("~/.hermes/cron/jobs.json"))

        def fake(raw):
            try:
                hit = osp.normpath(real_expanduser(raw)) == target
            except TypeError:
                hit = False
            return real_path if hit else real_expanduser(raw)
        return patch.object(osp, "expanduser", fake)

    def test_fresh_runs_real_pipeline_and_reports_slot_untouched(self):
        cron = self._fake_cron()
        calls = []

        def fake_run(cmd, env=None, **kw):
            calls.append((cmd, env or {}))
            return Mock(returncode=0)

        out = io.StringIO()
        with self._patch_expanduser(str(cron)), \
                patch.object(mk.subprocess, "run", side_effect=fake_run), \
                contextlib.redirect_stdout(out), contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(mk.main(["fresh"]), 0)

        cmd, env = calls[0]
        self.assertTrue(cmd[1].endswith("moodle_prep.py"))
        self.assertNotIn("--dry-run", cmd)                     # 真跑，不是试跑
        self.assertEqual(env["MOODLE_KILLER_SOURCE"], "fresh")  # 记进 last_run.json 的来源
        self.assertIn("定时名额没动", out.getvalue())
        self.assertIn("2026-09-30T08:30:00+08:00", out.getvalue())

    def test_fresh_reports_when_schedule_changed(self):
        """万一排期真的变了（别的东西动了它），必须吱声，不能装作没事。"""
        seq = iter(["2026-09-30T08:30:00+08:00", "2026-10-01T08:30:00+08:00"])
        out = io.StringIO()
        with patch.object(mk.subprocess, "run", return_value=Mock(returncode=0)), \
                patch.object(mk, "_cron_next_run_at", side_effect=lambda: next(seq)), \
                contextlib.redirect_stdout(out), contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(mk.main(["fresh"]), 0)
        self.assertIn("定时排期变了", out.getvalue())

    def test_manual_run_is_marked_in_last_run(self):
        out_dir = cs.out_dir()
        os.makedirs(out_dir, exist_ok=True)
        with patch.dict(os.environ, {"MOODLE_KILLER_SOURCE": "fresh"}):
            mp._write_last_run(out_dir, datetime(2026, 9, 29, 15, 0), "ok", [], "heartbeat", "无内容，未发送")
        self.assertEqual(json.loads((out_dir / "last_run.json").read_text())["source"], "fresh")
        with patch.dict(os.environ, {"MOODLE_KILLER_SOURCE": ""}):
            mp._write_last_run(out_dir, datetime(2026, 9, 30, 8, 30), "ok", [], "heartbeat", "已发送")
        self.assertEqual(json.loads((out_dir / "last_run.json").read_text())["source"], "")

    def test_deleted_entry_points_are_gone(self):
        """同一个能力只留一个入口：删掉的旧入口必须真的进不去。"""
        for dead in ("get", "time", "send", "prompt", "sandbox"):
            with self.subTest(cmd=dead), contextlib.redirect_stderr(io.StringIO()):
                with self.assertRaises(SystemExit):
                    mk.main([dead])

    def test_help_points_to_the_surviving_entry_points(self):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            mk.main(["help"])
        text = out.getvalue()
        self.assertIn("mk fresh", text)
        self.assertIn("mk set 时间 07:00", text)   # 改时间的唯一入口
        for dead in ("mk send", "mk sandbox", "mk prompt"):
            self.assertNotIn(dead, text)


class SemesterTests(IsolatedTest):
    """新学期：自动接课 + mk new 的判据必须是「Moodle 自己的学期分类」。"""

    def timeline(self, inprogress=(), future=(), past=()):
        return {"inprogress": list(inprogress), "future": list(future),
                "past": list(past), "all": list(inprogress) + list(future) + list(past)}

    def fake_scanner(self, timeline, scan_result=None, scan_error=None):
        s = Mock()
        s.courses_timeline.return_value = timeline
        if scan_error is not None:
            s.scan_course.side_effect = scan_error
        else:
            s.scan_course.return_value = scan_result or {
                "name": "新课", "new_files": [], "assignments": {}, "new_activities": {},
                "notes": [], "completion_targets": {}, "state_existed": False, "course_id": 99}
        return s

    def test_attaches_this_semester_course_and_guesses_its_folder(self):
        self.cfg["download"]["root"] = str(self.root)
        cs.save_config(self.cfg)
        (self.root / "数学").mkdir()                      # 已有目录：新课的课件应该复用，而不是另建
        scanner = self.fake_scanner(self.timeline(
            inprogress=[{"id": 99, "fullname": "数学 2027/01", "shortname": "MA"}]),
            scan_result={"name": "数学 2027/01", "new_files": ["a.pdf", "b.pdf"], "assignments": {},
                         "new_activities": {}, "notes": [], "completion_targets": {},
                         "state_existed": False, "course_id": 99})
        added, notes = mp.attach_new_courses(scanner, self.cfg, cs.load_courses(), download=True)
        self.assertEqual(len(added), 1)
        self.assertEqual(added[0]["files"], 2)
        self.assertEqual(notes, [])
        course = list(cs.load_courses().values())[-1]
        self.assertEqual(course["id"], 99)
        self.assertTrue(course["path"].rstrip("/").endswith("数学"), course["path"])
        scanner.scan_course.assert_called_once()
        self.assertIn("已接上", mp.attach_summary_item(added[0]))
        self.assertIn("2 个文件", mp.attach_summary_item(added[0]))

    def test_mk_rm_is_not_undone_by_auto_attach(self):
        # 自动接课按「本学期在 Moodle 上」来的：不带「不再自动接上」名单，mk rm 会变成假的。
        cs.ignore_course(99)
        course = {"id": 99, "fullname": "不想盯的课 2027/01", "shortname": "NO"}
        scanner = self.fake_scanner(self.timeline(inprogress=[course]))
        added, _ = mp.attach_new_courses(scanner, self.cfg, cs.load_courses())
        self.assertEqual(added, [])
        scanner.scan_course.assert_not_called()
        # 手动加回来（= mk add 里那一步）→ 撤出名单，自动接课才认它
        cs.unignore_course(99)
        added, _ = mp.attach_new_courses(scanner, self.cfg, cs.load_courses())
        self.assertEqual(len(added), 1)
        self.assertEqual(cs.load_ignored(), set())

    def test_rm_records_ignored_list(self):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.assertEqual(mk.main(["rm", "数学"]), 0)
        self.assertIn("7", cs.load_ignored())
        self.assertEqual(cs.load_courses(), {})
        self.assertIn("不再自动接上", out.getvalue())

    def test_chinese_course_name_reuses_existing_folder_too(self):
        # 中文课名没有「英文实词」，不能因此永远复用不了已有文件夹
        self.cfg["download"]["root"] = str(self.root)
        cs.save_config(self.cfg)
        (self.root / "高等数学").mkdir()
        scanner = self.fake_scanner(self.timeline(
            inprogress=[{"id": 99, "fullname": "高等数学（上）2027/01", "shortname": "GS"}]))
        added, _ = mp.attach_new_courses(scanner, self.cfg, cs.load_courses())
        self.assertTrue(added[0]["path"].rstrip("/").endswith("高等数学"), added[0]["path"])

    def test_unreadable_timeline_attaches_nothing(self):
        for timeline in ({}, Mock()):                     # 接口没给出结果 / 不是 dict
            with self.subTest(timeline=type(timeline).__name__):
                before = cs.courses_path().read_text()
                scanner = self.fake_scanner(timeline)
                added, _ = mp.attach_new_courses(scanner, self.cfg, cs.load_courses())
                self.assertEqual(added, [])
                scanner.scan_course.assert_not_called()
                self.assertEqual(cs.courses_path().read_text(), before)

    def test_already_registered_course_is_not_reattached(self):
        scanner = self.fake_scanner(self.timeline(
            inprogress=[{"id": 7, "fullname": "数学", "shortname": "MA"}]))
        added, _ = mp.attach_new_courses(scanner, self.cfg, cs.load_courses())
        self.assertEqual(added, [])
        scanner.scan_course.assert_not_called()

    def test_failed_first_scan_is_reported_not_swallowed(self):
        scanner = self.fake_scanner(self.timeline(
            inprogress=[{"id": 99, "fullname": "新课 2027/01", "shortname": "X"}]),
            scan_error=RuntimeError("网络断了"))
        added, notes = mp.attach_new_courses(scanner, self.cfg, cs.load_courses())
        self.assertIsNone(added[0]["files"])
        self.assertTrue(notes and "首次扫描失败" in notes[0])
        self.assertIn("首次扫描失败", mp.attach_summary_item(added[0]))

    def run_new(self, argv, timeline):
        client = self.fake_scanner(timeline)
        client.login.return_value = True
        with patch("moodle_client.MoodleClient", return_value=client), \
                contextlib.redirect_stdout(io.StringIO()) as out, \
                contextlib.redirect_stderr(io.StringIO()):
            code = mk.main(list(argv))
        return code, out.getvalue(), client

    def test_new_preview_changes_nothing(self):
        before = cs.courses_path().read_text()
        code, out, client = self.run_new(["new"], self.timeline(
            inprogress=[{"id": 7, "fullname": "数学", "shortname": "MA"},
                        {"id": 99, "fullname": "新学期的课 2027/01", "shortname": "NEW"}]))
        self.assertEqual(code, 0)
        self.assertIn("将接上", out)
        self.assertIn("mk new --yes", out)
        self.assertEqual(cs.courses_path().read_text(), before)
        client.scan_course.assert_not_called()            # 预览绝不动手，更不下文件

    def test_new_refuses_when_semester_unknown(self):
        # 读不到学期分类时绝不能「把课全摘了」——这条是命令存在的底线
        for timeline in ({}, Mock()):
            with self.subTest(timeline=type(timeline).__name__):
                before = cs.courses_path().read_text()
                code, _out, _c = self.run_new(["new", "--yes"], timeline)
                self.assertEqual(code, 2)
                self.assertEqual(cs.courses_path().read_text(), before)

    def test_new_yes_detaches_last_semester_and_attaches_this_one(self):
        cs.save_courses({"old": {"id": 5, "name": "上学期的高数", "path": str(self.root / "老课")},
                         "math": {"id": 7, "name": "数学", "path": str(self.root / "数学")}})
        code, out, client = self.run_new(["new", "--yes"], self.timeline(
            inprogress=[{"id": 99, "fullname": "新学期的课 2027/01", "shortname": "NEW"}],
            future=[{"id": 100, "fullname": "下学期的课", "shortname": "NEXT"}]))
        self.assertEqual(code, 0)
        left = cs.load_courses()
        self.assertNotIn("old", left)                     # 上学期的课摘掉
        self.assertNotIn("math", left)                    # 不在本学期清单里 → 也摘掉
        self.assertEqual([v["id"] for v in left.values()], [99])
        self.assertIn("下学期", out)                      # 下学期的只提示、不动
        self.assertTrue(list((cs.home() / "backups").glob("*_mk-new/courses.json")))
        self.assertTrue(list((self.root / "老课").parent.exists() for _ in [0]))   # 目录没被动

class FolderNameTests(IsolatedTest):
    """课程文件夹命名：字段拆分、模板校验、mk folder 的预览与真正重命名。"""

    def course(self, name="MAT203 Statistics 2026/09 Koh Siew Khew", code="MAT203",
               shortname="Stat"):
        return {"name": name, "code": code, "shortname": shortname}

    def test_fields_are_split_the_way_the_names_actually_look(self):
        f = cs.split_course_name(*[self.course().get(k) for k in ("name", "code", "shortname")])
        self.assertEqual(f["code"], "MAT203")
        self.assertEqual(f["name"], "Statistics")
        self.assertEqual(f["teacher"], "Koh Siew Khew")
        self.assertEqual(f["semester"], "2026-09")
        # 并列代号：MAT301 and MAT418 Partial Differential Equations
        f = cs.split_course_name("MAT301 and MAT418 Partial Differential Equations 2026/09 Yufeng Lu",
                                 "MAT301")
        self.assertEqual(f["code"], "MAT301")
        self.assertEqual(f["name"], "Partial Differential Equations")
        # 带斜杠的代号 + 连字符名称
        f = cs.split_course_name("MPU1022/MPU3322 Integrity & Anti-Corruption 2026/09 Dr Loh", "MPU1022")
        self.assertEqual(f["code"], "MPU1022")
        self.assertEqual(f["name"], "Integrity & Anti-Corruption")
        # 短名（AAI）不是代号 —— 别跟真编码（MAT211）打架；课名里没代号时留空
        f = cs.split_course_name("Abstract Algebra I 2026/09 Ali Azimi", "AAI", "AAI")
        self.assertEqual(f["code"], "")
        self.assertEqual(f["name"], "Abstract Algebra I")
        self.assertEqual(cs.render_folder_name(
            {"name": "Abstract Algebra I 2026/09 Ali Azimi", "code": "AAI"}, "{code} {name}"),
            "Abstract Algebra I")
        # 但带空格/斜杠的多词短名不算代号（Moodle 会给出「Corruption 2026/09」这种）
        f = cs.split_course_name("Integrity 2026/09 Dr Loh", "Corruption 2026/09")
        self.assertEqual(f["code"], "")

    def test_template_rendering_and_validation(self):
        c = self.course()
        self.assertEqual(cs.render_folder_name(c, "{code} {name}"), "MAT203 Statistics")
        self.assertEqual(cs.render_folder_name(c, "{name} ({teacher})"), "Statistics (Koh Siew Khew)")
        # 字段为空时不占位、不留双空格（这里课名没代号、code 也空）
        self.assertEqual(cs.render_folder_name({"name": "Statistics 2026/09"}, "{code} {name}"),
                         "Statistics")
        # 字段为空时不留空括号（Teams 来源常常没有老师）
        self.assertEqual(cs.render_folder_name({"name": "MAT201 Mathematical Analysis I", "code": "MAT201"},
                                               "{code} {name} ({teacher})"),
                         "MAT201 Mathematical Analysis I")
        # 非法字符（斜杠）不能进文件夹名
        self.assertNotIn("/", cs.render_folder_name({"name": "A/B 2026/09", "code": "AB12"}, "{code} {name}"))
        for bad_tpl, why in (("{nope} {name}", "不认识的字段"), ("", "空"), ("{semester}", "挤进同一个")):
            with self.subTest(tpl=bad_tpl), self.assertRaises(ValueError):
                cs.check_folder_template(bad_tpl)

    def make_folders(self):
        for name in ("Statistics", "Partial Differential Equations"):
            (self.root / name).mkdir(exist_ok=True)
            (self.root / name / "a.pdf").write_text("x")
        cs.save_courses({
            "stat": {"id": 1, "name": "MAT203 Statistics 2026/09 Koh Siew Khew", "code": "MAT203",
                     "shortname": "Stat", "path": str(self.root / "Statistics") + "/"},
            "pde": {"id": 2, "name": "MAT301 and MAT418 Partial Differential Equations 2026/09 Yufeng Lu",
                    "code": "MAT301", "shortname": "PDEs",
                    "path": str(self.root / "Partial Differential Equations") + "/"}})
        self.cfg["download"]["root"] = str(self.root)
        self.cfg["download"]["folder_template"] = "{code} {name}"
        cs.save_config(self.cfg)

    def test_folder_preview_does_not_touch_anything(self):
        self.make_folders()
        before = cs.courses_path().read_text()
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.assertEqual(mk.main(["folder"]), 0)
        text = out.getvalue()
        self.assertIn("MAT203 Statistics", text)
        self.assertIn("{code}", text)                     # 字段说明也在
        self.assertTrue((self.root / "Statistics").is_dir())
        self.assertEqual(cs.courses_path().read_text(), before)

    def test_folder_apply_renames_and_updates_config(self):
        self.make_folders()
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.assertEqual(mk.main(["folder", "--apply"]), 0)
        self.assertTrue((self.root / "MAT203 Statistics" / "a.pdf").exists())     # 文件跟着走
        self.assertFalse((self.root / "Statistics").exists())
        self.assertTrue((self.root / "MAT301 Partial Differential Equations").is_dir())
        paths = [v["path"] for v in cs.load_courses().values()]
        self.assertIn(str(self.root / "MAT203 Statistics") + "/", paths)
        backup = list((cs.home() / "backups").glob("*_folder-rename/courses.json"))
        self.assertTrue(backup, "改名必须先备份配置")

    def test_folder_apply_skips_conflicts_instead_of_merging(self):
        # 两门课算成同一个名字 → 都不许动（绝不把两门课合并进一个文件夹）
        cs.save_courses({"a": {"id": 1, "name": "Math 2026/09 T1", "code": "MA10", "path": str(self.root / "甲") + "/"},
                         "b": {"id": 2, "name": "Math 2026/09 T2", "code": "MA11", "path": str(self.root / "乙") + "/"}})
        self.cfg["download"]["folder_template"] = "{name}"
        cs.save_config(self.cfg)
        (self.root / "甲").mkdir(); (self.root / "乙").mkdir()
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            mk.main(["folder", "--apply"])
        self.assertIn("冲突", out.getvalue())
        self.assertTrue((self.root / "甲").is_dir() and (self.root / "乙").is_dir())

    def test_config_pointing_at_a_missing_dir_is_detected_and_repaired(self):
        """配置指向 A、磁盘上是 B → 必须认出来（只比配置字符串会一路说「没问题」）。"""
        (self.root / "MPU3322 Integrity & Anti-Corruption").mkdir()
        (self.root / "MPU3322 Integrity & Anti-Corruption" / "a.pdf").write_text("x")
        cs.save_courses({"mpu": {"id": 3, "name": "MPU1022/MPU3322 Integrity & Anti-Corruption 2026/09 Dr Loh",
                                 "code": "MPU1022", "shortname": "Corruption 2026/09",
                                 "path": str(self.root / "MPU1022 Integrity & Anti-Corruption") + "/"}})
        self.cfg["download"]["folder_template"] = "{code} {name}"
        cs.save_config(self.cfg)

        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.assertEqual(mk.main(["folder"]), 0)
        self.assertIn("配置路径已失效", out.getvalue())        # 预览就要说清楚

        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(mk.main(["folder", "--apply"]), 0)
        # 磁盘上的目录被改名成模板名，文件跟着走；配置指向新名字
        self.assertTrue((self.root / "MPU1022 Integrity & Anti-Corruption" / "a.pdf").exists())
        self.assertFalse((self.root / "MPU3322 Integrity & Anti-Corruption").exists())
        self.assertEqual(list(cs.load_courses().values())[0]["path"],
                         str(self.root / "MPU1022 Integrity & Anti-Corruption") + "/")

    def test_disk_already_matches_template_only_needs_config_fixed(self):
        (self.root / "MAT203 Statistics").mkdir()
        cs.save_courses({"s": {"id": 4, "name": "MAT203 Statistics 2026/09 Koh", "code": "MAT203",
                               "path": str(self.root / "Statistics") + "/"}})
        self.cfg["download"]["folder_template"] = "{code} {name}"
        cs.save_config(self.cfg)
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.assertEqual(mk.main(["folder", "--apply"]), 0)
        self.assertIn("修好配置", out.getvalue())
        self.assertTrue((self.root / "MAT203 Statistics").is_dir())          # 没有多余改名
        self.assertEqual(list(cs.load_courses().values())[0]["path"],
                         str(self.root / "MAT203 Statistics") + "/")

    def test_stored_code_wins_when_the_name_carries_several_codes(self):
        """课名里挂着两个代号时（MPU1022/MPU3322，按入学批次分），以课程配置指定的为准。"""
        nm = "MPU1022/MPU3322 Integrity & Anti-Corruption 2026/09 Dr Loh"
        self.assertEqual(cs.split_course_name(nm, "MPU3322")["code"], "MPU3322")
        self.assertEqual(cs.split_course_name(nm, "MPU1022")["code"], "MPU1022")
        self.assertEqual(cs.render_folder_name({"name": nm, "code": "MPU3322"}, "{code} {name}"),
                         "MPU3322 Integrity & Anti-Corruption")
        # 普通课不受影响：名字里只有一个代号，取它
        self.assertEqual(cs.split_course_name("MAT203 Statistics 2026/09 Koh", "MAT203")["code"], "MAT203")

    def test_ambiguous_candidates_are_never_guessed(self):
        """磁盘上有两个都像这门课的目录 → 一个都不许动（挑错 = 把课件搬进别人的目录）。"""
        for name in ("MAT203 Statistics A", "MAT203 Statistics B"):
            (self.root / name).mkdir()
        cs.save_courses({"a": {"id": 1, "name": "MAT203 Statistics 2026/09 Koh", "code": "MAT203",
                               "path": str(self.root / "GoneDir") + "/"}})
        self.cfg["download"]["folder_template"] = "{code} {name}"
        cs.save_config(self.cfg)
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.assertEqual(mk.main(["folder", "--apply"]), 0)
        self.assertIn("歧义", out.getvalue())
        self.assertTrue((self.root / "MAT203 Statistics A").is_dir())
        self.assertTrue((self.root / "MAT203 Statistics B").is_dir())

    def test_case_only_difference_can_still_be_fixed(self):
        (self.root / "mat203 statistics").mkdir()
        (self.root / "mat203 statistics" / "a.pdf").write_text("x")
        cs.save_courses({"a": {"id": 1, "name": "MAT203 Statistics 2026/09 Koh", "code": "MAT203",
                               "path": str(self.root / "mat203 statistics") + "/"}})
        self.cfg["download"]["folder_template"] = "{code} {name}"
        cs.save_config(self.cfg)
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(mk.main(["folder", "--apply"]), 0)
        self.assertTrue((self.root / "MAT203 Statistics" / "a.pdf").exists())
        # 大小写不敏感的文件系统上「旧名字」会一并消失（是同一个 inode）
        self.assertEqual([p.name for p in self.root.iterdir() if p.name.lower() == "mat203 statistics"],
                         ["MAT203 Statistics"])

    def test_unexpanded_tilde_path_is_understood(self):
        """配置里手写 ~/… 时不能把好目录判成「配置路径已失效」。"""
        home = self.root / "fakehome"
        (home / "Knowledge" / "MAT203 Statistics").mkdir(parents=True)
        cs.save_courses({"a": {"id": 1, "name": "MAT203 Statistics 2026/09 Koh", "code": "MAT203",
                               "path": "~/Knowledge/MAT203 Statistics/"}})
        self.cfg["download"]["folder_template"] = "{code} {name}"
        cs.save_config(self.cfg)
        out = io.StringIO()
        with patch.dict(os.environ, {"HOME": str(home)}), contextlib.redirect_stdout(out):
            self.assertEqual(mk.main(["folder", "--apply"]), 0)
        self.assertNotIn("失效", out.getvalue())
        self.assertIn("对得上", out.getvalue())

    def test_symlinked_course_dir_moves_the_link_not_the_target(self):
        """课件目录是软链时：只能搬链接本身。带尾斜杠 move 会跟随软链、把真实目录搬走。"""
        real = self.root / "realschool"
        real.mkdir()
        (real / "keep.pdf").write_text("x")
        (self.root / "Statistics").symlink_to(real)
        cs.save_courses({"a": {"id": 1, "name": "MAT203 Statistics 2026/09 Koh", "code": "MAT203",
                               "path": str(self.root / "Statistics") + "/"}})
        self.cfg["download"]["folder_template"] = "{code} {name}"
        cs.save_config(self.cfg)
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(mk.main(["folder", "--apply"]), 0)
        self.assertTrue((real / "keep.pdf").exists(), "真实目录不许被搬走")
        self.assertTrue((self.root / "MAT203 Statistics").is_symlink())
        self.assertFalse((self.root / "Statistics").exists())

    def test_doctor_flags_a_course_dir_that_is_missing_from_disk(self):
        (self.root / "MPU3322 Integrity & Anti-Corruption").mkdir()
        cs.save_courses({"mpu": {"id": 3, "name": "MPU1022/MPU3322 Integrity & Anti-Corruption 2026/09 Dr Loh",
                                 "code": "MPU1022", "path": str(self.root / "MPU1022 Integrity & Anti-Corruption") + "/"}})
        cs.save_config(self.cfg)
        # doctor 的检查项内联在 cmd_doctor 里 → 直接跑命令，断言输出里点出这条
        out = io.StringIO()
        with patch("moodle_client.MoodleClient", side_effect=SystemExit("跳过登录")), \
                contextlib.redirect_stdout(out), contextlib.redirect_stderr(io.StringIO()):
            mk.main(["doctor"])
        self.assertIn("对不上磁盘", out.getvalue())
        self.assertIn("mk folder --apply", out.getvalue())

    def test_folder_set_rejects_bad_template(self):
        for bad in ("{nope} {name}", "", "{semester}"):
            with self.subTest(tpl=bad):
                with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
                    self.assertEqual(mk.main(["folder", bad]), 2)
        self.assertEqual(cs.get_path(cs.load_config(), "download.folder_template"), "{code} {name}")

    def test_folder_apply_never_merges_into_existing_target(self):
        self.make_folders()
        (self.root / "MAT203 Statistics").mkdir()          # 目标已经存在（别的东西占着）
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.assertEqual(mk.main(["folder", "--apply"]), 0)
        self.assertIn("目标目录已存在", out.getvalue())
        self.assertTrue((self.root / "Statistics" / "a.pdf").exists())     # 原目录没被动


class CourseCodeTests(IsolatedTest):
    """课程代号：短名不算代号、资料里的 Course Code 才算、Teams 交叉验证要是同一门课。"""

    def make_course_dir(self, name, files=(), syllabus_text=None):
        d = self.root / name
        d.mkdir(exist_ok=True)
        for f in files:
            (d / f).write_text("x")
        if syllabus_text:
            sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "moodle-killer" / "scripts"))
            import course_code as cc
            with patch.object(cc, "_pdf_text_head", return_value=syllabus_text):
                return cc
        return None

    def test_shortname_is_never_used_as_a_course_code(self):
        # 短名（AAI）不是代号：它跟真编码（MAT211）会打架
        f = cs.split_course_name("Abstract Algebra I 2026/09 Ali Azimi", "AAI", "AAI", "")
        self.assertEqual(f["code"], "")
        self.assertEqual(cs.render_folder_name(
            {"name": "Abstract Algebra I 2026/09 Ali Azimi", "code": "AAI", "shortname": "AAI"},
            "{code} {name}"), "Abstract Algebra I")
        # 但真代号（或带可信来源标注的）照样用
        self.assertEqual(cs.split_course_name("Abstract Algebra I 2026/09 Ali Azimi", "MAT211",
                                              "AAI", "material")["code"], "MAT211")

    def test_syllabus_course_code_field_is_the_source_of_truth(self):
        import course_code as cc
        d = self.root / "AAI Abstract Algebra I"
        d.mkdir()
        (d / "Course-Information.pdf").write_text("x")
        entry = {"name": "Abstract Algebra I 2026/09 Ali Azimi", "shortname": "AAI",
                 "path": str(d) + "/"}
        with patch.object(cc, "_pdf_text_head",
                          return_value="Abstract Algebra I\nCourse Codes: MAT211\nCredits: 4"):
            res = cc.lookup(entry, deep=True)
        self.assertEqual(res["status"], "confirmed")
        self.assertEqual(res["code"], "MAT211")
        self.assertIn("Course Code", res["why"])

    def test_attached_style_shallow_lookup_does_not_borrow_another_course_code(self):
        """浅查（只看目录名/大纲类文件名）不能把别的课的代号认成自己的。"""
        import course_code as cc
        d = self.root / "AAI Abstract Algebra I"
        d.mkdir()
        (d / "Textbook").mkdir()
        (d / "Textbook" / "MAT201-Chapter-1.pdf").write_text("x")     # 别的课的教材
        entry = {"name": "Abstract Algebra I 2026/09 Ali Azimi", "shortname": "AAI", "path": str(d) + "/"}
        teams = {"mat201": {"name": "MAT201 Mathematical Analysis I", "source": "/x/MAT201 Analysis",
                            "path": str(self.root / "MAT201 Mathematical Analysis I")}}
        res = cc.lookup(entry, teams_sources=teams, deep=False)
        self.assertEqual(res["code"], "")                              # 不许变成 MAT201
        self.assertEqual(res["status"], "none")

    def test_teams_cross_check_only_for_the_same_course(self):
        import course_code as cc
        d = self.root / "Mathematical Analysis I"
        d.mkdir()
        entry = {"name": "MAT201 Mathematical Analysis I 202609", "path": str(d) + "/"}
        teams = {"mat201": {"name": "MAT201 Mathematical Analysis I", "source": "/x/MAT201 Analysis 202609",
                            "path": str(self.root / "MAT201 Mathematical Analysis I")}}
        res = cc.lookup(entry, teams_sources=teams, deep=True)
        self.assertEqual(res["status"], "confirmed")
        self.assertEqual(res["code"], "MAT201")

    def test_multi_code_name_needs_a_human_pick(self):
        import course_code as cc
        nm = "MPU1022/MPU3322 Integrity & Anti-Corruption 2026/09 Dr Loh"
        base = {"name": nm, "path": str(self.root / "x") + "/"}
        self.assertEqual(cc.lookup(base, deep=True)["status"], "multi")
        self.assertEqual(cc.lookup({**base, "code": "MPU3322"}, deep=True)["code"], "MPU3322")

    def test_enrich_writes_confirmed_codes_and_marks_manual(self):
        import course_code as cc
        d = self.root / "MAT203 Statistics"
        d.mkdir()
        courses = {"s": {"id": 1, "name": "MAT203 Statistics 2026/09 Koh", "path": str(d) + "/"}}
        rows = cc.enrich(courses, None, {}, deep=True, write=True)
        self.assertTrue(rows[0]["changed"])
        self.assertEqual(courses["s"]["code"], "MAT203")
        self.assertEqual(courses["s"]["code_source"], "name")
        # 人工指定过的永不被自动改
        courses["s"]["code_source"] = "manual"
        courses["s"]["code"] = "XXXX99"
        cc.enrich(courses, None, {}, deep=True, write=True)
        self.assertEqual(courses["s"]["code"], "XXXX99")

    def test_mk_code_manual_and_apply(self):
        d = self.root / "AAI Abstract Algebra I"
        d.mkdir()
        (d / "Course-Information.pdf").write_text("x")
        cs.save_courses({"aai": {"id": 9, "name": "Abstract Algebra I 2026/09 Ali Azimi",
                                 "shortname": "AAI", "path": str(d) + "/"}})
        import course_code as cc
        with patch.object(cc, "_pdf_text_head", return_value="Course Codes: MAT211"), \
                contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(mk.main(["code", "--apply"]), 0)
        self.assertEqual(cs.load_courses()["aai"]["code"], "MAT211")
        self.assertEqual(cs.load_courses()["aai"]["code_source"], "material")

        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.assertEqual(mk.main(["code", "Abstract", "MAT211"]), 0)   # 人工改
        self.assertEqual(cs.load_courses()["aai"]["code_source"], "manual")
        # 人工指定后：自动查证不再覆盖
        with patch.object(cc, "_pdf_text_head", return_value="Course Codes: ZZ999"), \
                contextlib.redirect_stdout(io.StringIO()):
            mk.main(["code", "--apply"])
        self.assertEqual(cs.load_courses()["aai"]["code"], "MAT211")

    def test_mk_code_rejects_bad_input(self):
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(mk.main(["code", "Abstract"]), 2)           # 缺代号
            self.assertEqual(mk.main(["code", "Abstract", "MAT 2 1"]), 2)  # 代号含空格
            self.assertEqual(mk.main(["code", "没有这门课", "MAT211"]), 2)   # 找不到课

    def test_pipeline_confirms_a_missing_code_from_the_syllabus(self):
        """真跑时：课名里没代号、Moodle 官方编号为空 → 翻课程资料里的 Course Code 补上。"""
        import course_code as cc
        d = self.root / "files"
        d.mkdir()
        (d / "Course-Information.pdf").write_text("x")
        cs.save_courses({"aai": {"id": 9, "name": "Abstract Algebra I 2026/09 Ali Azimi",
                                 "shortname": "AAI", "path": str(d) + "/"}})
        cs.save_config(self.cfg)
        scanner = Mock()
        scanner.login.return_value = True
        scanner.state_dir = str(cs.state_dir())
        scanner.check_notifications.return_value = []
        scanner.scan_course.return_value = {"name": "Abstract Algebra I 2026/09 Ali Azimi",
                                            "new_files": [], "assignments": {}, "new_activities": {},
                                            "notes": [], "completion_targets": {},
                                            "state_existed": True, "course_id": 9}
        scanner.courses_timeline.return_value = {}
        with patch.object(mp, "MoodleClient", return_value=scanner), \
                patch.object(sender, "send_text", return_value=(True, "已发送")), \
                patch.object(cc, "_pdf_text_head", return_value="Course Codes: MAT211"), \
                contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(mp.main([]), 0)
        entry = cs.load_courses()["aai"]
        self.assertEqual(entry["code"], "MAT211")
        self.assertEqual(entry["code_source"], "material")
        report = (cs.out_dir() / "verify_report.txt").read_text(encoding="utf-8")
        self.assertIn("课程代号: Abstract Algebra I", report)
        self.assertIn("MAT211", report)

    def test_doctor_flags_unconfirmed_codes(self):
        cs.save_courses({"a": {"id": 1, "name": "数学 2026/09 老师", "path": str(self.root) + "/"}})
        out = io.StringIO()
        with patch("moodle_client.MoodleClient", side_effect=SystemExit("跳过登录")), \
                contextlib.redirect_stdout(out), contextlib.redirect_stderr(io.StringIO()):
            mk.main(["doctor"])
        self.assertIn("课程代号", out.getvalue())
        self.assertIn("mk code", out.getvalue())


if __name__ == "__main__":
    unittest.main()
