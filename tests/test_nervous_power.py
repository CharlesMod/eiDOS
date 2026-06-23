"""M4 gates — the Renogy power reader and (critically) its SELF-HEALING.

Dean uses the Renogy phone app himself; a BLE peripheral allows one central at a time, so whenever his
phone is connected the MPPT is unreachable to eiDOS. That must be a normal, recoverable condition — never
a crash, never a wedge, never a corrupted reserve. These tests pin: fail-open, keep-last-good + go-stale,
exponential backoff, automatic recovery + re-anchor, and that nothing raises into the tick path.
"""
import json
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from nervous import PowerMonitor, lifepo4_soc, parse_mppt, NervousBus, Kind, Modality  # noqa: E402
from nervous.power import _build_read, _crc16  # noqa: E402


def _frame(regs):
    """Build a valid Modbus 0x0100 response frame from a 34-register list."""
    data = b"".join(int(v).to_bytes(2, "big") for v in regs)
    f = bytes([255, 3, len(data)]) + data
    return f + _crc16(f)


def _good_regs(soc_ctrl=100, batt_dV=274, charge_cA=2106, load_cA=554, pv_dV=619, pv_cA=970, pv_w=601):
    regs = [0] * 34
    regs[0] = soc_ctrl; regs[1] = batt_dV; regs[2] = charge_cA
    regs[5] = load_cA; regs[7] = pv_dV; regs[8] = pv_cA; regs[9] = pv_w
    return regs


class FakeMetabolism:
    def __init__(self):
        self.energy = 0.5
        self.anchored = []
        self.raises = False

    def anchor_soc(self, soc):
        if self.raises:
            raise RuntimeError("metabolism boom")
        self.anchored.append(soc)
        self.energy = soc / 100.0


class _Clock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t


class TestSocAndParse(unittest.TestCase):
    def test_crc_matches_reference(self):
        self.assertEqual(list(_build_read(255, 0x0100, 34)), [255, 3, 1, 0, 0, 34, 209, 241])

    def test_parse_extracts_pv_and_derives_soc(self):
        out = parse_mppt(_frame(_good_regs()))
        self.assertEqual(out["pv_power"], 601)
        self.assertEqual(out["battery_voltage"], 27.4)
        self.assertEqual(out["net_current"], round(21.06 - 5.54, 2))
        self.assertTrue(90 <= out["soc"] <= 100)            # near-full pack

    def test_lifepo4_curve_monotonic_and_bounded(self):
        self.assertEqual(lifepo4_soc(30.0), 100.0)          # above the top knee
        self.assertEqual(lifepo4_soc(20.0), 0.0)            # below the bottom knee
        hi = lifepo4_soc(27.2); lo = lifepo4_soc(25.8)
        self.assertGreater(hi, lo)                          # more volts -> more charge
        self.assertTrue(0.0 <= lo <= hi <= 100.0)

    def test_charge_current_correction_lowers_soc(self):
        # the same terminal voltage reads as LESS charged once you subtract the charging I·R offset
        rested = lifepo4_soc(26.4, net_current_a=0.0)
        charging = lifepo4_soc(26.4, net_current_a=20.0)
        self.assertLessEqual(charging, rested)

    def test_v_empty_reanchors_curve_bottom(self):
        # generic curve is pessimistic at the bottom — it reads 0% by 24.0V even though the pack still
        # drives the load lower. Anchoring the observed floor stretches that band to real resolution.
        self.assertEqual(lifepo4_soc(24.0), 0.0)                          # generic: flat 0% too early
        self.assertEqual(lifepo4_soc(22.92, v_empty=22.92), 1.0)         # observed floor -> ~1% mark
        re24 = lifepo4_soc(24.0, v_empty=22.92)
        self.assertGreater(re24, 0.0)                                    # 24.0V now has resolution...
        self.assertLess(re24, 8.0)                                       # ...a sane low single digit
        self.assertEqual(lifepo4_soc(20.0, v_empty=22.92), 0.0)         # BMS hard floor still 0%
        self.assertEqual(lifepo4_soc(25.5), lifepo4_soc(25.5, v_empty=22.92))  # mid/upper curve untouched
        # a "floor" above the 5% knee (never drawn down) is ignored — can't masquerade as empty
        self.assertEqual(lifepo4_soc(25.5, v_empty=26.0), lifepo4_soc(25.5))

    def test_parse_rejects_garbage(self):
        with self.assertRaises(ValueError):
            parse_mppt(b"\x01\x83\x02\xc0\xf1")             # an exception frame is not a reading


