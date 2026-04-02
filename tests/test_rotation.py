"""Tests for log rotation."""

import gzip
import json
import os
import sys
import tempfile
import time
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent))

import unittest
from config import Config
from memory import append_observation
from rotation import rotate_if_needed, cleanup_old_archives


class TestRotation(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.config = Config()
        self.config.workspace_dir = os.path.join(self.tmp, "workspace")
        os.makedirs(self.config.workspace_dir)
        self.config.obs_max_lines = 10

    def tearDown(self):
        import shutil
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_no_rotation_under_limit(self):
        for i in range(5):
            append_observation(self.config, {"tick": i, "output": f"line_{i}"})
        rotated = rotate_if_needed(self.config)
        self.assertFalse(rotated)

    def test_rotation_over_limit(self):
        for i in range(20):
            append_observation(self.config, {"tick": i, "output": f"line_{i}"})
        rotated = rotate_if_needed(self.config)
        self.assertTrue(rotated)

        # Live file should have obs_max_lines
        with open(self.config.observations_path) as f:
            lines = f.readlines()
        self.assertEqual(len(lines), 10)

        # Archive should exist
        archives = list(Path(self.config.workspace_dir).glob("observations_archive_*.jsonl.gz"))
        self.assertEqual(len(archives), 1)

        # Archive should contain the older lines
        with gzip.open(archives[0], "rt") as f:
            archive_lines = f.readlines()
        self.assertEqual(len(archive_lines), 10)

    def test_no_file_no_rotation(self):
        rotated = rotate_if_needed(self.config)
        self.assertFalse(rotated)

    def test_cleanup_old_archives(self):
        # Create a fake old archive
        old_archive = Path(self.config.workspace_dir) / "observations_archive_20200101_000000.jsonl.gz"
        with gzip.open(old_archive, "wt") as f:
            f.write('{"old": true}\n')
        # Set mtime to long ago
        old_time = time.time() - (30 * 86400)
        os.utime(old_archive, (old_time, old_time))

        self.config.obs_archive_days = 14
        deleted = cleanup_old_archives(self.config)
        self.assertEqual(deleted, 1)
        self.assertFalse(old_archive.exists())


if __name__ == "__main__":
    unittest.main()
