"""Battery profiler gates — learn this pack's true 0→100 over time (voltage+coulomb fusion).

Pins: full detection (charge taper at top), OBSERVE-ONLY cutoff (only on a real load-collapse at low
voltage, never guessed), capacity learned by coulomb traversal, fusion trusting coulomb in the flat
middle, persistence across restarts (survives a wipe), and that ingest never raises into the poll.
"""
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from nervous.battery_profiler import BatteryProfiler  # noqa: E402


class Clock:
    def __init__(self, t=1000.0):
        self.t = t

    def __call__(self):
        return self.t

    def adv(self, s):
        self.t += s


def full_reading():      # at the top, charge tapered to float, sun up → FULL
    return {"battery_voltage": 27.4, "net_current": 0.5, "pv_power": 200, "load_power": 40}


def discharge_reading():  # mid-band, heavy load (OCV ≈ 3.28 V/cell — the flat zone)
    return {"battery_voltage": 25.5, "net_current": -50.0, "pv_power": 0, "load_power": 1300}


def cutoff_reading():    # load collapsed at LOW voltage → inverter LVD tripped
    return {"battery_voltage": 24.0, "net_current": 0.0, "pv_power": 0, "load_power": 0}


class TestProfiler(unittest.TestCase):
    def test_observe_only_before_any_anchor(self):
        clk = Clock()
        p = BatteryProfiler(path=None, clock=clk)
        est = p.ingest({"battery_voltage": 26.4, "net_current": -5.0, "pv_power": 100, "load_power": 200})
        self.assertEqual(est["soc_method"], "voltage")
        self.assertEqual(est["soc_confidence"], "calibrating")
        self.assertIsNone(est["v_empty"])               # cutoff NEVER guessed — observe-only
        self.assertIsNotNone(est["soc"])                # still gives a number (the generic curve)

    def test_full_detected_learns_v_full(self):
        clk = Clock()
        p = BatteryProfiler(path=None, clock=clk)
        p.update(full_reading())
        self.assertEqual(p.state["anchor"], "full")
        self.assertEqual(p.state["full_events"], 1)
        self.assertIsNotNone(p.state["v_full"])
        self.assertEqual(p.state["coulomb_soc"], 1.0)

    def test_cutoff_only_on_load_collapse_at_low_voltage(self):
        clk = Clock()
        p = BatteryProfiler(path=None, clock=clk)
        # a load dropping at NORMAL voltage is just the load turning off — NOT a cutoff
        p.update({"battery_voltage": 26.6, "net_current": -40.0, "pv_power": 0, "load_power": 1000})
        p.update({"battery_voltage": 26.7, "net_current": 0.0, "pv_power": 0, "load_power": 0})
        self.assertIsNone(p.state["v_empty"])
        self.assertEqual(p.state["empty_events"], 0)
        # but a load collapse at LOW voltage IS the inverter cutoff
        p.update({"battery_voltage": 24.3, "net_current": -40.0, "pv_power": 0, "load_power": 1000})
        p.update(cutoff_reading())
        self.assertIsNotNone(p.state["v_empty"])
        self.assertEqual(p.state["empty_events"], 1)

    def test_capacity_learned_across_full_to_empty_traversal(self):
        clk = Clock()
        p = BatteryProfiler(path=None, clock=clk)
        p.update(full_reading())                        # anchor full (coulomb_soc = 1.0)
        for _ in range(4):                              # 4 × 0.5h × 50A = 100 Ah drawn
            clk.adv(1800)
            p.update(discharge_reading())
        clk.adv(1800)
        p.update(cutoff_reading())                      # load collapse at low V → empty
        self.assertIsNotNone(p.state["capacity_ah"])
        self.assertAlmostEqual(p.state["capacity_ah"], 100.0, delta=2.0)  # ≈ the coulombs traversed

    def test_fusion_trusts_coulomb_in_flat_middle(self):
        clk = Clock()
        p = BatteryProfiler(path=None, clock=clk)
        # learn both endpoints + capacity
        p.update(full_reading())
        for _ in range(4):
            clk.adv(1800); p.update(discharge_reading())
        clk.adv(1800); p.update(cutoff_reading())       # now calibrated, anchored empty (coulomb_soc 0)
        # charge back 25 Ah → coulomb says 25%, while the flat-band voltage curve reads ~50% (terminal
        # inflated by the 50A charge so OCV lands at ~3.28 V/cell — squarely in the useless flat zone)
        clk.adv(1800)
        est = p.ingest({"battery_voltage": 27.0, "net_current": 50.0, "pv_power": 300, "load_power": 0})
        self.assertEqual(est["soc_method"], "fusion")
        self.assertEqual(est["soc_confidence"], "good")
        self.assertLess(est["soc"], 35.0)              # pulled toward coulomb (~25), not voltage (~50)
        self.assertGreater(est["soc"], 18.0)

    def test_persistence_survives_restart(self):
        d = tempfile.mkdtemp()
        path = os.path.join(d, "battery_profile.json")
        try:
            clk = Clock()
            p = BatteryProfiler(path=path, clock=clk, save_every_s=0.0)  # save eagerly
            p.update(full_reading())
            p.save()
            self.assertTrue(os.path.exists(path))
            p2 = BatteryProfiler(path=path, clock=Clock())              # fresh instance, same file
            self.assertEqual(p2.state["v_full"], p.state["v_full"])
            self.assertEqual(p2.state["anchor"], "full")
        finally:
            import shutil
            shutil.rmtree(d, ignore_errors=True)

    def test_ingest_never_raises_on_garbage(self):
        p = BatteryProfiler(path=None, clock=Clock())
        for junk in ({}, {"battery_voltage": None}, {"battery_voltage": "x", "net_current": None},
                     {"net_current": -5.0}):
            r = p.ingest(dict(junk))
            self.assertIsInstance(r, dict)              # returns a dict, never throws

    def test_long_gap_does_not_corrupt_coulomb(self):
        # a dashboard restart leaves a huge dt between samples — it must NOT integrate a bogus charge
        clk = Clock()
        p = BatteryProfiler(path=None, clock=clk, max_gap_s=1800.0)
        p.update(full_reading())                        # anchor full
        clk.adv(100000)                                 # ~28h gap (restart)
        p.update(discharge_reading())
        self.assertEqual(p.state["coulomb_ah_since_anchor"], 0.0)   # gap skipped, not integrated


if __name__ == "__main__":
    unittest.main()
