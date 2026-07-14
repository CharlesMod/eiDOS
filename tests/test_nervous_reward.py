"""Reward-learning keystone gates: the reward function, TD value updates via RPE, dopamine -> neuromod,
dream-replay distilling lessons, felt-state-dependent keys, and persistence."""
import json
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from nervous import NervousBus, NeuromodulatoryState, RewardLearner, SleepCycle  # noqa: E402
from nervous.interoception import Interoception  # noqa: E402


def reader(**vals):
    base = {"ram_pct": None, "disk_free_gb": None, "cpu_pct": None,
            "vram_used_pct": None, "gpu_temp_c": None}
    base.update(vals)
    return lambda: base


class TestRewardFunction(unittest.TestCase):
    def test_reward_sign_tracks_outcome(self):
        rl = RewardLearner()
        good = rl.reward_of(success=True, made_progress=True, felt_delta=0.3, valence=0.4, strain=0)
        bad = rl.reward_of(success=False, made_progress=False, felt_delta=-0.3, valence=-0.4, strain=3)
        self.assertGreater(good, 0.5)
        self.assertLess(bad, -0.4)
        self.assertLessEqual(good, 1.0)
        self.assertGreaterEqual(bad, -1.0)

    def test_progress_and_felt_help(self):
        rl = RewardLearner()
        base = rl.reward_of(success=True, made_progress=False, felt_delta=0.0, valence=0.0, strain=0)
        prog = rl.reward_of(success=True, made_progress=True, felt_delta=0.0, valence=0.0, strain=0)
        self.assertGreater(prog, base)


