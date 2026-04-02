"""Tests for session detection."""

import os
import sys
import tempfile
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent))

import unittest
from config import Config
from session import human_present, take_workspace_snapshot, _list_workspace_files


class TestSessionDetection(unittest.TestCase):

    def test_human_present_returns_bool(self):
        result = human_present()
        self.assertIsInstance(result, bool)

    def test_workspace_snapshot(self):
        tmp = tempfile.mkdtemp()
        config = Config()
        config.workspace_dir = tmp

        # Create some files
        Path(tmp, "test.txt").write_text("hello")
        Path(tmp, "data.json").write_text("{}")

        snapshot = take_workspace_snapshot(config)
        self.assertIn("ts", snapshot)
        self.assertIn("files", snapshot)
        self.assertIn("test.txt", snapshot["files"])
        self.assertIn("data.json", snapshot["files"])

        import shutil
        shutil.rmtree(tmp, ignore_errors=True)

    def test_list_workspace_files_empty(self):
        tmp = tempfile.mkdtemp()
        config = Config()
        config.workspace_dir = tmp
        files = _list_workspace_files(config)
        self.assertEqual(files, [])
        import shutil
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    unittest.main()
