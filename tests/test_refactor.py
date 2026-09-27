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
        last = cs.out_dir() / "last_run.json"
        last.write_text("previous", encoding="utf-8")
        original = state.read_bytes()
        code, send, collect, _, _ = self.run_pipeline(["--dry-run"])
        self.assertEqual(code, 0)
        send.assert_not_called()
        self.assertFalse(collect.call_args.kwargs["do_download"])
        self.assertEqual(state.read_bytes(), original)
        self.assertEqual(last.read_text(), "previous")

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

    def test_cli_send_and_channel_test_do_not_change_config(self):
        before = cs.config_path().read_bytes()
        for argv, expected in ((["send", "你好", "Moodle", "--json"], "你好 Moodle"),
                               (["channel", "test", "--json"], "通道测试")):
            output = io.StringIO()
            with patch.object(sender, "send_text", return_value=(True, "已发送")) as send, \
                    contextlib.redirect_stdout(output):
                self.assertEqual(mk.main(argv), 0)
            self.assertIn(expected, send.call_args.args[0])
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

    def test_folder_diff_sees_later_additions(self):
        first = self.activities()
        self.assertEqual(len(self.client.diff_new_files(7, first)[0]), 3)
        self.assertFalse(self.client.diff_new_files(7, self.activities())[0])
        updated = self.activities("<a href='/pluginfile.php/55/mod_folder/content/0/new.txt'>new.txt</a>")
        new, _ = self.client.diff_new_files(7, updated)
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
        self.assertEqual(len(checks), 20)
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


if __name__ == "__main__":
    unittest.main()
