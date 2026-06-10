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
)
from memory import write_plan, append_observation


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


class TestBriefingModel(unittest.TestCase):
    """Test the briefing model context assembly."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.config = Config()
        self.config.workspace_dir = os.path.join(self.tmp, "workspace")
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
