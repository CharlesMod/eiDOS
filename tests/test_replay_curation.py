"""WISDOM_PLAN §2 (counterfactual replay) + §5 (utility-grounded curation) — offline unit tests.

Acceptance (WISDOM_PLAN §2/§5, invariants §W):
  - Replayable-episode selection: only a FAILURE with a LATER VERIFIED FIX (a differing succeeded
    signature at a higher tick, same situation key) is replayable (WIS1's documented rule).
  - Prompt reconstruction includes TODAY's real recall for the recorded situation (the point of
    replay: does current memory teach the move?).
  - Scoring is three-way (mock LLM emitting the fix / the original failure / something else) →
    learned / unlearned / divergent.
  - Settlement lands on the pinned stats keys (`replay_learned` / `replay_unlearned`) THROUGH the
    bet ledger's new replay channel, and strength moves in the right direction.
  - The history file is bounded; the report line is D3's number.
  - Replay SKIPS gracefully (typed reason, no hang) when the LLM is unreachable and when the
    metabolic reserve is low.
  - Curation decay math: grace counter, acceleration past the window, protection for positive
    utility, faster decay for un-earned inherited, demote-to-archive below the floor.
  - Flag-off is byte-identical for BOTH flags, INDEPENDENTLY (WIS7).

No services / tick loop / GPU / real model — temp workspaces, a fake llm callable, mock_mode for the
deterministic embedder. The library is driven directly.
"""

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

import pytest

from config import Config
import bets
import engram
import replay
from bets import BetLedger
from engram import Consolidator, Engram, EncodedAt, LongTermStore, STRENGTH_DEFAULT
from memory_manager import MemoryManager


# --- helpers -------------------------------------------------------------------------------------

def _cfg(tmp_path, *, replay_on=True, curation_on=True, bets_on=True) -> Config:
    cfg = Config()
    cfg.workspace_dir = str(tmp_path / "workspace")
    cfg.mock_mode = True                 # deterministic hash embedder (no ONNX model needed)
    cfg.wisdom_replay_enabled = replay_on
    cfg.wisdom_curation_enabled = curation_on
    cfg.pillars_bet_ledger_enabled = bets_on
    cfg.wisdom_replay_batch = 4
    cfg.wisdom_curation_grace_sleeps = 10
    (tmp_path / "workspace").mkdir(parents=True, exist_ok=True)
    return cfg


def _write_episode(cfg, *, tick, key, tool, sig="", fail_kind="", success=True, summary=""):
    rec = {"tick": tick, "key": key, "tool": tool, "sig": sig or tool,
           "fail_kind": fail_kind, "success": success, "summary": summary, "ts": 1.0}
    with open(cfg.workspace / "episodes.jsonl", "a", encoding="utf-8") as f:
        f.write(json.dumps(rec) + "\n")


def _seed_failure_with_fix(cfg, key="obj1|restart the wedged voice service"):
    """The canonical replayable situation: a failed action, then a LATER differing success (the fix)."""
    _write_episode(cfg, tick=10, key=key, tool="bash", sig="systemctl start voicesvc",
                   fail_kind="error", success=False, summary="unit masked")
    _write_episode(cfg, tick=12, key=key, tool="bash", sig="systemctl unmask restart voicesvc",
                   success=True, summary="came back")
    return key


class _FakeLLM:
    """A fake (messages, *, grammar=None) -> str. Returns a canned tool-call action string."""
    def __init__(self, action_text):
        self.action_text = action_text
        self.calls = 0

    def __call__(self, messages, *, grammar=None):
        self.calls += 1
        return self.action_text


def _action(cmd):
    return f'<tool>bash</tool><args>{{"cmd": "{cmd}"}}</args>'


