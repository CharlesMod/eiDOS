"""context._creature_identity_block — the '## You' block that lets the creature's voice grow with it.

Dean's tone refinement: the creature should sound like who it actually is right now (stage, age,
grown-in tendencies), not fixed-baby-cute and not angsty. That only works if the creature can PERCEIVE
its own stage/traits — this block is what surfaces them. These tests pin that it reflects the real
life-stage and traits, reads grammatically, and degrades gracefully on missing/corrupt state.
"""

import json
import tempfile
import unittest
from pathlib import Path

import context


class _Cfg:
    def __init__(self, ws):
        self.workspace = ws
        self.creature_mode = True


def _ws(persona=None, creature=None):
    d = Path(tempfile.mkdtemp())
    if persona is not None:
        (d / "persona.json").write_text(json.dumps(persona), encoding="utf-8")
    if creature is not None:
        (d / "creature.json").write_text(json.dumps(creature), encoding="utf-8")
    return d


class TestCreatureIdentity(unittest.TestCase):
    def test_guardian_with_traits(self):
        ws = _ws({"level": 9, "total_ticks": 5200, "traits": ["curious", "resilient", "veteran"]},
                 {"last_stage": "guardian", "hatch": {"hatched": True}})
        out = context._creature_identity_block(_Cfg(ws))
        self.assertIn("## You", out)
        self.assertIn("guardian", out)
        self.assertIn("for a long time now", out)
        # grown-in tendencies surface so the voice can reflect them
        self.assertIn("curious", out)
        self.assertIn("resilient", out)

    def test_fresh_egg_no_traits(self):
        ws = _ws({"level": 1, "total_ticks": 3, "traits": []},
                 {"last_stage": "egg", "hatch": {"hatched": False}})
        out = context._creature_identity_block(_Cfg(ws))
        self.assertIn("an egg", out)              # article agreement
        self.assertIn("for only a few moments", out)
        self.assertIn("still figuring out who you are", out)

    def test_article_agreement(self):
        # adult → "an adult"; juvenile → "a juvenile"
        ws_a = _ws({"level": 6, "total_ticks": 900}, {"last_stage": "adult", "hatch": {"hatched": True}})
        self.assertIn("an adult", context._creature_identity_block(_Cfg(ws_a)))
        ws_j = _ws({"level": 3, "total_ticks": 900}, {"last_stage": "juvenile", "hatch": {"hatched": True}})
        self.assertIn("a juvenile", context._creature_identity_block(_Cfg(ws_j)))

    def test_falls_back_to_derived_stage(self):
        # No creature.json → derive stage from persona level (level 6, hatched-by-default fallback).
        ws = _ws({"level": 6, "total_ticks": 900})
        out = context._creature_identity_block(_Cfg(ws))
        self.assertIn("## You", out)
        self.assertTrue(any(s in out for s in ("adult", "juvenile", "hatchling")))

    def test_corrupt_state_does_not_raise(self):
        ws = Path(tempfile.mkdtemp())
        (ws / "creature.json").write_text("{not json", encoding="utf-8")
        # no persona.json either → load_persona returns defaults; must not raise, must be non-empty
        out = context._creature_identity_block(_Cfg(ws))
        self.assertIn("## You", out)


if __name__ == "__main__":
    unittest.main()
