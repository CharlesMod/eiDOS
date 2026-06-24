# V3 Boundary Reference

Use this reference for `eidos-nervous-system-design`.

## Prime Directive

Boundaries first, organs second. Weakness in V3 is expected to come from
coupling: raw feeds, hidden shared state, back-channels, multiple writers, or
timing assumptions. The seam must be correct before organs grow behind it.

## Core Invariants

- **One dumb bus.** The bus routes bytes by kind/modality and delivery class.
  It does not know organ-specific policy.
- **Clean boundaries.** An organ's public interface is typed inputs and outputs.
  No hidden direct calls to sibling state.
- **Bounded contracts.** Crossings use versioned, serializable events; payloads
  are bounded or content-addressed references.
- **Event-driven.** Prefer notifications, queues, condition waits, or blocking
  call-response over polling loops.
- **Fail-safe degradation.** A missing sense makes eiDOS partially blind; it
  does not crash the core.
- **Single writer per datum.** This includes derived projections such as felt
  state and body render state.
- **GPU residency is explicit.** CPU senses are the baseline; the GPU mind,
  TTS, and rare escalated perception need arbitration.
- **Capability negotiated.** Body schema and organ capability are versioned and
  reliable.
- **Location transparent.** In-process, cross-process, and cross-device organs
  use the same contract.
- **Fair admission.** No source monopolizes the scarce context admission path.

## Delivery Classes

- `fungible`: best-effort sensory samples; drop by priority under load.
- `ordered`: in-order sequences; abort whole sequence rather than deliver holes.
- `reliable`: action requests, capability, relevance, efference copy; never
  silently dropped.
- `retained`: last-value-wins state such as modulation or capability.

## Honest LLM Reality

The context window is an immutable input to a discrete decode. "Broadcast into
workspace" means admitted content joins the next tick's volatile block.
Steering happens between ticks, or by aborting and re-prefilling. There is no
mid-decode interrupt.

## Organ Placement Hints

- Raw device -> receptor adapter.
- Cheap salience or frame/audio change -> pre-attentive filter.
- Delta from expectation -> change/novelty layer.
- Host telemetry -> interoceptive transduction.
- Global arousal/affect -> neuromodulatory state, retained.
- Goal relevance -> relevance set on the bus, not a direct back-channel.
- Self-caused sensory consequence -> efference copy/forward model.
- Fast local safety or actuator reaction -> reflex arc near the effector.

## Vision/Motion Example

For a webcam motion watcher, raw frames stay inside a vision receptor adapter.
A CPU pre-attentive filter computes frame diff or motion score; the change layer
publishes compact motion onset/change events; the salience gate decides whether
the event reaches the next tick's volatile context. Rare high-salience motion may
escalate through a foveal perception path with a GPU lease.

Good prompt-level result: "something moved near the doorway." Bad result:
continuous frame summaries or raw frame bytes in the LLM context.

Motion events should be compact and fungible:

```text
kind=change or sensory
modality=vision
delivery=fungible
source_organ=webcam_motion
payload_ref=bounded JSON or content-addressed non-core payload
```

Publish retained capability state for camera present/unavailable, supported
modes, and last-ok timestamp. Missing camera, permission denial, GPU lease
failure, or remote organ disappearance should make the visual sense go dark
without wedging the bus or tick loop.

## Verification Hints

Prefer unit tests that prove delivery, degradation, ownership, and no raw-stream
admission. Use dashboard or live smokes only after the contract is proven
offline.

For modality watchers, include tests that unchanged input publishes nothing,
meaningful change emits one bounded event, raw bytes never reach
`AfferentContext`, floods drop fungible events fairly, and missing devices
degrade without blocking.
