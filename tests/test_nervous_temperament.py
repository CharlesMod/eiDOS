"""DMN temperament — the slow personality drift (initiative/persistence/caution).

Pins: the drift direction for success / failure / override, that it is SLOW (one tick barely moves
it), that persistence scales the gate's park threshold (the teeth), the disposition labels, and the
save/load round-trip so temperament survives a restart.
"""
import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from config import Config
from genome import GENE_LOADINGS, GENOME_FILENAME, LATENTS
from nervous.temperament import Temperament


def _temp():
    """A temperament born with a NEUTRAL genome (all genes 1.0, baselines 0.5), so this file's
    exact-value pins on the drift/spring/park mechanics stay deterministic — the congenital draw
    itself (and its consumer seams) is test_genome.py's job."""
    cfg = Config()
    cfg.workspace_dir = tempfile.mkdtemp()
    doc = {"v": 1, "seed": 0, "born_ts": 0.0,
           "latents": {n: 0.0 for n in LATENTS},
           "genes": {name: 1.0 for name in GENE_LOADINGS},
           "stamp_baselines": {"initiative": 0.5, "persistence": 0.5, "caution": 0.5}}
    (cfg.workspace / GENOME_FILENAME).write_text(json.dumps(doc), encoding="utf-8")
    return Temperament(config=cfg)


class TestDrift(unittest.TestCase):

    def test_starts_at_congenital_baseline(self):
        # Birth draw (4.3 divergence): a fresh creature starts AT its own drawn baselines. The
        # first birth births the genome (genome.py), whose stamp_baselines are clamped to
        # [BASELINE_LO, BASELINE_HI] = [0.38, 0.62] — strictly inside every disposition() band,
        # so a newborn is never pre-labeled at birth.
        t = _temp()
        for ax in ("initiative", "persistence", "caution"):
            self.assertEqual(getattr(t, ax), t.baselines[ax])
            self.assertLessEqual(abs(t.baselines[ax] - 0.5), 0.121)
        self.assertEqual(t.disposition(), "steady")

    def test_sustained_success_raises_initiative_lowers_caution(self):
        t = _temp()
        for _ in range(200):
            t.observe(success=True, failed=False, overridden=False)
        self.assertGreater(t.initiative, 0.7)
        self.assertGreater(t.persistence, 0.7)
        self.assertLess(t.caution, 0.3)

    def test_sustained_failure_raises_caution(self):
        t = _temp()
        for _ in range(200):
            t.observe(success=False, failed=True, overridden=False)
        self.assertGreater(t.caution, 0.7)

    def test_override_lowers_persistence_and_initiative(self):
        t = _temp()
        for _ in range(200):
            t.observe(success=False, failed=False, overridden=True)
        self.assertLess(t.persistence, 0.3)   # learn to let go sooner
        self.assertLess(t.initiative, 0.3)    # being overridden teaches deference
        self.assertGreater(t.caution, 0.7)

    def test_drift_is_slow(self):
        """One tick must barely move a setpoint — temperament is weather, not a knee-jerk."""
        t = _temp()
        before = t.initiative
        t.observe(success=True, failed=False, overridden=False)
        self.assertLess(abs(t.initiative - before), 0.05)

    def test_neutral_tick_does_not_move(self):
        t = _temp()
        before = (t.initiative, t.persistence, t.caution)
        t.observe(success=False, failed=False, overridden=False)
        self.assertEqual((t.initiative, t.persistence, t.caution), before)


class TestTeeth(unittest.TestCase):

    def test_persistence_scales_park_threshold(self):
        t = _temp()
        t.persistence = 0.0
        low = t.park_threshold(8)
        t.persistence = 1.0
        high = t.park_threshold(8)
        self.assertLess(low, high)            # dogged grinds longer before the gate parks it
        t.persistence = 0.5
        self.assertEqual(t.park_threshold(8), 8)   # neutral = base

    def test_park_threshold_floor(self):
        t = _temp()
        t.persistence = 0.0
        self.assertGreaterEqual(t.park_threshold(2), 3)   # never collapses to an instant park


class TestDisposition(unittest.TestCase):

    def test_driven_and_wary_labels(self):
        t = _temp()
        t.initiative, t.persistence, t.caution = 0.9, 0.8, 0.1
        self.assertEqual(t.disposition(), "driven")
        t.initiative, t.persistence, t.caution = 0.2, 0.4, 0.9
        self.assertEqual(t.disposition(), "wary")


class TestPersistence(unittest.TestCase):

    def test_save_load_round_trip(self):
        cfg = Config()
        cfg.workspace_dir = tempfile.mkdtemp()
        t = Temperament(config=cfg)
        for _ in range(50):
            t.observe(success=True, failed=False, overridden=False)
        t.save()
        t2 = Temperament(config=cfg)             # fresh load from disk
        self.assertAlmostEqual(t2.initiative, t.initiative, places=3)
        self.assertEqual(t2.updates, t.updates)


if __name__ == "__main__":
    unittest.main()
