"""The Commission (commission.py) — offline unit tests.

Red-able gates (COMMISSION_PLAN.md):
  - the creature's done is a CLAIM: claim_done pays nothing and moves nothing but state;
  - settlement is ground truth only — a checkable claim measuring TRUE (glue), or an operator
    verdict file (confirm/reject/drop); reject REOPENS with the note (the feedback loop);
  - an ungradeable claim is unrepresentable: add() refuses a claim that doesn't parse;
  - bounded store (COMMISSION_MAX_LIVE), bounded render (RENDER_MAX_TASKS), capped brief;
  - verdicts are consume-and-delete, malformed dropped WITH a log, never silently;
  - the payout rides the Settlement (xp/feed) — the library never touches persona/metabolism.

No services / tick loop / GPU / model — temp workspaces only.
"""

import json
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

import pytest

import commission as cm
from commission import (
    Commission, Task, write_verdict, load_brief, brief_path, verdicts_dir,
    OPEN, DONE_CLAIMED, CONFIRMED, DROPPED,
    COMMISSION_MAX_LIVE, COMMISSION_XP_CONFIRMED, COMMISSION_FEED,
    BRIEF_MAX_CHARS, RENDER_MAX_TASKS,
)
from config import Config


def _cfg(tmp_path) -> Config:
    cfg = Config()
    cfg.workspace_dir = str(tmp_path / "workspace")
    cfg.mock_mode = False
    cfg.workspace.mkdir(parents=True, exist_ok=True)
    return cfg


# =================================================================================================
class TestTaskLifecycle:
    def test_add_and_claim_done_pays_nothing(self, tmp_path):
        c = Commission(_cfg(tmp_path))
        t = c.add("implement the scoring system", detail="per the brief §2")
        assert t.state == OPEN and t.id == 1
        t2 = c.claim_done(1, evidence="see game/score.py + the test run")
        assert t2.state == DONE_CLAIMED
        assert c.confirmed_total() == 0          # a claim settles nothing

    def test_claim_done_requires_an_open_task(self, tmp_path):
        c = Commission(_cfg(tmp_path))
        c.add("one")
        c.claim_done(1)
        with pytest.raises(ValueError):
            c.claim_done(1)                       # already claimed
        with pytest.raises(ValueError):
            c.claim_done(99)                      # no such task

    def test_ungradeable_claim_is_unrepresentable(self, tmp_path):
        c = Commission(_cfg(tmp_path))
        with pytest.raises(ValueError):
            c.add("vague promise", claim="make it feel better")
        # The three checkable shapes all pass the boundary.
        c.add("file claim", claim="exists:game/main.py")
        c.add("negative file claim", claim="not_exists:game/crash.log")
        c.add("stat claim", claim="skills.total >= 2")

    def test_live_bound_refuses_past_cap(self, tmp_path):
        c = Commission(_cfg(tmp_path))
        for i in range(COMMISSION_MAX_LIVE):
            c.add(f"task {i}")
        with pytest.raises(ValueError):
            c.add("one too many")

    def test_store_round_trips(self, tmp_path):
        cfg = _cfg(tmp_path)
        Commission(cfg).add("persisted", detail="d", claim="exists:x.txt")
        t = Commission(cfg).load()[0]
        assert (t.title, t.detail, t.claim, t.state) == ("persisted", "d", "exists:x.txt", OPEN)


# =================================================================================================
class TestClaimSettlement:
    def test_true_claim_confirms_and_carries_payout(self, tmp_path):
        cfg = _cfg(tmp_path)
        c = Commission(cfg)
        c.add("ship the file", claim="exists:proof.txt")
        assert c.settle_claims({}) == []          # not there yet — defers
        import tools as _tools
        root = _tools._creature_root(cfg)
        root.mkdir(parents=True, exist_ok=True)
        (root / "proof.txt").write_text("done")
        settled = c.settle_claims({})
        assert len(settled) == 1
        s = settled[0]
        assert s.how == "claim" and s.outcome == CONFIRMED
        assert s.xp == COMMISSION_XP_CONFIRMED and s.feed == COMMISSION_FEED
        assert c.load()[0].state == CONFIRMED
        assert c.settle_claims({}) == []          # settled once, never again

    def test_stat_claim_measures_against_stats(self, tmp_path):
        c = Commission(_cfg(tmp_path))
        c.add("earn trust", claim="skills.trusted >= 2")
        assert c.settle_claims({"skills": {"trusted": 1}}) == []
        assert c.settle_claims(None) == []        # unmeasurable defers, never defaults
        settled = c.settle_claims({"skills": {"trusted": 2}})
        assert len(settled) == 1 and settled[0].outcome == CONFIRMED

    def test_operator_judged_task_never_glue_settles(self, tmp_path):
        c = Commission(_cfg(tmp_path))
        c.add("make the game fun")                # no claim — operator's call
        c.claim_done(1)
        assert c.settle_claims({"anything": 1}) == []


