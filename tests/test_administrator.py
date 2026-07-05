"""Pillars 5.2: the Administrator (administrator.py) — offline unit tests.

Red-able gates (PILLARS_TODO 5.2):
  - THE FULL CYCLE (mock llm, unattended except approvals): sleep-completion event →
    should_check_in → dossier compiled → check_in proposes a gap-targeted quest (a tier with zero
    trusted skills appears in the dossier; the canned proposal targets it) → approve_proposal →
    the quest appears in the System's queue → issue_next + glue adjudication passes it → XP
    settles through the reward sink → the NEXT check-in's dossier references the outcome;
  - THE WALL: the creature-facing render (quests.render_active) contains NO Administrator
    internals — dossier / fourth-wall strings never appear in any render output, and quests.py
    imports nothing from administrator.py (one-directional by construction);
  - tuning flags name knobs but never values; malformed mock output is dropped-with-log
    (asserted), never committed;
  - graduated autonomy: after the declared approval streak on a tier the next proposal in that
    tier auto-issues; revoke_autonomy stops it;
  - flag off → every entrypoint is a no-op (nothing written).

No services / tick loop / GPU — temp workspaces and a MOCK llm only.
"""

import json
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

import administrator
from administrator import (
    AdminState, EVT_LEVEL_CANDIDACY, EVT_OPERATOR_REQUEST, EVT_QUEST_CLOSED, EVT_SLEEP_COMPLETE,
    EVT_SUSPENSION, AUTONOMY_MIN_SAMPLE, approve_proposal, build_admin_grammar, check_in,
    compile_dossier, fourth_wall_context, parse_admin_output, pending_proposals, reject_proposal,
    revoke_autonomy, should_check_in, tier_has_autonomy,
)
import persona as persona_mod
import quests
import skills as skills_mod


class _Config:
    """Minimal Config stand-in: the paths + flags administrator.py and its evidence sources read."""
    def __init__(self, root: Path, *, admin_on: bool = True):
        self.workspace = root / "workspace"
        self.workspace.mkdir(parents=True, exist_ok=True)
        self.pillars_administrator_enabled = admin_on
        self.pillars_mastery_gates_enabled = True
        self.pillars_min_sleeps_per_level = 3

    @property
    def state_dir(self) -> Path:
        return self.workspace / "state"

    @property
    def knowledge_dir(self) -> Path:
        return self.workspace / "knowledge"


def _gap_dossier_manifest(config) -> None:
    """A skill economy with trusted TIER-1 skills and ZERO trusted skills in tier 2 — the gap the
    canned Administrator proposal targets."""
    skills_mod._save_manifest(config, {"skills": {
        "list_dir": {"status": "trusted", "tier": 1, "invocations": 9, "successes": 9},
        "read_file": {"status": "trusted", "tier": 1, "invocations": 7, "successes": 7},
        "half_baked": {"status": "active", "tier": 2, "invocations": 1, "successes": 0},
    }})


def _canned_output(qid="adm_t2_first_trusted", tier=2, knob="TRUSTED_PER_TIER") -> str:
    """A grammar-valid canned check-in output: one gap-targeted quest + one tuning flag."""
    return json.dumps({
        "quests": [{
            "id": qid,
            "directive": "Earn trust at the new tier. Author nothing; harden what exists.",
            "tier": tier,
            "reward_xp": 40,
            "expiry_hours": 0,
            "criteria": {"path": "skills.tiers.2.trusted", "op": ">=", "value": 1},
        }],
        "weakness_report": "Tier 2 has zero trusted skills; calibration history is thin.",
        "narrator": "The next door does not open for the unproven.",
        "tuning_flags": [{"knob": knob,
                          "evidence": "tier-2 trusted count has been 0 for the whole window"}],
    })


def _mock_llm(output: str):
    """An injectable (messages, grammar) -> str that records what it was called with."""
    calls = []

    def llm(messages, grammar):
        calls.append({"messages": messages, "grammar": grammar})
        return output

    llm.calls = calls
    return llm


