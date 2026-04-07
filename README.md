# Kairos

An autonomous LLM agent runtime designed for long-duration, unsupervised operation on a Raspberry Pi 4. Kairos pursues open-ended goals over days or weeks, managing its own memory, planning, tool use, and crash recovery — all powered by a local language model running on-device via llama.cpp.

## What It Does

Kairos runs an infinite **tick loop**. Each tick:

1. **Assembles context** — goal, working memory, recent observations, knowledge recall, environment alerts, and any operator messages are formatted into a prompt with strict per-section character budgets.
2. **Calls the LLM** — sends the prompt to a local llama.cpp server and streams the response.
3. **Parses a tool call** — extracts a structured `<tool>name</tool><args>{...}</args>` invocation from the model's output.
4. **Executes the tool** — runs the requested action (shell command, file I/O, HTTP request, memory update, etc.) with safety checks.
5. **Records the observation** — logs the tool result to `observations.jsonl` for future context.
6. **Sleeps** — waits for the configured tick interval before repeating.

Periodically, a **dream cycle** (compaction) fires: the LLM distills accumulated observations into a concise plan, extracts durable knowledge, and clears the observation log — preventing unbounded context growth.

## Architecture Overview

```
┌──────────────────────────────────────────────────────┐
│                     eidos.py                         │
│              Main tick loop / orchestrator            │
│  crash recovery · signal handling · persona · WAL    │
├──────────┬───────────┬───────────┬───────────────────┤
│context.py│ prompts.py│  llm.py   │     tools.py      │
│ context  │  prompt   │   LLM     │  tool registry    │
│ assembly │ templates │ interface │  & execution      │
│ budgets  │           │ streaming │  safety checks    │
├──────────┴───────────┤ hot-swap  ├───────────────────┤
│     memory.py        ├───────────┤    parser.py      │
│  goal · plan · obs   │compaction │  tool call &      │
│  subgoals · WAL      │   .py     │  reply parsing    │
│  interventions       │ dream     │  JSON cleanup     │
│                      │ cycle     │                   │
├──────────────────────┴───────────┴───────────────────┤
│  knowledge.py    persona.py    telemetry.py          │
│  BM25 search     XP/traits     heartbeat/metrics     │
│  fact store      mood/titles   activity tracking     │
│  embedding.py    ascii_art.py  rotation.py           │
│  (optional ONNX) creature art  log rotation          │
├──────────────────────────────────────────────────────┤
│  safety.py       session.py    env_snapshot.py       │
│  cmd blocking    SSH detection  disk/RAM/temp        │
│  disk/RAM/temp   standby mode   alerts               │
├──────────────────────────────────────────────────────┤
│  dashboard.py — HTTP status dashboard (port 8099)    │
│  read-only · auto-refresh · ASCII creature · Tailscale│
└──────────────────────────────────────────────────────┘
```

## Core Modules

### `eidos.py` — Agent Runtime

The main entry point and tick loop. Handles:

- **Crash recovery** via write-ahead log (`wal.json`) — reconstructs state on restart, validates goal/memory, restores from snapshots if empty, marks dead background jobs.
- **Goal detection** — watches for `goal.md` creation or changes, automatically triggers subgoal planning via hot-swap to a larger model.
- **Compaction scheduling** — triggers dream cycles when observation volume or tick count exceeds thresholds.
- **Loop detection** — tracks recent tool call hashes; injects a warning into the prompt when repeated actions are detected.
- **Adaptive token budgets** — on reasoning exhaustion (thinking model used all tokens with zero content), bumps `max_tokens` for the next tick and forces compaction after repeated failures.
- **Self-healing** — restarts llama-server after consecutive LLM failures.
- **Persona updates** — awards XP, recomputes traits/mood/titles each tick.

### `llm.py` — LLM Interface

Communicates with any OpenAI-compatible endpoint (llama.cpp, LM Studio, etc.):

- **Streaming** with token-by-token callback for live dashboard updates.
- **Reasoning model support** — separates thinking tokens from content tokens; raises `ReasoningExhausted` when the model uses its full budget on reasoning with zero output.
- **Hot-swap planning** — `planning_complete()` temporarily swaps the llama-server to a larger model (e.g., 4B) for subgoal generation, then restores the smaller fast model.
- **Interaction logging** to `llm_log.jsonl` for post-run analysis.

### `tools.py` — Tool Registry

All tools available to the agent:

| Tool | Description |
|------|-------------|
| `bash` | Shell command execution (120s timeout, output truncation, safety checks) |
| `read_file` | Read file contents (path traversal prevention) |
| `write_file` | Write/create files (disk space check) |
| `bg_run` | Launch background job (tracked in `jobs.json` ledger) |
| `bg_check` | Check status of background job |
| `http_get` | Fetch a URL (timeout, size limit) |
| `remember` | Append to working memory (`plan.md`) |
| `update_plan` | Replace working memory entirely |
| `plan_goal` | Break goal into subgoals (hot-swaps to planning model) |
| `goal_complete` | Mark current goal as finished |
| `reply` | Send conversational reply to operator |
| `store_knowledge` | Save durable fact/procedure/error/reflection |

