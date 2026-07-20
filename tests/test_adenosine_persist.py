"""Sleep economics: adenosine (sleep pressure) survives a process restart.

Adenosine used to live only in RAM: `_aden_mark = time.monotonic()` and
`level_hours = 0.0` reset on every boot, so a 30 s crash-respawn "rested" the
body exactly like a full night. With real operator sessions (a few hours at a
time) the creature accrued ~1 nap in 9 awake-hours while the level gates want 3
naps/level — the whole sleep-gated growth loop starved (PILLARS_TODO "Dream vs
nap" known seam: "adenosine is in-memory — a restart rests the body").

The fix persists level_hours + a wall-clock stamp under config.state_dir on
every mutation and, on boot, credits REAL elapsed downtime as rest at the
organ's resting rate. These pins nail the boundary cases:

  · persistence round-trip (save then reload restores the pressure);
  · crash-respawn (a ~30 s gap leaves pressure essentially unchanged — the bug);
  · long downtime (a ~10 h overnight gap rests the body ≈ a night's sleep);
  · corrupt / missing file → fresh default (level_hours 0), never a crash;
  · backward clock (saved_at in the future) → no rest, no crash.

No services / GPU — a temp-workspace Config only (test_health_probe conventions).
"""
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).parent.parent))

from config import Config
from nervous.neuromod import Adenosine


def _cfg() -> Config:
    c = Config()
    c.workspace_dir = tempfile.mkdtemp()
    c.workspace.mkdir(parents=True, exist_ok=True)
    c.state_dir.mkdir(parents=True, exist_ok=True)
    return c


