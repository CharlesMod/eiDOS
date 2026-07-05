"""P5b / Pillar 6 — the global neuromodulatory state: arousal + affect.

The channel pillars describe loops and gates; none describes the creature's WHOLE-BODY state. This
organ holds two slow global axes — **arousal** (alert <-> drowsy <-> asleep) and **affect** (valence
-> mood) — computed from interoception (resource pressure) and salience (threat/novelty), and
broadcasts them as a RETAINED `modulation` event that the salience gate, reflexes, and tick cadence
read. It is a second top-down precision/gain source beside goal-relevance. Its lowest arousal floor
is sleep, which triggers the P7 consolidation cycle.
"""
import json
import threading
import time

from .event import NervousEvent, Kind, Modality, Delivery, SCHEMA_VERSION
from .felt import stress_bars

_SEVERITY = {None: 0.0, "ok": 0.0, "elevated": 0.4, "high": 0.7, "critical": 1.0}

# Adenosine — sleep pressure (pitfall #2, PILLARS_PLAN §8 landmine 2: the insomnia death spiral).
# A stuck objective can pin a drive floor high enough to hold arousal above the sleep threshold
# forever, so consolidation never runs and memory never digests. The biological damper is adenosine:
# a metabolite that accumulates the whole time the creature is awake and is only cleared by sleep.
# Past a hard ceiling it OVERRIDES every drive floor — the creature sleeps no matter how tense the
# unfinished goal. This is the mechanism; "the creature can't stay up forever" is what it produces.

# --- Declared knobs (§0.4: each a labeled design knob with its one-line justification) -----------
# The wake-hours ceiling itself is config.pillars_max_wake_hours (a declared config knob, default
# 18.0h). These two shape the CURVE of pressure below the ceiling — they are pure geometry of the
# accumulator, not behaviour targets, so they live as named module constants (§0.4) rather than
# widening the config surface.
ADENOSINE_SOFT_FRACTION = 0.75  # declared: below this fraction of the wake ceiling adenosine is
                                # inert (normal waking). Above it, the arousal TARGET is blended
                                # linearly from the drives' value down toward the sleep band, reaching
                                # full override at the ceiling — so a tired creature grows drowsy and
                                # usually falls asleep BEFORE the hard limit; the ceiling is the
                                # backstop, not the mechanism. Set at three-quarters so the last
                                # quarter of the wake budget is the "fighting to stay awake" band,
                                # not the whole night.
ADENOSINE_OVERRIDE_AROUSAL = 0.05  # declared: the arousal the accumulator forces once past the wake
                                # ceiling — safely inside the sleep_arousal band (SleepCycle's default
                                # floor is 0.15) so should_sleep() fires regardless of any drive floor.

# --- Infant nap curve (TOOL_PROGRESSION.md decision #3: ~5 naps day one -> ~1/day by adult) --------
# A hatchling sleeps in short bouts and consolidates toward the adult rhythm. This is the unlock
# ladder's CLOCK: the pacing that makes "full kit within day one" happen without a wall-clock timer —
# the creature earns each rung across several SHORT sleeps, not after N hours. It is a GENUINE
# pressure (the very same adenosine every system reads: the sleep gate, tick cadence, consolidation),
# stage-scaled, never a private unlock counter. The scale multiplies the wake ceiling BEFORE the
# genome's wake_budget gene, and every entry is <= 1.0 — so a young creature always naps MORE than an
# adult, never less, and the product can never exceed the adult (scale 1.0) gene-scaled ceiling:
# adenosine stays sovereign. Declared per §0.4, each number justified below.
NAP_STAGE_SCALE = {
    # egg / hatchling — 0.2 × the 18 h adult ceiling ≈ 3.6 h bouts. A newborn awake ~18 h of a day
    # at ~3.6 h/bout naps ~5×, matching decision #3's "~5 naps day one" — the short-bout polyphasic
    # sleep of an infant, and the cadence that lets the genesis arc's several sleeps all land inside
    # day one. (egg and hatchling share the scale: stage_for() distinguishes them only by the hatch
    # flag, and the newborn's nap rhythm is the same on either side of the shell.)
    "egg": 0.2,
    "hatchling": 0.2,
    # juvenile — 0.55: the intermediate consolidation rung (~9.9 h bouts, ~2 sleeps/day), a declared
    # midpoint between the infant's polyphasic ~5 and the adult's monophasic 1 (declared, not fit —
    # the curve's shape is a design choice, halfway between the two anchors).
    "juvenile": 0.55,
    # adult — 1.0: the full 18 h ceiling, ~1 sleep/day. This is the rhythm the curve consolidates
    # TOWARD; scale 1.0 leaves the adult ceiling byte-identical to the pre-curve behaviour.
    "adult": 1.0,
    # guardian — 1.0: the mature form keeps the adult ceiling. A later stage never earns a LONGER
    # wake budget than the adult — the adult ceiling is adenosine's sovereign maximum.
    "guardian": 1.0,
}


