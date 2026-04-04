"""Tests for session detection."""

import os
import sys
import tempfile
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent))

import unittest
from unittest.mock import patch, MagicMock
from config import Config
from session import human_present, take_workspace_snapshot, workspace_diff, _list_workspace_files


class TestSessionDetection(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.config = Config()
        self.config.workspace_dir = self.tmp

    def tearDown(self):
        import shutil
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_human_present_returns_bool(self):
        result = human_present()
        self.assertIsInstance(result, bool)

    @patch("session.subprocess.run")
    def test_human_present_ssh_detected(self, mock_run):
        mock_run.return_value = MagicMock(
            stdout="user     pts/0        2026-04-04 10:00 (192.168.1.5)\n"
        )
        self.assertTrue(human_present())

    @patch("session.subprocess.run")
    def test_human_present_no_ssh(self, mock_run):
        mock_run.return_value = MagicMock(stdout="user     console      2026-04-04 10:00\n")
        self.assertFalse(human_present())

    @patch("session.subprocess.run")
    def test_human_present_empty_who(self, mock_run):
        mock_run.return_value = MagicMock(stdout="")
        self.assertFalse(human_present())

    @patch("session.subprocess.run")
    def test_human_present_timeout(self, mock_run):
        import subprocess
        mock_run.side_effect = subprocess.TimeoutExpired("who", 5)
        self.assertFalse(human_present())

    def test_workspace_snapshot(self):
        Path(self.tmp, "test.txt").write_text("hello")
        Path(self.tmp, "data.json").write_text("{}")

        snapshot = take_workspace_snapshot(self.config)
        self.assertIn("ts", snapshot)
        self.assertIn("files", snapshot)
        self.assertIn("test.txt", snapshot["files"])
        self.assertIn("data.json", snapshot["files"])

    def test_list_workspace_files_empty(self):
        files = _list_workspace_files(self.config)
        self.assertEqual(files, [])

    def test_list_workspace_files_sorted(self):
        Path(self.tmp, "b.txt").write_text("b")
        Path(self.tmp, "a.txt").write_text("a")
        files = _list_workspace_files(self.config)
        self.assertEqual(files, ["a.txt", "b.txt"])

    def test_list_workspace_files_skips_dirs(self):
        Path(self.tmp, "subdir").mkdir()
        Path(self.tmp, "file.txt").write_text("x")
        files = _list_workspace_files(self.config)
        self.assertEqual(files, ["file.txt"])

    def test_list_workspace_nonexistent(self):
        self.config.workspace_dir = "/nonexistent/path"
        files = _list_workspace_files(self.config)
        self.assertEqual(files, [])

    # --- workspace_diff ---

    def test_workspace_diff_no_changes(self):
        Path(self.tmp, "a.txt").write_text("x")
        snapshot = take_workspace_snapshot(self.config)
        diff = workspace_diff(self.config, snapshot)
        self.assertEqual(diff, "")

    def test_workspace_diff_new_file(self):
        snapshot = take_workspace_snapshot(self.config)
        Path(self.tmp, "new.txt").write_text("appeared")
        diff = workspace_diff(self.config, snapshot)
        self.assertIn("New files", diff)
        self.assertIn("new.txt", diff)

    def test_workspace_diff_removed_file(self):
        Path(self.tmp, "old.txt").write_text("x")
        snapshot = take_workspace_snapshot(self.config)
        Path(self.tmp, "old.txt").unlink()
        diff = workspace_diff(self.config, snapshot)
        self.assertIn("Removed files", diff)
        self.assertIn("old.txt", diff)

    def test_workspace_diff_empty_before(self):
        """Diff against empty snapshot."""
        Path(self.tmp, "file.txt").write_text("x")
        diff = workspace_diff(self.config, {"files": [], "pip_packages": set()})
        self.assertIn("New files", diff)


if __name__ == "__main__":
    unittest.main()
