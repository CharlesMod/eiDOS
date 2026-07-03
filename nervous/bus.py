"""NervousBus — the one dumb seam (EIDOS_V3_ARCHITECTURE.md §3/§4, I1).

Generalizes the proven in-proc primitives:
  - `voice.py` speech pub/sub (set of subscribers + lock + drop-on-full),
  - `gpu_gate.py` Condition + monotonic-liveness blocking-acquire,
  - `dashboard.py` control channel (seq + Condition + last-value) for retained topics,
  - `tools.py` job-ledger reliable later-delivery + `glue.py` JSONL drop accounting.

The bus owns ALL delivery-class logic; the Transport only moves wire-dicts between buses. The bus
ALWAYS round-trips to_wire/from_wire (even in-proc), so the tri-mode equivalence gate is meaningful.

Delivery classes:
  fungible  — best-effort, drop-by-priority when a subscriber is full (every drop counted+logged).
  ordered   — in-order, atomic-abort (whole sequence or a `sequence_aborted`), never a hole.
  reliable  — never dropped under normal backpressure; priority floor; pins its payload until ack.
              Backstop: a reliable event stuck past `reliable_backpressure_max_s` (dead consumer) is
              dropped, logged, AND announced via a `reliable_undeliverable` event (the self-health alarm).
  retained  — last-value-wins global state; a late subscriber gets the current value immediately.
"""
import dataclasses
import heapq
import json
import logging
import threading
import time

from .event import (NervousEvent, Kind, Modality, Delivery, SCHEMA_VERSION,
                    RELIABLE_KINDS, RETAINED_KINDS, RELIABLE_FLOOR)
from .payload import PayloadStore
from .transport import InProcTransport

logger = logging.getLogger("eidos.nervous")


@dataclasses.dataclass
class PublishResult:
    delivered: int = 0     # subscribers the event reached
    dropped: int = 0       # fungible drops caused by this publish
    aborted: int = 0       # ordered sequences aborted by this publish
    rejected: bool = False # schema mismatch / admission rejection


class _Record:
    # `alive` supports lazy deletion from the mailbox heaps (T9): removing an arbitrary record from a
    # binary heap is O(n), so instead we clear the flag and skip cleared records when they surface at
    # a heap root. Popped/evicted/swept records set alive=False; live_count tracks the survivors.
    __slots__ = ("wire", "delivery", "admit_priority", "seq", "enqueued", "kind", "alive")

    def __init__(self, wire, delivery, admit_priority, seq, kind):
        self.wire = wire
        self.delivery = delivery
        self.admit_priority = admit_priority
        self.seq = seq
        self.enqueued = time.monotonic()
        self.kind = kind
        self.alive = True