class TestSelfHealing(unittest.TestCase):
    def test_success_anchors_and_publishes(self):
        bus = NervousBus(); self.addCleanup(bus.close)
        met = FakeMetabolism()
        pm = PowerMonitor(bus, reader=lambda: parse_mppt(_frame(_good_regs())), metabolism=met)
        reading = pm.poll_once()
        self.assertIsNotNone(reading)
        self.assertTrue(pm.is_fresh())
        self.assertEqual(len(met.anchored), 1)              # reserve anchored to real SOC
        ev = bus.retained_snapshot(Kind.power, Modality.device)
        self.assertIsNotNone(ev)                            # retained power event published
        body = json.loads(bus.payloads.get(ev.payload_ref).decode("utf-8"))
        self.assertEqual(body["pv_power"], 601)

    def test_reader_exception_is_fail_open(self):
        met = FakeMetabolism()

        def boom():
            raise OSError("device busy (Dean's phone has the link)")

        pm = PowerMonitor(None, reader=boom, metabolism=met)
        self.assertIsNone(pm.poll_once())                   # returns None, does NOT raise
        self.assertFalse(pm.is_fresh())
        self.assertEqual(met.anchored, [])                  # reserve untouched
        self.assertEqual(pm.snapshot()["consecutive_fails"], 1)

    def test_keeps_last_good_then_goes_stale(self):
        clk = _Clock()
        seq = [parse_mppt(_frame(_good_regs()))]

        def reader():
            if seq:
                return seq.pop()
            raise OSError("busy")

        pm = PowerMonitor(None, reader=reader, metabolism=FakeMetabolism(),
                          stale_after_s=300.0, clock=clk)
        pm.poll_once()                                      # one good read
        self.assertTrue(pm.is_fresh())
        last = pm.latest()
        clk.t += 100                                        # 100s later, reads now fail
        pm.poll_once()
        self.assertEqual(pm.latest(), last)                 # still serves the last good reading
        self.assertTrue(pm.is_fresh())                      # 100s < 300s
        clk.t += 250                                        # now >300s stale
        self.assertFalse(pm.is_fresh())                     # feed marked stale -> sim takes over

    def test_backoff_grows_then_resets_on_recovery(self):
        state = {"fail": True}

        def reader():
            if state["fail"]:
                raise OSError("busy")
            return parse_mppt(_frame(_good_regs()))

        pm = PowerMonitor(None, reader=reader, metabolism=FakeMetabolism(),
                          interval_s=60.0, backoff_max_s=600.0)
        base = pm._next_delay()
        pm.poll_once(); d1 = pm._next_delay()
        pm.poll_once(); d2 = pm._next_delay()
        self.assertEqual(base, 60.0)                        # healthy cadence
        self.assertGreater(d1, base)                        # back off after a failure
        self.assertGreater(d2, d1)                          # and keep backing off
        self.assertLessEqual(d2, 600.0)                     # capped
        state["fail"] = False
        self.assertIsNotNone(pm.poll_once())                # device free again
        self.assertEqual(pm._next_delay(), 60.0)            # snaps straight back to normal cadence

    def test_recovers_and_reanchors_after_outage(self):
        state = {"fail": False}

        def reader():
            if state["fail"]:
                raise OSError("busy")
            return parse_mppt(_frame(_good_regs(batt_dV=262)))

        met = FakeMetabolism()
        pm = PowerMonitor(None, reader=reader, metabolism=met)
        pm.poll_once(); n_after_first = len(met.anchored)
        state["fail"] = True
        for _ in range(3):
            pm.poll_once()
        self.assertEqual(len(met.anchored), n_after_first)  # no anchors during the outage
        state["fail"] = False
        pm.poll_once()
        self.assertEqual(len(met.anchored), n_after_first + 1)  # re-anchored on recovery
        self.assertTrue(pm.is_fresh())

    def test_unparseable_reading_counts_as_failure(self):
        pm = PowerMonitor(None, reader=lambda: {"soc": None}, metabolism=FakeMetabolism())
        self.assertIsNone(pm.poll_once())
        self.assertFalse(pm.is_fresh())

    def test_metabolism_fault_does_not_break_monitor(self):
        met = FakeMetabolism(); met.raises = True
        pm = PowerMonitor(None, reader=lambda: parse_mppt(_frame(_good_regs())), metabolism=met)
        reading = pm.poll_once()                            # anchor_soc raises internally...
        self.assertIsNotNone(reading)                       # ...but the read still succeeds + is recorded
        self.assertTrue(pm.is_fresh())

    def test_no_reader_is_inert(self):
        pm = PowerMonitor(None, reader=None, metabolism=FakeMetabolism())
        self.assertIsNone(pm.poll_once())
        self.assertFalse(pm.is_fresh())


