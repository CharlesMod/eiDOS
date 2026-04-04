---
id: tailscale_network
category: facts
tags: [network, tailscale, vpn, connectivity]
confidence: verified
source_goal: "Initial system survey"
source_tick: 8
created: "2026-04-01T10:30:00Z"
updated: "2026-04-01T10:30:00Z"
---
Network access is via Tailscale VPN. The Pi's Tailscale IP is 100.74.178.26. The control machine is at 100.113.123.91. There is no direct internet access — all external traffic routes through the Tailscale network. Pull-only architecture: the Pi never initiates outbound connections to untrusted hosts.
