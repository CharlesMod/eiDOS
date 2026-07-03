# RUNTIME — "Sprinter" (the live Linux box)

> **Read this before you kill, restart, or GPU-starve anything.** eiDOS now runs on **Sprinter**, a
> Linux box supervised by **systemd**. `CLAUDE.md`'s run/restart discipline (nssm services,
> `Restart-Service EidosDashboard`, `taskkill`, PowerShell, "the mind needs ~15.7 GB") describes the
> OLD Windows host and is **wrong here**. This file is the source of truth for the live runtime.
> **Authored:** 2026-07-03, from live inspection. Update it when the topology changes.

## The machine
- **Host:** Sprinter · **OS:** Pop!_OS 24.04 LTS · **GPU:** RTX 5080, 16 GB · **User:** `cmod`
- **`sudo` is passwordless** for `cmod` — you *can* stop/start the system services yourself.
- Desktop is COSMIC (Wayland); `cosmic-*` + `claude-desktop` hold ~1.3 GB of GPU on their own.

## The surprise: two systemd SYSTEM services, both `Restart=always`
A bare `kill`/`pkill` of these processes **does nothing lasting** — systemd respawns them in seconds
(new PID). You must go through `systemctl`. (Units live in `/etc/systemd/system/`.)

| Service | Supervises | Listen | Restart | Notes |
|---|---|---|---|---|
| **`eidos-dashboard.service`** | `dashboard.py` — which itself spawns/watchdogs the `eidos.py` tick loop as a **child** | dashboard **:8099** | always, 10 s | There is **no separate eidos.service**; the dashboard *is* the supervisor. `WorkingDirectory=/home/cmod/Documents/Software/eiDOS`, runs the repo venv. `EIDOS_SUPERVISED` deliberately unset → the dashboard Start button restarts the tick loop *in place*; `systemctl restart` is what hot-reloads `dashboard.py` itself. |
| **`llama-swap.service`** | `llama-swap` (:8080), which boots a `llama-server` **child on demand** | swap **:8080**, child on a dynamic port (e.g. 5801) | always, 5 s | Lazy: loads NO model at startup; first `/v1/chat/completions` for a model boots it. `LD_LIBRARY_PATH=/usr/local/cuda-13.0/lib64`. |

### Process topology
```
systemd(1)
 ├─ eidos-dashboard.service → dashboard.py (:8099, the watchdog) ──spawns──▶ eidos.py (the tick loop, child)
 └─ llama-swap.service      → llama-swap  (:8080, router)         ──spawns──▶ llama-server (one model, dynamic port)
```
eiDOS reaches the model **directly at `http://127.0.0.1:8080`** (set in `config.local.toml`, overriding
`config.toml`'s `:8088`). The Windows-era **monitor tap (:8088), voice (:8098), and IDE (:8100) are NOT
running on Sprinter** — don't assume they exist.

## The model stack (llama-swap `config.yaml`, `/home/cmod/llm/llama-swap/`)
Only **one model resident at a time** (they share the 16 GB serially). `ttl: 0` = stay resident.
- **`gemma4-12b`** — the house mind. `gemma-4-12b-it-qat-q4_0.gguf` (~6.5 GB) + vision mmproj, **`-ngl 99`
  (full offload)**, `-c 16384`. This is eiDOS's primary model. (Note: ~6.5 GB, *not* the 15.7 GB CLAUDE.md
  quotes for the old box — there is real VRAM headroom here.)
- **`qwen27b`** — `Qwen_Qwen3.6-27B-Q5_K_L.gguf` (~21 GB), **partial offload `-ngl 30`** (rest on CPU),
  `-c 8192`. Slower; used when selected by name.
- Models dir: `/home/cmod/llm/models/`. Swap binary + config: `/home/cmod/llm/llama-swap/`.
  llama.cpp build: `/home/cmod/llm/src/llama.cpp/build/bin/llama-server`.

## Operations cookbook (the Sprinter-correct commands)
```bash
# STOP eiDOS entirely (dashboard + its child tick loop):
sudo systemctl stop eidos-dashboard.service

# FREE ALL VRAM (for an eval / GPU spike): stop the requester, then unload the model.
sudo systemctl stop eidos-dashboard.service      # no more LLM requests
sudo systemctl stop llama-swap.service           # unloads the resident llama-server child
#   ...do your GPU work...
sudo systemctl start llama-swap.service eidos-dashboard.service   # restore (eidos boots PAUSED by design)

# RESTART the dashboard CODE (hot-reload dashboard.py):
sudo systemctl restart eidos-dashboard.service

# RESTART just the eidos tick loop, keep the dashboard up:
#   use the dashboard Start button / POST http://127.0.0.1:8099/api/control/resume,
#   or kill the eidos CHILD pid (the dashboard watchdog respawns it on fresh code).
#   Do NOT kill the dashboard for this — that stops the supervisor.

# STATUS / LOGS:
systemctl status eidos-dashboard.service llama-swap.service
journalctl -u eidos-dashboard.service -n 100 --no-pager
```

## Gotchas that will bite you
- **`Restart=always`**: `kill`/`taskkill`/`pkill` → systemd relaunches in seconds. Use `systemctl stop`.
- **`pgrep -f "eidos.py --config"` matches your own shell** (the pattern is in your command line) — it
  returns your subshell's PID too, so a `kill $(pgrep …)` can SIGTERM *yourself*. Use the bracket trick:
  `ps -eo pid,args | grep "[e]idos.py"`.
- **eidos boots PAUSED** by design (kill-switch). After a start/restart it won't act until resumed
  ("GO" in the dashboard chat, or `POST :8099/api/control/resume`). A plain crash-respawn resumes running.
- **VRAM budget**: gemma (~6.5 GB) leaves ~8 GB free after desktop; qwen27b (~10.6 GB resident w/ partial
  offload) leaves only ~3 GB. Check `nvidia-smi` before launching a second llama-server.
- **CLAUDE.md is Windows-era.** Ignore its nssm/`Restart-Service`/`taskkill`/PowerShell instructions on
  Sprinter; this file supersedes them for the runtime.
