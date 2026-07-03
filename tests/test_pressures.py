"""Pillars 0.3: the causal ledger (pressures.py).

The gate (red-able): for any action recorded in the last N days, the pressure field that
produced it must be retrievable by tick number — from the live ledger AND after a monthly
archive rotation has moved it out of the live file. These offline tests write synthetic tick
records, force a rotation past the byte threshold, and read a field back by tick.
"""

import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

import pressures
from config import Config


class _Neuromod:
    """Minimal stand-in for NeuromodulatoryState — just the attributes collect_field reads."""
    def __init__(self, arousal=0.5, valence=-0.2, drive_floor=0.3, floors=None):
        self.arousal = arousal
        self.valence = valence
        self.drive_floor = drive_floor
        self._drive_floors = floors if floors is not None else {}


class _Drive:
    def __init__(self, level):
        self.level = level


class _Metabolism:
    def __init__(self, energy):
        self.energy = energy


def _field(tick, **over):
    """A synthetic pressure-field record for `tick` (via the real collect_field mapping)."""
    kw = dict(
        neuromod=_Neuromod(floors={"curiosity": 0.3, "goal_tension": 0.1}),
        goaltension=_Drive(0.42), curiosity=_Drive(0.18),
        metabolism=_Metabolism(0.77),
        active_objective={"title": "learn the house", "frustration": 3},
        condition="FOCUSED", strain=4,
        admitted_events=2, xp_delta=1, xp_source="bash",
    )
    kw.update(over)
    return pressures.collect_field(tick=tick, **kw)


class TestCollectField(unittest.TestCase):

    def test_maps_all_signals(self):
        f = _field(7)
        self.assertEqual(f["tick"], 7)
        self.assertAlmostEqual(f["arousal"], 0.5)
        self.assertAlmostEqual(f["valence"], -0.2)
        self.assertAlmostEqual(f["drive_floor"], 0.3)
        self.assertEqual(f["floors"], {"curiosity": 0.3, "goal_tension": 0.1})
        self.assertEqual(f["strain"], 4)
        self.assertEqual(f["condition"], "FOCUSED")
        self.assertAlmostEqual(f["goal_tension"], 0.42)
        self.assertAlmostEqual(f["restlessness"], 0.18)
        self.assertAlmostEqual(f["energy_reserve"], 0.77)
        self.assertEqual(f["active_objective"], "learn the house")
        self.assertEqual(f["objective_frustration"], 3)
        self.assertEqual(f["admitted_events"], 2)
        self.assertEqual(f["xp_delta"], 1)
        self.assertEqual(f["xp_source"], "bash")

    def test_all_organs_none_is_safe(self):
        # Every organ disabled (a P3 boot with no nervous system) must still produce a record.
        f = pressures.collect_field(tick=1)
        self.assertEqual(f["tick"], 1)
        self.assertEqual(f["arousal"], 0.0)
        self.assertEqual(f["floors"], {})
        self.assertEqual(f["active_objective"], "")


class TestLedger(unittest.TestCase):

    def setUp(self):
        self.config = Config()
        self.config.workspace_dir = tempfile.mkdtemp()

    def test_append_and_read_by_tick(self):
        ledger = pressures.PressureLedger(self.config, max_bytes=10_000_000)
        for t in range(1, 6):
            ledger.append(_field(t, xp_delta=t))
        got = pressures.read_field_by_tick(self.config, 3)
        self.assertIsNotNone(got)
        self.assertEqual(got["tick"], 3)
        self.assertEqual(got["xp_delta"], 3)

    def test_missing_tick_is_none(self):
        ledger = pressures.PressureLedger(self.config, max_bytes=10_000_000)
        ledger.append(_field(1))
        self.assertIsNone(pressures.read_field_by_tick(self.config, 999))

    def test_rotation_then_read_back_from_archive(self):
        # A tiny threshold forces a rotation almost immediately; the field for an early tick
        # must still be retrievable AFTER it has been rolled into the monthly archive.
        ledger = pressures.PressureLedger(self.config, max_bytes=400)
        for t in range(1, 40):
            ledger.append(_field(t))

        # A rotation must have happened: at least one dated archive exists.
        archives = list(self.config.state_dir.glob("pressures_archive_*.jsonl"))
        self.assertTrue(archives, "expected a monthly archive after crossing max_bytes")

        # The live file must be bounded (rotation emptied it at least once).
        live = self.config.state_dir / pressures.LEDGER_NAME
        self.assertLess(live.stat().st_size, 400 + 4096)

        # Tick 1 was written first and has certainly been rotated out of the live file —
        # it is retrievable only if the archive fallback works. This is the gate.
        early = pressures.read_field_by_tick(self.config, 1)
        self.assertIsNotNone(early, "early tick must survive rotation into the archive")
        self.assertEqual(early["tick"], 1)

        # A late tick (still in the live file) is retrievable too.
        late = pressures.read_field_by_tick(self.config, 39)
        self.assertIsNotNone(late)
        self.assertEqual(late["tick"], 39)

    def test_read_recent_newest_first(self):
        ledger = pressures.PressureLedger(self.config, max_bytes=10_000_000)
        for t in range(1, 11):
            ledger.append(_field(t))
        recent = pressures.read_recent_fields(self.config, n=3)
        self.assertEqual([r["tick"] for r in recent], [10, 9, 8])

    def test_append_never_raises(self):
        # Best-effort contract (I5): a bad record / broken path must not raise into the loop.
        ledger = pressures.PressureLedger(self.config, max_bytes=10_000_000)
        ledger.append({"tick": 1, "bad": object()})  # not JSON-serialisable — swallowed


if __name__ == "__main__":
    unittest.main()
