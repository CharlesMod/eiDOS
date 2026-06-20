"""P5b / Pillar 6 gates: the neuromodulatory state — arousal rises with pressure, affect tracks,
modulation is broadcast retained."""
import json
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from nervous import NervousBus, Kind, Modality, Delivery, NeuromodulatoryState  # noqa: E402


class TestNeuromod(unittest.TestCase):
    def test_at_rest_is_calm(self):
        bus = NervousBus()
        self.addCleanup(bus.close)
        nm = NeuromodulatoryState(bus, baseline_arousal=0.3)
        nm.observe_interoception({"bars": {"ram": "ok", "vram": "ok"}})
        self.assertEqual(nm.mood(), "calm")

    def test_arousal_rises_with_pressure(self):
        bus = NervousBus()
        self.addCleanup(bus.close)
        nm = NeuromodulatoryState(bus, baseline_arousal=0.3)
        a0 = nm.arousal
        for _ in range(20):
            nm.observe_interoception({"bars": {"vram": "critical"}})
        self.assertGreater(nm.arousal, a0)          # the body's stress raises arousal
        self.assertLess(nm.valence, 0.0)            # and lowers valence
        self.assertIn(nm.mood(), ("vigilant", "distressed", "uneasy", "tense"))

    def test_bump_raises_arousal(self):
        bus = NervousBus()
        self.addCleanup(bus.close)
        nm = NeuromodulatoryState(bus)
        a0 = nm.arousal
        nm.bump(0.5)
        self.assertGreater(nm.arousal, a0)          # a startle spike

    def test_modulation_published_retained(self):
        bus = NervousBus()
        self.addCleanup(bus.close)
        sub = bus.subscribe(topics={(Kind.modulation, Modality.system)}, deliveries={Delivery.retained})
        nm = NeuromodulatoryState(bus)
        nm.observe_interoception({"bars": {"ram": "high"}})
        nm.publish()
        e = bus.recv(sub, timeout=1.0)
        self.assertIsNotNone(e)
        d = json.loads(bus.payloads.get(e.payload_ref).decode("utf-8"))
        self.assertIn("arousal", d)
        self.assertIn("valence", d)
        self.assertIn("mood", d)


if __name__ == "__main__":
    unittest.main()
