"""Gates for the rest of the learning layer: the world-model (transition prediction + surprise),
the curiosity drive (novelty → intrinsic reward + restlessness), and habit formation."""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from nervous import (NervousBus, NeuromodulatoryState, WorldModel,  # noqa: E402
                     CuriosityDrive, RewardLearner)
from nervous.worldmodel import SURPRISE_MAX  # noqa: E402


class TestWorldModel(unittest.TestCase):
    def test_repetition_lowers_surprise(self):
        wm = WorldModel()
        first = wm.surprise("s", "a", "s2")          # never seen -> maximally novel
        self.assertAlmostEqual(first, SURPRISE_MAX)
        for _ in range(10):
            wm.observe("s", "a", "s2")               # the world becomes predictable
        self.assertLess(wm.surprise("s", "a", "s2"), first)   # what we predict no longer surprises

    def test_unexpected_transition_is_surprising(self):
        wm = WorldModel()
        for _ in range(10):
            wm.observe("s", "a", "s2")
        predictable = wm.surprise("s", "a", "s2")
        novel = wm.surprise("s", "a", "s_unexpected")
        self.assertGreater(novel, predictable)       # a never-before-seen outcome surprises more
        self.assertGreaterEqual(wm.snapshot()["contexts"], 1)


class TestCuriosity(unittest.TestCase):
    def test_novelty_yields_intrinsic_reward(self):
        cur = CuriosityDrive()
        hi = cur.observe(SURPRISE_MAX)               # maximally novel
        lo = cur.observe(0.0)                        # fully predictable
        self.assertGreater(hi, lo)
        self.assertGreaterEqual(lo, 0.0)

    def test_boredom_builds_and_bumps_arousal(self):
        bus = NervousBus()
        self.addCleanup(bus.close)
        nm = NeuromodulatoryState(bus, baseline_arousal=0.2)
        a0 = nm.arousal
        cur = CuriosityDrive(bus=bus, neuromod=nm, boredom_threshold=0.3, boredom_arousal_bump=0.1)
        for _ in range(30):
            cur.observe(0.0)                         # a long predictable lull
        self.assertGreater(cur.snapshot()["restlessness"], 0.3)   # restlessness builds
        self.assertGreater(nm.arousal, a0)                        # the itch to explore raises arousal

    def test_drive_event_published(self):
        bus = NervousBus()
        self.addCleanup(bus.close)
        from nervous import Kind, Modality, Delivery
        sub = bus.subscribe(topics={(Kind.drive, Modality.system)}, deliveries={Delivery.retained})
        CuriosityDrive(bus=bus).observe(3.0)
        self.assertIsNotNone(bus.recv(sub, timeout=1.0))


class TestHabits(unittest.TestCase):
    def test_over_learned_action_becomes_a_habit(self):
        rl = RewardLearner(alpha=0.6)
        for _ in range(8):
            rl.observe(situation="tidying", action="memorize_note", success=True, made_progress=True)
        rl.observe(situation="rare", action="oddball", success=True)   # one-off, not a habit
        habits = rl.habits(min_value=0.4, min_count=5)
        blob = " ".join(habits).lower()
        self.assertIn("memorize_note", blob)         # the reliably-rewarded routine is a habit
        self.assertNotIn("oddball", blob)            # a one-off is not


if __name__ == "__main__":
    unittest.main()
