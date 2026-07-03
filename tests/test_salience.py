"""Pillars 1.3 gate — the salience gate (PILLARS_TODO.md 1.3; plan §4 N-2).

Red-able per V3 doctrine: each test fails on a correctness violation, not a smoke check.
  - relevant events admit ahead of equally-loud noise;
  - a relevance_set change measurably reorders ALREADY-PENDING admission;
  - reliable/ordered delivery guarantees hold under the gate (nothing dropped, order kept);
  - the exploration floor admits low-salience events over many rounds (anti-Matthew);
  - flag off → delivery byte-identical to no gate at all (regression-critical).
"""
import json
import os
import random
import sys
import types
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from nervous import (NervousBus, NervousEvent, Kind, Modality, Delivery,  # noqa: E402
                     OrganRegistry, SalienceGate, publish_relevance_set)
from nervous.event import SCHEMA_VERSION  # noqa: E402


def cfg(enabled):
    return types.SimpleNamespace(pillars_salience_gate_enabled=enabled)


def ev(source="s", kind=Kind.sensory, modality=Modality.device, delivery=Delivery.fungible,
       salience=0.0, **kw):
    return NervousEvent(SCHEMA_VERSION, source, kind, modality, delivery,
                        salience=salience, t=0.0, **kw)


def payload_text(bus, event):
    p = bus.payloads.get(event.payload_ref) if event.payload_ref else None
    return p.decode("utf-8") if p else ""


