"""Tests for tools module."""

import json
import os
import sys
import tempfile
import time
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent))

import unittest
from unittest.mock import patch, MagicMock
from config import Config
from parser import ToolCall
from tools import (
    execute_tool, tool_bash, tool_write_file, tool_read_file,
    tool_bg_run, tool_bg_check, tool_http_get,
    tool_remember, tool_update_plan, tool_memorize, tool_recall,
    tool_goal_complete, tool_ask_supervisor, tool_plan_goal,
    refresh_jobs, _read_jobs, _write_jobs,
)


class TestTools(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.config = Config()
        self.config.workspace_dir = os.path.join(self.tmp, "workspace")
        os.makedirs(self.config.workspace_dir)
        os.makedirs(str(self.config.outputs_dir))

    def tearDown(self):
        import shutil
        shutil.rmtree(self.tmp, ignore_errors=True)

    # --- bash ---

    def test_bash_simple(self):
        result = tool_bash({"cmd": "echo hello"}, self.config)
        self.assertTrue(result.success)
        self.assertIn("hello", result.output)

    def test_bash_blocked(self):
        result = tool_bash({"cmd": "rm -rf /"}, self.config)
        self.assertFalse(result.success)
        self.assertIn("BLOCKED", result.output)

    def test_bash_no_cmd(self):
        result = tool_bash({}, self.config)
        self.assertFalse(result.success)

    def test_bash_timeout(self):
        self.config.cmd_timeout_s = 1
        result = tool_bash({"cmd": "sleep 10"}, self.config)
        self.assertFalse(result.success)
        self.assertIn("TIMEOUT", result.output)

    def test_bash_truncation(self):
        self.config.output_truncation_chars = 50
        result = tool_bash({"cmd": "python3 -c \"print('x' * 200)\""}, self.config)
        self.assertTrue(result.success)
        self.assertIn("[truncated", result.output)
        self.assertIsNotNone(result.full_output_path)
        self.assertTrue(Path(result.full_output_path).exists())

    def test_bash_stderr_captured(self):
        result = tool_bash({"cmd": "echo err >&2"}, self.config)
        self.assertIn("err", result.output)
        self.assertIn("[stderr]", result.output)

    def test_bash_nonzero_exit(self):
        result = tool_bash({"cmd": "exit 42"}, self.config)
        self.assertFalse(result.success)

    # --- write_file / read_file ---

    def test_write_file(self):
        path = os.path.join(self.config.workspace_dir, "test.txt")
        result = tool_write_file({"path": path, "content": "hello world"}, self.config)
        self.assertTrue(result.success)
        self.assertEqual(Path(path).read_text(), "hello world")

    def test_write_file_no_path(self):
        result = tool_write_file({"content": "data"}, self.config)
        self.assertFalse(result.success)

    def test_write_file_creates_subdirs(self):
        path = os.path.join(self.config.workspace_dir, "sub", "dir", "file.txt")
        result = tool_write_file({"path": path, "content": "nested"}, self.config)
        self.assertTrue(result.success)
        self.assertEqual(Path(path).read_text(), "nested")

    def test_write_file_relative_path(self):
        result = tool_write_file({"path": "notes.txt", "content": "relative"}, self.config)
        self.assertTrue(result.success)
        self.assertEqual(Path(self.config.workspace_dir, "notes.txt").read_text(), "relative")

    def test_read_file(self):
        path = os.path.join(self.config.workspace_dir, "read_me.txt")
        Path(path).write_text("contents here")
        result = tool_read_file({"path": path}, self.config)
        self.assertTrue(result.success)
        self.assertIn("contents here", result.output)

    def test_read_file_no_path(self):
        result = tool_read_file({}, self.config)
        self.assertFalse(result.success)

    def test_read_file_missing(self):
        result = tool_read_file({"path": "/nonexistent/path"}, self.config)
        self.assertFalse(result.success)

    def test_write_file_traversal_blocked(self):
        result = tool_write_file({"path": "../../etc/evil.txt", "content": "bad"}, self.config)
        self.assertFalse(result.success)
        self.assertIn("escapes workspace", result.output)

    def test_read_file_traversal_blocked(self):
        result = tool_read_file({"path": "../../etc/passwd"}, self.config)
        self.assertFalse(result.success)
        self.assertIn("escapes workspace", result.output)

    def test_write_file_absolute_traversal_blocked(self):
        result = tool_write_file({"path": "/tmp/evil.txt", "content": "bad"}, self.config)
        self.assertFalse(result.success)
        self.assertIn("escapes workspace", result.output)

    def test_read_file_symlink_traversal_blocked(self):
        """Symlink pointing outside workspace should be blocked."""
        link = Path(self.config.workspace_dir) / "sneaky_link"
        link.symlink_to("/etc/hosts")
        result = tool_read_file({"path": str(link)}, self.config)
        self.assertFalse(result.success)
        self.assertIn("escapes workspace", result.output)

    # --- bg_run ---

    def test_bg_run_success(self):
        result = tool_bg_run({"cmd": "echo bg_test", "name": "test_job"}, self.config)
        self.assertTrue(result.success)
        self.assertIn("test_job", result.output)
        self.assertIn("PID", result.output)
        # Job registered in ledger
        jobs = _read_jobs(self.config)
        self.assertEqual(len(jobs), 1)
        self.assertEqual(jobs[0]["name"], "test_job")
        self.assertEqual(jobs[0]["status"], "running")

    def test_bg_run_missing_args(self):
        result = tool_bg_run({"cmd": "echo hi"}, self.config)
        self.assertFalse(result.success)
        result = tool_bg_run({"name": "x"}, self.config)
        self.assertFalse(result.success)

    def test_bg_run_blocked_command(self):
        result = tool_bg_run({"cmd": "rm -rf /", "name": "evil"}, self.config)
        self.assertFalse(result.success)
        self.assertIn("BLOCKED", result.output)

    # --- bg_check ---

    def test_bg_check_completed_job(self):
        # Start a fast job that will finish immediately
        tool_bg_run({"cmd": "echo done", "name": "fast"}, self.config)
        time.sleep(0.5)  # Let it finish
        # Poll briefly — process may need a moment to be reaped
        for _ in range(5):
            result = tool_bg_check({"name": "fast"}, self.config)
            if "completed" in result.output:
                break
            time.sleep(0.2)
        self.assertTrue(result.success)
        self.assertIn("completed", result.output)

    def test_bg_check_missing_name(self):
        result = tool_bg_check({}, self.config)
        self.assertFalse(result.success)

    def test_bg_check_unknown_job(self):
        result = tool_bg_check({"name": "nonexistent"}, self.config)
        self.assertFalse(result.success)
        self.assertIn("No job named", result.output)

    def test_bg_check_reads_output(self):
        tool_bg_run({"cmd": "echo bg_output_here", "name": "reader"}, self.config)
        time.sleep(0.3)
        result = tool_bg_check({"name": "reader"}, self.config)
        self.assertIn("bg_output_here", result.output)

    # --- http_get ---

    @patch("urllib.request.urlopen")
    def test_http_get_success(self, mock_urlopen):
        mock_resp = MagicMock()
        mock_resp.read.return_value = b"Hello, World!"
        mock_resp.__enter__ = lambda s: s
        mock_resp.__exit__ = MagicMock(return_value=False)
        mock_urlopen.return_value = mock_resp

        result = tool_http_get({"url": "http://example.com"}, self.config)
        self.assertTrue(result.success)
        self.assertIn("Hello, World!", result.output)

    @patch("urllib.request.urlopen")
    def test_http_get_error(self, mock_urlopen):
        import urllib.error
        mock_urlopen.side_effect = urllib.error.URLError("connection refused")
        result = tool_http_get({"url": "http://bad.host"}, self.config)
        self.assertFalse(result.success)
        self.assertIn("HTTP error", result.output)

    def test_http_get_no_url(self):
        result = tool_http_get({}, self.config)
        self.assertFalse(result.success)

    @patch("urllib.request.urlopen")
    def test_http_get_truncation(self, mock_urlopen):
        self.config.output_truncation_chars = 50
        mock_resp = MagicMock()
        mock_resp.read.return_value = b"x" * 200
        mock_resp.__enter__ = lambda s: s
        mock_resp.__exit__ = MagicMock(return_value=False)
        mock_urlopen.return_value = mock_resp

        result = tool_http_get({"url": "http://example.com"}, self.config)
        self.assertTrue(result.success)
        self.assertIn("[truncated", result.output)

    # --- remember ---

    def test_remember_success(self):
        # Write initial memory
        Path(self.config.workspace_dir, "memory.md").write_text("initial memory")
        result = tool_remember({"note": "important fact"}, self.config)
        self.assertTrue(result.success)
        memory = Path(self.config.workspace_dir, "memory.md").read_text()
        self.assertIn("important fact", memory)
        self.assertIn("[Remembered at", memory)

    def test_remember_no_note(self):
        result = tool_remember({}, self.config)
        self.assertFalse(result.success)

    def test_remember_budget_cap(self):
        """Memory should be capped at context_memory_max_chars."""
        Path(self.config.workspace_dir, "memory.md").write_text("x" * 5000)
        self.config.context_memory_max_chars = 200
        result = tool_remember({"note": "new note"}, self.config)
        self.assertTrue(result.success)
        memory = Path(self.config.workspace_dir, "memory.md").read_text()
        self.assertLessEqual(len(memory), 200)
        self.assertIn("new note", memory)

    # --- goal_complete ---

    def test_goal_complete_success(self):
        result = tool_goal_complete({"summary": "task done", "evidence": "tests pass"}, self.config)
        self.assertTrue(result.success)
        self.assertIn("GOAL_COMPLETE", result.output)
        self.assertIn("task done", result.output)

    def test_goal_complete_no_summary(self):
        result = tool_goal_complete({}, self.config)
        self.assertFalse(result.success)

    # --- ask_supervisor ---

    def test_ask_supervisor_success(self):
        result = tool_ask_supervisor({"question": "need help?"}, self.config)
        self.assertTrue(result.success)
        self.assertIn("need help?", result.output)
        # Verify written to pending_questions.jsonl
        qpath = Path(self.config.workspace_dir) / "pending_questions.jsonl"
        self.assertTrue(qpath.exists())
        entry = json.loads(qpath.read_text().strip())
        self.assertEqual(entry["question"], "need help?")
        self.assertEqual(entry["status"], "pending")

    def test_ask_supervisor_no_question(self):
        result = tool_ask_supervisor({}, self.config)
        self.assertFalse(result.success)

    def test_ask_supervisor_appends(self):
        """Multiple questions should be separate JSONL lines."""
        tool_ask_supervisor({"question": "q1"}, self.config)
        tool_ask_supervisor({"question": "q2"}, self.config)
        qpath = Path(self.config.workspace_dir) / "pending_questions.jsonl"
        lines = qpath.read_text().strip().splitlines()
        self.assertEqual(len(lines), 2)

    # --- refresh_jobs ---

    def test_refresh_jobs_empty(self):
        jobs = refresh_jobs(self.config)
        self.assertEqual(jobs, [])

    def test_refresh_jobs_marks_completed(self):
        # Write a fake job with a PID that doesn't exist
        _write_jobs(self.config, [{
            "name": "dead", "pid": 999999999, "cmd": "echo x",
            "started": "2026-01-01T00:00:00Z", "status": "running",
            "output_path": "",
        }])
        jobs = refresh_jobs(self.config)
        self.assertEqual(jobs[0]["status"], "completed")

    def test_refresh_jobs_keeps_already_completed(self):
        _write_jobs(self.config, [{
            "name": "done", "pid": 1, "cmd": "echo x",
            "started": "2026-01-01T00:00:00Z", "status": "completed",
            "output_path": "",
        }])
        jobs = refresh_jobs(self.config)
        self.assertEqual(jobs[0]["status"], "completed")

    # --- dispatch ---

    def test_unknown_tool(self):
        call = ToolCall(tool="nonexistent", args={}, raw="")
        result = execute_tool(call, self.config)
        self.assertFalse(result.success)
        self.assertIn("Unknown tool", result.output)

    def test_execute_tool_dispatch(self):
        call = ToolCall(tool="bash", args={"cmd": "echo dispatch_test"}, raw="")
        result = execute_tool(call, self.config)
        self.assertTrue(result.success)
        self.assertIn("dispatch_test", result.output)

    def test_execute_tool_dispatch_all_names(self):
        """All registered tool names should be dispatchable."""
        from tools import TOOLS
        for name in TOOLS:
            self.assertIn(name, TOOLS)

    # --- update_plan ---

    def test_update_plan_success(self):
        Path(self.config.workspace_dir, "plan.md").write_text("# Plan\nStep 1")
        result = tool_update_plan({"note": "Step 1 complete, moving to step 2"}, self.config)
        self.assertTrue(result.success)
        plan = Path(self.config.workspace_dir, "plan.md").read_text()
        self.assertIn("Step 1 complete", plan)
        self.assertIn("[Updated at", plan)

    def test_update_plan_no_note(self):
        result = tool_update_plan({}, self.config)
        self.assertFalse(result.success)

    def test_update_plan_budget_cap(self):
        Path(self.config.workspace_dir, "plan.md").write_text("x" * 2000)
        self.config.context_plan_max_chars = 200
        result = tool_update_plan({"note": "new step"}, self.config)
        self.assertTrue(result.success)
        plan = Path(self.config.workspace_dir, "plan.md").read_text()
        self.assertLessEqual(len(plan), 200)
        self.assertIn("new step", plan)

    def test_update_plan_creates_file(self):
        """update_plan should work even if plan.md doesn't exist yet."""
        result = tool_update_plan({"note": "first plan note"}, self.config)
        self.assertTrue(result.success)
        self.assertTrue(Path(self.config.workspace_dir, "plan.md").exists())

    # --- memorize ---

    def test_memorize_success(self):
        result = tool_memorize({
            "fact": "pip requires --break-system-packages on Bookworm",
            "tags": ["pip", "bookworm"],
            "category": "facts",
        }, self.config)
        self.assertTrue(result.success)
        self.assertIn("Stored to long-term memory", result.output)
        # Verify file was created
        knowledge_dir = self.config.knowledge_dir / "facts"
        self.assertTrue(any(knowledge_dir.glob("*.md")))

    def test_memorize_no_fact(self):
        result = tool_memorize({"tags": ["x"]}, self.config)
        self.assertFalse(result.success)
        self.assertIn("'fact' required", result.output)

    def test_memorize_no_tags(self):
        """Missing tags defaults to ['general'] and succeeds."""
        result = tool_memorize({"fact": "something"}, self.config)
        self.assertTrue(result.success)

    def test_memorize_tags_as_string(self):
        """Tags can be provided as comma-separated string."""
        result = tool_memorize({
            "fact": "test fact",
            "tags": "tag1, tag2, tag3",
        }, self.config)
        self.assertTrue(result.success)

    def test_memorize_invalid_category_defaults(self):
        """Invalid category should default to 'facts'."""
        result = tool_memorize({
            "fact": "test",
            "tags": ["t1"],
            "category": "bogus",
        }, self.config)
        self.assertTrue(result.success)

    def test_memorize_via_dispatch(self):
        call = ToolCall(tool="memorize", args={
            "fact": "dispatch test",
            "tags": ["test"],
        }, raw="")
        result = execute_tool(call, self.config)
        self.assertTrue(result.success)

    # --- recall ---

    def test_recall_empty_store(self):
        result = tool_recall({"query": "anything"}, self.config)
        self.assertTrue(result.success)
        self.assertIn("No relevant knowledge found", result.output)

    def test_recall_no_query(self):
        result = tool_recall({}, self.config)
        self.assertFalse(result.success)
        self.assertIn("'query' required", result.output)

    def test_recall_finds_stored_entry(self):
        """Store something, then recall it."""
        from knowledge import rebuild_index, _invalidate_bm25_cache

        tool_memorize({
            "fact": "The DHT22 sensor is connected on GPIO pin 4",
            "tags": ["dht22", "gpio", "sensor"],
            "category": "facts",
        }, self.config)

        # Force full rebuild so BM25 picks up the new entry
        rebuild_index(self.config)
        _invalidate_bm25_cache()

        result = tool_recall({"query": "DHT22 sensor GPIO pin"}, self.config)
        self.assertTrue(result.success)
        self.assertIn("DHT22", result.output)

    def test_recall_via_dispatch(self):
        call = ToolCall(tool="recall", args={"query": "test"}, raw="")
        result = execute_tool(call, self.config)
        self.assertTrue(result.success)

    # --- new tools in registry ---

    def test_new_tools_registered(self):
        from tools import TOOLS
        self.assertIn("update_plan", TOOLS)
        self.assertIn("memorize", TOOLS)
        self.assertIn("recall", TOOLS)
        self.assertIn("plan_goal", TOOLS)

    # --- plan_goal ---

    def test_plan_goal_missing_goal(self):
        result = tool_plan_goal({}, self.config)
        self.assertFalse(result.success)
        self.assertIn("'goal' required", result.output)

    @patch("llm.planning_complete")
    def test_plan_goal_writes_subgoals(self, mock_planning):
        mock_planning.return_value = "Goal: Test\nDone when: tests pass\n\n- [ ] Write tests\n- [ ] Run tests"
        result = tool_plan_goal({"goal": "Test the app", "context": "Python project"}, self.config)
        self.assertTrue(result.success)
        self.assertIn("Subgoals generated", result.output)
        # Verify subgoals.md was written
        from memory import read_subgoals
        subgoals = read_subgoals(self.config)
        self.assertIn("- [ ] Write tests", subgoals)

    @patch("llm.planning_complete")
    def test_plan_goal_handles_llm_error(self, mock_planning):
        from llm import LLMError
        mock_planning.side_effect = LLMError("connection failed")
        result = tool_plan_goal({"goal": "Test"}, self.config)
        self.assertFalse(result.success)
        self.assertIn("Planning model error", result.output)

    def test_plan_goal_via_dispatch(self):
        call = ToolCall(tool="plan_goal", args={"goal": "test"}, raw="")
        with patch("llm.planning_complete", return_value="- [ ] Step 1"):
            result = execute_tool(call, self.config)
        self.assertTrue(result.success)


if __name__ == "__main__":
    unittest.main()
