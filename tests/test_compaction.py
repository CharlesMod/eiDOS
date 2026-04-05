"""Tests for compaction module."""

import os
import sys
import tempfile
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent))

import unittest
from unittest.mock import patch
from config import Config
from memory import write_memory, append_observation, read_memory, write_plan, read_plan
from compaction import (
    should_compact, compact, compact_briefing,
    _snapshot_memory, _format_observations, _build_fallback_memory,
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

    def test_snapshot_created(self):
        write_memory(self.config, "test memory before compaction")
        _snapshot_memory(self.config)
        snapshots = list(self.config.snapshots_dir.glob("memory_snapshot_*.md"))
        self.assertEqual(len(snapshots), 1)
        self.assertEqual(snapshots[0].read_text(), "test memory before compaction")

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

    @patch("compaction.complete", side_effect=ReasoningExhausted(
        "Thinking Process:\n1. **Analyze** the request...", 2047, 2048
    ))
    def test_compact_keeps_memory_on_reasoning_exhaustion(self, _mock_complete):
        """When thinking model exhausts all tokens on reasoning (both attempts
        raise ReasoningExhausted), compact() must preserve old memory AND
        incorporate observation facts via fallback."""
        write_memory(self.config, "important prior memory")
        append_observation(self.config, {"tick": 1, "tool": "bash", "success": True, "output": "work"})

        compact(self.config)

        mem = read_memory(self.config)
        self.assertIn("important prior memory", mem)
        self.assertIn("work", mem)
        self.assertIn("Uncompacted Observations", mem)

    @patch("compaction.complete", side_effect=[
        ReasoningExhausted("deep thinking...", 2047, 2048),
        "# Working Memory\nRetry succeeded with higher budget.",
    ])
    def test_compact_retries_on_reasoning_exhaustion(self, mock_complete):
        """When first attempt raises ReasoningExhausted, compact() retries with
        higher max_tokens budget and budget feedback."""
        self.config.compaction_retry_max_tokens = 4096
        write_memory(self.config, "old memory")
        append_observation(self.config, {"tick": 1, "tool": "bash", "success": True, "output": "work"})

        compact(self.config)

        self.assertEqual(mock_complete.call_count, 2)
        # First call uses compaction_max_tokens
        _, kwargs1 = mock_complete.call_args_list[0]
        self.assertEqual(kwargs1.get("max_tokens") or self.config.compaction_max_tokens,
                         self.config.compaction_max_tokens)
        # Second call uses retry budget
        _, kwargs2 = mock_complete.call_args_list[1]
        self.assertEqual(kwargs2["max_tokens"], 4096)
        # Memory should be the retry result
        self.assertIn("Retry succeeded", read_memory(self.config))

    @patch("compaction.complete", side_effect=[
        ReasoningExhausted("thinking...", 2047, 2048),
        "# Working Memory\nRetry worked.",
    ])
    def test_compact_retry_includes_budget_feedback(self, mock_complete):
        """Retry messages should include feedback about the exhaustion."""
        write_memory(self.config, "old memory")
        append_observation(self.config, {"tick": 1, "tool": "bash", "success": True, "output": "work"})

        compact(self.config)

        # Check the retry call's messages include feedback
        retry_args = mock_complete.call_args_list[1]
        retry_messages = retry_args[0][0]  # first positional arg
        # Should have extra messages for budget feedback
        self.assertGreater(len(retry_messages), 2)
        user_feedback = retry_messages[-1]["content"]
        self.assertIn("token budget", user_feedback.lower())

    @patch("compaction.complete", return_value="Condensed memory with goal context.")
    def test_compact_includes_goal_in_prompt(self, mock_complete):
        """Compaction prompt must include the current goal so the LLM knows
        what information is relevant to retain."""
        self.config.goal_path.write_text("Build a weather station dashboard.")
        write_memory(self.config, "# Working Memory\nPrior observations.")
        append_observation(self.config, {"tick": 1, "tool": "bash", "success": True, "output": "data"})

        compact(self.config)

        # Inspect the messages passed to complete()
        call_args = mock_complete.call_args
        messages = call_args[0][0]  # first positional arg
        user_msg = next(m["content"] for m in messages if m["role"] == "user")
        self.assertIn("Build a weather station dashboard", user_msg)
        self.assertIn("immutable", user_msg.lower())

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

    def test_compact_noop_when_nothing_exists(self):
        """No observations AND no memory → compact does nothing."""
        compact(self.config)
        self.assertEqual(read_memory(self.config), "")

    @patch("compaction.complete", return_value="# Memory\nFrom observations only.")
    def test_compact_observations_only_no_prior_memory(self, _mock):
        """Compaction with observations but no prior memory.md."""
        append_observation(self.config, {"tick": 1, "tool": "bash", "success": True, "output": "hello"})
        compact(self.config)
        self.assertIn("From observations only", read_memory(self.config))

    @patch("compaction.complete", return_value="# Memory\nConsolidated.")
    def test_compact_memory_only_no_observations(self, _mock):
        """Compaction with existing memory but no observations."""
        write_memory(self.config, "existing memory content")
        compact(self.config)
        self.assertEqual(read_memory(self.config), "# Memory\nConsolidated.")

    @patch("compaction.complete", return_value="x" * 10000)
    def test_compact_output_capped_to_memory_budget(self, _mock):
        """Compaction output exceeding context_memory_max_chars gets trimmed."""
        self.config.context_memory_max_chars = 500
        write_memory(self.config, "old")
        append_observation(self.config, {"tick": 1, "tool": "bash", "success": True, "output": "data"})
        compact(self.config)
        mem = read_memory(self.config)
        self.assertLessEqual(len(mem), 550)  # slack for line-boundary trimming
        self.assertIn("compaction trimmed", mem)

    @patch("compaction.complete", return_value="# New consolidated memory.")
    def test_compact_logs_compaction_observation(self, _mock):
        """Compaction appends a compaction event to observations.jsonl."""
        write_memory(self.config, "old memory")
        append_observation(self.config, {"tick": 1, "tool": "bash", "success": True, "output": "work"})
        compact(self.config)
        import json
        with open(self.config.observations_path) as f:
            lines = f.readlines()
        last = json.loads(lines[-1])
        self.assertEqual(last["tick"], "compaction")
        self.assertEqual(last["tool"], "dream")
        self.assertTrue(last["success"])

    @patch("compaction.complete", return_value="# Consolidated with personality.")
    def test_compact_with_persona(self, mock_complete):
        """Persona traits are injected into compaction system prompt."""
        write_memory(self.config, "old")
        append_observation(self.config, {"tick": 1, "tool": "bash", "success": True, "output": "data"})
        self.config.persona_enabled = True
        persona = {"traits": ["curious", "cautious"], "mood": "focused"}
        compact(self.config, persona=persona)
        call_args = mock_complete.call_args
        messages = call_args[0][0]
        system_msg = messages[0]["content"]
        self.assertIn("curious", system_msg)
        self.assertIn("focused", system_msg)

    @patch("compaction.complete", return_value="# Consolidated without personality.")
    def test_compact_without_persona(self, mock_complete):
        """No persona traits when persona_enabled is False."""
        write_memory(self.config, "old")
        append_observation(self.config, {"tick": 1, "tool": "bash", "success": True, "output": "data"})
        self.config.persona_enabled = False
        persona = {"traits": ["curious"], "mood": "focused"}
        compact(self.config, persona=persona)
        call_args = mock_complete.call_args
        messages = call_args[0][0]
        system_msg = messages[0]["content"]
        self.assertNotIn("curious", system_msg)

    @patch("compaction.complete", return_value="# Consolidated.")
    def test_compact_truncates_oversized_existing_memory(self, mock_complete):
        """Memory exceeding compaction_memory_max_chars gets truncated in prompt."""
        self.config.compaction_memory_max_chars = 100
        write_memory(self.config, "x" * 300)
        append_observation(self.config, {"tick": 1, "tool": "bash", "success": True, "output": "data"})
        compact(self.config)
        call_args = mock_complete.call_args
        messages = call_args[0][0]
        user_msg = next(m["content"] for m in messages if m["role"] == "user")
        self.assertIn("truncated", user_msg)

    # --- New tests: double-exhaust fallback (THE BUG FIX) ---

    @patch("compaction.complete", side_effect=ReasoningExhausted(
        "Thinking...", 2047, 2048
    ))
    def test_double_exhaust_fallback_preserves_observations(self, _mock):
        """CRITICAL: When compaction double-exhausts, observation facts must
        be preserved in fallback memory, not silently dropped."""
        write_memory(self.config, "# Working Memory\nGoal: find config files.")
        append_observation(self.config, {
            "tick": 1, "tool": "bash", "success": True,
            "output": "raspberrypi-kairos",
        })
        append_observation(self.config, {
            "tick": 2, "tool": "bash", "success": True,
            "output": "/dev/sda1  32G  8.2G  22G  28%",
        })
        append_observation(self.config, {
            "tick": 3, "tool": "remember", "success": True,
            "output": "SSH key found at /home/pi/.ssh/id_rsa",
        })

        compact(self.config)
        mem = read_memory(self.config)

        # Old memory preserved
        self.assertIn("find config files", mem)
        # Observation facts preserved in fallback
        self.assertIn("raspberrypi-kairos", mem)
        self.assertIn("32G", mem)
        self.assertIn("SSH key", mem)
        self.assertIn("Uncompacted Observations", mem)

    @patch("compaction.complete", side_effect=ReasoningExhausted(
        "Thinking...", 2047, 2048
    ))
    def test_double_exhaust_fallback_includes_goal(self, _mock):
        """Fallback memory includes the goal section."""
        self.config.goal_path.write_text("Monitor system temperature.")
        write_memory(self.config, "")
        append_observation(self.config, {
            "tick": 1, "tool": "bash", "success": True, "output": "temp=42C",
        })

        compact(self.config)
        mem = read_memory(self.config)

        self.assertIn("Monitor system temperature", mem)
        self.assertIn("temp=42C", mem)

    @patch("compaction.complete", side_effect=ReasoningExhausted(
        "Thinking...", 2047, 2048
    ))
    def test_double_exhaust_fallback_respects_cap(self, _mock):
        """Fallback memory respects context_memory_max_chars budget."""
        self.config.context_memory_max_chars = 500
        write_memory(self.config, "old memory")
        for i in range(50):
            append_observation(self.config, {
                "tick": i, "tool": "bash", "success": True,
                "output": f"observation data line {i} with some content",
            })

        compact(self.config)
        mem = read_memory(self.config)

        self.assertLessEqual(len(mem), 600)  # some slack for existing memory
        self.assertIn("trimmed", mem.lower())

    @patch("compaction.complete", side_effect=ReasoningExhausted(
        "Thinking...", 2047, 2048
    ))
    def test_double_exhaust_no_prior_memory_no_goal(self, _mock):
        """Fallback with observations but no prior memory and no goal."""
        append_observation(self.config, {
            "tick": 1, "tool": "bash", "success": True, "output": "hello world",
        })

        compact(self.config)
        mem = read_memory(self.config)

        self.assertIn("hello world", mem)
        self.assertIn("Uncompacted Observations", mem)

    # --- New tests: _build_fallback_memory unit tests ---

    def test_build_fallback_goal_and_observations(self):
        obs = [
            {"tick": 1, "tool": "bash", "output": "hostname: pi-kairos"},
            {"tick": 2, "tool": "bash", "output": "disk: 32G"},
        ]
        result = _build_fallback_memory("old memory", "Monitor system", obs, 4000)
        self.assertIn("Monitor system", result)
        self.assertIn("old memory", result)
        self.assertIn("pi-kairos", result)
        self.assertIn("32G", result)

    def test_build_fallback_no_goal(self):
        obs = [{"tick": 1, "tool": "bash", "output": "data"}]
        result = _build_fallback_memory("memory", "", obs, 4000)
        self.assertNotIn("Active Goal", result)
        self.assertIn("memory", result)
        self.assertIn("data", result)

    def test_build_fallback_no_observations(self):
        result = _build_fallback_memory("old memory", "goal", [], 4000)
        self.assertIn("old memory", result)
        self.assertNotIn("Uncompacted", result)

    def test_build_fallback_empty_everything(self):
        result = _build_fallback_memory("", "", [], 4000)
        self.assertIn("Compaction failed", result)

    def test_build_fallback_truncates_long_observation_output(self):
        obs = [{"tick": 1, "tool": "bash", "output": "x" * 300}]
        result = _build_fallback_memory("", "goal", obs, 4000)
        self.assertIn("...", result)

    def test_build_fallback_caps_total_size(self):
        obs = [{"tick": i, "tool": "bash", "output": f"data_{i}"} for i in range(100)]
        result = _build_fallback_memory("", "goal", obs, 500)
        self.assertLessEqual(len(result), 600)  # some slack


# ===========================================================================
# Phase 4: Knowledge extraction parsing
# ===========================================================================

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
FACT [hostname]: The Pi's hostname is pi-kairos
ERROR [pip, bookworm]: pip requires --break-system-packages""")
    def test_combined_updates_plan_and_stores_knowledge(self, _mock):
        self.config.goal_path.write_text("Set up weather station")
        write_plan(self.config, "# Plan\nStep 1: check hostname")
        append_observation(self.config, {
            "tick": 1, "tool": "bash", "success": True,
            "output": "hostname: pi-kairos",
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

    @patch("compaction.complete", return_value="""=== PLAN ===
Dream plan.

=== KNOWLEDGE ===
NONE""")
    def test_briefing_creates_snapshot(self, _mock):
        write_plan(self.config, "old plan")
        # Also write memory.md so snapshot has something
        write_memory(self.config, "old memory for snapshot")
        append_observation(self.config, {"tick": 1, "tool": "bash", "success": True, "output": "ok"})

        compact_briefing(self.config)

        snapshots = list(self.config.snapshots_dir.glob("memory_snapshot_*.md"))
        self.assertEqual(len(snapshots), 1)


if __name__ == "__main__":
    unittest.main()
