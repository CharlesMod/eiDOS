"""The mastery portfolio (4.3b) — levels from FRESH, adjudicated, novelty-weighted evidence.

Pins the design decisions (Charlie, 2026-07-20): portfolio over the all-AND wall, fresh-per-
level (spend on cross), adjudicated-only XP (the +1 trickle goes dark), suspension-only
regression (unchanged). Plus the REACHABILITY INVARIANT: no level's requirement may exceed
what the unlock ladder makes achievable — the genesis-03 deadlock class, unrepresentable.
"""

import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

import level_gates
import mastery
import persona as persona_mod
from config import Config


def _cfg(portfolio=True):
    c = Config()
    c.workspace_dir = tempfile.mkdtemp()
    c.workspace.mkdir(parents=True, exist_ok=True)
    c.state_dir.mkdir(parents=True, exist_ok=True)
    c.pillars_mastery_gates_enabled = True
    c.pillars_portfolio_gates_enabled = portfolio
    return c


class TestRequirementCurve(unittest.TestCase):
    def test_declared_schedule(self):
        self.assertEqual(mastery.requirement(2), (3.0, 2))   # the genesis arc IS a L2 portfolio
        self.assertEqual(mastery.requirement(3), (4.0, 2))
        self.assertEqual(mastery.requirement(4), (5.0, 3))
        self.assertEqual(mastery.requirement(6), (7.0, 3))
        self.assertEqual(mastery.requirement(7), (8.0, 4))
        self.assertEqual(mastery.requirement(12), (8.0, 4))  # K caps at 8

    def test_never_unsatisfiable(self):
        # K can never exceed what all classes at cap can supply; M never exceeds class count.
        for lvl in range(2, 30):
            k, m = mastery.requirement(lvl)
            self.assertLessEqual(m, len(mastery.CLASSES))
            self.assertLessEqual(k, mastery.CLASS_SCORE_CAP * len(mastery.CLASSES))


class TestLedger(unittest.TestCase):
    def setUp(self):
        self.cfg = _cfg()
        self.p = {"xp": 0, "level": 1}

    def test_flag_off_records_nothing(self):
        cfg = _cfg(portfolio=False)
        self.assertIsNone(mastery.record_evidence(cfg, self.p, "quest_passed", "q1"))
        self.assertEqual(mastery.portfolio_report(cfg, 2)["items"], 0)

    def test_unknown_class_and_empty_key_refused(self):
        self.assertIsNone(mastery.record_evidence(self.cfg, self.p, "vibes", "k"))
        self.assertIsNone(mastery.record_evidence(self.cfg, self.p, "quest_passed", "  "))

    def test_same_event_never_counts_twice(self):
        self.assertIsNotNone(mastery.record_evidence(self.cfg, self.p, "quest_passed", "q1"))
        self.assertIsNone(mastery.record_evidence(self.cfg, self.p, "quest_passed", "q1"))
        self.assertEqual(mastery.portfolio_report(self.cfg, 2)["items"], 1)

    def test_near_duplicate_earns_dup_weight(self):
        a = mastery.record_evidence(self.cfg, self.p, "skill_trusted", "list_home_files",
                                    title="list home files")
        b = mastery.record_evidence(self.cfg, self.p, "skill_trusted", "list_home_files_two",
                                    title="list home files two")
        c = mastery.record_evidence(self.cfg, self.p, "skill_trusted", "probe_lan_printers",
                                    title="probe lan printers")
        self.assertEqual(a["weight"], 1.0)
        self.assertEqual(b["weight"], mastery.DUP_WEIGHT)    # a re-shape, not new mastery
        self.assertEqual(c["weight"], 1.0)                   # genuinely new ground

    def test_adjudicated_xp_pays_by_class(self):
        mastery.record_evidence(self.cfg, self.p, "skill_trusted", "s1", title="s1")
        self.assertEqual(self.p["xp"], mastery.CLASSES["skill_trusted"])
        mastery.record_evidence(self.cfg, self.p, "quest_passed", "q1")   # pays its own reward
        self.assertEqual(self.p["xp"], mastery.CLASSES["skill_trusted"])  # no double-pay

    def test_class_score_cap(self):
        for i in range(6):
            mastery.record_evidence(self.cfg, self.p, "error_recovery", f"recovery-{i}",
                                    title=f"recovered from failure number {i} in a distinct way")
        pf = mastery.portfolio_report(self.cfg, 2)
        self.assertLessEqual(pf["by_class"]["error_recovery"], mastery.CLASS_SCORE_CAP)
        self.assertFalse(pf["ok"])                            # one class can never buy a level
        self.assertEqual(pf["classes"], 1)

    def test_spend_archives_and_empties(self):
        for i, cls in enumerate(("quest_passed", "skill_trusted", "objective_completed")):
            mastery.record_evidence(self.cfg, self.p, cls, f"k{i}", title=f"item {i}")
        self.assertEqual(mastery.spend(self.cfg, level_reached=2), 3)
        pf = mastery.portfolio_report(self.cfg, 3)
        self.assertEqual(pf["items"], 0)                      # fresh per level
        self.assertEqual(pf["score"], 0.0)
        data = mastery._load(self.cfg)
        self.assertEqual(len(data["archive"]), 3)
        self.assertTrue(all(i["spent_at_level"] == 2 for i in data["archive"]))

    def test_prediction_quality_filter(self):
        ok = mastery.prediction_counts
        self.assertTrue(ok(0.7, "deadline"))
        self.assertFalse(ok(0.7, "claim"))       # instant-settle path — the self-fulfilling farm
        self.assertFalse(ok(0.99, "deadline"))   # near-certainty is not a bet
        self.assertFalse(ok(0.5, "deadline"))    # coin-flip is not conviction
        self.assertFalse(ok("nan?", "deadline"))


