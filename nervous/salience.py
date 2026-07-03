"""Pillars 1.3 — the salience gate (PILLARS_PLAN.md §4 N-2, the present-pillar's core).

The V3 audit's sharpest finding: delivery classes carry the whole admission load and the designed
`relevance_set` topic has no publisher — the creature cannot weight one signal over another. This
organ computes, per pending afferent, an **admission bias**:

    admission_bias = bottom_up × top_down × gain

  bottom_up — from the event's own properties: its published `salience` (magnitude) damped by
              recency-repetition of the same (source, kind, modality) key (habituation — a signal
              repeating every tick stops out-competing a fresh one);
  top_down  — similarity of the event to the current `relevance_set`, the topic the core publishes
              via `publish_relevance_set` (the designed-but-unpublished seam this module provides).
              Fail-open: nothing published → neutral 1.0 (the gate biases nothing it doesn't know);
  gain      — the neuromodulatory arousal read from the retained `modulation` topic, *stamped at
              ingest* (arousal at encoding colors salience, mirroring the engram's emotional stamp;
              the stamp also makes the ×gain term a real cross-event orderer — events taken in
              while vigilant carry more weight than events taken in while drowsy). Fail-open 1.0.

The bias is a field over ADMISSION ORDER only — which pending events surface first within a
budget. It never drops or delays a guarantee-class event behind a fungible one: reliable /
ordered / retained events surface FIRST, in exactly the order the bus's own priority heap
released them (the RELIABLE_FLOOR "reliable outranks fungible" semantic, ordered streams'
in-sequence release, and retained last-value delivery are preserved by construction — the gate
re-ranks only the droppable fungible class, the one class the bus itself already drops by
priority). Un-admitted fungibles stay pending; only a bounded pending pool evicts, lowest-bias
first (the same drop-by-priority contract the bus mailbox applies, counted the same way).

Dark behind `pillars_salience_gate_enabled`: flag off, `admit()` is a verbatim pass-through of
the bus subscription's own delivery order — byte-identical to no gate at all.

§0 discipline: every name here is a mechanism (bias, admission, relevance, gain), never the
hoped-for behavior; whatever the creature appears to concentrate on is what the field produces.
"""
import collections
import json
import random
import re
import time

from .event import NervousEvent, Kind, Modality, Delivery, SCHEMA_VERSION

# --- declared constants (every constant derived or declared — PILLARS_TODO.md §0) --------------

# Exploration floor: the share of each fungible admission budget reserved for a uniform sample of
# the NON-top-ranked pending events, minimum one slot whenever more is pending than fits. Without
# it the gate is a perfect echo chamber — only what already matches the relevance_set ever
# surfaces, so the relevance_set can never learn it is wrong (the Matthew effect at the senses;
# same doctrine as recall's anti-Matthew slot, PILLARS_TODO.md 2.2). 1/8 mirrors the skill
# economy's ε (3.2): small enough not to blunt the field, large enough to be a steady trickle.
EXPLORATION_SHARE = 0.125
EXPLORATION_MIN_SLOTS = 1

# Bottom-up base: keeps a zero-salience event's bias non-zero so top-down relevance and the
# exploration floor can still surface it (a multiplicative field must never hard-zero a factor).
BOTTOM_UP_BASE = 0.1

# Top-down span: a full relevance_set match multiplies bias by (1 + RELEVANCE_SPAN); a non-match
# stays neutral 1.0 (relevance ELEVATES what matches, it never suppresses what doesn't — only the
# budget does). 4.0 makes a full match dominate a 5× loudness gap, so a quiet on-relevance signal
# outranks loud off-relevance noise; a partial match scales linearly.
RELEVANCE_SPAN = 4.0

# Habituation: each recent repeat of the same (source, kind, modality) key within the novelty
# window damps bottom_up by 1/(1 + NOVELTY_DAMP·repeats). 0.5 halves a signal's edge after two
# repeats without ever zeroing it; the window (64) covers a few ticks of a chatty organ.
NOVELTY_DAMP = 0.5
NOVELTY_WINDOW = 64

# Neuromod gain span: arousal 0 → 0.5, arousal 1 → 1.5, absent → exactly 1.0 (fail-open neutral).
GAIN_MIN, GAIN_MAX = 0.5, 1.5

# Bounded pending pool for fungibles (mirrors the bus's fungible_qsize default); overflow evicts
# lowest-bias first — the same drop-by-priority contract the bus mailbox already applies.
FUNGIBLE_POOL_CAP = 256

_TOKEN_RE = re.compile(r"[a-z0-9_]+")


def _tokens(text):
    return frozenset(_TOKEN_RE.findall(text.lower()))


def publish_relevance_set(bus, terms, *, source="core"):
    """The publisher seam for the designed-but-unpublished `relevance_set` topic (V3 §6).

    The deliberative core calls this each tick with its current focus terms; the event rides the
    bus as a reliable `relevance_set` (RELIABLE_KINDS) so the gate — and any other organ — can
    subscribe to it. This module only PROVIDES the seam; wiring eidos.py to call it is the
    Phase 5.5 cutover's job. Returns the bus PublishResult.
    """
    payload = json.dumps({"terms": [str(t) for t in terms]}, ensure_ascii=False).encode("utf-8")
    ev = NervousEvent(SCHEMA_VERSION, source, Kind.relevance_set, Modality.system,
                      Delivery.reliable, t=time.monotonic())
    return bus.publish(ev, payload)


