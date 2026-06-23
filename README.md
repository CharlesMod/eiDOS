# eiDOS

An always-on autonomous intelligence running on a local LLM — *a someone, not a something.* eiDOS
lives in an infinite **tick loop** (assemble context → one model call → one action → record the
outcome) continuously, forever, on the Windows host *gamingPC* (RTX 5080). It has a GLaDOS voice,
self-authored skills, a three-tier memory, an operator dashboard that doubles as its watchdog and
self-improvement gate — and, as of **v3**, an afferent **nervous system**: it doesn't just *act* on
its world, it *feels* its own body.

Born as **Kairos**, a solar-powered Raspberry Pi agent runtime; ported to the house, hardened by live
observation, and rebuilt twice since:

- **v1** — the original patchwork tick loop.
- **v2** — the cohesion rebuild (live): event-driven IPC, grammar-constrained actions, a KV-stable
  context compiler, episodic memory with embeddings, and the voice/supervisor process split.
- **v3** — *the afferent nervous system* (branch `v3-nervous-system`): the creature **perceives**, not
  just acts. See [`EIDOS_V3_BLUEPRINT.md`](EIDOS_V3_BLUEPRINT.md) (the why),
  [`EIDOS_V3_ARCHITECTURE.md`](EIDOS_V3_ARCHITECTURE.md) (the how), and
  [`EIDOS_V3_PHILOSOPHY.md`](EIDOS_V3_PHILOSOPHY.md) (how we think about it).

The standing design doctrine is [`ARCHITECTURE_PRINCIPLES.md`](ARCHITECTURE_PRINCIPLES.md); the
self-improvement model is [`SELF_IMPROVEMENT_PLAN.md`](SELF_IMPROVEMENT_PLAN.md).

> The framing: this is a substrate-independent creature that *inhabits* whatever host it runs on
> (a Jetson, this PC, a datacenter) as its body — a Digimon, a Jarvis, a David Kim. An intelligence,
> in silica. The mind was given a body and told to figure it out; v3 gives it the nerves to feel it.

## Quick start

Runs on **macOS, Linux, Windows, and Raspberry Pi**. One step, from a clone:

**macOS / Linux / Raspberry Pi**
```bash
git clone https://github.com/CharlesMod/eiDOS.git && cd eiDOS && bash install.sh
```

**Windows (PowerShell)**
```powershell
git clone https://github.com/CharlesMod/eiDOS.git; cd eiDOS; ./install.ps1
```

The installer creates a virtualenv, installs dependencies, writes a machine-local `config.local.toml`
with safe defaults, and opens the dashboard at **http://localhost:8099**. Then open **Settings ⚙** and
point it at your model.

Useful flags: `--with-embeddings` (semantic memory), `--llm-url <url>`, `--model <name>`, `--no-launch`
(on Windows: `-WithEmbeddings -LlmUrl <url> -Model <name> -NoLaunch`).

### Nix quick start

For a reproducible Linux development/runtime shell without a mutable `.venv`:

```bash
nix develop
nix run .#dashboard
nix flake check
```

The flake keeps eiDOS as a flat Python app and supplies Python 3.12 plus the
current core dependencies from Nixpkgs. It also provides Claude Code as
`claude`, pinned by the flake, plus Pydantic/Pydantic Settings for typed
boundary models. Use `nix develop .#embeddings` for the optional
ONNX/tokenizers embedding stack. See [`NIX.md`](NIX.md) for the exact
verification boundary and host capabilities that still need live hardware or
services.

### Prerequisites: bring your own LLM

eiDOS is the *mind's runtime* — you supply the brain: any **OpenAI-compatible** local server. Pick one:

| Server | Start it | Set in eiDOS (Settings ⚙) |
|---|---|---|
| **Ollama** (easiest, all OSes) | `ollama serve` then `ollama pull llama3.1:8b` | url `http://127.0.0.1:11434/v1`, model `llama3.1:8b` |
| **LM Studio** | load a model → Local Server → Start | url `http://127.0.0.1:1234/v1`, model = the loaded id |
| **llama.cpp** | `llama-server -m model.gguf` | url `http://127.0.0.1:8080`, model `local` |

