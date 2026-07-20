# RUNTIME — "Sprinter" (the live Linux box)

> **Read this before you kill, restart, or GPU-starve anything.** eiDOS now runs on **Sprinter**, a
> Linux box supervised by **systemd**. `CLAUDE.md`'s run/restart discipline (nssm services,
> `Restart-Service EidosDashboard`, `taskkill`, PowerShell, "the mind needs ~15.7 GB") describes the
> OLD Windows host and is **wrong here**. This file is the source of truth for the live runtime.
> **Authored:** 2026-07-03, from live inspection. Update it when the topology changes.

> **⚠ CURRENT LIVE HOST (2026-07-18): `cmod-s`, NOT Sprinter.** The software was moved to a different
> box. This one has **dual Tesla P100-PCIE-16GB (32 GB total)**, not the RTX 5080 described below.
> The mind is **gemma-4-12b-it Q8_0**, served by **llama-swap @ `http://127.0.0.1:9292`** (config
> `/etc/llama-swap/config.yaml`, launched with `-watch-config`; alias `gemma-4-12b`, **262144 ctx with
> full-precision f16 KV**, ~21 G resident — iSWA keeps the global KV to 8 layers). The embedder is
> **nomic-embed-text-v1.5 (768-dim) on GPU @ `:8082`** via **`llama-embedding.service`**. GGUF models
> live in **`/home/cmod/models/`**. Vision is currently disabled (this llama.cpp build lacks gemma-4-12b's
> `gemma4uv` CLIP projector). The Sprinter details below (RTX 5080, `:8080`, `/home/cmod/llm/…`,
> `eidos-embed.service`) describe the EARLIER host — on cmod-s use the values in this callout.

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
| **`eidos-embed.service`** | a dedicated `llama-server --embedding` | embeddings **:8082** | always, 5 s | Semantic recall. `nomic-embed-text-v1.5.Q8_0.gguf` (768-dim, ~140 MB), full offload, **resident in spare VRAM alongside the mind** (~0.4 GB). **NOT** in llama-swap: llama-swap keeps one model resident, so routing embeddings through it would swap out gemma. `embedding.py` POSTs to `:8082/v1/embeddings`; enabled via `[knowledge] embedding_enabled=true` in `config.local.toml`. Unit template: `/home/cmod/llm/deploy/eidos-embed.service`. |

### Process topology
```
systemd(1)
 ├─ eidos-dashboard.service → dashboard.py (:8099, the watchdog) ──spawns──▶ eidos.py (the tick loop, child)
 ├─ llama-swap.service      → llama-swap  (:8080, router)         ──spawns──▶ llama-server (one model, dynamic port)
 └─ eidos-embed.service     → llama-server (:8082, --embedding, nomic 768-dim, resident in spare VRAM)
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

# FRESH SLATE (retire the creature, birth a new one — archive workspace, reseed nuggets +
# genesis quests, restart on fresh code, boot PAUSED). Never hand-roll this: the watchdog
# respawns eidos, so the workspace swap must happen with the dashboard STOPPED.
scripts/fresh_slate.sh
```

## Dashboard read panels (operator-facing)
- **Growth panel** (`GET :8099/api/growth`, built by `growth.build_growth`): the read-only D1–D10
  dream-test scoreboard (PILLARS_PLAN §10) + KPI trends + raw vitals, aggregated from existing
  workspace/state stores. Every metric carries `{value, status(measured|unmeasured|human-judged),
  basis}` and is fail-open — it never fakes a number and never crashes on a missing store. The
  creature can't see this panel (operator-only); it replaces the by-hand functional-review tally.

## The pi coding agent (the delegate / workshop limb)
- **Installed 2026-07-10:** `/usr/bin/pi` (`@earendil-works/pi-coding-agent`, npm -g, Node 22 from
  NodeSource). The delegate spawns it detached; result returns tagged `[↩ delegate N]`.
- **Provider:** `~/.pi/agent/models.json` defines the `house` provider → llama-swap
  `http://127.0.0.1:8080/v1` (OpenAI-compatible), models `gemma4-12b` (default) and `qwen27b`.
  Telemetry off in `~/.pi/agent/settings.json`.
- Config: `[delegate]` in config.toml (`pi_provider = "house"`, `pi_model = "gemma4-12b"`,
  `pi_path = ""` → PATH). Verified end-to-end: tool_delegate → pi → gemma → runnable artifact.

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

## The 32k window flip (WISDOM_PLAN §0 — operator runbook)

The mind serves at `-c 16384` with **f16 KV** (`/home/cmod/llm/llama-swap/config.yaml`, entry
`gemma4-12b`). The Q8_0 weights are **12.7 GB** on the 16 GB RTX 5080, so 32k does NOT fit with
f16 KV — the flip requires KV quantization. The serving change and the config budgets are a PAIR
(coherence rule: `n_ctx >= max_total_chars/chars_per_token + response_reserve`); apply them
together or not at all.

1. **Stop the stack** (creature already stopped, or dashboard Stop first):
   `sudo systemctl stop llama-swap.service`.
2. **Edit the gemma entry** in `/home/cmod/llm/llama-swap/config.yaml`:
   `-c 16384` → `-c 32768`, and `--cache-type-k f16 --cache-type-v f16` →
   `--cache-type-k q8_0 --cache-type-v q8_0 -fa` (q8_0 KV halves the cache at negligible
   quality cost; `-fa` flash attention is required for a quantized V-cache).
3. **Restart + verify**: `sudo systemctl start llama-swap.service`; force a load with one small
   chat completion against `gemma4-12b`; check `nvidia-smi` — want ≥ 1.5 GB headroom AFTER load
   with the desktop up. OOM or thin headroom → fall back to `-c 24576` (still +50%) rather than
   shrinking KV further.
4. **Long-context smoke**: one ~25k-token-prompt request must complete cleanly before trusting it.
5. **Only then** apply the paired budgets (dashboard Settings or `config.local.toml`):
   ```toml
   [context]
   obs_max_chars = 44000
   memory_max_chars = 16000
   intelligence_max_chars = 8000
   interventions_max_chars = 8000
   max_total_chars = 76000
   [compaction]
   token_threshold = 11000
   obs_max_chars = 32000
   [wisdom]
   block_max_chars = 1400
   ```
   (~19k prompt tokens + ~5k response reserve under 32768 at chars_per_token 4.0 — the same
   margin discipline as the 16k sizing.) Restart eidos (dashboard Start) to pick them up.
6. **Rollback** = revert the yaml entry + remove the overlay keys. Two stale-doc notes while
   you're in here: the committed `config.toml` `[llm]` block (:9292 / dual P100 / 262k ctx) is a
   **cmod-s fossil** — the local overlay (:8080, `gemma4-12b`) is what serves on Sprinter; and
   the "~6.5 GB gemma" figure above predates the Q8_0 model (12.7 GB). Trust `nvidia-smi`.
