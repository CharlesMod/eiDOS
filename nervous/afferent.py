"""The bus -> context bridge (P3).

`AfferentContext` is the deliberative core's afferent intake. It subscribes a "core" reader to the
bus and, ONCE per tick (at the tick boundary), drains the admitted events and renders them into a
compact text block for the volatile *situation* tail of the context (see context.py). KV-safe by
construction: the block is batched per tick into the volatile message — never the stable prefix or
the history turns (EIDOS_V3_ARCHITECTURE.md §5). Empty until organs publish, so wiring it into the
tick loop is inert until P1a (interoception) arrives.
"""
import json

from .event import Kind


class AfferentContext:
    def __init__(self, bus, *, max_events=12, max_chars=1500, topics=None, deliveries=None):
        self.bus = bus
        self.max_events = int(max_events)
        self.max_chars = int(max_chars)
        # Subscribe to everything by default; the bus delivery classes already gate volume, and a
        # real salience gate organ will refine what is admitted (P3+).
        self.sub = bus.subscribe(topics=topics, deliveries=deliveries)

    @classmethod
    def from_config(cls, bus, config):
        return cls(bus,
                   max_events=getattr(config, "nervous_context_max_events", 12),
                   max_chars=getattr(config, "nervous_context_max_chars", 1500))

    def drain_block(self):
        """Drain up to max_events admitted events (NON-blocking) and render a compact, char-capped
        block. Acks each event. Returns ('', 0) when nothing is admitted (the common idle case), so
        the tick's context is byte-identical to today when no organ has fired."""
        events = []
        while len(events) < self.max_events:
            ev = self.bus.recv(self.sub, timeout=0.0)   # non-blocking drain
            if ev is None:
                break
            events.append(ev)
            self.bus.ack(ev)
        if not events:
            return "", 0
        block = "\n".join(self._render(ev) for ev in events)
        if len(block) > self.max_chars:
            block = block[:self.max_chars].rsplit("\n", 1)[0] + "\n… (afferent truncated)"
        return block, len(events)

    def _render(self, ev):
        payload = self.bus.payloads.get(ev.payload_ref) if ev.payload_ref else None
        # Interoceptive felt-state (P1b): render the QUALIA (the Pantheon abstraction) — the creature
        # feels "running hot" or "mind resident on the GPU", not "vram: high / 92%" (high VRAM is the
        # resident mind, felt as calm posture, never distress). Same projection the render reads (I6).
        if ev.kind == Kind.interoceptive and payload:
            try:
                d = json.loads(payload.decode("utf-8"))
                if isinstance(d, dict) and "overall" in d:
                    felt = "; ".join(d.get("felt") or []) or "nothing amiss"
                    return "- body feels %s (%s)" % (d["overall"], felt)
            except Exception:
                pass
        bits = ["- [%s/%s]" % (ev.modality.value, ev.kind.value)]
        if ev.source_organ:
            bits.append("from %s" % ev.source_organ)
        if payload:
            try:
                txt = payload.decode("utf-8")
            except Exception:
                txt = "<%dB>" % len(payload)
            if len(txt) > 160:
                txt = txt[:160] + "…"
            bits.append(txt)
        if ev.salience:
            bits.append("(sal=%.2f)" % ev.salience)
        return " ".join(bits)

    def close(self):
        try:
            self.bus.unsubscribe(self.sub)
        except Exception:
            pass
