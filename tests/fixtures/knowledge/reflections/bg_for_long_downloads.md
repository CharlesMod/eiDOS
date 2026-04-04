---
id: bg_for_long_downloads
category: reflections
tags: [background, downloads, efficiency, bg_run]
confidence: verified
source_goal: "Set up Python environment"
source_tick: 20
created: "2026-04-01T12:00:00Z"
updated: "2026-04-01T12:00:00Z"
---
Used bash for a large pip install that took over 90 seconds and timed out. Should have used bg_run for any download or install expected to take more than 30 seconds. The bg_check tool can poll for completion on subsequent ticks. This avoids wasting a tick on a timeout failure.