Safety enforcement: regex-matched command blocking (no `rm -rf /`, no `systemctl stop eidos`, etc.), disk/RAM threshold checks before writes, output truncation with spillover to disk.

### `context.py` — Context Assembly

Builds the prompt messages list each tick with strict budget enforcement:

- **Briefing mode** (production): compressed system prompt → Mission → Plan → Subgoals → Intelligence (auto-recalled knowledge) → Chat → Environment alerts → Observations → Tick prompt. Total ceiling ~6500 chars (~1870 tokens).
- **Legacy mode**: full system prompt → Goal → Memory → Environment → Interventions → Observations → Tick prompt. Higher budgets for development.
- **Inverted pyramid** for observations: newest gets full output, older get progressively shorter summaries.
- **Budget overruns** logged to `ctx_overruns.jsonl` for tuning.
- **Current subtask injection** into the tick prompt — parses `subgoals.md` for the first unchecked item and puts it front-and-center so the model focuses on one task at a time.

### `memory.py` — State Persistence

Three-tier memory with atomic writes (temp file + rename):

- **`goal.md`** — the immutable long-term objective.
- **`plan.md`** — working memory, distilled from observations during dream cycles. Replaces older `memory.md`.
- **`subgoals.md`** — checklist of subtasks generated by `plan_goal`. Format: `- [ ] task` / `- [x] done`.
- **`observations.jsonl`** — append-only log of tool execution results, consumed by context assembly and cleared after compaction.
- **`interventions/`** — human operator messages dropped as files, consumed and marked `.done`.

### `compaction.py` — Dream Cycles

Periodic LLM-driven consolidation that prevents unbounded context growth:

1. **Snapshot** current memory to `snapshots/` for crash recovery.
2. **Plan update** — LLM reads recent observations and rewrites `plan.md` to reflect current progress.
3. **Knowledge extraction** — LLM identifies durable facts, procedures, or error patterns from observations and writes them to the knowledge store.
4. **Clear observations** — truncates `observations.jsonl` after successful compaction.

Supports combined (single LLM call) or two-phase modes. Handles `ReasoningExhausted` with retry at larger budget.

### `parser.py` — Response Parsing

Extracts structured tool calls from LLM output with extensive fallback handling for small models that produce sloppy JSON:

- Primary format: `<tool>name</tool><args>{"key": "value"}</args>`
- Handles unclosed tags, missing args, raw text args, markdown fences in JSON, HTML entities, single→double quote conversion.
- Fallback format: `TOOL: name PARAMS: {...}`
- Also parses `<reply>...</reply>` for operator responses.

### `knowledge.py` — Knowledge Store

Persistent markdown-based knowledge base with BM25 search:

- Entries stored as markdown files with YAML frontmatter (id, category, tags, confidence, source).
- Categories: `facts`, `procedures`, `errors`, `reflections`.
- **BM25 keyword search** for automatic recall during context assembly.
- Optional **semantic search** via ONNX embeddings (`embedding.py`, all-MiniLM-L6-v2, 384-dim).
- Knowledge survives across goals — extracted during dream cycles, recalled during context assembly.

### `persona.py` — Personality System

Emergent personality that evolves with usage:

- **XP & Levels** — XP awarded for successful ticks, error recoveries, compactions, goal completions. Level = 1 + √(xp/50).
- **Traits** — derived from tool usage patterns: "methodical" (>60% bash/read_file), "creative" (>30% write_file), "resilient" (error recoveries > 20), "curious" (http_get > 50), etc.
- **Mood** — computed from recent success rate and events: curious, focused, determined, frustrated, struggling, triumphant.
- **Titles** — achievement-based: "First Steps" (1 goal), "Centurion" (100 ticks), "Unkillable" (50 error recoveries).
- **ASCII art** — evolving creature sprites by level (Seed → Sprout → Creature → Guardian) × mood × animation frame.

### `dashboard.py` — Web Dashboard

Single-file stdlib HTTP server serving live agent status:

- **`GET /`** — auto-refreshing HTML dashboard with ASCII creature, goal, plan, observations, knowledge, persona stats.
- **`GET /api/status`** — full JSON status blob.
- **`GET /api/ping`** — tiny health check for remote monitoring.
- Read-only; accessed over Tailscale.

### `safety.py` — Safety Checks

- **Command blocking** — regex patterns for destructive commands (`rm -rf /`, `systemctl stop eidos`, `shutdown`, `mkfs`, `dd of=/dev/`, etc.).
- **Resource checks** — disk space minimum, RAM ceiling, CPU temperature monitoring.
- **Child process cleanup** — kills runaway children on RAM pressure.

### `telemetry.py` — Metrics & Monitoring

- **`heartbeat.json`** — atomic per-tick snapshot: tick number, persona state, resource usage, LLM timing, tool result.
- **`metrics.jsonl`** — append-only time series for post-run analysis.
- **`activity.json`** — current agent state (sleeping/thinking/executing/dreaming/error) for dashboard live display.

## Validation & Testing

