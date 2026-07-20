"""Tests for growth.build_growth — the D1–D10 dream-test aggregator (growth.py).

Convention borrowed from tests/test_dashboard_data.py / test_health_probe.py: build a temp
workspace, drop synthetic fixture files into the exact stores growth.py reads, assert each
metric computes correctly, and assert the fail-open contract (missing files → 'unmeasured'
or an honest zero, never a crash) plus the invariant that the D-table always has ten rows.
"""

import json
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from config import Config
import growth


def _cfg():
    c = Config()
    c.workspace_dir = tempfile.mkdtemp()
    c.workspace.mkdir(parents=True, exist_ok=True)
    c.state_dir.mkdir(parents=True, exist_ok=True)
    c.knowledge_dir.mkdir(parents=True, exist_ok=True)
    (c.workspace / "skills").mkdir(parents=True, exist_ok=True)
    c.proposals_dir.mkdir(parents=True, exist_ok=True)
    c.snapshots_dir.mkdir(parents=True, exist_ok=True)
    return c


def _write_jsonl(path: Path, records):
    path.write_text("\n".join(json.dumps(r) for r in records) + "\n", encoding="utf-8")


class TestEmptyWorkspace(unittest.TestCase):
    """Fail-open: a bare workspace never crashes; the D-table still has ten rows."""

    def setUp(self):
        self.c = _cfg()

    def tearDown(self):
        shutil.rmtree(self.c.workspace_dir, ignore_errors=True)

    def test_no_crash_and_ten_rows(self):
        out = growth.build_growth(self.c)
        self.assertIn("d_tests", out)
        self.assertEqual(len(out["d_tests"]), 10)
        # Every row carries the metric contract.
        for row in out["d_tests"]:
            self.assertIn("d", row)
            self.assertIn("name", row)
            self.assertIn("status", row)
            self.assertIn("basis", row)
            self.assertIn(row["status"], ("measured", "unmeasured", "human-judged"))

    def test_d_labels_in_order(self):
        out = growth.build_growth(self.c)
        labels = [r["d"] for r in out["d_tests"]]
        self.assertEqual(labels, ["D1", "D2", "D3", "D4", "D5", "D6", "D7", "D8", "D9", "D10"])

    def test_human_judged_tests_never_measured(self):
        out = growth.build_growth(self.c)
        by_d = {r["d"]: r for r in out["d_tests"]}
        for d in ("D1", "D4", "D6", "D7", "D9"):
            self.assertEqual(by_d[d]["status"], "human-judged", f"{d} must be human-judged")

    def test_vitals_present_even_when_empty(self):
        out = growth.build_growth(self.c)
        v = out["vitals"]
        for k in ("sleeps_total", "goals_completed", "level", "stage",
                  "total_ticks", "objectives", "self_edit_proposals", "strategy_engrams"):
            self.assertIn(k, v)


class TestD2RepeatFailure(unittest.TestCase):
    def setUp(self):
        self.c = _cfg()

    def tearDown(self):
        shutil.rmtree(self.c.workspace_dir, ignore_errors=True)

    def _d2(self):
        return {r["d"]: r for r in growth.build_growth(self.c)["d_tests"]}["D2"]

    def test_repeat_detected_in_error_engrams(self):
        # Two identical error bodies (a repeat) + one unique → repeat_events 2 of 3.
        recs = [
            {"kind": "error", "body": "path my_space/data/x does not exist", "created": "2026-06-01"},
            {"kind": "error", "body": "PATH my_space/data/x   does not exist", "created": "2026-07-01"},
            {"kind": "error", "body": "duplicate objective added", "created": "2026-07-02"},
            {"kind": "fact", "body": "unrelated fact", "created": "2026-07-02"},
        ]
        _write_jsonl(self.c.knowledge_dir / "engram_longterm.jsonl", recs)
        d2 = self._d2()
        self.assertEqual(d2["status"], "measured")
        self.assertEqual(d2["value"]["total_failures"], 3)
        self.assertEqual(d2["value"]["distinct_signatures"], 2)
        self.assertEqual(d2["value"]["repeated_signatures"], 1)
        # 2 repeat events / 3 total.
        self.assertAlmostEqual(d2["value"]["repeat_failure_rate"], round(2 / 3, 4))
        self.assertIn("engram_longterm.jsonl", d2["basis"])

    def test_fallback_to_observations_archive(self):
        # No error engrams → fall back to failed obs in the archive.
        _write_jsonl(self.c.knowledge_dir / "engram_longterm.jsonl",
                     [{"kind": "fact", "body": "just a fact"}])
        arch = self.c.state_dir / "observations_archive_202607.jsonl"
        _write_jsonl(arch, [
            {"tool": "bash", "success": False, "output": "boom"},
            {"tool": "bash", "success": False, "output": "boom"},
            {"tool": "write_file", "success": True, "output": "ok"},
        ])
        d2 = self._d2()
        self.assertEqual(d2["status"], "measured")
        self.assertIn("observations_archive", d2["basis"])
        self.assertEqual(d2["value"]["total_failures"], 2)
        self.assertEqual(d2["value"]["repeated_signatures"], 1)

    def test_unmeasured_when_no_errors_anywhere(self):
        _write_jsonl(self.c.knowledge_dir / "engram_longterm.jsonl",
                     [{"kind": "fact", "body": "only facts"}])
        d2 = self._d2()
        self.assertEqual(d2["status"], "unmeasured")


