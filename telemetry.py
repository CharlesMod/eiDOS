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
from atomicio import replace_with_retry


# --- CPU % measurement (normalized to 0–100 regardless of core count) ---
_prev_cpu_times = None

def get_cpu_pct() -> float:
    """Return total system CPU usage as 0–100%, normalized across all cores.

    Windows-native: delta between two GetSystemTimes() reads (idle vs
    kernel+user; kernel time includes idle). First call primes the counter
    and returns 0.0; on non-Windows platforms or error, returns 0.0.
    """
    global _prev_cpu_times
    import sys
    if sys.platform != "win32":
        return 0.0
    try:
        import ctypes

        idle_t = ctypes.c_ulonglong()
        kernel_t = ctypes.c_ulonglong()
        user_t = ctypes.c_ulonglong()
        if not ctypes.windll.kernel32.GetSystemTimes(
                ctypes.byref(idle_t), ctypes.byref(kernel_t), ctypes.byref(user_t)):
            return 0.0
        idle = idle_t.value
        total = kernel_t.value + user_t.value

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
    except (OSError, AttributeError, ValueError):
        return 0.0


def write_activity(config: Config, state: str, *, detail: str = "",
                   partial: str = ""):
    """Write current activity state for dashboard live display.

    States: sleeping, thinking, executing, dreaming, error
    *partial* contains streaming LLM output for live token display.
    """
    path = config.workspace / "activity.json"
    tmp = path.with_suffix(".tmp")
    try:
        tmp.write_text(json.dumps({
            "state": state,
            "since": time.time(),
            "detail": detail,
            "partial": partial[-500:] if partial else "",
        }))
        replace_with_retry(tmp, path)
    except OSError:
        pass  # telemetry is best-effort — never crash the consciousness loop


def write_heartbeat(config: Config, *, tick: int, level: int, mood: str,
                    xp: int, goal_snippet: str, consecutive_failures: int,
                    current_max_tokens: int, disk_free_gb: float,
                    ram_pct: float, llm_elapsed_s: float,
                    tool_name: str, tool_success: bool, uptime_s: float,
                    cpu_pct: float = 0.0, idle_since: float = None,
                    gate_wait_s: float = 0.0, gate_reason: str = ""):
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
        "llm_elapsed_s": round(llm_elapsed_s, 2),
        "tool_name": tool_name,
        "tool_success": tool_success,
        "uptime_s": round(uptime_s, 1),
    }
    if gate_wait_s:
        # GPU speech-gate held this tick (TTS owned the GPU) — visible contention, not dead air
        hb["gate_wait_s"] = round(gate_wait_s, 2)
        hb["gate_reason"] = gate_reason
    if idle_since is not None:
        hb["idle_since"] = idle_since

    path = config.workspace / "heartbeat.json"
    tmp = path.with_suffix(".tmp")
    try:
        tmp.write_text(json.dumps(hb))
        replace_with_retry(tmp, path)
    except OSError:
        pass  # best-effort telemetry


def append_metrics(config: Config, *, tick: int, level: int, mood: str,
                   xp: int, consecutive_failures: int,
                   current_max_tokens: int, disk_free_gb: float,
                   ram_pct: float, llm_elapsed_s: float,
                   tool_name: str, tool_success: bool, uptime_s: float,
                   cpu_pct: float = 0.0,
                   prompt_tokens: int = 0, completion_tokens: int = 0,
                   reasoning_tokens: int = 0, ctx_chars: int = 0,
                   memory_chars: int = 0, obs_count: int = 0,
                   tool_duration_s: float = 0.0, compacted: bool = False,
                   gate_wait_s: float = 0.0, gate_reason: str = ""):
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
        "gate_wait_s": round(gate_wait_s, 2),
        "gate_reason": gate_reason,
    }
    path = config.workspace / "metrics.jsonl"
    try:
        with open(path, "a") as f:
            f.write(json.dumps(line) + "\n")
    except OSError:
        pass  # best-effort telemetry