class TestSharedCache(unittest.TestCase):
    """The always-on dashboard owns the single BLE poll and writes a shared cache; eidos and the panel
    READ it — so battery/solar stays live even when eidos is paused/stopped, with one radio owner."""

    def setUp(self):
        import tempfile
        self.dir = tempfile.mkdtemp()
        self.cache = str(Path(self.dir) / "power_latest.json")

    def tearDown(self):
        import shutil
        shutil.rmtree(self.dir, ignore_errors=True)

    def test_write_then_read_roundtrip_fresh(self):
        from nervous.power import write_power_cache, read_power_cache
        reading = parse_mppt(_frame(_good_regs()))
        write_power_cache(self.cache, reading)
        c = read_power_cache(self.cache, max_age_s=600.0)
        self.assertIsNotNone(c)
        self.assertTrue(c["fresh"])
        self.assertEqual(c["reading"]["pv_power"], 601)
        self.assertLess(c["age_s"], 5.0)

    def test_absent_cache_is_none(self):
        from nervous.power import read_power_cache
        self.assertIsNone(read_power_cache(self.cache))

    def test_old_reading_reads_stale(self):
        # a reading written long ago is served but marked NOT fresh (panel shows "stale", sim takes over)
        from nervous.power import read_power_cache
        old = {"reading": parse_mppt(_frame(_good_regs())), "ts": __import__("time").time() - 1000.0}
        Path(self.cache).write_text(json.dumps(old), encoding="utf-8")
        c = read_power_cache(self.cache, max_age_s=600.0)
        self.assertIsNotNone(c)
        self.assertFalse(c["fresh"])
        self.assertGreater(c["age_s"], 600.0)

    def test_dashboard_poll_writes_cache_eidos_consumes_it(self):
        # End-to-end: the dashboard's BLE poller writes the cache; an eidos-side monitor reads it via
        # cache_reader and anchors its metabolism — the radio is polled exactly ONCE (by the dashboard).
        from nervous.power import cache_reader
        ble_reads = {"n": 0}

        def fake_ble():
            ble_reads["n"] += 1
            return parse_mppt(_frame(_good_regs(batt_dV=268)))

        dash = PowerMonitor(None, reader=fake_ble, metabolism=None, cache_path=self.cache)
        self.assertIsNotNone(dash.poll_once())                 # dashboard does the ONE real BLE read
        self.assertEqual(ble_reads["n"], 1)

        bus = NervousBus(); self.addCleanup(bus.close)
        met = FakeMetabolism()
        eidos = PowerMonitor(bus, reader=cache_reader(None, self.cache, max_age_s=600.0), metabolism=met)
        reading = eidos.poll_once()                            # eidos consumes the cache (no BLE)
        self.assertIsNotNone(reading)
        self.assertEqual(ble_reads["n"], 1)                    # still ONE radio read — no contention
        self.assertEqual(len(met.anchored), 1)                 # metabolism anchored to the shared reading
        self.assertIsNotNone(bus.retained_snapshot(Kind.power, Modality.device))

    def test_cache_reader_fails_open_when_stale(self):
        # a stale/absent cache makes the eidos-side reader raise → the monitor's fail-open path engages
        from nervous.power import cache_reader
        reader = cache_reader(None, self.cache, max_age_s=600.0)
        with self.assertRaises(IOError):
            reader()                                           # absent
        old = {"reading": parse_mppt(_frame(_good_regs())), "ts": __import__("time").time() - 5000.0}
        Path(self.cache).write_text(json.dumps(old), encoding="utf-8")
        with self.assertRaises(IOError):
            reader()                                           # present but stale
        pm = PowerMonitor(None, reader=reader, metabolism=FakeMetabolism())
        self.assertIsNone(pm.poll_once())                      # fail-open: None, never raises
        self.assertFalse(pm.is_fresh())


if __name__ == "__main__":
    unittest.main()
