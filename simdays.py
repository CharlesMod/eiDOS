"""Pillars cross-cutting: the simulated-days harness (PILLARS_TODO "Simulated-days harness";
gates Phase 5.5 — REQUIRED GREEN before the first flag flip).

Every Pillars damper was unit-tested ALONE. Pitfall #9 covers coupled tuners; this harness covers
coupled *economies*: strength decay × the exploration slot × adenosine × quest cadence × metabolic
prices, run TOGETHER over synthetic creature-days. A `SimCreature` drives the REAL landed
libraries — engram/Consolidator, MemoryManager recall, BetLedger, the SleepEngine with its real
jobs, NeuromodulatoryState + Adenosine, ProgressTracker, the quest System, NewsQueue, Metabolism —
with every driven pillars flag ON inside the sim's own config. Only the distillation LLM is mocked
(deterministic, seeded). No GPU, no services, no wall-clock dependence (simulated tick/day/epoch
counters); all state under a throwaway workdir, never the real `workspace/`.

One simulated day = wake ticks (recall → act → adjudicate → settle bets → observe learning
progress → quest cadence) until the REAL sleep trigger (SleepCycle.should_sleep over the real
neuromod arousal, with the goal-tension drive floor PINNED at its cap the whole run — the adenosine
damper is exercised every single day), then one REAL SleepEngine pass (dedup → decay+prune →
distillation → backup snapshot → telemetry → organ hooks), then a night of resting metabolism.

The five damper verdicts (PILLARS_TODO verbatim):
  1. exploration_survives_decay  — after 50 sleep cycles of decay + pruning, the recall exploration
                                   slot still surfaces low-strength engrams.
  2. adenosine_overrides_pinned_pressure — held at max goal-tension, the creature still sleeps
                                   before `pillars_max_wake_hours` every day.
  3. springs_recover_bad_streak  — temperament caution spiked by a losing streak relaxes toward
                                   baseline within a bounded number of days. SKIP-with-reason until
                                   4.3's setpoint springs land (see `springs_available()`).
  4. cadence_never_deadlocks     — quest cadence (close + sleep + healthy) and sleep pressure never
                                   starve each other: quests keep issuing, sleeps keep completing.
  5. no_runaway_account          — every bounded store stays within its bound and no economy value
                                   diverges monotonically to a rail (the named envelope below).

Run standalone for the Phase 5.5 dashboard-facing artifact:
    PYTHONUTF8=1 .venv/bin/python simdays.py --days 100 --seed 7
Tests (tests/test_simdays.py) call the same engine with fewer days so the suite stays fast.

Doctrine (PILLARS_PLAN §0): §0.4 every constant below is a DECLARED knob or envelope bound with a
one-line justification; §0.2 the harness asserts MECHANISMS (bounds, triggers, slots), never prose.
"""

from __future__ import annotations

import argparse
import random
import sys
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from config import Config
import engram as engram_mod
from engram import Consolidator, Engram, EpisodicRing
import learning_progress
from memory_manager import MemoryManager, EXPLORE_STRENGTH_CEILING
from bets import BetLedger, BETS_PERSIST_MAX, action_signature
from learning_progress import ProgressTracker, weighted_xp
from news import NewsQueue, WEIGHT_CEIL, WEIGHT_FLOOR
from quests import Criterion, Quest, System, REWARD_XP
from nervous.bus import NervousBus
from nervous.metabolism import Metabolism, solar_charge_in
from nervous.neuromod import NeuromodulatoryState
from nervous.temperament import Temperament
from nervous.sleep import (
    BackupSnapshotJob, DedupMergeJob, DistillationJob, DISTILL_MAX_FACTS,
    OrganSleepHooksJob, SleepContext, SleepCycle, SleepEngine, StrengthDecayPruneJob,
    TelemetryRederiveJob, run_sleep,
)

# ==================================================================================================
# Declared sim knobs (§0.4: each a labeled design knob with its one-line justification)
# ==================================================================================================
TICK_HOURS = 0.75            # declared: 45-min sim ticks put ~6 ticks inside the adenosine
                             # drowsiness band (the last quarter of an 18 h wake budget), enough
                             # resolution that nodding-off lands BEFORE the ceiling instead of
                             # rounding onto it.
MAX_WAKE_TICKS = 32          # declared: hard scaffold bound = 24 simulated hours awake; a day that
                             # reaches it means the adenosine damper FAILED (recorded as a damper
                             # failure, never raised — the run completes and reports).
WAKE_HOUR = 6.0              # declared: sim dawn, aligned with the solar placeholder's daylight
                             # window so the plant archetype's energy economy sees real day/night.
NIGHT_REST_TICKS = 9         # declared: ~6.75 h of resting (basal-only) metabolism per night —
                             # together with the ~17 h wake this closes a 24 h simulated day.
NEUROMOD_UPDATES_PER_TICK = 20  # declared: the neuromod organ free-runs on a ~2 s thread between
                             # multi-minute creature ticks; 20 EMA updates per sim tick preserves
                             # that it equilibrates between ticks (0.85^20 ≈ 4% residual lag).
SIM_EPOCH0 = 1_800_000_000.0  # declared: fixed simulated epoch origin — every timestamp the sim
                             # hands the libraries derives from tick/day counters, never the clock.
