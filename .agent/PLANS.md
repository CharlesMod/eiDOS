# eiDOS Execution Plans (ExecPlans)

This document defines the ExecPlan standard for eiDOS. An ExecPlan is a durable,
self-contained execution specification that a coding agent or human maintainer
can follow to deliver a working, observable change.

Use this file when authoring, revising, or implementing any ExecPlan in this
repository. Active plans live under `.agent/execplans/`.

The canonical file name is uppercase `.agent/PLANS.md`, matching the local
workspace convention. Informal references to `plans.md` mean this file unless a
future repository change explicitly introduces another path.

## How To Use ExecPlans

When authoring an ExecPlan, read this entire file first. Start from the
skeleton below, then fill it in after inspecting the actual repository files
and commands involved. Do not write from memory when the current tree can answer
the question.

When implementing an ExecPlan, keep the plan open as the primary record of
progress. Do not ask the operator for routine "next steps" after every
milestone. Complete the next milestone, update the plan with evidence, and
continue until the plan is done or a true blocker requires operator input.

Milestones are ordered. Finish and verify Milestone N before starting Milestone
N+1 unless the plan explicitly says a spike may run in parallel. If you discover
that the milestone order is wrong, update the `Decision Log` and revise the
plan before proceeding.

An ExecPlan is not a proposal memo. It is a working document. It should become
more accurate as implementation proceeds.

## When An ExecPlan Is Required

Use an ExecPlan for:

- multi-step features or refactors
- dashboard, watchdog, voice, or process lifecycle changes
- self-editing, git safety, rollback, protected path, or proposal workflow
  changes
- durable state migrations or changes to `workspace/` formats
- nervous system semantics, event bus behavior, organ coordination, or monitor
  projection changes
- runtime, installer, Nix, dependency, or environment changes
- security, authentication, local-network, credential, or device-control
  changes
- changes that need live LLM, live dashboard, voice, BLE, camera, or other
  hardware evidence
- work likely to span more than one session

An ExecPlan is optional for a narrow typo fix, a single focused unit test, or a
small documentation edit that does not change behavior.

If in doubt, write a short ExecPlan. A short plan is better than a large change
whose intent and evidence live only in chat history.

## Non-Negotiable Requirements

Every ExecPlan must be self-contained. Assume the next reader has the current
working tree and the single plan file, but no memory of earlier chats, branches,
or private context.

Every ExecPlan must explain the user-visible or operator-visible purpose. If the
change is internal, explain how its effect can still be observed through tests,
logs, dashboard behavior, saved state, or a controlled smoke.

Every ExecPlan must define repo-specific terms the first time it uses them.
Examples: tick loop, dashboard supervisor, watchdog, proposal, last good,
nervous event, retained event, felt state, workspace state, live validation.

Every ExecPlan must name concrete files and commands. Avoid "wire it up" unless
the plan immediately states where and how.

Every ExecPlan must maintain these living sections:

- `Progress`
- `Surprises & Discoveries`
- `Decision Log`
- `Outcomes & Retrospective`

Every ExecPlan must include validation. A change is not done because code was
edited; it is done when the plan's observable acceptance criteria have been
met, or when a documented blocker explains why they cannot be met.

## File Location And Naming

Store active plans under `.agent/execplans/`.

Use a name that includes a short topic and date:

    .agent/execplans/dashboard-auth-20260623.md
    .agent/execplans/nervous-monitor-retained-events-20260623.md
    .agent/execplans/runtime-nix-shell-20260623.md

Keep the plan in the same branch as the work it describes. If the branch is
rebased or split, update the plan so it still matches the branch.

## Formatting

When an ExecPlan file contains only the plan, omit outer triple backticks.

Use Markdown headings and plain prose. Prefer sentences over dense tables.
Checklists are required in the `Progress` section and should use `- [ ]` or
`- [x]`.

Do not nest fenced code blocks inside an ExecPlan if the plan itself is being
quoted inside another fenced block. In ordinary `.md` plan files, fenced command
blocks are allowed, but keep them short.

