---
id: cron_setup
category: procedures
tags: [cron, scheduling, automation]
confidence: verified
source_goal: "Set up data collection"
source_tick: 45
created: "2026-04-02T16:00:00Z"
updated: "2026-04-02T16:00:00Z"
---
To schedule recurring tasks: use crontab -e (never edit /var/spool/cron directly). Always use absolute paths in crontab entries. Redirect output to a log file to capture errors. Test the command manually first. Example: */5 * * * * /usr/bin/python3 /home/pi/collect.py >> /home/pi/collect.log 2>&1
