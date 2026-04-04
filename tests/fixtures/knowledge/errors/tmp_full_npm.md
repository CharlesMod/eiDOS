---
id: tmp_full_npm
category: errors
tags: [npm, node, tmp, disk, installation]
confidence: tentative
source_goal: "Set up web dashboard"
source_tick: 52
created: "2026-04-02T18:00:00Z"
updated: "2026-04-02T18:00:00Z"
---
npm install fails silently when /tmp is full. The error output is unhelpful — check df -h /tmp first when npm operations fail unexpectedly. Fix: clear /tmp or set npm cache to a different location with npm config set cache /home/pi/.npm-cache.