# =================================================================================================
class TestTriggers(unittest.TestCase):
    """Check-ins are event-driven only (ARCH #1)."""

    def setUp(self):
        import tempfile
        self.tmp = tempfile.TemporaryDirectory()
        self.cfg = _Config(Path(self.tmp.name))

    def tearDown(self):
        self.tmp.cleanup()

    def test_wake_events(self):
        for ev in (EVT_SLEEP_COMPLETE, EVT_QUEST_CLOSED, EVT_LEVEL_CANDIDACY,
                   EVT_SUSPENSION, EVT_OPERATOR_REQUEST):
            self.assertTrue(should_check_in(self.cfg, ev))
            self.assertTrue(should_check_in(self.cfg, {"kind": ev}))

    def test_non_events_do_not_wake(self):
        self.assertFalse(should_check_in(self.cfg, "tick"))
        self.assertFalse(should_check_in(self.cfg, {"kind": "timer"}))
        self.assertFalse(should_check_in(self.cfg, None))

    def test_no_timer_no_schedule_in_module(self):
        """ARCH #1 structurally: the module never sleeps, schedules, or polls."""
        src = Path(administrator.__file__).read_text(encoding="utf-8")
        self.assertNotIn("time.sleep", src)
        self.assertNotIn("import sched", src)
        self.assertNotIn("Timer(", src)
        self.assertNotIn("while True", src)


# =================================================================================================
class TestDossier(unittest.TestCase):
    def setUp(self):
        import tempfile
        self.tmp = tempfile.TemporaryDirectory()
        self.cfg = _Config(Path(self.tmp.name))

    def tearDown(self):
        self.tmp.cleanup()

    def test_dossier_surfaces_the_gap(self):
        """A tier with zero trusted skills is visible in the skill-economy section."""
        _gap_dossier_manifest(self.cfg)
        d = compile_dossier(self.cfg, persona=persona_mod._default_persona())
        econ = d["skill_economy"]
        self.assertEqual(econ["trusted_by_tier"].get("1"), 2)
        self.assertNotIn("2", econ["trusted_by_tier"])   # the gap
        self.assertEqual(econ["authored"], 3)

    def test_dossier_has_every_section_even_on_empty_stores(self):
        d = compile_dossier(self.cfg, persona=persona_mod._default_persona())
        for key in ("level", "quests", "calibration_by_domain", "error_slopes_by_domain",
                    "skill_economy", "condition", "pitfall_health", "notable_episodes",
                    "last_checkin"):
            self.assertIn(key, d)
        self.assertIn("can_level", d["level"])
        self.assertIn("suspension_count", d["pitfall_health"])

    def test_dossier_is_fresh_no_persistence(self):
        """Compiling a dossier writes nothing — nothing persists between check-ins but the marker."""
        compile_dossier(self.cfg, persona=persona_mod._default_persona())
        self.assertFalse((self.cfg.state_dir / administrator.STATE_NAME).exists())


# =================================================================================================
class TestFourthWall(unittest.TestCase):
    """The one-directional wall: the Administrator sees the project; the creature never sees it."""

    def setUp(self):
        import tempfile
        self.tmp = tempfile.TemporaryDirectory()
        self.cfg = _Config(Path(self.tmp.name))

    def tearDown(self):
        self.tmp.cleanup()

    def test_context_pack_breaks_the_wall_by_design(self):
        ctx = fourth_wall_context(self.cfg)
        # The plan's §7a (its own spec) and the dream-tests are in its context...
        self.assertIn("The Administrator", ctx)
        self.assertIn("Dream-tests", ctx)
        # ...and so is the capabilities map.
        self.assertIn("eidos_capabilities", ctx)

    def test_quests_module_never_imports_administrator(self):
        """One-directional by construction: the creature-facing render path (quests.py) has no
        reference to administrator.py at all."""
        src = Path(quests.__file__).read_text(encoding="utf-8")
        self.assertNotIn("import administrator", src)
        self.assertNotIn("from administrator", src)


