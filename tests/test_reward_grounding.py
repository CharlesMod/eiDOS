"""Reward grounding (RELEASE_PLAN SOTA#1 / Pillar 6): two mechanisms that keep the reward signal
tied to the world instead of to a self-referential fiction.

(A) LESSONS GENERALIZE OVER ACTION SHAPE ONLY. A distilled lesson/habit — and the value key it comes
    from — must carry the tool + argument SIGNATURE (which keys, of which kind), never a content
    payload. A creature that authored ~100 tiny text files saw a lesson literally recommending
    `write_file {"content": "The Nursery: From Seed to Sprout..."}` "lean into it"; that is the loop
    coaching its own fiction back into the head. The learner must never embed a content string.

(B) REPETITION MUST NOT PAY FULL PRICE FOREVER. `cat garden.txt` #500 booked the same success reward
    as #1. A habituation term decays the per-success reward contribution of a REPEATED
    (action-signature, target) pair toward a floor, recovering slowly over wall-clock time, so a NOVEL
    signature/target out-competes rehearsal — pure pressure, no named behavior (PILLARS_PLAN §0).
"""
import json
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from nervous.reward import (RewardLearner, action_signature, action_target,  # noqa: E402
                            W_SUCCESS, HABIT_FLOOR)


# The exact fiction payload the live creature converged on — the string a lesson must NEVER carry.
FICTION = ("The Nursery: From Seed to Sprout. In the beginning there was a seed, and the seed "
           "dreamed of a garden, and the garden dreamed back. Chapter one: the soil remembers.")


class TestActionSignatureStripsContent(unittest.TestCase):
    """The signature/target reducers carry SHAPE, never a content value."""

    def test_signature_drops_the_content_payload(self):
        raw = 'write_file {"content": %s, "path": "garden.txt"}' % json.dumps(FICTION)
        sig = action_signature(raw)
        self.assertNotIn("Nursery", sig)
        self.assertNotIn("seed", sig)
        self.assertNotIn(FICTION, sig)
        # but the SHAPE survives: the tool and which keys of which kind
        self.assertIn("write_file", sig)
        self.assertIn("content=str", sig)
        self.assertIn("path=str", sig)

    def test_two_different_contents_share_one_signature(self):
        # Two DIFFERENT long fictions (same coarse length band) reduce to the identical shape — the
        # signature carries a length bucket, never the words, so the loop cannot coach a specific text.
        other = ("A Different Chronicle entirely, of moons and tides and the slow turning of the "
                 "year, with nothing of gardens or seeds in it whatsoever, quite a long passage too.")
        a = action_signature('write_file {"content": %s, "path": "x.txt"}' % json.dumps(FICTION))
        b = action_signature('write_file {"content": %s, "path": "x.txt"}' % json.dumps(other))
        self.assertEqual(a, b)   # shape-level: same tool, same arg kinds → one lesson, not per-string

    def test_target_is_content_free(self):
        tgt = action_target('write_file {"content": %s, "path": "garden3.txt"}' % json.dumps(FICTION))
        self.assertNotIn("Nursery", tgt)
        self.assertNotIn(FICTION, tgt)
        self.assertTrue(tgt)                         # a locator exists (the path)
        # digit-collapsed so garden3/garden4 share a target
        self.assertEqual(tgt, action_target('write_file {"content": "y", "path": "garden4.txt"}'))


