---
name: eidos-tool-capability-design
description: "Use when designing, modifying, or reviewing eiDOS tools, skills, atoms, delegates, operating-manual capability docs, typed tool failures, action validation, skill promotion/demotion, or the positive capability surface in tools.py, skills.py, skill_atoms.py, delegate.py, OPERATING_MANUAL.md, or eidos_capabilities.md."
---

# eiDOS Tool Capability Design

Use this skill for the positive design of what eiDOS can do: built-in tools,
runtime-authored skills, atom vocabulary, delegates, manuals, and capability
documentation. Use `eidos-self-improvement-safety` as well when the change can
mutate source or approval state.

## Trigger Signals

- Editing `tools.py`, `skills.py`, `skill_atoms.py`, `delegate.py`,
  `OPERATING_MANUAL.md`, or `eidos_capabilities.md`.
- Adding or changing a built-in tool, atom, runtime-authored skill API, delegate
  mode, tool schema, failure type, or action validator.
- Designing skill-language growth: atoms, compositions, promotion, demotion,
  reuse, mastery, or capability references.
- Fixing a repeated tool misuse pattern, missing capability, broken skill
  import, or hallucinated primitive.

## Do Not Trigger

- Self-edit proposal/apply safety without positive capability-surface changes;
  use `eidos-self-improvement-safety`.
- Generic Python helper refactors that do not change what eiDOS can call,
  validate, document, or learn to reuse.
- Codex skills for repository agents unless they alter eiDOS runtime tools.

## Required Workflow

1. Read `AGENTS.md`, `OPERATING_MANUAL.md`, `eidos_capabilities.md`, and the
   active ExecPlan.
2. Load `references/capability-surface.md`.
3. Classify the capability as built-in tool, atom, runtime-authored skill,
   delegate path, documentation-only capability, or validation/repair path.
4. Prefer exposing reliable primitives over asking eiDOS to rederive fragile
   commands.
5. Define typed inputs, typed failures, preconditions, repair guidance, and
   runtime documentation.
6. Update eiDOS self-knowledge when capabilities change.
7. Verify real use, not just authoring or compile success.

## References To Load

- `references/capability-surface.md`: atoms, skill-language, promotion,
  validation, manuals, and anti-wall patterns.

## Hard Lines

- Do not let authored skills depend on unavailable imports when an atom should
  expose the capability.
- Do not count skill authoring as mastery; require runtime success and reuse.
- Do not let guardrails stonewall without repair guidance.
- Do not document a capability in `OPERATING_MANUAL.md` unless the runtime
  actually provides it.
