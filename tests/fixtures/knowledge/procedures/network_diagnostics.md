---
id: network_diagnostics
category: procedures
tags: [network, diagnostics, troubleshooting, connectivity]
confidence: verified
source_goal: "Fix network outage"
source_tick: 67
created: "2026-04-03T14:00:00Z"
updated: "2026-04-03T14:00:00Z"
---
When network connectivity fails, diagnose in this order: 1) ip link show — check interface is UP. 2) ping the default gateway. 3) Check /etc/resolv.conf for DNS. 4) systemctl status systemd-networkd. 5) Check tailscale status for VPN health. 6) If all else fails, restarting tailscaled often fixes transient issues.
