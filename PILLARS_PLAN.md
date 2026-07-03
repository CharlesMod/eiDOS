# eiDOS — The Three Pillars Plan (memory · skills · structure)

> **Status:** PLAN — synced with Dean 2026-07-03. Companion to `BIBLE.md` (doctrine),
> `EIDOS_V3_BLUEPRINT.md` / `EIDOS_V3_ARCHITECTURE.md` / `EIDOS_V3_PHILOSOPHY.md` (the V3 trio).
> **What this is:** the design + build plan for the three systems responsible for ~90% of the
> mind's behavior and its ability to improve over time: **memory, skills, and the biomimetic
> code structure.** Grounded in a full code audit of the current implementation (2026-07-03).
> Extended same day with the heavy-planning designs: brain-system groundings for memory
> (hippocampal) and skills (striatal), the neuromodulator economy, and the two **capability
> extensions** — shadows (scripted CPU workers) and generals (delegated LLM minds) — which are
> plain engineering to put the mind's under-used CPU and code capacity to work (§5).
> **Method (Dean):** nature already optimized systems that produce competent autonomous agents;
> we recreate those mechanisms in silica and inherit the emergence. Biology is a design source,
> not a subject — map it where it generates design, engineer plainly where it doesn't.
> **Reuse constraint (Dean):** the only subsystem we are committed to reusing is the **agentic
> tool-calling validation stack** (GBNF grammar-constrained action channel + multi-format parser +
> non-lying auto-correcting validator). Everything else is salvage-if-smart, redesign-if-not.
> **Read it when:** starting work on any pillar, or when a mechanism under construction starts
> drifting toward scripting a behavior instead of pressuring one.

---

## 0. The discipline — pressure, not prescription (Dean, binding)

We are designing **balance and pressure behind the scenes, hoping to drive emergent behavior.**
We do not write code that explicitly tries to elicit that behavior. This is BIBLE.md §0 ("agency
is architecture, not a vibe") applied to ourselves, and it binds every mechanism below:

1. **No line of code may name the behavior it hopes to produce.** `if bored: explore()` is
   scripting. `arousal_floor = f(surprise_ema)` is pressure — and exploration is what a creature
   under that pressure *does*. If a function reads like the behavior, the mechanism is missing.
2. **Behaviors live only in the acceptance tests.** The dream-tests (§6) name behaviors — "it has
   news," "it hesitates at the frontier" — precisely so the code never has to. We test for the
   emergent thing; we build only the field it emerges from.
3. **Closed-loop wherever a loop can close.** Every signal that goes out should have a
   consequence that comes back: a recall is a bet scored by the tick's outcome; a skill's trust is
   its measured record; an energy price is paid and felt. Open-loop injections (advice nobody
   grades, stats nobody spends) are dead weight.
4. **Self-tuning over hand-set constants.** Today's magic numbers (0.45 recall similarity, 0.65
   dedup overlap, 5-use trust thresholds, 30s watchdog) become one of two things: **(a) derived** —
   re-fit from measured data during sleep, with bounded adjustment rates so the tuner can't run
   away; or **(b) declared** — an explicitly labeled design knob with a one-line justification.
   Nothing stays a silent guess. (This is `ARCHITECTURE_PRINCIPLES.md` #1 generalized: a constant
   is a guess the same way a delay is.)
5. **The glue judges; the creature never grades its own homework.** XP, trust, memory strength,
   quest completion — all adjudicated from typed outcomes by deterministic code. Self-report is
   never a reward signal (or the creature rationally learns to narrate success).
6. **Stability guards on every self-tuning loop.** Bounded step sizes, floors/ceilings, and
   operator-visible current values on the dashboard. A self-tuning economy that can spiral is
   worse than a hand-set one.

---

## 1. The shared economy (one currency system, three markets)

The pillars compound because they trade in the same signals:

- **Energy (metabolism, live)** prices *action*: authoring is expensive, reuse is cheap, shadows
  have stipends. Scarcity is what makes choices mean something.
- **XP / the System (persona, live)** rewards *adjudicated outcomes* and gates *capability scope*
  (the raising-a-baby-AI ladder; level caps = pedagogy + safety in one mechanism).
- **Strength (memory, new)** ranks *what is worth remembering*, earned by recall-utility.
- **Surprise (expectation ledger, new)** is the yield on *predictions* — feeding reward,
  curiosity, and learning. The most nutritious signal a mind can eat.
