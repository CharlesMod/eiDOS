"""Tests for compaction module."""

import os
import sys
import tempfile
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent))

import unittest
from unittest.mock import patch
from config import Config
from memory import write_memory, append_observation, read_memory
from compaction import should_compact, compact, _snapshot_memory, _format_observations


class TestCompactionTriggers(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.config = Config()
        self.config.workspace_dir = os.path.join(self.tmp, "workspace")
        os.makedirs(self.config.workspace_dir)
        os.makedirs(str(self.config.snapshots_dir))
        self.config.compaction_token_threshold = 500
        self.config.compaction_tick_threshold = 10

    def tearDown(self):
        import shutil
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_no_compact_initially(self):
        self.assertFalse(should_compact(self.config, ticks_since_last=0))

    def test_compact_by_tick_count(self):
        self.assertTrue(should_compact(self.config, ticks_since_last=10))

    def test_compact_by_token_threshold(self):
        # Write enough observations to exceed threshold
        for i in range(50):
            append_observation(self.config, {"tick": i, "output": "x" * 50})
        self.assertTrue(should_compact(self.config, ticks_since_last=0))

    def test_snapshot_created(self):
        write_memory(self.config, "test memory before compaction")
        _snapshot_memory(self.config)
        snapshots = list(self.config.snapshots_dir.glob("memory_before_*.md"))
        self.assertEqual(len(snapshots), 1)
        self.assertEqual(snapshots[0].read_text(), "test memory before compaction")

    def test_snapshot_empty_memory(self):
        _snapshot_memory(self.config)
        snapshots = list(self.config.snapshots_dir.glob("memory_before_*.md"))
        self.assertEqual(len(snapshots), 0)  # No snapshot for empty memory

    def test_format_observations(self):
        obs = [
            {"ts": "2026-04-01T10:00:00Z", "tick": 1, "tool": "bash", "success": True, "output": "hello"},
            {"ts": "2026-04-01T10:01:00Z", "tick": 2, "tool": "bash", "success": False, "output": "error"},
        ]
        formatted = _format_observations(obs)
        self.assertIn("tick 1", formatted)
        self.assertIn("OK", formatted)
        self.assertIn("FAIL", formatted)

    def test_format_observations_truncates_long_output(self):
        obs = [{"ts": "now", "tick": 1, "tool": "bash", "success": True, "output": "x" * 1000}]
        formatted = _format_observations(obs)
        self.assertIn("...", formatted)
        self.assertLess(len(formatted), 600)

    @patch("compaction.complete", return_value="")
    def test_compact_empty_llm_output_preserves_existing_memory(self, _mock_complete):
        write_memory(self.config, "important prior memory")
        append_observation(self.config, {"tick": 1, "tool": "bash", "success": True, "output": "did work"})

        compact(self.config)

        self.assertEqual(read_memory(self.config), "important prior memory")

    @patch("compaction.complete", return_value="condensed memory")
    def test_compact_writes_new_memory_when_llm_returns_content(self, _mock_complete):
        write_memory(self.config, "old memory")
        append_observation(self.config, {"tick": 1, "tool": "bash", "success": True, "output": "did work"})

        compact(self.config)

        self.assertEqual(read_memory(self.config), "condensed memory")


if __name__ == "__main__":
    unittest.main()