class Subscription:
    """One reader. The mailbox is two indexed heaps over the same live records, so drain/admission
    are O(log n) instead of the old O(n) list scan (T9: the list tailed to ~p95 730 ms in-proc under
    flood vs ZMQ's ~2 ms — a real bug, not just theory). Ordered streams stage here until
    contiguous/aborted; retained/reliable/ordered are never dropped by priority, only fungible is.

    Two heaps share the *same* `_Record` objects, kept consistent by lazy deletion (skip cleared
    records at a root — see `_Record.alive`):
      `_pri`  — max-heap by (admit_priority, oldest-seq) for recv()'s pop-highest.
      `_fung` — min-heap by (admit_priority, oldest-seq) over FUNGIBLE records only, for eviction.
    `reliable` indexes reliable records by seq so the backstop sweep iterates the few reliable
    records instead of the whole mailbox. Heap tuples carry `seq` (unique, monotonic) as the second
    element so two records never compare `_Record` objects and ties resolve to the older seq."""

    def __init__(self, topics, deliveries, fungible_cap):
        self.topics = topics          # set of (Kind, Modality), or None = all
        self.deliveries = deliveries  # set of Delivery, or None = all
        self.fungible_cap = int(fungible_cap)
        self.lock = threading.Condition()
        self._pri = []                # max-heap: (-admit_priority, seq, _Record)  -> highest first
        self._fung = []               # min-heap: (admit_priority, seq, _Record)   -> lowest first
        self.reliable = {}            # seq -> _Record (reliable records, for the backstop sweep)
        self.live_count = 0           # live records across the heaps (recv's wait predicate)
        self.fungible_count = 0       # live fungible records (the fungible cap)
        self.ordered = {}             # sequence_id -> {"buf": {ordinal: _Record}, "next": int}
        self.dropped = 0
        self.closed = False

    def matches(self, kind, modality, delivery) -> bool:
        if self.topics is not None and (kind, modality) not in self.topics:
            return False
        if self.deliveries is not None and delivery not in self.deliveries:
            return False
        return True

    def _push(self, rec: _Record) -> None:
        """Admit a live record into the mailbox heaps. Caller holds `self.lock`."""
        heapq.heappush(self._pri, (-rec.admit_priority, rec.seq, rec))
        if rec.delivery == Delivery.fungible:
            heapq.heappush(self._fung, (rec.admit_priority, rec.seq, rec))
            self.fungible_count += 1
        elif rec.delivery == Delivery.reliable:
            self.reliable[rec.seq] = rec
        self.live_count += 1
        self._maybe_compact()

    def _maybe_compact(self) -> None:
        """Reclaim lazy-deletion garbage. A retired record's heap tuples linger until they reach a
        root; under a sustained flood the `_fung` heap in particular keeps tuples for fungibles that
        recv() popped from `_pri` (they never surface as a `_fung` root). Rebuild a heap once its
        dead entries pass half its size — the 2× slack bounds wasted space and makes the O(n) rebuild
        amortize to O(1) per push (at most one rebuild per n pushes). Caller holds `self.lock`."""
        if len(self._pri) > 2 * self.live_count + 8:  # +8: don't churn tiny heaps
            self._pri = [t for t in self._pri if t[2].alive]
            heapq.heapify(self._pri)
        if len(self._fung) > 2 * self.fungible_count + 8:
            self._fung = [t for t in self._fung if t[2].alive and t[2].delivery == Delivery.fungible]
            heapq.heapify(self._fung)

    def _retire(self, rec: _Record) -> None:
        """Mark a record dead (lazy delete) and drop the live/fungible/reliable accounting. Its heap
        slots are reclaimed when they surface at a root. Caller holds `self.lock`."""
        rec.alive = False
        self.live_count -= 1
        if rec.delivery == Delivery.fungible:
            self.fungible_count -= 1
        elif rec.delivery == Delivery.reliable:
            self.reliable.pop(rec.seq, None)

    def _pop_pri(self) -> _Record:
        """Pop the highest-priority live record; discard dead roots (lazy deletion). Returns None if
        the mailbox is empty. Caller holds `self.lock`."""
        while self._pri:
            _, _, rec = heapq.heappop(self._pri)
            if rec.alive:
                self._retire(rec)
                return rec
        return None

    def _peek_lowest_fungible(self) -> _Record:
        """The lowest-priority live fungible record (eviction victim), or None. Discards dead roots.
        Caller holds `self.lock`."""
        while self._fung:
            rec = self._fung[0][2]
            if rec.alive and rec.delivery == Delivery.fungible:
                return rec
            heapq.heappop(self._fung)
        return None


