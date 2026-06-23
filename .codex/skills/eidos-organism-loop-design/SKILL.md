---
name: eidos-organism-loop-design
description: "Use when designing or reviewing eiDOS feedback loops for drives, metabolism, power, hunger, sleep, learning progress, curiosity, connection, mastery, reward, temperament, or anti-Goodhart evidence. Use to replace prose rules with observable self-regulating loops; do not use merely because a design is biology-inspired."
---

# eiDOS Organism Loop Design

Use this skill when an eiDOS feature is meant to create organism-like behavior
through measured pressure, feedback, and self-regulation.

## Trigger Signals

- Designing drives, appetites, hunger, energy, sleep, power, metabolism, mood,
  temperament, curiosity, learning-progress, connection, mastery, or reward.
- Replacing prompt instructions with a measured feedback loop.
- Deciding how a biology-inspired idea becomes a buildable runtime mechanism.
- Checking whether a reward/drive can be gamed by narration, noise, or trivial
  self-generated action.
- Adding dashboard or behind-the-curtain readouts for organism state.

## Do Not Trigger

- Any code loop that is not an organism feedback loop.
- Creature/avatar work that does not touch drives, state, reward, or truth
  rendering.
- General biology metaphors without a concrete feedback mechanism.

## Required Workflow

1. Read `AGENTS.md` and the active ExecPlan.
2. Load `references/organism-loop-patterns.md`.
3. Name the behavior in plain engineering terms.
4. Define the loop: sensed signal, internal state, pressure, action tendency,
   feedback, relief/reinforcement.
5. Define the anti-Goodhart check.
6. Mark the mechanism as buildable now, degraded stand-in, learned later, or
   too speculative.
7. Verify both positive behavior and at least one self-correction/failure case.

## References To Load

- `references/organism-loop-patterns.md`: metabolism, food/power, mastery,
  connection, temperament, and anti-Goodhart loop patterns.

## Hard Lines

- Do not reward self-declared success.
- Do not treat abstract learning/connection as literal energy after the power
  pivot unless a later decision explicitly changes it.
- Do not encode personality as prompt prose when a threshold, state, or pressure
  should own it.
- Do not hide uncertainty behind biological names.