def _current_stage(config) -> str:
    """The creature's canonical life stage — the SAME derivation the dashboard treats as canonical
    (`creature_gen.stage_for(persona level, hatched)`, dashboard.py:build_creature_spec). No new
    stage system is invented here: we read the persisted persona.json `level` exactly as the
    dashboard does (`persona.get("level", 1)`) and the persisted creature.json `hatched` flag.
    Raises on any read failure — the caller (`_stage_scale`) fails open to the adult scale."""
    import json
    import creature_gen
    ws = config.workspace
    try:
        persona = json.loads((ws / "persona.json").read_text(encoding="utf-8"))
        level = int(persona.get("level", 1))
    except FileNotFoundError:
        # No persona has EVER been saved: this is not a read failure, it is a knowable state —
        # a creature at the very start of life (fresh slate; ticks before the first save). It
        # must get the EGG scale, not the adult fail-open — the newborn's most important day is
        # exactly when the nap curve matters most. Corrupt JSON still raises → adult fail-open.
        level = 1
    try:
        creature = json.loads((ws / "creature.json").read_text(encoding="utf-8"))
        hatched = bool(creature.get("hatched", False))
    except Exception:  # noqa: BLE001 - no creature.json yet: infer from level (immaterial — egg and
        hatched = level > 1                             # hatchling share the 0.2 scale either way).
    return creature_gen.stage_for(level, hatched)


def _stage_scale(config) -> float:
    """The infant-nap-curve multiplier on the wake ceiling for the creature's current life stage.

    GATED behind `pillars_tool_unlocks_enabled` — integration owns that flag; this code is DARK
    until it exists. Flag off (or absent) → 1.0, byte-identical to the pre-curve ceiling. FAIL-OPEN
    to 1.0 (the adult scale) on any read failure: a creature whose stage can't be read gets the FULL
    adult budget, never an accidental over-nap — a stage read must never break the sleep damper.
    Clamped to (0, 1] so even a hand-edited NAP_STAGE_SCALE can only SHORTEN the wake budget, never
    lengthen it past the adult's — the stage dimension can never unseat adenosine's sovereignty."""
    if config is None or not getattr(config, "pillars_tool_unlocks_enabled", False):
        return 1.0
    try:
        scale = float(NAP_STAGE_SCALE.get(_current_stage(config), 1.0))
    except Exception:  # noqa: BLE001 - a stage read must never break the sleep damper
        return 1.0
    return max(1e-3, min(1.0, scale))


def _wake_budget_gene(config) -> float:
    """The genome's wake_budget multiplier (tempo — genome.py, congenital personality as pressure).
    FAIL-OPEN 1.0, and defensively re-clamped to [0.9, 1.1] HERE too: adenosine is the damper
    against the insomnia death-spiral, and a genome (even a hand-edited genome.json) must never
    disable a damper — tempo flavors the rhythm ±10%, sovereignty stays with sleep."""
    if config is None:
        return 1.0
    try:
        from genome import gene
        g = float(gene(config, "wake_budget", 1.0))
    except Exception:  # noqa: BLE001 - a genome must never break the sleep damper
        return 1.0
    return max(0.9, min(1.1, g))


