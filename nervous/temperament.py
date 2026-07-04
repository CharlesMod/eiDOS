"""Temperament — the DMN's slow personality drift (BIBLE §3 DMN row, §2.12).

Personality is NOT a row of knobs in the prompt; it is the slow-moving SETPOINTS the glue computes from
how life has actually gone. Three axes drift over minutes–hours from the creature's own success /
failure / override history:

  - initiative  — how readily it pushes on its own. Rises when acting autonomously pays off (progress);
                  falls when the world keeps overriding it (the gate forcibly parks what it chose to
                  pursue) — a creature that gets corrected a lot learns to push less.
  - persistence — how long it stays on a hard objective before the gate has to drag it off. Rises with
                  progress; falls with forced parks (learn to let go sooner).
  - caution     — how much a failure makes it hedge. Rises with failure / override; falls with a run of
                  success.

Drift is DELIBERATELY slow (a small step per update toward the target this tick's experience implies),
so temperament is a weather system, not a knee-jerk — it takes hundreds of ticks (minutes–hours of real
ticking) to move meaningfully. Persisted to state_dir/temperament.json so it survives a restart.

The setpoints feed MECHANISM, not prose: the objectives gate's park threshold via park_threshold()
(persistence → Basal Ganglia), and the goal-tension itch via the initiative axis. A single DERIVED
disposition WORD is surfaced in context — a label like the condition label, never the raw floats
(BIBLE §2.12: collapse to function; personality is emergent, not a dial the model reads).
"""
from __future__ import annotations

import json

STEP = 0.02          # per-update drift rate — slow (a target shift takes ~50 updates to close ~63%)
_NEUTRAL = 0.5

# Pillars 4.3 setpoint springs (pitfall #3, flag-gated by pillars_mastery_gates_enabled):
GENOME_BASELINE = _NEUTRAL   # declared: the species-mean setpoint axes are elastically pulled toward —
                             # the "who it is underneath" that a streak bends but never rewrites.
BIRTH_SPREAD = 0.08          # declared: at FIRST BIRTH (no temperament.json yet) each axis's personal
                             # baseline is drawn once, uniform in GENOME_BASELINE ± BIRTH_SPREAD, and
                             # persisted for life — a congenital disposition. Two creatures with
                             # identical code diverge from tick one, and the springs pull each toward
                             # ITS OWN nature, not a universal mean (Charlie: new creatures should
                             # become vastly different beings over years). 0.08 keeps every draw well
                             # inside the disposition() bands so no newborn starts pre-labeled.
SPRING_STEP = 0.004          # declared: STEP/5 — experience outpulls the spring 5:1 (a real losing
                             # streak still moves caution), but on neutral ticks the residue relaxes
                             # ~63% back toward baseline in ~250 updates instead of ratcheting forever
                             # (the depression-spiral damper: bad streak → caution ↑ → stall → recover).


def _gene(config, name):
    """The genome's multiplier on one of this module's pressure constants (genome.py — congenital
    personality as pressure). FAIL-OPEN: no genome file / no module → exactly 1.0, so pre-genome
    behavior is byte-identical. genome.py owns the clamps; this never raises."""
    try:
        from genome import gene
        return gene(config, name)
    except Exception:  # noqa: BLE001 - a genome must never break temperament
        return 1.0