# =================================================================================================
class TestGrammarAndParser(unittest.TestCase):
    def test_grammar_builds_and_names_the_schema(self):
        g = build_admin_grammar()
        self.assertIn("root ::=", g)
        for lit in ("quests", "weakness_report", "narrator", "tuning_flags",
                    "knob", "evidence", "criteria", "directive"):
            self.assertIn(lit, g)
        # The flag rule structurally has NO value slot — knobs are named, never set.
        flag_rule = next(ln for ln in g.splitlines() if ln.startswith("flag ::="))
        self.assertNotIn("value", flag_rule)

    def test_canned_output_parses(self):
        parsed = parse_admin_output(_canned_output())
        self.assertIsNotNone(parsed)
        self.assertEqual(len(parsed["quests"]), 1)
        self.assertEqual(parsed["quests"][0]["tier"], 2)
        self.assertEqual(parsed["tuning_flags"][0]["knob"], "TRUSTED_PER_TIER")

    def test_flags_name_knobs_never_values(self):
        base = json.loads(_canned_output())
        # A flag that smuggles a value field is malformed.
        base["tuning_flags"] = [{"knob": "BRIER_MAX", "evidence": "e", "value": 0.5}]
        self.assertIsNone(parse_admin_output(json.dumps(base)))
        # A knob that isn't a bare name (an assignment, a sentence) is malformed.
        base["tuning_flags"] = [{"knob": "BRIER_MAX = 0.5", "evidence": "e"}]
        self.assertIsNone(parse_admin_output(json.dumps(base)))
        base["tuning_flags"] = [{"knob": "set it to five", "evidence": "e"}]
        self.assertIsNone(parse_admin_output(json.dumps(base)))

    def test_malformed_shapes_rejected(self):
        self.assertIsNone(parse_admin_output("not json at all"))
        self.assertIsNone(parse_admin_output(json.dumps({"quests": []})))  # missing keys
        base = json.loads(_canned_output())
        base["quests"][0]["criteria"] = {"path": "", "op": ">=", "value": 1}   # un-checkable
        self.assertIsNone(parse_admin_output(json.dumps(base)))
        base = json.loads(_canned_output())
        base["quests"][0]["criteria"] = {"path": "x", "op": "~=", "value": 1}  # unknown op
        self.assertIsNone(parse_admin_output(json.dumps(base)))
        base = json.loads(_canned_output())
        base["quests"][0]["extra"] = "smuggled"                                # extra key
        self.assertIsNone(parse_admin_output(json.dumps(base)))
        base = json.loads(_canned_output())
        base["quests"] = base["quests"] * 5                                    # over the bound
        self.assertIsNone(parse_admin_output(json.dumps(base)))


