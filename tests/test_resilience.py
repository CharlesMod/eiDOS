"""Tests for deployment-critical resilience scenarios.

Covers:
  1. Service restart mid-tick (WAL recovery, no duplication)
  2. Disk full during write (graceful degradation)
  3. LLM crash mid-request (retry, no hang)
  4. Cold start from empty workspace (clean initialization)
"""

import json
import os
import sys
import tempfile
import time
from pathlib import Path
from unittest.mock import patch, MagicMock

sys.path.insert(0, str(Path(__file__).parent.parent))

import unittest
from config import Config
from eidos import write_wal, read_wal, clear_wal, recover, run_loop, attempt_llm_restart
from memory import (
    write_memory,
    read_memory,
    append_observation,
    read_recent_observations,
    validate_observations,
    read_goal,
    write_plan,
    read_plan,
)
from tools import execute_tool, refresh_jobs
from parser import ToolCall


def _make_config(tmp):
    """Create a Config pointing at a temp workspace with all required dirs."""
    config = Config()
    config.workspace_dir = os.path.join(tmp, "workspace")
    os.makedirs(config.workspace_dir, exist_ok=True)
    os.makedirs(str(config.snapshots_dir), exist_ok=True)
    os.makedirs(str(config.interventions_dir), exist_ok=True)
    os.makedirs(str(config.outputs_dir), exist_ok=True)
    return config


# ===========================================================================
#  1. SERVICE RESTART MID-TICK
# ===========================================================================


class TestServiceRestartMidTick(unittest.TestCase):
    """Simulate systemd stopping eidos while a tick is in progress.

    The WAL should allow clean resumption with no duplicated observations
    and no lost state.
    """

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.config = _make_config(self.tmp)

    def tearDown(self):
        import shutil
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_wal_preserves_tick_state_across_restart(self):
        """Write WAL mid-tick, then recover — tick number and counters survive."""
        write_wal(self.config, tick_number=37, ticks_since_compaction=12,
                  goal_start_time=1000.0, consecutive_failures=2,
                  reasoning_exhaustions=1, current_max_tokens=2048)

        # Simulate process restart: new recover() call
        wal = recover(self.config)
        self.assertEqual(wal["tick_number"], 37)
        self.assertEqual(wal["ticks_since_compaction"], 12)
        self.assertEqual(wal["consecutive_failures"], 2)
        self.assertEqual(wal["reasoning_exhaustions"], 1)
        self.assertEqual(wal["current_max_tokens"], 2048)

    def test_observations_not_duplicated_after_restart(self):
        """Observations written before crash don't get re-written on recovery."""
        append_observation(self.config, {"tick": 10, "tool": "bash", "output": "ok"})
        append_observation(self.config, {"tick": 11, "tool": "bash", "output": "also ok"})
        write_wal(self.config, tick_number=12, ticks_since_compaction=3,
                  goal_start_time=1000.0)

        # Count observations before recovery
        with open(self.config.observations_path) as f:
            pre_count = sum(1 for _ in f)

        # Recover (this appends a system observation about recovery)
        recover(self.config)

        with open(self.config.observations_path) as f:
            lines = f.readlines()

        # Only the two original + one recovery system message
        original_ticks = [json.loads(l)["tick"] for l in lines if json.loads(l)["tick"] > 0]
        self.assertEqual(original_ticks, [10, 11])

    def test_memory_survives_restart(self):
        """Memory.md written before crash is preserved on recovery."""
        write_memory(self.config, "critical context about GPIO sensor readings")
        write_wal(self.config, tick_number=5, ticks_since_compaction=2,
                  goal_start_time=1000.0)

        wal = recover(self.config)
        content = read_memory(self.config)
        self.assertIn("critical context about GPIO sensor readings", content)

    def test_memory_restored_from_snapshot_if_empty(self):
        """If memory.md is empty after crash, restore from snapshot."""
        # Create a snapshot
        snap_path = self.config.snapshots_dir / "memory_snapshot_001.md"
        snap_path.write_text("snapshot: sensor calibration data")

        # Create empty memory.md (simulating crash mid-write)
        self.config.memory_path.write_text("")

        wal = recover(self.config)
        content = read_memory(self.config)
        self.assertIn("sensor calibration data", content)

    def test_corrupted_observation_truncated_on_restart(self):
        """Crash mid-append leaves partial JSON — recovery trims it."""
        append_observation(self.config, {"tick": 1, "tool": "bash", "output": "good"})
        # Simulate partial write (crash mid-line)
        with open(self.config.observations_path, "a") as f:
            f.write('{"tick": 2, "tool": "bash", "output": "incom')

        wal = recover(self.config)

        # The incomplete line should be gone
        with open(self.config.observations_path) as f:
            lines = f.readlines()

        valid_ticks = []
        for line in lines:
            line = line.strip()
            if not line:
                continue
            try:
                entry = json.loads(line)
                if entry.get("tick", 0) > 0:
                    valid_ticks.append(entry["tick"])
            except json.JSONDecodeError:
                self.fail(f"Found invalid JSON after recovery: {line[:100]}")

        self.assertIn(1, valid_ticks)
        # Tick 2 was corrupted and should be gone
        self.assertNotIn(2, valid_ticks)

    def test_wal_tmp_file_cleaned_up(self):
        """No .tmp files left after WAL write (atomic rename succeeded)."""
        write_wal(self.config, tick_number=1, ticks_since_compaction=0,
                  goal_start_time=time.time())

        tmp_files = list(Path(self.config.workspace_dir).glob("*.tmp"))
        self.assertEqual(len(tmp_files), 0, f"Stale tmp files: {tmp_files}")

    def test_plan_survives_restart(self):
        """plan.md is preserved across crash recovery."""
        write_plan(self.config, "Step 1: read sensor\nStep 2: log data")
        write_wal(self.config, tick_number=8, ticks_since_compaction=3,
                  goal_start_time=1000.0)

        recover(self.config)
        content = read_plan(self.config)
        self.assertIn("Step 1: read sensor", content)


