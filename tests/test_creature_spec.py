"""dashboard.build_creature_spec — creature.json lifecycle + v2 truth expression.

Pins: the spec rides /api/status without breaking the legacy `creature` key;
creature.json is created once and not churned by plain polls; the expression
layer reads the REAL v2 signals (glue condition from outcomes.jsonl, delegate
jobs from jobs.json, listening hold, crash-dead) — the creature renders truth.
"""

import json
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

import dashboard
from config import Config


def F(sig="bash:probe"):
    return {"ok": False, "kind": "exec", "sig": sig, "tool": "bash"}


def OK():
    return {"ok": True, "kind": "", "sig": "", "tool": "bash"}


def TH():
    return {"ok": True, "kind": "", "sig": "", "tool": "thought"}


class Base(unittest.TestCase):

    def setUp(self):
        self.config = Config()
        self.config.workspace_dir = tempfile.mkdtemp()
        self.ws = Path(self.config.workspace_dir)

    def _persona(self, level=1, xp=0):
        (self.ws / "persona.json").write_text(json.dumps({"level": level, "xp": xp}))
        return {"level": level, "xp": xp}

    def _outcomes(self, rows):
        (self.ws / "state").mkdir(parents=True, exist_ok=True)
        (self.ws / "state" / "outcomes.jsonl").write_text(
            "\n".join(json.dumps(r) for r in rows), encoding="utf-8")

    def _spec(self, persona=None, heartbeat=None, goal="explore the house"):
        return dashboard.build_creature_spec(
            self.config, persona or {"level": 5, "xp": 600},
            heartbeat or {}, goal)


class TestLifecycle(Base):

    def test_created_once_then_stable(self):
        s1 = self._spec()
        path = self.ws / "creature.json"
        self.assertTrue(path.exists())
        mtime = path.stat().st_mtime_ns
        time.sleep(0.01)
        s2 = self._spec()
        self.assertEqual(s1["id"], s2["id"])
        # hatched doc + plain poll → no disk churn
        self.assertEqual(path.stat().st_mtime_ns, mtime)

    def test_corrupted_file_regenerates(self):
        (self.ws / "creature.json").write_text("{not json")
        s = self._spec()
        self.assertIn("id", s)
        self.assertTrue(json.loads((self.ws / "creature.json").read_text())["seed"])

    def test_status_carries_spec_and_legacy_key(self):
        self._persona(level=3, xp=400)
        status = dashboard.build_status(self.config)
        self.assertIn("creature", status)        # legacy fallback stays
        self.assertIsNotNone(status["creature_spec"])
        self.assertIn("base", status["creature_spec"])


class TestHatching(Base):

    def test_egg_then_cracks_then_hatch_once(self):
        s = self._spec(persona={"level": 1, "xp": 0})
        self.assertEqual(s["stage"], "egg")
        self.assertEqual(s["hatch"]["progress"], 0.0)

        s = self._spec(persona={"level": 1, "xp": 10})
        self.assertEqual(s["stage"], "egg")
        doc = json.loads((self.ws / "creature.json").read_text())
        self.assertEqual(sum(1 for e in doc["events"] if e["kind"] == "crack"), 1)

        for _ in range(3):  # repeated polls past the threshold
            s = self._spec(persona={"level": 1, "xp": 30})
        self.assertEqual(s["stage"], "hatchling")
        doc = json.loads((self.ws / "creature.json").read_text())
        self.assertEqual(sum(1 for e in doc["events"] if e["kind"] == "hatched"), 1)


