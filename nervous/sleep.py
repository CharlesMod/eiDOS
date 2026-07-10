"""P7 / Pillars 2.4 — the sleep / consolidation cycle: the digestive tract of the memory economy.

When arousal drops to its floor, the creature sleeps: an OFFLINE cycle that replays recent activity,
re-fits the change-detection baselines (the buildable-now consolidation), and is where the learned
models — real predictive coding (T2), interoceptive inference (T3), allostasis (T4) — will live. It
runs ONLY at low arousal, so learning never competes with live perception or steals the GPU mid-tick.
The lowest arousal floor of the neuromodulatory state (Pillar 6) IS this sleep.

Two layers live here:

  - `SleepCycle` (P7, original) — the arousal-gated *trigger*: it decides WHEN to sleep and re-fits
    baselines + replays the reward learner. Unchanged; still the on-switch.

  - `SleepEngine` (Pillars 2.4) — the real *digestive tract* the sleep window runs (PILLARS_PLAN §2:
    Compressed replay, Synaptic downscaling/SHY, Gist extraction; §4: unify sleep). A PRIORITY-ORDERED
    job list, each job GUARDED (I5) so one job's fault never aborts the sleep. The jobs mutate
    long-term memory ONLY through the engram `Consolidator` (the single writer, §I6): dedup/merge,
    strength decay + prune-to-budget (SHY), grammar-constrained distillation (replacing compaction's
    regex extraction — a distilled fact is `provenance='dreamed'` and CONFIDENCE-CAPPED, pitfall #5:
    a dream is a hypothesis, not ground truth), the 0.4 backup snapshot, telemetry re-derivation, and
    finally every organ's registered `on_sleep` hook (the 1.1 registry).

Ships DARK behind `config.pillars_sleep_engine_enabled` (default False). This module is a LIBRARY —
`SleepEngine` is not imported by eidos.py or the tick loop; a later cutover owns the wiring. With the
flag off nothing in the running system changes; the machinery is exercised by unit tests in isolation.
"""
import json
import logging
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Optional, Protocol, runtime_checkable

from .event import NervousEvent, Kind, Modality, Delivery, SCHEMA_VERSION

logger = logging.getLogger("eidos.sleep")


class SleepCycle:
    def __init__(self, bus, *, neuromod=None, change_detectors=None, learner=None,
                 sleep_arousal=0.15, min_consolidate_interval_s=0.0):
        self.bus = bus
        self.neuromod = neuromod
        self.change_detectors = list(change_detectors or [])
        self.learner = learner          # the reward learner whose tagged experiences we replay (dreaming)
        self.sleep_arousal = float(sleep_arousal)
        self.min_consolidate_interval_s = float(min_consolidate_interval_s)
        self.cycles = 0
        self._last_consolidate = 0.0
        self._stop = threading.Event()
        self._thread = None

    def should_sleep(self) -> bool:
        if self.neuromod is None or self.neuromod.arousal > self.sleep_arousal:
            return False
        # throttle: a calm creature dreams during lulls, not every tick (default 0 = no throttle)
        if self.min_consolidate_interval_s > 0 and self._last_consolidate:
            if (time.monotonic() - self._last_consolidate) < self.min_consolidate_interval_s:
                return False
        return True

    def consolidate(self):
        """One consolidation pass: re-fit the baselines (reset change-detection novelty so the new
        normal is re-learned), REPLAY the reward learner's tagged experiences into durable lessons
        (dreaming — the home of self-improvement over time), and publish a sleep marker."""
        for cd in self.change_detectors:
            cd.novelty.reset()                 # re-fit 'normal' — what was surprising yesterday isn't today
        replayed = None
        if self.learner is not None:
            try:
                replayed = self.learner.replay()
            except Exception:  # noqa: BLE001 - dreaming must never wake the system badly
                replayed = None
        self.cycles += 1
        self._last_consolidate = time.monotonic()
        payload = json.dumps({"cycle": self.cycles, "action": "consolidate",
                              "replayed": (replayed or {}).get("replayed"),
                              "lessons": len((replayed or {}).get("lessons") or [])},
                             ensure_ascii=False).encode("utf-8")
        ev = NervousEvent(SCHEMA_VERSION, "sleep", Kind.capability, Modality.system,
                          Delivery.retained, salience=0.0, t=time.monotonic())
        return self.bus.publish(ev, payload)

    def tick(self) -> bool:
        """Sleep if arousal is low enough (+ throttle); returns True iff a consolidation pass ran."""
        if self.should_sleep():
            self.consolidate()
            return True
        return False

    def start(self, interval_s=10.0):
        self._thread = threading.Thread(target=self._run, args=(float(interval_s),),
                                        name="sleep-cycle", daemon=True)
        self._thread.start()
        return self

    def _run(self, interval_s):
        while not self._stop.wait(interval_s):
            try:
                self.tick()
            except Exception:
                pass

    def stop(self):
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=1.0)