# ===========================================================================
#  2. DISK FULL DURING WRITE
# ===========================================================================


class TestDiskFull(unittest.TestCase):
    """Verify graceful degradation when disk space is exhausted.

    On a Pi with a small SD card, this is the #1 cause of data loss.
    """

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.config = _make_config(self.tmp)

    def tearDown(self):
        import shutil
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_bash_wget_blocked_when_disk_low(self):
        """bash tool blocks write-indicator commands (wget, pip install) on low disk."""
        from tools import tool_bash

        self.config.disk_min_gb = 99999.0
        result = tool_bash({"cmd": "wget http://example.com/big.tar.gz"}, self.config)
        self.assertFalse(result.success)
        self.assertIn("disk", result.output.lower())

    def test_bash_pip_install_blocked_when_disk_low(self):
        """pip install via bash blocked on low disk."""
        from tools import tool_bash

        self.config.disk_min_gb = 99999.0
        result = tool_bash({"cmd": "pip install numpy"}, self.config)
        self.assertFalse(result.success)
        self.assertIn("disk", result.output.lower())

    def test_bash_git_clone_blocked_when_disk_low(self):
        """git clone via bash blocked on low disk."""
        from tools import tool_bash

        self.config.disk_min_gb = 99999.0
        result = tool_bash({"cmd": "git clone https://example.com/repo"}, self.config)
        self.assertFalse(result.success)
        self.assertIn("disk", result.output.lower())

    def test_observation_append_on_nearly_full_disk(self):
        """Observations should still append if the single line is small."""
        # This tests the happy path — observations are tiny and should
        # always fit even on a nearly-full disk
        append_observation(self.config, {"tick": 1, "tool": "test", "output": "ok"})
        obs = read_recent_observations(self.config, max_chars=10000, max_count=10)
        self.assertEqual(len(obs), 1)

    def test_memory_write_fails_gracefully_on_oserror(self):
        """write_memory raises on actual OS write failure (temp file creation)."""
        # Make the workspace dir read-only to simulate disk full / permissions error
        os.chmod(self.config.workspace_dir, 0o444)
        try:
            with self.assertRaises(OSError):
                write_memory(self.config, "data that cannot be written")
        finally:
            # Restore permissions for cleanup
            os.chmod(self.config.workspace_dir, 0o755)

    def test_write_file_tool_blocked_when_disk_low(self):
        """tool_write_file refuses when disk space is below threshold."""
        from tools import tool_write_file

        self.config.disk_min_gb = 99999.0
        result = tool_write_file({
            "path": os.path.join(self.config.workspace_dir, "data.txt"),
            "content": "sensor data",
        }, self.config)
        self.assertFalse(result.success)
        self.assertIn("disk", result.output.lower())

    def test_remember_tool_blocked_when_disk_low(self):
        """tool_remember refuses when disk space is below threshold."""
        from tools import tool_remember

        self.config.disk_min_gb = 99999.0
        result = tool_remember({"note": "important note"}, self.config)
        self.assertFalse(result.success)
        self.assertIn("disk", result.output.lower())

    def test_update_plan_tool_blocked_when_disk_low(self):
        """tool_update_plan refuses when disk space is below threshold."""
        from tools import tool_update_plan

        self.config.disk_min_gb = 99999.0
        result = tool_update_plan({"note": "step 1"}, self.config)
        self.assertFalse(result.success)
        self.assertIn("disk", result.output.lower())

    def test_memorize_tool_blocked_when_disk_low(self):
        """tool_memorize refuses when disk space is below threshold."""
        from tools import tool_memorize

        self.config.disk_min_gb = 99999.0
        result = tool_memorize({
            "fact": "the sky is blue",
            "tags": ["test"],
            "category": "facts",
        }, self.config)
        self.assertFalse(result.success)
        self.assertIn("disk", result.output.lower())


