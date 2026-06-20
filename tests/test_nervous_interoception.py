"""P1a gates: interoception — the first organ. raw->felt monotonic, fault-injection crosses the
felt threshold, and the felt body surfaces in context (intero -> bus -> afferent intake)."""
import json
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from nervous import NervousBus, Kind, Modality, AfferentContext  # noqa: E402
from nervous.interoception import (Interoception, felt_bars, worst_salience,  # noqa: E402
                                   _bin, _bin_free, LEVELS)


def reader(**vals):
    base = {"ram_pct": None, "disk_free_gb": None, "cpu_pct": None,
            "vram_used_pct": None, "gpu_temp_c": None}
    base.update(vals)
    return lambda: base


class TestBins(unittest.TestCase):
    def test_pressure_bin_is_monotonic(self):
        vals = [0, 10, 50, 69, 70, 84, 85, 94, 95, 99, 100]
        idx = [LEVELS.index(_bin(v, 70, 85, 95)) for v in vals]
        self.assertEqual(idx, sorted(idx))                       # non-decreasing in raw value

    def test_free_bin_is_monotonic(self):
        free = [0, 0.5, 1, 4, 5, 19, 20, 50, 100]
        idx = [LEVELS.index(_bin_free(v, 20, 5, 1)) for v in free]
        self.assertEqual(idx, sorted(idx, reverse=True))         # more free -> non-increasing pressure


class TestFaultInjection(unittest.TestCase):
    def test_high_pressure_crosses_to_critical(self):
        bars = felt_bars({"ram_pct": 99, "disk_free_gb": 0.5, "cpu_pct": 99,
                          "vram_used_pct": 99, "gpu_temp_c": 95})
        self.assertEqual(bars["vram"], "critical")
        self.assertEqual(bars["ram"], "critical")
        self.assertEqual(bars["disk"], "critical")
        self.assertEqual(worst_salience(bars), 1.0)

    def test_at_rest_is_quiet(self):
        bars = felt_bars({"ram_pct": 10, "disk_free_gb": 500, "cpu_pct": 5,
                          "vram_used_pct": 10, "gpu_temp_c": 40})
        self.assertTrue(all(v == "ok" for v in bars.values()))
        self.assertEqual(worst_salience(bars), 0.1)              # body barely salient at rest

    def test_missing_signal_is_skipped(self):
        bars = felt_bars({"ram_pct": 96, "disk_free_gb": None, "cpu_pct": None,
                          "vram_used_pct": None, "gpu_temp_c": None})
        present = {k: v for k, v in bars.items() if v is not None}
        self.assertEqual(present, {"ram": "critical"})


class TestOrgan(unittest.TestCase):
    def test_emit_publishes_interoceptive_event(self):
        bus = NervousBus()
        self.addCleanup(bus.close)
        sub = bus.subscribe()
        org = Interoception(bus, reader=reader(ram_pct=99, vram_used_pct=99))
        org.emit()
        e = bus.recv(sub, timeout=1.0)
        self.assertIsNotNone(e)
        self.assertEqual(e.kind, Kind.interoceptive)
        self.assertEqual(e.modality, Modality.intero)
        self.assertEqual(e.source_organ, "interoception")       # single writer of the felt-state (I6)
        self.assertAlmostEqual(e.salience, 1.0)
        payload = json.loads(bus.payloads.get(e.payload_ref).decode("utf-8"))
        self.assertEqual(payload["vram"], "critical")

    def test_felt_body_surfaces_in_context(self):
        bus = NervousBus()
        self.addCleanup(bus.close)
        aff = AfferentContext(bus, max_events=5, max_chars=2000)
        self.addCleanup(aff.close)
        Interoception(bus, reader=reader(ram_pct=99, vram_used_pct=99)).emit()
        block, n = aff.drain_block()
        self.assertEqual(n, 1)
        self.assertIn("intero/interoceptive", block)
        self.assertIn("vram", block)
        self.assertIn("critical", block)


if __name__ == "__main__":
    unittest.main()
