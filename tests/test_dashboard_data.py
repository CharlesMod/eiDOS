"""Tests for dashboard data-building functions."""

import json
import os
import sys
import tempfile
import time
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent))

import unittest
from config import Config
from dashboard import build_status, build_ping, build_chat


class TestBuildStatus(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.config = Config()
        self.config.workspace_dir = self.tmp
        # Create required dirs
        self.config.interventions_dir.mkdir(parents=True, exist_ok=True)
        self.config.snapshots_dir.mkdir(parents=True, exist_ok=True)

    def tearDown(self):
        import shutil
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_build_status_empty_workspace(self):
        """Should not crash with no workspace files."""
        status = build_status(self.config)
        self.assertIn("heartbeat", status)
        self.assertIn("persona", status)
        self.assertIn("creature", status)
        self.assertIn("goal", status)
        self.assertIn("observations", status)
        self.assertIn("ts", status)

    def test_build_status_with_heartbeat(self):
        hb = {"ts": 1712000000, "tick": 42, "level": 3, "mood": "focused",
               "consecutive_failures": 0}
        (Path(self.tmp) / "heartbeat.json").write_text(json.dumps(hb))
        status = build_status(self.config)
        self.assertEqual(status["heartbeat"]["tick"], 42)

    def test_build_status_with_goal(self):
        (Path(self.tmp) / "goal.md").write_text("Build a web scraper")
        status = build_status(self.config)
        self.assertIn("Build a web scraper", status["goal"])

    def test_build_status_goal_truncated(self):
        (Path(self.tmp) / "goal.md").write_text("x" * 1000)
        status = build_status(self.config)
        self.assertLessEqual(len(status["goal"]), 500)

    def test_build_status_dead_state(self):
        hb = {"consecutive_failures": 10}
        (Path(self.tmp) / "heartbeat.json").write_text(json.dumps(hb))
        status = build_status(self.config)
        # Creature should be in dead state
        self.assertIn("creature", status)

    def test_build_status_sleeping_no_goal(self):
        # No goal.md → sleeping
        status = build_status(self.config)
        self.assertIn("creature", status)

    def test_build_status_with_observations(self):
        obs = [{"tick": 1, "output": "hello"}, {"tick": 2, "output": "world"}]
        obs_path = Path(self.tmp) / "observations.jsonl"
        with open(obs_path, "w") as f:
            for o in obs:
                f.write(json.dumps(o) + "\n")
        status = build_status(self.config)
        self.assertEqual(len(status["observations"]), 2)

    def test_build_status_persona(self):
        persona = {"name": "TestBot", "level": 5, "xp": 100, "mood": "curious",
                    "traits": ["verbose"], "titles": ["Debugger"], "goals_completed": 3,
                    "total_ticks": 50, "longest_streak": 10}
        (Path(self.tmp) / "persona.json").write_text(json.dumps(persona))
        status = build_status(self.config)
        self.assertEqual(status["persona"]["name"], "TestBot")
        self.assertEqual(status["persona"]["level"], 5)


class TestBuildPing(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.config = Config()
        self.config.workspace_dir = self.tmp

    def tearDown(self):
        import shutil
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_build_ping_empty(self):
        ping = build_ping(self.config)
        self.assertIn("ok", ping)
        self.assertIn("tick", ping)

    def test_build_ping_with_heartbeat(self):
        hb = {"ts": 1712000000, "tick": 10, "level": 2, "mood": "focused",
               "consecutive_failures": 0, "ram_pct": 45.0,
               "disk_free_gb": 10, "uptime_s": 3600}
        (Path(self.tmp) / "heartbeat.json").write_text(json.dumps(hb))
        ping = build_ping(self.config)
        self.assertTrue(ping["ok"])
        self.assertEqual(ping["tick"], 10)
        self.assertEqual(ping["mood"], "focused")

    def test_build_ping_failure_state(self):
        hb = {"consecutive_failures": 7}
        (Path(self.tmp) / "heartbeat.json").write_text(json.dumps(hb))
        ping = build_ping(self.config)
        self.assertFalse(ping["ok"])


class TestBuildChat(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.config = Config()
        self.config.workspace_dir = self.tmp
        self.config.interventions_dir.mkdir(parents=True, exist_ok=True)

    def tearDown(self):
        import shutil
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_build_chat_empty(self):
        chat = build_chat(self.config)
        self.assertIn("messages", chat)
        self.assertEqual(len(chat["messages"]), 0)

    def test_build_chat_intervention_pending(self):
        ipath = self.config.interventions_dir / "msg_001.md"
        ipath.write_text("Please check memory usage")
        chat = build_chat(self.config)
        self.assertEqual(len(chat["messages"]), 1)
        self.assertEqual(chat["messages"][0]["direction"], "outgoing")
        self.assertEqual(chat["messages"][0]["status"], "pending")
        self.assertIn("memory usage", chat["messages"][0]["text"])

    def test_build_chat_intervention_done(self):
        ipath = self.config.interventions_dir / "msg_001.md.done"
        ipath.write_text("Old intervention")
        chat = build_chat(self.config)
        self.assertEqual(len(chat["messages"]), 1)
        self.assertEqual(chat["messages"][0]["status"], "delivered")

    def test_build_chat_ignores_legacy_pending_questions(self):
        """ask_supervisor/pending_questions was removed in v2 phase 0h — a leftover
        file must contribute nothing to the chat feed."""
        qpath = Path(self.tmp) / "pending_questions.jsonl"
        entry = {"ts": "2026-04-04T10:00:00Z", "question": "What port?", "status": "pending"}
        qpath.write_text(json.dumps(entry) + "\n")
        chat = build_chat(self.config)
        self.assertEqual(len(chat["messages"]), 0)

    def test_build_chat_mixed_sorted(self):
        """Interventions and outgoing replies are sorted by timestamp."""
        ipath = self.config.interventions_dir / "msg_001.md"
        ipath.write_text("First message")
        os.utime(ipath, (1712000000, 1712000000))

        rpath = Path(self.tmp) / "chat_replies.jsonl"
        entry = {"ts": "2026-04-04T12:00:00Z", "tick": 7, "text": "Later reply"}
        rpath.write_text(json.dumps(entry) + "\n")

        chat = build_chat(self.config)
        self.assertEqual(len(chat["messages"]), 2)
        self.assertIn("Later reply", chat["messages"][1]["text"])

    def test_build_chat_skips_hidden_files(self):
        (self.config.interventions_dir / ".hidden").write_text("secret")
        chat = build_chat(self.config)
        self.assertEqual(len(chat["messages"]), 0)

    def test_build_chat_skips_empty_interventions(self):
        (self.config.interventions_dir / "empty.md").write_text("")
        chat = build_chat(self.config)
        self.assertEqual(len(chat["messages"]), 0)

    def test_build_chat_truncates_long_messages(self):
        ipath = self.config.interventions_dir / "long.md"
        ipath.write_text("x" * 5000)
        chat = build_chat(self.config)
        self.assertLessEqual(len(chat["messages"][0]["text"]), 2000)

    def test_build_chat_no_interventions_dir(self):
        import shutil
        shutil.rmtree(str(self.config.interventions_dir))
        chat = build_chat(self.config)
        self.assertEqual(len(chat["messages"]), 0)


if __name__ == "__main__":
    unittest.main()
