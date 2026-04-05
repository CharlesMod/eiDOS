#!/usr/bin/env python3
"""Seed workspace with test data for dashboard preview."""
import json, time
from pathlib import Path

ws = Path("workspace")
ws.mkdir(exist_ok=True)

# Goal
(ws / "goal.md").write_text(
    "Set up a weather monitoring station using the DHT22 sensor on GPIO4. "
    "Log temperature and humidity readings every 5 minutes to a CSV file. "
    "Create a simple web dashboard to view current and historical readings."
)

# Plan with checkboxes
(ws / "plan.md").write_text(
    "# Weather Station Setup\n"
    "- [x] Verify DHT22 sensor wiring on GPIO4\n"
    "- [x] Install adafruit-circuitpython-dht library\n"
    "- [x] Write sensor reading script (read_dht22.py)\n"
    "- [ ] Add CSV logging with rotation\n"
    "- [ ] Set up cron job for 5-minute readings\n"
    "- [ ] Build Flask dashboard for readings\n"
    "  Will need to check if flask is available or use stdlib http.server\n"
    "- [ ] Add historical chart using chart.js\n"
)

# Memory
(ws / "memory.md").write_text(
    "# Working Memory\n"
    "## Progress\n"
    "- DHT22 sensor confirmed working on GPIO4. Reads take ~2s.\n"
    "- adafruit-circuitpython-dht installed in venv at /home/pi/eidos-env\n"
    "- read_dht22.py written and tested -- outputs temp_c, humidity_pct to stdout\n"
    "\n## Next Steps\n"
    "- Need to add CSV writer to read_dht22.py\n"
    "- Consider using pathlib for file paths\n"
    "- Flask may be too heavy -- stdlib http.server might be better for Pi\n"
    "\n## Notes\n"
    "- CRC errors happen occasionally -- retry logic handles it (max 3 retries)\n"
    "- Sensor needs 2s cooldown between reads\n"
)

# Heartbeat
hb = {
    "ts": time.time(),
    "iso": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    "tick": 47,
    "level": 3,
    "mood": "focused",
    "xp": 520,
    "goal_snippet": "Set up a weather monitoring station using the DHT22 sensor",
    "consecutive_failures": 0,
    "current_max_tokens": 1024,
    "disk_free_gb": 24.3,
    "ram_pct": 42.1,
    "cpu_temp_c": 52.3,
    "llm_elapsed_s": 18.7,
    "tool_name": "write_file",
    "tool_success": True,
    "uptime_s": 14400,
}
(ws / "heartbeat.json").write_text(json.dumps(hb))

# Persona
persona = {
    "name": "eiDOS",
    "born": "2026-04-01T08:00:00Z",
    "xp": 520,
    "level": 3,
    "goals_completed": 1,
    "total_ticks": 47,
    "total_errors_recovered": 3,
    "total_compactions": 4,
    "longest_streak": 22,
    "current_streak": 15,
    "tools_used": {
        "bash": 18, "write_file": 12, "read_file": 8,
        "remember": 5, "memorize": 3, "http_get": 1,
    },
    "traits": ["methodical", "creative"],
    "mood": "focused",
    "titles": ["First Goal"],
    "last_goal_summary": "Initial system survey completed",
    "uptime_total_s": 86400,
}
(ws / "persona.json").write_text(json.dumps(persona, indent=2))

# Flavor text
flavor = {
    "text": "Three sensors down, a whole dashboard to go. Kind of like building a house from the roof.",
    "ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    "mood": "focused",
}
(ws / "flavor.json").write_text(json.dumps(flavor))

# Observations
obs = [
    {"ts": "2026-04-04T09:20:00Z", "tick": 38, "tool": "system", "args": {}, "success": True, "output": "Human SSH session detected. Entering standby."},
    {"ts": "2026-04-04T09:25:00Z", "tick": 39, "tool": "system", "args": {}, "success": True, "output": "Resumed by operator. Workspace changes detected: modified config.toml, added read_dht22.py"},
    {"ts": "2026-04-04T09:30:00Z", "tick": 40, "tool": "bash", "args": {"cmd": "pip install adafruit-circuitpython-dht --break-system-packages"}, "success": True, "output": "Successfully installed adafruit-circuitpython-dht-4.0.1", "duration_s": 14.2},
    {"ts": "2026-04-04T09:35:00Z", "tick": 41, "tool": "bash", "args": {"cmd": "gpio readall"}, "success": True, "output": "+-----+-----+---------+------+---+---Pi 4B--+---+------+---------+-----+-----+", "duration_s": 0.8},
    {"ts": "2026-04-04T09:40:00Z", "tick": 42, "tool": "write_file", "args": {"path": "read_dht22.py", "content": "..."}, "success": True, "output": "Written 923 chars to read_dht22.py", "duration_s": 0.01},
    {"ts": "2026-04-04T09:45:00Z", "tick": 43, "tool": "bash", "args": {"cmd": "python3 read_dht22.py"}, "success": False, "output": "Traceback: RuntimeError: DHT sensor not found. Check wiring.", "duration_s": 2.1},
    {"ts": "2026-04-04T09:50:00Z", "tick": 43, "tool": "remember", "args": {"note": "DHT22 needs pull-up resistor on data line. GPIO4 internal pull-up may not be strong enough."}, "success": True, "output": "Noted in memory: DHT22 needs pull-up resistor on data line.", "duration_s": 0.0},
    {"ts": "2026-04-04T10:00:00Z", "tick": 44, "tool": "bash", "args": {"cmd": "python3 read_dht22.py"}, "success": True, "output": "Temperature: 23.4C, Humidity: 61.2%", "duration_s": 2.3},
    {"ts": "2026-04-04T10:05:00Z", "tick": 45, "tool": "memorize", "args": {"fact": "DHT22 CRC errors happen when wire exceeds 3m -- keep jumper wires short", "tags": ["dht22", "gpio", "hardware"]}, "success": True, "output": "Stored to long-term memory: dht22_crc_short_wires_20260404", "duration_s": 0.1},
    {"ts": "2026-04-04T10:10:00Z", "tick": 46, "tool": "read_file", "args": {"path": "read_dht22.py"}, "success": True, "output": "import adafruit_dht\\nimport board\\n...", "duration_s": 0.01},
    {"ts": "2026-04-04T10:12:00Z", "tick": 46, "tool": "dream", "args": {}, "success": True, "output": "Dream cycle complete. Plan: 450 -> 380 chars. Knowledge: 2 entries extracted.", "duration_s": 12.5},
    {"ts": "2026-04-04T10:15:00Z", "tick": 47, "tool": "write_file", "args": {"path": "read_dht22.py", "content": "..."}, "success": True, "output": "Written 1847 chars to read_dht22.py", "duration_s": 0.02},
]
with open(ws / "observations.jsonl", "w") as f:
    for o in obs:
        f.write(json.dumps(o) + "\n")

