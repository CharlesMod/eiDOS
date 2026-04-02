"""Tests for context assembly."""

import json
import os
import sys
import tempfile
import time
from pathlib import Path
from unittest.mock import patch
sys.path.insert(0, str(Path(__file__).parent.parent))

import unittest
from config import Config
from context import assemble_context, _format_elapsed, _truncate, estimate_tokens, _log_overrun
from memory import write_memory, append_observation


class TestContextAssembly(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.config = Config()
        self.config.workspace_dir = os.path.join(self.tmp, "workspace")
        os.makedirs(self.config.workspace_dir)
        os.makedirs(str(self.config.interventions_dir))
        os.makedirs(str(self.config.snapshots_dir))
        os.makedirs(str(self.config.outputs_dir))

        # Set up minimal state
        self.config.goal_path.write_text("Test goal: do something useful.")
        write_memory(self.config, "Working memory content.")

    def tearDown(self):
        import shutil
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_message_order(self):
        messages = assemble_context(self.config, tick_number=1, goal_start_time=time.time())
        self.assertEqual(len(messages), 3)  # system, user (sections), tick prompt
        self.assertEqual(messages[0]["role"], "system")
        self.assertEqual(messages[1]["role"], "user")
        self.assertEqual(messages[2]["role"], "user")

    def test_goal_in_context(self):
        messages = assemble_context(self.config, tick_number=1, goal_start_time=time.time())
        content = messages[1]["content"]
        self.assertIn("Test goal", content)

    def test_memory_in_context(self):
        messages = assemble_context(self.config, tick_number=1, goal_start_time=time.time())
        content = messages[1]["content"]
        self.assertIn("Working memory content", content)

    def test_observations_in_context(self):
        append_observation(self.config, {"tick": 1, "tool": "bash", "success": True, "output": "obs_marker"})
        messages = assemble_context(self.config, tick_number=2, goal_start_time=time.time())
        content = messages[1]["content"]
        self.assertIn("obs_marker", content)

    def test_loop_detection_warning(self):
        messages = assemble_context(
            self.config, tick_number=5, goal_start_time=time.time(),
            loop_detected=True, repeat_count=3,
        )
        tick_msg = messages[2]["content"]
        self.assertIn("repeated the same action", tick_msg.lower())

    def test_no_loop_warning_normally(self):
        messages = assemble_context(self.config, tick_number=1, goal_start_time=time.time())
        tick_msg = messages[2]["content"]
        self.assertNotIn("repeated", tick_msg.lower())

    def test_system_prompt_has_tools(self):
        messages = assemble_context(self.config, tick_number=1, goal_start_time=time.time())
        system = messages[0]["content"]
        self.assertIn("bash", system)
        self.assertIn("<tool>", system)


class TestFormatElapsed(unittest.TestCase):

    def test_seconds(self):
        self.assertEqual(_format_elapsed(30), "30s ago")

    def test_minutes(self):
        self.assertEqual(_format_elapsed(120), "2m ago")

    def test_hours(self):
        self.assertEqual(_format_elapsed(3660), "1h 1m ago")

    def test_days(self):
        self.assertEqual(_format_elapsed(90000), "1d 1h ago")


# -------------------------------------------------------------------
# Context budget enforcement
# -------------------------------------------------------------------

_CANNED_ENV = "=== Environment ===\nTime: test\nUptime: test\nDisk: 50 GB\nRAM: ok\nJobs: none"


class TestContextBudgets(unittest.TestCase):
    """Verify per-section truncation, overrun logging, and total budget cap."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.config = Config()
        self.config.workspace_dir = os.path.join(self.tmp, "workspace")
        os.makedirs(self.config.workspace_dir)
        os.makedirs(str(self.config.interventions_dir))
        os.makedirs(str(self.config.snapshots_dir))
        os.makedirs(str(self.config.outputs_dir))
        self.config.goal_path.write_text("Short goal.")
        write_memory(self.config, "Short memory.")

    def tearDown(self):
        import shutil
        shutil.rmtree(self.tmp, ignore_errors=True)

    # --- Helper ---

    def _assemble(self, **config_overrides):
        for k, v in config_overrides.items():
            setattr(self.config, k, v)
        with patch("context.generate_env_snapshot", return_value=_CANNED_ENV):
            return assemble_context(self.config, tick_number=1,
                                     goal_start_time=time.time())

    # --- _truncate ---

    def test_truncate_short_text_unchanged(self):
        self.assertEqual(_truncate("hello", 100, "x"), "hello")

    def test_truncate_long_text(self):
        text = "A" * 500
        result = _truncate(text, 100, "goal")
        self.assertTrue(result.startswith("A" * 100))
        self.assertIn("truncated", result)
        self.assertIn("500 chars exceeded 100", result)

    # --- estimate_tokens ---

    def test_estimate_tokens(self):
        self.assertEqual(estimate_tokens("x" * 350, 3.5), 100)

    # --- Goal truncation ---

    def test_goal_truncated_when_over_budget(self):
        big_goal = "G" * 5000
        self.config.goal_path.write_text(big_goal)
        messages = self._assemble(context_goal_max_chars=200)
        content = messages[1]["content"]
        # Goal section should not contain the full 5000 chars
        goal_section = content.split("## Goal\n")[1].split("\n\n##")[0]
        self.assertLess(len(goal_section), 5000)
        self.assertIn("truncated", goal_section)

    def test_goal_not_truncated_when_under_budget(self):
        messages = self._assemble(context_goal_max_chars=2000)
        content = messages[1]["content"]
        self.assertNotIn("truncated", content.split("## Goal")[1].split("##")[0])

    # --- Memory truncation ---

    def test_memory_truncated_when_over_budget(self):
        write_memory(self.config, "M" * 8000)
        messages = self._assemble(context_memory_max_chars=500)
        content = messages[1]["content"]
        mem_section = content.split("## Working Memory\n")[1].split("\n\n##")[0]
        self.assertLess(len(mem_section), 8000)
        self.assertIn("truncated", mem_section)

    # --- Env truncation ---

    def test_env_truncated_when_over_budget(self):
        big_env = "E" * 2000
        with patch("context.generate_env_snapshot", return_value=big_env):
            messages = assemble_context(self.config, tick_number=1,
                                         goal_start_time=time.time())
        content = messages[1]["content"]
        env_section = content.split("## Environment\n")[1].split("\n\n##")[0]
        # Default env budget is 800
        self.assertIn("truncated", env_section)

    # --- Interventions truncation ---

    def test_interventions_truncated_when_over_budget(self):
        # Write a big intervention file
        (self.config.interventions_dir / "huge.txt").write_text("I" * 5000)
        messages = self._assemble(context_interventions_max_chars=300)
        content = messages[1]["content"]
        self.assertIn("truncated", content)

    # --- Observations truncation ---

    def test_observations_truncated_when_over_budget(self):
        for i in range(100):
            append_observation(self.config, {
                "tick": i, "tool": "bash", "success": True,
                "output": "x" * 200,
            })
        messages = self._assemble(context_obs_max_chars=500, context_obs_max_count=100)
        content = messages[1]["content"]
        if "## Recent Observations" in content:
            obs_section = content.split("## Recent Observations")[1]
            self.assertLess(len(obs_section), 25000)  # bounded, not unbounded

    # --- Overrun logging ---

    def test_overrun_logged_to_file(self):
        big_goal = "G" * 5000
        self.config.goal_path.write_text(big_goal)
        self._assemble(context_goal_max_chars=200)
        overrun_path = self.config.workspace / "ctx_overruns.jsonl"
        self.assertTrue(overrun_path.exists(), "ctx_overruns.jsonl should be created")
        lines = overrun_path.read_text().strip().splitlines()
        self.assertGreater(len(lines), 0)
        entry = json.loads(lines[0])
        self.assertEqual(entry["section"], "goal")
        self.assertEqual(entry["actual_chars"], 5000)
        self.assertEqual(entry["budget_chars"], 200)
        self.assertGreater(entry["overage_chars"], 0)

    def test_total_overrun_logged(self):
        # Set a tiny total budget so the normal context exceeds it
        self._assemble(context_max_total_chars=100)
        overrun_path = self.config.workspace / "ctx_overruns.jsonl"
        self.assertTrue(overrun_path.exists())
        entries = [json.loads(l) for l in overrun_path.read_text().strip().splitlines()]
        total_entries = [e for e in entries if e["section"] == "TOTAL"]
        self.assertGreater(len(total_entries), 0)

    def test_no_overrun_file_when_under_budget(self):
        self._assemble(context_max_total_chars=100000,
                       context_goal_max_chars=10000,
                       context_memory_max_chars=10000)
        overrun_path = self.config.workspace / "ctx_overruns.jsonl"
        self.assertFalse(overrun_path.exists(),
                         "No overrun file when everything fits in budget")

    # --- Total context stays bounded ---

    def test_total_context_bounded_with_all_sections_huge(self):
        """Even with enormous inputs, the assembled context respects per-section caps."""
        self.config.goal_path.write_text("G" * 10000)
        write_memory(self.config, "M" * 10000)
        for i in range(200):
            append_observation(self.config, {
                "tick": i, "tool": "bash", "success": True,
                "output": "O" * 500,
            })
        (self.config.interventions_dir / "big.txt").write_text("I" * 5000)

        messages = self._assemble(
            context_goal_max_chars=500,
            context_memory_max_chars=500,
            context_env_max_chars=300,
            context_interventions_max_chars=300,
            context_obs_max_chars=500,
            context_obs_max_count=5,
        )
        total = sum(len(m["content"]) for m in messages)
        # Should be well under 10K with these tight budgets
        self.assertLess(total, 10000,
                        f"Total context {total} should be bounded by per-section caps")


if __name__ == "__main__":
    unittest.main()