# =================================================================================================
# Pillars 2.4 — the SleepEngine: the digestive tract the sleep window runs.
# =================================================================================================
# Declared knobs (§0.4: each a labeled design knob with its one-line justification) ---------------
STRENGTH_DECAY_PER_SLEEP = 0.03    # declared: SHY synaptic downscaling — every long-term engram loses
                                   # this much strength each sleep (§2 line 101: "forget the trivial,
                                   # keep the signal"). Small so a single night can't erase a useful
                                   # memory, but compounding across nights an un-recalled trace fades
                                   # while a repeatedly-credited one is topped back up by the bet ledger.
LONGTERM_BUDGET = 5000             # declared: prune-to-budget ceiling on the consolidated store (§M-3:
                                   # every store bounded). Above episodic's 2400 ring so consolidation
                                   # can promote freely, but finite — a whole-file recall stays fast and
                                   # forgetting stays a feature. When over budget, the LOWEST-strength
                                   # engrams are pruned (interference-aware: strength is earned utility).
DREAMED_CONFIDENCE_CAP = 0.4       # declared: pitfall #5 — a distilled ("dreamed") fact is a HYPOTHESIS,
                                   # not ground truth, so its confidence is capped BELOW the neutral 0.5
                                   # until experience corroborates it. Entrenchment damper: a wrong
                                   # distillation cannot masquerade as high-confidence truth.
DISTILL_MAX_FACTS = 32             # declared: bounded distillation output per sleep — the grammar caps
                                   # the model at this many fact lines so one dream can't flood the store
                                   # (bounded work per sleep, §0).
TELEMETRY_MAX_STEPS = 64           # declared: telemetry re-derivation is bounded work (§2.4 "bounded
                                   # steps") — at most this many declared-derivable constants recomputed
                                   # per sleep, so the job can never run unbounded inside the window.
CALIBRATION_MIN_CLOSED = 3         # declared: closed claim-bearing bets required before calibration may
                                   # move disposition — a single bet's Brier is noise, not evidence, and
                                   # temperament must never swing on one settled wager.
CALIBRATION_BRIER_NEUTRAL = 0.25   # declared: a coin flip's worth of squared error (expectations.py's
                                   # own yardstick) — at/below this the creature is calibrated ENOUGH
                                   # and sleep applies no caution pressure; only genuinely poor
                                   # calibration (chronic over-confidence) pushes caution up.
CALIBRATION_CAUTION_STEP_MAX = 0.05  # declared: the per-sleep bound on the caution nudge (pitfall #3:
                                   # clamp the step — one bad night must not ratchet disposition; the
                                   # temperament springs relax it back as calibration recovers).


# --- The job protocol ----------------------------------------------------------------------------
@runtime_checkable
class SleepJob(Protocol):
    """One unit of sleep work. `priority` orders execution (LOWER runs FIRST); `name` labels it in the
    report and the guard log. `run(ctx)` does the work and returns a small JSON-able summary dict; it
    may raise — the engine guards every job so one fault never aborts the sleep (I5)."""
    name: str
    priority: int

    def run(self, ctx: "SleepContext") -> dict: ...


