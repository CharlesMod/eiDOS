"""Pillars 2.4: the sleep engine (nervous/sleep.py) + adenosine (nervous/neuromod.py) — offline gates.

Gate (PILLARS_TODO 2.4, red-able):
  - post-sleep recall precision >= pre-sleep on a small synthetic held-out replay set;
  - observations digest end-to-end with ZERO silently-dropped extractions (malformed -> LOGGED,
    counted, asserted — never swallowed);
  - a creature held at max goal-tension still sleeps BEFORE the wake-hours limit (adenosine,
    pitfall #2);
  - jobs run in priority order; a throwing job is ISOLATED and the sleep completes (I5).

No services / tick loop / GPU / model — temp workspaces, deterministic token-overlap recall
(mock_mode off + no embedder => engram recall's embedding-free fallback), a fake llm callable.
"""

import json
import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

import pytest

from config import Config
import engram
from engram import Engram, Consolidator
from nervous import NervousBus, NeuromodulatoryState, SleepCycle
from nervous.neuromod import (
    Adenosine, ADENOSINE_OVERRIDE_AROUSAL, ADENOSINE_SOFT_FRACTION,
)
from nervous.organs import OrganRegistry
from nervous.sleep import (
    SleepEngine, SleepContext, SleepJob, SleepReport, JobResult,
    run_sleep, default_sleep_engine,
    DedupMergeJob, StrengthDecayPruneJob, DistillationJob, BackupSnapshotJob,
    TelemetryRederiveJob, OrganSleepHooksJob,
    build_distillation_grammar, parse_distillations,
    DREAMED_CONFIDENCE_CAP, DISTILL_MAX_FACTS,
)


# --- helpers -------------------------------------------------------------------------------------

def _cfg(tmp_path) -> Config:
    cfg = Config()
    cfg.workspace_dir = str(tmp_path / "workspace")
    cfg.mock_mode = False               # no embedder at all => recall uses the deterministic
    #                                     token-overlap fallback (no ONNX model needed)
    cfg.pillars_sleep_engine_enabled = True
    cfg.workspace.mkdir(parents=True, exist_ok=True)
    return cfg


def _seed_longterm(cons: Consolidator, engrams: list) -> None:
    """Construct a PRE-SLEEP long-term store verbatim (bypassing commit-time merge) — the test is
    building the 'undigested' condition the sleep pass is supposed to clean up."""
    engram._commit_to_store(cons.store, engrams)


class _RecordingJob:
    """A minimal SleepJob that records its execution order (and optionally throws)."""

    def __init__(self, name, priority, log, *, boom=False):
        self.name = name
        self.priority = priority
        self._log = log
        self._boom = boom

    def run(self, ctx):
        self._log.append(self.name)
        if self._boom:
            raise RuntimeError(f"{self.name} exploded")
        return {"ran": self.name}


# =================================================================================================
# Gate: jobs run in priority order; a throwing job is isolated and sleep completes (I5)
# =================================================================================================
class TestEngineOrderAndGuard:
    def test_priority_order_with_registration_tiebreak(self, tmp_path):
        log = []
        eng = SleepEngine()
        eng.register(_RecordingJob("late", 90, log))
        eng.register(_RecordingJob("early", 10, log))
        eng.register(_RecordingJob("mid_a", 50, log))
        eng.register(_RecordingJob("mid_b", 50, log))    # same priority: registration order wins
        report = eng.run(SleepContext(config=_cfg(tmp_path)))
        assert log == ["early", "mid_a", "mid_b", "late"]
        assert report.ok and report.ran == 4

    def test_throwing_job_is_isolated_and_sleep_completes(self, tmp_path):
        log = []
        eng = SleepEngine([
            _RecordingJob("first", 10, log),
            _RecordingJob("bomb", 20, log, boom=True),
            _RecordingJob("last", 30, log),
        ])
        report = eng.run(SleepContext(config=_cfg(tmp_path)))
        assert log == ["first", "bomb", "last"]          # the fault did NOT abort the sleep
        assert not report.ok
        assert [r.name for r in report.failed] == ["bomb"]
        assert "exploded" in report.by_name("bomb").error
        assert report.by_name("first").ok and report.by_name("last").ok

    def test_default_engine_reads_like_a_digestive_tract(self):
        names = [j.name for j in default_sleep_engine().jobs]
        assert names == ["dedup_merge", "strength_decay_prune", "distillation",
                         "backup_snapshot", "telemetry_rederive", "organ_on_sleep"]

    def test_jobs_satisfy_the_protocol(self):
        for job in default_sleep_engine().jobs:
            assert isinstance(job, SleepJob)


