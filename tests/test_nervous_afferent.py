"""P3 gates: the bus -> context bridge (AfferentContext) + the KV-safe context injection."""
import os
import shutil
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from nervous import NervousBus, NervousEvent, Kind, Modality, Delivery, AfferentContext  # noqa: E402
from nervous.event import SCHEMA_VERSION  # noqa: E402
from config import Config  # noqa: E402
import context as ctxmod  # noqa: E402


class TestAfferentBridge(unittest.TestCase):
    def setUp(self):
        self.bus = NervousBus()

    def tearDown(self):
        self.bus.close()

    def test_drain_block_renders_admitted_events(self):
        aff = AfferentContext(self.bus, max_events=10, max_chars=2000)
        self.assertEqual(aff.drain_block(), ("", 0))   # idle: empty, byte-identical to today
        self.bus.publish(NervousEvent(SCHEMA_VERSION, "intero", Kind.interoceptive, Modality.intero,
                                      Delivery.fungible, salience=0.4), b'{"vram":"strained"}')
        self.bus.publish(NervousEvent(SCHEMA_VERSION, "cam", Kind.percept, Modality.vision,
                                      Delivery.fungible, salience=0.7))
        block, n = aff.drain_block()
        self.assertEqual(n, 2)
        self.assertIn("intero", block)
        self.assertIn("vram", block)            # small json payload rendered inline
        self.assertIn("vision/percept", block)
        self.assertEqual(aff.drain_block(), ("", 0))   # drained
        aff.close()

    def test_drain_block_respects_max_events(self):
        aff = AfferentContext(self.bus, max_events=3, max_chars=2000)
        for _ in range(10):
            self.bus.publish(NervousEvent(SCHEMA_VERSION, "s", Kind.action_request, Modality.device,
                                          Delivery.reliable, salience=0.1))
        _block, n = aff.drain_block()
        self.assertEqual(n, 3)
        aff.close()


class TestContextInjection(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="aff-ctx-")
        self.cfg = Config(workspace_dir=self.tmp)
        (self.cfg.workspace / "state").mkdir(parents=True, exist_ok=True)
        (self.cfg.workspace / "goal.md").write_text("test goal", encoding="utf-8")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _assemble(self, afferent_block):
        return ctxmod.assemble_context(self.cfg, tick_number=1, goal_start_time=0.0,
                                       afferent_block=afferent_block)

    def test_afferent_lands_in_volatile_situation_only(self):
        marker = "- [intero/interoceptive] from body full, warm"
        with_aff = self._assemble(marker)
        without = self._assemble("")
        hits = [i for i, m in enumerate(with_aff) if "## Afferent (senses)" in m["content"]]
        self.assertEqual(len(hits), 1)                       # appears exactly once
        idx = hits[0]
        self.assertEqual(with_aff[idx]["role"], "user")
        self.assertGreater(idx, 1)                           # after system(0) + durable(1): volatile tail
        self.assertIn(marker, with_aff[idx]["content"])
        self.assertFalse(any("Afferent" in m["content"] for m in without))  # empty -> no section

    def test_afferent_is_kv_safe_stable_prefix_unchanged(self):
        a = self._assemble("- [vision/percept] from cam something")
        b = self._assemble("")
        # The stable prefix (system + durable blob) is byte-identical whether or not senses fired —
        # the afferent block only ever touches the volatile situation message (KV-safe, P3 mandate).
        self.assertEqual(a[0], b[0])   # system message
        self.assertEqual(a[1], b[1])   # durable blob


if __name__ == "__main__":
    unittest.main()