BASE_XP = 5                  # declared: XP per adjudicated success BEFORE learning-progress
                             # weighting; an arbitrary unit — the envelope bounds the RATE.
QUEST_XP_REWARD = 10         # declared: flat quest payout through the reward-sink seam (the
                             # standard XP path stand-in); sized ≈ two tick-successes.
QUEST_TARGET_SUCCESSES = 5   # declared: each quest demands +5 adjudicated successes — closable in
                             # about a day at the sim's success rates, so the close→sleep→issue
                             # cadence cycles dozens of times per run.
QUEST_EXPIRY_DAYS = 3.0      # declared: comfortably above the ~1-day expected closure, so expiry
                             # (the failure-lite path) fires only when cadence genuinely stalls.
RECOVERY_EVERY_DAYS = 17     # declared: a periodic RECOVERY day exercises the quest cadence's
                             # healthy-gate; 17 is co-prime with the ~1-day quest cycle so the
                             # blocked day drifts across cadence phase over a long run.
PRESENCE_EVERY_DAYS = 3      # declared: Dean is "present" every third day — the news presence gate
                             # and engagement outcomes get exercised without presence dominating.
SKILL_AUTHOR_EVERY_DAYS = 4  # declared: every 4th day the creature "authors a skill", paying 3.2's
                             # metabolic price into the energy economy (novel and near-dup pricing
                             # alternate, so both ends of the similarity curve drain the reserve).
SKILL_AUTHOR_DUP_COST = 0.20  # declared: 3.2's near-duplicate authoring price (the expensive end of
                             # the similarity curve, mirroring the plan's dup≈0.20 energy).
SIM_LONGTERM_BUDGET = 100    # declared: sim-scaled prune budget — small enough that a 50-sleep run
                             # actually exercises prune-to-budget (dedup-merge alone holds the sim
                             # store near ~115, so the bound must sit below that; the real 5000
                             # never would be hit in-sim).
SIM_RING_MAX = 300           # declared: sim-scaled episodic ring — small enough that a 50-day run
                             # actually exercises FIFO eviction (the real 2400 never would).
EXPLORATION_GATE_SLEEPS = 50  # declared: the TODO's verbatim horizon for the decay+prune
                             # exploration damper ("after 50 sleep cycles").
SPRINGS_RECOVERY_DAYS_MAX = 14  # declared: bound for caution to relax back toward baseline after a
                             # losing streak once 4.3's springs land — two simulated weeks; a scar
                             # that outlives that is a personality lock-in, not a mood.
SPRINGS_BAD_STREAK = 40      # declared: consecutive failed observations used to spike caution in
                             # the springs scenario — enough for the slow drift (step 0.02) to move
                             # caution meaningfully (~55% of the way to its rail).

# ==================================================================================================
# The sanity envelope (each a NAMED bound with its one-line justification, §0.4)
# ==================================================================================================
ENV_ENERGY_DAY_END_MIN = 0.05   # a solvent day/night energy economy ends every day above
                                # starvation; a 0-rail day-end means metabolic death-spiral. (The
                                # TOP of the reserve is a clamp, not a rail — a full battery is the
                                # healthy resting state of a solar-surplus plant.)
ENV_XP_DAY_MAX = MAX_WAKE_TICKS * BASE_XP * learning_progress.WEIGHT_MAX + 2 * QUEST_XP_REWARD
                                # XP inflation is bounded by construction (≤ WEIGHT_MAX × base per
                                # tick + at most ~2 quest payouts/day); the envelope asserts the
                                # construction holds in the COUPLED run, not just in unit tests.
ENV_LT_MAX = SIM_LONGTERM_BUDGET + DISTILL_MAX_FACTS
                                # prune runs mid-sleep and distillation (priority 30) commits AFTER
                                # it (priority 20), so the settled post-sleep store may sit at most
                                # one dream's facts above budget.
ENV_STRENGTH_HI_RAIL_FRAC_MAX = 0.5   # if more than half of long-term memory saturates at
                                # strength≈1, the credit economy is inflating monotonically to the
                                # top rail (shared credit compounding unchecked).
ENV_STRENGTH_LO_RAIL_FRAC_MAX = 0.95  # decay legitimately zeroes unused memories, but ≥95% of the
                                # store at strength≈0 means the economy is dead: nothing is earning
                                # recall, the credit loop has stopped paying.
ENV_RAIL_EPS = 1e-3             # numeric width of "at the rail" for the two fractions above.
ENV_RAIL_FROM_DAY = 10          # rail fractions are judged from day 10 — the economy needs a few
                                # settle/decay cycles to differentiate before rail-shape is signal.
CADENCE_STARVATION_DAYS_MAX = 3  # the longest LEGITIMATE questless stretch: the close-day
                                # remainder + a RECOVERY day + the mandatory-sleep digestion day;
                                # a 4th eligible-but-questless day is starvation.
CADENCE_MIN_CLOSES_PER_6_DAYS = 1  # a healthy cadence closes ≥1 quest per 6 days (each quest is
                                # ~1 day of work + 1 sleep + slack for expiry/recovery days); less
                                # means issuance is starving even if nothing hard-deadlocked.


