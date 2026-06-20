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


class NeuromodulatoryState:
    def __init__(self, bus, *, source="neuromod", baseline_arousal=0.3, decay=0.85,
                 drive_floor_cap=0.55):
        self.bus = bus
        self.source = source
        self.baseline = float(baseline_arousal)
        self.decay = float(decay)
        self.arousal = float(baseline_arousal)
        self.valence = 0.0                       # -1 (bad) .. +1 (good)
        # A slow drive (e.g. curiosity restlessness) can raise a BOUNDED arousal floor — a tonic "itch"
        # the body settles at, never above drive_floor_cap. This replaced an unbounded per-tick bump that
        # pinned an idle creature's arousal at 1.0 (2026-06-20: newborn creature stuck "vigilant", looping).
        self.drive_floor = 0.0
        self.drive_floor_cap = float(drive_floor_cap)
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
            target = max(self.baseline, pressure, self.drive_floor)
            self.arousal = self.arousal * self.decay + target * (1.0 - self.decay)
            self.valence = -pressure

    def bump(self, amount):
        """A threat/novelty spike raises arousal immediately (the startle response)."""
        with self._lock:
            self.arousal = min(1.0, self.arousal + float(amount))

    def set_drive_floor(self, amount):
        """A slow drive sets a BOUNDED tonic arousal floor (e.g. curiosity restlessness = the itch to
        explore). arousal relaxes toward this floor via observe_interoception; phasic threat/reward
        spikes ride above it and decay back. Bounded by drive_floor_cap so a drive can never pin
        arousal at 1.0 the way the old per-tick bump did."""
        with self._lock:
            self.drive_floor = max(0.0, min(self.drive_floor_cap, float(amount)))

    def observe_reward(self, rpe, reward):
        """Dopamine: a reward-prediction-error spike raises arousal (the surprise is salient) and nudges
        valence toward the reward's sign (it felt good / bad). Transient — interoception still sets the
        baseline mood; this is the phasic dopamine bump on top."""
        with self._lock:
            # Capped phasic bump: a high-RPE newborn (every action novel, V starts at 0) would otherwise
            # slam arousal to 1.0 every tick. A single surprise lifts arousal by at most 0.15.
            self.arousal = min(1.0, self.arousal + min(0.15, 0.5 * abs(float(rpe))))
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
