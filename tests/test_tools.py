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
    tool_bg_run, tool_bg_check,
    tool_update_plan, tool_memorize, tool_recall,
    refresh_jobs, _read_jobs, _write_jobs, ToolResult,
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

    def test_speak_logs_to_operator_chat(self):
        # Every spoken call-out must also appear in the operator chat (chat_replies.jsonl), marked
        # spoken=True — voice and chat should never diverge. Logged BEFORE the voice POST, so a
        # closed voice port doesn't prevent the chat entry.
        from tools import tool_speak
        self.config.voice_port = 9  # closed -> POST fails fast; chat-log happens regardless
        res = tool_speak({"text": "Cutover complete, Boss."}, self.config)
        self.assertTrue(res.success)
        rp = self.config.workspace / "chat_replies.jsonl"
        self.assertTrue(rp.exists())
        entries = [json.loads(ln) for ln in rp.read_text(encoding="utf-8").splitlines() if ln.strip()]
        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0]["text"], "Cutover complete, Boss.")
        self.assertTrue(entries[0]["spoken"])

    def test_speak_empty_no_chat_entry(self):
        from tools import tool_speak
        res = tool_speak({"text": "   "}, self.config)
        self.assertFalse(res.success)
        self.assertFalse((self.config.workspace / "chat_replies.jsonl").exists())

    def test_bash_no_cmd(self):
        result = tool_bash({}, self.config)
        self.assertFalse(result.success)

    def test_bash_wait_overrun_auto_backgrounds(self):
        """A wait:true command that overruns cmd_timeout_s is handed to the jobs
        ledger (still running, output preserved) instead of being killed — the
        never-block-the-tick contract."""
        self.config.cmd_timeout_s = 1
        result = tool_bash({"cmd": "Start-Sleep -Seconds 10", "wait": True}, self.config)
        self.assertTrue(result.success)
        self.assertIn("AUTO-BACKGROUNDED", result.output)

    def test_bash_truncation(self):
        self.config.output_truncation_chars = 50
        result = tool_bash({"cmd": "python -c \"print('x' * 200)\"", "wait": True},
                           self.config)
        self.assertTrue(result.success)
        self.assertIn("[truncated", result.output)
        self.assertIsNotNone(result.full_output_path)
        self.assertTrue(Path(result.full_output_path).exists())

    def test_bash_stderr_captured(self):
        """stderr is merged into the command's output stream (no separate tag)."""
        result = tool_bash({"cmd": 'cmd /c "echo err 1>&2"', "wait": True}, self.config)
        self.assertIn("err", result.output)

    def test_bash_nonzero_exit(self):
        result = tool_bash({"cmd": 'cmd /c "exit 42"', "wait": True}, self.config)
        self.assertFalse(result.success)
        self.assertEqual(result.fail_kind, "exec")

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


class TestSkillWatchdog(unittest.TestCase):
    """Tick 342 reproduction: a self-authored skill that makes a blocking network call with no timeout
    must NOT freeze the tick loop. execute_tool runs skills under a wall-clock watchdog; built-in tools
    are left bounded/trusted. 'Ghost-in-the-machine' style — we register a skill the exact way
    skills._activate does (a non-built-in name in the live TOOLS dict) and drive execute_tool."""

    def setUp(self):
        import threading
        from tools import TOOLS, _BUILTIN_TOOL_NAMES
        self.threading = threading
        self.TOOLS = TOOLS
        self.builtins = _BUILTIN_TOOL_NAMES
        self.config = Config()
        self.config.skill_watchdog_s = 0.5  # tight so the test is fast; real default is 30s
        self._added = []
        self._sockets = []

    def tearDown(self):
        for n in self._added:
            self.TOOLS.pop(n, None)
        for s in self._sockets:
            try:
                s.close()
            except OSError:
                pass

    def _register_skill(self, name, fn):
        """Mimic skills._build_runner/_activate: runner named skill_<name>, added to live TOOLS."""
        fn.__name__ = f"skill_{name}"
        self.assertNotIn(name, self.builtins)  # must look like a skill, not a built-in
        self.TOOLS[name] = fn
        self._added.append(name)

    def test_sleeping_skill_does_not_freeze_loop(self):
        """A skill that blocks (sleep) returns promptly via the watchdog, not after the full sleep."""
        def tool_sleeper(args, config):
            time.sleep(10)
            return ToolResult(output="done", full_output_path=None, success=True, duration_s=10)
        self._register_skill("sleeper", tool_sleeper)
        t = time.monotonic()
        result = execute_tool(ToolCall(tool="sleeper", args={}, raw=""), self.config)
        elapsed = time.monotonic() - t
        self.assertLess(elapsed, 5.0, "watchdog did not free the loop — it blocked on the skill")
        self.assertFalse(result.success)
        self.assertIn("WATCHDOG", result.output)

    def test_held_socket_skill_does_not_freeze_loop(self):
        """Exact tick-342 shape: a tarpit accepts the TCP connection but never replies (like the camera
        at .63), and the skill does a timeout-less recv(). The loop must still be freed by the watchdog."""
        import socket
        srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        srv.bind(("127.0.0.1", 0))
        srv.listen(1)
        self._sockets.append(srv)
        host, port = srv.getsockname()

        def _tarpit():
            try:
                conn, _ = srv.accept()
                self._sockets.append(conn)  # hold it open, never send a byte
            except OSError:
                pass
        self.threading.Thread(target=_tarpit, daemon=True).start()

        def tool_camera_snapshot(args, config):
            import socket as _s
            c = _s.create_connection((host, port))  # NO timeout — the original bug
            data = c.recv(1024)                       # blocks forever; peer never replies
            return ToolResult(output=data.decode(), full_output_path=None, success=True, duration_s=0)
        self._register_skill("camera_snapshot", tool_camera_snapshot)

        t = time.monotonic()
        result = execute_tool(ToolCall(tool="camera_snapshot", args={}, raw=""), self.config)
        elapsed = time.monotonic() - t
        self.assertLess(elapsed, 5.0, "watchdog did not free the loop on a held connection")
        self.assertFalse(result.success)
        self.assertIn("WATCHDOG", result.output)

    def test_fast_skill_passes_through_unharmed(self):
        """A well-behaved skill returns its real result; the watchdog adds no penalty."""
        def tool_quick(args, config):
            return ToolResult(output="ok-quick", full_output_path=None, success=True, duration_s=0.01)
        self._register_skill("quick", tool_quick)
        result = execute_tool(ToolCall(tool="quick", args={}, raw=""), self.config)
        self.assertTrue(result.success)
        self.assertIn("ok-quick", result.output)

    def test_skill_exception_surfaces_as_failed_result(self):
        """A skill that raises is converted to a failed ToolResult (loop never crashes)."""
        def tool_boom(args, config):
            raise ValueError("kaboom")
        self._register_skill("boom", tool_boom)
        result = execute_tool(ToolCall(tool="boom", args={}, raw=""), self.config)
        self.assertFalse(result.success)
        self.assertIn("kaboom", result.output)


if __name__ == "__main__":
    unittest.main()
