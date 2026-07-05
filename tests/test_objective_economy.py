"""The objective similarity-economy (Concern 2: backlog sprawl).

The creature spun up "Skill Library", "Skill Library Foundation", and "Utility Skill Suite" as
three distinct goals — objectives had exact-title dedup only and zero holding cost, while skills
already have similarity-pricing and auto-retire. This ports that ONE economy to goals: a
near-duplicate goal is the same goal reworded (merges, doesn't spawn), and a nap consolidates the
backlog (merge accumulated dupes + archive long-stale ones), the goal analog of memory
consolidation. Same token-overlap notion (knowledge.text_overlap) — no hardcoded merge list.

No services / GPU — temp workspaces only.
"""
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

import knowledge
import objectives
from config import Config


class _Base(unittest.TestCase):
    def setUp(self):
        self.cfg = Config()
        self.cfg.workspace_dir = tempfile.mkdtemp()
        (Path(self.cfg.workspace_dir) / "workspace").mkdir(parents=True, exist_ok=True)

    def _titles(self, states=("active", "blocked")):
        return sorted(o["title"] for o in objectives.list_objectives(self.cfg)
                      if o.get("state") in states)


class TestSharedSimilarity(unittest.TestCase):
    def test_jaccard_separates_elaboration_from_distinct_goal(self):
        # An elaboration merges; a distinct LARGER commitment sharing the title's words does NOT
        # (the false-positive direction the review flagged — subset ≠ duplicate).
        elaboration = knowledge.token_jaccard("Skill Library", "Skill Library Foundation")
        distinct = knowledge.token_jaccard("Skill Library", "Skill Library Governance Board")
        self.assertGreaterEqual(elaboration, objectives.MERGE_SIM)     # 2/3 ≈ 0.67
        self.assertLess(distinct, objectives.MERGE_SIM)               # 2/4 = 0.5
        # unrelated goals stay far apart
        self.assertLess(knowledge.token_jaccard("Map the holt", "Place calibration wagers"), 0.2)

    def test_version_strings_do_not_force_goals_apart(self):
        # IP gating must NOT leak into goals: two goals citing different dotted-quad versions
        # still compare on their words (the medium finding).
        self.assertGreater(knowledge.token_jaccard("Upgrade release 2.0.0.1 build",
                                                   "Upgrade release 5.6.7.8 build"), 0.5)


class TestMergeOnAdd(_Base):
    def test_reworded_goal_merges_not_spawns(self):
        a = objectives.add(self.cfg, "Skill Library", "build a library of reusable skills", tick=1)
        b = objectives.add(self.cfg, "Skill Library Foundation",
                           "build the foundation for a skill library", tick=2)
        self.assertEqual(a["id"], b["id"])                 # same commitment, reworded
        self.assertEqual(len(self._titles()), 1)

    def test_distinct_goals_stay_distinct(self):
        objectives.add(self.cfg, "Map the holt directory", "understand my filesystem", tick=1)
        objectives.add(self.cfg, "Place calibration wagers", "improve my foresight", tick=2)
        self.assertEqual(len(self._titles()), 2)

    def test_larger_commitment_sharing_title_words_is_not_swallowed(self):
        # The review's dangerous case: a distinct larger goal must NOT fold into the small one.
        a = objectives.add(self.cfg, "Skill Library", "build the skill store", tick=1)
        b = objectives.add(self.cfg, "Skill Library Governance Board",
                           "an oversight body for how skills are approved", tick=2)
        self.assertNotEqual(a["id"], b["id"])
        self.assertEqual(len(self._titles()), 2)

    def test_reraising_a_finished_goal_creates_fresh(self):
        o = objectives.add(self.cfg, "Map the LAN", "see the network", tick=1)
        objectives.mark_done(self.cfg, o["id"])
        again = objectives.add(self.cfg, "Map the LAN", "map it again, fully", tick=9)
        self.assertNotEqual(again["id"], o["id"])          # a done goal is history, not a merge target
        self.assertEqual(again["state"], "active")

    def test_rearticulating_a_parked_goal_thaws_it(self):
        o = objectives.add(self.cfg, "Skill Library", "reusable skills", tick=1)
        objectives.block(self.cfg, o["id"], reason="stuck")
        self.assertEqual(objectives._by_id(objectives._load(self.cfg), o["id"])["state"], "blocked")
        again = objectives.add(self.cfg, "A skill library", "build reusable skills library", tick=9)
        self.assertEqual(again["id"], o["id"])
        self.assertEqual(again["state"], "active")         # re-raising a goal revives it