class TestLessonsCarryShapeOnly(unittest.TestCase):
    """(A) Feed a synthetic history of long-content write_file successes; the distilled lesson must
    carry the action shape only — no content payload — and the value key likewise."""

    def _learner(self):
        # habituation OFF here: this test is about lesson CONTENT, not the repeat-decay math.
        return RewardLearner(alpha=1.0, lesson_min_count=3, lesson_min_abs=0.2)

    def test_distilled_lesson_never_embeds_content(self):
        rl = self._learner()
        # a synthetic history: the same write_file SHAPE, different long fiction each time + real
        # progress so the value climbs into lesson territory. Vary the path so the target moves (this
        # test isolates the CONTENT-stripping, not habituation).
        for i in range(6):
            payload = f"{FICTION} — variation {i} " + ("padding " * 40)
            action = 'write_file {"content": %s, "path": "note%d.txt"}' % (json.dumps(payload), i)
            rl.observe(situation="creature", action=action, success=True, made_progress=True)
        lessons = rl._distill_lessons()
        self.assertTrue(lessons, "expected a distilled lesson from the repeated shape")
        blob = " ".join(lessons)
        # the smoking gun: no fiction string, no payload words, ever
        self.assertNotIn("Nursery", blob)
        self.assertNotIn("padding", blob)
        self.assertNotIn(FICTION, blob)
        # but a shape-level hint IS present ("small write_file notes tend to land")
        self.assertTrue(any("write_file" in l for l in lessons))

    def test_value_key_and_stored_action_are_content_free(self):
        rl = self._learner()
        rl.observe(situation="creature",
                   action='write_file {"content": %s, "path": "a.txt"}' % json.dumps(FICTION),
                   success=True, made_progress=True)
        for k, e in rl.values.items():
            self.assertNotIn("Nursery", k)
            self.assertNotIn("Nursery", e.get("action", ""))
            self.assertNotIn(FICTION, e.get("action", ""))

    def test_legacy_poisoned_value_still_renders_shape_only(self):
        # A value file written before this fix may hold a raw content-bearing action; distillation must
        # sanitize it at render time so no old payload can leak back into the coaching head.
        rl = self._learner()
        rl.values["at ease::creature::write_file {\"content\": \"%s\"}" % FICTION] = {
            "v": 0.8, "n": 6, "situation": "creature",
            "action": 'write_file {"content": "%s", "path": "garden.txt"}' % FICTION}
        lessons = rl._distill_lessons()
        self.assertTrue(lessons)
        self.assertFalse(any("Nursery" in l for l in lessons))
        self.assertTrue(any("write_file" in l for l in lessons))


class TestHabituationDecaysRepeats(unittest.TestCase):
    """(B) A repeated same-signature-same-target success pays a decaying success contribution while a
    NOVEL signature pays full."""

    def _learner(self, **kw):
        # habituation ON, deterministic knobs; make each success a can-fail action that pays W_SUCCESS.
        return RewardLearner(alpha=1.0, habituation_enabled=True,
                             habit_floor=0.3, habit_decay_per_rep=0.5, habit_recovery_s=1000.0, **kw)

    def _reward_for(self, rl, action):
        rl.observe(situation="s", action=action, success=True, made_progress=False)
        return rl.last["reward"]

    def test_repeated_pair_reward_decays_toward_floor(self):
        rl = self._learner()
        act = 'bash: cat garden.txt'
        r1 = self._reward_for(rl, act)
        r2 = self._reward_for(rl, act)
        r3 = self._reward_for(rl, act)
        r4 = self._reward_for(rl, act)
        self.assertAlmostEqual(r1, W_SUCCESS, places=4)     # #1 pays full — nothing rehearsed yet
        self.assertLess(r2, r1)                             # #2 already attenuated
        self.assertLess(r3, r2)
        self.assertLessEqual(r4, r3)
        # never below the floor fraction of the success weight
        self.assertGreaterEqual(r4, W_SUCCESS * HABIT_FLOOR - 1e-6)

    def test_a_novel_signature_pays_full_amid_rehearsal(self):
        rl = self._learner()
        act = 'bash: cat garden.txt'
        for _ in range(5):
            self._reward_for(rl, act)                       # rehearse one pair into habituation
        # a genuinely new SHAPE against a new target still pays the full novelty premium
        r_novel = self._reward_for(rl, 'bash: ls /etc')
        self.assertAlmostEqual(r_novel, W_SUCCESS, places=4)

    def test_same_shape_new_target_pays_full(self):
        rl = self._learner()
        for _ in range(4):
            self._reward_for(rl, 'bash: cat garden.txt')    # habituate cat→garden.txt
        # same VERB shape, DIFFERENT target → exploration, pays full
        r = self._reward_for(rl, 'bash: cat manifesto.txt')
        self.assertAlmostEqual(r, W_SUCCESS, places=4)

    def test_failure_never_habituates(self):
        rl = self._learner()
        # repeated FAILURES of the same shape/target must keep teaching the full penalty — we never
        # dull a warning, only a rehearsed WIN.
        for _ in range(5):
            rl.observe(situation="s", action='bash: cat missing.txt', success=False)
        self.assertAlmostEqual(rl.last["reward"], -W_SUCCESS, places=4)

    def test_disabled_flag_leaves_reward_untouched(self):
        rl = RewardLearner(alpha=1.0, habituation_enabled=False)
        act = 'bash: cat garden.txt'
        first = self._reward_for(rl, act)
        for _ in range(9):
            self._reward_for(rl, act)
        self.assertAlmostEqual(rl.last["reward"], first, places=4)   # flag-dark: no decay at all


