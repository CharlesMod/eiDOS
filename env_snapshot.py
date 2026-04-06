"""Generate a fresh environment snapshot each tick."""

import subprocess
import sys
import shutil
import time

from config import Config
from tools import refresh_jobs


def generate(config: Config) -> str:
    """Return a formatted environment snapshot string."""
    sections = []
    ts = time.strftime("%Y-%m-%d %H:%M:%S UTC", time.gmtime())
    sections.append(f"Time: {ts}")

    # Uptime
    try:
        result = subprocess.run(["uptime"], capture_output=True, text=True, timeout=5)
        uptime_str = result.stdout.strip()
        sections.append(f"Uptime: {uptime_str}")
    except (OSError, subprocess.TimeoutExpired):
        sections.append("Uptime: unavailable")

    # Disk
    try:
        usage = shutil.disk_usage(str(config.workspace))
        free_gb = usage.free / (1024 ** 3)
        total_gb = usage.total / (1024 ** 3)
        sections.append(f"Disk: {free_gb:.1f} GB free / {total_gb:.1f} GB total")
    except OSError:
        sections.append("Disk: unavailable")

    # RAM
    if sys.platform == "linux":
        try:
            with open("/proc/meminfo") as f:
                lines = f.readlines()
            info = {}
            for line in lines:
                parts = line.split(":")
                if len(parts) == 2:
                    info[parts[0].strip()] = int(parts[1].strip().split()[0])
            total_mb = info.get("MemTotal", 0) / 1024
            avail_mb = info.get("MemAvailable", 0) / 1024
            used_mb = total_mb - avail_mb
            pct = (used_mb / total_mb * 100) if total_mb > 0 else 0
            sections.append(f"RAM: {used_mb:.0f} MB used / {total_mb:.0f} MB total ({pct:.0f}%)")
        except (OSError, KeyError, ValueError):
            sections.append("RAM: unavailable")
    elif sys.platform == "darwin":
        try:
            result = subprocess.run(
                ["sysctl", "-n", "hw.memsize"],
                capture_output=True, text=True, timeout=5,
            )
            total_bytes = int(result.stdout.strip())
            total_mb = total_bytes / (1024 ** 2)
            # vm_stat is imprecise but good enough for a snapshot
            sections.append(f"RAM: {total_mb:.0f} MB total (detailed stats via vm_stat)")
        except (OSError, ValueError, subprocess.TimeoutExpired):
            sections.append("RAM: unavailable")
    else:
        sections.append("RAM: unavailable (unknown platform)")

    # Background jobs
    jobs = refresh_jobs(config)
    running = [j for j in jobs if j["status"] == "running"]
    if running:
        job_lines = [f"  - {j['name']} (PID {j['pid']})" for j in running]
        sections.append(f"Background jobs ({len(running)} running):\n" + "\n".join(job_lines))
    else:
        sections.append("Background jobs: none")

    return "=== Environment ===\n" + "\n".join(sections)


def generate_alerts(config: Config) -> str:
    """Return environment alerts only — empty string when everything is normal.

    Surfaces disk, RAM, and thermal anomalies.  Used by the briefing-model
    context to avoid wasting chars on normal readings every tick.
    """
    alerts = []

    # Disk
    try:
        usage = shutil.disk_usage(str(config.workspace))
        free_gb = usage.free / (1024 ** 3)
        if free_gb < config.disk_min_gb:
            total_gb = usage.total / (1024 ** 3)
            alerts.append(f"⚠ DISK LOW: {free_gb:.1f} GB free / {total_gb:.1f} GB (threshold {config.disk_min_gb} GB)")
    except OSError:
        pass

    # RAM (Linux only — production target)
    if sys.platform == "linux":
        try:
            with open("/proc/meminfo") as f:
                lines = f.readlines()
            info = {}
            for line in lines:
                parts = line.split(":")
                if len(parts) == 2:
                    info[parts[0].strip()] = int(parts[1].strip().split()[0])
            total_mb = info.get("MemTotal", 0) / 1024
            avail_mb = info.get("MemAvailable", 0) / 1024
            if total_mb > 0:
                used_pct = (total_mb - avail_mb) / total_mb * 100
                if used_pct > config.ram_max_pct:
                    alerts.append(f"⚠ RAM HIGH: {used_pct:.0f}% used ({avail_mb:.0f} MB free)")
        except (OSError, KeyError, ValueError):
            pass

    # Thermal (Linux only)
    if sys.platform == "linux":
        try:
            temp_path = "/sys/class/thermal/thermal_zone0/temp"
            with open(temp_path) as f:
                temp_c = int(f.read().strip()) / 1000.0
        except (OSError, ValueError):
            pass

    # Background jobs — include only if any are running (brief)
    jobs = refresh_jobs(config)
    running = [j for j in jobs if j["status"] == "running"]
    if running:
        names = ", ".join(j["name"] for j in running)
        alerts.append(f"BG jobs running: {names}")

    if not alerts:
        return ""
    return "⚠ Alerts: " + " | ".join(alerts)
