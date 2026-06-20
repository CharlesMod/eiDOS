"""Behind-the-curtain monitor gates: it snapshots the live nervous-system state by READING the bus
projections (I6, never recomputing), reflects the arbiter's GPU holder, degrades gracefully on an empty
bus, and round-trips through an atomic file write."""
import json
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from nervous import NervousBus, NervousMonitor, GpuArbiter  # noqa: E402
from nervous.interoception import Interoception  # noqa: E402


def reader(**vals):
    base = {"ram_pct": None, "disk_free_gb": None, "cpu_pct": None,
            "vram_used_pct": None, "gpu_temp_c": None}
    base.update(vals)
    return lambda: base


class TestMonitor(unittest.TestCase):
    def test_snapshot_shape_and_reads_the_felt_projection(self):
        bus = NervousBus()
        self.addCleanup(bus.close)
        mon = NervousMonitor(bus, arbiter=GpuArbiter(bus=bus))
        # the body publishes: VRAM baseline (resident mind) + a real thermal stressor
        Interoception(bus, reader=reader(vram_used_pct=99, gpu_temp_c=92)).emit()
        snap = mon.tick()
        for k in ("ts", "felt", "mood", "gpu_holder", "bus", "organs", "feed", "baseline_systems"):
            self.assertIn(k, snap)
        self.assertEqual(snap["felt"]["overall"], "in distress")     # READ from the projection (thermal)
        self.assertIn("vram", snap["baseline_systems"])
        intero = next(o for o in snap["organs"] if o["name"] == "interoception")
        self.assertTrue(intero["active"])
        self.assertIn("distress", intero["detail"])
        self.assertTrue(any(f["kind"] == "interoceptive" for f in snap["feed"]))  # event crossed the bus

    def test_gpu_holder_reflects_the_arbiter(self):
        bus = NervousBus()
        self.addCleanup(bus.close)
        arb = GpuArbiter(bus=bus)
        mon = NervousMonitor(bus, arbiter=arb)
        lease = arb.acquire("mind")
        self.assertEqual(mon.tick()["gpu_holder"], "mind")
        arb.release(lease)
        self.assertIsNone(mon.tick()["gpu_holder"])

    def test_empty_bus_is_graceful(self):
        bus = NervousBus()
        self.addCleanup(bus.close)
        snap = NervousMonitor(bus).tick()
        self.assertEqual(snap["felt"], {})
        self.assertIsInstance(snap["bus"], dict)
        self.assertTrue(all(not o["active"] for o in snap["organs"]))

    def test_write_then_read_roundtrip(self):
        bus = NervousBus()
        self.addCleanup(bus.close)
        path = os.path.join(tempfile.mkdtemp(), "snap.json")
        mon = NervousMonitor(bus, snapshot_path=path)
        Interoception(bus, reader=reader(cpu_pct=90)).emit()    # cpu high -> strained
        mon.tick()
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        self.assertEqual(data["felt"]["overall"], "strained")


if __name__ == "__main__":
    unittest.main()
