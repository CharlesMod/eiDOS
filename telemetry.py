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

    Substrate-agnostic (interoception reads this every tick, on whatever host the creature inhabits):
    - Windows: delta between two GetSystemTimes() reads (idle vs kernel+user; kernel includes idle).
    - Linux / Raspberry Pi: delta between two /proc/stat 'cpu' aggregate reads (busy = total − idle−iowait).
    - Other POSIX (macOS): a coarse load-average proxy (loadavg / core-count) — better than a blind 0.0.
    First call primes the counter and returns 0.0; any error returns 0.0.
    """
    global _prev_cpu_times
    import sys

    def _delta_pct(idle: int, total: int) -> float:
        """Shared idle/total delta → busy% (both the win32 and /proc/stat paths feed (idle, total))."""
        global _prev_cpu_times
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

    if sys.platform == "win32":
        try:
            import ctypes
            idle_t = ctypes.c_ulonglong()
            kernel_t = ctypes.c_ulonglong()
            user_t = ctypes.c_ulonglong()
            if not ctypes.windll.kernel32.GetSystemTimes(
                    ctypes.byref(idle_t), ctypes.byref(kernel_t), ctypes.byref(user_t)):
                return 0.0
            # Windows kernel time INCLUDES idle, so total = kernel + user.
            return _delta_pct(idle_t.value, kernel_t.value + user_t.value)
        except (OSError, AttributeError, ValueError):
            return 0.0

    # Linux / Pi: /proc/stat first line is the aggregate 'cpu' counters (jiffies), already summed
    # across every core, so busy% is normalized by construction.
    try:
        with open("/proc/stat", "r") as f:
            fields = f.readline().split()
        if fields and fields[0] == "cpu":
            vals = [int(x) for x in fields[1:]]
            idle = vals[3] + (vals[4] if len(vals) > 4 else 0)   # idle + iowait
            return _delta_pct(idle, sum(vals))
    except (OSError, ValueError, IndexError):
        pass

    # Other POSIX (no /proc/stat, e.g. macOS): coarse run-queue proxy, clamped to 0–100.
    try:
        n = os.cpu_count() or 1
        return round(min(100.0, (os.getloadavg()[0] / n) * 100.0), 1)
    except (OSError, AttributeError, ValueError):
        return 0.0


def record_goal_horizon(config: Config, horizon: int, cause: str, tick: int) -> None:
    """SOTA#9 autonomy KPI — the coherent-goal-pursuit HORIZON: how many consecutive on-track acting
    ticks the creature sustains before it DERAILS (a forced rotation/park/death, a whole-backlog
    escalation, or a detected loop). This turns the pillar ("persist toward a goal without derailing")
    into something MEASURABLE. Keeps a BOUNDED rolling summary in one overwritten file (mean/max/last +
    the newest 50 samples + a per-cause tally) — no unbounded growth. Best-effort; never raises."""
    try:
        state = config.state_dir
        state.mkdir(parents=True, exist_ok=True)
        p = state / "goal_horizon_stats.json"
        try:
            s = json.loads(p.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            s = {"samples": 0, "sum": 0, "max": 0, "recent": [], "by_cause": {}}
        s["samples"] = int(s.get("samples", 0)) + 1
        s["sum"] = int(s.get("sum", 0)) + int(horizon)
        s["max"] = max(int(s.get("max", 0)), int(horizon))
        s["mean"] = round(s["sum"] / max(1, s["samples"]), 2)
        s["last"] = {"horizon": int(horizon), "cause": str(cause), "tick": int(tick)}
        s["recent"] = (list(s.get("recent", []))[-49:] + [int(horizon)])
        bc = dict(s.get("by_cause", {}))
        bc[str(cause)] = int(bc.get(str(cause), 0)) + 1
        s["by_cause"] = bc
        tmp = p.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(s), encoding="utf-8")
        replace_with_retry(str(tmp), str(p))
    except OSError:
        pass


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
