# eiDOS Agent Notes

This file is the top-level operating guide for coding agents working in the
eiDOS repository. Treat it as binding unless a more specific `AGENTS.md` exists
closer to the files you are editing.

eiDOS is not a generic Python service. It is an always-on autonomous agent
runtime with a dashboard supervisor, a local-LLM tick loop, a voice process, a
git-backed self-improvement path, durable memory under `workspace/`, and a v3
`nervous/` subsystem. Work here should preserve that creature/runtime shape
rather than sanding it down into an ordinary web app.

## Start Here

- Read [README.md](README.md) for the current product shape, runtime map,
  dashboard, nervous system, and test commands.
- Read [CLAUDE.md](CLAUDE.md) before touching live-process behavior, service
  startup, dashboard restart, voice/TTS, self-editing, git safety, or the
  operator workflow. It contains practical runbook details that matter in this
  repo.
- Read [ARCHITECTURE_PRINCIPLES.md](ARCHITECTURE_PRINCIPLES.md) before
  designing a fix. In particular, prefer event-driven or blocking
  call-response designs over sleeps, guesses, or polling loops.
- Read [SELF_IMPROVEMENT_PLAN.md](SELF_IMPROVEMENT_PLAN.md) before changing
  `selfedit.py`, `git_safety.py`, dashboard apply/restore endpoints, proposal
  formats, protected paths, watchdog rollback, or any agent-writable code path.
- Read [EIDOS_V3_ARCHITECTURE.md](EIDOS_V3_ARCHITECTURE.md),
  [EIDOS_V3_BLUEPRINT.md](EIDOS_V3_BLUEPRINT.md), and
  [EIDOS_V3_PHILOSOPHY.md](EIDOS_V3_PHILOSOPHY.md) before changing `nervous/`
  semantics.
- Read [OPERATING_MANUAL.md](OPERATING_MANUAL.md) before adding, replacing, or
  documenting built-in tools such as `speak`, `vision`, `ask_ai`, network
  probes, device access, CPU workers, or delegate jobs. The manual exists to
  keep eiDOS from rebuilding capabilities it already has.

When a document above conflicts with this file, prefer the more specific and
newer repo document, but record the conflict in the change notes or ExecPlan.

## ExecPlans

- For complex features, significant refactors, architecture work, live-service
  workflow changes, durable state migrations, self-editing changes, Nix/runtime
  changes, or anything expected to span multiple sessions, use an ExecPlan from
  design through verification.
- The ExecPlan standard is [.agent/PLANS.md](.agent/PLANS.md).
- If an instruction informally says `plans.md`, treat it as
  `.agent/PLANS.md` in this repository unless the operator explicitly asks for
  a different path.
- Store plan documents under [.agent/execplans/](.agent/execplans/), named with
  a short slug and date, for example `.agent/execplans/dashboard-auth-20260623.md`.
- Keep ExecPlans living. Update `Progress`, `Surprises & Discoveries`,
  `Decision Log`, and `Outcomes & Retrospective` as work proceeds, not only at
  the end.
- Every ExecPlan must be self-contained enough for a fresh agent to restart from
  only the current working tree and the plan file.
- Do not use an ExecPlan as an excuse to pause after planning. Once the plan is
  adequate and authority is clear, proceed milestone by milestone and keep the
  plan updated.
- Small, local, low-risk edits do not need an ExecPlan, but the bar for "small"
  is higher in this repository when live state, self-improvement, or runtime
  process behavior is involved.

## Current Governance Posture

This repository does not currently have the heavier governance machinery used
by some sibling workspaces. Do not invent references to unavailable
`governance.run`, `submit_to_ci`, or fleet publication paths unless such tools
are added to this repo.

The default local workflow is:

1. Work on a distinct topic branch.
2. Keep edits narrowly scoped.
3. Run the relevant local tests and smoke checks.
4. Commit with a clear message when the work should be preserved.
5. Push or open a PR only when asked or when the current task explicitly
   includes publication.

If a future branch adds repository-owned automation, prefer that checked-in
automation over ad hoc commands and update this file in the same change.

## Runtime And Environment

