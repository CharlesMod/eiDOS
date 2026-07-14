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


class TestCreaturePlanNag(unittest.TestCase):
    """A creature must NOT be fed the frozen plan first-line as an imperative 'next step' every tick —
    it stays stale between dreams and nags the creature to redo finished work (the morose-tone source).
    The task-driven (non-creature) mode still gets it."""

    def setUp(self):
        import context
        self.context = context
        self.cfg = Config()
        self.cfg.workspace_dir = tempfile.mkdtemp()
        self.cfg.workspace.mkdir(parents=True, exist_ok=True)
        write_plan(self.cfg, "1. Read and analyze the two files.\n2. Do the next thing.")

    def test_creature_mode_drops_the_next_step_imperative(self):
        self.cfg.creature_mode = True
        self.assertNotIn("next step", self.context._current_focus(self.cfg))

    def test_task_mode_keeps_the_next_step(self):
        self.cfg.creature_mode = False
        self.assertIn("next step", self.context._current_focus(self.cfg))


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


_CANNED_ENV_ALERTS = ""  # Normal - no alerts
_CANNED_ENV_WITH_ALERT = "ALERT DISK LOW: 0.5 GB free / 32.0 GB (threshold 1.0 GB)"


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
        # [system, durable, situation, tick] — the volatile situation message rides AFTER
        # the (empty here) history thread so per-tick churn never invalidates history KV.
        messages = self._assemble()
        self.assertEqual(len(messages), 4)
        self.assertEqual(messages[0]["role"], "system")
        for m in messages[1:]:
            self.assertEqual(m["role"], "user")
        self.assertIn("## Right now", messages[-2]["content"])     # situation before tick prompt
        self.assertNotIn("## Right now", messages[1]["content"])   # not in the stable durable blob

    def test_uses_compressed_system_prompt(self):
        messages = self._assemble()
        system = messages[0]["content"]
        self.assertIn("eiDOS", system)
        self.assertIn("memorize", system)
        self.assertIn("recall", system)
        self.assertIn("house", system)  # the house-AI briefing prompt, not a generic one

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
        # alerts render in the presence block, which lives in the situation message
        self.assertIn("DISK LOW", messages[-2]["content"])

    def test_observations_in_history_thread(self):
        """Observations replay as real assistant/user turns (the history thread),
        not as a flattened section in the durable blob."""
        cmds = ["arp -a", "Get-Process", "Get-Service", "ipconfig", "Get-Date"]
        for i in range(5):
            append_observation(self.config, {
                "tick": i, "tool": "bash", "args": {"cmd": cmds[i]},
                "success": True, "output": f"output_for_tick_{i}"
            })
        messages = self._assemble()
        full = "\n".join(m["content"] for m in messages)
        self.assertIn("output_for_tick_4", full)
        self.assertNotIn("output_for_tick_4", messages[1]["content"])
        self.assertGreater(len(messages), 3)  # durable + thread turns + tick prompt

    def test_plan_budget_enforced(self):
        write_plan(self.config, "X" * 5000)
        messages = self._assemble(context_plan_max_chars=200)
        content = messages[1]["content"]
        plan_section = content.split("## Plan\n")[1].split("\n\n##")[0]
        self.assertIn("truncated", plan_section)
        self.assertLess(len(plan_section), 5000)

    def test_total_budget_bounded(self):
        # A huge plan + many observations; the ceiling must trim them while preserving
        # the system prompt and the trailing tick prompt. Ceiling set above the system
        # prompt floor (realistic — production is 120k) so trimming is observable.
        self.config.goal_path.write_text("G" * 3000)
        write_plan(self.config, "P" * 8000)
        for i in range(40):
            append_observation(self.config, {
                "tick": i, "tool": "bash", "args": {"cmd": f"c{i}"},
                "success": True, "output": "O" * 500,
            })
        base = self._assemble()  # measure the system prompt size for this build
        ceiling = len(base[0]["content"]) + 1500
        messages = self._assemble(context_max_total_chars=ceiling)
        total = sum(len(m["content"]) for m in messages)
        self.assertLessEqual(total, ceiling + 100)            # ceiling enforced
        self.assertEqual(messages[0]["role"], "system")        # system preserved
        self.assertNotIn("P" * 2000, messages[1]["content"])   # the 8k plan was trimmed

    def test_no_plan_no_section(self):
        """When neither plan.md nor memory.md exists, no Plan section appears."""
        os.unlink(str(self.config.plan_path))
        messages = self._assemble()
        content = messages[1]["content"]
        self.assertNotIn("## Plan", content)

    def test_interventions_in_briefing(self):
        (self.config.interventions_dir / "001_hint.md").write_text("Check the logs")
        messages = self._assemble()
        full = "\n".join(m["content"] for m in messages)
        self.assertIn("## Conversation with Boss", full)
        self.assertIn("Check the logs", full)
        # and the conversation lives in the volatile situation message, not the stable blob
        self.assertNotIn("## Conversation with Boss", messages[1]["content"])

    def test_history_window_anchored_not_sliding(self):
        # KV-stability: the history window's HEAD must not move every tick. Build 40 ticks
        # of observations and assemble at consecutive tick numbers — the first history turn
        # must be identical across a step window (it only advances every n_ticks/2 ticks).
        from context import _build_history_thread

        def _cmd(i):
            # distinct WORDS per tick — digits are normalized out of the collapse
            # signature, so numbered commands would all merge into one counted turn
            return f"run_{chr(97 + i % 26)}{chr(97 + (i // 26) % 26)} --once"

        for i in range(40):
            append_observation(self.config, {
                "tick": i, "tool": "bash", "args": {"cmd": _cmd(i)},
                "success": True, "output": f"out{i}",
            })
        first = _build_history_thread(self.config)[0]["content"]
        # one more tick lands — the head must NOT slide
        append_observation(self.config, {
            "tick": 40, "tool": "bash", "args": {"cmd": _cmd(40)},
            "success": True, "output": "out40",
        })
        thread = _build_history_thread(self.config)
        self.assertEqual(thread[0]["content"], first)      # head stable (append-only between cuts)
        self.assertIn("out40", thread[-1]["content"])      # new turn appended

    def test_loop_detection_in_briefing(self):
        with patch("context.generate_env_alerts", return_value=""):
            messages = assemble_context(
                self.config, tick_number=5, goal_start_time=time.time(),
                loop_detected=True, repeat_count=3,
            )
        tick_msg = messages[-1]["content"]
        self.assertIn("without real progress", tick_msg.lower())



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
        """When the store has learned entries, the world-model panel appears."""
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
        self.assertIn("What you've learned", content)
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
