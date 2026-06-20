"""P4 — change / novelty detection (the buildable-now form of Pillar 3).

"Forward only prediction error" requires a learned generative model (T2). The buildable-now form is
change detection: track the last value per channel and emit a `change` event ONLY when it differs —
"only what changed rises". A quiet, stable environment generates near-zero upward traffic; a real
change spikes. Honest label: change/novelty detection, NOT predictive coding.
"""
import threading

from .event import NervousEvent, Kind, Delivery, SCHEMA_VERSION

_MISSING = object()


class Novelty:
    """Tracks the last-seen value per key; returns True iff the value is new/changed. Pure + testable."""

    def __init__(self):
        self._last = {}
        self._lock = threading.Lock()

    def is_novel(self, key, value) -> bool:
        with self._lock:
            if self._last.get(key, _MISSING) == value:
                return False
            self._last[key] = value
            return True

    def reset(self, key=None):
        with self._lock:
            if key is None:
                self._last.clear()
            else:
                self._last.pop(key, None)


class ChangeDetector:
    """An organ: re-emits a `change` event only when an input's value (by `key_of`) differs from the
    last seen for its (source, modality, kind). Forwards only the delta upward."""

    def __init__(self, bus, *, topics=None, key_of=None):
        self.bus = bus
        self.novelty = Novelty()
        self.key_of = key_of or (lambda ev, payload: payload)
        self.sub = bus.subscribe(topics=topics)
        self._stop = threading.Event()
        self._thread = None

    def _key(self, ev):
        return (ev.source_organ, ev.modality.value, ev.kind.value)

    def step(self, ev, payload=None):
        """Process one event synchronously; returns True iff a `change` was emitted."""
        if self.novelty.is_novel(self._key(ev), self.key_of(ev, payload)):
            self._emit_change(ev, payload)
            return True
        return False

    def _emit_change(self, src_ev, payload):
        ev = NervousEvent(SCHEMA_VERSION, "change", Kind.change, src_ev.modality, Delivery.fungible,
                          salience=src_ev.salience, t=src_ev.t)
        self.bus.publish(ev, payload)

    def start(self):
        self._thread = threading.Thread(target=self._run, name="change-detector", daemon=True)
        self._thread.start()
        return self

    def _run(self):
        while not self._stop.is_set():
            ev = self.bus.recv(self.sub, timeout=0.2)
            if ev is None:
                continue
            payload = self.bus.payloads.get(ev.payload_ref) if ev.payload_ref else None
            self.bus.ack(ev)
            if ev.kind == Kind.change:   # never re-process our own output (no loop)
                continue
            self.step(ev, payload)

    def stop(self):
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=1.0)
        self.bus.unsubscribe(self.sub)
