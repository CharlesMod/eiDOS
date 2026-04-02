"""Tests for tools module."""

import json
import os
import sys
import tempfile
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent))

import unittest
from config import Config
from parser import ToolCall
from tools import execute_tool, tool_bash, tool_write_file, tool_read_file


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
        # Full output file should exist
        self.assertTrue(Path(result.full_output_path).exists())

    def test_write_file(self):
        path = os.path.join(self.tmp, "test.txt")
        result = tool_write_file({"path": path, "content": "hello world"}, self.config)
        self.assertTrue(result.success)
        self.assertEqual(Path(path).read_text(), "hello world")

    def test_read_file(self):
        path = os.path.join(self.tmp, "read_me.txt")
        Path(path).write_text("contents here")
        result = tool_read_file({"path": path}, self.config)
        self.assertTrue(result.success)
        self.assertIn("contents here", result.output)

    def test_read_file_missing(self):
        result = tool_read_file({"path": "/nonexistent/path"}, self.config)
        self.assertFalse(result.success)

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


if __name__ == "__main__":
    unittest.main()
