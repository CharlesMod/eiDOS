"""The bus -> context bridge (P3).

`AfferentContext` is the deliberative core's afferent intake. It subscribes a "core" reader to the
bus and, ONCE per tick (at the tick boundary), drains the admitted events and renders them into a
compact text block for the volatile *situation* tail of the context (see context.py). KV-safe by
construction: the block is batched per tick into the volatile message — never the stable prefix or
the history turns (EIDOS_V3_ARCHITECTURE.md §5). Empty until organs publish, so wiring it into the
tick loop is inert until P1a (interoception) arrives.
"""
import json

from .event import Kind, Modality


class AfferentContext:
    def __init__(self, bus, *, max_events=12, max_chars=1500, topics=None, deliveries=None):
        self.bus = bus
        self.max_events = int(max_events)
        self.max_chars = int(max_chars)
        self.gate = None   # when attached (attach_gate), the core intake reads the gate's RANKED admission
        # Subscribe to everything by default; the bus delivery classes already gate volume, and a
        # real salience gate organ will refine what is admitted (P3+).
        self.sub = bus.subscribe(topics=topics, deliveries=deliveries)

    def attach_gate(self, gate):
        """Route the core intake through the salience gate's RANKED admission instead of this
        context's own raw-order subscription (H4: the gate computed a full admission — top-down
        relevance × arousal gain × habituation, with the exploration floor — that the core never
        read). UNSUBSCRIBE our own sub so events aren't double-delivered/leaked; the gate's admit()
        becomes the single intake. flag-off stays byte-identical because admit() is then a verbatim
        drain of the gate's own (identical) subscription."""
        self.gate = gate
        if self.sub is not None:
            try:
                self.bus.unsubscribe(self.sub)
            except Exception:  # noqa: BLE001
                pass
            self.sub = None

    @classmethod
    def from_config(cls, bus, config):
        return cls(bus,
                   max_events=getattr(config, "nervous_context_max_events", 12),
                   max_chars=getattr(config, "nervous_context_max_chars", 1500))

    # Bus self-health plumbing: the monitor's "numbness" alarms, not senses. They stay on the bus
    # for the monitor; rendering them into the creature's felt world taught it nothing (a live
    # block showed raw `consumer_dead_past_liveness_cap` JSON sitting among its senses).
    _PLUMBING_KINDS = (Kind.reliable_undeliverable, Kind.sequence_aborted)

    def drain_block(self):
        """Drain up to max_events admitted events (NON-blocking) and render a compact, char-capped
        block. Acks each event. Returns ('', 0) when nothing is admitted (the common idle case), so
        the tick's context is byte-identical to today when no organ has fired.

        COALESCED: repeated events from the same (modality, kind, organ) collapse to the FRESHEST
        one with a ×N count — a live block once spent 8 of its 12 lines on identical neuromod
        readings. A small mind's senses report change, not steady state, eight times over."""
        events = []
        if self.gate is not None:
            # Ranked admission (the gate acks each event it hands out). Guarantee-class events —
            # including the RETAINED felt-body — come first in bus order; fungibles follow by
            # descending admission_bias with the exploration floor. flag-off inside the gate makes
            # this a verbatim pass-through, so a disabled gate is byte-identical to the raw drain.
            for ev in self.gate.admit(self.max_events):
                if ev.kind in self._PLUMBING_KINDS:
                    continue
                events.append(ev)
        else:
            while len(events) < self.max_events:
                ev = self.bus.recv(self.sub, timeout=0.0)   # non-blocking drain
                if ev is None:
                    break
                self.bus.ack(ev)
                if ev.kind in self._PLUMBING_KINDS:
                    continue                                # acked, logged by the bus — not a sense
                events.append(ev)
        # Backfill the felt body from the RETAINED snapshot when no fresh interoceptive event arrived
        # this tick. Interoception free-runs on its own ~5s timer, decoupled from tick cadence, so on
        # most ticks nothing fresh sits in the mailbox — yet the current felt-state is still on the bus
        # as a retained (last-value) value. Reading ONLY the transient mailbox made the "body feels …"
        # line blink in and out of the prompt across ticks (intermittent numbness, no signal for
        # "unchanged" vs "unknown"); the whole point of retained delivery is to always read the current
        # body state. Only backfill when the retained payload is still renderable (retained payloads
        # aren't pinned, so a rare eviction leaves payloads.get() → None; skip rather than emit a
        # contentless generic line).
        if not any(ev.kind == Kind.interoceptive for ev in events):
            snap = self._retained_intero()
            if snap is not None and snap.payload_ref and self.bus.payloads.get(snap.payload_ref):
                events.append(snap)
        if not events:
            return "", 0
        # Coalesce: first-seen order, freshest payload wins, duplicates counted.
        slots: dict = {}
        for ev in events:
            key = (ev.modality, ev.kind, ev.source_organ)
            if key in slots:
                slots[key] = (slots[key][0] + 1, ev)    # newer event replaces; count grows
            else:
                slots[key] = (1, ev)
        lines = []
        for n, ev in slots.values():
            line = self._render(ev)
            lines.append(f"{line} ×{n}" if n > 1 else line)
        block = "\n".join(lines)
        if len(block) > self.max_chars:
            block = block[:self.max_chars].rsplit("\n", 1)[0] + "\n… (afferent truncated)"
        return block, len(events)

    def _retained_intero(self):
        """The current felt-state as a retained (last-value) event, or None. Mirrors reward._current_felt
        / monitor._retained_json, which already read the snapshot rather than the transient mailbox."""
        try:
            return self.bus.retained_snapshot(Kind.interoceptive, Modality.intero)
        except Exception:  # noqa: BLE001 - intake must never break the tick
            return None

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
        # Neuromodulatory state (Pillar 6): render the MOOD as felt language ("mind feels vigilant"),
        # the same projection neuromod._mood produces — not raw {arousal,valence,mood} JSON. This is
        # new felt info (mood/arousal), distinct from the interoceptive body line above; hunger is
        # already folded into that body line by interoception, so metabolism is not re-rendered here.
        if ev.kind == Kind.modulation and payload:
            try:
                d = json.loads(payload.decode("utf-8"))
                mood = d.get("mood") if isinstance(d, dict) else None
                if mood:
                    return "- mind feels %s" % mood
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
        if self.sub is None:   # already handed intake to the gate (attach_gate unsubscribed us)
            return
        try:
            self.bus.unsubscribe(self.sub)
        except Exception:
            pass
