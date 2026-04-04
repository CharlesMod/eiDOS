---
id: dht22_crc_errors
category: errors
tags: [dht22, sensor, gpio, crc]
confidence: verified
source_goal: "Deploy weather station"
source_tick: 38
created: "2026-04-02T09:00:00Z"
updated: "2026-04-02T09:00:00Z"
---
DHT22 sensor occasionally returns CRC checksum errors. These are transient hardware timing issues, not software bugs. Retry up to 3 times with a 2-second delay between attempts. If errors persist after 3 retries, the wiring or sensor may be faulty. Do not treat CRC errors as fatal.