# =================================================================================================
class TestFullCycle(unittest.TestCase):
    """THE GATE: a full cycle runs unattended except approvals."""

    def setUp(self):
        import tempfile
        self.tmp = tempfile.TemporaryDirectory()
        self.cfg = _Config(Path(self.tmp.name))
        _gap_dossier_manifest(self.cfg)
        self.persona = persona_mod._default_persona()

    def tearDown(self):
        self.tmp.cleanup()

    def test_full_cycle(self):
        llm = _mock_llm(_canned_output())

        # 1. Sleep completes → the Administrator wakes.
        self.assertTrue(should_check_in(self.cfg, EVT_SLEEP_COMPLETE))

        # 2. Check-in: dossier compiled (the LLM was shown the gap), proposal lands pending.
        report = check_in(self.cfg, llm, EVT_SLEEP_COMPLETE, persona=self.persona)
        self.assertFalse(report.dropped)
        self.assertEqual(report.pending_ids, ["adm_t2_first_trusted"])
        self.assertEqual(report.auto_issued_ids, [])
        # The dossier the mock saw contains the gap it targeted (tier 2: zero trusted).
        user_msg = llm.calls[0]["messages"][1]["content"]
        self.assertIn('"trusted_by_tier"', user_msg)
        self.assertNotIn('"2":', json.loads(user_msg.split("DOSSIER:\n", 1)[1])
                         ["skill_economy"]["trusted_by_tier"])
        # The call was grammar-constrained.
        self.assertIn("root ::=", llm.calls[0]["grammar"])

        # 3. Operator approves → the quest appears in the System's queue.
        quest = approve_proposal(self.cfg, "adm_t2_first_trusted")
        self.assertIsNotNone(quest)
        sysm = quests.System(
            self.cfg,
            reward_sink=lambda cfg, q: quests.default_reward_sink(cfg, q, self.persona))
        self.assertIn("adm_t2_first_trusted", [q.id for q in sysm.store.queue()])

        # 4. issue_next promotes it (cadence permitting)...
        active = sysm.issue_next(sleeps_since_close=1, condition="STABLE")
        self.assertIsNotNone(active)
        self.assertEqual(active.id, "adm_t2_first_trusted")

        # THE WALL: the creature-facing render contains NO Administrator internals.
        window = quests.render_active(active)
        self.assertIn("SYSTEM", window)
        self.assertIn(active.directive, window)
        for leaked in ("Administrator", "dossier", "DOSSIER", "PILLARS_PLAN", "fourth wall",
                       "weakness", "trusted_by_tier", "eidos_capabilities", "Dean",
                       report.weakness_report, report.narrator,
                       administrator.ADMIN_SYSTEM_PROMPT[:40]):
            self.assertNotIn(leaked, window)
        # ...and the reveal path leaks nothing either.
        self.assertNotIn("dossier", quests.render_reveal(active))

        # 5. Glue adjudication passes it → XP settles through the reward sink.
        xp_before = self.persona.get("xp", 0)
        stats = {"skills": {"tiers": {"2": {"trusted": 1}}}}   # the drill's typed evidence
        result = sysm.check(active, stats)
        self.assertTrue(result["passed"])
        self.assertEqual(self.persona["xp"], xp_before + 40)

        # 6. The NEXT check-in's dossier references the outcome (the marker state works).
        llm2 = _mock_llm(_canned_output(qid="adm_followup"))
        report2 = check_in(self.cfg, llm2, EVT_QUEST_CLOSED, persona=self.persona)
        self.assertFalse(report2.dropped)
        dossier2 = json.loads(llm2.calls[0]["messages"][1]["content"].split("DOSSIER:\n", 1)[1])
        self.assertEqual(dossier2["last_checkin"]["event"], EVT_SLEEP_COMPLETE)
        self.assertEqual(dossier2["last_checkin"]["outcomes"]["adm_t2_first_trusted"],
                         quests.PASSED)

    def test_malformed_output_dropped_with_log_never_committed(self):
        llm = _mock_llm('{"quests": "not a list"}')
        with self.assertLogs("eidos.administrator", level="WARNING") as cm:
            report = check_in(self.cfg, llm, EVT_SLEEP_COMPLETE, persona=self.persona)
        self.assertTrue(report.dropped)
        self.assertTrue(any("malformed" in m for m in cm.output))
        # Nothing committed: no pending proposals, no marker, empty quest queue.
        self.assertEqual(pending_proposals(self.cfg), [])
        self.assertEqual(AdminState(self.cfg).last_checkin, {})
        self.assertEqual(quests.QuestStore(self.cfg).queue(), [])

    def test_llm_failure_dropped_with_log(self):
        def broken(messages, grammar):
            raise RuntimeError("gpu on fire")
        with self.assertLogs("eidos.administrator", level="WARNING"):
            report = check_in(self.cfg, broken, EVT_SLEEP_COMPLETE, persona=self.persona)
        self.assertTrue(report.dropped)
        self.assertEqual(pending_proposals(self.cfg), [])


