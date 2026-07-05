"""Tests for memory module."""

import json
import os
import sys
import tempfile
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent))

import unittest
from config import Config
from memory import (
    read_goal,
    read_plan,
    write_plan,
    append_observation,
    read_recent_observations,
    count_observation_chars,
    count_observation_lines,
    truncate_observations,
    validate_observations,
    read_interventions,
)


class TestObservationArchive(unittest.TestCase):
    """Compaction clears observations.jsonl — the raw lines must land in a dated archive
    first (dream extraction is lossy; before this the raw record was gone forever)."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.config = Config()
        self.config.workspace_dir = os.path.join(self.tmp, "workspace")
        os.makedirs(self.config.workspace_dir)

    def tearDown(self):
        import shutil
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_truncate_archives_then_keeps_a_tail(self):
        # Post-dream continuity: the last _OBS_KEEP_TAIL observations SURVIVE the dream (the
        # creature wakes mid-thought, not amnesiac); only the removed bulk is archived.
        from memory import _OBS_KEEP_TAIL
        n = _OBS_KEEP_TAIL + 3
        for i in range(n):
            append_observation(self.config, {"tick": i, "tool": "bash", "success": True,
                                             "output": f"obs {i}"})
        removed = truncate_observations(self.config)
        self.assertEqual(removed, 3)
        self.assertEqual(count_observation_lines(self.config), _OBS_KEEP_TAIL)
        arcs = list(self.config.state_dir.glob("observations_archive_*.jsonl"))
        self.assertEqual(len(arcs), 1)
        rows = [json.loads(ln) for ln in arcs[0].read_text(encoding="utf-8").splitlines()]
        self.assertEqual([r["tick"] for r in rows], [0, 1, 2])   # the bulk, oldest-first
        live = [json.loads(ln) for ln in
                open(self.config.observations_path, encoding="utf-8")]
        self.assertEqual([r["tick"] for r in live], list(range(3, n)))   # the tail lives on

    def test_archive_appends_across_compactions(self):
        # Each dream archives what it removes; the kept tail is archived by the NEXT dream that
        # removes it (no loss, no duplication).
        from memory import _OBS_KEEP_TAIL
        for i in range(_OBS_KEEP_TAIL + 1):
            append_observation(self.config, {"tick": i, "tool": "bash", "success": True,
                                             "output": "a"})
        self.assertEqual(truncate_observations(self.config), 1)
        for i in range(_OBS_KEEP_TAIL):
            append_observation(self.config, {"tick": 100 + i, "tool": "bash", "success": True,
                                             "output": "b"})
        self.assertEqual(truncate_observations(self.config), _OBS_KEEP_TAIL)
        arcs = list(self.config.state_dir.glob("observations_archive_*.jsonl"))
        self.assertEqual(len(arcs), 1)
        self.assertEqual(len(arcs[0].read_text(encoding="utf-8").splitlines()),
                         1 + _OBS_KEEP_TAIL)

    def test_small_stream_survives_untouched(self):
        # Fewer observations than the tail → nothing removed, nothing archived.
        append_observation(self.config, {"tick": 1, "tool": "bash", "success": True, "output": "x"})
        self.assertEqual(truncate_observations(self.config), 0)
        self.assertEqual(count_observation_lines(self.config), 1)
        self.assertEqual(list(self.config.state_dir.glob("observations_archive_*.jsonl")), [])

    def test_empty_observations_no_archive(self):
        self.assertEqual(truncate_observations(self.config), 0)
        self.assertEqual(list(self.config.state_dir.glob("observations_archive_*.jsonl")), [])


class TestMemory(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.config = Config()
        self.config.workspace_dir = os.path.join(self.tmp, "workspace")
        os.makedirs(self.config.workspace_dir)
        os.makedirs(str(self.config.interventions_dir))

    def tearDown(self):
        import shutil
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _chat(self):
        import json as _j
        p = self.config.workspace / "chat_replies.jsonl"
        if not p.exists():
            return []
        return [_j.loads(ln) for ln in p.read_text(encoding="utf-8").splitlines() if ln.strip()]

    def test_chat_line_reply_then_speak_merges_to_one_spoken(self):
        from memory import append_chat_line
        append_chat_line(self.config, "Hello Boss.", spoken=False, tick=5)   # <reply>
        append_chat_line(self.config, "Hello Boss.", spoken=True)            # speak — same line
        entries = self._chat()
        self.assertEqual(len(entries), 1)            # ONE entry, not a duplicate
        self.assertTrue(entries[0]["spoken"])        # upgraded to spoken
        self.assertEqual(entries[0]["text"], "Hello Boss.")

    def test_chat_line_spoken_opener_of_longer_reply_keeps_longer(self):
        from memory import append_chat_line
        append_chat_line(self.config, "Hello Boss.", spoken=True)                      # spoke opener
        append_chat_line(self.config, "Hello Boss. The network looks healthy.", spoken=False)  # full reply
        entries = self._chat()
        self.assertEqual(len(entries), 1)
        self.assertTrue(entries[0]["spoken"])
        self.assertEqual(entries[0]["text"], "Hello Boss. The network looks healthy.")  # longer kept

    def test_chat_line_distinct_lines_not_merged(self):
        from memory import append_chat_line
        append_chat_line(self.config, "First thing.", spoken=False, tick=1)
        append_chat_line(self.config, "Totally different thing.", spoken=True, tick=2)
        self.assertEqual(len(self._chat()), 2)

    def test_chat_line_speak_only_logs_spoken(self):
        from memory import append_chat_line
        append_chat_line(self.config, "Just talking out loud.", spoken=True)
        entries = self._chat()
        self.assertEqual(len(entries), 1)
        self.assertTrue(entries[0]["spoken"])

    def test_is_degenerate_catches_byte_loop(self):
        from memory import is_degenerate
        self.assertTrue(is_degenerate("¥¥¡" * 200))   # the ¥¥¡ flood
        self.assertTrue(is_degenerate("a" * 200))                    # single-char run
        self.assertTrue(is_degenerate("ababab" * 50))                # 2-char cycle

    def test_is_degenerate_clears_real_text(self):
        from memory import is_degenerate
        self.assertFalse(is_degenerate("Boss, the network scan found 25 hosts; pivoting to port 8080."))
        self.assertFalse(is_degenerate("ok"))                        # too short to judge
        self.assertFalse(is_degenerate(""))

    def test_degenerate_thought_dropped(self):
        from memory import append_thought, read_recent_thoughts
        append_thought(self.config, 90, "¥¥¡" * 200)  # degenerate → dropped
        append_thought(self.config, 91, "A real, useful thought about the next step.")
        thoughts = read_recent_thoughts(self.config, n=10)
        self.assertEqual(len(thoughts), 1)
        self.assertIn("real, useful", thoughts[0]["text"])

    def test_degenerate_chat_line_dropped(self):
        from memory import append_chat_line
        append_chat_line(self.config, "¥¥¡" * 200, spoken=True)
        self.assertEqual(self._chat(), [])

    def test_junk_run_catches_partial_prefix(self):
        from memory import has_junk_run
        # the tick-93 case: byte-junk PREFIX then recovered real text (is_degenerate alone misses it)
        self.assertTrue(has_junk_run("¥¥¡¥¥¡¥√%500 I've identified a web interface on .147:3000."))
        self.assertFalse(has_junk_run("Boss, port 9100 is the raw print protocol; probing .60 next."))
        self.assertFalse(has_junk_run("Café señor paid £5 at 20° — totally fine prose."))  # isolated symbols ok

    def test_junk_prefixed_thought_dropped(self):
        from memory import append_thought, read_recent_thoughts
        append_thought(self.config, 93, "¥¥¡¥¥¡¥√ then some recovered-looking text that should not be trusted")
        append_thought(self.config, 94, "A clean follow-up thought.")
        thoughts = read_recent_thoughts(self.config, n=10)
        self.assertEqual(len(thoughts), 1)
        self.assertIn("clean follow-up", thoughts[0]["text"])

    def test_log_degeneration_writes_record(self):
        from memory import log_degeneration
        log_degeneration(self.config, 90, "¥¥¡ raw response here", reason="junk_run")
        p = self.config.workspace / "degeneration_log.jsonl"
        self.assertTrue(p.exists())
        import json as _j
        rec = _j.loads(p.read_text(encoding="utf-8").splitlines()[0])
        self.assertEqual(rec["tick"], 90)
        self.assertEqual(rec["reason"], "junk_run")

    def test_read_goal_missing(self):
        self.assertEqual(read_goal(self.config), "")

    def test_read_goal_exists(self):
        self.config.goal_path.write_text("Find the answer.")
        self.assertEqual(read_goal(self.config), "Find the answer.")



    def test_append_observation(self):
        append_observation(self.config, {"tick": 1, "tool": "bash", "output": "hello"})
        append_observation(self.config, {"tick": 2, "tool": "bash", "output": "world"})
        with open(self.config.observations_path) as f:
            lines = f.readlines()
        self.assertEqual(len(lines), 2)
        entry = json.loads(lines[0])
        self.assertEqual(entry["tick"], 1)
        self.assertIn("ts", entry)  # timestamp auto-added

    def test_read_recent_newest_first(self):
        for i in range(5):
            append_observation(self.config, {"tick": i, "tool": "bash", "output": f"out_{i}"})
        obs = read_recent_observations(self.config, max_chars=10000, max_count=3)
        self.assertEqual(len(obs), 3)
        self.assertEqual(obs[0]["tick"], 4)  # newest first

    def test_read_recent_char_budget(self):
        for i in range(20):
            append_observation(self.config, {"tick": i, "tool": "bash", "output": "x" * 100})
        obs = read_recent_observations(self.config, max_chars=500, max_count=100)
        total = sum(len(json.dumps(o)) for o in obs)
        # Should be under budget (roughly)
        self.assertLess(len(obs), 20)

    def test_validate_good_file(self):
        append_observation(self.config, {"tick": 1, "output": "ok"})
        truncated = validate_observations(self.config)
        self.assertEqual(truncated, 0)

    def test_validate_malformed_last_line(self):
        with open(self.config.observations_path, "w") as f:
            f.write('{"tick": 1}\n')
            f.write('{"tick": 2}\n')
            f.write('{"tick": 3, broken\n')
        truncated = validate_observations(self.config)
        self.assertEqual(truncated, 1)
        with open(self.config.observations_path) as f:
            lines = f.readlines()
        self.assertEqual(len(lines), 2)

    def test_interventions(self):
        # Write an intervention file
        (self.config.interventions_dir / "001_hint.md").write_text("Try a different API endpoint")
        interventions = read_interventions(self.config)
        self.assertEqual(len(interventions), 1)
        self.assertIn("different API", interventions[0]["content"])
        # File should be renamed to .done
        self.assertTrue((self.config.interventions_dir / "001_hint.md.done").exists())

    def test_interventions_skip_done(self):
        (self.config.interventions_dir / "old.md.done").write_text("already consumed")
        interventions = read_interventions(self.config)
        self.assertEqual(len(interventions), 0)







    # --- New tests: observation edge cases ---

    def test_append_observation_creates_file(self):
        """observations.jsonl is created on first append."""
        self.assertFalse(self.config.observations_path.exists())
        append_observation(self.config, {"tick": 1, "output": "first"})
        self.assertTrue(self.config.observations_path.exists())

    def test_append_observation_preserves_existing(self):
        append_observation(self.config, {"tick": 1, "output": "one"})
        append_observation(self.config, {"tick": 2, "output": "two"})
        append_observation(self.config, {"tick": 3, "output": "three"})
        with open(self.config.observations_path) as f:
            lines = f.readlines()
        self.assertEqual(len(lines), 3)
        self.assertEqual(json.loads(lines[0])["output"], "one")
        self.assertEqual(json.loads(lines[2])["output"], "three")

    def test_append_observation_auto_timestamp(self):
        append_observation(self.config, {"tick": 1, "output": "data"})
        with open(self.config.observations_path) as f:
            entry = json.loads(f.readline())
        self.assertIn("ts", entry)
        self.assertRegex(entry["ts"], r"\d{4}-\d{2}-\d{2}T")

    def test_append_observation_rejects_bad_typed_record(self):
        with self.assertRaises(ValueError):
            append_observation(self.config, {"tick": "one", "output": "bad"})
        self.assertFalse(self.config.observations_path.exists())

    def test_read_recent_observations_skips_bad_typed_record(self):
        good = {"tick": 2, "ts": "2026-06-23T00:00:00Z", "output": "ok"}
        bad = {"tick": "two", "ts": "2026-06-23T00:00:01Z", "output": "bad"}
        self.config.observations_path.write_text(
            json.dumps(good) + "\n" + json.dumps(bad) + "\n",
            encoding="utf-8",
        )
        obs = read_recent_observations(self.config, max_chars=10000, max_count=10)
        self.assertEqual(obs, [good])

    def test_chat_reply_record_round_trips(self):
        from memory import append_chat_line
        append_chat_line(self.config, "Typed record.", spoken=True, tick=3)
        entries = self._chat()
        self.assertEqual(entries[0]["text"], "Typed record.")
        self.assertTrue(entries[0]["spoken"])
        self.assertEqual(entries[0]["tick"], 3)

    def test_append_observation_preserves_existing_timestamp(self):
        append_observation(self.config, {"tick": 1, "ts": "2026-01-01T00:00:00Z", "output": "data"})
        with open(self.config.observations_path) as f:
            entry = json.loads(f.readline())
        self.assertEqual(entry["ts"], "2026-01-01T00:00:00Z")

    # --- New tests: read_recent_observations ---

    def test_read_recent_empty_file(self):
        self.config.observations_path.write_text("")
        obs = read_recent_observations(self.config, max_chars=10000, max_count=10)
        self.assertEqual(obs, [])

    def test_read_recent_max_count_exact(self):
        for i in range(5):
            append_observation(self.config, {"tick": i, "output": f"obs_{i}"})
        obs = read_recent_observations(self.config, max_chars=100000, max_count=5)
        self.assertEqual(len(obs), 5)

    def test_read_recent_skips_malformed_lines(self):
        with open(self.config.observations_path, "w") as f:
            f.write('{"tick": 1, "output": "good"}\n')
            f.write('BROKEN JSON LINE\n')
            f.write('{"tick": 3, "output": "also good"}\n')
        obs = read_recent_observations(self.config, max_chars=10000, max_count=10)
        self.assertEqual(len(obs), 2)
        self.assertEqual(obs[0]["tick"], 3)  # newest first

    def test_read_recent_handles_blank_lines(self):
        with open(self.config.observations_path, "w") as f:
            f.write('{"tick": 1, "output": "one"}\n')
            f.write('\n')
            f.write('{"tick": 2, "output": "two"}\n')
            f.write('\n\n')
        obs = read_recent_observations(self.config, max_chars=10000, max_count=10)
        self.assertEqual(len(obs), 2)

    def test_read_recent_single_entry(self):
        append_observation(self.config, {"tick": 1, "output": "only"})
        obs = read_recent_observations(self.config, max_chars=10000, max_count=10)
        self.assertEqual(len(obs), 1)
        self.assertEqual(obs[0]["output"], "only")

    def test_read_recent_file_missing(self):
        obs = read_recent_observations(self.config, max_chars=10000, max_count=10)
        self.assertEqual(obs, [])

    # --- New tests: count functions ---

    def test_count_observation_chars(self):
        append_observation(self.config, {"tick": 1, "output": "hello"})
        chars = count_observation_chars(self.config)
        self.assertGreater(chars, 0)

    def test_count_observation_chars_missing(self):
        self.assertEqual(count_observation_chars(self.config), 0)

    def test_count_observation_lines(self):
        for i in range(7):
            append_observation(self.config, {"tick": i, "output": f"obs_{i}"})
        self.assertEqual(count_observation_lines(self.config), 7)

    def test_count_observation_lines_missing(self):
        self.assertEqual(count_observation_lines(self.config), 0)

    # --- New tests: validate edge cases ---

    def test_validate_missing_file(self):
        truncated = validate_observations(self.config)
        self.assertEqual(truncated, 0)

    def test_validate_empty_file(self):
        self.config.observations_path.write_text("")
        truncated = validate_observations(self.config)
        self.assertEqual(truncated, 0)

    def test_validate_all_lines_good(self):
        for i in range(5):
            append_observation(self.config, {"tick": i, "output": f"obs_{i}"})
        truncated = validate_observations(self.config)
        self.assertEqual(truncated, 0)
        self.assertEqual(count_observation_lines(self.config), 5)

    # --- New tests: intervention edge cases ---

    def test_interventions_sorted_order(self):
        (self.config.interventions_dir / "003_last.md").write_text("third")
        (self.config.interventions_dir / "001_first.md").write_text("first")
        (self.config.interventions_dir / "002_mid.md").write_text("second")
        interventions = read_interventions(self.config)
        self.assertEqual(len(interventions), 3)
        self.assertEqual(interventions[0]["content"], "first")
        self.assertEqual(interventions[2]["content"], "third")

    def test_interventions_skip_hidden_files(self):
        (self.config.interventions_dir / ".hidden").write_text("should be skipped")
        (self.config.interventions_dir / "visible.md").write_text("should be read")
        interventions = read_interventions(self.config)
        self.assertEqual(len(interventions), 1)
        self.assertEqual(interventions[0]["content"], "should be read")

    def test_interventions_skip_empty_files(self):
        (self.config.interventions_dir / "empty.md").write_text("")
        (self.config.interventions_dir / "whitespace.md").write_text("   \n\n  ")
        interventions = read_interventions(self.config)
        self.assertEqual(len(interventions), 0)

    def test_interventions_dir_missing(self):
        import shutil
        shutil.rmtree(str(self.config.interventions_dir))
        interventions = read_interventions(self.config)
        self.assertEqual(len(interventions), 0)

    # --- Plan (plan.md) tests ---

    def test_write_read_plan(self):
        write_plan(self.config, "# Plan\nStep 1: do the thing")
        self.assertEqual(read_plan(self.config), "# Plan\nStep 1: do the thing")

    def test_plan_path_is_plan_md(self):
        self.assertTrue(str(self.config.plan_path).endswith("plan.md"))




    def test_read_plan_missing_both(self):
        self.assertEqual(read_plan(self.config), "")

    def test_write_plan_atomic(self):
        write_plan(self.config, "plan content")
        tmp_files = list(Path(self.config.workspace_dir).glob(".plan_*"))
        self.assertEqual(len(tmp_files), 0)


if __name__ == "__main__":
    unittest.main()
