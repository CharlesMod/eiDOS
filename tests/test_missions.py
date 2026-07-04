"""Pillars 7: generals — missions.py offline unit tests (PILLARS_TODO Phase 7 gate).

Acceptance (the gate, verbatim):
  - One end-to-end mission (research-shaped): compile context pack → dispatch on the mock
    adapter → grammar-valid report ingested → findings persisted as confidence-discounted `told`
    engrams → delegation episode written → general dissolved. Completes within budget.
  - A budget-blowing general is terminated cleanly (token/wall budget exceeded → kill via
    adapter, loss episode, no leak in the roster).
  - A garbage report measurably lowers that delegation-shape's recall bias (the loss episode's
    strength makes the shape's episodes rank lower on the next compile — mechanical).
  - max_generals admission enforced; cohort dispatch batches concurrent missions; malformed
    report → drop-with-log, never ingested; spawn-nothing asserted; flag off → no-ops.

No services / tick loop / GPU — temp workspaces only; the LLM substrate is a MOCK adapter (the
injectable seam); nothing here opens a socket or touches :8080/:8099.
"""

import json
import sys
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

import pytest

import missions
from config import Config
from engram import Consolidator, Engram, LongTermStore, STRENGTH_DEFAULT
from memory_manager import MemoryManager
from missions import (
    Budget, CPUSmallAdapter, GrantError, MalformedReport, Mission, MissionError,
    MissionRunner, RemoteGangliaAdapter, SlotShareAdapter, SubstrateAdapter,
    SubstrateUnavailable, build_report_grammar, compile_context_pack, compile_mission,
    delegation_bias, mission_shape, price_energy, price_gpu_seconds, per_slot_rate,
    validate_grant, validate_report,
    FINDING_MAX_CHARS, MAX_FINDINGS, MAX_PROPOSALS, MISSION_CREDIT, MISSION_LOSS,
    PROPOSAL_MAX_CHARS, TOLD_CONFIDENCE_DISCOUNT,
)


# --- helpers --------------------------------------------------------------------------------------

MONARCH_CAPS = ["web_search", "read_file", "check_system"]

GOOD_REPORT = json.dumps({
    "findings": [
        {"body": "Sensor archives rotate nightly at #am under workspace paths.", "confidence": 0.9},
        {"body": "Archive volume has doubled since last month.", "confidence": 0.6},
    ],
    "proposals": ["Suggest pruning archives older than a month."],
    "escalate": False,
})


def _cfg(tmp_path, *, enabled: bool = True) -> Config:
    cfg = Config()
    cfg.workspace_dir = str(tmp_path / "workspace")
    cfg.mock_mode = True                    # deterministic hash embedder (no ONNX model needed)
    cfg.pillars_generals_enabled = enabled
    (tmp_path / "workspace").mkdir(parents=True, exist_ok=True)
    return cfg


def _budget(tokens=400, energy=1.0, wall=10.0) -> Budget:
    return Budget(tokens=tokens, energy=energy, wall_clock_s=wall)


def _mission(objective, *, grant=(), budget=None, escalation=(), manager=None) -> Mission:
    return compile_mission(manager, objective, monarch_capabilities=MONARCH_CAPS,
                           budget=budget or _budget(), grant=grant, escalation=list(escalation))


class MockAdapter(SubstrateAdapter):
    """The injectable mock substrate: unlimited admission, scripted responses, records every
    admit/kill/release, tracks the concurrency high-water mark, and supports a hold-until-killed
    mode for the budget-blower test."""

    grade = "house"

    def __init__(self, responder=None, *, hold: bool = False):
        self.responder = responder or (lambda prompt, **kw: {"text": GOOD_REPORT, "tokens": 120})
        self.hold = hold
        self.admitted: list[str] = []
        self.killed: list[str] = []
        self.active: set[str] = set()
        self.max_concurrent = 0
        self._kill_events: dict[str, threading.Event] = {}
        self._generating: set[str] = set()
        self._lock = threading.Lock()

    def admit(self, mission_id, *, timeout=None):
        self.admitted.append(mission_id)
        self.active.add(mission_id)
        return True

    def generate(self, mission_id, prompt, *, grammar, max_tokens):
        with self._lock:
            self._generating.add(mission_id)
            self.max_concurrent = max(self.max_concurrent, len(self._generating))
            ev = self._kill_events.setdefault(mission_id, threading.Event())
        try:
            if self.hold:
                ev.wait(timeout=10.0)   # runs until killed (or the safety ceiling)
                return {"text": "", "tokens": 0}
            return self.responder(prompt, grammar=grammar, max_tokens=max_tokens)
        finally:
            with self._lock:
                self._generating.discard(mission_id)

    def kill(self, mission_id):
        self.killed.append(mission_id)
        with self._lock:
            self._kill_events.setdefault(mission_id, threading.Event()).set()
        self.release(mission_id)

    def release(self, mission_id):
        self.active.discard(mission_id)