# =================================================================================================
class TestApprovalSeamsAndAutonomy(unittest.TestCase):
    def setUp(self):
        import tempfile
        self.tmp = tempfile.TemporaryDirectory()
        self.cfg = _Config(Path(self.tmp.name))
        _gap_dossier_manifest(self.cfg)
        self.persona = persona_mod._default_persona()

    def tearDown(self):
        self.tmp.cleanup()

    def _propose_one(self, qid: str, tier: int = 2):
        llm = _mock_llm(_canned_output(qid=qid, tier=tier))
        report = check_in(self.cfg, llm, EVT_OPERATOR_REQUEST, persona=self.persona)
        self.assertFalse(report.dropped)
        return report

    def test_reject_keeps_the_wall_shut(self):
        self._propose_one("adm_rejected")
        self.assertTrue(reject_proposal(self.cfg, "adm_rejected", reason="too easy"))
        self.assertEqual(quests.QuestStore(self.cfg).queue(), [])   # nothing crossed
        self.assertEqual(pending_proposals(self.cfg), [])

    def test_approve_with_edit(self):
        self._propose_one("adm_edited")
        q = approve_proposal(self.cfg, "adm_edited",
                             edit={"directive": "Harder. Two trusted skills at tier 2.",
                                   "reward_xp": 60})
        self.assertEqual(q.directive, "Harder. Two trusted skills at tier 2.")
        self.assertEqual(q.reward["amount"], 60)
        self.assertEqual([x.id for x in quests.QuestStore(self.cfg).queue()], ["adm_edited"])

    def test_edit_cannot_break_glue_checkability(self):
        self._propose_one("adm_badedit")
        q = approve_proposal(self.cfg, "adm_badedit",
                             edit={"criteria": {"path": "", "op": ">=", "value": 1}})
        self.assertIsNone(q)   # refused — criteria must stay checkable
        self.assertEqual(quests.QuestStore(self.cfg).queue(), [])

    def test_graduated_autonomy_earn_then_revoke(self):
        # Earn: the declared approval streak on tier 2.
        for i in range(AUTONOMY_MIN_SAMPLE):
            self._propose_one(f"adm_earn_{i}")
            self.assertIsNotNone(approve_proposal(self.cfg, f"adm_earn_{i}"))
        self.assertTrue(tier_has_autonomy(self.cfg, 2))

        # The NEXT tier-2 proposal auto-issues: straight into the System's queue, no approval.
        report = self._propose_one("adm_auto")
        self.assertEqual(report.auto_issued_ids, ["adm_auto"])
        self.assertEqual(report.pending_ids, [])
        self.assertIn("adm_auto", [q.id for q in quests.QuestStore(self.cfg).queue()])

        # An unearned tier still routes to pending (autonomy is PER-TIER).
        report3 = self._propose_one("adm_t3", tier=3)
        self.assertEqual(report3.pending_ids, ["adm_t3"])

        # The ban-hammer: revoke_autonomy stops auto-issue immediately.
        revoke_autonomy(self.cfg, 2)
        self.assertFalse(tier_has_autonomy(self.cfg, 2))
        report2 = self._propose_one("adm_after_revoke")
        self.assertEqual(report2.auto_issued_ids, [])
        self.assertEqual(report2.pending_ids, ["adm_after_revoke"])
        self.assertNotIn("adm_after_revoke",
                         [q.id for q in quests.QuestStore(self.cfg).queue()])

    def test_rejections_debit_the_rate(self):
        # 2 rejections + 4 approvals = 4/6 < 0.8 → no autonomy (the rate never crosses the bar
        # mid-sequence either: [0,0,1,1,1] at the min sample is 0.6).
        for i in range(2):
            self._propose_one(f"adm_r{i}")
            reject_proposal(self.cfg, f"adm_r{i}")
        for i in range(4):
            self._propose_one(f"adm_a{i}")
            approve_proposal(self.cfg, f"adm_a{i}")
        self.assertFalse(tier_has_autonomy(self.cfg, 2))


# =================================================================================================
class TestFlagOff(unittest.TestCase):
    """Flag off → every entrypoint is a no-op and nothing is written."""

    def setUp(self):
        import tempfile
        self.tmp = tempfile.TemporaryDirectory()
        self.cfg = _Config(Path(self.tmp.name), admin_on=False)

    def tearDown(self):
        self.tmp.cleanup()

    def _nothing_written(self):
        self.assertFalse((self.cfg.state_dir / administrator.STATE_NAME).exists())
        self.assertFalse((self.cfg.workspace / "quests.jsonl").exists())

    def test_all_entrypoints_noop(self):
        self.assertFalse(should_check_in(self.cfg, EVT_SLEEP_COMPLETE))
        self.assertEqual(compile_dossier(self.cfg), {})
        self.assertEqual(fourth_wall_context(self.cfg), "")
        llm = _mock_llm(_canned_output())
        self.assertIsNone(check_in(self.cfg, llm, EVT_SLEEP_COMPLETE))
        self.assertEqual(llm.calls, [])                       # the model was never even called
        self.assertEqual(pending_proposals(self.cfg), [])
        self.assertIsNone(approve_proposal(self.cfg, "x"))
        self.assertFalse(reject_proposal(self.cfg, "x"))
        self.assertFalse(tier_has_autonomy(self.cfg, 2))
        revoke_autonomy(self.cfg, 2)
        self._nothing_written()