Use repository-relative paths such as `dashboard.py`, `nervous/bus.py`, and
`tests/test_dashboard.py`.

## Repository Grounding

Before writing concrete steps, inspect the relevant current files. At minimum:

- For runtime shape and high-level modules, read `README.md`.
- For live service and operator workflow, read `CLAUDE.md`.
- For design preferences, read `ARCHITECTURE_PRINCIPLES.md`.
- For self-editing or git safety, read `SELF_IMPROVEMENT_PLAN.md` plus the
  touched modules.
- For v3 nervous system work, read `EIDOS_V3_ARCHITECTURE.md` and at least one
  existing `nervous/` test for the subsystem being changed.
- For tool or capability changes, read `OPERATING_MANUAL.md`,
  `eidos_capabilities.md`, `tools.py`, and relevant tests.
- For config changes, read `config.py`, `config.toml`, `config.template.toml`,
  `settings_schema.py`, and `tests/test_settings.py` or `tests/test_config.py`
  if present on the branch.

Model examples on current code. Do not invent module names, commands, service
ports, or test harnesses from another repository.

## Milestones

Milestones should tell a story: what exists now, what will exist after this
milestone, what files change, how to run it, and what should be observed.

Each milestone must be independently verifiable. A later milestone may build on
an earlier one, but each completed milestone should leave the repo in a coherent
state.

Good milestone examples:

- "Add the typed parser boundary and tests while leaving callers untouched."
- "Wire the dashboard endpoint to the new boundary and prove old valid payloads
  still work."
- "Run a throwaway-port dashboard smoke against a temporary workspace."

Weak milestone examples:

- "Implement feature."
- "Fix tests."
- "Clean up."

If a spike is needed, label it as a spike. State what question it answers, where
its code or notes live, and whether it will be deleted, promoted, or kept.

## Validation Standards

Prefer offline tests first:

    python -m pytest -q -m "not slow and not live"

For targeted work, run focused tests first and then broaden according to risk:

    python -m pytest -q tests/test_tools.py
    python -m pytest -q tests/test_dashboard.py tests/test_dashboard_data.py
    python -m pytest -q tests/test_nervous_bus.py tests/test_nervous_monitor.py

When a branch has Nix:

    nix develop --command python -m pytest -q -m "not slow and not live"
    nix flake check --print-build-logs

For dashboard or service smokes, use a temporary workspace and throwaway port.
State how the process was stopped afterward.

For live LLM validation, state:

- endpoint URL or service name, if safe to disclose
- whether the model was already running
- exact command
- expected output
- actual output summary
- whether eiDOS was paused to protect GPU residency

For live devices or LAN scans, state why live access was required and what
authorization was assumed.

If a test cannot run because a dependency or live service is unavailable, record
that as a verification gap with a concrete next action. Do not silently replace
it with a weaker check.

## Safety And Idempotence

Plans must be safe to resume.

State whether commands are idempotent. If a command writes state, explain how to
repeat it safely or how to clean up after it.

Avoid destructive operations. Do not use `git reset --hard`, broad deletion, or
process-tree kills as routine steps. If a destructive operation is truly needed,
the plan must explain why, what is backed up, and how to verify the target path
before running it.

Use `/tmp` for disposable work. Do not leave generated files, pycache, logs,
downloaded models, throwaway workspaces, or scratch scripts in the repository
unless the plan names them as durable artifacts.

When touching live services, include a live-service safety section. State what
will be restarted, why, how the operator will observe it, and how to roll back.

## Required Sections