def _mission_episodes(cfg, shape=None):
    eps = [e for e in LongTermStore(cfg).load()
           if e.kind == "episode" and e.stats.get("src") == "mission"]
    if shape is not None:
        eps = [e for e in eps if e.stats.get("mission_shape") == shape]
    return eps


def _told_facts(cfg):
    return [e for e in LongTermStore(cfg).load() if e.provenance == "told"]


# ===================================================================================================
# The gate: one end-to-end mission (research-shaped)
# ===================================================================================================

def test_end_to_end_mission(tmp_path):
    cfg = _cfg(tmp_path)
    manager = MemoryManager(cfg)
    manager.encode("fact", "Sensor archives live under workspace and rotate on a nightly schedule.")

    from nervous.bus import NervousBus
    bus = NervousBus()
    sub = bus.subscribe()
    try:
        adapter = MockAdapter()
        runner = MissionRunner(cfg, adapter=adapter, manager=manager, bus=bus)
        budget = _budget(tokens=400, energy=1.0, wall=10.0)
        m = _mission("research the nightly sensor archives rotation", manager=manager,
                     grant=["web_search"], budget=budget, escalation=["stuck"])

        # The monarch-compiled pack carries the recalled engram, skills slot, constraints slot.
        assert m.context_pack["engrams"], "context pack should recall the seeded engram"
        assert m.report_grammar and "root ::=" in m.report_grammar

        results = runner.dispatch_cohort([m], tick=7)
        assert len(results) == 1
        r = results[0]
        assert r.success and r.reason == "report_ingested"

        # Within budget on every axis.
        assert r.tokens_used == 120 <= budget.tokens
        assert r.wall_s <= budget.wall_clock_s
        assert r.energy_charged <= budget.energy
        assert r.gpu_seconds > 0

        # Findings persisted as confidence-DISCOUNTED `told` engrams via the single writer.
        told = _told_facts(cfg)
        assert len(told) == 2 and len(r.finding_ids) == 2
        by_body = {e.body: e for e in told}
        assert abs(by_body["Archive volume has doubled since last month."].confidence
                   - 0.6 * TOLD_CONFIDENCE_DISCOUNT) < 1e-9
        assert all(e.stats.get("mission_id") == m.id for e in told)

        # Delegation episode written, credit-side, keyed on the task shape.
        eps = _mission_episodes(cfg, shape=m.shape)
        assert len(eps) == 1
        assert eps[0].strength == pytest.approx(STRENGTH_DEFAULT + MISSION_CREDIT)
        assert eps[0].stats.get("delegated") is True

        # Delegated XP mark: the outcome NEVER counts toward mastery (pitfall #8).
        assert r.outcome["delegated"] is True and r.outcome["success"] is True

        # The general dissolved — ephemeral, no persistent identity, no roster leak.
        assert runner.roster == {} and adapter.active == set()

        # The report entered as an AFFERENT: a fungible percept from the general's organ name.
        ev = bus.recv(sub, timeout=2.0)
        assert ev is not None and ev.source_organ == f"general:{m.id[:8]}"
        payload = json.loads(bus.payloads.get(ev.payload_ref).decode("utf-8"))
        assert payload["report"]["findings"][0]["confidence"] == 0.9
        assert payload["shape"] == m.shape
    finally:
        bus.close()


