# eiDOS — the house AI (this repo)

eiDOS is an always-on autonomous agent (extended from the Kairos tick-loop framework) that runs
Charlie's house on a local LLM. Operator = Charlie (called "Dean"/"Boss" in older docs — same
person). There is exactly ONE eiDOS and it is normally already running — never try to "start the
LLM/eidos" from scratch; they exist as services.

## The live host: Sprinter (Linux / Pop!_OS, systemd)
`RUNTIME_SPRINTER.md` is the authoritative runtime + operations cookbook — read it before you
kill, restart, or GPU-starve anything. The short version:

- Mind = **`gemma4-12b`** (~6.5 GB, full offload) served by **`llama-swap.service`** at
  `http://127.0.0.1:8080` (set in `config.local.toml`; `qwen27b` available by name, partial
  offload). Semantic-recall embeddings = **`eidos-embed.service`** (:8082, nomic-embed, resident
  in spare VRAM).
- Tick loop: `eidos.py run_loop`. Config: `config.toml` + machine-local overrides in
  `config.local.toml`. Working state: `workspace/`.
- **Dashboard (`dashboard.py`, http://127.0.0.1:8099)** runs under **`eidos-dashboard.service`**
  and is also the **watchdog**: it spawns eidos as a CHILD process and auto-restarts /
  auto-rolls-back. The UI HTML lives in `static/dashboard.html` (served by `do_GET "/"`), not inline.
- The Windows-era monitor tap (:8088), voice service (:8098), and IDE (:8100) are **not running**
  on Sprinter — don't assume they exist. (`voice.py` is the future voice service if/when revived.)

## Run / restart discipline (IMPORTANT)
- Both system services are `Restart=always`: a bare `kill`/`pkill` just respawns them —
  **use `sudo systemctl stop|restart eidos-dashboard.service` / `llama-swap.service`**
  (passwordless sudo). A dashboard.py code change goes live via
  `sudo systemctl restart eidos-dashboard.service`.
- **Restart just the eidos tick loop** (the dashboard's CHILD): use the dashboard Start button /
  control API, or kill the eidos child PID — the watchdog respawns it on fresh code. Never stop
  the dashboard for this; that stops the supervisor. Gotcha: `pgrep -f "eidos.py"` matches your
  own subshell — use `ps -eo pid,args | grep "[e]idos.py"`.
- eidos boots **paused** (kill-switch design): operator starts and apply/restore restarts boot
  PAUSED; a plain crash-respawn resumes running (continuity). Resume via "GO" in the dashboard
  chat or `POST :8099/api/control/{start,stop,resume,pause,status}`.
- Fresh slate (retire creature, birth a new one): `scripts/fresh_slate.sh` — never hand-roll it.
- Manual debug run: `PYTHONUTF8=1 python dashboard.py --config config.toml --port 8099`.
  Always `PYTHONUTF8=1` (unicode in prompts/output).
- VRAM: gemma leaves ~8 GB free after desktop; check `nvidia-smi` before launching any second
  llama-server. To free ALL VRAM for an eval: stop `eidos-dashboard.service`, then
  `llama-swap.service`; restore in reverse (eidos boots PAUSED).

## IMPORTANT: keep eiDOS's self-knowledge current
`eidos_capabilities.md` is the authoritative map of what the platform provides — eiDOS reads it via
the `check_system` tool so it operates existing subsystems instead of rebuilding them (it kept
reinventing chat loggers and JSON memory stores at Lv.0). **When you add or change a subsystem/
capability, update `eidos_capabilities.md`** (and, for a critical "never rebuild this", the condensed
block in `prompts.py SYSTEM_PROMPT_BRIEFING`). That single update is how eiDOS learns the feature
exists — far better than re-teaching it each time. The curated bootstrap facts pre-seeded into a
fresh eiDOS after every wipe live in **`preserved_nuggets.toml`** (a small hand-edited `[[nugget]]`
database; `seed_knowledge.py` just loads it). Add a durable fact there to make the next wipe smarter.

## Self-improvement system (live)
eiDOS can be coached and improve itself from the :8099 dashboard. Principle: **eiDOS PROPOSES, the
operator-controlled dashboard APPLIES** (git-reversible accident-safety, not adversary-proof).
- **Self-guide** (`workspace/self_guide.md`) — Charlie's standing directives, injected every tick.
  Edit the file or the dashboard panel; eiDOS proposes changes via the `update_self_guide` tool.
- **Self-code-editing** — eiDOS `propose_self_edit(target_file, new_content, rationale)` → staged +
  compile-checked → Charlie approves the diff in the "Self-Edit Proposals" panel → `selfedit.apply`
  (pre-apply git checkpoint → write → commit) → restarts eidos. OFF-LIMITS: dashboard.py, config,
  the safety files, skills.py (`git_safety.PROTECT_PATHS`). `self_edit_enabled` in config.
- **Git safety** (`git_safety.py`) — checkpoints = commits + `eidos-good-*` tags (source only;
  `workspace/` excluded). Dashboard "Git Safety" panel: checkpoint / restore last good.
- **Watchdog auto-rollback** — crash-loop (5×/180s) → restore `last_good`, bounded to 2 tries, then
  stand down. State in `workspace/state/` (`rollback_attempted`, `watchdog_events.log`).
- **Listening hold** — focusing the dashboard chat box quiets the loop (blue "listening" state).
- Full design + deferred hardening: **`SELF_IMPROVEMENT_PLAN.md`** (written for the Windows era;
  its OS-isolation/ACL specifics predate Sprinter, the principles stand).

## Architecture principles (read `ARCHITECTURE_PRINCIPLES.md`)
Standing design preferences. #1 (Charlie): **event-driven over polled — call-response, notification,
or interrupt, never delay-based.** Prefer an interrupt/`notify`, else a bounded blocking acquire
(server-side event wait), and only poll-with-sleep when there is genuinely no signal to subscribe
to. No `sleep(N)`-and-hope, no fixed cooldown timers as a stand-in for "is it done yet". Delays are
guesses; events are ground truth. #4 (Charlie): **the system never lies to the creature** — a tool
result says what actually happened; gates/caps/no-ops are visible typed failures carrying the real
rule, never a success-wrapped nothing (a success-lie in `objective_add` once no-op'd 59 straight
commitments and deadlocked the whole progression).

## Async tool model
eiDOS's `bash` is **async by default**: it dispatches and the result returns later tagged `[↩ job N]`;
pass `"wait": true` only when the output is needed that tick. Slow commands auto-background at
`cmd_timeout_s` (10s); hard ceiling `cmd_async_ceiling_s` (180s). `bg_run`/`bg_check` for long work.

## Legacy note (Windows era)
This project previously ran on a Windows/nssm host (`EidosDashboard`/`EidosVoice`/`EidosTap`
services, `Restart-Service`, `taskkill`, the :8088 monitor tap, HouseAI-Llama at :8081 needing
~15.7 GB). Any doc or comment giving those instructions is describing that box, not Sprinter —
`RUNTIME_SPRINTER.md` supersedes them. `install.ps1` / `scripts/*.ps1` are Windows-era artifacts.
