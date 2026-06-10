"""Persona system — emergent personality, XP, traits, mood, titles.

Identity state persists in workspace/persona.json, separate from volatile
working memory. Inspired by persistent file-based identity patterns.
"""

import json
import math
import time
from pathlib import Path
from typing import Dict, List, Optional


def _default_persona() -> dict:
    """Return a fresh persona dict."""
    return {
        "name": "eiDOS",
        "born": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "xp": 0,
        "level": 1,
        "goals_completed": 0,
        "total_ticks": 0,
        "total_errors_recovered": 0,
        "total_compactions": 0,
        "longest_streak": 0,
        "current_streak": 0,
        "tools_used": {},
        "traits": [],
        "mood": "curious",
        "titles": [],
        "last_goal_summary": "",
        "uptime_total_s": 0,
    }


def load_persona(workspace: Path) -> dict:
    """Load persona from workspace/persona.json, or create default."""
    path = workspace / "persona.json"
    if path.exists():
        try:
            with open(path) as f:
                data = json.load(f)
            # Merge with defaults so new fields are added on upgrade
            default = _default_persona()
            default.update(data)
            return default
        except (json.JSONDecodeError, OSError):
            pass
    return _default_persona()


def save_persona(workspace: Path, persona: dict) -> None:
    """Atomically save persona to workspace/persona.json."""
    path = workspace / "persona.json"
    tmp = path.with_suffix(".tmp")
    with open(tmp, "w") as f:
        json.dump(persona, f, indent=2)
    tmp.replace(path)


def compute_level(xp: int) -> int:
    """Level = 1 + floor(sqrt(xp / 50)). Fast early, slower later."""
    if xp <= 0:
        return 1
    return 1 + int(math.floor(math.sqrt(xp / 50)))


def award_xp(persona: dict, amount: int, reason: str = "") -> int:
    """Add XP and recompute level. Returns new level."""
    persona["xp"] = persona.get("xp", 0) + amount
    persona["level"] = compute_level(persona["xp"])
    return persona["level"]


def record_tick(persona: dict, tool_name: Optional[str], success: bool) -> None:
    """Update persona stats after a tick."""
    persona["total_ticks"] = persona.get("total_ticks", 0) + 1

    if tool_name:
        tools = persona.get("tools_used", {})
        tools[tool_name] = tools.get(tool_name, 0) + 1
        persona["tools_used"] = tools

        if success:
            award_xp(persona, 1)
            persona["current_streak"] = persona.get("current_streak", 0) + 1
            if persona["current_streak"] > persona.get("longest_streak", 0):
                persona["longest_streak"] = persona["current_streak"]
        else:
            persona["current_streak"] = 0


def record_error_recovery(persona: dict) -> None:
    """Call when a failed tick is followed by a successful one."""
    persona["total_errors_recovered"] = persona.get("total_errors_recovered", 0) + 1
    award_xp(persona, 5)


def record_compaction(persona: dict) -> None:
    """Call after a successful compaction."""
    persona["total_compactions"] = persona.get("total_compactions", 0) + 1
    award_xp(persona, 2)


def record_goal_complete(persona: dict, summary: str) -> None:
    """Call when goal_complete tool succeeds."""
    persona["goals_completed"] = persona.get("goals_completed", 0) + 1
    persona["last_goal_summary"] = summary
    award_xp(persona, 100)


# --- Trait computation ---