def test_escalation_wakes_the_monarch_reliably(tmp_path):
    cfg = _cfg(tmp_path)
    report = json.loads(GOOD_REPORT)
    report["escalate"] = True
    adapter = MockAdapter(lambda prompt, **kw: {"text": json.dumps(report), "tokens": 50})

    from nervous.bus import NervousBus
    from nervous.event import Delivery
    bus = NervousBus()
    sub = bus.subscribe()
    try:
        runner = MissionRunner(cfg, adapter=adapter, bus=bus)
        m = _mission("investigate the failing auth token refresh", escalation=["stuck_on_auth"])
        [r] = runner.dispatch_cohort([m])
        assert r.success
        # Reliable-class escalation outranks the fungible report afferent: it surfaces FIRST.
        first = bus.recv(sub, timeout=2.0)
        assert first is not None and first.delivery == Delivery.reliable
        payload = json.loads(bus.payloads.get(first.payload_ref).decode("utf-8"))
        assert payload["escalation"] == "stuck_on_auth"
    finally:
        bus.close()


# ===================================================================================================
# The gate: a budget-blowing general is terminated cleanly
# ===================================================================================================

def test_wall_clock_blower_terminated_cleanly(tmp_path):
    cfg = _cfg(tmp_path)
    adapter = MockAdapter(hold=True)   # generates forever until killed
    runner = MissionRunner(cfg, adapter=adapter)
    m = _mission("summarize everything ever written", budget=_budget(wall=0.2))

    [r] = runner.dispatch_cohort([m], tick=3)
    assert not r.success and r.reason == "wall_clock_exceeded"
    assert adapter.killed == [m.id], "the blower must be killed VIA THE ADAPTER"
    # Loss episode written; roster and adapter slots both clean (no leak).
    eps = _mission_episodes(cfg, shape=m.shape)
    assert len(eps) == 1
    assert eps[0].strength == pytest.approx(STRENGTH_DEFAULT - MISSION_LOSS)
    assert runner.roster == {} and adapter.active == set()
    assert r.outcome["delegated"] is True


def test_token_blower_terminated_cleanly(tmp_path):
    cfg = _cfg(tmp_path)
    # The substrate overruns the token ceiling (max_tokens should prevent this at the sampler;
    # the runner re-checks anyway — belt and braces at the settlement boundary).
    adapter = MockAdapter(lambda prompt, **kw: {"text": GOOD_REPORT, "tokens": 9999})
    runner = MissionRunner(cfg, adapter=adapter)
    m = _mission("research the modbus battery telemetry", budget=_budget(tokens=100))

    [r] = runner.dispatch_cohort([m])
    assert not r.success and r.reason == "token_budget_exceeded"
    assert adapter.killed == [m.id]
    assert not _told_facts(cfg), "an over-budget general's report is never ingested"
    assert runner.roster == {} and adapter.active == set()


# ===================================================================================================
# The gate: a garbage report measurably lowers that shape's recall bias
# ===================================================================================================

def test_garbage_report_lowers_shape_recall_bias(tmp_path):
    cfg = _cfg(tmp_path)
    manager = MemoryManager(cfg)
    objective_a = "research the overnight sensor logs"
    objective_b = "research the overnight weather panel"

    good = MockAdapter()
    runner = MissionRunner(cfg, adapter=good, manager=manager)

    # Shape A earns credit first...
    [r1] = runner.dispatch_cohort([_mission(objective_a, manager=manager)], tick=1)
    assert r1.success
    bias_before = delegation_bias(runner.consolidator.store, objective_a)
    assert bias_before == pytest.approx(STRENGTH_DEFAULT + MISSION_CREDIT)

    # ...then returns garbage: the loss episode drags the shape's bias down, measurably.
    garbage = MockAdapter(lambda prompt, **kw: {"text": "here are my findings: lots!", "tokens": 30})
    runner_bad = MissionRunner(cfg, adapter=garbage, manager=manager)
    [r2] = runner_bad.dispatch_cohort([_mission(objective_a, manager=manager)], tick=2)
    assert not r2.success and r2.reason == "malformed_report"
    bias_after = delegation_bias(runner_bad.consolidator.store, objective_a)
    assert bias_after < bias_before

    # Shape B settles a clean success; on the next compile the ordinary recall ranking
    # (relevance × strength) sinks A's LOSS episode below B's success — the mechanical
    # delegate-bias, and the shape aggregates order the same way.
    [r3] = runner.dispatch_cohort([_mission(objective_b, manager=manager)], tick=3)
    assert r3.success
    recalled = manager.recall("research the overnight")
    pos = {(e.stats.get("mission_shape"), e.stats.get("success")): i
           for i, e in enumerate(recalled) if e.stats.get("src") == "mission"}
    shape_a, shape_b = mission_shape(objective_a), mission_shape(objective_b)
    assert (shape_a, False) in pos, "the loss episode must surface on the next compile"
    assert pos[(shape_b, True)] < pos[(shape_a, False)]
    store = runner.consolidator.store
    assert delegation_bias(store, objective_a) < delegation_bias(store, objective_b)


