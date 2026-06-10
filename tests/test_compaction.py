"""Tests for compaction module."""

import os
import sys
import tempfile
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent))

import unittest
from unittest.mock import patch
from config import Config
from memory import append_observation, write_plan, read_plan
from compaction import (
    should_compact, compact_briefing,
    _snapshot_memory, _format_observations,
    parse_extractions, _parse_combined_output, _store_extractions,
)
from llm import ReasoningExhausted


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


    def test_snapshot_empty_memory(self):
        _snapshot_memory(self.config)
        snapshots = list(self.config.snapshots_dir.glob("memory_snapshot_*.md"))
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

    # --- New tests: compaction trigger edge cases ---

    def test_should_compact_exact_tick_threshold(self):
        """Exact threshold should trigger compaction."""
        self.assertTrue(should_compact(self.config, ticks_since_last=10))

    def test_should_compact_just_under_tick_threshold(self):
        self.assertFalse(should_compact(self.config, ticks_since_last=9))

    def test_should_compact_just_under_token_threshold(self):
        for i in range(2):
            append_observation(self.config, {"tick": i, "output": "x" * 50})
        self.assertFalse(should_compact(self.config, ticks_since_last=0))

    # --- New tests: compact() edge cases ---


class TestParseExtractions(unittest.TestCase):

    def test_parse_fact(self):
        text = "FACT [pip, bookworm]: pip requires --break-system-packages on Bookworm"
        results = parse_extractions(text)
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0]["category"], "facts")
        self.assertEqual(results[0]["tags"], ["pip", "bookworm"])
        self.assertIn("--break-system-packages", results[0]["content"])

    def test_parse_error(self):
        text = "ERROR [dht22, gpio]: DHT22 CRC errors when wire exceeds 3m"
        results = parse_extractions(text)
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0]["category"], "errors")

    def test_parse_procedure(self):
        text = "PROCEDURE [systemd]: Use systemctl --user for non-root services"
        results = parse_extractions(text)
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0]["category"], "procedures")

    def test_parse_reflection(self):
        text = "REFLECTION [debugging]: Always check journalctl before restarting"
        results = parse_extractions(text)
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0]["category"], "reflections")

    def test_parse_multiple_lines(self):
        text = """FACT [pip]: pip needs --break-system-packages
ERROR [dht22]: CRC errors above 3m
PROCEDURE [venv]: Always use python3 -m venv
REFLECTION [debugging]: Check logs first"""
        results = parse_extractions(text)
        self.assertEqual(len(results), 4)

    def test_parse_none(self):
        results = parse_extractions("NONE")
        self.assertEqual(results, [])

    def test_parse_empty(self):
        results = parse_extractions("")
        self.assertEqual(results, [])

    def test_parse_garbage_lines_skipped(self):
        text = """Here are the extractions:
FACT [pip]: pip needs flag
Some random text
Another random line
ERROR [dht22]: CRC errors"""
        results = parse_extractions(text)
        self.assertEqual(len(results), 2)

    def test_parse_case_insensitive(self):
        text = "fact [pip]: pip needs flag"
        results = parse_extractions(text)
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0]["category"], "facts")

    def test_parse_missing_tags_skipped(self):
        text = "FACT []: content without tags"
        results = parse_extractions(text)
        self.assertEqual(results, [])


# ===========================================================================
# Phase 4: Combined output parsing
# ===========================================================================