@dataclass
class SleepContext:
    """The shared state a sleep pass hands to each job — the sleep analogue of the tick loop's `ctx`.

    `config`         — the live Config (workspace, knowledge_dir, flags).
    `consolidator`   — the engram Consolidator: the SINGLE WRITER of long-term memory (§I6). Every job
                       that mutates long-term memory does so through this, never by touching the store.
    `episodic`       — the EpisodicRing (read source for replay/distillation), optional.
    `neuromod`       — the NeuromodulatoryState, so sleep can CLEAR adenosine (the creature wakes rested).
    `organ_registry` — the 1.1 OrganRegistry whose on_sleep hooks the engine runs.
    `llm`            — a callable (messages, *, grammar=None) -> str for grammar-constrained distillation.
                       Optional: with no llm the distillation job is a clean no-op (no silent drops).
    `observations`   — the raw material to distill (list of dicts, compaction's shape), optional.
    `temperament`    — the DMN Temperament, so calibration can apply its bounded caution step. Optional:
                       with none the calibration job reports and moves nothing.
    `scratch`        — a free dict jobs may stash cross-job state in (unused by the built-ins today)."""
    config: Any
    consolidator: Any = None
    episodic: Any = None
    neuromod: Any = None
    organ_registry: Any = None
    llm: Optional[Callable[..., str]] = None
    observations: list = field(default_factory=list)
    temperament: Any = None
    scratch: dict = field(default_factory=dict)


@dataclass
class JobResult:
    """One job's outcome in the sleep report: its name, whether it ran clean, its summary, any error."""
    name: str
    ok: bool
    summary: dict = field(default_factory=dict)
    error: Optional[str] = None


@dataclass
class SleepReport:
    """The result of one full sleep pass — the ordered per-job results plus derived totals. `ok` is
    True iff EVERY job ran clean; a False here means at least one job was isolated (and the sleep still
    completed — that is the point of the guard)."""
    results: list = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return all(r.ok for r in self.results)

    @property
    def ran(self) -> int:
        return len(self.results)

    @property
    def failed(self) -> list:
        return [r for r in self.results if not r.ok]

    def by_name(self, name: str) -> Optional[JobResult]:
        return next((r for r in self.results if r.name == name), None)


class SleepEngine:
    """The priority-ordered, fault-isolated job runner the sleep window executes (Pillars 2.4).

    `jobs` run in PRIORITY ORDER (ascending — lowest priority number first); ties break on registration
    order (stable sort), so the engine is deterministic. Every job is GUARDED (I5): a job that raises is
    caught, logged, recorded as a failed JobResult, and the sleep CONTINUES to the next job — one job's
    fault never aborts consolidation (the digestive tract must finish even if one enzyme misfires).

    The engine holds no long-term-memory write path of its own: the mutating jobs go through the
    Consolidator on the ctx (§I6). This class is pure orchestration."""

    def __init__(self, jobs: Optional[list] = None):
        self._jobs: list = list(jobs or [])

    def register(self, job: SleepJob) -> "SleepEngine":
        """Add a job. Order of registration is the tie-breaker among equal priorities."""
        self._jobs.append(job)
        return self

    @property
    def jobs(self) -> list:
        """The jobs in the order they WILL run (priority-sorted, stable)."""
        return sorted(self._jobs, key=lambda j: getattr(j, "priority", 0))

    def run(self, ctx: "SleepContext") -> SleepReport:
        """Run every registered job in priority order, each guarded. Returns a SleepReport. Never
        raises for a job fault — that is the guarantee the sleep window relies on."""
        report = SleepReport()
        for job in self.jobs:
            name = getattr(job, "name", type(job).__name__)
            try:
                summary = job.run(ctx) or {}
                report.results.append(JobResult(name=name, ok=True, summary=dict(summary)))
            except Exception as e:  # noqa: BLE001 - a sleep job's fault must never abort the sleep (I5)
                logger.warning("sleep job %s failed (isolated): %s", name, e)
                report.results.append(JobResult(name=name, ok=False, error=repr(e)))
        return report