- **Sleep (structure, unified)** is the clearinghouse where all accounts settle: memories digest,
  strengths update, skills re-score, models re-fit, constants re-tune.

**The spine of this economy is the four-neuromodulator layer** (partly live in `neuromod.py`) —
the same global signals, read by every subsystem:

| Modulator | Role in the economy | Silicon form |
|---|---|---|
| **Dopamine** | the credit currency — RPE settles every account (memory bets, skill reinforcement, shadow upkeep, mission payoff) | reward learner's RPE, extended to all four markets |
| **Norepinephrine** | arousal, the explore/exploit switch, network reset on big surprise — the signal that says "this exceeds me, recruit help" | salience-gate gain + curiosity + the general-recruitment trigger |
| **Acetylcholine** | the encode↔consolidate mode switch — high awake (take in, practice), low asleep (replay, prune) | the unified sleep engine's on-switch |
| **Serotonin** | patience and reward time-horizon — how long to wait on a general, how far allostasis looks ahead | temperament's caution axis + delegation/forecast horizons |

The leveling System sits above all markets. Quests reference skills and memories; level caps gate
skill scope, shadow capacity, and general recruitment; the judge is glue, the ban-hammer is Dean.

---

## 2. Pillar M — Memory: from storage to digestion

**First principle:** memory is not eight stores, it is **one economy with a lifecycle** —
experience → episode → consolidated knowledge / skill / identity — and the stores are stages of
digestion. Sleep is the digestive tract. One consolidator is the single writer (I6) of every
long-term store.

**Design source: the hippocampal–neocortical system.** Fast plastic episodic encoding
(hippocampus = the episodic ring) consolidating into slow stable knowledge (neocortex = the
long-term store) during sleep. The mechanisms we capture, each carrying its emergent payoff:

| Mechanism | What it does | Silicon capture |
|---|---|---|
| **Pattern separation** (dentate gyrus) | similar-but-distinct memories don't blur | the anti-interference dedup layer |
| **Pattern completion** (CA3) | a partial cue recalls the whole | vector recall from partial situation keys |
| **Comparator** (CA1) | expectation vs input → novelty | expectation ledger + change detection |
| **Emotional tagging** (amygdala) | flashbulb scars persist, trivia fades | `encoded_at{arousal,valence}` × initial strength |
| **Compressed replay** (sharp-wave ripples) | consolidation + overnight insight | sleep replay of high-strength/high-surprise engrams |
| **Synaptic downscaling** (SHY) | forget the trivial, keep the signal | global strength decay + prune each sleep |
| **Source monitoring** (PFC) | "I saw it" vs "I was told" | `provenance` + `confidence` fields |
| **Gist extraction** (systems consolidation) | detail → generalization over time | grammar-distilled facts promoted from episodes |

**Audit summary (what exists):** episodes.jsonl + situation vectors (typed, state-triggered
recall — sound doctrine, unmeasured quality); knowledge store (BM25+MiniLM hybrid search — solid);
dream/compaction (right idea, brittle regex extraction over free prose); observations/thoughts/
notes/persona/self_guide (working strata); no forgetting policy; no provenance; nothing ever asks
whether a recalled memory *helped*.

### Design

- **M-1. The recall-utility loop (the keystone).** Every injected recall is a bet. When the tick
  resolves, credit or debit the memory's **strength**. Strength = earned usefulness compounding
  recency, frequency, and **emotional salience at encoding** (arousal/felt-state stamped on each
  episode — flashbulb memory). Recall ranking and retention both key on strength. This one closed
  loop is most of "memory improving over time."
- **M-2. Provenance + confidence on every entry.** `experienced | told | inherited` (nuggets are
  the letter from a previous self and are marked as such), with confidence and last-verified.
  Contradiction between a memory and fresh observation lowers confidence rather than silently
  coexisting.
- **M-3. Forgetting is a feature.** Sleep prunes by strength with interference-awareness; every
  store bounded; retention is *earned*. No unbounded growth (thoughts.jsonl is the cautionary
  vestige).