eiDOS has two practical runtime modes:

- Current upstream installer/venv path: use `install.sh` or `install.ps1` for a
  machine setup, then run tests with the venv Python shown in [README.md](README.md).
- Nix path, when a branch contains `flake.nix`: prefer `nix develop` for local
  commands and `nix flake check --print-build-logs` for broad verification.

Do not assume a branch has Nix files just because another branch or PR does.
Check the working tree first.

For Python commands, prefer the repo's active environment:

- Linux/macOS/Pi venv: `.venv/bin/python -m pytest -q -m "not slow and not live"`
- Windows venv: `.venv\Scripts\python.exe -m pytest -q -m "not slow and not live"`
- Nix branch: `nix develop --command python -m pytest -q -m "not slow and not live"`

Use live validation scripts such as `validate.py` and `validate_memory.py` only
when the task calls for a real LLM endpoint and the operator has provided or
approved the endpoint. Most code changes should be proven with the offline test
suite first.

## Live Service Safety

Assume a real eiDOS instance may already be running on the operator's machine.
Do not casually start, stop, restart, or kill services.

- The dashboard is normally the supervisor for the tick loop.
- The voice process is separate and may share a small GPU with the local model.
- Restarting dashboard, voice, or the house model can interrupt the operator and
  the creature's state.
- Pause eiDOS before GPU-heavy evaluation or anything that may evict the local
  model from VRAM.
- Never use broad process kills such as killing a process tree unless the
  relevant runbook says to do so for the exact service.

For purely local tests, use temporary workspaces, temporary ports, and mock or
offline paths where possible. Do not point test runs at the real `workspace/`
state unless the task explicitly requires live-state investigation.

## Repository Shape

eiDOS intentionally has a flat Python module layout at the repository root.
Do not migrate to `src/`, introduce packaging churn, rename modules, or split
the app into a framework layout as part of an unrelated task.

Important areas:

- `eidos.py` owns the tick loop and builds/runs the nervous system.
- `dashboard.py` owns the operator UI, watchdog, control endpoints, and the
  apply/restore side of self-improvement.
- `voice.py`, `gpu_gate.py`, and speech-related dashboard endpoints own the
  voice process and GPU speech gate.
- `config.py` and `config.toml` define defaults; `config.local.toml` is
  machine-local and must not be committed.
- `workspace/` contains runtime memory and state; nearly all of it is ignored.
- `context.py`, `prompts.py`, `parser.py`, and `grammar.py` shape model input
  and tool-call interpretation.
- `tools.py`, `skills.py`, `skill_atoms.py`, and `delegate.py` are the agent's
  action surface.
- `memory.py`, `knowledge.py`, `notes.py`, `objectives.py`, `episodes.py`, and
  `compaction.py` own durable memory and recall.
- `selfedit.py`, `git_safety.py`, `safety.py`, and `atomicio.py` are safety
  critical; review the self-improvement docs before changing them.
- `nervous/` owns the v3 event bus, organs, felt body state, and monitor
  projection. Preserve its event-driven, body-sensing design.
- `static/dashboard.html`, `static/creature.js`, and `static/ide.js` are the
  operator-facing frontend surfaces.

## State, Secrets, And Local Artifacts

Do not commit machine-local state, credentials, generated caches, or runtime
memory.

Never commit:

- `config.local.toml`
- `preserved_nuggets.local.toml`
- `workspace/` runtime state except the intentionally tracked
  `workspace/goal.md`
- `.venv/`, `venv/`, `__pycache__/`, `*.pyc`
- downloaded embedding models under `models/`
- temporary audio, image, probe, dashboard, or camera artifacts

Use `/tmp` for disposable experiments. If a task requires a durable artifact,
put it under a documented repo path and explain who owns it, what evidence it
contains, and what the next action is.

## Self-Improvement Boundary

eiDOS proposes changes; the operator-controlled dashboard applies them. Keep
that trust boundary intact.

- Do not add agent-reachable paths that write source directly.
- Do not let proposals self-approve through files the agent can write.
- Do not weaken protected path logic, git safety, rollback markers, or
  dashboard approval checks without an explicit ExecPlan and tests.