def test_malformed_report_drop_with_log_never_ingested(tmp_path, caplog):
    cfg = _cfg(tmp_path)
    bad_reports = [
        "not json at all",
        json.dumps({"findings": []}),                                          # missing keys
        json.dumps({"findings": [], "proposals": [], "escalate": "yes"}),      # untyped escalate
        json.dumps({"findings": [], "proposals": [], "escalate": False,
                    "actions": ["rm -rf /"]}),                                 # smuggled action channel
    ]
    for i, text in enumerate(bad_reports):
        adapter = MockAdapter(lambda prompt, _t=text, **kw: {"text": _t, "tokens": 10})
        runner = MissionRunner(cfg, adapter=adapter)
        with caplog.at_level("WARNING", logger="eidos.missions"):
            [r] = runner.dispatch_cohort([_mission(f"probe malformed variant {i} of the report")])
        assert not r.success and r.reason == "malformed_report"
        assert runner.dropped_reports == 1
        assert "dropped (malformed)" in caplog.text
        caplog.clear()
    assert not _told_facts(cfg), "no malformed report may ever reach memory"


# ===================================================================================================
# The gate: max_generals admission enforced (+ the arbiter seam)
# ===================================================================================================

def test_slot_share_admission_cap_enforced(tmp_path):
    cfg = _cfg(tmp_path)
    cfg.pillars_max_generals = 2
    adapter = SlotShareAdapter(cfg, lambda prompt, **kw: {"text": GOOD_REPORT, "tokens": 10})
    assert adapter.admit("m1", timeout=0.05)
    assert adapter.admit("m2", timeout=0.05)
    assert not adapter.admit("m3", timeout=0.05), "the derived cap must deny the 3rd general"
    adapter.release("m1")
    assert adapter.admit("m3", timeout=0.5), "a freed slot re-admits (event-driven release)"


def test_slot_share_admission_cap_through_runner(tmp_path):
    cfg = _cfg(tmp_path)
    cfg.pillars_max_generals = 2
    adapter = SlotShareAdapter(cfg, lambda prompt, **kw: {"text": GOOD_REPORT, "tokens": 10})
    runner = MissionRunner(cfg, adapter=adapter)
    cohort = [_mission(f"research admission case number {i} details") for i in range(3)]
    results = runner.dispatch_cohort(cohort, admission_timeout_s=0.05)
    by_reason = sorted(r.reason for r in results)
    assert by_reason.count("admission_denied") == 1
    assert sum(1 for r in results if r.success) == 2
    assert adapter.active == set() and runner.roster == {}


def test_arbiter_gate_holds_and_releases_event_driven(tmp_path):
    cfg = _cfg(tmp_path)
    adapter = SlotShareAdapter(cfg, lambda prompt, **kw: {"text": "", "tokens": 0}, max_generals=5)
    adapter.gate(mind_busy=True)
    assert not adapter.admit("m1", timeout=0.05), "admission HOLDS during a mind burst"

    got = {}
    waiter = threading.Thread(target=lambda: got.update(ok=adapter.admit("m2", timeout=5.0)))
    waiter.start()
    adapter.gate(mind_busy=False)   # the idle gap: the gate NOTIFIES the waiter (no polling)
    waiter.join(timeout=2.0)
    assert not waiter.is_alive() and got.get("ok") is True


def test_slot_share_kill_forwards_to_client_cancel(tmp_path):
    cfg = _cfg(tmp_path)

    class Client:
        def __init__(self):
            self.cancelled = []

        def __call__(self, prompt, **kw):
            return {"text": GOOD_REPORT, "tokens": 10}

        def cancel(self, mission_id):
            self.cancelled.append(mission_id)

    client = Client()
    adapter = SlotShareAdapter(cfg, client, max_generals=5)
    assert adapter.admit("m1", timeout=0.1)
    adapter.kill("m1")
    assert client.cancelled == ["m1"], "kill must reach the substrate's cancel seam"
    assert adapter.active == set()