# --- Built-in jobs -------------------------------------------------------------------------------
# Priorities chosen so the pass reads like a digestive tract: replay/dedup first (settle what's
# already there), then downscale + prune (SHY), then distill new gist from raw material, then the
# durable snapshot, then telemetry, then the organ hooks last (they observe the settled result).

class DedupMergeJob:
    """Compressed replay + pattern-separation dedup (§2 line 100). Walks the long-term store and MERGES
    near-restatement pairs (overlap ≥ the consolidator's merge_threshold, same kind) through the
    Consolidator — keeping merged provenance/strength (Consolidator.merge is the policy). Idempotent:
    a store with no near-duplicates is left untouched. Single-writer safe: every merge goes through the
    consolidator, never the store directly (§I6)."""
    name = "dedup_merge"
    priority = 10

    def run(self, ctx: "SleepContext") -> dict:
        cons = ctx.consolidator
        if cons is None:
            return {"skipped": "no consolidator"}
        import engram as _eg
        entries = cons.store.load()
        # Single pass, oldest-first keep-list: the FIRST witness of a memory is the keeper; every
        # later near-restatement of the same kind is FOLDED INTO it via the Consolidator's merge
        # POLICY (strength/confidence max, links union, credit summed — merged provenance kept),
        # then dropped from the settled set. Consolidator.merge alone folds stats but cannot REMOVE
        # the absorbed twin (its contract is "commit an incoming engram"), so the final settled set
        # is re-persisted once through the same single-writer bridge the Consolidator uses (§I6) —
        # the sleep jobs ARE consolidation policy; no second write path exists.
        kept: list = []
        merges = 0
        for e in entries:
            keeper = next((k for k in kept
                           if k.kind == e.kind and _eg._overlap(e.body, k.body) >= cons.merge_threshold),
                          None)
            if keeper is None:
                kept.append(e)
            else:
                cons.merge(keeper, e)   # the one merge POLICY (provenance/strength/links carry-through)
                merges += 1
        if merges:
            _eg._commit_to_store(cons.store, kept)   # drop the absorbed twins (single writer, §I6)
        return {"merges": merges, "engrams": len(kept)}


class StrengthDecayPruneJob:
    """Synaptic downscaling (SHY, §2 line 101) + prune-to-budget (§M-3: every store bounded). Applies a
    small global strength decay to every long-term engram (the trivial fades, the repeatedly-credited is
    topped back up elsewhere by the bet ledger), then, if the store is over LONGTERM_BUDGET, prunes the
    LOWEST-strength engrams down to budget (interference-aware forgetting — strength is earned utility).
    All writes go through the Consolidator's single writer (§I6) via its module-level bridge."""
    name = "strength_decay_prune"
    priority = 20

    def __init__(self, *, decay: float = STRENGTH_DECAY_PER_SLEEP, budget: int = LONGTERM_BUDGET):
        self.decay = float(decay)
        self.budget = int(budget)

    def run(self, ctx: "SleepContext") -> dict:
        cons = ctx.consolidator
        if cons is None:
            return {"skipped": "no consolidator"}
        import engram as _eg
        entries = cons.store.load()
        if not entries:
            return {"decayed": 0, "pruned": 0, "engrams": 0}
        for e in entries:
            e.strength = max(0.0, min(1.0, e.strength - self.decay))
        pruned = 0
        if len(entries) > self.budget:
            # Keep the strongest `budget`; the weakest fall out of memory (forgetting is a feature).
            entries.sort(key=lambda e: e.strength, reverse=True)
            pruned = len(entries) - self.budget
            entries = entries[: self.budget]
        # Re-persist the decayed (and possibly pruned) set through the SINGLE writer (§I6).
        _eg._commit_to_store(cons.store, entries)
        return {"decayed": len(entries) + pruned, "pruned": pruned, "engrams": len(entries)}


