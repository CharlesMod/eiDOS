"""Pillars 5.1: the quest engine (quests.py) — offline unit tests.

Red-able gates (PILLARS_TODO 5.1):
  - exactly ONE quest active at a time;
  - the next quest issues only after close + ≥1 sleep + healthy condition (blocked in RECOVERY);
  - a passed criteria applies the reward through the sink (standard XP path);
  - a HIDDEN quest stays unrendered until it passes, then reveals;
  - an EXPIRED quest records a failure-lite, episode-shaped outcome (returned, not written);
  - the bounded quests.jsonl rotates its terminal overflow to a monthly archive.

No services / tick loop / GPU — temp workspaces only.
"""

import sys
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

import quests
from quests import (
    Criterion, Quest, System, QuestStore, daily_quest, render_active, render_reveal,
    ACTIVE, OFFERED, PASSED, EXPIRED, DAILY_KINDS, REWARD_XP,
)


class _Config:
    """Minimal Config stand-in: only the two path properties quests.py reads."""
    def __init__(self, root: Path):
        self.workspace = root / "workspace"
        self.workspace.mkdir(parents=True, exist_ok=True)

    @property
    def state_dir(self) -> Path:
        p = self.workspace / "state"
        return p


def _cfg(tmp) -> _Config:
    return _Config(Path(tmp))


def _level_quest(qid="q1", level=3, reward_amount=50, hidden=False, expiry_ts=None):
    return Quest(
        id=qid,
        directive=f"Reach level {level}.",
        success_criteria=Criterion(path="persona.level", op=">=", value=level),
        reward={"kind": REWARD_XP, "amount": reward_amount},
        tier=2,
        hidden=hidden,
        expiry_ts=expiry_ts,
    )


# =================================================================================================
class TestCriteriaGlue(unittest.TestCase):
    """§0.5: criteria are typed predicates over a stats dict, never free text / self-report."""

    def test_leaf_ops(self):
        stats = {"persona": {"level": 3}, "skills": {"trusted_count": 5}}
        self.assertTrue(Criterion(path="persona.level", op=">=", value=3).check(stats))
        self.assertFalse(Criterion(path="persona.level", op=">=", value=4).check(stats))
        self.assertTrue(Criterion(path="skills.trusted_count", op=">", value=4).check(stats))

    def test_missing_path_never_passes(self):
        # A criterion over an absent stat cannot pass — glue never guesses.
        self.assertFalse(Criterion(path="nope.here", op=">=", value=0).check({}))
        self.assertFalse(Criterion(path="persona.level", op="==", value=None).check({"persona": {}}))

    def test_compound_all_any(self):
        stats = {"persona": {"level": 3}, "skills": {"trusted_count": 2}}
        both = Criterion(all_of=[
            Criterion(path="persona.level", op=">=", value=3),
            Criterion(path="skills.trusted_count", op=">=", value=2),
        ])
        self.assertTrue(both.check(stats))
        either = Criterion(any_of=[
            Criterion(path="persona.level", op=">=", value=9),
            Criterion(path="skills.trusted_count", op=">=", value=2),
        ])
        self.assertTrue(either.check(stats))
        self.assertFalse(Criterion(all_of=[
            Criterion(path="persona.level", op=">=", value=3),
            Criterion(path="skills.trusted_count", op=">=", value=9),
        ]).check(stats))

    def test_roundtrip_serialization(self):
        c = Criterion(all_of=[
            Criterion(path="persona.level", op=">=", value=3),
            Criterion(any_of=[Criterion(path="skills.trusted_count", op=">", value=1)]),
        ])
        back = Criterion.from_dict(c.to_dict())
        stats = {"persona": {"level": 4}, "skills": {"trusted_count": 2}}
        self.assertEqual(c.check(stats), back.check(stats))


# =================================================================================================
class TestOneActive(unittest.TestCase):
    """BIBLE L-2: exactly one active quest at a time."""

    def test_issue_next_only_promotes_one(self):
        cfg = _cfg(self._tmp())
        sysm = System(cfg)
        sysm.propose(_level_quest("q1"))
        sysm.propose(_level_quest("q2"))
        first = sysm.issue_next(sleeps_since_close=1, condition="STABLE")
        self.assertIsNotNone(first)
        self.assertEqual(first.state, ACTIVE)
        # A second issue while one is active must be silence — one at a time.
        second = sysm.issue_next(sleeps_since_close=5, condition="STABLE")
        self.assertIsNone(second)
        actives = [q for q in sysm.store.load() if q.state == ACTIVE]
        self.assertEqual(len(actives), 1)

    def _tmp(self):
        import tempfile
        self._td = tempfile.mkdtemp()
        return self._td


