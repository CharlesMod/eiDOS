"""P4 gates: change / novelty detection — only what changed rises."""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from nervous import NervousBus, NervousEvent, Kind, Modality, Delivery, Novelty, ChangeDetector  # noqa: E402
from nervous.event import SCHEMA_VERSION  # noqa: E402


class TestNovelty(unittest.TestCase):
    def test_tracks_change_per_key(self):
        n = Novelty()
        self.assertTrue(n.is_novel("a", b"x"))      # first sight
        self.assertFalse(n.is_novel("a", b"x"))     # unchanged
        self.assertTrue(n.is_novel("a", b"y"))      # changed
        self.assertTrue(n.is_novel("b", b"x"))      # different channel
        n.reset("a")
        self.assertTrue(n.is_novel("a", b"y"))      # forgotten -> novel again


class TestChangeDetector(unittest.TestCase):
    def test_forwards_only_changes(self):
        bus = NervousBus()
        self.addCleanup(bus.close)
        sub = bus.subscribe(topics={(Kind.change, Modality.intero)})
        cd = ChangeDetector(bus)

        def ev():
            return NervousEvent(SCHEMA_VERSION, "intero", Kind.interoceptive, Modality.intero,
                                Delivery.fungible)

        self.assertTrue(cd.step(ev(), b'{"overall":"at ease"}'))    # novel -> rises
        self.assertFalse(cd.step(ev(), b'{"overall":"at ease"}'))   # quiet -> no spike
        self.assertTrue(cd.step(ev(), b'{"overall":"strained"}'))   # changed -> rises
        got = []
        while True:
            e = bus.recv(sub, timeout=0.1)
            if e is None:
                break
            got.append(e)
            bus.ack(e)
        self.assertEqual(len(got), 2)                # only the two changes, not the unchanged repeat
        self.assertTrue(all(e.kind == Kind.change for e in got))


if __name__ == "__main__":
    unittest.main()
