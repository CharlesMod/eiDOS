"""Telemetry: heartbeat.json writer and metrics.jsonl logger.

heartbeat.json — atomic overwrite every tick. Single point-in-time snapshot.
metrics.jsonl — append-only time series. One line per tick.
activity.json — current agent state (thinking, executing, sleeping, dreaming).
Both are read-only by the dashboard; eiDOS is the sole writer.
"""

import json
import time
from pathlib import Path

from config import Config


def write_activity(config: Config, state: str, *, detail: str = "",
                   partial: str = ""):
    """Write current activity state for dashboard live display.

    States: sleeping, thinking, executing, dreaming, error
    *partial* contains streaming LLM output for live token display.
    """
    path = config.workspace / "activity.json"
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps({
        "state": state,
        "since": time.time(),
        "detail": detail,
        "partial": partial[-500:] if partial else "",
    }))
    tmp.rename(path)


def write_heartbeat(config: Config, *, tick: int, level: int, mood: str,
                    xp: int, goal_snippet: str, consecutive_failures: int,
                    current_max_tokens: int, disk_free_gb: float,
                    ram_pct: float, cpu_temp_c, llm_elapsed_s: float,
                    tool_name: str, tool_success: bool, uptime_s: float,
                    idle_since: float = None):
    """Atomically write the current heartbeat snapshot."""
    hb = {
        "ts": time.time(),
        "iso": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "tick": tick,
        "level": level,
        "mood": mood,
        "xp": xp,
        "goal_snippet": goal_snippet[:80] if goal_snippet else "",
        "consecutive_failures": consecutive_failures,
        "current_max_tokens": current_max_tokens,
        "disk_free_gb": round(disk_free_gb, 2),
        "ram_pct": round(ram_pct, 1),
        "cpu_temp_c": round(cpu_temp_c, 1) if cpu_temp_c is not None else None,
        "llm_elapsed_s": round(llm_elapsed_s, 2),
        "tool_name": tool_name,
        "tool_success": tool_success,
        "uptime_s": round(uptime_s, 1),
    }
    if idle_since is not None:
        hb["idle_since"] = idle_since

    path = config.workspace / "heartbeat.json"
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(hb))
    tmp.rename(path)


def append_metrics(config: Config, *, tick: int, level: int, mood: str,
                   xp: int, consecutive_failures: int,
                   current_max_tokens: int, disk_free_gb: float,
                   ram_pct: float, cpu_temp_c, llm_elapsed_s: float,
                   tool_name: str, tool_success: bool, uptime_s: float,
                   prompt_tokens: int = 0, completion_tokens: int = 0,
                   reasoning_tokens: int = 0, ctx_chars: int = 0,
                   memory_chars: int = 0, obs_count: int = 0,
                   tool_duration_s: float = 0.0, compacted: bool = False):
    """Append one metrics line to metrics.jsonl."""
    line = {
        "ts": time.time(),
        "tick": tick,
        "level": level,
        "mood": mood,
        "xp": xp,
        "consecutive_failures": consecutive_failures,
        "current_max_tokens": current_max_tokens,
        "disk_free_gb": round(disk_free_gb, 2),
        "ram_pct": round(ram_pct, 1),
        "cpu_temp_c": round(cpu_temp_c, 1) if cpu_temp_c is not None else None,
        "llm_elapsed_s": round(llm_elapsed_s, 2),
        "tool_name": tool_name,
        "tool_success": tool_success,
        "uptime_s": round(uptime_s, 1),
        "prompt_tokens": prompt_tokens,
        "completion_tokens": completion_tokens,
        "reasoning_tokens": reasoning_tokens,
        "ctx_chars": ctx_chars,
        "memory_chars": memory_chars,
        "obs_count": obs_count,
        "tool_duration_s": round(tool_duration_s, 3),
        "compacted": compacted,
    }
    path = config.workspace / "metrics.jsonl"
    with open(path, "a") as f:
        f.write(json.dumps(line) + "\n")
