"""Pillars 4.1: the expectation ledger (expectations.py + the `predict` tool + glue settlement) —
offline unit tests.

Acceptance (PILLARS_TODO 4.1 / the phase brief):
  - `predict` creates a kind='prediction' engram; the tool is DARK (unregistered + blocked) when
    `pillars_expectations_enabled` is off;
  - the open-prediction count is BOUNDED by `pillars_max_open_predictions`: the (N+1)th bet is
    refused, or (evict_oldest) retires the oldest open bet;
  - GLUE closes on deadline and on matching event; a confident-wrong bet scores HIGHER surprise
    than an unconfident-wrong one; closure births an episode engram (the §M-4 residue) and feeds
    surprise into the reward-RPE + curiosity hooks;
  - LLM self-report NEVER closes a prediction (reason whitelist + no closing tool surface);
  - `brier_calibration_by_domain()` returns sane per-domain scores on a synthetic set.

No services / tick loop / GPU — temp workspaces only.
"""

import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

import pytest

from config import Config
import expectations
import glue
import tools
from expectations import (
    ExpectationLedger, Prediction, Closure, CLOSE_REASONS,
    close_prediction, close_due_predictions, close_event_predictions,
    surprise_of, brier_calibration_by_domain, SURPRISE_MAX, DEFAULT_CONFIDENCE,
)


# --- helpers -------------------------------------------------------------------------------------

def _cfg(tmp_path, *, enabled: bool = True, max_open: int = 3) -> Config:
    cfg = Config()
    cfg.workspace_dir = str(tmp_path / "workspace")
    cfg.pillars_expectations_enabled = enabled
    cfg.pillars_max_open_predictions = max_open
    return cfg


def _future(seconds: float = 3600.0) -> float:
    return time.time() + seconds


@pytest.fixture(autouse=True)
def _predict_tool_stays_dark():
    """tools.TOOLS is module-global: whatever a test registers, deregister after — the `predict`
    tool must not leak into other test modules' view of the registry."""
    yield
    tools.TOOLS.pop("predict", None)
    tools._TOOL_ARG_MODELS.pop("predict", None)


# =================================================================================================
class TestPredictCreatesEngram:
    def test_ledger_predict_writes_prediction_engram(self, tmp_path):
        cfg = _cfg(tmp_path)
        led = ExpectationLedger(cfg)
        p = led.predict(statement="the backup finishes tonight",
                        target="backup file exists on the nas",
                        deadline=_future(), confidence=0.8, domain="ops")
        ring = led.ring.load()
        assert len(ring) == 1
        assert ring[0].kind == "prediction"
        back = Prediction.from_engram(ring[0])
        assert back is not None
        assert back.statement == "the backup finishes tonight"
        assert back.target == "backup file exists on the nas"
        assert back.confidence == pytest.approx(0.8)
        assert back.domain == "ops"
        assert back.status == "open"
        assert back.id == p.id == ring[0].id

    def test_tool_predict_creates_prediction_engram(self, tmp_path):
        cfg = _cfg(tmp_path)
        res = tools.tool_predict({"statement": "dean gets home before dinner",
                                  "target": "front door opens", "deadline": "in 2h",
                                  "confidence": 0.7, "domain": "house"}, cfg)
        assert res.success, res.output
        led = ExpectationLedger(cfg)
        opens = led.open_predictions()
        assert len(opens) == 1
        assert opens[0].domain == "house"
        # "in 2h" parsed to a FUTURE epoch (~7200s out)
        assert opens[0].deadline == pytest.approx(time.time() + 7200, abs=60)

    def test_tool_predict_blocked_when_flag_off(self, tmp_path):
        cfg = _cfg(tmp_path, enabled=False)
        res = tools.tool_predict({"statement": "anything"}, cfg)
        assert not res.success
        assert res.fail_kind == "blocked"
        assert not ExpectationLedger(cfg).ring.load()   # nothing written while dark

    def test_registration_is_flag_gated(self, tmp_path):
        # Flag off → predict absent from the registry (never enters the tick grammar).
        assert not tools.register_predict_tool(_cfg(tmp_path, enabled=False))
        assert "predict" not in tools.TOOLS
        # Flag on → registered, with its arg model.
        assert tools.register_predict_tool(_cfg(tmp_path, enabled=True))
        assert tools.TOOLS["predict"] is tools.tool_predict
        assert tools._TOOL_ARG_MODELS["predict"] is tools.PredictArgs
        # Flip back off → deregistered (idempotent both ways).
        assert not tools.register_predict_tool(_cfg(tmp_path, enabled=False))
        assert "predict" not in tools.TOOLS