# ===================================================================================================
# The gate: cohort dispatch batches concurrent missions (overlap emergent, priced honestly)
# ===================================================================================================

def test_cohort_dispatch_overlaps_generations(tmp_path):
    cfg = _cfg(tmp_path)
    barrier = threading.Barrier(3)

    def responder(prompt, **kw):
        # Every general must be MID-GENERATION at once for the barrier to pass — proof of
        # overlap from concurrent dispatch, with no beat and no timer anywhere.
        barrier.wait(timeout=5.0)
        return {"text": GOOD_REPORT, "tokens": 60}

    adapter = MockAdapter(responder)
    runner = MissionRunner(cfg, adapter=adapter)
    cohort = [_mission(f"research cohort sub objective number {i} closely") for i in range(3)]
    results = runner.dispatch_cohort(cohort)
    assert all(r.success for r in results), "serial dispatch would have broken the barrier"
    assert adapter.max_concurrent == 3
    # Cohort pricing rode the batched curve: cheaper per mission than a serial dispatch.
    assert results[0].energy_charged == pytest.approx(price_energy(60, 3))
    assert results[0].energy_charged < price_energy(60, 1)


def test_pricing_curve_matches_spike_numbers():
    # Declared curve: ~9%/slot per-slot hit off the 77.8 tok/s baseline.
    assert per_slot_rate(1) == pytest.approx(77.8)
    assert per_slot_rate(2) == pytest.approx(77.8 * 0.91)
    # Batched is honestly cheaper per mission, and the 5-general cohort's TOTAL wall sits in
    # the spike's measured band (~5 s batched vs ~13 s serial → ratio ~0.3-0.4).
    assert price_gpu_seconds(1000, 5) < price_gpu_seconds(1000, 1)
    batched_total = 1000 / per_slot_rate(5)            # the cohort overlaps: one shared wall
    serial_total = 5 * 1000 / per_slot_rate(1)
    assert 0.2 < batched_total / serial_total < 0.5
    # The floor guards the arithmetic beyond the measured range.
    assert per_slot_rate(50) == missions.MIN_PER_SLOT_RATE


# ===================================================================================================
# The report contract — grammar-constrained BOTH directions, bounded-string doctrine
# ===================================================================================================

def test_report_grammar_is_bounded_and_shaped():
    g = build_report_grammar()
    assert "root ::=" in g and '"escalate"' in g.replace("\\", "")
    # Bounded-string doctrine (administrator.py smokes): no open jstring in free-text slots —
    # bodies and proposals are bounded schar repetitions.
    assert f"schar{{1,{FINDING_MAX_CHARS}}}" in g
    assert f"schar{{1,{PROPOSAL_MAX_CHARS}}}" in g
    finding_rule = next(ln for ln in g.splitlines() if ln.startswith("finding ::="))
    assert "jstring" not in finding_rule and "jobject" not in finding_rule
    # Structured sub-values constrained to their real shape: confidence is [0,1] at the sampler,
    # and both arrays are bounded.
    assert "conf ::=" in g and "jnumber" not in finding_rule
    assert f"{{0,{MAX_FINDINGS - 1}}}" in g and f"{{0,{MAX_PROPOSALS - 1}}}" in g


