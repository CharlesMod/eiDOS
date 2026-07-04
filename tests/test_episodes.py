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

    def test_proven_success_recalled_without_failures(self):
        # 2+ successes and zero failures → proactive "reuse what worked", not silence
        self._ep("bash:arp", True, summary="arp -a got 25 hosts")
        self._ep("bash:arp", True, summary="arp -a got 25 hosts")
        rec = episodes.recall(self.config, key="objA|map the lan")
        self.assertEqual(rec["failures"], [])
        self.assertTrue(rec.get("proven"))
        self.assertEqual(rec["worked"][0]["oks"], 2)
        text = episodes.render_recall(rec)
        self.assertIn("reuse what worked", text)
        self.assertIn("×2", text)

    def test_single_success_stays_silent(self):
        # one success could be luck — recalling it every tick would be noise
        self._ep("bash:arp", True)
        rec = episodes.recall(self.config, key="objA|map the lan")
        self.assertEqual(rec["worked"], [])
        self.assertEqual(episodes.render_recall(rec), "")

    def test_systemic_blocker_same_sig_across_objectives(self):
        # one approach failing under TWO different objectives, never succeeding → systemic
        for obj in ("objA", "objB"):
            for _ in range(2):
                self._ep("bash:mqtt_sub", False, "network", key=f"{obj}|get printer status")
        blk = episodes.systemic_blocker(self.config)
        self.assertIsNotNone(blk)
        self.assertEqual(blk["scope"], "sig")
        self.assertEqual(blk["fails"], 4)
        self.assertEqual(blk["objectives"], 2)
        self.assertIn("Systemic blocker", episodes.render_systemic(blk))

    def test_no_systemic_when_confined_to_one_objective(self):
        for _ in range(6):
            self._ep("bash:mqtt_sub", False, "network", key="objA|get printer status")
        self.assertIsNone(episodes.systemic_blocker(self.config))

    def test_no_systemic_when_sig_sometimes_succeeds(self):
        for obj in ("objA", "objB"):
            for _ in range(2):
                self._ep("bash:mqtt_sub", False, "network", key=f"{obj}|get printer status")
        self._ep("bash:mqtt_sub", True, key="objB|get printer status")  # it CAN work → not a wall
        self.assertIsNone(episodes.systemic_blocker(self.config))

    def test_systemic_kind_pattern_across_many_objectives(self):
        # different commands, same failure KIND, three objectives → environmental pattern
        for i, obj in enumerate(("objA", "objB", "objC")):
            for j in range(3):
                self._ep(f"bash:cmd_{obj}_{j}", False, "network", key=f"{obj}|step {i}")
        blk = episodes.systemic_blocker(self.config)
        self.assertIsNotNone(blk)
        self.assertEqual(blk["scope"], "kind")
        self.assertEqual(blk["kind"], "network")

    def test_episodes_survive_objective_change(self):
        # Same normalized step filed under a DEAD objective id — recall under the new
        # objective id still finds it (the step is the stable part of the situation).
        for _ in range(2):
            self._ep("bash:scan", False, "network", key="objOLD|map the lan")
        rec = episodes.recall(self.config, key="objNEW|map the lan")
        self.assertEqual(len(rec["failures"]), 1)
        self.assertEqual(rec["failures"][0]["fails"], 2)

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