class TestD3WakesUpSmarter(unittest.TestCase):
    def setUp(self):
        self.c = _cfg()

    def tearDown(self):
        shutil.rmtree(self.c.workspace_dir, ignore_errors=True)

    def test_always_unmeasured_but_carries_context(self):
        (self.c.state_dir / "calibration.json").write_text(
            json.dumps({"general": {"brier": 0.05, "n": 4}}), encoding="utf-8")
        (self.c.snapshots_dir / "dream_20260719_1200.md").write_text("dream", encoding="utf-8")
        (self.c.snapshots_dir / "dream_20260719_1300.md").write_text("dream", encoding="utf-8")
        d3 = {r["d"]: r for r in growth.build_growth(self.c)["d_tests"]}["D3"]
        # D3 is honestly unmeasured (no per-cycle history), but surfaces context.
        self.assertEqual(d3["status"], "unmeasured")
        self.assertEqual(d3["value"]["current_brier"], 0.05)
        self.assertEqual(d3["value"]["dream_cycles"], 2)


class TestD5ReuseVsAuthorship(unittest.TestCase):
    def setUp(self):
        self.c = _cfg()

    def tearDown(self):
        shutil.rmtree(self.c.workspace_dir, ignore_errors=True)

    def _d5(self):
        return {r["d"]: r for r in growth.build_growth(self.c)["d_tests"]}["D5"]

    def test_reuse_ratio_computed(self):
        manifest = {"skills": {
            "a": {"invocations": 100, "successes": 98},
            "b": {"invocations": 40, "successes": 40},
        }}
        (self.c.workspace / "skills" / "_index.json").write_text(
            json.dumps(manifest), encoding="utf-8")
        d5 = self._d5()
        self.assertEqual(d5["status"], "measured")
        self.assertEqual(d5["value"]["skills_authored"], 2)
        self.assertEqual(d5["value"]["total_invocations"], 140)
        self.assertEqual(d5["value"]["reuse_ratio"], 70.0)

    def test_no_skills_yet_is_measured_zero(self):
        (self.c.workspace / "skills" / "_index.json").write_text(
            json.dumps({"skills": {}}), encoding="utf-8")
        d5 = self._d5()
        self.assertEqual(d5["status"], "measured")
        self.assertEqual(d5["value"]["skills_authored"], 0)

    def test_missing_manifest_unmeasured(self):
        d5 = self._d5()
        self.assertEqual(d5["status"], "unmeasured")


class TestD8NothingFreezes(unittest.TestCase):
    def setUp(self):
        self.c = _cfg()

    def tearDown(self):
        shutil.rmtree(self.c.workspace_dir, ignore_errors=True)

    def _d8(self):
        return {r["d"]: r for r in growth.build_growth(self.c)["d_tests"]}["D8"]

    def test_counts_events_and_stale_restarts(self):
        (self.c.state_dir / "watchdog_events.log").write_text(
            "2026-07-01T00:00:00Z  stale heartbeat → restart\n"
            "2026-07-01T01:00:00Z  crash-loop → restore last_good (rollback)\n"
            "2026-07-01T02:00:00Z  stood down\n", encoding="utf-8")
        d8 = self._d8()
        self.assertEqual(d8["status"], "measured")
        self.assertEqual(d8["value"]["watchdog_events"], 3)
        self.assertEqual(d8["value"]["stale_heartbeat_restarts"], 1)
        self.assertEqual(d8["value"]["rollback_events"], 1)

    def test_absent_log_is_measured_zero(self):
        d8 = self._d8()
        self.assertEqual(d8["status"], "measured")
        self.assertEqual(d8["value"]["watchdog_events"], 0)