# =================================================================================================
class TestReplayableSelection:
    def test_only_failure_with_later_verified_fix_is_replayable(self, tmp_path):
        cfg = _cfg(tmp_path)
        key = _seed_failure_with_fix(cfg)
        # Noise that must NOT be selected: a lone failure (no fix), a lone success, a success BEFORE
        # the failure (not a recovery), and a "fix" that is the SAME signature as the failure.
        _write_episode(cfg, tick=1, key="obj2|scan the subnet", tool="bash",
                       sig="nmap subnet", success=False)   # failure, no fix
        _write_episode(cfg, tick=2, key="obj3|water plants", tool="bash",
                       sig="water", success=True)          # lone success
        _write_episode(cfg, tick=3, key="obj4|charge",  tool="bash", sig="charge",
                       success=True)                       # success then failure = not a recovery
        _write_episode(cfg, tick=4, key="obj4|charge",  tool="bash", sig="charge",
                       success=False)
        _write_episode(cfg, tick=5, key="obj5|noop", tool="bash", sig="noop", success=False)
        _write_episode(cfg, tick=6, key="obj5|noop", tool="bash", sig="noop", success=True)  # same sig

        reps = replay.replayable_episodes(cfg)
        keys = {r["key"] for r in reps}
        assert keys == {key}
        r = reps[0]
        assert r["fail_sig"] and r["fix_sig"] and r["fix_sig"] != r["fail_sig"]
        assert r["fix_tick"] > r["fail_tick"]


# =================================================================================================
class TestPromptReconstruction:
    def test_prompt_includes_todays_recall(self, tmp_path):
        cfg = _cfg(tmp_path)
        key = _seed_failure_with_fix(cfg)
        mgr = MemoryManager(cfg)
        # A guardrail memory that recall should surface for this situation.
        taught = mgr.encode("strategy",
                            "When the voice service is wedged, `systemctl unmask restart voicesvc`.",
                            provenance="experienced")
        # Give the engram the situation stamp so the exact-match recall layer fires.
        _stamp_situation(cfg, taught.id, key)

        ep = replay.replayable_episodes(cfg)[0]
        messages, recalled_ids = replay.reconstruct_prompt(cfg, ep, mgr)
        joined = "\n".join(m["content"] for m in messages)
        assert "Before you act" in joined
        assert "voice service" in joined            # the recalled precedent is IN the prompt
        assert taught.id in recalled_ids


def _stamp_situation(cfg, eid, key):
    """Directly set stats['situation'] on a stored engram through the single writer (test helper)."""
    store = LongTermStore(cfg)
    entries = store.load()
    for e in entries:
        if e.id == eid:
            e.stats["situation"] = key
    engram._commit_to_store(store, entries)


# =================================================================================================
class TestScoring:
    def test_three_way_scoring(self, tmp_path):
        cfg = _cfg(tmp_path)
        _seed_failure_with_fix(cfg)
        ep = replay.replayable_episodes(cfg)[0]

        # matches the verified fix -> learned
        sig_fix = replay._replayed_action_signature(_action("systemctl unmask restart voicesvc"))
        assert replay.score_replay(sig_fix, ep) == "learned"
        # matches the original failure -> unlearned
        sig_fail = replay._replayed_action_signature(_action("systemctl start voicesvc"))
        assert replay.score_replay(sig_fail, ep) == "unlearned"
        # neither -> divergent
        sig_other = replay._replayed_action_signature(_action("reboot the whole machine now please"))
        assert replay.score_replay(sig_other, ep) == "divergent"
        # unparseable / empty -> divergent
        assert replay.score_replay("", ep) == "divergent"


