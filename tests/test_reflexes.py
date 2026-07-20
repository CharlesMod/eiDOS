"""The reflex rung of the crystallization ladder (WISDOM_PLAN §1) — reflexes.py + the eidos.py
execution hook. Pins the binding invariants (§W):

  - WIS1: promotion reads the ADJUDICATED episodic ledger; guards are quests.Criterion only.
  - WIS2: a reflex-handled outcome is marked automated and farms NO economy; it is EXCLUDED from
    the promotion streak (a reflex can never self-promote).
  - WIS3: a firing renders "[REFLEX]" into the stream; one failed adjudication demotes + disarms.
  - WIS7: flag-off is byte-identical — nothing registers, nothing fires, no file is written.
  - WIS8: bounded registry, atomic, fail-open; the armed cap and the per-tick loop bound hold.
"""

import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

import reflexes
import episodes
import eidos
from config import Config
from parser import ToolCall


def _cfg(**flags):
    c = Config()
    c.workspace_dir = tempfile.mkdtemp()
    c.workspace.mkdir(parents=True, exist_ok=True)
    (c.workspace / "state").mkdir(parents=True, exist_ok=True)
    c.wisdom_reflexes_enabled = True
    c.wisdom_reflex_promote_successes = 5
    c.wisdom_reflex_max_armed = 12
    c.wisdom_reflex_auto_arm = False
    c.wisdom_reflex_saves_tick = False
    for k, v in flags.items():
        setattr(c, k, v)
    return c


def _ep(tick, key, tool, sig, success, automated=False):
    return {"tick": tick, "key": key, "tool": tool, "sig": sig,
            "fail_kind": "" if success else "exec", "success": success,
            "summary": "", "ts": 0.0, **({"automated": True} if automated else {})}


class _Result:
    """A stand-in ToolResult (execute_tool contract: output/success/duration_s/fail_kind)."""
    def __init__(self, success=True, output="did the thing", fail_kind=""):
        self.output = output
        self.success = success
        self.duration_s = 0.0
        self.fail_kind = fail_kind if not success else ""
        self.full_output_path = None


# =================================================================================================
# Promotion streak logic (WIS1) — consecutive successes, interleaved failure resets
# =================================================================================================
class TestPromotionStreak(unittest.TestCase):
    def setUp(self):
        self.c = _cfg()

    def test_five_clean_successes_proposes(self):
        eps = [_ep(i, "objA|do x", "bash", "bash:do", True) for i in range(5)]
        changed = reflexes.scan_promotions(self.c, promote_at=5, episodes=eps)
        self.assertEqual(len(changed), 1)
        r = reflexes.get(self.c, changed[0])
        self.assertEqual(r["status"], reflexes.PROPOSED)
        self.assertEqual(r["trigger"]["situation_key"], "objA|do x")
        self.assertEqual(r["action"]["tool"], "bash")
        self.assertEqual(r["action"]["sig"], "bash:do")
        self.assertEqual(r["provenance"]["successes"], 5)

    def test_four_successes_does_not_propose(self):
        eps = [_ep(i, "objA|do x", "bash", "bash:do", True) for i in range(4)]
        self.assertEqual(reflexes.scan_promotions(self.c, promote_at=5, episodes=eps), [])
        self.assertEqual(reflexes.list_reflexes(self.c), [])

    def test_interleaved_failure_resets_streak(self):
        # 3 successes, a failure in the SAME situation, then 4 successes → trailing run is 4 (< 5).
        eps = ([_ep(i, "objA|do x", "bash", "bash:do", True) for i in range(3)]
               + [_ep(3, "objA|do x", "bash", "bash:do", False)]
               + [_ep(i, "objA|do x", "bash", "bash:do", True) for i in range(4, 8)])
        self.assertEqual(reflexes.scan_promotions(self.c, promote_at=5, episodes=eps), [])

    def test_failure_of_other_action_in_situation_also_resets(self):
        # A failure under situation S (a DIFFERENT action) still breaks the streak keyed on S:
        # the situation started failing again, so it is no longer 'solved'.
        eps = ([_ep(i, "objA|do x", "bash", "bash:do", True) for i in range(4)]
               + [_ep(4, "objA|do x", "read_file", "read_file:foo", False)]
               + [_ep(i, "objA|do x", "bash", "bash:do", True) for i in range(5, 8)])
        self.assertEqual(reflexes.scan_promotions(self.c, promote_at=5, episodes=eps), [])

    def test_automated_episodes_excluded_from_streak(self):
        # WIS2: reflex-handled (automated) episodes never count toward promotion. 5 automated
        # successes must NOT promote; they are the reflex's own firings.
        eps = [_ep(i, "objA|do x", "bash", "bash:do", True, automated=True) for i in range(5)]
        self.assertEqual(reflexes.scan_promotions(self.c, promote_at=5, episodes=eps), [])

    def test_scan_is_idempotent(self):
        eps = [_ep(i, "objA|do x", "bash", "bash:do", True) for i in range(6)]
        reflexes.scan_promotions(self.c, promote_at=5, episodes=eps)
        reflexes.scan_promotions(self.c, promote_at=5, episodes=eps)
        self.assertEqual(len(reflexes.list_reflexes(self.c)), 1)  # no duplicate


