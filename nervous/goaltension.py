"""The goal-tension drive (Ventral Striatum — incompletion / regret pressure).

Biology: the ventral striatum tracks the gap between where you are and the goal you are committed to.
An open, unfinished commitment is a low-grade pressure that KEEPS you engaged; closing it (real
progress) discharges the pressure (relief). eiDOS turns its objective state into exactly that pressure:

  - while an objective is OPEN and the tick made no real progress, tension climbs; a stalled
    (frustrated) objective presses HARDER (regret — "I keep not getting this done"),
  - REAL progress (a new fact, a new skill, a closed objective) discharges it toward zero (relief),
  - past a threshold the tension raises a BOUNDED arousal floor — the itch to keep going. Because the
    sleep cycle and metabolic torpor trigger at LOW arousal, this floor keeps the creature awake and
    acting WHILE WORK REMAINS: an unfinished goal is structurally what stops it drowsing, instead of a
    prompt sentence begging it to "have an inner life when idle."

This is the structural replacement for that plea (BIBLE §0, §2.3, §2.8, brain-map Ventral Striatum):
behaviour from a deterministic glue signal with real teeth (arousal → sleep/cadence), not prose. The
initiative temperament (DMN) scales how hard the itch bites. Pure observer of objective state; never
acts; never raises. Publishes a retained drive event for the behind-the-curtain tab.
"""
import json
import threading
import time

from .event import NervousEvent, Kind, Modality, Delivery, SCHEMA_VERSION


COMMISSION_PRESS = 0.6   # declared: how hard an OPEN commission task presses when nothing else
                         # does — above a fresh objective's 0.5 (an operator's standing order
                         # outranks a self-chosen goal) but below a fully-frustrated one (1.0):
                         # the commission itches steadily, it never screams.


class GoalTensionDrive:
    def __init__(self, *, bus=None, neuromod=None, decay=0.9,
                 tension_arousal_max=0.4, press_threshold=0.5):
        self.bus = bus
        self.neuromod = neuromod
        self.decay = float(decay)
        # Above press_threshold, tension ramps a bounded arousal floor (the itch to make progress) to at
        # most tension_arousal_max — bounded like curiosity's floor so an unfinished goal nudges the
        # creature awake without pinning arousal at 1.0.
        self.tension_arousal_max = float(tension_arousal_max)
        self.press_threshold = float(press_threshold)
        self.level = 0.0           # incompletion / regret pressure, 0..1
        self.last_target = 0.0
        self._lock = threading.Lock()

    def observe(self, *, made_progress: bool, open_objective: bool,
                frustration_frac: float = 0.0, initiative: float = 0.5,
                open_commission: bool = False) -> float:
        """Fold this tick's objective state into the drive; return the tension level 0..1.

        made_progress discharges it (relief); an open-but-unprogressed objective charges it, a
        frustrated one harder (regret); an open COMMISSION task presses too (a standing order from
        the operator is an open commitment — COMMISSION_PLAN.md); nothing open => no goal pressure
        at all (idle novelty is curiosity's job, not this drive's — a creature with no mission
        simply *is*). Default open_commission=False keeps every pre-commission caller byte-identical."""
        frustration_frac = max(0.0, min(1.0, float(frustration_frac)))
        if made_progress:
            target = 0.0                                      # the gap just shrank → relief
        elif open_objective or open_commission:
            target = min(1.0, 0.5 + 0.5 * frustration_frac) if open_objective else 0.0
            if open_commission:
                target = max(target, COMMISSION_PRESS)        # the standing order's steady itch
        else:
            target = 0.0                                      # no open commitment → no tension
        with self._lock:
            self.level = max(0.0, min(1.0, self.level * self.decay + target * (1.0 - self.decay)))
            self.last_target = target
            level = self.level
        if self.neuromod is not None:
            try:
                over = max(0.0, (level - self.press_threshold) / max(1e-6, 1.0 - self.press_threshold))
                # initiative (DMN temperament, ~0..1; 0.5 neutral) scales the itch: a driven creature
                # feels an open goal more sharply, a deferential one less. neuromod clamps to its cap.
                scale = 0.5 + max(0.0, min(1.0, float(initiative)))
                self.neuromod.set_drive_floor(over * self.tension_arousal_max * scale, source="goal_tension")
            except Exception:  # noqa: BLE001
                pass
        self._publish(level, target)
        return level

    def snapshot(self):
        with self._lock:
            return {"tension": round(self.level, 3), "target": round(self.last_target, 3)}

    def _publish(self, level, target):
        if self.bus is None:
            return
        try:
            payload = json.dumps({"drive": "goal_tension", "tension": round(level, 3),
                                  "target": round(target, 3)}, ensure_ascii=False).encode("utf-8")
            ev = NervousEvent(SCHEMA_VERSION, "goal_tension", Kind.drive, Modality.system,
                              Delivery.retained, salience=round(level, 3), t=time.monotonic())
            self.bus.publish(ev, payload)
        except Exception:  # noqa: BLE001
            pass