# =================================================================================================
class TestSettlementThroughBetLedger:
    def _setup(self, tmp_path):
        cfg = _cfg(tmp_path)
        key = _seed_failure_with_fix(cfg)
        mgr = MemoryManager(cfg)
        taught = mgr.encode("strategy",
                            "When the voice service is wedged, `systemctl unmask restart voicesvc`.")
        _stamp_situation(cfg, taught.id, key)
        return cfg, mgr, taught

    def test_learned_credits_recalled_memory_and_pins_stat(self, tmp_path):
        cfg, mgr, taught = self._setup(tmp_path)
        llm = _FakeLLM(_action("systemctl unmask restart voicesvc"))   # reproduces the fix -> learned
        rep = replay.run_replay(cfg, manager=mgr, llm=llm, tick=5)
        assert rep["learned"] == 1 and rep["unlearned"] == 0
        after = LongTermStore(cfg).get(taught.id)
        assert int(after.stats.get("replay_learned", 0)) == 1     # the pinned cross-stream key
        assert float(after.strength) > STRENGTH_DEFAULT           # it gained strength via the ledger

    def test_unlearned_debits_and_pins_stat(self, tmp_path):
        cfg, mgr, taught = self._setup(tmp_path)
        llm = _FakeLLM(_action("systemctl start voicesvc"))        # reproduces the FAILURE -> unlearned
        rep = replay.run_replay(cfg, manager=mgr, llm=llm, tick=5)
        assert rep["unlearned"] == 1 and rep["learned"] == 0
        after = LongTermStore(cfg).get(taught.id)
        assert int(after.stats.get("replay_unlearned", 0)) == 1
        assert float(after.strength) < STRENGTH_DEFAULT           # it lost strength (taught the failure)

    def test_divergent_records_only_no_settlement(self, tmp_path):
        cfg, mgr, taught = self._setup(tmp_path)
        llm = _FakeLLM(_action("reboot the whole machine now please"))
        rep = replay.run_replay(cfg, manager=mgr, llm=llm, tick=5)
        assert rep["divergent"] == 1 and rep["learned"] == 0 and rep["unlearned"] == 0
        after = LongTermStore(cfg).get(taught.id)
        assert int(after.stats.get("replay_learned", 0)) == 0
        assert int(after.stats.get("replay_unlearned", 0)) == 0
        assert float(after.strength) == pytest.approx(taught.strength)   # untouched

    def test_settle_replay_inert_when_bet_ledger_dark(self, tmp_path):
        cfg = _cfg(tmp_path, bets_on=False)
        e = Consolidator(cfg).commit(Engram(kind="fact", body="the hallway router is behind the coats"))
        led = BetLedger(cfg)
        assert led.settle_replay(tick=1, engram_ids=[e.id], learned=True) == []
        # store byte-identical: strength unmoved, no stat pinned
        after = LongTermStore(cfg).get(e.id)
        assert float(after.strength) == pytest.approx(STRENGTH_DEFAULT)
        assert "replay_learned" not in after.stats


# =================================================================================================
class TestHistoryBounded:
    def test_history_appends_and_is_bounded(self, tmp_path):
        cfg = _cfg(tmp_path)
        path = cfg.state_dir / "replay_history.jsonl"
        for i in range(replay.REPLAY_HISTORY_MAX + 25):
            replay._append_history(cfg, {"ts": "t", "tick": i, "learned": 0,
                                         "unlearned": 0, "divergent": 0, "episode_ids": []})
        lines = [ln for ln in path.read_text().splitlines() if ln.strip()]
        assert len(lines) == replay.REPLAY_HISTORY_MAX
        # the newest survive (FIFO trim)
        assert json.loads(lines[-1])["tick"] == replay.REPLAY_HISTORY_MAX + 24

    def test_report_line_carries_d3_number(self, tmp_path):
        cfg, mgr, taught = TestSettlementThroughBetLedger()._setup(tmp_path)
        llm = _FakeLLM(_action("systemctl unmask restart voicesvc"))
        replay.run_replay(cfg, manager=mgr, llm=llm, tick=9)
        path = cfg.state_dir / "replay_history.jsonl"
        last = json.loads([ln for ln in path.read_text().splitlines() if ln.strip()][-1])
        assert last["learned"] == 1 and "episode_ids" in last and last["tick"] == 9