# --- Grammar-constrained distillation (replaces compaction.py's regex-over-prose extraction) -----
# The distillation grammar CONSTRAINS the model to emit ONLY well-formed fact lines — the sampler
# cannot produce a malformed extraction, which is exactly the "structured extraction everywhere"
# principle (§M-6) that retires compaction's regex-over-prose. Each line is:
#     CATEGORY [tag, tag]: free text\n
# with CATEGORY drawn from a closed set that maps onto engram kinds. On the rare malformed line
# (e.g. an unconstrained fallback call), the parser DROPS IT WITH A LOG — never silently (the gate
# asserts zero silent drops).
_DISTILL_CATEGORIES = {
    "FACT": "fact",
    "ERROR": "error",
    "PROCEDURE": "procedure",
    "IDENTITY": "identity",
}


def build_distillation_grammar(*, max_facts: int = DISTILL_MAX_FACTS) -> str:
    """A GBNF grammar constraining distillation output to at most `max_facts` `CATEGORY [tags]: text`
    lines (or the literal `NONE`). Structural, in the house style of grammar.build_tick_grammar: the
    sampler literally cannot emit an unknown category or a malformed line."""
    cats = " | ".join(f'"{c}"' for c in sorted(_DISTILL_CATEGORIES))
    n = max(1, int(max_facts))
    return "\n".join([
        f'root ::= "NONE" | line line{{0,{n - 1}}}',
        'line ::= category " [" tags "] : " text "\\n"',
        f"category ::= {cats}",
        'tags ::= tag ( ", " tag )*',
        'tag ::= [a-z0-9_]+',
        # Fact text: any run of visible characters and spaces up to the newline; no control chars.
        'text ::= [^\\n\\x00-\\x1F] [^\\n\\x00-\\x1F]*',
    ])


# The extraction line, mirroring compaction's contract so the two are interchangeable during cutover.
_DISTILL_LINE = None
def _distill_line_re():
    global _DISTILL_LINE
    if _DISTILL_LINE is None:
        import re
        _DISTILL_LINE = re.compile(
            r"^(FACT|ERROR|PROCEDURE|IDENTITY)\s*\[([^\]]*)\]\s*:\s*(.+)$",
            re.IGNORECASE,
        )
    return _DISTILL_LINE


def parse_distillations(text: str) -> tuple:
    """Parse grammar-constrained distillation output into (facts, dropped).

    `facts` is a list of {category, tags, content} dicts; `dropped` is a list of the raw lines that did
    NOT match the contract. A well-formed (grammar-constrained) output yields zero drops; a malformed
    line is returned in `dropped` so the caller can LOG it — never silently swallowed (pitfall/gate:
    zero silently-dropped extractions)."""
    facts, dropped = [], []
    rex = _distill_line_re()
    for raw in (text or "").splitlines():
        line = raw.strip()
        if not line or line.upper() == "NONE":
            continue
        m = rex.match(line)
        if not m:
            dropped.append(line)
            continue
        cat = _DISTILL_CATEGORIES.get(m.group(1).upper(), "fact")
        tags = [t.strip().lower() for t in m.group(2).split(",") if t.strip()]
        content = m.group(3).strip()
        if content:
            facts.append({"category": cat, "tags": tags, "content": content})
        else:
            dropped.append(line)   # matched the shape but empty body — still a drop, still logged
    return facts, dropped


