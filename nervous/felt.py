"""P1b — the felt-qualia transfer function + the creature-render's window onto the felt-state.

`to_felt` is the *designed transfer function* (the Pantheon "wetware abstraction"): it turns the
coarse interoceptive bars (P1a's ok/elevated/high/critical) into FELT language — the creature feels
"running hot" or "memory tight", never "gpu_temp: high / 88C". High VRAM is felt as calm POSTURE (the
mind resident in its body, by design), never stress — see BASELINE_SYSTEMS. It is honestly a transfer
function, NOT model-based interoceptive inference (that is T3).

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
    "gpu_temp": {"elevated": "warming", "high": "running hot", "critical": "overheating"},
    # Metabolism (M0): hunger = a depleting energy reserve. NOT baseline — a hungry body genuinely
    # feels worse, which is the whole point (it rides wellbeing→reward so the creature seeks to feed).
    "energy": {"elevated": "peckish", "high": "hungry", "critical": "starving"},
}

# Baseline systems are felt as calm POSTURE, never stress. The mind is resident on the GPU and fills
# VRAM by design (~98% at rest) — the creature notices its own brain at home there, without alarm.
_BASELINE_PHRASE = {
    "vram": {"elevated": "mind settling in", "high": "mind resident on the GPU",
             "critical": "mind fully resident"},
}

# Systems whose HIGH reading is expected BY DESIGN, not a sign of trouble: the mind lives on the GPU and
# fills VRAM the way a brain fills a skull. These are felt as posture/proprioception, never stress — they
# must not drive the overall body-feeling, salience, or arousal. (Genuine GPU strain is contention for
# the one GPU — the arbiter/speech-gate's signal, wired later; thermal strain is gpu_temp, which is NOT
# baseline and still escalates normally.)
BASELINE_SYSTEMS = ("vram",)


def stress_bars(bars):
    """The bars that count as STRESS: present (non-None) and non-baseline. The single place that
    decides what the body may feel stressed about, so overall-feeling, salience, and arousal can never
    diverge on it (I6) — and high VRAM (the resident mind) is never felt as distress."""
    return {k: v for k, v in bars.items() if v is not None and k not in BASELINE_SYSTEMS}


def system_phrase(system, level):
    """The felt phrase for ONE system at ONE level — the per-sense sub-function the monitor surfaces in
    the 'behind the curtain' transduction stack. Baseline systems (VRAM = the resident mind) use the calm
    posture phrasing. Returns '' for 'ok'/None/unknown (a system at ease has nothing to say)."""
    if not level or level == "ok":
        return ""
    table = _BASELINE_PHRASE if system in BASELINE_SYSTEMS else _PHRASE
    return table.get(system, {}).get(level, "")


def to_felt(bars):
    """bars: {system -> 'ok'|'elevated'|'high'|'critical'} (None values ignored).
    Returns {'overall': <feeling word>, 'felt': [<phrase>, ...]} — the felt qualia.

    Baseline systems (BASELINE_SYSTEMS — expected-high by design, e.g. the mind resident in VRAM) are
    felt as calm posture: they contribute a felt phrase but NEVER raise the overall body-feeling."""
    present = {k: v for k, v in bars.items() if v is not None}
    worst = max((_LEVEL_IDX.get(v, 0) for v in stress_bars(present).values()), default=0)
    felt = []
    for k, v in present.items():
        if v == "ok":
            continue
        table = _BASELINE_PHRASE if k in BASELINE_SYSTEMS else _PHRASE
        phrase = table.get(k, {}).get(v)
        if phrase:
            felt.append(phrase)
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
