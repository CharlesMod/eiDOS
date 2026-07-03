"""Pillars 2.3: the bet ledger (bets.py + glue.settle_bets) — offline unit tests.

Acceptance (PILLARS_TODO 2.3, decision #5):
  - An injected engram that precedes a successful adjudicated outcome gains strength; one that
    precedes a failure loses it (small shared-outcome credit/debit).
  - A provable recalled-fix follow (signature match) pays STRONG credit; the same outcome WITHOUT
    the match pays only the small shared credit.
  - LLM self-report cannot settle a bet: settle() rejects non-bool outcomes, and the glue hook
    exposes no parameter through which a narrated outcome could arrive.
  - error_patterns decay slower than facts; the inherited strength floor holds until contradicted
    by fresh experience (a signature-matched failure), then drops; clique-only co-scorers get
    their shared credit shrunk relative to an engram that also scores alone (pitfall #6).
  - Flag off → no bets logged, no strength mutated (long-term store byte-identical).

No services / tick loop / GPU — temp workspaces only; mock_mode for the deterministic embedder.
The ledger is driven directly and through the glue hook (its only production entry point).
"""

import inspect
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

import pytest

from config import Config
import bets
import glue
from bets import BetLedger
from engram import (Consolidator, EncodedAt, Engram, LongTermStore,
                    INHERITED_STRENGTH_FLOOR, STRENGTH_DEFAULT)


# --- helpers -------------------------------------------------------------------------------------

def _cfg(tmp_path, *, enabled: bool = True) -> Config:
    cfg = Config()
    cfg.workspace_dir = str(tmp_path / "workspace")
    cfg.mock_mode = True                 # deterministic hash embedder (no ONNX model needed)
    cfg.pillars_bet_ledger_enabled = enabled
    (tmp_path / "workspace").mkdir(parents=True, exist_ok=True)
    return cfg


def _commit(cfg, body, *, kind="fact", provenance="experienced", strength=None,
            encoded_at=None, stats=None) -> Engram:
    kw = {}
    if strength is not None:
        kw["strength"] = strength
    if encoded_at is not None:
        kw["encoded_at"] = encoded_at
    if stats is not None:
        kw["stats"] = stats
    return Consolidator(cfg).commit(Engram(kind=kind, body=body, provenance=provenance, **kw))


def _record(cfg, ok: bool, tool: str = "bash") -> None:
    glue.record_outcome(cfg, success=ok, fail_kind="" if ok else "error",
                        signature="" if ok else "sig-x", tool=tool)


def _strength(cfg, eid: str) -> float:
    e = LongTermStore(cfg).get(eid)
    assert e is not None
    return float(e.strength)


# =================================================================================================
class TestSharedOutcome:
    def test_success_gains_failure_loses(self, tmp_path):
        cfg = _cfg(tmp_path)
        winner = _commit(cfg, "the router lives in the hallway closet behind the coat rail")
        loser = _commit(cfg, "morning greetings should mention the weather forecast first")
        ledger = BetLedger(cfg)

        ledger.open_bets(1, [winner])
        _record(cfg, True)
        settled = glue.settle_bets(cfg, tick=1)
        assert len(settled) == 1 and settled[0]["credit"] == pytest.approx(bets.SHARED_CREDIT)
        assert _strength(cfg, winner.id) > STRENGTH_DEFAULT

        ledger.open_bets(2, [loser])
        _record(cfg, False)
        settled = glue.settle_bets(cfg, tick=2)
        assert len(settled) == 1 and settled[0]["credit"] == pytest.approx(-bets.SHARED_CREDIT)
        assert _strength(cfg, loser.id) < STRENGTH_DEFAULT

    def test_settlement_updates_recall_bookkeeping(self, tmp_path):
        cfg = _cfg(tmp_path)
        e = _commit(cfg, "sprinter boots the mind through llama swap on port eightyeightyone")
        ledger = BetLedger(cfg)
        ledger.open_bets(3, [e])
        assert ledger.settle(tick=3, success=True)
        after = LongTermStore(cfg).get(e.id)
        assert after.stats["credit_sum"] == pytest.approx(bets.SHARED_CREDIT)
        assert after.stats["last_recalled_tick"] == 3
        assert after.stats["recall_count"] == 1