# =================================================================================================
class TestRunsClaims:
    """`runs:<command>` — the strongest claim shape: EXECUTED at the done-claim event, exit 0
    confirms and pays, a failure REOPENS with the output tail as feedback (glue rejection, the
    tightest loop in the system). Never executed while the task merely sits open."""

    def test_never_runs_while_open_confirms_on_claim(self, tmp_path):
        cfg = _cfg(tmp_path)
        c = Commission(cfg)
        c.add("game boots", claim="runs:exit 0")
        assert c.settle_claims({}) == []                  # open → the command did NOT run
        c.claim_done(1, evidence="ran it myself")
        settled = c.settle_claims({})
        assert len(settled) == 1
        s = settled[0]
        assert s.how == "claim" and s.outcome == CONFIRMED
        assert s.xp == COMMISSION_XP_CONFIRMED and s.feed == COMMISSION_FEED
        assert "exit 0" in c.load()[0].verdict_note
        assert c.settle_claims({}) == []                  # settled once, never again

    def test_failing_run_reopens_with_the_error_as_feedback(self, tmp_path):
        cfg = _cfg(tmp_path)
        c = Commission(cfg)
        c.add("tests pass", claim="runs:echo boom-trace >&2; exit 3")
        c.claim_done(1)
        settled = c.settle_claims({})
        assert len(settled) == 1
        s = settled[0]
        assert s.outcome == "rejected" and s.xp == 0      # glue rejection pays nothing
        t = c.load()[0]
        assert t.state == OPEN                            # back on the queue
        assert "boom-trace" in t.verdict_note             # the error IS the feedback
        assert c.settle_claims({}) == []                  # reopened → no re-run until re-claimed

    def test_runs_claim_passes_the_add_boundary(self, tmp_path):
        c = Commission(_cfg(tmp_path))
        c.add("ok", claim="runs:python game/test.py")
        with pytest.raises(ValueError):
            c.add("empty command", claim="runs:   ")
        with pytest.raises(ValueError):
            c.add("a script, not a command", claim="runs:" + "x " * 200)

    def test_timeout_is_a_failure_with_the_reason(self, tmp_path, monkeypatch):
        monkeypatch.setattr(cm, "RUNS_CLAIM_TIMEOUT_S", 0.2)
        c = Commission(_cfg(tmp_path))
        c.add("hangs", claim="runs:sleep 5")
        c.claim_done(1)
        settled = c.settle_claims({})
        assert settled[0].outcome == "rejected"
        assert "timed out" in c.load()[0].verdict_note


# =================================================================================================
class TestOperatorVerdicts:
    def test_confirm_pays_and_closes(self, tmp_path):
        cfg = _cfg(tmp_path)
        c = Commission(cfg)
        c.add("level select screen")
        c.claim_done(1, evidence="menu.py")
        write_verdict(cfg, task_id=1, verdict="confirm", note="works great")
        settled = c.consume_verdicts()
        assert len(settled) == 1
        s = settled[0]
        assert (s.how, s.outcome, s.xp, s.feed) == (
            "operator", CONFIRMED, COMMISSION_XP_CONFIRMED, COMMISSION_FEED)
        assert c.load()[0].verdict_note == "works great"
        assert list(verdicts_dir(cfg).glob("*.json")) == []   # consume-and-delete

    def test_reject_reopens_with_the_feedback(self, tmp_path):
        cfg = _cfg(tmp_path)
        c = Commission(cfg)
        c.add("enemy ai")
        c.claim_done(1)
        write_verdict(cfg, task_id=1, verdict="reject", note="enemies walk through walls")
        settled = c.consume_verdicts()
        assert settled[0].outcome == "rejected" and settled[0].xp == 0
        t = c.load()[0]
        assert t.state == OPEN                                # back on the queue
        assert t.verdict_note == "enemies walk through walls"  # the coworker feedback

    def test_drop_closes_unpaid(self, tmp_path):
        cfg = _cfg(tmp_path)
        c = Commission(cfg)
        c.add("cut feature")
        write_verdict(cfg, task_id=1, verdict="drop")
        settled = c.consume_verdicts()
        assert settled[0].outcome == DROPPED and settled[0].xp == 0
        assert c.load()[0].state == DROPPED

    def test_malformed_and_unknown_are_dropped_with_log_not_silence(self, tmp_path, caplog):
        cfg = _cfg(tmp_path)
        c = Commission(cfg)
        c.add("real task")
        vdir = verdicts_dir(cfg)
        vdir.mkdir(parents=True, exist_ok=True)
        (vdir / "a_bad.json").write_text("{not json")
        (vdir / "b_unknown.json").write_text(json.dumps(
            {"task_id": 99, "verdict": "confirm"}))
        import logging
        with caplog.at_level(logging.WARNING, logger="eidos.commission"):
            assert c.consume_verdicts() == []
        assert len(caplog.records) == 2                       # both logged, neither silent
        assert list(vdir.glob("*.json")) == []                # both consumed
        assert c.load()[0].state == OPEN                      # the real task untouched

    def test_write_verdict_refuses_unknown_word(self, tmp_path):
        with pytest.raises(ValueError):
            write_verdict(_cfg(tmp_path), task_id=1, verdict="maybe")

    def test_verdict_on_settled_task_is_ignored(self, tmp_path):
        cfg = _cfg(tmp_path)
        c = Commission(cfg)
        c.add("t")
        write_verdict(cfg, task_id=1, verdict="drop")
        c.consume_verdicts()
        write_verdict(cfg, task_id=1, verdict="confirm")      # too late — no double settle
        assert c.consume_verdicts() == []
        assert c.load()[0].state == DROPPED