class TestAdenosinePersist(unittest.TestCase):
    def _path(self, cfg):
        return cfg.state_dir / Adenosine.STATE_NAME

    def test_round_trip_restores_pressure(self):
        """accumulate() persists; a fresh Adenosine on the SAME workspace reloads it."""
        cfg = _cfg()
        a = Adenosine(max_wake_hours=18.0, config=cfg)
        a.accumulate(5.0)                       # 5 wake-hours banked and written to disk
        self.assertTrue(self._path(cfg).exists())

        # Reload with saved_at ~now (no downtime credited) — pressure comes back intact.
        with patch("nervous.neuromod.time.time", return_value=json.loads(
                self._path(cfg).read_text())["saved_at"]):
            b = Adenosine(max_wake_hours=18.0, config=cfg)
        self.assertAlmostEqual(b.level_hours, 5.0, places=6)

    def test_crash_respawn_keeps_pressure(self):
        """A ~30 s gap (crash-respawn) must NOT meaningfully reset sleep pressure — the whole bug."""
        cfg = _cfg()
        a = Adenosine(max_wake_hours=18.0, config=cfg)
        a.accumulate(10.0)
        saved_at = json.loads(self._path(cfg).read_text())["saved_at"]

        with patch("nervous.neuromod.time.time", return_value=saved_at + 30.0):
            b = Adenosine(max_wake_hours=18.0, config=cfg)
        # 30 s of downtime rests 30/3600 * 2.5 ≈ 0.021 h — a rounding error vs. 10 banked hours.
        self.assertGreater(b.level_hours, 9.9)
        self.assertAlmostEqual(b.level_hours, 10.0, places=1)
        # And the creature is still just as tired: pressure is essentially unchanged.
        self.assertGreater(b.pressure(), a.pressure() - 0.01)

    def test_long_downtime_rests_the_body(self):
        """A ~10 h overnight gap should rest the body roughly like a full night's sleep."""
        cfg = _cfg()
        a = Adenosine(max_wake_hours=18.0, config=cfg)
        a.accumulate(17.0)                      # nearly saturated: an exhausted creature
        self.assertGreater(a.pressure(), 0.9)
        saved_at = json.loads(self._path(cfg).read_text())["saved_at"]

        with patch("nervous.neuromod.time.time", return_value=saved_at + 10 * 3600.0):
            b = Adenosine(max_wake_hours=18.0, config=cfg)
        # 10 h * 2.5 = 25 h of rest credited — more than the whole ceiling → fully rested.
        self.assertEqual(b.level_hours, 0.0)
        self.assertEqual(b.pressure(), 0.0)

    def test_moderate_downtime_rests_partially(self):
        """Rest is proportional: an hour off clears REST_HOURS_PER_DOWN_HOUR of banked wake."""
        cfg = _cfg()
        a = Adenosine(max_wake_hours=18.0, config=cfg)
        a.accumulate(10.0)
        saved_at = json.loads(self._path(cfg).read_text())["saved_at"]

        with patch("nervous.neuromod.time.time", return_value=saved_at + 3600.0):
            b = Adenosine(max_wake_hours=18.0, config=cfg)
        # 1 h off → 2.5 h rested → 10 - 2.5 = 7.5 h remaining.
        self.assertAlmostEqual(b.level_hours, 7.5, places=3)

    def test_corrupt_file_fails_open(self):
        """A garbage state file must yield a fresh default (level 0), never crash construction."""
        cfg = _cfg()
        self._path(cfg).write_text("{ this is not json ]", encoding="utf-8")
        a = Adenosine(max_wake_hours=18.0, config=cfg)     # must not raise
        self.assertEqual(a.level_hours, 0.0)

    def test_missing_file_fresh_default(self):
        """No state file yet (fresh creature / first boot) → level 0, no crash."""
        cfg = _cfg()
        self.assertFalse(self._path(cfg).exists())
        a = Adenosine(max_wake_hours=18.0, config=cfg)
        self.assertEqual(a.level_hours, 0.0)

    def test_negative_level_in_file_distrusted(self):
        """A file claiming negative/NaN wake pressure is distrusted → fresh default."""
        cfg = _cfg()
        self._path(cfg).write_text(json.dumps({"level_hours": -5.0, "saved_at": 1.0}),
                                   encoding="utf-8")
        a = Adenosine(max_wake_hours=18.0, config=cfg)
        self.assertEqual(a.level_hours, 0.0)

    def test_backward_clock_no_rest_no_crash(self):
        """saved_at in the future (system clock moved back) → keep pressure, credit no rest."""
        cfg = _cfg()
        a = Adenosine(max_wake_hours=18.0, config=cfg)
        a.accumulate(8.0)
        saved_at = json.loads(self._path(cfg).read_text())["saved_at"]

        # Reload with the wall clock 1 h BEHIND the save (down_s negative).
        with patch("nervous.neuromod.time.time", return_value=saved_at - 3600.0):
            b = Adenosine(max_wake_hours=18.0, config=cfg)
        self.assertAlmostEqual(b.level_hours, 8.0, places=6)     # unchanged, not increased

    def test_clear_persists_the_boundary(self):
        """A sleep clear() writes 0 to disk, so a restart right after a nap stays rested."""
        cfg = _cfg()
        a = Adenosine(max_wake_hours=18.0, config=cfg)
        a.accumulate(12.0)
        a.clear()
        self.assertEqual(json.loads(self._path(cfg).read_text())["level_hours"], 0.0)

        saved_at = json.loads(self._path(cfg).read_text())["saved_at"]
        with patch("nervous.neuromod.time.time", return_value=saved_at + 30.0):
            b = Adenosine(max_wake_hours=18.0, config=cfg)
        self.assertEqual(b.level_hours, 0.0)

    def test_no_config_persistence_off(self):
        """No config → pure in-memory behaviour (byte-identical to the pre-persistence organ)."""
        a = Adenosine(max_wake_hours=18.0)     # config=None
        a.accumulate(4.0)                      # must not raise despite no state_dir
        self.assertAlmostEqual(a.level_hours, 4.0, places=6)


if __name__ == "__main__":
    unittest.main()
