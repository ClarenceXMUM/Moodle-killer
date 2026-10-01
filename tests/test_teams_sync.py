"""Local Teams sync: incremental copies, updates, dry runs, and collisions."""
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "moodle-killer" / "scripts"))
import teams_sync as ts
import moodle_prep as mp
import config_store as cs
from unittest.mock import Mock
import io
import contextlib


class TeamsSyncTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        env = patch.dict(os.environ, {"MOODLE_KILLER_HOME": str(self.root / "data")})
        env.start()
        self.addCleanup(env.stop)
        self.src = self.root / "OneDrive"
        self.dst = self.root / "Knowledge"
        (self.src / "02 General").mkdir(parents=True)
        (self.src / "Student Folders").mkdir()
        (self.src / "02 General" / "Course Information.pdf").write_bytes(b"%PDF-course")
        (self.src / "Student Folders" / "private.pdf").write_bytes(b"not copied")
        self.info = {"source": str(self.src), "path": str(self.dst)}

    def test_copy_then_update_and_ignore_student_folders(self):
        first = ts.scan_source("MAT201", self.info)
        target = self.dst / "Course Information" / "Course Information.pdf"
        self.assertEqual(first["new_files"], ["Course Information/Course Information.pdf"])
        self.assertEqual(target.read_bytes(), b"%PDF-course")
        self.assertFalse((self.dst / "Student Folders").exists())
        self.assertEqual(ts.scan_source("MAT201", self.info)["new_files"], [])
        (self.src / "02 General" / "Course Information.pdf").write_bytes(b"%PDF-updated")
        self.assertEqual(len(ts.scan_source("MAT201", self.info)["new_files"]), 1)
        self.assertEqual(target.read_bytes(), b"%PDF-updated")

    def test_dry_run_and_collision_do_not_change_files_or_state(self):
        target = self.dst / "Course Information" / "Course Information.pdf"
        target.parent.mkdir(parents=True)
        target.write_bytes(b"my notes")
        self.assertEqual(len(ts.scan_source("MAT201", self.info, download=False)["new_files"]), 0)
        result = ts.scan_source("MAT201", self.info)
        self.assertTrue(result["notes"])
        self.assertEqual(target.read_bytes(), b"my notes")
        self.assertFalse(ts._state_path("MAT201").exists())

    def test_missing_source_is_error(self):
        with self.assertRaises(FileNotFoundError):
            ts.scan_source("MAT201", {"source": str(self.root / "missing"), "path": str(self.dst)})

    def test_moodle_pipeline_collects_teams_signals(self):
        ts.sources_path().write_text(json.dumps({"mat201": {"name": "MAT201", **self.info}}), encoding="utf-8")
        scanner = Mock()
        scanner.check_notifications.return_value = []
        items, _, meta = mp.collect(scanner, {}, {}, do_download=True)
        self.assertEqual(meta["scanned"], 1)
        self.assertEqual(meta["missing"], [])
        self.assertIn("Course Information.pdf", items[0]["text"])
        self.assertEqual(mp.collect(scanner, {}, {}, do_download=True)[0], [])

    def test_teams_only_pipeline_needs_no_moodle_login(self):
        ts.sources_path().write_text(json.dumps({"mat201": {"name": "MAT201", **self.info}}), encoding="utf-8")
        cfg = cs.load_config()
        cfg["delivery"]["channel"] = "none"
        cs.save_config(cfg)
        with patch.object(mp, "MoodleClient") as moodle, contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(mp.main(["--dry-run"]), 0)
            self.assertFalse((self.dst / "Course Information").exists())
            self.assertEqual(mp.main([]), 0)
            self.assertEqual(mp.main([]), 0)
        moodle.assert_not_called()
        self.assertEqual((self.dst / "Course Information" / "Course Information.pdf").read_bytes(), b"%PDF-course")
        self.assertIn("仅 Teams", (cs.out_dir() / "verify_report.txt").read_text(encoding="utf-8"))

    def test_missing_teams_folder_fails_closed(self):
        ts.sources_path().write_text(json.dumps({"mat201": {"name": "MAT201", "source": str(self.root / "missing"), "path": str(self.dst)}}), encoding="utf-8")
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(mp.main(["--dry-run"]), 2)
        # 试跑的产出落在 .dryrun/，不覆盖正常扫描的记录
        report = (cs.out_dir() / ".dryrun" / "verify_report.txt").read_text(encoding="utf-8")
        self.assertIn("Teams 扫描失败", report)


if __name__ == "__main__":
    unittest.main()
