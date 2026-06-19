"""P0 gates for the V3 nervous-system bus (EIDOS_V3_ARCHITECTURE.md §8).

Each test is RED-ABLE: it fails on a correctness violation, not a smoke check. Heavy/load and
ZMQ-setup tests are marked `slow` so `pytest tests/ -m "not slow"` stays fast.
"""
import json
import os
import shutil
import sys
import tempfile
import time
import unittest

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from nervous import (NervousBus, NervousEvent, Kind, Modality, Delivery,  # noqa: E402
                     ZmqTransport, PayloadStore)
from nervous.event import SCHEMA_VERSION  # noqa: E402


def ev(source="s", kind=Kind.sensory, modality=Modality.time, delivery=Delivery.fungible,
       salience=0.0, t=None, schema_version=SCHEMA_VERSION, **kw):
    return NervousEvent(schema_version, source, kind, modality, delivery,
                        salience=salience, t=(time.monotonic() if t is None else t), **kw)


def drain(bus, sub, timeout=0.4, limit=100000):
    out = []
    while len(out) < limit:
        e = bus.recv(sub, timeout=timeout)
        if e is None:
            break
        out.append(e)
        bus.ack(e)
    return out


class BusTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="nervous-")
        self._buses = []

    def tearDown(self):
        for b in self._buses:
            try:
                b.close()
            except Exception:
                pass
        shutil.rmtree(self.tmp, ignore_errors=True)

    def make_bus(self, **kw):
        b = NervousBus(**kw)
        self._buses.append(b)
        return b

    def droplog(self):
        return os.path.join(self.tmp, "drops.jsonl")

    def read_drops(self):
        p = self.droplog()
        if not os.path.exists(p):
            return []
        with open(p, encoding="utf-8") as f:
            return [json.loads(line) for line in f if line.strip()]


class TestContract(BusTest):
    def test_event_wire_roundtrip_identity(self):
        # the single serialization seam must be lossless for every kind/delivery (I3/I9)
        cases = [
            ev("a", Kind.sensory, Modality.vision, Delivery.fungible, salience=0.42, t=1.0),
            ev("b", Kind.action_request, Modality.device, Delivery.reliable, salience=0.1, t=2.0),
            ev("c", Kind.percept, Modality.audio, Delivery.ordered, t=3.0, sequence_id="s", ordinal=7),
            ev("d", Kind.modulation, Modality.system, Delivery.retained, precision=0.9, t=4.0),
            ev("e", Kind.interoceptive, Modality.intero, Delivery.fungible, payload_ref="sha256:deadbeef"),
        ]
        for e in cases:
            self.assertEqual(NervousEvent.from_wire(e.to_wire()), e)
            # and json-safe
            self.assertEqual(NervousEvent.from_wire(json.loads(json.dumps(e.to_wire()))), e)


class TestFungible(BusTest):
    def test_fungible_drops_by_priority(self):
        bus = self.make_bus(fungible_qsize=4, drop_log_path=self.droplog())
        sub = bus.subscribe()
        for sal in (0.1, 0.2, 0.3, 0.4):
            bus.publish(ev("s", salience=sal, delivery=Delivery.fungible))
        # full now (4). A higher-priority event evicts the lowest (0.1).
        r_hi = bus.publish(ev("s", salience=0.9, delivery=Delivery.fungible))
        self.assertEqual(r_hi.delivered, 1)
        self.assertEqual(r_hi.dropped, 1)
        # a lower-priority event than everything queued is itself dropped.
        r_lo = bus.publish(ev("s", salience=0.05, delivery=Delivery.fungible))
        self.assertEqual(r_lo.delivered, 0)
        self.assertEqual(r_lo.dropped, 1)
        got = [e.salience for e in drain(bus, sub)]
        self.assertEqual(got, [0.9, 0.4, 0.3, 0.2])     # highest-priority first, 0.1 evicted
        self.assertNotIn(0.1, got)
        drops = self.read_drops()
        self.assertGreaterEqual(len(drops), 2)
        self.assertTrue(all(d["reason"] == "queue_full" for d in drops))


