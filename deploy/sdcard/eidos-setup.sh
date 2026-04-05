#!/usr/bin/env bash
# eiDOS — Raspberry Pi first-boot setup script
# Runs unattended as root via eidos-first-boot.service.
# Installs all dependencies, builds llama.cpp, downloads the model,
# extracts the eiDOS bundle, generates a unique identity, and enables services.
set -euo pipefail

BOOT_DIR="/boot/firmware"
LOG_TAG="eidos-setup"

log() { echo "[$(date '+%Y-%m-%d %H:%M:%S')] [$LOG_TAG] $*"; }
die() { log "FATAL: $*"; exit 1; }

# ── 1. Load configuration ──────────────────────────────────────────────────
log "Loading configuration from $BOOT_DIR/eidos.env"
[ -f "$BOOT_DIR/eidos.env" ] || die "eidos.env not found on boot partition"
# shellcheck source=/dev/null
source "$BOOT_DIR/eidos.env"

PI_USER="${PI_USER:-pi}"
HOME_DIR="/home/$PI_USER"

# Guard against re-running
if [ -f "$HOME_DIR/.eidos-setup-done" ]; then
    log "Setup already completed (found $HOME_DIR/.eidos-setup-done). Exiting."
    exit 0
fi

[ -n "${TAILSCALE_AUTHKEY:-}" ] || die "TAILSCALE_AUTHKEY not set in eidos.env"
[ -n "${MODEL_URL:-}" ] || die "MODEL_URL not set in eidos.env"

# ── 2. Derive unique identity from Pi serial ────────────────────────────────
SERIAL=$(tr -d '\0' < /proc/device-tree/serial-number 2>/dev/null || echo "unknown")
SHORT_ID="${SERIAL: -6}"
log "Pi serial: $SERIAL — short ID: $SHORT_ID"

# Deterministic creature name from serial hex digits
ADJECTIVES=(
    Amber Ashen Azure Blaze Bolt Brave Bright Brisk Bronze Cedar
    Cinder Cobalt Cold Coral Crimson Crystal Dapper Dawn Deep Dire
    Drift Dusk Dusty Elder Ember Fable Faint Feral Fever Flare
    Fleet Flint Frost Ghost Gleam Glint Glow Gold Grim Hallow
    Haze Hollow Humble Iron Ivory Jade Keen Lapis Lunar Marble
    Mist Moss Neon Noble Oaken Onyx Pale Pearl Pilot Plume
    Prism Prowl Pulse Quartz Quiet Raven Ridge Ripple Rogue Root
    Rowan Rune Rust Sage Sable Salt Shade Sharp Sheer Slate
    Smoke Solar Spark Spell Spire Steel Stone Storm Swift Thorn
    Thunder Timber Torch Vapor Velvet Verdant Vigil Void Warden Wild
    Wind Winter Woven Wraith Xenon Yield Zeal Zenith Zinc Zephyr
)
CREATURES=(
    Ant Asp Bat Bear Bee Boar Buck Cat Clam Cod
    Colt Crab Crow Deer Doe Dove Duck Eel Elk Ewe
    Fawn Finch Fish Flea Fly Fox Frog Gnat Goat Grub
    Gull Hare Hawk Hen Hog Ibex Ibis Jack Jay Kite
    Koi Lark Leech Lion Lynx Mink Mite Mole Moth Newt
    Owl Ox Paw Pike Pony Puma Quail Ram Rat Rook
    Ruff Seal Shad Slug Sole Stag Swan Teal Tick Toad
    Trout Vole Wasp Whelk Wolf Worm Wren Yak Zebu Crane
    Dace Drake Egret Finch Gecko Grebe Heron Hippo Hyena Igloo
    Jackal Koala Lemur Llama Magpie Marten Moose Mouse Otter Parrot
    Perch Pipit Plover Puffin Quoll Robin Roach Sable Shrike Skunk
    Snail Snake Snipe Squid Stoat Stork Tern Thrush Tiger Viper
    Warbler Weasel Whale Bison Coyote Falcon
)

