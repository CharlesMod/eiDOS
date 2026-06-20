"""P5 — the efferent loop: action, reflex arcs, efference copy, and proprioception (Pillar 5).

Sensing and acting are one closed loop. The Effector runs an action and emits an EFFERENCE COPY
predicting the self-caused sensory change, so the change layer (P4) recognises that change as
self-caused and does NOT surface it as surprise — the sense of AGENCY ("I did that"), and the fix
for a creature reacting to its own speech/motion. ReflexArcs are fast sense->act paths that fire
WITHOUT the core, local to their effector (I9). Proprioceptors sense the creature's own effectors /
in-flight actions (the sixth sense).
"""
import json
import threading
import time

from .event import NervousEvent, Kind, Modality, Delivery, SCHEMA_VERSION


class SelfModel:
    """Efference-copy store: predicted self-caused sensory changes (consumed on inspection), so the
    change layer can recognise them as self-caused rather than surprising world events."""

    def __init__(self, ttl=5.0):
        self.ttl = float(ttl)
        self._expected = {}
        self._lock = threading.Lock()

    def expect(self, key, value):
        with self._lock:
            self._expected[key] = (value, time.monotonic() + self.ttl)

    def is_expected(self, key, value) -> bool:
        with self._lock:
            v = self._expected.pop(key, None)   # consume
            if not v:
                return False
            val, expires = v
            return time.monotonic() <= expires and val == value


class Effector:
    """The action layer: run an action handler and emit its efference copy (corollary discharge)."""

    def __init__(self, bus, *, name="effector", handlers=None, self_model=None):
        self.bus = bus
        self.name = name
        self.handlers = handlers or {}
        self.self_model = self_model
        self.acted = []

    def act(self, action, payload=None, predicts=None):
        """Run `action`; `predicts`=(key, value) is the self-caused sensory change to expect."""
        handler = self.handlers.get(action)
        result = handler(payload) if handler else None
        self.acted.append(action)
        if predicts is not None and self.self_model is not None:
            self.self_model.expect(predicts[0], predicts[1])   # corollary discharge
        self._emit_efference_copy(action, predicts)
        return result

    def _emit_efference_copy(self, action, predicts):
        payload = json.dumps({"action": action, "predicts": predicts},
                             ensure_ascii=False, default=str).encode("utf-8")
        ev = NervousEvent(SCHEMA_VERSION, self.name, Kind.efference_copy, Modality.proprio,
                          Delivery.reliable, t=time.monotonic())
        self.bus.publish(ev, payload)


class ReflexArc:
    """A fast sense->act path that fires WITHOUT the core, local to its effector (I9)."""

    def __init__(self, effector, *, name, trigger, action, predicts=None):
        self.effector = effector
        self.name = name
        self.trigger = trigger        # predicate(ev, payload) -> bool
        self.action = action
        self.predicts = predicts

    def consider(self, ev, payload=None) -> bool:
        """Returns True iff the reflex fired. A non-matching event is left to escalate to the core."""
        if self.trigger(ev, payload):
            self.effector.act(self.action, payload, predicts=self.predicts)
            self._emit_reflex_fired()
            return True
        return False

    def _emit_reflex_fired(self):
        ev = NervousEvent(SCHEMA_VERSION, self.name, Kind.reflex_fired, Modality.proprio,
                          Delivery.reliable, t=time.monotonic())
        self.effector.bus.publish(ev, None)


class Proprioceptor:
    """Senses the creature's OWN effector / in-flight-action state (am I speaking? what's running?)."""

    def __init__(self, bus, *, name="proprioception", state_fn=None):
        self.bus = bus
        self.name = name
        self.state_fn = state_fn or (lambda: {})

    def emit(self):
        payload = json.dumps(self.state_fn(), ensure_ascii=False).encode("utf-8")
        ev = NervousEvent(SCHEMA_VERSION, self.name, Kind.proprioceptive, Modality.proprio,
                          Delivery.fungible, salience=0.1, t=time.monotonic())
        return self.bus.publish(ev, payload)