class TestOrdered(BusTest):
    def test_ordered_no_hole(self):
        bus = self.make_bus()
        sub = bus.subscribe()
        for o in range(10):
            bus.publish(ev("s", kind=Kind.percept, modality=Modality.audio,
                           delivery=Delivery.ordered, sequence_id="s1", ordinal=o))
        got = [e.ordinal for e in drain(bus, sub) if e.kind == Kind.percept]
        self.assertEqual(got, list(range(10)))   # in order, no hole

    def test_ordered_atomic_abort(self):
        bus = self.make_bus(ordered_seq_max_buffered=4, drop_log_path=self.droplog())
        sub = bus.subscribe()
        # release 0,1; then withhold 2 and flood 3..7 -> staging overflows -> atomic abort
        for o in (0, 1, 3, 4, 5, 6, 7):
            bus.publish(ev("s", kind=Kind.percept, modality=Modality.audio,
                           delivery=Delivery.ordered, sequence_id="sx", ordinal=o))
        got = drain(bus, sub)
        ordinals = sorted(e.ordinal for e in got if e.kind == Kind.percept)
        self.assertEqual(ordinals, [0, 1])                       # only the contiguous prefix
        self.assertTrue(any(e.kind == Kind.sequence_aborted for e in got))  # and an abort signal
        self.assertFalse(any((e.ordinal or 0) >= 3 for e in got if e.kind == Kind.percept))  # never a hole
        self.assertTrue(any(d["reason"] == "ordered_overflow" for d in self.read_drops()))


class TestReliable(BusTest):
    @pytest.mark.slow
    def test_reliable_never_dropped_under_load(self):
        bus = self.make_bus(fungible_qsize=20, drop_log_path=self.droplog())
        sub = bus.subscribe()
        n_reliable = 300
        for i in range(n_reliable):
            bus.publish(ev("eff", kind=Kind.action_request, modality=Modality.device,
                           delivery=Delivery.reliable, salience=(i % 7) / 7.0))
            for _ in range(8):  # fungible flood between every reliable
                bus.publish(ev("noise", salience=0.01, delivery=Delivery.fungible))
        got = drain(bus, sub, timeout=0.5)
        reliable = [e for e in got if e.kind == Kind.action_request]
        self.assertEqual(len(reliable), n_reliable)   # not one reliable lost
        self.assertGreater(len([e for e in got if e.kind == Kind.sensory]), 0)  # some fungible got through
        self.assertTrue(any(d["reason"] == "queue_full" for d in self.read_drops()))  # fungible WAS dropped

    def test_reliable_undeliverable_emits_alarm(self):
        bus = self.make_bus(reliable_backpressure_max_s=0.2, sweep_interval_s=0.05,
                            drop_log_path=self.droplog())
        dead = bus.subscribe()        # a consumer that never recv()s
        monitor = bus.subscribe()     # watches for the alarm
        # publish a reliable event to all subscribers; `dead` lets it go stale
        t0 = time.monotonic()
        bus.publish(ev("x", kind=Kind.action_request, modality=Modality.device, delivery=Delivery.reliable))
        self.assertLess(time.monotonic() - t0, 0.1)   # publish never wedges (ARCH #2)
        # collect from the monitor until we see the alarm (or time out)
        alarm = None
        deadline = time.monotonic() + 1.5
        while time.monotonic() < deadline:
            e = bus.recv(monitor, timeout=0.2)
            if e is not None and e.kind == Kind.reliable_undeliverable:
                alarm = e
                break
        self.assertIsNotNone(alarm, "expected a reliable_undeliverable self-health alarm")
        self.assertTrue(any(d["reason"] == "undeliverable" for d in self.read_drops()))
        _ = dead


class TestRetained(BusTest):
    def test_retained_late_subscriber_gets_current(self):
        bus = self.make_bus()
        bus.publish(ev("neuro", kind=Kind.modulation, modality=Modality.system,
                       delivery=Delivery.retained, precision=0.7))
        late = bus.subscribe()        # subscribes AFTER the retained value was set
        e = bus.recv(late, timeout=0.5)
        self.assertIsNotNone(e, "late subscriber should immediately get the current retained value")
        self.assertEqual(e.kind, Kind.modulation)
        self.assertEqual(e.precision, 0.7)


class TestPayload(BusTest):
    def test_payload_ref_immutable_no_torn_read(self):
        store = PayloadStore(max_bytes=120)
        r1 = store.put(b"A" * 50)
        self.assertEqual(r1, store.put(b"A" * 50))           # idempotent: same bytes -> same ref
        store.pin(r1)                                        # held by a reliable/ordered event
        store.put(b"B" * 50)
        store.put(b"C" * 50)                                 # forces eviction past 120 bytes
        self.assertEqual(store.get(r1), b"A" * 50)           # pinned ref survives (no torn read)
        store.unpin(r1)
        for i in range(5):
            store.put(bytes([i]) * 50)                       # now r1 is evictable
        self.assertIsNone(store.get(r1))                     # gone once unpinned + pressured out