def test_validate_report_mirrors_the_grammar():
    report = validate_report(GOOD_REPORT)
    assert report["escalate"] is False and len(report["findings"]) == 2

    with pytest.raises(MalformedReport):
        validate_report(json.dumps({"findings": [], "proposals": [], "escalate": False,
                                    "spawn": "general"}))       # extra key = smuggled channel
    with pytest.raises(MalformedReport):
        validate_report(json.dumps({"findings": [{"body": "x", "confidence": 1.5}],
                                    "proposals": [], "escalate": False}))
    with pytest.raises(MalformedReport):
        validate_report(json.dumps({"findings": [{"body": "", "confidence": 0.5}],
                                    "proposals": [], "escalate": False}))
    with pytest.raises(MalformedReport):                        # over the bounded-string cap
        validate_report(json.dumps({"findings": [{"body": "x" * (FINDING_MAX_CHARS + 1),
                                                  "confidence": 0.5}],
                                    "proposals": [], "escalate": False}))
    with pytest.raises(MalformedReport):                        # over the bounded-array cap
        validate_report(json.dumps({"findings": [{"body": f"f{i}", "confidence": 0.5}
                                                 for i in range(MAX_FINDINGS + 1)],
                                    "proposals": [], "escalate": False}))
    with pytest.raises(MalformedReport):                        # proposals are strings, not actions
        validate_report(json.dumps({"findings": [], "proposals": [{"do": "rm -rf"}],
                                    "escalate": False}))


# ===================================================================================================
# The contract: one objective, attenuated grant, spawn-nothing
# ===================================================================================================

def test_mission_carries_exactly_one_objective(tmp_path):
    with pytest.raises(MissionError):
        _mission(["objective one", "objective two"])   # a list is a cohort, not a mission
    with pytest.raises(MissionError):
        _mission("   ")


def test_grant_attenuation_and_spawn_nothing():
    assert validate_grant(["web_search"], MONARCH_CAPS) == ["web_search"]
    with pytest.raises(GrantError):
        validate_grant(["web_search", "launch_rockets"], MONARCH_CAPS)   # ⊄ monarch's own
    # Spawn-class is forbidden STRUCTURALLY — even if the monarch itself holds it.
    for cap in ("spawn_general", "spawn_shadow", "dispatch_cohort", "delegate"):
        with pytest.raises(GrantError):
            validate_grant([cap], MONARCH_CAPS + [cap])


def test_budget_must_be_positive():
    for bad in (Budget(0, 1.0, 1.0), Budget(100, 0.0, 1.0), Budget(100, 1.0, 0.0)):
        with pytest.raises(MissionError):
            bad.validate()


# ===================================================================================================
# Substrate stubs fail loud at bind (I8), one interface (I9)
# ===================================================================================================

def test_cpu_small_and_remote_ganglia_are_explicit_stubs():
    for adapter, needle in ((CPUSmallAdapter(), "no small GGUF"),
                            (RemoteGangliaAdapter(), "ZmqTransport")):
        with pytest.raises(SubstrateUnavailable, match=needle):
            adapter.admit("m1")
        with pytest.raises(SubstrateUnavailable):
            adapter.generate("m1", "p", grammar="", max_tokens=10)
    assert CPUSmallAdapter.grade == "small" and RemoteGangliaAdapter.grade == "remote"
    assert SlotShareAdapter.grade == "house"


# ===================================================================================================
# The gate: flag off → no-ops (ships dark)
# ===================================================================================================

def test_flag_off_all_entrypoints_noop(tmp_path):
    cfg = _cfg(tmp_path, enabled=False)
    adapter = MockAdapter()
    runner = MissionRunner(cfg, adapter=adapter)
    m = _mission("research anything at all today")

    assert runner.dispatch_cohort([m]) == []
    assert adapter.admitted == [], "a dark runner never touches the substrate"
    assert runner.settle_mission(m, success=True) is None
    assert runner.ingest_report(m, json.loads(GOOD_REPORT)) == []
    assert LongTermStore(cfg).load() == [] and runner.roster == {}


# ===================================================================================================
# Shape + bias plumbing
# ===================================================================================================

def test_mission_shape_normalizes_variants():
    a = mission_shape("Research the sensor logs on port 8099!")
    b = mission_shape("research the sensor logs on port 8080")
    assert a == b, "digit/punctuation variants of the same errand share a shape"
    assert len(a.split()) <= missions.SHAPE_TOKENS


def test_delegation_bias_reads_shape_history(tmp_path):
    cfg = _cfg(tmp_path)
    store = LongTermStore(cfg)
    assert delegation_bias(store, "never delegated before") is None
    runner = MissionRunner(cfg, adapter=MockAdapter())
    m = _mission("audit the backup rotation schedule weekly")
    runner.settle_mission(m, success=False, reason="test_loss", tick=1)
    bias = delegation_bias(store, m.objective)
    assert bias == pytest.approx(STRENGTH_DEFAULT - MISSION_LOSS)