# =================================================================================================
# Dedup / merge (compressed replay) — merged provenance kept, store actually shrinks
# =================================================================================================
class TestDedupMerge:
    def test_near_restatements_merge_and_store_shrinks(self, tmp_path):
        cfg = _cfg(tmp_path)
        cons = Consolidator(cfg)
        keeper = Engram(kind="fact", body="the mqtt broker listens on port 1883 on sprinter",
                        provenance="experienced", strength=0.4, links=["a1"])
        twin = Engram(kind="fact", body="the mqtt broker listens on port 1883 on sprinter box",
                      provenance="told", strength=0.8, links=["b2"])
        twin.stats["credit_sum"] = 1.5
        other = Engram(kind="procedure", body="restart the dashboard with systemctl restart eidos-dashboard")
        _seed_longterm(cons, [keeper, twin, other])       # the undigested pre-sleep condition

        summary = DedupMergeJob().run(SleepContext(config=cfg, consolidator=cons))
        assert summary == {"merges": 1, "engrams": 2}
        settled = cons.store.load()
        assert len(settled) == 2                          # the twin was absorbed, not just annotated
        survivor = next(e for e in settled if e.kind == "fact")
        assert survivor.id == keeper.id                   # oldest witness is the keeper
        assert survivor.provenance == "experienced"       # merged provenance kept (the keeper's)
        assert survivor.strength == pytest.approx(0.8)    # corroboration keeps the stronger witness
        assert set(survivor.links) == {"a1", "b2"}        # associations union, never lost
        assert survivor.stats["credit_sum"] == pytest.approx(1.5)

    def test_idempotent_on_a_clean_store(self, tmp_path):
        cfg = _cfg(tmp_path)
        cons = Consolidator(cfg)
        _seed_longterm(cons, [Engram(kind="fact", body="solar mppt charges the lifepo4 bank"),
                              Engram(kind="fact", body="the gpu arbiter leases vram by priority")])
        summary = DedupMergeJob().run(SleepContext(config=cfg, consolidator=cons))
        assert summary["merges"] == 0
        assert len(cons.store.load()) == 2

    def test_no_consolidator_is_a_clean_skip(self, tmp_path):
        assert "skipped" in DedupMergeJob().run(SleepContext(config=_cfg(tmp_path)))


# =================================================================================================
# Strength decay + prune-to-budget (SHY)
# =================================================================================================
class TestStrengthDecayPrune:
    def test_decay_applies_and_weakest_pruned_to_budget(self, tmp_path):
        cfg = _cfg(tmp_path)
        cons = Consolidator(cfg)
        strengths = [0.9, 0.7, 0.5, 0.3, 0.1, 0.02]
        _seed_longterm(cons, [Engram(kind="fact", body=f"synthetic fact number {i} about topic {i}",
                                     strength=s) for i, s in enumerate(strengths)])
        summary = StrengthDecayPruneJob(decay=0.1, budget=4).run(
            SleepContext(config=cfg, consolidator=cons))
        assert summary["pruned"] == 2 and summary["engrams"] == 4
        kept = sorted(e.strength for e in cons.store.load())
        # every survivor decayed by exactly 0.1; the two weakest fell out of memory
        assert kept == pytest.approx([0.2, 0.4, 0.6, 0.8])

    def test_decay_clamps_at_zero(self, tmp_path):
        cfg = _cfg(tmp_path)
        cons = Consolidator(cfg)
        _seed_longterm(cons, [Engram(kind="fact", body="a barely-there trace", strength=0.01)])
        StrengthDecayPruneJob(decay=0.1, budget=10).run(SleepContext(config=cfg, consolidator=cons))
        assert cons.store.load()[0].strength == 0.0


