"""Phase 1: the failure taxonomy (ToolResult.fail_kind) + async exit-code capture.

BIBLE section 5: type every failure — prose blobs can't be aggregated or drive
recovery playbooks. These tests pin the invariant that every failed ToolResult
leaves execute_tool with a non-empty fail_kind, and that the async bash path
recovers real exit codes via the sidecar file (pre-v2, any dead PID was marked
'completed' — failing background commands reported success).
"""

import os
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).parent.parent))

from config import Config
from parser import ToolCall
from tools import (
    TOOLS,
    ToolResult,
    collect_finished_jobs,
    execute_tool,
    tool_bash,
)


def _call(tool, args=None):
    return ToolCall(tool=tool, args=args or {}, raw="")


class TestFailKind(unittest.TestCase):

    def setUp(self):
        self.config = Config()
        self.config.workspace_dir = tempfile.mkdtemp()

    def test_success_default_is_empty(self):
        r = ToolResult("ok", None, True, 0.0)
        self.assertEqual(r.fail_kind, "")

    def test_unknown_tool_typed(self):
        r = execute_tool(_call("definitely_not_a_tool"), self.config)
        self.assertFalse(r.success)
        self.assertEqual(r.fail_kind, "no_such_tool")

    def test_bash_missing_cmd_is_args(self):
        r = tool_bash({}, self.config)
        self.assertFalse(r.success)
        self.assertEqual(r.fail_kind, "args")

    def test_bash_blocked_pattern(self):
        r = tool_bash({"cmd": "shutdown /s /t 0"}, self.config)
        self.assertFalse(r.success)
        self.assertEqual(r.fail_kind, "blocked")

    def test_untyped_failure_gets_backstop(self):
        def untyped(args, config):
            return ToolResult("failed for reasons", None, False, 0.0)
        with patch.dict(TOOLS, {"fake_untyped": untyped}):
            r = execute_tool(_call("fake_untyped"), self.config)
        self.assertFalse(r.success)
        self.assertEqual(r.fail_kind, "error")

    def test_raising_tool_is_crash(self):
        def boom(args, config):
            raise RuntimeError("kaboom")
        with patch.dict(TOOLS, {"fake_boom": boom}):
            r = execute_tool(_call("fake_boom"), self.config)
        self.assertFalse(r.success)
        self.assertEqual(r.fail_kind, "crash")

    def test_successful_result_passes_through_unkinded(self):
        def fine(args, config):
            return ToolResult("all good", None, True, 0.0)
        with patch.dict(TOOLS, {"fake_fine": fine}):
            r = execute_tool(_call("fake_fine"), self.config)
        self.assertTrue(r.success)
        self.assertEqual(r.fail_kind, "")


@unittest.skipUnless(os.name == "nt", "exit-code sidecar uses the PowerShell route")
class TestAsyncExitCodes(unittest.TestCase):

    def setUp(self):
        self.config = Config()
        self.config.workspace_dir = tempfile.mkdtemp()

    def _wait_finished(self, name, timeout=15.0):
        deadline = time.time() + timeout
        while time.time() < deadline:
            matches = [j for j in collect_finished_jobs(self.config)
                       if j.get("name") == name]
            if matches:
                return matches[0]
            time.sleep(0.5)
        self.fail(f"job {name} never finished")

    def test_sync_nonzero_exit_is_exec(self):
        r = tool_bash({"cmd": 'cmd /c "exit 3"', "wait": True}, self.config)
        self.assertFalse(r.success)
        self.assertEqual(r.fail_kind, "exec")

    def test_async_failure_reports_failed_with_exit_code(self):
        cmd = 'python -c "import sys; sys.exit(3)"'
        r = tool_bash({"cmd": cmd, "name": "failer"}, self.config)
        self.assertTrue(r.success, r.output)  # dispatch itself succeeds
        job = self._wait_finished("failer")
        self.assertEqual(job["status"], "failed")
        self.assertEqual(job["exit_code"], 3)

    def test_async_success_reports_completed_with_zero(self):
        tool_bash({"cmd": "echo ok", "name": "oker"}, self.config)
        job = self._wait_finished("oker")
        self.assertEqual(job["status"], "completed")
        self.assertEqual(job["exit_code"], 0)

    def test_explicit_exit_recovered_by_waiter(self):
        """A script that exits explicitly bypasses the PS epilogue, but the phase-4c
        waiter thread holds the Popen handle and records the REAL returncode anyway."""
        tool_bash({"cmd": "exit 0", "name": "explicit"}, self.config)
        job = self._wait_finished("explicit")
        self.assertEqual(job["status"], "completed")
        self.assertEqual(job["exit_code"], 0)

    def test_unknown_exit_never_lies(self):
        """The cross-restart recovery path (no waiter, no sidecar — e.g. a job whose
        waiter died with the previous eidos): exit UNKNOWN -> completed, never a
        fabricated failure."""
        import json as _json
        from tools import collect_finished_jobs
        job = {"name": "orphan", "pid": 99999999, "cmd": "echo x", "intent": "",
               "started": "2026-06-10T00:00:00Z", "started_ts": time.time(),
               "status": "running", "kind": "async",
               "output_path": "", "exit_path": str(Path(self.config.workspace_dir) / "nope.exit"),
               "notified": False}
        self.config.jobs_path.write_text(_json.dumps([job]))
        fins = collect_finished_jobs(self.config)
        self.assertEqual(len(fins), 1)
        self.assertEqual(fins[0]["status"], "completed")
        self.assertIsNone(fins[0]["exit_code"])


if __name__ == "__main__":
    unittest.main()
