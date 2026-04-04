---
id: check_os_before_install
category: reflections
tags: [planning, os, installation, efficiency]
confidence: verified
source_goal: "Set up Python environment"
source_tick: 14
created: "2026-04-01T11:15:00Z"
updated: "2026-04-01T11:15:00Z"
---
Wasted 3 ticks trying to install packages before checking the OS version. The pip --break-system-packages issue would have been caught immediately by checking /etc/os-release. Lesson: always check OS version and distribution before attempting package installation.
