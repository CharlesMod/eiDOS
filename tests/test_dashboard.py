"""Tests for ASCII art creature sprites."""

import json
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent))

import unittest
from ascii_art import get_creature, _stage


class TestStage(unittest.TestCase):

    def test_seed(self):
        self.assertEqual(_stage(1), "seed")
        self.assertEqual(_stage(2), "seed")

    def test_sprout(self):
        self.assertEqual(_stage(3), "sprout")
        self.assertEqual(_stage(4), "sprout")

    def test_creature(self):
        self.assertEqual(_stage(5), "creature")
        self.assertEqual(_stage(7), "creature")

    def test_guardian(self):
        self.assertEqual(_stage(8), "guardian")
        self.assertEqual(_stage(15), "guardian")


class TestGetCreature(unittest.TestCase):

    MOODS = ["focused", "curious", "determined", "triumphant", "frustrated", "struggling"]
    LEVELS = [1, 2, 3, 5, 8, 12]

    def test_all_mood_level_combos_return_frames(self):
        for level in self.LEVELS:
            for mood in self.MOODS:
                result = get_creature(level, mood)
                self.assertIn("frames", result)
                self.assertIsInstance(result["frames"], list)
                self.assertGreater(len(result["frames"]), 0, f"No frames for level={level} mood={mood}")
                for frame in result["frames"]:
                    self.assertIsInstance(frame, str)
                    self.assertGreater(len(frame.strip()), 0, f"Empty frame for level={level} mood={mood}")

    def test_result_has_required_keys(self):
        result = get_creature(5, "focused")
        self.assertIn("frames", result)
        self.assertIn("interval_ms", result)
        self.assertIn("stage", result)
        self.assertIn("particles", result)

    def test_special_sleeping(self):
        result = get_creature(5, "focused", special="sleeping")
        self.assertEqual(len(result["frames"]), 1)
        self.assertIn("z", result["frames"][0].lower())

    def test_special_thinking(self):
        result = get_creature(5, "focused", special="thinking")
        self.assertEqual(len(result["frames"]), 3)

    def test_special_thermal(self):
        result = get_creature(5, "focused", special="thermal")
        self.assertEqual(len(result["frames"]), 1)
        self.assertIn("~", result["frames"][0])

    def test_special_dead(self):
        result = get_creature(5, "focused", special="dead")
        self.assertIn("x", result["frames"][0])

    def test_unknown_mood_fallback(self):
        result = get_creature(5, "nonexistent_mood")
        self.assertGreater(len(result["frames"]), 0)

    def test_animation_intervals_positive(self):
        for level in self.LEVELS:
            result = get_creature(level, "focused")
            self.assertGreater(result["interval_ms"], 0)


class TestDashboardSmoke(unittest.TestCase):

    def test_dashboard_imports(self):
        """Verify dashboard.py can be imported without errors."""
        import dashboard
        self.assertTrue(hasattr(dashboard, "build_status"))
        self.assertTrue(hasattr(dashboard, "build_ping"))

    def test_build_status_empty_workspace(self):
        """build_status should not crash on missing workspace files."""
        import tempfile
        import shutil
        from config import Config
        import dashboard

        tmpdir = tempfile.mkdtemp()
        try:
            config = Config(workspace_dir=tmpdir)
            Path(tmpdir).mkdir(parents=True, exist_ok=True)
            status = dashboard.build_status(config)
            self.assertIn("heartbeat", status)
            self.assertIn("persona", status)
            self.assertIn("creature", status)
            self.assertIn("observations", status)
        finally:
            shutil.rmtree(tmpdir)

    def test_build_ping_empty(self):
        import tempfile
        import shutil
        from config import Config
        import dashboard

        tmpdir = tempfile.mkdtemp()
        try:
            config = Config(workspace_dir=tmpdir)
            Path(tmpdir).mkdir(parents=True, exist_ok=True)
            ping = dashboard.build_ping(config)
            self.assertIn("ok", ping)
            self.assertIn("tick", ping)
            self.assertLess(len(json.dumps(ping)), 500)
        finally:
            shutil.rmtree(tmpdir)


if __name__ == "__main__":
    import json
    unittest.main()
