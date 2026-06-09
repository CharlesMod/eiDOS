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
from context import (
    assemble_context, _format_elapsed, _truncate, estimate_tokens, _log_overrun,
    _render_observations_pyramid, _build_intelligence_section,
)
from memory import write_memory, write_plan, append_observation


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


# -------------------------------------------------------------------
# Inverted-pyramid observation rendering
# -------------------------------------------------------------------

class TestInvertedPyramid(unittest.TestCase):

    def test_single_observation_full_detail(self):
        obs = [{"tick": 5, "tool": "bash", "success": True, "output": "hello world"}]
        text = _render_observations_pyramid(obs)
        self.assertIn("[tick 5 | bash | OK]", text)
        self.assertIn("hello world", text)

    def test_two_observations_second_compressed(self):
        obs = [
            {"tick": 5, "tool": "bash", "success": True, "output": "recent full output"},
            {"tick": 4, "tool": "read_file", "success": True, "output": "line1\nline2\nline3"},
        ]
        text = _render_observations_pyramid(obs)
        # First: full
        self.assertIn("recent full output", text)
        # Second: one-line summary only
        self.assertIn("[tick 4 | read_file | OK] line1", text)
        self.assertNotIn("line2", text)

    def test_three_observations_third_minimal(self):
        obs = [
            {"tick": 5, "tool": "bash", "success": True, "output": "latest"},
            {"tick": 4, "tool": "bash", "success": True, "output": "previous"},
            {"tick": 3, "tool": "write_file", "success": False, "output": "error details"},
        ]
        text = _render_observations_pyramid(obs)
        # Third: outcome only, no output
        self.assertIn("[tick 3 | write_file | FAIL]", text)
        self.assertNotIn("error details", text)

    def test_four_observations_fourth_dropped(self):
        obs = [
            {"tick": 5, "tool": "bash", "success": True, "output": "a"},
            {"tick": 4, "tool": "bash", "success": True, "output": "b"},
            {"tick": 3, "tool": "bash", "success": True, "output": "c"},
            {"tick": 2, "tool": "bash", "success": True, "output": "SHOULD NOT APPEAR"},
        ]
        text = _render_observations_pyramid(obs)
        self.assertNotIn("SHOULD NOT APPEAR", text)
        self.assertNotIn("tick 2", text)

    def test_empty_observations(self):
        self.assertEqual(_render_observations_pyramid([]), "")

    def test_full_budget_truncates_first_obs(self):
        obs = [{"tick": 5, "tool": "bash", "success": True, "output": "x" * 5000}]
        text = _render_observations_pyramid(obs, full_budget=100)
        # Output should be truncated to ~100 chars
        self.assertLess(len(text), 200)


# -------------------------------------------------------------------
# Briefing model context assembly
# -------------------------------------------------------------------

_CANNED_ENV_ALERTS = ""  # Normal — no alerts
_CANNED_ENV_WITH_ALERT = "⚠ Alerts: ⚠ DISK LOW: 0.5 GB free / 32.0 GB (threshold 1.0 GB)"


