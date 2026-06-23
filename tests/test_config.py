"""Tests for config module."""

import os
import sys
import tempfile
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent))

import unittest
from unittest.mock import patch
from config import Config, load_config


class TestConfig(unittest.TestCase):

    def test_defaults(self):
        config = Config()
        self.assertEqual(config.llm_url, "http://127.0.0.1:8080")
        self.assertEqual(config.tick_interval_s, 5)
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
            self.assertGreater(float(config.tick_interval_s), 0)

    def test_env_var_override(self):
        os.environ["EIDOS_LLM_URL"] = "http://test:9999"
        try:
            config = load_config("/nonexistent/config.toml")
            self.assertEqual(config.llm_url, "http://test:9999")
        finally:
            del os.environ["EIDOS_LLM_URL"]

    def test_mock_mode_env(self):
        os.environ["EIDOS_MOCK"] = "1"
        try:
            config = load_config("/nonexistent/config.toml")
            self.assertTrue(config.mock_mode)
            self.assertEqual(config.tick_interval_s, 5)
        finally:
            del os.environ["EIDOS_MOCK"]

    def test_workspace_paths(self):
        config = Config()
        config.workspace_dir = "/tmp/test_workspace"
        self.assertEqual(config.goal_path, Path("/tmp/test_workspace/goal.md"))
        self.assertEqual(config.plan_path, Path("/tmp/test_workspace/plan.md"))
        self.assertEqual(config.observations_path, Path("/tmp/test_workspace/observations.jsonl"))
        self.assertEqual(config.wal_path, Path("/tmp/test_workspace/wal.json"))

    def test_self_healing_defaults(self):
        config = Config()
        self.assertEqual(config.llm_max_consecutive_failures, 5)

    def test_adaptive_token_defaults(self):
        config = Config()
        self.assertEqual(config.llm_max_tokens_ceiling, 4096)
        self.assertEqual(config.llm_token_backoff_step, 512)
        self.assertEqual(config.llm_reasoning_exhaust_compaction_trigger, 3)

    def test_log_rotation_defaults(self):
        config = Config()
        self.assertEqual(config.llm_log_max_bytes, 5_000_000)
        self.assertEqual(config.llm_log_archive_count, 3)
        self.assertEqual(config.snapshot_max_count, 20)

    def test_missing_toml_uses_defaults(self):
        config = load_config("/nonexistent/path.toml")
        self.assertEqual(config.llm_url, "http://127.0.0.1:8080")

    def test_rejects_invalid_dashboard_port(self):
        d = tempfile.mkdtemp()
        p = Path(d) / "config.toml"
        p.write_text("[dashboard]\nport = 70000\n", encoding="utf-8")
        with self.assertRaises(ValueError) as cm:
            load_config(str(p))
        self.assertIn("invalid", str(cm.exception))
        self.assertIn("dashboard.port", str(cm.exception))

    def test_rejects_unknown_safety_key(self):
        d = tempfile.mkdtemp()
        p = Path(d) / "config.toml"
        p.write_text("[safety]\ncmd_timeout_s = 5\nsurprise = true\n", encoding="utf-8")
        with self.assertRaises(ValueError) as cm:
            load_config(str(p))
        self.assertIn("safety.surprise", str(cm.exception))

    def test_rejects_bad_protected_pattern_regex(self):
        d = tempfile.mkdtemp()
        p = Path(d) / "config.toml"
        p.write_text('[safety]\nprotected_patterns = ["("]\n', encoding="utf-8")
        with self.assertRaises(ValueError) as cm:
            load_config(str(p))
        self.assertIn("protected_patterns", str(cm.exception))

    def test_rejects_coercible_but_wrong_toml_types(self):
        cases = [
            ("[dashboard]\nport = \"8099\"\n", "dashboard.port"),
            ("[safety]\ncmd_timeout_s = \"5\"\n", "safety.cmd_timeout_s"),
        ]
        for body, field in cases:
            with self.subTest(field=field):
                d = tempfile.mkdtemp()
                p = Path(d) / "config.toml"
                p.write_text(body, encoding="utf-8")
                with self.assertRaises(ValueError) as cm:
                    load_config(str(p))
                self.assertIn(field, str(cm.exception))

    def test_env_overrides_are_validated_by_settings_model(self):
        with patch.dict(os.environ, {"EIDOS_MOCK": "true"}, clear=False):
            config = load_config("/nonexistent/path.toml")
        self.assertTrue(config.mock_mode)

    def test_invalid_env_override_fails_closed(self):
        with patch.dict(os.environ, {"EIDOS_MOCK": "not-a-bool"}, clear=False):
            with self.assertRaises(ValueError) as cm:
                load_config("/nonexistent/path.toml")
        self.assertIn("environment", str(cm.exception))


if __name__ == "__main__":
    unittest.main()