class _Pending:
    """One pooled afferent: the event plus its ingest-time stamps (match tokens, bottom-up score,
    the arousal gain at encoding). `idx` is the arrival order — the deterministic tiebreak."""
    __slots__ = ("idx", "event", "tokens", "bottom_up", "gain")

    def __init__(self, idx, event, tokens, bottom_up, gain):
        self.idx = idx
        self.event = event
        self.tokens = tokens
        self.bottom_up = bottom_up
        self.gain = gain


class SalienceGate:
    """The salience-gate organ: a bias field over afferent admission (never an instruction).

    Sits between the bus and the deliberative core's intake: `ingest()` drains the gate's bus
    subscription into a pending pool, `admit(budget)` surfaces the next `budget` events —
    guarantee-class first in bus order, then fungibles by descending admission_bias with the
    exploration floor. Flag off, both collapse to a verbatim pass-through of bus delivery order.

    Registered via the 1.1 OrganRegistry with a **pre_tick** hook: intake must be drained and
    scored BEFORE the tick's deliberation compiles its context, so the admission the core reads
    THIS tick reflects the current bias field — a post_tick hook would leave every tick reading
    intake ranked by the previous tick's field.
    """

    def __init__(self, bus, *, config=None, topics=None, deliveries=None,
                 pool_cap=FUNGIBLE_POOL_CAP, rng=None, source="salience_gate"):
        self.bus = bus
        self.source = source
        self.enabled = bool(getattr(config, "pillars_salience_gate_enabled", False))
        self.pool_cap = int(pool_cap)
        self._rng = rng or random.Random()
        self.sub = bus.subscribe(topics=topics, deliveries=deliveries)
        # A dedicated relevance_set reader so a gate constructed with narrow afferent topics still
        # hears the core's published relevance. Only when enabled — a dark gate consumes nothing.
        self._rel_sub = (bus.subscribe(topics={(Kind.relevance_set, Modality.system)})
                         if self.enabled else None)
        self._relevance = frozenset()          # token set of the current relevance_set
        self._guaranteed = collections.deque() # reliable/ordered/retained, in bus-release order
        self._fungible = []                    # list[_Pending]
        self._recent = collections.deque(maxlen=NOVELTY_WINDOW)  # (source, kind, modality) keys
        self._arrival = 0
        self.evicted = 0

    # ---- organ lifecycle ---------------------------------------------------------------------
    def register(self, registry):
        """Plug into the 1.1 OrganRegistry (pre_tick — see class docstring for why not post)."""
        return registry.register(self, name=self.source, pre_tick=self._pre_tick,
                                 reads=("relevance_set/system", "modulation/system"),
                                 writes=())

    def _pre_tick(self, ctx):  # noqa: ARG002 - the hook signature is f(ctx); the gate needs none of it
        if self.enabled:
            self.ingest()

    # ---- intake --------------------------------------------------------------------------------
    def ingest(self):
        """Drain everything pending on the bus subscription into the pool, stamping each fungible
        with its ingest-time bottom-up score, match tokens, and the arousal gain at encoding.
        relevance_set events are consumed here (the gate IS their designed subscriber), never
        forwarded downstream. No-op when the flag is off (pass-through keeps the bus order)."""
        if not self.enabled:
            return
        self._drain_relevance()
        gain = self._neuromod_gain()
        while True:
            ev = self.bus.recv(self.sub, timeout=0.0)
            if ev is None:
                break
            if ev.kind == Kind.relevance_set:
                self._set_relevance(ev)
                self.bus.ack(ev)
                continue
            if ev.delivery == Delivery.fungible:
                self._fungible.append(_Pending(self._next_idx(), ev, self._event_tokens(ev),
                                               self._bottom_up(ev), gain))
            else:
                # Guarantee class (reliable / ordered / retained): pooled in exactly the order the
                # bus's own priority heap released them — the gate never re-ranks these, so the
                # reliable floor, ordered in-sequence release, and retained delivery hold EXACTLY.
                self._guaranteed.append(ev)
        self._evict_overflow()

    def _drain_relevance(self):
        while True:
            ev = self.bus.recv(self._rel_sub, timeout=0.0)
            if ev is None:
                break
            self._set_relevance(ev)
            self.bus.ack(ev)

    def _set_relevance(self, ev):
        payload = self.bus.payloads.get(ev.payload_ref) if ev.payload_ref else None
        if not payload:
            return
        try:
            terms = json.loads(payload.decode("utf-8")).get("terms") or []
        except Exception:
            return
        self._relevance = _tokens(" ".join(str(t) for t in terms))

    # ---- the bias field --------------------------------------------------------------------------
    def _bottom_up(self, ev):
        """From the event's own properties: published salience (magnitude) over a non-zero base,
        damped by recent repetition of the same key (habituation)."""
        key = (ev.source_organ, ev.kind, ev.modality)
        repeats = sum(1 for k in self._recent if k == key)
        self._recent.append(key)
        return (BOTTOM_UP_BASE + max(0.0, float(ev.salience))) / (1.0 + NOVELTY_DAMP * repeats)

    def _top_down(self, rec):
        """Similarity of the pooled event to the CURRENT relevance_set — recomputed at admit time,
        so a relevance change re-ranks what is already pending. Fail-open neutral 1.0."""
        if not self._relevance:
            return 1.0
        overlap = len(self._relevance & rec.tokens) / len(self._relevance)
        return 1.0 + RELEVANCE_SPAN * overlap

    def _bias(self, rec):
        return rec.bottom_up * self._top_down(rec) * rec.gain

    def _neuromod_gain(self):
        """Arousal from the retained `modulation` topic (the neuromod organ's broadcast), mapped
        to [GAIN_MIN, GAIN_MAX]. Fail-open: no modulation published → exactly neutral 1.0."""
        snap = self.bus.retained_snapshot(Kind.modulation, Modality.system)
        if snap is None:
            return 1.0
        arousal = None
        if snap.payload_ref:
            payload = self.bus.payloads.get(snap.payload_ref)
            if payload:
                try:
                    arousal = float(json.loads(payload.decode("utf-8")).get("arousal"))
                except Exception:
                    arousal = None
        if arousal is None:
            arousal = float(snap.salience)  # neuromod publishes salience=arousal (neuromod.py)
        arousal = max(0.0, min(1.0, arousal))
        return GAIN_MIN + arousal * (GAIN_MAX - GAIN_MIN)

    def _event_tokens(self, ev):
        bits = [ev.source_organ, ev.kind.value, ev.modality.value]
        payload = self.bus.payloads.get(ev.payload_ref) if ev.payload_ref else None
        if payload:
            try:
                bits.append(payload.decode("utf-8"))
            except Exception:
                pass
        return _tokens(" ".join(bits))

    # ---- admission -------------------------------------------------------------------------------
    def admit(self, budget):
        """Surface up to `budget` pending events. Flag off: a verbatim pass-through of the bus
        subscription's own delivery order (byte-identical to no gate). Flag on: guarantee-class
        events first in bus order (never dropped, never deferred behind a fungible), then
        fungibles by descending admission_bias with the exploration floor. Acks every event it
        hands out (the AfferentContext contract)."""
        budget = int(budget)
        if budget <= 0:
            return []
        if not self.enabled:
            out = []
            while len(out) < budget:
                ev = self.bus.recv(self.sub, timeout=0.0)
                if ev is None:
                    break
                self.bus.ack(ev)
                out.append(ev)
            return out

        self.ingest()  # idempotent; catches anything published since pre_tick
        admitted = []
        while self._guaranteed and len(admitted) < budget:
            ev = self._guaranteed.popleft()
            self.bus.ack(ev)
            admitted.append(ev)

        slots = budget - len(admitted)
        if slots > 0 and self._fungible:
            ranked = sorted(self._fungible, key=lambda r: (-self._bias(r), r.idx))
            if len(ranked) <= slots:
                chosen = ranked
            else:
                n_explore = 0
                if slots > 1:
                    # NE explore/exploit switch (plan §1 table): low arousal widens the sampled
                    # share (a drowsy field wanders), high arousal narrows it — bounded by the
                    # hard floor below and never the whole budget.
                    share = EXPLORATION_SHARE * (2.0 - self._neuromod_gain())
                    n_explore = min(max(EXPLORATION_MIN_SLOTS, int(slots * share)), slots - 1)
                top = ranked[:slots - n_explore]
                rest = ranked[slots - n_explore:]
                chosen = top + (self._rng.sample(rest, n_explore) if n_explore else [])
            chosen_ids = {id(r) for r in chosen}
            self._fungible = [r for r in self._fungible if id(r) not in chosen_ids]
            for rec in chosen:
                self.bus.ack(rec.event)
                admitted.append(rec.event)
        return admitted

    def _evict_overflow(self):
        """Bounded pending pool: past the cap, evict lowest-bias fungibles first — the same
        drop-by-priority contract the bus mailbox applies to this class. Guarantee-class events
        are never evicted (their deque is bounded by the bus's own backpressure machinery)."""
        overflow = len(self._fungible) - self.pool_cap
        if overflow <= 0:
            return
        self._fungible.sort(key=lambda r: (-self._bias(r), r.idx))
        for rec in self._fungible[self.pool_cap:]:
            self.bus.ack(rec.event)
        del self._fungible[self.pool_cap:]
        self.evicted += overflow

    def _next_idx(self):
        self._arrival += 1
        return self._arrival

    # ---- introspection / teardown ----------------------------------------------------------------
    def stats(self):
        return {"enabled": self.enabled, "pending_fungible": len(self._fungible),
                "pending_guaranteed": len(self._guaranteed), "evicted": self.evicted,
                "relevance_terms": len(self._relevance)}

    def close(self):
        for sub in (self.sub, self._rel_sub):
            if sub is not None:
                try:
                    self.bus.unsubscribe(sub)
                except Exception:
                    pass
