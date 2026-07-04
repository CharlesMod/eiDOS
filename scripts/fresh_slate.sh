#!/usr/bin/env bash
# fresh_slate.sh — birth a NEW creature (Sprinter/systemd host).
#
# Archives the current creature's workspace and boots a blank one: new congenital genome draw
# (workspace/genome.json is born on first construction), curated inheritance re-seeded from
# preserved_nuggets.toml, and the genesis questline queued (the System first speaks after the
# first sleep). The old creature's whole life is preserved intact in workspace-pre-freshslate-*.
#
# The dashboard is STOPPED for the swap — its watchdog respawns eidos on sight, so pausing eidos
# alone is not enough; the workspace must never be moved out from under a live watchdog. Starting
# the dashboard again also loads fresh code, and an operator start boots eidos PAUSED
# (kill-switch design): the newborn takes no tick until you say GO.
#
# Usage: scripts/fresh_slate.sh [--yes]
set -euo pipefail
cd "$(dirname "$0")/.."

DASH="http://127.0.0.1:8099"
TS="$(date +%Y%m%d-%H%M%S)"
ARCHIVE="workspace-pre-freshslate-$TS"
PY=".venv/bin/python"

[ -x "$PY" ] || { echo "!! no venv at $PY — run from the repo root on Sprinter" >&2; exit 1; }

if [ "${1:-}" != "--yes" ]; then
    echo "This RETIRES the current creature: workspace/ -> $ARCHIVE, then a blank rebirth."
    read -r -p "Type 'fresh' to proceed: " ans
    [ "$ans" = "fresh" ] || { echo "aborted"; exit 1; }
fi

echo "== 1/5 stopping the creature and its supervisor"
curl -s -m 5 -X POST "$DASH/api/control/stop" >/dev/null || true   # graceful, best-effort
sudo -n systemctl stop eidos-dashboard.service                     # synchronous; kills the cgroup
if pgrep -f "eidos.py run_loop" >/dev/null; then
    echo "!! an eidos process survived the service stop — refusing to move its workspace" >&2
    exit 1
fi

echo "== 2/5 archiving the old life -> $ARCHIVE"
if [ -d workspace ]; then
    mv workspace "$ARCHIVE"
else
    echo "   (no workspace/ to archive)"
fi
mkdir -p workspace
git checkout HEAD -- workspace/goal.md    # the tracked seed goal is part of a fresh workspace

echo "== 3/5 seeding the inheritance (preserved_nuggets.toml)"
PYTHONUTF8=1 "$PY" seed_knowledge.py

echo "== 4/5 queueing the genesis questline"
PYTHONUTF8=1 "$PY" seed_genesis_quests.py

echo "== 5/5 starting the dashboard (fresh code; eidos boots PAUSED)"
sudo -n systemctl start eidos-dashboard.service
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

echo
echo "status: $status"
echo "old life archived at: $ARCHIVE"
echo "The newborn is holding at PAUSED. It draws its genome on first breath."
echo "Say GO in the dashboard ($DASH) or: curl -X POST $DASH/api/control/resume"
