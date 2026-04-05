# eiDOS — Raspberry Pi Deployment

Flash an SD card, run one script, plug in your Pi. Zero-touch setup gives each
Pi a unique creature identity, joins your Tailscale network, builds llama.cpp,
downloads the model, and starts eiDOS automatically.

## Prerequisites

- **Raspberry Pi 4 (4GB+) or Pi 5** with 16GB+ SD card
- **Raspberry Pi Imager** — [download](https://www.raspberrypi.com/software/)
- **Ethernet cable** (plugged in before first boot)
- **Tailscale account** with a reusable auth key from
  [admin/settings/keys](https://login.tailscale.com/admin/settings/keys)

## Step 1: Flash the SD Card

1. Open Raspberry Pi Imager
2. Choose OS → **Raspberry Pi OS Lite (64-bit)** (Bookworm or newer)
3. Choose your SD card
4. Click the gear icon (⚙) for customization:
   - **Set hostname:** `raspberrypi` (will be overwritten by setup)
   - **Enable SSH:** Use password authentication
   - **Set username:** `pi`
   - **Set password:** (your choice)
   - **Set locale:** your timezone
5. Click **Write**

## Step 2: Configure eiDOS

```bash
cd deploy/sdcard

# Create your config from the template
cp eidos.env.example eidos.env

# Edit eidos.env — fill in your Tailscale auth key
# The model URL is pre-filled with Qwen 3.5 4B Q4_K_M
nano eidos.env  # or vim, or open in any editor
```

## Step 3: Prepare the SD Card

After flashing, the boot partition auto-mounts on macOS (usually `/Volumes/bootfs`):

```bash
# Make the script executable (first time only)
chmod +x prepare_sdcard.sh

# Run it, pointing at the boot partition
./prepare_sdcard.sh /Volumes/bootfs
```

This will:
- Bundle the eiDOS codebase (excluding tests, .git, dev-only files)
- Copy everything to the boot partition
- Patch the Imager's firstrun.sh to trigger eiDOS setup on first boot

## Step 4: Boot the Pi

1. Eject the SD card from your Mac
2. Insert into the Pi
3. Connect Ethernet
4. Power on

The Pi will:
1. Run Raspberry Pi Imager's initial setup (create user, enable SSH) → reboot
2. Run eiDOS setup (~20-40 min depending on Pi model + network speed):
   - Install system packages
   - Install and join Tailscale
   - Build llama.cpp from source
   - Download the GGUF model (~2.5GB)
   - Set up Python venv and install dependencies
   - Generate unique creature identity from Pi serial number
   - Install and enable systemd services
3. Reboot → all services start automatically

## Step 5: Verify

Once the Pi is back online (check `tailscale status` or your Tailscale admin panel):

```bash
# SSH in via Tailscale
ssh pi@eidos-XXXXXX    # where XXXXXX is the last 6 hex of the Pi serial

# Check setup log
sudo journalctl -u eidos-first-boot --no-pager

# Check services
systemctl status llama-server eidos dashboard

# Check the creature identity
cat ~/eidos/workspace/persona.json | python3 -m json.tool

# Check LLM health
curl http://127.0.0.1:8080/health

# Check dashboard
curl -s http://127.0.0.1:8099 | head -5
```

## What Gets Created on the Pi

```
/home/pi/
├── .eidos-setup-done          # marker file (prevents re-run)
├── models/
│   └── qwen3.5-4b-q4.gguf    # the GGUF model (~2.5GB)
└── eidos/                    # eiDOS application
    ├── .venv/                 # Python virtual environment
    ├── config.toml            # production config
    ├── eidos.py              # main entry point
    ├── workspace/
    │   ├── persona.json       # unique creature identity
    │   ├── goal.md            # current goal
    │   ├── interventions/
    │   ├── outputs/
    │   └── snapshots/
    └── deploy/
        ├── eidos.service
        ├── llama-server.service
        └── dashboard.service
```

## Unique Identity

Each Pi gets a deterministic name derived from its hardware serial number:
- **Hostname:** `eidos-a3f7c2` (last 6 hex of serial)
- **Tailscale name:** same
- **Creature name:** `Ember-Fox`, `Drift-Owl`, etc. (word lists indexed by serial)

The same Pi always gets the same name. Different Pis always get different names.

## Troubleshooting

### Setup seems stuck
SSH in over local network (find the IP from your router) and watch the log:
```bash
sudo journalctl -u eidos-first-boot -f
```

### Setup failed partway through
Fix the issue, then re-run:
```bash
sudo rm /home/pi/.eidos-setup-done
sudo systemctl enable eidos-first-boot
sudo bash /boot/firmware/eidos-setup.sh
```

### Services not starting after reboot
```bash
# Check each service
sudo journalctl -u llama-server --no-pager -n 50
sudo journalctl -u eidos --no-pager -n 50
sudo journalctl -u dashboard --no-pager -n 50
```

### Model download failed (network issue)
```bash
# Resume or retry the download
wget -c -O ~/models/qwen3.5-4b-q4.gguf "$(grep MODEL_URL /boot/firmware/eidos.env | cut -d= -f2-)"
```

### Want to reflash the same Pi
The setup is idempotent — it checks for existing files where possible. To force
a clean re-run, delete the marker: `rm ~/.eidos-setup-done` and reboot.

## Fleet Monitoring

Once multiple Pis are online, use the remote poller from any machine on your
Tailnet:

```bash
# Edit deploy/remote_poller.sh to list your nodes
NODES=("eidos-a3f7c2:100.x.x.x" "eidos-b2e9d1:100.x.x.x")
```

Or check the dashboard on any Pi: `http://eidos-XXXXXX:8099`
