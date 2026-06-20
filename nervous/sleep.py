"""P7 — the sleep / consolidation cycle (the home of the learned models).

When arousal drops to its floor, the creature sleeps: an OFFLINE cycle that replays recent activity,
re-fits the change-detection baselines (the buildable-now consolidation), and is where the learned
models — real predictive coding (T2), interoceptive inference (T3), allostasis (T4) — will live. It
runs ONLY at low arousal, so learning never competes with live perception or steals the GPU mid-tick.
The lowest arousal floor of the neuromodulatory state (Pillar 6) IS this sleep.
"""
import json
import threading
import time

from .event import NervousEvent, Kind, Modality, Delivery, SCHEMA_VERSION


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
