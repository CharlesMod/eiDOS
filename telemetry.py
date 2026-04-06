"""Telemetry: heartbeat.json writer and metrics.jsonl logger.

heartbeat.json — atomic overwrite every tick. Single point-in-time snapshot.
metrics.jsonl — append-only time series. One line per tick.
activity.json — current agent state (thinking, executing, sleeping, dreaming).
Both are read-only by the dashboard; eiDOS is the sole writer.
"""

import json
import os
import time
from pathlib import Path

from config import Config


# --- CPU % measurement (normalized to 0–100 regardless of core count) ---
_prev_cpu_times = None

def get_cpu_pct() -> float:
    """Return total system CPU usage as 0–100%, normalized across all cores.

    Uses delta between two reads of /proc/stat.
    Falls back to 0.0 on non-Linux or on error.
    """
    global _prev_cpu_times
    try:
        with open("/proc/stat") as f:
            parts = f.readline().split()
        # parts: cpu user nice system idle iowait irq softirq steal ...
        vals = [int(x) for x in parts[1:]]
        idle = vals[3] + vals[4]   # idle + iowait
        total = sum(vals)

        if _prev_cpu_times is None:
            _prev_cpu_times = (idle, total)
            return 0.0

        prev_idle, prev_total = _prev_cpu_times
        _prev_cpu_times = (idle, total)

        d_total = total - prev_total
        d_idle = idle - prev_idle
        if d_total <= 0:
            return 0.0
        return round((1.0 - d_idle / d_total) * 100.0, 1)
    except (OSError, IndexError, ValueError):
        return 0.0


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
                    cpu_pct: float = 0.0, idle_since: float = None):
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
        "cpu_pct": round(cpu_pct, 1),
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
                   cpu_pct: float = 0.0,
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
        "cpu_pct": round(cpu_pct, 1),
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