class TestBriefingModel(unittest.TestCase):
    """Test the briefing model context assembly."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.config = Config()
        self.config.workspace_dir = os.path.join(self.tmp, "workspace")
        self.config.briefing_model = True
        self.config.knowledge_enabled = False  # no knowledge store in basic tests
        os.makedirs(self.config.workspace_dir)
        os.makedirs(str(self.config.interventions_dir))
        os.makedirs(str(self.config.snapshots_dir))
        os.makedirs(str(self.config.outputs_dir))

        self.config.goal_path.write_text("Test goal: deploy monitoring.")
        write_plan(self.config, "# Plan\nStep 1: install packages")

    def tearDown(self):
        import shutil
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _assemble(self, **config_overrides):
        for k, v in config_overrides.items():
            setattr(self.config, k, v)
        with patch("context.generate_env_alerts", return_value=_CANNED_ENV_ALERTS):
            return assemble_context(self.config, tick_number=1,
                                     goal_start_time=time.time())

    def test_message_structure(self):
        messages = self._assemble()
        self.assertEqual(len(messages), 3)
        self.assertEqual(messages[0]["role"], "system")
        self.assertEqual(messages[1]["role"], "user")
        self.assertEqual(messages[2]["role"], "user")

    def test_uses_compressed_system_prompt(self):
        messages = self._assemble()
        system = messages[0]["content"]
        # Briefing prompt is shorter than legacy
        self.assertLess(len(system), 1800)
        self.assertIn("eiDOS", system)
        self.assertIn("memorize", system)
        self.assertIn("recall", system)

    def test_mission_section(self):
        messages = self._assemble()
        content = messages[1]["content"]
        self.assertIn("## Mission", content)
        self.assertIn("deploy monitoring", content)

    def test_plan_section(self):
        messages = self._assemble()
        content = messages[1]["content"]
        self.assertIn("## Plan", content)
        self.assertIn("install packages", content)

    def test_plan_fallback_to_memory(self):
        """When plan.md is missing, briefing model reads memory.md."""
        os.unlink(str(self.config.plan_path))
        write_memory(self.config, "legacy memory content")
        messages = self._assemble()
        content = messages[1]["content"]
        self.assertIn("## Plan", content)
        self.assertIn("legacy memory content", content)

    def test_no_env_when_normal(self):
        """Alert-only env produces no Environment section when all is well."""
        messages = self._assemble()
        content = messages[1]["content"]
        self.assertNotIn("## Environment", content)

    def test_env_alerts_shown(self):
        with patch("context.generate_env_alerts", return_value=_CANNED_ENV_WITH_ALERT):
            messages = assemble_context(self.config, tick_number=1,
                                         goal_start_time=time.time())
        content = messages[1]["content"]
        self.assertIn("## Environment", content)
        self.assertIn("DISK LOW", content)

    def test_observations_inverted_pyramid(self):
        for i in range(5):
            append_observation(self.config, {
                "tick": i, "tool": "bash", "success": True,
                "output": f"output_for_tick_{i}"
            })
        messages = self._assemble()
        content = messages[1]["content"]
        # Latest (tick 4) should have full output
        self.assertIn("output_for_tick_4", content)
        # tick 0 and tick 1 should be dropped
        self.assertNotIn("output_for_tick_0", content)
        self.assertNotIn("output_for_tick_1", content)

    def test_plan_budget_enforced(self):
        write_plan(self.config, "X" * 5000)
        messages = self._assemble(context_plan_max_chars=200)
        content = messages[1]["content"]
        plan_section = content.split("## Plan\n")[1].split("\n\n##")[0]
        self.assertIn("truncated", plan_section)
        self.assertLess(len(plan_section), 5000)

    def test_total_budget_bounded(self):
        self.config.goal_path.write_text("G" * 3000)
        write_plan(self.config, "P" * 3000)
        for i in range(20):
            append_observation(self.config, {
                "tick": i, "tool": "bash", "success": True,
                "output": "O" * 500,
            })
        messages = self._assemble(
            context_goal_max_chars=500,
            context_plan_max_chars=300,
            context_obs_max_chars=500,
            context_max_total_chars=3000,
        )
        total = sum(len(m["content"]) for m in messages)
        self.assertLessEqual(total, 3100)  # small overrun from trim notice OK

    def test_no_plan_no_section(self):
        """When neither plan.md nor memory.md exists, no Plan section appears."""
        os.unlink(str(self.config.plan_path))
        messages = self._assemble()
        content = messages[1]["content"]
        self.assertNotIn("## Plan", content)

    def test_interventions_in_briefing(self):
        (self.config.interventions_dir / "001_hint.md").write_text("Check the logs")
        messages = self._assemble()
        content = messages[1]["content"]
        self.assertIn("## Chat with supervisor", content)
        self.assertIn("Check the logs", content)

    def test_loop_detection_in_briefing(self):
        with patch("context.generate_env_alerts", return_value=""):
            messages = assemble_context(
                self.config, tick_number=5, goal_start_time=time.time(),
                loop_detected=True, repeat_count=3,
            )
        tick_msg = messages[2]["content"]
        self.assertIn("repeated the same action", tick_msg.lower())



# -------------------------------------------------------------------
# Briefing model with knowledge store
# -------------------------------------------------------------------

class TestBriefingIntelligence(unittest.TestCase):
    """Test passive knowledge recall in the briefing model."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.config = Config()
        self.config.workspace_dir = os.path.join(self.tmp, "workspace")
        self.config.briefing_model = True
        self.config.knowledge_enabled = True
        os.makedirs(self.config.workspace_dir)
        os.makedirs(str(self.config.interventions_dir))
        os.makedirs(str(self.config.snapshots_dir))
        os.makedirs(str(self.config.outputs_dir))

        self.config.goal_path.write_text("Install pip packages on Raspberry Pi")
        write_plan(self.config, "# Plan\nNeed to install packages with pip")

    def tearDown(self):
        import shutil
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_intelligence_section_populated(self, knowledge_fixture=None):
        """When knowledge store has entries, Intelligence section appears."""
        # Copy fixture knowledge into our workspace
        import shutil as sh
        fixture_src = Path(__file__).parent / "fixtures" / "knowledge"
        knowledge_dst = self.config.knowledge_dir
        sh.copytree(str(fixture_src), str(knowledge_dst))

        # Reset knowledge module caches
        import knowledge
        knowledge._index_cache = None
        knowledge._index_mtime = 0.0
        knowledge._invalidate_bm25_cache()

        with patch("context.generate_env_alerts", return_value=""):
            messages = assemble_context(self.config, tick_number=1,
                                         goal_start_time=time.time())
        content = messages[1]["content"]
        self.assertIn("## Intelligence", content)
        # Should have recalled pip-related entries
        self.assertIn("pip", content.lower())

    def test_no_intelligence_when_disabled(self):
        self.config.knowledge_enabled = False
        with patch("context.generate_env_alerts", return_value=""):
            messages = assemble_context(self.config, tick_number=1,
                                         goal_start_time=time.time())
        content = messages[1]["content"]
        self.assertNotIn("## Intelligence", content)

    def test_no_intelligence_when_empty_store(self):
        with patch("context.generate_env_alerts", return_value=""):
            messages = assemble_context(self.config, tick_number=1,
                                         goal_start_time=time.time())
        content = messages[1]["content"]
        self.assertNotIn("## Intelligence", content)


if __name__ == "__main__":
    unittest.main()