# Knowledge index
kn_dir = ws / "knowledge"
kn_dir.mkdir(exist_ok=True)
for cat in ["facts", "errors", "procedures", "reflections"]:
    (kn_dir / cat).mkdir(exist_ok=True)

index = [
    {"id": "dht22_crc_short_wires", "category": "errors", "tags": ["dht22", "gpio", "hardware"], "confidence": "verified", "source_goal": "Weather station", "source_tick": 45, "created": "2026-04-04T10:05:00Z", "updated": "2026-04-04T10:05:00Z", "content_preview": "DHT22 CRC errors happen when wire exceeds 3m. Keep jumper wires short for reliable readings."},
    {"id": "pip_bookworm_flag", "category": "facts", "tags": ["pip", "python", "bookworm"], "confidence": "verified", "source_goal": "Setup env", "source_tick": 12, "created": "2026-04-01T11:00:00Z", "updated": "2026-04-01T11:00:00Z", "content_preview": "pip on Bookworm requires --break-system-packages flag or use a venv."},
    {"id": "gpio4_dht22_wiring", "category": "facts", "tags": ["gpio", "dht22", "sensor"], "confidence": "verified", "source_goal": "Weather station", "source_tick": 34, "created": "2026-04-02T08:30:00Z", "updated": "2026-04-02T08:30:00Z", "content_preview": "DHT22 on GPIO4. Needs adafruit-circuitpython-dht. 2s between reads. Occasional CRC errors normal."},
    {"id": "tailscale_network", "category": "facts", "tags": ["network", "tailscale", "vpn"], "confidence": "verified", "source_goal": "Initial survey", "source_tick": 8, "created": "2026-04-01T10:30:00Z", "updated": "2026-04-01T10:30:00Z", "content_preview": "Network via Tailscale VPN. Pi at 100.74.178.26. Pull-only architecture."},
    {"id": "systemd_user_services", "category": "procedures", "tags": ["systemd", "services"], "confidence": "tentative", "source_goal": "Weather station", "source_tick": 30, "created": "2026-04-02T07:00:00Z", "updated": "2026-04-02T07:00:00Z", "content_preview": "Use systemctl --user for non-root services. Requires loginctl enable-linger."},
    {"id": "check_os_before_install", "category": "reflections", "tags": ["debugging", "workflow"], "confidence": "verified", "source_goal": "Setup env", "source_tick": 15, "created": "2026-04-01T12:00:00Z", "updated": "2026-04-01T12:00:00Z", "content_preview": "Always check OS version before installing packages. Bookworm vs Bullseye differs."},
]
(kn_dir / "index.json").write_text(json.dumps(index, indent=2))

# Dream snapshots
snap_dir = ws / "snapshots"
snap_dir.mkdir(exist_ok=True)
for ts, content in [
    ("20260401_120000", "# Working Memory\n## Initial Survey\n- Pi 4B 4GB confirmed\n- Bookworm OS\n- Tailscale connected\n- 24GB free disk\n\n## Next: set up Python venv"),
    ("20260402_080000", "# Working Memory\n## Environment Ready\n- Python 3.11 in venv\n- pip working with --break-system-packages\n- GPIO libraries installed\n\n## Next: test DHT22 sensor"),
    ("20260403_100000", "# Working Memory\n## Sensor Working\n- DHT22 reads OK on GPIO4\n- CRC errors < 5% with retries\n- read_dht22.py v1 complete\n\n## Next: add CSV logging"),
    ("20260404_101200", "# Working Memory\n## Progress\n- DHT22 sensor confirmed on GPIO4\n- Script written and tested\n- adafruit lib installed\n\n## Next: CSV logging + cron"),
]:
    (snap_dir / f"memory_snapshot_{ts}.md").write_text(content)

# WAL
wal = {
    "tick_number": 48, "ticks_since_compaction": 3,
    "goal_start_time": time.time() - 14400,
    "consecutive_failures": 0, "reasoning_exhaustions": 0,
    "current_max_tokens": 1024, "ts": time.time(),
}
(ws / "wal.json").write_text(json.dumps(wal))

print("Test data seeded!")
