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


if __name__ == "__main__":
    unittest.main()