# =================================================================================================
class TestCadence(unittest.TestCase):
    """§7: next issues only after close + ≥1 sleep + healthy condition (blocked in RECOVERY)."""

    def setUp(self):
        import tempfile
        self.cfg = _cfg(tempfile.mkdtemp())
        self.sysm = System(self.cfg)
        self.sysm.propose(_level_quest("q1"))

    def test_needs_a_sleep(self):
        # 0 sleeps since close → silence.
        self.assertIsNone(self.sysm.issue_next(sleeps_since_close=0, condition="STABLE"))
        # ≥1 sleep → issues.
        self.assertIsNotNone(self.sysm.issue_next(sleeps_since_close=1, condition="STABLE"))

    def test_recovery_blocks(self):
        # Healthy condition required; RECOVERY is the one condition that stays the System's hand.
        self.assertIsNone(self.sysm.issue_next(sleeps_since_close=3, condition="RECOVERY"))
        # Any other condition (even STRAINED — challenge is not withheld to protect) issues.
        self.assertIsNotNone(self.sysm.issue_next(sleeps_since_close=3, condition="STRAINED"))

    def test_no_queue_is_silence(self):
        sysm = System(_cfg(__import__("tempfile").mkdtemp()))
        self.assertIsNone(sysm.issue_next(sleeps_since_close=9, condition="STABLE"))


# =================================================================================================
class TestAdjudicationAndReward(unittest.TestCase):
    """§0.5: glue judges; a passed criteria applies the reward through the sink."""

    def setUp(self):
        import tempfile
        self.cfg = _cfg(tempfile.mkdtemp())

    def test_pass_applies_reward_via_sink(self):
        paid = []
        sysm = System(self.cfg, reward_sink=lambda cfg, q: paid.append((q.id, q.reward["amount"])))
        sysm.propose(_level_quest("q1", level=3, reward_amount=50))
        q = sysm.issue_next(sleeps_since_close=1, condition="STABLE")
        # Criteria unmet → still active, no payout.
        r = sysm.check(q, {"persona": {"level": 2}})
        self.assertFalse(r["passed"])
        self.assertEqual(paid, [])
        self.assertIsNotNone(sysm.store.active())
        # Criteria met → PASSED + reward through the sink + no longer active.
        r = sysm.check(q, {"persona": {"level": 3}})
        self.assertTrue(r["passed"])
        self.assertEqual(paid, [("q1", 50)])
        self.assertEqual(r["quest"].state, PASSED)
        self.assertIsNone(sysm.store.active())

    def test_default_sink_awards_xp_on_persona(self):
        # The default sink pays through the standard persona.award_xp path.
        import persona as persona_mod
        p = persona_mod._default_persona()
        sysm = System(self.cfg, reward_sink=lambda cfg, q: quests.default_reward_sink(cfg, q, p))
        sysm.propose(_level_quest("q1", level=1, reward_amount=200))
        q = sysm.issue_next(sleeps_since_close=1, condition="STABLE")
        before = p["xp"]
        sysm.check(q, {"persona": {"level": 1}})
        self.assertEqual(p["xp"], before + 200)
        self.assertGreaterEqual(p["level"], 2)   # 200 XP crosses to level 3 by compute_level


# =================================================================================================
class TestHidden(unittest.TestCase):
    """§7: a hidden quest stays unrendered until it passes, then reveals."""

    def test_hidden_unrendered_until_pass(self):
        import tempfile
        cfg = _cfg(tempfile.mkdtemp())
        sysm = System(cfg)
        sysm.propose(_level_quest("secret", level=2, hidden=True))
        q = sysm.issue_next(sleeps_since_close=1, condition="STABLE")
        # Active but hidden → render_active is empty.
        self.assertEqual(render_active(q), "")
        # Unmet → no reveal.
        r = sysm.check(q, {"persona": {"level": 1}})
        self.assertFalse(r["reveal"])
        # Met → reveal=True; render_reveal announces it.
        r = sysm.check(q, {"persona": {"level": 2}})
        self.assertTrue(r["passed"])
        self.assertTrue(r["reveal"])
        self.assertIn("ACHIEVEMENT", render_reveal(r["quest"]))

    def test_visible_quest_renders(self):
        q = _level_quest("v1")
        q.state = ACTIVE
        out = render_active(q)
        self.assertIn("SYSTEM", out)
        self.assertIn("QUEST", out)
        self.assertIn("Reach level", out)


