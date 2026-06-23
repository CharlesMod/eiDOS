"""Typed tool-argument boundary tests."""

import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from config import Config
from tools import (
    _read_jobs,
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


if __name__ == "__main__":
    unittest.main()