# =================================================================================================
# Lifecycle — propose / arm / demote
# =================================================================================================
class TestLifecycle(unittest.TestCase):
    def setUp(self):
        self.c = _cfg()

    def test_arm_and_list(self):
        rid = reflexes.propose(self.c, situation_key="s|x", tool="bash", sig="bash:x")
        self.assertEqual(reflexes.list_reflexes(self.c, status=reflexes.PROPOSED)[0]["id"], rid)
        res = reflexes.arm(self.c, rid)
        self.assertTrue(res["ok"])
        self.assertEqual(reflexes.get(self.c, rid)["status"], reflexes.ARMED)

    def test_arm_missing_is_typed_failure_not_lie(self):
        res = reflexes.arm(self.c, "rfx_nope")
        self.assertFalse(res["ok"])
        self.assertEqual(res["reason"], "no_such_reflex")

    def test_auto_arm_arms_on_proposal(self):
        c = _cfg(wisdom_reflex_auto_arm=True)
        eps = [_ep(i, "objA|do x", "bash", "bash:do", True) for i in range(5)]
        changed = reflexes.scan_promotions(c, promote_at=5, auto_arm=True, episodes=eps)
        self.assertEqual(reflexes.get(c, changed[0])["status"], reflexes.ARMED)

    def test_demote_disarms_and_records_tick(self):
        rid = reflexes.propose(self.c, situation_key="s|x", tool="bash", sig="bash:x")
        reflexes.arm(self.c, rid)
        reflexes.demote(self.c, rid, tick=42, reason="exec")
        r = reflexes.get(self.c, rid)
        self.assertEqual(r["status"], reflexes.DEMOTED)
        self.assertEqual(r["failed_count"], 1)
        self.assertEqual(r["provenance"]["last_adjudicated"], 42)

    def test_demoted_cannot_arm_until_reproposed(self):
        rid = reflexes.propose(self.c, situation_key="s|x", tool="bash", sig="bash:x")
        reflexes.demote(self.c, rid, tick=10)
        res = reflexes.arm(self.c, rid)
        self.assertFalse(res["ok"])
        self.assertEqual(res["reason"], "demoted_needs_reproposal")

    def test_demoted_reproposes_only_from_fresh_run(self):
        # Demote at tick 10. A streak whose last tick is <= 10 is stale → no re-propose. A streak
        # ending AFTER 10 (fresh successes) re-proposes.
        rid = reflexes.propose(self.c, situation_key="objA|do x", tool="bash", sig="bash:do")
        reflexes.demote(self.c, rid, tick=10)
        stale = [_ep(i, "objA|do x", "bash", "bash:do", True) for i in range(5, 10)]  # last tick 9
        self.assertEqual(reflexes.scan_promotions(self.c, promote_at=5, episodes=stale), [])
        self.assertEqual(reflexes.get(self.c, rid)["status"], reflexes.DEMOTED)
        fresh = [_ep(i, "objA|do x", "bash", "bash:do", True) for i in range(11, 16)]  # last 15
        changed = reflexes.scan_promotions(self.c, promote_at=5, episodes=fresh)
        self.assertEqual(changed, [rid])
        self.assertEqual(reflexes.get(self.c, rid)["status"], reflexes.PROPOSED)