class TestParseCombinedOutput(unittest.TestCase):

    def test_parse_both_sections(self):
        output = """=== PLAN ===
Step 1 done. Next: step 2.

=== KNOWLEDGE ===
FACT [pip]: needs --break-system-packages
ERROR [dht22]: CRC errors above 3m"""

        plan, extractions = _parse_combined_output(output, "fallback plan")
        self.assertIn("Step 1 done", plan)
        self.assertEqual(len(extractions), 2)

    def test_parse_plan_only_no_knowledge(self):
        output = """=== PLAN ===
All steps complete.

=== KNOWLEDGE ===
NONE"""

        plan, extractions = _parse_combined_output(output, "fallback")
        self.assertIn("All steps complete", plan)
        self.assertEqual(extractions, [])

    def test_parse_no_section_headers_fallback(self):
        output = "Some raw text without section headers"
        plan, extractions = _parse_combined_output(output, "fallback")
        self.assertEqual(plan, "Some raw text without section headers")
        self.assertEqual(extractions, [])

    def test_parse_empty_output_uses_fallback(self):
        plan, extractions = _parse_combined_output("", "fallback plan")
        self.assertEqual(plan, "fallback plan")
        self.assertEqual(extractions, [])

    def test_parse_none_output_uses_fallback(self):
        plan, extractions = _parse_combined_output(None, "fallback plan")
        self.assertEqual(plan, "fallback plan")
        self.assertEqual(extractions, [])

    def test_parse_empty_plan_uses_fallback(self):
        output = """=== PLAN ===

=== KNOWLEDGE ===
FACT [test]: something"""

        plan, extractions = _parse_combined_output(output, "fallback plan")
        self.assertEqual(plan, "fallback plan")
        self.assertEqual(len(extractions), 1)


# ===========================================================================
# Phase 4: Store extractions
# ===========================================================================

class TestStoreExtractions(unittest.TestCase):

    def setUp(self):
        import tempfile, shutil
        self.tmp = tempfile.mkdtemp()
        self.config = Config()
        self.config.workspace_dir = os.path.join(self.tmp, "workspace")
        os.makedirs(self.config.workspace_dir)

    def tearDown(self):
        import shutil
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_store_multiple_extractions(self):
        extractions = [
            {"category": "facts", "tags": ["pip"], "content": "pip needs flag"},
            {"category": "errors", "tags": ["dht22"], "content": "CRC errors"},
        ]
        stored = _store_extractions(self.config, extractions, source_goal="test goal")
        self.assertEqual(stored, 2)
        # Verify files exist
        self.assertTrue(any((self.config.knowledge_dir / "facts").glob("*.md")))
        self.assertTrue(any((self.config.knowledge_dir / "errors").glob("*.md")))

    def test_store_empty_list(self):
        stored = _store_extractions(self.config, [], source_goal="")
        self.assertEqual(stored, 0)


# ===========================================================================
# Phase 4: Briefing-model dream cycle (compact_briefing)
# ===========================================================================