# =================================================================================================
class TestStrongChannel:
    def test_signature_match_pays_strong_credit_others_small(self, tmp_path):
        cfg = _cfg(tmp_path)
        fix = _commit(cfg, "when the voice service wedges the cure is a clean restart",
                      kind="error", stats={"fix_sig": "systemctl restart voicesvc"})
        bystander = _commit(cfg, "dean prefers the porch lights amber after sunset")
        wrong_fix = _commit(cfg, "package troubles resolve by reinstalling",
                            kind="error", stats={"fix_sig": "pip install --force glados-tts"})
        ledger = BetLedger(cfg)
        ledger.open_bets(1, [fix, bystander, wrong_fix])
        _record(cfg, True)
        settled = glue.settle_bets(cfg, tick=1, action_tool="bash",
                                   action_args={"cmd": "systemctl restart voicesvc"})
        by_id = {s["eid"]: s for s in settled}
        assert by_id[fix.id]["matched"] is True
        assert by_id[fix.id]["credit"] == pytest.approx(bets.STRONG_CREDIT)
        # The SAME adjudicated outcome without the signature match pays only the shared coin.
        for other in (bystander, wrong_fix):
            assert by_id[other.id]["matched"] is False
            assert by_id[other.id]["credit"] == pytest.approx(bets.SHARED_CREDIT)
        assert _strength(cfg, fix.id) > _strength(cfg, bystander.id)

    def test_fix_signature_from_backticked_body(self, tmp_path):
        cfg = _cfg(tmp_path)
        e = _commit(cfg, "While reviving audio, `pactl unload-module module-suspend` succeeded.",
                    kind="error")
        assert bets.fix_signature_of(e)   # extracted from the backticked span
        assert bets.signature_match(
            bets.fix_signature_of(e),
            bets.action_signature("bash", {"cmd": "pactl unload-module module-suspend"}))

    def test_bare_tool_name_cannot_prove_a_follow(self, tmp_path):
        # A single generic token (SIG_MIN_TOKENS guard) matches everything → proves nothing.
        assert not bets.signature_match("bash", bets.action_signature("bash", {"cmd": "anything"}))


# =================================================================================================
class TestSelfReportCannotSettle:
    def test_narrated_outcome_raises(self, tmp_path):
        cfg = _cfg(tmp_path)
        ledger = BetLedger(cfg)
        with pytest.raises(TypeError):
            ledger.settle(tick=1, success="I applied the fix and it definitely worked")
        with pytest.raises(TypeError):
            ledger.settle(tick=1, success=1)   # even a truthy int is not the adjudicated bool

    def test_no_api_path_accepts_narration(self):
        # The glue hook exposes no parameter a narrated outcome could ride in on: the outcome is
        # read from glue's own adjudicated record, the signature from the executed tool call.
        params = set(inspect.signature(glue.settle_bets).parameters)
        assert params == {"config", "tick", "action_tool", "action_args", "ledger"}
        for banned in ("success", "outcome", "report", "claim", "narrative", "event_text"):
            assert banned not in params
        # And the ledger grows no side door for narrated settlement.
        for name in ("settle_self_report", "settle_from_text", "report_outcome", "narrate"):
            assert not hasattr(BetLedger, name)


# =================================================================================================
class TestStrengthDynamics:
    def test_error_patterns_decay_slower_than_facts(self, tmp_path):
        cfg = _cfg(tmp_path)
        fact = _commit(cfg, "the greenhouse sensor battery drains within roughly a fortnight",
                       kind="fact")
        scar = _commit(cfg, "flashing firmware over wifi bricks the thermostat radio",
                       kind="error")
        ledger = BetLedger(cfg)
        for tick, e in ((1, fact), (2, fact), (3, scar), (4, scar)):
            ledger.open_bets(tick, [e])
            ledger.settle(tick=tick, success=True)
        f, s = LongTermStore(cfg).get(fact.id), LongTermStore(cfg).get(scar.id)
        # Same two credits, but the error kind's earlier credit faded less.
        assert s.stats["credit_sum"] > f.stats["credit_sum"]
        assert s.strength > f.strength

    def test_inherited_floor_holds_until_contradicted_then_drops(self, tmp_path):
        cfg = _cfg(tmp_path)
        nugget = _commit(cfg, "the garage door remote pairs after holding the learn button",
                         provenance="inherited", strength=INHERITED_STRENGTH_FLOOR,
                         stats={"fix_sig": "hold the learn button"})
        ledger = BetLedger(cfg)
        # Shared-outcome failures alone do NOT breach the floor.
        ledger.open_bets(1, [nugget])
        ledger.settle(tick=1, success=False)
        assert _strength(cfg, nugget.id) == pytest.approx(INHERITED_STRENGTH_FLOOR)
        # A signature-matched FAILURE — the inherited fix was provably followed and provably
        # failed — is the contradiction by fresh experience: the floor drops (plan M-2).
        ledger.open_bets(2, [nugget])
        ledger.settle(tick=2, success=False,
                      action_sig=bets.action_signature("bash", {"cmd": "hold the learn button"}))
        assert _strength(cfg, nugget.id) < INHERITED_STRENGTH_FLOOR
        # Once contradicted, the floor stays gone.
        ledger.open_bets(3, [nugget])
        ledger.settle(tick=3, success=False)
        assert _strength(cfg, nugget.id) < INHERITED_STRENGTH_FLOOR

    def test_emotional_stamp_amplifies_earned_credit(self, tmp_path):
        cfg = _cfg(tmp_path)
        hot = _commit(cfg, "the basement flooded while the sump pump breaker sat tripped",
                      encoded_at=EncodedAt(tick=1, arousal=1.0, valence=-1.0))
        cool = _commit(cfg, "tuesday recycling collection alternates with garden waste")
        ledger = BetLedger(cfg)
        for tick, e in ((1, hot), (2, cool)):
            ledger.open_bets(tick, [e])
            ledger.settle(tick=tick, success=True)
        # Identical credit; the flashbulb stamp multiplies its effect on strength.
        assert _strength(cfg, hot.id) > _strength(cfg, cool.id)
        assert _strength(cfg, hot.id) == pytest.approx(
            STRENGTH_DEFAULT + bets.SHARED_CREDIT * (1.0 + bets.EMO_GAIN))


