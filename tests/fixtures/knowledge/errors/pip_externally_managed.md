---
id: pip_externally_managed
category: errors
tags: [pip, python, bookworm, installation]
confidence: verified
source_goal: "Set up Python environment"
source_tick: 10
created: "2026-04-01T10:45:00Z"
updated: "2026-04-01T10:45:00Z"
---
pip install fails with "externally-managed-environment" error on Raspberry Pi OS Bookworm. This is a PEP 668 restriction. Fix: use --break-system-packages flag or create a virtual environment. The error message is misleading — it's not a permissions issue but an OS policy.
