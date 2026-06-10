# eiDOS

An always-on autonomous house AI running on a local LLM. eiDOS lives in an infinite
**tick loop** — assemble context → one model call → one tool call → record the outcome —
continuously, forever, on the Windows host *gamingPC* (RTX 5080), with a GLaDOS voice,
self-authored skills, a three-tier memory, and an operator dashboard that doubles as its
watchdog and self-improvement gate.

Born as **Kairos**, a solar-powered Raspberry Pi agent runtime; ported to the house and
hardened by live observation. The v1 → v2 rebuild (branch `eidos-v2`) is underway — see
[`EIDOS_V2_BLUEPRINT.md`](EIDOS_V2_BLUEPRINT.md) for the architecture and
[`V2_PROGRESS.md`](V2_PROGRESS.md) for status. The design doctrine is
[`BIBLE.md`](BIBLE.md); standing engineering preferences are
[`ARCHITECTURE_PRINCIPLES.md`](ARCHITECTURE_PRINCIPLES.md).

## The stack

| Piece | Where | Role |
|---|---|---|
| **house-ai** (Gemma-4-12B, llama.cpp) | `:8081` (via monitor tap `:8088`) | the mind — OpenAI-compatible, think-OFF |
| **eidos.py** | child of the dashboard | the tick loop (`run_loop`) |
| **dashboard.py** | `:8099` | operator UI + watchdog + git safety + self-edit gate + GLaDOS TTS streaming + GPU speech-gate |
| **Chatterbox TTS** | `:8004` (FX proxy `:8005`) | the voice (GLaDOS clone, bf16, segment-streamed) |
| Config | `config.toml` | one file, loaded by `config.py` |
| State | `workspace/` | gitignored working state (goal, plan, knowledge, observations, jobs…) |

## Core modules

- **eidos.py** — tick loop, WAL crash recovery (every production boot is a crash boot —
  by design), loop detection, adaptive cadence, auto-speak.
- **context.py / prompts.py / parser.py** — the briefing context (single assembly path):
  standing orders → durable state blob (focus / backlog / self-guide / mission / plan /
  world-model / notebook / presence / chat) → history-as-real-turns → salience block →
  tick prompt.
- **tools.py / skills.py** — async-by-default bash + jobs ledger, the quote-aware
  auto-correcting command linter, network primitives, `http_request`, `speak`, `ask_ai`,
  `vision`, and the self-authored-skill pipeline (validate → dry-run → version → hot-load
  → watchdog).
- **memory.py / knowledge.py / notes.py / compaction.py** — plan.md working memory,
  notebooks, the BM25 knowledge store with store-time near-dup rejection, and the dream
  cycle (plan rewrite + knowledge extraction in one call).
- **objectives.py** — the backlog + frustration-driven rotation gate ("glue with teeth"):
  stalled objectives are parked automatically and focus rotates; escalates to the
  operator at most once per 60 ticks.
- **selfedit.py / git_safety.py** — eiDOS *proposes* source edits; the operator approves
  in the dashboard; applies are checkpointed, committed, and watchdog-guarded
  (accident-safety model — see `SELF_IMPROVEMENT_PLAN.md`).

## Run / restart discipline

- The dashboard is the `EidosDashboard` nssm service; reload code with
  `Restart-Service EidosDashboard`. It spawns eidos as a child — **never
  `taskkill /T` the dashboard.**
- Restart eidos alone: `taskkill /PID <eidos-pid> /F`; the watchdog respawns it on fresh
  code. Operator start and apply/restore restarts boot **PAUSED** (kill-switch design);
  a plain crash-respawn resumes running (continuity).
- Resume: dashboard **GO** or `POST :8099/api/control/resume`.
- Always run Python with `PYTHONUTF8=1`.
- One GPU, 16 GB: house-ai (~15.7 GB) must be resident; pause eiDOS and stop the
  service before running evals.

## Tests

```
PYTHONUTF8=1 .venv\Scripts\python.exe -m pytest -q
```

`tests/` covers parser, tools, skills, context, compaction, memory, objectives,
telemetry, rotation, resilience (crash recovery), and the dashboard read models.
Offline harnesses: `validate.py` (staged live-LLM validation), `exam.py` (graded tasks),
`simulate.py` (multi-tick sandbox), `stress.py` (adversarial/prompt-injection),
`validate_memory.py` (memory pipeline stages).

## License

Private project.