# =================================================================================================
class TestDashboardChannel:
    """The operator's side: `/commission done|reject|drop <id> [note]` typed into the normal chat
    box routes to a verdict file; everything else flows to the creature untouched."""

    def _cfg(self, tmp_path):
        cfg = _cfg(tmp_path)
        cfg.pillars_commission_enabled = True
        return cfg

    def test_plain_chat_is_not_intercepted(self, tmp_path):
        import dashboard
        assert dashboard._commission_chat_command(self._cfg(tmp_path),
                                                  "hey buddy, the game is fun") is None

    def test_done_routes_a_confirm_verdict(self, tmp_path):
        import dashboard
        cfg = self._cfg(tmp_path)
        r = dashboard._commission_chat_command(cfg, "/commission done 3 nice work on the menu")
        assert r["ok"] and r["routed"] == "commission"
        files = list(verdicts_dir(cfg).glob("*.json"))
        assert len(files) == 1
        v = json.loads(files[0].read_text(encoding="utf-8"))
        assert v == {"task_id": 3, "verdict": "confirm",
                     "note": "nice work on the menu", "ts": v["ts"]}

    def test_mission_alias_and_hash_id(self, tmp_path):
        import dashboard
        cfg = self._cfg(tmp_path)
        r = dashboard._commission_chat_command(cfg, "/mission reject #2 enemies clip walls")
        assert r["ok"]
        v = json.loads(next(verdicts_dir(cfg).glob("*.json")).read_text(encoding="utf-8"))
        assert (v["task_id"], v["verdict"], v["note"]) == (2, "reject", "enemies clip walls")

    def test_bad_usage_errors_at_the_operator(self, tmp_path):
        import dashboard
        r = dashboard._commission_chat_command(self._cfg(tmp_path), "/commission frobnicate 3")
        assert not r["ok"] and "usage" in r["error"]

    def test_flag_off_refuses(self, tmp_path):
        import dashboard
        cfg = _cfg(tmp_path)
        cfg.pillars_commission_enabled = False
        r = dashboard._commission_chat_command(cfg, "/commission done 1")
        assert not r["ok"]

    def test_status_payload_shape(self, tmp_path):
        import dashboard
        cfg = self._cfg(tmp_path)
        c = Commission(cfg)
        c.add("a")
        c.add("b")
        c.claim_done(2, evidence="look at b.py")
        s = dashboard._commission_status(cfg)
        assert s["open"] == 1 and s["confirmed_total"] == 0 and s["brief"] is False
        assert s["awaiting"] == [{"id": 2, "title": "b", "evidence": "look at b.py"}]