# =================================================================================================
# Guard evaluation via quests.Criterion (WIS1 — no new predicate language)
# =================================================================================================
class TestGuards(unittest.TestCase):
    def setUp(self):
        self.c = _cfg()

    def test_guarded_match_requires_criterion_true(self):
        rid = reflexes.propose(self.c, situation_key="s|x", tool="bash", sig="bash:x",
                               guard={"path": "persona.level", "op": ">=", "value": 3})
        reflexes.arm(self.c, rid)
        self.assertIsNone(reflexes.match(self.c, "s|x", {"persona": {"level": 2}}))
        self.assertIsNotNone(reflexes.match(self.c, "s|x", {"persona": {"level": 5}}))

    def test_empty_guard_is_permissive(self):
        rid = reflexes.propose(self.c, situation_key="s|x", tool="bash", sig="bash:x")
        reflexes.arm(self.c, rid)
        self.assertIsNotNone(reflexes.match(self.c, "s|x", {}))

    def test_only_armed_match(self):
        reflexes.propose(self.c, situation_key="s|x", tool="bash", sig="bash:x")  # proposed, not armed
        self.assertIsNone(reflexes.match(self.c, "s|x", {}))

    def test_situation_mismatch_no_match(self):
        rid = reflexes.propose(self.c, situation_key="s|x", tool="bash", sig="bash:x")
        reflexes.arm(self.c, rid)
        self.assertIsNone(reflexes.match(self.c, "s|OTHER", {}))

    def test_compound_criterion_guard(self):
        rid = reflexes.propose(self.c, situation_key="s|x", tool="bash", sig="bash:x",
                               guard={"all_of": [{"path": "persona.level", "op": ">=", "value": 2},
                                                 {"path": "quests.passed", "op": ">=", "value": 1}]})
        reflexes.arm(self.c, rid)
        self.assertIsNone(reflexes.match(self.c, "s|x", {"persona": {"level": 3}, "quests": {"passed": 0}}))
        self.assertIsNotNone(reflexes.match(self.c, "s|x", {"persona": {"level": 3}, "quests": {"passed": 2}}))


# =================================================================================================
# Caps (WIS8)
# =================================================================================================
class TestCaps(unittest.TestCase):
    def test_max_armed_bound(self):
        c = _cfg(wisdom_reflex_max_armed=2)
        ids = [reflexes.propose(c, situation_key=f"s|{i}", tool="bash", sig=f"bash:{i}")
               for i in range(4)]
        self.assertTrue(reflexes.arm(c, ids[0])["ok"])
        self.assertTrue(reflexes.arm(c, ids[1])["ok"])
        capped = reflexes.arm(c, ids[2])
        self.assertFalse(capped["ok"])
        self.assertEqual(capped["reason"], "max_armed")
        self.assertEqual(len(reflexes.list_reflexes(c, status=reflexes.ARMED)), 2)

    def test_auto_arm_respects_cap(self):
        c = _cfg(wisdom_reflex_max_armed=1, wisdom_reflex_auto_arm=True)
        eps = ([_ep(i, "objA|x", "bash", "bash:a", True) for i in range(5)]
               + [_ep(i, "objB|y", "bash", "bash:b", True) for i in range(5)])
        reflexes.scan_promotions(c, promote_at=5, auto_arm=True, episodes=eps)
        self.assertEqual(len(reflexes.list_reflexes(c, status=reflexes.ARMED)), 1)