class DistillationJob:
    """Gist extraction (§2 line 103): distil raw observations into consolidated facts, GRAMMAR-CONSTRAINED
    (replacing compaction's regex-over-prose, §M-6). Each distilled fact becomes an engram with
    `provenance='dreamed'` and confidence CAPPED at DREAMED_CONFIDENCE_CAP (pitfall #5: a dream is a
    hypothesis until experience corroborates it), committed through the Consolidator (§I6). Malformed
    model output is DROPPED WITH A LOG and counted — never silently (the gate asserts zero silent drops).
"""
    name = "distillation"
    priority = 30

    def __init__(self, *, confidence_cap: float = DREAMED_CONFIDENCE_CAP,
                 max_facts: int = DISTILL_MAX_FACTS):
        self.confidence_cap = float(confidence_cap)
        self.max_facts = int(max_facts)

    # Category -> engram kind (identity/error/procedure/fact are all valid engram KINDS).
    _KIND = {"fact": "fact", "error": "error", "procedure": "procedure", "identity": "identity"}

    def run(self, ctx: "SleepContext") -> dict:
        cons = ctx.consolidator
        if cons is None:
            return {"skipped": "no consolidator"}
        if not ctx.observations:
            return {"distilled": 0, "dropped": 0, "note": "no observations"}
        if ctx.llm is None:
            # No mind available this sleep: distillation is a clean no-op, NOT a silent drop.
            return {"distilled": 0, "dropped": 0, "note": "no llm"}

        obs_text = self._format(ctx.observations)
        grammar = build_distillation_grammar(max_facts=self.max_facts)
        messages = [
            {"role": "system", "content":
             "You are the dreaming mind distilling raw observations into durable facts. Emit ONLY lines "
             "of the form `CATEGORY [tag, tag]: fact`, one per fact, CATEGORY in "
             "{FACT, ERROR, PROCEDURE, IDENTITY}. Emit `NONE` if nothing is worth keeping."},
            {"role": "user", "content": obs_text},
        ]
        try:
            output = ctx.llm(messages, grammar=grammar)
        except Exception as e:  # noqa: BLE001 - a distillation LLM fault is isolated by the engine guard,
            raise RuntimeError(f"distillation llm call failed: {e}") from e

        facts, dropped = parse_distillations(output or "")
        for line in dropped:
            # Malformed extraction: logged, never silently swallowed (the gate asserts this count).
            logger.warning("distillation dropped malformed line: %r", line)

        from engram import Engram
        committed = 0
        for f in facts[: self.max_facts]:
            kind = self._KIND.get(f["category"], "fact")
            body = f["content"]
            if f["tags"]:
                body = f"{body}  [tags: {', '.join(f['tags'])}]"
            try:
                eg = Engram(
                    kind=kind, body=body,
                    provenance="dreamed",                    # sleep-distilled hypothesis (§M-2 source monitoring)
                    confidence=min(self.confidence_cap, 0.7),  # capped: a dream is a hypothesis (pitfall #5)
                )
                eg.stats["dreamed"] = True                   # origin stamp so 2.3 can key the cap on it
                cons.commit(eg)
                committed += 1
            except Exception as e:  # noqa: BLE001 - one bad fact is dropped-with-log, not a job abort
                logger.warning("distillation could not commit fact %r: %s", body[:80], e)
                dropped.append(body)
        return {"distilled": committed, "dropped": len(dropped),
                "confidence_cap": self.confidence_cap}

    @staticmethod
    def _format(observations: list) -> str:
        lines = []
        for obs in observations:
            if isinstance(obs, dict):
                out = str(obs.get("output", ""))
                tool = obs.get("tool", "?")
                ok = "OK" if obs.get("success", False) else "FAIL"
                lines.append(f"[{tool} | {ok}] {out[:500]}")
            else:
                lines.append(str(obs)[:500])
        return "\n".join(lines) or "(no observations)"


class BackupSnapshotJob:
    """Call the existing 0.4 backup snapshot (backup.snapshot) — a durable tar.gz of workspace/ taken
    while the creature is quiescent (sleep is the safe moment to snapshot: nothing is mutating memory).
    Guarded like every job; a snapshot failure is isolated and the sleep still completes."""
    name = "backup_snapshot"
    priority = 40

    def run(self, ctx: "SleepContext") -> dict:
        try:
            import backup
        except Exception as e:  # noqa: BLE001
            return {"skipped": f"backup unavailable: {e}"}
        path = backup.snapshot(ctx.config)
        return {"snapshot": str(path)}