class TestCompactBriefing(unittest.TestCase):

    def setUp(self):
        import tempfile
        self.tmp = tempfile.mkdtemp()
        self.config = Config()
        self.config.workspace_dir = os.path.join(self.tmp, "workspace")
        os.makedirs(self.config.workspace_dir)
        os.makedirs(str(self.config.snapshots_dir))
        self.config.dream_combined = True

    def tearDown(self):
        import shutil
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_noop_when_nothing_exists(self):
        compact_briefing(self.config)
        # No crash, no plan written
        self.assertEqual(read_plan(self.config), "")

    @patch("compaction.complete", return_value="""=== PLAN ===
Step 1 done. Next: configure DHT22 sensor.

=== KNOWLEDGE ===
FACT [hostname]: The Pi's hostname is pi-eidos
ERROR [pip, bookworm]: pip requires --break-system-packages""")
    def test_combined_updates_plan_and_stores_knowledge(self, _mock):
        self.config.goal_path.write_text("Set up weather station")
        write_plan(self.config, "# Plan\nStep 1: check hostname")
        append_observation(self.config, {
            "tick": 1, "tool": "bash", "success": True,
            "output": "hostname: pi-eidos",
        })

        compact_briefing(self.config)

        plan = read_plan(self.config)
        self.assertIn("configure DHT22", plan)
        # Knowledge should be stored
        facts = list((self.config.knowledge_dir / "facts").glob("*.md"))
        errors = list((self.config.knowledge_dir / "errors").glob("*.md"))
        self.assertGreaterEqual(len(facts), 1)
        self.assertGreaterEqual(len(errors), 1)

    @patch("compaction.complete", return_value="""=== PLAN ===
Updated plan content.

=== KNOWLEDGE ===
NONE""")
    def test_combined_no_extractions(self, _mock):
        write_plan(self.config, "old plan")
        append_observation(self.config, {"tick": 1, "tool": "bash", "success": True, "output": "ok"})

        compact_briefing(self.config)

        self.assertIn("Updated plan content", read_plan(self.config))

    @patch("compaction.complete", return_value="")
    def test_combined_empty_output_keeps_plan(self, _mock):
        write_plan(self.config, "existing plan")
        append_observation(self.config, {"tick": 1, "tool": "bash", "success": True, "output": "data"})

        compact_briefing(self.config)

        self.assertEqual(read_plan(self.config), "existing plan")

    @patch("compaction.complete", return_value="x" * 2000)
    def test_combined_plan_capped(self, _mock):
        self.config.context_plan_max_chars = 300
        write_plan(self.config, "old")
        append_observation(self.config, {"tick": 1, "tool": "bash", "success": True, "output": "data"})

        compact_briefing(self.config)

        plan = read_plan(self.config)
        self.assertLessEqual(len(plan), 350)  # slack for trim suffix
        self.assertIn("plan trimmed", plan)

    @patch("compaction.complete", side_effect=ReasoningExhausted("thinking...", 2047, 2048))
    def test_combined_exhaustion_keeps_plan(self, _mock):
        write_plan(self.config, "important plan")
        append_observation(self.config, {"tick": 1, "tool": "bash", "success": True, "output": "data"})

        compact_briefing(self.config)

        self.assertEqual(read_plan(self.config), "important plan")

    @patch("compaction.complete", return_value="""=== PLAN ===
New plan.

=== KNOWLEDGE ===
FACT [test]: test fact""")
    def test_combined_logs_dream_observation(self, _mock):
        write_plan(self.config, "old plan")
        append_observation(self.config, {"tick": 1, "tool": "bash", "success": True, "output": "work"})

        compact_briefing(self.config)

        import json
        with open(self.config.observations_path) as f:
            lines = f.readlines()
        last = json.loads(lines[-1])
        self.assertEqual(last["tick"], "compaction")
        self.assertEqual(last["tool"], "dream")
        self.assertTrue(last["success"])
        self.assertIn("1 entries extracted", last["output"])

    @patch("compaction.complete", return_value="""=== PLAN ===
Personality plan.

=== KNOWLEDGE ===
NONE""")
    def test_combined_with_persona(self, mock_complete):
        write_plan(self.config, "old")
        append_observation(self.config, {"tick": 1, "tool": "bash", "success": True, "output": "data"})
        self.config.persona_enabled = True
        persona = {"traits": ["curious"], "mood": "focused"}

        compact_briefing(self.config, persona=persona)

        call_args = mock_complete.call_args
        messages = call_args[0][0]
        system_msg = messages[0]["content"]
        self.assertIn("curious", system_msg)

    # --- Split mode tests ---

    @patch("compaction.complete", side_effect=[
        "Plan from first call.",
        "FACT [pip]: pip needs --break-system-packages",
    ])
    def test_split_mode_two_calls(self, mock_complete):
        self.config.dream_combined = False
        self.config.goal_path.write_text("Set up Pi")
        write_plan(self.config, "old plan")
        append_observation(self.config, {"tick": 1, "tool": "bash", "success": True, "output": "data"})

        compact_briefing(self.config)

        self.assertEqual(mock_complete.call_count, 2)
        self.assertIn("Plan from first call", read_plan(self.config))
        facts = list((self.config.knowledge_dir / "facts").glob("*.md"))
        self.assertGreaterEqual(len(facts), 1)

    @patch("compaction.complete", side_effect=[
        "Plan from first call.",
        "NONE",
    ])
    def test_split_mode_no_extractions(self, mock_complete):
        self.config.dream_combined = False
        write_plan(self.config, "old plan")
        append_observation(self.config, {"tick": 1, "tool": "bash", "success": True, "output": "data"})

        compact_briefing(self.config)

        self.assertIn("Plan from first call", read_plan(self.config))


if __name__ == "__main__":
    unittest.main()