# =================================================================================================
# Gate: digestion end-to-end with ZERO silently-dropped extractions
# =================================================================================================
GOOD_LINE_1 = "FACT [network, mqtt]: the mqtt broker answers on port 1883"
GOOD_LINE_2 = "PROCEDURE [restart]: restart the dashboard via systemctl restart eidos-dashboard"
BAD_LINE = "this line matches no extraction contract at all"


class TestDistillation:
    def _ctx(self, tmp_path, llm):
        cfg = _cfg(tmp_path)
        cons = Consolidator(cfg)
        obs = [{"tool": "bash", "success": True, "output": "port 1883 open on sprinter"},
               {"tool": "bash", "success": False, "output": "dashboard needed a restart"}]
        return SleepContext(config=cfg, consolidator=cons, llm=llm, observations=obs), cons

    def test_digestion_end_to_end_zero_silent_drops(self, tmp_path, caplog):
        emitted = "\n".join([GOOD_LINE_1, BAD_LINE, GOOD_LINE_2])
        ctx, cons = self._ctx(tmp_path, lambda messages, grammar=None: emitted)
        with caplog.at_level(logging.WARNING, logger="eidos.sleep"):
            summary = DistillationJob().run(ctx)
        # Full accounting: every emitted line is either a committed fact or a LOGGED drop.
        assert summary["distilled"] == 2
        assert summary["dropped"] == 1
        assert summary["distilled"] + summary["dropped"] == len(emitted.splitlines())
        dropped_logs = [r for r in caplog.records if "dropped malformed line" in r.getMessage()]
        assert len(dropped_logs) == 1                      # the malformed line was logged...
        assert BAD_LINE in dropped_logs[0].getMessage()    # ...verbatim — never silently swallowed
        stored = cons.store.load()
        assert len(stored) == 2
        kinds = {e.kind for e in stored}
        assert kinds == {"fact", "procedure"}
        for e in stored:
            # pitfall #5: a dream is a hypothesis — confidence CAPPED below neutral, origin stamped
            assert e.confidence <= DREAMED_CONFIDENCE_CAP
            assert e.stats.get("dreamed") is True

    def test_grammar_gets_passed_to_the_llm(self, tmp_path):
        seen = {}

        def llm(messages, grammar=None):
            seen["grammar"] = grammar
            return "NONE"

        ctx, _ = self._ctx(tmp_path, llm)
        DistillationJob().run(ctx)
        assert seen["grammar"] == build_distillation_grammar(max_facts=DISTILL_MAX_FACTS)

    def test_no_llm_and_no_observations_are_clean_noops(self, tmp_path):
        ctx, cons = self._ctx(tmp_path, None)
        assert DistillationJob().run(ctx)["note"] == "no llm"
        ctx2, _ = self._ctx(tmp_path, lambda m, grammar=None: GOOD_LINE_1)
        ctx2.observations = []
        assert DistillationJob().run(ctx2)["note"] == "no observations"
        assert len(cons.store.load()) == 0                 # nothing dreamed into memory

    def test_llm_fault_is_raised_for_the_engine_guard(self, tmp_path):
        def llm(messages, grammar=None):
            raise ConnectionError("mind offline")
        ctx, _ = self._ctx(tmp_path, llm)
        with pytest.raises(RuntimeError):
            DistillationJob().run(ctx)                     # the SleepEngine guard isolates this

    def test_parse_distillations_accounting(self):
        facts, dropped = parse_distillations(
            "\n".join([GOOD_LINE_1, "NONE", "", BAD_LINE, "ERROR [gpu]:   "]))
        assert [f["category"] for f in facts] == ["fact"]
        assert facts[0]["tags"] == ["network", "mqtt"]
        assert len(dropped) == 2                           # the junk line AND the empty-bodied line
        assert BAD_LINE in dropped

    def test_grammar_is_bounded_and_closed(self):
        g = build_distillation_grammar(max_facts=5)
        assert '"NONE"' in g
        assert "line{0,4}" in g                            # at most 5 fact lines per dream
        for cat in ("FACT", "ERROR", "PROCEDURE", "IDENTITY"):
            assert f'"{cat}"' in g