# ===========================================================================
#  3. LLM CRASH MID-REQUEST
# ===========================================================================


class TestLLMCrashMidRequest(unittest.TestCase):
    """Verify the tick loop handles LLM server failures without hanging.

    On Pi, llama-server can OOM, crash, or restart at any point.
    """

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.config = _make_config(self.tmp)
        self.config.mock_mode = True
        self.config.tick_interval_s = 0   # no sleeping in tests
        self.config.persona_enabled = False
        # Reset the shutdown flag before each test
        import eidos as _k
        _k._shutdown_requested = False

    def tearDown(self):
        import shutil
        import eidos as _k
        _k._shutdown_requested = False
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_connection_refused_logs_failure(self):
        """LLM server down → LLMError → logged, no hang."""
        from llm import LLMError

        self.config.goal_path.write_text("Test goal")
        write_memory(self.config, "Test memory")

        call_count = [0]

        def fail_and_shutdown(*a, **kw):
            call_count[0] += 1
            import eidos
            eidos._shutdown_requested = True
            raise LLMError("Connection refused")

        with patch("eidos.complete", side_effect=fail_and_shutdown):
            with patch("eidos.time.sleep"):
                run_loop(self.config, persona=None)

        # Should have logged the failure as an observation
        obs = read_recent_observations(self.config, max_chars=50000, max_count=100)
        llm_errors = [o for o in obs if o.get("tool") == "llm_error"]
        self.assertGreater(len(llm_errors), 0)
        self.assertIn("Connection refused", llm_errors[0]["output"])

    def test_timeout_during_completion_recovers(self):
        """LLM hanging mid-inference → timeout → logged, agent continues."""
        from llm import LLMError

        self.config.goal_path.write_text("Test goal")
        write_memory(self.config, "Test memory")

        tick_count = [0]

        def counting_side_effect(*a, **kw):
            tick_count[0] += 1
            if tick_count[0] >= 2:
                import eidos
                eidos._shutdown_requested = True
            raise LLMError("Request timed out after 300s")

        with patch("eidos.complete", side_effect=counting_side_effect):
            with patch("eidos.time.sleep"):
                run_loop(self.config, persona=None)

        # Agent didn't hang — it ran at least one tick
        self.assertGreater(tick_count[0], 0)

    def test_consecutive_failures_trigger_restart(self):
        """After N consecutive LLM failures, attempt_llm_restart is called."""
        from llm import LLMError

        self.config.goal_path.write_text("Test goal")
        write_memory(self.config, "Test memory")
        self.config.llm_max_consecutive_failures = 3
        self.config.llm_restart_cmd = "systemctl restart llama-server"

        failure_count = [0]

        def fail_then_shutdown(*a, **kw):
            failure_count[0] += 1
            if failure_count[0] > 4:
                import eidos
                eidos._shutdown_requested = True
            raise LLMError("Connection refused")

        with patch("eidos.complete", side_effect=fail_then_shutdown):
            with patch("eidos.time.sleep"):
                with patch("eidos.attempt_llm_restart", return_value=True) as mock_restart:
                    run_loop(self.config, persona=None)

        # Should have attempted restart after 3 consecutive failures
        self.assertTrue(mock_restart.called,
                        "LLM restart not attempted after consecutive failures")

    def test_reasoning_exhausted_bumps_tokens(self):
        """ReasoningExhausted → next tick gets higher max_tokens budget."""
        from llm import ReasoningExhausted, LLMError

        self.config.goal_path.write_text("Test goal")
        write_memory(self.config, "Test memory")
        self.config.llm_max_tokens = 1024
        self.config.llm_token_backoff_step = 512
        self.config.llm_max_tokens_ceiling = 4096

        call_count = [0]

        def exhaust_then_shutdown(*a, **kw):
            call_count[0] += 1
            max_tok = kw.get("max_tokens", 1024)
            if call_count[0] == 1:
                raise ReasoningExhausted(
                    reasoning="thinking...",
                    reasoning_tokens=max_tok,
                    max_tokens=max_tok,
                )
            # Second call should have higher budget
            if call_count[0] == 2:
                self.assertGreater(max_tok, 1024,
                                   "max_tokens was not increased after reasoning exhaustion")
                import eidos
                eidos._shutdown_requested = True
                raise LLMError("stopping test")
            raise LLMError("stopping")

        with patch("eidos.complete", side_effect=exhaust_then_shutdown):
            with patch("eidos.time.sleep"):
                run_loop(self.config, persona=None)

        self.assertGreaterEqual(call_count[0], 2)

    def test_wal_written_during_llm_failure(self):
        """WAL is persisted during LLM error handling (survives a hard crash).

        Note: clean shutdown (via _shutdown_requested) clears the WAL.
        This test verifies WAL is written mid-tick by checking it during
        the error path, before the loop exits.
        """
        from llm import LLMError

        self.config.goal_path.write_text("Test goal")
        write_memory(self.config, "Test memory")

        wal_snapshots = []

        def capture_wal_then_shutdown(*a, **kw):
            # After first LLM error, WAL should be written before sleep.
            # We capture it in the sleep mock.
            wal = read_wal(self.config)
            if wal:
                wal_snapshots.append(wal)
            import eidos
            eidos._shutdown_requested = True

        def fail_always(*a, **kw):
            raise LLMError("server gone")

        with patch("eidos.complete", side_effect=fail_always):
            with patch("eidos.time.sleep", side_effect=capture_wal_then_shutdown):
                run_loop(self.config, persona=None)

        # WAL was written during the error path (captured before shutdown cleared it)
        self.assertGreater(len(wal_snapshots), 0, "WAL was never written during LLM failure")
        self.assertIn("tick_number", wal_snapshots[0])
        self.assertGreater(wal_snapshots[0]["consecutive_failures"], 0)


