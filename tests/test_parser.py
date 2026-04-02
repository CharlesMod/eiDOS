"""Tests for tool call parser."""

import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent))

import unittest
from parser import parse_tool_call


class TestParser(unittest.TestCase):

    def test_valid_tool_call(self):
        text = 'I will list files.\n<tool>bash</tool>\n<args>{"cmd": "ls -la"}</args>'
        result = parse_tool_call(text)
        self.assertIsNotNone(result)
        self.assertEqual(result.tool, "bash")
        self.assertEqual(result.args, {"cmd": "ls -la"})

    def test_no_tool_call(self):
        text = "I would suggest running ls to see the files."
        result = parse_tool_call(text)
        self.assertIsNone(result)

    def test_malformed_json(self):
        text = '<tool>bash</tool>\n<args>{cmd: "ls"}</args>'
        result = parse_tool_call(text)
        self.assertIsNone(result)

    def test_multiple_tool_calls_takes_first(self):
        text = (
            '<tool>bash</tool>\n<args>{"cmd": "ls"}</args>\n'
            '<tool>read_file</tool>\n<args>{"path": "/tmp/x"}</args>'
        )
        result = parse_tool_call(text)
        self.assertIsNotNone(result)
        self.assertEqual(result.tool, "bash")

    def test_whitespace_in_tags(self):
        text = '<tool>  bash  </tool>\n<args>  {"cmd": "pwd"}  </args>'
        result = parse_tool_call(text)
        self.assertIsNotNone(result)
        self.assertEqual(result.tool, "bash")

    def test_nested_json_args(self):
        text = '<tool>write_file</tool>\n<args>{"path": "/tmp/x", "content": "line1\\nline2"}</args>'
        result = parse_tool_call(text)
        self.assertIsNotNone(result)
        self.assertEqual(result.args["path"], "/tmp/x")

    def test_tool_name_case_insensitive(self):
        text = '<tool>BASH</tool>\n<args>{"cmd": "ls"}</args>'
        result = parse_tool_call(text)
        self.assertIsNotNone(result)
        self.assertEqual(result.tool, "bash")

    def test_args_not_dict(self):
        text = '<tool>bash</tool>\n<args>["ls", "-la"]</args>'
        result = parse_tool_call(text)
        self.assertIsNone(result)

    def test_multiline_args(self):
        text = '<tool>write_file</tool>\n<args>\n{\n  "path": "/tmp/test.txt",\n  "content": "hello world"\n}\n</args>'
        result = parse_tool_call(text)
        self.assertIsNotNone(result)
        self.assertEqual(result.args["content"], "hello world")

    def test_raw_preserved(self):
        raw = '<tool>bash</tool>\n<args>{"cmd": "ls"}</args>'
        text = f"Thinking about it.\n{raw}\nDone."
        result = parse_tool_call(text)
        self.assertIsNotNone(result)
        self.assertIn("<tool>", result.raw)


if __name__ == "__main__":
    unittest.main()
