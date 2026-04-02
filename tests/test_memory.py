"""Tests for memory module."""

import json
import os
import sys
import tempfile
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent))

import unittest
from config import Config
from memory import (
    read_goal,
    read_memory,
    write_memory,
    append_observation,
    read_recent_observations,
    validate_observations,
    read_interventions,
)


class TestMemory(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.config = Config()
        self.config.workspace_dir = os.path.join(self.tmp, "workspace")
        os.makedirs(self.config.workspace_dir)
        os.makedirs(str(self.config.interventions_dir))

    def tearDown(self):
        import shutil
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_read_goal_missing(self):
        self.assertEqual(read_goal(self.config), "")

    def test_read_goal_exists(self):
        self.config.goal_path.write_text("Find the answer.")
        self.assertEqual(read_goal(self.config), "Find the answer.")

    def test_write_read_memory(self):
        write_memory(self.config, "test memory content")
        self.assertEqual(read_memory(self.config), "test memory content")

    def test_atomic_write(self):
        # Write should not leave temp files on success
        write_memory(self.config, "content")
        tmp_files = list(Path(self.config.workspace_dir).glob(".memory_*"))
        self.assertEqual(len(tmp_files), 0)

    def test_append_observation(self):
        append_observation(self.config, {"tick": 1, "tool": "bash", "output": "hello"})
        append_observation(self.config, {"tick": 2, "tool": "bash", "output": "world"})
        with open(self.config.observations_path) as f:
            lines = f.readlines()
        self.assertEqual(len(lines), 2)
        entry = json.loads(lines[0])
        self.assertEqual(entry["tick"], 1)
        self.assertIn("ts", entry)  # timestamp auto-added

    def test_read_recent_newest_first(self):
        for i in range(5):
            append_observation(self.config, {"tick": i, "tool": "bash", "output": f"out_{i}"})
        obs = read_recent_observations(self.config, max_chars=10000, max_count=3)
        self.assertEqual(len(obs), 3)
        self.assertEqual(obs[0]["tick"], 4)  # newest first

    def test_read_recent_char_budget(self):
        for i in range(20):
            append_observation(self.config, {"tick": i, "tool": "bash", "output": "x" * 100})
        obs = read_recent_observations(self.config, max_chars=500, max_count=100)
        total = sum(len(json.dumps(o)) for o in obs)
        # Should be under budget (roughly)
        self.assertLess(len(obs), 20)

    def test_validate_good_file(self):
        append_observation(self.config, {"tick": 1, "output": "ok"})
        truncated = validate_observations(self.config)
        self.assertEqual(truncated, 0)

    def test_validate_malformed_last_line(self):
        with open(self.config.observations_path, "w") as f:
            f.write('{"tick": 1}\n')
            f.write('{"tick": 2}\n')
            f.write('{"tick": 3, broken\n')
        truncated = validate_observations(self.config)
        self.assertEqual(truncated, 1)
        with open(self.config.observations_path) as f:
            lines = f.readlines()
        self.assertEqual(len(lines), 2)

    def test_interventions(self):
        # Write an intervention file
        (self.config.interventions_dir / "001_hint.md").write_text("Try a different API endpoint")
        interventions = read_interventions(self.config)
        self.assertEqual(len(interventions), 1)
        self.assertIn("different API", interventions[0]["content"])
        # File should be renamed to .done
        self.assertTrue((self.config.interventions_dir / "001_hint.md.done").exists())

    def test_interventions_skip_done(self):
        (self.config.interventions_dir / "old.md.done").write_text("already consumed")
        interventions = read_interventions(self.config)
        self.assertEqual(len(interventions), 0)


if __name__ == "__main__":
    unittest.main()