class TestMetamorphosis(Base):

    def _doc(self):
        return json.loads((self.ws / "creature.json").read_text())

    def test_upgrade_triggers_cocoon_interlude_once(self):
        # establish a hatched juvenile baseline
        s = self._spec(persona={"level": 3, "xp": 400})
        self.assertEqual(s["stage"], "juvenile")
        self.assertNotIn("interlude", s)
        # level up → metamorphosis: cocoon body, no eyes, one event
        s = self._spec(persona={"level": 5, "xp": 900})
        self.assertEqual(s["stage"], "adult")
        self.assertIn("interlude", s)
        self.assertEqual(s["interlude"]["kind"], "cocoon")
        self.assertIsNone(s["eyes"])
        # repeated polls mid-interlude: still cocooned, still exactly one event
        s = self._spec(persona={"level": 5, "xp": 900})
        self.assertIn("interlude", s)
        doc = self._doc()
        self.assertEqual(
            sum(1 for e in doc["events"] if e["kind"] == "metamorphosis"), 1)
        # emergence: expire the interlude → normal adult body, eyes back
        doc["interlude_until"] = time.time() - 1
        (self.ws / "creature.json").write_text(json.dumps(doc))
        s = self._spec(persona={"level": 5, "xp": 900})
        self.assertNotIn("interlude", s)
        self.assertIsNotNone(s["eyes"])

    def test_hatch_is_not_a_metamorphosis(self):
        self._spec(persona={"level": 1, "xp": 0})       # egg
        s = self._spec(persona={"level": 1, "xp": 30})  # hatch
        self.assertEqual(s["stage"], "hatchling")
        self.assertNotIn("interlude", s)
        self.assertEqual(sum(1 for e in self._doc()["events"]
                             if e["kind"] == "metamorphosis"), 0)

    def test_first_record_on_existing_creature_is_silent(self):
        # pre-Phase-B creature.json has no last_stage (the live adult's case)
        self._spec(persona={"level": 5, "xp": 900})
        doc = self._doc()
        doc.pop("last_stage", None)
        doc.pop("interlude_until", None)
        (self.ws / "creature.json").write_text(json.dumps(doc))
        s = self._spec(persona={"level": 5, "xp": 900})
        self.assertNotIn("interlude", s)
        self.assertEqual(self._doc()["last_stage"], "adult")

    def test_downgrade_is_silent(self):
        self._spec(persona={"level": 3, "xp": 400})   # juvenile baseline
        self._spec(persona={"level": 5, "xp": 900})   # upgrade → 1 metamorphosis
        doc = self._doc()
        doc.pop("interlude_until", None)              # clear the cocoon window
        (self.ws / "creature.json").write_text(json.dumps(doc))
        s = self._spec(persona={"level": 3, "xp": 100})
        self.assertEqual(s["stage"], "juvenile")
        self.assertNotIn("interlude", s)
        self.assertEqual(sum(1 for e in self._doc()["events"]
                             if e["kind"] == "metamorphosis"), 1)  # only the upgrade's


class TestExpression(Base):

    def test_delegating_from_jobs_array(self):
        (self.ws / "jobs.json").write_text(json.dumps([
            {"name": "dlg_x", "kind": "delegate", "status": "running"}]))
        self.assertTrue(self._spec()["expr"]["delegating"])
        (self.ws / "jobs.json").write_text(json.dumps([
            {"name": "dlg_x", "kind": "delegate", "status": "completed"},
            {"name": "j1", "kind": "async", "status": "running"}]))
        self.assertFalse(self._spec()["expr"]["delegating"])

    def test_listening_fresh_vs_stale(self):
        (self.ws / "state").mkdir(parents=True, exist_ok=True)
        hold = self.ws / "state" / "chat_hold.json"
        hold.write_text(json.dumps({"held": True, "ts": time.time()}))
        self.assertTrue(self._spec()["expr"]["listening"])
        hold.write_text(json.dumps({"held": True, "ts": time.time() - 120}))
        self.assertFalse(self._spec()["expr"]["listening"])

    def test_listening_honors_continuous_ceiling(self):
        # A tab refreshed within the TTL but held past chat_hold_max_continuous_s is no
        # longer honored by eiDOS — so it must stop rendering as listening (the bug: TTL
        # alone showed ~5 min of false listening on a forgotten focused tab).
        (self.ws / "state").mkdir(parents=True, exist_ok=True)
        hold = self.ws / "state" / "chat_hold.json"
        now = time.time()
        ceiling = self.config.chat_hold_max_continuous_s
        hold.write_text(json.dumps({"held": True, "ts": now,
                                    "first_held_ts": now - ceiling - 10}))
        self.assertFalse(self._spec()["expr"]["listening"])
        # fresh ts AND fresh first_held_ts → still honored
        hold.write_text(json.dumps({"held": True, "ts": now, "first_held_ts": now - 5}))
        self.assertTrue(self._spec()["expr"]["listening"])

    def test_dead_from_heartbeat(self):
        self.assertTrue(
            self._spec(heartbeat={"consecutive_failures": 6})["expr"]["dead"])
        self.assertFalse(
            self._spec(heartbeat={"consecutive_failures": 2})["expr"]["dead"])

    def test_condition_strained(self):
        self._outcomes([F("a")] * 6)
        self.assertEqual(self._spec()["expr"]["condition"], "STRAINED")

    def test_condition_ruminating(self):
        self._outcomes([OK(), TH(), TH(), TH(), TH()])
        self.assertEqual(self._spec()["expr"]["condition"], "RUMINATING")

    def test_no_goal_flag(self):
        self.assertFalse(self._spec(goal="  ")["expr"]["has_goal"])
        self.assertTrue(self._spec(goal="map the LAN")["expr"]["has_goal"])


if __name__ == "__main__":
    unittest.main()
