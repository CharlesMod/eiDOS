"""Phase 8.2: the self-edit health-probe leg.

Before this, a self-edit that BOOTED but misbehaved was invisible — the watchdog
checked PID-exists only. The probe arms a pending_apply marker at apply time; the
booting eidos drops an applied_ok breadcrumb (a paused eidos never ticks, so the
heartbeat alone can't prove a healthy boot); the watchdog resolves it (paused-and-
booted, or ticking-past-baseline) or rolls back to prev_sha at the deadline.

These tests cover the marker/breadcrumb lifecycle and the _selfedit_probe decision
(git restore + restart are patched — they belong to the live smoke, not unit tests).
"""

import json
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).parent.parent))

import selfedit
import dashboard
from config import Config


def _cfg():
    c = Config()
    c.workspace_dir = tempfile.mkdtemp()
    c.workspace.mkdir(parents=True, exist_ok=True)
    c.state_dir.mkdir(parents=True, exist_ok=True)
    return c


def _set_heartbeat(config, ts):
    (config.workspace / "heartbeat.json").write_text(json.dumps({"ts": ts}))


class TestMarkerLifecycle(unittest.TestCase):

    def setUp(self):
        self.config = _cfg()

    def test_write_read_clear(self):
        selfedit.write_pending_apply(self.config, "se_1", "abc123", 100.0, time.time() + 90)
        p = selfedit.read_pending_apply(self.config)
        self.assertEqual(p["id"], "se_1")
        self.assertEqual(p["prev_sha"], "abc123")
        selfedit.clear_pending_apply(self.config)
        self.assertIsNone(selfedit.read_pending_apply(self.config))

    def test_new_apply_invalidates_old_breadcrumb(self):
        selfedit.write_pending_apply(self.config, "se_1", "a", 0, time.time() + 90)
        selfedit.write_applied_ok(self.config)
        self.assertEqual(selfedit.read_applied_ok(self.config)["id"], "se_1")
        # a fresh apply must drop the stale breadcrumb
        selfedit.write_pending_apply(self.config, "se_2", "b", 0, time.time() + 90)
        self.assertIsNone(selfedit.read_applied_ok(self.config))

    def test_breadcrumb_carries_pending_id(self):
        selfedit.write_pending_apply(self.config, "se_42", "a", 0, time.time() + 90)
        selfedit.write_applied_ok(self.config)
        self.assertEqual(selfedit.read_applied_ok(self.config)["id"], "se_42")

    def test_breadcrumb_noop_without_pending(self):
        selfedit.write_applied_ok(self.config)
        self.assertIsNone(selfedit.read_applied_ok(self.config))


class TestProbeDecision(unittest.TestCase):

    def setUp(self):
        self.config = _cfg()

    def test_no_pending_is_noop(self):
        with patch("selfedit.autorollback") as ar:
            dashboard._selfedit_probe(self.config)
            ar.assert_not_called()

    def test_healthy_when_booted_and_paused(self):
        selfedit.write_pending_apply(self.config, "se_1", "a", 0, time.time() + 90)
        selfedit.write_applied_ok(self.config)            # booted
        (self.config.workspace / "paused").write_text("x")  # awaiting GO
        with patch("selfedit.autorollback") as ar:
            dashboard._selfedit_probe(self.config)
            ar.assert_not_called()
        self.assertIsNone(selfedit.read_pending_apply(self.config))  # resolved

    def test_healthy_when_booted_and_ticking(self):
        selfedit.write_pending_apply(self.config, "se_1", "a",
                                     baseline_heartbeat_ts=100.0, deadline_epoch=time.time() + 90)
        selfedit.write_applied_ok(self.config)
        _set_heartbeat(self.config, 200.0)  # advanced past baseline
        with patch("selfedit.autorollback") as ar:
            dashboard._selfedit_probe(self.config)
            ar.assert_not_called()
        self.assertIsNone(selfedit.read_pending_apply(self.config))

    def test_within_window_waits(self):
        selfedit.write_pending_apply(self.config, "se_1", "a", 0, time.time() + 90)
        # no breadcrumb yet, deadline far off -> keep watching, don't roll back
        with patch("selfedit.autorollback") as ar:
            dashboard._selfedit_probe(self.config)
            ar.assert_not_called()
        self.assertIsNotNone(selfedit.read_pending_apply(self.config))

    def test_deadline_rollback_when_never_booted(self):
        selfedit.write_pending_apply(self.config, "se_1", "deadbeef",
                                     baseline_heartbeat_ts=0, deadline_epoch=time.time() - 1)
        with patch("selfedit.autorollback", return_value={"restored": 3}) as ar, \
             patch("dashboard._restart_eidos_keep_armed", return_value=4242), \
             patch("dashboard._watchdog_note"), patch("dashboard._watchdog_event"):
            dashboard._selfedit_probe(self.config)
            ar.assert_called_once()
            self.assertEqual(ar.call_args[0][1], "deadbeef")  # rolled back to prev_sha

    def test_deadline_rollback_when_wedged_alive(self):
        # booted, but running (not paused) with a stale heartbeat past the deadline
        selfedit.write_pending_apply(self.config, "se_1", "feed",
                                     baseline_heartbeat_ts=500.0, deadline_epoch=time.time() - 1)
        selfedit.write_applied_ok(self.config)
        _set_heartbeat(self.config, 500.0)  # never advanced past baseline
        with patch("selfedit.autorollback", return_value={"restored": 1}) as ar, \
             patch("dashboard._restart_eidos_keep_armed", return_value=1), \
             patch("dashboard._watchdog_note"), patch("dashboard._watchdog_event"):
            dashboard._selfedit_probe(self.config)
            ar.assert_called_once()


class TestAutorollback(unittest.TestCase):

    def setUp(self):
        self.config = _cfg()

    def test_marks_proposal_and_clears_marker(self):
        selfedit.write_pending_apply(self.config, "se_9", "abc", 0, time.time())
        with patch("git_safety.restore_to", return_value={"ok": True, "restored": 2}), \
             patch("selfedit._load_manifest", return_value={"status": "applied", "id": "se_9"}), \
             patch("selfedit._save_manifest") as save:
            res = selfedit.autorollback(self.config, "abc", "se_9")
        self.assertTrue(res.get("ok"))
        self.assertIsNone(selfedit.read_pending_apply(self.config))
        saved = save.call_args[0][1]
        self.assertEqual(saved["status"], "rolled_back")


if __name__ == "__main__":
    unittest.main()
