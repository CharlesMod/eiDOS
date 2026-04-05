"""Tests for persona system — XP, leveling, traits, mood, titles, persistence."""

import json
import math
import os
import tempfile
from pathlib import Path

import pytest

from persona import (
    _default_persona,
    load_persona,
    save_persona,
    compute_level,
    award_xp,
    record_tick,
    record_compaction,
    record_goal_complete,
    record_error_recovery,
    compute_traits,
    compute_mood,
    check_titles,
    format_prefix,
    format_status_line,
)


# --- XP & Leveling ---

class TestLeveling:
    def test_level_at_zero_xp(self):
        assert compute_level(0) == 1

    def test_level_negative_xp(self):
        assert compute_level(-10) == 1

    def test_level_at_50(self):
        assert compute_level(50) == 2

    def test_level_at_200(self):
        assert compute_level(200) == 3

    def test_level_at_800(self):
        assert compute_level(800) == 5

    def test_level_at_4050(self):
        # 1 + floor(sqrt(4050/50)) = 1 + floor(sqrt(81)) = 1 + 9 = 10
        assert compute_level(4050) == 10

    def test_level_at_4500(self):
        assert compute_level(4500) == 10

    def test_level_formula_monotonic(self):
        """Level never decreases as XP increases."""
        prev = 1
        for xp in range(0, 10001, 50):
            lvl = compute_level(xp)
            assert lvl >= prev
            prev = lvl


class TestXPAwards:
    def test_award_xp_basic(self):
        p = _default_persona()
        award_xp(p, 100)
        assert p["xp"] == 100
        assert p["level"] == 2  # sqrt(100/50) = sqrt(2) ~= 1.4, floor = 1, +1 = 2

    def test_award_xp_accumulates(self):
        p = _default_persona()
        award_xp(p, 50)
        award_xp(p, 50)
        assert p["xp"] == 100

    def test_goal_complete_awards_100(self):
        p = _default_persona()
        record_goal_complete(p, "test goal done")
        assert p["xp"] == 100
        assert p["goals_completed"] == 1
        assert p["last_goal_summary"] == "test goal done"

    def test_compaction_awards_2(self):
        p = _default_persona()
        record_compaction(p)
        assert p["xp"] == 2
        assert p["total_compactions"] == 1

    def test_error_recovery_awards_5(self):
        p = _default_persona()
        record_error_recovery(p)
        assert p["xp"] == 5
        assert p["total_errors_recovered"] == 1


# --- Record Tick ---

class TestRecordTick:
    def test_tick_increments_total(self):
        p = _default_persona()
        record_tick(p, "bash", True)
        assert p["total_ticks"] == 1

    def test_successful_tick_awards_xp(self):
        p = _default_persona()
        record_tick(p, "bash", True)
        assert p["xp"] == 1

    def test_failed_tick_no_xp(self):
        p = _default_persona()
        record_tick(p, "bash", False)
        assert p["xp"] == 0

    def test_streak_tracking(self):
        p = _default_persona()
        record_tick(p, "bash", True)
        record_tick(p, "bash", True)
        record_tick(p, "bash", True)
        assert p["current_streak"] == 3
        assert p["longest_streak"] == 3

    def test_streak_breaks_on_failure(self):
        p = _default_persona()
        record_tick(p, "bash", True)
        record_tick(p, "bash", True)
        record_tick(p, "bash", False)
        assert p["current_streak"] == 0
        assert p["longest_streak"] == 2

    def test_tool_usage_counted(self):
        p = _default_persona()
        record_tick(p, "bash", True)
        record_tick(p, "bash", True)
        record_tick(p, "write_file", True)
        assert p["tools_used"]["bash"] == 2
        assert p["tools_used"]["write_file"] == 1

    def test_none_tool_still_counts_tick(self):
        p = _default_persona()
        record_tick(p, None, False)
        assert p["total_ticks"] == 1
        assert p["tools_used"] == {}


# --- Traits ---

class TestTraits:
    def test_no_traits_initially(self):
        p = _default_persona()
        traits = compute_traits(p)
        assert traits == []

    def test_methodical_trait(self):
        p = _default_persona()
        p["tools_used"] = {"bash": 70, "read_file": 10, "write_file": 5}
        traits = compute_traits(p)
        assert "methodical" in traits

    def test_creative_trait(self):
        p = _default_persona()
        p["tools_used"] = {"write_file": 40, "bash": 10, "read_file": 5}
        traits = compute_traits(p)
        assert "creative" in traits

    def test_resilient_trait(self):
        p = _default_persona()
        p["total_errors_recovered"] = 25
        p["tools_used"] = {"bash": 50}
        traits = compute_traits(p)
        assert "resilient" in traits

    def test_veteran_trait(self):
        p = _default_persona()
        p["level"] = 6
        p["tools_used"] = {"bash": 50}
        traits = compute_traits(p)
        assert "veteran" in traits

    def test_max_three_traits(self):
        p = _default_persona()
        p["level"] = 10
        p["total_errors_recovered"] = 100
        p["longest_streak"] = 200
        p["tools_used"] = {"bash": 600, "read_file": 100, "http_get": 80, "remember": 200, "write_file": 300}
        traits = compute_traits(p)
        assert len(traits) <= 3

    def test_traits_stored_on_persona(self):
        p = _default_persona()
        p["tools_used"] = {"bash": 70, "read_file": 20}
        compute_traits(p)
        assert p["traits"] == compute_traits(p)


