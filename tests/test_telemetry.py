"""Tests for telemetry module."""

import json
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent))

import unittest
import tempfile
import shutil
from config import Config
from telemetry import write_heartbeat, append_metrics


class TestHeartbeat(unittest.TestCase):

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.config = Config(workspace_dir=self.tmpdir)

    def tearDown(self):
        shutil.rmtree(self.tmpdir)

    def test_write_heartbeat_creates_file(self):
        write_heartbeat(self.config, tick=1, level=2, mood="focused",
                        xp=50, goal_snippet="test goal", consecutive_failures=0,
                        current_max_tokens=1024, disk_free_gb=10.0,
                        ram_pct=45.0, llm_elapsed_s=12.5,
                        tool_name="bash", tool_success=True, uptime_s=600.0)

        hb_path = Path(self.tmpdir) / "heartbeat.json"
        self.assertTrue(hb_path.exists())
        data = json.loads(hb_path.read_text())
        self.assertEqual(data["tick"], 1)
        self.assertEqual(data["level"], 2)
        self.assertEqual(data["mood"], "focused")
        self.assertEqual(data["tool_name"], "bash")
        self.assertIn("ts", data)
        self.assertIn("iso", data)
        self.assertNotIn("idle_since", data)

    def test_heartbeat_with_idle_since(self):
        write_heartbeat(self.config, tick=5, level=1, mood="curious",
                        xp=0, goal_snippet="", consecutive_failures=0,
                        current_max_tokens=1024, disk_free_gb=8.0,
                        ram_pct=30.0, llm_elapsed_s=0,
                        tool_name="", tool_success=False, uptime_s=100.0,
                        idle_since=1700000000.0)

        data = json.loads((Path(self.tmpdir) / "heartbeat.json").read_text())
        self.assertEqual(data["idle_since"], 1700000000.0)

    def test_heartbeat_goal_truncation(self):
        long_goal = "x" * 200
        write_heartbeat(self.config, tick=1, level=1, mood="focused",
                        xp=0, goal_snippet=long_goal, consecutive_failures=0,
                        current_max_tokens=1024, disk_free_gb=5.0,
                        ram_pct=50.0, llm_elapsed_s=10.0,
                        tool_name="bash", tool_success=True, uptime_s=10.0)

        data = json.loads((Path(self.tmpdir) / "heartbeat.json").read_text())
        self.assertEqual(len(data["goal_snippet"]), 80)


class TestMetrics(unittest.TestCase):

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.config = Config(workspace_dir=self.tmpdir)

    def tearDown(self):
        shutil.rmtree(self.tmpdir)

    def _append(self, tick=1):
        append_metrics(self.config, tick=tick, level=1, mood="focused",
                       xp=10, consecutive_failures=0, current_max_tokens=1024,
                       disk_free_gb=10.0, ram_pct=40.0, llm_elapsed_s=5.0, tool_name="bash", tool_success=True,
                       uptime_s=300.0, ctx_chars=5000, obs_count=15)

    def test_append_creates_file(self):
        self._append()
        path = Path(self.tmpdir) / "metrics.jsonl"
        self.assertTrue(path.exists())
        lines = path.read_text().strip().splitlines()
        self.assertEqual(len(lines), 1)
        data = json.loads(lines[0])
        self.assertEqual(data["tick"], 1)
        self.assertEqual(data["ctx_chars"], 5000)

    def test_append_is_additive(self):
        self._append(tick=1)
        self._append(tick=2)
        self._append(tick=3)
        path = Path(self.tmpdir) / "metrics.jsonl"
        lines = path.read_text().strip().splitlines()
        self.assertEqual(len(lines), 3)


if __name__ == "__main__":
    unittest.main()