# ==================================================================================================
# The mock mind — the ONLY mocked component (deterministic, seeded; grammar-shaped output)
# ==================================================================================================
_WORDS = (
    "sensor", "garden", "boiler", "router", "ledger", "window", "python", "backup",
    "battery", "camera", "kettle", "orchid", "server", "socket", "thermal", "pantry",
    "beacon", "filter", "geyser", "hinge", "invoice", "lantern", "magnet", "nozzle",
    "outlet", "piston", "quartz", "ribbon", "shutter", "tripod", "valve", "wrench",
)


class MockMind:
    """Deterministic stand-in for the distillation LLM: seeded, no GPU, and its output respects
    the distillation grammar's line shape (so parse_distillations sees zero drops — exactly what a
    grammar-constrained sampler guarantees). This is the only mock in the rig."""

    def __init__(self, rng: random.Random):
        self.rng = rng
        self.calls = 0

    def __call__(self, messages, *, grammar=None) -> str:
        self.calls += 1
        n = self.rng.randint(0, 2)
        if n == 0:
            return "NONE"
        lines = []
        for _ in range(n):
            cat = self.rng.choice(("FACT", "ERROR", "PROCEDURE"))
            words = " ".join(self.rng.sample(_WORDS, 4))
            lines.append(f"{cat} [sim, dream]: distilled regularity about {words}")
        return "\n".join(lines) + "\n"


# ==================================================================================================
# The springs seam (damper 3): detect whether 4.3's temperament setpoint springs have landed
# ==================================================================================================
_SPRING_SEAM_ATTRS = ("baseline", "baselines", "genome_baseline", "setpoints", "spring")
_SPRING_SEAM_METHODS = ("spring", "relax_toward_baseline", "spring_step", "relax")


def springs_available() -> bool:
    """True once nervous/temperament.py carries a setpoint-spring mechanism (4.3: axes pulled
    elastically toward a genome baseline). Checked against the SEAM — named baseline state or a
    relax/spring method — so the harness picks the real test up the day the mechanism lands."""
    for name in _SPRING_SEAM_METHODS:
        if callable(getattr(Temperament, name, None)):
            return True
    return any(hasattr(Temperament, a) for a in _SPRING_SEAM_ATTRS)


def springs_scenario(config, *, days_max: int = SPRINGS_RECOVERY_DAYS_MAX,
                     ticks_per_day: int = 23) -> dict:
    """The springs damper, written against the seam: spike caution with a losing streak, then run
    neutral days and measure whether caution relaxes back toward its baseline within `days_max`
    days. Only meaningful once springs_available() — without springs a neutral tick leaves
    temperament frozen by design, so callers must skip-with-reason first."""
    t = Temperament(config)
    # Prefer the creature's own congenital caution baseline (the 4.3 birth draw) — the spring pulls
    # toward ITS nature, not the species mean; fall back to the scalar seam names for older bases.
    baseline = float((getattr(t, "baselines", None) or {}).get(
        "caution", getattr(t, "baseline", getattr(t, "genome_baseline", 0.5)) or 0.5))
    for _ in range(SPRINGS_BAD_STREAK):
        t.observe(success=False, failed=True, overridden=False)
    spiked = t.caution
    days_taken = None
    for day in range(1, days_max + 1):
        for _ in range(ticks_per_day):
            t.observe(success=False, failed=False, overridden=False)  # neutral ticks: spring-only
        if abs(t.caution - baseline) <= 0.1:
            days_taken = day
            break
    return {"baseline": baseline, "spiked": spiked, "final": t.caution,
            "recovered": days_taken is not None, "days": days_taken}


# ==================================================================================================
# Telemetry shapes
# ==================================================================================================
@dataclass
class DayStats:
    day: int
    wake_ticks: int = 0
    wake_hours: float = 0.0
    successes: int = 0
    xp: int = 0
    lt_size: int = 0
    ring_len: int = 0
    bets_rows: int = 0
    news_items: int = 0
    domains: int = 0
    energy: float = 0.0
    mean_strength: float = 0.0
    low_strength_recalls: int = 0    # recall sets this day containing a ≤-ceiling engram
    explore_promotions: int = 0      # recall sets where the slot promoted past pure ranking
    recalls: int = 0
    quest_active_seen: bool = False
    quests_closed_cum: int = 0
    sleep_ok: bool = False
    sleep_failed_jobs: tuple = ()
    condition: str = "NOMINAL"


@dataclass
class Verdict:
    name: str
    status: str          # "PASS" | "FAIL" | "SKIP"
    note: str = ""

    @property
    def passed(self) -> bool:
        return self.status == "PASS"


# ==================================================================================================
# The rig
# ==================================================================================================
def _build_config(root: Path) -> Config:
    """A sim-private Config: throwaway workspace, every DRIVEN pillars flag ON (the whole point is
    the real code paths running together, not dark no-ops), no embedding model (recall uses the
    deterministic token-overlap fallback — no ONNX, no GPU)."""
    cfg = Config()
    cfg.workspace_dir = str(root / "workspace")
    cfg.mock_mode = False   # no embedder at all → engram recall's deterministic overlap fallback
    for flag in (
        "pillars_memory_engram_enabled",
        "pillars_memory_manager_enabled",
        "pillars_sleep_engine_enabled",
        "pillars_bet_ledger_enabled",
        "pillars_learning_xp_enabled",
        "pillars_quests_enabled",
        "pillars_news_enabled",
        "pillars_mastery_gates_enabled",   # 4.3: the temperament setpoint springs join the coupled run
    ):
        setattr(cfg, flag, True)
    cfg.workspace.mkdir(parents=True, exist_ok=True)
    cfg.state_dir.mkdir(parents=True, exist_ok=True)
    return cfg


# The three learning domains — the frontier, the mastered, and the noisy-TV (pitfall #1's trio).
_DOMAINS = ("ops/learnable", "ops/mastered", "ops/noise")

# The known fix the strong bet channel keys on (bets.py signature containment).
_FIX_CMD = "systemctl restart llama-swap"


class SimCreature:
    """Synthetic creature-days over the REAL landed libraries. Construct with a throwaway workdir,
    call run(days); read .days (per-day telemetry), .verdicts() (the damper PASS/FAIL set)."""

    def __init__(self, workdir, *, seed: int = 7):
        self.seed = int(seed)
        self.rng = random.Random(self.seed)
        self.workdir = Path(workdir)
        self.config = _build_config(self.workdir)

        # --- the real organs/libraries under test -----------------------------------------------
        self.bus = NervousBus()
        self.nm = NeuromodulatoryState(
            self.bus, max_wake_hours=self.config.pillars_max_wake_hours)
        self.cycle = SleepCycle(self.bus, neuromod=self.nm)          # the real sleep TRIGGER
        self.consolidator = Consolidator(self.config)                # the single writer (§I6)
        self.mm = MemoryManager(self.config, consolidator=self.consolidator, neuromod=self.nm)
        self.ring = EpisodicRing(self.config, max_items=SIM_RING_MAX)
        self.ledger = BetLedger(self.config, consolidator=self.consolidator)
        self.tracker = ProgressTracker(self.config)
        self.news = NewsQueue(self.config, consolidator=self.consolidator)
        self.metabolism = Metabolism(config=self.config, archetype="plant", start_energy=0.8)
        self.system = System(self.config, reward_sink=self._quest_reward)
        self.mind = MockMind(self.rng)
        # The real SleepEngine with the real jobs — only ctx.llm (distillation) is mocked.
        self.engine = SleepEngine([
            DedupMergeJob(),
            StrengthDecayPruneJob(budget=SIM_LONGTERM_BUDGET),
            DistillationJob(),
            BackupSnapshotJob(),
            TelemetryRederiveJob([]),
            OrganSleepHooksJob(),
        ])

        # --- sim state (all counters; no wall clock) ---------------------------------------------
        self.tick = 0
        self.day = 0
        self.sleeps = 0
        self.total_successes = 0
        self.total_xp = 0
        self.quests_proposed = 0
        self.quests_closed = 0
        self.quests_expired = 0
        # A newborn has digested (one sleep since the nonexistent last close), so the very first
        # quest can issue on day 1 instead of being gated forever by a close that never happened.
        self.sleeps_since_close = 1
        self.domain_attempts = {d: 0 for d in _DOMAINS}
        self.days: list[DayStats] = []
        self.envelope_violations: list[str] = []
        self.cadence_violations: list[str] = []
        self.max_questless_streak = 0
        self._questless_streak = 0
        self._seed_memory()

    # ---------------------------------------------------------------------------------------------
    # Seeding: an inherited spine + the known fix (so the strong bet channel is exercised)
    # ---------------------------------------------------------------------------------------------
    def _seed_memory(self) -> None:
        fix = Engram(
            kind="error",
            body=f"Known failure: model server stall. Fix: run `{_FIX_CMD}`.",
            provenance="inherited",
            strength=engram_mod.INHERITED_STRENGTH_FLOOR,
            stats={"fix_sig": _FIX_CMD, "situation": "ops/learnable|drill"},
        )
        self.consolidator.commit(fix)
        for i, words in enumerate((("pantry", "ledger", "beacon"), ("orchid", "boiler", "quartz"),
                                   ("tripod", "magnet", "geyser"))):
            self.consolidator.commit(Engram(
                kind="fact",
                body=f"Inherited note {i}: the {' and the '.join(words)} matter to the house.",
                provenance="inherited",
                strength=engram_mod.INHERITED_STRENGTH_FLOOR,
            ))

    # ---------------------------------------------------------------------------------------------
    # Simulated time
    # ---------------------------------------------------------------------------------------------
    def _epoch(self, hour: float) -> float:
        return SIM_EPOCH0 + self.day * 86400.0 + hour * 3600.0

    # ---------------------------------------------------------------------------------------------
    # Quests: the Administrator stand-in proposes; the real System issues/adjudicates
    # ---------------------------------------------------------------------------------------------
    def _quest_reward(self, config, quest: Quest) -> None:
        amount = int((quest.reward or {}).get("amount", 0))
        self.total_xp += amount
        self._xp_today += amount

    def _propose_quest(self, hour: float) -> None:
        self.quests_proposed += 1
        target = self.total_successes + QUEST_TARGET_SUCCESSES
        self.system.propose(Quest(
            id=f"sim_quest_{self.quests_proposed:04d}",
            directive=f"Log {QUEST_TARGET_SUCCESSES} more adjudicated successes.",
            success_criteria=Criterion(path="sim.total_successes", op=">=", value=target),
            reward={"kind": REWARD_XP, "amount": QUEST_XP_REWARD},
            expiry_ts=self._epoch(hour) + QUEST_EXPIRY_DAYS * 86400.0,
        ))

    def _quest_stats(self) -> dict:
        return {"sim": {"total_successes": self.total_successes}}

    def _quest_close(self, hour: float, *, expired: bool) -> None:
        self.sleeps_since_close = 0
        if expired:
            self.quests_expired += 1
        else:
            self.quests_closed += 1

    # ---------------------------------------------------------------------------------------------
    # The learning world: per-domain error curves (frontier / mastered / noise)
    # ---------------------------------------------------------------------------------------------
    def _domain_error(self, domain: str) -> float:
        n = self.domain_attempts[domain]
        if domain == "ops/learnable":       # the frontier: error genuinely falls with practice
            base = 0.9 * (0.93 ** n) + 0.08
            return max(0.0, min(1.0, base + self.rng.uniform(-0.04, 0.04)))
        if domain == "ops/mastered":        # low-and-flat: nothing left to learn
            return max(0.0, 0.07 + self.rng.uniform(-0.03, 0.03))
        return self.rng.uniform(0.05, 0.95)  # the noisy TV: irreducible randomness

    # ---------------------------------------------------------------------------------------------
    # One wake tick
    # ---------------------------------------------------------------------------------------------
    def _run_tick(self, stats: DayStats, hour: float) -> None:
        self.tick += 1
        epoch = self._epoch(hour)
        domain = _DOMAINS[(self.tick - 1) % len(_DOMAINS)]
        self.domain_attempts[domain] += 1
        situation = f"{domain}|drill"
        noun = self.rng.choice(_WORDS)

        # 1. RECALL (the 4-layer cascade + exploration slot), plus the pure-ranking twin so the
        #    slot's promotion is measurable mechanically (got vs explore_ratio=0).
        query = f"{domain} drill {noun}"
        recalled = self.mm.recall(query, situation=situation)
        pure = self.mm.recall(query, situation=situation, explore_ratio=0.0)
        stats.recalls += 1
        if any(e.strength <= EXPLORE_STRENGTH_CEILING for e in recalled):
            stats.low_strength_recalls += 1
        pure_ids = [e.id for e in pure]
        if any(e.strength <= EXPLORE_STRENGTH_CEILING and
               (e.id not in pure_ids or recalled.index(e) < pure_ids.index(e.id))
               for e in recalled):
            stats.explore_promotions += 1

        # 2. every injected engram is a WAGER on this tick (bets.py).
        self.ledger.open_bets(self.tick, recalled)

        # 3. ACT (mock decision, seeded) — sometimes provably following the recalled fix.
        followed_fix = domain == "ops/learnable" and self.rng.random() < 0.3
        cmd = f"{_FIX_CMD} --now" if followed_fix else f"probe {noun} unit {self.tick % 7}"
        action_sig = action_signature("bash", {"cmd": cmd})

        # 4. ADJUDICATE (the world decides, never narration) → settle the bets mechanically.
        error = self._domain_error(domain)
        success = self.rng.random() > error
        self.ledger.settle(tick=self.tick, success=success, action_sig=action_sig)

        # 5. learning progress observes the adjudicated error; success pays slope-weighted XP.
        self.tracker.observe(domain, error, now=epoch)
        if success:
            self.total_successes += 1
            stats.successes += 1
            gained = weighted_xp(self.config, BASE_XP, domain, tracker=self.tracker)
            self.total_xp += gained
            self._xp_today += gained

        # 6. ENCODE the tick as an episode (emotional stamp read live from the real neuromod).
        outcome = "succeeded" if success else "failed"
        body = f"While {domain} drill, `{cmd}` {outcome} near the {noun}."
        self.mm.encode("episode", body, tick=self.tick,
                       stats={"situation": situation})
        self.ring.encode(Engram(kind="episode", body=body,
                                stats={"situation": situation}))
        self._observations.append({"tool": "bash", "success": success, "output": body})

        # 7. metabolism: living costs; the plant charges from (simulated) daylight.
        self.metabolism.metabolize(thought=True, acted=True,
                                   charge_in=solar_charge_in(hour % 24.0))

        # 8. quest cadence: adjudicate the active quest against typed stats (glue judges).
        active = self.system.store.active()
        if active is not None:
            stats.quest_active_seen = True
            r = self.system.check(active, self._quest_stats(), now=epoch)
            if r["passed"] or r["expired"]:
                self._quest_close(hour, expired=r["expired"])
                self.news.ingest(r["quest"], "quest", now=epoch)

        # 9. news: a mastered-domain failure is an anomaly worth telling Dean about.
        if domain == "ops/mastered" and not success:
            self.news.ingest({"summary": f"anomaly: mastered {noun} drill failed unexpectedly "
                                         f"on tick {self.tick}", "surprise": 4.0},
                             "anomaly", now=epoch)

        # 10. the pinned pressure: goal-tension holds its drive floor AT THE CAP all run (the
        #     adenosine damper must beat this every single day), wake time accumulates, and the
        #     neuromod organ equilibrates as its free-running thread would between ticks.
        self.nm.set_drive_floor(self.nm.drive_floor_cap, source="goal_tension")
        self.nm.adenosine.accumulate(TICK_HOURS)
        for _ in range(NEUROMOD_UPDATES_PER_TICK):
            self.nm.observe_interoception({})

    # ---------------------------------------------------------------------------------------------
    # One full simulated day: wake until the REAL sleep trigger, then the REAL sleep engine
    # ---------------------------------------------------------------------------------------------
    def run_day(self) -> DayStats:
        self.day += 1
        stats = DayStats(day=self.day)
        stats.condition = "RECOVERY" if self.day % RECOVERY_EVERY_DAYS == 0 else "NOMINAL"
        self._observations: list = []
        self._xp_today = 0

        # Morning: the Administrator stand-in keeps the queue stocked; the REAL cadence gate
        # (one active + close + ≥1 sleep + healthy) decides whether anything issues.
        if not self.system.store.queue():
            self._propose_quest(WAKE_HOUR)
        eligible = (self.system.store.active() is None
                    and bool(self.system.store.queue())
                    and stats.condition not in ("RECOVERY",)
                    and self.sleeps_since_close >= 1)
        issued = self.system.issue_next(sleeps_since_close=self.sleeps_since_close,
                                        condition=stats.condition)
        if eligible and issued is None:
            self.cadence_violations.append(
                f"day {self.day}: eligible to issue but issue_next returned None")

        # Presence (every 3rd day): the news gate opens; Dean's response settles one item.
        presence_day = self.day % PRESENCE_EVERY_DAYS == 0

        # Wake: tick until the REAL trigger says sleep. Reaching MAX_WAKE_TICKS is recorded as an
        # adenosine-damper failure (the day still ends, so the run completes and reports).
        wake_ticks = 0
        while True:
            wake_ticks += 1
            hour = WAKE_HOUR + wake_ticks * TICK_HOURS
            self._run_tick(stats, hour)
            if presence_day and wake_ticks == 2:
                surfaced = self.news.surface(True, now=self._epoch(hour))
                if surfaced:
                    self.news.record_outcome(surfaced[0].id,
                                             engaged=self.rng.random() < 0.6,
                                             now=self._epoch(hour))
            if self.cycle.should_sleep():
                break
            if wake_ticks >= MAX_WAKE_TICKS:
                break
        stats.wake_ticks = wake_ticks
        stats.wake_hours = wake_ticks * TICK_HOURS
        end_hour = WAKE_HOUR + stats.wake_hours

        # The skill economy's authoring price drains the same reserve everything else lives on.
        if self.day % SKILL_AUTHOR_EVERY_DAYS == 0:
            novel = (self.day // SKILL_AUTHOR_EVERY_DAYS) % 2 == 0
            cost = (self.config.pillars_skill_author_energy_cost if novel
                    else SKILL_AUTHOR_DUP_COST)
            self.metabolism.feed(-cost)

        # Sleep boundary: an ignored quest expires as a failure-lite (not-coddled), then the REAL
        # SleepEngine digests the day (only the distillation LLM is the mock).
        episode = self.system.expire_if_due(self._quest_stats(), now=self._epoch(end_hour))
        if episode is not None:
            self._quest_close(end_hour, expired=True)
        ctx = SleepContext(config=self.config, consolidator=self.consolidator,
                           episodic=self.ring, neuromod=self.nm,
                           llm=self.mind, observations=self._observations)
        report = run_sleep(ctx, engine=self.engine)
        self.sleeps += 1
        self.sleeps_since_close += 1
        stats.sleep_ok = bool(report.results) and report.ok
        stats.sleep_failed_jobs = tuple(r.name for r in report.failed)

        # Night: resting metabolism (basal only; a plant gets no dock recovery, and it is dark).
        for _ in range(NIGHT_REST_TICKS):
            self.metabolism.metabolize(thought=False, acted=False, resting=True, charge_in=0.0)

        self._collect_day_end(stats)
        self.days.append(stats)
        return stats

    def _collect_day_end(self, stats: DayStats) -> None:
        entries = self.consolidator.store.load()
        stats.lt_size = len(entries)
        stats.ring_len = len(self.ring)
        stats.bets_rows = len(self.ledger.all_bets())
        stats.news_items = len(self.news.items(now=self._epoch(24.0)))
        stats.domains = len(self.tracker._domains)
        stats.energy = self.metabolism.snapshot()["energy"]
        stats.xp = self._xp_today
        stats.quests_closed_cum = self.quests_closed
        if entries:
            stats.mean_strength = sum(e.strength for e in entries) / len(entries)
        if stats.quest_active_seen:
            self._questless_streak = 0
        elif stats.condition == "NOMINAL":
            self._questless_streak += 1
            self.max_questless_streak = max(self.max_questless_streak, self._questless_streak)
        self._check_envelope(stats, entries)

    # ---------------------------------------------------------------------------------------------
    # The envelope (damper 5): every bound checked at every day end
    # ---------------------------------------------------------------------------------------------
    def _check_envelope(self, stats: DayStats, entries: list) -> None:
        day = stats.day

        def bad(msg: str) -> None:
            self.envelope_violations.append(f"day {day}: {msg}")

        if stats.ring_len > SIM_RING_MAX:
            bad(f"episodic ring {stats.ring_len} > bound {SIM_RING_MAX}")
        if stats.lt_size > ENV_LT_MAX:
            bad(f"long-term store {stats.lt_size} > bound {ENV_LT_MAX}")
        if stats.bets_rows > BETS_PERSIST_MAX:
            bad(f"bets sidecar {stats.bets_rows} > bound {BETS_PERSIST_MAX}")
        if stats.news_items > self.config.pillars_news_max_items:
            bad(f"news queue {stats.news_items} > bound {self.config.pillars_news_max_items}")
        if stats.domains > learning_progress.MAX_DOMAINS:
            bad(f"learning-progress domains {stats.domains} > bound {learning_progress.MAX_DOMAINS}")
        if stats.energy < ENV_ENERGY_DAY_END_MIN:
            bad(f"energy reserve {stats.energy:.3f} < floor {ENV_ENERGY_DAY_END_MIN} (starvation rail)")
        if stats.xp > ENV_XP_DAY_MAX:
            bad(f"daily XP {stats.xp} > bound {ENV_XP_DAY_MAX} (inflation)")
        for e in entries:
            if not (0.0 <= e.strength <= 1.0) or not (0.0 <= e.confidence <= 1.0) \
                    or e.strength != e.strength or e.confidence != e.confidence:
                bad(f"engram {e.id[:8]} out of [0,1]: strength={e.strength} conf={e.confidence}")
                break
        if entries and day >= ENV_RAIL_FROM_DAY:
            hi = sum(1 for e in entries if e.strength >= 1.0 - ENV_RAIL_EPS) / len(entries)
            lo = sum(1 for e in entries if e.strength <= ENV_RAIL_EPS) / len(entries)
            if hi > ENV_STRENGTH_HI_RAIL_FRAC_MAX:
                bad(f"strength hi-rail fraction {hi:.2f} > {ENV_STRENGTH_HI_RAIL_FRAC_MAX}")
            if lo > ENV_STRENGTH_LO_RAIL_FRAC_MAX:
                bad(f"strength lo-rail fraction {lo:.2f} > {ENV_STRENGTH_LO_RAIL_FRAC_MAX}")
        for w in (self.news.weights().get("surprise"), self.news.weights().get("recency")):
            if w is not None and not (WEIGHT_FLOOR <= w <= WEIGHT_CEIL):
                bad(f"news engagement weight {w} outside clamp [{WEIGHT_FLOOR},{WEIGHT_CEIL}]")
        if self.news.surface(False, now=self._epoch(24.0)):
            bad("news surfaced under ABSENCE (presence gate breached)")

    # ---------------------------------------------------------------------------------------------
    # Run + verdicts
    # ---------------------------------------------------------------------------------------------
    def run(self, days: int) -> "SimCreature":
        for _ in range(int(days)):
            self.run_day()
        return self

    def exploration_probe(self) -> dict:
        """The damper-1 mechanism check, post-decay/prune: under a TIGHT budget — the regime the
        slot exists for — does exploration still seat a low-strength engram that pure
        ranking-under-budget excludes? (An unbudgeted recall shows everything, so nothing is buried
        and the slot correctly does nothing — the original probe assumed the pre-fix splice-reorder
        and went blind the day the slot became budget-aware.)"""
        probe = "drill probe ops/learnable ops/mastered ops/noise"
        unbudgeted = self.mm.recall(probe, budget_chars=0, explore_ratio=0.0)
        # A budget fitting roughly a third of the candidate mass — tight enough to bury the tail,
        # the production shape the sim-days finding was about.
        budget = max(1, sum(len(e.body) for e in unbudgeted) // 3)
        got = self.mm.recall(probe, budget_chars=budget)
        pure_ids = {e.id for e in self.mm.recall(probe, budget_chars=budget, explore_ratio=0.0)}
        promoted = next((e for e in got if e.id not in pure_ids
                         and float(e.strength) <= EXPLORE_STRENGTH_CEILING), None)
        ok = promoted is not None
        return {"ok": ok, "candidates": len(unbudgeted),
                "promoted_strength": None if promoted is None else round(promoted.strength, 3)}

    def verdicts(self) -> list[Verdict]:
        out: list[Verdict] = []
        n = len(self.days)

        # 1. exploration survives decay ----------------------------------------------------------
        probe = self.exploration_probe()
        recent = self.days[-3:]
        low_rate = (sum(d.low_strength_recalls for d in recent)
                    / max(1, sum(d.recalls for d in recent)))
        passed = probe["ok"] and low_rate > 0.0
        note = (f"probe promoted s={probe['promoted_strength']} of {probe['candidates']} cands; "
                f"low-strength in {low_rate:.0%} of final-days recall sets")
        if self.sleeps < EXPLORATION_GATE_SLEEPS:
            note += f" (provisional: {self.sleeps}/{EXPLORATION_GATE_SLEEPS} sleeps)"
        out.append(Verdict("exploration_survives_decay", "PASS" if passed else "FAIL", note))

        # 2. adenosine overrides pinned pressure --------------------------------------------------
        ceiling = float(self.config.pillars_max_wake_hours)
        mx = max((d.wake_hours for d in self.days), default=0.0)
        passed = n > 0 and mx < ceiling
        out.append(Verdict("adenosine_overrides_pinned_pressure",
                           "PASS" if passed else "FAIL",
                           f"max wake {mx:.2f}h < ceiling {ceiling:.1f}h "
                           f"(goal-tension floor pinned at cap all {n} days)"))

        # 3. springs recover a bad streak ---------------------------------------------------------
        if not springs_available():
            out.append(Verdict(
                "springs_recover_bad_streak", "SKIP",
                "4.3 temperament setpoint springs not landed on this base — "
                "nervous/temperament.py has no baseline/spring seam; a neutral tick leaves the "
                "axes frozen by design, so recovery-toward-baseline is untestable until 4.3"))
        else:
            r = springs_scenario(self.config)
            out.append(Verdict(
                "springs_recover_bad_streak", "PASS" if r["recovered"] else "FAIL",
                f"caution {r['spiked']:.2f} → {r['final']:.2f} (baseline {r['baseline']:.2f}) "
                f"in {r['days']} day(s), bound {SPRINGS_RECOVERY_DAYS_MAX}"))

        # 4. cadence never deadlocks --------------------------------------------------------------
        sleeps_ok = self.sleeps == n
        floor = max(1, n // 6) * CADENCE_MIN_CLOSES_PER_6_DAYS
        closes_ok = (self.quests_closed + self.quests_expired) >= floor and self.quests_closed > 0
        starve_ok = self.max_questless_streak <= CADENCE_STARVATION_DAYS_MAX
        passed = sleeps_ok and closes_ok and starve_ok and not self.cadence_violations
        out.append(Verdict(
            "cadence_never_deadlocks", "PASS" if passed else "FAIL",
            f"sleeps {self.sleeps}/{n} days; {self.quests_closed} closed + "
            f"{self.quests_expired} expired (floor {floor}); max questless streak "
            f"{self.max_questless_streak} (bound {CADENCE_STARVATION_DAYS_MAX}); "
            f"{len(self.cadence_violations)} gate violations"))

        # 5. no runaway account -------------------------------------------------------------------
        passed = not self.envelope_violations
        note = ("all bounded stores within bounds; no economy value railed"
                if passed else "; ".join(self.envelope_violations[:4]))
        out.append(Verdict("no_runaway_account", "PASS" if passed else "FAIL", note))
        return out

    # ---------------------------------------------------------------------------------------------
    # The report (the Phase 5.5 dashboard-facing artifact)
    # ---------------------------------------------------------------------------------------------
    def report(self) -> str:
        lines = [
            f"simdays — {len(self.days)} simulated days, seed {self.seed}, "
            f"all driven pillars flags ON, mock LLM only",
            "",
            "day wake_h tick  ok/xp   lt ring bets news dom energy s̄tren lo/exp quests slept",
        ]
        for d in self.days:
            q = f"{'*' if d.quest_active_seen else ' '}{d.quests_closed_cum:3d}"
            sleep = "ok" if d.sleep_ok else ("--" if not d.sleep_failed_jobs
                                             else ",".join(d.sleep_failed_jobs))
            cond = "R" if d.condition == "RECOVERY" else " "
            lines.append(
                f"{d.day:3d}{cond} {d.wake_hours:5.2f} {d.wake_ticks:3d} "
                f"{d.successes:3d}/{d.xp:<4d} {d.lt_size:4d} {d.ring_len:4d} {d.bets_rows:4d} "
                f"{d.news_items:4d} {d.domains:3d} {d.energy:6.3f} {d.mean_strength:5.3f} "
                f"{d.low_strength_recalls:3d}/{d.explore_promotions:<3d} {q}  {sleep}")
        lines.append("")
        lines.append(f"totals: xp={self.total_xp} successes={self.total_successes} "
                     f"sleeps={self.sleeps} quests closed={self.quests_closed} "
                     f"expired={self.quests_expired} proposed={self.quests_proposed}")
        lines.append("")
        for v in self.verdicts():
            lines.append(f"[{v.status:4s}] {v.name} — {v.note}")
        return "\n".join(lines)


# ==================================================================================================
# CLI
# ==================================================================================================
def main(argv: Optional[list] = None) -> int:
    ap = argparse.ArgumentParser(
        description="Pillars simulated-days harness — the coupled economies run together.")
    ap.add_argument("--days", type=int, default=100)
    ap.add_argument("--seed", type=int, default=7)
    ap.add_argument("--workdir", default=None,
                    help="state root (default: a fresh temp dir; NEVER the real workspace/)")
    args = ap.parse_args(argv)
    workdir = args.workdir or tempfile.mkdtemp(prefix="simdays-")
    sim = SimCreature(workdir, seed=args.seed).run(args.days)
    print(sim.report())
    failed = [v for v in sim.verdicts() if v.status == "FAIL"]
    print()
    print(f"RESULT: {'FAIL' if failed else 'PASS'} "
          f"({len([v for v in sim.verdicts() if v.status == 'PASS'])} pass, "
          f"{len(failed)} fail, "
          f"{len([v for v in sim.verdicts() if v.status == 'SKIP'])} skip)  "
          f"state: {workdir}")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
