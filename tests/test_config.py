"""Tests for config module."""

import os
import sys
import tempfile
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent))

import unittest
from config import Config, load_config


class TestConfig(unittest.TestCase):

    def test_defaults(self):
        config = Config()
        self.assertEqual(config.llm_url, "http://127.0.0.1:8080")
        self.assertEqual(config.tick_interval_s, 300)
        self.assertEqual(config.cmd_timeout_s, 120)
        self.assertGreater(len(config.protected_patterns), 0)

    def test_load_from_toml(self):
        # Load the actual config.toml
        project_root = Path(__file__).parent.parent
        config_path = project_root / "config.toml"
        if config_path.exists():
            config = load_config(str(config_path))
            # Just verify it loads without error and overrides defaults
            self.assertIsInstance(config.llm_url, str)
            self.assertTrue(config.llm_url.startswith("http"))
            self.assertEqual(config.tick_interval_s, 300)

    def test_env_var_override(self):
        os.environ["KAIROS_LLM_URL"] = "http://test:9999"
        try:
            config = load_config("/nonexistent/config.toml")
            self.assertEqual(config.llm_url, "http://test:9999")
        finally:
            del os.environ["KAIROS_LLM_URL"]

    def test_mock_mode_env(self):
        os.environ["KAIROS_MOCK"] = "1"
        try:
            config = load_config("/nonexistent/config.toml")
            self.assertTrue(config.mock_mode)
            self.assertEqual(config.tick_interval_s, 5)
        finally:
            del os.environ["KAIROS_MOCK"]

    def test_workspace_paths(self):
        config = Config()
        config.workspace_dir = "/tmp/test_workspace"
        self.assertEqual(config.goal_path, Path("/tmp/test_workspace/goal.md"))
        self.assertEqual(config.memory_path, Path("/tmp/test_workspace/memory.md"))
        self.assertEqual(config.observations_path, Path("/tmp/test_workspace/observations.jsonl"))
        self.assertEqual(config.wal_path, Path("/tmp/test_workspace/wal.json"))

    def test_self_healing_defaults(self):
        config = Config()
        self.assertEqual(config.llm_restart_cmd, "")
        self.assertEqual(config.llm_max_consecutive_failures, 5)

    def test_log_rotation_defaults(self):
        config = Config()
        self.assertEqual(config.llm_log_max_bytes, 5_000_000)
        self.assertEqual(config.llm_log_archive_count, 3)
        self.assertEqual(config.snapshot_max_count, 20)

    def test_missing_toml_uses_defaults(self):
        config = load_config("/nonexistent/path.toml")
        self.assertEqual(config.llm_url, "http://127.0.0.1:8080")


if __name__ == "__main__":
    unittest.main()
