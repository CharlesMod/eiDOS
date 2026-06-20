"""Tests for tool call parser."""

import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent))

import unittest
from parser import parse_tool_call, parse_reply


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

    # --- the bare `toolname {json}` format the creature prompt teaches (2026-06-20: creature-mode
    #     emitted exactly this and the parser DROPPED every call as "thought" — the gagged creature) ---
    def test_bare_format_simple(self):
        result = parse_tool_call('bash {"cmd": "ls -la"}')
        self.assertIsNotNone(result)
        self.assertEqual(result.tool, "bash")
        self.assertEqual(result.args, {"cmd": "ls -la"})

    def test_bare_format_after_a_thought(self):
        text = 'I feel the urge to act.\n\nbash {"cmd": "echo hi"}'
        result = parse_tool_call(text)
        self.assertIsNotNone(result)
        self.assertEqual(result.tool, "bash")

    def test_bare_format_nested_escaped_json(self):
        # the creature's actual t146 call: a python one-liner with escaped quotes + a Windows path.
        # A greedy/lazy regex mangles this; brace-matching survives it.
        text = ('I will write the schema.\n'
                'bash {"cmd":"python -c \\"import json; open(r\'C:\\\\x.json\',\'w\').write(\'{}\')\\""}')
        result = parse_tool_call(text)
        self.assertIsNotNone(result)
        self.assertEqual(result.tool, "bash")
        self.assertIn("python -c", result.args["cmd"])

    def test_bare_text_arg(self):
        result = parse_tool_call('bash df -h')
        self.assertIsNotNone(result)
        self.assertEqual(result.tool, "bash")
        self.assertEqual(result.args, {"cmd": "df -h"})

    def test_bare_does_not_false_trigger_on_prose(self):
        self.assertIsNone(parse_tool_call("I keep bashing my head and should note that down later."))
        self.assertIsNone(parse_tool_call("my plan note {a vague reminder to self, not json}"))

    def test_bare_format_wrapped_in_inline_backticks(self):
        # The live 5-min run (2026-06-20): the creature emitted a valid write_file EVERY tick, each
        # wrapped in markdown inline-code backticks — all silently dropped (false "rumination"). The
        # parser must see through a leading/trailing backtick.
        text = ('I need to record my progress now.\n\n'
                '`write_file {"path":"C:\\\\x\\\\growth.json","content":"{\\"tick\\": 110}"}`')
        result = parse_tool_call(text)
        self.assertIsNotNone(result)
        self.assertEqual(result.tool, "write_file")
        self.assertEqual(result.args["path"], "C:\\x\\growth.json")

    def test_bare_format_in_triple_fence(self):
        text = 'ok here goes\n```\nbash {"cmd":"ls -la"}\n```'
        result = parse_tool_call(text)
        self.assertIsNotNone(result)
        self.assertEqual(result.tool, "bash")
        self.assertEqual(result.args["cmd"], "ls -la")

    def test_bare_text_arg_in_backticks(self):
        result = parse_tool_call("`bash df -h`")
        self.assertIsNotNone(result)
        self.assertEqual(result.tool, "bash")
        self.assertEqual(result.args, {"cmd": "df -h"})

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

    def test_raw_text_args_wrapped_for_bash(self):
        """Raw text (non-JSON, no leading {) args for bash are auto-wrapped as cmd."""
        text = '<tool>bash</tool>\n<args>NOT JSON AT ALL</args>'
        result = parse_tool_call(text)
        self.assertIsNotNone(result)
        self.assertEqual(result.tool, "bash")
        self.assertEqual(result.args, {"cmd": "NOT JSON AT ALL"})


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


    def test_alt_format_invalid_json(self):
        """Alt format with bad JSON should fail."""
        text = "TOOL: bash PARAMS: {cmd: ls}"
        result = parse_tool_call(text)
        self.assertIsNone(result)


