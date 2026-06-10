"""Phase 2: the GBNF tick-output grammar (grammar.py).

Offline structural tests — no model needed. Live conformance was validated
during phase 2 (unconstrained 7/8 vs grammar 8/8 against house-ai with the
real system prompt; the unconstrained failure was an attribute-style
<tool name="..."> malformation the grammar makes unrepresentable).
"""

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

import grammar
from grammar import build_tick_grammar, tick_grammar_cached


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


class TestRegistryGrammarBuilds(unittest.TestCase):
    """The live tool registry must always produce a valid grammar."""

    def test_real_registry(self):
        from tools import TOOLS
        g = build_tick_grammar(TOOLS.keys())
        self.assertGreater(len(g), 200)
        self.assertIn("toolname ::=", g)


if __name__ == "__main__":
    unittest.main()