# =================================================================================================
class TestBoundedLedger:
    def test_n_plus_one_is_refused(self, tmp_path):
        cfg = _cfg(tmp_path, max_open=3)
        led = ExpectationLedger(cfg)
        for i in range(3):
            led.predict(statement=f"bet number {i}", target=f"target {i}", deadline=_future())
        with pytest.raises(ValueError, match="full"):
            led.predict(statement="one bet too many", target="t", deadline=_future())
        assert len(led.open_predictions()) == 3

    def test_tool_refusal_is_typed_not_a_crash(self, tmp_path):
        cfg = _cfg(tmp_path, max_open=1)
        assert tools.tool_predict({"statement": "first bet"}, cfg).success
        res = tools.tool_predict({"statement": "second bet"}, cfg)
        assert not res.success
        assert res.fail_kind == "blocked"
        assert "refused" in res.output.lower()

    def test_evict_oldest_makes_room(self, tmp_path):
        cfg = _cfg(tmp_path, max_open=2)
        led = ExpectationLedger(cfg)
        oldest = led.predict(statement="the oldest open bet", target="a", deadline=_future())
        led.predict(statement="the middle bet", target="b", deadline=_future())
        led.predict(statement="the newest bet", target="c", deadline=_future(),
                    evict_oldest=True)
        opens = led.open_predictions()
        assert len(opens) == 2                                # still at the bound
        assert oldest.id not in {p.id for p in opens}          # the oldest was retired
        assert {p.statement for p in opens} == {"the middle bet", "the newest bet"}


# =================================================================================================
class TestGlueClosure:
    def test_glue_closes_on_deadline(self, tmp_path):
        cfg = _cfg(tmp_path)
        led = ExpectationLedger(cfg)
        led.predict(statement="the scan finishes fast", target="scan report present",
                    deadline=time.time() - 5, confidence=0.9)   # already overdue
        closures = glue.settle_predictions(cfg, tick=7)
        assert len(closures) == 1
        c = closures[0]
        assert c.reason == "deadline"
        assert c.outcome is False                               # deadline passed unmet → wrong
        assert not ExpectationLedger(cfg).open_predictions()    # settled: no longer open

    def test_glue_closes_on_matching_event(self, tmp_path):
        cfg = _cfg(tmp_path)
        led = ExpectationLedger(cfg)
        led.predict(statement="the backup completes", target="backup file exists on the nas",
                    deadline=_future(), confidence=0.6)
        closures = glue.settle_predictions(
            cfg, event_text="observed: backup file exists on the nas (4.2 GB)", tick=3)
        assert len(closures) == 1
        assert closures[0].reason == "event"
        assert closures[0].outcome is True
        assert not ExpectationLedger(cfg).open_predictions()

    def test_unrelated_event_closes_nothing(self, tmp_path):
        cfg = _cfg(tmp_path)
        ExpectationLedger(cfg).predict(statement="the backup completes",
                                       target="backup file exists on the nas",
                                       deadline=_future())
        assert glue.settle_predictions(cfg, event_text="mqtt broker restarted cleanly") == []
        assert len(ExpectationLedger(cfg).open_predictions()) == 1

    def test_confident_wrong_beats_unconfident_wrong(self, tmp_path):
        # The pure ordering...
        assert surprise_of(0.95, False) > surprise_of(0.2, False)
        # ...and a confident-RIGHT close is quietly confirming (low surprise).
        assert surprise_of(0.95, True) < surprise_of(0.95, False)
        assert 0.0 <= surprise_of(1.0, False) <= SURPRISE_MAX
        # ...and through the full glue path: two overdue bets, one held confidently, one barely.
        cfg = _cfg(tmp_path)
        led = ExpectationLedger(cfg)
        led.predict(statement="a bold confident bet", target="alpha marker",
                    deadline=time.time() - 5, confidence=0.95)
        led.predict(statement="a hedged tentative bet", target="beta marker",
                    deadline=time.time() - 5, confidence=0.2)
        by_stmt = {c.prediction.statement: c for c in glue.settle_predictions(cfg)}
        assert by_stmt["a bold confident bet"].surprise > by_stmt["a hedged tentative bet"].surprise

    def test_closure_births_episode_engram(self, tmp_path):
        cfg = _cfg(tmp_path)
        led = ExpectationLedger(cfg)
        p = led.predict(statement="the deploy lands green", target="ci pipeline green",
                        deadline=time.time() - 1, confidence=0.9)
        closures = glue.settle_predictions(cfg, tick=11)
        assert len(closures) == 1
        residue = closures[0].residue
        episodes = [e for e in ExpectationLedger(cfg).ring.load() if e.kind == "episode"]
        assert len(episodes) == 1                        # closure birthed exactly one episode
        assert episodes[0].id == residue.id
        assert p.id in episodes[0].links                  # the residue points back at the bet
        assert "WRONG" in episodes[0].body                # it carries the verdict
        # confident-wrong residue is born STRONG (§M-4: the highest-value episodic input)
        assert episodes[0].strength > expectations.RESIDUE_STRENGTH_FLOOR

    def test_surprise_feeds_reward_rpe_and_curiosity(self, tmp_path):
        cfg = _cfg(tmp_path)
        ExpectationLedger(cfg).predict(statement="a confident bet", target="gamma marker",
                                       deadline=time.time() - 1, confidence=0.9)

        class CuriosityStub:
            def __init__(self):
                self.saw = []
            def observe(self, surprise):
                self.saw.append(float(surprise))
                return 0.123                              # the intrinsic bonus it hands back

        class RewardStub:
            def __init__(self):
                self.calls = []
            def observe(self, **kw):
                self.calls.append(kw)

        cur, rew = CuriosityStub(), RewardStub()
        closures = glue.settle_predictions(cfg, tick=5, reward=rew, curiosity=cur)
        assert len(closures) == 1
        assert cur.saw == [pytest.approx(closures[0].surprise)]      # curiosity got the surprise
        assert len(rew.calls) == 1
        call = rew.calls[0]
        assert call["success"] is False                              # the bet was wrong
        assert call["intrinsic"] == pytest.approx(0.123)             # curiosity's bonus → the RPE
        assert call["situation"] == "prediction:general"

    def test_dark_gate_no_ops(self, tmp_path):
        cfg = _cfg(tmp_path)
        led = ExpectationLedger(cfg)
        p = led.predict(statement="a bet made while lit", target="x",
                        deadline=time.time() - 1)
        cfg.pillars_expectations_enabled = False
        assert glue.settle_predictions(cfg) == []
        assert close_due_predictions(cfg, led) == []
        assert close_event_predictions(cfg, led, "x") == []
        assert close_prediction(cfg, led, p, outcome=True, reason="event") is None


