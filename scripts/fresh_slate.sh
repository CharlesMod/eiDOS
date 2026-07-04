#!/usr/bin/env bash
# fresh_slate.sh — birth a NEW creature (Sprinter/systemd host).
#
# Thin wrapper: the wipe itself is reset_eidos.py (the SAME procedure the dashboard settings
# menu runs — one source of truth: watchdog disarm, job reap, kill-all-writers, archive, clear,
# nugget + genesis-questline reseed, new congenital genome on next first-birth). What this
# script adds is the part the dashboard can't do to itself on systemd: restart the
# eidos-dashboard service so FRESH CODE loads, then issue the operator start — eidos boots
# PAUSED (kill-switch design) and takes no tick until you say GO.
#
# Same creature back to an egg instead (keeps knowledge/skills/genome):
#     .venv/bin/python reset_eidos.py --yes --keep-knowledge   (then restart + start yourself)
#
# Usage: scripts/fresh_slate.sh [--yes]
set -euo pipefail
cd "$(dirname "$0")/.."

DASH="http://127.0.0.1:8099"
PY=".venv/bin/python"

[ -x "$PY" ] || { echo "!! no venv at $PY — run from the repo root on Sprinter" >&2; exit 1; }

if [ "${1:-}" != "--yes" ]; then
    echo "This RETIRES the current creature (archives its workspace), then a blank rebirth."
    read -r -p "Type 'fresh' to proceed: " ans
    [ "$ans" = "fresh" ] || { echo "aborted"; exit 1; }
fi

echo "== 1/3 reset (stop, archive, clear, reseed — via reset_eidos.py)"
PYTHONUTF8=1 "$PY" reset_eidos.py --yes

echo "== 2/3 restarting the dashboard (fresh code)"
sudo -n systemctl restart eidos-dashboard.service
status=""
for _ in $(seq 1 30); do                  # bounded wait on the ground-truth endpoint
    status="$(curl -s -m 2 "$DASH/api/control/status" 2>/dev/null || true)"
    [ -n "$status" ] && break
    sleep 1
done
if [ -z "$status" ]; then
    echo "!! dashboard did not answer on $DASH within 30s — check:" >&2
    echo "   sudo systemctl status eidos-dashboard.service" >&2
    exit 1
fi

echo "== 3/3 operator start (boots PAUSED)"
curl -s -m 30 -X POST "$DASH/api/control/start"; echo

echo
echo "The newborn is holding at PAUSED. It draws its genome on first breath."
echo "Say GO in the dashboard ($DASH) or: curl -X POST $DASH/api/control/resume"