class Temperament:
    AXES = ("initiative", "persistence", "caution")
    genome_baseline = GENOME_BASELINE   # Pillars 4.3 seam: the setpoint the springs pull toward —
                                        # class-visible so harnesses (sim-days) can probe for the
                                        # mechanism and read the recovery target without guessing.

    def __init__(self, config=None, *, step=STEP):
        self.config = config
        self.step = float(step)
        self.initiative = _NEUTRAL
        self.persistence = _NEUTRAL
        self.caution = _NEUTRAL
        self.baselines = {ax: GENOME_BASELINE for ax in self.AXES}
        self.updates = 0
        born = not self.load()
        if born and config is not None:
            # First birth: this creature's congenital baselines, and each axis starts AT its own
            # nature. When a genome exists (workspace/genome.json — genome.py) the baselines are
            # its latent-derived stamp_baselines, clamped inside every disposition() band so no
            # newborn starts pre-labeled; with no genome (fail-open) the original uniform draw in
            # GENOME_BASELINE ± BIRTH_SPREAD stands. Persisted immediately — the draw happens once.
            drawn = None
            try:
                from genome import Genome
                # Load-OR-BIRTH: the creature's first birth is the one place the congenital draw
                # happens — the read-only gene() accessor everywhere else never births, so without
                # this call no genome would ever exist and every gene would silently stay 1.0.
                # An existing creature (load() above succeeded) never reaches here, so a mid-life
                # code upgrade can never retro-fit a genome onto a creature that grew up without one.
                drawn = dict(Genome(config).stamp_baselines)
            except Exception:  # noqa: BLE001 - a genome must never break a birth
                drawn = None
            if drawn:
                for ax in self.AXES:
                    self.baselines[ax] = round(
                        max(0.0, min(1.0, float(drawn.get(ax, GENOME_BASELINE)))), 3)
            else:
                import random
                for ax in self.AXES:
                    b = GENOME_BASELINE + random.uniform(-BIRTH_SPREAD, BIRTH_SPREAD)
                    self.baselines[ax] = round(max(0.0, min(1.0, b)), 3)
            for ax in self.AXES:
                setattr(self, ax, self.baselines[ax])
            self.save()

    def _path(self):
        return self.config.state_dir / "temperament.json"

    def load(self) -> bool:
        """Returns True when a persisted temperament existed (False = this is a fresh birth)."""
        try:
            d = json.loads(self._path().read_text(encoding="utf-8"))
            for ax in self.AXES:
                setattr(self, ax, max(0.0, min(1.0, float(d.get(ax, _NEUTRAL)))))
            saved = d.get("baselines") or {}
            for ax in self.AXES:
                self.baselines[ax] = max(0.0, min(1.0, float(saved.get(ax, GENOME_BASELINE))))
            self.updates = int(d.get("updates", 0))
            return True
        except Exception:  # noqa: BLE001 - missing/corrupt file => fresh birth
            return False

    def save(self):
        try:
            self.config.state_dir.mkdir(parents=True, exist_ok=True)
            p = self._path()
            tmp = p.with_suffix(".json.tmp")
            tmp.write_text(json.dumps(self.snapshot(), ensure_ascii=False), encoding="utf-8")
            tmp.replace(p)
        except Exception:  # noqa: BLE001 - temperament is best-effort, never breaks the tick
            pass

    @staticmethod
    def _toward(cur, target, step):
        return max(0.0, min(1.0, cur + (target - cur) * step))

    def observe(self, *, success: bool, failed: bool, overridden: bool) -> None:
        """Nudge each axis a small step toward the target this tick's experience implies. An override
        (the gate forcibly parked the active objective) is the strongest teacher and takes precedence; a
        purely neutral tick (no progress, no failure, no override) leaves temperament where it is."""
        # Genome drift_rate (sensitivity): impressionable vs stubborn — applied where STEP is read,
        # fail-open ×1.0 so a genome-less creature drifts at exactly the declared constant.
        s = self.step * _gene(self.config, "drift_rate")
        if overridden:
            self.initiative = self._toward(self.initiative, 0.0, s)
            self.persistence = self._toward(self.persistence, 0.0, s)   # learn to let go sooner
            self.caution = self._toward(self.caution, 1.0, s)
        elif success:
            self.initiative = self._toward(self.initiative, 1.0, s)
            self.persistence = self._toward(self.persistence, 1.0, s)
            self.caution = self._toward(self.caution, 0.0, s)
        elif failed:
            self.caution = self._toward(self.caution, 1.0, s)
            self.initiative = self._toward(self.initiative, 0.3, s)
        if self.config is not None and getattr(self.config, "pillars_mastery_gates_enabled", False):
            # Setpoint springs (pitfall #3): after the experience nudge, every axis relaxes one small
            # bounded step toward THIS creature's congenital baseline — including on neutral ticks,
            # which is exactly when a spiked caution gets to recover. Flag off = byte-identical.
            # Genome spring_return (sensitivity down, tenacity up): sensitive = feelings linger
            # (weaker spring), tenacious = steadier — fail-open ×1.0 on the declared SPRING_STEP.
            spring = SPRING_STEP * _gene(self.config, "spring_return")
            for ax in self.AXES:
                setattr(self, ax, self._toward(getattr(self, ax), self.baselines[ax], spring))
        self.updates += 1
        if self.updates % 10 == 0:        # persist periodically, not every tick (I/O frugality)
            self.save()

    def park_threshold(self, base: int) -> int:
        """The objectives gate's park threshold, scaled by persistence (DMN → Basal Ganglia). Neutral
        persistence (0.5) = base; dogged (1.0) ≈ base*1.25; quick-to-let-go (0.0) ≈ base*0.75. So a
        creature that has learned persistence pays will grind a little longer before the gate rotates
        it, and one that keeps getting overridden gives up sooner — emergent, not configured. The
        genome's grip gene (tenacity) scales the persistence effect on top: a tenacious creature
        grinds congenitally longer, a loose-gripped one lets go sooner — fail-open ×1.0."""
        return max(3, int(round(base * (0.75 + 0.5 * self.persistence)
                                * _gene(self.config, "grip"))))

    def disposition(self) -> str:
        """One human-readable disposition word from the axes — a label for context, never the floats."""
        if self.caution >= 0.66 and self.initiative <= 0.4:
            return "wary"
        if self.initiative >= 0.66 and self.persistence >= 0.6:
            return "driven"
        if self.persistence >= 0.66:
            return "dogged"
        if self.initiative >= 0.66:
            return "eager"
        if self.caution >= 0.66:
            return "careful"
        if self.initiative <= 0.34 and self.persistence <= 0.34:
            return "reticent"
        return "steady"

    def snapshot(self) -> dict:
        d = {ax: round(getattr(self, ax), 3) for ax in self.AXES}
        d["updates"] = self.updates
        d["disposition"] = self.disposition()
        d["baselines"] = dict(self.baselines)
        return d
