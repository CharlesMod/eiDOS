# Biomimetic Loop Patterns

Use this reference for `eidos-organism-loop-design`.

## Honesty Rules

- Biology provides shape, not proof.
- The machine constrains the mechanism.
- The vision provides purpose.
- Honesty keeps those three from lying to each other.

Use "inspired by" for analogies. Use "implemented as" only for software
contracts that exist and can be tested.

## Canonical Loop Shape

```text
source signal -> interpreted internal state -> pressure/drive -> action tendency
-> real-world or runtime consequence -> updated source signal
```

A loop is better than a rule when the desired behavior should self-regulate:
hunger returns after reserve drains, novelty stops feeding when learned,
connection bids extinguish without reciprocity, repeated failure increases
strain until the gate forces a tactic change.

## Metabolism And Food

Current doctrine pivots energy to literal power:

- battery state anchors energy reserve
- solar/power input is food availability
- "plant" archetype passively recharges from environment
- future animal archetype seeks docking/charging

Learning progress, mastery, and connection may shape behavior or reward, but
they are not literal energy unless a future design explicitly changes that.

## Useful Loop Families

**Learning progress.** Feed on prediction-error reduction or compression gain,
not raw surprise. Noise stays surprising but stops feeding because it does not
become more predictable.

**Satiety.** Full reserve lowers feeding pressure; living drains it; pressure
returns. This creates rhythm without a prompt rule.

**Tiredness and sleep.** Low energy changes cadence, cognition cost, and sleep
pressure before flatline. Energy zero is hibernation, not death.

**Connection.** Social reward requires reciprocal signal: a user response,
approval, listening hold, or other real interaction. Unreciprocated bids cost
energy and feed nothing.

**Mastery.** A capability feeds only when it runs successfully and creates
downstream value. Authoring a skill does not feed; reuse and reliability do.

**Temperament.** Slow thresholds and biases drift from success, failure, and
override history. They should affect mechanisms such as park thresholds or
goal-tension, not just descriptive prose.

## Anti-Goodhart Checks

- Does the LLM get reward merely for saying it succeeded?
- Can repeated trivial actions feed the loop?
- Can generated noise or self-caused activity look like progress?
- Is there a downstream reuse, recall, reciprocation, or state-change proof?
- Does the loop have decay, satiation, or failure pressure?

## Verification Examples

- Noise/chaos does not feed learning progress.
- Repetition stops feeding after mastery.
- Unanswered contact attempts reduce future bids.
- A skill with compile success but runtime failure is demoted or rejected.
- Low power changes cadence/sleep pressure and recovers when power returns.