# =================================================================================================
class TestSelfReportNeverCloses:
    def test_self_report_reason_is_rejected(self, tmp_path):
        cfg = _cfg(tmp_path)
        led = ExpectationLedger(cfg)
        p = led.predict(statement="the model will vouch for itself", target="t",
                        deadline=_future())
        for prose_reason in ("self_report", "llm", "the model said so", ""):
            with pytest.raises(ValueError, match="ground truth"):
                close_prediction(cfg, led, p, outcome=True, reason=prose_reason)
        assert len(led.open_predictions()) == 1           # still open — prose settled nothing
        assert CLOSE_REASONS == {"deadline", "event"}     # the whitelist IS the doctrine

    def test_no_closing_tool_surface(self, tmp_path):
        """Only GLUE can close: enabling the organ adds exactly ONE tool (`predict` — a maker of
        bets), and nothing in the tool registry can settle one."""
        before = set(tools.TOOLS)
        tools.register_predict_tool(_cfg(tmp_path, enabled=True))
        added = set(tools.TOOLS) - before
        assert added == {"predict"}
        # no tool settles a bet: nothing predict-adjacent offers close/settle/resolve
        # (note_close closes NOTES — a different organ entirely).
        assert not [n for n in tools.TOOLS
                    if ("predict" in n or "expect" in n) and n != "predict"]
        assert not [n for n in tools.TOOLS
                    if n != "note_close" and ("close" in n or "settle" in n or "resolve" in n)]

    def test_tool_predict_cannot_mutate_an_open_bet(self, tmp_path):
        cfg = _cfg(tmp_path, max_open=5)
        led = ExpectationLedger(cfg)
        p = led.predict(statement="the original bet", target="t", deadline=_future())
        # The model "re-predicting the outcome happened" just makes ANOTHER open bet; the
        # original stays open until glue settles it.
        tools.tool_predict({"statement": "the original bet came true, I am sure"}, cfg)
        opens = ExpectationLedger(cfg).open_predictions()
        assert len(opens) == 2
        assert next(q for q in opens if q.id == p.id).status == "open"


