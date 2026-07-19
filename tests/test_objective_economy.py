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


class TestObjectiveHandle(_Base):
    """2026-07-13 friction: the list showed '[3]' (the PRIORITY) which read like an id, so a newborn
    typed '3' into objective_done and nothing matched. The TITLE is the handle; the list shows it and
    a refusal teaches it."""

    def test_done_by_title_substring_not_priority_number(self):
        import tools
        objectives.add(self.cfg, "Build the contradiction log", "why", priority=3, tick=1)
        # The priority number is NOT a handle — a bare "3" must be refused, teachingly.
        r = tools.tool_objective_done({"id": "3"}, self.cfg)
        assert not r.success
        assert "Build the contradiction log" in r.output and "TITLE" in r.output
        # A few words of the title DO close it.
        r = tools.tool_objective_done({"title": "contradiction log"}, self.cfg)
        assert r.success and "contradiction log" in r.output.lower()

    def test_list_leads_with_title_and_teaches_the_handle(self):
        import tools
        objectives.add(self.cfg, "Map the nest", "why", priority=5, tick=1)
        out = tools.tool_objective_list({}, self.cfg).output
        assert "Map the nest" in out
        assert "p5" in out                      # priority demoted to a trailing tag, not a leading id
        assert "objective_done" in out and "TITLE" in out   # the footer teaches how to act


class TestExposureCappedDeath(_Base):
    """The doom-loop cure: a futile SELF-set goal that is tested EXPOSURE_CAP times without ever
    making STRONG progress is RELEASED as dead — terminal, never re-thawed into the creature's face.
    A single strong-progress tick proves controllability and restores the full budget."""

    def _find(self, title):
        return next(o for o in objectives._load(self.cfg)["objectives"] if o["title"] == title)

    def test_futile_self_goal_is_released_dead_after_exposure_cap(self):
        objectives.add(self.cfg, "Map the Outside", "reach beyond the nest", priority=5, tick=1)
        t, died = 2, None
        # A long futile campaign: never any strong progress. The gate parks, cools down, thaws
        # (exposure++), re-parks — and after EXPOSURE_CAP thaws, releases the goal as dead.
        for _ in range(400):
            g = objectives.record_tick(self.cfg, made_progress=False, tool_failed=False,
                                       tick_number=t, progress_strong=False)
            if g.get("died"):
                died = g["died"]; break
            # backlog emptied (parked, nothing active) → jump past the thaw cooldown to force a retry
            t += objectives.THAW_COOLDOWN + 1 if objectives.get_active(self.cfg) is None else 1
        assert died is not None, "the futile goal was never released — the doom loop persists"
        assert died["title"] == "Map the Outside"
        m = self._find("Map the Outside")
        assert m["state"] == "dead" and m["exposures"] >= objectives.EXPOSURE_CAP
        # Terminal: more idle ticks never re-activate it — no thaw ping-pong, backlog stays empty.
        for _ in range(4):
            t += objectives.THAW_COOLDOWN + 1
            objectives.record_tick(self.cfg, made_progress=False, tool_failed=False,
                                   tick_number=t, progress_strong=False)
        assert objectives.get_active(self.cfg) is None

    def test_strong_progress_refutes_and_resets_the_exposure_budget(self):
        objectives.add(self.cfg, "Learnable", "hard but doable", priority=5, tick=1)
        objectives.block(self.cfg, "Learnable", "seems hard", "")
        # past the cooldown, an idle tick thaws it (exposure 1, tagged _thawed_from_block)
        g = objectives.record_tick(self.cfg, made_progress=False, tool_failed=False,
                                   tick_number=40, progress_strong=False)
        assert objectives.get_active(self.cfg)["title"] == "Learnable"
        assert self._find("Learnable")["exposures"] == 1
        # STRONG progress on the thawed goal → refutes the block AND resets exposures to 0
        g = objectives.record_tick(self.cfg, made_progress=True, tool_failed=False,
                                   tick_number=41, progress_strong=True)
        assert g.get("refuted_block") and g["refuted_block"]["title"] == "Learnable"
        assert self._find("Learnable")["exposures"] == 0

    def test_weak_progress_relieves_but_neither_refutes_nor_resets(self):
        objectives.add(self.cfg, "Nest", "tidy up", priority=5, tick=1)
        objectives.block(self.cfg, "Nest", "stuck", "")
        objectives.record_tick(self.cfg, made_progress=False, tool_failed=False,
                               tick_number=40, progress_strong=False)   # thaw → exposures 1
        assert self._find("Nest")["exposures"] == 1
        before = self._find("Nest")["frustration"]
        # WEAK progress (a file) relieves frustration but must NOT refute the block or reset exposures
        g = objectives.record_tick(self.cfg, made_progress=True, tool_failed=False,
                                   tick_number=41, progress_strong=False)
        assert g.get("refuted_block") is None
        assert self._find("Nest")["exposures"] == 1                     # budget NOT restored by a diary file
        assert self._find("Nest")["frustration"] <= before             # but it did relieve

    def test_operator_and_survival_goals_never_reach_this_gate(self):
        # Provenance safety: the exposure gate only governs SELF-set objectives. This is structural —
        # commissions/quests/survival live in other stores this module never loads. Assert the module
        # only ever touches objectives.json (no cross-store reach).
        import inspect
        src = inspect.getsource(objectives.record_tick)
        assert "commission" not in src and "quest" not in src and "metabolism" not in src

    def test_impossible_goal_that_only_weak_progresses_still_dies(self):
        # H1: WEAK progress (a cosmetic file) relieves frustration BY DESIGN, so a goal minting files
        # forever would keep frustration near 0 and never trip the frustration-park gate — immortal.
        # The strong-progress-stall clock must park it anyway and feed the exposure/death cascade.
        objectives.add(self.cfg, "Map the Outside", "reach beyond the nest", priority=5, tick=1)
        t, died = 2, None
        for _ in range(3000):
            g = objectives.record_tick(self.cfg, made_progress=True, tool_failed=False,
                                       tick_number=t, progress_strong=False)   # WEAK progress EVERY tick
            if g.get("died"):
                died = g["died"]; break
            t += objectives.THAW_COOLDOWN + 1 if objectives.get_active(self.cfg) is None else 1
        assert died is not None, "an impossible weak-progress-only goal never died — immortal doom loop"
        assert died["title"] == "Map the Outside"
        assert self._find("Map the Outside")["state"] == "dead"

    def test_exposure_spent_block_is_released_dead_at_the_thaw_choke_point(self):
        # H2: the death gate used to live ONLY in the frustration-park branch, so the model could
        # ping-pong a futile goal via its own objective_block tool (block → gate thaws → block …)
        # forever without ever routing through that branch. Now every re-thaw passes the death check.
        objectives.add(self.cfg, "Futile", "cannot be done", priority=5, tick=1)
        objectives.block(self.cfg, "Futile", "stuck", "")
        data = objectives._load(self.cfg)
        objectives._by_id(data, self._find("Futile")["id"])["exposures"] = objectives.EXPOSURE_CAP
        objectives._save(self.cfg, data)
        # past the cooldown the gate would normally thaw it — but its budget is spent → release dead
        g = objectives.record_tick(self.cfg, made_progress=False, tool_failed=False,
                                   tick_number=100, progress_strong=False)
        assert g.get("died") and g["died"]["title"] == "Futile"
        assert self._find("Futile")["state"] == "dead"
        assert objectives.get_active(self.cfg) is None


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
