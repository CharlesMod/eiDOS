"""Ventral Striatum — the goal-tension drive (incompletion/regret → initiative when idle).

Pins: progress discharges tension (relief); an open, unprogressed objective charges it; a frustrated
objective presses harder (regret); past threshold it raises a BOUNDED arousal floor (the itch that
keeps the creature awake while work remains); no open objective => no floor; and the multi-source
neuromod floor co-exists with curiosity's (one drive relaxing can't silence the other).
"""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from nervous.goaltension import GoalTensionDrive
from nervous import NervousBus, NeuromodulatoryState


class FakeNeuromod:
    def __init__(self):
        self.floors = {}
    def set_drive_floor(self, amount, source="curiosity"):
        if amount <= 0:
            self.floors.pop(source, None)
        else:
            self.floors[source] = amount


class TestGoalTension(unittest.TestCase):

    def test_open_objective_charges_tension(self):
        d = GoalTensionDrive()
        for _ in range(30):
            lvl = d.observe(made_progress=False, open_objective=True)
        self.assertGreater(lvl, 0.4)

    def test_progress_relieves(self):
        d = GoalTensionDrive()
        for _ in range(30):
            d.observe(made_progress=False, open_objective=True)
        high = d.level
        for _ in range(30):
            d.observe(made_progress=True, open_objective=True)
        self.assertLess(d.level, high)

    def test_frustrated_presses_harder(self):
        calm = GoalTensionDrive()
        regret = GoalTensionDrive()
        for _ in range(20):
            calm.observe(made_progress=False, open_objective=True, frustration_frac=0.0)
            regret.observe(made_progress=False, open_objective=True, frustration_frac=1.0)
        self.assertGreater(regret.level, calm.level)

    def test_no_open_objective_no_tension(self):
        d = GoalTensionDrive()
        for _ in range(30):
            lvl = d.observe(made_progress=False, open_objective=False)
        self.assertEqual(lvl, 0.0)

    def test_raises_bounded_arousal_floor_when_pressed(self):
        nm = FakeNeuromod()
        d = GoalTensionDrive(neuromod=nm)
        for _ in range(40):
            d.observe(made_progress=False, open_objective=True, frustration_frac=1.0)
        self.assertIn("goal_tension", nm.floors)
        self.assertGreater(nm.floors["goal_tension"], 0.0)

    def test_floor_releases_on_progress(self):
        nm = FakeNeuromod()
        d = GoalTensionDrive(neuromod=nm)
        for _ in range(40):
            d.observe(made_progress=False, open_objective=True, frustration_frac=1.0)
        self.assertGreater(nm.floors.get("goal_tension", 0.0), 0.0)
        for _ in range(60):
            d.observe(made_progress=True, open_objective=True)
        self.assertEqual(nm.floors.get("goal_tension", 0.0), 0.0)   # discharged below threshold

    def test_coexists_with_curiosity_on_real_neuromod(self):
        """A real neuromod keeps BOTH drives' floors; the live floor is their max (multi-source)."""
        bus = NervousBus()
        self.addCleanup(bus.close)
        nm = NeuromodulatoryState(bus)
        nm.set_drive_floor(0.2, source="curiosity")
        d = GoalTensionDrive(neuromod=nm)
        for _ in range(40):
            d.observe(made_progress=False, open_objective=True, frustration_frac=1.0)
        # curiosity's floor is still registered AND goal-tension added its own; floor is the max.
        self.assertGreaterEqual(nm.drive_floor, 0.2)
        self.assertIn("curiosity", nm._drive_floors)
        self.assertIn("goal_tension", nm._drive_floors)


if __name__ == "__main__":
    unittest.main()
