"""Tests for WAL crash recovery and self-healing in eidos.py."""

import json
import os
import sys
import tempfile
import time
from pathlib import Path
from unittest.mock import patch, MagicMock

sys.path.insert(0, str(Path(__file__).parent.parent))

import unittest
from config import Config
from eidos import write_wal, read_wal, clear_wal, recover
from memory import write_memory, append_observation, read_goal


class TestWAL(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.config = Config()
        self.config.workspace_dir = os.path.join(self.tmp, "workspace")
        os.makedirs(self.config.workspace_dir)
        os.makedirs(str(self.config.snapshots_dir))
        os.makedirs(str(self.config.interventions_dir))
        os.makedirs(str(self.config.outputs_dir))

    def tearDown(self):
        import shutil
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_write_and_read_wal(self):
        write_wal(self.config, tick_number=42, ticks_since_compaction=7,
                  goal_start_time=1000.0, consecutive_failures=2,
                  reasoning_exhaustions=1, current_max_tokens=1536)
        wal = read_wal(self.config)
        self.assertEqual(wal["tick_number"], 42)
        self.assertEqual(wal["ticks_since_compaction"], 7)
        self.assertEqual(wal["goal_start_time"], 1000.0)
        self.assertEqual(wal["consecutive_failures"], 2)
        self.assertEqual(wal["reasoning_exhaustions"], 1)
        self.assertEqual(wal["current_max_tokens"], 1536)
        self.assertIn("ts", wal)

    def test_read_wal_missing_returns_empty(self):
        wal = read_wal(self.config)
        self.assertEqual(wal, {})

    def test_read_wal_corrupt_returns_empty(self):
        self.config.wal_path.write_text("not json{{{")
        wal = read_wal(self.config)
        self.assertEqual(wal, {})

    def test_clear_wal_removes_file(self):
        write_wal(self.config, 1, 0, time.time())
        self.assertTrue(self.config.wal_path.exists())
        clear_wal(self.config)
        self.assertFalse(self.config.wal_path.exists())

    def test_clear_wal_missing_no_error(self):
        clear_wal(self.config)  # should not raise

    def test_wal_atomic_write(self):
        """WAL write uses tmp+rename for atomicity."""
        write_wal(self.config, 10, 3, 500.0)
        # No .tmp file should remain
        self.assertFalse(self.config.wal_path.with_suffix(".tmp").exists())
        # WAL should be valid JSON
        data = json.loads(self.config.wal_path.read_text())
        self.assertEqual(data["tick_number"], 10)


class TestRecoverWithWAL(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.config = Config()
        self.config.workspace_dir = os.path.join(self.tmp, "workspace")
        os.makedirs(self.config.workspace_dir)
        os.makedirs(str(self.config.snapshots_dir))
        os.makedirs(str(self.config.interventions_dir))
        os.makedirs(str(self.config.outputs_dir))

    def tearDown(self):
        import shutil
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_recover_returns_wal_state(self):
        write_wal(self.config, tick_number=15, ticks_since_compaction=5,
                  goal_start_time=2000.0, consecutive_failures=1)
        wal = recover(self.config)
        self.assertEqual(wal["tick_number"], 15)
        self.assertEqual(wal["ticks_since_compaction"], 5)
        self.assertEqual(wal["consecutive_failures"], 1)

    def test_recover_no_wal_returns_empty(self):
        wal = recover(self.config)
        self.assertEqual(wal, {})

    def test_recover_creates_memory_if_missing(self):
        recover(self.config)
        self.assertTrue(self.config.memory_path.exists())

    def test_recover_preserves_existing_memory(self):
        write_memory(self.config, "important memory content")
        recover(self.config)
        self.assertEqual(self.config.memory_path.read_text(), "important memory content")



if __name__ == "__main__":
    unittest.main()
