"""P1b — the felt-qualia transfer function + the creature-render's window onto the felt-state.

`to_felt` is the *designed transfer function* (the Pantheon "wetware abstraction"): it turns the
coarse interoceptive bars (P1a's ok/elevated/high/critical) into FELT language — the creature feels
"strained / GPU tight", never "vram: high / 92%". It is honestly a transfer function, NOT model-based
interoceptive inference (that is T3).

`FeltStateView` is the creature render's read-only window: it subscribes (retained) to the single
felt-state projection interoception publishes, and exposes the CURRENT value. It reads the one source
of truth (I6) — it never recomputes from telemetry — so the body can only ever show what the core
feels (truth-rendering; the documented "renders falsehoods" bug class cannot recur).
"""
import json

from .event import Kind, Modality, Delivery

_LEVEL_IDX = {"ok": 0, "elevated": 1, "high": 2, "critical": 3}

# Overall body feeling by worst-system severity (the Pantheon abstraction: a felt word, not a number).
_FEELING = {0: "at ease", 1: "a little tense", 2: "strained", 3: "in distress"}

# Per-system felt phrases, surfaced only when a system is not "ok".
_PHRASE = {
    "ram": {"elevated": "memory filling", "high": "memory tight", "critical": "out of memory"},
    "disk": {"elevated": "disk getting full", "high": "disk nearly full", "critical": "out of disk"},
    "cpu": {"elevated": "working hard", "high": "straining", "critical": "pegged"},
    "vram": {"elevated": "GPU filling", "high": "GPU tight", "critical": "GPU full"},
    "gpu_temp": {"elevated": "warming", "high": "running hot", "critical": "overheating"},
}


def to_felt(bars):
    """bars: {system -> 'ok'|'elevated'|'high'|'critical'} (None values ignored).
    Returns {'overall': <feeling word>, 'felt': [<phrase>, ...]} — the felt qualia."""
    present = {k: v for k, v in bars.items() if v is not None}
    worst = max((_LEVEL_IDX.get(v, 0) for v in present.values()), default=0)
    felt = [_PHRASE[k][v] for k, v in present.items()
            if v != "ok" and k in _PHRASE and v in _PHRASE[k]]
    return {"overall": _FEELING[worst], "felt": felt}


def felt_state(bars):
    """The single felt-state projection interoception publishes: the raw bins + the felt qualia."""
    present = {k: v for k, v in bars.items() if v is not None}
    return {"bars": present, **to_felt(present)}


class FeltStateView:
    """The creature render's read-only window onto the felt-state (the single source of truth, I6).
    It subscribes retained and reads the current projection — it never recomputes from telemetry."""

    def __init__(self, bus):
        self.bus = bus
        self.sub = bus.subscribe(topics={(Kind.interoceptive, Modality.intero)},
                                 deliveries={Delivery.retained})
        self._current = None

    def current(self):
        """Drain pending updates (last-value-wins) and return the current felt-state dict (or None)."""
        while True:
            ev = self.bus.recv(self.sub, timeout=0.0)
            if ev is None:
                break
            payload = self.bus.payloads.get(ev.payload_ref) if ev.payload_ref else None
            if payload:
                try:
                    self._current = json.loads(payload.decode("utf-8"))
                except Exception:
                    pass
            self.bus.ack(ev)
        return self._current

    def close(self):
        try:
            self.bus.unsubscribe(self.sub)
        except Exception:
            pass