class TestParserUnescapedQuotes(unittest.TestCase):
    """Test recovery from unescaped quotes inside JSON string values.

    This is the most common parse failure from 4B thinking models.
    The model produces commands like:  grep -v "pattern"  inside JSON
    without escaping the inner quotes.
    """

    def test_grep_with_unescaped_quotes(self):
        """Real failure: {"cmd": "grep -v "^-""}"""
        text = '<tool>bash</tool>\n<args>{"cmd": "grep -v "^-""}</args>'
        result = parse_tool_call(text)
        self.assertIsNotNone(result, "Should recover from unescaped quotes in cmd")
        self.assertIn("grep", result.args["cmd"])
        self.assertIn("^-", result.args["cmd"])

    def test_free_with_unescaped_quotes(self):
        """Real failure from live test: free -h && df -h | grep -v "^-" """
        text = '<tool>bash</tool>\n<args>{"cmd": "free -h && df -h | grep -v "^-""}</args>'
        result = parse_tool_call(text)
        self.assertIsNotNone(result, "Should recover from unescaped quotes in cmd")
        self.assertIn("free -h", result.args["cmd"])

    def test_echo_with_unescaped_quotes(self):
        """{"cmd": "echo "hello world""}"""
        text = '<tool>bash</tool>\n<args>{"cmd": "echo "hello world""}</args>'
        result = parse_tool_call(text)
        self.assertIsNotNone(result)
        self.assertIn("hello world", result.args["cmd"])

    def test_sed_with_unescaped_quotes(self):
        """{"cmd": "sed -i "s/old/new/g" file.txt"}"""
        text = '<tool>bash</tool>\n<args>{"cmd": "sed -i "s/old/new/g" file.txt"}</args>'
        result = parse_tool_call(text)
        self.assertIsNotNone(result)
        self.assertIn("sed", result.args["cmd"])
        self.assertIn("s/old/new/g", result.args["cmd"])

    def test_awk_with_unescaped_quotes(self):
        """{"cmd": "awk "{print $1}" file.txt"}"""
        text = '<tool>bash</tool>\n<args>{"cmd": "awk "{print $1}" file.txt"}</args>'
        result = parse_tool_call(text)
        self.assertIsNotNone(result)
        self.assertIn("awk", result.args["cmd"])

    def test_find_with_unescaped_quotes(self):
        """{"cmd": "find / -name "*.conf""}"""
        text = '<tool>bash</tool>\n<args>{"cmd": "find / -name "*.conf""}</args>'
        result = parse_tool_call(text)
        self.assertIsNotNone(result)
        self.assertIn("find", result.args["cmd"])
        self.assertIn("*.conf", result.args["cmd"])

    def test_valid_json_not_affected(self):
        """Normal JSON with properly escaped quotes is unchanged."""
        text = '<tool>bash</tool>\n<args>{"cmd": "echo \\"hello\\""}</args>'
        result = parse_tool_call(text)
        self.assertIsNotNone(result)
        self.assertEqual(result.args["cmd"], 'echo "hello"')

    def test_unclosed_args_with_unescaped_quotes(self):
        """Unescaped quotes + missing </args> tag."""
        text = '<tool>bash</tool>\n<args>{"cmd": "grep "pattern" file.txt"}'
        result = parse_tool_call(text)
        self.assertIsNotNone(result)
        self.assertIn("grep", result.args["cmd"])

    def test_fallback_only_for_cmd_key(self):
        """Non-cmd keys with broken JSON still fail (no blind guessing)."""
        text = '<tool>write_file</tool>\n<args>{"path": "/tmp/"bad"", "content": "hi"}</args>'
        result = parse_tool_call(text)
        # This is ambiguous with multiple keys — fallback is cmd-only
        self.assertIsNone(result)

    def test_empty_cmd_after_extraction_fails(self):
        """Empty command value should not produce a tool call."""
        text = '<tool>bash</tool>\n<args>{"cmd": ""}</args>'
        result = parse_tool_call(text)
        # Empty cmd should still parse as valid JSON (it is), but cmd is empty
        self.assertIsNotNone(result)
        self.assertEqual(result.args["cmd"], "")


class TestParseReply(unittest.TestCase):

    def test_basic_reply(self):
        text = '<reply>Hello operator, everything is fine.</reply>'
        self.assertEqual(parse_reply(text), "Hello operator, everything is fine.")

    def test_reply_with_tool_call(self):
        text = ('<reply>Got it, checking now.</reply>\n'
                '<tool>bash</tool>\n<args>{"cmd": "uptime"}</args>')
        self.assertEqual(parse_reply(text), "Got it, checking now.")
        # Tool call should also be parseable
        self.assertIsNotNone(parse_tool_call(text))

    def test_no_reply(self):
        text = '<tool>bash</tool>\n<args>{"cmd": "ls"}</args>'
        self.assertIsNone(parse_reply(text))

    def test_empty_reply(self):
        text = '<reply>  </reply>'
        self.assertIsNone(parse_reply(text))

    def test_multiline_reply(self):
        text = '<reply>Line one.\nLine two.\nLine three.</reply>'
        self.assertEqual(parse_reply(text), "Line one.\nLine two.\nLine three.")

    def test_reply_case_insensitive(self):
        text = '<Reply>Hello!</Reply>'
        self.assertEqual(parse_reply(text), "Hello!")

    def test_reply_with_surrounding_text(self):
        text = 'I see the operator asked a question.\n<reply>Sure, here is the answer.</reply>\nNow back to work.'
        self.assertEqual(parse_reply(text), "Sure, here is the answer.")


if __name__ == "__main__":
    unittest.main()
