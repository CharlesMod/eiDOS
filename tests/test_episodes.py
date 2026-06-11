"""Phase 7b: episodic memory (episodes.py) — typed (situation→action→outcome→fix) store
with state-triggered recall.

Recall fires on SITUATION resemblance and surfaces what changes the next decision: repeated
failures (don't repeat) and recoveries (what worked). These tests pin the record/trim lifecycle,
the situation key, and the recall decision logic.
"""

import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

import episodes
from config import Config


def _cfg():
    c = Config()
    c.workspace_dir = tempfile.mkdtemp()
    c.workspace.mkdir(parents=True, exist_ok=True)
    return c


class TestRecordAndRead(unittest.TestCase):

    def setUp(self):
        self.config = _cfg()

    def test_record_and_recall_roundtrip(self):
        episodes.record_episode(self.config, tick=1, tool="bash", sig="bash:probe",
                                fail_kind="network", success=False, summary="timeout", key="objA|probe")
        rec = episodes.recall(self.config, key="objA|probe")
        self.assertEqual(len(rec["failures"]), 1)
        self.assertEqual(rec["failures"][0]["fail_kind"], "network")

    def test_non_action_ticks_skipped(self):
        for t in ("system", "watchdog", "dream", ""):
            episodes.record_episode(self.config, tick=1, tool=t, sig=t,
                                    fail_kind="", success=True, key="k")
        self.assertEqual(episodes._read(self.config), [])

    def test_trim_bounds_file(self):
        for i in range(episodes._MAX_EPISODES + 120):
            episodes.record_episode(self.config, tick=i, tool="bash", sig=f"s{i}",
                                    fail_kind="exec", success=False, key="k")
        self.assertLessEqual(len(episodes._read(self.config, limit=10_000)), episodes._MAX_EPISODES)


class TestRecallDecision(unittest.TestCase):

    def setUp(self):
        self.config = _cfg()

    def _ep(self, sig, success, fail_kind="exec", key="objA|map the lan", summary=""):
        episodes.record_episode(self.config, tick=1, tool="bash", sig=sig,
                                fail_kind=fail_kind, success=success, summary=summary, key=key)

    def test_repeated_failure_surfaces_with_count(self):
        for _ in range(3):
            self._ep("bash:scan", False, "network")
        rec = episodes.recall(self.config, key="objA|map the lan")
        self.assertEqual(len(rec["failures"]), 1)
        self.assertEqual(rec["failures"][0]["fails"], 3)
        self.assertEqual(rec["worked"], [])

    def test_self_recovery_alone_produces_no_warning(self):
        # the only history is X failed then X succeeded → already recovered → no standing failure
        self._ep("bash:scan", False, "network")
        self._ep("bash:scan", True, summary="worked the 2nd time")
        rec = episodes.recall(self.config, key="objA|map the lan")
        self.assertEqual(rec["failures"], [])
        self.assertEqual(rec["worked"], [])   # nothing to warn about → no recall noise

    def test_failed_approach_with_a_different_working_one(self):
        self._ep("bash:ping_sweep", False, "timeout")          # standing failure
        self._ep("bash:arp", True, summary="arp -a got 25 hosts")  # a DIFFERENT thing worked
        rec = episodes.recall(self.config, key="objA|map the lan")
        self.assertEqual({f["sig"] for f in rec["failures"]}, {"bash:ping_sweep"})
        self.assertEqual({w["sig"] for w in rec["worked"]}, {"bash:arp"})
        self.assertIn("arp", rec["worked"][0]["ok_summary"])

    def test_exact_key_preferred_over_objective(self):
        self._ep("bash:a", False, key="objA|step one")
        self._ep("bash:b", False, key="objA|step two")
        # exact match on step two returns only step two's failure
        rec = episodes.recall(self.config, key="objA|step two")
        sigs = {f["sig"] for f in rec["failures"]}
        self.assertEqual(sigs, {"bash:b"})

    def test_objective_fallback_when_no_exact_step(self):
        self._ep("bash:a", False, key="objA|step one")
        # no episode under "step three" exactly -> fall back to same objective
        rec = episodes.recall(self.config, key="objA|step three")
        self.assertEqual(len(rec["failures"]), 1)
        self.assertEqual(rec["failures"][0]["sig"], "bash:a")

    def test_empty_when_nothing_relevant(self):
        self._ep("bash:a", False, key="objA|step one")
        rec = episodes.recall(self.config, key="objZ|unrelated")
        self.assertEqual(rec["failures"], [])
        self.assertEqual(rec["worked"], [])

    def test_render_failures_and_worked(self):
        self._ep("bash:scan", False, "network", summary="timed out")  # standing failure
        self._ep("bash:arp", True, summary="got the table")            # worked
        text = episodes.render_recall(episodes.recall(self.config, key="objA|map the lan"))
        self.assertIn("Episodic recall", text)
        self.assertIn("Don't repeat", text)
        self.assertIn("WORKED", text)

    def test_render_empty(self):
        self.assertEqual(episodes.render_recall({"failures": [], "worked": []}), "")


class TestSituationKey(unittest.TestCase):

    def setUp(self):
        self.config = _cfg()

    def test_key_normalizes_numbers(self):
        from memory import write_plan
        write_plan(self.config, "# Plan\nProbe host 192.168.86.50 on port 8080")
        k1 = episodes.situation_key(self.config)
        write_plan(self.config, "# Plan\nProbe host 192.168.86.99 on port 5000")
        k2 = episodes.situation_key(self.config)
        self.assertEqual(k1, k2)  # numbers collapse → same situation

    def test_key_has_objective_and_step(self):
        from memory import write_plan
        write_plan(self.config, "# Plan\nMap the network")
        k = episodes.situation_key(self.config)
        self.assertIn("|", k)
        self.assertIn("map the network", k)


if __name__ == "__main__":
    unittest.main()