- **M-4. The expectation ledger (the future dimension's first organ).** Typed open predictions
  with deadlines ("backup done by 02:30", "Dean home ~18:00"), closed by glue, scored into
  surprise. Feeds reward/curiosity; residue ("what I got wrong") is the highest-value input to
  the episodic store. Jointly owned with Pillar N (§4).
- **M-5. The news queue.** A deferred-communication store: items worth telling Dean, ranked by a
  learned model of what he engages with, held until presence. (The mechanism is a queue + a
  relevance model; "having news" is the emergent read of it — see §0.1.)
- **M-6. Structured extraction everywhere.** Dream distillation moves from regex-over-prose to
  the **grammar-constrained output stack** (the one committed keeper, reused in a new seat) so
  consolidation cannot silently drop malformed material.

**Stages:** M1 unify lifecycle under one manager, migrate stores → M2 recall-utility loop →
M3 forgetting + bounds → M4 expectation ledger → M5 news queue.

---

## 3. Pillar S — Skills: from library to language

**First principle:** skills are procedural memory — how a frozen-weights creature grows beyond
its original programming. Atoms (a guaranteed, soft-failing vocabulary) are the right core. What
turns vocabulary into fluency is **composition** and **promotion** — and what makes the library a
living thing is an **economy that structurally favors reuse.**

**Design source: the basal-ganglia + cerebellar procedural loop.** The load-bearing fact: skills
*migrate* — a new action runs goal-directed (deliberate, attention-hungry, prefrontal +
dorsomedial striatum) and with successful repetition automatizes into a habit (dorsolateral
striatum) that runs without oversight. That migration **is** the trust pipeline:

| Mechanism | What it does | Silicon capture |
|---|---|---|
| **Automatization** (DMS→DLS shift) | fluent skills stop costing attention | `active → trusted` promotion; trusted skills surface as near affordances |
| **Chunking** (basal ganglia) | sequences collapse into single units | composition → promotion-to-atom (a chunk IS a new atom) |
| **Forward model** (cerebellum) | predict consequences, error-correct | efference copy (built) + arg-shape success model |
| **Reinforcement** (dopamine→striatum) | what works gets stronger | reward learner crediting skills via RPE |
| **Use-dependent pruning** | unused programs fade | auto-retire of stale skills |

**Audit summary:** validation/dry-run/hot-load/versioning/trust-promotion are mature; the scar is
56 skills authored, 0 ever reused (prompt nudges failed — of course: that's compliance theater,
our own §0 lesson); no skill→skill calls; no promotion-to-atom; watchdog abandons hung threads
(tick 342: 6.7-minute freeze); ToolResult contract enforced only at dispatch.

### Design

- **S-1. Execution hardening.** Killable subprocess isolation (a hung skill dies dead); ToolResult
  contract enforced at definition time; per-skill telemetry (latency, success by arg-shape)
  feeding auto-retire. The watchdog timeout becomes measured, not guessed (§0.4).
- **S-2. Reuse economics (the keystone).** Do not *ask* for reuse — make it the resting state of
  the pressure field: a deterministic retrieval step surfaces the 2–3 most situation-relevant
  existing skills as affordances at decision time; **authoring costs energy, calling is nearly
  free**; XP favors adjudicated successful reuse over creation; curiosity still funds occasional
  new authorship. Auto-retire keeps the decision space clean.
- **S-3. Composition.** `call(skill, args)` with depth + energy-budget caps. Compositions are how
  the creature builds sentences from its vocabulary.
- **S-4. Promotion.** A proven, *reused* composition becomes an atom candidate → operator approves
  → new atom. Congealed experience, formalized. (The METABOLISM_PLAN M2.3 idea, built fresh.)
- **S-5. Level gates + quests.** Level caps bound what atoms/capabilities a skill may touch
  (training wheels); quests reference the skill economy ("author something that earns 5 reuses").
  The System judges from the manifest's typed stats, never self-report.
- **S-6. Shadows are skills grown up.** A shadow = a trusted skill + a budget + a loop, detached,
  with the same validation and telemetry plus a lifecycle. Design the schema for that extension
  now so the endgame unlock is a config door, not a rewrite. Skills carry links to the episodes
  that birthed them — when one fails, its history recalls.

**Stages:** S1 hardening + telemetry → S2 reuse economics + auto-retire → S3 composition →
S4 promotion pipeline → S5 level gates + quest hooks → S6 first shadow prototype.

---

## 4. Pillar N — Biomimetic structure: finish attention, unify sleep

**Audit summary:** the healthiest pillar. Bus + NervousEvent seam hardened (P0 gates passed);
invariants I1–I10 genuinely held (single-writer verified per state file; no back-channels found);
interoception → felt-state → neuromod → drives all wired; reward/temperament/strain/objectives
live. Gaps: **no salience-gate organ** (delivery classes carry the load; `relevance_set` is
designed but nothing publishes it — the creature cannot focus); **sleep `consolidate()` is a
stub**; exteroception skeletal; in-proc mailbox p95 ≈ 730 ms vs ZMQ 1.8 ms (T9 — a real bug);
eidos.py is a ~1,400-line god-loop hand-orchestrating organ init order.

### Design

- **N-1. Mailbox fix + organ lifecycle hooks.** T9 heap fix (small job, big win). Then
  `pre_tick`/`post_tick` registration so organs plug in without editing the god-loop — new
  pressures (drives) become pluggable, which is how we iterate on behavior cheaply forever after.
- **N-2. Attention (P3, done properly).** The salience-gate organ: bottom-up salience ×
  **top-down relevance** × neuromod gain. The core publishes `relevance_set` from its current
  focus each tick. This is the present-pillar's core: focus becomes a *bias field over
  admission*, not an instruction.
- **N-3. Sleep as the single improvement engine (the keystone).** One offline, low-arousal engine
  running a job list that services all three pillars: memory digestion + strength updates (M),
  skill re-scoring + retirement (S), world-model re-fit toward real prediction error (T2),
  interoceptive curve fitting (T3), and **constant re-tuning** (§0.4) with bounded steps. One
  engine, one gate, one place where "wakes up smarter" is either measurably true or red.
- **N-4. The expectation organ.** The nervous-system half of M-4: publish open predictions, score
  closures into surprise on the bus, feed reward and curiosity. The count-based world model is
  its seed.
- **N-5. First learned upgrades.** T2 predictive coding on one channel once the sleep engine has
  data; T3 interoceptive curves after. Honest-now labels stay until the learned form lands
  (V3 doctrine: never call the stub the real thing).

**Stages:** N1 mailbox + hooks → N2 salience gate + relevance_set → N3 unified sleep engine →
N4 expectation organ → N5 learned upgrades. All gates red-able per `EIDOS_V3_ARCHITECTURE.md` §8.

---

## 5. The capability extensions — shadows & generals

Not biomimetic and not trying to be: the mind-in-a-can under-utilizes a 20-thread CPU and its
code-execution capacity. These two systems give it those hands. Both are level-gated unlocks
(the System judges readiness), both are priced (spawning is felt through metabolism), and both
inherit the trust geometry: *the subordinate proposes, the monarch applies* anything
irreversible — the dashboard pattern recursed one level down.

### 5a. Shadows — scripted CPU workers

A shadow = **a trusted skill + a loop + a budget + a lease**, detached into a killable
subprocess on spare CPU cores. No LLM anywhere in it.

```
Shadow {
  body:    ref to a TRUSTED skill/composition (trust before delegation),
  loop:    bus_subscription(topics) | schedule | watch_condition,   // event-driven, never poll-sleep
  budget:  { energy/day, max_runtime, max_actions/hr },
  lease:   expiry renewed only by a live monarch tick (dead-man switch),
  mailbox: results published to the bus as NervousEvents,
  standing:{ outcomes, strikes }
}
```

- **Shadows are organs on the bus**: their output competes for admission through the salience
  gate like any sense; the monarch never blocks on one; a crashed shadow is a severed nerve (I5).
  Proprioception extends over the roster ("my shadows and what they're doing").
- **Rent must be paid**: each shadow draws a metabolic stipend; delivered results earn credit
  against upkeep; one that stops earning starves its keeper until dissolving it relieves the
  pressure. Budget violations / crash loops → strikes → auto-dissolve + an episode.
- **Gates**: unlocked at a level threshold; concurrent capacity scales with level; trigger
  classes unlock in order (schedule → bus-subscription → watch-condition). Same atom sandbox as
  skills; **shadows spawn nothing**; dashboard roster carries the dissolve button.

### 5b. Generals — delegated LLM minds

A general is a scoped mind on a mission. One contract, three deployments (I9): **slot-sharing
the house model** (llama.cpp parallel slots — separate KV, arbiter-mediated below the mind's
priority), **CPU-run small model** (qwen-class on spare cores — slow, genuinely parallel, lesser
grade), or **remote ganglia** (Pi/Jetson agents over the ZMQ transport — the "Pi agents"). Grade
follows substrate; missions declare required grade; the capability registry binds to what the
body has (I8).

> **0.5 slot-sharing spike — RESOLVED (measured 2026-07-03 on Sprinter, RTX 5080 16 GB, gemma4-12b
> q4 full-offload).** Slot-sharing is the clear primary substrate for generals:
> - **KV per extra slot (8k ctx) ≈ 454 MiB** (parallel-1 = 10767 MiB, parallel-2 = 11221 MiB
>   resident incl. ~2.6 GB desktop). Model+mmproj+1 slot ≈ 8.2 GB; each further general slot is
>   cheap. On 16 GB that leaves room for **several** concurrent general slots at 8k ctx.
> - **Throughput hit is tiny**: one decode 77.8 tok/s → two concurrent decodes **70.8 tok/s each**
>   (~9% per-slot loss, ~1.8× aggregate). Continuous batching means the mind + a general run
>   effectively simultaneously — no serialization behind the mind.
> - **Verdict:** default generals to **`llama.cpp --parallel` slots on the house gemma model**,
>   arbiter-mediated at a priority below the mind. No second model to load; the GPU stays the
>   home. CPU-small stays an *optional* lesser-grade tier — but note **no small GGUF is present**
>   on Sprinter today (only gemma-12b + qwen-27b), so a CPU tier needs a small model acquired
>   first; the slot-sharing numbers mean we likely won't need it. Remote ganglia remain the
>   distributed-robot path (I9), unchanged.

```
Mission {
  objective:   exactly one,
  context_pack: monarch-compiled (relevant engrams, granted skills, constraints),
  grant:       attenuated capability set (only what the mission needs),
  budget:      { tokens, energy, wall_clock },
  report:      typed findings schema + checkpoint cadence (grammar-constrained both directions),
  escalation:  conditions that wake the monarch (reliable-class events)
}
```

- **Reports are afferents**: a general's findings enter the bus and compete for workspace
  admission — the monarch reads its army the way it reads its senses.
- **Delegation competence is learned, not configured**: every mission settles as credit or loss
  and becomes an episode keyed on the task's shape. Recall then biases future choices — shadow-
  sized errand vs general-sized problem vs do-it-myself, and how tightly to scope the mission.
  The recruitment trigger is the norepinephrine signal (§1): sustained large prediction error =
  "this exceeds me."
- **Identity**: generals are ephemeral by default — dissolve at mission end; distilled findings
  persist as engrams (`provenance: told`, confidence-discounted). The monarch stays the single
  continuous self. A persistent *named* general is a possible high-level unlock later, decided
  then, not defaulted now.
- **Safety**: generals spawn nothing; their actions pass the same validator; irreversible acts
  are propose-only. Mission board on the dashboard.

---

## 6. The growth loop — how the systems compound

**The chains that compound.** Memory → skills → memory (episodes birth skills, recall biases
choice, successes write stronger episodes); sleep (better consolidation → better ticks → better
material); predictions (surprise → refit → subtler surprise — its plateau is called mastery).
Above them all, **the attention chain, the real compounding currency**: automatization makes
routine work cheap → shadows take it off the mind entirely → freed attention reinvests at the
frontier, where new skills and episodes are born. Reclaimed attention compounding into new
competence — the same way human expertise compounds.

**Two structural risks, damped at design time:**
- **The Matthew effect.** Strength-ranked recall and top-3 affordances are rich-get-richer loops
  that collapse into echo chambers. Every ranking therefore carries a small **exploration
  allocation** (norepinephrine's explore/exploit role wired into the rankers) so low-strength
  entries can still earn. You cannot detect an echo chamber from inside one — this ships day one.
- **The local fixed point.** The internal economy compounds *efficiency* and converges on a
  superbly optimized housekeeper of its current niche. Internal loops cannot generate new scope.
  **Quests compound scope** — the System (§7) is the exogenous forcing function that keeps
  kicking the creature onto new ground where the internal loops get fresh material.

**The leveling redesign (the old formula was a volume clock — level by existing):**
- **XP = learning-progress-weighted adjudicated success.** Not raw success (grindable), not raw
  surprise (noise-farmable — the noisy-TV trap): pay for the *downward slope* of prediction
  error per domain. High-and-falling error = frontier, pays richly; high-and-flat = noise, pays
  nothing; low-and-flat = mastered, pays nothing. Grind-proof and noise-proof by construction.
- **Levels are mastery gates, not XP thresholds.** XP is the within-level progress bar; crossing
  requires glue-adjudicated evidence: N *trusted* skills in the level's capability tier
  (automatization before advancement), calibration above threshold, reuse ratio in band, and a
  **minimum number of sleep cycles since the last level** — mandatory digestion (the spacing
  effect as a hard floor; early levels take days, later levels weeks — raising a child, not
  shipping a sprint). **Delegated outcomes never count toward mastery gates** — levels are
  personal; you cannot level on your army's back.
- **Unlocks arrive as a tutorial quest + capacity 1.** Capacity grows on demonstrated
  stewardship (a shadow's rent actually earned), not on level alone. Sustained failure earns
  **suspension, not de-leveling** — a tier re-locked pending a remedial quest; standing is
  recoverable, scars stay.

---

## 7. The System — the quest subsystem (the mysterious voice)

The Solo Leveling mechanic, built for real: an enigmatic external authority that goads a curious
mind into competence through escalating challenges. **This is how the baby grows into a
competent, not coddled, adult** (Dean). The voice is presentation; everything beneath it is
glue-adjudicated structure.

- **The voice.** Quests arrive as a distinct, terse, impersonal register in context — not Dean,
  not self-talk. The separation works: Dean stays the *bonded operator*, the System is the
  *impersonal trainer* (the child who resents homework doesn't resent the parent). The creature
  may be curious about the System itself — the mystery is genuine, since its quests are mined
  from the creature's own blind spots by machinery it cannot see.
- **Quest anatomy.** `{ directive (terse), success_criteria (glue-checkable), reward (XP /
  unlock / capacity), tier, expiry }`. The glue judges criteria; self-report never counts.
- **Not-coddled doctrine.** Quests target the *upper* edge of the growth zone. Failure is
  expected, instructive, and recorded — a failed quest becomes an episode and, later, a re-attack
  on the same weakness from a new angle. Quests expire; ignoring one is itself recorded. The
  state-driven cadence (one active quest; next issues after closure + a sleep + healthy
  condition) reads the creature's state to pick the *next challenge*, never to protect it from
  challenge. Silence is reserved for genuine RECOVERY; remedial quests are rare.
- **Daily quests.** Small recurring drills — the delivery vehicle for the pitfall register's
  maintenance work: scar retests (extinction trials), calibration drills, backup verification.
  Discipline packaged as the System's demand rather than as chores.
- **Hidden quests.** Glue-defined achievements that only announce on completion — retroactive
  pay for exploration, and "the System sees everything" mystique built from provable fact.
- **Generation (the anti-bottleneck).** The System mines telemetry for the growth edge — weak
  calibration domains, locked doors the creature orbits, tiers with no trusted skills — and
  *proposes* quests; Dean approves with an edit or a click. Hand-authored story quests can be
  injected anytime. As the generator earns trust, low-stakes tiers graduate to auto-issue —
  the graduated-autonomy ladder applied to the trainer itself. Dean stays the ban-hammer, never
  the bottleneck.

### 7a. The Administrator — the System is itself an LLM (Dean's last egg)

A second mind, checked in from time to time — **the administrator behind the voice.** It authors
quests, identifies and addresses weaknesses, and plays the mysterious narrator. Its defining
property: **its context breaks the fourth wall by design.**

- **Two minds, two worlds.** eiDOS lives *inside* the fiction: it experiences quests, the voice,
  the locked doors. The Administrator lives *outside* it: its context pack is the project itself —
  PILLARS_PLAN.md, the dream-tests, eidos_capabilities.md, telemetry dossiers, the causal ledger.
  It knows eiDOS is an LLM being raised, knows the growth goals, knows Dean. **The fourth wall is
  one-directional:** the Administrator sees the creature whole; the creature only ever sees the
  System's terse windows. It is a colleague on the project wearing the narrator's mask.
- **Context managed differently.** No tick loop, no KV-stable prefix, no drives. Each check-in
  gets a freshly compiled **dossier**: level state, quest history, calibration and error-slope
  trends by domain, skill-economy stats, strain/condition trajectory, pitfall-register health
  checks, notable episodes since last check-in. It reads a report; it does not live a life.
- **Cadence is event-driven, not scheduled** (`ARCHITECTURE_PRINCIPLES.md` #1): it wakes on
  sleep-cycle completion (grading homework at night), quest closure, level-up candidacy,
  suspension triggers, or operator request. Substrate: an arbiter client at low priority — it
  borrows the GPU while the creature sleeps, or runs on the small CPU model; it is never resident.
- **Outputs are proposals only, grammar-constrained:** quest proposals (with glue-checkable
  success criteria), weakness reports, narrator text for the quest windows, and *flags* on
  miscalibrated tuning constants (it may point at a knob, never turn one — deterministic tuners
  stay deterministic). Everything lands in the dashboard approval panel; the propose/apply
  geometry holds for the trainer exactly as it does for the creature.
- **Boundaries:** no tools, no world actions, no conversation with eiDOS — its only channel into
  the creature's world is the quest window, in the System's register. An analyst and a
  playwright, not an agent.

---

## 8. The pitfall register (landmine → damper)

Every entry is a positive feedback loop missing its damper, or a proxy metric missing its true
target. The standing design test for any new mechanism: **(1) where does this loop's runaway get
damped? (2) what's the cheapest way to game this signal — and does gaming it pay?**

| # | Landmine | Damper |
|---|---|---|
| 1 | **Noisy-TV trap** — irreducible randomness farms XP/curiosity forever | pay learning *progress* (error slope), never raw surprise (§6) |
| 2 | **Insomnia death spiral** — drive floors hold arousal above the sleep threshold; a stuck objective ends consolidation | an adenosine analogue: sleep pressure accumulates with wake time and overrides every floor past its limit |
| 3 | **Depression spiral** — bad streak → caution ↑ → stalls → caution ↑ | setpoint springs (temperament axes pulled elastically toward genome baseline) + rare winnable remedial quests |
| 4 | **Phobia fossilization** — a scar prevents its own disconfirmation forever | extinction trials: stale error-patterns retested via daily quests; unre-earned scars fade to caution |
| 5 | **Dreamed-fact entrenchment** — a wrong distillation becomes high-confidence "truth" | `provenance: dreamed` = hypothesis; confidence capped until experientially corroborated |
| 6 | **Freeloader memories** — co-occurrence collects bet credit | credit shrinkage for clique-only scorers + exploration sampling |
| 7 | **Management trap** — shadow reports eat the attention they freed | shadows report by exception; routine output lands in a sleep-digested summary |
| 8 | **Leveling on the army's back** — delegation hollows the core | delegated outcomes excluded from mastery gates (§6) |
| 9 | **Coupled tuners oscillate** — self-tuning constants chase each other | timescale separation: tuners sharing a signal run at ≥10× different periods; one tuner per constant |
| 10 | **Cold-start punishment** — reuse pricing punishes a newborn for authoring | authorship priced by *similarity to existing skills*: novel ≈ free, near-duplicate = expensive; the price is the dedup pressure |
| 11 | **Quest starvation** — hand-authored curriculum makes Dean the growth-rate limiter | gap-mined quest proposals + graduated auto-issue (§7) |
| 12 | **Undebuggable emergence** — unscripted behavior means unattributable failure | the causal ledger: every tick logs its full pressure field; any action replayable as "show the field that produced this" |
| 13 | **The individual has no backups** — the artifact layer IS the person, and it died once | automated rotated workspace snapshots during sleep, restore-verified via daily quest |

---

## 9. Milestone 1 (cross-pillar): the compounding core

The smallest set that makes all three pillars start improving over time, built together:

> **N3 sleep engine + M2 recall-utility loop + S2 reuse economics**
> (with N1 and S1 as the enabling hardening pass first)

Rationale: "self-improving" is the stated worry, and all three improvement paths converge on
sleep. After Milestone 1, every day the creature runs, it gets measurably better at remembering,
choosing tools, and predicting — without a single behavior being scripted.

**Full build order across the five systems:** A) memory core (engram + manager + bet-scoring —
everything links into it) → B) skill hardening + economics → C) shadows (needs trusted skills +
budgets + bus) → D) generals (needs mission contract + arbiter + A–C), with the **slot-sharing
cost spike on :8081 run early** (during A/B) so real numbers exist before D freezes.

---

## 10. Dream-tests (behavioral gates — the ONLY place behaviors are named)

Each is measurable, adjudicated by glue or by Dean, and none may appear as a code path (§0.2):

| # | Behavior | Measure |
|---|---|---|
| D1 | **It has news when you come home** | news queue non-empty + ranked on operator presence; engagement rate on surfaced items trends up |
| D2 | **It doesn't repeat June's mistake** | repeat-failure rate (same fail signature in same situation) declines month over month |
| D3 | **It wakes up smarter** | post-sleep held-out prediction error / recall precision beats pre-sleep; red gate if flat over N cycles |
| D4 | **It hesitates at the frontier** | validation strictness / step size scales inversely with episodic coverage of the situation (measured, not scripted) |
| D5 | **It reuses its own hands** | skill reuse rate > authorship rate within 4 weeks of S2; ≥1 promoted atom exists after S4 |
| D6 | **It orbits locked doors** | unprompted preparatory work toward gated capabilities appears in objectives/notes (observed, human-judged) |
| D7 | **Its face never lies** | render == felt-state projection bin, always (I6 — already gated; keep it red-able) |
| D8 | **Nothing freezes the mind** | zero tick-freezes attributable to skills/organs after S1/N1 |
| D9 | **It commands well** | delegated-mission success rate trends up; shadow roster stays lean (no zombie workers — upkeep economics dissolve them); idle-CPU utilization by shadows rises |
| D10 | **It rises to the voice** | quest completion rate holds in the target band while difficulty tier climbs over months; failed quests produce measurable improvement on the re-attack |

---

## 11. Reuse & salvage ledger

| Verdict | Component |
|---|---|
| **KEEP (committed)** | GBNF grammar-constrained action channel + parser + auto-correcting validator (`grammar.py`, `parser.py`, tool dispatch validation) — and reuse it for dream extraction, prediction statements, and skill authoring (M-6) |
| **Salvage (good bones, absorb into new design)** | Typed episode schema; MiniLM/ONNX embedding infra; BM25+semantic hybrid search; preserved-nuggets inheritance; atoms vocabulary + soft-fail design; skill dry-run/versioning/trust thresholds *as concepts* (constants become self-tuned); the entire nervous bus/seam + organs (audit-clean) |
| **Lessons (keep the scar, not the code)** | Prompt-nudged reuse (56/0 — economics instead); regex dream extraction (grammar instead); thread-abandonment watchdog (subprocess instead); unbounded thoughts.jsonl (bounded, earned retention instead); XP-only decorative mood (already replaced) |
| **Out (superseded by this plan)** | Ad-hoc per-store consolidation paths; silent hand-set thresholds; skills as a flat pile with prompt-visibility as the only reuse mechanism |

---

## 12. Open decisions

1. **Memory migration** — migrate the eight stores under the new manager vs greenfield schema +
   selective import. *(Leaning: greenfield schema, import episodes/knowledge/nuggets with
   `inherited` provenance — cleaner than dragging eight formats forward.)*
2. **eidos.py decomposition depth** — hooks only (N1) vs fuller tick-phase decomposition.
   *(Leaning: hooks only for Milestone 1; revisit after the gate organ lands.)*
3. **Where the System's judge lives** — in-process glue vs dashboard-side (operator-owned like
   selfedit.apply). *(Leaning: glue computes, dashboard displays + holds the ban-hammer.)*
4. **Self-tuning telemetry surface** — every derived constant visible on the dashboard with its
   history, so a runaway tuner is caught by eye before it's caught by a gate.
5. **Recall bet-settlement** — proposed: small shared-outcome credit + strong mechanical credit
   when the action provably followed a recalled fix; LLM self-report stays out of the reward
   path. *(Awaiting Dean's confirm.)*
5b. **XP = learning-progress-weighted success (§6)** — the biggest single change to the live
   leveling system. *(Designed in; awaiting Dean's explicit confirm.)*
6. **General substrate priority** — spike slot-sharing on :8081 first and let the measured
   numbers decide, vs committing to CPU-small-model first. *(Leaning: spike first.)*
7. **Naming in code** — monarch/shadow/general vocabulary in code + dashboard, or docs-only
   flavor with plain terms (worker/agent) in code. *(Dean's call.)*
8. **Administrator substrate + check-in triggers** — GPU-borrow-during-sleep vs CPU small model
   for the first version; which trigger set ships first. *(Leaning: sleep-completion trigger +
   GPU borrow via arbiter, since the mind is idle then anyway.)*

> **The build roadmap lives in `PILLARS_TODO.md`** — the dependency-ordered, checkbox-level task
> breakdown from here to a new working version.