| Script | Purpose |
|--------|---------|
| `validate.py` | Multi-stage validation against a live LLM endpoint (health, parsing, execution, multi-turn) |
| `exam.py` | 10 graded tasks of escalating difficulty including adversarial prompt injection |
| `simulate.py` | Multi-tick sandbox simulation with full context assembly and loop detection |
| `stress.py` | Adversarial stress test with prompt injection and context confusion payloads |

All run in a sandboxed environment with bash blocked and subprocess monkey-patched.

### Unit Tests

616 tests in `tests/`, organized per-module:

```
tests/
├── test_compaction.py      test_parser.py
├── test_config.py          test_persona.py
├── test_context.py         test_rotation.py
├── test_dashboard.py       test_safety.py
├── test_dashboard_data.py  test_session.py
├── test_embedding.py       test_simulation.py
├── test_kairos.py          test_telemetry.py
├── test_knowledge.py       test_tools.py
├── test_live_llm.py        
├── test_llm.py             
├── test_memory.py          
└── fixtures/knowledge/     # Seed knowledge for tests
```

Markers: `@pytest.mark.slow`, `@pytest.mark.live` (for tests hitting real LLM endpoints).

## Deployment

### Target Hardware

- **Raspberry Pi 4** (4GB RAM), Raspberry Pi OS Bookworm
- **Solar-powered** — designed for low-power, always-on operation
- **Tailscale** for all remote access (pull-only architecture, nodes never initiate outbound)

### Services (systemd)

| Service | Description |
|---------|-------------|
| `eidos.service` | Main agent loop — 24h runtime limit, 3GB RAM cap, 80% CPU quota, auto-restart |
| `llama-server.service` | llama.cpp inference — localhost:8080, 4 threads, configurable context size and reasoning budget |
| `dashboard.service` | Web dashboard — depends on eidos, auto-restart |

### Models

- **Main inference**: `qwen3-1.7b-q4_k_m.gguf` — fast enough for ~3-6 min ticks
- **Planning (hot-swap)**: `qwen3.5-4b-q4.gguf` — used only for `plan_goal` subgoal generation

### SD Card Provisioning

`deploy/sdcard/` contains tools for headless Pi setup:

- `prepare_sdcard.sh` — writes OS image, injects first-boot scripts and config
- `eidos-setup.sh` — first-boot: creates user, installs dependencies, clones repo, builds llama.cpp, enables services
- `config.production.toml` — production configuration template
- `eidos.env` / `eidos.env.example` — environment variables for deployment

### Quick Deploy (from dev machine)

```bash
# Deploy code changes to running Pi
scp eidos.py context.py memory.py prompts.py ei@<pi-tailscale-ip>:/home/ei/kairos/
ssh ei@<pi-tailscale-ip> "sudo systemctl restart eidos && sudo systemctl restart dashboard"
```

## Configuration

All configuration lives in `config.toml`, loaded by `config.py` into a dataclass. Key sections:

```toml
[llm]
url = "http://127.0.0.1:8080"      # llama.cpp endpoint
max_tokens = 512                     # per-tick generation budget
temperature = 0.6
request_timeout_s = 1800             # 30 min timeout for slow hardware

[tick]
interval_s = 5                       # sleep between ticks

[compaction]
token_threshold = 8000               # compact when obs exceed this
tick_threshold = 20                   # or after this many ticks
max_tokens = 512                     # LLM budget for compaction

[context]
max_total_chars = 6500               # hard ceiling for entire prompt
goal_max_chars = 2000
obs_max_chars = 4000
memory_max_chars = 4000

[planning]
model_path = "/home/ei/models/qwen3.5-4b-q4.gguf"  # larger model for plan_goal
context_size = 2048
reasoning_budget = 512

[safety]
protected_patterns = [...]           # regex list of blocked commands
disk_min_gb = 0.5
ram_max_pct = 85

[persona]
enabled = true

[knowledge]
enabled = true
recall_top_k = 3
```

Environment variable overrides: `EIDOS_LLM_URL`, `EIDOS_WORKSPACE`, etc.

## Runtime Workspace

Created automatically at runtime in the configured workspace directory:

```
workspace/
├── goal.md                  # Current objective
├── plan.md                  # Working memory (distilled by dream cycles)
├── subgoals.md              # Task checklist from plan_goal
├── observations.jsonl       # Tool execution log (cleared after compaction)
├── wal.json                 # Crash recovery state
├── heartbeat.json           # Per-tick status snapshot
├── activity.json            # Current state for dashboard
├── persona.json             # Personality persistence
├── llm_log.jsonl            # LLM interaction log
├── metrics.jsonl            # Time series metrics
├── knowledge/               # Persistent knowledge store
│   ├── index.json
│   ├── facts/
│   ├── procedures/
│   ├── errors/
│   └── reflections/
├── interventions/           # Human operator messages
├── snapshots/               # Memory snapshots (pre-compaction)
└── outputs/                 # Full tool output spillover
```

## Requirements

- Python 3.9+
- `tomli` (for Python < 3.11)
- `pyyaml`
- `rank-bm25` (for knowledge search)
- `numpy`
- Optional: `onnxruntime`, `tokenizers` (for semantic search)

## License

Private project.