class TestUnlockLadder:
    """U7: the commission unit is a MILESTONE grant — the whole genesis line closed plus real
    digestion — and its verbs exist only under the flag (register_commission_tools)."""

    def test_milestone_grants_at_maturity(self, tmp_path):
        import unlocks
        cfg = _cfg(tmp_path)
        ripe = {"quests": {"passed": unlocks.COMMISSION_QUESTS_REQUIRED},
                "sleeps": {"total": unlocks.COMMISSION_SLEEPS_REQUIRED}}
        green = {"quests": {"passed": unlocks.COMMISSION_QUESTS_REQUIRED - 1},
                 "sleeps": {"total": unlocks.COMMISSION_SLEEPS_REQUIRED}}
        u = unlocks.unit("commission")
        assert u is not None and u.criterion.check(ripe)
        assert not u.criterion.check(green)

    def test_verbs_register_only_under_the_flag(self, tmp_path):
        import tools as _tools
        cfg = _cfg(tmp_path)
        try:
            cfg.pillars_commission_enabled = True
            assert _tools.register_commission_tools(cfg)
            assert "commission_add" in _tools.TOOLS and "commission_done" in _tools.TOOLS
            cfg.pillars_commission_enabled = False
            assert not _tools.register_commission_tools(cfg)
            assert "commission_add" not in _tools.TOOLS
        finally:
            _tools.TOOLS.pop("commission_add", None)
            _tools.TOOLS.pop("commission_done", None)


class TestCouplings:
    """The commission is FELT, not just visible: it presses the goal-tension drive, the System
    reads it in the dossier, the vocabulary can adjudicate it, and the delegated builder is handed
    the standing order behind its narrow task."""

    def test_open_commission_presses_goal_tension(self):
        from nervous.goaltension import GoalTensionDrive, COMMISSION_PRESS
        d = GoalTensionDrive(decay=0.0)      # decay 0: level == this tick's target (pure signal)
        base = d.observe(made_progress=False, open_objective=False)
        assert base == 0.0                    # nothing open → no tension (unchanged)
        pressed = d.observe(made_progress=False, open_objective=False, open_commission=True)
        assert pressed == COMMISSION_PRESS    # the standing order's steady itch
        # Progress still discharges — relief beats the itch (the loop, not a ratchet).
        assert d.observe(made_progress=True, open_objective=False, open_commission=True) == 0.0
        # A fully-frustrated objective still presses harder than the commission alone.
        frustrated = d.observe(made_progress=False, open_objective=True,
                               frustration_frac=1.0, open_commission=True)
        assert frustrated == 1.0

    def test_vocabulary_adjudicates_commission_stats(self, tmp_path):
        import quests
        cfg = _cfg(tmp_path)
        c = Commission(cfg)
        c.add("judged by charlie")
        c.add("measured", claim="exists:x.txt")
        write_verdict(cfg, task_id=1, verdict="confirm")
        c.consume_verdicts()
        stats = {"commission": {"confirmed_total": c.confirmed_total(),
                                "open": len([t for t in c.live() if t.state == OPEN])}}
        assert "commission.confirmed_total" in quests.ADJUDICATABLE_PATHS
        crit = quests.Criterion(path="commission.confirmed_total", op=">=", value=1)
        assert crit.check(stats)
        assert quests.Criterion(path="commission.open", op="<=", value=0).check(stats) is False

    def test_dossier_carries_the_commission(self, tmp_path):
        import administrator
        cfg = _cfg(tmp_path)
        cfg.pillars_commission_enabled = True
        brief_path(cfg).parent.mkdir(parents=True, exist_ok=True)
        brief_path(cfg).write_text("Build a small terminal game.", encoding="utf-8")
        c = Commission(cfg)
        c.add("first system")
        c.claim_done(1, evidence="see it")
        c.add("second system")
        s = administrator._commission_section(cfg)
        assert s["brief_present"] and "terminal game" in s["brief_head"]
        assert s["open"] == 1 and s["awaiting_verdict"] == 1 and s["confirmed_total"] == 0
        assert s["open_tasks"][0]["title"] == "second system"
        cfg.pillars_commission_enabled = False
        assert administrator._commission_section(cfg) is None   # dark → key omitted

    def test_hot_task_prefers_feedback_then_recency(self, tmp_path):
        c = Commission(_cfg(tmp_path))
        assert c.hot_task() is None
        c.add("first")
        c.add("second")                                    # newest open → hot
        assert c.hot_task().title == "second"
        c.claim_done(1)
        write_verdict(c.config, task_id=1, verdict="reject", note="redo the input loop")
        c.consume_verdicts()                               # #1 reopens with feedback → outranks all
        assert c.hot_task().id == 1

    def test_focus_terms_carry_the_hot_task(self, tmp_path):
        import eidos
        cfg = _cfg(tmp_path)
        cfg.pillars_commission_enabled = True
        hub = eidos._Pillars(cfg)
        try:
            Commission(cfg).add("terminal renderer", detail="draw the maze with box glyphs")
            terms = hub._focus_terms()
            assert "terminal" in terms and "renderer" in terms
        finally:
            import tools as _tools
            for name in ("commission_add", "commission_done", "weigh_options"):
                _tools.TOOLS.pop(name, None)

    def test_weigh_options_consults_three_lenses(self, tmp_path, monkeypatch):
        import tools as _tools
        import llm as _llm
        cfg = _cfg(tmp_path)
        seen = []

        def _fake_complete(messages, config, temperature=None, max_tokens=None, **kw):
            seen.append(messages[0]["content"])
            return f"approach {len(seen)}"

        monkeypatch.setattr(_llm, "complete", _fake_complete)
        r = _tools.tool_weigh_options({"question": "how should the game loop tick?"}, cfg)
        assert r.success
        assert len(seen) == 3                              # three separate advisors
        assert len({s for s in seen}) == 3                 # each argued a DIFFERENT lens
        for tag in ("[simplest]", "[robust]", "[frugal]"):
            assert tag in r.output
        assert "WHY" in r.output                           # the choice is pushed back to the creature
        # And it refuses without a question / without a mind.
        assert not _tools.tool_weigh_options({}, cfg).success
        cfg.mock_mode = True
        assert not _tools.tool_weigh_options({"question": "x"}, cfg).success

    def test_delegate_house_rules_carry_the_brief(self, tmp_path):
        import delegate
        cfg = _cfg(tmp_path)
        cfg.creature_mode = True
        cfg.pillars_commission_enabled = True
        brief_path(cfg).parent.mkdir(parents=True, exist_ok=True)
        brief_path(cfg).write_text("Build a small terminal game per spec.", encoding="utf-8")
        rules = delegate._write_house_rules(cfg).read_text(encoding="utf-8")
        assert "standing order" in rules and "terminal game" in rules
        cfg.pillars_commission_enabled = False
        rules_off = delegate._write_house_rules(cfg).read_text(encoding="utf-8")
        assert "terminal game" not in rules_off                 # dark → not a byte changes