# =================================================================================================
class TestBrierCalibration:
    def test_per_domain_scores_on_synthetic_set(self, tmp_path):
        cfg = _cfg(tmp_path, max_open=12)
        led = ExpectationLedger(cfg)
        # domain "net": (0.9, True) (0.8, True) (0.7, False) → brier = (0.01+0.04+0.49)/3 = 0.18
        net = [(0.9, True), (0.8, True), (0.7, False)]
        # domain "ops": (0.6, False) → brier = 0.36 (worse-calibrated than net)
        ops = [(0.6, False)]
        for i, (conf, outcome) in enumerate(net):
            p = led.predict(statement=f"net bet {i}", target=f"net target {i}",
                            deadline=_future(), confidence=conf, domain="net")
            close_prediction(cfg, led, p, outcome=outcome,
                             reason="event" if outcome else "deadline")
        for i, (conf, outcome) in enumerate(ops):
            p = led.predict(statement=f"ops bet {i}", target=f"ops target {i}",
                            deadline=_future(), confidence=conf, domain="ops")
            close_prediction(cfg, led, p, outcome=outcome, reason="deadline")
        led.predict(statement="still open — must not count", target="x",
                    deadline=_future(), confidence=0.5, domain="net")

        scores = brier_calibration_by_domain(cfg)
        assert set(scores) == {"net", "ops"}
        assert scores["net"]["brier"] == pytest.approx(0.18, abs=1e-3)
        assert scores["net"]["n"] == 3                    # the open bet did not count
        assert scores["ops"]["brier"] == pytest.approx(0.36, abs=1e-3)
        assert scores["ops"]["n"] == 1
        assert scores["net"]["brier"] < scores["ops"]["brier"]
        for d in scores.values():
            assert 0.0 <= d["brier"] <= 1.0
            assert 0.0 <= d["mean_confidence"] <= 1.0

    def test_empty_ledger_yields_empty_dict(self, tmp_path):
        assert brier_calibration_by_domain(_cfg(tmp_path)) == {}


# =================================================================================================
class TestAwaitingRenderer:
    def test_render_empty_when_nothing_open(self, tmp_path):
        assert ExpectationLedger(_cfg(tmp_path)).render() == ""

    def test_render_shows_open_bets(self, tmp_path):
        cfg = _cfg(tmp_path)
        led = ExpectationLedger(cfg)
        led.predict(statement="dean home by six", target="front door opens",
                    deadline=_future(600), confidence=0.75)
        block = led.render()
        assert "AWAITING" in block
        assert "dean home by six" in block
        assert "75%" in block
        # a settled bet drops out of the block
        for p in led.open_predictions():
            close_prediction(cfg, led, p, outcome=True, reason="event")
        assert led.render() == ""


# =================================================================================================
class TestTotalPlaced:
    """The honest monotonic ever-placed counter (quest glue's `expectations.total`): a bet IN the
    ledger, counted once, forever — closures and the ring's bounded forgetting never shrink it."""

    def test_counts_and_survives_closure(self, tmp_path):
        cfg = _cfg(tmp_path)
        led = ExpectationLedger(cfg)
        assert led.total_placed() == 0
        led.predict(statement="first bet", target="t1", deadline=_future())
        p2 = led.predict(statement="second bet", target="t2", deadline=_future())
        assert led.total_placed() == 2
        close_prediction(cfg, led, p2, outcome=True, reason="event")
        assert led.total_placed() == 2                 # a lifetime fact never walks back
        assert len(led.open_predictions()) == 1

    def test_survives_ring_eviction(self, tmp_path):
        # The ring is bounded and forgets (by design); the book of record must not.
        from engram import EpisodicRing
        cfg = _cfg(tmp_path, max_open=100)
        led = ExpectationLedger(cfg, ring=EpisodicRing(cfg, max_items=2))
        for i in range(5):
            led.predict(statement=f"bet {i}", target=f"t{i}", deadline=_future())
        assert len(led._all_predictions()) <= 2        # the ring forgot the old bets…
        assert led.total_placed() == 5                 # …the counter did not

    def test_reseeds_from_ring_evidence_when_counter_missing(self, tmp_path):
        cfg = _cfg(tmp_path)
        led = ExpectationLedger(cfg)
        led.predict(statement="a", target="t", deadline=_future())
        led.predict(statement="b", target="t", deadline=_future())
        (cfg.state_dir / expectations.STATS_NAME).unlink()     # the counter file is lost
        assert ExpectationLedger(cfg).total_placed() == 2      # fail-open re-seed from evidence

    def test_eviction_path_still_counts_the_new_bet(self, tmp_path):
        cfg = _cfg(tmp_path, max_open=1)
        led = ExpectationLedger(cfg)
        led.predict(statement="a", target="t", deadline=_future())
        led.predict(statement="b", target="t", deadline=_future(), evict_oldest=True)
        assert led.total_placed() == 2                 # the evicted bet was still PLACED