class TestGateIntegration(unittest.TestCase):
    def setUp(self):
        self.cfg = _cfg()
        self.p = {"xp": 0, "level": 1}

    def _fill_l2_portfolio(self):
        mastery.record_evidence(self.cfg, self.p, "skill_trusted", "inventory_probe",
                                title="inventory probe")
        mastery.record_evidence(self.cfg, self.p, "quest_passed", "genesis-01",
                                title="forge one tool of your own")
        mastery.record_evidence(self.cfg, self.p, "objective_completed", "goal-1",
                                title="map the local network")

    def test_portfolio_mode_floors_plus_portfolio(self):
        ok, report = level_gates.can_level(self.p, self.cfg)
        self.assertEqual(report["mode"], "portfolio")
        self.assertFalse(ok)                                   # nothing earned, no sleeps
        self._fill_l2_portfolio()
        ok, report = level_gates.can_level(self.p, self.cfg)
        self.assertFalse(ok)                                   # portfolio full, sleeps floor holds
        self.assertTrue(report["checks"]["portfolio"]["ok"])
        self.assertFalse(report["checks"]["sleep_cycles"]["ok"])
        for _ in range(3):
            level_gates.record_sleep_cycle(self.cfg)
        ok, report = level_gates.can_level(self.p, self.cfg)
        self.assertTrue(ok, report)

    def test_level_up_spends_the_portfolio(self):
        self._fill_l2_portfolio()
        for _ in range(3):
            level_gates.record_sleep_cycle(self.cfg)
        report = level_gates.apply_level_up(self.p, self.cfg)
        self.assertTrue(report["applied"])
        self.assertEqual(self.p["level"], 2)
        self.assertEqual(report["portfolio_spent"], 3)
        ok, report2 = level_gates.can_level(self.p, self.cfg)
        self.assertFalse(ok)                                   # L3 needs FRESH evidence
        self.assertEqual(report2["checks"]["portfolio"]["score"], 0.0)

    def test_flag_off_is_the_legacy_wall(self):
        cfg = _cfg(portfolio=False)
        ok, report = level_gates.can_level({"xp": 0, "level": 1}, cfg)
        self.assertEqual(report.get("mode"), "wall")
        self.assertIn("trusted_in_tier", report["checks"])     # byte-identical legacy semantics

    def test_render_standing_shows_values_and_needs(self):
        self._fill_l2_portfolio()
        line = level_gates.render_standing(self.p, self.cfg)
        self.assertIn("portfolio 3.0/3.0", line)
        self.assertIn("3/2 evidence classes", line)
        self.assertIn("sleeps 0/3", line)


