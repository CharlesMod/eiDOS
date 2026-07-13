"""Phase 6: behavioral glue (glue.py) — strain, condition label, ACC teeth.

Pure-function tests over outcome windows, plus the persisted record/read round-trip.
The doctrine these enforce: detected states drive MECHANISM (gate frustration bump,
condition label), not prose the model can ignore.
"""

import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

import glue
from config import Config


def F(kind="exec", sig="bash:probe"):
    return {"ok": False, "kind": kind, "sig": sig}


def OK(tool="bash"):
    return {"ok": True, "kind": "", "sig": "", "tool": tool}


def TH():
    """A thought-only tick — logged ok=True, but it's reflection, not action."""
    return {"ok": True, "kind": "", "sig": "", "tool": "thought"}


class TestStrain(unittest.TestCase):

    def test_empty_is_zero(self):
        self.assertEqual(glue.compute_strain([]), 0)

    def test_failures_accumulate(self):
        self.assertEqual(glue.compute_strain([F("exec", "a"), F("exec", "b")]),
                         glue.STRAIN_FAIL * 2)

    def test_same_signature_hits_harder(self):
        diff = glue.compute_strain([F(sig="a"), F(sig="b")])
        same = glue.compute_strain([F(sig="a"), F(sig="a")])
        self.assertGreater(same, diff)

    def test_progress_relieves(self):
        strained = [F(sig="a"), F(sig="a"), F(sig="a")]
        s1 = glue.compute_strain(strained)
        s2 = glue.compute_strain(strained + [OK()])
        self.assertLess(s2, s1)

    def test_capped(self):
        self.assertLessEqual(glue.compute_strain([F(sig="a")] * 50), glue.STRAIN_CAP)


class TestRepeatedFailureSignature(unittest.TestCase):

    def test_detects_trailing_run(self):
        outs = [OK(), F(sig="x"), F(sig="x"), F(sig="x")]
        self.assertEqual(glue.repeated_failure_signature(outs, k=3), "x")

    def test_run_broken_by_success(self):
        outs = [F(sig="x"), F(sig="x"), OK(), F(sig="x")]
        self.assertEqual(glue.repeated_failure_signature(outs, k=3), "")

    def test_different_sigs_dont_count(self):
        outs = [F(sig="x"), F(sig="y"), F(sig="z")]
        self.assertEqual(glue.repeated_failure_signature(outs, k=3), "")

    def test_below_k(self):
        outs = [F(sig="x"), F(sig="x")]
        self.assertEqual(glue.repeated_failure_signature(outs, k=3), "")


class TestCondition(unittest.TestCase):

    def test_empty_is_stable(self):
        self.assertEqual(glue.compute_condition([]), "STABLE")

    def test_focused_on_success_run(self):
        self.assertEqual(glue.compute_condition([OK(), OK(), OK(), OK()]), "FOCUSED")

    def test_strained_on_failure_cluster(self):
        self.assertEqual(glue.compute_condition([F(sig="a"), F(sig="b"), F(sig="c")]), "STRAINED")

    def test_recovery_after_streak(self):
        self.assertEqual(glue.compute_condition([F(sig="a"), F(sig="a"), OK()]), "RECOVERY")


class TestGateBump(unittest.TestCase):

    def test_healthy_no_bump(self):
        self.assertEqual(glue.gate_frustration_bump([OK(), OK()]), 0)

    def test_strained_bumps(self):
        self.assertGreaterEqual(glue.gate_frustration_bump([F(sig="a")] * 4), 1)

    def test_repeated_signature_bumps_harder(self):
        spread = glue.gate_frustration_bump([F(sig="a"), F(sig="b"), F(sig="c"), F(sig="d")])
        same = glue.gate_frustration_bump([F(sig="a")] * 4)
        self.assertGreater(same, spread)


