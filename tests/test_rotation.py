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
from rotation import rotate_if_needed, cleanup_old_archives, rotate_llm_log, cleanup_old_snapshots


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


class TestLLMLogRotation(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.config = Config()
        self.config.workspace_dir = os.path.join(self.tmp, "workspace")
        os.makedirs(self.config.workspace_dir)
        self.config.llm_log_max_bytes = 1000  # low threshold for testing
        self.config.llm_log_archive_count = 2

    def tearDown(self):
        import shutil
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_no_rotation_under_limit(self):
        log_path = Path(self.config.workspace_dir) / "llm_log.jsonl"
        log_path.write_text('{"small": true}\n')
        self.assertFalse(rotate_llm_log(self.config))

    def test_no_rotation_missing_file(self):
        self.assertFalse(rotate_llm_log(self.config))

    def test_rotation_over_limit(self):
        log_path = Path(self.config.workspace_dir) / "llm_log.jsonl"
        log_path.write_text("x" * 2000)
        self.assertTrue(rotate_llm_log(self.config))
        # Live file should be empty after rotation
        self.assertEqual(log_path.read_text(), "")
        # Archive should exist
        archives = list(Path(self.config.workspace_dir).glob("llm_log_*.jsonl.gz"))
        self.assertEqual(len(archives), 1)
        # Archive should contain original content
        with gzip.open(archives[0], "rt") as f:
            self.assertEqual(len(f.read()), 2000)

    def test_prune_excess_archives(self):
        log_path = Path(self.config.workspace_dir) / "llm_log.jsonl"
        ws = Path(self.config.workspace_dir)
        # Create 3 pre-existing archives
        for i in range(3):
            archive = ws / f"llm_log_2026010{i}_000000.jsonl.gz"
            with gzip.open(archive, "wt") as f:
                f.write(f"archive_{i}\n")
            # Stagger mtime so oldest is first
            os.utime(archive, (time.time() - (3 - i) * 3600, time.time() - (3 - i) * 3600))

        # Trigger rotation
        log_path.write_text("x" * 2000)
        rotate_llm_log(self.config)

        # Should have at most llm_log_archive_count archives
        archives = list(ws.glob("llm_log_*.jsonl.gz"))
        self.assertLessEqual(len(archives), self.config.llm_log_archive_count)


class TestSnapshotCleanup(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.config = Config()
        self.config.workspace_dir = os.path.join(self.tmp, "workspace")
        os.makedirs(self.config.workspace_dir)
        os.makedirs(str(self.config.snapshots_dir))
        self.config.snapshot_max_count = 3

    def tearDown(self):
        import shutil
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_no_cleanup_under_limit(self):
        for i in range(2):
            (self.config.snapshots_dir / f"memory_snapshot_{i}.md").write_text(f"snap {i}")
        deleted = cleanup_old_snapshots(self.config)
        self.assertEqual(deleted, 0)

    def test_cleanup_excess_snapshots(self):
        for i in range(6):
            snap = self.config.snapshots_dir / f"memory_snapshot_{i}.md"
            snap.write_text(f"snap {i}")
            os.utime(snap, (time.time() - (6 - i) * 60, time.time() - (6 - i) * 60))

        deleted = cleanup_old_snapshots(self.config)
        self.assertEqual(deleted, 3)
        remaining = list(self.config.snapshots_dir.glob("memory_snapshot_*"))
        self.assertEqual(len(remaining), 3)

    def test_no_dir_no_error(self):
        import shutil
        shutil.rmtree(str(self.config.snapshots_dir))
        deleted = cleanup_old_snapshots(self.config)
        self.assertEqual(deleted, 0)


if __name__ == "__main__":
    unittest.main()