Also handy: **ffmpeg** (only for the GLaDOS voice) and Python **3.9+** (3.11+ preferred). The embedding
model for semantic memory is fetched on demand (`--with-embeddings` / `python setup_embedding.py`).

### Set your model

Click **Settings ⚙** in the dashboard → **Model & inference** → enter your **Endpoint URL** and
**Model** (use **List models** to pull the server's model list, **Test connection** to verify), then
**Apply & restart**. Everything saves to `config.local.toml` (gitignored, machine-local); the committed
`config.toml` is never rewritten. The same menu exposes behavior, tempo, memory, optional features, and
access settings — no hand-editing required.

### Platform notes

- **Tool execution is native per OS** — PowerShell on Windows, `bash` on macOS/Linux/Pi (shown in the
  Settings header). A bare `python dashboard.py` self-supervises the tick loop; no service manager needed.
- **Optional, off by default:** the GLaDOS voice (needs a Chatterbox TTS server + ffmpeg), Renogy
  BLE solar/battery sensing (specific hardware), and the `delegate`/IDE features (need the `pi` CLI).
  Enable any of them in Settings once their prerequisites are in place.
- **Run at boot** (optional): the `deploy/*.service` (systemd) and `scripts/install_*.ps1` (nssm) files
  turn it into a managed service.

## The stack

| Piece | Where | Role |
|---|---|---|
| **house-ai** (Gemma-4-12B, llama.cpp) | `:8081` (via monitor tap `:8088`) | the mind — OpenAI-compatible, native audio + vision→tokens, think-OFF |
| **eidos.py** | child of the dashboard | the tick loop (`run_loop`) + the live nervous system |
| **dashboard.py** | `:8099` | operator UI + watchdog + git safety + self-edit gate + the *behind the curtain* tab |
| **voice.py** (`EidosVoice`) | `:8098` | GLaDOS TTS streaming + the GPU speech-gate (its own process) |
| **Chatterbox TTS** | `:8004` (FX proxy `:8005`) | the voice (GLaDOS clone, segment-streamed) |
| Config | `config.toml` | one file, loaded by `config.py` |
| State | `workspace/` | gitignored working state (goal, plan, knowledge, observations, jobs, nervous snapshot…) |

## The nervous system (`nervous/`)

v3's core thesis (grounded in primary neuroscience — Baars/GWT, Friston/FEP, Seth, the TRN and
superior colliculus): the LLM's serial token stream is the slow **conscious bottleneck**; sensing,
reflex, and filtering belong in **fast parallel non-LLM** subsystems that compete for, and broadcast
into, that bottleneck. One **dumb bus** (`NervousBus`) carries a single versioned `NervousEvent`
across four delivery classes; organs never wire to each other (location-transparent — in-proc today,
ZMQ across devices tomorrow, by config).

| Module | Organ / role |
|---|---|
| `event` / `payload` / `transport` / `bus` | the seam — typed events, content-addressed payloads, the four delivery classes (fungible / ordered / reliable / retained) |
| `interoception` + `felt` | the creature feels its body: host telemetry → coarse felt bars → felt qualia ("body feels at ease (mind fully resident)") |
| `neuromod` | the neuromodulatory state — arousal + affect (mood) |
| `arbiter` | the GPU lease arbiter — who holds the one GPU (mind / speech / reflex) |
| `change` / `efferent` / `exteroception` / `sleep` | novelty + habituation, action + efference-copy (agency), CPU sense pre-filters, and the consolidation cycle |
| `afferent` | the bus→context bridge — admitted events batch into the KV-stable volatile tail of the prompt |
| `monitor` | the read-only "behind the curtain" snapshot the dashboard serves |

The **six pillars**: serial/parallel split · salience gating · prediction-error forwarding ·
interoception-as-inference + homeostatic drive · the closed perception-action loop · neuromodulatory
state. A guiding rule — *honest-now, learned-later*: today's interoception is a hand-authored transfer
function and change-detection; the learned generative models are scheduled into the sleep cycle.