class SalienceTest(unittest.TestCase):
    def setUp(self):
        self._buses = []

    def tearDown(self):
        for b in self._buses:
            try:
                b.close()
            except Exception:
                pass

    def make_bus(self, **kw):
        b = NervousBus(**kw)
        self._buses.append(b)
        return b

    def make_gate(self, bus, enabled=True, **kw):
        return SalienceGate(bus, config=cfg(enabled), **kw)

    # ---- the bias field ------------------------------------------------------------------------

    def test_relevant_admits_ahead_of_equally_loud_noise(self):
        """Events matching the relevance_set surface before EQUALLY-LOUD noise (same salience)."""
        bus = self.make_bus()
        gate = self.make_gate(bus)
        for i, text in enumerate(["weather chatter background", "garden hose leaking water",
                                  "random hallway noise", "garden bed sensor dry",
                                  "tv murmur downstairs", "garden gate opened"]):
            bus.publish(ev(source="sense%d" % i, salience=0.2), text.encode("utf-8"))
        publish_relevance_set(bus, ["garden"])
        gate.ingest()
        admitted = gate.admit(6)  # budget >= pending: pure bias order, no exploration slot
        self.assertEqual(len(admitted), 6)
        first_three = [payload_text(bus, e) for e in admitted[:3]]
        self.assertTrue(all("garden" in t for t in first_three),
                        "relevance-matching events must occupy the top admission slots: %r"
                        % first_three)

    def test_relevance_change_reorders_pending_admission(self):
        """Changing the relevance_set re-ranks events ALREADY pending in the pool."""
        bus = self.make_bus()
        gate = self.make_gate(bus)
        bus.publish(ev(source="a", salience=0.1), b"furnace temperature rising")
        bus.publish(ev(source="b", salience=0.1), b"driveway motion detected")
        bus.publish(ev(source="c", salience=0.1), b"mail delivered at door")
        publish_relevance_set(bus, ["driveway"])
        gate.ingest()
        self.assertIn("driveway", payload_text(bus, gate.admit(1)[0]))
        publish_relevance_set(bus, ["furnace"])   # pivot: the SAME pending pool must re-rank
        gate.ingest()
        self.assertIn("furnace", payload_text(bus, gate.admit(1)[0]))
        self.assertIn("mail", payload_text(bus, gate.admit(1)[0]))

    def test_fail_open_neutral_without_relevance_or_modulation(self):
        """No relevance_set published and no modulation → the gate biases nothing it doesn't
        know: admission order is the bus's own salience order."""
        bus = self.make_bus()
        gate = self.make_gate(bus)
        bus.publish(ev(source="s1", salience=0.1), b"one")
        bus.publish(ev(source="s2", salience=0.9), b"two")
        bus.publish(ev(source="s3", salience=0.5), b"three")
        gate.ingest()
        out = [e.salience for e in gate.admit(3)]
        self.assertEqual(out, [0.9, 0.5, 0.1])

    def test_neuromod_gain_read_and_fail_open(self):
        bus = self.make_bus()
        gate = self.make_gate(bus)
        self.assertEqual(gate._neuromod_gain(), 1.0)   # absent → exactly neutral
        state = json.dumps({"arousal": 0.8, "valence": 0.0, "mood": "vigilant"}).encode("utf-8")
        bus.publish(ev(source="neuromod", kind=Kind.modulation, modality=Modality.system,
                       delivery=Delivery.retained, salience=0.8), state)
        self.assertAlmostEqual(gate._neuromod_gain(), 0.5 + 0.8 * 1.0, places=6)

    # ---- delivery guarantees under the gate ------------------------------------------------------

    def test_reliable_never_dropped_and_surfaces_first(self):
        """A reliable event is never evicted by the pool cap and never deferred behind fungibles,
        even when every fungible matches the relevance_set and floods the pool."""
        bus = self.make_bus()
        gate = self.make_gate(bus, pool_cap=8, rng=random.Random(3))
        publish_relevance_set(bus, ["flood"])
        bus.publish(ev(source="core", kind=Kind.action_request, modality=Modality.system,
                       delivery=Delivery.reliable), b"speak: hello")
        for i in range(50):
            bus.publish(ev(source="noisy%d" % i, salience=0.9), b"flood flood flood")
        gate.ingest()
        self.assertGreater(gate.evicted, 0)   # the fungible pool really overflowed
        admitted = gate.admit(1)
        self.assertEqual(admitted[0].kind, Kind.action_request)
        # drain everything: the reliable appeared exactly once, never dropped
        rest = []
        while True:
            batch = gate.admit(10)
            if not batch:
                break
            rest.extend(batch)
        self.assertEqual(sum(1 for e in rest if e.kind == Kind.action_request), 0)

    def test_ordered_sequence_stays_in_order(self):
        """Ordered delivery: in-sequence, never a hole, regardless of the bias field."""
        bus = self.make_bus()
        gate = self.make_gate(bus)
        publish_relevance_set(bus, ["shiny"])
        bus.publish(ev(source="noise", salience=0.9), b"shiny shiny distraction")
        for i in range(3):
            bus.publish(ev(source="stream", kind=Kind.sensory, delivery=Delivery.ordered,
                           sequence_id="seq1", ordinal=i), b"frame %d" % i)
        gate.ingest()
        admitted = gate.admit(10)
        ordinals = [e.ordinal for e in admitted if e.sequence_id == "seq1"]
        self.assertEqual(ordinals, [0, 1, 2])
        # and the ordered stream was not deferred behind the relevance-matching fungible
        kinds = [e.delivery for e in admitted]
        self.assertLess(kinds.index(Delivery.ordered), kinds.index(Delivery.fungible))

    # ---- exploration floor -------------------------------------------------------------------------

    def test_exploration_floor_admits_low_salience_events(self):
        """Anti-Matthew: with the top slots permanently saturated by relevance-matching events,
        low-salience off-relevance noise still trickles through over many rounds."""
        bus = self.make_bus()
        gate = self.make_gate(bus, rng=random.Random(7))
        publish_relevance_set(bus, ["garden"])
        for i in range(12):
            bus.publish(ev(source="static%d" % i, salience=0.0), b"untuned static hiss")
        admitted = []
        for r in range(10):
            for j in range(3):
                bus.publish(ev(source="rel%d_%d" % (r, j), salience=0.5), b"garden bed reading")
            gate.ingest()
            admitted.extend(gate.admit(3))
        noise = [e for e in admitted if payload_text(bus, e) == "untuned static hiss"]
        self.assertGreater(len(noise), 0,
                           "the exploration floor must admit some low-salience events")
        # ...but the floor is a trickle, not the field: relevance still dominates admission
        self.assertGreater(len(admitted) - len(noise), len(noise))

    # ---- flag off: byte-identical (regression-critical) ---------------------------------------------

    def test_flag_off_delivery_byte_identical(self):
        """Flag off, the gate is a verbatim pass-through: same events, same order, same wire
        bytes as a plain bus subscription."""
        bus = self.make_bus()
        plain = bus.subscribe()
        gate = self.make_gate(bus, enabled=False)
        # a mixed stream: fungibles at varying salience, reliables, retained, an ordered sequence,
        # and a relevance_set (which the OFF gate must pass through untouched, not consume)
        for i in range(10):
            bus.publish(ev(source="f%d" % i, salience=(i % 5) / 5.0), b"fungible %d" % i)
        bus.publish(ev(source="core", kind=Kind.action_request, modality=Modality.system,
                       delivery=Delivery.reliable), b"act")
        bus.publish(ev(source="neuromod", kind=Kind.modulation, modality=Modality.system,
                       delivery=Delivery.retained, salience=0.4), b'{"arousal": 0.4}')
        for i in range(3):
            bus.publish(ev(source="stream", delivery=Delivery.ordered,
                           sequence_id="s", ordinal=i), b"o%d" % i)
        publish_relevance_set(bus, ["anything"])

        expect = []
        while True:
            e = bus.recv(plain, timeout=0.0)
            if e is None:
                break
            bus.ack(e)
            expect.append(e.to_wire())
        got = [e.to_wire() for e in gate.admit(10_000)]
        self.assertEqual(got, expect)
        self.assertEqual(gate.admit(10), [])   # drained: nothing invented, nothing held back

    def test_flag_off_ingest_and_pre_tick_are_inert(self):
        bus = self.make_bus()
        gate = self.make_gate(bus, enabled=False)
        registry = OrganRegistry()
        gate.register(registry)
        bus.publish(ev(source="x", salience=0.7), b"payload")
        registry.run_pre_tick(None)
        gate.ingest()
        s = gate.stats()
        self.assertEqual((s["pending_fungible"], s["pending_guaranteed"]), (0, 0))
        self.assertEqual(len(gate.admit(5)), 1)   # still delivered, via pass-through

    # ---- organ registry ------------------------------------------------------------------------------

    def test_registry_pre_tick_ingests_when_enabled(self):
        bus = self.make_bus()
        gate = self.make_gate(bus)
        registry = OrganRegistry()
        rec = gate.register(registry)
        self.assertEqual(rec.name, "salience_gate")
        self.assertIn("relevance_set/system", rec.reads)
        bus.publish(ev(source="x", salience=0.3), b"payload")
        registry.run_pre_tick(None)
        self.assertEqual(gate.stats()["pending_fungible"], 1)


if __name__ == "__main__":
    unittest.main()
