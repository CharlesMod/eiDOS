---
name: eidos-nervous-system-design
description: "Use when designing, modifying, or reviewing eiDOS v3 nervous-system architecture or code: NervousEvent contracts, bus delivery classes, organ boundaries, afferent/efferent/admission flows, interoception, proprioception, modulation, sleep, location-transparent sensors/effectors, and invariant tests."
---

# eiDOS Nervous System Design

Use this skill for V3 nervous-system seam work: organs, the bus, event
contracts, delivery semantics, context admission, senses, action loops, and the
truthful body/felt-state projection.

## Trigger Signals

- Touching `nervous/` semantics or tests.
- Designing or reviewing `NervousEvent` fields, delivery classes, retained
  state, capability events, or payload references.
- Adding or changing an organ, receptor, pre-filter, salience gate,
  interoceptive/proprioceptive path, neuromodulatory state, sleep/consolidation,
  power sensor, reflex arc, or efference-copy behavior.
- Deciding where a sense/action behavior belongs in the V3 graph.
- Testing degradation when a sense, organ, process, or device vanishes.

## Do Not Trigger

- Generic event-driven work outside the V3 nervous-system seam.
- Ordinary code cleanup under `nervous/` that does not alter contracts,
  semantics, ownership, or degradation behavior.
- Pure biology discussion with no software boundary decision.

## Required Workflow

1. Read `AGENTS.md` and the active ExecPlan.
2. Load `references/v3-boundaries.md`.
3. State the feeling or competence this mechanism gives eiDOS.
4. Place the behavior in the allowed graph before editing.
5. Define the seam: event kind, modality, delivery class, payload, owner, and
   degradation behavior.
6. Keep bus policy-free; put selection, scoring, and context assembly in organs
   or glue.
7. Verify with contract/degradation tests before live smokes.

## References To Load

- `references/v3-boundaries.md`: invariants, delivery classes, LLM-reality
  constraints, and organ-placement hints.

## Hard Lines

- Do not stream raw modality data into the LLM.
- Do not add hidden organ-to-organ back-channels.
- Do not put salience/admission policy in the bus.
- Do not let two writers compute competing derived state.
- Do not claim biology proves an engineering choice.
