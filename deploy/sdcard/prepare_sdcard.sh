#!/usr/bin/env bash
# eiDOS — macOS SD card preparation script
# Bundles the eiDOS repo, copies everything to the Pi boot partition,
# and patches firstrun.sh so the setup runs automatically on first boot.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
REPO_DIR="$(cd "$SCRIPT_DIR/../.." && pwd)"

RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m'

log()  { echo -e "${GREEN}[eiDOS]${NC} $*"; }
warn() { echo -e "${YELLOW}[eiDOS]${NC} $*"; }
die()  { echo -e "${RED}[eiDOS]${NC} $*" >&2; exit 1; }

# ── Validate arguments ──────────────────────────────────────────────────────
if [ $# -lt 1 ]; then
    echo "Usage: $0 <boot-partition-path>"
    echo "  e.g. $0 /Volumes/bootfs"
    exit 1
fi

BOOT="$1"

[ -d "$BOOT" ] || die "Directory not found: $BOOT"
[ -f "$BOOT/config.txt" ] || die "$BOOT doesn't look like a Pi boot partition (no config.txt)"

# ── Check for eidos.env ─────────────────────────────────────────────────────
ENV_FILE="$SCRIPT_DIR/eidos.env"
if [ ! -f "$ENV_FILE" ]; then
    die "eidos.env not found. Copy eidos.env.example to eidos.env and fill in your values:\n  cp $SCRIPT_DIR/eidos.env.example $SCRIPT_DIR/eidos.env"
fi

# Validate the env file has real values
# shellcheck source=/dev/null
source "$ENV_FILE"
if [ "${TAILSCALE_AUTHKEY:-}" = "tskey-auth-CHANGEME" ] || [ -z "${TAILSCALE_AUTHKEY:-}" ]; then
    die "TAILSCALE_AUTHKEY is not set in eidos.env. Get a reusable key from https://login.tailscale.com/admin/settings/keys"
fi

# ── Create the bundle ────────────────────────────────────────────────────────
log "Creating eiDOS bundle from $REPO_DIR"
BUNDLE_PATH="$SCRIPT_DIR/eidos-bundle.tar.gz"

tar czf "$BUNDLE_PATH" \
    -C "$REPO_DIR" \
    --exclude='tests' \
    --exclude='workspace/outputs' \
    --exclude='workspace/snapshots' \
    --exclude='workspace/live_test_logs' \
    --exclude='workspace/interventions' \
    --exclude='workspace/knowledge' \
    --exclude='workspace/memory.md' \
    --exclude='workspace/observations.jsonl' \
    --exclude='workspace/goal.md' \
    --exclude='workspace/plan.md' \
    --exclude='workspace/wal.json' \
    --exclude='workspace/heartbeat.json' \
    --exclude='workspace/flavor.json' \
    --exclude='workspace/exam_results.json' \
    --exclude='workspace/stress_results.json' \
    --exclude='workspace/llm_log.jsonl' \
    --exclude='__pycache__' \
    --exclude='.git' \
    --exclude='.gitignore' \
    --exclude='.venv' \
    --exclude='.pytest_cache' \
    --exclude='deploy/sdcard' \
    --exclude='*.pyc' \
    --exclude='_test_*.py' \
    --exclude='_analyze.py' \
    --exclude='_seed_dashboard.py' \
    --exclude='simulate.py' \
    --exclude='stress.py' \
    --exclude='exam.py' \
    --exclude='setup_embedding.py' \
    .

BUNDLE_SIZE=$(du -h "$BUNDLE_PATH" | cut -f1)
log "Bundle created: $BUNDLE_SIZE"

# ── Copy files to boot partition ─────────────────────────────────────────────
log "Copying files to $BOOT"
cp "$BUNDLE_PATH"                           "$BOOT/eidos-bundle.tar.gz"
cp "$SCRIPT_DIR/eidos-setup.sh"             "$BOOT/eidos-setup.sh"
cp "$SCRIPT_DIR/eidos.env"                  "$BOOT/eidos.env"
cp "$SCRIPT_DIR/eidos-first-boot.service"   "$BOOT/eidos-first-boot.service"
cp "$SCRIPT_DIR/config.production.toml"     "$BOOT/config.production.toml"

# ── Patch firstrun.sh ────────────────────────────────────────────────────────
FIRSTRUN="$BOOT/firstrun.sh"

INJECT='# eiDOS: install first-boot provisioning service
cp /boot/firmware/eidos-first-boot.service /etc/systemd/system/eidos-first-boot.service
chmod 644 /etc/systemd/system/eidos-first-boot.service
systemctl enable eidos-first-boot.service'

if [ -f "$FIRSTRUN" ]; then
    # Raspberry Pi Imager creates firstrun.sh with a cleanup line at the end.
    # Insert our service installation just before "exit 0".
    if grep -q "^exit 0" "$FIRSTRUN"; then
        log "Patching Imager firstrun.sh (inserting before exit 0)"
        # macOS sed needs '' after -i
        sed -i '' '/^exit 0$/i\
\
# eiDOS: install first-boot provisioning service\
cp /boot/firmware/eidos-first-boot.service /etc/systemd/system/eidos-first-boot.service\
chmod 644 /etc/systemd/system/eidos-first-boot.service\
systemctl enable eidos-first-boot.service\
' "$FIRSTRUN"
    else
        # firstrun.sh exists but doesn't have the expected cleanup line — append
        warn "firstrun.sh found but no cleanup line detected; appending"
        echo "" >> "$FIRSTRUN"
        echo "$INJECT" >> "$FIRSTRUN"
    fi
else
    # No firstrun.sh — create a minimal one that installs our service
    warn "No firstrun.sh found (raw flash without Imager customization)"
    log "Creating firstrun.sh and patching cmdline.txt"
    cat > "$FIRSTRUN" << 'EOF'
#!/bin/bash
set +e

# eiDOS: install first-boot provisioning service
cp /boot/firmware/eidos-first-boot.service /etc/systemd/system/eidos-first-boot.service
chmod 644 /etc/systemd/system/eidos-first-boot.service
systemctl enable eidos-first-boot.service

# Self-destruct
rm -f /boot/firmware/firstrun.sh
sed -i 's| systemd.run.*||g' /boot/firmware/cmdline.txt
exit 0
EOF
    chmod +x "$FIRSTRUN"

    # Raspberry Pi OS checks cmdline.txt for the systemd.run directive
    CMDLINE="$BOOT/cmdline.txt"
    if [ -f "$CMDLINE" ]; then
        # Append the systemd.run hook if not already present
        if ! grep -q "systemd.run=" "$CMDLINE"; then
            CURRENT=$(cat "$CMDLINE")
            echo "${CURRENT} systemd.run=/boot/firmware/firstrun.sh systemd.run_success_action=reboot systemd.unit=kernel-command-line.target" > "$CMDLINE"
        fi
    fi
fi

# ── Clean up the local bundle ────────────────────────────────────────────────
rm -f "$BUNDLE_PATH"

# ── Summary ──────────────────────────────────────────────────────────────────
log ""
log "════════════════════════════════════════════════════════"
log "  SD card prepared for eiDOS deployment"
log "════════════════════════════════════════════════════════"
log ""
log "  Files on boot partition:"
log "    eidos-bundle.tar.gz      — application bundle"
log "    eidos-setup.sh           — Pi-side setup script"
log "    eidos.env                — your configuration"
log "    eidos-first-boot.service — systemd one-shot"
log "    config.production.toml   — production config"
log ""
log "  Next steps:"
log "    1. Eject the SD card"
log "    2. Insert into your Pi"
log "    3. Connect Ethernet and power on"
log "    4. Wait ~20-40 minutes for setup to complete"
log "    5. Pi will reboot automatically when done"
log ""
log "  After reboot, check via Tailscale:"
log "    ssh $PI_USER@eidos-<serial>.tail<net>"
log "    sudo journalctl -u eidos-first-boot"
log "    systemctl status llama-server eidos dashboard"
log ""
