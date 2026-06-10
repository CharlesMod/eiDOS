"""Phase 4: the event-driven control channel (dashboard producer + eidos consumer).

Tests the Condition semantics directly (control_wait/control_notify are module-level
in dashboard.py — no HTTP server needed), the producer hooks on the control mutations,
and the eidos-side client's fail-open behavior when the channel is down.
"""

import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

import dashboard
import gpu_gate
from config import Config


def _cfg():
    c = Config()
    c.workspace_dir = tempfile.mkdtemp()
    c.workspace.mkdir(parents=True, exist_ok=True)
    return c


class TestControlWaitSemantics(unittest.TestCase):

    def test_returns_immediately_when_already_past_since(self):
        cfg = _cfg()
        t0 = time.monotonic()
        res = dashboard.control_wait(cfg, since=-1, max_s=10.0)
        self.assertLess(time.monotonic() - t0, 0.5)
        self.assertGreaterEqual(res["seq"], 0)

    def test_blocks_then_wakes_on_notify(self):
        cfg = _cfg()
        base = dashboard.control_wait(cfg, since=-1, max_s=0.0)["seq"]
        woke = {}

        def waiter():
            t0 = time.monotonic()
            res = dashboard.control_wait(cfg, since=base, max_s=10.0)
            woke["dt"] = time.monotonic() - t0
            woke["seq"] = res["seq"]

        th = threading.Thread(target=waiter)
        th.start()
        time.sleep(0.3)                       # let the waiter block
        dashboard.control_notify("test")
        th.join(timeout=5)
        self.assertFalse(th.is_alive())
        self.assertLess(woke["dt"], 2.0)       # woke on the event, not the 10s timeout
        self.assertGreater(woke["seq"], base)

    def test_times_out_bounded_without_notify(self):
        cfg = _cfg()
        base = dashboard.control_wait(cfg, since=-1, max_s=0.0)["seq"]
        t0 = time.monotonic()
        res = dashboard.control_wait(cfg, since=base, max_s=0.5)
        dt = time.monotonic() - t0
        self.assertGreaterEqual(dt, 0.4)
        self.assertLess(dt, 3.0)
        self.assertEqual(res["seq"], base)     # nothing changed

    def test_snapshot_reflects_state(self):
        cfg = _cfg()
        (cfg.workspace / "paused").write_text("x")
        cfg.interventions_dir.mkdir(parents=True, exist_ok=True)
        (cfg.interventions_dir / "dash_1.md").write_text("hi")
        (cfg.interventions_dir / "old.md.done").write_text("done")
        res = dashboard.control_wait(cfg, since=-1, max_s=0.0)
        self.assertTrue(res["paused"])
        self.assertEqual(res["interventions"], 1)   # .done excluded
        self.assertFalse(res["held"])


class TestProducerHooks(unittest.TestCase):
    """Every control mutation must bump the seq (and so wake any waiter)."""

    def _seq(self, cfg):
        return dashboard.control_wait(cfg, since=-1, max_s=0.0)["seq"]

    def test_pause_resume_bump(self):
        cfg = _cfg()
        s0 = self._seq(cfg)
        dashboard._ctrl_pause(cfg)
        s1 = self._seq(cfg)
        self.assertGreater(s1, s0)
        dashboard._ctrl_resume(cfg)
        self.assertGreater(self._seq(cfg), s1)

    def test_chat_hold_bumps_both_ways(self):
        cfg = _cfg()
        s0 = self._seq(cfg)
        dashboard._write_chat_hold(cfg, True)
        s1 = self._seq(cfg)
        self.assertGreater(s1, s0)
        dashboard._write_chat_hold(cfg, False)
        self.assertGreater(self._seq(cfg), s1)


class TestClientFailOpen(unittest.TestCase):
    """gpu_gate.control_wait must return None fast when no dashboard is listening,
    then skip attempts during the cooldown (offline runs don't hammer connects)."""

    def setUp(self):
        gpu_gate._ctrl_down_until = 0.0

    def tearDown(self):
        gpu_gate._ctrl_down_until = 0.0

    def test_no_server_returns_none_and_cools_down(self):
        cfg = Config()
        cfg.dashboard_port = 1  # nothing listens here
        t0 = time.monotonic()
        self.assertIsNone(gpu_gate.control_wait(cfg, since=-1, max_s=0.5))
        self.assertLess(time.monotonic() - t0, 3.0)
        # second call inside the cooldown: instant None, no connect attempt
        t1 = time.monotonic()
        self.assertIsNone(gpu_gate.control_wait(cfg, since=-1, max_s=5.0))
        self.assertLess(time.monotonic() - t1, 0.05)


if __name__ == "__main__":
    unittest.main()