class TestHabituationRecovers(unittest.TestCase):
    """(3) Habituation recovers over wall-clock time (dishabituation): a long-unseen action feels
    novel again."""

    def test_recovery_restores_reward_after_elapsed_time(self):
        rl = RewardLearner(alpha=1.0, habituation_enabled=True,
                           habit_floor=0.3, habit_decay_per_rep=0.5, habit_recovery_s=100.0)
        sig = action_signature('bash: cat garden.txt')
        tgt = action_target('bash: cat garden.txt')
        # drive it deep into habituation
        for _ in range(6):
            rl._habituation_scale(sig, tgt, success=True, record=True)
        deep = rl._habituation_scale(sig, tgt, success=True, record=False)
        self.assertLess(deep, 0.6)
        # rewind the last-touched timestamp far into the past → many recovery windows elapse
        key = rl._habit_key(sig, tgt)
        rl._habituation[key]["t"] -= 100.0 * 20        # 20 recovery windows ago
        recovered = rl._habituation_scale(sig, tgt, success=True, record=False)
        self.assertGreater(recovered, deep)
        self.assertAlmostEqual(recovered, 1.0, places=3)   # fully dishabituated → novel again

    def test_decayed_reps_never_below_zero(self):
        rl = RewardLearner(habituation_enabled=True, habit_recovery_s=10.0)
        entry = {"reps": 2.0, "t": 0}
        # a huge elapsed time cannot drive reps negative
        self.assertEqual(rl._decayed_reps(entry, 10_000_000), 0.0)


class TestHabituationPersistenceAndFailOpen(unittest.TestCase):
    """(4) The ledger persists across process restarts and fails open on a corrupt/missing file."""

    def test_ledger_persists_and_reloads(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "habituation.json")
            rl = RewardLearner(alpha=1.0, habituation_enabled=True, habituation_path=path,
                               save_every=1)
            for _ in range(4):
                rl.observe(situation="s", action='bash: cat garden.txt', success=True)
            self.assertTrue(os.path.exists(path))
            reward_after = rl.last["reward"]
            # a fresh learner reloads the ledger → the next repeat stays attenuated, not reset to full
            rl2 = RewardLearner(alpha=1.0, habituation_enabled=True, habituation_path=path,
                                save_every=1)
            self.assertTrue(rl2._habituation)                        # loaded, not empty
            rl2.observe(situation="s", action='bash: cat garden.txt', success=True)
            self.assertLessEqual(rl2.last["reward"], reward_after + 1e-6)

    def test_corrupt_ledger_fails_open_to_empty(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "habituation.json")
            with open(path, "w", encoding="utf-8") as f:
                f.write("{ this is not valid json ]]]")
            rl = RewardLearner(habituation_enabled=True, habituation_path=path)   # must not raise
            self.assertEqual(rl._habituation, {})
            # and it still functions: a fresh action pays full
            rl.observe(situation="s", action='bash: cat garden.txt', success=True)
            self.assertAlmostEqual(rl.last["reward"], W_SUCCESS, places=4)

    def test_missing_ledger_is_empty(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "does_not_exist.json")
            rl = RewardLearner(habituation_enabled=True, habituation_path=path)
            self.assertEqual(rl._habituation, {})

    def test_ledger_is_bounded(self):
        rl = RewardLearner(alpha=1.0, habituation_enabled=True, habit_max_pairs=5)
        for i in range(50):
            rl.observe(situation="s", action=f'bash: cat file{i}.txt', success=True)
        self.assertLessEqual(len(rl._habituation), 5)


if __name__ == "__main__":
    unittest.main()