class TestRisklessSuccessChannel(unittest.TestCase):
    """A structurally-riskless action (no failure was possible) must NOT book the free ±W_SUCCESS —
    otherwise the value learner collapses onto the safest no-op (the 'thought tends to go well' trap)."""

    def test_can_fail_false_zeroes_the_success_channel(self):
        rl = RewardLearner()
        risky = rl.reward_of(success=True, made_progress=False, felt_delta=0.0, valence=0.0,
                             strain=0, can_fail=True)
        riskless = rl.reward_of(success=True, made_progress=False, felt_delta=0.0, valence=0.0,
                                strain=0, can_fail=False)
        self.assertAlmostEqual(riskless, 0.0)          # nothing was at stake → no signal
        self.assertAlmostEqual(risky, 0.40)            # the full success reward when it could fail
        self.assertGreater(risky, riskless)

    def test_riskless_is_neutral_not_penalized(self):
        # zeroing is NOT a penalty — a penalty would teach "thinking goes badly". Progress/felt still pay.
        rl = RewardLearner()
        r = rl.reward_of(success=True, made_progress=True, felt_delta=0.2, valence=0.0,
                         strain=0, can_fail=False)
        self.assertGreater(r, 0.0)                     # lives on real progress + felt, never on the no-op

    def test_note_append_is_riskless_and_never_distilled_as_a_lesson(self):
        # note_append is pure reflection (write to my own notebook) — it must NOT book the free
        # +0.40, and even a poisoned cached value must NOT render as a "lean into it" lesson (the
        # 2026-07-13 morose-loop coach). Guards both the reward channel and lesson distillation.
        from nervous.reward import CANT_FAIL_ACTIONS
        self.assertIn("note_append", CANT_FAIL_ACTIONS)
        rl = RewardLearner(alpha=1.0)
        rl.observe(situation="calm", action='note_append {"text": "the wall is my horizon."}',
                   success=True, made_progress=False)
        v = next(iter(rl.values.values()))["v"]
        self.assertAlmostEqual(v, 0.0, places=4)          # no free reward for journaling
        # Force a high cached value (as if poisoned pre-fix) and confirm it is filtered from lessons.
        rl.values["calm::x::note_append {\"text\": \"the den is enough.\"}"] = {
            "v": 0.4, "n": 5, "situation": "calm", "action": 'note_append {"text": "the den is enough."}'}
        lessons = rl._distill_lessons()
        self.assertFalse(any("note_append" in l for l in lessons))

    def test_thought_action_is_gated_by_name_backstop(self):
        # observe() catches a riskless action by name even if a direct caller forgets can_fail=False.
        rl = RewardLearner(alpha=1.0)
        rl.observe(situation="calm", action="thought", success=True, made_progress=False)
        v = next(iter(rl.values.values()))["v"]
        self.assertAlmostEqual(v, 0.0, places=4)       # no free +0.40 fixed point for a bare thought
        rl.observe(situation="calm", action="bash", success=True, made_progress=False)
        vb = [e for e in rl.values.values() if e["action"] == "bash"][0]["v"]
        self.assertAlmostEqual(vb, 0.40, places=4)     # a can-fail action still books the win

    def test_successful_read_is_riskless_but_a_failed_read_still_penalizes(self):
        # 2026-07-14 rotating re-read spiral: a SUCCESSFUL read booked the free +0.40 (an OS read of an
        # existing file cannot fail), building V≈0.46 that pulled the creature back into re-reading.
        # Asymmetric fix: a successful read pays 0 on the success channel; a FAILED read (missing file)
        # STILL books -W_SUCCESS (a real error). Unlike CANT_FAIL, gated only when success is True.
        from nervous.reward import SUCCESS_RISKLESS_ACTIONS
        self.assertIn("read_file", SUCCESS_RISKLESS_ACTIONS)
        rl = RewardLearner(alpha=1.0)
        rl.observe(situation="calm", action='read_file {"path": "garden_summary.txt"}',
                   success=True, made_progress=False)
        v_ok = next(iter(rl.values.values()))["v"]
        self.assertAlmostEqual(v_ok, 0.0, places=4)        # the leak: no free +0.40 for a successful read
        rl.observe(situation="calm", action='read_file {"path": "ghost.txt"}',
                   success=False, made_progress=False)
        v_fail = [e for e in rl.values.values() if "ghost" in e["action"]][0]["v"]
        self.assertAlmostEqual(v_fail, -0.40, places=4)    # a genuine failed read still teaches

    def test_result_novelty_gates_re_reads_across_any_verb(self):
        # 2026-07-14: the name-gate closed read_file, but the re-read loop re-formed via `bash cat` and
        # self-authored read-wrapper skills (both booked +0.40). Result-novelty gates by OUTPUT, not
        # verb: an action whose result was seen recently pays 0 on the success channel — and it matches
        # ACROSS verbs (a read_file records content; a later skill/bash returning it registers stale).
        import hashlib
        sig = lambda s: hashlib.md5(s.encode()).hexdigest()
        rl = RewardLearner(alpha=1.0)
        # first bash-cat of a file: novel -> full success reward
        rl.observe(situation="explore", action='bash {"cmd":"cat notes.txt"}', success=True,
                   made_progress=False, result_sig=sig("NOTES CONTENT"))
        v_novel = [e for e in rl.values.values() if "cat notes" in e["action"]][0]["v"]
        self.assertAlmostEqual(v_novel, 0.40, places=4)
        # a self-authored skill returning the SAME bytes: stale cross-verb -> success channel 0
        rl.observe(situation="explore", action="nest_check {}", success=True,
                   made_progress=False, result_sig=sig("NOTES CONTENT"))
        v_skill = [e for e in rl.values.values() if e["action"] == "nest_check {}"][0]["v"]
        self.assertAlmostEqual(v_skill, 0.0, places=4)

    def test_result_novelty_leaves_new_content_and_failures_alone(self):
        import hashlib
        sig = lambda s: hashlib.md5(s.encode()).hexdigest()
        rl = RewardLearner(alpha=1.0)
        # a NEW-content write (novel result + real progress) still pays fully
        rl.observe(situation="s", action='write_file {"path":"a.txt"}', success=True,
                   made_progress=True, result_sig=sig("Written 100 chars"))
        v = [e for e in rl.values.values() if "write_file" in e["action"]][0]["v"]
        self.assertGreater(v, 0.5)
        # empty/None result is never treated as stale (no over-gating of no-output commands)
        rl.observe(situation="s", action='bash {"cmd":"mkdir d"}', success=True,
                   made_progress=False, result_sig=None)
        rl.observe(situation="s", action='bash {"cmd":"mkdir d"}', success=True,
                   made_progress=False, result_sig=None)
        vm = [e for e in rl.values.values() if "mkdir" in e["action"]][0]["v"]
        self.assertAlmostEqual(vm, 0.40, places=4)   # None result_sig -> normal success reward, not gated

    def test_a_successful_read_never_distills_into_a_habit(self):
        # "you reliably read garden_summary.txt" is the loop coaching itself — must never distill,
        # via EITHER the lesson path (_distill_lessons) or the habit path (habits(), previously unguarded).
        rl = RewardLearner(alpha=1.0)
        rl.values['calm::x::read_file {"path": "garden_summary.txt"}'] = {
            "v": 0.6, "n": 9, "situation": "calm", "action": 'read_file {"path": "garden_summary.txt"}'}
        self.assertFalse(any("read_file" in l for l in rl._distill_lessons()))
        self.assertFalse(any("read_file" in h for h in rl.habits(min_value=0.5, min_count=5)))