# ===========================================================================
#  4. COLD START FROM EMPTY WORKSPACE
# ===========================================================================


class TestColdStart(unittest.TestCase):
    """Verify first start on a fresh Pi with nothing in the workspace.

    No goal.md, no memory.md, no observations.jsonl, no persona.json.
    The agent should initialize cleanly and idle until a goal appears.
    """

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.config = _make_config(self.tmp)
        import eidos as _k
        _k._shutdown_requested = False

    def tearDown(self):
        import shutil
        import eidos as _k
        _k._shutdown_requested = False
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_recover_on_empty_workspace(self):
        """recover() on bare workspace should not crash."""
        wal = recover(self.config)
        self.assertEqual(wal, {})

    def test_memory_created_on_first_start(self):
        """recover() creates initial memory.md if missing."""
        self.assertFalse(self.config.memory_path.exists())
        recover(self.config)
        self.assertTrue(self.config.memory_path.exists())
        content = read_memory(self.config)
        self.assertTrue(len(content) > 0, "memory.md should have initial content")

    def test_no_goal_means_idle(self):
        """Without goal.md, run_loop should exit cleanly in mock mode."""
        self.config.mock_mode = True
        self.config.tick_interval_s = 0
        self.config.persona_enabled = False
        write_memory(self.config, "initial")

        # No goal.md — mock mode exits immediately
        with patch("eidos.time.sleep"):
            run_loop(self.config, persona=None)

        # Should exit cleanly (no exceptions)

    def test_observations_created_by_recovery(self):
        """recover() creates observations with startup system messages."""
        recover(self.config)
        self.assertTrue(self.config.observations_path.exists())
        obs = read_recent_observations(self.config, max_chars=50000, max_count=100)
        system_obs = [o for o in obs if o.get("tool") == "system"]
        self.assertGreater(len(system_obs), 0,
                           "Recovery should log at least one system observation")

    def test_empty_workspace_dirs_created(self):
        """All required subdirectories exist after config creation."""
        # _make_config already creates them, but verify they exist
        self.assertTrue(self.config.snapshots_dir.exists())
        self.assertTrue(self.config.interventions_dir.exists())
        self.assertTrue(self.config.outputs_dir.exists())

    def test_persona_loads_fresh_on_cold_start(self):
        """Loading persona on a fresh workspace returns sensible defaults."""
        from persona import load_persona
        persona = load_persona(self.config.workspace)
        self.assertEqual(persona["level"], 1)
        self.assertEqual(persona["xp"], 0)
        self.assertIn("mood", persona)

    def test_goal_then_start_begins_ticking(self):
        """Writing goal.md then starting the loop should produce a tick."""
        self.config.mock_mode = True
        self.config.tick_interval_s = 0
        self.config.persona_enabled = False
        write_memory(self.config, "fresh start")
        self.config.goal_path.write_text("Monitor CPU temperature every tick")

        tick_count = [0]

        def mock_complete(messages, cfg, max_tokens=None):
            tick_count[0] += 1
            import eidos
            eidos._shutdown_requested = True
            return '<tool>bash</tool>\n<args>{"cmd": "echo 42"}</args>'

        with patch("eidos.complete", side_effect=mock_complete):
            with patch("eidos.time.sleep"):
                run_loop(self.config, persona=None)

        self.assertEqual(tick_count[0], 1)
        # Should have observations from the tool execution
        obs = read_recent_observations(self.config, max_chars=50000, max_count=100)
        bash_obs = [o for o in obs if o.get("tool") == "bash"]
        self.assertEqual(len(bash_obs), 1)
        self.assertTrue(bash_obs[0]["success"])

    def test_cold_start_full_sequence(self):
        """End-to-end: recover → load persona → run one tick → verify state."""
        self.config.mock_mode = True
        self.config.tick_interval_s = 0
        self.config.persona_enabled = True

        # 1. Recovery on empty workspace
        wal = recover(self.config)
        self.assertEqual(wal, {})

        # 2. Load persona
        from persona import load_persona, compute_traits
        persona = load_persona(self.config.workspace)
        compute_traits(persona)
        self.assertEqual(persona["level"], 1)

        # 3. Write a goal
        self.config.goal_path.write_text("Say hello")

        # 4. Run one tick
        def mock_complete(messages, cfg, max_tokens=None):
            import eidos
            eidos._shutdown_requested = True
            return '<tool>bash</tool>\n<args>{"cmd": "echo hello"}</args>'

        with patch("eidos.complete", side_effect=mock_complete):
            with patch("eidos.time.sleep"):
                run_loop(self.config, persona=persona, wal=wal)

        # 5. Clean shutdown clears WAL (correct behavior)
        wal_after = read_wal(self.config)
        self.assertEqual(wal_after, {},
                         "WAL should be cleared after graceful shutdown")

        # 6. Verify observation was logged (proves the tick executed)
        obs = read_recent_observations(self.config, max_chars=50000, max_count=100)
        bash_obs = [o for o in obs if o.get("tool") == "bash"]
        self.assertGreater(len(bash_obs), 0)

        # 7. Verify shutdown was logged
        shutdown_obs = [o for o in obs if "shutting down" in o.get("output", "").lower()]
        self.assertGreater(len(shutdown_obs), 0)

    def test_interventions_dir_empty_no_crash(self):
        """Reading interventions from empty dir should return empty list."""
        from memory import read_interventions
        result = read_interventions(self.config)
        self.assertEqual(result, [])

    def test_refresh_jobs_on_empty_workspace(self):
        """refresh_jobs with no jobs.json should return empty list."""
        jobs = refresh_jobs(self.config)
        self.assertEqual(jobs, [])

    def test_knowledge_dir_missing_no_crash(self):
        """Recall tool should handle missing knowledge dir gracefully."""
        from tools import tool_recall
        self.config.knowledge_enabled = True
        result = tool_recall({"query": "anything"}, self.config)
        # Should not crash — either returns empty results or an error message
        self.assertIsNotNone(result.output)


if __name__ == "__main__":
    unittest.main()
