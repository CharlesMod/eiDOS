# eiDOS V3 — system architecture & build plan

> **Status:** PROPOSAL — **rev 2.1**, nothing built. Companion to `EIDOS_V3_BLUEPRINT.md` (the *what/why*).
> This is the *how*: how to build the afferent nervous system **without building weakness into it.**
> **Authored:** 2026-06-19 (Dean + Claude). **rev 2** folded in a 5-lens adversarial review (triaged, §11),
> the **substrate decision** (CPU-only baseline on the 12700KF; Jetson as upgrade), and **I9 — location
> transparency**. **rev 2.1** adds four organs from a completeness pass (§3): a **neuromodulatory state**
> (arousal + affect — blueprint Pillar 6), a **forward model** (efference copy), **proprioceptors**, and an
> offline **sleep / consolidation cycle**.
> **What the review changed, honestly:** the anti-coupling *engineering spine* survived intact. Corrections
> were concentrated in (a) the **perception↔core seam** (the context window is an immutable, glue-composed
> input to a discrete serial decode — not a live workspace) and (b) **labelling engineering / hand-authored
> mappings as settled biology**. rev 2 fixed the mechanics and tells the truth about which parts are
> biology-inspired vs engineering.
> **Prime directive (unchanged):** interoperability is where deep dependencies and design faults breed
> fastest. Get **boundaries and contracts right before any organ is built.** Boundaries first, organs second.
> **Discipline (unchanged):** every mechanism must trace to the blueprint's north star (alive-to-itself,
> competent-in-its-world). Architecture that can't is bark with no tree.

---

## 0. How weakness gets built in (the failure we design against)

Weakness is rarely one bad component. It is **coupling**: organ A reaches into B's state; a raw feed makes C
depend on D's timing; two processes write the same file; everything assumes it owns the GPU. Each shortcut
is invisible alone and lethal in aggregate — an N² web where no change is local and no failure is contained.
eiDOS carries scars of this (V2 audit: cross-process unlocked writes to `observations.jsonl`/`jobs.json`,
six browser poll loops). V3 adds many parallel senses — exactly the conditions under which coupling
explodes. So we fix it at the seams.

The plan reduces to: **one dumb seam, single-responsibility organs behind it, a fixed set of invariants no
organ may violate, and an honest model of the one slow serial core they all feed.**

---

## 1. Substrate reality & the deployment model (read first — it reframes everything)

The adversarial review's hardest finding: the blueprint's "fast parallel periphery running orders of
magnitude faster than the core" is **physically false on a single 16 GB GPU the mind already fills
(~15.7/16 GB)**. There is no VRAM for parallel GPU sense-models, and a GPU arbiter would serialize them
*behind* the mind — inverting the premise. The resolution is binding:

- **The periphery is non-LLM and runs on the CPU.** The host is an **i7-12700KF (12 C / 20 T, ~5 GHz,
  64 GB)** whose CPU is *mostly idle during a tick* (llama.cpp is GPU-bound). ASR (`whisper.cpp`), small
  vision (ONNX), embeddings, and change-detection run there, genuinely parallel with the mind because **they
  never touch the GPU.** "Fast" = CPU-fast (tens–hundreds of ms) — still orders of magnitude faster than a
  seconds-scale tick. The asymmetry survives; it's about **seriality**, not raw FLOPS.
- **Gemma-4's native A/V is NOT the periphery.** It lives *inside* the 15.7 GB mind; routing exteroception
  through it puts sensing back in the serial core (violating Pillar 1). It is the rare **escalation / foveal
  path** the salience gate invokes deliberately.
- **Cheap non-LLM pre-filters decide what to escalate** (VAD, frame-diff, small embedding) *before* paying
  any tokenization cost. The core never sees a raw or streamed modality — only an occasional admitted snapshot.

### Location transparency (I9) — one architecture, many deployments

The same organ runs in any of three modes, **chosen at config/deploy time, no code change**:

| Mode | Use | Cost |
|---|---|---|
| **in-process** (thread/async) | cheap, co-located organs | lowest latency |
| **cross-process, same host** | fault isolation for risky/native organs (voice-split) | IPC latency |
| **cross-device** (over the wire) | true parallelism — the Jetson ganglia | network latency + failure |

"Desktop now, a cluster of Jetson Orin Nanos on a robot later" is **one architecture, two deployment
manifests** (§9). The enabling discipline: **design the contract for the hardest transport (the network)
from day one; local is just its fast path.** Every event is serializable and self-contained (by value or
content-addressed ref — never a shared-memory pointer); the bus always behaves like a broker even
in-process. Two load-bearing, biomimetic consequences:

- **A remote organ can vanish; that is a severed nerve, not a crash.** Covered by I5 + I8 — the sense goes
  dark, the core is unaffected. Unreliability is *expected*, not assumed away.
- **Deliberation can be remote; a tight reflex loop cannot be remote from its effector.** A sub-100 ms
  reflex must be co-located with the actuator it drives. Biology settled this: spinal reflexes are local
  *because* the round-trip to the brain is too slow.

---

## 2. Architectural invariants (the anti-weakness rules)

**Honest ledger:** I1 and I3 are genuinely biology-inspired (Global-Workspace / prediction-error). I2 is
ordinary encapsulation. **I4–I10 are distributed-systems discipline** from `ARCHITECTURE_PRINCIPLES.md` and
the V2 audit — they need no biological pedigree.

- **I1 — One *dumb* bus, not N² wires** (Global-Workspace-inspired). Organs never call each other; they
  publish to / subscribe from one bus that carries **bytes, delivery-class routing, retained topics, and the
  drop policy — and no organ-specific logic.** (Selection, context-compilation, and the broadcast log are
  *organs*, not powers of the bus — prevents the bus becoming a god-object.)
- **I2 — Clean boundaries (encapsulation).** An organ's only interface is its typed inputs and outputs. No
  back-channels, no shared globals. *(Engineering hygiene — not a literal "Markov blanket"; no
  conditional-independence claim.)*
- **I3 — Bounded contracts: typed events, never raw streams.** Every crossing is a bounded, versioned,
  serializable event. (Prediction-error-inspired.)
