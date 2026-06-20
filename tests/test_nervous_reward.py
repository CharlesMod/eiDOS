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