# =================================================================================================
# Gate: post-sleep recall precision >= pre-sleep on a synthetic held-out replay set
# =================================================================================================
class TestRecallPrecision:
    QUERIES = [
        ("mqtt broker port", "the mqtt broker listens on port 1883 over tcp"),
        ("solar charge controller battery", "the solar charge controller tops the lifepo4 battery at noon"),
        ("dashboard watchdog restart", "the dashboard watchdog handles the eidos restart on crash"),
    ]

    @staticmethod
    def _precision(store, relevant_ids, *, top_k=3) -> float:
        per_query = []
        for query, _ in TestRecallPrecision.QUERIES:
            got = store.recall(query, top_k=top_k)
            assert got, f"recall must never come back empty for {query!r}"
            hits = sum(1 for e in got if e.id in relevant_ids[query])
            per_query.append(hits / len(got))
        return sum(per_query) / len(per_query)

    def test_post_sleep_precision_gte_pre_sleep(self, tmp_path):
        cfg = _cfg(tmp_path)
        cons = Consolidator(cfg)
        signal, junk = [], []
        relevant_ids = {}
        for query, body in self.QUERIES:
            e = Engram(kind="fact", body=body, strength=0.9)     # earned, repeatedly-credited signal
            signal.append(e)
            relevant_ids[query] = {e.id}
            for i in range(4):                                    # weak distractors sharing a token
                tok = query.split()[0]
                junk.append(Engram(kind="fact", body=f"{tok} chatter noise sample {i}",
                                   strength=0.05))
        _seed_longterm(cons, signal + junk)                       # the undigested pre-sleep store

        adenosine = Adenosine(max_wake_hours=18.0)
        adenosine.accumulate(6.0)

        class _NM:                                                # the neuromod surface run_sleep touches
            pass
        _NM.adenosine = adenosine

        pre = self._precision(cons.store, relevant_ids)
        report = run_sleep(
            SleepContext(config=cfg, consolidator=cons, neuromod=_NM()),
            engine=default_sleep_engine(budget=len(signal), decay=0.03))
        assert report.ok, [r.error for r in report.failed]
        post = self._precision(cons.store, relevant_ids)

        assert post >= pre                                        # THE GATE
        assert post > pre                                         # and on this set, strictly better
        assert post == pytest.approx(1.0)                         # the digested store is pure signal
        assert adenosine.level_hours == 0.0                       # the creature wakes rested

    def test_dark_flag_makes_run_sleep_a_noop(self, tmp_path):
        cfg = _cfg(tmp_path)
        cfg.pillars_sleep_engine_enabled = False
        cons = Consolidator(cfg)
        _seed_longterm(cons, [Engram(kind="fact", body="an undisturbed memory", strength=0.5)])
        nm_ad = Adenosine()
        nm_ad.accumulate(5.0)

        class _NM:
            adenosine = nm_ad
        report = run_sleep(SleepContext(config=cfg, consolidator=cons, neuromod=_NM()))
        assert report.ran == 0                                    # dark: nothing ran...
        assert cons.store.load()[0].strength == 0.5               # ...nothing decayed...
        assert nm_ad.level_hours == 5.0                           # ...and no fake rest was granted


