"""The curiosity drive (intrinsic motivation).

Biology: novelty-seeking dopamine + the exploration drive — animals are intrinsically rewarded for
reducing uncertainty, and grow restless when their world becomes too predictable. eiDOS turns the
world-model's SURPRISE into two things:

  - an intrinsic REWARD bonus for novelty (fed into the reward learner, so exploring the unknown is
    reinforced alongside extrinsic outcomes), and
  - a curiosity/restlessness DRIVE that climbs during predictable lulls (low surprise) and is satisfied
    by novelty; when it climbs high enough it nudges arousal upward (the itch to go look at something),
    which — paired with creature mode — pushes the creature to explore on its own.

Honest-now: a running novelty estimate, not a learning-progress model. Publishes the drive as a retained
event so the behind-the-curtain tab can show it. Pure observer; never acts; never raises.
"""
import json
import threading
import time

from .event import NervousEvent, Kind, Modality, Delivery, SCHEMA_VERSION
from .worldmodel import SURPRISE_MAX

INTRINSIC_SCALE = 0.15     # max novelty bonus added to a tick's reward (small — it nudges, doesn't dominate)


class CuriosityDrive:
    def __init__(self, *, bus=None, neuromod=None, decay=0.9, boredom_arousal_bump=0.06,
                 boredom_threshold=0.7):
        self.bus = bus
        self.neuromod = neuromod
        self.decay = float(decay)
        self.boredom_arousal_bump = float(boredom_arousal_bump)
        self.boredom_threshold = float(boredom_threshold)
        self.level = 0.0           # restlessness 0..1 — rises when bored (predictable), falls on novelty
        self.last_novelty = 0.0
        self._lock = threading.Lock()

    def observe(self, surprise) -> float:
        """Fold one transition's surprise into the drive; return the intrinsic reward bonus for this tick.
        Novelty satisfies curiosity (lowers restlessness + earns a reward bonus); a predictable lull
        raises restlessness, and past the threshold nudges arousal (the urge to explore)."""
        novelty = max(0.0, min(1.0, float(surprise) / SURPRISE_MAX))
        intrinsic = INTRINSIC_SCALE * novelty
        with self._lock:
            # EMA toward (1 - novelty): sustained low novelty => restlessness climbs toward 1
            self.level = max(0.0, min(1.0, self.level * self.decay + (1.0 - novelty) * (1.0 - self.decay)))
            self.last_novelty = round(novelty, 3)
            restless = self.level
        if self.neuromod is not None and restless > self.boredom_threshold:
            try:
                self.neuromod.bump(self.boredom_arousal_bump)   # the itch to go look at something
            except Exception:  # noqa: BLE001
                pass
        self._publish(restless, novelty, intrinsic)
        return intrinsic

    def snapshot(self):
        with self._lock:
            return {"restlessness": round(self.level, 3), "last_novelty": self.last_novelty}

    def _publish(self, level, novelty, intrinsic):
        if self.bus is None:
            return
        try:
            payload = json.dumps({"drive": "curiosity", "restlessness": round(level, 3),
                                  "last_novelty": round(novelty, 3),
                                  "intrinsic": round(intrinsic, 3)}, ensure_ascii=False).encode("utf-8")
            ev = NervousEvent(SCHEMA_VERSION, "curiosity", Kind.drive, Modality.system,
                              Delivery.retained, salience=round(level, 3), t=time.monotonic())
            self.bus.publish(ev, payload)
        except Exception:  # noqa: BLE001
            pass
