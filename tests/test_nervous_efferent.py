"""P5 gates: the efferent loop — reflex fires without the core, efference copy grants agency,
proprioception senses the creature's own state."""
import json
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from nervous import (NervousBus, NervousEvent, Kind, Modality, Delivery,  # noqa: E402
                     ChangeDetector, Effector, SelfModel, ReflexArc, Proprioceptor)
from nervous.event import SCHEMA_VERSION  # noqa: E402


def sensory(source, modality=Modality.device):
    return NervousEvent(SCHEMA_VERSION, source, Kind.sensory, modality, Delivery.fungible)


class TestReflex(unittest.TestCase):
    def test_reflex_fires_without_core_and_escalates_otherwise(self):
        bus = NervousBus()
        self.addCleanup(bus.close)
        fired = []
        eff = Effector(bus, handlers={"withdraw": lambda p: fired.append("withdraw")})
        reflex = ReflexArc(eff, name="pain-withdraw", trigger=lambda ev, p: p == b"hot", action="withdraw")
        self.assertTrue(reflex.consider(sensory("skin"), b"hot"))    # fires, no core involved
        self.assertEqual(fired, ["withdraw"])
        self.assertFalse(reflex.consider(sensory("skin"), b"cold"))  # no match -> escalate, not fired
        self.assertEqual(fired, ["withdraw"])                        # still just the one


class TestAgency(unittest.TestCase):
    def test_efference_copy_cancels_self_caused_change(self):
        bus = NervousBus()
        self.addCleanup(bus.close)
        sm = SelfModel()
        cd = ChangeDetector(bus, self_model=sm)
        eff = Effector(bus, self_model=sm)
        key = ("camera", "vision", "sensory")                       # the channel that will change
        eff.act("turn_head", predicts=(key, b"moved"))             # corollary discharge
        cam = NervousEvent(SCHEMA_VERSION, "camera", Kind.sensory, Modality.vision, Delivery.fungible)
        self.assertFalse(cd.step(cam, b"moved"))                   # I did that -> no surprise (agency)
        self.assertTrue(cd.step(cam, b"intruder"))                 # world-caused -> surprise rises


class TestProprioception(unittest.TestCase):
    def test_proprioceptor_publishes_own_state(self):
        bus = NervousBus()
        self.addCleanup(bus.close)
        sub = bus.subscribe(topics={(Kind.proprioceptive, Modality.proprio)})
        Proprioceptor(bus, state_fn=lambda: {"speaking": True, "jobs": 2}).emit()
        e = bus.recv(sub, timeout=1.0)
        self.assertIsNotNone(e)
        self.assertEqual(e.kind, Kind.proprioceptive)
        payload = json.loads(bus.payloads.get(e.payload_ref).decode("utf-8"))
        self.assertTrue(payload["speaking"])
        self.assertEqual(payload["jobs"], 2)


if __name__ == "__main__":
    unittest.main()