# =================================================================================================
# Gate: a creature held at max goal-tension still sleeps before the wake-hours limit (adenosine)
# =================================================================================================
class TestAdenosine:
    def test_accumulator_grows_clamps_and_clears(self):
        a = Adenosine(max_wake_hours=10.0)
        a.accumulate(4.0)
        a.accumulate(-3.0)                                        # clock skew must not lower it
        assert a.level_hours == 4.0
        assert a.pressure() == pytest.approx(0.4)
        assert not a.overrides()
        a.accumulate(7.0)
        assert a.pressure() == 1.0 and a.overrides()
        a.clear()
        assert a.level_hours == 0.0 and not a.overrides()

    @staticmethod
    def _pinned_creature(bus, *, max_wake_hours=18.0):
        """A creature whose strongest unmet goal pins the drive floor at its cap — the insomnia
        condition the adenosine damper exists for (pitfall #2)."""
        nm = NeuromodulatoryState(bus, max_wake_hours=max_wake_hours)
        nm.set_drive_floor(nm.drive_floor_cap, source="goaltension")   # held at MAX goal-tension
        return nm

    @staticmethod
    def _settle(nm, n=80):
        for _ in range(n):
            nm.observe_interoception({"bars": {}})

    def test_max_goal_tension_sleeps_before_the_wake_hours_limit(self):
        bus = NervousBus()
        try:
            nm = self._pinned_creature(bus)
            sleep = SleepCycle(bus, neuromod=nm, sleep_arousal=0.15)
            # Fully awake and pinned: no sleep.
            self._settle(nm)
            assert nm.arousal > 0.5 and not sleep.tick()
            # Deep in the drowsiness band but still BEFORE the 18h limit: adenosine drags the
            # target into the sleep band and the creature nods off — the ceiling is the backstop.
            nm.adenosine.accumulate(0.98 * nm.adenosine.max_wake_hours)
            assert not nm.adenosine.overrides()                   # strictly before the limit
            self._settle(nm)
            assert nm.arousal < 0.15
            assert sleep.tick()                                   # THE GATE: it sleeps
        finally:
            bus.close()

    def test_hard_override_past_the_ceiling_beats_every_drive_floor(self):
        bus = NervousBus()
        try:
            nm = self._pinned_creature(bus, max_wake_hours=2.0)
            nm.adenosine.accumulate(2.5)                          # past the ceiling
            assert nm.adenosine.overrides()
            self._settle(nm)
            assert nm.arousal == pytest.approx(ADENOSINE_OVERRIDE_AROUSAL, abs=0.02)
            assert SleepCycle(bus, neuromod=nm, sleep_arousal=0.15).tick()
        finally:
            bus.close()

    def test_below_the_soft_fraction_adenosine_is_inert(self):
        bus = NervousBus()
        try:
            nm = self._pinned_creature(bus)
            nm.adenosine.accumulate(0.5 * ADENOSINE_SOFT_FRACTION * nm.adenosine.max_wake_hours)
            self._settle(nm)
            assert nm.arousal == pytest.approx(nm.drive_floor_cap, abs=0.02)   # wide awake, driven
        finally:
            bus.close()


# =================================================================================================
# The remaining stock jobs: backup snapshot, telemetry re-derivation, organ on_sleep hooks
# =================================================================================================
class TestStockJobs:
    def test_backup_snapshot_job_writes_a_tarball(self, tmp_path):
        cfg = _cfg(tmp_path)
        (cfg.workspace / "a_memory.txt").write_text("worth keeping", encoding="utf-8")
        summary = BackupSnapshotJob().run(SleepContext(config=cfg))
        snap = Path(summary["snapshot"])
        assert snap.exists() and snap.name.endswith(".tar.gz")

    def test_telemetry_rederive_bounded_and_fault_isolated(self, tmp_path):
        cfg = _cfg(tmp_path)
        derivers = [("answer", lambda ctx: 42),
                    ("boom", lambda ctx: 1 / 0),                  # one bad deriver is skipped
                    ("workspace", lambda ctx: str(ctx.config.workspace))]
        summary = TelemetryRederiveJob(derivers, max_steps=8).run(SleepContext(config=cfg))
        assert summary["rederived"] == 2
        assert summary["names"] == ["answer", "workspace"]
        on_disk = json.loads((cfg.workspace / "state" / "derived_constants.json")
                             .read_text(encoding="utf-8"))
        assert on_disk["answer"] == 42

    def test_organ_on_sleep_hooks_run_in_the_sleep_window(self, tmp_path):
        cfg = _cfg(tmp_path)
        reg = OrganRegistry()
        seen = []
        reg.register(object(), name="hippocampus", on_sleep=lambda ctx: seen.append("hippocampus"))
        reg.register(object(), name="grumpy", on_sleep=lambda ctx: 1 / 0)   # guarded by the registry
        reg.register(object(), name="cortex", on_sleep=lambda ctx: seen.append("cortex"))
        summary = OrganSleepHooksJob().run(SleepContext(config=cfg, organ_registry=reg))
        assert seen == ["hippocampus", "cortex"]                  # order kept, fault swallowed
        assert summary["organs_with_on_sleep"] == 3

    def test_skips_are_clean_without_wiring(self, tmp_path):
        ctx = SleepContext(config=_cfg(tmp_path))
        assert "skipped" in OrganSleepHooksJob().run(ctx)
        assert TelemetryRederiveJob().run(ctx)["rederived"] == 0


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-q"]))
