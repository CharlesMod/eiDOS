# eiDOS Decision Values

Use this reference after `eidos-tradeoff-decision` triggers.

## Value Order

1. **Explicit contracts and invariants.** Preserve typed/bounded event
   contracts, single-writer ownership, proposal/apply boundaries, protected
   paths, state ownership, and clear module responsibilities.
2. **Verifiable behavior.** Prefer options that can be proven with tests,
   controlled smokes, evidence logs, dashboard readbacks, or reproducible
   failure cases.
3. **Live-service and operator safety.** Avoid interrupting the real eiDOS,
   dashboard, voice, local model, GPU residency, credentials, or runtime state
   unless the task explicitly authorizes that disruption.
4. **Substrate honesty.** Design for the machine eiDOS actually inhabits.
   CPU-side periphery is not GPU-side parallelism; discrete LLM ticks are not a
   literal continuous stream; transfer functions are not learned inference.
5. **Epistemic honesty.** Keep future agents' map aligned with the territory.
   Label probes, stubs, fallbacks, analogies, and unbuilt aspirations plainly.
6. **Delivery speed.** Optimize for speed only after the higher values remain
   true.

## Source Routing

- General architecture choices: `BIBLE.md`, `ARCHITECTURE_PRINCIPLES.md`,
  `CONTEXT_REDESIGN.md`.
- Nervous-system boundaries or organs: `EIDOS_V3_BLUEPRINT.md`,
  `EIDOS_V3_ARCHITECTURE.md`, `EIDOS_V3_PHILOSOPHY.md`.
- Biomimetic drives, organism economics, power/food, skill-language mastery:
  `METABOLISM_PLAN.md`.
- Dashboard apply/restore, proposals, protected paths, rollback:
  `SELF_IMPROVEMENT_PLAN.md`, `CLAUDE.md`.
- Operator runbooks and live-service facts: `CLAUDE.md`, `OPERATING_MANUAL.md`.

## Common eiDOS Decisions

**Prompt plea vs mechanism.** Choose mechanism when behavior needs to be
reliable. A prompt sentence is acceptable only as a temporary cue or UX copy,
not as the controlling safety/agency path.

**Biological analogy vs engineering contract.** Use the analogy to find the
shape, then state the actual software contract. If the contract cannot be
tested, the analogy is not enough.

**One broad skill/doc vs several narrow ones.** Prefer narrow triggers when
false positives would load too much doctrine or confuse agents. Merge only when
the same task always needs the same references.

**Live smoke vs offline proof.** Start offline. Use live smokes only when the
claim is specifically about live service integration, local model behavior,
voice, BLE, dashboard process lifecycle, or hardware.

**Buildable-now vs learned-later.** Ship the honest degraded form only when it
is named as such and has a scheduled path or explicit deferral for the real
mechanism.