class TestSemanticSituationRecall(unittest.TestCase):
    """Phase 7a-2: cross-situation recall by resemblance. Uses the deterministic mock embedder
    (knowledge_embedding_enabled + mock_mode), so no ONNX model is needed."""

    def setUp(self):
        self.config = _cfg()
        self.config.mock_mode = True
        self.config.knowledge_embedding_enabled = True

    def _fail(self, key, sig="bash:dht_read", kind="timeout", summary="read timed out"):
        episodes.record_episode(self.config, tick=1, tool="bash", sig=sig,
                                fail_kind=kind, success=False, summary=summary, key=key)

    def test_novel_situation_matches_resembling_failure(self):
        # A failure under objA, wiring a dht sensor
        self._fail("objA|wire up the dht## temperature sensor")
        self._fail("objA|wire up the dht## temperature sensor")
        # A NEW situation under a DIFFERENT objective, semantically similar step
        rec = episodes.recall(self.config, key="objB|install the dht## sensor on gpio")
        self.assertTrue(rec.get("similar"))
        self.assertEqual(rec["via_step"], "wire up the dht## temperature sensor")
        self.assertEqual({f["sig"] for f in rec["failures"]}, {"bash:dht_read"})

    def test_unrelated_situation_does_not_match(self):
        self._fail("objA|wire up the dht## temperature sensor")
        rec = episodes.recall(self.config, key="objC|write a poem about the ocean")
        self.assertNotIn("similar", rec)
        self.assertEqual(rec["failures"], [])

    def test_exact_match_is_not_flagged_similar(self):
        # When the exact situation has history, the deterministic path wins — no "similar" tag.
        self._fail("objA|map the network")
        rec = episodes.recall(self.config, key="objA|map the network")
        self.assertEqual(len(rec["failures"]), 1)
        self.assertNotIn("similar", rec)

    def test_disabled_embeddings_no_semantic_and_no_index(self):
        self.config.knowledge_embedding_enabled = False
        self._fail("objA|wire up the dht## temperature sensor")
        # no situation index is written when the layer is off
        self.assertFalse(episodes._sit_key_path(self.config).exists())
        rec = episodes.recall(self.config, key="objB|install the dht## sensor on gpio")
        self.assertNotIn("similar", rec)
        self.assertEqual(rec["failures"], [])

    def test_index_self_builds_on_record(self):
        self._fail("objA|map the network")
        vecs, keys = episodes._load_situations(self.config)
        self.assertIn("objA|map the network", keys)
        self.assertEqual(vecs.shape[0], len(keys))

    def test_resolved_failure_in_similar_situation_stays_quiet(self):
        # similar past situation that FAILED then RECOVERED → no standing failure → no recall
        episodes.record_episode(self.config, tick=1, tool="bash", sig="bash:dht_read",
                                fail_kind="timeout", success=False, key="objA|wire up the dht## sensor")
        episodes.record_episode(self.config, tick=2, tool="bash", sig="bash:dht_read",
                                fail_kind="", success=True, summary="worked", key="objA|wire up the dht## sensor")
        rec = episodes.recall(self.config, key="objB|install the dht## sensor on gpio")
        self.assertEqual(rec["failures"], [])


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

    def test_key_strips_plan_list_marker(self):
        # A numbered plan line digit-collapses to "#." — the marker is plan bookkeeping, not part
        # of the step, and it must not ride into the key (and from there into recall bodies).
        from memory import write_plan
        write_plan(self.config, "# Plan\n1. Create a journal.md file in the nest/ directory")
        step = episodes.situation_key(self.config).split("|", 1)[1]
        self.assertTrue(step.startswith("create a journal"), step)

    def test_key_step_cut_lands_on_word_boundary(self):
        from memory import write_plan
        write_plan(self.config, "# Plan\n1. " + "reconfigure " * 12)   # far past the step cap
        step = episodes.situation_key(self.config).split("|", 1)[1]
        self.assertLessEqual(len(step), episodes.STEP_CHARS)
        self.assertTrue(step.endswith("…"))
        self.assertTrue(all(w == "reconfigure" for w in step[:-1].split()), step)


class TestCleanFragment(unittest.TestCase):

    def test_strips_leading_list_markers(self):
        for raw in ("#. probe the lan", "- probe the lan", "* probe the lan",
                    "1. probe the lan", "1) probe the lan", "2. - probe the lan"):
            self.assertEqual(episodes.clean_fragment(raw, 80), "probe the lan", raw)

    def test_marker_needs_its_own_token(self):
        # A collapsed ip or a glob is content, not a list marker — it must survive.
        self.assertEqual(episodes.clean_fragment("#.#.#.# answered the probe", 80),
                         "#.#.#.# answered the probe")
        self.assertEqual(episodes.clean_fragment("*.txt files moved", 80), "*.txt files moved")

    def test_marker_only_cleans_to_empty(self):
        self.assertEqual(episodes.clean_fragment("#.", 80), "")
        self.assertEqual(episodes.clean_fragment("  - ", 80), "")

    def test_collapses_whitespace_to_one_line(self):
        self.assertEqual(episodes.clean_fragment("probe\nthe   lan\t now", 80), "probe the lan now")

    def test_truncates_at_word_boundary_with_ellipsis(self):
        s = episodes.clean_fragment("meaningful " * 20, 80)
        self.assertLessEqual(len(s), 80)
        self.assertTrue(s.endswith("…"))
        self.assertTrue(all(w == "meaningful" for w in s[:-1].split()), s)   # no severed word

    def test_heals_a_legacy_hard_slice_at_the_cap(self):
        # Pre-fix records were cut s[:cap] mid-word; a shard landing exactly ON the cap is that
        # residue, and a re-clean at the same cap must back it off to a word boundary.
        sliced = ("track my progress with a journal and keep it honest " * 3)[:80]
        healed = episodes.clean_fragment(sliced, 80)
        self.assertTrue(healed.endswith("…"))
        self.assertTrue(healed[:-1].strip().split()[-1] in
                        "track my progress with a journal and keep it honest".split(), healed)

    def test_idempotent_reclean_never_chews_another_word(self):
        once = episodes.clean_fragment("meaningful " * 20, 80)
        self.assertEqual(episodes.clean_fragment(once, 80), once)

    def test_no_limit_means_no_cut(self):
        long = "word " * 50
        self.assertEqual(episodes.clean_fragment(long, 0), long.strip())

    def test_record_cuts_summary_on_word_boundary(self):
        cfg = _cfg()
        episodes.record_episode(cfg, tick=1, tool="bash", sig="bash:probe", fail_kind="",
                                success=True, summary="the probe returned " + "results " * 30,
                                key="objA|probe")
        rec = episodes._read(cfg)[0]
        self.assertLessEqual(len(rec["summary"]), episodes.SUMMARY_CHARS)
        self.assertTrue(rec["summary"].endswith("…"))
        self.assertTrue(all(w in ("the", "probe", "returned", "results")
                            for w in rec["summary"][:-1].split()), rec["summary"])


if __name__ == "__main__":
    unittest.main()