# --- Mood ---

class TestMood:
    def test_fresh_start_curious(self):
        p = _default_persona()
        mood = compute_mood(p)
        assert mood == "curious"

    def test_high_success_focused(self):
        p = _default_persona()
        mood = compute_mood(p, recent_successes=[True]*9 + [False])
        assert mood == "focused"

    def test_medium_success_determined(self):
        p = _default_persona()
        mood = compute_mood(p, recent_successes=[True]*6 + [False]*4)
        assert mood == "determined"

    def test_low_success_frustrated(self):
        p = _default_persona()
        mood = compute_mood(p, recent_successes=[True]*3 + [False]*7)
        assert mood == "frustrated"

    def test_very_low_success_struggling(self):
        p = _default_persona()
        mood = compute_mood(p, recent_successes=[True] + [False]*9)
        assert mood == "struggling"

    def test_post_goal_triumphant(self):
        p = _default_persona()
        mood = compute_mood(p, recent_successes=[True]*5, ticks_since_goal=3)
        assert mood == "triumphant"

    def test_triumphant_decays(self):
        p = _default_persona()
        mood = compute_mood(p, recent_successes=[True]*8, ticks_since_goal=6)
        assert mood == "focused"

    def test_mood_stored_on_persona(self):
        p = _default_persona()
        compute_mood(p, recent_successes=[True]*10)
        assert p["mood"] == "focused"


# --- Titles ---

class TestTitles:
    def test_no_titles_initially(self):
        p = _default_persona()
        new = check_titles(p)
        assert new == []
        assert p["titles"] == []

    def test_first_steps(self):
        p = _default_persona()
        p["goals_completed"] = 1
        new = check_titles(p)
        assert "First Steps" in new
        assert "First Steps" in p["titles"]

    def test_centurion(self):
        p = _default_persona()
        p["total_ticks"] = 100
        new = check_titles(p)
        assert "Centurion" in new

    def test_dream_weaver(self):
        p = _default_persona()
        p["total_compactions"] = 10
        new = check_titles(p)
        assert "Dream Weaver" in new

    def test_title_not_duplicated(self):
        p = _default_persona()
        p["goals_completed"] = 1
        check_titles(p)
        new = check_titles(p)
        assert new == []
        assert p["titles"].count("First Steps") == 1

    def test_multiple_titles_at_once(self):
        p = _default_persona()
        p["goals_completed"] = 5
        p["total_ticks"] = 1000
        new = check_titles(p)
        assert "First Steps" in new
        assert "Goal Machine" in new
        assert "Centurion" in new
        assert "Marathoner" in new


# --- Persistence ---

class TestPersistence:
    def test_save_load_roundtrip(self):
        with tempfile.TemporaryDirectory() as td:
            ws = Path(td)
            p = _default_persona()
            p["xp"] = 42
            p["level"] = 2
            p["traits"] = ["methodical"]
            save_persona(ws, p)
            loaded = load_persona(ws)
            assert loaded["xp"] == 42
            assert loaded["level"] == 2
            assert loaded["traits"] == ["methodical"]

    def test_load_missing_creates_default(self):
        with tempfile.TemporaryDirectory() as td:
            ws = Path(td)
            p = load_persona(ws)
            assert p["xp"] == 0
            assert p["level"] == 1
            assert p["name"] == "eiDOS"

    def test_load_merges_new_fields(self):
        """If persona.json is from an older version, new fields get defaults."""
        with tempfile.TemporaryDirectory() as td:
            ws = Path(td)
            # Write a minimal persona
            with open(ws / "persona.json", "w") as f:
                json.dump({"name": "eiDOS", "xp": 99}, f)
            p = load_persona(ws)
            assert p["xp"] == 99
            assert p["total_ticks"] == 0  # new field gets default
            assert p["mood"] == "curious"  # new field gets default

    def test_save_atomic(self):
        """Verify temp file is used (no partial writes)."""
        with tempfile.TemporaryDirectory() as td:
            ws = Path(td)
            p = _default_persona()
            save_persona(ws, p)
            # Should not leave .tmp file around
            assert not (ws / "persona.json.tmp").exists()
            assert (ws / "persona.json").exists()

    def test_corrupt_file_returns_default(self):
        with tempfile.TemporaryDirectory() as td:
            ws = Path(td)
            (ws / "persona.json").write_text("NOT JSON!!!")
            p = load_persona(ws)
            assert p["xp"] == 0  # got default


# --- Formatting ---

class TestFormatting:
    def test_format_prefix_basic(self):
        p = _default_persona()
        p["level"] = 3
        p["mood"] = "focused"
        assert format_prefix(p) == "[eidos ✦ Lv.3 focused]"

    def test_format_prefix_default(self):
        p = _default_persona()
        pfx = format_prefix(p)
        assert "Lv.1" in pfx
        assert "curious" in pfx

    def test_format_status_line(self):
        p = _default_persona()
        p["level"] = 5
        p["xp"] = 800
        p["total_ticks"] = 500
        p["goals_completed"] = 3
        p["traits"] = ["methodical", "resilient"]
        line = format_status_line(p)
        assert "Lv.5" in line
        assert "800 XP" in line
        assert "500 ticks" in line
        assert "3 goals" in line
        assert "methodical, resilient" in line

    def test_format_status_line_no_traits(self):
        p = _default_persona()
        line = format_status_line(p)
        assert "developing" in line
