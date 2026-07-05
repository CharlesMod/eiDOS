"""Belief-falsification (Concern 1: the false belief that persisted, reinforced by avoidance).

The otter believed "tools are locked," pivoted to manual work, the manual work succeeded — so the
FALSE belief got REINFORCED by the avoidance-success (recall_count 12–13) and was never tested.
Classic phobia maintenance: take the stairs, arrive safely, "elevators are dangerous" is confirmed.

The systemic fix (no hardcoding): a block is a belief, and a belief earns its keep by EXPOSURE, not
avoidance.
  · A thawed-from-block objective that then makes progress is a REFUTATION — record_tick reports it
    (the loop fires maximal surprise → the correction is strongly encoded, and writes a knowledge
    reflection so the wall can't quietly re-form).
  · A stale block that was never re-tested (exposures == 0) is force-EXPOSED at the nap, not buried;
    only a block that was tested and stayed stuck earns the archive.

No services / GPU — temp workspaces only.
"""
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

import objectives
from config import Config


class _Base(unittest.TestCase):
    def setUp(self):
        self.cfg = Config()
        self.cfg.workspace_dir = tempfile.mkdtemp()
        (Path(self.cfg.workspace_dir) / "workspace").mkdir(parents=True, exist_ok=True)

    def _get(self, oid):
        return objectives._by_id(objectives._load(self.cfg), oid)


class TestRefutation(_Base):
    def test_thaw_tags_and_counts_exposure(self):
        o = objectives.add(self.cfg, "Build a tool", "make a skill", tick=1)
        objectives.block(self.cfg, o["id"], reason="tools are locked")
        # no other objective → the gate thaws the parked one when it looks for work
        gate = objectives.record_tick(self.cfg, made_progress=False, tool_failed=False,
                                      tick_number=1 + objectives.THAW_COOLDOWN)
        active = objectives.get_active(self.cfg)
        self.assertEqual(active["id"], o["id"])
        self.assertEqual(active["state"], "active")
        self.assertEqual(active["exposures"], 1)              # one exposure recorded
        self.assertTrue(active.get("_thawed_from_block"))

    def test_progress_after_thaw_reports_refutation(self):
        o = objectives.add(self.cfg, "Build a tool", "make a skill", tick=1)
        objectives.block(self.cfg, o["id"], reason="create_skill is locked")
        objectives.record_tick(self.cfg, made_progress=False, tool_failed=False,
                               tick_number=1 + objectives.THAW_COOLDOWN)   # thaw
        gate = objectives.record_tick(self.cfg, made_progress=True, tool_failed=False,
                                      tick_number=2 + objectives.THAW_COOLDOWN)  # it WORKS
        self.assertIsNotNone(gate["refuted_block"])
        self.assertEqual(gate["refuted_block"]["title"], "Build a tool")
        self.assertIn("locked", gate["refuted_block"]["reason"])
        # the tag is consumed — a second progress tick is not a second refutation
        gate2 = objectives.record_tick(self.cfg, made_progress=True, tool_failed=False,
                                       tick_number=3 + objectives.THAW_COOLDOWN)
        self.assertIsNone(gate2["refuted_block"])

    def test_ordinary_progress_is_not_a_refutation(self):
        objectives.add(self.cfg, "Normal work", "just do it", tick=1)
        gate = objectives.record_tick(self.cfg, made_progress=True, tool_failed=False,
                                      tick_number=2)
        self.assertIsNone(gate["refuted_block"])


class TestExposureBeforeArchive(_Base):
    def test_untested_stale_block_is_exposed_not_archived(self):
        o = objectives.add(self.cfg, "Avoided thing", "believed impossible", tick=1)
        objectives.block(self.cfg, o["id"], reason="impossible")
        # never thawed → exposures 0
        self.assertEqual(self._get(o["id"])["exposures"], 0)
        rep = objectives.consolidate(self.cfg, tick=1 + objectives.STALE_ARCHIVE_TICKS)
        self.assertIn(o["id"], rep["exposed"])
        self.assertNotIn(o["id"], rep["archived"])
        got = self._get(o["id"])
        self.assertEqual(got["state"], "active")              # force-exposed for a real test
        self.assertTrue(got.get("_thawed_from_block"))

    def test_tested_stale_block_earns_the_archive(self):
        o = objectives.add(self.cfg, "Genuinely stuck", "really can't", tick=1)
        objectives.block(self.cfg, o["id"], reason="hard wall")
        # simulate a prior exposure that stayed stuck
        data = objectives._load(self.cfg)
        objectives._by_id(data, o["id"])["exposures"] = 2
        objectives._save(self.cfg, data)
        rep = objectives.consolidate(self.cfg, tick=1 + objectives.STALE_ARCHIVE_TICKS)
        self.assertIn(o["id"], rep["archived"])
        self.assertEqual(self._get(o["id"])["state"], "dead")


if __name__ == "__main__":
    unittest.main()
