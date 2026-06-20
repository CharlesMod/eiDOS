"""P6 — exteroception: cheap non-LLM pre-filters + rare escalation.

The periphery is non-LLM and CPU-bound (the substrate decision). A PreFilter scores raw audio/video
salience cheaply (VAD / frame-diff) BEFORE paying any tokenization cost. Only a salient observation
ESCALATES: it acquires a GPU lease (PRI_REFLEX) and is tokenized — the rare foveal path (e.g. Gemma's
native A/V) — into a `percept` event. The raw modality NEVER enters the core; only the occasional
admitted percept does (the eufy "event + snapshot, not stream" instinct, generalized).
"""
import json
import time

from .event import NervousEvent, Kind, Delivery, SCHEMA_VERSION
from .arbiter import PRI_REFLEX


class PreFilter:
    """A cheap non-LLM salience scorer. Override score(raw) -> float in [0, 1]."""

    def score(self, raw) -> float:
        raise NotImplementedError


class FrameDiffFilter(PreFilter):
    """Vision pre-filter: salience = fraction of changed bytes vs the last frame (a stand-in for a
    real motion detector). Cheap, CPU, no GPU."""

    def __init__(self):
        self._last = None

    def score(self, frame) -> float:
        if self._last is None:
            self._last = frame
            return 1.0                                  # first frame: a novel scene is salient
        n = max(1, len(frame))
        diff = sum(1 for a, b in zip(frame, self._last) if a != b) + abs(len(frame) - len(self._last))
        self._last = frame
        return min(1.0, diff / n)


class VadFilter(PreFilter):
    """Audio pre-filter: salience = loudness above a floor (a voice-activity stand-in)."""

    def __init__(self, floor=0.2):
        self.floor = float(floor)

    def score(self, level) -> float:
        level = float(level)
        if level < self.floor:
            return 0.0
        return max(0.0, min(1.0, (level - self.floor) / (1.0 - self.floor)))


class Exteroceptor:
    """A sense: pre-filter cheaply; escalate only the salient (acquire GPU, tokenize -> percept)."""

    def __init__(self, bus, *, name, modality, prefilter, threshold=0.3, arbiter=None, tokenizer=None):
        self.bus = bus
        self.name = name
        self.modality = modality
        self.prefilter = prefilter
        self.threshold = float(threshold)
        self.arbiter = arbiter
        self.tokenizer = tokenizer or (lambda raw: {"summary": "<percept>"})
        self.escalations = 0
        self.dropped = 0

    def observe(self, raw):
        """Process one raw observation. Returns the published percept event, or None if pre-filtered
        out (below threshold the raw modality is dropped and NEVER reaches the core)."""
        sal = self.prefilter.score(raw)
        if sal < self.threshold:
            self.dropped += 1
            return None
        # salient -> the rare foveal path: acquire the GPU, tokenize, emit a bounded percept
        lease = self.arbiter.acquire(f"{self.name}-escalation", PRI_REFLEX, timeout=2.0) if self.arbiter else None
        try:
            percept = self.tokenizer(raw)   # e.g. Gemma A/V -> tokens (the only place the model sees it)
        finally:
            if lease is not None:
                self.arbiter.release(lease)
        self.escalations += 1
        payload = json.dumps(percept, ensure_ascii=False, default=str).encode("utf-8")
        ev = NervousEvent(SCHEMA_VERSION, self.name, Kind.percept, self.modality, Delivery.fungible,
                          salience=sal, t=time.monotonic())
        return self.bus.publish(ev, payload)