class TestD10RisesToVoice(unittest.TestCase):
    def setUp(self):
        self.c = _cfg()

    def tearDown(self):
        shutil.rmtree(self.c.workspace_dir, ignore_errors=True)

    def _d10(self):
        return {r["d"]: r for r in growth.build_growth(self.c)["d_tests"]}["D10"]

    def test_by_tier_counts_and_cadence(self):
        quests = [
            {"id": "q1", "tier": 1, "state": "passed"},
            {"id": "q2", "tier": 1, "state": "failed"},
            {"id": "q3", "tier": 2, "state": "passed"},
            {"id": "q4", "tier": 2, "state": "active"},
            {"id": "q5", "tier": 2, "state": "expired"},
        ]
        _write_jsonl(self.c.workspace / "quests.jsonl", quests)
        (self.c.state_dir / "quest_cadence.json").write_text(
            json.dumps({"sleeps_since_close": 12}), encoding="utf-8")
        d10 = self._d10()
        self.assertEqual(d10["status"], "measured")
        self.assertEqual(d10["value"]["active"], "q4")
        self.assertEqual(d10["value"]["sleeps_since_close"], 12)
        self.assertEqual(d10["value"]["totals"]["passed"], 2)
        self.assertEqual(d10["value"]["by_tier"]["1"], {"passed": 1, "failed": 1})
        self.assertEqual(d10["value"]["by_tier"]["2"], {"passed": 1, "active": 1, "expired": 1})
        # completion = passed 2 / terminal (2 passed + 1 failed + 1 expired) = 0.5
        self.assertEqual(d10["value"]["completion_rate"], 0.5)

    def test_missing_quests_unmeasured(self):
        d10 = self._d10()
        self.assertEqual(d10["status"], "unmeasured")


class TestGoalHorizonAndVitals(unittest.TestCase):
    def setUp(self):
        self.c = _cfg()

    def tearDown(self):
        shutil.rmtree(self.c.workspace_dir, ignore_errors=True)

    def test_goal_horizon_trend(self):
        (self.c.state_dir / "goal_horizon_stats.json").write_text(
            json.dumps({"samples": 7, "sum": 81, "max": 52, "mean": 11.57,
                        "recent": [8, 0, 21, 52], "by_cause": {"loop": 7}}),
            encoding="utf-8")
        gh = growth.build_growth(self.c)["kpis"]["goal_horizon"]
        self.assertEqual(gh["status"], "measured")
        self.assertEqual(gh["value"]["mean"], 11.57)
        self.assertEqual(gh["value"]["max"], 52)

    def test_goal_horizon_missing_unmeasured(self):
        gh = growth.build_growth(self.c)["kpis"]["goal_horizon"]
        self.assertEqual(gh["status"], "unmeasured")

    def test_vitals_from_stores(self):
        (self.c.state_dir / "level_gates.json").write_text(
            json.dumps({"sleeps_total": 5}), encoding="utf-8")
        (self.c.workspace / "persona.json").write_text(
            json.dumps({"goals_completed": 3, "level": 2, "total_ticks": 999}), encoding="utf-8")
        (self.c.workspace / "phenotype.json").write_text(
            json.dumps({"stage": "hatchling"}), encoding="utf-8")
        (self.c.workspace / "objectives.json").write_text(
            json.dumps({"objectives": [
                {"id": "o1", "state": "active"},
                {"id": "o2", "state": "done"},
                {"id": "o3", "state": "dead"},
                {"id": "o4", "state": "blocked"},
            ]}), encoding="utf-8")
        # two self-edit proposals (json manifests) + one staged sidecar that must NOT count
        (self.c.proposals_dir / "1234.json").write_text("{}", encoding="utf-8")
        (self.c.proposals_dir / "5678.json").write_text("{}", encoding="utf-8")
        (self.c.proposals_dir / "9999.staged.py").write_text("pass", encoding="utf-8")
        # strategy engrams
        _write_jsonl(self.c.knowledge_dir / "engram_longterm.jsonl", [
            {"kind": "strategy", "body": "guardrail A"},
            {"kind": "strategy", "body": "guardrail B"},
            {"kind": "fact", "body": "not a strategy"},
        ])
        v = growth.build_growth(self.c)["vitals"]
        self.assertEqual(v["sleeps_total"]["value"], 5)
        self.assertEqual(v["goals_completed"]["value"], 3)
        self.assertEqual(v["level"]["value"], 2)
        self.assertEqual(v["stage"]["value"], "hatchling")
        self.assertEqual(v["total_ticks"]["value"], 999)
        self.assertEqual(v["objectives"]["value"]["open"], 2)   # active + blocked
        self.assertEqual(v["objectives"]["value"]["done"], 1)
        self.assertEqual(v["objectives"]["value"]["dead"], 1)
        self.assertEqual(v["self_edit_proposals"]["value"], 2)
        self.assertEqual(v["strategy_engrams"]["value"], 2)

    def test_corrupt_files_fail_open(self):
        # Corrupt JSON in several stores must degrade to unmeasured/zero, never raise.
        (self.c.state_dir / "level_gates.json").write_text("{not json", encoding="utf-8")
        (self.c.workspace / "persona.json").write_text("]]broken", encoding="utf-8")
        (self.c.state_dir / "goal_horizon_stats.json").write_text("nope", encoding="utf-8")
        (self.c.workspace / "quests.jsonl").write_text("garbage line\n{bad\n", encoding="utf-8")
        out = growth.build_growth(self.c)   # must not raise
        self.assertEqual(len(out["d_tests"]), 10)
        self.assertEqual(out["vitals"]["sleeps_total"]["status"], "unmeasured")
        self.assertEqual(out["kpis"]["goal_horizon"]["status"], "unmeasured")


if __name__ == "__main__":
    unittest.main()
