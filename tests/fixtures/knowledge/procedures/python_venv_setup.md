---
id: python_venv_setup
category: procedures
tags: [python, virtualenv, venv, packages]
confidence: verified
source_goal: "Set up Python environment"
source_tick: 15
created: "2026-04-01T11:30:00Z"
updated: "2026-04-01T11:30:00Z"
---
To set up a Python virtual environment on the Pi: python3 -m venv ~/myenv && source ~/myenv/bin/activate. Then pip install works without --break-system-packages. Activate before running scripts: source ~/myenv/bin/activate && python3 script.py. The venv should be created once and reused across ticks.
