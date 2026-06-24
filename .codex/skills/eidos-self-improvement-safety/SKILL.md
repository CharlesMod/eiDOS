---
name: eidos-self-improvement-safety
description: "Use when designing, modifying, or reviewing eiDOS self-improvement, proposal staging, dashboard approval, git checkpoint/rollback, protected paths, dynamic skill activation, agent-writable files, or any source/runtime mutation path. Preserve the boundary: eiDOS proposes, the operator-controlled dashboard applies."
---

# eiDOS Self-Improvement Safety

Use this skill before changing any path where eiDOS, authored skills, the
dashboard, or operator approval can mutate source, runtime state, git state,
proposal state, approval state, or live service lifecycle.

## Trigger Signals

- Editing or reviewing `selfedit.py`, `git_safety.py`, `safety.py`,
  `atomicio.py`, proposal manifests, protected paths, dashboard apply/restore,
  watchdog rollback, or control endpoints.
- Adding agent-writable files that influence source, git, runtime, approvals,
  or restarts.
- Changing dynamic skill activation, promotion, demotion, quarantine, or
  authored-skill safety.
- Designing a restart, health-probe, rollback, restore, or operator approval
  flow.

## Do Not Trigger

- General improvement backlog work with no proposal/apply/mutation path.
- Normal code-quality cleanup that cannot affect source mutation, runtime
  state, git state, or live process lifecycle.
- Codex repo-local skill scaffolds unless they affect eiDOS runtime-authored
  skills or source mutation.

## Required Workflow

1. Read `AGENTS.md`, `CLAUDE.md`, and the active ExecPlan.
2. Load `references/self-improvement-boundary.md`.
3. Identify each actor and authority file/API.
4. Preserve the boundary: eiDOS proposes; dashboard applies.
5. Re-validate paths, protected files, stale bases, syntax/import health, and
   unrelated dirty-tree custody at the privileged step.
6. Add refusal-path tests; happy-path tests are not enough.
7. For restart or rollback, state exactly which process is affected and how the
   operator recovers.

## References To Load

- `references/self-improvement-boundary.md`: proposal/apply authority,
  protected surfaces, apply requirements, runtime skill promotion, and refusal
  tests.

## Hard Lines

- Do not add direct source writes to agent-facing tools.
- Do not trust proposal JSON status alone.
- Do not use `git reset --hard` as a safety mechanism.
- Do not casually restart live services in tests.
- Do not call accident-safety adversary-proof.