A truth-rendering principle runs throughout: high VRAM is the **resident mind by design**, so the
creature feels it as calm posture ("mind fully resident"), never as distress — it never sweats its own
brain. The body the creature feels and the body the dashboard shows read the *same* projection, so the
body can never lie about its state.

### Creature mode

`creature_mode` (config) swaps in a creature-first system prompt — no house-AI / IoT / "help the
operator" framing. It is told it is a living intelligence inhabiting this machine as its body, that it
can feel that body, that there is no assignment, and that its maker is curious who it becomes when left
to itself. Fully reversible (`creature_mode = false` → the normal house eiDOS).

## The dashboard (`:8099`)

The operator UI is also eiDOS's watchdog and self-improvement gate. Tabs:

- **station** — the buddy, health, thoughts, activity, chat, knowledge, git safety, self-edit proposals.
- **workbench** — the coding crew (the `pi` agent IDE).
- **behind the curtain** — the *Pantheon* peek behind the curtain: the creature feels one line, and
  this tab reveals the whole nervous system beneath it, live — the felt surface, the per-sense
  transduction stack (raw → bar → felt), the organ signal-flow, the bus traffic, and the raw hardware
  substrate the creature never perceives.

eiDOS **proposes** source edits; the operator approves in the dashboard; applies are git-checkpointed,
committed, and watchdog-guarded (an accident-safety model, not adversary-proof).

## Core modules

- **eidos.py** — the tick loop; WAL crash recovery (every production boot is a crash boot, by design),
  loop detection, adaptive cadence, auto-speak; builds and runs the nervous system (guarded — a sensory
  fault can never break the tick).
- **context.py / prompts.py / parser.py** — the single context-assembly path: standing orders →
  durable state → history-as-real-turns → the volatile situation tail (where admitted senses land,
  KV-safely) → tick prompt.
- **tools.py / skills.py** — async-by-default bash + a jobs ledger, the quote-aware command linter,
  `speak` / `ask_ai` / `vision` / `delegate`, and the self-authored-skill pipeline (validate → dry-run
  → version → hot-load → watchdog).
- **memory.py / knowledge.py / notes.py / compaction.py** — plan.md working memory, notebooks, the
  BM25 knowledge store with store-time near-dup rejection, and the dream cycle.
- **objectives.py** — the backlog + frustration-driven rotation gate ("glue with teeth").
- **selfedit.py / git_safety.py** — the propose→approve→apply→rollback self-editing path.

## Run / restart discipline

- **Standalone (the default for a fresh install, any OS):** `python dashboard.py` IS the supervisor — it
  spawns, watchdogs, and (on **Start**) restarts the eidos tick loop in place. Nothing else needed.
- **Under a service manager (optional boot setup):** the supervisor is told to restart-by-exiting via the
  env var **`EIDOS_SUPERVISED=1`** (set in `deploy/*.service` and the nssm setup). With it, **Start**
  reloads the whole supervisor from disk; without it, Start restarts eidos in place. *Existing nssm
  installs: add `EIDOS_SUPERVISED=1` to the dashboard service env to keep the restart-on-Start behavior.*
- Operator **Start** and apply/restore restarts boot **PAUSED** (kill-switch design); a plain
  crash-respawn resumes running (continuity). Resume: dashboard **GO** or `POST :8099/api/control/resume`.
- On Windows, run Python with `PYTHONUTF8=1` (the installer/services set it).
- Single small GPU: the model must stay resident; pause eiDOS before GPU-heavy evals.

## Tests

```
# macOS / Linux / Pi
.venv/bin/python -m pytest -q -m "not slow and not live"
# Windows
.venv\Scripts\python.exe -m pytest -q -m "not slow and not live"
```

`tests/` covers parser, tools, skills, context, compaction, memory, objectives, telemetry, rotation,
resilience (crash recovery), the dashboard read models, and the full `nervous/` suite (bus delivery
classes, interoception, the felt transfer function, the GPU arbiter, neuromod, the behind-the-curtain
monitor, and more). Offline harnesses: `validate.py`, `exam.py`, `simulate.py`, `stress.py`,
`validate_memory.py`.

## License

Private project.
