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
GENOME_BASELINE = _NEUTRAL   # declared: the genome setpoint every axis is elastically pulled toward —
                             # the "who it is underneath" that a streak bends but never rewrites.
SPRING_STEP = 0.004          # declared: STEP/5 — experience outpulls the spring 5:1 (a real losing
                             # streak still moves caution), but on neutral ticks the residue relaxes
                             # ~63% back toward baseline in ~250 updates instead of ratcheting forever
                             # (the depression-spiral damper: bad streak → caution ↑ → stall → recover).


class Temperament:
    AXES = ("initiative", "persistence", "caution")

    def __init__(self, config=None, *, step=STEP):
        self.config = config
        self.step = float(step)
        self.initiative = _NEUTRAL
        self.persistence = _NEUTRAL
        self.caution = _NEUTRAL
        self.updates = 0
        self.load()

    def _path(self):
        return self.config.state_dir / "temperament.json"

    def load(self):
        try:
            d = json.loads(self._path().read_text(encoding="utf-8"))
            for ax in self.AXES:
                setattr(self, ax, max(0.0, min(1.0, float(d.get(ax, _NEUTRAL)))))
            self.updates = int(d.get("updates", 0))
        except Exception:  # noqa: BLE001 - missing/corrupt file => start neutral
            pass

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
        s = self.step
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
            # bounded step toward the genome baseline — including on neutral ticks, which is exactly
            # when a spiked caution gets to recover. Flag off = byte-identical legacy drift.
            for ax in self.AXES:
                setattr(self, ax, self._toward(getattr(self, ax), GENOME_BASELINE, SPRING_STEP))
        self.updates += 1
        if self.updates % 10 == 0:        # persist periodically, not every tick (I/O frugality)
            self.save()

    def park_threshold(self, base: int) -> int:
        """The objectives gate's park threshold, scaled by persistence (DMN → Basal Ganglia). Neutral
        persistence (0.5) = base; dogged (1.0) ≈ base*1.25; quick-to-let-go (0.0) ≈ base*0.75. So a
        creature that has learned persistence pays will grind a little longer before the gate rotates
        it, and one that keeps getting overridden gives up sooner — emergent, not configured."""
        return max(3, int(round(base * (0.75 + 0.5 * self.persistence))))

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
        return d
