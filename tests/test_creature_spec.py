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


def _iso(ts):
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(ts))


class TestTerrarium(Base):

    def _lay_hatched(self, hatched_ago=3600):
        import creature_gen
        seed = 12345
        now = time.time()
        doc = {"v": 1, "seed": seed, "genome": creature_gen.genome_from_seed(seed),
               "born_ts": now - hatched_ago - 100, "hatched": True, "hatch_xp": 25,
               "events": [{"ts": now - hatched_ago, "kind": "hatched"}],
               "last_stage": "adult"}
        (self.ws / "creature.json").write_text(json.dumps(doc))
        return doc, now

    def _write_index(self, records):
        (self.ws / "knowledge").mkdir(parents=True, exist_ok=True)
        (self.ws / "knowledge" / "index.json").write_text(
            json.dumps(records), encoding="utf-8")

    def test_spec_includes_terrarium(self):
        self._lay_hatched()
        s = self._spec(persona={"level": 5, "xp": 600, "titles": ["A"]})
        t = s["terrarium"]
        self.assertEqual(t["frame_w"], 23)
        for row in (t["sky"], t["ground"], t["under"]):
            self.assertEqual(len(row), 23)

    def test_seed_and_incarnation_filters(self):
        doc, now = self._lay_hatched(hatched_ago=3600)
        self._write_index([
            {"id": "seed1", "category": "facts", "source_goal": "seed",
             "created": _iso(now)},                       # excluded: seed
            {"id": "old1", "category": "facts", "source_goal": "g",
             "created": _iso(now - 7200)},                # excluded: pre-hatch
            {"id": "f1", "category": "facts", "source_goal": "g", "created": _iso(now)},
            {"id": "f2", "category": "facts", "source_goal": "g", "created": _iso(now)},
            {"id": "f3", "category": "facts", "source_goal": "g", "created": _iso(now)},
            {"id": "p1", "category": "procedures", "source_goal": "g", "created": _iso(now)},
            {"id": "p2", "category": "procedures", "source_goal": "g", "created": _iso(now)},
            {"id": "r1", "category": "reflections", "source_goal": "g", "created": _iso(now)},
            {"id": "e1", "category": "errors", "source_goal": "g", "created": _iso(now)},
        ])
        g = dashboard._build_garden(self.config, doc,
                                    {"titles": ["A", "B"]})
        self.assertEqual(sum(g["facts"]), 3)     # seed + pre-hatch dropped
        self.assertEqual(sum(g["trees"]), 2)
        self.assertEqual(sum(g["moss"]), 1)
        self.assertEqual(sum(g["stones"]), 1)
        self.assertEqual(g["titles"], 2)

    def test_mail_and_done(self):
        doc, now = self._lay_hatched()
        self._write_index([])
        # done objectives
        (self.ws / "objectives.json").write_text(json.dumps(
            {"objectives": [{"state": "done"}, {"state": "done"},
                            {"state": "blocked"}]}))
        # an unconsumed intervention
        idir = self.ws / "interventions"
        idir.mkdir(parents=True, exist_ok=True)
        (idir / "msg.md").write_text("hi")
        g = dashboard._build_garden(self.config, doc, {})
        self.assertTrue(g["mail"])
        self.assertEqual(g["done"], 2)
        # consumed → renamed .done → mailbox empties
        (idir / "msg.md").rename(idir / "msg.md.done")
        self.assertFalse(dashboard._build_garden(self.config, doc, {})["mail"])


class TestDelegatesPayload(Base):

    def test_delegates_reflect_jobs(self):
        (self.ws / "jobs.json").write_text(json.dumps([
            {"name": "dlg_a", "kind": "delegate", "mode": "code",
             "status": "running", "started_ts": 100},
            {"name": "dlg_b", "kind": "delegate", "mode": "research",
             "status": "completed", "started_ts": 50},
            {"name": "j_other", "kind": "async", "status": "running"},
        ]))
        dels = self._spec()["delegates"]
        names = {d["name"]: d for d in dels}
        self.assertEqual(set(names), {"dlg_a", "dlg_b"})   # async row excluded
        self.assertEqual(names["dlg_a"]["mode"], "code")
        self.assertEqual(names["dlg_a"]["status"], "running")
        self.assertEqual(names["dlg_b"]["status"], "completed")

    def test_no_jobs_empty_list(self):
        self.assertEqual(self._spec()["delegates"], [])


class TestPendingAndBeats(Base):

    def test_spec_carries_pending_and_beats(self):
        s = self._spec()
        self.assertIn("pending", s)
        self.assertIn("beats", s)

    def test_pending_self_guide_and_selfedits(self):
        p = dashboard._build_pending(self.config)
        self.assertFalse(p["self_guide"])
        self.assertEqual(p["selfedits"], 0)
        self.config.self_guide_proposed_path.write_text("proposed guide")
        self.assertTrue(dashboard._build_pending(self.config)["self_guide"])
        pdir = self.ws / "proposals"
        pdir.mkdir(parents=True, exist_ok=True)
        (pdir / "p1.json").write_text(json.dumps(
            {"id": "p1", "kind": "self_edit", "status": "pending"}))
        (pdir / "p2.json").write_text(json.dumps(
            {"id": "p2", "kind": "self_edit", "status": "applied"}))
        self.assertEqual(dashboard._build_pending(self.config)["selfedits"], 1)

    def test_consume_beat_lifecycle(self):
        doc = {}
        idir = self.ws / "interventions"
        idir.mkdir(parents=True, exist_ok=True)
        (idir / "a.md.done").write_text("x")          # historical consume
        self.assertEqual(dashboard._update_beats(self.config, doc), [])  # baseline, no beat
        (idir / "b.md.done").write_text("x")          # a NEW consume
        beats = dashboard._update_beats(self.config, doc)
        self.assertEqual(len(beats), 1)
        self.assertEqual(beats[0]["type"], "consume")
        first_id = beats[0]["id"]
        # no change → no new beat, same id
        self.assertEqual(dashboard._update_beats(self.config, doc)[-1]["id"], first_id)
        # two new consumes between polls collapse to ONE beat
        (idir / "c.md.done").write_text("x")
        (idir / "d.md.done").write_text("x")
        beats3 = dashboard._update_beats(self.config, doc)
        self.assertEqual(len(beats3), 2)
        self.assertNotEqual(beats3[-1]["id"], first_id)


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
