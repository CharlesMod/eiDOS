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


class TestParserSloppyJSON(unittest.TestCase):
    """Test parser recovery from common 4B model mistakes."""

    # --- Trailing junk after JSON ---

    def test_trailing_angle_bracket(self):
        """Real failure: model outputs {"cmd": "pwd && ls -la"}>"""
        text = '<tool>bash</tool>\n<args>{"cmd": "pwd && ls -la"}></args>'
        result = parse_tool_call(text)
        self.assertIsNotNone(result, "Should recover from trailing >")
        self.assertEqual(result.args["cmd"], "pwd && ls -la")

    def test_trailing_comma(self):
        text = '<tool>bash</tool>\n<args>{"cmd": "ls"},</args>'
        result = parse_tool_call(text)
        self.assertIsNotNone(result, "Should recover from trailing comma")

    def test_trailing_semicolon(self):
        text = '<tool>bash</tool>\n<args>{"cmd": "ls"};</args>'
        result = parse_tool_call(text)
        self.assertIsNotNone(result)

    def test_trailing_period(self):
        text = '<tool>bash</tool>\n<args>{"cmd": "ls"}.</args>'
        result = parse_tool_call(text)
        self.assertIsNotNone(result)

    def test_trailing_newline_text(self):
        text = '<tool>bash</tool>\n<args>{"cmd": "ls"}\nDone.</args>'
        result = parse_tool_call(text)
        self.assertIsNotNone(result)

    # --- Markdown fences ---

    def test_markdown_json_fence(self):
        text = '<tool>bash</tool>\n<args>```json\n{"cmd": "ls"}\n```</args>'
        result = parse_tool_call(text)
        self.assertIsNotNone(result, "Should strip markdown fences")
        self.assertEqual(result.args["cmd"], "ls")

    def test_markdown_plain_fence(self):
        text = '<tool>bash</tool>\n<args>```\n{"cmd": "ls"}\n```</args>'
        result = parse_tool_call(text)
        self.assertIsNotNone(result)

    # --- HTML entities ---

    def test_html_entities(self):
        text = '<tool>bash</tool>\n<args>{&quot;cmd&quot;: &quot;ls&quot;}</args>'
        result = parse_tool_call(text)
        self.assertIsNotNone(result, "Should decode HTML entities")
        self.assertEqual(result.args["cmd"], "ls")

    def test_html_amp(self):
        """HTML entities that break JSON parsing get decoded."""
        text = '<tool>bash</tool>\n<args>{&quot;cmd&quot;: &quot;echo a &amp;&amp; echo b&quot;}</args>'
        result = parse_tool_call(text)
        self.assertIsNotNone(result)
        self.assertEqual(result.args["cmd"], "echo a && echo b")

    # --- Single quotes ---

    def test_single_quotes(self):
        text = "<tool>bash</tool>\n<args>{'cmd': 'ls -la'}</args>"
        result = parse_tool_call(text)
        self.assertIsNotNone(result, "Should convert single quotes")
        self.assertEqual(result.args["cmd"], "ls -la")

    def test_single_quotes_with_embedded_double(self):
        """Don't break when values contain double quotes."""
        text = """<tool>bash</tool>\n<args>{'cmd': 'echo "hello"'}</args>"""
        result = parse_tool_call(text)
        # This case is ambiguous — conversion would break it. Should fail gracefully.
        # (json.loads will fail either way, which is correct)

    # --- Extra braces ---

    def test_extra_closing_brace(self):
        text = '<tool>bash</tool>\n<args>{"cmd": "ls"}}</args>'
        result = parse_tool_call(text)
        self.assertIsNotNone(result, "Should fix extra closing brace")
        self.assertEqual(result.args["cmd"], "ls")

    # --- Tag casing ---

    def test_uppercase_tags(self):
        text = '<TOOL>bash</TOOL>\n<ARGS>{"cmd": "ls"}</ARGS>'
        result = parse_tool_call(text)
        self.assertIsNotNone(result, "Should handle uppercase tags")
        self.assertEqual(result.tool, "bash")

    def test_mixed_case_tags(self):
        text = '<Tool>bash</Tool>\n<Args>{"cmd": "ls"}</Args>'
        result = parse_tool_call(text)
        self.assertIsNotNone(result, "Should handle mixed case tags")

    # --- Combined failures ---

    def test_markdown_plus_trailing_junk(self):
        text = '<tool>bash</tool>\n<args>```json\n{"cmd": "ls"}\n```></args>'
        result = parse_tool_call(text)
        self.assertIsNotNone(result, "Should handle stacked cleanup")

    def test_clean_json_still_works(self):
        """Clean JSON should still parse without cleanup overhead."""
        text = '<tool>remember</tool>\n<args>{"note": "all systems go"}</args>'
        result = parse_tool_call(text)
        self.assertIsNotNone(result)
        self.assertEqual(result.args["note"], "all systems go")

    # --- Things that should still fail ---

    def test_truly_broken_json_still_fails(self):
        """Unrecoverable garbage should return None, not crash."""
        text = '<tool>bash</tool>\n<args>NOT JSON AT ALL</args>'
        result = parse_tool_call(text)
        self.assertIsNone(result)

    def test_truncated_json_still_fails(self):
        """Incomplete JSON (model hit token limit mid-output)."""
        text = '<tool>bash</tool>\n<args>{"cmd": "ls</args>'
        result = parse_tool_call(text)
        self.assertIsNone(result)

    def test_unquoted_keys_still_fails(self):
        """JS-style unquoted keys — too dangerous to auto-fix."""
        text = '<tool>bash</tool>\n<args>{cmd: "ls"}</args>'
        result = parse_tool_call(text)
        self.assertIsNone(result)

    # --- Alternate format (TOOL: name PARAMS: {...}) ---

    def test_alt_format_basic(self):
        """Model uses TOOL: name PARAMS: {...} instead of XML tags."""
        text = 'TOOL: write_file PARAMS: {"path": "x.txt", "content": "hello"}'
        result = parse_tool_call(text)
        self.assertIsNotNone(result)
        self.assertEqual(result.tool, "write_file")
        self.assertEqual(result.args["path"], "x.txt")
        self.assertEqual(result.args["content"], "hello")

    def test_alt_format_with_reasoning(self):
        """Alt format preceded by reasoning text."""
        text = "I should create the file now.\nTOOL: goal_complete PARAMS: {\"summary\": \"done\"}"
        result = parse_tool_call(text)
        self.assertIsNotNone(result)
        self.assertEqual(result.tool, "goal_complete")
        self.assertEqual(result.args["summary"], "done")

    def test_alt_format_invalid_json(self):
        """Alt format with bad JSON should fail."""
        text = "TOOL: bash PARAMS: {cmd: ls}"
        result = parse_tool_call(text)
        self.assertIsNone(result)


if __name__ == "__main__":
    unittest.main()
