# eiDOS — the house AI (this repo)

eiDOS is an always-on autonomous agent (extended from the Kairos tick-loop framework) that runs
Dean's house on the local **house-ai** model. Operator = Dean. There is exactly ONE eiDOS and it
is normally already running — never try to "start the LLM/TTS/eidos" from scratch; they exist as
services.

## How it runs
- Mind = `HouseAI-Llama` service (Gemma-4-12B, llama.cpp) at `http://127.0.0.1:8081`. eiDOS's LLM
  calls go through a monitor tap at `:8088` → `:8081` (so its tokens show on the eval dashboard :9100).
- Tick loop: `eidos.py run_loop`. Config: `config.toml`. Working state: `workspace/`.
- **Dashboard (`dashboard.py`, http://127.0.0.1:8099)** also runs the **watchdog** that supervises
  eidos and auto-restarts/auto-rolls-back. It spawns eidos as a CHILD process.

## Run / restart discipline (IMPORTANT)
- Launch dashboard: `PYTHONUTF8=1 python dashboard.py --config config.toml --port 8099`.
- **Restart the dashboard with `taskkill /PID <pid> /F` — NEVER `/T`.** eidos is the dashboard's
  child; `/T` kills eidos too. `/F` alone lets eidos keep running (the watchdog covers gaps).
- **Restart eidos** by `taskkill /PID <eidos-pid> /F`; the watchdog respawns it on fresh code,
  booted PAUSED. Resume via the dashboard "GO" or `POST /api/control/resume`.
- eidos boots **paused** (kill-switch design). Control endpoints: `/api/control/{start,stop,resume,pause,status}`.
- Always `PYTHONUTF8=1` (unicode in prompts/output).

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
- **Self-guide** (`workspace/self_guide.md`) — Dean's standing directives, injected every tick.
  Edit the file or the dashboard panel; eiDOS proposes changes via the `update_self_guide` tool.
- **Self-code-editing** — eiDOS `propose_self_edit(target_file, new_content, rationale)` → staged +
  compile-checked → Dean approves the diff in the "Self-Edit Proposals" panel → `selfedit.apply`
  (pre-apply git checkpoint → write → commit) → restarts eidos. OFF-LIMITS: dashboard.py, config,
  the safety files, skills.py (`git_safety.PROTECT_PATHS`). `self_edit_enabled` in config.
- **Git safety** (`git_safety.py`) — checkpoints = commits + `eidos-good-*` tags (source only;
  `workspace/` excluded). Dashboard "Git Safety" panel: checkpoint / restore last good.
- **Watchdog auto-rollback** — crash-loop (5×/180s) → restore `last_good`, bounded to 2 tries, then
  stand down. State in `workspace/state/` (`rollback_attempted`, `watchdog_events.log`).
- **Listening hold** — focusing the dashboard chat box quiets the loop (blue "listening" state).
- Full design + deferred hardening: **`SELF_IMPROVEMENT_PLAN.md`**.

## Async tool model
eiDOS's `bash` is **async by default**: it dispatches and the result returns later tagged `[↩ job N]`;
pass `"wait": true` only when the output is needed that tick. Slow commands auto-background at
`cmd_timeout_s` (10s); hard ceiling `cmd_async_ceiling_s` (180s). `bg_run`/`bg_check` for long work.

## VRAM note
eiDOS needs HouseAI-Llama resident (~15.7 GB of 16). Running an eval (which loads its own llama)
will evict it — pause eiDOS and `Stop-Service HouseAI-Llama` before evals, restore after.
