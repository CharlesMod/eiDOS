"""Tests for compaction module."""

import os
import sys
import tempfile
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent))

import unittest
from unittest.mock import patch
from config import Config
from memory import write_memory, append_observation, read_memory
from compaction import should_compact, compact, _snapshot_memory, _format_observations


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
        snapshots = list(self.config.snapshots_dir.glob("memory_before_*.md"))
        self.assertEqual(len(snapshots), 1)
        self.assertEqual(snapshots[0].read_text(), "test memory before compaction")

    def test_snapshot_empty_memory(self):
        _snapshot_memory(self.config)
        snapshots = list(self.config.snapshots_dir.glob("memory_before_*.md"))
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

    @patch("compaction.complete", return_value=(
        "Thinking Process:\n\n"
        "1.  **Analyze the Request:**\n"
        "    *   Role: Memory compaction system.\n"
        "    *   Task: Rewrite working memory.\n"
        "This is raw reasoning that should NOT be saved as memory."
    ))
    def test_compact_discards_reasoning_dump(self, _mock_complete):
        """Thinking models may exhaust tokens on reasoning, returning raw
        'Thinking Process:' output instead of actual compacted memory.
        compact() must detect this and keep the old memory."""
        write_memory(self.config, "important prior memory")
        append_observation(self.config, {"tick": 1, "tool": "bash", "success": True, "output": "work"})

        compact(self.config)

        mem = read_memory(self.config)
        self.assertEqual(mem, "important prior memory")
        self.assertNotIn("Thinking Process", mem)

    @patch("compaction.complete", return_value=(
        "  \n**Analyze the Request:** The user wants...\nStep by step analysis..."
    ))
    def test_compact_discards_reasoning_with_leading_whitespace(self, _mock_complete):
        """Reasoning dumps may have leading whitespace before the marker."""
        write_memory(self.config, "real memory content")
        append_observation(self.config, {"tick": 1, "tool": "bash", "success": True, "output": "work"})

        compact(self.config)

        self.assertEqual(read_memory(self.config), "real memory content")

    @patch("compaction.complete", return_value="# Working Memory\nGoal: explore system.\n- Checked disk: OK")
    def test_compact_keeps_legitimate_memory_starting_with_heading(self, _mock_complete):
        """Normal compaction output (markdown headings, bullet points) must not
        be falsely flagged as reasoning."""
        write_memory(self.config, "old memory")
        append_observation(self.config, {"tick": 1, "tool": "bash", "success": True, "output": "work"})

        compact(self.config)

        self.assertIn("Goal: explore system", read_memory(self.config))

    @patch("compaction.complete", side_effect=[
        "Thinking Process:\n1. **Analyze** the request...",
        "# Working Memory\nRetry succeeded with higher budget.",
    ])
    def test_compact_retries_on_reasoning_dump(self, mock_complete):
        """When first attempt returns reasoning dump, compact() retries with
        higher max_tokens budget."""
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

    @patch("compaction.complete")
    def test_compact_passes_enable_thinking_false(self, mock_complete):
        """Compaction should request thinking disabled via enable_thinking=False."""
        mock_complete.return_value = "Condensed memory."
        write_memory(self.config, "old memory")
        append_observation(self.config, {"tick": 1, "tool": "bash", "success": True, "output": "work"})

        compact(self.config)

        # Check that enable_thinking=False was passed
        _, kwargs = mock_complete.call_args_list[0]
        self.assertFalse(kwargs.get("enable_thinking"))

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


if __name__ == "__main__":
    unittest.main()