class TestAdjudicatedOnlyXP(unittest.TestCase):
    def test_tool_call_trickle_goes_dark(self):
        cfg = _cfg()
        p = {"xp": 0, "level": 1}
        for _ in range(50):
            persona_mod.record_tick(p, "bash", True, config=cfg)
        self.assertEqual(p["xp"], 0)                           # volume pays nothing now
        self.assertEqual(p["current_streak"], 50)              # honest facts still advance

    def test_trickle_intact_when_flag_off(self):
        cfg = _cfg(portfolio=False)
        p = {"xp": 0, "level": 1}
        persona_mod.record_tick(p, "bash", True, config=cfg)
        self.assertEqual(p["xp"], 1)                           # legacy byte-identical

    def test_goal_complete_routes_through_evidence(self):
        cfg = _cfg()
        p = {"xp": 0, "level": 1}
        persona_mod.record_goal_complete(p, "mapped the LAN", config=cfg)
        self.assertEqual(p["goals_completed"], 1)
        self.assertEqual(p["xp"], mastery.CLASSES["objective_completed"])
        pf = mastery.portfolio_report(cfg, 2)
        self.assertEqual(pf["by_class"].get("objective_completed"), 1.0)

    def test_error_recovery_capped_evidence(self):
        cfg = _cfg()
        p = {"xp": 0, "level": 1}
        for _ in range(8):
            persona_mod.record_error_recovery(p, config=cfg)
        self.assertEqual(p["total_errors_recovered"], 8)
        pf = mastery.portfolio_report(cfg, 2)
        self.assertLessEqual(pf["by_class"]["error_recovery"], mastery.CLASS_SCORE_CAP)


class TestReachabilityInvariant(unittest.TestCase):
    """The genesis-03 lesson, made structural: every level's requirement must be satisfiable
    with what the ladder actually grants. If a class's enabling unit leaves the ladder, or M
    outgrows the reachable classes, this test — not a live creature — is what breaks."""

    # Which unlock unit each evidence class NEEDS (None = available from birth / System-driven).
    CLASS_UNIT = {
        "skill_trusted": "skillcraft",       # U2 — granted at genesis-01 issuance
        "prediction_settled": "foresight",   # U3 — granted at genesis-02 issuance
        "objective_completed": "resolve",    # U5 — granted at genesis-03 issuance
        "quest_passed": None,                # the System issues; no tool needed
        "commission_confirmed": None,        # commission tools are flag-, not level-, gated
        "error_recovery": None,              # any failed->succeeded tick
    }

    def test_class_map_covers_every_class(self):
        self.assertEqual(set(self.CLASS_UNIT), set(mastery.CLASSES))

    def test_enabling_units_exist_in_the_ladder(self):
        import unlocks
        for cls, unit in self.CLASS_UNIT.items():
            if unit is not None:
                self.assertIn(unit, unlocks.UNIT_IDS,
                              f"{cls} depends on unit '{unit}' which left the ladder")

    def test_requirements_reachable_at_every_level(self):
        # The genesis arc grants every enabling unit during LEVEL 1 (issuance-grant pattern),
        # so from the first crossing onward all classes are reachable. M must never exceed
        # that, and K must be coverable at cap by the reachable classes.
        reachable = len(self.CLASS_UNIT)
        for lvl in range(2, 30):
            k, m = mastery.requirement(lvl)
            self.assertLessEqual(m, reachable, f"level {lvl} demands more classes than exist")
            self.assertLessEqual(k, mastery.CLASS_SCORE_CAP * reachable,
                                 f"level {lvl} demands more score than all classes can supply")


if __name__ == "__main__":
    unittest.main()
