---
id: pip_bookworm_flag
category: facts
tags: [pip, python, bookworm, raspberry-pi]
confidence: verified
source_goal: "Set up Python environment"
source_tick: 12
created: "2026-04-01T11:00:00Z"
updated: "2026-04-01T11:00:00Z"
---
On Raspberry Pi OS Bookworm (Debian 12), pip install requires the --break-system-packages flag or use of a virtual environment. Without this flag pip refuses to install packages globally.
