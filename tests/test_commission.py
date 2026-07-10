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