class TestBriefAndRender:
    def test_brief_is_capped_and_optional(self, tmp_path):
        cfg = _cfg(tmp_path)
        assert load_brief(cfg) == ""
        brief_path(cfg).parent.mkdir(parents=True, exist_ok=True)
        brief_path(cfg).write_text("x" * (BRIEF_MAX_CHARS + 500), encoding="utf-8")
        assert len(load_brief(cfg)) == BRIEF_MAX_CHARS

    def test_render_block_empty_without_a_commission(self, tmp_path):
        assert Commission(_cfg(tmp_path)).render_block() == ""

    def test_render_block_bounded_and_carries_feedback(self, tmp_path):
        cfg = _cfg(tmp_path)
        brief_path(cfg).parent.mkdir(parents=True, exist_ok=True)
        brief_path(cfg).write_text("Build a small game per spec.", encoding="utf-8")
        c = Commission(cfg)
        for i in range(RENDER_MAX_TASKS + 3):
            c.add(f"task number {i}")
        c.claim_done(1)
        write_verdict(cfg, task_id=2, verdict="reject", note="needs sound")
        c.consume_verdicts()
        block = c.render_block()
        assert "COMMISSION" in block and "Build a small game" in block
        assert "…awaiting confirmation" in block              # the claimed task
        assert "[feedback: needs sound]" in block             # rejection surfaces in-place
        assert f"(+{3} more)" in block                        # render bound holds

    def test_rejected_build_points_back_at_its_workshop_job(self, tmp_path):
        cfg = _cfg(tmp_path)
        c = Commission(cfg)
        c.add("the maze level")
        c.claim_done(1, evidence="workshop built it", job="maze")
        assert c.load()[0].job == "maze"                      # round-trips
        write_verdict(cfg, task_id=1, verdict="reject", note="walls flicker")
        c.consume_verdicts()
        block = c.render_block()
        assert "[feedback: walls flicker]" in block
        assert "'maze' workshop job holds this build" in block   # the revision hook

    def test_exemplar_line_shows_the_last_confirmed_work(self, tmp_path):
        cfg = _cfg(tmp_path)
        c = Commission(cfg)
        c.add("older win")
        write_verdict(cfg, task_id=1, verdict="confirm", note="fine")
        c.consume_verdicts()
        c.add("newest win")
        c.claim_done(2, evidence="run log attached, score test green")
        write_verdict(cfg, task_id=2, verdict="confirm", note="exactly right")
        c.consume_verdicts()
        c.add("still open")                                   # so the block renders a todo too
        block = c.render_block()
        assert "THE BAR" in block
        assert "newest win" in block and "run log attached" in block
        assert "exactly right" in block