class TestValueLearning(unittest.TestCase):
    def test_repeated_good_outcome_raises_value(self):
        rl = RewardLearner(alpha=0.5)
        for _ in range(8):
            r = rl.observe(situation="research", action="read_docs", success=True, made_progress=True)
        # value for this (felt?, situation, action) should have climbed toward the reward, RPE shrinking
        self.assertGreater(rl.last["reward"], 0)
        self.assertLess(abs(rl.last["rpe"]), 0.3)        # prediction caught up to reality
        snap = rl.snapshot()
        self.assertGreaterEqual(snap["values"], 1)

    def test_bad_action_learns_negative_value(self):
        rl = RewardLearner(alpha=0.5)
        for _ in range(6):
            rl.observe(situation="stuck", action="retry_same", success=False, strain=3)
        # the value cache holds a negative estimate for the repeatedly-failing action
        neg = [v["v"] for v in rl.values.values() if v["action"] == "retry_same"]
        self.assertTrue(neg and min(neg) < 0)


class TestDopamine(unittest.TestCase):
    def test_reward_event_and_neuromod_bump(self):
        bus = NervousBus()
        self.addCleanup(bus.close)
        nm = NeuromodulatoryState(bus, baseline_arousal=0.3)
        a0 = nm.arousal
        rl = RewardLearner(bus=bus, neuromod=nm)
        # a big surprise (no prior value) fires a large RPE -> arousal bump (dopamine)
        rl.observe(situation="s", action="a", success=True, made_progress=True)
        self.assertGreater(nm.arousal, a0)


class TestDreamReplay(unittest.TestCase):
    def test_sleep_replay_distills_lessons(self):
        bus = NervousBus()
        self.addCleanup(bus.close)
        rl = RewardLearner(bus=bus, alpha=0.5, lesson_min_count=2, lesson_min_abs=0.1)
        for _ in range(4):
            rl.observe(situation="research", action="read_docs", success=True, made_progress=True)
        for _ in range(4):
            rl.observe(situation="stuck", action="retry_same", success=False, strain=3)
        out = rl.replay()
        self.assertGreater(out["replayed"], 0)
        self.assertTrue(out["lessons"])
        blob = " ".join(out["lessons"]).lower()
        self.assertIn("read_docs", blob)                 # a positive lesson surfaced
        self.assertTrue(any("badly" in l for l in out["lessons"]))   # and a cautionary one

    def test_sleepcycle_replays_via_learner(self):
        bus = NervousBus()
        self.addCleanup(bus.close)
        nm = NeuromodulatoryState(bus, baseline_arousal=0.1)
        nm.observe_interoception({"bars": {"ram": "ok"}})          # calm -> eligible to dream
        rl = RewardLearner(bus=bus, alpha=0.5, lesson_min_count=1, lesson_min_abs=0.05)
        rl.observe(situation="research", action="read_docs", success=True, made_progress=True)
        sleep = SleepCycle(bus, neuromod=nm, learner=rl, sleep_arousal=0.15)
        self.assertTrue(sleep.tick())                              # dreams
        self.assertGreaterEqual(len(rl.lessons()), 1)              # lessons exist after the dream


class TestStateDependence(unittest.TestCase):
    def test_felt_state_keys_the_value(self):
        bus = NervousBus()
        self.addCleanup(bus.close)
        rl = RewardLearner(bus=bus)
        # same action under two different felt bodies -> two distinct value keys (state-dependent)
        Interoception(bus, reader=reader(gpu_temp_c=92)).emit()    # in distress
        rl.observe(situation="x", action="a", success=True)
        Interoception(bus, reader=reader(cpu_pct=5)).emit()         # at ease
        rl.observe(situation="x", action="a", success=True)
        keys = list(rl.values.keys())
        self.assertEqual(len(keys), 2)


class TestPersistence(unittest.TestCase):
    def test_values_and_lessons_persist(self):
        d = tempfile.mkdtemp()
        vp = os.path.join(d, "values.json")
        lp = os.path.join(d, "lessons.json")
        ep = os.path.join(d, "exp.jsonl")
        rl = RewardLearner(value_path=vp, experience_path=ep, lessons_path=lp,
                           alpha=0.5, save_every=1, lesson_min_count=1, lesson_min_abs=0.05)
        rl.observe(situation="research", action="read_docs", success=True, made_progress=True)
        rl.replay()
        self.assertTrue(os.path.exists(vp))
        self.assertTrue(os.path.exists(ep))
        # a fresh learner loads the persisted value cache + lessons
        rl2 = RewardLearner(value_path=vp, experience_path=ep, lessons_path=lp)
        self.assertGreaterEqual(rl2.snapshot()["values"], 1)
        self.assertTrue(rl2.lessons())


if __name__ == "__main__":
    unittest.main()