class TestConsolidate(_Base):
    def test_nap_merges_accumulated_near_dupes(self):
        # Simulate the sprawl that formed BEFORE merge-on-add existed by bypassing add().
        import json, os
        objs = [objectives._new(t, w, 5, 1) for t, w in [
            ("Skill Library", "build reusable skills"),
            ("Skill Library Foundation", "foundation for reusable skills library"),
            ("Utility Skill Suite", "a suite of utility skills to build"),
            ("Map the holt", "understand the filesystem layout"),
        ]]
        p = objectives._path(self.cfg)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps({"active_id": objs[0]["id"], "objectives": objs}), encoding="utf-8")
        rep = objectives.consolidate(self.cfg, tick=10)
        self.assertGreaterEqual(len(rep["merged"]), 1)     # the skill-library variants collapse
        live = self._titles()
        self.assertIn("Map the holt", live)                # the unrelated goal survives
        self.assertLess(len(live), 4)

    def test_nap_archives_long_stale_blocked_goals(self):
        o = objectives.add(self.cfg, "Old parked thing", "something abandoned", tick=1)
        objectives.block(self.cfg, o["id"], reason="dead end")
        # a block earns the archive only AFTER exposure (belief-refutation guard); simulate a
        # prior re-test that stayed stuck, then archive.
        data = objectives._load(self.cfg)
        objectives._by_id(data, o["id"])["exposures"] = 1
        objectives._save(self.cfg, data)
        rep = objectives.consolidate(self.cfg, tick=2 + objectives.STALE_ARCHIVE_TICKS)
        self.assertIn(o["id"], rep["archived"])
        self.assertEqual(objectives._by_id(objectives._load(self.cfg), o["id"])["state"], "dead")

    def test_consolidate_keeps_active_pointer_valid(self):
        import json
        objs = [objectives._new(t, w, 5, 1) for t, w in [
            ("Skill Library", "reusable skills"),
            ("Skill Library Foundation", "foundation reusable skills"),
        ]]
        # point active at the one that will lose the merge (lower momentum → merged away)
        objs[1]["last_progress_tick"] = 0
        p = objectives._path(self.cfg)
        p.write_text(json.dumps({"active_id": objs[1]["id"], "objectives": objs}), encoding="utf-8")
        objectives.consolidate(self.cfg, tick=10)
        act = objectives.get_active(self.cfg)
        self.assertTrue(act is None or act.get("state") != "dead")

    def test_active_falls_back_to_blocked_survivor_not_none(self):
        # Review medium: survivor is BLOCKED and the active pointer was the merged loser — must
        # point at the blocked survivor, not go None.
        import json
        surv = objectives._new("Skill Library", "reusable skills", 5, 1)
        surv["state"] = "blocked"; surv["last_progress_tick"] = 500
        lose = objectives._new("Skill Library Foundation", "foundation reusable skills", 5, 1)
        lose["last_progress_tick"] = 0
        p = objectives._path(self.cfg)
        p.write_text(json.dumps({"active_id": lose["id"], "objectives": [surv, lose]}),
                     encoding="utf-8")
        objectives.consolidate(self.cfg, tick=600)
        data = objectives._load(self.cfg)
        self.assertEqual(data["active_id"], surv["id"])    # points at the workable blocked survivor

    def test_consolidate_is_a_noop_when_nothing_similar(self):
        objectives.add(self.cfg, "Alpha task", "do alpha", tick=1)
        objectives.add(self.cfg, "Beta work", "handle beta", tick=2)
        rep = objectives.consolidate(self.cfg, tick=5)
        self.assertEqual(rep, {"merged": [], "archived": [], "exposed": []})


if __name__ == "__main__":
    unittest.main()