# =================================================================================================
# Execution hook (eidos._maybe_fire_reflex) — WIS2 economy exclusion + WIS3 honesty + loop bound
# =================================================================================================
class TestExecutionHook(unittest.TestCase):
    def setUp(self):
        self.c = _cfg(wisdom_reflex_saves_tick=True)
        # A stable situation key for the hook: patch episodes.situation_key.
        self._orig_sit = episodes.situation_key
        episodes.situation_key = lambda config: "objA|do x"
        self._orig_exec = eidos.execute_tool
        # Arm a reflex on that situation.
        rid = reflexes.propose(self.c, situation_key="objA|do x", tool="bash", sig="bash:do")
        reflexes.arm(self.c, rid)
        self.rid = rid

    def tearDown(self):
        episodes.situation_key = self._orig_sit
        eidos.execute_tool = self._orig_exec

    def test_fire_marks_automated_and_skips_economy(self):
        calls = {"execute": 0}

        def fake_exec(call, config):
            calls["execute"] += 1
            self.assertIsInstance(call, ToolCall)
            self.assertEqual(call.tool, "bash")
            return _Result(success=True)
        eidos.execute_tool = fake_exec

        persona = {"level": 1, "xp": 0}
        saved = eidos._maybe_fire_reflex(self.c, persona, tick_number=100)
        self.assertTrue(saved)                 # reflex_saves_tick on → tick ends here
        self.assertEqual(calls["execute"], 1)  # fired through the one chokepoint

        # WIS2: NO economy — persona untouched (no XP awarded, no counters moved).
        self.assertEqual(persona["xp"], 0)
        self.assertEqual(persona.get("ticks_alive", 0), 0)

        # The automated episode is on disk and marked automated (excluded from future promotion).
        rows = episodes._read(self.c)
        self.assertEqual(len(rows), 1)
        self.assertTrue(rows[0]["automated"])
        self.assertEqual(rows[0]["tool"], "bash")
        self.assertTrue(rows[0]["success"])

        # WIS3: rendered as a reflex in the observation stream.
        obs = (self.c.observations_path).read_text(encoding="utf-8")
        self.assertIn("[REFLEX]", obs)

        # The below-model fraction counts this tick as reflex-handled.
        self.assertGreater(reflexes.fraction_today(self.c), 0.0)

    def test_automated_episode_cannot_self_promote(self):
        # Fire the reflex promote_successes times; the automated episodes must NOT re-promote it
        # (WIS2 exclusion end-to-end through the real hook).
        eidos.execute_tool = lambda call, config: _Result(success=True)
        for t in range(6):
            eidos._maybe_fire_reflex(self.c, {"level": 1, "xp": 0}, tick_number=200 + t)
        promoted = reflexes.scan_promotions(self.c, promote_at=5)
        self.assertEqual(promoted, [])

    def test_fire_failure_demotes_and_scars(self):
        eidos.execute_tool = lambda call, config: _Result(success=False, fail_kind="exec")
        saved = eidos._maybe_fire_reflex(self.c, {"level": 1, "xp": 0}, tick_number=300)
        # A failed reflex demotes; saves_tick still True means the LLM was skipped this tick.
        self.assertTrue(saved)
        self.assertEqual(reflexes.get(self.c, self.rid)["status"], reflexes.DEMOTED)
        # No longer matches (disarmed).
        self.assertIsNone(reflexes.match(self.c, "objA|do x", {}))
        obs = (self.c.observations_path).read_text(encoding="utf-8")
        self.assertIn("[REFLEX]", obs)

    def test_soak_mode_does_not_save_tick(self):
        c = _cfg(wisdom_reflex_saves_tick=False)
        rid = reflexes.propose(c, situation_key="objA|do x", tool="bash", sig="bash:do")
        reflexes.arm(c, rid)
        eidos.execute_tool = lambda call, config: _Result(success=True)
        saved = eidos._maybe_fire_reflex(c, {"level": 1, "xp": 0}, tick_number=400)
        self.assertFalse(saved)   # soak: reflex ran, in-stream, but the model still runs this tick
        # Still recorded the automated episode and rendered the reflex.
        self.assertEqual(len(episodes._read(c)), 1)

    def test_loop_bound_disarms_rabbit_hole(self):
        # A reflex that fires bound-many times in the same unchanging situation disarms itself.
        c = _cfg(wisdom_reflex_saves_tick=True, wisdom_reflex_loop_bound=3)
        rid = reflexes.propose(c, situation_key="objA|do x", tool="bash", sig="bash:do")
        reflexes.arm(c, rid)
        eidos.execute_tool = lambda call, config: _Result(success=True)
        fired = 0
        for t in range(6):
            if eidos._maybe_fire_reflex(c, {"level": 1, "xp": 0}, tick_number=500 + t):
                fired += 1
            if reflexes.get(c, rid)["status"] == reflexes.DEMOTED:
                break
        # It fired at most loop_bound times, then disarmed itself.
        self.assertEqual(reflexes.get(c, rid)["status"], reflexes.DEMOTED)
        self.assertLessEqual(fired, 3)

    def test_no_match_returns_false_and_counts_below_model_false(self):
        c = _cfg(wisdom_reflex_saves_tick=True)
        # No reflex armed for "objA|do x" in this fresh config.
        saved = eidos._maybe_fire_reflex(c, {"level": 1, "xp": 0}, tick_number=600)
        self.assertFalse(saved)
        self.assertEqual(reflexes.fraction_today(c), 0.0)  # not reflex-handled


