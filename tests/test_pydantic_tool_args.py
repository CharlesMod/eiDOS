"""Typed tool-argument boundary tests."""

import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from config import Config
from parser import ToolCall
from tools import (
    _BUILTIN_TOOL_NAMES,
    _TOOL_ARG_MODELS,
    _read_jobs,
    _write_jobs,
    execute_tool,
    tool_bash,
    tool_bg_run,
    tool_http_request,
    tool_write_file,
)


class TestPydanticToolArgs(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.config = Config()
        self.config.workspace_dir = os.path.join(self.tmp, "workspace")
        os.makedirs(self.config.workspace_dir)
        os.makedirs(str(self.config.outputs_dir))

    def tearDown(self):
        import shutil

        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_bash_command_alias_still_works(self):
        result = tool_bash({"command": "python -c \"print('typed-ok')\"", "wait": True}, self.config)

        self.assertTrue(result.success, result.output)
        self.assertIn("typed-ok", result.output)

    def test_bash_rejects_extra_field_without_running(self):
        marker = Path(self.config.workspace_dir) / "should_not_exist.txt"
        cmd = (
            f"{sys.executable} -c "
            f"\"from pathlib import Path; Path({str(marker)!r}).write_text('bad')\""
        )

        result = tool_bash({"cmd": cmd, "wait": True, "surprise": "execute"}, self.config)

        self.assertFalse(result.success)
        self.assertEqual(result.fail_kind, "args")
        self.assertIn("invalid bash arguments", result.output)
        self.assertFalse(marker.exists())

    def test_write_file_rejects_extra_field_without_writing(self):
        target = Path(self.config.workspace_dir) / "should_not_write.txt"

        result = tool_write_file(
            {"path": str(target), "content": "bad", "surprise": "write anyway"},
            self.config,
        )

        self.assertFalse(result.success)
        self.assertEqual(result.fail_kind, "args")
        self.assertIn("invalid write_file arguments", result.output)
        self.assertFalse(target.exists())

    def test_bg_run_rejects_extra_field_without_dispatching(self):
        marker = Path(self.config.workspace_dir) / "bg_should_not_exist.txt"
        cmd = (
            f"{sys.executable} -c "
            f"\"from pathlib import Path; Path({str(marker)!r}).write_text('bad')\""
        )

        result = tool_bg_run(
            {"cmd": cmd, "name": "typed_bad", "surprise": "dispatch"},
            self.config,
        )

        self.assertFalse(result.success)
        self.assertEqual(result.fail_kind, "args")
        self.assertIn("invalid bg_run arguments", result.output)
        self.assertEqual(_read_jobs(self.config), [])
        self.assertFalse(marker.exists())

    def test_http_request_rejects_unsafe_method_before_network(self):
        result = tool_http_request(
            {"url": "http://127.0.0.1:9", "method": "TRACE"},
            self.config,
        )

        self.assertFalse(result.success)
        self.assertEqual(result.fail_kind, "args")
        self.assertIn("invalid http_request arguments", result.output)

    def test_http_request_rejects_ambiguous_body(self):
        result = tool_http_request(
            {
                "url": "http://127.0.0.1:9",
                "method": "POST",
                "json": {"hello": "world"},
                "data": "raw",
            },
            self.config,
        )

        self.assertFalse(result.success)
        self.assertEqual(result.fail_kind, "args")
        self.assertIn("provide either json or data", result.output)

    def test_every_builtin_tool_has_dispatch_boundary_model(self):
        self.assertEqual(_BUILTIN_TOOL_NAMES - set(_TOOL_ARG_MODELS), set())

    def test_dispatch_rejects_extra_field_for_non_pilot_tool_without_side_effect(self):
        result = execute_tool(
            ToolCall(
                tool="update_plan",
                args={"note": "this should not be written", "surprise": "pwn"},
                raw="",
            ),
            self.config,
        )

        self.assertFalse(result.success)
        self.assertEqual(result.fail_kind, "args")
        self.assertIn("invalid update_plan arguments", result.output)
        self.assertFalse((Path(self.config.workspace_dir) / "plan.md").exists())

    def test_dispatch_normalizes_aliases_for_remaining_tools(self):
        result = execute_tool(
            ToolCall(tool="manual", args={"section": "tts"}, raw=""),
            self.config,
        )

        self.assertTrue(result.success, result.output)
        self.assertIn("tts", result.output.lower())

    def test_dispatch_rejects_malformed_network_args_before_io(self):
        result = execute_tool(
            ToolCall(
                tool="tcp_probe",
                args={"ip": "127.0.0.1", "port": 70000},
                raw="",
            ),
            self.config,
        )

        self.assertFalse(result.success)
        self.assertEqual(result.fail_kind, "args")
        self.assertIn("port", result.output)

    def test_dispatch_rejects_speak_extra_before_chat_log(self):
        result = execute_tool(
            ToolCall(
                tool="speak",
                args={"text": "hello", "surprise": True},
                raw="",
            ),
            self.config,
        )

        self.assertFalse(result.success)
        self.assertEqual(result.fail_kind, "args")
        self.assertFalse((Path(self.config.workspace_dir) / "chat_replies.jsonl").exists())

    def test_jobs_ledger_rejects_bad_status_on_write(self):
        with self.assertRaises(ValueError):
            _write_jobs(self.config, [{"name": "bad", "status": "runningish"}])
        self.assertFalse(self.config.jobs_path.exists())

    def test_jobs_ledger_fails_closed_on_bad_persisted_record(self):
        self.config.jobs_path.write_text(
            '[{"name":"bad","status":"runningish"}]',
            encoding="utf-8",
        )

        self.assertEqual(_read_jobs(self.config), [])


if __name__ == "__main__":
    unittest.main()