# =================================================================================================
class TestSkips:
    def test_no_llm_skips_gracefully(self, tmp_path):
        cfg = _cfg(tmp_path)
        _seed_failure_with_fix(cfg)
        rep = replay.run_replay(cfg, llm=None)
        assert rep == {"skipped": "no llm"}

    def test_low_reserve_skips(self, tmp_path):
        cfg = _cfg(tmp_path)
        _seed_failure_with_fix(cfg)
        cfg.state_dir.mkdir(parents=True, exist_ok=True)
        (cfg.state_dir / "metabolism.json").write_text(json.dumps({"energy": 0.05}))
        rep = replay.run_replay(cfg, llm=_FakeLLM(_action("anything at all here")))
        assert rep == {"skipped": "low reserve"}

    def test_full_reserve_does_not_skip(self, tmp_path):
        cfg = _cfg(tmp_path)
        _seed_failure_with_fix(cfg)
        cfg.state_dir.mkdir(parents=True, exist_ok=True)
        (cfg.state_dir / "metabolism.json").write_text(json.dumps({"energy": 0.9}))
        rep = replay.run_replay(cfg, llm=_FakeLLM(_action("systemctl unmask restart voicesvc")))
        assert "skipped" not in rep

    def test_no_replayable_material_skips(self, tmp_path):
        cfg = _cfg(tmp_path)
        rep = replay.run_replay(cfg, llm=_FakeLLM(_action("noop")))
        assert rep["skipped"] == "no replayable episodes"


# =================================================================================================
class TestCurationDecayMath:
    def _cons(self, cfg):
        return Consolidator(cfg)

    def test_positive_utility_protected(self, tmp_path):
        cfg = _cfg(tmp_path)
        cons = self._cons(cfg)
        e = cons.commit(Engram(kind="fact", body="proven-useful fact", strength=0.8,
                               stats={"replay_learned": 2}))
        rep = replay.curate(cfg, base_decay=0.03)
        after = LongTermStore(cfg).get(e.id)
        # protected decay = 0.03 * 0.25 = 0.0075
        assert after.strength == pytest.approx(0.8 - 0.03 * replay.CURATION_DECAY_PROTECT)
        assert int(after.stats.get(replay.GRACE_STAT, -1)) == 0
        assert rep["protected"] == 1

    def test_grace_counts_then_accelerates(self, tmp_path):
        cfg = _cfg(tmp_path)
        cfg.wisdom_curation_grace_sleeps = 2
        cons = self._cons(cfg)
        e = cons.commit(Engram(kind="fact", body="never-helps noise", strength=0.9))
        # sleeps 1 and 2: within grace -> ordinary base decay, grace increments
        replay.curate(cfg, base_decay=0.03)
        replay.curate(cfg, base_decay=0.03)
        mid = LongTermStore(cfg).get(e.id)
        assert int(mid.stats.get(replay.GRACE_STAT)) == 2
        assert mid.strength == pytest.approx(0.9 - 2 * 0.03)
        # sleep 3: grace exceeded -> accelerated decay
        rep = replay.curate(cfg, base_decay=0.03)
        after = LongTermStore(cfg).get(e.id)
        assert int(after.stats.get(replay.GRACE_STAT)) == 3
        assert after.strength == pytest.approx(0.9 - 2 * 0.03 - 0.03 * replay.CURATION_DECAY_ACCEL)
        assert rep["accelerated"] == 1

    def test_inherited_unearned_decays_faster(self, tmp_path):
        cfg = _cfg(tmp_path)
        cons = self._cons(cfg)
        inh = cons.commit(Engram(kind="fact", body="a letter from a previous self",
                                 provenance="inherited", strength=0.7))
        replay.curate(cfg, base_decay=0.03)
        after = LongTermStore(cfg).get(inh.id)
        # within grace but inherited + un-earned -> INHERITED_ACCEL, not base
        assert after.strength == pytest.approx(0.7 - 0.03 * replay.CURATION_INHERITED_ACCEL)

    def test_inherited_that_earns_is_protected(self, tmp_path):
        cfg = _cfg(tmp_path)
        cons = self._cons(cfg)
        inh = cons.commit(Engram(kind="fact", body="an inherited claim now verified",
                                 provenance="inherited", strength=0.7,
                                 stats={"replay_learned": 1}))
        replay.curate(cfg, base_decay=0.03)
        after = LongTermStore(cfg).get(inh.id)
        assert after.strength == pytest.approx(0.7 - 0.03 * replay.CURATION_DECAY_PROTECT)

    def test_demote_to_archive_below_floor_never_deletes(self, tmp_path):
        cfg = _cfg(tmp_path)
        cons = self._cons(cfg)
        spent = cons.commit(Engram(kind="fact", body="a nearly-spent trace", strength=0.06))
        rep = replay.curate(cfg, base_decay=0.03)   # 0.06 - 0.03 = 0.03 <= floor 0.05
        after = LongTermStore(cfg).get(spent.id)
        assert after is not None                      # NOT deleted (supersede-not-delete)
        assert after.stats.get(replay.ARCHIVE_STAT)   # demoted to archive
        assert rep["demoted"] == 1
        # a second curate leaves the archived engram alone (no double-decay)
        s0 = after.strength
        replay.curate(cfg, base_decay=0.03)
        assert LongTermStore(cfg).get(spent.id).strength == pytest.approx(s0)


