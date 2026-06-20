"""P7 gates: the sleep / consolidation cycle — sleeps at low arousal, re-fits baselines."""
import json
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from nervous import (NervousBus, NervousEvent, Kind, Modality, Delivery,  # noqa: E402
                     NeuromodulatoryState, ChangeDetector, SleepCycle)
from nervous.event import SCHEMA_VERSION  # noqa: E402


class TestSleep(unittest.TestCase):
    def test_sleeps_at_low_arousal_and_wakes_high(self):
        bus = NervousBus()
        self.addCleanup(bus.close)
        nm = NeuromodulatoryState(bus, baseline_arousal=0.1)
        nm.observe_interoception({"bars": {"ram": "ok"}})      # at rest -> arousal drops low
        sleep = SleepCycle(bus, neuromod=nm, sleep_arousal=0.15)
        self.assertTrue(sleep.tick())                          # low arousal -> sleeps
        self.assertEqual(sleep.cycles, 1)
        nm.bump(0.8)                                           # startled awake
        self.assertFalse(sleep.tick())                        # high arousal -> no sleep

    def test_consolidate_refits_baselines(self):
        bus = NervousBus()
        self.addCleanup(bus.close)
        cd = ChangeDetector(bus)
        ev = NervousEvent(SCHEMA_VERSION, "s", Kind.interoceptive, Modality.intero, Delivery.fungible)
        self.assertTrue(cd.step(ev, b"x"))                    # novel
        self.assertFalse(cd.step(ev, b"x"))                   # then known
        SleepCycle(bus, change_detectors=[cd]).consolidate()  # sleep re-fits the baseline of 'normal'
        self.assertTrue(cd.step(ev, b"x"))                    # novel again after consolidation

    def test_sleep_marker_published(self):
        bus = NervousBus()
        self.addCleanup(bus.close)
        sub = bus.subscribe(topics={(Kind.capability, Modality.system)}, deliveries={Delivery.retained})
        SleepCycle(bus).consolidate()
        e = bus.recv(sub, timeout=1.0)
        self.assertIsNotNone(e)
        d = json.loads(bus.payloads.get(e.payload_ref).decode("utf-8"))
        self.assertEqual(d["action"], "consolidate")


if __name__ == "__main__":
    unittest.main()