class TestSchema(BusTest):
    def test_schema_mismatch_rejected(self):
        bus = self.make_bus(schema_version=1, drop_log_path=self.droplog())
        sub = bus.subscribe()
        r = bus.publish(ev("s", schema_version=99))
        self.assertTrue(r.rejected)
        self.assertIsNone(bus.recv(sub, timeout=0.2))        # not delivered
        self.assertTrue(any(d["reason"] == "schema_mismatch" for d in self.read_drops()))


class TestFairAdmission(BusTest):
    @pytest.mark.slow
    def test_fair_admission(self):
        # tiny per-source budget, long window so the bucket doesn't refill mid-test; big queue so
        # admission (not the queue) is the limiter.
        bus = self.make_bus(admits_per_source_per_window=10, admission_window_s=30.0,
                            fungible_qsize=10000, drop_log_path=self.droplog())
        sub = bus.subscribe()
        for _ in range(200):
            bus.publish(ev("greedy", salience=0.5, delivery=Delivery.fungible))
        for _ in range(200):
            bus.publish(ev("polite", salience=0.5, delivery=Delivery.fungible))
        got = drain(bus, sub)
        per = {}
        for e in got:
            per[e.source_organ] = per.get(e.source_organ, 0) + 1
        # neither source admitted more than its budget; the polite one is NOT starved by the flood.
        self.assertLessEqual(per.get("greedy", 0), 12)
        self.assertGreaterEqual(per.get("polite", 0), 8)
        self.assertTrue(any(d["reason"] == "source_budget" for d in self.read_drops()))


class TestTransports(BusTest):
    @pytest.mark.slow
    def test_tri_mode_byte_identical(self):
        # A fixed, deterministic event set must produce the IDENTICAL received wire-dicts whether the
        # consumer is in-process or a ZMQ hop away (in-proc / cross-proc / cross-device share one
        # contract; loopback exercises the full serialize->socket->deserialize round-trip).
        def fixed_events():
            return [
                ev("a", Kind.sensory, Modality.vision, Delivery.fungible, salience=0.1, t=1.0),
                ev("a", Kind.sensory, Modality.vision, Delivery.fungible, salience=0.5, t=2.0),
                ev("b", Kind.action_request, Modality.device, Delivery.reliable, salience=0.3, t=3.0),
                ev("c", Kind.modulation, Modality.system, Delivery.retained, precision=0.7, t=4.0),
            ]

        def collect(received):
            return sorted(json.dumps(e.to_wire(), sort_keys=True) for e in received)

        # in-proc
        ip = self.make_bus()
        sub = ip.subscribe()
        for i, e in enumerate(fixed_events()):
            ip.publish(e, ("p%d" % i).encode())
        inproc = collect(drain(ip, sub, timeout=0.5))

        # zmq (loopback = cross-device proxy)
        a = self.make_bus(transport=ZmqTransport(bind="tcp://0.0.0.0:8201"))
        b = self.make_bus(transport=ZmqTransport(connect="tcp://127.0.0.1:8201"))
        bsub = b.subscribe()
        time.sleep(0.5)
        for i, e in enumerate(fixed_events()):
            a.publish(e, ("p%d" % i).encode())
        zmq_recv = collect(drain(b, bsub, timeout=0.8))

        self.assertEqual(len(inproc), 4)
        self.assertEqual(inproc, zmq_recv)   # byte-identical across transports

    def test_remote_organ_vanish_is_severed_nerve(self):
        a = self.make_bus(transport=ZmqTransport(bind="tcp://0.0.0.0:8211"))
        b = self.make_bus(transport=ZmqTransport(connect="tcp://127.0.0.1:8211"))
        bsub = b.subscribe()
        time.sleep(0.4)
        a.publish(ev("sense", salience=0.5))
        self.assertIsNotNone(b.recv(bsub, timeout=1.0))   # link works
        a.close()                                          # the peer vanishes (severed nerve)
        # b must NOT wedge: recv returns None on timeout, and b stays operable locally.
        self.assertIsNone(b.recv(bsub, timeout=0.3))
        local = b.subscribe()
        b.publish(ev("local", salience=0.5))
        self.assertIsNotNone(b.recv(local, timeout=0.5))   # b is alive, not wedged


class TestFirehoseMetrics(BusTest):
    @pytest.mark.slow
    def test_firehose_reports_metrics(self):
        from nervous import firehose
        for transport, port in (("inproc", 0), ("zmq", 8221)):
            rep = firehose.run(transport=transport, seconds=1.0, n_producers=2, port=port)
            self.assertGreater(rep["received"], 0)
            for k in ("admits_per_sec", "p50_ms", "p95_ms"):
                self.assertIsInstance(rep[k], (int, float))
                self.assertGreaterEqual(rep[k], 0.0)


if __name__ == "__main__":
    unittest.main()