- Treat `dashboard.py`, `git_safety.py`, `selfedit.py`, `safety.py`,
  `atomicio.py`, `config.py`, `config.toml`, `.gitignore`, `llm.py`, and
  `skills.py` as safety-sensitive. Changes there need extra scrutiny.
- Prefer reversible, git-checkpointed, testable changes. Avoid destructive git
  operations such as `git reset --hard`.

If the implementation touches self-editing, proposal formats, dashboard apply
endpoints, protected paths, or rollback behavior, add or update tests that prove
both the allowed path and the refusal path.

## Design Preferences

Use these preferences when the exact implementation is not specified:

1. Preserve the creature/runtime model. Changes should fit the tick loop,
   dashboard supervisor, durable memory, and nervous system instead of bypassing
   them.
2. Prefer event-driven or call-response mechanisms over sleeps and polling.
3. Keep slow work asynchronous. The tick loop must not block on long tools.
4. Validate untrusted inputs at boundaries. Keep internal hot paths light unless
   a boundary crosses them.
5. Prefer small, observable changes with clear tests over broad rewrites.
6. Update eiDOS's self-knowledge when capabilities change:
   [eidos_capabilities.md](eidos_capabilities.md), [OPERATING_MANUAL.md](OPERATING_MANUAL.md),
   and, when appropriate, the condensed briefing in `prompts.py`.
7. Keep docs truthful about the current branch. If a branch lacks Nix, do not
   document Nix as mandatory. If a branch lacks governance automation, do not
   document governance automation as mandatory.

## Tests And Verification

Run tests that match the risk of your change.

Common offline baseline:

    python -m pytest -q -m "not slow and not live"

Targeted examples:

    python -m pytest -q tests/test_dashboard.py tests/test_dashboard_data.py
    python -m pytest -q tests/test_tools.py tests/test_safety.py
    python -m pytest -q tests/test_memory.py tests/test_knowledge.py
    python -m pytest -q tests/test_nervous_bus.py tests/test_nervous_monitor.py

When a branch has Nix:

    nix develop --command python -m pytest -q -m "not slow and not live"
    nix flake check --print-build-logs

For dashboard runtime smokes, use a temporary workspace and a throwaway port.
Confirm the process is shut down after the smoke.

For live LLM or device tests, state the endpoint or device, why live access is
needed, and what was observed. Do not treat unavailable live hardware as a code
failure unless the task specifically requires it.

## Branch And Git Workflow

- Create a distinct branch for non-trivial work. Use `codex/<short-topic>` when
  no branch name is specified.
- Inspect `git status --short --branch` before editing. If the tree is already
  dirty, understand the existing changes before touching overlapping files.
- Never revert or overwrite work you did not create unless the user explicitly
  asks for that exact operation.
- Keep commits scoped and reviewable.
- Before final handoff, run `git status --short --branch` and report whether the
  tree is clean or what intentional changes remain.

## Subagents And Delegation

Read-only subagents are useful for parallel repository research, plan review,
or risk review. Keep their asks narrow and concrete.

Implementation subagents may be used only when their file ownership is clear and
their write set will not collide with other active work. Tell them they are not
alone in the codebase and must not revert unrelated edits.

Do not delegate live service mutation, credential handling, publication, or
destructive git operations unless the user explicitly authorized that exact
work.

## Documentation Updates

Docs move with semantics.

- Runtime or installer behavior: update [README.md](README.md), [CLAUDE.md](CLAUDE.md),
  or branch-specific runtime docs.
- Agent capability changes: update [eidos_capabilities.md](eidos_capabilities.md)
  and [OPERATING_MANUAL.md](OPERATING_MANUAL.md) when eiDOS should know the
  capability exists.
- Architecture or nervous-system changes: update the relevant v3 docs.
- Self-improvement safety changes: update [SELF_IMPROVEMENT_PLAN.md](SELF_IMPROVEMENT_PLAN.md)
  and tests.
- Multi-step work: update the active ExecPlan under `.agent/execplans/`.

Living documents should explain what changed, why, how to verify it, and what
risks remain.