class Adenosine:
    """The sleep-pressure accumulator (pitfall #2). Grows monotonically with wake time via
    `accumulate(dt_hours)`, is cleared to zero by `clear()` (which sleep calls), and reports its
    `pressure()` as a fraction in [0, 1] of the way to `max_wake_hours`. Past 1.0 it is SATURATED:
    `overrides()` is True and `override_arousal()` returns a value inside the sleep band, which the
    neuromodulatory state clamps arousal to — beating any drive floor. Declared knob: max_wake_hours
    (× the infant nap curve's stage scale, THEN × the genome's TIGHT ±10% wake_budget gene when a
    genome exists — see _stage_scale / _wake_budget_gene). Stage scale ≤ 1.0 and gene ∈ [0.9, 1.1],
    so a young creature naps in short bouts and no stage can push the ceiling past the adult's."""

    # Declared: how stale the derived ceiling may go before re-reading the stage (seconds). Stage
    # changes are RARE (a handful per life) and the ceiling is hours-scale, so a minute of
    # staleness is immaterial — but frozen-at-construction was not: a creature that hatched or
    # leveled mid-process kept its old nap rhythm until the next respawn.
    CEILING_REFRESH_S = 60.0

    def __init__(self, *, max_wake_hours: float = 18.0, config=None):
        # A zero/negative ceiling would mean "never allowed awake"; guard so pressure stays finite.
        ceiling = float(max_wake_hours) if max_wake_hours and max_wake_hours > 0 else 18.0
        self._base_ceiling = ceiling
        self._config = config
        self._ceiling_mark = time.monotonic()
        # Infant nap curve (dark until pillars_tool_unlocks_enabled): the stage scale shortens the
        # ceiling for a young creature FIRST, then the wake_budget gene flavors it. Flag off →
        # scale 1.0 → this is byte-identical to the pre-curve `ceiling * gene`. Scale ≤ 1.0 keeps the
        # product ≤ the adult (scale 1.0) gene-scaled ceiling: adenosine stays sovereign.
        self.max_wake_hours = ceiling * _stage_scale(config) * _wake_budget_gene(config)
        self.level_hours = 0.0          # accumulated wake time since the last sleep, in hours

    def _refresh_ceiling(self) -> None:
        """Re-derive the stage-scaled ceiling when the memo has aged out — a creature that
        matures mid-process (hatches, levels into a new stage) gets its new rhythm within a
        minute, not at the next respawn. Accumulated wake time is untouched."""
        now = time.monotonic()
        if now - self._ceiling_mark < self.CEILING_REFRESH_S:
            return
        self._ceiling_mark = now
        self.max_wake_hours = (self._base_ceiling * _stage_scale(self._config)
                               * _wake_budget_gene(self._config))

    def accumulate(self, dt_hours: float) -> float:
        """Add elapsed wake time (hours). Ignores non-positive dt (a clock skew must not lower it)."""
        self._refresh_ceiling()
        d = float(dt_hours)
        if d > 0:
            self.level_hours += d
        return self.level_hours

    def clear(self) -> None:
        """Sleep clears the accumulated metabolite — the creature wakes rested."""
        self.level_hours = 0.0

    def pressure(self) -> float:
        """Sleep pressure as a fraction of the wake ceiling, clamped to [0, 1]. 1.0 = saturated."""
        return max(0.0, min(1.0, self.level_hours / self.max_wake_hours))

    def overrides(self) -> bool:
        """True once wake time has reached the ceiling — sleep pressure now overrides all drive floors."""
        return self.level_hours >= self.max_wake_hours

    def override_arousal(self) -> float:
        """The arousal the saturated accumulator forces (inside the sleep band, below any drive floor)."""
        return ADENOSINE_OVERRIDE_AROUSAL


