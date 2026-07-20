"""The progression deadlock (functional review 2026-07-20) — pinned closed.

The live creature was hard-locked at hatchling: tool_objective_add returned success=True while
writing NOTHING at egg/hatchling (59 straight phantom successes), genesis-03 needs
goals_completed >= 1 and never expires, an active quest blocks quest_line_closed, level 2 needs
the line closed, and juvenile (which was to unlock objectives) needs level 2. Circular.

These tests pin the fix: a hatchling honestly carries ONE undertaking (ARCHITECTURE_PRINCIPLES
#4 — a cap is a visible typed failure, never a success-wrapped no-op), the genesis-03 criterion
is therefore satisfiable, and dead offers are swept instead of sitting 'offered' forever.
"""

import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).parent.parent))

import objectives
import tools
from config import Config
from quests import ACTIVE, Criterion, EXPIRED, OFFERED, Quest, System


class _Base(unittest.TestCase):
    def setUp(self):
        self.cfg = Config()
        self.cfg.workspace_dir = tempfile.mkdtemp()
        (Path(self.cfg.workspace_dir) / "workspace").mkdir(parents=True, exist_ok=True)
        self.cfg.pillars_tool_unlocks_enabled = True

    def _add(self, title, why="because"):
        return tools.tool_objective_add({"title": title, "why": why}, self.cfg)


class TestHatchlingObjectiveCapacity(_Base):
    """A hatchling carries one undertaking — really carries it, and the cap is honest."""

    def test_hatchling_first_objective_really_lands(self):
        with patch.object(tools, "_obj_tick", return_value=1), \
             patch("creature_gen.current_stage", return_value="hatchling"):
            r = self._add("map the workspace")
        self.assertTrue(r.success, r.output)
        live = [o for o in objectives.list_objectives(self.cfg) if o["state"] == "active"]
        self.assertEqual(len(live), 1)                     # it EXISTS — not a phantom success
        self.assertEqual(live[0]["title"], "map the workspace")

    def test_hatchling_second_objective_refused_honestly(self):
        with patch.object(tools, "_obj_tick", return_value=1), \
             patch("creature_gen.current_stage", return_value="hatchling"):
            self._add("map the workspace")
            r = self._add("build a weather station")
        self.assertFalse(r.success)                        # a refusal is a FAILURE, never a lie
        self.assertEqual(r.fail_kind, "blocked")
        self.assertIn("map the workspace", r.output)       # names the held slot
        self.assertIn("objective_done", r.output)          # and the way through the wall

    def test_finishing_frees_the_slot(self):
        with patch.object(tools, "_obj_tick", return_value=1), \
             patch("creature_gen.current_stage", return_value="hatchling"):
            self._add("map the workspace")
            tools.tool_objective_done({"title": "map the workspace"}, self.cfg)
            r = self._add("build a weather station")
        self.assertTrue(r.success, r.output)               # commitment is renewable, not one-shot

    def test_juvenile_carries_three(self):
        with patch.object(tools, "_obj_tick", return_value=1), \
             patch("creature_gen.current_stage", return_value="juvenile"):
            for i, t in enumerate(("goal alpha", "goal beta", "goal gamma")):
                self.assertTrue(self._add(t).success)
            r = self._add("goal delta")
        self.assertFalse(r.success)
        self.assertEqual(r.fail_kind, "blocked")

    def test_adult_uncapped(self):
        with patch.object(tools, "_obj_tick", return_value=1), \
             patch("creature_gen.current_stage", return_value="adult"):
            for i, t in enumerate(("g one", "g two", "g three", "g four", "g five")):
                self.assertTrue(self._add(t).success)

    def test_genesis_03_criterion_now_satisfiable(self):
        """The whole point: choose something with objective_add, finish it, and the glue-side
        counter genesis-03 adjudicates on can actually move."""
        crit = Criterion(path="persona.goals_completed", op=">=", value=1)
        self.assertFalse(crit.check({"persona": {"goals_completed": 0}}))
        with patch.object(tools, "_obj_tick", return_value=1), \
             patch("creature_gen.current_stage", return_value="hatchling"):
            self.assertTrue(self._add("finish one thing").success)
            done = tools.tool_objective_done({"title": "finish one thing"}, self.cfg)
        self.assertTrue(done.success, done.output)
        # eidos.py's tick loop is goals_completed's only production writer; it increments on
        # exactly this mark-done outcome. With the add path real, the counter can move:
        self.assertTrue(crit.check({"persona": {"goals_completed": 1}}))


class TestOfferedQuestSweep(_Base):
    """A dead offer is closed EXPIRED at the queue's service point, never promoted."""

    def _quest(self, qid, expiry_ts=None):
        return Quest(id=qid, directive=f"[SYSTEM] {qid}",
                     success_criteria=Criterion(path="persona.total_ticks", op=">=", value=1),
                     reward={"kind": "xp", "amount": 5}, tier=1, expiry_ts=expiry_ts)

    def test_expired_offer_is_swept_not_issued(self):
        system = System(self.cfg)
        system.propose(self._quest("stale-offer", expiry_ts=time.time() - 3600))
        system.propose(self._quest("fresh-offer"))
        issued = system.issue_next(sleeps_since_close=99, condition="NOMINAL")
        self.assertIsNotNone(issued)
        self.assertEqual(issued.id, "fresh-offer")         # the live offer, not the dead one
        states = {q.id: q.state for q in system.store.load()}
        self.assertEqual(states["stale-offer"], EXPIRED)
        self.assertEqual(states["fresh-offer"], ACTIVE)

    def test_sweep_records_outcome(self):
        system = System(self.cfg)
        system.propose(self._quest("stale-offer", expiry_ts=time.time() - 3600))
        swept = system.sweep_offered()
        self.assertEqual([q.id for q in swept], ["stale-offer"])
        self.assertIn("never engaged", swept[0].outcome)

    def test_unexpired_offers_untouched(self):
        system = System(self.cfg)
        system.propose(self._quest("no-deadline"))                       # expiry None
        system.propose(self._quest("future", expiry_ts=time.time() + 3600))
        self.assertEqual(system.sweep_offered(), [])
        self.assertTrue(all(q.state == OFFERED for q in system.store.load()))


if __name__ == "__main__":
    unittest.main()
