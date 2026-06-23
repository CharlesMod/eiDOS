"""Phase 6: the objectives-gate strain teeth (extra_frustration).

The mechanical effect that replaces the old advisory 'you seem stuck' prose: when the
strain glue detects chronic/repeated failure, it feeds extra frustration into the gate,
so a dead end parks and rotates FASTER. The model cannot veto it — the harness moves.
"""

import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

import objectives
from config import Config


class TestGateStrainTeeth(unittest.TestCase):

    def setUp(self):
        self.config = Config()
        self.config.workspace_dir = tempfile.mkdtemp()
        objectives.ensure_seeded(self.config, 1)

    def _frust(self):
        return objectives.get_active(self.config)["frustration"]

    def test_normal_fail_adds_base_only(self):
        before = self._frust()
        objectives.record_tick(self.config, made_progress=False, tool_failed=True,
                               tick_number=2, extra_frustration=0)
        self.assertEqual(self._frust() - before, objectives.FRUST_FAIL)

    def test_strained_fail_adds_base_plus_bump(self):
        before = self._frust()
        objectives.record_tick(self.config, made_progress=False, tool_failed=True,
                               tick_number=2, extra_frustration=2)
        self.assertEqual(self._frust() - before, objectives.FRUST_FAIL + 2)

    def test_strain_parks_faster(self):
        """Under strain a stalled objective reaches the park threshold in fewer ticks."""
        # Count ticks-to-park without strain vs with strain (fresh objective each run).
        def ticks_to_park(bump):
            cfg = Config()
            cfg.workspace_dir = tempfile.mkdtemp()
            objectives.ensure_seeded(cfg, 1)
            start_id = objectives.get_active(cfg)["id"]
            for t in range(2, 60):
                ev = objectives.record_tick(cfg, made_progress=False, tool_failed=True,
                                            tick_number=t, extra_frustration=bump)
                if ev.get("rotated") or objectives.get_active(cfg) is None \
                        or objectives.get_active(cfg)["id"] != start_id:
                    return t
            return 60
        self.assertLess(ticks_to_park(2), ticks_to_park(0))

    def test_extra_frustration_defaults_zero(self):
        before = self._frust()
        objectives.record_tick(self.config, made_progress=False, tool_failed=True, tick_number=2)
        self.assertEqual(self._frust() - before, objectives.FRUST_FAIL)


class TestTemperamentParkThreshold(unittest.TestCase):
    """DMN temperament feeds the gate a persistence-scaled park threshold: a dogged creature grinds
    longer before the gate rotates it; a deferential one lets go sooner. None => the default FRUST_PARK."""

    def _ticks_to_park(self, park_threshold):
        cfg = Config()
        cfg.workspace_dir = tempfile.mkdtemp()
        objectives.ensure_seeded(cfg, 1)
        start_id = objectives.get_active(cfg)["id"]
        for t in range(2, 200):
            objectives.record_tick(cfg, made_progress=False, tool_failed=True,
                                   tick_number=t, park_threshold=park_threshold)
            act = objectives.get_active(cfg)
            if act is None or act["id"] != start_id:
                return t
        return 200

    def test_higher_threshold_grinds_longer(self):
        self.assertLess(self._ticks_to_park(6), self._ticks_to_park(11))

    def test_none_uses_default(self):
        self.assertEqual(self._ticks_to_park(None), self._ticks_to_park(objectives.FRUST_PARK))

    def test_parked_flag_set_on_forced_park(self):
        cfg = Config()
        cfg.workspace_dir = tempfile.mkdtemp()
        objectives.ensure_seeded(cfg, 1)
        ev = {}
        for t in range(2, 200):
            ev = objectives.record_tick(cfg, made_progress=False, tool_failed=True,
                                        tick_number=t, park_threshold=6)
            if ev.get("parked"):
                break
        self.assertTrue(ev.get("parked"))      # the override signal the temperament learns from


class TestCreatureModeNoHouseAgenda(unittest.TestCase):
    """A creature is born with NO preset agenda. The hardcoded _SEED is the house-AI's six-point
    mission; planting it makes a fresh creature fixate on cameras/GLaDOS/LAN no matter how clean its
    workspace (2026-06-20). ensure_seeded must plant nothing in creature mode."""

    def _fresh(self, creature):
        cfg = Config()
        cfg.workspace_dir = tempfile.mkdtemp()
        cfg.creature_mode = creature
        objectives.ensure_seeded(cfg, 1)
        return objectives.list_objectives(cfg)

    def test_creature_mode_seeds_no_objectives(self):
        self.assertEqual(self._fresh(creature=True), [])     # a creature carries no house agenda

    def test_house_mode_still_seeds(self):
        self.assertGreater(len(self._fresh(creature=False)), 0)   # the house AI still gets its backlog


if __name__ == "__main__":
    unittest.main()
