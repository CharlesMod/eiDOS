"""Safety gates: command blocking, disk/RAM checks."""

import re
import shutil
import subprocess
import sys
from pathlib import Path
from typing import List, Optional, Tuple


def is_command_blocked(cmd: str, protected_patterns: List[str]) -> Optional[str]:
    """Check if a command matches any protected pattern.

    Returns the matched pattern string if blocked, None if allowed.
    """
    for pattern in protected_patterns:
        if re.search(pattern, cmd, re.IGNORECASE):
            return pattern
    return None


def check_disk_space(path: str = "/", min_gb: float = 1.0) -> Tuple[bool, float]:
    """Check if disk has sufficient free space.

    Returns (ok, free_gb).
    """
    usage = shutil.disk_usage(path)
    free_gb = usage.free / (1024 ** 3)
    return free_gb >= min_gb, free_gb


def check_ram(max_pct: float = 85.0) -> Tuple[bool, float]:
    """Check if RAM usage is below threshold.

    Returns (ok, used_pct). Works on Linux and macOS.
    """
    if sys.platform == "linux":
        return _check_ram_linux(max_pct)
    elif sys.platform == "darwin":
        return _check_ram_darwin(max_pct)
    # Unknown platform — assume OK
    return True, 0.0


def _check_ram_linux(max_pct: float) -> Tuple[bool, float]:
    try:
        with open("/proc/meminfo") as f:
            lines = f.readlines()
        info = {}
        for line in lines:
            parts = line.split(":")
            if len(parts) == 2:
                key = parts[0].strip()
                val = parts[1].strip().split()[0]
                info[key] = int(val)
        total = info.get("MemTotal", 1)
        available = info.get("MemAvailable", total)
        used_pct = ((total - available) / total) * 100
        return used_pct <= max_pct, used_pct
    except (OSError, KeyError, ValueError, ZeroDivisionError):
        return True, 0.0


def _check_ram_darwin(max_pct: float) -> Tuple[bool, float]:
    try:
        result = subprocess.run(
            ["sysctl", "-n", "hw.memsize"],
            capture_output=True, text=True, timeout=5,
        )
        total_bytes = int(result.stdout.strip())

        result = subprocess.run(
            ["vm_stat"],
            capture_output=True, text=True, timeout=5,
        )
        lines = result.stdout.strip().split("\n")
        page_size = 16384  # default
        for line in lines:
            if "page size" in line.lower():
                nums = re.findall(r'\d+', line)
                if nums:
                    page_size = int(nums[-1])
                break

        stats = {}
        for line in lines[1:]:
            parts = line.split(":")
            if len(parts) == 2:
                key = parts[0].strip()
                val = parts[1].strip().rstrip(".")
                try:
                    stats[key] = int(val)
                except ValueError:
                    pass

        free_pages = stats.get("Pages free", 0)
        inactive_pages = stats.get("Pages inactive", 0)
        speculative_pages = stats.get("Pages speculative", 0)
        available_bytes = (free_pages + inactive_pages + speculative_pages) * page_size
        used_pct = ((total_bytes - available_bytes) / total_bytes) * 100
        return used_pct <= max_pct, used_pct
    except (OSError, ValueError, ZeroDivisionError):
        return True, 0.0


def get_cpu_temp() -> Optional[float]:
    """Read CPU temperature in °C. Returns None on unsupported platforms."""
    if sys.platform == "linux":
        try:
            with open("/sys/class/thermal/thermal_zone0/temp") as f:
                return int(f.read().strip()) / 1000.0
        except (OSError, ValueError):
            return None
    return None


def kill_child_processes(parent_pid: int = None) -> int:
    """Kill child processes of the given PID (or current process).

    Returns count of processes killed.
    """
    if parent_pid is None:
        import os
        parent_pid = os.getpid()

    killed = 0
    try:
        # Use pkill to kill children of our process
        result = subprocess.run(
            ["pkill", "-P", str(parent_pid)],
            capture_output=True, timeout=5,
        )
        if result.returncode == 0:
            killed = 1  # pkill doesn't report count, just success
    except (OSError, subprocess.TimeoutExpired):
        pass
    return killed