# =================================================================================================
# Flag-off byte-identical (WIS7) — nothing registers, nothing fires, no file is written
# =================================================================================================
class TestFlagOff(unittest.TestCase):
    def test_hook_inert_when_flag_off(self):
        c = _cfg(wisdom_reflexes_enabled=False, wisdom_reflex_saves_tick=True)
        # Even with an armed reflex present, the hook must not fire when the master flag is off.
        rid = reflexes.propose(c, situation_key="objA|do x", tool="bash", sig="bash:do")
        reflexes.arm(c, rid)
        state_before = (c.state_dir / "reflexes.json").read_text(encoding="utf-8")
        orig_sit = episodes.situation_key
        episodes.situation_key = lambda config: "objA|do x"
        called = {"n": 0}
        orig_exec = eidos.execute_tool
        eidos.execute_tool = lambda call, config: called.__setitem__("n", called["n"] + 1)
        try:
            saved = eidos._maybe_fire_reflex(c, {"level": 1, "xp": 0}, tick_number=1)
        finally:
            episodes.situation_key = orig_sit
            eidos.execute_tool = orig_exec
        self.assertFalse(saved)
        self.assertEqual(called["n"], 0)                   # nothing executed
        self.assertFalse(episodes._path(c).exists())       # no automated episode written
        self.assertFalse((c.state_dir / "reflex_fraction.json").exists())  # no fraction file
        # The registry is untouched by the inert hook.
        self.assertEqual((c.state_dir / "reflexes.json").read_text(encoding="utf-8"), state_before)


# =================================================================================================
# Persistence bounds + fraction (WIS8)
# =================================================================================================
class TestPersistence(unittest.TestCase):
    def setUp(self):
        self.c = _cfg()

    def test_registry_atomic_roundtrip(self):
        rid = reflexes.propose(self.c, situation_key="s|x", tool="bash", sig="bash:x")
        raw = json.loads((self.c.state_dir / "reflexes.json").read_text(encoding="utf-8"))
        self.assertEqual(raw["reflexes"][0]["id"], rid)

    def test_corrupt_registry_fails_open(self):
        (self.c.state_dir / "reflexes.json").write_text("{not json", encoding="utf-8")
        self.assertEqual(reflexes.list_reflexes(self.c), [])
        # And a fresh propose still works (rewrites the file).
        reflexes.propose(self.c, situation_key="s|x", tool="bash", sig="bash:x")
        self.assertEqual(len(reflexes.list_reflexes(self.c)), 1)

    def test_registry_bounded(self):
        for i in range(reflexes._MAX_REFLEXES + 20):
            reflexes.propose(self.c, situation_key=f"s|{i}", tool="bash", sig=f"bash:{i}")
        self.assertLessEqual(len(reflexes.list_reflexes(self.c)), reflexes._MAX_REFLEXES)

    def test_fraction_records_and_reads(self):
        reflexes.record_tick_outcome(self.c, handled_by_reflex=True, day="2026-07-20")
        reflexes.record_tick_outcome(self.c, handled_by_reflex=False, day="2026-07-20")
        reflexes.record_tick_outcome(self.c, handled_by_reflex=False, day="2026-07-20")
        self.assertAlmostEqual(reflexes.fraction_today(self.c, day="2026-07-20"), 1 / 3)

    def test_fraction_bounded_days(self):
        for d in range(reflexes._MAX_FRACTION_DAYS + 10):
            reflexes.record_tick_outcome(self.c, handled_by_reflex=True, day=f"2026-{(d % 12) + 1:02d}-{(d % 28) + 1:02d}")
        raw = json.loads((self.c.state_dir / "reflex_fraction.json").read_text(encoding="utf-8"))
        self.assertLessEqual(len(raw["days"]), reflexes._MAX_FRACTION_DAYS)


if __name__ == "__main__":
    unittest.main()