class TestRumination(unittest.TestCase):
    """Phase 9: analysis-paralysis teeth. A window dominated by thought-only ticks bumps the
    gate (and labels the condition RUMINATING) — narration must not read as progress."""

    def test_pure_thought_window_max_bump(self):
        outs = [TH()] * glue.RUMINATE_WINDOW
        self.assertEqual(glue.rumination_bump(outs), 2)

    def test_interleaved_bookkeeping_still_detected(self):
        # th th act th th th — 5 thoughts in the 6-window despite one real action between
        outs = [TH(), TH(), OK("note_append"), TH(), TH(), TH()]
        self.assertEqual(glue.rumination_bump(outs), 1)

    def test_real_action_clears_instantly(self):
        # Heavy thinking, but the LAST tick acted — no nag, the model already self-corrected
        outs = [TH(), TH(), TH(), TH(), TH(), OK()]
        self.assertEqual(glue.rumination_bump(outs), 0)
        self.assertEqual(glue.rumination_streak(outs), 0)

    def test_below_threshold_no_bump(self):
        outs = [OK(), TH(), OK(), TH(), OK(), TH()]
        self.assertEqual(glue.rumination_bump(outs), 0)

    def test_condition_ruminating(self):
        self.assertEqual(glue.compute_condition([TH()] * 5), "RUMINATING")

    def test_thoughts_do_not_read_as_focused(self):
        # 4 ok=True thoughts used to satisfy the FOCUSED success count
        self.assertNotEqual(glue.compute_condition([TH(), TH(), TH(), TH()]), "FOCUSED")

    def test_thoughts_do_not_relieve_strain(self):
        strained = [F(sig="a"), F(sig="a"), F(sig="a")]
        self.assertEqual(glue.compute_strain(strained + [TH()]),
                         glue.compute_strain(strained))

    def test_thought_is_not_recovery(self):
        self.assertNotEqual(glue.compute_condition([F(sig="a"), F(sig="a"), TH()]), "RECOVERY")

    def test_legacy_rows_without_tool_unaffected(self):
        # Pre-phase-9 outcome rows have no "tool" key — strain/condition behave as before
        legacy_ok = {"ok": True, "kind": "", "sig": ""}
        self.assertEqual(glue.compute_condition([legacy_ok] * 4), "FOCUSED")
        strained = [F(sig="a"), F(sig="a"), F(sig="a")]
        self.assertLess(glue.compute_strain(strained + [legacy_ok]),
                        glue.compute_strain(strained))


class TestPersistence(unittest.TestCase):

    def setUp(self):
        self.config = Config()
        self.config.workspace_dir = tempfile.mkdtemp()

    def test_record_and_read_roundtrip(self):
        glue.record_outcome(self.config, success=False, fail_kind="exec", signature="bash:x")
        glue.record_outcome(self.config, success=True)
        rows = glue.recent_outcomes(self.config)
        self.assertEqual(len(rows), 2)
        self.assertFalse(rows[0]["ok"])
        self.assertEqual(rows[0]["sig"], "bash:x")
        self.assertTrue(rows[1]["ok"])

    def test_bounded_on_disk(self):
        for i in range(80):
            glue.record_outcome(self.config, success=False, signature=f"s{i}")
        rows = glue.recent_outcomes(self.config, n=1000)
        self.assertLessEqual(len(rows), glue._PERSIST)

    def test_missing_file_is_empty(self):
        self.assertEqual(glue.recent_outcomes(self.config), [])


class TestEscalationHint(unittest.TestCase):
    """The STRAINED pivot-steer: fires only on a trailing run of the SAME failure
    signature, and points at the delegate instead of another retry."""

    def test_three_same_sig_fires_and_mentions_delegate(self):
        hint = glue.escalation_hint([F(sig="a"), F(sig="a"), F(sig="a")])
        self.assertTrue(hint)
        self.assertIn("delegate", hint)

    def test_mixed_signatures_do_not_fire(self):
        self.assertEqual(glue.escalation_hint([F(sig="a"), F(sig="b"), F(sig="a")]), "")

    def test_trailing_success_clears(self):
        self.assertEqual(
            glue.escalation_hint([F(sig="a"), F(sig="a"), F(sig="a"), OK()]), "")

    def test_two_in_a_row_not_enough(self):
        self.assertEqual(glue.escalation_hint([F(sig="a"), F(sig="a")]), "")


class TestMotifBrake(unittest.TestCase):
    """The content-aware rumination brake: a loop that rephrases ONE theme every tick (evading the
    thought-only counter) is caught by content-token-pair dominance, where token_jaccard is blind."""

    def test_theme_loop_fires_healthy_exploration_does_not(self):
        loop = ["the wall is there. the words stay. a small solid space.",
                "the wall. the words. they stay. a strange truth.",
                "the wall is there, the words stay, a heavy truth to carry.",
                "the wall. the words. a repeating truth, a pulse.",
                "the wall the words they stay the foundation of it all."]
        healthy = ["ooh what's in this file", "i made a folder it's mine",
                   "feeling low i'll rest", "what happens if i run that",
                   "the stories folder has three things"]
        self.assertGreaterEqual(glue.motif_dominance(loop), glue.MOTIF_DOMINANCE)
        self.assertEqual(glue.motif_bump(loop), glue.MOTIF_BUMP)
        self.assertLess(glue.motif_dominance(healthy), glue.MOTIF_DOMINANCE)
        self.assertEqual(glue.motif_bump(healthy), 0)

    def test_too_few_bodies_is_never_a_loop(self):
        self.assertEqual(glue.motif_dominance(["the wall", "the wall"]), 0.0)


if __name__ == "__main__":
    unittest.main()