# =================================================================================================
class TestCliqueShrinkage:
    def test_clique_only_coscorers_get_shrunk(self, tmp_path):
        cfg = _cfg(tmp_path)
        a = _commit(cfg, "the driveway camera catches raccoons around midnight")
        b = _commit(cfg, "compost turning smells strongest before rainfall")
        c = _commit(cfg, "the workshop dehumidifier trips its outlet when saturated")
        ledger = BetLedger(cfg)
        # C earns INDIVIDUAL evidence once (sole bet on its tick).
        ledger.open_bets(1, [c])
        ledger.settle(tick=1, success=True)
        # A and B only ever score together — the same co-recalled clique, three times.
        for tick in (2, 3, 4):
            ledger.open_bets(tick, [a, b])
            ledger.settle(tick=tick, success=True)
        # Now all three co-score: the clique-only pair gets shrunk credit, C gets full.
        ledger.open_bets(5, [a, b, c])
        settled = {s["eid"]: s for s in ledger.settle(tick=5, success=True)}
        assert settled[a.id]["shrunk"] and settled[b.id]["shrunk"]
        assert settled[a.id]["credit"] == pytest.approx(bets.SHARED_CREDIT * bets.CLIQUE_SHRINK)
        assert not settled[c.id]["shrunk"]
        assert settled[c.id]["credit"] == pytest.approx(bets.SHARED_CREDIT)
        assert settled[a.id]["credit"] < settled[c.id]["credit"]

    def test_debits_are_never_shrunk(self, tmp_path):
        # Shielding a freeloader from losses would protect the ride (pitfall #6 damper is
        # one-sided): a clique-only pair still pays the FULL shared debit on failure.
        cfg = _cfg(tmp_path)
        a = _commit(cfg, "hallway motion sensor misfires when the heating vent opens")
        b = _commit(cfg, "the attic fan belt squeals in humid weather")
        ledger = BetLedger(cfg)
        for tick in (1, 2, 3):
            ledger.open_bets(tick, [a, b])
            ledger.settle(tick=tick, success=True)
        ledger.open_bets(4, [a, b])
        settled = ledger.settle(tick=4, success=False)
        for s in settled:
            assert s["credit"] == pytest.approx(-bets.SHARED_CREDIT)
            assert not s["shrunk"]


# =================================================================================================
class TestDarkFlag:
    def test_flag_off_no_bets_no_mutation(self, tmp_path):
        cfg = _cfg(tmp_path, enabled=False)
        e = _commit(cfg, "the porch swing bolt wants retightening every spring")
        store_path = cfg.knowledge_dir / "engram_longterm.jsonl"
        before = store_path.read_bytes()

        ledger = BetLedger(cfg)
        assert ledger.open_bets(1, [e]) == []
        _record(cfg, True)
        assert glue.settle_bets(cfg, tick=1) == []
        assert ledger.settle(tick=1, success=True) == []

        assert store_path.read_bytes() == before          # byte-identical long-term store
        assert not (cfg.state_dir / "bets.jsonl").exists()       # no bets logged
        assert not (cfg.state_dir / "bets_state.json").exists()  # no bookkeeping written


# =================================================================================================
class TestBounds:
    def test_open_bets_bounded_and_idempotent(self, tmp_path):
        cfg = _cfg(tmp_path)
        ledger = BetLedger(cfg)
        crowd = [Engram(kind="fact", body=f"distinct observation number {i} about the house")
                 for i in range(bets.MAX_OPEN_PER_TICK + 5)]
        opened = ledger.open_bets(1, crowd)
        assert len(opened) == bets.MAX_OPEN_PER_TICK      # per-tick cap (declared knob)
        assert ledger.open_bets(1, crowd[:3]) == []       # re-logging the same tick is a no-op

    def test_stale_open_bets_void_uncredited(self, tmp_path):
        cfg = _cfg(tmp_path)
        e = _commit(cfg, "the shed padlock combination rotates quarterly")
        ledger = BetLedger(cfg)
        ledger.open_bets(1, [e])
        # Far-future settlement: the tick-1 bet is stale — voided, never credited.
        assert ledger.settle(tick=1 + bets.STALE_BET_TICKS + 1, success=True) == []
        rows = ledger.all_bets()
        assert rows and rows[0]["status"] == "void"
        assert _strength(cfg, e.id) == pytest.approx(STRENGTH_DEFAULT)