class NervousBus:
    def __init__(self, *, transport=None, payload_store=None, schema_version=SCHEMA_VERSION,
                 fungible_qsize=256, ordered_seq_max_buffered=512,
                 reliable_backpressure_max_s=30.0, ordered_backpressure_max_s=10.0,
                 admits_per_source_per_window=1000, admission_window_s=1.0,
                 payload_store_max_bytes=64 * 1024 * 1024,
                 drop_log_path=None, metrics_log_path=None, sweep_interval_s=0.5):
        self.schema_version = int(schema_version)
        self.fungible_qsize = int(fungible_qsize)
        self.ordered_seq_max_buffered = int(ordered_seq_max_buffered)
        self.reliable_backpressure_max_s = float(reliable_backpressure_max_s)
        self.ordered_backpressure_max_s = float(ordered_backpressure_max_s)
        self.admits_per_source_per_window = int(admits_per_source_per_window)
        self.admission_window_s = float(admission_window_s)
        self.drop_log_path = drop_log_path
        self.metrics_log_path = metrics_log_path

        self.payloads = payload_store or PayloadStore(payload_store_max_bytes)
        self.transport = transport or InProcTransport()

        self._lock = threading.Lock()        # guards _subs, _retained, _buckets, _seq
        self._subs = set()
        self._retained = {}                  # (kind, modality, source) -> wire
        self._buckets = {}                   # source -> [tokens, last_refill_monotonic]
        self._seq = 0
        self._log_lock = threading.Lock()
        self.stats_counters = {"published": 0, "delivered": 0, "dropped": 0,
                               "aborted": 0, "rejected": 0, "undeliverable": 0}

        self.transport.start(self._on_remote)
        self._stop = threading.Event()
        self._sweeper = threading.Thread(target=self._sweep_loop, args=(float(sweep_interval_s),),
                                         name="nervous-sweeper", daemon=True)
        self._sweeper.start()

    @classmethod
    def from_config(cls, config, transport=None, payload_store=None):
        return cls(
            transport=transport, payload_store=payload_store,
            schema_version=getattr(config, "nervous_schema_version", SCHEMA_VERSION),
            fungible_qsize=getattr(config, "nervous_fungible_qsize", 256),
            ordered_seq_max_buffered=getattr(config, "nervous_ordered_seq_max_buffered", 512),
            reliable_backpressure_max_s=getattr(config, "nervous_reliable_backpressure_max_s", 30.0),
            ordered_backpressure_max_s=getattr(config, "nervous_ordered_backpressure_max_s", 10.0),
            admits_per_source_per_window=getattr(config, "nervous_admits_per_source_per_window", 1000),
            admission_window_s=getattr(config, "nervous_admission_window_s", 1.0),
            payload_store_max_bytes=getattr(config, "nervous_payload_store_max_bytes", 64 * 1024 * 1024),
            drop_log_path=getattr(config, "nervous_drop_log_path", None),
            metrics_log_path=getattr(config, "nervous_metrics_log_path", None),
        )

    # ---- subscription ----------------------------------------------------------------
    def subscribe(self, *, topics=None, deliveries=None) -> Subscription:
        t = None if topics is None else set(topics)
        d = None if deliveries is None else set(deliveries)
        sub = Subscription(t, d, self.fungible_qsize)
        with self._lock:
            self._subs.add(sub)
            retained = list(self._retained.items())
        # A late subscriber gets the current retained value immediately (dashboard since/seq model).
        for (kind, modality, _source), wire in retained:
            if sub.matches(kind, modality, Delivery.retained):
                self._enqueue(sub, _Record(wire, Delivery.retained,
                                           float(wire.get("admit_priority", 0.0)),
                                           self._next_seq(), kind))
        return sub

    def unsubscribe(self, sub: Subscription) -> None:
        with self._lock:
            self._subs.discard(sub)
        with sub.lock:
            sub.closed = True
            sub.lock.notify_all()

    # ---- publish / inject ------------------------------------------------------------
    def publish(self, event: NervousEvent, payload: bytes = None) -> PublishResult:
        return self._admit(event, payload, forward=True)

    def _on_remote(self, wire: dict, payload: bytes):
        try:
            event = NervousEvent.from_wire(wire)
        except Exception as e:  # noqa: BLE001
            logger.debug("nervous bad wire dropped: %s", e)
            return
        self._admit(event, payload, forward=False)

    def _admit(self, event: NervousEvent, payload, forward: bool) -> PublishResult:
        res = PublishResult()
        # schema gate (T5 stub: reject mismatch + log; full negotiation at P3)
        if int(event.schema_version) != self.schema_version:
            self._record_drop(str(event.delivery.value if isinstance(event.delivery, Delivery) else event.delivery),
                              event.source_organ, "schema_mismatch")
            self.stats_counters["rejected"] += 1
            res.rejected = True
            return res

        eff = self._effective_delivery(event)
        admit_priority = (RELIABLE_FLOOR if eff == Delivery.reliable else 0.0) + float(event.salience)

        # fair admission (I10): fungible from an over-budget source is dropped first.
        if eff == Delivery.fungible and not self._take_token(event.source_organ):
            self._record_drop("fungible", event.source_organ, "source_budget")
            self.stats_counters["dropped"] += 1
            res.dropped += 1
            return res

        # payload: store + (for reliable/ordered) pin until ack.
        ref = event.payload_ref
        payload_bytes = payload
        if payload is not None:
            ref = self.payloads.put(payload)
        elif ref is not None:
            payload_bytes = self.payloads.get(ref)
        if eff in (Delivery.reliable, Delivery.ordered) and ref is not None:
            self.payloads.pin(ref)

        seq = self._next_seq()
        ev = dataclasses.replace(event, delivery=eff, admit_priority=admit_priority, payload_ref=ref)
        wire = ev.to_wire()
        self.stats_counters["published"] += 1

        # retained: update last-value, then deliver to matching subscribers.
        if eff == Delivery.retained:
            with self._lock:
                self._retained[(ev.kind, ev.modality, ev.source_organ)] = wire

        with self._lock:
            subs = list(self._subs)
        for sub in subs:
            if not sub.matches(ev.kind, ev.modality, eff):
                continue
            rec = _Record(wire, eff, admit_priority, seq, ev.kind)
            if eff == Delivery.ordered and ev.sequence_id is not None:
                res.aborted += self._stage_ordered(sub, ev, rec)
            else:
                delivered, dropped = self._enqueue(sub, rec)
                if delivered:
                    res.delivered += 1
                res.dropped += dropped

        if forward:
            self.transport.send(wire, payload_bytes)
        self.stats_counters["delivered"] += res.delivered
        self.stats_counters["dropped"] += res.dropped
        self.stats_counters["aborted"] += res.aborted
        return res

    # ---- per-subscriber enqueue ------------------------------------------------------
    def _enqueue(self, sub: Subscription, rec: _Record):
        """Returns (delivered: bool, dropped: int). dropped counts a drop CAUSED by this enqueue —
        either the incoming event (delivered=False) or an evicted lower-priority victim
        (delivered=True). Reliable/ordered/retained are never dropped here; only fungible is, by
        priority."""
        with sub.lock:
            if sub.closed:
                return (False, 0)
            if rec.delivery == Delivery.fungible:
                if sub.fungible_count >= sub.fungible_cap:
                    victim = sub._peek_lowest_fungible()
                    if victim is None or victim.admit_priority >= rec.admit_priority:
                        sub.dropped += 1
                        self._record_drop("fungible", rec.wire.get("source_organ", "?"), "queue_full")
                        return (False, 1)
                    # evict the lower-priority victim, admit the incoming
                    sub._retire(victim)
                    sub.dropped += 1
                    self._record_drop("fungible", victim.wire.get("source_organ", "?"), "queue_full")
                    sub._push(rec)
                    sub.lock.notify()
                    return (True, 1)
            sub._push(rec)
            sub.lock.notify()
            return (True, 0)

    def _stage_ordered(self, sub: Subscription, ev: NervousEvent, rec: _Record) -> int:
        """Stage an ordered event; release contiguous ordinals from `next`. Returns 1 if the
        sequence was aborted (overflow), else 0."""
        with sub.lock:
            if sub.closed:
                return 0
            sid = ev.sequence_id
            st = sub.ordered.get(sid)
            if st is None:
                st = {"buf": {}, "next": 0}
                sub.ordered[sid] = st
            st["buf"][int(ev.ordinal)] = rec
            # release contiguous
            while st["next"] in st["buf"]:
                r = st["buf"].pop(st["next"])
                sub._push(r)
                st["next"] += 1
            sub.lock.notify()
            # atomic abort on overflow: too many out-of-order staged → drop the whole sequence
            if len(st["buf"]) > self.ordered_seq_max_buffered:
                del sub.ordered[sid]
                self._record_drop("ordered", ev.source_organ, "ordered_overflow")
                aborted_ev, _ = self._control_event(Kind.sequence_aborted, sequence_id=sid)
                aborted_wire = aborted_ev.to_wire()
                sub._push(_Record(aborted_wire, Delivery.reliable, RELIABLE_FLOOR,
                                  self._next_seq(), Kind.sequence_aborted))
                sub.lock.notify()
                return 1
            return 0

    # ---- receive / ack ---------------------------------------------------------------
    def recv(self, sub: Subscription, timeout: float = None):
        """Blocking acquire (never a poll). Returns the highest-priority NervousEvent, or None on
        timeout/close. Reliable/ordered payloads stay pinned until you `ack`."""
        deadline = None if timeout is None else time.monotonic() + timeout
        with sub.lock:
            while sub.live_count == 0:
                if sub.closed:
                    return None
                if deadline is None:
                    sub.lock.wait()
                else:
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        return None
                    sub.lock.wait(timeout=remaining)
            rec = sub._pop_pri()   # O(log n) highest-priority pop (oldest seq breaks ties)
        return NervousEvent.from_wire(rec.wire)

    def ack(self, event: NervousEvent) -> None:
        """Release a reliable/ordered event (unpin its payload). Idempotent / safe for any event."""
        if event.payload_ref is not None:
            self.payloads.unpin(event.payload_ref)

    def retained_snapshot(self, kind, modality, source=None):
        with self._lock:
            if source is not None:
                w = self._retained.get((kind, modality, source))
                return NervousEvent.from_wire(w) if w else None
            for (k, m, _s), w in self._retained.items():
                if k == kind and m == modality:
                    return NervousEvent.from_wire(w)
        return None

    # ---- reliable backstop (self-health) ---------------------------------------------
    def _sweep_loop(self, interval_s: float):
        while not self._stop.wait(interval_s):
            try:
                self._sweep_reliable_backstop()
            except Exception as e:  # noqa: BLE001
                logger.debug("nervous sweep error: %s", e)

    def _sweep_reliable_backstop(self) -> None:
        """A reliable event stuck in a mailbox past the liveness cap means a dead/wedged consumer:
        drop it, log it, AND emit a `reliable_undeliverable` alarm. Never wedges (ARCH #2)."""
        now = time.monotonic()
        with self._lock:
            subs = list(self._subs)
        for sub in subs:
            stale = []
            with sub.lock:
                # Only reliable records can go stale; iterate the reliable index, not the whole
                # mailbox. `_retire` lazily removes them from `_pri` (dead roots are skipped on pop).
                for r in list(sub.reliable.values()):
                    if (r.kind not in (Kind.sequence_aborted, Kind.reliable_undeliverable)
                            and now - r.enqueued > self.reliable_backpressure_max_s):
                        sub._retire(r)
                        stale.append(r)
            for r in stale:
                self.ack(NervousEvent.from_wire(r.wire))  # unpin
                self._record_drop("reliable", r.wire.get("source_organ", "?"), "undeliverable")
                self.stats_counters["undeliverable"] += 1
                alarm = self._control_event(
                    Kind.reliable_undeliverable,
                    payload={"source": r.wire.get("source_organ"), "kind": r.wire.get("kind"),
                             "reason": "consumer_dead_past_liveness_cap"})
                self.publish(alarm[0], alarm[1])

    # ---- helpers ---------------------------------------------------------------------
    def _effective_delivery(self, event: NervousEvent) -> Delivery:
        if event.kind in RETAINED_KINDS:
            return Delivery.retained
        if event.kind in RELIABLE_KINDS:
            return Delivery.reliable
        return event.delivery

    def _control_event(self, kind: Kind, *, sequence_id=None, payload=None):
        """Build a bus-emitted control/health event (+ optional small json payload bytes)."""
        pbytes = None if payload is None else json.dumps(payload, ensure_ascii=False).encode("utf-8")
        ev = NervousEvent(schema_version=self.schema_version, source_organ="bus",
                          kind=kind, modality=Modality.system, delivery=Delivery.reliable,
                          t=time.monotonic(), sequence_id=sequence_id)
        return (ev, pbytes)

    def _take_token(self, source: str) -> bool:
        now = time.monotonic()
        with self._lock:
            b = self._buckets.get(source)
            if b is None:
                b = [float(self.admits_per_source_per_window), now]
                self._buckets[source] = b
            # refill
            elapsed = now - b[1]
            if elapsed >= self.admission_window_s:
                b[0] = float(self.admits_per_source_per_window)
                b[1] = now
            if b[0] >= 1.0:
                b[0] -= 1.0
                return True
            return False

    def _next_seq(self) -> int:
        with self._lock:
            self._seq += 1
            return self._seq

    def _record_drop(self, delivery_class: str, source: str, reason: str) -> None:
        if not self.drop_log_path:
            return
        entry = {"ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                 "delivery_class": delivery_class, "source_organ": source, "reason": reason}
        try:
            with self._log_lock:
                with open(self.drop_log_path, "a", encoding="utf-8") as f:
                    f.write(json.dumps(entry, ensure_ascii=False) + "\n")
        except Exception as e:  # noqa: BLE001 - logging must never break the bus
            logger.debug("nervous drop-log write failed: %s", e)

    def stats(self) -> dict:
        with self._lock:
            n_subs = len(self._subs)
            n_retained = len(self._retained)
        d = dict(self.stats_counters)
        d.update({"subscribers": n_subs, "retained_topics": n_retained,
                  "payloads": self.payloads.stats()})
        return d

    def close(self) -> None:
        self._stop.set()
        try:
            self._sweeper.join(timeout=1.0)
        except Exception:
            pass
        try:
            self.transport.close()
        except Exception:
            pass
        with self._lock:
            subs = list(self._subs)
        for sub in subs:
            self.unsubscribe(sub)
