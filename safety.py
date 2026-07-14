"""Safety gates: command blocking, disk/RAM checks (Windows-native host)."""

import ctypes
import re
import shutil
import sys
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


class _MEMORYSTATUSEX(ctypes.Structure):
    _fields_ = [
        ("dwLength", ctypes.c_ulong),
        ("dwMemoryLoad", ctypes.c_ulong),
        ("ullTotalPhys", ctypes.c_ulonglong),
        ("ullAvailPhys", ctypes.c_ulonglong),
        ("ullTotalPageFile", ctypes.c_ulonglong),
        ("ullAvailPageFile", ctypes.c_ulonglong),
        ("ullTotalVirtual", ctypes.c_ulonglong),
        ("ullAvailVirtual", ctypes.c_ulonglong),
        ("ullAvailExtendedVirtual", ctypes.c_ulonglong),
    ]


def check_ram(max_pct: float = 85.0) -> Tuple[bool, float]:
    """Check if RAM usage is below threshold. Returns (ok, used_pct).

    Windows uses GlobalMemoryStatusEx; Linux reads /proc/meminfo (MemAvailable vs MemTotal — the
    kernel's own 'how much can actually be allocated' figure, so cache doesn't read as pressure).
    On any error / unknown platform, fails open with (True, 0.0)."""
    if sys.platform == "win32":
        try:
            stat = _MEMORYSTATUSEX()
            stat.dwLength = ctypes.sizeof(_MEMORYSTATUSEX)
            if not ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(stat)):
                return True, 0.0
            used_pct = float(stat.dwMemoryLoad)
            return used_pct <= max_pct, used_pct
        except (OSError, AttributeError, ValueError):
            return True, 0.0
    try:
        info = {}
        with open("/proc/meminfo", encoding="ascii", errors="ignore") as f:
            for line in f:
                k, _, rest = line.partition(":")
                if rest:
                    info[k.strip()] = float(rest.strip().split()[0])   # values are in kB
        total = info.get("MemTotal", 0.0)
        avail = info.get("MemAvailable", info.get("MemFree", 0.0))
        if total > 0:
            used_pct = 100.0 * (total - avail) / total
            return used_pct <= max_pct, used_pct
    except (OSError, ValueError, IndexError):
        pass
    return True, 0.0