# =================================================================================================
class TestLadderLint(unittest.TestCase):
    """TOOL_PROGRESSION §0 at the Administrator seam: a directive naming a tool outside the
    creature's world is held at intake (never auto-issued) and refused at approve — a locked door
    stays invisible no matter who writes the window. (Found live on the maiden walk: the first
    sleep_complete check-in proposed a quest naming net_scan at a newborn that had 8 organs.)"""

    NEWBORN = {"bash", "check_tools", "note_append", "note_close", "note_list", "note_read",
               "read_file", "write_file"}

    def setUp(self):
        import tempfile
        self.tmp = tempfile.TemporaryDirectory()
        self.cfg = _Config(Path(self.tmp.name))
        self.cfg.creature_mode = True
        self.cfg.pillars_tool_unlocks_enabled = True
        _gap_dossier_manifest(self.cfg)
        self.persona = persona_mod._default_persona()

    def tearDown(self):
        self.tmp.cleanup()

    @staticmethod
    def _output(qid, directive):
        return json.dumps({
            "quests": [{"id": qid, "directive": directive, "tier": 2, "reward_xp": 40,
                        "expiry_hours": 0,
                        "criteria": {"path": "skills.tiers.2.trusted", "op": ">=", "value": 1}}],
            "weakness_report": "w", "narrator": "n", "tuning_flags": [],
        })

    def _propose(self, qid, directive):
        return check_in(self.cfg, _mock_llm(self._output(qid, directive)), EVT_SLEEP_COMPLETE,
                        persona=self.persona)

    def test_locked_tool_mentions(self):
        hits = administrator.locked_tool_mentions(
            self.cfg, "Create one skill with create_skill that runs net_scan on the tailnet.")
        self.assertEqual(hits, ["create_skill", "net_scan"])
        # Prose collisions and newborn organs are never leaks.
        self.assertEqual(administrator.locked_tool_mentions(
            self.cfg, "See the manual, speak your mind, bash around, predict nothing."), [])
        # Ladder off → nothing is locked (byte-identical pre-cutover behavior).
        off = _Config(Path(self.tmp.name) / "off")
        self.assertEqual(administrator.locked_tool_mentions(
            off, "create_skill and net_scan galore"), [])

    def test_leaking_proposal_is_held_never_auto_issued(self):
        # Earn tier-2 autonomy the declared way first…
        for i in range(AUTONOMY_MIN_SAMPLE):
            self._propose(f"adm_clean_{i}", "Earn trust at the new tier. Author nothing.")
            self.assertIsNotNone(approve_proposal(self.cfg, f"adm_clean_{i}"))
        self.assertTrue(tier_has_autonomy(self.cfg, 2))
        # …then a leaking proposal in that tier still pends, tagged with WHY.
        report = self._propose("adm_leak", "Run net_scan across the subnet and log it.")
        self.assertEqual(report.auto_issued_ids, [])
        self.assertEqual(report.pending_ids, ["adm_leak"])
        record = AdminState(self.cfg).proposals["adm_leak"]
        self.assertEqual(record["locked_tool_mentions"], ["net_scan"])
        self.assertNotIn("adm_leak", [q.id for q in quests.QuestStore(self.cfg).queue()])

    def test_approve_refuses_until_the_unlock_lands(self):
        self._propose("adm_forge", "Forge one tool of your own with create_skill.")
        # Locked door: approval refuses; the proposal stays pending (edit or wait).
        self.assertIsNone(approve_proposal(self.cfg, "adm_forge"))
        self.assertEqual(AdminState(self.cfg).proposals["adm_forge"]["status"], "pending")
        self.assertEqual(quests.QuestStore(self.cfg).queue(), [])
        # The unlock lands → the same approval crosses the wall.
        import unlocks
        self.assertTrue(unlocks.grant(self.cfg, "skillcraft", "test"))
        q = approve_proposal(self.cfg, "adm_forge")
        self.assertIsNotNone(q)
        self.assertIn("adm_forge", [x.id for x in quests.QuestStore(self.cfg).queue()])

    def test_dossier_carries_creature_tools_only_when_ladder_active(self):
        d = compile_dossier(self.cfg, persona=self.persona)
        self.assertLessEqual(self.NEWBORN, set(d["creature_tools"]))
        self.assertNotIn("create_skill", d["creature_tools"])
        self.assertNotIn("net_scan", d["creature_tools"])
        off = _Config(Path(self.tmp.name) / "off")
        self.assertNotIn("creature_tools", compile_dossier(off, persona=self.persona))


if __name__ == "__main__":
    unittest.main()