ADJ_IDX=$(( 16#${SHORT_ID:0:2} % ${#ADJECTIVES[@]} ))
NOUN_IDX=$(( 16#${SHORT_ID:2:2} % ${#CREATURES[@]} ))
CREATURE_NAME="${ADJECTIVES[$ADJ_IDX]}-${CREATURES[$NOUN_IDX]}"
HOSTNAME="eidos-${SHORT_ID}"

log "Identity: $CREATURE_NAME (hostname: $HOSTNAME)"

# ── 3. Set hostname ─────────────────────────────────────────────────────────
hostnamectl set-hostname "$HOSTNAME"
sed -i "s/127\.0\.1\.1.*/127.0.1.1\t$HOSTNAME/" /etc/hosts

# ── 4. System packages ──────────────────────────────────────────────────────
log "Installing system packages"
export DEBIAN_FRONTEND=noninteractive
apt-get update -qq
apt-get install -y -qq \
    python3 python3-pip python3-venv \
    git cmake build-essential \
    wget curl jq \
    > /dev/null 2>&1
log "System packages installed"

# ── 5. Install Tailscale ────────────────────────────────────────────────────
log "Installing Tailscale"
curl -fsSL https://tailscale.com/install.sh | sh
log "Joining Tailnet as $HOSTNAME"
tailscale up --authkey="$TAILSCALE_AUTHKEY" --hostname="$HOSTNAME"
log "Tailscale connected"

# ── 6. Build llama.cpp ──────────────────────────────────────────────────────
# Ensure adequate swap for the link step (needs ~2GB)
if [ ! -f /swapfile ]; then
    log "Creating 2GB swap file"
    dd if=/dev/zero of=/swapfile bs=1M count=2048 status=none
    chmod 600 /swapfile
    mkswap /swapfile > /dev/null
    swapon /swapfile
    echo '/swapfile none swap sw 0 0' >> /etc/fstab
fi

# Scale build parallelism to available RAM (link step is memory-hungry)
TOTAL_MB=$(awk '/MemTotal/ {printf "%d", $2/1024}' /proc/meminfo)
if [ "$TOTAL_MB" -le 2048 ]; then
    BUILD_JOBS=1
elif [ "$TOTAL_MB" -le 4096 ]; then
    BUILD_JOBS=2
else
    BUILD_JOBS=$(nproc)
fi

log "Building llama.cpp from source ($BUILD_JOBS jobs, ${TOTAL_MB}MB RAM)"
LLAMA_BUILD="/tmp/llama-build"
rm -rf "$LLAMA_BUILD"
git clone --depth 1 https://github.com/ggerganov/llama.cpp.git "$LLAMA_BUILD"
cd "$LLAMA_BUILD"
cmake -B build -DCMAKE_BUILD_TYPE=Release -DGGML_NATIVE=ON
cmake --build build -j"$BUILD_JOBS" --target llama-server
cp build/bin/llama-server /usr/local/bin/llama-server
chmod +x /usr/local/bin/llama-server
cd /
rm -rf "$LLAMA_BUILD"
log "llama-server installed to /usr/local/bin/"

# ── 7. Download the GGUF model ──────────────────────────────────────────────
MODEL_DIR="$HOME_DIR/models"
MODEL_PATH="$MODEL_DIR/qwen3.5-4b-q4.gguf"
mkdir -p "$MODEL_DIR"

if [ ! -f "$MODEL_PATH" ]; then
    log "Downloading GGUF model (~2.5GB, patience...)"
    wget -q --show-progress -O "$MODEL_PATH" "$MODEL_URL"
    log "Model downloaded to $MODEL_PATH"
else
    log "Model already exists at $MODEL_PATH, skipping download"
fi

# ── 8. Extract eiDOS bundle ─────────────────────────────────────────────────
EIDOS_DIR="$HOME_DIR/kairos"
log "Extracting eiDOS bundle to $EIDOS_DIR"
mkdir -p "$EIDOS_DIR"
tar xzf "$BOOT_DIR/eidos-bundle.tar.gz" -C "$EIDOS_DIR"

# ── 9. Production config ────────────────────────────────────────────────────
log "Installing production config"
cp "$BOOT_DIR/config.production.toml" "$EIDOS_DIR/config.toml"

# ── 10. Generate unique persona ─────────────────────────────────────────────
log "Generating persona: $CREATURE_NAME"
WORKSPACE_DIR="$EIDOS_DIR/workspace"
mkdir -p "$WORKSPACE_DIR"/{interventions,outputs,snapshots,live_test_logs}

BORN=$(date -u +"%Y-%m-%dT%H:%M:%SZ")
cat > "$WORKSPACE_DIR/persona.json" << PERSONA
{
    "name": "$CREATURE_NAME",
    "born": "$BORN",
    "xp": 0,
    "level": 1,
    "goals_completed": 0,
    "total_ticks": 0,
    "total_errors_recovered": 0,
    "total_compactions": 0,
    "longest_streak": 0,
    "current_streak": 0,
    "tools_used": {},
    "traits": [],
    "mood": "curious",
    "titles": [],
    "last_goal_summary": "",
    "uptime_total_s": 0
}
PERSONA

# ── 11. Write default goal ──────────────────────────────────────────────────
cat > "$WORKSPACE_DIR/goal.md" << 'GOAL'
# Current Goal

Explore and understand the system you're running on. Check hardware specs,
available memory, disk space, network connectivity, and CPU temperature.
Report your findings and plan your next steps.
GOAL

# ── 12. Create Python venv ──────────────────────────────────────────────────
log "Creating Python virtual environment"
python3 -m venv "$EIDOS_DIR/.venv"
"$EIDOS_DIR/.venv/bin/pip" install --quiet --upgrade pip
"$EIDOS_DIR/.venv/bin/pip" install --quiet -r "$EIDOS_DIR/requirements.txt"
log "Python dependencies installed"

# ── 13. Install systemd services ────────────────────────────────────────────
log "Installing systemd services"

# Patch service files for this Pi's user and venv Python
for svc in kairos.service dashboard.service; do
    sed -i "s|/usr/bin/python3|$EIDOS_DIR/.venv/bin/python3|g" "$EIDOS_DIR/deploy/$svc"
    sed -i "s|User=pi|User=$PI_USER|g" "$EIDOS_DIR/deploy/$svc"
    sed -i "s|/home/pi/kairos|$EIDOS_DIR|g" "$EIDOS_DIR/deploy/$svc"
done

# Patch llama-server service for this Pi's user and model path
sed -i "s|User=pi|User=$PI_USER|g" "$EIDOS_DIR/deploy/llama-server.service"
sed -i "s|/home/pi/models|$MODEL_DIR|g" "$EIDOS_DIR/deploy/llama-server.service"

cp "$EIDOS_DIR/deploy/llama-server.service" /etc/systemd/system/
cp "$EIDOS_DIR/deploy/kairos.service" /etc/systemd/system/
cp "$EIDOS_DIR/deploy/dashboard.service" /etc/systemd/system/

systemctl daemon-reload
systemctl enable llama-server.service kairos.service dashboard.service

# ── 14. Reduce GPU memory (headless — no desktop needed) ────────────────────
if ! grep -q "gpu_mem=" /boot/firmware/config.txt; then
    echo "gpu_mem=16" >> /boot/firmware/config.txt
    log "Set gpu_mem=16 in config.txt"
fi

# ── 15. Fix ownership ───────────────────────────────────────────────────────
chown -R "$PI_USER:$PI_USER" "$EIDOS_DIR" "$MODEL_DIR"

# ── 16. Mark setup complete ─────────────────────────────────────────────────
touch "$HOME_DIR/.eidos-setup-done"
systemctl disable eidos-first-boot.service

log "══════════════════════════════════════════════════════"
log "  eiDOS setup complete!"
log "  Creature: $CREATURE_NAME"
log "  Hostname: $HOSTNAME"
log "  Tailscale: $(tailscale ip -4 2>/dev/null || echo 'pending')"
log "══════════════════════════════════════════════════════"
log "Rebooting in 5 seconds to start services..."
sleep 5
reboot