- **I4 — Event-driven, never polled across a boundary.** (`ARCHITECTURE_PRINCIPLES.md` #1.)
- **I5 — Fail-safe, typed degradation.** Any organ may crash, time out, or vanish off the network without
  wounding the core. The system loses a sense; it does not halt.
- **I6 — Single writer per datum, *including derived data*.** Raw and derived data each have exactly one
  owning writer; readers subscribe to the **one shared projection — they never recompute their own.** (What
  truth-rendering requires: the core and the creature-render must read the *same* felt-state projection.)
- **I7 — Explicit GPU arbitration (residency, not just utilization).** The arbiter mediates the **mind, TTS,
  and occasional escalated perception** for *residency*. Senses are CPU, so this set is small.
- **I8 — Capability-negotiated, versioned, *reliably-delivered* interfaces.** Organs advertise what they are
  and need; the body schema re-binds per host. Capability/version events are a reliable class (never dropped).
- **I9 — Location transparency / design-for-remote-first.** Deploy in-proc / cross-proc / cross-device by
  config; identical contract. **Tight reflex loops are co-located with their effectors.** (§1.)
- **I10 — Fair admission.** No single source may exceed its fair share of workspace admission under
  contention (the common *partial* failure crash-isolation doesn't cover).

---

## 3. The organs (single-responsibility decomposition)

Each organ does one thing, owns its state, touches the world only through the bus. **Honest labels:** where a
mechanism isn't built yet, the organ ships its *degraded, buildable* form now; the full version is a §10 TODO.

*(rev 2.1 — organ set expanded after a completeness pass against the biology + LIDA/Soar/ACT-R module lists.
The four additions are marked ✦.)*

| Organ | Responsibility (buildable now) | Owns | Fails → |
|---|---|---|---|
| **Receptor adapters** (per modality) | transduce raw source → typed sensory event | its device handle | that sense goes dark |
| ✦ **Proprioceptors** | sense the creature's own effector/avatar & in-flight-action state | — (reads effector state) | blind to own body |
| **Pre-attentive filter** (non-LLM, CPU) | cheap salience pre-score (VAD/frame-diff/embed) before tokenizing | its filter thresholds | passes-through |
| **Change/novelty layer** | per-channel **change detection** (true predictive-coding = T2) | its baselines | raw-delta passthrough |
| **Interoceptive transduction** | host telemetry → felt-state via a **designed transfer function** (inference = T3) | the felt-state projection | coarse raw bars |
| ✦ **Neuromodulatory state** (arousal + affect) | maintain global arousal (alert↔drowsy↔asleep) + core affect (valence×arousal = mood); broadcast `modulation` | the global state vector | neutral baseline (fixed gain/tempo) |
| **Salience gate** | score salience × **top-down relevance** × precision (gain set by `modulation`); admit | its thresholds | deaf-safe (admit-none) |
| **Context-compiler** (an organ) | KV-stable assembly of the next tick's volatile block | the compiled context | core runs last-good context |
| **Transport bus** | bytes + delivery classes + retained topics + drop policy (no logic) | the broadcast log | supervised restart; organs buffer |
| **Core adapter** | the LLM mind (`:8081`) behind a clean interface | — (stateless) | core down → reflexes only |
| **Efferent / action** | execute action-requests; close the loop | actuator handles | action refused, typed-fail |
| ✦ **Forward model** (efference copy) | predict self-caused sensory change from each action → cancel it downstream | the action→sense predictor | no cancellation (self-change reads as world-change) |
| **Reflex arcs** (local to effector) | fast sense→act, bypass the core | their trigger rules | reflex disabled, escalate |
| ✦ **Consolidation & sleep cycle** | *offline* (low-arousal) replay, prune, re-fit predictive/intero baselines (T2/T3/T4) + Q4 re-bind | the episodic store + learned models | no learning; perception runs |
| **GPU arbiter** | residency lease: mind / TTS / escalation | the lease state | conservative serialization |
| **Body / capability registry** | what senses & effectors this host has | the body schema | assume minimal body |

Adapters over existing services (core→`:8081`, efferent→voice `:8098`, supervision→`:8099`, eufy bridge) are
wrapped, never entangled — see §6.

---

## 4. The seam: the `NervousEvent` contract

Everything an organ shares is ONE message type on the bus. The whole public surface area.

```
NervousEvent {
  schema_version: int            // I8 — reliably delivered, negotiated
  source_organ:   id             // provenance, never a call target
  kind:           enum { sensory, proprioceptive, change, interoceptive, percept,
                         reflex_fired, action_request, efference_copy,
                         relevance_set,   // TOP-DOWN: core → gate (goal-relevance)
                         modulation,      // GLOBAL: neuromodulatory state → all (retained, last-value)
                         capability }
  modality:       enum { audio, vision, intero, proprio, device, time, … }
  delivery:       enum { fungible,   // best-effort, drop-by-priority OK
                         ordered,    // in-order, atomic-abort (whole or signal aborted)
                         reliable,   // never dropped
                         retained }  // last-value-wins global state (modulation, capability)
  sequence_id:    id?
  ordinal:        int?
  salience:       float            // bottom-up
  precision:      float            // confidence; set top-down via relevance_set AND modulation gain
  t:              monotonic
  payload_ref:    handle           // content-addressed, IMMUTABLE, producer-owned lifetime
}
```

- **The bus is dumb** (I1). Topic by `kind`+`modality`.
- **Delivery classes are the contract (fixes the backpressure correctness bug):**
  - **fungible** afferent samples — drop-by-priority under load (counted+logged). The periphery drops ~99%.
  - **ordered** streams (a sentence, a multi-step action, a delta sequence) — delivered in-order, **no holes**;
    on overload dropped atomically with a `sequence_aborted`. Never a partial sequence acted on as whole.
  - **reliable** (`action_request`, `reflex_fired`, `capability`, `relevance_set`, `efference_copy`) —
    **never dropped**, priority floor. (`efference_copy` *must* reach the change-detector before the sensory
    consequence it predicts.)
  - **retained** (`modulation`, `capability`) — last-value-wins **global state**, not a stream; a new
    subscriber gets the current value immediately. This is how arousal/affect and the body schema are read
    by everyone without a firehose.
- **`payload_ref` is content-addressed + immutable + producer-owned** (no torn reads; required for I9
  cross-device).
- **Fair admission (I10):** a per-source admission budget; no organ monopolizes the workspace.
- **Bandwidth is first-class:** the core ingests only at tick boundaries, one batched volatile block per tick,
  so `max_admits/sec ≈ (tail-token budget) ÷ (tick period)` — a trickle, each batch costing a tail re-prefill.
  **Measured at P0** (T8); gate thresholds derive from the number.

---

## 5. Core integration — the honest LLM-reality layer

The biggest rev-1 weakness was treating the context window as a live shared-memory workspace. It is the
**immutable, glue-composed input to a discrete, blocking, serial llama.cpp decode.** The design respects that:

- **Two steering granularities — no third.** (a) Between ticks; (b) abort the in-flight decode and re-prefill.
  There is **no mid-decode interrupt.** Blueprint **Q1 "interrupt" = abort + re-prefill the changed tail,
  gated by a salience threshold** (it throws away partial compute).
- **"Broadcast into the workspace" = batched into the next tick's volatile block.** Admitted events join one
  KV-stable volatile tail (V2 compiler). Injecting high to gain attention re-creates the V2 KV re-prefill bug;
  we don't. **All competition and selection happen in deterministic glue *before* the decode** — never inside
  the window (the CONTEXT_REDESIGN north star).
- **Continuity = persistent carried-over state across discrete, self-triggered decodes** (durable blob + KV
  prefix-reuse + episodic store) — not a running stream. Replaces "never request-response" with
  **self-triggered request-response with continuous carried state.** A self-generated drive (homeostatic gap,
  modulated by affect) authors the *next* tick's prompt.
- **Admission is not free GPU.** Re-prefilling the volatile tail is GPU work on the mind's card — arbitrated
  by I7. The periphery is off-GPU; admitting its output is not.
- **The biology is inspired-by, not proven-by** (blueprint §4): Global Workspace, predictive coding,
  interoceptive inference, allostasis, neuromodulation, the Markov blanket — design analogies. Where the
  buildable mechanism differs (transfer-function not inference; change-detection not prediction; gap not
  allostasis), §3 and §10 say so plainly.

---

## 6. Dependency rules (the allowed graph)

```
receptors / proprioceptors → pre-filter → change → salience-gate → context-compiler → core-adapter → efferent
                                              ↑            ↑                  │                            │
   relevance_set (core → bus → gate)  ────────┘            │                  │                            │
   modulation (neuromod-state → ALL organs, retained)  ⟲ sets gain/tempo everywhere                        │
   efference_copy (efferent → change: "expect this self-caused change")  ◄───────────────────────────────┘
   reflex arcs: receptors → (fast path, local to effector) → efferent          [bypasses core; Pillar 5]
```

- **Top-down relevance stays on the bus.** The core publishes `relevance_set`; the gate subscribes. Goal-relevance
  and real precision **without a back-channel** (I2 intact) — the review's single highest-leverage fix.
- **The neuromodulatory state broadcasts `modulation`** (retained) to every organ: a *second* top-down
  precision/gain source alongside `relevance_set` — it raises/lowers gate thresholds, reflex sensitivity, and
  tick cadence by global arousal, and colors salience by affect. It is itself driven by interoception
  (resource pressure → arousal) and salience (threat/novelty → arousal); its lowest arousal state *is* sleep,
  which triggers the consolidation cycle.
- **Efference copy closes the self-model loop.** On acting, the efferent layer emits `efference_copy` to the
  change-detector so self-caused sensory change is cancelled and only **world-caused** change surprises the
  core — the sense of agency, and the fix for the self-reaction feedback loop.
- **Proprioception is an afferent like any receptor** (modality `proprio`): the creature's own effector/avatar
  state, feeding the change-detector and (with efference copy) the forward model.
- **The core depends on NO specific sense** (I5/I8). **No direct cycles** — mutual need goes on the bus.
- **Existing services are adapters behind the seam** (mind `:8081`, voice `:8098`, watchdog `:8099`, eufy):
  the firewall against interop faults. **Reflexes are local to their effectors** (I9).

---

## 7. Risk register (each fault → its killing invariant)

| System-design fault | Mitigated by |
|---|---|
| GPU residency contention | **I7** residency arbiter; senses are CPU (§1) |
| Fast senses outrun the core | **I3** + delivery classes (fungible drops; §4) |
| Ordered / efferent signal lost by drop | **§4 delivery classes** (ordered = atomic; efferent = reliable) |
| Efferent priority inversion | reliable class + priority floor (§4) |
| **Creature reacts to its own action (self-reaction loop)** | **forward model / efference copy** cancels self-caused change |
| **Fixed temperament — can't shift vigilance or rest** | **neuromodulatory state** (arousal+affect `modulation`) |
| Shared-state write races | **I6** single writer (incl. derived) |
| Deep coupling / N² web | **I1** one dumb bus; **I2** boundaries |
| Bus becomes a god-object/SPOF | **I1** bus carries bytes only; compiler/log are organs |
| One greedy organ starves the rest | **I10** fair admission |
| Interop entangles service internals | **§6** adapters-only firewall |
| KV re-prefill storm from sensory injection | **§5** batched volatile tail; no high-placement |
| "Interrupt mid-thought" assumed | **§5** two granularities; Q1 = abort+re-prefill |
| Body renders a falsehood | **I6** single felt-state projection; render *subscribes* |
| Remote organ vanishes | **I5/I8** severed-nerve degradation (I9) |
| Overclaimed biology misleads a decision | **§2 honest ledger** + §5 inspired-by labels |
| Silent truncation | fungible drops counted+logged; ordered/reliable never silently dropped |

---

## 8. Build sequence (dependency-ordered; gates are *red-able* contract tests)

Each gate can **fail on a correctness violation**, not just a smoke check. The order builds the **seam and
rails first**.

- **P0 — The seam (hardened).** Bus + `NervousEvent` with delivery classes (incl. *retained*) + a **synthetic
  firehose** (multi-priority + large `payload_ref` lifecycle) + run one organ in **all three deployment modes**.
  **Gate:** fungible priority-drop verified; *ordered* never delivers a hole; *reliable* never dropped;
  *retained* gives a new subscriber the current value; `payload_ref` lifetime safe; **byte-identical across the
  3 modes**; measured admits/sec + per-hop latency reported (T8).
- **P1a — Interoception as coarse raw bars** (no model, no GPU, no render). **Gate:** raw→felt is *monotonic*;
  fault-injection crosses the felt threshold within N ms; single writer (I6).
- **P2 — The lease arbiter** (the speech-gate becomes its *first client*). **Gate:** under mind + TTS + one
  escalation demand, total resident ≤ VRAM with measured no-thrash, **OR** an explicit logged evict/reload with
  bounded latency *and* the felt-state reflects it.
- **P1b — Interoceptive transfer-function + creature render subscribes.** **Gate:** render == the felt-state
  projection bin (never recomputed — I6); the "renders falsehoods" bug class cannot recur.
- **P3 — Pre-filter + salience gate + `relevance_set` path + workspace→core.** **Gate:** only admitted events
  reach the core, batched into the KV-stable tail (no full re-prefill); a published `relevance_set` measurably
  biases admission; the core is never drowned (admits/sec ≤ T8 budget).
- **P4 — Change/novelty detection (one channel) + proprioception + efference copy.** True predictive coding is
  T2. **Gate (red-able):** detection ≥ X% / false-positive ≤ Y% on *predictably-changing* input; **and the
  efference copy cancels a self-caused change** — the creature does not react to its own speech/motion (inject
  a self-action, assert no surprise raised).
- **P5 — Efferent loop + reflex arcs (local to effector).** **Gate:** closed loop; reflexes fire without the
  core; a defined set that **must NOT** reflex stays escalated (escalation correctness); core-down leaves
  reflexes alive.
- **P5b — Neuromodulatory state (arousal + affect).** **Gate:** arousal measurably shifts gate thresholds +
  tick cadence under injected threat / novelty / resource-pressure; affect (valence×arousal) tracks
  interoception + salience and drives the creature's *expressed* mood (truth-rendering — the body shows it);
  the low-arousal floor triggers the sleep cycle.
- **P6 — Exteroception.** CPU pre-filters do always-on sensing; Gemma-native A/V is the **rare escalated foveal
  path** only. **Gate:** raw/streamed modality never enters the core; admit precision/recall against a
  "should-admit" oracle; budget never blown.
- **P7 — Consolidation / sleep, learned models, re-bind.** The sleep cycle (low-arousal, offline) runs replay +
  prune + the learned generative model (real predictive coding T2 / interoceptive inference T3 / allostasis T4)
  + substrate re-bind. **Gate:** held-out prediction error drops ≥ X% over N cycles; waking on a new host
  re-binds the body schema.

P0–P2 are pure foundation; no perception *feature* ships until the seam and rails are proven.

---

## 9. Deployment manifests (concrete, from §1 / I9)

- **Now — "megalith on the desktop":** all organs on gamingPC. Cheap organs in-process; risky/native organs
  (vision libs, TTS) as isolated processes (the voice-split pattern); the mind on the GPU, senses on the
  12700KF's spare cores. Single `NervousEvent` bus, in-proc fast path.
- **Later — "distributed on a robot":** the same organs, redeployed — a cluster of Jetson Orin Nano ganglia,
  each running a sense + its pre-filter + **local reflexes** (co-located with the limbs they drive), streaming
  `NervousEvent`s over a local switch to a central deliberative core. **No code change — a different manifest.**

---

## 10. Deferred-fix backlog (real, solvable with tact — scheduled, not forgotten)

The learned-model items (T2–T4) now have an explicit home: the **sleep / consolidation cycle (P7)**.

| | Fix | Replaces the honest-now form | Lands |
|---|---|---|---|
| **T1** | Habituation / adaptive-gain load-shedding *upstream* (suppress the expected, preserve the novel) | naive priority-drop on fungible | P4+ |
| **T2** | Learned per-channel generative model → *real* prediction-error | change/novelty detection | P7 (sleep) |
| **T3** | Learned interoceptive estimator over telemetry → *real* inference | designed transfer function | P7 (sleep) |
| **T4** | Allostatic forecaster (resource-trajectory) → *anticipatory* drive | reactive homeostatic gap | P7 (sleep) |
| **T5** | Capability/version negotiation protocol (reliable, min-common-version, reject-and-log) | I8 reliable stub | P0/P3 |
| **T6** | Cross-modal salience/precision normalization owner + schema conformance tests | per-organ scoring convention | P3 |
| **T7** | Full multi-tenant arbiter with priority + preemption | speech-gate as first client | P2+ |
| **T8** | Measure admits/sec + prefill cost on house-ai → derive gate thresholds | the bandwidth estimate | P0 ✓ (ZMQ p95 ≈ 1.8 ms — ratified) |
| **T9** | Flatten the in-proc mailbox latency tail under flood (O(n) list → heap/indexed) | the P0 O(n)-list mailbox | post-P0 (P0-measured: in-proc p95 ≈ 730 ms vs ZMQ ≈ 1.8 ms) |

---

## 11. Adversarial-review triage (rev 1 → rev 2) & completeness pass (→ rev 2.1)

A 5-lens adversarial review (coupling, LLM-mechanics, GPU/substrate, biomimetic-fidelity, build-plan; several
grounded in `llm.py`/`context.py`/`gpu_gate.py`/`embedding.py`). Disposition:

- **Resolved by the substrate decision (§1):** no-VRAM-for-parallel-GPU-senses; arbiter-serializes-periphery;
  Gemma-A/V-is-the-trap → senses CPU; Gemma A/V = escalation; periphery non-LLM. *Partially dismissed:*
  "degradation = normal mode" (CPU senses run continuously).
- **Resolved by I9 (§1):** process-model-deferral; thread↔process equivalence; isolation-before-P0 → deployment
  is config; equivalence is a P0 gate.
- **Fixed in rev 2:** backpressure correctness (delivery classes, §4); efferent priority-inversion; no
  mid-thought-interrupt / "continuous" reframe / KV-broadcast bug / GWT-leak (§5); feed-forward gate + unfilled
  precision (top-down `relevance_set`, §6); bus god-object (dumb bus, I1); single-writer on derived data (I6);
  `payload_ref` race (§4); greedy-organ starvation (I10); honest restatements (I2, §3 labels, §2 ledger);
  build-order (split P1, reorder P3/P4, P2=arbiter, P0 firehose, red-able gates, §8).
- **Deferred with tact (§10):** T1–T8.
- **Pushed back on (kept, with correction):** "one bus is the flaw" → make it *dumb*. "Continuity is an
  illusion" → carried-state continuity is real; correct the mechanism, keep the goal. "Drop-by-priority is
  wrong" → wrong only for ordered/efferent; right for fungible.

**Completeness pass (→ rev 2.1):** the 13-organ set was checked against the biology (structures we *cited but
never instantiated*) and LIDA/Soar/ACT-R. Four real gaps added (§3 ✦): **neuromodulatory state** (arousal +
affect — system-wide gain/tempo + mood; blueprint Pillar 6); **forward model / efference copy** (self-vs-world,
agency); **proprioceptors** (sensing the creature's own body/effectors); and the offline **sleep / consolidation
cycle** (home of the learned-model TODOs). Folded into existing organs rather than added: a nociceptive/alarm
fast-path (a reflex-class + an arousal spike that decides when to pay Q1's abort+re-prefill), a temporal/circadian
sense (a receptor feeding prediction + arousal), and nervous-system self-health ("numbness" = interoception of
the organs themselves). Rejected as out-of-layer: a narrative self/identity model and procedural skill memory
(both higher cognition / existing subsystems).

---

## 12. Open decisions

1. **Substrate** — **RESOLVED:** CPU-only periphery on the 12700KF now; Jetson Orin Nano ganglia as the
   documented upgrade (§1/§9).
2. **Process/isolation** — **RESOLVED by I9:** per-organ deployment is config.
3. **Bus substrate** — decide at the **P0 spike against projected worst-case load** (T8 feeds this).
4. **Doc home** — settled: this engineering companion to `EIDOS_V3_BLUEPRINT.md`.
