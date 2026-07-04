"""Phase 2: the GBNF tick-output grammar (grammar.py).

Offline structural tests — no model needed. Live conformance was validated
during phase 2 (unconstrained 7/8 vs grammar 8/8 against house-ai with the
real system prompt; the unconstrained failure was an attribute-style
<tool name="..."> malformation the grammar makes unrepresentable).
"""

import re
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

import grammar
from grammar import build_tick_grammar, tick_grammar_cached
from parser import parse_reply, parse_tool_call

# The single-line thought production, pinned byte-exact: text may not contain '<'
# (tags) or a line break; the trailing ws is the only newline the rule can consume.
THOUGHT_RULE = r"thought ::= [^<\n\r] [^<\n\r]* ws"


class TestBuildTickGrammar(unittest.TestCase):

    def test_enumerates_tool_names(self):
        g = build_tick_grammar(["bash", "speak", "recall"])
        self.assertIn('"bash"', g)
        self.assertIn('"speak"', g)
        self.assertIn('"recall"', g)
        self.assertIn("toolname ::=", g)

    def test_has_reply_and_toolcall_and_json_rules(self):
        g = build_tick_grammar(["bash"])
        for rule in ("root ::=", "reply ::=", "toolcall ::=", "jobject ::=", "jstring ::="):
            self.assertIn(rule, g)

    def test_reply_precedes_toolcall_in_root(self):
        g = build_tick_grammar(["bash"])
        root = [ln for ln in g.splitlines() if ln.startswith("root ::=")][0]
        self.assertLess(root.index("reply"), root.index("toolcall"),
                        "reply must be orderable before toolcall (phase-3 reply-first)")

    def test_require_reply_makes_reply_mandatory(self):
        g = build_tick_grammar(["bash"], require_reply=True)
        root = [ln for ln in g.splitlines() if ln.startswith("root ::=")][0]
        # reply present without a '?' immediately after it -> mandatory
        self.assertIn("reply ws", root)
        self.assertNotIn("reply? ws toolcall", root)

    def test_empty_registry_raises(self):
        with self.assertRaises(ValueError):
            build_tick_grammar([])

    def test_unsafe_tool_name_raises(self):
        with self.assertRaises(ValueError):
            build_tick_grammar(["bash", "evil name"])
        with self.assertRaises(ValueError):
            build_tick_grammar(["semi;colon"])

    def test_dedup_and_sorted(self):
        g = build_tick_grammar(["zebra", "alpha", "alpha"])
        self.assertEqual(g.count('"alpha"'), 1)
        self.assertLess(g.index('"alpha"'), g.index('"zebra"'))


class TestThoughtSingleLine(unittest.TestCase):
    """Observed live (2026-07): a multi-line thought let a second prose line
    impersonate a call ('bash {"cmd":...}') before the real tags — wasted tokens
    and a phantom call format the model relearns from its own transcript. The
    grammar must make that second line unrepresentable, not merely discouraged."""

    def _thought_line(self, g: str) -> str:
        return [ln for ln in g.splitlines() if ln.startswith("thought ::=")][0]

    def test_thought_rule_is_single_line_form(self):
        for req in (False, True):
            g = build_tick_grammar(["bash"], require_reply=req)
            self.assertEqual(self._thought_line(g), THOUGHT_RULE)

    def test_no_production_admits_newline_in_thought_text(self):
        g = build_tick_grammar(["bash"])
        body = self._thought_line(g).split("::=", 1)[1]
        # Every character class in the thought production must be negated AND
        # exclude both line-break escapes; the sole rule reference is ws (bounded
        # whitespace), so nothing printable can follow a newline before the tags.
        classes = re.findall(r"\[(\^?)((?:[^\]\\]|\\.)*)\]", body)
        self.assertTrue(classes, "thought production lost its character classes")
        for negated, chars in classes:
            self.assertEqual(negated, "^", "thought text classes must be negated")
            self.assertIn(r"\n", chars)
            self.assertIn(r"\r", chars)
        refs = re.findall(r"(?<![\[\^\\<\"])\b([a-z]+)\b", re.sub(r"\[[^\]]*\]", "", body))
        self.assertEqual(set(refs), {"ws"})
        # and the old any-char-but-'<' production is gone
        self.assertNotIn("[^<] [^<]*", g)

    def test_everything_else_byte_stable(self):
        # The single-line thought must not disturb the proven-live tag structure.
        g = build_tick_grammar(["bash"])
        self.assertIn("root ::= ( thought reply? ws toolcall? | reply ws toolcall? | toolcall ) ws", g)
        self.assertIn('toolcall ::= "<tool>" toolname "</tool>" ws "<args>" jws jobject "</args>"', g)
        self.assertIn('reply ::= "<reply>" rtext "</reply>"', g)
        gr = build_tick_grammar(["bash"], require_reply=True)
        self.assertIn("root ::= thought? reply ws toolcall? ws", gr)


class TestGrammarShapedOutputsParse(unittest.TestCase):
    """Outputs shaped by the single-line-thought grammar must still round-trip
    through parser.py — the grammar constrains the sampler; the parser reads it."""

    def test_thought_plus_call(self):
        out = ('I should check if the nest directory exists.\n'
               '<tool>bash</tool> <args>{"cmd": "mkdir -p nest"}</args>')
        call = parse_tool_call(out)
        self.assertIsNotNone(call)
        self.assertEqual(call.tool, "bash")
        self.assertEqual(call.args, {"cmd": "mkdir -p nest"})

    def test_thought_only(self):
        self.assertIsNone(parse_tool_call("The house is quiet; nothing needs me this tick."))

    def test_thought_reply_call(self):
        out = ('One short thought. <reply>On it, Boss.</reply>\n'
               '<tool>bash</tool> <args>{"cmd": "ls"}</args>')
        self.assertEqual(parse_reply(out), "On it, Boss.")
        self.assertEqual(parse_tool_call(out).tool, "bash")


class TestGrammarCache(unittest.TestCase):

    def setUp(self):
        grammar._cache_key = None
        grammar._cache_value = None

    def test_same_registry_returns_identical_string(self):
        a = tick_grammar_cached(["bash", "speak"])
        b = tick_grammar_cached(["speak", "bash"])  # order-insensitive
        self.assertIs(a, b)

    def test_changed_registry_rebuilds(self):
        a = tick_grammar_cached(["bash"])
        b = tick_grammar_cached(["bash", "speak"])  # a skill hot-loaded
        self.assertIsNot(a, b)
        self.assertIn('"speak"', b)

    def test_require_reply_is_part_of_key(self):
        a = tick_grammar_cached(["bash"], require_reply=False)
        b = tick_grammar_cached(["bash"], require_reply=True)
        self.assertNotEqual(a, b)

    def test_invalidation_rebuilds_with_single_line_thought(self):
        # Every rebuild the cache serves — original, post-hot-load, and the
        # require_reply flip — must carry the single-line thought production.
        a = tick_grammar_cached(["bash"])
        b = tick_grammar_cached(["bash", "speak"])          # skill hot-loaded
        c = tick_grammar_cached(["bash", "speak"], require_reply=True)
        self.assertIsNot(a, b)
        self.assertIsNot(b, c)
        for g in (a, b, c):
            self.assertIn(THOUGHT_RULE, g)


class TestRegistryGrammarBuilds(unittest.TestCase):
    """The live tool registry must always produce a valid grammar."""

    def test_real_registry(self):
        from tools import TOOLS
        g = build_tick_grammar(TOOLS.keys())
        self.assertGreater(len(g), 200)
        self.assertIn("toolname ::=", g)


if __name__ == "__main__":
    unittest.main()
