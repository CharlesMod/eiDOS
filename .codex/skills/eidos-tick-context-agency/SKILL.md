---
name: eidos-tick-context-agency
description: "Use when changing eiDOS tick-context construction or structural agency signals: current focus, world-state panels, new-since-last-tick salience, memory recall, KV-stable prompt assembly, goal tension, strain, loop-breaking, failure typing, output contracts, or prompt-to-glue migrations."
---

# eiDOS Tick Context Agency

Use this skill when changing what the LLM core sees each tick or how
deterministic glue makes eiDOS act instead of merely narrating.

## Trigger Signals

- Editing `context.py`, `prompts.py`, `parser.py`, `objectives.py`, recall,
  compaction, condition labels, goal tension, strain, or loop breakers.
- Changing current focus, world-state panels, active concerns, new-since-last
  tick salience, or recent-history rendering.
- Moving behavior from prompt prose into structural glue.
- Adjusting tool-call output contracts, parser repair paths, or grammar
  enforcement.
- Fixing rumination, write-only memory, objective drift, or buried operator
  messages.

## Do Not Trigger

- Generic use of the word "context" in code review or documentation.
- Philosophical agency discussion with no tick-context or glue change.
- Ordinary prompt wording edits that do not affect runtime decision structure.

## Required Workflow

1. Read `AGENTS.md` and the active ExecPlan.
2. Load `references/tick-context-doctrine.md`.
3. State the agency failure or capability as observable behavior.
4. Prefer deterministic glue over prompt pleading.
5. Preserve exactly one trustworthy current objective.
6. Place fresh, salient data at the decision point; keep stable material in the
   stable prefix.
7. Verify the real decision path with a ghost replay, context snapshot, parser
   test, unit test, or focused simulation.

## References To Load

- `references/tick-context-doctrine.md`: minimal context pack, glue signals,
  hard-won lessons, and verification patterns.

## Context Acid Test

Each tick, the model should be able to answer without a tool call:

- What do I know?
- What am I doing?
- What just changed?
- Am I blocked?

If not, adding more prose rules is probably the wrong fix.