class NeuromodulatoryState:
    def __init__(self, bus, *, source="neuromod", baseline_arousal=0.3, decay=0.85,
                 drive_floor_cap=0.55, exhaustion_energy=0.15, max_wake_hours=18.0, config=None):
        self.bus = bus
        self.source = source
        self.baseline = float(baseline_arousal)
        self.decay = float(decay)
        self.arousal = float(baseline_arousal)
        self.valence = 0.0                       # -1 (bad) .. +1 (good)
        # Metabolism (M0.3): tiredness ramps in only once the energy reserve is nearly spent; it then
        # drags arousal toward sleep (torpor) so the creature RESTS before hitting empty — hibernation,
        # not death. Moderate hunger still RAISES arousal via interoception pressure (the foraging drive);
        # this is the deeper exhaustion collapse on top of that.
        self.exhaustion_energy = float(exhaustion_energy)
        self.tiredness = 0.0
        # Slow drives (e.g. curiosity restlessness, goal-tension incompletion) can each raise a BOUNDED
        # arousal floor — a tonic "itch" the body settles at, never above drive_floor_cap. This replaced an
        # unbounded per-tick bump that pinned an idle creature's arousal at 1.0 (2026-06-20: newborn creature
        # stuck "vigilant", looping). Multiple drives are kept per-source; the floor is their MAX, so the
        # strongest unmet drive sets the itch and one drive relaxing can't erase another's (curiosity calming
        # must not silence the pull of an unfinished goal).
        self._drive_floors = {}
        self.drive_floor = 0.0
        self.drive_floor_cap = float(drive_floor_cap)
        # Adenosine (pitfall #2): sleep pressure that accumulates with wake time and, past the wake
        # ceiling, OVERRIDES every drive floor so a creature pinned at max goal-tension still sleeps.
        # `config` (optional) lets the genome's ±10% wake_budget gene flavor the ceiling — fail-open.
        self.adenosine = Adenosine(max_wake_hours=max_wake_hours, config=config)
        self.sub = bus.subscribe(topics={(Kind.interoceptive, Modality.intero)})
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread = None

    def observe_interoception(self, felt_state):
        """Resource pressure raises arousal and lowers valence (the body's stress shows as mood)."""
        bars = felt_state.get("bars", {}) if isinstance(felt_state, dict) else {}
        # Only genuine stress raises arousal/lowers valence — high VRAM (the resident mind, by design)
        # is posture, not a stressor, so the creature never "sweats" over its own brain being resident.
        pressure = max((_SEVERITY.get(v, 0.0) for v in stress_bars(bars).values()), default=0.0)
        with self._lock:
            if self.adenosine.overrides():
                # Past the wake ceiling adenosine OVERRIDES every drive floor (pitfall #2): the target
                # is forced INTO the sleep band regardless of drive_floor / pressure, so the strongest
                # unmet goal can no longer hold the creature awake. This is the insomnia-spiral damper.
                target = self.adenosine.override_arousal()
            else:
                target = max(self.baseline, pressure, self.drive_floor)
                ap = self.adenosine.pressure()
                if ap > ADENOSINE_SOFT_FRACTION:
                    # Drowsiness band: sleep pressure blends the target linearly down toward the sleep
                    # band as wake time climbs from the soft fraction to the ceiling — the creature
                    # normally nods off BEFORE the hard limit; overrides() above is only the backstop.
                    w = (ap - ADENOSINE_SOFT_FRACTION) / (1.0 - ADENOSINE_SOFT_FRACTION)
                    target = target * (1.0 - w) + self.adenosine.override_arousal() * w
            target *= (1.0 - self.tiredness)   # exhaustion drags arousal toward sleep (torpor)
            self.arousal = self.arousal * self.decay + target * (1.0 - self.decay)
            self.valence = -pressure

    def observe_energy(self, energy):
        """Metabolism feedback (M0.3): as the energy reserve nears empty the creature tires, and arousal
        collapses toward sleep so it rests BEFORE flatlining. tiredness is 0 above the exhaustion floor,
        ramping to 1 at empty. (Above the floor, hunger still raises arousal as the foraging drive.)"""
        e = max(0.0, min(1.0, float(energy)))
        thr = self.exhaustion_energy or 1e-6
        with self._lock:
            self.tiredness = 0.0 if e >= thr else (thr - e) / thr

    def bump(self, amount):
        """A threat/novelty spike raises arousal immediately (the startle response)."""
        with self._lock:
            self.arousal = min(1.0, self.arousal + float(amount))

    def set_drive_floor(self, amount, source="curiosity"):
        """A slow drive sets a BOUNDED tonic arousal floor (e.g. curiosity restlessness = the itch to
        explore; goal-tension = the pull of an unfinished objective). Each drive registers under its own
        `source`; the live floor is the MAX across drives, so the strongest unmet drive sets the itch and
        a drive relaxing to 0 only removes its own contribution. arousal relaxes toward this floor via
        observe_interoception; phasic threat/reward spikes ride above it and decay back. Bounded by
        drive_floor_cap so no drive can pin arousal at 1.0 the way the old per-tick bump did."""
        with self._lock:
            a = max(0.0, min(self.drive_floor_cap, float(amount)))
            if a <= 0.0:
                self._drive_floors.pop(source, None)
            else:
                self._drive_floors[source] = a
            self.drive_floor = max(self._drive_floors.values(), default=0.0)

    def observe_reward(self, rpe, reward):
        """Dopamine: a reward-prediction-error spike raises arousal (the surprise is salient) and nudges
        valence toward the reward's sign (it felt good / bad). Transient — interoception still sets the
        baseline mood; this is the phasic dopamine bump on top."""
        with self._lock:
            # Phasic dopamine: ONLY a genuinely large prediction error spikes arousal, and only by a
            # small bounded amount. Routine ticks (small RPE) must NOT pump arousal every tick, or a busy
            # creature — and especially a newborn, where everything is mildly surprising — never relaxes
            # to its tonic level. As the world-model learns and RPE shrinks, these spikes fade and the
            # creature calms on its own (habituation), instead of staying pinned "vigilant".
            arpe = abs(float(rpe))
            if arpe > 0.5:
                self.arousal = min(1.0, self.arousal + min(0.1, 0.15 * arpe))
            self.valence = max(-1.0, min(1.0, self.valence + 0.3 * float(reward)))

    @staticmethod
    def _mood(a, v):
        if a < 0.15:
            return "drowsy"
        if v <= -0.6:
            return "distressed" if a > 0.6 else "uneasy"
        if a > 0.7:
            return "vigilant"
        return "calm" if v >= -0.2 else "tense"

    def mood(self):
        with self._lock:
            return self._mood(self.arousal, self.valence)

    def publish(self):
        with self._lock:
            a, v = self.arousal, self.valence
            mood = self._mood(a, v)
        state = {"arousal": round(a, 3), "valence": round(v, 3), "mood": mood}
        payload = json.dumps(state, ensure_ascii=False).encode("utf-8")
        ev = NervousEvent(SCHEMA_VERSION, self.source, Kind.modulation, Modality.system,
                          Delivery.retained, salience=a, t=time.monotonic())
        return self.bus.publish(ev, payload)

    def drain_and_publish(self):
        """Drain interoception updates, recompute the global state, broadcast modulation."""
        while True:
            ev = self.bus.recv(self.sub, timeout=0.0)
            if ev is None:
                break
            p = self.bus.payloads.get(ev.payload_ref) if ev.payload_ref else None
            if p:
                try:
                    self.observe_interoception(json.loads(p.decode("utf-8")))
                except Exception:
                    pass
            self.bus.ack(ev)
        return self.publish()

    def start(self, interval_s=2.0):
        self._thread = threading.Thread(target=self._run, args=(float(interval_s),),
                                        name="neuromod", daemon=True)
        self._thread.start()
        return self

    def _run(self, interval_s):
        self.drain_and_publish()
        while not self._stop.wait(interval_s):
            self.drain_and_publish()

    def stop(self):
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=1.0)