Use this structure unless the work is tiny and a shorter equivalent is clearer.
Do not remove the living sections.

    # <Short, action-oriented title>

    This ExecPlan is a living document. Maintain it according to
    `.agent/PLANS.md`.

    ## Purpose / Big Picture

    Explain what this changes from the operator or user point of view. Name the
    behavior that will be visible when the work is done.

    ## Progress

    - [x] (2026-06-23 14:00Z) Example completed step with evidence.
    - [ ] Example remaining step.

    Every stopping point must update this list. Use UTC timestamps.

    ## Surprises & Discoveries

    - Observation: ...
      Evidence: ...

    Record unexpected behavior, compatibility discoveries, failures, or useful
    repo facts.

    ## Decision Log

    - Decision: ...
      Rationale: ...
      Date/Author: 2026-06-23 / Codex

    Record every meaningful implementation decision and scope change.

    ## Outcomes & Retrospective

    Summarize what was achieved, what remains, and what should be handled by a
    follow-up plan.

    ## Context And Orientation

    Explain the relevant repo shape for a novice. Name files and modules. Define
    any term that is not ordinary English.

    ## Constraints

    State boundaries, non-goals, live-service limits, state/secret limits, and
    compatibility promises.

    ## Plan Of Work

    Describe the ordered milestones in prose. For each milestone, name the files
    to inspect or edit and the observable result.

    ## Concrete Steps

    Give exact commands and working directories. Update this section as commands
    change during implementation.

    ## Validation And Acceptance

    State the tests, smokes, and observations that prove the behavior. Include
    expected outputs or success criteria.

    ## Idempotence And Recovery

    Explain how to rerun steps, recover from partial completion, and clean up
    temporary state.

    ## Artifacts And Notes

    Include short evidence snippets, important diffs, ports used, temporary
    paths, or links to durable artifacts.

    ## Interfaces And Dependencies

    Name any new or changed public interfaces, config keys, files, endpoints,
    tools, tests, dependencies, or runtime assumptions.

## eiDOS-Specific Planning Guidance

### Tick Loop Work

The tick loop is the repeated cycle in `eidos.py` that assembles context, calls
the model, parses one action, executes it, and records the result. Plans that
touch the tick loop must state how they avoid blocking it on slow work.

Prefer:

- async tool dispatch
- bounded event waits
- state read/write helpers with clear failure behavior
- tests around crash recovery and observation logging

Avoid:

- sleeps as synchronization
- foreground network or device calls inside the loop
- broad exception swallowing that hides a broken tick

### Dashboard And Watchdog Work

The dashboard is both operator UI and supervisor. Plans that touch it must state
which endpoints are read-only, which endpoints mutate state, and how auth or
operator approval is preserved.

When adding or changing endpoints, include refusal tests for malformed payloads
and unauthorized or unsafe paths where relevant.

When changing restart behavior, describe how the child `eidos.py` process, the
dashboard process, and the voice process are affected.

### Self-Improvement Work

Self-improvement is accident-safety, not adversary-proofing. The core boundary
is still important: eiDOS proposes, dashboard applies.

Plans must state:

- which actor can write each file or marker
- how proposal identity is preserved
- which files are protected
- what happens on stale proposals
- how rollback or restore is tested
- how unrelated dirty worktrees are protected

### Nervous System Work

The `nervous/` subsystem models the body-sensing/event side of eiDOS. Plans
must state which organ or event path is changing and how the dashboard monitor
or context bridge will show it.

Preserve the dumb-bus design: organs should communicate through events rather
than direct cross-wiring unless a plan explicitly justifies an exception.

### Runtime And Dependency Work

This repository may be on a branch with only venv/install scripts, or on a
branch with Nix support. Plans must inspect the branch before declaring the
runtime authority.

If adding dependencies, update all active dependency surfaces on that branch:
`requirements.txt`, installer scripts, Nix files if present, and any docs that
tell users how to install or test.

### Documentation And Self-Knowledge Work

When the agent gains a new capability or an existing one changes, update the
places eiDOS reads or the operator uses:

- `eidos_capabilities.md`
- `OPERATING_MANUAL.md`
- `prompts.py` condensed briefing, when the change affects standing behavior
- `README.md` or `CLAUDE.md`, when runtime or operator workflow changes

## Revision Notes

- 2026-06-23: Added the initial eiDOS ExecPlan standard, adapted from the
  workspace planning pattern and tuned for eiDOS's lighter governance, flat
  Python layout, live dashboard/voice services, self-improvement boundary, and
  nervous-system architecture.
