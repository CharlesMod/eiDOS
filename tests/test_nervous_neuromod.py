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
            nm.observe_interoception({"bars": {"gpu_temp": "critical"}})   # a real stressor (heat)
        self.assertGreater(nm.arousal, a0)          # the body's stress raises arousal
        self.assertLess(nm.valence, 0.0)            # and lowers valence
        self.assertIn(nm.mood(), ("vigilant", "distressed", "uneasy", "tense"))

    def test_high_vram_does_not_sweat(self):
        # high VRAM is the resident mind (high usage BY DESIGN) — it must not raise arousal or sour mood.
        bus = NervousBus()
        self.addCleanup(bus.close)
        nm = NeuromodulatoryState(bus, baseline_arousal=0.3)
        for _ in range(20):
            nm.observe_interoception({"bars": {"vram": "critical"}})
        self.assertEqual(nm.valence, 0.0)           # pressure = 0 -> valence unsoured
        self.assertEqual(nm.mood(), "calm")         # an at-ease body, a calm mind

    def test_bump_raises_arousal(self):
        bus = NervousBus()
        self.addCleanup(bus.close)
        nm = NeuromodulatoryState(bus)
        a0 = nm.arousal
        nm.bump(0.5)
        self.assertGreater(nm.arousal, a0)          # a startle spike

    def test_reward_arousal_is_phasic_not_per_tick(self):
        # Routine small-RPE ticks must NOT pump arousal every tick (the newborn-creature pin); only a
        # genuine surprise spikes it, and only by a small bounded amount.
        bus = NervousBus()
        self.addCleanup(bus.close)
        nm = NeuromodulatoryState(bus, baseline_arousal=0.3)
        base = nm.arousal
        for _ in range(20):
            nm.observe_reward(rpe=0.1, reward=0.05)      # 20 routine ticks
        self.assertLessEqual(nm.arousal, base + 1e-9)    # arousal untouched — it can relax to baseline
        nm.observe_reward(rpe=1.0, reward=0.5)           # a real surprise
        self.assertGreater(nm.arousal, base)             # spikes
        self.assertLessEqual(nm.arousal, base + 0.1 + 1e-9)   # but bounded

    def test_exhaustion_collapses_arousal_toward_sleep(self):
        # M0.3: above the exhaustion floor, energy doesn't sap arousal; near-empty, arousal collapses
        # toward sleep (torpor) so the creature RESTS before flatlining — hibernation, not death.
        bus = NervousBus()
        self.addCleanup(bus.close)
        nm = NeuromodulatoryState(bus, baseline_arousal=0.3, exhaustion_energy=0.15)
        nm.observe_energy(0.8)                              # well-fed
        for _ in range(20):
            nm.observe_interoception({"bars": {}})
        self.assertGreater(nm.arousal, 0.2)                # rests near baseline when fed
        nm.observe_energy(0.0)                              # reserve empty
        for _ in range(40):
            nm.observe_interoception({"bars": {}})
        self.assertLess(nm.arousal, 0.15)                  # collapsed into the sleep range (torpor)

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
