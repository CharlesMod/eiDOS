# Kairos Pi Deployment Guide

## Architecture

```
┌──────────────────────────────────┐
│  Raspberry Pi 4 (field node)     │
│                                  │
│  llama-server  ←  kairos.py      │
│  (port 8080)     (tick loop)     │
│                                  │
│  dashboard.py                    │
│  (port 8099) ── read-only ──→ workspace/
│                                  │
│  Tailscale  (passive listener)   │
└──────────────┬───────────────────┘
               │ pull only (inbound)
               │
┌──────────────┴───────────────────┐
│  Operator's machine              │
│  remote_poller.sh → /api/ping    │
│  browser → :8099                 │
└──────────────────────────────────┘
```

**Nodes never initiate outbound connections.**  
All monitoring is pull-based via Tailscale.

## Prerequisites

- Raspberry Pi 4 (4GB+)
- Raspbian/Debian with Python 3.11+
- Tailscale installed and authenticated
- llama.cpp compiled for ARM (`llama-server` binary)
- Q4 GGUF model file (e.g., `qwen3.5-4b-q4.gguf`)

## Setup

### 1. Copy Kairos to the Pi

```bash
scp -r . pi@<tailscale-ip>:/home/pi/kairos/
```

### 2. Production config.toml

Edit `/home/pi/kairos/config.toml`:

```toml
[llm]
url = "http://127.0.0.1:8080"    # local llama-server
model = "local"
max_tokens = 1024
request_timeout_s = 600           # on-device inference is slower

[tick]
interval_s = 300                  # 5 minutes between ticks

[safety]
thermal_pause_c = 72.0            # conservative for Pi 4

[self_healing]
restart_cmd = "sudo systemctl restart llama-server"
local_only = true
max_consecutive_failures = 5

[dashboard]
port = 8099
```

### 3. Install systemd services

```bash
sudo cp deploy/kairos.service /etc/systemd/system/
sudo cp deploy/llama-server.service /etc/systemd/system/
sudo cp deploy/dashboard.service /etc/systemd/system/

# Edit paths if needed
sudo systemctl daemon-reload
sudo systemctl enable llama-server kairos dashboard
sudo systemctl start llama-server kairos dashboard
```

### 4. Verify

```bash
# Check all services
systemctl status llama-server kairos dashboard

# Test dashboard
curl http://localhost:8099/api/ping

# Test from operator machine via Tailscale
curl http://<tailscale-ip>:8099/api/ping
```

## Monitoring

### Dashboard

Open `http://<tailscale-ip>:8099` in a browser. Auto-refreshes every tick interval.

### Remote poller (operator's machine)

```bash
# Edit deploy/remote_poller.sh — set your node IPs
chmod +x deploy/remote_poller.sh
./deploy/remote_poller.sh

# Or run on a schedule
watch -n 300 ./deploy/remote_poller.sh
```

### API Endpoints

| Endpoint | Purpose | Size |
|---|---|---|
| `GET /` | HTML dashboard | ~8KB |
| `GET /api/status` | Full status JSON | ~5-15KB |
| `GET /api/ping` | Health check | <500B |

## Setting a Goal

```bash
ssh pi@<tailscale-ip>
echo "Monitor disk health and report SMART data every hour" > /home/pi/kairos/workspace/goal.md
```

Kairos will pick it up on the next tick.

## Logs & Diagnostics

```bash
# Kairos logs
journalctl -u kairos -f

# LLM server logs
journalctl -u llama-server -f

# Workspace files
ls /home/pi/kairos/workspace/
# heartbeat.json  — latest tick snapshot
# metrics.jsonl   — time series
# observations.jsonl — activity log
# persona.json    — creature identity
# memory.md       — working memory
# wal.json        — crash recovery state
```

## Daily Restart

The systemd unit has `RuntimeMaxSec=86400` — Kairos restarts every 24 hours.  
WAL-based recovery makes this seamless. No state is lost.