class TelemetryRederiveJob:
    """Re-derive the platform's DECLARED-DERIVABLE constants from live telemetry (§2.4: bounded steps,
    dashboard-visible). A derivable is any (name, deriver) pair whose value is a function of measured
    state rather than a hand-tuned knob (§0.4). This job recomputes at most TELEMETRY_MAX_STEPS of them
    and writes the results to workspace for the dashboard; it MUTATES no memory. With no derivers
    supplied it is a clean no-op — the seam is real, the deriver set lands with its telemetry source."""
    name = "telemetry_rederive"
    priority = 50

    def __init__(self, derivers: Optional[list] = None, *, max_steps: int = TELEMETRY_MAX_STEPS):
        # Each deriver: (name:str, fn: ctx -> value). Bounded to max_steps so the job is always finite.
        self.derivers = list(derivers or [])
        self.max_steps = int(max_steps)

    def run(self, ctx: "SleepContext") -> dict:
        derived = {}
        for name, fn in self.derivers[: self.max_steps]:
            try:
                derived[name] = fn(ctx)
            except Exception as e:  # noqa: BLE001 - one bad deriver is skipped, not a job abort
                logger.warning("telemetry rederive %s failed: %s", name, e)
        if derived:
            try:
                out = ctx.config.workspace / "state"
                out.mkdir(parents=True, exist_ok=True)
                (out / "derived_constants.json").write_text(
                    json.dumps(derived, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
            except OSError as e:
                logger.warning("telemetry rederive write failed: %s", e)
        return {"rederived": len(derived), "names": sorted(derived)}


class SkillRetireJob:
    """The skill half of §N-3's job "S": archive skills whose last use is older than the declared
    retirement window (skills.retire_unused_skills — flag-gated on the skill economy internally,
    idempotent, recoverable via rollback_skill). Sleep is the right beat for it: retirement is
    forgetting applied to the procedural store, and it belongs in the same offline pass that decays
    engrams. Re-SCORING is not faked here — trust promotion/demotion already settles live at each
    invocation (skills._record_invocation); this job only runs the time-based half."""
    name = "skill_retire"
    priority = 60

    def run(self, ctx: "SleepContext") -> dict:
        try:
            import skills as _skills
        except Exception as e:  # noqa: BLE001
            return {"skipped": f"skills unavailable: {e}"}
        retired = _skills.retire_unused_skills(ctx.config)
        return {"retired": len(retired), "names": sorted(retired)}


class CalibrationJob:
    """The Brier→temperament seam expectations.py exposes (§M-4: calibration → caution): score the
    creature's closed claim-bearing bets per domain, persist the calibration book for the dashboard,
    and — when the evidence says chronic mis-calibration — apply ONE bounded caution step. The
    temperament springs (4.3) relax it back toward the congenital baseline as calibration recovers,
    so this can pressure but never ratchet (pitfall #3)."""
    name = "calibration"
    priority = 65

    def __init__(self, *, min_closed: int = CALIBRATION_MIN_CLOSED,
                 brier_neutral: float = CALIBRATION_BRIER_NEUTRAL,
                 step_max: float = CALIBRATION_CAUTION_STEP_MAX):
        self.min_closed = int(min_closed)
        self.brier_neutral = float(brier_neutral)
        self.step_max = float(step_max)

    def run(self, ctx: "SleepContext") -> dict:
        if not getattr(ctx.config, "pillars_expectations_enabled", False):
            return {"skipped": "expectations dark"}
        from expectations import brier_calibration_by_domain
        cal = brier_calibration_by_domain(ctx.config)
        # Persist the calibration book (dashboard/dossier visibility), whether or not caution moves.
        try:
            out = ctx.config.state_dir
            out.mkdir(parents=True, exist_ok=True)
            (out / "calibration.json").write_text(
                json.dumps(cal, ensure_ascii=False, indent=2), encoding="utf-8")
        except OSError as e:
            logger.warning("calibration book write failed: %s", e)
        n = sum(d.get("n", 0) for d in cal.values())
        if n == 0:
            return {"domains": 0, "closed": 0, "caution_step": 0.0}
        # Exposure-weighted mean Brier: a domain with many closed bets carries its weight.
        brier = sum(d["brier"] * d["n"] for d in cal.values()) / n
        step = 0.0
        if (n >= self.min_closed and brier > self.brier_neutral
                and ctx.temperament is not None):
            # Excess mis-calibration in [0,1] scales the step, clamped at the declared bound.
            excess = (brier - self.brier_neutral) / (1.0 - self.brier_neutral)
            step = self.step_max * max(0.0, min(1.0, excess))
            t = ctx.temperament
            t.caution = t._toward(t.caution, 1.0, step)
            t.save()
        return {"domains": len(cal), "closed": n, "brier": round(brier, 4),
                "caution_step": round(step, 4)}


class OrganSleepHooksJob:
    """Run every organ's registered `on_sleep` hook (the 1.1 OrganRegistry), LAST, so the organs see the
    settled post-consolidation state. The registry already guards each hook (I5); this job wraps the
    whole sweep so even a registry-level fault is isolated by the sleep engine's guard too."""
    name = "organ_on_sleep"
    priority = 90

    def run(self, ctx: "SleepContext") -> dict:
        reg = ctx.organ_registry
        if reg is None:
            return {"skipped": "no organ registry"}
        reg.run_on_sleep(ctx)
        n = sum(1 for o in reg if getattr(o, "on_sleep", None) is not None)
        return {"organs_with_on_sleep": n}


def default_sleep_engine(*, decay: float = STRENGTH_DECAY_PER_SLEEP,
                         budget: int = LONGTERM_BUDGET,
                         confidence_cap: float = DREAMED_CONFIDENCE_CAP,
                         telemetry_derivers: Optional[list] = None) -> SleepEngine:
    """The stock digestive tract: dedup/merge → decay+prune (SHY) → distillation → backup → telemetry
    → skill retirement → calibration → organ on_sleep hooks. Jobs are priority-ordered inside the
    engine; this just wires the defaults."""
    return SleepEngine([
        DedupMergeJob(),
        StrengthDecayPruneJob(decay=decay, budget=budget),
        DistillationJob(confidence_cap=confidence_cap),
        BackupSnapshotJob(),
        TelemetryRederiveJob(telemetry_derivers or []),
        SkillRetireJob(),
        CalibrationJob(),
        OrganSleepHooksJob(),
    ])


def run_sleep(ctx: "SleepContext", *, engine: Optional[SleepEngine] = None,
              clear_adenosine: bool = True) -> SleepReport:
    """Run one full sleep pass and (for a NAP) clear adenosine — the creature wakes rested. This is
    the clean function eidos.py calls from inside the sleep window; it does NOT touch the tick loop.
    Adenosine is cleared AFTER the jobs run so a job that inspects sleep pressure still sees the
    pre-sleep value; when it clears, it clears unconditionally (even a partially-failed sleep still
    rests the body).

    `clear_adenosine=False` is the DREAM leg of the dream-vs-nap split: a context-compaction doze
    runs the memory jobs but does NOT rest the body — tiredness keeps accruing through dreams, so
    real naps still arrive on the nap curve instead of being reset every few minutes.

    DARK by config: with `pillars_sleep_engine_enabled` off (the default) this is a logged no-op
    returning an empty report — even an accidental early wiring changes nothing in the running system."""
    if not getattr(ctx.config, "pillars_sleep_engine_enabled", False):
        logger.info("sleep engine dark (pillars_sleep_engine_enabled off): no-op")
        return SleepReport()
    engine = engine or default_sleep_engine()
    report = engine.run(ctx)
    nm = ctx.neuromod
    if clear_adenosine and nm is not None and getattr(nm, "adenosine", None) is not None:
        nm.adenosine.clear()
    return report
