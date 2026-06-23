---
name: eidos-tradeoff-decision
description: "Use when making a non-trivial eiDOS architecture or implementation tradeoff that affects nervous-system boundaries, event contracts, substrate assumptions, biomimetic claims, self-improvement safety, live-service behavior, capability surfaces, or whether to proceed/escalate without operator input."
---

# eiDOS Tradeoff Decision

Use this skill before making an autonomous eiDOS decision with multiple viable
paths and real consequences for contracts, safety, verification, substrate
truth, organism semantics, or future agent reasoning.

## Trigger Signals

- Choosing between two or more eiDOS architecture or implementation paths.
- Deciding whether to proceed, defer, escalate, merge, split, or expand scope.
- Relaxing, adding, or reinterpreting a nervous-system, self-improvement,
  capability, state, runtime, or safety invariant.
- Deciding how to adapt biology-inspired doctrine to buildable software.
- Tooling or validation friction tempts a shortcut around an eiDOS invariant.

## Do Not Trigger

- Routine refactors inside an already chosen design.
- Naming, formatting, small test selection, or local code-shape choices.
- Generic tradeoffs outside the eiDOS repository.

## Required Workflow

1. Read `AGENTS.md` and the active ExecPlan.
2. Load `references/decision-values.md`.
3. Classify the decision as `routine`, `non-trivial`, or `escalate`.
4. For non-trivial decisions, record a compact note in the ExecPlan
   `Decision Log` before or alongside implementation.
5. Apply the eiDOS value order from the reference.
6. Choose the smallest option that preserves higher-order values; if scope
   expands, record why the expansion is bounded and necessary.

## References To Load

- `references/decision-values.md`: eiDOS value order, source-doc routing, and
  common tradeoff patterns.

## Decision Note Shape

```text
Decision:
Problem:
Options considered:
Selection rationale:
Safety checks:
Rollback plan:
Scope:
```

## Output Contract

Report:

- Tradeoff made:
- Options considered:
- Value-order justification:
- Evidence:
- Rollback plan:
