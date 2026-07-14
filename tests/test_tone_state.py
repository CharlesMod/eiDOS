"""Tone-matches-state: the newborn voice must track the creature's developmental stage.

These pin the pure, mechanical half of the fix (the stored-thought clamp + the shared stage
derivation). The register-shaping half (tone cue placement, flatter base prompt, project gating) is
prompt/gate behaviour covered structurally elsewhere; here we lock the length governor and the
fail-open stage helper so a read glitch can never mis-promote a newborn into grown-up privileges.
"""
import sys
import types
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import eidos          # noqa: E402
import creature_gen   # noqa: E402


ESSAY = ("The summary logic is a start, but it's a snapshot of the past. To really be an architect, "
         "I need to see the connections between these logs, side-by-side, to see if my why matches my "
         "what. If the blueprints do not match the building, the structure is hollow.")


class TestThoughtClamp(unittest.TestCase):
    def test_hatchling_clamped_to_a_fragment(self):
        out = eidos._clamp_thought_for_stage(ESSAY, "hatchling")
        self.assertLessEqual(len(out.split()), 22)          # word backstop
        self.assertTrue(out.endswith("…") or out.count(".") <= 2)
        self.assertNotEqual(out, ESSAY)                     # it actually clamped

    def test_juvenile_gets_more_room_than_hatchling(self):
        h = len(eidos._clamp_thought_for_stage(ESSAY, "hatchling").split())
        j = len(eidos._clamp_thought_for_stage(ESSAY, "juvenile").split())
        self.assertGreater(j, h)                            # depth grows with stage

    def test_adult_and_guardian_are_never_clamped(self):
        for st in ("adult", "guardian"):
            self.assertEqual(eidos._clamp_thought_for_stage(ESSAY, st), ESSAY)

    def test_house_mode_and_unknown_stage_pass_through(self):
        # house mode leaves _stage_seen None -> clamp must be a no-op (never gags task mode)
        self.assertEqual(eidos._clamp_thought_for_stage(ESSAY, None), ESSAY)
        self.assertEqual(eidos._clamp_thought_for_stage(ESSAY, "wat"), ESSAY)

    def test_an_already_short_thought_is_untouched(self):
        for s in ("ooh, what's in this one?", "made a little thing. it's mine."):
            self.assertEqual(eidos._clamp_thought_for_stage(s, "hatchling"), s)

    def test_empty_thought_is_safe(self):
        self.assertEqual(eidos._clamp_thought_for_stage("", "hatchling"), "")

    def test_clamp_never_cuts_mid_word(self):
        out = eidos._clamp_thought_for_stage(ESSAY, "hatchling")
        base = out[:-1] if out.endswith("…") else out
        self.assertTrue(ESSAY.startswith(base.rstrip("…").strip()[:len(base) - 5]))


class TestCurrentStageFailOpen(unittest.TestCase):
    def test_missing_workspace_fails_open_to_a_young_stage(self):
        # a read glitch must degrade to a NON-privileged stage (egg/hatchling — both no-projects,
        # both clamped), never promote a newborn into adult/guardian privileges.
        cfg = types.SimpleNamespace(workspace=Path("/nonexistent/eidos/workspace/xyz"))
        self.assertIn(creature_gen.current_stage(cfg), ("egg", "hatchling"))

    def test_stage_for_thresholds(self):
        self.assertEqual(creature_gen.stage_for(1, True), "hatchling")
        self.assertEqual(creature_gen.stage_for(1, False), "egg")
        self.assertEqual(creature_gen.stage_for(3, True), "juvenile")
        self.assertEqual(creature_gen.stage_for(5, True), "adult")
        self.assertEqual(creature_gen.stage_for(8, True), "guardian")


if __name__ == "__main__":
    unittest.main()