def compute_traits(persona: dict) -> List[str]:
    """Derive traits from cumulative stats. Returns top 3."""
    tools = persona.get("tools_used", {})
    total_calls = sum(tools.values()) or 1

    candidates: List[tuple] = []  # (trait_name, strength)

    # methodical: >60% of calls are bash or read_file
    methodical_pct = (tools.get("bash", 0) + tools.get("read_file", 0)) / total_calls
    if methodical_pct > 0.6:
        candidates.append(("methodical", methodical_pct))

    # creative: >30% of calls are write_file
    creative_pct = tools.get("write_file", 0) / total_calls
    if creative_pct > 0.3:
        candidates.append(("creative", creative_pct))

    # resilient: error recovery count > 20
    errors_recovered = persona.get("total_errors_recovered", 0)
    if errors_recovered > 20:
        candidates.append(("resilient", errors_recovered / 100))

    # persistent: longest streak > 100
    streak = persona.get("longest_streak", 0)
    if streak > 100:
        candidates.append(("persistent", streak / 500))

    # curious: http_request used > 50
    if tools.get("http_request", 0) > 50:
        candidates.append(("curious", tools["http_request"] / 200))

    # introspective: remember used > 100
    if tools.get("remember", 0) > 100:
        candidates.append(("introspective", tools["remember"] / 500))

    # veteran: level >= 5
    level = persona.get("level", 1)
    if level >= 5:
        candidates.append(("veteran", level / 10))

    # architect: write_file > 200
    if tools.get("write_file", 0) > 200:
        candidates.append(("architect", tools["write_file"] / 500))

    # Sort by strength descending, take top 3
    candidates.sort(key=lambda x: x[1], reverse=True)
    traits = [c[0] for c in candidates[:3]]
    persona["traits"] = traits
    return traits


# --- Mood ---

def compute_mood(persona: dict, recent_successes: Optional[List[bool]] = None,
                 ticks_since_goal: Optional[int] = None) -> str:
    """Derive mood from recent success rate and events.

    Args:
        recent_successes: list of bools from last 10 observations (True=success).
        ticks_since_goal: how many ticks since last goal_complete (None = never).
    """
    # Post-goal glow
    if ticks_since_goal is not None and ticks_since_goal <= 5:
        persona["mood"] = "triumphant"
        return "triumphant"

    # Fresh start
    if not recent_successes:
        persona["mood"] = "curious"
        return "curious"

    success_rate = sum(recent_successes) / len(recent_successes)
    if success_rate >= 0.8:
        mood = "focused"
    elif success_rate >= 0.5:
        mood = "determined"
    elif success_rate >= 0.3:
        mood = "frustrated"
    else:
        mood = "struggling"

    persona["mood"] = mood
    return mood


# --- Titles ---

_TITLE_RULES = [
    ("First Steps", lambda p: p.get("goals_completed", 0) >= 1),
    ("Centurion", lambda p: p.get("total_ticks", 0) >= 100),
    ("Marathoner", lambda p: p.get("total_ticks", 0) >= 1000),
    ("Dream Weaver", lambda p: p.get("total_compactions", 0) >= 10),
    ("Unkillable", lambda p: p.get("total_errors_recovered", 0) >= 50),
    ("Goal Machine", lambda p: p.get("goals_completed", 0) >= 5),
    ("Shell Wizard", lambda p: p.get("tools_used", {}).get("bash", 0) >= 500),
    ("Memory Palace", lambda p: p.get("tools_used", {}).get("remember", 0) >= 50),
]


def check_titles(persona: dict) -> List[str]:
    """Check and award any newly earned titles. Returns list of new titles."""
    existing = set(persona.get("titles", []))
    new_titles = []
    for title, condition in _TITLE_RULES:
        if title not in existing and condition(persona):
            new_titles.append(title)
    persona["titles"] = list(existing | set(new_titles))
    return new_titles


# --- Output formatting ---

def format_prefix(persona: dict) -> str:
    """Return the mood-aware prefix: [eidos ✦ Lv.3 focused]"""
    level = persona.get("level", 1)
    mood = persona.get("mood", "neutral")
    return f"[eidos ✦ Lv.{level} {mood}]"


def format_status_line(persona: dict) -> str:
    """One-line summary for startup display."""
    level = persona.get("level", 1)
    xp = persona.get("xp", 0)
    ticks = persona.get("total_ticks", 0)
    goals = persona.get("goals_completed", 0)
    traits = persona.get("traits", [])
    trait_str = ", ".join(traits) if traits else "developing"
    return f"Lv.{level} ({xp} XP) | {ticks} ticks | {goals} goals | traits: {trait_str}"