# =================================================================================================
class TestFlagOffByteIdentical:
    def test_replay_flag_off_is_noop(self, tmp_path):
        cfg = _cfg(tmp_path, replay_on=False)
        _seed_failure_with_fix(cfg)
        rep = replay.run_replay(cfg, llm=_FakeLLM(_action("systemctl unmask restart voicesvc")))
        assert rep == {"skipped": "flag off"}
        # no history file written
        assert not (cfg.state_dir / "replay_history.jsonl").exists()

    def test_curation_flag_off_is_noop(self, tmp_path):
        cfg = _cfg(tmp_path, curation_on=False)
        cons = Consolidator(cfg)
        e = cons.commit(Engram(kind="fact", body="untouched", strength=0.8))
        rep = replay.curate(cfg)
        assert rep == {"skipped": "flag off"}
        assert LongTermStore(cfg).get(e.id).strength == pytest.approx(0.8)   # byte-identical

    def test_flags_are_independent(self, tmp_path):
        # curation ON, replay OFF: curation runs, replay is a no-op.
        cfg = _cfg(tmp_path, replay_on=False, curation_on=True)
        cons = Consolidator(cfg)
        e = cons.commit(Engram(kind="fact", body="decays under curation only", strength=0.8))
        assert replay.run_replay(cfg, llm=_FakeLLM(_action("x"))) == {"skipped": "flag off"}
        rep = replay.curate(cfg, base_decay=0.03)
        assert "skipped" not in rep
        assert LongTermStore(cfg).get(e.id).strength < 0.8

        # replay ON, curation OFF: replay runs, curation is a no-op.
        cfg2 = _cfg(tmp_path, replay_on=True, curation_on=False)
        cfg2.workspace_dir = str(tmp_path / "ws2")
        (tmp_path / "ws2").mkdir(parents=True, exist_ok=True)
        _seed_failure_with_fix(cfg2)
        mgr = MemoryManager(cfg2)
        taught = mgr.encode("strategy",
                            "When the voice service is wedged, `systemctl unmask restart voicesvc`.")
        _stamp_situation(cfg2, taught.id, "obj1|restart the wedged voice service")
        r = replay.run_replay(cfg2, manager=mgr,
                              llm=_FakeLLM(_action("systemctl unmask restart voicesvc")), tick=1)
        assert r["learned"] == 1
        assert replay.curate(cfg2) == {"skipped": "flag off"}