# =================================================================================================
class TestExpiry(unittest.TestCase):
    """§7: an expired/ignored quest records a failure-lite, episode-shaped outcome (returned)."""

    def setUp(self):
        import tempfile
        self.cfg = _cfg(tempfile.mkdtemp())

    def test_expiry_via_check(self):
        sysm = System(self.cfg)
        past = time.time() - 10
        sysm.propose(_level_quest("q1", level=9, expiry_ts=past))
        q = sysm.issue_next(sleeps_since_close=1, condition="STABLE")
        r = sysm.check(q, {"persona": {"level": 1}})  # unmet + past expiry
        self.assertTrue(r["expired"])
        self.assertEqual(r["quest"].state, EXPIRED)
        ep = r["episode"]
        # Episode-shaped (mirrors episodes.py) failure-lite record.
        self.assertFalse(ep["success"])
        self.assertEqual(ep["fail_kind"], "quest_expired")
        self.assertEqual(ep["sig"], "q1")
        self.assertEqual(ep["key"], "quest|q1")
        self.assertIsNone(sysm.store.active())

    def test_expire_if_due_records_ignore(self):
        # Ignoring a quest (never checked, lapses at the sleep boundary) is itself recorded.
        sysm = System(self.cfg)
        past = time.time() - 1
        sysm.propose(_level_quest("q1", level=9, expiry_ts=past))
        sysm.issue_next(sleeps_since_close=1, condition="STABLE")
        ep = sysm.expire_if_due()
        self.assertIsNotNone(ep)
        self.assertEqual(ep["fail_kind"], "quest_expired")
        self.assertIsNone(sysm.store.active())

    def test_expire_if_due_prefers_pass_when_met(self):
        # A quest that is complete at the deadline pays out rather than expiring.
        paid = []
        sysm = System(self.cfg, reward_sink=lambda cfg, q: paid.append(q.id))
        past = time.time() - 1
        sysm.propose(_level_quest("q1", level=2, expiry_ts=past))
        sysm.issue_next(sleeps_since_close=1, condition="STABLE")
        ep = sysm.expire_if_due({"persona": {"level": 5}})
        self.assertIsNone(ep)          # passed, not expired
        self.assertEqual(paid, ["q1"])
        self.assertEqual(sysm.store.active(), None)


# =================================================================================================
class TestDailyQuests(unittest.TestCase):
    """§7: daily quests are recurring drill slots — quest objects + criteria hooks only."""

    def test_all_kinds_build(self):
        for kind in DAILY_KINDS:
            q = daily_quest(kind)
            self.assertEqual(q.kind, "daily")
            self.assertTrue(q.directive)
            # Each carries a glue-checkable criterion hook the drill will satisfy.
            self.assertIsInstance(q.success_criteria, Criterion)

    def test_criterion_hook_adjudicates_from_stats(self):
        q = daily_quest("scar_retest")
        self.assertFalse(q.success_criteria.check({"drills": {"scar_retest_passed": False}}))
        self.assertTrue(q.success_criteria.check({"drills": {"scar_retest_passed": True}}))

    def test_unknown_kind_raises(self):
        with self.assertRaises(ValueError):
            daily_quest("nonexistent_drill")


# =================================================================================================
class TestRotation(unittest.TestCase):
    """Bounded quests.jsonl rotates terminal overflow to a dated monthly archive."""

    def test_bounded_jsonl_rotates(self):
        import tempfile
        cfg = _cfg(tempfile.mkdtemp())
        store = QuestStore(cfg, max_bytes=2000)   # tiny threshold to force a rotation offline
        # Fill with many terminal (passed) quests to cross the byte threshold.
        many = []
        for i in range(60):
            q = _level_quest(f"old{i}")
            q.state = PASSED
            q.closed_ts = f"2026-01-01T00:{i:02d}:00Z"
            many.append(q)
        # Keep one live/queued quest — it must NEVER be archived.
        live = _level_quest("live")
        live.state = OFFERED
        many.append(live)
        store.save(many)

        # Live file no longer holds all 60 terminal quests; the archive exists and holds the overflow.
        remaining = store.load()
        remaining_ids = {q.id for q in remaining}
        self.assertIn("live", remaining_ids)                     # live never archived
        self.assertLess(len(remaining), 61)                      # rotation trimmed the live file
        arc = cfg.state_dir / f"{quests.ARCHIVE_PREFIX}{time.strftime('%Y%m')}.jsonl"
        self.assertTrue(arc.exists())
        archived_lines = [l for l in arc.read_text(encoding="utf-8").splitlines() if l.strip()]
        self.assertGreater(len(archived_lines), 0)

    def test_single_writer_roundtrip(self):
        import tempfile
        cfg = _cfg(tempfile.mkdtemp())
        store = QuestStore(cfg)
        q = _level_quest("q1")
        store.save([q])
        loaded = store.load()
        self.assertEqual(len(loaded), 1)
        self.assertEqual(loaded[0].id, "q1")
        self.assertEqual(loaded[0].success_criteria.value, 3)


if __name__ == "__main__":
    unittest.main()
